"""Expression-index identity in the shared schema introspection (PR #366).

``PRAGMA index_xinfo`` reports every expression key with a NULL name, so before this change
``COALESCE(topic_id, 0)``, ``topic_id + 0`` and ``NULLIF(topic_id, topic_id)`` produced the same
shape. These tests pin the behaviour of ``table_shape`` / ``statement_identity`` on throw-away
in-memory databases; the updater-level refusal is in ``tests/test_update_canonical_content.py``.
"""

import io
import re
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from src import canonical_parity, manage_migrations
from src import schema_introspection as si
from src.canonical_tables import MIGRATIONS_DIR
from tests.schema_snapshot import compare, snapshot

REAL_INDEX = ("CREATE UNIQUE INDEX uniq_open ON sessions(user_id, section_id, COALESCE(topic_id, 0)) "
              "WHERE submitted_at IS NULL AND abandoned_at IS NULL")


# The ordinary-index path exactly as it was at the base commit (30e6b9e): the oracle that the
# fix leaves every non-expression shape byte for byte alone.
def base_normalize_sql(sql):
    sql = re.sub(r"--[^\n]*", "", sql or "")
    sql = re.sub(r"\s+", " ", sql).strip()
    return re.sub(r"\s*([(),])\s*", r"\1", sql)


def base_index_shape(conn, name, unique, origin, partial):
    rows = conn.execute(f'PRAGMA index_xinfo("{name}")').fetchall()
    key_columns = tuple((row[2], row[3], row[4]) for row in rows if row[5])
    predicate = None
    if partial:
        sql = conn.execute("SELECT sql FROM sqlite_master WHERE name = ?", (name,)).fetchone()[0]
        predicate = base_normalize_sql(re.split(r"\bWHERE\b", sql, maxsplit=1, flags=re.IGNORECASE)[1])
    return {"unique": bool(unique), "origin": origin, "columns": key_columns, "where": predicate}


def database(*statements):
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE sessions (id INTEGER PRIMARY KEY, user_id INTEGER, section_id INTEGER, "
                 "topic_id INTEGER, name TEXT, note TEXT, submitted_at TEXT, abandoned_at TEXT)")
    for statement in statements:
        conn.execute(statement)
    return conn


def index_shape(statement, name="ix"):
    with closing(database(statement)) as conn:
        return si.table_shape(conn, "sessions")["indexes"][name]


def expression_index(expression, *, where="", name="ix"):
    return index_shape(f"CREATE INDEX {name} ON sessions({expression}){where}", name)


class StatementIdentityTest(unittest.TestCase):
    def test_layout_outside_quotes_is_ignored(self):
        plain = "CREATE INDEX ix ON sessions(a, COALESCE(b, 0)) WHERE c IS NULL"
        for variant in (
            "CREATE  INDEX\tix\nON sessions ( a ,COALESCE ( b ,0 ) )\n  WHERE   c   IS NULL",
            "CREATE INDEX ix /* why */ ON sessions(a, -- note\n COALESCE(b, 0)) WHERE c IS NULL -- end",
            "CREATE INDEX ix ON sessions(a,COALESCE(b,0)) WHERE c IS NULL ",
        ):
            with self.subTest(variant=variant):
                self.assertEqual(si.statement_identity(variant), si.statement_identity(plain))

    def test_a_comment_separates_tokens_like_whitespace(self):
        self.assertEqual(si.statement_identity("a/**/b"), si.statement_identity("a b"))
        self.assertNotEqual(si.statement_identity("a/**/b"), si.statement_identity("ab"))

    def test_different_expressions_and_case_stay_distinct(self):
        base = "CREATE INDEX ix ON sessions(COALESCE(topic_id, 0))"
        for other in ("CREATE INDEX ix ON sessions(topic_id + 0)",
                      "CREATE INDEX ix ON sessions(NULLIF(topic_id, topic_id))",
                      "CREATE INDEX ix ON sessions(COALESCE(topic_id, -1))",
                      "CREATE INDEX ix ON sessions(coalesce(topic_id, 0))",       # conservative: case matters
                      "CREATE INDEX ix ON sessions(COALESCE(topic_id, 0), id)"):
            with self.subTest(other=other):
                self.assertNotEqual(si.statement_identity(other), si.statement_identity(base))

    def test_quoted_content_is_kept_byte_for_byte(self):
        def identity(literal):
            return si.statement_identity(f"CREATE INDEX ix ON t(COALESCE(a, {literal}))")
        pairs = [
            ("'a b'", "'a  b'"),            # inner whitespace
            ("'a b'", "'a\tb'"),
            ("'a''b'", "'a'' b'"),          # escaped quote
            ("'--x'", "'-- x'"),            # comment marker inside a literal
            ("'/*x*/'", "'/* x */'"),
            ("'(a)'", "'( a )'"),           # punctuation tolerance must not reach inside
            ("'a,b'", "'a, b'"),
            ("'WHERE x'", "'WHERE y'"),
            ("'a'", "'b'"),
            ("'it''s'", "'its'"),
            ('"a b"', '"a  b"'),            # quoted identifier
            ('"a""b"', '"a"" b"'),
            ("`a b`", "`a  b`"),
            ("[a b]", "[a  b]"),
            ("'a''b'", "'a' 'b'"),          # an escape is not two literals
        ]
        for left, right in pairs:
            with self.subTest(left=left, right=right):
                self.assertNotEqual(identity(left), identity(right))
        for literal in ("'a  b'", "'--x'", "'/*x*/'", "'it''s'", '"a  b"', "[a  b]"):
            with self.subTest(literal=literal):
                self.assertIn(literal, identity(literal))

    def test_comment_markers_in_quotes_do_not_swallow_following_text(self):
        one = si.statement_identity("CREATE INDEX ix ON t(COALESCE(a, '--')) WHERE b IS NULL")
        two = si.statement_identity("CREATE INDEX ix ON t(COALESCE(a, '--')) WHERE b IS NOT NULL")
        self.assertNotEqual(one, two)
        self.assertTrue(one.endswith("WHERE b IS NULL"))
        block = si.statement_identity("CREATE INDEX ix ON t(COALESCE(a, '/*')) WHERE b = '*/' AND c")
        self.assertTrue(block.endswith("WHERE b = '*/' AND c"))

    def test_nested_parentheses_are_kept(self):
        one = si.statement_identity("CREATE INDEX ix ON t((a + (b * c)) % 7)")
        two = si.statement_identity("CREATE INDEX ix ON t((a + b) * c % 7)")
        self.assertNotEqual(one, two)
        self.assertEqual(si.statement_identity("CREATE INDEX ix ON t ( ( a + ( b * c ) ) % 7 )"), one)
        # spacing around operators is not normalised: the identity errs on the side of "different"
        self.assertNotEqual(si.statement_identity("CREATE INDEX ix ON t((a+(b*c))%7)"), one)

    def test_unusable_input_fails_closed(self):
        for sql in (None, "", "   ", "CREATE INDEX ix ON t(COALESCE(a, 'oops))",
                    'CREATE INDEX ix ON t("oops)', "CREATE INDEX ix ON t([oops)",
                    "CREATE INDEX ix ON t(a) /* never closed"):
            with self.subTest(sql=sql), self.assertRaises(si.ExpressionIdentityError):
                si.statement_identity(sql)

    def test_only_sqlite_layout_characters_are_whitespace(self):
        # U+00A0 and friends are identifier characters in SQLite; Python's \\s would swallow them
        for char in ("\u00a0", "\u2003", "\u0085", "\u3000", "\x0b", "\x1c"):
            with self.subTest(char=repr(char)):
                for template in ("COALESCE(a{c}, 0)", "{c}a", "a{c}", "a{c}b"):
                    one = si.statement_identity("CREATE INDEX ix ON t(" + template.format(c=char) + ")")
                    self.assertNotEqual(one, si.statement_identity("CREATE INDEX ix ON t(" + template.format(c="") + ")"))
                    self.assertNotEqual(one, si.statement_identity("CREATE INDEX ix ON t(" + template.format(c=" ") + ")"))
        self.assertNotEqual(si.statement_identity("x\u00a0"), si.statement_identity("x"))             # at the edge
        self.assertNotEqual(si.statement_identity("\u00a0x"), si.statement_identity("x"))
        self.assertEqual(si.statement_identity(" \t\n\f\rx \t\n\f\r"), "x")                       # SQLite whitespace is trimmed
        self.assertEqual(si.statement_identity("a \t\n\f\r b"), "a b")
        self.assertEqual(si.partial_predicate("CREATE INDEX i ON t(a+0) WHERE f\u00a0"), "f\u00a0")

    def test_a_partial_predicate_is_found_outside_comments_quotes_and_parentheses(self):
        for sql, expected in (
            ("CREATE INDEX i ON t(a+0) WHERE x IS NULL", "x IS NULL"),
            ("CREATE INDEX i ON t(a+0) where x IS NULL", "x IS NULL"),
            ("CREATE INDEX i ON t(a+0) /* WHERE decoy */ WHERE x IS NULL", "x IS NULL"),
            ("CREATE INDEX i ON t(a+0) -- WHERE decoy\n WHERE x IS NULL", "x IS NULL"),
            ("CREATE INDEX i /* WHERE */ ON t(COALESCE(a, 'WHERE')) WHERE x IS NULL", "x IS NULL"),
            ("CREATE INDEX i ON t(COALESCE(\"where\", 'a WHERE b')) WHERE \"where\" IS NOT NULL", '"where" IS NOT NULL'),
            ("CREATE INDEX i ON t([where], a+0) WHERE [where] = 'WHERE'", "[where] = 'WHERE'"),
            ("CREATE INDEX i ON t(a+0) WHERE x = 'a  b' /* WHERE */ AND y", "x = 'a  b'  AND y".replace("  AND", " AND")),
            ("CREATE INDEX i ON t(a+0) WHERE x IS NULL -- trailing WHERE", "x IS NULL"),
            ("CREATE INDEX i ON t(a+0) WHERE\n  (x IS NULL)\n", "(x IS NULL)"),
            ("CREATE INDEX i ON t(a+0) WHERE x IS NULL AND y IN (SELECT 1)", "x IS NULL AND y IN(SELECT 1)"),
            ("CREATE INDEX i ON t(a+0) WHERE x = 'WHERE y'", "x = 'WHERE y'"),
            ("CREATE INDEX i ON t(a+0) WHERE WHEREVER IS NULL", "WHEREVER IS NULL"),
        ):
            with self.subTest(sql=sql):
                self.assertEqual(si.partial_predicate(sql), expected)
        for sql in ("CREATE INDEX i ON t(a+0)", "CREATE INDEX i ON t(a+0) /* WHERE x */",
                    "CREATE INDEX i ON t(a+0) -- WHERE x", "CREATE INDEX i ON t(COALESCE(a, 'WHERE'))",
                    "CREATE INDEX i ON t(a+0) WHERE x = 'oops", "CREATE INDEX i ON t(a+0) WHERE x /* oops"):
            with self.subTest(sql=sql), self.assertRaises(si.ExpressionIdentityError):
                si.partial_predicate(sql)
        # WHERE that is a prefix or suffix of a longer name is not the keyword
        self.assertEqual(si.partial_predicate("CREATE INDEX i ON t(a+0) WHERE NOWHERE = 1 AND WHERE\u00a0X = 2"),
                         "NOWHERE = 1 AND WHERE\u00a0X = 2")

    def test_a_bracket_quote_has_no_escape(self):
        # [a]] ends at the first "]"; a following "]" is ordinary text, so this is two segments.
        self.assertEqual(si.statement_identity("x [a]] y"), "x [a]] y")

    def test_normalize_sql_is_unchanged(self):
        for sql in ("a  b -- c\n d", "f ( x , y )", None, "'a  b'", "x /* c */ y"):
            with self.subTest(sql=sql):
                self.assertEqual(si.normalize_sql(sql), base_normalize_sql(sql))
        self.assertEqual(si.normalize_sql("'a  b'"), "'a b'")                    # the known, untouched limit


class ExpressionShapeTest(unittest.TestCase):
    def test_the_reproduced_wrong_expressions_do_not_equal_the_real_one(self):
        real = index_shape(REAL_INDEX, "uniq_open")
        wrong = {
            "plus zero": REAL_INDEX.replace("COALESCE(topic_id, 0)", "topic_id + 0"),
            "nullif": REAL_INDEX.replace("COALESCE(topic_id, 0)", "NULLIF(topic_id, topic_id)"),
            "other sentinel": REAL_INDEX.replace("COALESCE(topic_id, 0)", "COALESCE(topic_id, -1)"),
        }
        for label, statement in wrong.items():
            with self.subTest(label):
                shape = index_shape(statement, "uniq_open")
                self.assertNotEqual(shape, real)
                # everything the old comparison looked at is identical: this is the gap being closed
                self.assertEqual({k: v for k, v in shape.items() if k != "expression_identity"},
                                 {k: v for k, v in real.items() if k != "expression_identity"})

    def test_an_identical_recreated_index_matches_whatever_its_layout(self):
        real = index_shape(REAL_INDEX, "uniq_open")
        relaid = ("CREATE UNIQUE INDEX uniq_open ON sessions ( user_id,section_id ,\n  COALESCE( topic_id , 0 ) )"
                  "  -- same key\n WHERE submitted_at IS NULL AND abandoned_at IS NULL")
        self.assertEqual(index_shape(relaid, "uniq_open"), real)
        self.assertEqual(index_shape(REAL_INDEX, "uniq_open"), real)

    def test_mixed_column_and_expression_keys(self):
        one = index_shape("CREATE INDEX ix ON sessions(user_id, COALESCE(topic_id, 0), section_id DESC)")
        self.assertEqual([c[0] for c in one["columns"]], ["user_id", None, "section_id"])
        self.assertEqual(one["columns"][2][1], 1)
        for other in ("CREATE INDEX ix ON sessions(user_id, topic_id + 0, section_id DESC)",
                      "CREATE INDEX ix ON sessions(user_id, COALESCE(topic_id, 0), section_id)",
                      "CREATE INDEX ix ON sessions(COALESCE(topic_id, 0), user_id, section_id DESC)"):
            with self.subTest(other=other):
                self.assertNotEqual(index_shape(other), one)

    def test_two_expression_keys_in_one_index(self):
        one = index_shape("CREATE INDEX ix ON sessions(topic_id + 1, section_id * 2)")
        self.assertEqual(index_shape("CREATE INDEX ix ON sessions( topic_id + 1 , section_id * 2 )"), one)
        self.assertNotEqual(index_shape("CREATE INDEX ix ON sessions(topic_id + 1, section_id * 3)"), one)
        self.assertNotEqual(index_shape("CREATE INDEX ix ON sessions(section_id * 2, topic_id + 1)"), one)

    def test_nested_expressions(self):
        one = expression_index("COALESCE(NULLIF(topic_id, 0), (section_id + (user_id * 2)) % 7)")
        self.assertEqual(expression_index("COALESCE(NULLIF(topic_id,0),(section_id + (user_id * 2)) % 7)"), one)
        self.assertEqual(expression_index("COALESCE ( NULLIF ( topic_id , 0 ) , ( section_id + ( user_id * 2 ) ) % 7 )"),
                         one)
        self.assertNotEqual(expression_index("COALESCE(NULLIF(topic_id, 0), (section_id + user_id) * 2 % 7)"), one)
        self.assertNotEqual(expression_index("COALESCE(NULLIF(topic_id, 1), (section_id + (user_id * 2)) % 7)"), one)

    def test_literals_never_compare_equal_unless_identical(self):
        shapes = {}
        for literal in ("'a b'", "'a  b'", "'a''b'", "'a'' b'", "'--'", "'-- '", "'/*'", "'/* '", "'WHERE'",
                        "'WHERE x'", "'(a)'", "'( a )'", '"a b"', '"a  b"', "''", "' '"):
            shapes.setdefault(repr(expression_index(f"COALESCE(note, {literal})")), []).append(literal)
        self.assertEqual(sorted(len(v) for v in shapes.values()), [1] * 16, shapes)

    def test_where_text_inside_a_literal_is_not_a_predicate_boundary(self):
        one = expression_index("COALESCE(note, 'x WHERE y')", where=" WHERE submitted_at IS NULL")
        two = expression_index("COALESCE(note, 'x WHERE z')", where=" WHERE submitted_at IS NULL")
        three = expression_index("COALESCE(note, 'x WHERE y')", where=" WHERE submitted_at IS NOT NULL")
        self.assertEqual(len({repr(one), repr(two), repr(three)}), 3)
        self.assertEqual(one, expression_index("COALESCE(note, 'x WHERE y')", where="   WHERE  submitted_at  IS NULL"))

    def test_comment_markers_in_literals_are_not_comments(self):
        one = expression_index("COALESCE(note, '--') ", where=" WHERE submitted_at IS NULL")
        two = expression_index("COALESCE(note, '--x') ", where=" WHERE submitted_at IS NULL")
        self.assertNotEqual(one, two)
        self.assertEqual(one, expression_index("COALESCE(note, '--') -- real comment\n",
                                               where=" /* real */ WHERE submitted_at IS NULL"))

    def test_unquoted_identifiers_that_differ_only_by_a_non_breaking_space_stay_distinct(self):
        def shape(expression, where=""):
            with closing(database('CREATE TABLE wide (a, "a\u00a0", flag, "flag\u00a0")',
                                  f"CREATE INDEX ix ON wide({expression}){where}")) as conn:
                return si.table_shape(conn, "wide")["indexes"]["ix"]
        plain, nbsp = shape("COALESCE(a, 0)"), shape("COALESCE(a\u00a0, 0)")
        self.assertNotEqual(plain, nbsp)
        self.assertNotEqual(shape("a + 1"), shape("a\u00a0 + 1"))
        self.assertNotEqual(shape("COALESCE(a, 0)", " WHERE flag"), shape("COALESCE(a, 0)", " WHERE flag\u00a0"))
        self.assertNotEqual(shape("COALESCE(a, 0)", " WHERE flag IS NULL"),
                            shape("COALESCE(a, 0)", " WHERE flag\u00a0 IS NULL"))
        self.assertEqual(shape("COALESCE(a,0)", " WHERE flag"), shape("COALESCE ( a , 0 )", "\n WHERE  flag"))

    def test_partial_expression_indexes_tolerate_comments_with_where_outside_quotes(self):
        key = "COALESCE(NULLIF(topic_id, 0), (section_id + (user_id * 2)) % 7), note COLLATE NOCASE DESC"
        plain = index_shape(f"CREATE UNIQUE INDEX ix ON sessions({key}) WHERE submitted_at IS NULL AND (abandoned_at IS NULL)")
        self.assertEqual(plain["where"], "submitted_at IS NULL AND(abandoned_at IS NULL)")
        for variant in (
            f"CREATE UNIQUE INDEX ix ON sessions({key}) /* WHERE decoy */ WHERE submitted_at IS NULL AND (abandoned_at IS NULL)",
            f"CREATE UNIQUE INDEX ix ON sessions({key}) -- WHERE decoy\n WHERE submitted_at IS NULL AND (abandoned_at IS NULL)",
            f"CREATE UNIQUE INDEX ix /* WHERE */ ON sessions({key}) WHERE /* WHERE */ submitted_at IS NULL AND (abandoned_at IS NULL) -- WHERE",
            f"CREATE UNIQUE INDEX ix ON sessions({key})\n  where  submitted_at IS NULL\n AND ( abandoned_at IS NULL )".replace("where", "WHERE"),
        ):
            with self.subTest(variant=variant):
                self.assertEqual(index_shape(variant), plain)
        for different in (
            f"CREATE UNIQUE INDEX ix ON sessions({key}) /* WHERE decoy */ WHERE submitted_at IS NULL",
            f"CREATE UNIQUE INDEX ix ON sessions({key}) WHERE submitted_at IS NULL OR (abandoned_at IS NULL)",
            f"CREATE UNIQUE INDEX ix ON sessions({key.replace('% 7', '% 8')}) /* WHERE decoy */ WHERE submitted_at IS NULL AND (abandoned_at IS NULL)",
        ):
            with self.subTest(different=different):
                self.assertNotEqual(index_shape(different), plain)

    def test_where_in_quotes_is_not_the_predicate_boundary_of_a_partial_expression_index(self):
        def shape(literal, predicate):
            return index_shape('CREATE INDEX ix ON sessions(COALESCE("name", %s)) WHERE %s' % (literal, predicate))
        one = shape("'x WHERE y'", "note <> 'WHERE'")
        self.assertEqual(one["where"], "note <> 'WHERE'")
        self.assertEqual(one, shape("'x WHERE y'", "note <> /* WHERE */ 'WHERE'"))
        self.assertNotEqual(one, shape("'x WHERE z'", "note <> 'WHERE'"))
        self.assertNotEqual(one, shape("'x WHERE y'", "note <> 'WHERE '"))
        self.assertNotEqual(one, shape("'x WHERE y'", "note <> 'where'"))
        with closing(database('CREATE TABLE q ("where" TEXT, a)',
                              'CREATE INDEX ix ON q(COALESCE("where", \'WHERE\')) WHERE "where" IS NOT NULL')) as conn:
            self.assertEqual(si.table_shape(conn, "q")["indexes"]["ix"]["where"], '"where" IS NOT NULL')

    def test_quoted_index_and_table_names_are_supported(self):
        for statement, name, expected_where in (
            ('CREATE UNIQUE INDEX "open ix" ON "sessions"(COALESCE("topic_id", 0)) WHERE "submitted_at" IS NULL',
             "open ix", '"submitted_at" IS NULL'),
            ("CREATE UNIQUE INDEX [open ix] ON [sessions](COALESCE([topic_id], 0)) WHERE [submitted_at] IS NULL",
             "open ix", "[submitted_at] IS NULL"),
            ("CREATE INDEX `where ix` ON `sessions`(`topic_id` + 1)", "where ix", None),
            ('CREATE INDEX "a -- b" ON "sessions"(topic_id + 1) /* WHERE x */ WHERE note = \'/*\'', "a -- b", "note = '/*'"),
        ):
            with self.subTest(statement=statement):
                shape = index_shape(statement, name)
                self.assertEqual(shape["where"], expected_where)
                self.assertIn(name, shape["expression_identity"].replace("''", ""))
        self.assertNotEqual(index_shape('CREATE INDEX "a b" ON sessions(topic_id + 1)', "a b"),
                            index_shape('CREATE INDEX "a  b" ON sessions(topic_id + 1)', "a  b"))

    def test_the_other_checks_survive_for_expression_indexes(self):
        base = "CREATE UNIQUE INDEX ix ON sessions(COALESCE(topic_id, 0)) WHERE submitted_at IS NULL"
        shape = index_shape(base)
        self.assertTrue(shape["unique"])
        for label, other in {
            "not unique": base.replace("UNIQUE ", ""),
            "wrong predicate": base.replace("submitted_at IS NULL", "abandoned_at IS NULL"),
            "no predicate": base.split(" WHERE")[0],
            "descending": base.replace("0))", "0) DESC)"),
            "collation": base.replace("0))", "0) COLLATE NOCASE)"),
        }.items():
            with self.subTest(label):
                self.assertNotEqual(index_shape(other), shape)
        self.assertEqual(index_shape(base.replace("0))", "0) COLLATE NOCASE)"))["columns"][0][2], "NOCASE")

    def test_the_identity_is_recorded_only_for_expression_indexes(self):
        with closing(database(
                "CREATE INDEX plain ON sessions(user_id)",
                "CREATE UNIQUE INDEX partial ON sessions(user_id, section_id DESC) WHERE submitted_at IS NULL",
                "CREATE INDEX collated ON sessions(name COLLATE NOCASE)",
                "CREATE INDEX computed ON sessions(topic_id + 1)")) as conn:
            indexes = si.table_shape(conn, "sessions")["indexes"]
        self.assertEqual(sorted(name for name, shape in indexes.items() if "expression_identity" in shape),
                         ["computed"])
        self.assertTrue(indexes["computed"]["expression_identity"].startswith("CREATE INDEX computed ON sessions("))

    def test_unusable_statement_text_is_not_compared_as_an_unspecified_expression(self):
        with closing(database("CREATE INDEX ix ON sessions(topic_id + 1)")) as conn:
            conn.execute("PRAGMA writable_schema = ON")
            conn.execute("UPDATE sqlite_master SET sql = 'CREATE INDEX ix ON sessions(topic_id + ''1)' "
                         "WHERE name = 'ix'")
            with self.assertRaises(si.ExpressionIdentityError):
                si.table_shape(conn, "sessions")


class OrdinaryShapesUnchangedTest(unittest.TestCase):
    STATEMENTS = [
        "CREATE INDEX plain ON sessions(user_id)",
        "CREATE UNIQUE INDEX uniq ON sessions(user_id, section_id)",
        "CREATE INDEX ordered ON sessions(user_id DESC, section_id ASC)",
        "CREATE INDEX collated ON sessions(name COLLATE NOCASE, note COLLATE RTRIM DESC)",
        "CREATE UNIQUE INDEX part ON sessions(user_id) WHERE submitted_at IS NULL AND abandoned_at IS NULL",
        "CREATE INDEX part_text ON sessions(user_id) WHERE note = 'a  WHERE  b' -- trailing",
        "CREATE INDEX quoted ON sessions(\"user_id\", [section_id])",
        "CREATE INDEX part_comment ON sessions(user_id) /* WHERE decoy */ WHERE submitted_at IS NULL",
        'CREATE INDEX "quoted name" ON "sessions"("user_id") WHERE "submitted_at" IS NULL',
    ]

    def test_named_and_automatic_shapes_equal_the_base_implementation(self):
        with closing(database(*self.STATEMENTS)) as conn:
            conn.execute("CREATE TABLE tabled (a, b, c, UNIQUE (a, b), UNIQUE (c DESC), PRIMARY KEY (b, c)) ")
            conn.execute("CREATE TABLE keyed (k TEXT PRIMARY KEY, v UNIQUE) WITHOUT ROWID")
            checked = 0
            for table in ("sessions", "tabled", "keyed"):
                for _seq, name, unique, origin, partial in conn.execute(f'PRAGMA index_list("{table}")').fetchall():
                    with self.subTest(table=table, index=name):
                        self.assertEqual(si._index_shape(conn, name, unique, origin, partial),
                                         base_index_shape(conn, name, unique, origin, partial))
                        self.assertNotIn("expression_identity", si._index_shape(conn, name, unique, origin, partial))
                    checked += 1
            self.assertGreaterEqual(checked, 14)

    def test_whole_table_shapes_still_contain_exactly_the_base_keys(self):
        with closing(database(*self.STATEMENTS)) as conn:
            shape = si.table_shape(conn, "sessions")
        self.assertEqual(sorted(shape), ["auto_indexes", "autoincrement", "checks", "columns", "foreign_keys",
                                         "indexes", "without_rowid"])
        for index in shape["indexes"].values():
            self.assertEqual(sorted(index), ["columns", "origin", "unique", "where"])


class SharedConsumersTest(unittest.TestCase):
    """The fix lives in the shared helper, so every consumer of ``table_shape`` sees it."""

    def pair(self, real_sql, wrong_sql):
        left, right = database(real_sql), database(wrong_sql)
        self.addCleanup(left.close)
        self.addCleanup(right.close)
        return left, right

    def test_snapshot_comparison_reports_the_wrong_expression(self):
        left, right = self.pair(REAL_INDEX, REAL_INDEX.replace("COALESCE(topic_id, 0)", "topic_id + 0"))
        problems = compare(snapshot(left), snapshot(right), "real", "wrong")
        self.assertEqual(len(problems), 1, problems)
        self.assertTrue(problems[0].startswith("index uniq_open:"), problems)
        same_left, same_right = self.pair(REAL_INDEX, REAL_INDEX.replace(", ", ",  "))
        self.assertEqual(compare(snapshot(same_left), snapshot(same_right)), [])

    def test_baseline_requirement_check_reports_the_wrong_expression(self):
        expected, wrong = self.pair(REAL_INDEX, REAL_INDEX.replace("COALESCE(topic_id, 0)", "NULLIF(topic_id, topic_id)"))
        wanted = si.table_shape(expected, "sessions")
        self.assertEqual(si.shape_requirement_problems(wanted, si.table_shape(expected, "sessions")), [])
        self.assertEqual(si.shape_requirement_problems(wanted, si.table_shape(wrong, "sessions")),
                         ["index uniq_open is missing or differs"])

    def test_canonical_parity_reports_the_wrong_expression_on_a_migrated_database(self):
        with tempfile.TemporaryDirectory(prefix="learnpilot-introspection-") as directory:
            path = Path(directory) / "migrated.db"
            sqlite3.connect(path).close()
            self.assertEqual(manage_migrations.apply_migrations(path, MIGRATIONS_DIR, out=io.StringIO()), 0)
            with closing(sqlite3.connect(path)) as conn:
                self.assertEqual(canonical_parity.schema_parity_problems(conn), [])
                (sql,) = conn.execute("SELECT sql FROM sqlite_master WHERE name = "
                                      "'uniq_practice_sessions_open_scope'").fetchone()
                self.assertIn("COALESCE(topic_id, 0)", sql)
                conn.execute("DROP INDEX uniq_practice_sessions_open_scope")
                conn.execute(sql.replace("COALESCE(topic_id, 0)", "topic_id + 0"))
                self.assertEqual(canonical_parity.schema_parity_problems(conn),
                                 ["table practice_sessions: shape differs from the migrated schema"])
                conn.execute("DROP INDEX uniq_practice_sessions_open_scope")
                conn.execute(re.sub(r"\s+", "  ", sql))              # same statement, different layout
                self.assertEqual(canonical_parity.schema_parity_problems(conn), [])


if __name__ == "__main__":
    unittest.main()
