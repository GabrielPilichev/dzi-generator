"""019 adds ``user_identities`` additively: storage for external sign-in identities only.

One new table and one index, the approved schema of docs/ACCOUNTS_DESIGN.md section 5.
``users`` and every existing row stay as they were, nothing is seeded and no
application code uses the table yet. Every database here is temporary: built by the
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

MIGRATIONS = ROOT / "web" / "migrations"
MIGRATION = "019_user_identities.sql"
PREVIOUS = "018_assignment_lifecycle.sql"
TABLE = "user_identities"
INDEX = "idx_user_identities_user"
UNIQUE_INDEX = "sqlite_autoindex_user_identities_1"
COLUMNS = ("id", "user_id", "issuer", "subject", "provider", "email_at_link", "created_at", "last_login_at")
GOOGLE = "https://accounts.google.com"
MICROSOFT = "https://login.microsoftonline.com/9188040d-6c67-4c5b-b112-36a304b66dad/v2.0"
ADMIN_KEY = "admin-session-key-0123456789abcdef"
ADMIN_HASH = "scrypt:32768:8:1$adminsalt$" + "a1" * 64
DISABLED_HASH = "pbkdf2:sha256:600000$othersalt$" + "b2" * 32
THROTTLE_KEY = "f" * 64

# Account state on top of POPULATE_SQL (teacher 51 owns assignment 71, student 52 owns
# attempt 91): credentials, a disabled account, a second factor, a throttle row and a
# manual close. All of it must come through the migration untouched.
ACCOUNTS_SQL = f"""
    INSERT INTO users (id, email, display_name, role, password_hash, is_active, session_key,
                       password_changed_at, last_login_at, created_at, updated_at) VALUES
        (61, 'admin@example.bg', 'Админ', 'admin', '{ADMIN_HASH}', 1, '{ADMIN_KEY}',
         '2026-01-02 03:04:05', '2026-02-03 04:05:06', '2025-12-01 00:00:00', '2026-01-02 03:04:05'),
        (62, 'disabled@example.bg', 'Спрян', 'teacher', '{DISABLED_HASH}', 0,
         'disabled-session-key-0123456789abcd', NULL, NULL, '2025-12-02 00:00:00', '2025-12-03 00:00:00');
    INSERT INTO totp_credentials (user_id, secret_ciphertext, confirmed_at, last_used_step)
        VALUES (61, 'fernet-token', '2026-01-02 03:04:05', 58000000);
    INSERT INTO totp_recovery_codes (id, user_id, code_hash) VALUES (141, 61, 'scrypt:recovery');
    INSERT INTO login_throttle (identifier_hash, failure_count, window_started_at)
        VALUES ('{THROTTLE_KEY}', 3, '2026-02-03 04:00:00');
    UPDATE quiz_assignments SET closed_at = '2026-03-01 10:00:00', closed_by_user_id = 61 WHERE id = 81;
"""

INSERT = "INSERT INTO user_identities (user_id, issuer, subject, provider, email_at_link) VALUES (?, ?, ?, ?, ?)"


def apply(path, migrations_dir=MIGRATIONS):
    out = io.StringIO()
    code = manage_migrations.apply_migrations(path, migrations_dir, out=out)
    return code, out.getvalue()


def migration_text() -> str:
    return (MIGRATIONS / MIGRATION).read_text(encoding="utf-8")


def statements() -> str:
    """The migration without its comments."""
    return "\n".join(line for line in migration_text().splitlines() if not line.lstrip().startswith("--"))


def squeezed(sql: str) -> str:
    return " ".join(sql.split())


class UpgradedDatabaseTestCase(unittest.TestCase):
    """A database at migration 018 that already holds accounts and their data."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="user-identities-migration-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.path = self.root / "upgrade.db"
        sqlite3.connect(self.path).close()
        previous = self.root / "through-018"
        previous.mkdir()
        # 000..019 only: what 019 itself changes, whatever later migrations add.
        self.through = self.root / "through-019"
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
        answers index_list / foreign_key_list from its cached schema, which does not
        know a table that another connection created.
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

    def contents(self):
        return {name: self.conn.execute(f'SELECT * FROM "{name}" ORDER BY 1').fetchall()
                for (name,) in self.conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' AND name != 'schema_migrations'")}

    def schema_objects(self):
        """Everything sqlite_master records, except what belongs to the new table."""
        return self.conn.execute(
            "SELECT type, name, tbl_name, rootpage, sql FROM sqlite_master WHERE tbl_name != ? "
            "ORDER BY type, name", (TABLE,)).fetchall()

    def count(self):
        return self.conn.execute(f"SELECT COUNT(*) FROM {TABLE}").fetchone()[0]


class UserIdentitiesUpgradeTest(UpgradedDatabaseTestCase):
    def test_upgrade_from_018_adds_one_empty_table_and_changes_no_row(self):
        self.assertNotIn(TABLE, self.contents())
        before, objects_before = self.contents(), self.schema_objects()
        ledger_before = self.conn.execute("SELECT filename, applied_at FROM schema_migrations ORDER BY rowid").fetchall()
        self.assertEqual(ledger_before[-1][0], PREVIOUS)
        self.migrate()
        after = self.contents()
        self.assertEqual(after.pop(TABLE), [])  # nothing is seeded or backfilled
        # Accounts, second factors, assignments, attempts, answers, uploads, content and
        # every sequence (no user_identities entry appears in sqlite_sequence): untouched.
        self.assertEqual(after, before)
        # No existing table, index or root page changed: nothing was altered or rebuilt.
        self.assertEqual(self.schema_objects(), objects_before)
        ledger = self.conn.execute("SELECT filename, applied_at FROM schema_migrations ORDER BY rowid").fetchall()
        self.assertEqual(ledger[:-1], ledger_before)
        self.assertEqual(ledger[-1][0], MIGRATION)
        self.assertEqual(self.conn.execute("PRAGMA foreign_key_check").fetchall(), [])
        self.assertEqual(self.conn.execute("PRAGMA integrity_check").fetchall(), [("ok",)])

    def test_existing_accounts_credentials_sessions_roles_and_ownership_are_unchanged(self):
        users = "SELECT * FROM users ORDER BY id"
        ownership = {
            "assignments": "SELECT id, owner_user_id, closed_by_user_id FROM quiz_assignments ORDER BY id",
            "attempts": "SELECT id, assignment_id, user_id, student_name FROM quiz_attempts ORDER BY id",
            "second factor": "SELECT * FROM totp_credentials ORDER BY user_id",
            "recovery codes": "SELECT * FROM totp_recovery_codes ORDER BY id",
            "throttle": "SELECT * FROM login_throttle ORDER BY identifier_hash",
        }
        users_before = self.conn.execute(users).fetchall()
        before = {name: self.conn.execute(sql).fetchall() for name, sql in ownership.items()}
        self.assertEqual(len(users_before), 4)
        self.migrate()
        self.assertEqual(self.conn.execute(users).fetchall(), users_before)
        self.assertEqual({name: self.conn.execute(sql).fetchall() for name, sql in ownership.items()}, before)
        # Stated once in clear: the hash, the session key (016 rotated admin keys; 019
        # rotates nothing), the role and the active flag of each account.
        self.assertEqual(
            self.conn.execute("SELECT id, role, is_active, password_hash, session_key FROM users ORDER BY id").fetchall(),
            [(51, "teacher", 1, None, "12345678901234567890123456789012"),
             (52, "student", 1, None, "22345678901234567890123456789012"),
             (61, "admin", 1, ADMIN_HASH, ADMIN_KEY),
             (62, "teacher", 0, DISABLED_HASH, "disabled-session-key-0123456789abcd")])
        self.assertEqual(before["assignments"], [(71, 51, None), (81, None, 61)])
        self.assertEqual(before["attempts"], [(91, 71, 52, "Ученик"), (92, 71, None, "Исторически")])
        # The next account still gets the next id: no id is reused or skipped.
        self.assertEqual(self.conn.execute(
            "INSERT INTO users (display_name, session_key) VALUES ('Нов', ?)", ("n" * 32,)).lastrowid, 63)

    def test_it_is_a_plain_additive_migration_not_a_rebuild(self):
        dry = io.StringIO()
        manage_migrations.dry_run(self.path, self.through, out=dry)
        self.assertIn(f"pending migrations:\n  - {MIGRATION}\n", dry.getvalue())
        self.assertNotIn(TABLE, self.contents())  # the dry run wrote nothing
        report = self.migrate()
        self.assertIn(f"applied migrations:\n  - {MIGRATION}\n", report)
        self.assertIn("foreign_key_check: ok", report)
        self.assertNotIn("table-rebuild", report)
        self.assertFalse(manage_migrations.is_table_rebuild_migration(MIGRATION, migration_text()))

    def test_reapplying_is_a_noop_and_keeps_identity_rows(self):
        self.migrate()
        self.conn.execute(INSERT, (52, GOOGLE, "subject-1", "google", "student@example.bg"))
        self.conn.commit()
        before = self.contents()
        report = self.migrate()
        self.assertIn("applied migrations:\n  - none\n", report)
        self.assertEqual(self.contents(), before)
        # Even run by hand again the file changes nothing: IF NOT EXISTS, and no data statement.
        self.conn.executescript(migration_text())
        self.assertEqual(self.contents(), before)

    def test_upgraded_schema_equals_a_fresh_database(self):
        self.migrate()
        self.reopen()
        fresh_path = self.root / "fresh.db"
        sqlite3.connect(fresh_path).close()
        self.assertEqual(apply(fresh_path, self.through)[0], 0)
        with closing(sqlite3.connect(fresh_path)) as fresh:
            for pragma in ("table_info", "foreign_key_list", "index_list"):
                self.assertEqual(fresh.execute(f"PRAGMA {pragma}({TABLE})").fetchall(),
                                 self.conn.execute(f"PRAGMA {pragma}({TABLE})").fetchall(), pragma)
            schema = "SELECT type, name, sql FROM sqlite_master WHERE tbl_name = ? ORDER BY type, name"
            self.assertEqual(fresh.execute(schema, (TABLE,)).fetchall(),
                             self.conn.execute(schema, (TABLE,)).fetchall())
            self.assertEqual(fresh.execute(f"SELECT COUNT(*) FROM {TABLE}").fetchone()[0], 0)
            ledger = [row[0] for row in fresh.execute("SELECT filename FROM schema_migrations ORDER BY rowid")]
        self.assertEqual(ledger[ledger.index(PREVIOUS) + 1], MIGRATION)

    def test_019_is_never_adopted_without_running_it(self):
        self.conn.row_factory = sqlite3.Row
        self.assertEqual(manage_migrations.baseline_status(self.conn, MIGRATION)[0], "manual review required")


class UserIdentitiesSchemaTest(UpgradedDatabaseTestCase):
    def setUp(self):
        super().setUp()
        self.migrate()
        self.reopen()

    def add(self, user_id=52, issuer=GOOGLE, subject="subject-1", provider="google", email=None):
        return self.conn.execute(INSERT, (user_id, issuer, subject, provider, email)).lastrowid

    def owner(self, issuer, subject):
        rows = self.conn.execute(
            f"SELECT user_id FROM {TABLE} WHERE issuer = ? AND subject = ?", (issuer, subject)).fetchall()
        return [row[0] for row in rows]

    def test_columns_are_the_approved_ones_in_the_approved_order(self):
        info = self.conn.execute(f"PRAGMA table_info({TABLE})").fetchall()
        self.assertEqual(
            [(name, declared, notnull, default, pk) for _cid, name, declared, notnull, default, pk in info],
            [("id", "INTEGER", 0, None, 1),
             ("user_id", "INTEGER", 1, None, 0),
             ("issuer", "TEXT", 1, None, 0),
             ("subject", "TEXT", 1, None, 0),
             ("provider", "TEXT", 1, None, 0),
             ("email_at_link", "TEXT", 0, None, 0),
             ("created_at", "TEXT", 1, "CURRENT_TIMESTAMP", 0),
             ("last_login_at", "TEXT", 0, None, 0)])
        self.assertEqual(tuple(row[1] for row in info), COLUMNS)
        self.assertTrue(ct.is_autoincrement(self.conn, TABLE))

    def test_defaults_of_a_new_row(self):
        identity = self.add()
        row = self.conn.execute(
            f"SELECT user_id, issuer, subject, provider, email_at_link, created_at, last_login_at "
            f"FROM {TABLE} WHERE id = ?", (identity,)).fetchone()
        self.assertEqual(row[:5], (52, GOOGLE, "subject-1", "google", None))
        self.assertRegex(row[5], r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d$")
        self.assertIsNone(row[6])

    def test_the_only_foreign_key_is_users_id_without_any_action(self):
        self.assertEqual(
            [tuple(row[2:]) for row in self.conn.execute(f"PRAGMA foreign_key_list({TABLE})")],
            [("users", "user_id", "id", "NO ACTION", "NO ACTION", "NONE")])
        # Nothing was added to users, and no other table references the new one.
        referencing = [name for (name,) in self.conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
                       if any(row[2] == TABLE for row in self.conn.execute(f'PRAGMA foreign_key_list("{name}")'))]
        self.assertEqual(referencing, [])

    def test_indexes_are_the_user_index_and_the_identity_uniqueness_only(self):
        indexes = {row[1]: row for row in self.conn.execute(f"PRAGMA index_list({TABLE})")}
        self.assertEqual(sorted(indexes), [INDEX, UNIQUE_INDEX])
        # (unique, origin, partial): a plain index, and the table's UNIQUE constraint.
        self.assertEqual(tuple(indexes[INDEX][2:]), (0, "c", 0))
        self.assertEqual(tuple(indexes[UNIQUE_INDEX][2:]), (1, "u", 0))
        columns = {name: [row[2] for row in self.conn.execute(f"PRAGMA index_info({name})")] for name in indexes}
        self.assertEqual(columns, {INDEX: ["user_id"], UNIQUE_INDEX: ["issuer", "subject"]})
        # Compared byte for byte: neither NOCASE nor RTRIM.
        self.assertEqual({row[4] for row in self.conn.execute(f"PRAGMA index_xinfo({UNIQUE_INDEX})") if row[2]},
                         {"BINARY"})
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type IN ('trigger', 'view') AND tbl_name = ?",
            (TABLE,)).fetchone()[0], 0)

    def test_the_exact_lookup_and_the_per_user_listing_use_the_indexes(self):
        def plan(sql):
            return " ".join(row[3] for row in self.conn.execute("EXPLAIN QUERY PLAN " + sql, ("a", "b")[:sql.count("?")]))
        self.assertIn(UNIQUE_INDEX, plan(f"SELECT id FROM {TABLE} WHERE issuer = ? AND subject = ?"))
        self.assertIn(INDEX, plan(f"SELECT id FROM {TABLE} WHERE user_id = ?"))

    def test_provider_is_google_or_microsoft_only(self):
        self.add(subject="g", provider="google")
        self.add(subject="m", issuer=MICROSOFT, provider="microsoft")
        for provider in ("apple", "Google", "MICROSOFT", "google ", "", "facebook", None):
            with self.subTest(provider=provider), self.assertRaises(sqlite3.IntegrityError):
                self.add(subject="other", provider=provider)
        self.assertEqual(self.count(), 2)

    def test_issuer_and_subject_are_unique_together_whoever_asks(self):
        first = self.add(user_id=52)
        for user_id in (52, 51, 61):  # the same account again, and two other accounts
            with self.subTest(user_id=user_id), self.assertRaises(sqlite3.IntegrityError):
                self.add(user_id=user_id)
        with self.assertRaises(sqlite3.IntegrityError):  # other provider and email: still the same identity
            self.add(user_id=51, provider="microsoft", email="other@example.bg")
        self.assertEqual(self.conn.execute(f"SELECT id, user_id, provider, email_at_link FROM {TABLE}").fetchall(),
                         [(first, 52, "google", None)])

    def test_identity_keys_are_compared_exactly(self):
        self.add(user_id=52, issuer=GOOGLE, subject="AbC")
        different = [
            (GOOGLE, "abc"),                        # case of the subject
            (GOOGLE, "ABC"),
            ("https://Accounts.Google.com", "AbC"),  # case of the issuer
            (GOOGLE + "/", "AbC"),                  # a trailing slash is another issuer
            (GOOGLE, "AbC "),                       # nothing is trimmed
            (GOOGLE, " AbC"),
            (GOOGLE + " ", "AbC"),
            (MICROSOFT, "AbC"),                     # the same subject under another issuer
        ]
        for issuer, subject in different:
            with self.subTest(issuer=issuer, subject=subject):
                self.assertEqual(self.owner(issuer, subject), [])
                self.add(user_id=51, issuer=issuer, subject=subject)
                self.assertEqual(self.owner(issuer, subject), [51])
        self.assertEqual(self.owner(GOOGLE, "AbC"), [52])
        self.assertEqual(self.count(), len(different) + 1)

    def test_a_numeric_looking_subject_stays_text(self):
        self.add(subject="000123456789012345678901")
        self.assertEqual(self.conn.execute(f"SELECT subject, typeof(subject), typeof(issuer) FROM {TABLE}").fetchall(),
                         [("000123456789012345678901", "text", "text")])
        self.assertEqual(self.owner(GOOGLE, "123456789012345678901"), [])

    def test_user_issuer_subject_and_provider_are_required(self):
        for values in ((None, GOOGLE, "s", "google", None), (52, None, "s", "google", None),
                       (52, GOOGLE, None, "google", None), (52, GOOGLE, "s", None, None)):
            with self.subTest(values=values), self.assertRaises(sqlite3.IntegrityError):
                self.conn.execute(INSERT, values)
        self.assertEqual(self.count(), 0)

    def test_a_user_may_have_several_identities(self):
        self.add(subject="google-1")
        self.add(subject="google-2")
        self.add(issuer=MICROSOFT, subject="microsoft-1", provider="microsoft")
        self.assertEqual(self.conn.execute(f"SELECT COUNT(*) FROM {TABLE} WHERE user_id = 52").fetchone()[0], 3)

    def test_email_at_link_is_neither_unique_nor_a_key(self):
        self.add(user_id=51, subject="one", email="shared@example.bg")
        self.add(user_id=52, subject="two", email="shared@example.bg")
        self.add(user_id=52, subject="three", email="admin@example.bg")  # the email of account 61
        self.add(user_id=52, subject="four", email=None)
        self.assertEqual(
            self.conn.execute(f"SELECT user_id, email_at_link FROM {TABLE} ORDER BY id").fetchall(),
            [(51, "shared@example.bg"), (52, "shared@example.bg"), (52, "admin@example.bg"), (52, None)])
        indexed = {row[2] for name in (INDEX, UNIQUE_INDEX) for row in self.conn.execute(f"PRAGMA index_info({name})")}
        self.assertNotIn("email_at_link", indexed)

    def test_an_identity_needs_an_existing_user(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.add(user_id=9999)
        self.assertEqual(self.count(), 0)

    def test_nothing_cascades_from_the_account(self):
        account = self.conn.execute(
            "INSERT INTO users (display_name, session_key) VALUES ('Само с идентичност', ?)", ("i" * 32,)).lastrowid
        identity = self.add(user_id=account)
        self.conn.commit()
        for statement in ("DELETE FROM users WHERE id = ?", "UPDATE users SET id = id + 1000 WHERE id = ?"):
            with self.subTest(statement=statement), self.assertRaises(sqlite3.IntegrityError):
                self.conn.execute(statement, (account,))
        self.assertEqual(self.conn.execute(f"SELECT id, user_id FROM {TABLE}").fetchall(), [(identity, account)])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM users WHERE id = ?", (account,)).fetchone()[0], 1)
        self.assertEqual(self.conn.execute("PRAGMA foreign_key_check").fetchall(), [])


class UserIdentitiesMigrationTextTest(unittest.TestCase):
    def test_the_file_is_the_approved_schema_and_nothing_else(self):
        design = (ROOT / "docs" / "ACCOUNTS_DESIGN.md").read_text(encoding="utf-8")
        approved = [block for block in re.findall(r"```sql\n(.*?)```", design, flags=re.S)
                    if f"CREATE TABLE IF NOT EXISTS {TABLE}" in block]
        self.assertEqual(len(approved), 1)
        self.assertEqual(squeezed(statements()), squeezed("PRAGMA foreign_keys = ON;\n" + approved[0]))

    def test_nothing_destructive_and_nothing_of_a_later_phase(self):
        sql = statements().upper()
        self.assertEqual(re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)", sql), [TABLE.upper()])
        self.assertEqual(re.findall(r"CREATE INDEX IF NOT EXISTS (\w+)", sql), [INDEX.upper()])
        self.assertEqual(len(re.findall(r"\bCREATE\b", sql)), 2)
        for forbidden in ("DROP", "DELETE", "UPDATE", "INSERT", "ALTER", "RENAME", "REPLACE", "TRIGGER",
                          "CASCADE", "SET NULL", "RESTRICT", "OAUTH", "NONCE", "STATE", "TOKEN", "SECRET",
                          "PASSWORD", "SESSION"):
            self.assertNotIn(forbidden, sql, forbidden)
        self.assertNotIn("learnpilot-migration", migration_text())

    def test_users_is_referenced_and_not_changed(self):
        self.assertEqual(re.findall(r"\bREFERENCES\s+(\w+)\((\w+)\)", statements()), [("users", "id")])
        self.assertEqual(len(re.findall(r"\busers\b", statements())), 1)

    def test_the_files_are_numbered_without_a_collision(self):
        names = sorted(path.name for path in MIGRATIONS.glob("*.sql"))
        self.assertEqual([name for name in names if name.startswith("019_")], [MIGRATION])
        self.assertEqual(names[names.index(MIGRATION) - 1], PREVIOUS)
        self.assertEqual([name[:3] for name in names], [f"{n:03d}" for n in range(len(names))])
        created = [name for name in names
                   if re.search(rf"CREATE TABLE (IF NOT EXISTS )?{TABLE}\b", (MIGRATIONS / name).read_text(encoding="utf-8"))]
        self.assertEqual(created, [MIGRATION])


class UserIdentitiesClassificationTest(unittest.TestCase):
    def test_the_table_is_runtime_not_canonical_and_not_seeded(self):
        self.assertEqual(ct.TABLE_CLASSIFICATION[TABLE][0], ct.RUNTIME)
        self.assertIn(TABLE, ct.tables_of(ct.RUNTIME))
        self.assertNotIn(TABLE, ct.tables_of(ct.CANONICAL))
        self.assertNotIn(TABLE, ct.tables_of(ct.OPERATIONAL))
        self.assertNotIn(TABLE, ct.MIGRATION_SEEDED_ROWS)
        self.assertNotIn(TABLE, ct.NATURAL_KEYS)
        self.assertFalse((ROOT / "data" / "canonical" / f"{TABLE}.jsonl").exists())

    def test_manifest_records_the_level_and_the_exclusion(self):
        manifest = json.loads((ROOT / "data" / "canonical" / "manifest.json").read_text(encoding="utf-8"))
        self.assertGreaterEqual(manifest["schema"]["migration_level"], MIGRATION)
        self.assertIn(MIGRATION, manifest["schema"]["migrations"])
        self.assertIn(TABLE, manifest["runtime_exclusions"])
        self.assertIn(TABLE, manifest["classification"]["runtime"])
        self.assertNotIn(TABLE, manifest["classification"]["canonical"])
        self.assertNotIn(TABLE, manifest["canonical_tables"])
        self.assertNotIn(TABLE, manifest["tables"])  # never exported to data/canonical
        self.assertEqual(export.check_directory(ct.CANONICAL_DIR), [])

    def test_a_fresh_canonical_build_has_the_schema_and_no_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = build_test_database(Path(tmp) / "fresh.db")
            with closing(sqlite3.connect(path)) as conn:
                applied = [r[0] for r in conn.execute("SELECT filename FROM schema_migrations ORDER BY filename")]
                self.assertIn(MIGRATION, applied)
                self.assertEqual(tuple(r[1] for r in conn.execute(f"PRAGMA table_info({TABLE})")), COLUMNS)
                for table in (TABLE, "users"):
                    self.assertEqual(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0, table)
                # Not even a sequence entry: no identity row was ever inserted and removed.
                self.assertIsNone(ct.stored_sequence(conn, TABLE))
                self.assertEqual(conn.execute("SELECT * FROM pragma_foreign_key_check").fetchall(), [])
                self.assertEqual(ct.classification_problems(conn), [])
            self.assertEqual(builder.validation_problems(path), [])

    def test_a_database_that_holds_an_identity_is_not_a_canonical_build(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = build_test_database(Path(tmp) / "used.db")
            with closing(sqlite3.connect(path)) as conn:
                conn.execute("PRAGMA foreign_keys = ON")
                user = conn.execute("INSERT INTO users (display_name, session_key) VALUES ('Ученик', ?)",
                                    ("k" * 32,)).lastrowid
                conn.execute(INSERT, (user, GOOGLE, "subject-1", "google", None))
                conn.commit()
            problems = builder.validation_problems(path)
            self.assertIn(f"{TABLE}: 1 rows; a canonical rebuild must leave non-canonical tables empty", problems)


class UserIdentitiesCanonicalExclusionTest(unittest.TestCase):
    """Identity rows of a working database never reach a canonical export."""

    MARKERS = ("marker-issuer.example", "marker-subject-0123456789", "marker-link@example.bg",
               "marker-account@example.bg")

    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="user-identities-export-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.path = build_test_database(self.root / "working.db")
        with closing(sqlite3.connect(self.path)) as conn:
            conn.execute("PRAGMA foreign_keys = ON")
            user = conn.execute(
                "INSERT INTO users (email, display_name, session_key) VALUES (?, 'Маркер', ?)",
                (self.MARKERS[3], "k" * 32)).lastrowid
            conn.execute(INSERT, (user, f"https://{self.MARKERS[0]}", self.MARKERS[1], "google", self.MARKERS[2]))
            conn.execute(INSERT, (user, MICROSOFT, self.MARKERS[1], "microsoft", self.MARKERS[2]))
            conn.commit()

    def test_an_export_holds_the_canonical_tables_only_and_no_identity_value(self):
        out = self.root / "export"
        with closing(export.open_snapshot(self.path)) as conn:
            self.assertEqual(conn.execute(f"SELECT COUNT(*) FROM {TABLE}").fetchone()[0], 2)
            manifest = export.export_database(conn, out)
        self.assertEqual(sorted(path.name for path in out.iterdir()),
                         sorted([f"{table}.jsonl" for table in ct.tables_of(ct.CANONICAL)] + [ct.MANIFEST_NAME]))
        self.assertNotIn(TABLE, manifest["tables"])
        self.assertNotIn(TABLE, manifest["canonical_tables"])
        self.assertIn(TABLE, manifest["runtime_exclusions"])
        exported = "".join(path.read_text(encoding="utf-8") for path in out.iterdir())
        for marker in self.MARKERS:
            self.assertNotIn(marker, exported)
        self.assertEqual(export.check_directory(out), [])

    def test_the_exported_content_is_the_tracked_content_row_for_row(self):
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

    def test_an_export_that_included_identities_would_be_refused(self):
        out = self.root / "export"
        with closing(export.open_snapshot(self.path)) as conn:
            export.export_database(conn, out)
        (out / f"{TABLE}.jsonl").write_text('{"id":1}\n', encoding="utf-8")
        self.assertIn(f"runtime/operational table {TABLE} is included in the export",
                      export.identity_problems(ct.CANONICAL_DIR, out))
        self.assertIn(f"unexpected file in canonical directory: {TABLE}.jsonl", export.check_directory(out))


if __name__ == "__main__":
    unittest.main()
