"""027 adds the DZI Part 1 simulation runtime storage (C3.1; docs/DZI_PART1_SIMULATION_DESIGN.md).

Two new tables, ``dzi_part1_attempts`` and ``dzi_part1_answers``, and two indexes on the
attempts. **No row is written**, no existing table is rebuilt or altered, nothing is backfilled.
The tables are runtime state: never canonical, never exported.

What this module proves is the *schema*: columns, foreign keys, row-local CHECK constraints, the
open-attempt unique index, the additive upgrade from a populated 026 database, the fresh-build and
export parity, and that both runtime question-reference validators see ``question_ids_json`` and
``dzi_part1_answers.question_id`` and neither sees ``slot_format_json``. It does **not** prove who
may start, read or submit an attempt, the shape of either JSON column, the slot count, that a plan
holds distinct ids or that an answer row names its slot's planned question, answer length limits,
grading, clock handling or state transitions: those are later service slices. Two negative controls
below show what the table accepts on purpose (one question in two slots; a very long raw answer).

Constraint tests come with negative controls: the same bad row is applied to a copy of the
migration with exactly that constraint edited out, and must then be accepted. A test that the
real schema rejects a row therefore cannot pass for an unrelated reason.

Every database here is temporary: built by the shared migration runner from the repository files,
or by ``tests.support.build_test_database``. None is ``data/questions.db``. The genuine update and
no-op preservation of both tables (rows, storage classes and the AUTOINCREMENT mark) is pinned in
``tests/test_update_canonical_content.py``.
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
MIGRATION = "027_dzi_part1_runtime.sql"
ATTEMPTS, ANSWERS = "dzi_part1_attempts", "dzi_part1_answers"
OPEN_INDEX, HISTORY_INDEX = "uniq_dzi_part1_attempts_open_user", "idx_dzi_part1_attempts_user_started"

START, DEADLINE, CUTOFF = "2026-11-23 10:00:00", "2026-11-23 11:30:00", "2026-11-23 11:31:00"
SLOT_FORMAT = json.dumps([{"kind": "multiple_choice", "points": 1}, {"kind": "short_answer", "points": 3}])
GLOB = "[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9] [0-9][0-9]:[0-9][0-9]:[0-9][0-9]"

# On top of POPULATE_SQL (section 11, question 21, users 51 teacher / 52 student, assignments, attempts) and
# ACCOUNTS_SQL (61 admin, 62 disabled teacher): two blueprints, two more questions and 026's own rows
# (a practice session and answer, with the AUTOINCREMENT mark raised above MAX(id)).
BASE_SQL = POPULATE_SQL + ACCOUNTS_SQL + """
INSERT INTO dzi_blueprints (id, blueprint_slug, title_bg) VALUES (5, 'blueprint-5', 'План 5'),
                                                                  (6, 'blueprint-6', 'План 6');
INSERT INTO dzi_blueprint_slots (id, blueprint_id, slot_number, exam_part, task_kind, points)
    VALUES (1, 5, 1, 1, 'multiple_choice', 1);
INSERT INTO questions (id, source_exam, question_type, points, prompt) VALUES
    (22, 'legacy', 'short_answer', 3, 'Втори въпрос'), (23, 'legacy', 'multiple_choice', 1, 'Трети въпрос');
INSERT INTO practice_sessions (id, user_id, section_id, seed, question_ids_json, started_at, submitted_at,
                               score_correct, score_total)
    VALUES (1, 52, 11, 'practice', '[21]', '2026-11-20 10:00:00', '2026-11-20 10:05:00', 1, 1);
INSERT INTO practice_answers (session_id, question_id, chosen_letter, is_correct) VALUES (1, 21, 'A', 1);
UPDATE sqlite_sequence SET seq = 40 WHERE name = 'practice_sessions';
"""

ATTEMPT_COLUMNS = ("id", "user_id", "blueprint_id", "seed", "question_ids_json", "slot_format_json",
                   "max_points_planned", "duration_seconds", "started_at", "deadline_at", "submit_cutoff_at",
                   "submitted_at", "expired_at", "abandoned_at", "score_points", "score_max")
ANSWER_COLUMNS = ("attempt_id", "slot_number", "subquestion_number", "question_id", "kind", "chosen_letter",
                  "raw_answer", "is_correct", "points_possible", "points_awarded", "grader_version")
SUBMITTED = {"submitted_at": "2026-11-23 10:30:00", "score_points": 3, "score_max": 4}
EXPIRED = {"expired_at": CUTOFF}
ABANDONED = {"abandoned_at": "2026-11-23 10:45:00"}


def apply(path, migrations_dir=MIGRATIONS):
    out = io.StringIO()
    code = manage_migrations.apply_migrations(path, migrations_dir, out=out)
    return code, out.getvalue()


def migration_text() -> str:
    return (MIGRATIONS / MIGRATION).read_text(encoding="utf-8")


def statements() -> str:
    """The migration without its comments."""
    return "\n".join(line for line in migration_text().splitlines() if not line.lstrip().startswith("--"))


def shape_check(column: str, nullable: bool) -> str:
    """The exact text of one timestamp column's CHECK expression in the migration."""
    if nullable:
        return (f"{column} IS NULL\n               OR (typeof({column}) = 'text'\n"
                f"                   AND length({column}) = 19\n"
                f"                   AND length(CAST({column} AS BLOB)) = 19\n"
                f"                   AND {column} GLOB '{GLOB}')")
    return (f"typeof({column}) = 'text'\n               AND length({column}) = 19\n"
            f"               AND length(CAST({column} AS BLOB)) = 19\n"
            f"               AND {column} GLOB '{GLOB}'")


def add_attempt(conn, **override):
    values = {"id": None, "user_id": 52, "blueprint_id": 5, "seed": "seed", "question_ids_json": "[21, 22]",
              "slot_format_json": SLOT_FORMAT, "max_points_planned": 4, "duration_seconds": 5400,
              "started_at": START, "deadline_at": DEADLINE, "submit_cutoff_at": CUTOFF, "submitted_at": None,
              "expired_at": None, "abandoned_at": None, "score_points": None, "score_max": None}
    unknown = set(override) - set(values)
    assert not unknown, unknown
    values.update(override)
    conn.execute(f"INSERT INTO {ATTEMPTS} ({', '.join(ATTEMPT_COLUMNS)}) "
                 f"VALUES ({', '.join('?' for _ in ATTEMPT_COLUMNS)})", [values[name] for name in ATTEMPT_COLUMNS])


def add_answer(conn, **override):
    values = {"attempt_id": 1, "slot_number": 1, "subquestion_number": 0, "question_id": 21,
              "kind": "multiple_choice", "chosen_letter": "A", "raw_answer": None, "is_correct": 1,
              "points_possible": 1, "points_awarded": 1, "grader_version": "v1"}
    unknown = set(override) - set(values)
    assert not unknown, unknown
    values.update(override)
    conn.execute(f"INSERT INTO {ANSWERS} ({', '.join(ANSWER_COLUMNS)}) "
                 f"VALUES ({', '.join('?' for _ in ANSWER_COLUMNS)})", [values[name] for name in ANSWER_COLUMNS])


def short(**override):
    """A short-answer row: part 1 of slot 2, two of three points, wrong by default."""
    return dict({"slot_number": 2, "subquestion_number": 1, "question_id": 22, "kind": "short_answer",
                 "chosen_letter": None, "raw_answer": "отговор", "is_correct": 0, "points_possible": 1,
                 "points_awarded": 0}, **override)


class PopulatedAt026(unittest.TestCase):
    """One database at migration 026 with legacy rows; every test works on a private copy of it."""

    @classmethod
    def setUpClass(cls):
        cls._directory = tempfile.TemporaryDirectory(prefix="dzi-part1-migration-")
        cls.root = Path(cls._directory.name)
        previous = cls.root / "through-026"
        previous.mkdir()
        for path in sorted(MIGRATIONS.glob("*.sql")):
            if path.name < MIGRATION:
                shutil.copyfile(path, previous / path.name)
        cls.template = cls.root / "template-026.db"
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

    def migrate(self):
        code, report = apply(self.path)
        self.assertEqual(code, 0, report)
        return report

    def mutated(self, old: str, new: str, foreign_keys=False):
        """A copy of the 026 template with the migration applied after ``old`` was edited to ``new``."""
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


class UpgradeTest(PopulatedAt026):
    def test_only_the_part1_objects_and_the_ledger_row_are_added(self):
        conn = self.connect()
        before, objects_before = dump(conn, skip=("schema_migrations",)), objects(conn)
        ledger_before = conn.execute("SELECT filename FROM schema_migrations ORDER BY filename").fetchall()
        self.assertGreater(conn.execute("SELECT COUNT(*) FROM quiz_attempts").fetchone()[0], 0)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM practice_sessions").fetchone()[0], 1)
        self.assertEqual(conn.execute("SELECT seq FROM sqlite_sequence WHERE name = 'practice_sessions'").fetchone(), (40,))
        self.migrate()
        conn = self.connect()
        # Every other row, sqlite_sequence included, is as it was; the two new tables are empty.
        self.assertEqual(dump(conn, skip=("schema_migrations", ATTEMPTS, ANSWERS)), before)
        added = {row[1] for row in objects(conn) if row not in objects_before}
        self.assertEqual(added, {ATTEMPTS, ANSWERS, OPEN_INDEX, HISTORY_INDEX,
                                 f"sqlite_autoindex_{ANSWERS}_1", f"sqlite_autoindex_{ANSWERS}_2"})
        self.assertEqual({row[1] for row in objects_before} - {row[1] for row in objects(conn)}, set())
        ledger = conn.execute("SELECT filename FROM schema_migrations ORDER BY filename").fetchall()
        self.assertEqual(ledger, ledger_before + [(MIGRATION,)])
        self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
        self.assertEqual(conn.execute("PRAGMA integrity_check").fetchall(), [("ok",)])

    def test_nothing_is_seeded_backfilled_or_sequenced(self):
        self.migrate()
        conn = self.connect()
        for table in (ATTEMPTS, ANSWERS):
            self.assertEqual(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0)
            self.assertIsNone(conn.execute("SELECT 1 FROM sqlite_sequence WHERE name = ?", (table,)).fetchone())
        # The legacy attempts and 026's own session stay the business of their tables.
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM quiz_attempts").fetchone()[0], 2)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM practice_sessions").fetchone()[0], 1)

    def test_it_is_an_additive_migration_not_a_rebuild(self):
        dry = io.StringIO()
        manage_migrations.dry_run(self.path, MIGRATIONS, out=dry)
        self.assertIn(f"pending migrations:\n  - {MIGRATION}\n", dry.getvalue())
        self.assertIsNone(self.connect().execute("SELECT 1 FROM sqlite_master WHERE name = ?", (ATTEMPTS,)).fetchone())
        report = self.migrate()
        self.assertIn(f"applied migrations:\n  - {MIGRATION}\n", report)
        self.assertIn("foreign_key_check: ok", report)
        self.assertNotIn("table-rebuild", report)
        self.assertFalse(manage_migrations.is_table_rebuild_migration(MIGRATION, migration_text()))
        sql = statements().upper()
        for forbidden in ("DROP ", "ALTER ", "DELETE FROM", "UPDATE ", "TRIGGER", "INSERT ", "CASCADE"):
            self.assertNotIn(forbidden, sql)
        self.assertEqual(sql.count("CREATE TABLE"), 2)
        self.assertEqual(sql.count("CREATE UNIQUE INDEX"), 1)
        self.assertEqual(sql.count("CREATE INDEX"), 1)

    def test_the_runner_is_idempotent(self):
        self.migrate()
        conn = self.connect()
        ledger, schema = conn.execute("SELECT * FROM schema_migrations ORDER BY filename").fetchall(), objects(conn)
        content = dump(conn)
        code, report = apply(self.path)
        self.assertEqual(code, 0, report)
        self.assertNotIn(f"applied migrations:\n  - {MIGRATION}", report)
        conn = self.connect()
        self.assertEqual(conn.execute("SELECT * FROM schema_migrations ORDER BY filename").fetchall(), ledger)
        self.assertEqual(objects(conn), schema)
        self.assertEqual(dump(conn), content)

    def test_old_migrations_are_untouched_and_the_new_one_has_no_logic_beyond_ddl(self):
        names = sorted(path.name for path in MIGRATIONS.glob("*.sql"))
        self.assertEqual(names[-2:], ["026_practice_runtime.sql", MIGRATION])
        self.assertEqual(len([n for n in names if n.startswith("027_")]), 1)
        self.assertTrue(statements().lstrip().startswith("PRAGMA foreign_keys = ON;"))

    def test_code_that_predates_the_migration_still_runs(self):
        self.migrate()
        conn = self.connect()
        conn.execute("INSERT INTO quiz_assignments (section_id, title_bg, question_count, owner_user_id) "
                     "VALUES (11, 'Старият код', 1, 51)")
        conn.commit()
        self.assertEqual(conn.execute(f"SELECT COUNT(*) FROM {ATTEMPTS}").fetchone()[0], 0)


class TableDefinitionTest(PopulatedAt026):
    def setUp(self):
        super().setUp()
        self.migrate()
        self.conn = self.connect()

    def columns(self, table):
        return [(row[1], row[2], row[3], row[5]) for row in self.conn.execute(f"PRAGMA table_info({table})")]

    def test_attempt_columns(self):
        self.assertEqual(self.columns(ATTEMPTS), [
            ("id", "INTEGER", 0, 1), ("user_id", "INTEGER", 1, 0), ("blueprint_id", "INTEGER", 1, 0),
            ("seed", "TEXT", 1, 0), ("question_ids_json", "TEXT", 1, 0), ("slot_format_json", "TEXT", 1, 0),
            ("max_points_planned", "INTEGER", 1, 0), ("duration_seconds", "INTEGER", 1, 0),
            ("started_at", "TEXT", 1, 0), ("deadline_at", "TEXT", 1, 0), ("submit_cutoff_at", "TEXT", 1, 0),
            ("submitted_at", "TEXT", 0, 0), ("expired_at", "TEXT", 0, 0), ("abandoned_at", "TEXT", 0, 0),
            ("score_points", "INTEGER", 0, 0), ("score_max", "INTEGER", 0, 0)])
        self.assertIn("AUTOINCREMENT", self.conn.execute(
            "SELECT sql FROM sqlite_master WHERE name = ?", (ATTEMPTS,)).fetchone()[0].upper())

    def test_answer_columns_and_the_composite_primary_key(self):
        self.assertEqual(self.columns(ANSWERS), [
            ("attempt_id", "INTEGER", 1, 1), ("slot_number", "INTEGER", 1, 2), ("subquestion_number", "INTEGER", 1, 3),
            ("question_id", "INTEGER", 1, 0), ("kind", "TEXT", 1, 0), ("chosen_letter", "TEXT", 0, 0),
            ("raw_answer", "TEXT", 0, 0), ("is_correct", "INTEGER", 1, 0), ("points_possible", "INTEGER", 1, 0),
            ("points_awarded", "INTEGER", 1, 0), ("grader_version", "TEXT", 1, 0)])
        self.assertNotIn("AUTOINCREMENT", self.conn.execute(
            "SELECT sql FROM sqlite_master WHERE name = ?", (ANSWERS,)).fetchone()[0].upper())

    def test_every_foreign_key_is_an_explicit_restrict(self):
        def keys(table):
            return sorted((row[2], row[3], row[4], row[5], row[6])
                          for row in self.conn.execute(f"PRAGMA foreign_key_list({table})"))
        self.assertEqual(keys(ATTEMPTS), sorted([("users", "user_id", "id", "NO ACTION", "RESTRICT"),
                                                 ("dzi_blueprints", "blueprint_id", "id", "NO ACTION", "RESTRICT")]))
        self.assertEqual(keys(ANSWERS), sorted([(ATTEMPTS, "attempt_id", "id", "NO ACTION", "RESTRICT"),
                                                ("questions", "question_id", "id", "NO ACTION", "RESTRICT")]))

    def test_the_indexes_and_their_shape(self):
        indexes = {row[1]: (row[2], row[4]) for row in self.conn.execute(f"PRAGMA index_list({ATTEMPTS})")}
        self.assertEqual(indexes[OPEN_INDEX], (1, 1))        # unique, partial
        self.assertEqual(indexes[HISTORY_INDEX], (0, 0))
        self.assertEqual(sorted(indexes), sorted([OPEN_INDEX, HISTORY_INDEX]))
        sql = self.conn.execute("SELECT sql FROM sqlite_master WHERE name = ?", (OPEN_INDEX,)).fetchone()[0]
        self.assertIn("WHERE submitted_at IS NULL AND expired_at IS NULL AND abandoned_at IS NULL", sql)
        self.assertEqual([row[2] for row in self.conn.execute(f"PRAGMA index_info({OPEN_INDEX})")], ["user_id"])
        self.assertEqual([row[2] for row in self.conn.execute(f"PRAGMA index_info({HISTORY_INDEX})")],
                         ["user_id", "started_at"])
        answer_keys = [(row[2], [c[2] for c in self.conn.execute(f"PRAGMA index_info({row[1]})")])
                       for row in self.conn.execute(f"PRAGMA index_list({ANSWERS})")]
        self.assertEqual(sorted(answer_keys), sorted([
            (1, ["attempt_id", "slot_number", "subquestion_number"]),
            (1, ["attempt_id", "question_id", "subquestion_number"])]))

    def test_the_history_and_open_indexes_serve_their_reads(self):
        history = " ".join(row[3] for row in self.conn.execute(
            f"EXPLAIN QUERY PLAN SELECT id FROM {ATTEMPTS} WHERE user_id = 52 ORDER BY started_at DESC"))
        self.assertIn(HISTORY_INDEX, history)
        open_plan = " ".join(row[3] for row in self.conn.execute(
            f"EXPLAIN QUERY PLAN SELECT id FROM {ATTEMPTS} WHERE user_id = 52 AND submitted_at IS NULL "
            "AND expired_at IS NULL AND abandoned_at IS NULL"))
        self.assertIn(OPEN_INDEX, open_plan)

    def test_no_blob_columns_no_accepted_answers_no_text_of_questions_and_no_raw_answer_length_check(self):
        names = [row[0] for row in self.columns(ATTEMPTS)] + [row[0] for row in self.columns(ANSWERS)]
        for forbidden in ("name", "prompt", "blob", "email", "accepted", "correct_answer", "correct_letter", "lease"):
            self.assertEqual([n for n in names if forbidden in n], [], forbidden)
        types = {row[1].upper() for row in self.columns(ATTEMPTS)} | {row[1].upper() for row in self.columns(ANSWERS)}
        self.assertNotIn("BLOB", types)
        self.assertNotIn("length(raw_answer", statements())
        self.assertNotIn("TRIGGER", statements().upper())


class AttemptConstraintTest(PopulatedAt026):
    """Row-local invariants. Foreign keys are OFF so each case isolates the CHECK or index under test."""

    def setUp(self):
        super().setUp()
        self.migrate()
        self.conn = self.connect(foreign_keys=False)

    def assertRejected(self, conn=None, **row):
        with self.assertRaises(sqlite3.IntegrityError):
            add_attempt(conn or self.conn, **row)

    def check_control(self, edit, rows, reject_only=()):
        """Each row is rejected by the real schema and accepted once ``edit`` removes its constraint."""
        old, new = edit
        control = self.mutated(old, new)
        for index, row in enumerate(rows):
            with self.subTest(row=row):
                self.assertRejected(**row)
                add_attempt(control, **dict(row, id=1000 + index))          # the negative control
                control.execute(f"DELETE FROM {ATTEMPTS}")                  # keep the open-attempt key free
        for row in reject_only:
            with self.subTest(reject_only=row):
                self.assertRejected(**row)

    # --- identifiers, required fields, types

    def test_ids_must_be_positive_integers(self):
        for column in ("user_id", "blueprint_id"):
            self.check_control((f"CHECK (typeof({column}) = 'integer' AND {column} > 0)", "CHECK (1)"),
                               [{column: 0}, {column: -5}])
        self.check_control(("CHECK (typeof(id) = 'integer' AND id > 0)", "CHECK (1)"), [{"id": 0}, {"id": -1}])
        add_attempt(self.conn)                       # and the primary key itself auto-assigns a positive id
        add_attempt(self.conn, user_id=51)
        self.assertEqual([r[0] for r in self.conn.execute(f"SELECT id FROM {ATTEMPTS} ORDER BY id")], [1, 2])

    def test_required_fields_cannot_be_null_and_blobs_are_refused(self):
        for field in ("user_id", "blueprint_id", "seed", "question_ids_json", "slot_format_json",
                      "max_points_planned", "duration_seconds", "started_at", "deadline_at", "submit_cutoff_at"):
            with self.subTest(null=field):
                self.assertRejected(**{field: None})
        for field in ("seed", "question_ids_json", "slot_format_json"):
            with self.subTest(blob=field):
                self.assertRejected(**{field: b"[21]"})
        self.assertEqual(self.conn.execute(f"SELECT COUNT(*) FROM {ATTEMPTS}").fetchone()[0], 0)

    def test_planned_maximum_and_duration_are_positive_whole_numbers(self):
        for column in ("max_points_planned", "duration_seconds"):
            self.check_control((f"CHECK (typeof({column}) = 'integer' AND {column} > 0)", "CHECK (1)"),
                               [{column: 0}, {column: -1}, {column: 1.5}, {column: "x"}])

    # --- timestamps: shape and orderings

    def test_every_timestamp_has_the_fixed_width_lifecycle_shape(self):
        late = {"submit_cutoff_at": CUTOFF}
        # Bad values chosen so the orderings still hold: the shape CHECK is the only thing that refuses them.
        bad_text = ["2026-11-23 10:00:0", "2026-11-23 10:00:000", "2026-11-23 10:0a:00", "2026-11-23 10:00:00\x00junk"]
        cases = {
            "started_at": ([("2026-11-23 10:00:0", {}), ("2026-11-23 10:00:000", {}), ("2026-11-23 10:0a:00", {}),
                            ("2026-11-23 10:00:00\x00x", {})], False),
            "deadline_at": ([("2026-11-23 11:30:0", {}), ("2026-11-23 11:30:000", {}), ("2026-11-23 11:2a:00", {}),
                             ("2026-11-23 11:30:00\x00x", {})], False),
            "submit_cutoff_at": ([("2026-11-23 11:31:0", {}), ("2026-11-23 11:31:000", {}), ("2026-11-23 11:3a:00", {}),
                                  ("2026-11-23 11:31:00\x00x", {})], False),
            "submitted_at": ([("2026-11-23 10:30:0", SUBMITTED), ("2026-11-23 10:30:000", SUBMITTED),
                              ("2026-11-23 10:3a:00", SUBMITTED), ("2026-11-23 10:30:00\x00x", SUBMITTED)], True),
            "expired_at": ([("2026-11-23 11:31:0", None), ("2026-11-23 11:31:000", None), ("2026-11-23 11:3a:00", None),
                            ("2026-11-23 11:31:00\x00x", None)], True),
            "abandoned_at": ([("2026-11-23 10:45:0", {}), ("2026-11-23 10:45:000", {}), ("2026-11-23 10:4a:00", {}),
                              ("2026-11-23 10:45:00\x00x", {}), ("2026-11-23T10:45:00", {}), ("2026-11-23", {}),
                              ("2026-11-23 10:45:00Z", {}), (" 2026-11-23 10:45:00", {})], True),
        }
        del late, bad_text
        for column, (values, nullable) in cases.items():
            rows = []
            for value, extra in values:
                if column == "expired_at" and value < CUTOFF:
                    continue                                              # an earlier-than-cutoff value is an ordering case
                rows.append(dict(extra or {}, **{column: value}))
            old = f"CHECK ({shape_check(column, nullable)})"
            with self.subTest(column=column):
                self.check_control((old, "CHECK (1)"), [r for r in rows])

    def test_a_blob_or_integer_timestamp_is_refused_everywhere(self):
        for column in ("started_at", "deadline_at", "submit_cutoff_at", "abandoned_at"):
            for bad in (b"2026-11-23 10:00:00", 20261123100000):
                with self.subTest(column=column, value=bad):
                    self.assertRejected(**{column: bad})
        for column, extra in (("submitted_at", SUBMITTED), ("expired_at", {})):
            for bad in (b"2026-11-23 11:00:00", 20261123110000):
                with self.subTest(column=column, value=bad):
                    self.assertRejected(**dict(extra, **{column: bad}))

    def test_started_before_deadline_before_the_cutoff(self):
        old = "CHECK (started_at < deadline_at AND deadline_at < submit_cutoff_at),"
        self.check_control((old, "CHECK (1),"), [
            {"deadline_at": START}, {"deadline_at": "2026-11-23 09:59:59"},
            {"deadline_at": CUTOFF}, {"deadline_at": "2026-11-23 11:32:00"},
            {"submit_cutoff_at": DEADLINE}, {"submit_cutoff_at": "2026-11-23 11:29:59"}])
        add_attempt(self.conn, started_at="2026-11-23 10:00:00", deadline_at="2026-11-23 10:00:01",
                    submit_cutoff_at="2026-11-23 10:00:02")                 # the narrowest valid ordering

    def test_at_most_one_terminal_timestamp(self):
        old = "CHECK ((submitted_at IS NOT NULL) + (expired_at IS NOT NULL) + (abandoned_at IS NOT NULL) <= 1),"
        stamp = {"submitted_at": "2026-11-23 10:30:00", "score_points": 3, "score_max": 4}
        self.check_control((old, "CHECK (1),"), [
            dict(stamp, expired_at=CUTOFF), dict(stamp, **ABANDONED), dict(EXPIRED, **ABANDONED),
            dict(stamp, expired_at=CUTOFF, **ABANDONED)])

    def test_a_submit_lies_between_the_start_and_the_stored_cutoff(self):
        old = "CHECK (submitted_at IS NULL OR (started_at <= submitted_at AND submitted_at < submit_cutoff_at)),"
        def submitted(value):
            return {"submitted_at": value, "score_points": 3, "score_max": 4}
        self.check_control((old, "CHECK (1),"), [
            submitted("2026-11-23 09:59:59"), submitted(CUTOFF), submitted("2026-11-23 11:32:00")])
        for accepted in (START, DEADLINE, "2026-11-23 11:30:59"):            # at the start, at the deadline, in grace
            add_attempt(self.conn, **submitted(accepted))
            self.conn.execute(f"DELETE FROM {ATTEMPTS}")

    def test_an_expiry_is_at_or_after_the_stored_cutoff(self):
        old = "CHECK (expired_at IS NULL OR expired_at >= submit_cutoff_at),"
        self.check_control((old, "CHECK (1),"), [
            {"expired_at": START}, {"expired_at": DEADLINE}, {"expired_at": "2026-11-23 11:30:59"}])
        for accepted in (CUTOFF, "2026-11-23 11:31:01", "2026-12-01 00:00:00"):
            add_attempt(self.conn, expired_at=accepted)
            self.conn.execute(f"DELETE FROM {ATTEMPTS}")

    def test_abandonment_has_no_ordering_against_the_start_or_the_cutoff(self):
        """A row stamped later than lifecycle time can be abandoned earlier than its own start (design 7.4)."""
        for stamp in ("2026-11-23 09:00:00", START, "2026-11-23 10:59:59", CUTOFF, "2027-01-01 00:00:00"):
            with self.subTest(abandoned_at=stamp):
                add_attempt(self.conn, abandoned_at=stamp)
                self.conn.execute(f"DELETE FROM {ATTEMPTS}")
        self.assertNotRegex(statements(), r"abandoned_at\s*[<>]|[<>]=?\s*abandoned_at")

    # --- score coupling

    def test_an_unsubmitted_attempt_has_no_score(self):
        old = "CHECK (submitted_at IS NOT NULL OR (score_points IS NULL AND score_max IS NULL)),"
        self.check_control((old, "CHECK (1),"), [
            {"score_points": 1, "score_max": 4}, {"score_points": 0, "score_max": 0}, {"score_max": 4},
            {"score_points": 0}, dict(EXPIRED, score_points=0, score_max=4), dict(ABANDONED, score_points=1, score_max=4),
            dict(ABANDONED, score_points=1, score_max=4)])

    def test_a_submitted_score_is_whole_nonnegative_and_bounded(self):
        old = """CHECK (submitted_at IS NULL
           OR (typeof(score_points) = 'integer' AND typeof(score_max) = 'integer'
               AND score_points >= 0 AND score_max > 0 AND score_points <= score_max))"""
        bad = [{"score_points": None, "score_max": None}, {"score_points": 1, "score_max": None},
               {"score_points": None, "score_max": 4}, {"score_points": -1, "score_max": 4},
               {"score_points": 5, "score_max": 4}, {"score_points": 0, "score_max": 0},
               {"score_points": 0, "score_max": -1}, {"score_points": 1.5, "score_max": 4},
               {"score_points": "x", "score_max": 4}, {"score_points": 1, "score_max": 4.5}]
        self.check_control((old, "CHECK (1)"), [dict(row, submitted_at="2026-11-23 10:30:00") for row in bad])

    def test_valid_rows_in_every_state_are_accepted(self):
        add_attempt(self.conn)                                                             # open
        add_attempt(self.conn, user_id=51, **SUBMITTED)                                    # submitted
        add_attempt(self.conn, user_id=51, submitted_at=START, score_points=0, score_max=1)  # lowest score, at the start
        add_attempt(self.conn, user_id=51, submitted_at="2026-11-23 11:30:59", score_points=45, score_max=45)
        add_attempt(self.conn, user_id=51, **EXPIRED)
        add_attempt(self.conn, user_id=51, **ABANDONED)
        add_attempt(self.conn, user_id=51, abandoned_at="2026-11-22 00:00:00")             # before its own start
        self.assertEqual(self.conn.execute(f"SELECT COUNT(*) FROM {ATTEMPTS}").fetchone()[0], 7)

    def test_the_planned_maximum_and_the_score_maximum_are_independent_columns(self):
        add_attempt(self.conn, max_points_planned=45, submitted_at="2026-11-23 10:30:00", score_points=10, score_max=40)

    def test_json_columns_are_text_without_any_shape_rule(self):
        """The schema does not read either JSON column; shape and slot count are the service's."""
        for plan, slots in (("[]", "[]"), ("{}", "{}"), ("not json", "not json"), ("", ""), ("[21, 21]", "[1]")):
            with self.subTest(plan=plan):
                add_attempt(self.conn, question_ids_json=plan, slot_format_json=slots, user_id=51)
                self.conn.execute(f"DELETE FROM {ATTEMPTS}")
        self.assertNotIn("json_valid", statements())

    # --- the open-attempt unique index

    def test_two_open_attempts_of_one_account_collide(self):
        add_attempt(self.conn)
        self.assertRejected()
        self.assertRejected(blueprint_id=6, seed="other", started_at="2026-11-23 12:00:00",
                            deadline_at="2026-11-23 13:30:00", submit_cutoff_at="2026-11-23 13:31:00")
        control = self.mutated("CREATE UNIQUE INDEX uniq_dzi_part1_attempts_open_user",
                               "CREATE INDEX uniq_dzi_part1_attempts_open_user")     # not unique any more
        add_attempt(control)
        add_attempt(control)

    def test_another_account_does_not_collide(self):
        add_attempt(self.conn)
        add_attempt(self.conn, user_id=51)
        add_attempt(self.conn, user_id=61)
        self.assertEqual(self.conn.execute(f"SELECT COUNT(*) FROM {ATTEMPTS}").fetchone()[0], 3)

    def test_a_fresh_open_attempt_may_follow_every_terminal_state(self):
        for label, terminal in (("submitted", SUBMITTED), ("expired", EXPIRED), ("abandoned", ABANDONED)):
            with self.subTest(label):
                add_attempt(self.conn, **terminal)
                add_attempt(self.conn)                  # the fresh open attempt ...
                self.assertRejected()                   # ... and only one
                self.conn.execute(f"DELETE FROM {ATTEMPTS}")
        # Negative controls: a predicate that ignores one terminal state still counts those attempts as open.
        base = "WHERE submitted_at IS NULL AND expired_at IS NULL AND abandoned_at IS NULL"
        for label, terminal, predicate in (
                ("submitted", SUBMITTED, "WHERE expired_at IS NULL AND abandoned_at IS NULL"),
                ("expired", EXPIRED, "WHERE submitted_at IS NULL AND abandoned_at IS NULL"),
                ("abandoned", ABANDONED, "WHERE submitted_at IS NULL AND expired_at IS NULL")):
            with self.subTest(control=label):
                control = self.mutated(base, predicate)
                add_attempt(control, **terminal)
                with self.assertRaises(sqlite3.IntegrityError):
                    add_attempt(control)

    def test_closing_an_attempt_frees_the_account_and_terminal_states_are_not_reopened_over_an_open_one(self):
        for terminal, column in ((ABANDONED, "abandoned_at"), (EXPIRED, "expired_at")):
            add_attempt(self.conn)
            self.conn.execute(f"UPDATE {ATTEMPTS} SET {column} = ?", (terminal[column],))
            add_attempt(self.conn)
            with self.assertRaises(sqlite3.IntegrityError):         # the schema is not a state machine
                self.conn.execute(f"UPDATE {ATTEMPTS} SET {column} = NULL WHERE {column} IS NOT NULL")
            self.conn.execute(f"DELETE FROM {ATTEMPTS}")


class AnswerConstraintTest(PopulatedAt026):
    def setUp(self):
        super().setUp()
        self.migrate()
        self.conn = self.connect(foreign_keys=False)
        add_attempt(self.conn)

    def assertRejected(self, **row):
        with self.assertRaises(sqlite3.IntegrityError):
            add_answer(self.conn, **row)

    def check_control(self, edit, rows, reject_only=()):
        old, new = edit
        control = self.mutated(old, new)
        for index, row in enumerate(rows):
            with self.subTest(row=row):
                self.assertRejected(**row)
                add_answer(control, **row)                          # the negative control (foreign keys are off)
                control.execute(f"DELETE FROM {ANSWERS}")
        for row in reject_only:
            with self.subTest(reject_only=row):
                self.assertRejected(**row)

    def test_each_slot_part_is_unique_within_an_attempt(self):
        add_answer(self.conn)
        self.assertRejected(question_id=22)                             # same (attempt, slot, part), another question
        add_answer(self.conn, slot_number=2, question_id=22, **{k: v for k, v in short().items()
                                                                if k not in ("slot_number", "question_id")})
        add_attempt(self.conn, user_id=51)
        add_answer(self.conn, attempt_id=2)                             # the same slot in another attempt
        control = self.mutated("PRIMARY KEY (attempt_id, slot_number, subquestion_number),", "CHECK (1),")
        add_answer(control)
        add_answer(control, question_id=22)                             # accepted without the key

    def test_the_second_unique_key_stops_only_a_repeated_question_part(self):
        add_answer(self.conn, slot_number=16, question_id=22, **{k: v for k, v in short().items()
                                                                  if k not in ("slot_number", "question_id")})
        # The same question part in another slot: refused by the (attempt, question, part) key.
        self.assertRejected(**short(slot_number=17))
        control = self.mutated("UNIQUE (attempt_id, question_id, subquestion_number),", "CHECK (1),")
        add_answer(control, **short(slot_number=16))
        add_answer(control, **short(slot_number=17))

    def test_the_second_key_is_not_the_guarantee_that_a_question_sits_in_one_slot(self):
        """Documented negative control: (slot 16, Q, part 1) with (slot 17, Q, part 2) is ACCEPTED by the table.

        Exact plan membership, one question per slot and the part set of a question are service invariants
        (design section 5.3); a later service test must show a submit never writes such a pair.
        """
        add_answer(self.conn, **short(slot_number=16, subquestion_number=1))
        add_answer(self.conn, **short(slot_number=17, subquestion_number=2))
        rows = self.conn.execute(f"SELECT slot_number, question_id, subquestion_number FROM {ANSWERS} "
                                 "ORDER BY slot_number").fetchall()
        self.assertEqual(rows, [(16, 22, 1), (17, 22, 2)])
        # Neither does the table know the plan: a question the attempt never planned is accepted.
        add_answer(self.conn, slot_number=3, question_id=23, kind="multiple_choice")

    def test_referenced_ids_and_keys_are_positive_integers(self):
        for field, edit in (("attempt_id", "CHECK (typeof(attempt_id) = 'integer' AND attempt_id > 0)"),
                            ("slot_number", "CHECK (typeof(slot_number) = 'integer' AND slot_number > 0)"),
                            ("question_id", "CHECK (typeof(question_id) = 'integer' AND question_id > 0)")):
            self.check_control((edit, "CHECK (1)"), [{field: 0}, {field: -1}])
        with self.subTest("non-integers"):
            for field in ("attempt_id", "slot_number", "question_id", "subquestion_number"):
                for bad in (1.5, "x", b"1"):          # "1" would be coerced to 1 by the column's integer affinity
                    self.assertRejected(**{field: bad})

    def test_the_part_number_is_zero_for_multiple_choice_and_one_or_more_for_short_answer(self):
        self.check_control(("CHECK (typeof(subquestion_number) = 'integer' AND subquestion_number >= 0)", "CHECK (1)"),
                           [], reject_only=[{"subquestion_number": -1}, short(subquestion_number=-1)])
        mc_edit = ("CHECK (kind <> 'multiple_choice' OR (subquestion_number = 0 AND raw_answer IS NULL)),", "CHECK (1),")
        self.check_control(mc_edit, [{"subquestion_number": 1}, {"subquestion_number": 2}])
        sa_edit = ("CHECK (kind <> 'short_answer' OR (subquestion_number >= 1 AND chosen_letter IS NULL)),", "CHECK (1),")
        self.check_control(sa_edit, [short(subquestion_number=0)])

    def test_fields_must_match_the_kind(self):
        mc_edit = ("CHECK (kind <> 'multiple_choice' OR (subquestion_number = 0 AND raw_answer IS NULL)),", "CHECK (1),")
        self.check_control(mc_edit, [{"raw_answer": "текст"}, {"raw_answer": ""}])
        sa_edit = ("CHECK (kind <> 'short_answer' OR (subquestion_number >= 1 AND chosen_letter IS NULL)),", "CHECK (1),")
        self.check_control(sa_edit, [short(chosen_letter="A"), short(chosen_letter="")])

    def test_the_kind_is_one_of_the_two_supported_kinds(self):
        self.check_control(("CHECK (typeof(kind) = 'text' AND kind IN ('multiple_choice', 'short_answer'))", "CHECK (1)"),
                           [{"kind": "practical_web"}, {"kind": ""}, {"kind": "MULTIPLE_CHOICE"}],
                           reject_only=[{"kind": None}, {"kind": 1}, {"kind": b"multiple_choice"}])

    def test_the_verdict_is_a_required_boolean(self):
        self.check_control(("CHECK (typeof(is_correct) = 'integer' AND is_correct IN (0, 1))", "CHECK (1)"),
                           [], reject_only=[{"is_correct": value} for value in (None, 2, -1, "yes", 0.5)])
        add_answer(self.conn, question_id=100)
        add_answer(self.conn, slot_number=2, question_id=101, is_correct=0, points_awarded=0)

    def test_points_are_whole_numbers_and_coupled_to_the_verdict(self):
        possible = "CHECK (typeof(points_possible) = 'integer' AND points_possible > 0)"
        self.check_control((possible, "CHECK (1)"), [],
                           reject_only=[{"points_possible": value} for value in (0, -1, 1.5, "x", None)])
        awarded = "CHECK (typeof(points_awarded) = 'integer' AND points_awarded >= 0)"
        self.check_control((awarded, "CHECK (1)"), [], reject_only=[{"points_awarded": value}
                                                                    for value in (-1, 1.5, "x", None)])
        coupling = ("CHECK (points_awarded = CASE WHEN is_correct = 1 THEN points_possible ELSE 0 END)", "CHECK (1)")
        self.check_control(coupling, [
            {"is_correct": 1, "points_possible": 3, "points_awarded": 0},       # correct but nothing awarded
            {"is_correct": 1, "points_possible": 3, "points_awarded": 2},       # a partial award
            {"is_correct": 1, "points_possible": 1, "points_awarded": 4},       # more than possible
            {"is_correct": 0, "points_possible": 3, "points_awarded": 3},       # wrong but full points
            {"is_correct": 0, "points_possible": 3, "points_awarded": 1}])
        add_answer(self.conn, points_possible=3, points_awarded=3)
        add_answer(self.conn, slot_number=2, question_id=22, is_correct=0, points_possible=3, points_awarded=0)

    def test_the_grader_version_is_non_empty_text(self):
        self.check_control(("CHECK (typeof(grader_version) = 'text' AND grader_version <> '')", "CHECK (1)"),
                           [{"grader_version": ""}], reject_only=[{"grader_version": None}, {"grader_version": b"v1"}])
        add_answer(self.conn, grader_version="v1 (2026-11)")

    def test_the_letter_and_the_raw_answer_are_text_or_null_with_no_alphabet_and_no_length_rule(self):
        for number, letter in enumerate((None, "A", "D", "Д", "E", "a", "", "Z", "opt-1"), start=200):
            add_answer(self.conn, slot_number=number, question_id=number, chosen_letter=letter)
        self.assertRejected(slot_number=300, question_id=300, chosen_letter=b"A")
        for number, raw in enumerate((None, "", "отговор", "x" * 201, "я" * 1000, "z" * 70_000, "w" * 1_000_000),
                                     start=400):
            add_answer(self.conn, **short(slot_number=number, question_id=number, raw_answer=raw))
        self.assertEqual(self.conn.execute(f"SELECT MAX(length(raw_answer)) FROM {ANSWERS}").fetchone()[0], 1_000_000)
        for bad in (b"blob",):                   # a number would be coerced to text by the column's affinity
            with self.subTest(raw=bad):
                self.assertRejected(**short(slot_number=500, question_id=500, raw_answer=bad))

    def test_an_unanswered_item_is_representable_for_both_kinds(self):
        add_answer(self.conn, chosen_letter=None, is_correct=0, points_awarded=0)
        add_answer(self.conn, **short(raw_answer=None))
        self.assertEqual(self.conn.execute(f"SELECT kind, chosen_letter, raw_answer, is_correct FROM {ANSWERS} "
                                           "ORDER BY slot_number").fetchall(),
                         [("multiple_choice", None, None, 0), ("short_answer", None, None, 0)])

    def test_required_fields_cannot_be_null(self):
        for field in ANSWER_COLUMNS:
            if field in ("chosen_letter", "raw_answer"):
                continue
            with self.subTest(null=field):
                self.assertRejected(**{field: None})


class ForeignKeyTest(PopulatedAt026):
    """Foreign keys ON: references must resolve, and RESTRICT blocks deleting a referenced parent."""

    def setUp(self):
        super().setUp()
        self.migrate()
        self.conn = self.connect()
        # Parents that ONLY Part 1 rows reference (the legacy fixture uses account 52 and question 21 elsewhere):
        # account 62, blueprint 6 and question 22, so each refusal below is the Part 1 key's alone. Blueprint 5 and
        # question 23 are referenced only by Part 1 too, through the second attempt.
        add_attempt(self.conn, user_id=62, blueprint_id=6, **SUBMITTED)
        add_answer(self.conn, attempt_id=1, question_id=22)
        add_attempt(self.conn, user_id=52, blueprint_id=5)
        self.conn.commit()

    def test_references_must_resolve(self):
        for row in ({"user_id": 9999}, {"blueprint_id": 9999}):
            with self.subTest(row=row), self.assertRaises(sqlite3.IntegrityError):
                add_attempt(self.conn, **dict({"user_id": 51}, **row))
        for row in ({"attempt_id": 9999}, {"question_id": 9999}):
            with self.subTest(row=row), self.assertRaises(sqlite3.IntegrityError):
                add_answer(self.conn, **dict({"attempt_id": 1, "slot_number": 3, "question_id": 21}, **row))

    def test_a_referenced_parent_cannot_be_deleted(self):
        for label, sql in (("account", "DELETE FROM users WHERE id = 62"),
                           ("blueprint", "DELETE FROM dzi_blueprints WHERE id = 6"),
                           ("attempt with answers", f"DELETE FROM {ATTEMPTS} WHERE id = 1"),
                           ("question with answers", "DELETE FROM questions WHERE id = 22"),
                           ("account of an open attempt", "DELETE FROM users WHERE id = 52")):
            with self.subTest(label), self.assertRaises(sqlite3.IntegrityError):
                self.conn.execute(sql)
        self.assertEqual(self.conn.execute(f"SELECT COUNT(*) FROM {ATTEMPTS}").fetchone()[0], 2)
        self.assertEqual(self.conn.execute(f"SELECT COUNT(*) FROM {ANSWERS}").fetchone()[0], 1)

    def test_restrict_is_not_a_retention_policy(self):
        """RESTRICT only refuses while a reference remains; a future cleanup could remove dependants first."""
        self.conn.execute(f"DELETE FROM {ANSWERS}")
        self.conn.execute(f"DELETE FROM {ATTEMPTS}")
        for sql in ("DELETE FROM users WHERE id = 62", "DELETE FROM dzi_blueprints WHERE id = 6",
                    "DELETE FROM questions WHERE id = 22"):
            self.conn.execute(sql)
        self.assertEqual(self.conn.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_without_restrict_a_cascade_would_delete_history(self):
        """Negative controls: the RESTRICT tests above are not passing because of a default."""
        for old, delete in (("REFERENCES users(id) ON DELETE RESTRICT", "DELETE FROM users WHERE id = 62"),
                            ("REFERENCES dzi_blueprints(id) ON DELETE RESTRICT",
                             "DELETE FROM dzi_blueprints WHERE id = 6")):
            with self.subTest(old=old):
                control = self.mutated(old, old.replace("RESTRICT", "CASCADE"), foreign_keys=True)
                add_attempt(control, user_id=62, blueprint_id=6, **SUBMITTED)
                control.execute(delete)
                self.assertEqual(control.execute(f"SELECT COUNT(*) FROM {ATTEMPTS}").fetchone()[0], 0)
        control = self.mutated("REFERENCES dzi_part1_attempts(id) ON DELETE RESTRICT",
                               "REFERENCES dzi_part1_attempts(id) ON DELETE CASCADE", foreign_keys=True)
        add_attempt(control, user_id=62, blueprint_id=6, **SUBMITTED)
        add_answer(control, attempt_id=1, question_id=22)
        control.execute(f"DELETE FROM {ATTEMPTS}")
        self.assertEqual(control.execute(f"SELECT COUNT(*) FROM {ANSWERS}").fetchone()[0], 0)
        control = self.mutated("REFERENCES questions(id) ON DELETE RESTRICT",
                               "REFERENCES questions(id) ON DELETE CASCADE", foreign_keys=True)
        add_attempt(control, user_id=62, blueprint_id=6, **SUBMITTED)
        add_answer(control, attempt_id=1, question_id=22)
        control.execute("DELETE FROM questions WHERE id = 22")
        self.assertEqual(control.execute(f"SELECT COUNT(*) FROM {ANSWERS}").fetchone()[0], 0)


class FreshBuildAndExportTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._directory = tempfile.TemporaryDirectory(prefix="dzi-part1-fresh-")
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
                                (ATTEMPTS, ANSWERS)).fetchall()

    def test_a_fresh_build_has_both_tables_empty_and_the_upgrade_definition(self):
        with closing(sqlite3.connect(self.fresh)) as conn:
            for table in (ATTEMPTS, ANSWERS):
                self.assertEqual(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0)
            self.assertEqual(ct.classification_problems(conn), [])
            self.assertEqual(parity.compare_to_canonical(conn), [])
        self.assertEqual(self.definitions(self.fresh), self.definitions(self.upgraded))
        self.assertEqual(len(self.definitions(self.fresh)), 6)       # two tables, two indexes, two answer-key autoindexes

    def test_both_tables_are_runtime_and_never_canonical(self):
        tables = {ATTEMPTS, ANSWERS}
        self.assertEqual(tables - set(ct.tables_of(ct.RUNTIME)), set())
        self.assertEqual(tables & set(ct.tables_of(ct.CANONICAL) + ct.tables_of(ct.OPERATIONAL)), set())
        self.assertEqual(tables - set(tool.NONCANONICAL_TABLES), set())
        manifest = export.load_manifest(ct.CANONICAL_DIR)
        self.assertEqual(tables - set(manifest["runtime_exclusions"]), set())
        self.assertEqual(tables & set(manifest["canonical_tables"]), set())
        self.assertEqual(tables - set(manifest["classification"]["runtime"]), set())
        self.assertIn(MIGRATION, manifest["schema"]["migrations"])
        self.assertEqual(manifest["schema"]["migration_level"], MIGRATION)
        self.assertEqual(manifest["schema"]["migrations"][-2:], ["026_practice_runtime.sql", MIGRATION])
        for table in tables:
            self.assertFalse((ct.CANONICAL_DIR / f"{table}.jsonl").exists())
            self.assertNotIn(table, manifest["tables"])

    def test_export_of_a_fresh_build_reproduces_every_tracked_byte(self):
        out = self.root / "export-fresh"
        with closing(sqlite3.connect(self.fresh)) as conn:
            export.export_database(conn, out, sources=export.load_manifest(ct.CANONICAL_DIR)["sources"])
        self.assertEqual(sorted(p.name for p in out.iterdir()), sorted(p.name for p in ct.CANONICAL_DIR.iterdir()))
        for path in ct.CANONICAL_DIR.iterdir():
            self.assertEqual((out / path.name).read_bytes(), path.read_bytes(), path.name)

    def test_export_from_a_database_with_part1_rows_leaves_them_out(self):
        conn = self.copy("rows.db")
        conn.execute("PRAGMA foreign_keys = ON")
        user = conn.execute("INSERT INTO users (display_name, role, session_key) VALUES ('Ученик', 'student', ?)",
                            ("k" * 32,)).lastrowid
        blueprint = conn.execute("SELECT MIN(id) FROM dzi_blueprints").fetchone()[0]
        question = conn.execute("SELECT MIN(id) FROM questions").fetchone()[0]
        add_attempt(conn, user_id=user, blueprint_id=blueprint, question_ids_json=json.dumps([question]),
                    **SUBMITTED)
        add_answer(conn, attempt_id=1, question_id=question)
        conn.commit()
        out = self.root / "export-rows"
        export.export_database(conn, out, sources=export.load_manifest(ct.CANONICAL_DIR)["sources"])
        self.assertEqual({p.name for p in out.iterdir()}, {p.name for p in ct.CANONICAL_DIR.iterdir()})
        for path in ct.CANONICAL_DIR.iterdir():
            self.assertEqual((out / path.name).read_bytes(), path.read_bytes(), path.name)
        # A database holding runtime rows is not a canonical rebuild (runtime exclusion is enforced, not assumed).
        problems = parity.compare_to_canonical(conn)
        self.assertTrue(any(p.startswith(f"{ATTEMPTS}: 1 rows") for p in problems), problems)
        self.assertTrue(any(p.startswith(f"{ANSWERS}: 1 rows") for p in problems), problems)


class ReferenceValidatorTest(unittest.TestCase):
    """Both runtime question-reference validators see the two question columns and not ``slot_format_json``.

    The rows below are corrupted on disposable copies with foreign keys OFF, the only way to reach the guards:
    the schema's own foreign key keeps ``dzi_part1_answers.question_id`` honest.
    """

    @classmethod
    def setUpClass(cls):
        cls._directory = tempfile.TemporaryDirectory(prefix="dzi-part1-references-")
        cls.root = Path(cls._directory.name)
        cls.fresh = build_test_database(cls.root / "fresh.db")
        with closing(sqlite3.connect(cls.fresh)) as conn:
            cls.question_ids = [row[0] for row in conn.execute("SELECT id FROM questions ORDER BY id LIMIT 2")]
            cls.blueprint = conn.execute("SELECT MIN(id) FROM dzi_blueprints").fetchone()[0]

    @classmethod
    def tearDownClass(cls):
        cls._directory.cleanup()

    def database(self, plan, *, answer_question=None, slots=SLOT_FORMAT, foreign_keys=False):
        path = self.root / f"db-{self.id().rsplit('.', 1)[-1]}-{abs(hash((plan, answer_question, slots)))}.db"
        shutil.copyfile(self.fresh, path)
        conn = sqlite3.connect(path)
        self.addCleanup(conn.close)
        conn.execute(f"PRAGMA foreign_keys = {'ON' if foreign_keys else 'OFF'}")
        user = conn.execute("INSERT INTO users (display_name, role, session_key) VALUES ('Ученик', 'student', ?)",
                            ("k" * 32,)).lastrowid
        add_attempt(conn, user_id=user, blueprint_id=self.blueprint, question_ids_json=plan, slot_format_json=slots,
                    **SUBMITTED)
        if answer_question is not None:
            add_answer(conn, attempt_id=1, question_id=answer_question)
        conn.commit()
        return conn

    def validators(self, conn):
        ids = {row[0] for row in conn.execute("SELECT id FROM questions")}
        return (parity.runtime_reference_problems(conn, ids), parity.embedded_reference_problems(conn),
                tool.reference_gate(conn))

    def test_valid_rows_raise_nothing(self):
        a, b = self.question_ids
        conn = self.database(json.dumps([a, b]), answer_question=a, foreign_keys=True)
        self.assertEqual(self.validators(conn), ([], [], {}))

    def test_an_unresolved_id_in_the_plan_is_reported_by_both(self):
        for label, plan in (("list", json.dumps([self.question_ids[0], 9_999_999])),
                            ("dict", json.dumps({"question_ids": [9_999_999]})),
                            ("top-level integer", "9999999")):
            with self.subTest(label):
                runtime, embedded, gate = self.validators(self.database(plan))
                self.assertEqual([p.split(":")[0] for p in runtime], ["dzi_part1_attempts.question_ids_json"])
                self.assertEqual(embedded, runtime)
                self.assertEqual(gate, {("dzi_part1_attempts.question_ids_json", "preexisting_unresolved_reference"): 1})

    def test_an_unresolved_answer_question_is_reported_by_both(self):
        conn = self.database(json.dumps([self.question_ids[0]]), answer_question=9_999_999)
        runtime, embedded, gate = self.validators(conn)
        self.assertEqual([p.split(":")[0] for p in runtime], ["dzi_part1_answers.question_id"])
        self.assertEqual(embedded, runtime)
        self.assertEqual(gate[("dzi_part1_answers.question_id", "preexisting_unresolved_reference")], 1)
        self.assertIn(("dzi_part1_answers.foreign_key", "preexisting_fk_violation"), gate)   # the FK check sees it too

    def test_slot_format_integers_never_enter_either_reference_check(self):
        """Points, and any other integer inside slot_format_json, are not question ids (design section 5.4)."""
        slots = json.dumps([{"kind": "multiple_choice", "points": 9_999_999}, {"kind": "short_answer", "points": 3,
                                                                                "question_id": 8_888_888}])
        conn = self.database(json.dumps([self.question_ids[0]]), slots=slots)
        self.assertEqual(self.validators(conn), ([], [], {}))
        self.assertNotIn("slot_format_json", " ".join(f"{t}.{c}" for t, c in tool.JSON_ID_COLUMNS))
        for module in (parity, tool):                                    # code only: comments may name the column
            code = "\n".join(line for line in Path(module.__file__).read_text(encoding="utf-8").splitlines()
                             if not line.lstrip().startswith("#"))
            self.assertNotIn("slot_format_json", code, module.__name__)

    def test_the_gate_keeps_its_fixed_codes_for_malformed_and_noninteger_plans(self):
        a = self.question_ids[0]
        cases = {"{bad": "malformed_reference_json", "": "malformed_reference_json", "   ": "malformed_reference_json",
                 json.dumps([str(a)]): "noninteger_reference_value", f"[{a}.0]": "noninteger_reference_value"}
        for plan, code in cases.items():
            with self.subTest(plan=plan):
                gate = tool.reference_gate(self.database(plan))
                self.assertEqual(gate, {("dzi_part1_attempts.question_ids_json", code): 1})
        with self.subTest("a boolean member is ignored"):
            self.assertEqual(tool.reference_gate(self.database(f"[{a}, true]")), {})

    def test_a_flat_plan_is_read_identically_and_a_nested_document_is_not(self):
        """Characterization (design section 5.4): why question_ids_json must hold nothing but ids."""
        flat = "[5, 6, 7]"
        self.assertEqual(sorted(tool._json_ids(flat)[0]), sorted(parity._ids_in(flat)))
        nested = '[{"slot": 16, "question_id": 5}]'
        self.assertEqual(sorted(tool._json_ids(nested)[0]), [5, 16])      # the updater reads every integer
        self.assertEqual(sorted(parity._ids_in(nested)), [5])             # parity reads list items and id-like keys

    def test_the_validators_still_check_the_original_columns(self):
        """The new columns were added beside the old ones, not instead of them."""
        conn = self.database(json.dumps([self.question_ids[0]]))
        conn.execute("INSERT INTO generated_exams (exam_name, question_ids) VALUES ('x', '[9999999]')")
        runtime, _embedded, gate = self.validators(conn)
        self.assertEqual([p.split(":")[0] for p in runtime], ["generated_exams.question_ids"])
        self.assertEqual(list(gate), [("generated_exams.question_ids", "preexisting_unresolved_reference")])


if __name__ == "__main__":
    unittest.main()
