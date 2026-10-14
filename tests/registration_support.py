"""Shared fixture of the registration service tests (EMAIL_REGISTRATION PR 3).

Every database is a temporary clone of the migrated canonical template
(``tests.support.build_test_database``); none is ``data/questions.db``. Time is a value
the test sets (``self.wall``); nothing sleeps and nothing reads the real clock. Mail is
``InMemoryMailer`` or no mailer at all. The service never commits, so ``tx`` begins
``BEGIN IMMEDIATE`` and commits, or rolls back after an exception, as a caller must.
"""

import hashlib
import logging
import re
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from tests.support import build_test_database
from web.services import accounts, registration

T0 = datetime(2026, 10, 13, 12, 0, 0, tzinfo=timezone.utc)
KEY = "registration-test-key"
PASSWORD = "  pupil  passphrase  "
EMAIL = "ana@example.bg"
REGISTRATION_TABLES = ("pending_registrations", "registration_sends", "registration_control")
SENSITIVE_COLUMNS = ("email", "first_name", "last_name", "grade", "school_name", "subjects_json",
                     "password_hash", "code_hash", "code_expires_at")

_password_hash = None


def password_hash() -> str:
    """One scrypt hash of ``PASSWORD`` for the whole run; the service stores it as given."""
    global _password_hash
    if _password_hash is None:
        _password_hash = accounts.hash_new_password(PASSWORD, EMAIL)
    return _password_hash


def stamp(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def student(email=EMAIL, **overrides):
    values = {"account_type": "student", "first_name": "Ана", "last_name": "Иванова", "email": email,
              "grade": 9, "next": None}
    values.update(overrides)
    return values


def teacher(email="ivan@example.bg", **overrides):
    values = {"account_type": "teacher", "first_name": "Иван", "last_name": "Петров", "email": email,
              "school_name": "СУ „Васил Левски“, Пловдив",
              "subjects": [{"subject_id": "it", "label": "Информационни технологии"}], "next": None}
    values.update(overrides)
    return values


def code_of(result) -> str:
    """The six digits of an issued code message (a notice has none)."""
    found = re.findall(r"(?<!\d)\d{6}(?!\d)", result.message.text)
    assert len(found) == 1, "exactly one code in a code message"
    return found[0]


class RegistrationTestCase(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="registration-")
        self.addCleanup(directory.cleanup)
        self.path = build_test_database(Path(directory.name) / "registration.db")
        self.conn = self.connect()
        self.wall = T0
        self.sender_busy = False
        # Keeps the service's warnings off the test output; assertLogs still sees them.
        quiet = logging.NullHandler()
        logging.getLogger("learnpilot.registration").addHandler(quiet)
        self.addCleanup(logging.getLogger("learnpilot.registration").removeHandler, quiet)

    # ------------------------------------------------------------------ plumbing

    def connect(self, timeout=5.0):
        conn = sqlite3.connect(self.path, timeout=timeout)
        self.addCleanup(conn.close)
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA synchronous = OFF")  # a throwaway file: no need to wait for the disk
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

    # ------------------------------------------------------------------ the service

    def submit(self, email=EMAIL, *, cleaned=None, account_type=None, previous=None, conn=None, key=KEY,
               **kwargs):
        cleaned = cleaned or student(email)
        kwargs.setdefault("sender_busy", self.sender_busy)
        kwargs.setdefault("clock", self.clock)
        return self.tx(
            registration.create_pending, conn=conn, cleaned=cleaned,
            account_type=account_type or cleaned["account_type"], password_hash=password_hash(),
            previous_binding=previous, secret_key=key, **kwargs,
        )

    def binding(self, issued, key=KEY):
        return registration.binding_hash(key, issued.token)

    def verify(self, issued, code, *, conn=None, key=KEY, **kwargs):
        kwargs.setdefault("clock", self.clock)
        return self.tx(registration.verify, self.binding(issued, key), code, conn=conn, secret_key=key, **kwargs)

    def resend(self, issued, *, conn=None, key=KEY, **kwargs):
        kwargs.setdefault("sender_busy", self.sender_busy)
        kwargs.setdefault("clock", self.clock)
        return self.tx(registration.resend, self.binding(issued, key), conn=conn, secret_key=key, **kwargs)

    def cancel(self, issued, **kwargs):
        kwargs.setdefault("clock", self.clock)
        return self.tx(registration.cancel, self.binding(issued), **kwargs)

    def settle_raw(self, send_id, issuance_id, outcome, *, conn=None):
        """``settle_send`` in a transaction of its own; returns its ``Settlement``."""
        return self.tx(registration.settle_send, send_id, issuance_id, outcome, conn=conn, clock=self.clock)

    def settle(self, issued, outcome, *, conn=None):
        """Settle the issuance of a ``Result`` with its own identity; True when this call settled it.

        For a ledger row a test inserted itself (``add_send``) pass its id: the identity
        the row was given is read back, as the test is then its issuer.
        """
        if isinstance(issued, int):
            send_id = issued
            issuance_id = self.conn.execute(
                "SELECT issuance_id FROM registration_sends WHERE id = ?", (send_id,)).fetchone()[0]
        else:
            send_id, issuance_id = issued.send_id, issued.issuance_id
        return self.settle_raw(send_id, issuance_id, outcome, conn=conn) is registration.Settlement.SETTLED

    def cleanup(self):
        return self.tx(registration.cleanup, clock=self.clock)

    def dispatch(self, mailer, issued, *, conn=None, key=KEY):
        return registration.dispatch(conn or self.conn, mailer, issued, secret_key=key)

    def codes(self, *codes):
        """Issue exactly these codes, in order, for the rest of the test: nothing is left
        to the chance that two random six-digit codes differ."""
        patcher = patch.object(registration, "_new_code", side_effect=list(codes))
        self.addCleanup(patcher.stop)
        return patcher.start()

    # ------------------------------------------------------------------ reading

    def rows(self, sql, *params, conn=None):
        cursor = (conn or self.conn).execute(sql, params)
        columns = [col[0] for col in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]

    def pending(self, pending_id):
        return self.rows("SELECT * FROM pending_registrations WHERE id = ?", pending_id)[0]

    def send(self, send_id):
        return self.rows("SELECT * FROM registration_sends WHERE id = ?", send_id)[0]

    def sends(self):
        return self.rows("SELECT * FROM registration_sends ORDER BY id")

    def control(self):
        found = self.rows("SELECT * FROM registration_control")
        return found[0] if found else None

    def users(self):
        return self.rows("SELECT * FROM users ORDER BY id")

    def count(self, table):
        return self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    def digest(self, tables=REGISTRATION_TABLES + ("users", "user_profiles", "teacher_subjects")):
        """A content hash of the named tables: equal before and after means nothing was written."""
        sha = hashlib.sha256()
        for table in tables:
            for row in self.conn.execute(f"SELECT * FROM {table} ORDER BY 1, 2"):
                sha.update(repr((table, tuple(row))).encode("utf-8"))
        return sha.hexdigest()

    def assertScrubbed(self, pending_id, state):
        row = self.pending(pending_id)
        self.assertEqual(row["state"], state)
        for column in SENSITIVE_COLUMNS:
            self.assertIsNone(row[column], column)
        self.assertIsNotNone(row["closed_at"])

    def assertUnchanged(self, before):
        self.assertEqual(self.digest(), before)
        self.assertFalse(self.conn.in_transaction)

    # ------------------------------------------------------------------ direct state

    def set_control(self, *, high_water=None, hold=None):
        self.conn.execute(
            "INSERT OR REPLACE INTO registration_control (id, high_water_at, hold_reason, hold_set_at) "
            "VALUES (1, ?, ?, ?)",
            (stamp(high_water or self.wall), hold, stamp(self.wall) if hold else None),
        )
        self.conn.commit()

    def add_send(self, outcome="started", *, seconds=None, address=None, kind="code"):
        moment = self.wall if seconds is None else T0 + timedelta(seconds=seconds)
        send_id = self.conn.execute(
            "INSERT INTO registration_sends (email_hash, kind, attempted_at, outcome, issuance_id) "
            "VALUES (?, ?, ?, ?, ?)",
            (address or registration.email_hash(KEY, "ledger@example.bg"), kind, stamp(moment), outcome,
             registration._new_issuance_id()),
        ).lastrowid
        self.conn.commit()
        return send_id
