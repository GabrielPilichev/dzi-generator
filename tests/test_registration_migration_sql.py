"""021 adds the registration storage additively (test plan A1-A9, EMAIL_REGISTRATION PR 1).

Five new tables and one NULL column, ``users.email_verified_at``. Every existing row
stays as it was, nothing is seeded, and nothing but the profile service uses the tables
yet. ``users.profile_required_at`` belongs to PR 6 and is not here. Every database in
this module is temporary: built by the shared migration runner from the repository's
migration files, or cloned by ``tests.support.build_test_database``. None of them is
``data/questions.db``.
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
MIGRATION = "021_registration_schema.sql"
PREVIOUS = "020_oidc_consumed_transactions.sql"
NEW_TABLES = ("user_profiles", "teacher_subjects", "pending_registrations",
              "registration_sends", "registration_control")
NEW_INDEXES = ("idx_teacher_subjects_subject", "idx_teacher_subjects_other",
               "idx_pending_registrations_state", "idx_registration_sends_email",
               "idx_registration_sends_time")
COLUMNS = {
    "user_profiles": ("user_id", "account_type", "first_name", "last_name", "grade", "grade_set_at",
                      "school_name", "created_at", "updated_at"),
    "teacher_subjects": ("user_id", "position", "subject_id", "label"),
    "pending_registrations": ("id", "binding_hash", "account_type", "state", "is_shadow", "email",
                              "first_name", "last_name", "grade", "school_name", "subjects_json",
                              "password_hash", "code_hash", "code_expires_at", "code_send_id",
                              "code_attempts", "send_count", "resend_after", "created_at",
                              "expires_at", "closed_at"),
    "registration_sends": ("id", "pending_id", "email_hash", "kind", "attempted_at", "outcome",
                           "settled_at"),
    "registration_control": ("id", "high_water_at", "hold_reason", "hold_set_at"),
}
HASH = "a1" * 32
OTHER_HASH = "b2" * 32
STAMP = "2026-10-13 12:00:00"
OLD_USER_COLUMNS = ("id", "email", "display_name", "role", "password_hash", "is_active", "session_key",
                    "password_changed_at", "last_login_at", "created_at", "updated_at")


def apply(path, migrations_dir=MIGRATIONS):
    out = io.StringIO()
    code = manage_migrations.apply_migrations(path, migrations_dir, out=out)
    return code, out.getvalue()


def migration_text() -> str:
    return (MIGRATIONS / MIGRATION).read_text(encoding="utf-8")


def statements() -> str:
    """The migration without its comments."""
    return "\n".join(line for line in migration_text().splitlines() if not line.lstrip().startswith("--"))


def insert(conn, table, **values):
    columns = ", ".join(values)
    marks = ", ".join("?" for _ in values)
    return conn.execute(f"INSERT INTO {table} ({columns}) VALUES ({marks})", tuple(values.values()))


def pending(conn, **overrides):
    values = {"binding_hash": HASH, "account_type": "student", "state": "pending", "is_shadow": 0,
              "created_at": STAMP, "expires_at": "2026-10-13 12:30:00"}
    values.update(overrides)
    return insert(conn, "pending_registrations", **values)


class UpgradedDatabaseTestCase(unittest.TestCase):
    """A database at migration 020 that already holds accounts and their data."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="registration-migration-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.path = self.root / "upgrade.db"
        sqlite3.connect(self.path).close()
        previous = self.root / "through-020"
        previous.mkdir()
        # 000..021 only: what 021 itself changes, whatever later migrations add.
        self.through = self.root / "through-021"
        self.through.mkdir()
        for path in sorted(MIGRATIONS.glob("*.sql")):
            if path.name < MIGRATION:
                shutil.copyfile(path, previous / path.name)
            if path.name <= MIGRATION:
                shutil.copyfile(path, self.through / path.name)
        self.assertEqual(apply(self.path, previous)[0], 0)
        self.conn = None
        self.reopen()
        self.conn.executescript(POPULATE_SQL + ACCOUNTS_SQL)
        self.conn.commit()

    def reopen(self):
        """A new connection with foreign keys on, as the application's are.

        Needed after the migration for schema pragmas: a connection opened before it
        answers index_list / foreign_key_list from its cached schema.
        """
        if self.conn is not None:
            self.conn.close()
        self.conn = sqlite3.connect(self.path)
        self.addCleanup(self.conn.close)
        self.conn.execute("PRAGMA foreign_keys = ON")

    def migrate(self):
        code, report = apply(self.path, self.through)
        self.assertEqual(code, 0, report)
        return report

    def contents(self, skip=()):
        return {name: self.conn.execute(f'SELECT * FROM "{name}" ORDER BY 1').fetchall()
                for (name,) in self.conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' AND name != 'schema_migrations'")
                if name not in skip}

    def schema_objects(self):
        """Everything sqlite_master records except the new tables, their indexes and users."""
        marks = ", ".join("?" for _ in NEW_TABLES)
        return self.conn.execute(
            f"SELECT type, name, tbl_name, rootpage, sql FROM sqlite_master "
            f"WHERE tbl_name NOT IN ({marks}, 'users') ORDER BY type, name", NEW_TABLES).fetchall()


class RegistrationUpgradeTest(UpgradedDatabaseTestCase):
    def test_upgrade_from_020_adds_empty_tables_and_changes_no_row(self):  # A1, A2
        for table in NEW_TABLES:
            self.assertNotIn(table, self.contents())
        before, objects_before = self.contents(), self.schema_objects()
        users_before = self.conn.execute(f"SELECT {', '.join(OLD_USER_COLUMNS)} FROM users ORDER BY id").fetchall()
        users_root = self.conn.execute("SELECT rootpage FROM sqlite_master WHERE name = 'users'").fetchone()
        ledger_before = self.conn.execute("SELECT filename, applied_at FROM schema_migrations ORDER BY rowid").fetchall()
        self.assertEqual(ledger_before[-1][0], PREVIOUS)
        self.migrate()
        after = self.contents(skip=("users",))
        for table in NEW_TABLES:
            self.assertEqual(after.pop(table), [], table)  # nothing is seeded or backfilled
        before.pop("users")
        # Assignments, attempts, second factors, content and every sequence: untouched
        # (no new table appears in sqlite_sequence either).
        self.assertEqual(after, before)
        # No existing table other than users, and no index or root page, changed.
        self.assertEqual(self.schema_objects(), objects_before)
        self.assertEqual(self.conn.execute("SELECT rootpage FROM sqlite_master WHERE name = 'users'").fetchone(),
                         users_root)  # users was altered in place, not rebuilt
        ledger = self.conn.execute("SELECT filename, applied_at FROM schema_migrations ORDER BY rowid").fetchall()
        self.assertEqual(ledger[:-1], ledger_before)
        self.assertEqual(ledger[-1][0], MIGRATION)
        self.assertEqual(self.conn.execute("PRAGMA foreign_key_check").fetchall(), [])
        self.assertEqual(self.conn.execute("PRAGMA integrity_check").fetchall(), [("ok",)])
        # users: the same rows column for column, and the new column NULL for each.
        self.assertEqual(self.conn.execute(f"SELECT {', '.join(OLD_USER_COLUMNS)} FROM users ORDER BY id").fetchall(),
                         users_before)
        self.assertEqual(len(users_before), 4)
        self.assertEqual(self.conn.execute("SELECT id, email_verified_at FROM users ORDER BY id").fetchall(),
                         [(51, None), (52, None), (61, None), (62, None)])

    def test_users_gains_exactly_one_nullable_column_at_the_end(self):
        old = list(self.conn.execute("PRAGMA table_info(users)"))
        self.migrate()
        self.reopen()
        new = list(self.conn.execute("PRAGMA table_info(users)"))
        self.assertEqual(new[:-1], old)
        cid, name, kind, notnull, default, pk = new[-1]
        self.assertEqual((name, kind, notnull, default, pk), ("email_verified_at", "TEXT", 0, None, 0))
        # PR 6's column is not here.
        self.assertNotIn("profile_required_at", [row[1] for row in new])

    def test_existing_accounts_credentials_sessions_roles_and_ownership_are_unchanged(self):  # A2
        ownership = {
            "assignments": "SELECT id, owner_user_id, closed_by_user_id FROM quiz_assignments ORDER BY id",
            "attempts": "SELECT id, assignment_id, user_id, student_name FROM quiz_attempts ORDER BY id",
            "second factor": "SELECT * FROM totp_credentials ORDER BY user_id",
            "recovery codes": "SELECT * FROM totp_recovery_codes ORDER BY id",
            "throttle": "SELECT * FROM login_throttle ORDER BY identifier_hash",
            "identities": "SELECT * FROM user_identities ORDER BY id",
        }
        self.conn.execute("INSERT INTO user_identities (user_id, issuer, subject, provider) "
                          "VALUES (52, 'https://accounts.google.com', 'sub-1', 'google')")
        self.conn.commit()
        before = {name: self.conn.execute(sql).fetchall() for name, sql in ownership.items()}
        roles = "SELECT id, role, is_active, password_hash, session_key FROM users ORDER BY id"
        roles_before = self.conn.execute(roles).fetchall()
        self.migrate()
        self.assertEqual({name: self.conn.execute(sql).fetchall() for name, sql in ownership.items()}, before)
        self.assertEqual(self.conn.execute(roles).fetchall(), roles_before)
        self.assertEqual(before["attempts"], [(91, 71, 52, "Ученик"), (92, 71, None, "Исторически")])
        # The next account still gets the next id: no id is reused or skipped.
        self.assertEqual(self.conn.execute(
            "INSERT INTO users (display_name, session_key) VALUES ('Нов', ?)", ("n" * 32,)).lastrowid, 63)

    def test_it_is_a_plain_additive_migration_not_a_rebuild(self):  # A1
        dry = io.StringIO()
        manage_migrations.dry_run(self.path, self.through, out=dry)
        self.assertIn(f"pending migrations:\n  - {MIGRATION}\n", dry.getvalue())
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE name = 'user_profiles'").fetchone()[0], 0)  # dry run wrote nothing
        report = self.migrate()
        self.assertIn(f"applied migrations:\n  - {MIGRATION}\n", report)
        self.assertIn("foreign_key_check: ok", report)
        self.assertNotIn("table-rebuild", report)
        self.assertFalse(manage_migrations.is_table_rebuild_migration(MIGRATION, migration_text()))

    def test_reapplying_is_a_noop_in_the_runner(self):  # A1
        self.migrate()
        self.conn.execute("INSERT INTO user_profiles (user_id, account_type, first_name, last_name, grade, "
                          "grade_set_at) VALUES (52, 'student', 'Ана', 'Иванова', 9, ?)", (STAMP,))
        self.conn.commit()
        before = self.contents()
        report = self.migrate()
        self.assertIn("applied migrations:\n  - none\n", report)
        self.assertEqual(self.contents(), before)

    def test_upgraded_schema_equals_a_fresh_database(self):  # A1
        self.migrate()
        self.reopen()
        fresh_path = self.root / "fresh.db"
        sqlite3.connect(fresh_path).close()
        self.assertEqual(apply(fresh_path, self.through)[0], 0)
        with closing(sqlite3.connect(fresh_path)) as fresh:
            for table in (*NEW_TABLES, "users"):
                for pragma in ("table_info", "foreign_key_list", "index_list"):
                    self.assertEqual(fresh.execute(f"PRAGMA {pragma}({table})").fetchall(),
                                     self.conn.execute(f"PRAGMA {pragma}({table})").fetchall(), (table, pragma))
                schema = "SELECT type, name, sql FROM sqlite_master WHERE tbl_name = ? ORDER BY type, name"
                self.assertEqual(fresh.execute(schema, (table,)).fetchall(),
                                 self.conn.execute(schema, (table,)).fetchall(), table)
            for table in NEW_TABLES:
                self.assertEqual(fresh.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0)
            ledger = [row[0] for row in fresh.execute("SELECT filename FROM schema_migrations ORDER BY rowid")]
        self.assertEqual(ledger[ledger.index(PREVIOUS) + 1], MIGRATION)

    def test_021_is_never_adopted_without_running_it(self):
        self.conn.row_factory = sqlite3.Row
        self.assertEqual(manage_migrations.baseline_status(self.conn, MIGRATION)[0], "manual review required")

    def test_code_that_predates_the_migration_still_creates_and_loads_users(self):  # A9
        self.migrate()
        # An INSERT with the old, explicit column list, as migration-020 code writes it.
        uid = self.conn.execute(
            "INSERT INTO users (email, display_name, role, password_hash, is_active, session_key, "
            "password_changed_at) VALUES ('old@example.bg', 'Стар код', 'student', NULL, 1, ?, NULL)",
            ("o" * 32,)).lastrowid
        self.assertIsNone(self.conn.execute("SELECT email_verified_at FROM users WHERE id = ?", (uid,)).fetchone()[0])
        # The accounts service, unchanged, still creates, loads and authenticates.
        created = accounts.create_user(self.conn, display_name="Нов акаунт", email="new@example.bg",
                                       password="a-long-enough-password", role="teacher")
        self.conn.commit()
        loaded = accounts.get_user_by_id(self.conn, created)
        self.assertEqual((loaded["email"], loaded["role"]), ("new@example.bg", "teacher"))
        self.assertNotIn("email_verified_at", loaded)  # _USER_COLUMNS is not widened by PR 1
        self.assertEqual(accounts.authenticate(self.conn, "new@example.bg", "a-long-enough-password")["id"], created)
        self.assertEqual(accounts.get_user_by_id(self.conn, uid)["id"], uid)
        self.assertEqual(self.conn.execute("PRAGMA foreign_key_check").fetchall(), [])


class RegistrationSchemaTest(UpgradedDatabaseTestCase):
    def setUp(self):
        super().setUp()
        self.migrate()
        self.reopen()

    def refused(self, table, **values):
        with self.assertRaises(sqlite3.IntegrityError, msg=f"{table}: {values}"):
            insert(self.conn, table, **values)

    def accepted(self, table, **values):
        return insert(self.conn, table, **values)

    def student(self, user_id=52, **overrides):
        values = {"user_id": user_id, "account_type": "student", "first_name": "Ана", "last_name": "Иванова",
                  "grade": 9, "grade_set_at": STAMP}
        values.update(overrides)
        return values

    def teacher(self, user_id=51, **overrides):
        values = {"user_id": user_id, "account_type": "teacher", "first_name": "Иван", "last_name": "Петров",
                  "school_name": "СУ „Васил Левски“, Пловдив"}
        values.update(overrides)
        return values

    def test_columns_are_the_approved_ones_in_the_approved_order(self):
        for table, columns in COLUMNS.items():
            self.assertEqual(tuple(row[1] for row in self.conn.execute(f"PRAGMA table_info({table})")),
                             columns, table)

    def test_indexes_are_the_approved_ones(self):
        marks = ", ".join("?" for _ in NEW_TABLES)
        indexes = {row[0] for row in self.conn.execute(
            f"SELECT name FROM sqlite_master WHERE type = 'index' AND name NOT LIKE 'sqlite_%' "
            f"AND tbl_name IN ({marks})", NEW_TABLES)}
        self.assertEqual(indexes, set(NEW_INDEXES))
        unique = {row[1] for row in self.conn.execute("PRAGMA index_list(pending_registrations)") if row[2]}
        self.assertEqual(len(unique), 1)  # binding_hash

    def test_foreign_keys_are_the_approved_ones(self):
        def keys(table):
            return [(row[2], row[3], row[4], row[6]) for row in self.conn.execute(f"PRAGMA foreign_key_list({table})")]
        self.assertEqual(keys("user_profiles"), [("users", "user_id", "id", "NO ACTION")])
        self.assertEqual(keys("teacher_subjects"), [("user_profiles", "user_id", "user_id", "CASCADE")])
        for table in ("pending_registrations", "registration_sends", "registration_control"):
            self.assertEqual(keys(table), [], table)  # the ledger and the pending rows reference nothing

    def test_account_type_rejects_admin_and_anything_else_in_both_tables(self):  # A3
        for bad in ("admin", "Student", "", "parent", None):
            with self.subTest(account_type=bad):
                self.refused("user_profiles", **self.student(account_type=bad))
                self.refused("pending_registrations", binding_hash=HASH, account_type=bad, state="pending",
                             is_shadow=0, created_at=STAMP, expires_at=STAMP)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM user_profiles").fetchone()[0], 0)

    def test_student_and_teacher_shapes_are_exclusive(self):  # A4
        self.refused("user_profiles", **self.student(grade=None, grade_set_at=None))     # student without grade
        self.refused("user_profiles", **self.student(grade_set_at=None))                 # grade without its date
        self.refused("user_profiles", **self.student(school_name="СУ Тест"))             # student with school
        self.refused("user_profiles", **self.teacher(school_name=None))                  # teacher without school
        self.refused("user_profiles", **self.teacher(grade=9, grade_set_at=STAMP))       # teacher with grade
        self.refused("user_profiles", **self.teacher(grade_set_at=STAMP))                # a date without a grade
        self.accepted("user_profiles", **self.student())
        self.accepted("user_profiles", **self.teacher())
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM user_profiles").fetchone()[0], 2)

    def test_name_grade_and_school_bounds_are_enforced_by_the_table(self):  # A5
        for column in ("first_name", "last_name"):
            self.refused("user_profiles", **self.student(**{column: ""}))
            self.refused("user_profiles", **self.student(**{column: "а" * 40}))
        self.accepted("user_profiles", **self.student(first_name="а", last_name="б" * 39))
        for grade in (7, 13, 0, 9.5, "abc", -9):
            with self.subTest(grade=grade):
                self.refused("user_profiles", **self.student(user_id=61, grade=grade))
        for grade in (8, 12):
            self.accepted("user_profiles", **self.student(user_id=61 if grade == 8 else 62, grade=grade))
        self.conn.execute("DELETE FROM user_profiles")
        self.refused("user_profiles", **self.teacher(school_name="аб"))
        self.refused("user_profiles", **self.teacher(school_name="а" * 121))
        self.accepted("user_profiles", **self.teacher(school_name="абв"))
        self.conn.execute("DELETE FROM user_profiles")
        self.accepted("user_profiles", **self.teacher(school_name="а" * 120))

    def test_a_profile_needs_an_existing_user_and_one_profile_per_user(self):
        self.refused("user_profiles", **self.student(user_id=9999))
        self.accepted("user_profiles", **self.student())
        self.refused("user_profiles", **self.student())

    def test_a_user_with_a_profile_cannot_be_deleted_from_under_it(self):
        self.accepted("user_profiles", **self.student())
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("DELETE FROM users WHERE id = 52")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM users WHERE id = 52").fetchone()[0], 1)

    def test_a_profile_changes_nothing_about_the_account(self):
        users = "SELECT * FROM users ORDER BY id"
        before = self.conn.execute(users).fetchall()
        self.accepted("user_profiles", **self.student(user_id=51))      # a teacher account, a student profile
        self.accepted("user_profiles", **self.teacher(user_id=61))      # an admin account, a teacher profile
        self.assertEqual(self.conn.execute(users).fetchall(), before)

    def test_teacher_subject_bounds_and_cascade(self):  # A5
        self.accepted("user_profiles", **self.teacher())
        rows = {"user_id": 51, "subject_id": "it", "label": "Информационни технологии"}
        self.refused("teacher_subjects", position=0, **rows)
        self.refused("teacher_subjects", position=4, **rows)
        self.refused("teacher_subjects", position=1.5, **rows)
        self.refused("teacher_subjects", position=1, **{**rows, "label": "а"})
        self.refused("teacher_subjects", position=1, **{**rows, "label": "а" * 61})
        self.refused("teacher_subjects", position=1, **{**rows, "subject_id": ""})
        self.refused("teacher_subjects", position=1, **{**rows, "label": None})
        self.refused("teacher_subjects", position=1, **{**rows, "user_id": 52})           # no profile
        self.accepted("teacher_subjects", position=1, **rows)
        self.refused("teacher_subjects", position=1, **{**rows, "subject_id": "mathematics"})  # position taken
        self.accepted("teacher_subjects", position=3, **{**rows, "subject_id": "physics", "label": "Физика"})
        self.accepted("teacher_subjects", position=2, user_id=51, subject_id=None, label="Роден език")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM teacher_subjects").fetchone()[0], 3)
        self.conn.execute("DELETE FROM user_profiles WHERE user_id = 51")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM teacher_subjects").fetchone()[0], 0)

    def test_a_subject_and_the_free_text_entry_cannot_repeat(self):
        self.accepted("user_profiles", **self.teacher())
        self.accepted("teacher_subjects", user_id=51, position=1, subject_id="it", label="Информационни технологии")
        self.refused("teacher_subjects", user_id=51, position=2, subject_id="it", label="Информационни технологии")
        self.accepted("teacher_subjects", user_id=51, position=2, subject_id=None, label="Роден език")
        self.refused("teacher_subjects", user_id=51, position=3, subject_id=None, label="Друг език")
        self.accepted("user_profiles", **self.teacher(user_id=61))
        self.accepted("teacher_subjects", user_id=61, position=1, subject_id="it", label="Информационни технологии")

    def test_pending_registration_scrubbed_states_hold_no_sensitive_column(self):  # A6
        sensitive = {"email": "a@b.bg", "first_name": "Ана", "last_name": "Иванова", "grade": 9,
                     "school_name": "СУ Тест", "subjects_json": "[]", "password_hash": "scrypt:x",
                     "code_hash": HASH, "code_expires_at": STAMP}
        for state in ("activated", "void", "expired"):
            for column, value in sensitive.items():
                with self.subTest(state=state, column=column):
                    self.refused("pending_registrations", binding_hash=HASH, account_type="student", state=state,
                                 is_shadow=0, created_at=STAMP, expires_at=STAMP, **{column: value})
            self.accepted("pending_registrations", binding_hash={"activated": "a0", "void": "b0", "expired": "e0"}[state] * 32,
                          account_type="student", state=state, is_shadow=0, created_at=STAMP, expires_at=STAMP,
                          closed_at=STAMP)
        # A pending row may hold all of them at once.
        self.accepted("pending_registrations", binding_hash=HASH, account_type="teacher", state="pending",
                      is_shadow=0, created_at=STAMP, expires_at=STAMP, **sensitive)

    def test_pending_registration_states_and_keys(self):  # A6
        self.refused("pending_registrations", binding_hash=HASH, account_type="student", state="open",
                     is_shadow=0, created_at=STAMP, expires_at=STAMP)
        self.refused("pending_registrations", binding_hash=HASH, account_type="student", state="pending",
                     is_shadow=2, created_at=STAMP, expires_at=STAMP)
        self.refused("pending_registrations", binding_hash=HASH, account_type="student", state="pending",
                     is_shadow=0, created_at=STAMP, expires_at=STAMP, code_attempts=-1)
        self.refused("pending_registrations", binding_hash=HASH, account_type="student", state="pending",
                     is_shadow=0, created_at=STAMP, expires_at=STAMP, code_attempts=0.5)
        self.refused("pending_registrations", binding_hash=HASH, account_type="student", state="pending",
                     is_shadow=0, created_at=STAMP, expires_at=STAMP, send_count=-1)
        self.refused("pending_registrations", binding_hash=HASH, account_type="student", state="pending",
                     is_shadow=0, created_at=None, expires_at=STAMP)
        self.refused("pending_registrations", binding_hash=HASH, account_type="student", state="pending",
                     is_shadow=0, created_at=STAMP, expires_at=None)
        pending(self.conn)
        with self.assertRaises(sqlite3.IntegrityError):
            pending(self.conn)  # a duplicate binding_hash
        # No reservation: two pending rows for one address, and for an address with an account.
        pending(self.conn, binding_hash=OTHER_HASH, email="same@example.bg")
        pending(self.conn, binding_hash="c3" * 32, email="same@example.bg")
        pending(self.conn, binding_hash="d4" * 32, email="admin@example.bg", is_shadow=1)
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM pending_registrations WHERE email = 'same@example.bg'").fetchone()[0], 2)
        row = self.conn.execute("SELECT code_attempts, send_count FROM pending_registrations WHERE id = 1").fetchone()
        self.assertEqual(row, (0, 0))

    def test_keyed_hash_columns_are_64_bytes_of_lower_case_hex(self):  # A6
        bad = ["", "a" * 63, "a" * 65, "A1" * 32, "g" * 64, " " + "a" * 63, "a" * 63 + "\n",
               "a" * 64 + "\x00", "a" * 63 + "\x00", "a" * 32 + "\x00" + "a" * 31, "é" * 32, "a" * 62 + "é"]
        for value in bad:
            with self.subTest(value=value):
                self.refused("pending_registrations", binding_hash=value, account_type="student", state="pending",
                             is_shadow=0, created_at=STAMP, expires_at=STAMP)
                self.refused("pending_registrations", binding_hash=HASH, account_type="student", state="pending",
                             is_shadow=0, created_at=STAMP, expires_at=STAMP, code_hash=value)
                self.refused("registration_sends", email_hash=value, kind="code", attempted_at=STAMP)
        self.refused("pending_registrations", binding_hash=b"\xa1" * 32, account_type="student", state="pending",
                     is_shadow=0, created_at=STAMP, expires_at=STAMP)  # a blob, not text
        self.refused("registration_sends", email_hash=None, kind="code", attempted_at=STAMP)
        pending(self.conn, code_hash=None)
        pending(self.conn, binding_hash=OTHER_HASH, code_hash="0123456789abcdef" * 4)

    def test_send_ledger_checks_and_ids_are_never_reused(self):  # A7
        base = {"email_hash": HASH, "attempted_at": STAMP}
        self.refused("registration_sends", kind="sms", **base)
        self.refused("registration_sends", kind=None, **base)
        self.refused("registration_sends", kind="code", outcome="sent", **base)
        self.refused("registration_sends", kind="code", outcome=None, **base)
        self.refused("registration_sends", kind="code", email_hash=HASH, attempted_at=None)
        first = self.accepted("registration_sends", kind="code", **base).lastrowid
        self.assertEqual(self.conn.execute("SELECT outcome, settled_at, pending_id FROM registration_sends").fetchone(),
                         ("started", None, None))
        for outcome in ("accepted", "not_sent", "refused", "unknown", "started"):
            self.conn.execute("UPDATE registration_sends SET outcome = ? WHERE id = ?", (outcome, first))
        self.conn.execute("DELETE FROM registration_sends")
        second = self.accepted("registration_sends", kind="notice", **base).lastrowid
        self.assertGreater(second, first)  # AUTOINCREMENT: a deleted generation is not issued again
        # pending_id is not a foreign key: it may name a row that no longer exists.
        self.accepted("registration_sends", kind="code", pending_id=424242, **base)

    def test_registration_control_is_one_row_with_a_known_reason(self):  # A7
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM registration_control").fetchone()[0], 0)
        self.accepted("registration_control", id=1, high_water_at=STAMP)
        self.refused("registration_control", id=2, high_water_at=STAMP)
        self.refused("registration_control", id=1, high_water_at=STAMP)
        self.refused("registration_control", id=0, high_water_at=STAMP)
        self.conn.execute("UPDATE registration_control SET hold_reason = 'restore', hold_set_at = ?", (STAMP,))
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("UPDATE registration_control SET hold_reason = 'operator'")
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("UPDATE registration_control SET high_water_at = NULL")
        self.assertEqual(self.conn.execute("SELECT hold_reason FROM registration_control").fetchone()[0], "restore")

    def test_a_fresh_set_of_tables_holds_nothing(self):
        for table in NEW_TABLES:
            self.assertEqual(self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0, table)
        sequences = {row[0] for row in self.conn.execute("SELECT name FROM sqlite_sequence")}
        self.assertFalse(sequences & set(NEW_TABLES))


class RegistrationMigrationFileTest(unittest.TestCase):
    def test_the_file_adds_only_this_prs_schema(self):
        creates = re.findall(r"CREATE TABLE (?:IF NOT EXISTS )?(\w+)", statements())
        self.assertEqual(creates, list(NEW_TABLES))
        self.assertEqual(re.findall(r"ALTER TABLE (\w+) ADD COLUMN (\w+)", statements()),
                         [("users", "email_verified_at")])
        self.assertEqual(re.findall(r"CREATE (?:UNIQUE )?INDEX (\w+)", statements()),
                         ["idx_teacher_subjects_subject", "idx_teacher_subjects_other",
                          "idx_pending_registrations_state", "idx_registration_sends_email",
                          "idx_registration_sends_time"])

    def test_nothing_destructive_and_nothing_of_a_later_pr(self):
        text = statements()
        for forbidden in ("DROP ", "DELETE FROM", "UPDATE ", "INSERT ", "RENAME ", "PRAGMA writable_schema",
                          "profile_required_at", "quota", "recovery", "entitlement"):
            self.assertNotIn(forbidden, text, forbidden)
        self.assertEqual(re.findall(r"\bALTER TABLE\b", text), ["ALTER TABLE"])

    def test_users_is_changed_by_that_one_column_only(self):
        # One ALTER and one REFERENCES; no other statement names it.
        self.assertEqual(len(re.findall(r"\busers\b", statements())), 2)
        self.assertEqual(sorted(set(re.findall(r"REFERENCES\s+(\w+)\((\w+)\)", statements()))),
                         [("user_profiles", "user_id"), ("users", "id")])

    def test_the_files_are_numbered_without_a_collision(self):
        names = sorted(path.name for path in MIGRATIONS.glob("*.sql"))
        self.assertEqual([name for name in names if name.startswith("021_")], [MIGRATION])
        self.assertEqual(names[names.index(MIGRATION) - 1], PREVIOUS)
        self.assertEqual([name[:3] for name in names], [f"{n:03d}" for n in range(len(names))])
        for table in NEW_TABLES:
            created = [name for name in names if re.search(
                rf"CREATE TABLE (IF NOT EXISTS )?{table}\b", (MIGRATIONS / name).read_text(encoding="utf-8"))]
            self.assertEqual(created, [MIGRATION], table)


class RegistrationClassificationTest(unittest.TestCase):
    def test_every_table_is_runtime_not_canonical_and_not_seeded(self):  # A8
        for table in NEW_TABLES:
            with self.subTest(table=table):
                self.assertEqual(ct.TABLE_CLASSIFICATION[table][0], ct.RUNTIME)
                self.assertIn(table, ct.tables_of(ct.RUNTIME))
                self.assertNotIn(table, ct.tables_of(ct.CANONICAL))
                self.assertNotIn(table, ct.tables_of(ct.OPERATIONAL))
                self.assertNotIn(table, ct.MIGRATION_SEEDED_ROWS)
                self.assertNotIn(table, ct.NATURAL_KEYS)
                self.assertFalse((ROOT / "data" / "canonical" / f"{table}.jsonl").exists())

    def test_manifest_records_the_level_and_the_exclusions(self):  # A8
        manifest = json.loads((ROOT / "data" / "canonical" / "manifest.json").read_text(encoding="utf-8"))
        self.assertGreaterEqual(manifest["schema"]["migration_level"], MIGRATION)
        self.assertIn(MIGRATION, manifest["schema"]["migrations"])
        for table in NEW_TABLES:
            self.assertIn(table, manifest["runtime_exclusions"])
            self.assertIn(table, manifest["classification"]["runtime"])
            self.assertNotIn(table, manifest["classification"]["canonical"])
            self.assertNotIn(table, manifest["classification"]["operational"])
            self.assertNotIn(table, manifest["canonical_tables"])
            self.assertNotIn(table, manifest["tables"])  # never exported to data/canonical
        self.assertEqual(export.check_directory(ct.CANONICAL_DIR), [])

    def test_a_fresh_canonical_build_has_the_schema_and_no_registration_row(self):  # A8
        with tempfile.TemporaryDirectory() as tmp:
            path = build_test_database(Path(tmp) / "fresh.db")
            with closing(sqlite3.connect(path)) as conn:
                applied = [r[0] for r in conn.execute("SELECT filename FROM schema_migrations ORDER BY filename")]
                self.assertIn(MIGRATION, applied)
                for table, columns in COLUMNS.items():
                    self.assertEqual(tuple(r[1] for r in conn.execute(f"PRAGMA table_info({table})")), columns)
                    self.assertEqual(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0, table)
                    # Not even a sequence entry: no row was ever inserted and removed.
                    self.assertIsNone(ct.stored_sequence(conn, table))
                self.assertIn("email_verified_at", [r[1] for r in conn.execute("PRAGMA table_info(users)")])
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM users").fetchone()[0], 0)
                self.assertEqual(conn.execute("SELECT * FROM pragma_foreign_key_check").fetchall(), [])
                self.assertEqual(ct.classification_problems(conn), [])
            self.assertEqual(builder.validation_problems(path), [])

    def test_a_database_that_holds_registration_rows_is_not_a_canonical_build(self):  # A8
        for table, statement in (
            ("pending_registrations",
             "INSERT INTO pending_registrations (binding_hash, account_type, state, is_shadow, created_at, "
             f"expires_at) VALUES ('{HASH}', 'student', 'pending', 0, '{STAMP}', '{STAMP}')"),
            ("registration_sends",
             f"INSERT INTO registration_sends (email_hash, kind, attempted_at) VALUES ('{HASH}', 'code', '{STAMP}')"),
            ("registration_control", f"INSERT INTO registration_control (id, high_water_at) VALUES (1, '{STAMP}')"),
        ):
            with self.subTest(table=table), tempfile.TemporaryDirectory() as tmp:
                path = build_test_database(Path(tmp) / "used.db")
                with closing(sqlite3.connect(path)) as conn:
                    conn.execute(statement)
                    conn.commit()
                self.assertIn(f"{table}: 1 rows; a canonical rebuild must leave non-canonical tables empty",
                              builder.validation_problems(path))


class RegistrationCanonicalExclusionTest(unittest.TestCase):
    """Registration rows of a working database never reach a canonical export."""

    MARKERS = ("marker-first-name", "marker-last-name", "marker-school-name", "marker-pending@example.bg",
               "marker-subject-label", "marker-scrypt-hash", "marker-subjects-json")

    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="registration-export-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.path = build_test_database(self.root / "working.db")
        m = self.MARKERS
        with closing(sqlite3.connect(self.path)) as conn:
            conn.execute("PRAGMA foreign_keys = ON")
            user = conn.execute("INSERT INTO users (email, display_name, session_key, email_verified_at) "
                                "VALUES ('marker-account@example.bg', 'Маркер', ?, ?)", ("k" * 32, STAMP)).lastrowid
            conn.execute("INSERT INTO user_profiles (user_id, account_type, first_name, last_name, school_name) "
                         "VALUES (?, 'teacher', ?, ?, ?)", (user, m[0], m[1], m[2]))
            conn.execute("INSERT INTO teacher_subjects (user_id, position, subject_id, label) VALUES (?, 1, NULL, ?)",
                         (user, m[4]))
            conn.execute("INSERT INTO pending_registrations (binding_hash, account_type, state, is_shadow, email, "
                         "first_name, last_name, school_name, subjects_json, password_hash, created_at, expires_at) "
                         "VALUES (?, 'teacher', 'pending', 0, ?, ?, ?, ?, ?, ?, ?, ?)",
                         (HASH, m[3], m[0], m[1], m[2], m[6], m[5], STAMP, STAMP))
            conn.execute("INSERT INTO registration_sends (email_hash, kind, attempted_at) VALUES (?, 'code', ?)",
                         (OTHER_HASH, STAMP))
            conn.execute("INSERT INTO registration_control (id, high_water_at, hold_reason, hold_set_at) "
                         "VALUES (1, ?, 'restore', ?)", (STAMP, STAMP))
            conn.commit()

    def test_an_export_holds_the_canonical_tables_only_and_no_registration_value(self):  # A8
        out = self.root / "export"
        with closing(export.open_snapshot(self.path)) as conn:
            for table in NEW_TABLES:
                self.assertGreaterEqual(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 1, table)
            manifest = export.export_database(conn, out)
        self.assertEqual(sorted(path.name for path in out.iterdir()),
                         sorted([f"{table}.jsonl" for table in ct.tables_of(ct.CANONICAL)] + [ct.MANIFEST_NAME]))
        for table in NEW_TABLES:
            self.assertNotIn(table, manifest["tables"])
            self.assertNotIn(table, manifest["canonical_tables"])
            self.assertIn(table, manifest["runtime_exclusions"])
        exported = "".join(path.read_text(encoding="utf-8") for path in out.iterdir())
        for marker in (*self.MARKERS, "marker-account@example.bg", HASH, OTHER_HASH):
            self.assertNotIn(marker, exported)
        self.assertEqual(export.check_directory(out), [])

    def test_the_exported_content_is_the_tracked_content_row_for_row(self):  # canonical preservation
        out = self.root / "export"
        with closing(export.open_snapshot(self.path)) as conn:
            manifest = export.export_database(conn, out)
        tracked = export.load_manifest(ct.CANONICAL_DIR)
        # Rows, ids, sequences, columns and hashes of every canonical table.
        self.assertEqual(manifest["tables"], tracked["tables"])
        self.assertEqual(manifest["schema"], tracked["schema"])
        self.assertEqual(manifest["classification"], tracked["classification"])
        for table in ct.tables_of(ct.CANONICAL):
            self.assertEqual((out / f"{table}.jsonl").read_bytes(),
                             (ct.CANONICAL_DIR / f"{table}.jsonl").read_bytes(), table)
        self.assertEqual(export.identity_problems(ct.CANONICAL_DIR, out), [])

    def test_an_export_that_included_a_registration_table_would_be_refused(self):  # A8
        out = self.root / "export"
        with closing(export.open_snapshot(self.path)) as conn:
            export.export_database(conn, out)
        for table in NEW_TABLES:
            with self.subTest(table=table):
                (out / f"{table}.jsonl").write_text('{"id":1}\n', encoding="utf-8")
                self.assertIn(f"runtime/operational table {table} is included in the export",
                              export.identity_problems(ct.CANONICAL_DIR, out))
                self.assertIn(f"unexpected file in canonical directory: {table}.jsonl", export.check_directory(out))
                (out / f"{table}.jsonl").unlink()


if __name__ == "__main__":
    unittest.main()
