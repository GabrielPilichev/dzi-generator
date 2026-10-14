"""022 adds the issuance identity of the registration send ledger (EMAIL_REGISTRATION PR 3).

One nullable column, ``registration_sends.issuance_id``, and one partial unique index.
No row is written: every ledger row that exists when the migration runs keeps what it
had and has no identity, so the service neither sends nor settles it. Every database
in this module is temporary: built by the shared migration runner from the repository's
migration files, or cloned by ``tests.support.build_test_database``. None of them is
``data/questions.db``.
"""

import io
import json
import logging
import re
import shutil
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from src import build_database as builder
from src import canonical_export as export
from src import canonical_tables as ct
from src import manage_migrations
from tests.support import ROOT, build_test_database
from tests.test_assignment_public_token_migration_sql import POPULATE_SQL
from tests.test_user_identities_migration_sql import ACCOUNTS_SQL
from web.services import registration

MIGRATIONS = ROOT / "web" / "migrations"
MIGRATION = "022_registration_issuance_identity.sql"
PREVIOUS = "021_registration_schema.sql"
TABLE = "registration_sends"
INDEX = "idx_registration_sends_issuance"
OLD_COLUMNS = ("id", "pending_id", "email_hash", "kind", "attempted_at", "outcome", "settled_at")
HASH = "a1" * 32
OTHER_HASH = "b2" * 32
IDENTITY = "0123456789abcdef" * 2
STAMP = "2026-10-13 12:00:00"
NOW = datetime(2026, 10, 13, 12, 0, 30, tzinfo=timezone.utc)

# What a database at 021 could hold: accounts with profiles, registrations in every
# state, and ledger rows with every outcome.
REGISTRATION_SQL = f"""
INSERT INTO user_profiles (user_id, account_type, first_name, last_name, grade, grade_set_at)
    VALUES (52, 'student', 'Ана', 'Иванова', 9, '{STAMP}');
INSERT INTO user_profiles (user_id, account_type, first_name, last_name, school_name)
    VALUES (51, 'teacher', 'Иван', 'Петров', 'СУ „Васил Левски“');
INSERT INTO teacher_subjects (user_id, position, subject_id, label) VALUES (51, 1, 'it', 'Информационни технологии');
INSERT INTO pending_registrations (id, binding_hash, account_type, state, is_shadow, email, first_name, last_name,
        grade, password_hash, code_hash, code_expires_at, code_send_id, send_count, resend_after, created_at, expires_at)
    VALUES (7, '{HASH}', 'student', 'pending', 0, 'old@example.bg', 'Стар', 'Запис', 10, 'scrypt:old',
            '{OTHER_HASH}', '2026-10-13 12:10:00', 3, 1, '2026-10-13 12:01:00', '{STAMP}', '2026-10-13 12:30:00');
INSERT INTO pending_registrations (id, binding_hash, account_type, state, is_shadow, created_at, expires_at, closed_at)
    VALUES (8, '{OTHER_HASH}', 'student', 'void', 0, '{STAMP}', '{STAMP}', '{STAMP}');
INSERT INTO registration_sends (id, pending_id, email_hash, kind, attempted_at, outcome, settled_at) VALUES
    (1, 8, '{OTHER_HASH}', 'code', '2026-10-13 11:59:00', 'accepted', '2026-10-13 11:59:01'),
    (2, 8, '{OTHER_HASH}', 'notice', '2026-10-13 11:59:10', 'unknown', '2026-10-13 11:59:20'),
    (3, 7, '{HASH}', 'code', '{STAMP}', 'started', NULL),
    (4, NULL, '{HASH}', 'code', '2026-10-13 11:58:00', 'not_sent', '2026-10-13 11:58:01'),
    (5, 424242, '{HASH}', 'code', '2026-10-13 11:57:00', 'refused', '2026-10-13 11:57:01');
INSERT INTO registration_control (id, high_water_at, hold_reason, hold_set_at) VALUES (1, '{STAMP}', 'restore', '{STAMP}');
"""


def apply(path, migrations_dir=MIGRATIONS):
    out = io.StringIO()
    code = manage_migrations.apply_migrations(path, migrations_dir, out=out)
    return code, out.getvalue()


def migration_text() -> str:
    return (MIGRATIONS / MIGRATION).read_text(encoding="utf-8")


def statements() -> str:
    """The migration without its comments."""
    return "\n".join(line for line in migration_text().splitlines() if not line.lstrip().startswith("--"))


class UpgradedDatabaseTestCase(unittest.TestCase):
    """A database at migration 021 that already holds accounts, profiles and registration rows."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="issuance-migration-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.path = self.root / "upgrade.db"
        sqlite3.connect(self.path).close()
        previous = self.root / "through-021"
        previous.mkdir()
        # 000..022 only: what 022 itself changes, whatever later migrations add.
        self.through = self.root / "through-022"
        self.through.mkdir()
        for path in sorted(MIGRATIONS.glob("*.sql")):
            if path.name < MIGRATION:
                shutil.copyfile(path, previous / path.name)
            if path.name <= MIGRATION:
                shutil.copyfile(path, self.through / path.name)
        self.assertEqual(apply(self.path, previous)[0], 0)
        self.conn = None
        self.reopen()
        self.conn.executescript(POPULATE_SQL + ACCOUNTS_SQL + REGISTRATION_SQL)
        self.conn.commit()

    def reopen(self):
        """A new connection: one opened before the migration answers pragmas from its cached schema."""
        if self.conn is not None:
            self.conn.close()
        self.conn = sqlite3.connect(self.path)
        self.addCleanup(self.conn.close)
        self.conn.execute("PRAGMA foreign_keys = ON")

    def migrate(self):
        code, report = apply(self.path, self.through)
        self.assertEqual(code, 0, report)
        self.reopen()
        return report

    def contents(self):
        return {name: self.conn.execute(f'SELECT * FROM "{name}" ORDER BY 1, 2').fetchall()
                for (name,) in self.conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' AND name != 'schema_migrations'")}

    def schema_objects(self):
        """Everything sqlite_master records except the ledger table and its indexes."""
        return self.conn.execute(
            "SELECT type, name, tbl_name, rootpage, sql FROM sqlite_master WHERE tbl_name != ? "
            "ORDER BY type, name", (TABLE,)).fetchall()

    def insert(self, **values):
        row = {"email_hash": HASH, "kind": "code", "attempted_at": STAMP}
        row.update(values)
        return self.conn.execute(
            f"INSERT INTO {TABLE} ({', '.join(row)}) VALUES ({', '.join('?' for _ in row)})", tuple(row.values()))


class IssuanceUpgradeTest(UpgradedDatabaseTestCase):
    def test_upgrade_from_021_writes_no_row_and_invents_no_identity(self):
        before, objects_before = self.contents(), self.schema_objects()
        root = self.conn.execute("SELECT rootpage FROM sqlite_master WHERE name = ?", (TABLE,)).fetchone()
        ledger_before = self.conn.execute("SELECT filename, applied_at FROM schema_migrations ORDER BY rowid").fetchall()
        self.assertEqual(ledger_before[-1][0], PREVIOUS)
        self.assertEqual(len(before[TABLE]), 5)
        self.migrate()
        after = self.contents()
        # The ledger: every old row column for column, and no identity for any of them.
        self.assertEqual(after.pop(TABLE), [(*row, None) for row in before.pop(TABLE)])
        # Users, profiles, pending registrations, the control row, assignments, attempts,
        # second factors, content and every sequence: untouched.
        self.assertEqual(after, before)
        for table in ("users", "user_profiles", "teacher_subjects", "pending_registrations", "registration_control",
                      "quiz_assignments", "quiz_attempts", "sqlite_sequence"):
            self.assertTrue(before[table], table)
        self.assertEqual(self.schema_objects(), objects_before)
        self.assertEqual(self.conn.execute("SELECT rootpage FROM sqlite_master WHERE name = ?", (TABLE,)).fetchone(),
                         root)  # altered in place, not rebuilt
        ledger = self.conn.execute("SELECT filename, applied_at FROM schema_migrations ORDER BY rowid").fetchall()
        self.assertEqual(ledger[:-1], ledger_before)
        self.assertEqual(ledger[-1][0], MIGRATION)
        self.assertEqual(self.conn.execute("PRAGMA foreign_key_check").fetchall(), [])
        self.assertEqual(self.conn.execute("PRAGMA integrity_check").fetchall(), [("ok",)])

    def test_ledger_gains_exactly_one_nullable_column_at_the_end(self):
        old = list(self.conn.execute(f"PRAGMA table_info({TABLE})"))
        self.assertEqual(tuple(row[1] for row in old), OLD_COLUMNS)
        self.migrate()
        new = list(self.conn.execute(f"PRAGMA table_info({TABLE})"))
        self.assertEqual(new[:-1], old)
        cid, name, kind, notnull, default, pk = new[-1]
        self.assertEqual((name, kind, notnull, default, pk), ("issuance_id", "TEXT", 0, None, 0))
        indexes = {row[1]: (row[2], row[4]) for row in self.conn.execute(f"PRAGMA index_list({TABLE})")}
        self.assertEqual(indexes, {INDEX: (1, 1), "idx_registration_sends_email": (0, 0),
                                   "idx_registration_sends_time": (0, 0)})  # (unique, partial)
        self.assertEqual([row[2] for row in self.conn.execute(f"PRAGMA index_info({INDEX})")], ["issuance_id"])
        self.assertEqual(self.conn.execute(f"PRAGMA foreign_key_list({TABLE})").fetchall(), [])

    def test_accounting_history_counts_exactly_as_before(self):
        """Caps and the circuit read the old rows as they did: nothing is refunded or forgotten."""
        def readings():
            return (registration._count_since(self.conn, NOW, registration.HOUR),
                    registration._count_since(self.conn, NOW, registration.HOUR, HASH),
                    registration._count_since(self.conn, NOW, registration.DAILY_WINDOW, OTHER_HASH),
                    registration.address_capped(self.conn, HASH, NOW),
                    registration._circuit_open(self.conn, NOW),
                    self.conn.execute("SELECT seq FROM sqlite_sequence WHERE name = ?", (TABLE,)).fetchone())

        before = readings()
        self.assertEqual(before[:3], (5, 3, 2))
        self.migrate()
        self.assertEqual(readings(), before)
        # The next generation still gets the next number.
        self.assertEqual(self.insert(issuance_id=IDENTITY).lastrowid, 6)

    def test_old_rows_fail_closed_for_settlement_and_dispatch(self):
        self.migrate()
        before = self.contents()
        with self.assertLogs("learnpilot.registration", level=logging.WARNING) as logs:
            for send_id in (1, 2, 3, 4, 5):
                for identity in (IDENTITY, registration._new_issuance_id()):
                    self.conn.execute("BEGIN IMMEDIATE")
                    result = registration.settle_send(self.conn, send_id, identity, "not_sent", clock=lambda: NOW)
                    self.conn.commit()
                    self.assertIs(result, registration.Settlement.NOT_THIS_ISSUANCE, send_id)
        self.assertEqual(len(logs.output), 10)
        self.assertEqual(self.contents(), before)
        # The one unsettled old generation: its row is pending and current, and it is still not sendable.
        row = self.conn.execute("SELECT state, code_send_id, email FROM pending_registrations WHERE id = 7").fetchone()
        self.assertEqual(row, ("pending", 3, "old@example.bg"))
        message = registration._code_message("old@example.bg", "123456")
        for identity in (None, IDENTITY):
            issued = registration.Result(registration.Outcome.ISSUED, pending_id=7, send_id=3, kind="code",
                                         issuance_id=identity, message=message)
            sent = []
            with self.assertRaises(registration.NotSendable):
                registration.dispatch(self.conn, type("M", (), {"send": lambda self, m: sent.append(m)})(), issued,
                                      secret_key="key")
            self.assertEqual(sent, [])
        self.assertEqual(self.contents(), before)
        self.assertEqual(self.conn.execute(f"SELECT COUNT(*) FROM {TABLE} WHERE issuance_id IS NOT NULL").fetchone()[0], 0)

    def test_cleanup_still_removes_old_rows_by_age(self):
        self.migrate()
        later = datetime(2026, 10, 15, 12, 0, 0, tzinfo=timezone.utc)  # 48 hours after the newest
        self.conn.execute("UPDATE registration_control SET hold_reason = NULL, hold_set_at = NULL")
        self.conn.commit()
        self.conn.execute("BEGIN IMMEDIATE")
        result = registration.cleanup(self.conn, clock=lambda: later)
        self.conn.commit()
        self.assertEqual(result.deleted_sends, 5)

    def test_it_is_a_plain_additive_migration_not_a_rebuild(self):
        dry = io.StringIO()
        manage_migrations.dry_run(self.path, self.through, out=dry)
        self.assertIn(f"pending migrations:\n  - {MIGRATION}\n", dry.getvalue())
        self.assertNotIn("issuance_id", [row[1] for row in self.conn.execute(f"PRAGMA table_info({TABLE})")])
        report = self.migrate()
        self.assertIn(f"applied migrations:\n  - {MIGRATION}\n", report)
        self.assertIn("foreign_key_check: ok", report)
        self.assertNotIn("table-rebuild", report)
        self.assertFalse(manage_migrations.is_table_rebuild_migration(MIGRATION, migration_text()))

    def test_reapplying_is_a_noop_in_the_runner(self):
        self.migrate()
        self.insert(issuance_id=IDENTITY)
        self.conn.commit()
        before = self.contents()
        report = self.migrate()
        self.assertIn("applied migrations:\n  - none\n", report)
        self.assertEqual(self.contents(), before)

    def test_upgraded_schema_equals_a_fresh_database(self):
        self.migrate()
        fresh_path = self.root / "fresh.db"
        sqlite3.connect(fresh_path).close()
        self.assertEqual(apply(fresh_path, self.through)[0], 0)
        schema = "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name"
        with closing(sqlite3.connect(fresh_path)) as fresh:
            self.assertEqual(fresh.execute(schema).fetchall(), self.conn.execute(schema).fetchall())
            tables = [row[0] for row in fresh.execute("SELECT name FROM sqlite_master WHERE type = 'table'")]
            for table in tables:
                for pragma in ("table_info", "foreign_key_list", "index_list"):
                    self.assertEqual(fresh.execute(f"PRAGMA {pragma}({table})").fetchall(),
                                     self.conn.execute(f"PRAGMA {pragma}({table})").fetchall(), (table, pragma))
            self.assertEqual(fresh.execute(f"SELECT COUNT(*) FROM {TABLE}").fetchone()[0], 0)
            ledger = [row[0] for row in fresh.execute("SELECT filename FROM schema_migrations ORDER BY rowid")]
        self.assertEqual(ledger[-2:], [PREVIOUS, MIGRATION])

    def test_022_is_never_adopted_without_running_it(self):
        self.conn.row_factory = sqlite3.Row
        self.assertEqual(manage_migrations.baseline_status(self.conn, MIGRATION)[0], "manual review required")


class IssuanceSchemaTest(UpgradedDatabaseTestCase):
    def setUp(self):
        super().setUp()
        self.migrate()

    def test_identity_is_null_or_32_bytes_of_lower_case_hex(self):
        refused = (
            "", "0", IDENTITY[:31], IDENTITY + "0", IDENTITY.upper(), "g" * 32, " " + IDENTITY[:31],
            IDENTITY[:31] + "\n", IDENTITY + "\x00", IDENTITY[:16] + "\x00" + IDENTITY[:15], "ж" * 32, "ж" * 16,
            IDENTITY.encode("ascii"), b"\x01" * 32, 0, 1.5, int(IDENTITY[:15], 16),
        )
        for value in refused:
            with self.subTest(value=value), self.assertRaises(sqlite3.IntegrityError):
                self.insert(issuance_id=value)
        self.insert(issuance_id=IDENTITY)
        self.insert(issuance_id="f" * 32)
        self.insert(issuance_id="0" * 32)
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(f"UPDATE {TABLE} SET issuance_id = 'short' WHERE id = 1")
        # The service's own identifiers satisfy the constraint.
        for _ in range(50):
            self.insert(issuance_id=registration._new_issuance_id())

    def test_identity_is_unique_and_absent_identities_are_not(self):
        first = self.insert(issuance_id=IDENTITY).lastrowid
        with self.assertRaises(sqlite3.IntegrityError) as caught:
            self.insert(issuance_id=IDENTITY, email_hash=OTHER_HASH, kind="notice")
        self.assertIn(f"{TABLE}.issuance_id", str(caught.exception))
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(f"UPDATE {TABLE} SET issuance_id = ? WHERE id = 1", (IDENTITY,))
        # Rows without an identity: the five old ones and any number more.
        self.insert()
        self.insert(issuance_id=None)
        self.assertEqual(self.conn.execute(f"SELECT COUNT(*) FROM {TABLE} WHERE issuance_id IS NULL").fetchone()[0], 7)
        # A deleted generation frees its identity; nothing else depends on it.
        self.conn.execute(f"DELETE FROM {TABLE} WHERE id = ?", (first,))
        self.insert(issuance_id=IDENTITY)

    def test_default_is_no_identity_and_the_older_checks_still_hold(self):
        new = self.insert().lastrowid
        self.assertEqual(self.conn.execute(f"SELECT outcome, settled_at, issuance_id FROM {TABLE} WHERE id = ?",
                                           (new,)).fetchone(), ("started", None, None))
        for values in ({"kind": "sms"}, {"outcome": "sent"}, {"email_hash": "x"}, {"attempted_at": None}):
            with self.subTest(values=values), self.assertRaises(sqlite3.IntegrityError):
                self.insert(issuance_id=registration._new_issuance_id(), **values)


class IssuanceMigrationFileTest(unittest.TestCase):
    def test_the_file_adds_one_column_and_one_index(self):
        self.assertEqual(re.findall(r"ALTER TABLE (\w+) ADD COLUMN (\w+)", statements()), [(TABLE, "issuance_id")])
        self.assertEqual(re.findall(r"CREATE (UNIQUE )?INDEX (\w+)\s+ON (\w+)\((\w+)\) WHERE (.+);", statements()),
                         [("UNIQUE ", INDEX, TABLE, "issuance_id", "issuance_id IS NOT NULL")])
        self.assertEqual(re.findall(r"CREATE TABLE", statements()), [])
        self.assertEqual(len(re.findall(r";", statements())), 3)  # the pragma, the column, the index

    def test_nothing_destructive_no_backfill_and_no_default(self):
        text = statements()
        for forbidden in ("DROP ", "DELETE ", "UPDATE ", "INSERT ", "RENAME ", "DEFAULT", "NOT NULL,", "randomblob",
                          "PRAGMA writable_schema", "users", "pending_registrations", "code", "email"):
            self.assertNotIn(forbidden, text, forbidden)

    def test_the_files_are_numbered_without_a_collision(self):
        names = sorted(path.name for path in MIGRATIONS.glob("*.sql"))
        self.assertEqual([name for name in names if name.startswith("022_")], [MIGRATION])
        self.assertEqual(names[names.index(MIGRATION) - 1], PREVIOUS)
        self.assertEqual([name[:3] for name in names], [f"{n:03d}" for n in range(len(names))])
        mentions = [name for name in names if "issuance_id" in "\n".join(
            line for line in (MIGRATIONS / name).read_text(encoding="utf-8").splitlines()
            if not line.lstrip().startswith("--"))]
        self.assertEqual(mentions, [MIGRATION])


class IssuanceClassificationTest(unittest.TestCase):
    def test_the_ledger_stays_runtime_and_the_manifest_records_the_level(self):
        self.assertEqual(ct.TABLE_CLASSIFICATION[TABLE][0], ct.RUNTIME)
        self.assertNotIn(TABLE, ct.tables_of(ct.CANONICAL))
        self.assertFalse((ROOT / "data" / "canonical" / f"{TABLE}.jsonl").exists())
        manifest = json.loads((ROOT / "data" / "canonical" / "manifest.json").read_text(encoding="utf-8"))
        names = sorted(path.name for path in MIGRATIONS.glob("*.sql"))
        self.assertEqual(manifest["schema"]["migrations"], names)
        self.assertEqual(manifest["schema"]["migration_level"], names[-1])
        self.assertIn(MIGRATION, names)
        self.assertIn(TABLE, manifest["runtime_exclusions"])
        self.assertIn(TABLE, manifest["classification"]["runtime"])
        self.assertNotIn(TABLE, manifest["canonical_tables"])
        self.assertNotIn(TABLE, manifest["tables"])
        self.assertEqual(export.check_directory(ct.CANONICAL_DIR), [])

    def test_a_fresh_canonical_build_has_the_column_and_no_ledger_row(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = build_test_database(Path(tmp) / "fresh.db")
            with closing(sqlite3.connect(path)) as conn:
                applied = [r[0] for r in conn.execute("SELECT filename FROM schema_migrations ORDER BY filename")]
                self.assertIn(MIGRATION, applied)
                self.assertEqual(tuple(r[1] for r in conn.execute(f"PRAGMA table_info({TABLE})")),
                                 (*OLD_COLUMNS, "issuance_id"))
                self.assertEqual(conn.execute(f"SELECT COUNT(*) FROM {TABLE}").fetchone()[0], 0)
                self.assertIsNone(ct.stored_sequence(conn, TABLE))
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM users").fetchone()[0], 0)
                self.assertEqual(ct.classification_problems(conn), [])
            self.assertEqual(builder.validation_problems(path), [])

    def test_an_export_holds_no_identity_and_is_the_tracked_content_byte_for_byte(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = build_test_database(root / "working.db")
            with closing(sqlite3.connect(path)) as conn:
                conn.execute(f"INSERT INTO {TABLE} (email_hash, kind, attempted_at, issuance_id) "
                             "VALUES (?, 'code', ?, ?)", (HASH, STAMP, IDENTITY))
                conn.execute(f"INSERT INTO {TABLE} (email_hash, kind, attempted_at) VALUES (?, 'notice', ?)",
                             (OTHER_HASH, STAMP))
                conn.commit()
            out = root / "export"
            with closing(export.open_snapshot(path)) as conn:
                manifest = export.export_database(conn, out)
            tracked = export.load_manifest(ct.CANONICAL_DIR)
            self.assertEqual(manifest["tables"], tracked["tables"])
            self.assertEqual(manifest["schema"], tracked["schema"])
            self.assertEqual(manifest["classification"], tracked["classification"])
            self.assertNotIn(TABLE, manifest["tables"])
            self.assertEqual(sorted(p.name for p in out.iterdir()),
                             sorted([f"{table}.jsonl" for table in ct.tables_of(ct.CANONICAL)] + [ct.MANIFEST_NAME]))
            for table in ct.tables_of(ct.CANONICAL):
                self.assertEqual((out / f"{table}.jsonl").read_bytes(),
                                 (ct.CANONICAL_DIR / f"{table}.jsonl").read_bytes(), table)
            exported = "".join(p.read_text(encoding="utf-8") for p in out.iterdir())
            for marker in (IDENTITY, HASH, OTHER_HASH, '"issuance_id"'):
                self.assertNotIn(marker, exported)
            self.assertEqual(export.identity_problems(ct.CANONICAL_DIR, out), [])
            self.assertEqual(export.check_directory(out), [])


if __name__ == "__main__":
    unittest.main()
