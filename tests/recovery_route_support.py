"""Shared fixture of the password-recovery route tests (scope 8.3).

Every test has its own temporary database built from migrations and canonical data
(``tests.support.build_test_database``); none is ``data/questions.db``. Mail is
``InMemoryMailer``: nothing is sent and no socket is opened. One injected clock
(``self.wall``, which is ``self.now`` of the account fixture) drives the recovery routes and
``/login`` alike; nothing sleeps for a limit and nothing reads the real clock. Commit failures
are real exceptions raised in place of ``COMMIT`` on the request's own connection, which the code
under test then rolls back.

Accounts are built through the real account functions: a verified account the way registration
creates one (``accounts.create_verified_user``, which sets ``email_verified_at``), the others
the way the operator's CLI does (``accounts.create_user``, never verified).
"""

import hashlib
import io
import logging
import os
import re
import sqlite3
import tempfile
from contextlib import closing, redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock
from unittest.mock import patch

from src import manage_accounts
from tests.recovery_service_support import OLD_PASSWORD, password_hash_for
from tests.support import build_test_database, web_app
from tests.test_account_login import SAME_ORIGIN, AccountAppTestCase
from web.blueprints import recovery as recovery_blueprint
from web.recovery_settings import CONFIG_KEY, RecoverySettings
from web.services import accounts
from web.services import registration as registration_service
from web.services.mail import InMemoryMailer

T0 = datetime(2026, 10, 21, 10, 0, 0, tzinfo=timezone.utc)
EMAIL = "ana.ivanova@example.bg"
NEW_PASSWORD = "  new  recovery\tpassphrase  "  # exact: spaces and a tab are part of it
CROSS_ORIGIN = {"Origin": "http://evil.example"}
ROUTES = ("/recover", "/recover/verify", "/recover/resend", "/recover/cancel")
RECOVERY_TABLES = ("password_recoveries", "registration_sends", "registration_control")
ACCOUNT_TABLES = ("users", "login_throttle", "totp_credentials", "totp_recovery_codes", "user_identities",
                  "pending_registrations", "user_profiles")
SESSION_ACCOUNT_KEYS = ("uid", "sk", "iat", "mfa_uid", "mfa_sk", "mfa_iat")


class FlakyCommits:
    """The request's real connection, except that the commits whose ordinal (1-based, counted
    over the whole test) is in ``fail`` raise instead of committing. The transaction is then
    still open, as after a real failed ``COMMIT``. ``commits_then_raise`` ordinals commit for
    real and then raise, which is what a commit looks like when it took effect but reported an
    error. ``fail_rollback`` makes every rollback raise."""

    def __init__(self, conn, state):
        self._conn, self._state = conn, state

    def commit(self):
        self._state["commits"] += 1
        ordinal = self._state["commits"]
        if ordinal in self._state["fail"]:
            raise sqlite3.OperationalError("disk I/O error (injected at COMMIT)")
        result = self._conn.commit()
        if ordinal in self._state["commits_then_raise"]:
            raise sqlite3.OperationalError("disk I/O error (injected after COMMIT)")
        return result

    def execute(self, sql, *args, **kwargs):
        """Raises for a statement that contains one of ``fail_sql`` (a fault injected at that statement)."""
        for fragment in self._state["fail_sql"]:
            if fragment in sql:
                self._state["failed_sql"].append(fragment)
                raise sqlite3.OperationalError("disk I/O error (injected at a statement)")
        return self._conn.execute(sql, *args, **kwargs)

    def rollback(self):
        self._state["rollbacks"] += 1
        if self._state["fail_rollback"]:
            raise sqlite3.OperationalError("disk I/O error (injected at ROLLBACK)")
        return self._conn.rollback()

    def __getattr__(self, name):
        return getattr(self._conn, name)


class RecoveryRouteTestCase(AccountAppTestCase):
    """A fresh database per test, recovery enabled with an in-memory mailer."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="recovery-routes-")
        self.addCleanup(directory.cleanup)
        self.db_path = str(build_test_database(Path(directory.name) / "recovery.db"))
        self.app.config["DB_PATH"] = self.db_path
        web_app.DB_PATH = web_app.QUIZ_DB_PATH = Path(self.db_path)  # restored by preserved_app_state
        super().setUp()
        self.mailer = InMemoryMailer()
        self.app.config[CONFIG_KEY] = RecoverySettings(mailer=self.mailer)
        self.now = T0
        clock = patch.object(recovery_blueprint, "_utcnow", lambda: self.now)
        clock.start()
        self.addCleanup(clock.stop)
        self.log = logging.getLogger("learnpilot.recovery")
        for name in ("learnpilot.recovery", "learnpilot.registration"):
            quiet = logging.NullHandler()
            logging.getLogger(name).addHandler(quiet)
            self.addCleanup(logging.getLogger(name).removeHandler, quiet)

    # ------------------------------------------------------------------ clock

    @property
    def wall(self):
        return self.now

    @wall.setter
    def wall(self, value):
        self.now = value

    def at(self, seconds):
        self.now = T0 + timedelta(seconds=seconds)

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)

    # ------------------------------------------------------------------ accounts

    def verified(self, email=EMAIL, role="student", password=OLD_PASSWORD, display_name="Ана Иванова"):
        """An account as registration activates it (``email_verified_at`` set); returns it as ``make`` does."""
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute("BEGIN IMMEDIATE")
            user_id = accounts.create_verified_user(
                conn, email=email, display_name=display_name, role=role,
                password_hash=password_hash_for(email, password), verified_at=T0 - timedelta(days=1))
            conn.commit()
            session_key = conn.execute("SELECT session_key FROM users WHERE id = ?", (user_id,)).fetchone()[0]
        return {"id": user_id, "session_key": session_key, "email": email, "role": role}

    def cli_account(self, email, role="student", password=OLD_PASSWORD):
        """An account the operator's CLI creates: a password, never verified."""
        return self.make(role, email=email, password=password)

    def cli(self, *args, stdin=None):
        """An operator command (``src.manage_accounts``) against this test's database."""
        out, err = io.StringIO(), io.StringIO()
        env = {"DZI_SECRET_KEY": self.app.config["SECRET_KEY"]}
        with mock.patch.dict(os.environ, env), redirect_stdout(out), redirect_stderr(err):
            if stdin is not None:
                with mock.patch("sys.stdin", io.StringIO(stdin)):
                    status = manage_accounts.main(["--db", self.db_path, *args])
            else:
                status = manage_accounts.main(["--db", self.db_path, *args])
        self.assertEqual(status, 0, out.getvalue() + err.getvalue())
        return out.getvalue()

    # ------------------------------------------------------------------ requests

    def begin(self, email=EMAIL, *, client=None, headers=SAME_ORIGIN, **extra):
        """``POST /recover``."""
        return (client or self.client).post("/recover", data={"email": email, **extra}, headers=headers)

    def redeem(self, code, password=NEW_PASSWORD, confirmation=None, *, client=None, headers=SAME_ORIGIN, **extra):
        """``POST /recover/verify`` (the confirmation defaults to the password)."""
        data = {"code": code, "password": password,
                "confirm_password": password if confirmation is None else confirmation, **extra}
        return (client or self.client).post("/recover/verify", data=data, headers=headers)

    def resend(self, *, client=None, headers=SAME_ORIGIN):
        return (client or self.client).post("/recover/resend", headers=headers)

    def cancel(self, *, client=None, headers=SAME_ORIGIN):
        return (client or self.client).post("/recover/cancel", headers=headers)

    def last_code(self):
        """The eight digits of the last message sent (a notice has none)."""
        found = re.findall(r"(?<!\d)\d{8}(?!\d)", self.mailer.messages[-1].text)
        self.assertEqual(len(found), 1, "the last message is a code")
        return found[0]

    def recover(self, email=EMAIL, *, client=None, **extra):
        """Request a code and return the browser's client (the response is asserted a 303)."""
        response = self.begin(email, client=client, **extra)
        self.assertEqual(response.status_code, 303, self.body(response))
        return response

    # ------------------------------------------------------------------ state

    def rows(self, sql, *params):
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.row_factory = sqlite3.Row
            return [dict(row) for row in conn.execute(sql, params)]

    def recoveries(self):
        return self.rows("SELECT * FROM password_recoveries ORDER BY id")

    def sends(self):
        return self.rows("SELECT * FROM registration_sends ORDER BY id")

    def account(self, user_id):
        return self.rows("SELECT * FROM users WHERE id = ?", user_id)[0]

    def digest(self, tables=RECOVERY_TABLES + ACCOUNT_TABLES):
        """A content hash of the named tables: equal before and after means nothing was written."""
        sha = hashlib.sha256()
        with closing(sqlite3.connect(self.db_path)) as conn:
            for table in tables:
                for row in conn.execute(f"SELECT * FROM {table} ORDER BY 1, 2"):
                    sha.update(repr((table, tuple(row))).encode("utf-8"))
        return sha.hexdigest()

    def set_control(self, *, high_water=None, hold=None):
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO registration_control (id, high_water_at, hold_reason, hold_set_at) "
                "VALUES (1, ?, ?, ?)",
                (registration_service._stamp(high_water or self.now), hold,
                 registration_service._stamp(self.now) if hold else None))
            conn.commit()

    def control(self):
        found = self.rows("SELECT * FROM registration_control")
        return found[0] if found else None

    def slot(self, client=None):
        return self.session(client).get(recovery_blueprint.SLOT)

    def set_slot(self, value, client=None):
        with (client or self.client).session_transaction() as sess:
            sess[recovery_blueprint.SLOT] = value

    def authenticates(self, email, password):
        with closing(sqlite3.connect(self.db_path)) as conn:
            return accounts.authenticate(conn, email, password) is not None

    def flaky(self, *fail, commits_then_raise=(), fail_rollback=False, fail_sql=()):
        """Fail the commits of these ordinals on the request connections of the blueprint, and any
        statement containing one of ``fail_sql``."""
        state = {"commits": 0, "rollbacks": 0, "fail": set(fail), "commits_then_raise": set(commits_then_raise),
                 "fail_rollback": fail_rollback, "fail_sql": list(fail_sql), "failed_sql": []}
        real = recovery_blueprint.get_request_db
        self.lift_fault()
        patcher = patch.object(recovery_blueprint, "get_request_db", lambda: FlakyCommits(real(), state))
        patcher.start()
        self._fault = patcher
        self.addCleanup(self.lift_fault)
        return state

    def lift_fault(self):
        """Stop the injected fault, if one is active (it is also lifted at cleanup)."""
        patcher = getattr(self, "_fault", None)
        if patcher is not None:
            self._fault = None
            patcher.stop()

    def spy(self, target, name):
        """Record every call of ``target.name`` and pass it on."""
        calls = []
        real = getattr(target, name)

        def recorded(*args, **kwargs):
            calls.append((args, kwargs))
            return real(*args, **kwargs)

        patcher = patch.object(target, name, recorded)
        patcher.start()
        self.addCleanup(patcher.stop)
        return calls

    def observe(self, response, *addresses):
        """What a browser can see of a response, with the typed addresses taken out: the status, the
        ``Location``, the page, the names of the headers and the shape of the cookie."""
        page = self.body(response)
        for address in addresses:
            page = page.replace(address, "ADDRESS")
        page = re.sub(r"next=[^\"&]*", "next=", page)
        cookies = tuple(sorted(
            tuple(sorted(part.strip().split("=")[0].lower() for part in header.split(";")[1:]))
            for header in response.headers.getlist("Set-Cookie")))
        return (response.status_code, response.headers.get("Location"), page,
                tuple(sorted(name for name, _ in response.headers if name.lower() not in ("content-length", "date"))),
                cookies)

    def session_shape(self, client=None):
        """The structure of a browser's session: the keys, and for the slot the key set, the version, the token length."""
        state = self.session(client)
        slot = state.get(recovery_blueprint.SLOT)
        return (tuple(sorted(state)),
                None if slot is None else (tuple(sorted(slot)), slot["v"], len(slot["t"]), slot["next"]))

    def assertNoAccountSession(self, client=None):
        state = self.session(client)
        for key in SESSION_ACCOUNT_KEYS:
            self.assertNotIn(key, state)

    def assertCookieless(self, response):
        self.assertEqual(response.headers.getlist("Set-Cookie"), [])
