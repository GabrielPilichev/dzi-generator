"""Shared fixture of the password-recovery service tests (scope 8.2).

Every database is a temporary clone of the migrated canonical template
(``tests.support.build_test_database``); none is ``data/questions.db``. Time is a value the
test sets (``self.wall``); nothing sleeps and nothing reads the real clock. Mail is
``InMemoryMailer``; no socket is opened. The service never commits, so ``tx`` begins
``BEGIN IMMEDIATE`` and commits, or rolls back after an exception, as a caller must.

Accounts are built through the real account functions. A verified account is created the
way registration creates one (``accounts.create_verified_user``, which sets
``email_verified_at``); a CLI account through ``accounts.create_user`` (never verified).
"""

import hashlib
import logging
import re
import sqlite3
import tempfile
import unittest
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from cryptography.fernet import Fernet

from tests.registration_support import T0, stamp
from tests.support import build_test_database
from web.services import accounts, login_throttle, recovery, registration
from web.services.mail import InMemoryMailer, SendOutcome

KEY = "recovery-test-key"
TOTP_KEY = Fernet.generate_key().decode("ascii")  # a throwaway key for this run
OLD_PASSWORD = "old password 1234"
NEW_PASSWORD = "  new  recovery\tpassphrase  "
EMAIL = "ana@example.bg"
RECOVERY_TABLES = ("password_recoveries", "registration_sends", "registration_control")
ACCOUNT_TABLES = ("users", "login_throttle", "totp_credentials", "totp_recovery_codes", "user_identities",
                  "pending_registrations", "user_profiles")
SCRUBBED = ("email", "user_id", "session_key_digest", "code_hash", "code_expires_at")

_hashes: dict[str, str] = {}


def password_hash_for(email: str, password: str = OLD_PASSWORD) -> str:
    """One scrypt hash per address for the whole run, as registration stores it."""
    if (email, password) not in _hashes:
        _hashes[(email, password)] = accounts.hash_new_password(password, email)
    return _hashes[(email, password)]


def code_of(result) -> str:
    """The eight digits of an issued code message (a notice has none)."""
    found = re.findall(r"(?<!\d)\d{8}(?!\d)", result.message.text)
    assert len(found) == 1, "exactly one code in a code message"
    return found[0]


class RecoveryServiceTestCase(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="recovery-8-2-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.path = build_test_database(self.root / "recovery.db")
        self.conn = self.connect()
        self.wall = T0
        self.sender_busy = False
        for name in ("learnpilot.recovery", "learnpilot.registration"):
            quiet = logging.NullHandler()
            logging.getLogger(name).addHandler(quiet)
            self.addCleanup(logging.getLogger(name).removeHandler, quiet)

    # ------------------------------------------------------------------ plumbing

    def connect(self, timeout=5.0):
        conn = sqlite3.connect(self.path, timeout=timeout)
        self.addCleanup(conn.close)
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA synchronous = OFF")
        return conn

    def clock(self):
        return self.wall

    def at(self, seconds):
        self.wall = T0 + timedelta(seconds=seconds)
        return self.wall

    def tx(self, function, *args, conn=None, **kwargs):
        conn = conn or self.conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            result = function(conn, *args, **kwargs)
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        return result

    # ------------------------------------------------------------------ accounts

    def verified(self, email=EMAIL, role="student", password=OLD_PASSWORD):
        """An account as registration activates it (``email_verified_at`` set)."""
        self.conn.execute("BEGIN IMMEDIATE")
        user_id = accounts.create_verified_user(
            self.conn, email=email, display_name="Ана Иванова", role=role,
            password_hash=password_hash_for(email, password), verified_at=T0 - timedelta(days=1))
        self.conn.commit()
        assert user_id is not None
        return user_id

    def cli_account(self, email, role="student", password=OLD_PASSWORD, **kwargs):
        user_id = accounts.create_user(self.conn, display_name="Иван Петров", email=email,
                                       password=password, role=role, **kwargs)
        self.conn.commit()
        return user_id

    def run_account(self, function, *args):
        """An account function in its own committed transaction (operator or route path)."""
        return self.tx(lambda conn: function(conn, *args))

    def user(self, user_id):
        return self.rows("SELECT * FROM users WHERE id = ?", user_id)[0]

    # ------------------------------------------------------------------ the service

    def request(self, email=EMAIL, *, previous=None, conn=None, key=KEY, **kwargs):
        kwargs.setdefault("sender_busy", self.sender_busy)
        kwargs.setdefault("clock", self.clock)
        return self.tx(recovery.request, conn=conn, email=email, previous_binding=previous, secret_key=key,
                       **kwargs)

    def binding(self, issued, key=KEY):
        return recovery.binding_hash(key, issued.token)

    def resend(self, issued, *, conn=None, key=KEY, **kwargs):
        kwargs.setdefault("sender_busy", self.sender_busy)
        kwargs.setdefault("clock", self.clock)
        return self.tx(recovery.resend, self.binding(issued, key), conn=conn, secret_key=key, **kwargs)

    def redeem(self, issued, code, password=NEW_PASSWORD, *, conn=None, key=KEY, **kwargs):
        kwargs.setdefault("clock", self.clock)
        return self.tx(recovery.redeem, self.binding(issued, key), code, password, conn=conn, secret_key=key,
                       **kwargs)

    def cancel(self, issued, *, conn=None, **kwargs):
        kwargs.setdefault("clock", self.clock)
        return self.tx(recovery.cancel, self.binding(issued), conn=conn, **kwargs)

    def view(self, issued, *, conn=None):
        return recovery.pending_view(conn or self.conn, self.binding(issued), self.wall)

    def dispatch(self, mailer, issued, *, conn=None, key=KEY):
        return recovery.dispatch(conn or self.conn, mailer, issued, secret_key=key)

    def settle(self, issued, outcome, *, conn=None):
        return self.tx(recovery.settle_send, issued.send_id, issued.issuance_id, outcome, conn=conn,
                       clock=self.clock)

    def deliver(self, issued, outcome=SendOutcome.ACCEPTED):
        """Commit-then-dispatch-then-settle, as the 8.3 caller will: one mailer call."""
        mailer = InMemoryMailer(outcome)
        sent = self.dispatch(mailer, issued)
        self.settle(issued, sent)
        return mailer

    def codes(self, *codes):
        """Issue exactly these codes, in order, for the rest of the test."""
        patcher = patch.object(recovery, "_new_code", side_effect=list(codes))
        self.addCleanup(patcher.stop)
        return patcher.start()

    # ------------------------------------------------------------------ reading

    def rows(self, sql, *params, conn=None):
        cursor = (conn or self.conn).execute(sql, params)
        columns = [col[0] for col in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]

    def row(self, recovery_id):
        return self.rows("SELECT * FROM password_recoveries WHERE id = ?", recovery_id)[0]

    def send(self, send_id):
        return self.rows("SELECT * FROM registration_sends WHERE id = ?", send_id)[0]

    def sends(self):
        return self.rows("SELECT * FROM registration_sends ORDER BY id")

    def control(self):
        found = self.rows("SELECT * FROM registration_control")
        return found[0] if found else None

    def count(self, table, where="1"):
        return self.conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}").fetchone()[0]

    def digest(self, tables=RECOVERY_TABLES + ACCOUNT_TABLES):
        """A content hash of the named tables: equal before and after means nothing was written."""
        sha = hashlib.sha256()
        for table in tables:
            for row in self.conn.execute(f"SELECT * FROM {table} ORDER BY 1, 2"):
                sha.update(repr((table, tuple(row))).encode("utf-8"))
        return sha.hexdigest()

    def table_rows(self, table, where="1", *params):
        return [tuple(row) for row in self.conn.execute(f"SELECT * FROM {table} WHERE {where} ORDER BY 1",
                                                        params)]

    def assertScrubbed(self, recovery_id, state):
        row = self.row(recovery_id)
        self.assertEqual(row["state"], state)
        for column in SCRUBBED:
            self.assertIsNone(row[column], column)
        self.assertIsNotNone(row["closed_at"])

    def assertUnchanged(self, before):
        self.assertEqual(self.digest(), before)
        self.assertFalse(self.conn.in_transaction)

    def authenticates(self, email, password):
        conn = self.connect()
        user = accounts.authenticate(conn, email, password)
        conn.rollback()
        return user is not None

    # ------------------------------------------------------------------ direct state

    def set_control(self, *, high_water=None, hold=None):
        self.conn.execute(
            "INSERT OR REPLACE INTO registration_control (id, high_water_at, hold_reason, hold_set_at) "
            "VALUES (1, ?, ?, ?)",
            (stamp(high_water or self.wall), hold, stamp(self.wall) if hold else None),
        )
        self.conn.commit()

    def add_send(self, *, seconds=None, email="ledger@example.bg", kind="code", outcome="started", key=KEY):
        """A purpose-less ledger row (both ids NULL), as the cap fixtures insert them."""
        moment = self.wall if seconds is None else T0 + timedelta(seconds=seconds)
        send_id = self.conn.execute(
            "INSERT INTO registration_sends (email_hash, kind, attempted_at, outcome, issuance_id) "
            "VALUES (?, ?, ?, ?, ?)",
            (registration.email_hash(key, email), kind, stamp(moment), outcome, registration._new_issuance_id()),
        ).lastrowid
        self.conn.commit()
        return send_id

    def throttle_key(self, email):
        return login_throttle.identifier_hash(KEY, accounts.throttle_identifier(email))

    def fail_sign_in(self, email, times=1):
        """What ``/login`` does on a failed password: one throttle failure each."""
        for _ in range(times):
            self.tx(lambda conn: login_throttle.record_failure(conn, self.throttle_key(email), self.wall))

    def sign_in(self, email, password):
        """What ``/login`` does on a correct password: authenticate, clear the throttle."""
        def run(conn):
            if login_throttle.is_blocked(conn, self.throttle_key(email), self.wall):
                return "blocked"
            user = accounts.authenticate(conn, email, password)
            if user is None:
                login_throttle.record_failure(conn, self.throttle_key(email), self.wall)
                return "wrong"
            login_throttle.clear(conn, self.throttle_key(email))
            accounts.record_login(conn, user["id"])
            return "ok"
        return self.tx(run)

    # ------------------------------------------------------------------ instrumentation

    @contextmanager
    def statements(self, conn=None):
        """Every SQL statement ``conn`` runs inside the block, in order."""
        conn = conn or self.conn
        seen: list[str] = []
        conn.set_trace_callback(seen.append)
        try:
            yield seen
        finally:
            conn.set_trace_callback(None)

    @contextmanager
    def scrypt_calls(self):
        """Counts every password hash made (``accounts`` hashes through werkzeug)."""
        counter = {"hashes": 0}
        original = accounts.generate_password_hash

        def counting(*args, **kwargs):
            counter["hashes"] += 1
            return original(*args, **kwargs)

        with patch.object(accounts, "generate_password_hash", counting):
            yield counter


def normalized(statements):
    """Statements with literal values removed, for sequence comparisons."""
    return [re.sub(r"'[^']*'|\b\d+\b|\bNULL\b", "?", sql) for sql in statements]
