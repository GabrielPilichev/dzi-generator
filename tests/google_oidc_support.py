"""A fake Google with real cryptography, and the test base of the Google sign-in suites.

Nothing here reaches a network. ``FakeGoogle`` replaces the innermost transport
(``requests.adapters.HTTPAdapter.send``), so everything above it is the real code:
Authlib's client, the bounded body reader, the response rules, joserfc's signature
verification, the claim checks and the provider gate. It publishes a key set of real
RSA keys and mints ID tokens that are really signed with RS256.

"During the network call" is deterministic: ``during_exchange`` runs while the
application waits for the token endpoint, and ``hold()`` keeps the endpoint from
answering until the test releases it. No test sleeps to produce a timeout.

Every database is the temporary one of the test class; credentials are synthetic.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import logging
import os
import socket
import sqlite3
import tempfile
import threading
import time
import unittest
import warnings
from contextlib import closing, contextmanager
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, parse_qsl, urlsplit

import requests
import urllib3
from joserfc import jwt
from joserfc.jwk import RSAKey
from requests.adapters import HTTPAdapter
from requests.structures import CaseInsensitiveDict

import flask

from tests.auth_helpers import set_account_session
from tests.support import ROOT, build_test_database, web_app
from tests.test_account_login import PASSWORD, SAME_ORIGIN, AccountAppTestCase
from web import account_session, oauth_state
from web import db as web_db
from web.blueprints import oidc as oidc_blueprint
from web.services import google_oidc, oidc_consumption

CLIENT_ID = "learnpilot-tests.apps.googleusercontent.com"
CLIENT_SECRET = "synthetic-client-secret-for-tests-only"
PUBLIC_BASE_URL = "http://localhost"
REDIRECT_URI = PUBLIC_BASE_URL + "/auth/google/callback"
CALLBACK = "/auth/google/callback"
START_CLOCK = 5000  # where the replaced monotonic clock begins

_key_lock = threading.Lock()
_keys: dict[str, RSAKey] = {}


def signing_key(name: str, *, kid: str | None = None, bits: int = 2048) -> RSAKey:
    """A real RSA key, generated once per test process for each ``name``.

    ``kid`` defaults to the name; two names with one ``kid`` are two different keys
    that claim to be the same one.
    """
    with _key_lock:
        if name not in _keys:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")  # joserfc warns about a key below 2048 bits
                _keys[name] = RSAKey.generate_key(
                    bits, parameters={"kid": kid or name, "use": "sig", "alg": "RS256"})
        return _keys[name]


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def http_response(status: int, body: bytes = b"", headers: dict | None = None, *, raw=None) -> requests.Response:
    """A ``requests`` response as the real adapter builds it, over an in-memory body."""
    headers = {"Content-Type": "application/json", **(headers or {})}
    response = requests.Response()
    response.status_code = status
    response.headers = CaseInsensitiveDict(headers)
    response.raw = raw if raw is not None else urllib3.HTTPResponse(
        body=io.BytesIO(body), headers=headers, status=status, preload_content=False, decode_content=False)
    response.reason = "test"
    return response


def json_response(status: int, document, headers: dict | None = None) -> requests.Response:
    return http_response(status, json.dumps(document).encode("utf-8"), headers)


class FakeGoogle:
    """Google's token endpoint and key set, in memory.

    ``authorize`` plays the person at Google's screen for one authorization request
    and returns the code Google would redirect back with. ``send`` is the transport.
    """

    def __init__(self):
        self.keys = [signing_key("test-key-1"), signing_key("test-key-2")]
        self.published = list(self.keys)
        self.requests: list[dict] = []
        self.codes: dict[str, dict] = {}
        self.redeemed: list[str] = []
        self.unexpected: list[str] = []
        self.enforce_pkce = True
        self.during_exchange = None  # called while the application waits for the token endpoint
        self.token_response = None  # a callable(request) -> Response that replaces the token endpoint
        self.jwks_response = None  # the same for the key set
        self.jwks_headers = {"Cache-Control": "public, max-age=21000, must-revalidate, no-transform"}
        self.last_id_token = None
        self.last_access_token = None
        self._held = None
        self._entered = threading.Event()
        self._counter = 0
        self._lock = threading.Lock()

    # -- what a test does at "Google" ------------------------------------------

    def authorize(self, authorization_url: str, *, subject="google-subject-1", email="pupil@example.bg",
                  name="Ученик Тестов", **claims) -> str:
        """Authenticate at Google for this authorization request; returns the code."""
        query = dict(parse_qsl(urlsplit(authorization_url).query))
        with self._lock:
            self._counter += 1
            code = f"code-{self._counter}-" + b64url(hashlib.sha256(str(self._counter).encode()).digest())[:20]
        self.codes[code] = {
            "nonce": query.get("nonce"),
            "challenge": query.get("code_challenge"),
            "redirect_uri": query.get("redirect_uri"),
            "claims": {"sub": subject, "email": email, "name": name, **claims},
        }
        return code

    def id_token(self, claims: dict, *, key: RSAKey | None = None, header: dict | None = None) -> str:
        """An ID token really signed with RS256 (or as ``header`` says)."""
        key = key or self.keys[0]
        return jwt.encode({"alg": "RS256", "kid": key.kid, **(header or {})}, claims, key)

    def claims(self, nonce, overrides: dict | None = None) -> dict:
        now = int(time.time())
        claims = {
            "iss": "https://accounts.google.com",
            "aud": CLIENT_ID,
            "azp": CLIENT_ID,
            "iat": now,
            "exp": now + 3600,
            "nonce": nonce,
        }
        claims.update(overrides or {})
        return {name: value for name, value in claims.items() if value is not _ABSENT}

    def jwks(self) -> dict:
        return {"keys": [key.as_dict(private=False) for key in self.published]}

    # -- blocking --------------------------------------------------------------

    def hold(self) -> threading.Event:
        """Keep the token endpoint from answering until the returned event is set."""
        self._held = threading.Event()
        self._entered.clear()
        return self._held

    def wait_until_entered(self, timeout: float = 5.0) -> bool:
        return self._entered.wait(timeout)

    def release(self) -> None:
        if self._held is not None:
            self._held.set()

    # -- the transport ---------------------------------------------------------

    def send(self, request, stream=False, timeout=None, verify=True, cert=None, proxies=None):
        body = request.body.decode("utf-8") if isinstance(request.body, bytes) else (request.body or "")
        record = {
            "method": request.method, "url": request.url, "headers": dict(request.headers), "body": body,
            "stream": stream, "timeout": timeout, "verify": verify, "cert": cert, "proxies": proxies,
            "thread": threading.get_ident(),
        }
        self.requests.append(record)
        response = self._respond(request, body)
        # What the real adapter's build_response sets.
        response.request = request
        response.url = request.url
        return response

    def _respond(self, request, body: str) -> requests.Response:
        if request.url == google_oidc.TOKEN_ENDPOINT and request.method == "POST":
            self._entered.set()
            if self.during_exchange is not None:
                hook, self.during_exchange = self.during_exchange, None
                hook()
            held = self._held
            if held is not None:
                held.wait(30)
            if self.token_response is not None:
                return self.token_response(request)
            return self._token(request, body)
        if request.url == google_oidc.JWKS_URI and request.method == "GET":
            if self.jwks_response is not None:
                return self.jwks_response(request)
            return json_response(200, self.jwks(), self.jwks_headers)
        self.unexpected.append(f"{request.method} {request.url}")
        raise AssertionError("the fake provider was asked for an address it does not serve")

    def _token(self, request, body: str) -> requests.Response:
        form = {name: values[0] for name, values in parse_qs(body).items()}
        expected_auth = "Basic " + base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
        if request.headers.get("Authorization") != expected_auth:
            return json_response(401, {"error": "invalid_client", "error_description": "DESCRIPTION-SECRET"})
        code = form.get("code")
        grant = self.codes.get(code)
        if grant is None or code in self.redeemed or form.get("grant_type") != "authorization_code":
            return json_response(400, {"error": "invalid_grant", "error_description": "DESCRIPTION-SECRET"})
        if form.get("redirect_uri") != grant["redirect_uri"]:
            return json_response(400, {"error": "invalid_grant", "error_description": "DESCRIPTION-SECRET"})
        if self.enforce_pkce:
            verifier = form.get("code_verifier", "")
            if b64url(hashlib.sha256(verifier.encode("ascii")).digest()) != grant["challenge"]:
                return json_response(400, {"error": "invalid_grant", "error_description": "DESCRIPTION-SECRET"})
        self.redeemed.append(code)
        access_token = "access-token-" + b64url(hashlib.sha256(code.encode()).digest())
        claims = dict(grant["claims"])
        key = claims.pop("__key__", None)
        header = claims.pop("__header__", None)
        raw_token = claims.pop("__token__", None)
        id_token = raw_token if raw_token is not None else self.id_token(
            self.claims(grant["nonce"], claims), key=key, header=header)
        self.last_id_token, self.last_access_token = id_token, access_token
        return json_response(200, {
            "access_token": access_token, "expires_in": 3599, "scope": "openid email profile",
            "token_type": "Bearer", "id_token": id_token,
        })

    @property
    def token_requests(self) -> list[dict]:
        return [record for record in self.requests if record["url"] == google_oidc.TOKEN_ENDPOINT]

    @property
    def jwks_requests(self) -> list[dict]:
        return [record for record in self.requests if record["url"] == google_oidc.JWKS_URI]


# Marks a claim that a test wants left out of the token.
_ABSENT = object()
ABSENT = _ABSENT


def _refuse_network(*args, **kwargs):
    raise AssertionError("a test tried to open a real network connection")


@contextmanager
def no_network():
    """Any real socket connection or name lookup is an error."""
    with patch.object(socket.socket, "connect", _refuse_network), \
            patch.object(socket.socket, "connect_ex", _refuse_network), \
            patch.object(socket, "getaddrinfo", _refuse_network), \
            patch.object(socket, "create_connection", _refuse_network):
        yield


class FakeGoogleMixin:
    """Mounts a ``FakeGoogle`` as the transport and gives each test a fresh provider gate."""

    def mount_fake_google(self) -> FakeGoogle:
        self.google = FakeGoogle()
        self.real_transport = HTTPAdapter.send
        for context in (
            no_network(),
            patch.object(HTTPAdapter, "send", self.google.send),
            patch.object(google_oidc, "GATE", google_oidc.ProviderGate()),
        ):
            context.__enter__()
            self.addCleanup(context.__exit__, None, None, None)
        self.addCleanup(self._assert_only_pinned_addresses)
        self.addCleanup(self.finish_workers)
        # A line that a test does not assert on stays out of the test output.
        # assertLogs and assertNoLogs see every record all the same.
        quiet = logging.NullHandler()
        logging.getLogger("learnpilot.oidc").addHandler(quiet)
        self.addCleanup(logging.getLogger("learnpilot.oidc").removeHandler, quiet)
        return self.google

    def _assert_only_pinned_addresses(self):
        self.assertEqual(self.google.unexpected, [])

    def finish_workers(self, timeout: float = 10.0) -> None:
        """Release whatever a test still holds and wait for every worker thread to end."""
        self.google.release()
        for thread in threading.enumerate():
            if thread.name == "google-oidc" and thread is not threading.current_thread():
                thread.join(timeout)
                self.assertFalse(thread.is_alive(), "a provider worker thread outlived its test")


def provider() -> google_oidc.ProviderConfig:
    return google_oidc.provider_config(CLIENT_ID, CLIENT_SECRET, PUBLIC_BASE_URL)


class GoogleAppTestCase(FakeGoogleMixin, AccountAppTestCase):
    """The application with Google sign-in enabled, a fake Google and replaced clocks.

    ``self.clock`` is the monotonic clock of the OAuth state, in whole seconds. The
    wall clock is real unless a test patches it, so one can be moved without the other.
    """

    google_enabled = True

    def setUp(self):
        super().setUp()
        self.fresh_database()
        self.mount_fake_google()
        self.clock = START_CLOCK
        for context in (
            patch.dict(self.app.config, {
                oauth_state.PROVIDER_CONFIG_KEY: provider() if self.google_enabled else None}),
            patch.object(oauth_state, "monotonic_seconds", lambda: self.clock),
            # The throttle clock of the confirmation form, kept with the account forms'.
            patch.object(oidc_blueprint, "_utcnow", lambda: self.now),
        ):
            context.__enter__()
            self.addCleanup(context.__exit__, None, None, None)

    def fresh_database(self) -> None:
        """A database of this test's own, so that a dropped table, a trigger or a
        read-only file never reaches another test. Restored with the rest of the
        application state when the test ends (``IsolatedAppTestCase.run``)."""
        directory = tempfile.TemporaryDirectory(prefix="learnpilot-oidc-")
        self.addCleanup(directory.cleanup)
        path = build_test_database(Path(directory.name).resolve() / "questions.db")
        web_app.DB_PATH = path
        web_app.QUIZ_DB_PATH = path
        self.app.config["DB_PATH"] = str(path)
        os.environ["DZI_DB"] = str(path)
        self.db_path = str(path)

    @contextmanager
    def failing_retirement(self, fault):
        """Make the storage fail from the moment a retirement begins, and only then.

        What the route wrote and committed before it retires is unaffected. The
        request's connection is closed first, so that the retirement opens its own
        under the fault, as it does when the request had none. Yields the results.
        """
        real = oauth_state.retire_linking
        results = []

        def retire():
            open_connection = flask.g.pop("db", None)
            if open_connection is not None:
                open_connection.close()
            with fault:
                result = real()
            results.append(result)
            return result

        with patch.object(oauth_state, "retire_linking", retire), \
                patch.object(account_session, "retire_linking", retire):
            yield results

    # -- cookies ---------------------------------------------------------------

    def cookie(self, client=None) -> str | None:
        cookie = (client or self.client).get_cookie("session")
        return None if cookie is None else cookie.value

    def client_with_cookie(self, value: str | None):
        """A new browser that presents a copy of a session cookie."""
        client = self.app.test_client()
        if value is not None:
            client.set_cookie("session", value)
        return client

    def copy_of(self, client=None):
        return self.client_with_cookie(self.cookie(client))

    def signed_in(self, role="teacher", *, client=None, **options):
        """An account with a password, signed in on ``client`` without the login form."""
        account = self.make(role, **options)
        set_account_session(client or self.client, account["id"], account["session_key"])
        return account

    # -- the flow --------------------------------------------------------------

    def start(self, client=None, **form):
        return (client or self.client).post("/auth/google/start", data=form, headers=SAME_ORIGIN)

    def start_link(self, client=None):
        return (client or self.client).post("/auth/google/link", headers=SAME_ORIGIN)

    @staticmethod
    def state_of(authorization_url: str) -> str:
        return dict(parse_qsl(urlsplit(authorization_url).query))["state"]

    def callback(self, client=None, **query):
        return (client or self.client).get(CALLBACK, query_string=query)

    def return_from_google(self, started, client=None, **identity):
        """The person authenticates at Google and the browser comes back."""
        location = started.headers["Location"]
        code = self.google.authorize(location, **identity)
        return self.callback(client, code=code, state=self.state_of(location))

    def google_sign_in(self, client=None, *, next=None, **identity):
        started = self.start(client, **({"next": next} if next else {}))
        self.assertEqual(started.status_code, 303, self.body(started))
        return self.return_from_google(started, client, **identity)

    def link_until_pending(self, client=None, **identity):
        """Start a link and come back from Google: the session then holds a pending link."""
        started = self.start_link(client)
        self.assertEqual(started.status_code, 303, self.body(started))
        returned = self.return_from_google(started, client, **identity)
        self.assertEqual(
            (returned.status_code, returned.headers["Location"]), (302, "/auth/google/link/confirm"))
        return returned

    def confirm(self, client=None, password=PASSWORD, **options):
        return (client or self.client).post(
            "/auth/google/link/confirm", data={"current_password": password},
            headers=options.get("headers", SAME_ORIGIN))

    def cancel(self, client=None):
        return (client or self.client).post("/auth/google/link/cancel", headers=SAME_ORIGIN)

    def assert_redirect(self, response, location, status=302):
        self.assertEqual((response.status_code, response.headers.get("Location")), (status, location))

    # -- the database ----------------------------------------------------------

    def consumption_rows(self) -> list[tuple]:
        return self.query("SELECT tx_hash, expires_at FROM oidc_consumed_transactions ORDER BY tx_hash")

    def identity_rows(self, user_id=None) -> list[tuple]:
        if user_id is None:
            return self.query("SELECT user_id, issuer, subject, provider, email_at_link FROM user_identities ORDER BY id")
        return self.query(
            "SELECT user_id, issuer, subject, provider, email_at_link FROM user_identities "
            "WHERE user_id = ? ORDER BY id", (user_id,))

    def user_count(self) -> int:
        return self.query("SELECT COUNT(*) FROM users")[0][0]

    def consumption_key(self, purpose: str, value: str) -> str:
        return oidc_consumption.consumption_key(self.app.config["SECRET_KEY"], purpose, value)


def table_counts(db_path) -> dict:
    """Row counts of every table: a cheap way to show that nothing was written."""
    with closing(sqlite3.connect(str(db_path))) as conn:
        tables = [row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        return {table: conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0] for table in tables}


# --------------------------------------------------------------------------- storage faults
#
# Real SQLite failures, for the request connections opened while a fault is active.
# Two groups, kept apart (design section 9, "Ownership"):
#
# * at BEGIN IMMEDIATE (``fails_at_begin``): a write lock held elsewhere, and a
#   query-only connection. No transaction begins and the clock is not read;
# * inside the transaction: a missing table, a trigger that fails the insert, a
#   read-only database file, a commit that fails. BEGIN IMMEDIATE returns, the clock is
#   read once and the failing statement raises.
#
# Where the read-only file fails is a fact about these databases (rollback-journal,
# read-only by its mode, in a writable directory), not about SQLite in general.


class StorageFault:
    name = "no fault"
    fails_at_begin = False
    connection_class = None
    BUSY_TIMEOUT_MS = 20

    def __init__(self, test):
        self.test = test
        self.db_path = test.db_path
        self.statements: list[str] = []  # what the request connections executed, in order
        self._patch = None

    def install(self) -> None:
        pass

    def remove(self) -> None:
        pass

    def prepare(self, conn: sqlite3.Connection) -> None:
        pass

    def __enter__(self):
        self.install()
        real, fault = web_db.connect_db, self

        def connect_db(path, *, query_only=False, timeout=web_db.DEFAULT_TIMEOUT_SECONDS):
            if fault.connection_class is not None and not query_only:
                assert str(path) == fault.db_path, "a fault serves the test's own database only"
                conn = sqlite3.connect(str(path), timeout=timeout, factory=fault.connection_class)
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA foreign_keys=ON")
            else:
                conn = real(path, query_only=query_only, timeout=timeout)
            fault.prepare(conn)
            conn.set_trace_callback(fault.statements.append)
            return conn

        self._patch = patch.object(web_db, "connect_db", connect_db)
        self._patch.__enter__()
        return self

    def __exit__(self, *exc_info):
        self._patch.__exit__(None, None, None)
        self.remove()
        return False

    def executed(self, prefix: str) -> list[str]:
        return [statement for statement in self.statements if statement.upper().startswith(prefix.upper())]

    def _execute(self, script: str) -> None:
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.executescript(script)


class Tracing(StorageFault):
    """No fault at all: only the statements of the request connections."""


class HeldLock(StorageFault):
    name = "the write lock is held elsewhere"
    fails_at_begin = True

    def install(self):
        self.blocker = sqlite3.connect(self.db_path)
        self.blocker.execute("BEGIN IMMEDIATE")

    def remove(self):
        self.blocker.rollback()
        self.blocker.close()

    def prepare(self, conn):
        conn.execute(f"PRAGMA busy_timeout = {self.BUSY_TIMEOUT_MS}")


class QueryOnly(StorageFault):
    name = "the connection is query_only"
    fails_at_begin = True

    def prepare(self, conn):
        conn.execute("PRAGMA query_only = ON")


class ReadOnlyFile(StorageFault):
    name = "the database file is read-only"

    def install(self):
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            raise unittest.SkipTest("file modes do not bind root")
        with closing(sqlite3.connect(self.db_path)) as conn:
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        self.test.assertEqual(mode, "delete", "the read-only-file timing is asserted for a rollback-journal database")
        os.chmod(self.db_path, 0o400)

    def remove(self):
        os.chmod(self.db_path, 0o600)


class MissingTable(StorageFault):
    name = "the consumption table is missing"

    def install(self):
        self._execute("DROP TABLE oidc_consumed_transactions")

    def remove(self):
        self._execute((ROOT / "web" / "migrations" / "020_oidc_consumed_transactions.sql").read_text(encoding="utf-8"))


class FailingInsert(StorageFault):
    """A trigger fails the insert of one chosen key (every key when none is given)."""

    name = "a trigger fails the insert"

    def __init__(self, test, key: str | None = None):
        super().__init__(test)
        self.key = key

    def install(self):
        condition = "" if self.key is None else f"WHEN NEW.tx_hash = '{self.key}'"
        self._execute(
            "CREATE TRIGGER oidc_test_failing_insert BEFORE INSERT ON oidc_consumed_transactions "
            f"{condition} BEGIN SELECT RAISE(ABORT, 'forced by the test'); END;")

    def remove(self):
        self._execute("DROP TRIGGER oidc_test_failing_insert")


class _FailingCommitConnection(sqlite3.Connection):
    def commit(self):
        raise sqlite3.OperationalError("disk I/O error")


class FailingCommit(StorageFault):
    """A stand-in: a COMMIT that fails, as a full disk or an I/O error makes it."""

    name = "the commit fails"
    connection_class = _FailingCommitConnection


class CommitBlockedByReader(StorageFault):
    """Real: on a rollback-journal database a reader's lock makes the COMMIT fail."""

    name = "a reader blocks the commit"

    def install(self):
        self.reader = sqlite3.connect(self.db_path)
        self.reader.execute("BEGIN")
        self.reader.execute("SELECT COUNT(*) FROM users").fetchone()

    def remove(self):
        self.reader.rollback()
        self.reader.close()

    def prepare(self, conn):
        conn.execute(f"PRAGMA busy_timeout = {self.BUSY_TIMEOUT_MS}")


class _CommitThenErrorConnection(sqlite3.Connection):
    def commit(self):
        super().commit()
        raise sqlite3.OperationalError("disk I/O error")


class CommitThenError(StorageFault):
    """A stand-in: the COMMIT takes effect and an error is reported all the same."""

    name = "the commit takes effect and reports an error"
    connection_class = _CommitThenErrorConnection


class _FailingCommitAndRollbackConnection(sqlite3.Connection):
    def commit(self):
        raise sqlite3.OperationalError("disk I/O error")

    def rollback(self):
        raise sqlite3.OperationalError("cannot rollback")


class FailingCommitAndRollback(StorageFault):
    """A stand-in: the COMMIT fails, and the ROLLBACK that follows fails as well."""

    name = "the commit and the rollback fail"
    connection_class = _FailingCommitAndRollbackConnection


# Every way a retirement's record can fail to be written.
RETIREMENT_FAULTS = (
    HeldLock, QueryOnly, ReadOnlyFile, MissingTable, FailingInsert, FailingCommit, CommitBlockedByReader,
)
