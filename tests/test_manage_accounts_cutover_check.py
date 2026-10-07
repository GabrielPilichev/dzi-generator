"""Accounts PR9 — ``manage_accounts cutover-check``, the read-only deployment preflight.

PASS needs one viable admin: active, with an email and a structurally valid password
hash, a confirmed TOTP credential that the configured ``DZI_TOTP_KEY`` decrypts, and an
unused recovery code. The command never writes, never consumes a TOTP step or recovery
code, and prints no secret, code or hash.

It inspects only a self-contained rollback-journal database without sidecar files (a
``src.backup_sqlite`` snapshot): a WAL-mode database is refused before SQLite opens it,
because even a read-only connection creates or rewrites ``-wal``/``-shm``. The temporary
databases here are built from migrations 012, 013 and 016 in SQLite's default
rollback-journal mode unless a test says otherwise; the key, secrets and codes are
synthetic and generated per run.
"""

import hashlib
import os
import shutil
import sqlite3
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import pyotp
from cryptography.fernet import Fernet
from werkzeug.security import check_password_hash, generate_password_hash

from src import backup_sqlite, manage_accounts
from tests.test_manage_accounts_totp import ADMIN_PW, CODE_LINE, URI_LINE, TotpCliTestCase
from web.services import accounts

ADMIN = "admin@example.bg"
NOW = datetime(2026, 3, 1, 9, 0, 0, tzinfo=timezone.utc)


class CutoverCheckTestCase(TotpCliTestCase):
    migrations = ("012_users.sql", "013_login_throttle.sql", "016_admin_totp.sql")

    def setUp(self):
        super().setUp()
        self.secrets = []

    def viable_admin(self, email=ADMIN):
        """An admin enrolled and confirmed through the real CLI; returns its recovery codes."""
        self.create(email)
        self.assertEqual(self.run_cli("totp-enroll", "--email", email), 0, self.err)
        secret = pyotp.parse_uri(URI_LINE.search(self.out).group(0)).secret
        self.assertEqual(self.run_cli("totp-confirm", "--email", email, "--code-stdin",
                                      stdin=pyotp.TOTP(secret).now() + "\n"), 0, self.err)
        codes = CODE_LINE.findall(self.out)
        self.secrets += [secret, *codes, *(code.replace("-", "") for code in codes)]
        return codes

    def check(self):
        code = self.run_cli("cutover-check")
        self.assertEqual(self.err, "")
        return code

    def assert_pass(self, *viable):
        self.assertEqual(self.check(), 0, self.out)
        self.assertEqual(self.out.splitlines()[0], "cutover-check: PASS")
        for email in viable:
            self.assertIn(email, self.out)

    def assert_fail(self, *reasons):
        self.assertEqual(self.check(), 1, self.out)
        self.assertEqual(self.out.splitlines()[0], "cutover-check: FAIL")
        self.assertNotIn("PASS", self.out)
        for reason in reasons:
            self.assertIn(reason, self.out)

    def execute(self, sql, params=()):
        conn = sqlite3.connect(self.db)
        try:
            conn.execute(sql, params)
            conn.commit()
        finally:
            conn.close()


    def files(self):
        """Every file beside the database, with its bytes: sidecars included."""
        return {path.name: path.read_bytes() for path in sorted(self.db.parent.iterdir())}


class CutoverCheckPassTest(CutoverCheckTestCase):
    def test_viable_admin_passes(self):
        self.viable_admin()
        self.assert_pass(ADMIN)

    def test_one_viable_admin_is_enough(self):
        self.viable_admin()
        self.create("unenrolled@example.bg")          # an admin without TOTP
        self.create("disabled@example.bg")
        self.assertEqual(self.run_cli("disable", "--email", "disabled@example.bg"), 0, self.err)
        self.viable_admin("spent@example.bg")
        self.execute("UPDATE totp_recovery_codes SET used_at = CURRENT_TIMESTAMP WHERE user_id = "
                     "(SELECT id FROM users WHERE email = 'spent@example.bg')")
        self.assert_pass(ADMIN)
        for other in ("unenrolled@example.bg", "disabled@example.bg", "spent@example.bg"):
            self.assertNotIn(other, self.out)

    def test_the_removed_shared_login_switch_is_not_part_of_the_check(self):
        # There is no bridge to switch: a leftover variable of any spelling is ignored.
        self.viable_admin()
        for value in ("0", "false", "", "1", "true", "on", "maybe"):
            with self.subTest(value=value), mock.patch.dict(os.environ, {"DZI_LEGACY_SHARED_LOGIN": value}):
                self.assert_pass(ADMIN)
                self.assertNotIn("DZI_LEGACY_SHARED_LOGIN", self.out)


class CutoverCheckFailTest(CutoverCheckTestCase):
    def test_no_admin_account(self):
        self.assert_fail("no active admin account")
        self.create("teacher@example.bg", role="teacher")
        self.assert_fail("no active admin account")

    def test_disabled_admin(self):
        self.viable_admin()
        self.assertEqual(self.run_cli("disable", "--email", ADMIN), 0, self.err)
        self.assert_fail("no active admin account (1 disabled; run enable)")

    def test_admin_without_a_password(self):
        self.viable_admin()
        self.execute("UPDATE users SET password_hash = NULL")
        self.assert_fail(f"admin {ADMIN} has no password (run set-password)")

    def test_admin_without_an_email_cannot_sign_in(self):
        self.viable_admin()
        self.execute("UPDATE users SET email = NULL, password_hash = NULL")
        self.assert_fail("has no email to sign in with")

    def test_no_totp_or_unconfirmed_totp(self):
        self.create()
        self.assert_fail(f"admin {ADMIN} has no TOTP credential")
        self.assertEqual(self.run_cli("totp-enroll", "--email", ADMIN), 0, self.err)
        self.assert_fail(f"admin {ADMIN} has an unconfirmed TOTP enrolment (run totp-confirm)")

    def test_missing_totp_key(self):
        self.viable_admin()
        for env in ({}, {"DZI_TOTP_KEY": ""}):
            with self.subTest(env=env), mock.patch.dict(os.environ, env):
                if not env:
                    del os.environ["DZI_TOTP_KEY"]
                self.assert_fail("DZI_TOTP_KEY is not set")

    def test_malformed_totp_key(self):
        self.viable_admin()
        with mock.patch.dict(os.environ, {"DZI_TOTP_KEY": "not-a-fernet-key"}):
            self.assert_fail("DZI_TOTP_KEY must be a Fernet key")
            self.assertNotIn("not-a-fernet-key", self.out)

    def test_wrong_but_well_formed_key_cannot_decrypt(self):
        self.viable_admin()
        other = Fernet.generate_key().decode()
        with mock.patch.dict(os.environ, {"DZI_TOTP_KEY": other}):
            self.assert_fail(f"admin {ADMIN} has a TOTP secret that DZI_TOTP_KEY cannot decrypt",
                             "no admin account can complete an account sign-in")
            self.assertNotIn(other, self.out)

    def test_corrupt_stored_secret(self):
        self.viable_admin()
        self.execute("UPDATE totp_credentials SET secret_ciphertext = 'garbage'")
        self.assert_fail("has a TOTP secret that DZI_TOTP_KEY cannot decrypt")

    def test_no_unused_recovery_code(self):
        self.viable_admin()
        self.execute("UPDATE totp_recovery_codes SET used_at = CURRENT_TIMESTAMP")
        self.assert_fail(f"admin {ADMIN} has no unused recovery code (run totp-recovery-codes)")
        self.execute("DELETE FROM totp_recovery_codes")
        self.assert_fail("has no unused recovery code")

    def test_missing_account_tables(self):
        for migrations, missing in (
            (("012_users.sql",), "login_throttle, totp_credentials, totp_recovery_codes"),
            (("012_users.sql", "016_admin_totp.sql"), "login_throttle"),
        ):
            with self.subTest(migrations=migrations):
                self.db.unlink()
                conn = sqlite3.connect(self.db)
                for name in migrations:
                    conn.executescript((manage_accounts.Path(__file__).resolve().parents[1]
                                        / "web" / "migrations" / name).read_text(encoding="utf-8"))
                conn.close()
                self.assert_fail(f"database is missing account tables: {missing}")

    def test_database_without_users_or_missing_file_is_an_operator_error(self):
        self.db.unlink()
        sqlite3.connect(self.db).close()
        self.assertEqual(self.run_cli("cutover-check"), 1)
        self.assertIn("migration 012 required", self.err)
        self.db.unlink()
        self.assertEqual(self.run_cli("cutover-check"), 1)
        self.assertIn("database not found", self.err)
        self.assertFalse(self.db.exists())  # never created


class CutoverCheckReadOnlyTest(CutoverCheckTestCase):
    STATE_SQL = (
        "SELECT id, email, role, is_active, password_hash, session_key, password_changed_at, "
        "last_login_at, updated_at FROM users ORDER BY id",
        "SELECT user_id, secret_ciphertext, confirmed_at, last_used_step, last_verified_at, "
        "updated_at FROM totp_credentials ORDER BY user_id",
        "SELECT id, user_id, code_hash, used_at FROM totp_recovery_codes ORDER BY id",
        "SELECT * FROM login_throttle",
    )

    def state(self):
        return [self.query(sql) for sql in self.STATE_SQL]

    def digest(self):
        return hashlib.sha256(self.db.read_bytes()).hexdigest()

    def test_pass_and_fail_leave_the_database_byte_identical(self):
        self.viable_admin()
        self.create("unenrolled@example.bg")
        before, digest, mtime = self.state(), self.digest(), self.db.stat().st_mtime_ns
        self.assert_pass(ADMIN)
        with mock.patch.dict(os.environ, {"DZI_TOTP_KEY": Fernet.generate_key().decode()}):
            self.assert_fail("cannot decrypt")
        with mock.patch.dict(os.environ, {"DZI_TOTP_KEY": "broken"}):
            self.assert_fail("must be a Fernet key")
        self.assertEqual(self.state(), before)
        self.assertEqual(self.digest(), digest)
        self.assertEqual(self.db.stat().st_mtime_ns, mtime)
        self.assertEqual([path.name for path in self.db.parent.iterdir()], [self.db.name])

    def test_totp_step_and_recovery_codes_are_not_consumed(self):
        codes = self.viable_admin()
        step = self.query("SELECT last_used_step, last_verified_at FROM totp_credentials")
        for _ in range(3):
            self.assert_pass(ADMIN)
        self.assertEqual(self.query("SELECT last_used_step, last_verified_at FROM totp_credentials"), step)
        self.assertEqual(self.query("SELECT COUNT(*) FROM totp_recovery_codes WHERE used_at IS NULL"),
                         [(len(codes),)])
        self.assertEqual(len(codes), 10)

    def test_the_connection_cannot_write(self):
        self.viable_admin()
        conn = manage_accounts.open_database(self.db, read_only=True)
        try:
            for sql in ("UPDATE users SET is_active = 0",
                        "UPDATE totp_recovery_codes SET used_at = CURRENT_TIMESTAMP",
                        "DELETE FROM totp_credentials",
                        "CREATE TABLE probe (id INTEGER)"):
                with self.subTest(sql=sql), self.assertRaises(sqlite3.OperationalError):
                    conn.execute(sql)
        finally:
            conn.close()
        self.assertIn("cutover-check", manage_accounts.READ_ONLY_COMMANDS)

    def test_no_secret_code_key_or_hash_is_printed(self):
        self.viable_admin()
        self.create("unenrolled@example.bg")
        hashes = [row[0] for row in self.query("SELECT password_hash FROM users")]
        hashes += [row[0] for row in self.query("SELECT code_hash FROM totp_recovery_codes")]
        hashes += [row[0] for row in self.query("SELECT secret_ciphertext FROM totp_credentials")]
        hashes += [row[0] for row in self.query("SELECT session_key FROM users")]
        outputs = []
        self.assert_pass(ADMIN)
        outputs.append(self.out + self.err)
        self.execute("UPDATE totp_recovery_codes SET used_at = CURRENT_TIMESTAMP")
        self.assert_fail("no unused recovery code")
        outputs.append(self.out + self.err)
        with mock.patch.dict(os.environ, {"DZI_TOTP_KEY": Fernet.generate_key().decode()}):
            self.assert_fail("cannot decrypt")
            outputs.append(self.out + self.err)
        for output in outputs:
            for secret in (*self.secrets, *hashes, self.key, ADMIN_PW):
                self.assertNotIn(secret, output)
            self.assertIsNone(URI_LINE.search(output))
            self.assertIsNone(CODE_LINE.search(output))
            self.assertNotIn("scrypt", output)

    def test_other_commands_still_open_the_database_read_write(self):
        self.viable_admin()
        self.assertEqual(self.run_cli("sign-out", "--email", ADMIN), 0, self.err)
        self.assertTrue(accounts.ROLES)


SALT = "Zm9vYmFyc2FsdDEyMzQ1"
SCRYPT_DIGEST = "ab" * 64
SHA256_DIGEST = "cd" * 32
# What sign-in can verify: the repository's own scrypt hashes, and the older werkzeug
# forms it still accepts (and upgrades after a successful sign-in).
SUPPORTED_HASH_SHAPES = {
    "scrypt with parameters": f"scrypt:32768:8:1${SALT}${SCRYPT_DIGEST}",
    "scrypt with other parameters": f"scrypt:16384:8:2${SALT}${SCRYPT_DIGEST}",
    "scrypt with default parameters": f"scrypt${SALT}${SCRYPT_DIGEST}",
    "pbkdf2 sha256": f"pbkdf2:sha256:600000${SALT}${SHA256_DIGEST}",
    "pbkdf2 defaults": f"pbkdf2${SALT}${SHA256_DIGEST}",
    "pbkdf2 sha1": f"pbkdf2:sha1:1000${SALT}${'ef' * 20}",
}
MALFORMED_HASHES = {
    "not a hash": "broken-hash",
    "plain text": "correct horse battery",
    "unsupported method": f"argon2id:65536:3:4${SALT}${SCRYPT_DIGEST}",
    "bcrypt layout": "$2b$12$" + "a" * 53,
    "retired plain method": f"sha256${SALT}${SHA256_DIGEST}",
    "method in another case": f"SCRYPT:32768:8:1${SALT}${SCRYPT_DIGEST}",
    "empty method": f"${SALT}${SCRYPT_DIGEST}",
    "scrypt with two parameters": f"scrypt:32768:8${SALT}${SCRYPT_DIGEST}",
    "scrypt with four parameters": f"scrypt:32768:8:1:1${SALT}${SCRYPT_DIGEST}",
    "scrypt with a non-numeric parameter": f"scrypt:32768:x:1${SALT}${SCRYPT_DIGEST}",
    "scrypt with an empty parameter": f"scrypt:32768::1${SALT}${SCRYPT_DIGEST}",
    "scrypt with a negative parameter": f"scrypt:32768:-8:1${SALT}${SCRYPT_DIGEST}",
    "scrypt with a zero parameter": f"scrypt:32768:8:0${SALT}${SCRYPT_DIGEST}",
    "scrypt n not a power of two": f"scrypt:32767:8:1${SALT}${SCRYPT_DIGEST}",
    "scrypt n of one": f"scrypt:1:8:1${SALT}${SCRYPT_DIGEST}",
    "pbkdf2 with an unknown digest": f"pbkdf2:nosuchhash:1000${SALT}${SHA256_DIGEST}",
    "pbkdf2 with non-numeric iterations": f"pbkdf2:sha256:many${SALT}${SHA256_DIGEST}",
    "pbkdf2 with three parameters": f"pbkdf2:sha256:1000:1${SALT}${SHA256_DIGEST}",
    "missing salt": f"scrypt:32768:8:1$${SCRYPT_DIGEST}",
    "missing salt separator": f"scrypt:32768:8:1${SCRYPT_DIGEST}",
    "missing digest": f"scrypt:32768:8:1${SALT}$",
    "missing digest separator": f"scrypt:32768:8:1${SALT}",
    "method only": "scrypt:32768:8:1",
    "truncated digest": f"scrypt:32768:8:1${SALT}${SCRYPT_DIGEST[:-2]}",
    "overlong digest": f"scrypt:32768:8:1${SALT}${SCRYPT_DIGEST}ab",
    "digest of the wrong method length": f"scrypt:32768:8:1${SALT}${SHA256_DIGEST}",
    "digest that is not hex": f"scrypt:32768:8:1${SALT}${'zz' * 64}",
    "upper-case digest": f"scrypt:32768:8:1${SALT}${SCRYPT_DIGEST.upper()}",
    "extra field": f"scrypt:32768:8:1${SALT}${SCRYPT_DIGEST}$extra",
    "surrounding whitespace": f" scrypt:32768:8:1${SALT}${SCRYPT_DIGEST}\n",
}


class PasswordHashStructureTest(unittest.TestCase):
    """``accounts.password_hash_is_verifiable`` alone: no database, no hashing, no password."""

    def test_hashes_the_account_service_generates_are_verifiable(self):
        stored = generate_password_hash(ADMIN_PW, method=accounts.PASSWORD_METHOD)
        self.assertTrue(accounts.password_hash_is_verifiable(stored))
        self.assertTrue(stored.startswith("scrypt:"))
        self.assertEqual(len(stored.split("$")[2]), 128)
        # Older werkzeug methods are still verified by sign-in, so they are usable too.
        for method in ("pbkdf2", "pbkdf2:sha1:1000", "scrypt:16384:8:1"):
            with self.subTest(method=method):
                older = generate_password_hash(ADMIN_PW, method=method)
                self.assertTrue(check_password_hash(older, ADMIN_PW))
                self.assertTrue(accounts.password_hash_is_verifiable(older))

    def test_supported_shapes(self):
        for label, stored in SUPPORTED_HASH_SHAPES.items():
            with self.subTest(label):
                self.assertTrue(accounts.password_hash_is_verifiable(stored))

    def test_missing_and_malformed_hashes_are_not_verifiable(self):
        cases = {**MALFORMED_HASHES, "none": None, "empty": "", "bytes": b"scrypt", "number": 12345}
        for label, stored in cases.items():
            with self.subTest(label):
                self.assertIs(accounts.password_hash_is_verifiable(stored), False)

    def test_every_rejected_hash_really_cannot_sign_in(self):
        # The gate must not be stricter than sign-in about a hash that would work:
        # werkzeug itself refuses (or can never match) each malformed value.
        for label, stored in MALFORMED_HASHES.items():
            with self.subTest(label):
                try:
                    matched = check_password_hash(stored, ADMIN_PW)
                except (ValueError, TypeError):   # werkzeug/hashlib refuse the parameters
                    matched = False
                self.assertFalse(matched)

    def test_validation_computes_no_hash(self):
        stored = generate_password_hash(ADMIN_PW, method=accounts.PASSWORD_METHOD)
        with mock.patch("hashlib.scrypt", side_effect=AssertionError("hashed")), \
                mock.patch("hashlib.pbkdf2_hmac", side_effect=AssertionError("hashed")):
            self.assertTrue(accounts.password_hash_is_verifiable(stored))
            self.assertFalse(accounts.password_hash_is_verifiable("broken-hash"))


class CutoverCheckPasswordHashTest(CutoverCheckTestCase):
    def set_hash(self, stored):
        self.execute("UPDATE users SET password_hash = ?", (stored,))

    def stored_hash(self):
        return self.query("SELECT password_hash FROM users WHERE email = ?", (ADMIN,))[0][0]

    def test_repository_generated_hash_passes_and_still_authenticates(self):
        self.viable_admin()
        stored = self.stored_hash()
        self.assertTrue(stored.startswith("scrypt:32768:8:1$"))
        self.assert_pass(ADMIN)
        self.assertEqual(self.stored_hash(), stored)
        conn = sqlite3.connect(self.db)
        try:
            self.assertIsNotNone(accounts.authenticate(conn, ADMIN, ADMIN_PW))
        finally:
            conn.close()

    def test_broken_hash_fails_although_everything_else_is_viable(self):
        # The reviewed defect: any non-empty hash used to count as a password.
        self.viable_admin()
        self.set_hash("broken-hash")
        conn = sqlite3.connect(self.db)
        try:
            self.assertIsNone(accounts.authenticate(conn, ADMIN, ADMIN_PW))
            conn.rollback()
        finally:
            conn.close()
        self.assert_fail(
            f"admin {ADMIN} has a password hash that account sign-in cannot verify (run set-password)",
            "no admin account can complete an account sign-in")
        self.assertNotIn("broken-hash", self.out + self.err)
        self.assertEqual(self.stored_hash(), "broken-hash")

    def test_malformed_hashes_fail_without_being_printed_or_changed(self):
        self.viable_admin()
        for label, stored in MALFORMED_HASHES.items():
            with self.subTest(label):
                self.set_hash(stored)
                before = self.files()
                self.assertEqual(self.check(), 1, self.out)
                self.assertEqual(self.out.splitlines()[0], "cutover-check: FAIL")
                self.assertIn("has a password hash that account sign-in cannot verify (run set-password)",
                              self.out)
                self.assertNotIn(stored.strip(), self.out)
                for part in filter(None, stored.strip().split("$")):
                    if len(part) > 8:
                        self.assertNotIn(part, self.out)
                self.assertEqual(self.files(), before)
                self.assertEqual(self.stored_hash(), stored)

    def test_supported_shapes_pass_without_being_upgraded(self):
        self.viable_admin()
        for label, stored in SUPPORTED_HASH_SHAPES.items():
            with self.subTest(label):
                self.set_hash(stored)
                self.assert_pass(ADMIN)
                self.assertEqual(self.stored_hash(), stored)

    def test_set_password_repairs_a_broken_hash(self):
        self.viable_admin()
        self.set_hash("broken-hash")
        self.assert_fail("run set-password")
        self.assertEqual(self.run_cli("set-password", "--email", ADMIN, "--password-stdin",
                                      stdin=ADMIN_PW + "\n"), 0, self.err)
        self.assert_pass(ADMIN)

    def test_a_healthy_admin_outweighs_one_with_a_broken_hash(self):
        self.viable_admin()
        self.viable_admin("second@example.bg")
        self.execute("UPDATE users SET password_hash = 'broken-hash' WHERE email = 'second@example.bg'")
        self.assert_pass(ADMIN)
        self.assertNotIn("second@example.bg", self.out)


class CutoverCheckSnapshotOnlyTest(CutoverCheckTestCase):
    """The preflight never opens a database it could not read without side effects."""

    def refused(self, *reasons):
        code = self.run_cli("cutover-check")
        self.assertEqual(code, 1, self.out + self.err)
        self.assertEqual(self.out, "")
        self.assertNotIn("PASS", self.err)
        for reason in ("did not open this file", "python3 -m src.backup_sqlite --db",
                       "--output <snapshot.db>",
                       "python3 -m src.manage_accounts --db <snapshot.db> cutover-check", *reasons):
            self.assertIn(reason, self.err)
        for word in ("corrupt", "damaged", "malformed"):
            self.assertNotIn(word, self.err)

    def snapshot(self, name="snapshot.db"):
        """A ``src.backup_sqlite`` snapshot of the database, in its own directory."""
        directory = self.db.parent.parent / f"{self.db.parent.name}-snapshots"
        directory.mkdir(exist_ok=True)
        self.addCleanup(shutil.rmtree, directory, ignore_errors=True)
        destination = directory / name
        backup_sqlite.backup_database(self.db, destination)
        return destination

    def check_snapshot(self, snapshot):
        before = {path.name: path.read_bytes() for path in snapshot.parent.iterdir()}
        mtime = snapshot.stat().st_mtime_ns
        out, err, code = self.run_snapshot(snapshot)
        self.assertEqual({path.name: path.read_bytes() for path in snapshot.parent.iterdir()}, before)
        self.assertEqual(snapshot.stat().st_mtime_ns, mtime)
        return out, err, code

    def run_snapshot(self, snapshot):
        original, self.db = self.db, snapshot
        try:
            code = self.run_cli("cutover-check")
        finally:
            self.db = original
        return self.out, self.err, code

    def journal_mode(self, value):
        conn = sqlite3.connect(self.db)
        try:
            self.assertEqual(conn.execute(f"PRAGMA journal_mode={value}").fetchone()[0], value.lower())
        finally:
            conn.close()

    def test_clean_closed_wal_database_is_refused_and_no_sidecar_is_created(self):
        # Reviewed case A: a read-only SQLite connection would create -wal and -shm here.
        self.viable_admin()
        self.journal_mode("WAL")
        before = self.files()
        self.assertEqual(sorted(before), [self.db.name])
        mtime = self.db.stat().st_mtime_ns
        self.refused("is a WAL-mode database")
        self.assertEqual(self.files(), before)
        self.assertEqual(self.db.stat().st_mtime_ns, mtime)

    def test_the_refusal_happens_before_sqlite_opens_the_file(self):
        self.viable_admin()
        self.journal_mode("WAL")
        with mock.patch.object(manage_accounts.sqlite3, "connect",
                               side_effect=AssertionError("opened")) as connect:
            self.refused("is a WAL-mode database")
        connect.assert_not_called()

    def test_live_wal_database_is_refused_and_its_sidecars_are_untouched(self):
        # Reviewed case B: another connection is open and the admin exists only in the WAL.
        holder = sqlite3.connect(self.db)
        self.addCleanup(holder.close)
        self.assertEqual(holder.execute("PRAGMA journal_mode=WAL").fetchone()[0], "wal")
        holder.execute("PRAGMA wal_autocheckpoint=0")
        holder.execute("CREATE TABLE holder_marker (id INTEGER)")
        holder.commit()
        self.viable_admin()   # committed by other connections; never checkpointed
        before = self.files()
        self.assertEqual(sorted(before), [self.db.name, f"{self.db.name}-shm", f"{self.db.name}-wal"])
        self.assertGreater(len(before[f"{self.db.name}-wal"]), 0)

        self.refused("has SQLite sidecar files (-wal, -shm)")
        self.assertEqual(self.files(), before)

        # The main file alone is stale: read without its WAL it has no admin at all,
        # which is what an immutable open would have reported on.
        stale = sqlite3.connect(f"{self.db.resolve().as_uri()}?immutable=1", uri=True)
        try:
            self.assertEqual(stale.execute("SELECT COUNT(*) FROM users").fetchone()[0], 0)
        finally:
            stale.close()
        self.assertEqual(self.files(), before)

        # A snapshot holds the committed WAL content, and the check passes on it.
        snapshot = self.snapshot()
        out, err, code = self.check_snapshot(snapshot)
        self.assertEqual((code, err), (0, ""), out)
        self.assertEqual(out.splitlines()[0], "cutover-check: PASS")
        self.assertIn(ADMIN, out)
        copy = sqlite3.connect(f"{snapshot.resolve().as_uri()}?mode=ro", uri=True)
        try:
            self.assertEqual(copy.execute("SELECT email FROM users").fetchall(), [(ADMIN,)])
            self.assertEqual(copy.execute("PRAGMA journal_mode").fetchone()[0], "delete")
        finally:
            copy.close()

    def test_a_raw_copy_of_a_live_wal_main_file_is_refused_too(self):
        holder = sqlite3.connect(self.db)
        self.addCleanup(holder.close)
        holder.execute("PRAGMA journal_mode=WAL")
        holder.execute("PRAGMA wal_autocheckpoint=0")
        self.viable_admin()
        raw = self.db.parent.parent / f"{self.db.parent.name}-raw.db"
        raw.write_bytes(self.db.read_bytes())   # cp of the main file: misses the WAL
        self.addCleanup(raw.unlink)
        out, err, code = self.run_snapshot(raw)
        self.assertEqual((code, out), (1, ""))
        self.assertIn("is a WAL-mode database", err)
        self.assertFalse(Path(f"{raw}-wal").exists() or Path(f"{raw}-shm").exists())

    def test_snapshot_of_a_wal_database_passes_and_stays_byte_identical(self):
        self.viable_admin()
        self.journal_mode("WAL")
        snapshot = self.snapshot()
        self.assertEqual([path.name for path in snapshot.parent.iterdir()], [snapshot.name])
        for _ in range(2):
            out, err, code = self.check_snapshot(snapshot)
            self.assertEqual((code, err), (0, ""), out)
            self.assertEqual(out.splitlines()[0], "cutover-check: PASS")

    def test_a_snapshot_reports_the_state_at_the_time_it_was_taken(self):
        self.viable_admin()
        snapshot = self.snapshot("before.db")
        self.execute("UPDATE users SET password_hash = 'broken-hash'")
        self.assertEqual(self.check_snapshot(snapshot)[2], 0)
        out, err, code = self.check_snapshot(self.snapshot("after.db"))
        self.assertEqual(code, 1)
        self.assertIn("cannot verify", out)

    def test_rollback_journal_database_with_a_sidecar_is_refused(self):
        self.viable_admin()
        for suffix in ("-journal", "-wal", "-shm"):
            with self.subTest(suffix=suffix):
                sidecar = Path(f"{self.db}{suffix}")
                sidecar.write_bytes(b"left behind")
                try:
                    before = self.files()
                    self.refused(f"has SQLite sidecar files ({suffix})")
                    self.assertEqual(self.files(), before)
                finally:
                    sidecar.unlink()
        self.assert_pass(ADMIN)

    def test_a_file_that_is_not_a_database_is_an_operator_error(self):
        self.db.write_bytes(b"this is not an sqlite database, just some bytes")
        self.assertEqual(self.run_cli("cutover-check"), 1)
        self.assertIn("is not an SQLite database", self.err)
        self.assertEqual(self.out, "")

    def test_other_commands_still_work_on_a_wal_database(self):
        self.journal_mode("WAL")
        self.create()
        self.assertEqual(self.run_cli("list"), 0, self.err)
        self.assertIn(ADMIN, self.out)


if __name__ == "__main__":
    unittest.main()
