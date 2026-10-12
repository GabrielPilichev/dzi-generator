"""020 adds ``oidc_consumed_transactions`` additively: single-use records, storage only.

One new table without an index or a row (docs/reviews/OIDC_CONSUMPTION_SCOPE.md, and
docs/ACCOUNTS_DESIGN.md section 5). Every other table and row stays as it was, and no
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
from tests.test_user_identities_migration_sql import ACCOUNTS_SQL

MIGRATIONS = ROOT / "web" / "migrations"
MIGRATION = "020_oidc_consumed_transactions.sql"
PREVIOUS = "019_user_identities.sql"
TABLE = "oidc_consumed_transactions"
PRIMARY_KEY_INDEX = f"sqlite_autoindex_{TABLE}_1"
COLUMNS = ("tx_hash", "expires_at")
HEX = "0123456789abcdef" * 4
OTHER = "f" * 64

# On top of POPULATE_SQL and ACCOUNTS_SQL: what migration 019 stores. All of it must
# come through migration 020 untouched.
IDENTITIES_SQL = """
    INSERT INTO user_identities (id, user_id, issuer, subject, provider, email_at_link, created_at, last_login_at)
        VALUES (151, 52, 'https://accounts.google.com', '110169484474386276334', 'google',
                'student@example.bg', '2026-03-02 08:00:00', '2026-03-03 09:00:00');
"""

INSERT = f"INSERT INTO {TABLE} (tx_hash, expires_at) VALUES (?, ?)"

# Every shape a key must not have. The NUL cases are why the CHECK counts both the
# characters and the bytes: SQLite's length() and GLOB stop at the first NUL.
REFUSED_HASHES = {
    "63 characters": HEX[:63],
    "65 characters": HEX + "0",
    "empty": "",
    "upper-case hexadecimal": HEX.upper(),
    "one upper-case digit": HEX[:63] + "A",
    "one character outside 0-9a-f": HEX[:63] + "g",
    "a 43-character URL-safe value": "Ab-_" * 10 + "Ab-",
    "64 URL-safe characters": "Zz-_" * 16,
    "trailing NUL after 64 digits": HEX + "\x00",
    "trailing NUL and more after 64 digits": HEX + "\x00" + "not hexadecimal",
    "embedded NUL, 64 bytes, digits on both sides": HEX[:10] + "\x00" + HEX[:53],
    "embedded NUL, 64 bytes, anything after it": HEX[:10] + "\x00" + "Z" * 53,
    "leading NUL, 64 bytes": "\x00" + HEX[:63],
    "NUL as the 64th byte": HEX[:63] + "\x00",
    "trailing newline after 64 digits": HEX + "\n",
    "newline as the 64th character": HEX[:63] + "\n",
    "carriage return as the 64th character": HEX[:63] + "\r",
    "space padding": " " + HEX[:62] + " ",
    "a non-ASCII character, 64 characters": HEX[:63] + "é",
    "a non-ASCII character, 64 bytes": HEX[:62] + "é",
    "a full-width digit": HEX[:61] + "０",
    "a 64-byte BLOB of digits": HEX.encode("ascii"),
    "the integer 123": 123,
    "a real number": 1.5,
    "NULL": None,
}


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
    """A database at migration 019 that already holds accounts, identities and their data."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="oidc-consumed-migration-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.path = self.root / "upgrade.db"
        sqlite3.connect(self.path).close()
        previous = self.root / "through-019"
        previous.mkdir()
        # 000..020 only: what 020 itself changes, whatever later migrations add.
        self.through = self.root / "through-020"
        self.through.mkdir()
        for path in sorted(MIGRATIONS.glob("*.sql")):
            if path.name < MIGRATION:
                shutil.copyfile(path, previous / path.name)
            if path.name <= MIGRATION:
                shutil.copyfile(path, self.through / path.name)
        self.assertEqual(apply(self.path, previous)[0], 0)
        self.conn = None
        self.reopen()
        self.conn.executescript(POPULATE_SQL + ACCOUNTS_SQL + IDENTITIES_SQL)
        self.conn.commit()

    def reopen(self):
        """A new connection with foreign keys on, as the application's are.

        Needed after the migration for schema pragmas: a connection opened before it
        answers them from its cached schema, which does not know a table that another
        connection created.
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

    def rows(self):
        return self.conn.execute(f"SELECT tx_hash, expires_at FROM {TABLE} ORDER BY tx_hash").fetchall()


class ConsumedTransactionsUpgradeTest(UpgradedDatabaseTestCase):
    def test_upgrade_from_019_adds_one_empty_table_and_changes_no_row(self):
        self.assertNotIn(TABLE, self.contents())
        before, objects_before = self.contents(), self.schema_objects()
        ledger_before = self.conn.execute("SELECT filename, applied_at FROM schema_migrations ORDER BY rowid").fetchall()
        self.assertEqual(ledger_before[-1][0], PREVIOUS)
        self.migrate()
        after = self.contents()
        self.assertEqual(after.pop(TABLE), [])  # nothing is seeded
        # Accounts, identities, second factors, the throttle, assignments, attempts,
        # answers, uploads, content, the lifecycle clock and every sequence: untouched.
        self.assertEqual(after, before)
        # No existing table, index or root page changed: nothing was altered or rebuilt.
        self.assertEqual(self.schema_objects(), objects_before)
        ledger = self.conn.execute("SELECT filename, applied_at FROM schema_migrations ORDER BY rowid").fetchall()
        self.assertEqual(ledger[:-1], ledger_before)
        self.assertEqual(ledger[-1][0], MIGRATION)
        self.assertEqual(self.conn.execute("PRAGMA foreign_key_check").fetchall(), [])
        self.assertEqual(self.conn.execute("PRAGMA integrity_check").fetchall(), [("ok",)])

    def test_existing_accounts_identities_and_sign_in_state_are_unchanged(self):
        state = {
            "users": "SELECT * FROM users ORDER BY id",
            "identities": "SELECT * FROM user_identities ORDER BY id",
            "second factor": "SELECT * FROM totp_credentials ORDER BY user_id",
            "recovery codes": "SELECT * FROM totp_recovery_codes ORDER BY id",
            "throttle": "SELECT * FROM login_throttle ORDER BY identifier_hash",
            "assignments": "SELECT id, owner_user_id, closed_at, closed_by_user_id FROM quiz_assignments ORDER BY id",
            "attempts": "SELECT id, assignment_id, user_id, student_name FROM quiz_attempts ORDER BY id",
            "clock": "SELECT * FROM lifecycle_clock",
        }
        before = {name: self.conn.execute(sql).fetchall() for name, sql in state.items()}
        self.assertEqual([len(before[name]) for name in ("users", "identities", "throttle", "clock")], [4, 1, 1, 1])
        self.migrate()
        self.assertEqual({name: self.conn.execute(sql).fetchall() for name, sql in state.items()}, before)
        self.assertEqual(before["identities"][0][:5],
                         (151, 52, "https://accounts.google.com", "110169484474386276334", "google"))
        # Nobody is signed out: every session key is the one it was.
        self.assertEqual([row[0] for row in self.conn.execute("SELECT session_key FROM users ORDER BY id")],
                         ["12345678901234567890123456789012", "22345678901234567890123456789012",
                          "admin-session-key-0123456789abcdef", "disabled-session-key-0123456789abcd"])
        # The next account and the next identity still get the next id.
        self.assertEqual(self.conn.execute(
            "INSERT INTO users (display_name, session_key) VALUES ('Нов', ?)", ("n" * 32,)).lastrowid, 63)
        self.assertEqual(self.conn.execute(
            "INSERT INTO user_identities (user_id, issuer, subject, provider) VALUES (51, 'https://i', 's', 'google')"
        ).lastrowid, 152)

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

    def test_reapplying_is_a_noop_and_keeps_consumption_rows(self):
        self.migrate()
        self.conn.execute(INSERT, (HEX, 600))
        self.conn.commit()
        before = self.contents()
        report = self.migrate()
        self.assertIn("applied migrations:\n  - none\n", report)
        self.assertEqual(self.contents(), before)
        # Even run by hand again the file changes nothing: IF NOT EXISTS, and no data statement.
        self.conn.executescript(migration_text())
        self.assertEqual(self.contents(), before)
        self.assertEqual(self.rows(), [(HEX, 600)])

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

    def test_020_is_never_adopted_without_running_it(self):
        self.conn.row_factory = sqlite3.Row
        self.assertEqual(manage_migrations.baseline_status(self.conn, MIGRATION)[0], "manual review required")


class ConsumedTransactionsSchemaTest(UpgradedDatabaseTestCase):
    def setUp(self):
        super().setUp()
        self.migrate()
        self.reopen()

    def refused(self, tx_hash=HEX, expires_at=600):
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(INSERT, (tx_hash, expires_at))

    def test_the_columns_are_exactly_the_hash_and_the_expiry(self):
        info = self.conn.execute(f"PRAGMA table_info({TABLE})").fetchall()
        self.assertEqual(
            [(name, declared, notnull, default, pk) for _cid, name, declared, notnull, default, pk in info],
            [("tx_hash", "TEXT", 1, None, 1), ("expires_at", "INTEGER", 1, None, 0)])
        self.assertEqual(tuple(row[1] for row in info), COLUMNS)
        # Which run of the service wrote a row is not recorded anywhere.
        self.assertNotIn("instance", {row[1] for row in info})

    def test_the_table_is_without_rowid_and_keyed_by_the_hash(self):
        with self.assertRaises(sqlite3.OperationalError):
            self.conn.execute(f"SELECT rowid FROM {TABLE}")
        self.assertFalse(ct.is_autoincrement(self.conn, TABLE))
        self.assertEqual(ct.primary_key_columns(self.conn, TABLE), ["tx_hash"])
        # Byte for byte: neither NOCASE nor RTRIM decides whether two keys are the same.
        self.assertEqual({row[4] for row in self.conn.execute(f"PRAGMA index_xinfo({PRIMARY_KEY_INDEX})") if row[2]},
                         {"BINARY"})

    def test_there_is_no_secondary_index_trigger_view_or_foreign_key(self):
        indexes = self.conn.execute(f"PRAGMA index_list({TABLE})").fetchall()
        # The primary key of a WITHOUT ROWID table is the table itself: its only index.
        self.assertEqual([(row[1], row[2], row[3]) for row in indexes], [(PRIMARY_KEY_INDEX, 1, "pk")])
        self.assertEqual(self.conn.execute(
            "SELECT type, name FROM sqlite_master WHERE tbl_name = ? ORDER BY type", (TABLE,)).fetchall(),
            [("table", TABLE)])
        self.assertEqual(self.conn.execute(f"PRAGMA foreign_key_list({TABLE})").fetchall(), [])
        referencing = [name for (name,) in self.conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
                       if any(row[2] == TABLE for row in self.conn.execute(f'PRAGMA foreign_key_list("{name}")'))]
        self.assertEqual(referencing, [])

    def test_a_key_of_64_lower_case_hexadecimal_characters_is_stored_as_given(self):
        for tx_hash in (HEX, "0" * 64, "9" * 64, "a" * 64, OTHER):
            self.conn.execute(INSERT, (tx_hash, 600))
        self.assertEqual(
            self.conn.execute(f"SELECT tx_hash, typeof(tx_hash), length(tx_hash), length(CAST(tx_hash AS BLOB)) "
                              f"FROM {TABLE} WHERE tx_hash = ?", (HEX,)).fetchall(),
            [(HEX, "text", 64, 64)])
        self.assertEqual(len(self.rows()), 5)

    def test_every_other_shape_of_key_is_refused(self):
        for label, tx_hash in REFUSED_HASHES.items():
            with self.subTest(label):
                self.refused(tx_hash=tx_hash)
        self.assertEqual(self.rows(), [])

    def test_a_refused_key_is_refused_next_to_stored_rows_too(self):
        # A value that SQLite compares equal to nothing stored must still not get in.
        self.conn.execute(INSERT, (HEX, 600))
        for label in ("trailing NUL after 64 digits", "embedded NUL, 64 bytes, digits on both sides",
                      "trailing newline after 64 digits", "a 64-byte BLOB of digits"):
            with self.subTest(label):
                self.refused(tx_hash=REFUSED_HASHES[label])
        self.assertEqual(self.rows(), [(HEX, 600)])

    def test_whole_numbers_are_stored_as_integers_whatever_form_they_arrive_in(self):
        # INTEGER affinity converts these before the CHECK sees them. Accepted, and
        # pinned here so that it is known and not assumed.
        arrivals = {"an integer": 600, "the text '600'": "600", "padded text": " 600 ", "exponent text": "6e2",
                    "the real 600.0": 600.0, "the text '600.0'": "600.0"}
        for number, (label, value) in enumerate(arrivals.items()):
            with self.subTest(label):
                tx_hash = f"{number:064x}"
                self.conn.execute(INSERT, (tx_hash, value))
                self.assertEqual(
                    self.conn.execute(f"SELECT expires_at, typeof(expires_at) FROM {TABLE} WHERE tx_hash = ?",
                                      (tx_hash,)).fetchone(),
                    (600, "integer"))

    def test_the_expiry_may_be_zero_and_as_large_as_an_integer_gets(self):
        self.conn.execute(INSERT, (HEX, 0))
        self.conn.execute(INSERT, (OTHER, 2**63 - 1))
        self.assertEqual(self.conn.execute(f"SELECT expires_at, typeof(expires_at) FROM {TABLE} ORDER BY 1").fetchall(),
                         [(0, "integer"), (2**63 - 1, "integer")])

    def test_an_expiry_that_is_not_a_non_negative_whole_number_is_refused(self):
        refused = {"text that is not a number": "soon", "empty text": "", "a real with a fraction": 600.5,
                   "text with a fraction": "600.5", "a negative integer": -1, "negative text": "-1",
                   "NULL": None, "a BLOB": b"600", "a real beyond 64 bits": 1e19,
                   "text beyond 64 bits": "9223372036854775808", "infinity": float("inf"),
                   "hexadecimal text": "0x10"}
        for label, value in refused.items():
            with self.subTest(label):
                self.refused(expires_at=value)
        self.assertEqual(self.rows(), [])

    def test_a_duplicate_key_is_refused_by_the_primary_key_and_the_row_is_unchanged(self):
        self.conn.execute(INSERT, (HEX, 600))
        for expires_at in (600, 601, 0):
            with self.subTest(expires_at=expires_at):
                self.refused(expires_at=expires_at)
        self.conn.execute(INSERT, (OTHER, 600))  # another key with the same expiry is another row
        self.assertEqual(self.rows(), [(HEX, 600), (OTHER, 600)])


class ConsumedTransactionsMigrationTextTest(unittest.TestCase):
    def test_the_file_is_the_documented_schema_and_nothing_else(self):
        design = (ROOT / "docs" / "ACCOUNTS_DESIGN.md").read_text(encoding="utf-8")
        documented = [block for block in re.findall(r"```sql\n(.*?)```", design, flags=re.S)
                      if f"CREATE TABLE IF NOT EXISTS {TABLE}" in block]
        self.assertEqual(len(documented), 1)
        self.assertEqual(squeezed(statements()), squeezed("PRAGMA foreign_keys = ON;\n" + documented[0]))

    def test_the_files_are_numbered_without_a_collision(self):
        names = sorted(path.name for path in MIGRATIONS.glob("*.sql"))
        self.assertEqual([name for name in names if name.startswith("020_")], [MIGRATION])
        self.assertEqual(names[names.index(MIGRATION) - 1], PREVIOUS)
        self.assertEqual([name[:3] for name in names], [f"{n:03d}" for n in range(len(names))])
        created = [name for name in names
                   if re.search(rf"CREATE TABLE (IF NOT EXISTS )?{TABLE}\b", (MIGRATIONS / name).read_text(encoding="utf-8"))]
        self.assertEqual(created, [MIGRATION])


class ConsumedTransactionsClassificationTest(unittest.TestCase):
    def test_the_table_is_operational_not_canonical_not_runtime_and_not_seeded(self):
        self.assertEqual(ct.TABLE_CLASSIFICATION[TABLE][0], ct.OPERATIONAL)
        self.assertIn(TABLE, ct.tables_of(ct.OPERATIONAL))
        self.assertNotIn(TABLE, ct.tables_of(ct.CANONICAL))
        self.assertNotIn(TABLE, ct.tables_of(ct.RUNTIME))
        self.assertNotIn(TABLE, ct.MIGRATION_SEEDED_ROWS)
        self.assertNotIn(TABLE, ct.NATURAL_KEYS)
        self.assertFalse((ROOT / "data" / "canonical" / f"{TABLE}.jsonl").exists())

    def test_manifest_records_the_level_and_the_exclusion(self):
        manifest = json.loads((ROOT / "data" / "canonical" / "manifest.json").read_text(encoding="utf-8"))
        self.assertGreaterEqual(manifest["schema"]["migration_level"], MIGRATION)
        self.assertIn(MIGRATION, manifest["schema"]["migrations"])
        self.assertIn(TABLE, manifest["operational_exclusions"])
        self.assertIn(TABLE, manifest["classification"]["operational"])
        self.assertNotIn(TABLE, manifest["runtime_exclusions"])
        self.assertNotIn(TABLE, manifest["classification"]["runtime"])
        self.assertNotIn(TABLE, manifest["classification"]["canonical"])
        self.assertNotIn(TABLE, manifest["canonical_tables"])
        self.assertNotIn(TABLE, manifest["tables"])  # never exported to data/canonical
        self.assertEqual(export.check_directory(ct.CANONICAL_DIR), [])

    def test_a_fresh_canonical_build_has_the_table_and_it_is_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = build_test_database(Path(tmp) / "fresh.db")
            with closing(sqlite3.connect(path)) as conn:
                applied = [r[0] for r in conn.execute("SELECT filename FROM schema_migrations ORDER BY filename")]
                self.assertIn(MIGRATION, applied)
                self.assertEqual(tuple(r[1] for r in conn.execute(f"PRAGMA table_info({TABLE})")), COLUMNS)
                self.assertEqual(conn.execute(f"SELECT COUNT(*) FROM {TABLE}").fetchone()[0], 0)
                self.assertEqual(conn.execute("SELECT * FROM pragma_foreign_key_check").fetchall(), [])
                self.assertEqual(ct.classification_problems(conn), [])
            self.assertEqual(builder.validation_problems(path), [])

    def test_a_database_that_holds_a_consumption_record_is_not_a_canonical_build(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = build_test_database(Path(tmp) / "used.db")
            with closing(sqlite3.connect(path)) as conn:
                conn.execute(INSERT, (HEX, 600))
                conn.commit()
            self.assertIn(f"{TABLE}: 1 rows; a canonical rebuild must leave non-canonical tables empty",
                          builder.validation_problems(path))


class ConsumedTransactionsCanonicalExclusionTest(unittest.TestCase):
    """Consumption records of a working database never reach a canonical export."""

    MARKERS = ("c0ffee" * 10 + "c0ff", "decade" * 10 + "deca")

    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="oidc-consumed-export-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.path = build_test_database(self.root / "working.db")
        with closing(sqlite3.connect(self.path)) as conn:
            for marker in self.MARKERS:
                conn.execute(INSERT, (marker, 4242424242))
            conn.commit()

    def test_an_export_holds_the_canonical_tables_only_and_no_consumption_record(self):
        out = self.root / "export"
        with closing(export.open_snapshot(self.path)) as conn:
            self.assertEqual(conn.execute(f"SELECT COUNT(*) FROM {TABLE}").fetchone()[0], 2)
            manifest = export.export_database(conn, out)
        self.assertEqual(sorted(path.name for path in out.iterdir()),
                         sorted([f"{table}.jsonl" for table in ct.tables_of(ct.CANONICAL)] + [ct.MANIFEST_NAME]))
        self.assertNotIn(TABLE, manifest["tables"])
        self.assertNotIn(TABLE, manifest["canonical_tables"])
        self.assertIn(TABLE, manifest["operational_exclusions"])
        exported = "".join(path.read_text(encoding="utf-8") for path in out.iterdir())
        for marker in (*self.MARKERS, "4242424242"):
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
        self.assertEqual(manifest["canonical_tables"], tracked["canonical_tables"])
        for table in ct.tables_of(ct.CANONICAL):
            self.assertEqual((out / f"{table}.jsonl").read_bytes(),
                             (ct.CANONICAL_DIR / f"{table}.jsonl").read_bytes(), table)
        self.assertEqual(export.identity_problems(ct.CANONICAL_DIR, out), [])

    def test_an_export_that_included_the_records_would_be_refused(self):
        out = self.root / "export"
        with closing(export.open_snapshot(self.path)) as conn:
            export.export_database(conn, out)
        (out / f"{TABLE}.jsonl").write_text('{"tx_hash":"x"}\n', encoding="utf-8")
        self.assertIn(f"runtime/operational table {TABLE} is included in the export",
                      export.identity_problems(ct.CANONICAL_DIR, out))
        self.assertIn(f"unexpected file in canonical directory: {TABLE}.jsonl", export.check_directory(out))


if __name__ == "__main__":
    unittest.main()
