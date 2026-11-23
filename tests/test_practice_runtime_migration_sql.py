"""026 adds the student practice runtime storage (C2.1; docs/STUDENT_PRACTICE_DESIGN.md).

Two new tables, ``practice_sessions`` and ``practice_answers``, and two indexes on
them. **No row is written**, no existing table is rebuilt or altered, no legacy
attempt is backfilled. The tables are runtime state: never canonical, never exported.

What this module proves is the *schema*: columns, foreign keys, row-local CHECK
constraints, the open-scope unique index, the additive upgrade from a populated
025 database, the fresh-build/export parity, and that both runtime question-reference
validators see the new columns. It does **not** prove who may start or read a
session, JSON shape, topic-in-section membership, question eligibility, scoring,
state transitions or concurrent starts: those are later service/HTTP slices.

Constraint tests come with negative controls: the same bad row is applied to a copy of the
migration with exactly that constraint edited out, and must then be accepted. A test that
the real schema rejects a row therefore cannot pass for an unrelated reason (a foreign key,
another CHECK, a typo in the row).

Every database here is temporary: built by the shared migration runner from the repository
files, or by ``tests.support.build_test_database``. None is ``data/questions.db``.
"""

import io
import json
import shutil
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from src import canonical_export as export
from src import canonical_parity as parity
from src import canonical_tables as ct
from src import manage_migrations
from src import update_canonical_content as tool
from tests.support import ROOT, build_test_database
from tests.test_assignment_public_token_migration_sql import POPULATE_SQL
from tests.test_user_identities_migration_sql import ACCOUNTS_SQL

MIGRATIONS = ROOT / "web" / "migrations"
MIGRATION = "026_practice_runtime.sql"
SESSIONS, ANSWERS = "practice_sessions", "practice_answers"
OPEN_INDEX, SCOPE_INDEX = "uniq_practice_sessions_open_scope", "idx_practice_sessions_user_scope"
STAMP = "2026-11-20 10:00:00"

# On top of POPULATE_SQL (section 11, questions 21, users 51 teacher / 52 student, attempts) and
# ACCOUNTS_SQL (61 admin, 62 disabled teacher): two topics of section 11 and a second question.
BASE_SQL = POPULATE_SQL + ACCOUNTS_SQL + """
INSERT INTO curriculum_topics (id, topic_slug, title_bg, section_id) VALUES
    (31, 'topic-31', 'Тема 31', 11), (32, 'topic-32', 'Тема 32', 11);
INSERT INTO curriculum_sections (id, section_slug, title_bg, class, section_type)
    VALUES (12, 'second', 'Втори раздел', 12, 'section');
INSERT INTO questions (id, source_exam, question_type, points, prompt)
    VALUES (22, 'legacy', 'short_answer', 3, 'Втори въпрос');
"""

SESSION_COLUMNS = ("id", "user_id", "section_id", "topic_id", "seed", "question_ids_json", "started_at",
                   "submitted_at", "abandoned_at", "score_correct", "score_total")


def apply(path, migrations_dir=MIGRATIONS):
    out = io.StringIO()
    code = manage_migrations.apply_migrations(path, migrations_dir, out=out)
    return code, out.getvalue()


def migration_text() -> str:
    return (MIGRATIONS / MIGRATION).read_text(encoding="utf-8")


def statements() -> str:
    """The migration without its comments."""
    return "\n".join(line for line in migration_text().splitlines() if not line.lstrip().startswith("--"))


def add_session(conn, **override):
    values = {"id": None, "user_id": 52, "section_id": 11, "topic_id": None, "seed": "seed",
              "question_ids_json": "[21, 22]", "started_at": STAMP, "submitted_at": None,
              "abandoned_at": None, "score_correct": None, "score_total": None}
    unknown = set(override) - set(values)
    assert not unknown, unknown
    values.update(override)
    conn.execute(f"INSERT INTO {SESSIONS} ({', '.join(SESSION_COLUMNS)}) VALUES ({', '.join('?' for _ in SESSION_COLUMNS)})",
                 [values[name] for name in SESSION_COLUMNS])


def add_answer(conn, session_id=1, question_id=21, chosen_letter="A", is_correct=1):
    conn.execute(f"INSERT INTO {ANSWERS} (session_id, question_id, chosen_letter, is_correct) VALUES (?, ?, ?, ?)",
                 (session_id, question_id, chosen_letter, is_correct))


class PopulatedAt025(unittest.TestCase):
    """One database at migration 025 with legacy rows; every test works on a private copy of it."""

    @classmethod
    def setUpClass(cls):
        cls._directory = tempfile.TemporaryDirectory(prefix="practice-migration-")
        cls.root = Path(cls._directory.name)
        previous = cls.root / "through-025"
        previous.mkdir()
        for path in sorted(MIGRATIONS.glob("*.sql")):
            if path.name < MIGRATION:
                shutil.copyfile(path, previous / path.name)
        # The real runner over a directory that ends at 026: the 025-to-026 upgrade test stays exact, whatever
        # migration is added later.
        cls.through_026 = cls.root / "through-026"
        cls.through_026.mkdir()
        for path in sorted(MIGRATIONS.glob("*.sql")):
            if path.name <= MIGRATION:
                shutil.copyfile(path, cls.through_026 / path.name)
        cls.template = cls.root / "template-025.db"
        sqlite3.connect(cls.template).close()
        code, report = apply(cls.template, previous)
        assert code == 0, report
        with closing(sqlite3.connect(cls.template)) as conn:
            conn.execute("PRAGMA foreign_keys = ON")
            conn.executescript(BASE_SQL)
            conn.commit()

    @classmethod
    def tearDownClass(cls):
        cls._directory.cleanup()

    def setUp(self):
        self.path = self.root / f"{self.id().rsplit('.', 1)[-1]}.db"
        shutil.copyfile(self.template, self.path)

    def connect(self, foreign_keys=True):
        conn = sqlite3.connect(self.path)
        self.addCleanup(conn.close)
        conn.execute(f"PRAGMA foreign_keys = {'ON' if foreign_keys else 'OFF'}")
        return conn

    def migrate(self, migrations_dir=MIGRATIONS):
        code, report = apply(self.path, migrations_dir)
        self.assertEqual(code, 0, report)
        return report

    def mutated(self, old: str, new: str, foreign_keys=False):
        """A copy of the 025 template with the migration applied after ``old`` was edited to ``new``."""
        text = migration_text()
        self.assertEqual(text.count(old), 1, f"mutation target must occur once: {old!r}")
        path = self.root / f"mutated-{abs(hash((old, new)))}.db"
        shutil.copyfile(self.template, path)
        with closing(sqlite3.connect(path)) as conn:
            conn.executescript(text.replace(old, new))
        conn = sqlite3.connect(path)
        self.addCleanup(conn.close)
        conn.execute(f"PRAGMA foreign_keys = {'ON' if foreign_keys else 'OFF'}")
        return conn


def dump(conn, skip=()):
    return {name: conn.execute(f'SELECT * FROM "{name}" ORDER BY 1, 2').fetchall()
            for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            if name not in skip}


def objects(conn):
    return conn.execute("SELECT type, name, tbl_name, rootpage, sql FROM sqlite_master ORDER BY type, name").fetchall()


class UpgradeTest(PopulatedAt025):
    def test_only_the_practice_objects_and_the_ledger_row_are_added(self):
        conn = self.connect()
        before, objects_before = dump(conn, skip=("schema_migrations",)), objects(conn)
        ledger_before = conn.execute("SELECT filename FROM schema_migrations ORDER BY filename").fetchall()
        self.assertGreater(conn.execute("SELECT COUNT(*) FROM quiz_attempts").fetchone()[0], 0)
        self.migrate(self.through_026)               # exactly 025 -> 026: later migrations have their own tests
        conn = self.connect()
        self.assertEqual(dump(conn, skip=("schema_migrations", SESSIONS, ANSWERS)), before)   # every other row, as it was
        added = {row[1] for row in objects(conn) if row not in objects_before}
        self.assertEqual(added, {SESSIONS, ANSWERS, OPEN_INDEX, SCOPE_INDEX, f"sqlite_autoindex_{ANSWERS}_1"})
        self.assertEqual({row[1] for row in objects_before} - {row[1] for row in objects(conn)}, set())
        ledger = conn.execute("SELECT filename FROM schema_migrations ORDER BY filename").fetchall()
        self.assertEqual(ledger, ledger_before + [(MIGRATION,)])
        self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
        self.assertEqual(conn.execute("PRAGMA integrity_check").fetchall(), [("ok",)])

    def test_nothing_is_seeded_backfilled_or_sequenced(self):
        self.migrate()
        conn = self.connect()
        for table in (SESSIONS, ANSWERS):
            self.assertEqual(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0)
        self.assertIsNone(conn.execute("SELECT 1 FROM sqlite_sequence WHERE name = ?", (SESSIONS,)).fetchone())
        # The legacy attempts stay the quiz tables' business.
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM quiz_attempts").fetchone()[0], 2)

    def test_it_is_an_additive_migration_not_a_rebuild(self):
        dry = io.StringIO()
        manage_migrations.dry_run(self.path, MIGRATIONS, out=dry)
        self.assertIn(f"pending migrations:\n  - {MIGRATION}\n", dry.getvalue())
        self.assertIsNone(self.connect().execute("SELECT 1 FROM sqlite_master WHERE name = ?", (SESSIONS,)).fetchone())
        report = self.migrate()
        self.assertIn(f"applied migrations:\n  - {MIGRATION}\n", report)
        self.assertIn("foreign_key_check: ok", report)
        self.assertNotIn("table-rebuild", report)
        self.assertFalse(manage_migrations.is_table_rebuild_migration(MIGRATION, migration_text()))
        sql = statements().upper()
        for forbidden in ("DROP ", "ALTER ", "DELETE FROM", "UPDATE ", "TRIGGER", "INSERT ", "CASCADE"):
            self.assertNotIn(forbidden, sql)
        self.assertEqual(sql.count("CREATE TABLE"), 2)
        self.assertEqual(sql.count("CREATE UNIQUE INDEX") + sql.count("CREATE INDEX"), 2)

    def test_the_runner_is_idempotent(self):
        self.migrate()
        conn = self.connect()
        ledger, schema = conn.execute("SELECT * FROM schema_migrations ORDER BY filename").fetchall(), objects(conn)
        code, report = apply(self.path)
        self.assertEqual(code, 0, report)
        self.assertNotIn(f"applied migrations:\n  - {MIGRATION}", report)
        conn = self.connect()
        self.assertEqual(conn.execute("SELECT * FROM schema_migrations ORDER BY filename").fetchall(), ledger)
        self.assertEqual(objects(conn), schema)

    def test_code_that_predates_the_migration_still_runs(self):
        self.migrate()
        conn = self.connect()
        conn.execute("INSERT INTO quiz_assignments (section_id, title_bg, question_count, owner_user_id) "
                     "VALUES (11, 'Старият код', 1, 51)")
        conn.commit()
        self.assertEqual(conn.execute(f"SELECT COUNT(*) FROM {SESSIONS}").fetchone()[0], 0)


class TableDefinitionTest(PopulatedAt025):
    def setUp(self):
        super().setUp()
        self.migrate()
        self.conn = self.connect()

    def test_session_columns(self):
        columns = [(row[1], row[2], row[3], row[5]) for row in self.conn.execute(f"PRAGMA table_info({SESSIONS})")]
        self.assertEqual(columns, [
            ("id", "INTEGER", 0, 1), ("user_id", "INTEGER", 1, 0), ("section_id", "INTEGER", 1, 0),
            ("topic_id", "INTEGER", 0, 0), ("seed", "TEXT", 1, 0), ("question_ids_json", "TEXT", 1, 0),
            ("started_at", "TEXT", 1, 0), ("submitted_at", "TEXT", 0, 0), ("abandoned_at", "TEXT", 0, 0),
            ("score_correct", "INTEGER", 0, 0), ("score_total", "INTEGER", 0, 0)])
        self.assertIn("AUTOINCREMENT", self.conn.execute(
            "SELECT sql FROM sqlite_master WHERE name = ?", (SESSIONS,)).fetchone()[0].upper())

    def test_answer_columns_have_no_surrogate_id_and_the_pair_is_the_key(self):
        columns = [(row[1], row[2], row[3], row[5]) for row in self.conn.execute(f"PRAGMA table_info({ANSWERS})")]
        self.assertEqual(columns, [("session_id", "INTEGER", 1, 1), ("question_id", "INTEGER", 1, 2),
                                   ("chosen_letter", "TEXT", 0, 0), ("is_correct", "INTEGER", 1, 0)])
        self.assertNotIn("AUTOINCREMENT", self.conn.execute(
            "SELECT sql FROM sqlite_master WHERE name = ?", (ANSWERS,)).fetchone()[0].upper())

    def test_every_foreign_key_is_an_explicit_restrict(self):
        def keys(table):
            return sorted((row[2], row[3], row[4], row[5], row[6])
                          for row in self.conn.execute(f"PRAGMA foreign_key_list({table})"))
        self.assertEqual(keys(SESSIONS), sorted([("users", "user_id", "id", "NO ACTION", "RESTRICT"),
                                                 ("curriculum_sections", "section_id", "id", "NO ACTION", "RESTRICT"),
                                                 ("curriculum_topics", "topic_id", "id", "NO ACTION", "RESTRICT")]))
        self.assertEqual(keys(ANSWERS), sorted([("practice_sessions", "session_id", "id", "NO ACTION", "RESTRICT"),
                                                ("questions", "question_id", "id", "NO ACTION", "RESTRICT")]))

    def test_the_two_indexes_and_their_shape(self):
        indexes = {row[1]: (row[2], row[4]) for row in self.conn.execute(f"PRAGMA index_list({SESSIONS})")}
        self.assertEqual(indexes[OPEN_INDEX], (1, 1))      # unique, partial
        self.assertEqual(indexes[SCOPE_INDEX], (0, 0))
        sql = self.conn.execute("SELECT sql FROM sqlite_master WHERE name = ?", (OPEN_INDEX,)).fetchone()[0]
        self.assertIn("COALESCE(topic_id, 0)", sql)
        self.assertIn("WHERE submitted_at IS NULL AND abandoned_at IS NULL", sql)
        self.assertEqual([row[2] for row in self.conn.execute(f"PRAGMA index_info({SCOPE_INDEX})")],
                         ["user_id", "section_id", "topic_id"])

    def test_the_scope_index_serves_the_owner_scope_lookup(self):
        plan = " ".join(row[3] for row in self.conn.execute(
            f"EXPLAIN QUERY PLAN SELECT id FROM {SESSIONS} WHERE user_id = 52 AND section_id = 11 AND topic_id = 31"))
        self.assertIn(SCOPE_INDEX, plan)

    def test_no_binary_columns_and_no_name_or_question_text(self):
        names = [row[1] for row in self.conn.execute(f"PRAGMA table_info({SESSIONS})")] + \
                [row[1] for row in self.conn.execute(f"PRAGMA table_info({ANSWERS})")]
        for forbidden in ("name", "prompt", "text", "blob", "email"):
            self.assertEqual([n for n in names if forbidden in n], [], forbidden)
        self.assertNotIn("BLOB", {row[2].upper() for row in self.conn.execute(f"PRAGMA table_info({SESSIONS})")})


class SessionConstraintTest(PopulatedAt025):
    """Row-local invariants. Foreign keys are OFF so each case isolates the CHECK or index under test."""

    def setUp(self):
        super().setUp()
        self.migrate()
        self.conn = self.connect(foreign_keys=False)

    def assertRejected(self, conn, **row):
        with self.assertRaises(sqlite3.IntegrityError):
            add_session(conn, **row)

    def check_control(self, edit, rows):
        """Each row is rejected by the real schema and accepted once ``edit`` removes its constraint."""
        old, new = edit
        control = self.mutated(old, new)
        for index, row in enumerate(rows):
            with self.subTest(row=row):
                self.assertRejected(self.conn, **row)
                add_session(control, **dict(row, id=1000 + index))          # the negative control
                control.execute(f"DELETE FROM {SESSIONS}")                  # keep the open-scope key free

    def test_ids_must_be_positive_integers(self):
        self.check_control(("CHECK (typeof(user_id) = 'integer' AND user_id > 0)", "CHECK (1)"),
                           [{"user_id": 0}, {"user_id": -5}])
        self.check_control(("CHECK (typeof(section_id) = 'integer' AND section_id > 0)", "CHECK (1)"),
                           [{"section_id": 0}, {"section_id": -1}])
        self.check_control(("CHECK (topic_id IS NULL OR (typeof(topic_id) = 'integer' AND topic_id > 0))", "CHECK (1)"),
                           [{"topic_id": 0}, {"topic_id": -31}])
        self.check_control(("CHECK (typeof(id) = 'integer' AND id > 0)", "CHECK (1)"),
                           [{"id": 0}, {"id": -1}])
        # And the primary key itself still auto-assigns a positive id.
        add_session(self.conn)
        add_session(self.conn, topic_id=31)
        self.assertEqual([row[0] for row in self.conn.execute(f"SELECT id FROM {SESSIONS} ORDER BY id")], [1, 2])

    def test_required_fields_cannot_be_null_and_blobs_are_refused(self):
        for field in ("user_id", "section_id", "seed", "question_ids_json", "started_at"):
            with self.subTest(null=field):
                self.assertRejected(self.conn, **{field: None})
        for field in ("seed", "question_ids_json", "started_at"):
            with self.subTest(blob=field):
                self.assertRejected(self.conn, **{field: b"[21]"})
        with self.subTest("blob terminal timestamp"):
            self.assertRejected(self.conn, abandoned_at=b"x")
        self.assertEqual(self.conn.execute(f"SELECT COUNT(*) FROM {SESSIONS}").fetchone()[0], 0)

    def test_a_session_is_not_both_submitted_and_abandoned(self):
        self.check_control(("CHECK (submitted_at IS NULL OR abandoned_at IS NULL),", "CHECK (1),"),
                           [{"submitted_at": STAMP, "abandoned_at": STAMP, "score_correct": 1, "score_total": 2}])

    def test_an_unsubmitted_session_has_no_score(self):
        old = "CHECK (submitted_at IS NOT NULL OR (score_correct IS NULL AND score_total IS NULL)),"
        self.check_control((old, "CHECK (1),"), [
            {"score_correct": 1, "score_total": 2},
            {"score_correct": 0, "score_total": 0},
            {"score_total": 2},
            {"score_correct": 0, "abandoned_at": STAMP},
            {"score_correct": 1, "score_total": 2, "abandoned_at": STAMP},
        ])

    def test_a_submitted_score_is_whole_nonnegative_and_bounded(self):
        old = """CHECK (submitted_at IS NULL
           OR (typeof(score_correct) = 'integer' AND typeof(score_total) = 'integer'
               AND score_correct >= 0 AND score_total > 0 AND score_correct <= score_total))"""
        bad = [{"score_correct": None, "score_total": None}, {"score_correct": 1, "score_total": None},
               {"score_correct": None, "score_total": 2}, {"score_correct": -1, "score_total": 2},
               {"score_correct": 3, "score_total": 2}, {"score_correct": 0, "score_total": 0},
               {"score_correct": 0, "score_total": -1}, {"score_correct": 1.5, "score_total": 2},
               {"score_correct": "x", "score_total": 2}]
        self.check_control((old, "CHECK (1)"), [dict(row, submitted_at=STAMP) for row in bad])

    def test_valid_rows_in_every_state_are_accepted(self):
        add_session(self.conn)                                                                    # open, section-wide
        add_session(self.conn, topic_id=31, seed="t")                                             # open, topic
        add_session(self.conn, submitted_at=STAMP, score_correct=0, score_total=1)                # lowest score
        add_session(self.conn, submitted_at=STAMP, score_correct=2, score_total=2)                # full score
        add_session(self.conn, abandoned_at=STAMP)                                                # abandoned
        self.assertEqual(self.conn.execute(f"SELECT COUNT(*) FROM {SESSIONS}").fetchone()[0], 5)

    # --- the open-scope unique index

    def test_two_open_section_sessions_collide_on_the_null_topic(self):
        add_session(self.conn)
        self.assertRejected(self.conn)
        control = self.mutated("(user_id, section_id, COALESCE(topic_id, 0))", "(user_id, section_id, topic_id)")     # a plain key lets NULLs coexist
        add_session(control)
        add_session(control)

    def test_two_open_topic_sessions_in_one_topic_collide(self):
        add_session(self.conn, topic_id=31)
        self.assertRejected(self.conn, topic_id=31)
        control = self.mutated("CREATE UNIQUE INDEX uniq_practice_sessions_open_scope",
                               "CREATE INDEX uniq_practice_sessions_open_scope")      # not unique any more
        add_session(control, topic_id=31)
        add_session(control, topic_id=31)

    def test_other_accounts_scopes_and_topics_do_not_collide(self):
        add_session(self.conn)
        add_session(self.conn, user_id=51)                       # another account, same scope
        add_session(self.conn, section_id=12)                    # another section
        add_session(self.conn, topic_id=31)                      # a topic scope beside the section-wide one
        add_session(self.conn, topic_id=32)                      # another topic
        add_session(self.conn, user_id=51, topic_id=31)
        self.assertEqual(self.conn.execute(f"SELECT COUNT(*) FROM {SESSIONS}").fetchone()[0], 6)

    def test_a_fresh_open_session_may_follow_a_submitted_or_abandoned_one(self):
        for terminal in ({"submitted_at": STAMP, "score_correct": 1, "score_total": 2}, {"abandoned_at": STAMP}):
            with self.subTest(terminal=terminal):
                for topic in (None, 32):
                    add_session(self.conn, topic_id=topic, **terminal)
                    add_session(self.conn, topic_id=topic)         # the fresh open session
                    self.assertRejected(self.conn, topic_id=topic)  # ... and only one
                    self.conn.execute(f"DELETE FROM {SESSIONS} WHERE topic_id IS ? AND user_id = 52", (topic,))
        # Negative control: a predicate that only excludes submitted sessions still counts abandoned ones.
        control = self.mutated("WHERE submitted_at IS NULL AND abandoned_at IS NULL", "WHERE submitted_at IS NULL")
        add_session(control, abandoned_at=STAMP)
        with self.assertRaises(sqlite3.IntegrityError):
            add_session(control)

    def test_closing_a_session_frees_the_scope(self):
        add_session(self.conn, topic_id=31)
        self.conn.execute(f"UPDATE {SESSIONS} SET abandoned_at = ? WHERE topic_id = 31", (STAMP,))
        add_session(self.conn, topic_id=31)
        self.assertEqual(self.conn.execute(f"SELECT COUNT(*) FROM {SESSIONS}").fetchone()[0], 2)
        # Re-opening the abandoned one while another is open would collide; the schema is not a state machine.
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(f"UPDATE {SESSIONS} SET abandoned_at = NULL WHERE abandoned_at IS NOT NULL")

    def test_the_sentinel_cannot_alias_a_real_topic(self):
        # topic_id 0 is what COALESCE would map NULL to: the positive-id CHECK keeps it out of the table.
        self.assertRejected(self.conn, topic_id=0)


class AnswerConstraintTest(PopulatedAt025):
    def setUp(self):
        super().setUp()
        self.migrate()
        self.conn = self.connect(foreign_keys=False)
        add_session(self.conn)

    def assertRejected(self, **row):
        with self.assertRaises(sqlite3.IntegrityError):
            add_answer(self.conn, **row)

    def test_one_answer_per_session_and_question(self):
        add_answer(self.conn)
        self.assertRejected(chosen_letter="B", is_correct=0)
        add_answer(self.conn, question_id=22)                          # another question of the session
        add_session(self.conn, user_id=51)
        add_answer(self.conn, session_id=2)                            # the same question in another session
        control = self.mutated("PRIMARY KEY (session_id, question_id)", "CHECK (1)")
        add_session(control)
        add_answer(control)
        add_answer(control, chosen_letter="B", is_correct=0)           # accepted without the key

    def test_referenced_ids_are_positive_integers(self):
        for field, edit in (("session_id", "CHECK (typeof(session_id) = 'integer' AND session_id > 0)"),
                            ("question_id", "CHECK (typeof(question_id) = 'integer' AND question_id > 0)")):
            control = self.mutated(edit, "CHECK (1)")
            for bad in (0, -1):
                with self.subTest(field=field, value=bad):
                    self.assertRejected(**{field: bad})
                    add_answer(control, **{field: bad})              # control; the FK is off on both

    def test_the_verdict_is_a_required_boolean(self):
        control = self.mutated("CHECK (typeof(is_correct) = 'integer' AND is_correct IN (0, 1))", "CHECK (1)")
        for bad in (None, 2, -1, "yes", 0.5):
            with self.subTest(verdict=bad):
                self.assertRejected(is_correct=bad)
                if bad is not None:                                  # NOT NULL itself stays in the control
                    add_answer(control, is_correct=bad)
                    control.execute(f"DELETE FROM {ANSWERS}")
        for good in (0, 1):
            add_answer(self.conn, question_id=100 + good, is_correct=good)

    def test_the_letter_is_text_or_null_and_not_restricted_to_a_to_d(self):
        for number, letter in enumerate((None, "A", "D", "Д", "E", "a", "", "Z"), start=200):
            add_answer(self.conn, question_id=number, chosen_letter=letter)    # the existing representation, not A-D
        self.assertRejected(question_id=300, chosen_letter=b"A")

    def test_an_unanswered_question_is_representable(self):
        add_answer(self.conn, chosen_letter=None, is_correct=0)
        self.assertEqual(self.conn.execute(f"SELECT chosen_letter, is_correct FROM {ANSWERS}").fetchall(), [(None, 0)])


class ForeignKeyTest(PopulatedAt025):
    """Foreign keys ON: references must resolve, and RESTRICT blocks deleting a referenced parent."""

    def setUp(self):
        super().setUp()
        self.migrate()
        self.conn = self.connect()
        add_session(self.conn, topic_id=31, submitted_at=STAMP, score_correct=1, score_total=2)
        add_answer(self.conn, session_id=1, question_id=21)
        # Parents that ONLY practice rows reference (the legacy fixture uses account 52, section 11 and question 21
        # elsewhere): account 62, section 12 and question 22, so each refusal below is the practice key's alone.
        add_session(self.conn, user_id=62, section_id=12, submitted_at=STAMP, score_correct=1, score_total=1)
        add_answer(self.conn, session_id=2, question_id=22)
        self.conn.commit()

    def test_references_must_resolve(self):
        for row in ({"user_id": 9999}, {"section_id": 9999}, {"topic_id": 9999}):
            with self.subTest(row=row), self.assertRaises(sqlite3.IntegrityError):
                add_session(self.conn, **row)
        for row in ({"session_id": 9999}, {"question_id": 9999}):
            with self.subTest(row=row), self.assertRaises(sqlite3.IntegrityError):
                add_answer(self.conn, **dict({"session_id": 1, "question_id": 22}, **row))

    def test_a_referenced_parent_cannot_be_deleted(self):
        for label, sql in (("account", "DELETE FROM users WHERE id = 62"),
                           ("section", "DELETE FROM curriculum_sections WHERE id = 12"),
                           ("topic", "DELETE FROM curriculum_topics WHERE id = 31"),
                           ("session with answers", f"DELETE FROM {SESSIONS} WHERE id = 2"),
                           ("question with answers", "DELETE FROM questions WHERE id = 22")):
            with self.subTest(label), self.assertRaises(sqlite3.IntegrityError):
                self.conn.execute(sql)
        self.assertEqual(self.conn.execute(f"SELECT COUNT(*) FROM {SESSIONS}").fetchone()[0], 2)
        self.assertEqual(self.conn.execute(f"SELECT COUNT(*) FROM {ANSWERS}").fetchone()[0], 2)

    def test_restrict_is_not_a_retention_policy(self):
        """RESTRICT only refuses while a reference remains; a future cleanup could remove dependants first."""
        self.conn.execute(f"DELETE FROM {ANSWERS}")
        self.conn.execute(f"DELETE FROM {SESSIONS}")
        for sql in ("DELETE FROM curriculum_topics WHERE id = 31", "DELETE FROM users WHERE id = 62",
                    "DELETE FROM curriculum_sections WHERE id = 12", "DELETE FROM questions WHERE id = 22"):
            self.conn.execute(sql)
        self.assertEqual(self.conn.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_without_restrict_a_cascade_would_delete_history(self):
        """Negative control: the RESTRICT tests above are not passing because of a default."""
        control = self.mutated("REFERENCES users(id) ON DELETE RESTRICT", "REFERENCES users(id) ON DELETE CASCADE",
                               foreign_keys=True)
        add_session(control, user_id=62)               # an account nothing else references
        control.execute("DELETE FROM users WHERE id = 62")
        self.assertEqual(control.execute(f"SELECT COUNT(*) FROM {SESSIONS}").fetchone()[0], 0)

    def test_a_null_topic_is_a_valid_section_wide_session(self):
        add_session(self.conn, topic_id=None)               # open, section-wide, beside the NULL-topic session 2
        self.assertEqual(self.conn.execute(f"SELECT COUNT(*) FROM {SESSIONS} WHERE topic_id IS NULL").fetchone()[0], 2)


class FreshBuildAndExportTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._directory = tempfile.TemporaryDirectory(prefix="practice-fresh-")
        cls.root = Path(cls._directory.name)
        cls.fresh = build_test_database(cls.root / "fresh.db")
        upgraded_root = cls.root / "up"
        upgraded_root.mkdir()
        cls.upgraded = upgraded_root / "u.db"
        sqlite3.connect(cls.upgraded).close()
        assert apply(cls.upgraded)[0] == 0

    @classmethod
    def tearDownClass(cls):
        cls._directory.cleanup()

    def copy(self, name):
        path = self.root / name
        shutil.copyfile(self.fresh, path)
        conn = sqlite3.connect(path)
        self.addCleanup(conn.close)
        return conn

    def definitions(self, path):
        with closing(sqlite3.connect(path)) as conn:
            return conn.execute("SELECT type, name, sql FROM sqlite_master WHERE tbl_name IN (?, ?) ORDER BY type, name",
                                (SESSIONS, ANSWERS)).fetchall()

    def test_a_fresh_build_has_both_tables_empty_and_the_upgrade_definition(self):
        with closing(sqlite3.connect(self.fresh)) as conn:
            for table in (SESSIONS, ANSWERS):
                self.assertEqual(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0)
            self.assertEqual(ct.classification_problems(conn), [])
            self.assertEqual(parity.compare_to_canonical(conn), [])
        self.assertEqual(self.definitions(self.fresh), self.definitions(self.upgraded))
        self.assertEqual(len(self.definitions(self.fresh)), 5)

    def test_both_tables_are_runtime_and_never_canonical(self):
        self.assertEqual({SESSIONS, ANSWERS} - set(ct.tables_of(ct.RUNTIME)), set())
        self.assertEqual({SESSIONS, ANSWERS} & set(ct.tables_of(ct.CANONICAL) + ct.tables_of(ct.OPERATIONAL)), set())
        manifest = export.load_manifest(ct.CANONICAL_DIR)
        self.assertEqual({SESSIONS, ANSWERS} - set(manifest["runtime_exclusions"]), set())
        self.assertEqual({SESSIONS, ANSWERS} & set(manifest["canonical_tables"]), set())
        self.assertIn(MIGRATION, manifest["schema"]["migrations"])
        # The level is the latest migration file that actually exists, so a later migration need not edit this test.
        self.assertEqual(manifest["schema"]["migration_level"], sorted(p.name for p in MIGRATIONS.glob("*.sql"))[-1])
        self.assertFalse((ct.CANONICAL_DIR / f"{SESSIONS}.jsonl").exists())
        self.assertFalse((ct.CANONICAL_DIR / f"{ANSWERS}.jsonl").exists())

    def test_export_of_a_fresh_build_reproduces_every_tracked_byte(self):
        out = self.root / "export-fresh"
        with closing(sqlite3.connect(self.fresh)) as conn:
            export.export_database(conn, out, sources=export.load_manifest(ct.CANONICAL_DIR)["sources"])
        self.assertEqual(sorted(p.name for p in out.iterdir()), sorted(p.name for p in ct.CANONICAL_DIR.iterdir()))
        for path in ct.CANONICAL_DIR.iterdir():
            self.assertEqual((out / path.name).read_bytes(), path.read_bytes(), path.name)

    def test_export_from_a_database_with_practice_rows_leaves_them_out(self):
        conn = self.copy("rows.db")
        conn.execute("PRAGMA foreign_keys = ON")
        user = conn.execute("INSERT INTO users (display_name, role, session_key) VALUES ('Ученик', 'student', ?)",
                            ("k" * 32,)).lastrowid
        section, question = (conn.execute("SELECT MIN(id) FROM curriculum_sections").fetchone()[0],
                             conn.execute("SELECT MIN(id) FROM questions").fetchone()[0])
        add_session(conn, user_id=user, section_id=section, question_ids_json=json.dumps([question]),
                    submitted_at=STAMP, score_correct=1, score_total=1)
        add_answer(conn, session_id=1, question_id=question)
        conn.commit()
        out = self.root / "export-rows"
        export.export_database(conn, out, sources=export.load_manifest(ct.CANONICAL_DIR)["sources"])
        self.assertEqual({p.name for p in out.iterdir()}, {p.name for p in ct.CANONICAL_DIR.iterdir()})
        for path in ct.CANONICAL_DIR.iterdir():
            self.assertEqual((out / path.name).read_bytes(), path.read_bytes(), path.name)
        # A database holding runtime rows is not a canonical rebuild (runtime exclusion is enforced, not assumed).
        problems = parity.compare_to_canonical(conn)
        self.assertTrue(any(p.startswith(f"{SESSIONS}: 1 rows") for p in problems), problems)
        self.assertTrue(any(p.startswith(f"{ANSWERS}: 1 rows") for p in problems), problems)


class ReferenceValidatorTest(unittest.TestCase):
    """Both runtime question-reference validators must see the two practice columns (C1 review N5 and N7).

    The practice rows below are corrupted on disposable copies with foreign keys OFF, the only way to
    reach the guards: the schema's own foreign key keeps ``practice_answers.question_id`` honest.
    """

    @classmethod
    def setUpClass(cls):
        cls._directory = tempfile.TemporaryDirectory(prefix="practice-references-")
        cls.root = Path(cls._directory.name)
        cls.fresh = build_test_database(cls.root / "fresh.db")
        with closing(sqlite3.connect(cls.fresh)) as conn:
            cls.question_ids = [row[0] for row in conn.execute("SELECT id FROM questions ORDER BY id LIMIT 2")]
            cls.section = conn.execute("SELECT MIN(id) FROM curriculum_sections").fetchone()[0]

    @classmethod
    def tearDownClass(cls):
        cls._directory.cleanup()

    def database(self, plan, *, answer_question=None, foreign_keys=False):
        path = self.root / f"db-{self.id().rsplit('.', 1)[-1]}-{abs(hash((plan, answer_question)))}.db"
        shutil.copyfile(self.fresh, path)
        conn = sqlite3.connect(path)
        self.addCleanup(conn.close)
        conn.execute(f"PRAGMA foreign_keys = {'ON' if foreign_keys else 'OFF'}")
        user = conn.execute("INSERT INTO users (display_name, role, session_key) VALUES ('Ученик', 'student', ?)",
                            ("k" * 32,)).lastrowid
        add_session(conn, user_id=user, section_id=self.section, question_ids_json=plan,
                    submitted_at=STAMP, score_correct=0, score_total=1)
        if answer_question is not None:
            add_answer(conn, session_id=1, question_id=answer_question)
        conn.commit()
        return conn

    def validators(self, conn):
        ids = {row[0] for row in conn.execute("SELECT id FROM questions")}
        return (parity.runtime_reference_problems(conn, ids), parity.embedded_reference_problems(conn),
                tool.reference_gate(conn))

    def test_valid_practice_rows_raise_nothing(self):
        a, b = self.question_ids
        conn = self.database(json.dumps([a, b]), answer_question=a, foreign_keys=True)
        runtime, embedded, gate = self.validators(conn)
        self.assertEqual((runtime, embedded, gate), ([], [], {}))

    def test_an_unresolved_id_in_the_session_plan_is_reported_by_both(self):
        for label, plan in (("list", json.dumps([self.question_ids[0], 9_999_999])),
                            ("dict", json.dumps({"question_ids": [9_999_999]})),
                            ("top-level integer", "9999999")):
            with self.subTest(label):
                runtime, embedded, gate = self.validators(self.database(plan))
                self.assertEqual([p.split(":")[0] for p in runtime], ["practice_sessions.question_ids_json"])
                self.assertEqual(embedded, runtime)
                self.assertEqual(gate, {("practice_sessions.question_ids_json", "preexisting_unresolved_reference"): 1})

    def test_an_unresolved_answer_question_is_reported_by_both(self):
        a = self.question_ids[0]
        conn = self.database(json.dumps([a]), answer_question=9_999_999)
        runtime, embedded, gate = self.validators(conn)
        self.assertEqual([p.split(":")[0] for p in runtime], ["practice_answers.question_id"])
        self.assertEqual(embedded, runtime)
        self.assertEqual(gate[("practice_answers.question_id", "preexisting_unresolved_reference")], 1)
        self.assertIn(("practice_answers.foreign_key", "preexisting_fk_violation"), gate)    # the FK check sees it too

    def test_the_gate_keeps_its_fixed_codes_for_malformed_and_noninteger_plans(self):
        a = self.question_ids[0]
        cases = {"{bad": "malformed_reference_json", "": "malformed_reference_json", "   ": "malformed_reference_json",
                 json.dumps([str(a)]): "noninteger_reference_value", f"[{a}.0]": "noninteger_reference_value"}
        for plan, code in cases.items():
            with self.subTest(plan=plan):
                gate = tool.reference_gate(self.database(plan))
                self.assertEqual(gate, {("practice_sessions.question_ids_json", code): 1})
        with self.subTest("a boolean member is ignored"):
            self.assertEqual(tool.reference_gate(self.database(f"[{a}, true]")), {})

    def test_the_validators_still_check_the_original_columns(self):
        """The new columns were added beside the old ones, not instead of them."""
        conn = self.database(json.dumps([self.question_ids[0]]))
        conn.execute("INSERT INTO generated_exams (exam_name, question_ids) VALUES ('x', '[9999999]')")
        runtime, _embedded, gate = self.validators(conn)
        self.assertEqual([p.split(":")[0] for p in runtime], ["generated_exams.question_ids"])
        self.assertEqual(list(gate), [("generated_exams.question_ids", "preexisting_unresolved_reference")])


if __name__ == "__main__":
    unittest.main()
