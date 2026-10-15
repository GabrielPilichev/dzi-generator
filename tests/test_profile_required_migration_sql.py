"""023 adds the "profile required" mark of an account (EMAIL_REGISTRATION PR 6, test plan A1, A2, A8, A9).

One nullable column, ``users.profile_required_at``. No row is written: every account
that exists when the migration runs is unmarked, whatever its role, credentials,
profile, identities or attempts. Every database in this module is temporary: built by the
shared migration runner from the repository's migration files, or cloned by
``tests.support.build_test_database``. None of them is ``data/questions.db``.
"""

import io
import json
import re
import shutil
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from src import build_database as builder
from src import canonical_export as export
from src import canonical_tables as ct
from src import manage_migrations
from tests.support import ROOT, build_test_database
from tests.test_assignment_public_token_migration_sql import POPULATE_SQL
from tests.test_user_identities_migration_sql import ACCOUNTS_SQL
from web.services import accounts

MIGRATIONS = ROOT / "web" / "migrations"
MIGRATION = "023_profile_required.sql"
PREVIOUS = "022_registration_issuance_identity.sql"
COLUMN = "profile_required_at"
OLD_COLUMNS = ("id", "email", "display_name", "role", "password_hash", "is_active", "session_key",
               "password_changed_at", "last_login_at", "created_at", "updated_at", "email_verified_at")
STAMP = "2026-10-13 12:00:00"

# What a database at 022 could hold: students with and without profiles, a Google
# identity, an email-registered account and a teacher with a teacher profile.
REGISTERED_SQL = f"""
INSERT INTO users (id, email, display_name, role, password_hash, is_active, session_key, email_verified_at)
    VALUES (70, 'ana@example.bg', 'Ана Иванова', 'student', 'scrypt:x', 1, 'registered-session-key-0123456789ab', '{STAMP}'),
           (71, NULL, 'Мария Петрова', 'student', NULL, 1, 'google-session-key-0123456789abcdef', NULL),
           (72, 'ivan@example.bg', 'Иван Петров', 'teacher', 'scrypt:y', 1, 'teacher-session-key-0123456789abcd', '{STAMP}');
INSERT INTO user_profiles (user_id, account_type, first_name, last_name, grade, grade_set_at)
    VALUES (70, 'student', 'Ана', 'Иванова', 9, '{STAMP}');
INSERT INTO user_profiles (user_id, account_type, first_name, last_name, school_name)
    VALUES (72, 'teacher', 'Иван', 'Петров', 'СУ „Васил Левски“');
INSERT INTO teacher_subjects (user_id, position, subject_id, label) VALUES (72, 1, 'it', 'Информационни технологии');
INSERT INTO user_identities (user_id, issuer, subject, provider) VALUES (71, 'https://accounts.google.com', 'sub-71', 'google');
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
    """A database at migration 022 that already holds accounts of every kind."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="profile-required-migration-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.path = self.root / "upgrade.db"
        sqlite3.connect(self.path).close()
        previous = self.root / "through-022"
        previous.mkdir()
        # 000..023 only: what 023 itself changes, whatever later migrations add.
        self.through = self.root / "through-023"
        self.through.mkdir()
        for path in sorted(MIGRATIONS.glob("*.sql")):
            if path.name < MIGRATION:
                shutil.copyfile(path, previous / path.name)
            if path.name <= MIGRATION:
                shutil.copyfile(path, self.through / path.name)
        self.assertEqual(apply(self.path, previous)[0], 0)
        self.conn = None
        self.reopen()
        self.conn.executescript(POPULATE_SQL + ACCOUNTS_SQL + REGISTERED_SQL)
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

    def contents(self, skip=()):
        return {name: self.conn.execute(f'SELECT * FROM "{name}" ORDER BY 1, 2').fetchall()
                for (name,) in self.conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' AND name != 'schema_migrations'")
                if name not in skip}

    def schema_objects(self):
        """Everything sqlite_master records except the users table and its indexes."""
        return self.conn.execute(
            "SELECT type, name, tbl_name, rootpage, sql FROM sqlite_master WHERE tbl_name != 'users' "
            "ORDER BY type, name").fetchall()


class ProfileRequiredUpgradeTest(UpgradedDatabaseTestCase):
    def test_upgrade_from_022_changes_no_row_and_marks_nobody(self):  # A1, A2
        before, objects_before = self.contents(skip=("users",)), self.schema_objects()
        users_before = self.conn.execute(f"SELECT {', '.join(OLD_COLUMNS)} FROM users ORDER BY id").fetchall()
        root = self.conn.execute("SELECT rootpage FROM sqlite_master WHERE name = 'users'").fetchone()
        ledger_before = self.conn.execute("SELECT filename, applied_at FROM schema_migrations ORDER BY rowid").fetchall()
        self.assertEqual(ledger_before[-1][0], PREVIOUS)
        self.assertEqual(len(users_before), 7)
        self.migrate()
        # Every other table, sequence and index: untouched.
        self.assertEqual(self.contents(skip=("users",)), before)
        self.assertEqual(self.schema_objects(), objects_before)
        self.assertEqual(self.conn.execute("SELECT rootpage FROM sqlite_master WHERE name = 'users'").fetchone(),
                         root)  # altered in place, not rebuilt
        ledger = self.conn.execute("SELECT filename, applied_at FROM schema_migrations ORDER BY rowid").fetchall()
        self.assertEqual(ledger[:-1], ledger_before)
        self.assertEqual(ledger[-1][0], MIGRATION)
        self.assertEqual(self.conn.execute("PRAGMA foreign_key_check").fetchall(), [])
        self.assertEqual(self.conn.execute("PRAGMA integrity_check").fetchall(), [("ok",)])
        # users: the same rows column for column.
        self.assertEqual(self.conn.execute(f"SELECT {', '.join(OLD_COLUMNS)} FROM users ORDER BY id").fetchall(),
                         users_before)

    def test_every_existing_account_is_unmarked_whatever_it_is(self):  # A2, J10
        self.migrate()
        rows = self.conn.execute(f"SELECT id, role, {COLUMN} FROM users ORDER BY id").fetchall()
        self.assertEqual([row[0] for row in rows], [51, 52, 61, 62, 70, 71, 72])
        self.assertEqual({row[1] for row in rows}, {"student", "teacher", "admin"})
        self.assertEqual([row[2] for row in rows], [None] * 7)
        # In particular a student with no profile, and one created through Google, are
        # not asked: "nothing retroactive" is a property of the data.
        self.assertEqual(self.conn.execute(
            f"SELECT COUNT(*) FROM users WHERE {COLUMN} IS NOT NULL").fetchone()[0], 0)

    def test_users_gains_exactly_one_nullable_column_at_the_end(self):
        old = list(self.conn.execute("PRAGMA table_info(users)"))
        self.assertEqual(tuple(row[1] for row in old), OLD_COLUMNS)
        self.migrate()
        new = list(self.conn.execute("PRAGMA table_info(users)"))
        self.assertEqual(new[:-1], old)
        cid, name, kind, notnull, default, pk = new[-1]
        self.assertEqual((name, kind, notnull, default, pk), (COLUMN, "TEXT", 0, None, 0))
        self.assertEqual(self.conn.execute("PRAGMA foreign_key_list(users)").fetchall(), [])

    def test_no_index_trigger_or_constraint_is_added(self):
        indexes = self.conn.execute("PRAGMA index_list(users)").fetchall()
        triggers = self.conn.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'").fetchall()
        self.migrate()
        self.assertEqual(self.conn.execute("PRAGMA index_list(users)").fetchall(), indexes)
        self.assertEqual(self.conn.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'").fetchall(), triggers)
        # The column takes a time, and NULL again; nothing about it is checked by SQL.
        self.conn.execute(f"UPDATE users SET {COLUMN} = ? WHERE id = 71", (STAMP,))
        self.assertEqual(self.conn.execute(f"SELECT {COLUMN} FROM users WHERE id = 71").fetchone(), (STAMP,))
        self.conn.execute(f"UPDATE users SET {COLUMN} = NULL WHERE id = 71")

    def test_code_that_predates_the_migration_still_creates_and_loads_users(self):  # A9
        """Explicit column lists: the account service names its columns, so the new
        column changes nothing for it, and a created account is unmarked."""
        self.migrate()
        user_id = accounts.create_user(self.conn, display_name="Нов Ученик", email="new@example.bg",
                                       password="a-long-enough-password", role="student")
        self.conn.commit()
        self.assertEqual(accounts.get_user_by_id(self.conn, user_id)["display_name"], "Нов Ученик")
        self.assertEqual(accounts.authenticate(self.conn, "new@example.bg", "a-long-enough-password")["id"], user_id)
        self.assertEqual(self.conn.execute(f"SELECT {COLUMN} FROM users WHERE id = ?", (user_id,)).fetchone(), (None,))
        self.assertNotIn(COLUMN, accounts._USER_COLUMNS)   # not widened: no request reads the mark

    def test_it_is_a_plain_additive_migration_not_a_rebuild(self):
        dry = io.StringIO()
        manage_migrations.dry_run(self.path, self.through, out=dry)
        self.assertIn(f"pending migrations:\n  - {MIGRATION}\n", dry.getvalue())
        self.assertNotIn(COLUMN, [row[1] for row in self.conn.execute("PRAGMA table_info(users)")])
        report = self.migrate()
        self.assertIn(f"applied migrations:\n  - {MIGRATION}\n", report)
        self.assertIn("foreign_key_check: ok", report)
        self.assertNotIn("table-rebuild", report)
        self.assertFalse(manage_migrations.is_table_rebuild_migration(MIGRATION, migration_text()))

    def test_reapplying_is_a_noop_in_the_runner(self):
        self.migrate()
        self.conn.execute(f"UPDATE users SET {COLUMN} = ? WHERE id = 71", (STAMP,))
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
            self.assertEqual(fresh.execute("SELECT COUNT(*) FROM users").fetchone()[0], 0)
            ledger = [row[0] for row in fresh.execute("SELECT filename FROM schema_migrations ORDER BY rowid")]
        self.assertEqual(ledger[-2:], [PREVIOUS, MIGRATION])

    def test_023_is_never_adopted_without_running_it(self):
        self.conn.row_factory = sqlite3.Row
        self.assertEqual(manage_migrations.baseline_status(self.conn, MIGRATION)[0], "manual review required")


class ProfileRequiredMigrationFileTest(unittest.TestCase):
    def test_the_file_adds_one_column_and_nothing_else(self):
        self.assertEqual(re.findall(r"ALTER TABLE (\w+) ADD COLUMN (\w+) (\w+)", statements()),
                         [("users", COLUMN, "TEXT")])
        self.assertEqual(re.findall(r"CREATE ", statements()), [])
        self.assertEqual(len(re.findall(r";", statements())), 2)  # the pragma and the column

    def test_nothing_destructive_no_backfill_and_no_default(self):
        text = statements()
        for forbidden in ("DROP ", "DELETE ", "UPDATE ", "INSERT ", "RENAME ", "DEFAULT", "NOT NULL", "CHECK",
                          "CURRENT_TIMESTAMP", "PRAGMA writable_schema", "role"):
            self.assertNotIn(forbidden, text, forbidden)

    def test_the_files_are_numbered_without_a_collision(self):
        names = sorted(path.name for path in MIGRATIONS.glob("*.sql"))
        self.assertEqual([name for name in names if name.startswith("023_")], [MIGRATION])
        self.assertEqual(names[names.index(MIGRATION) - 1], PREVIOUS)
        self.assertEqual([name[:3] for name in names], [f"{n:03d}" for n in range(len(names))])
        mentions = [name for name in names if COLUMN in "\n".join(
            line for line in (MIGRATIONS / name).read_text(encoding="utf-8").splitlines()
            if not line.lstrip().startswith("--"))]
        self.assertEqual(mentions, [MIGRATION])


class ProfileRequiredClassificationTest(unittest.TestCase):
    def test_users_stays_runtime_and_the_manifest_records_the_level(self):  # A8
        self.assertEqual(ct.TABLE_CLASSIFICATION["users"][0], ct.RUNTIME)
        self.assertNotIn("users", ct.tables_of(ct.CANONICAL))
        self.assertFalse((ROOT / "data" / "canonical" / "users.jsonl").exists())
        manifest = json.loads((ROOT / "data" / "canonical" / "manifest.json").read_text(encoding="utf-8"))
        names = sorted(path.name for path in MIGRATIONS.glob("*.sql"))
        self.assertEqual(manifest["schema"]["migrations"], names)
        self.assertEqual(manifest["schema"]["migration_level"], names[-1])
        self.assertIn(MIGRATION, names)
        self.assertIn("users", manifest["runtime_exclusions"])
        self.assertIn("users", manifest["classification"]["runtime"])
        self.assertNotIn("users", manifest["tables"])
        self.assertEqual(export.check_directory(ct.CANONICAL_DIR), [])

    def test_a_fresh_canonical_build_has_the_column_and_no_account(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = build_test_database(Path(tmp) / "fresh.db")
            with closing(sqlite3.connect(path)) as conn:
                applied = [r[0] for r in conn.execute("SELECT filename FROM schema_migrations ORDER BY filename")]
                self.assertEqual(applied[-1], MIGRATION)
                self.assertEqual(tuple(r[1] for r in conn.execute("PRAGMA table_info(users)"))[-2:],
                                 ("email_verified_at", COLUMN))
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM users").fetchone()[0], 0)
                self.assertEqual(ct.classification_problems(conn), [])
            self.assertEqual(builder.validation_problems(path), [])

    def test_an_export_is_the_tracked_content_byte_for_byte_and_holds_no_mark(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = build_test_database(root / "working.db")
            with closing(sqlite3.connect(path)) as conn:
                accounts.create_user(conn, display_name="Ученик Един", role="student")
                conn.execute(f"UPDATE users SET {COLUMN} = ?", (STAMP,))
                conn.commit()
            out = root / "export"
            with closing(export.open_snapshot(path)) as conn:
                manifest = export.export_database(conn, out)
            tracked = export.load_manifest(ct.CANONICAL_DIR)
            self.assertEqual(manifest["tables"], tracked["tables"])
            self.assertEqual(manifest["schema"], tracked["schema"])
            self.assertEqual(manifest["classification"], tracked["classification"])
            for table in ct.tables_of(ct.CANONICAL):
                self.assertEqual((out / f"{table}.jsonl").read_bytes(),
                                 (ct.CANONICAL_DIR / f"{table}.jsonl").read_bytes(), table)
            exported = "".join(p.read_text(encoding="utf-8") for p in out.iterdir())
            self.assertNotIn(COLUMN, exported)
            self.assertNotIn("Ученик Един", exported)
            self.assertEqual(export.identity_problems(ct.CANONICAL_DIR, out), [])
            self.assertEqual(export.check_directory(out), [])

    def test_no_canonical_file_mentions_the_column(self):
        for path in sorted((ROOT / "data" / "canonical").iterdir()):
            if path.name != ct.MANIFEST_NAME:
                self.assertNotIn(COLUMN.encode(), path.read_bytes(), path.name)


if __name__ == "__main__":
    unittest.main()
