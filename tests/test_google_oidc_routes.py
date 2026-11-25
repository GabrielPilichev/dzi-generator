"""Google OIDC, test plan B4: the routes.

``POST /auth/google/start`` and ``/link``, ``GET /auth/google/callback``,
``/auth/google/link/confirm`` and ``POST /auth/google/link/cancel``, with the account
page and the sign-in page that show their outcomes. Requests go through the real
application; Google is the fake of ``tests.google_oidc_support``.

What a cancellation may say is pinned row by row (design 1.6): it answers
``link_cancelled`` only when its own record ended the link and the account had no
Google identity when it was read. A link that was completed, an object that had
already been used, an empty session, a record that could not be written and an account
that could not be read never produce it.
"""

import csv
import hashlib
import io
import json
import logging
import re
import sqlite3
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, redirect_stderr, redirect_stdout
from datetime import timedelta
from html import unescape
from itertools import product
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qsl, urlsplit

from werkzeug.exceptions import NotFound

from tests.auth_helpers import create_account, set_account_session
from tests.google_oidc_support import (
    CALLBACK,
    CLIENT_ID,
    CLIENT_SECRET,
    REDIRECT_URI,
    RETIREMENT_FAULTS,
    CommitBlockedByReader,
    CommitThenError,
    FailingCommit,
    FailingCommitAndRollback,
    FailingInsert,
    GoogleAppTestCase,
    HeldLock,
    QueryOnly,
    ReadOnlyFile,
    Tracing,
    json_response,
    provider,
    table_counts,
)
from tests.lifecycle_support import eligible_section
from tests.support import NO_DEADLINE_FORM, student_entry_path, web_app
from tests.test_account_login import CROSS_ORIGIN, NEW_PASSWORD, PASSWORD, SAME_ORIGIN, THROTTLED
from web import access, oauth_state, request_integrity
from web import db as web_db
from web.blueprints import account as account_blueprint
from web.blueprints import oidc as oidc_blueprint
from web.services import accounts, google_oidc, identities, oidc_accounts, oidc_consumption

ISSUER = "https://accounts.google.com"
ROUTES = {
    "/auth/google/start": ("oidc.google_start", {"POST"}, access.PUBLIC, request_integrity.SAME_ORIGIN),
    "/auth/google/link": ("oidc.google_link", {"POST"}, access.SIGNED_IN, request_integrity.SAME_ORIGIN),
    "/auth/google/callback": ("oidc.google_callback", {"GET"}, access.PUBLIC, request_integrity.OAUTH_STATE),
    "/auth/google/link/confirm": (
        "oidc.google_link_confirm", {"GET", "POST"}, access.SIGNED_IN, request_integrity.SAME_ORIGIN),
    "/auth/google/link/cancel": ("oidc.google_link_cancel", {"POST"}, access.SIGNED_IN, request_integrity.SAME_ORIGIN),
}
POST_ROUTES = ("/auth/google/start", "/auth/google/link", "/auth/google/link/confirm", "/auth/google/link/cancel")

# The exact texts of design 1.9, and where each is shown.
TEXTS = {
    "cancelled": "Входът с Google беше отказан.",
    "failed": "Входът с Google не успя. Опитай отново.",
    "expired": "Времето изтече. Опитай отново.",
    "unavailable": "Входът с Google временно не е достъпен. Опитай по-късно.",
    "email_exists": "Вече има профил с този имейл. Влез с имейл и парола и свържи Google от „Профил“.",
    "not_allowed": "Този профил не може да използва вход с Google.",
    "linked": "Google профилът е свързан.",
    "already_linked": "Този Google профил вече е свързан с твоя профил.",
    "identity_taken": "Този Google профил вече е свързан с друг профил.",
    "email_other_account": (
        "Имейлът на този Google профил е на друг профил в LearnPilot. Влез в онзи профил и го свържи оттам."),
    "link_cancelled": "Свързването с Google беше отказано.",
    "link_not_pending": "Няма свързване с Google, което тази заявка да прекрати.",
    "account_linked": "Профилът ти вече е свързан с Google.",
    "cancel_unconfirmed": "Не успяхме да потвърдим отказа.",
}
SIGN_IN_PAGE_CODES = {"cancelled", "failed", "expired", "unavailable", "email_exists", "not_allowed"}
ACCOUNT_PAGE_CODES = set(TEXTS) - {"email_exists"}
CANCEL_CODES = ("link_cancelled", "link_not_pending", "account_linked", "cancel_unconfirmed")
PENDING_NOTICE = "Имаш незавършено свързване с Google."


class RoutesTestCase(GoogleAppTestCase):
    def html(self, response):
        return unescape(self.body(response))

    def account_page(self, client=None, code=None):
        response = (client or self.client).get("/account" + (f"?google={code}" if code else ""))
        self.assertEqual(response.status_code, 200)
        return self.html(response)

    def give_identity(self, user_id, subject="pre-linked", email=None):
        with closing(sqlite3.connect(self.db_path)) as conn:
            identities.add_identity(conn, user_id, issuer=ISSUER, subject=subject, provider="google",
                                    email_at_link=email)
            conn.commit()

    def slots(self, client=None) -> set:
        return {oauth_state.TX_SLOT, oauth_state.LINK_SLOT} & set(self.session(client))

    def assert_cancel(self, response, code, client=None):
        """A cancellation answered ``code``: both slots are gone, and the page it leads
        to shows that code's text and none of the other three."""
        self.assert_redirect(response, f"/account?google={code}")
        self.assertEqual(self.slots(client), set())
        page = self.account_page(client, code)
        for other in CANCEL_CODES:
            (self.assertIn if other == code else self.assertNotIn)(TEXTS[other], page)
        return page


# --------------------------------------------------------------------------- the route table


class RouteTableTest(RoutesTestCase):
    def test_the_blueprint_owns_exactly_the_five_routes(self):
        rules = {rule.rule: rule for rule in self.app.url_map.iter_rules() if rule.endpoint.startswith("oidc.")}
        self.assertEqual(set(rules), set(ROUTES))
        for url, (endpoint, methods, policy, integrity) in ROUTES.items():
            with self.subTest(url=url):
                view = self.app.view_functions[endpoint]
                self.assertEqual(rules[url].endpoint, endpoint)
                self.assertEqual(rules[url].methods - {"HEAD", "OPTIONS"}, methods)
                self.assertEqual(view.__module__, "web.blueprints.oidc")
                self.assertEqual(access.policy_for_view(view), policy)
                self.assertEqual(request_integrity.integrity_policy_for_view(view), integrity)
        # No path says "login" or "logout", and the blueprint is not the removed "auth".
        self.assertFalse([url for url in ROUTES if "login" in url or "logout" in url])
        self.assertIn("oidc", self.app.blueprints)

    def test_every_code_a_route_can_answer_has_a_text(self):
        self.assertEqual({code: text for code, (text, _, _) in account_blueprint.GOOGLE_MESSAGES.items()}, TEXTS)
        shown = {code: pages for code, (_, _, pages) in account_blueprint.GOOGLE_MESSAGES.items()}
        self.assertEqual({code for code, pages in shown.items() if "sign-in" in pages}, SIGN_IN_PAGE_CODES)
        self.assertEqual({code for code, pages in shown.items() if "account" in pages}, ACCOUNT_PAGE_CODES)
        answered = {
            oidc_blueprint.CANCELLED, oidc_blueprint.FAILED, oidc_blueprint.EXPIRED, oidc_blueprint.UNAVAILABLE,
            oidc_blueprint.LINK_CANCELLED, oidc_blueprint.LINK_NOT_PENDING, oidc_blueprint.ACCOUNT_LINKED,
            oidc_blueprint.CANCEL_UNCONFIRMED, oidc_accounts.LINKED, oidc_accounts.ALREADY_LINKED,
            oidc_accounts.IDENTITY_TAKEN, oidc_accounts.NOT_ALLOWED, oidc_accounts.EMAIL_EXISTS,
            oidc_accounts.EMAIL_OTHER_ACCOUNT, oidc_accounts.EXPIRED, oidc_accounts.FAILED,
        }
        self.assertEqual(answered, set(TEXTS))

    def test_the_password_form_texts_are_the_account_forms_texts(self):
        self.assertEqual(oidc_blueprint.THROTTLED_MESSAGE, account_blueprint.THROTTLED_MESSAGE)
        self.assertEqual(oidc_blueprint.WRONG_CURRENT_PASSWORD_MESSAGE, account_blueprint.WRONG_CURRENT_PASSWORD_MESSAGE)


# --------------------------------------------------------------------------- start


class StartTest(RoutesTestCase):
    def test_a_same_origin_post_redirects_to_google_with_exactly_the_documented_parameters(self):
        started = self.start(next="/account/attempts")
        self.assertEqual(started.status_code, 303)
        self.assertEqual(started.headers["Cache-Control"], "no-store")
        location = urlsplit(started.headers["Location"])
        self.assertEqual(f"{location.scheme}://{location.netloc}{location.path}", google_oidc.AUTHORIZATION_ENDPOINT)
        query = dict(parse_qsl(location.query))
        transaction = self.session()[oauth_state.TX_SLOT]
        self.assertEqual(set(query), {"response_type", "client_id", "redirect_uri", "scope", "state", "nonce",
                                      "code_challenge", "code_challenge_method", "prompt"})
        self.assertEqual((query["response_type"], query["client_id"], query["redirect_uri"], query["scope"]),
                         ("code", CLIENT_ID, REDIRECT_URI, "openid email profile"))
        self.assertEqual((query["state"], query["nonce"]), (transaction["state"], transaction["nonce"]))
        self.assertEqual((query["code_challenge_method"], query["prompt"]), ("S256", "select_account"))
        # The verifier stays in the session; only its challenge travels.
        self.assertNotIn(transaction["cv"], started.headers["Location"])
        self.assertNotIn(CLIENT_SECRET, started.headers["Location"])
        self.assertEqual(self.google.requests, [])

    def test_only_a_same_origin_post_starts(self):
        self.assertEqual(self.client.get("/auth/google/start").status_code, 405)
        forged = self.client.post("/auth/google/start", headers=CROSS_ORIGIN)
        self.assertEqual((forged.status_code, forged.headers.getlist("Set-Cookie")), (403, []))
        with patch.dict(self.app.config, {"TESTING": False}):
            self.assertEqual(self.client.post("/auth/google/start").status_code, 403)
        self.assertEqual(self.session(), {})

    def test_nothing_but_next_is_read_from_the_form(self):
        started = self.client.post("/auth/google/start", headers=SAME_ORIGIN, data={
            "next": "/account/attempts", "intent": "link", "state": "x" * 43, "uid": "1", "login_hint": "a@b.bg",
            "hd": "school.example"})
        transaction = self.session()[oauth_state.TX_SLOT]
        self.assertEqual((transaction["intent"], transaction["next"]), ("signin", "/account/attempts"))
        self.assertNotEqual(transaction["state"], "x" * 43)
        query = dict(parse_qsl(urlsplit(started.headers["Location"]).query))
        self.assertNotIn("login_hint", query)
        self.assertNotIn("hd", query)

    def test_a_signed_in_account_starts_nothing(self):
        for role, home in (("student", "/"), ("teacher", "/teacher/new"), ("admin", "/teacher")):
            with self.subTest(role=role):
                client = self.app.test_client()
                account = create_account(self.db_path, role)
                set_account_session(client, account["id"], account["session_key"])
                before = self.session(client)
                self.assert_redirect(self.start(client), home)
                self.assert_redirect(self.start(client, next="/account/attempts"), "/account/attempts")
                self.assert_redirect(self.start(client, next="//evil.example/"), home)
                self.assertEqual(self.session(client), before)
        self.assertEqual(self.google.requests, [])

    def test_the_redirect_uri_ignores_a_forged_host(self):
        # A forwarding header is not trusted outside the production proxy setup, and
        # the redirect URI is the configured one whatever a request says.
        started = self.client.post("/auth/google/start", headers={**SAME_ORIGIN, "X-Forwarded-Host": "evil.example",
                                                                 "X-Forwarded-Proto": "https"})
        self.assertEqual(started.status_code, 303)
        self.assertEqual(dict(parse_qsl(urlsplit(started.headers["Location"]).query))["redirect_uri"], REDIRECT_URI)
        # On another host the callback would arrive without this host's cookie: no flow starts there.
        for host in ("evil.example", "localhost:8443", "127.0.0.1"):
            with self.subTest(host=host):
                client = self.app.test_client()
                refused = client.post("/auth/google/start", base_url=f"http://{host}",
                                      headers={"Origin": f"http://{host}"})
                self.assertEqual((refused.status_code, refused.headers.getlist("Set-Cookie")), (404, []))
                self.assertNotIn("Location", refused.headers)
        # The same for a link, from a browser that is signed in on that other host.
        signed_in = self.app.test_client()
        self.signed_in("teacher", client=signed_in)
        elsewhere = self.app.test_client()
        elsewhere.set_cookie("session", self.cookie(signed_in), domain="evil.example")
        refused = elsewhere.post("/auth/google/link", base_url="http://evil.example",
                                 headers={"Origin": "http://evil.example"})
        self.assertEqual((refused.status_code, refused.headers.getlist("Set-Cookie")), (404, []))
        self.assertEqual(self.slots(signed_in), set())


# --------------------------------------------------------------------------- the callback


class CallbackOutcomeTest(RoutesTestCase):
    def test_a_first_sign_in_creates_a_student_and_the_next_signs_the_same_account_in(self):
        with self.assertLogs("learnpilot.oidc", logging.INFO) as logs:
            first = self.google_sign_in(subject="subject-1", email="pupil@example.bg", name="Мария Петрова")
        self.assert_redirect(first, "/")
        self.assertEqual(first.headers["Cache-Control"], "no-store")
        self.assertEqual(first.headers["Referrer-Policy"], "no-referrer")
        state = self.session()
        self.assertEqual(set(state), {"uid", "sk", "iat"})
        user_id = state["uid"]
        self.assertEqual(self.query("SELECT email, display_name, role, password_hash IS NULL FROM users"),
                         [(None, "Мария Петрова", "student", 1)])
        self.assertEqual(self.identity_rows(), [(user_id, ISSUER, "subject-1", "google", "pupil@example.bg")])
        self.assertEqual(logs.output, [f"INFO:learnpilot.oidc:callback outcome=created intent=signin user={user_id} reason=-"])
        self.assertEqual(self.client.get("/account/attempts").status_code, 200)

        other_browser = self.app.test_client()
        with self.assertLogs("learnpilot.oidc", logging.INFO) as logs:
            again = self.google_sign_in(other_browser, subject="subject-1", email="changed@example.bg")
        self.assert_redirect(again, "/")
        self.assertEqual(self.session(other_browser)["uid"], user_id)
        self.assertEqual(self.user_count(), 1)
        self.assertIn("outcome=signed_in", logs.output[0])

    def test_a_cancellation_and_a_provider_error(self):
        for error, code in (("access_denied", "cancelled"), ("server_error", "failed"), ("", "failed"),
                            ("ACCESS_DENIED", "failed"), (["access_denied", "access_denied"], "failed")):
            with self.subTest(error=error):
                client = self.app.test_client()
                started = self.start(client)
                state = self.state_of(started.headers["Location"])
                with self.assertLogs("learnpilot.oidc", logging.INFO) as logs:
                    answered = self.callback(client, state=state, error=error, error_description="SECRET-DESCRIPTION")
                self.assert_redirect(answered, f"/login?google={code}")
                self.assertEqual(self.session(client), {})
                # The value is never echoed or logged.
                self.assertNotIn("SECRET-DESCRIPTION", "\n".join(logs.output) + answered.headers["Location"])
                page = self.html(client.get(answered.headers["Location"]))
                self.assertIn(TEXTS[code], page)
        self.assertEqual((self.google.requests, self.user_count()), ([], 0))

    def test_a_wrong_iss_parameter_fails_and_the_right_one_is_accepted(self):
        for iss in ("https://accounts.google.com/", "accounts.google.com", "https://evil.example",
                    ["https://accounts.google.com", "https://accounts.google.com"], ""):
            with self.subTest(iss=iss):
                client = self.app.test_client()
                started = self.start(client)
                code = self.google.authorize(started.headers["Location"])
                with self.assertLogs("learnpilot.oidc", logging.WARNING):
                    answered = self.callback(client, code=code, state=self.state_of(started.headers["Location"]), iss=iss)
                self.assert_redirect(answered, "/login?google=failed")
        self.assertEqual(self.google.requests, [])
        started = self.start()
        code = self.google.authorize(started.headers["Location"])
        done = self.callback(code=code, state=self.state_of(started.headers["Location"]), iss="https://accounts.google.com")
        self.assert_redirect(done, "/")

    def test_the_transaction_is_spent_before_any_other_parameter_is_looked_at(self):
        # A matched callback with an error, a malformed code or a wrong iss leaves a
        # consumption record and makes no outbound request. The person starts again.
        cases = ({"error": "access_denied"}, {"error": "anything"}, {}, {"code": "c" * 257}, {"code": "sp ace"},
                 {"code": "fine", "iss": "https://evil.example"})
        for query in cases:
            with self.subTest(query=query):
                client = self.app.test_client()
                started = self.start(client)
                transaction = self.session(client)[oauth_state.TX_SLOT]
                with self.assertLogs("learnpilot.oidc", logging.INFO):
                    self.callback(client, state=transaction["state"], **query)
                key = self.consumption_key("callback", transaction["state"])
                self.assertIn((key, transaction["exp"]), self.consumption_rows())
                # A second callback with a real code is refused: the first spent it.
                code = self.google.authorize(started.headers["Location"])
                client.set_cookie("session", self.cookie_with(transaction))
                with self.assertNoLogs("learnpilot.oidc"):
                    replay = self.callback(client, state=transaction["state"], code=code)
                self.assert_redirect(replay, "/login?google=failed")
        self.assertEqual((self.google.requests, self.user_count()), ([], 0))

    def cookie_with(self, transaction) -> str:
        """A session cookie that holds this transaction again: a restored start cookie."""
        client = self.app.test_client()
        with client.session_transaction() as sess:
            sess[oauth_state.TX_SLOT] = transaction
        return self.cookie(client)

    def test_a_sign_in_callback_that_meets_a_signed_in_account_fails_and_touches_nothing(self):
        started = self.start()
        transaction = self.session()[oauth_state.TX_SLOT]
        teacher = self.make("teacher")
        set_account_session(self.client, teacher["id"], teacher["session_key"])
        before = self.session()
        code = self.google.authorize(started.headers["Location"], subject="someone")
        with self.assertLogs("learnpilot.oidc", logging.WARNING) as logs:
            answered = self.callback(code=code, state=transaction["state"])
        self.assert_redirect(answered, "/login?google=failed")
        # No linking happens, no account is switched and nothing is asked of Google.
        after = self.session()
        self.assertEqual({key: after[key] for key in ("uid", "sk", "iat")}, {key: before[key] for key in ("uid", "sk", "iat")})
        self.assertEqual((self.identity_rows(), self.google.requests, self.user_count()), ([], [], 1))
        self.assertIn("reason=signed_in", logs.output[0])
        # The message reaches the signed-in person on the account page.
        self.assert_redirect(self.client.get("/login?google=failed"), "/account?google=failed")

    def test_the_callback_never_returns_a_page(self):
        def assert_only_a_redirect(response, location):
            self.assert_redirect(response, location)
            self.assertEqual(response.headers["Cache-Control"], "no-store")
            self.assertEqual(response.headers["Referrer-Policy"], "no-referrer")
            # Nothing of the application's pages: only the stub that names the local target.
            body = self.body(response)
            self.assertLess(len(body), 400)
            for fragment in ("app-shell", "login-card", "Google", "code=", "state="):
                self.assertNotIn(fragment, body)

        raisers = {
            "the exchange": patch.object(google_oidc.GATE, "exchange", side_effect=RuntimeError("SECRET-TEXT")),
            "the onboarding": patch.object(oidc_accounts, "sign_in_or_onboard", side_effect=ValueError("SECRET-TEXT")),
            "the database": patch.object(oidc_accounts, "sign_in_or_onboard",
                                         side_effect=sqlite3.OperationalError("SECRET-TEXT")),
        }
        for name, raiser in raisers.items():
            with self.subTest(raising=name):
                client = self.app.test_client()
                started = self.start(client)
                with raiser, self.assertLogs("learnpilot.oidc", logging.WARNING) as logs:
                    answered = self.return_from_google(started, client, subject=f"subject-{len(name)}")
                assert_only_a_redirect(answered, "/login?google=failed")
                # Logged by class name only, and rolled back.
                self.assertRegex(logs.output[-1], r"callback outcome=failed intent=signin user=- reason=[A-Z][A-Za-z]+$")
                self.assertNotIn("SECRET-TEXT", "\n".join(logs.output))
        self.assertEqual((self.identity_rows(), self.user_count()), ([], 0))
        assert_only_a_redirect(self.callback(code="x", state="y" * 43), "/login?google=failed")

        # An exception after the commit: the account exists, no session was issued, and
        # the answer is still only a redirect. The next sign-in finds the account.
        client = self.app.test_client()
        started = self.start(client)
        with patch.object(oidc_blueprint, "issue_account_session", side_effect=KeyError("SECRET-TEXT")), \
                self.assertLogs("learnpilot.oidc", logging.WARNING) as logs:
            answered = self.return_from_google(started, client, subject="committed")
        assert_only_a_redirect(answered, "/login?google=failed")
        self.assertIn("reason=KeyError", logs.output[-1])
        self.assertEqual((self.user_count(), self.session(client)), (1, {}))
        self.assert_redirect(self.google_sign_in(client, subject="committed"), "/")
        self.assertEqual(self.user_count(), 1)

    def test_a_failed_onboarding_is_rolled_back(self):
        started = self.start()
        with patch.object(identities, "add_identity", side_effect=sqlite3.OperationalError("disk I/O error")), \
                self.assertLogs("learnpilot.oidc", logging.WARNING):
            answered = self.return_from_google(started)
        self.assert_redirect(answered, "/login?google=failed")
        self.assertEqual((self.user_count(), self.identity_rows(), self.session()), (0, [], {}))

    def test_an_email_of_an_existing_account_creates_nothing_and_says_how_to_link(self):
        self.make("teacher", email="teacher@example.bg")
        with self.assertLogs("learnpilot.oidc", logging.WARNING):
            answered = self.google_sign_in(subject="teachers-google", email="Teacher@Example.BG")
        self.assert_redirect(answered, "/login?google=email_exists")
        self.assertEqual((self.user_count(), self.identity_rows(), self.session()), (1, [], {}))
        self.assertIn(TEXTS["email_exists"], self.html(self.client.get("/login?google=email_exists")))

    def test_a_provider_outage_and_an_overrun_end_unavailable(self):
        started = self.start()
        self.google.token_response = lambda request: json_response(503, {})
        with self.assertLogs("learnpilot.oidc", logging.WARNING) as logs:
            self.assert_redirect(self.return_from_google(started), "/login?google=unavailable")
        self.assertIn("callback outcome=unavailable intent=signin user=- reason=status", logs.output[0])
        self.google.token_response = None

        client = self.app.test_client()
        started = self.start(client)
        held = self.google.hold()
        database = table_counts(self.db_path)
        with patch.object(google_oidc, "NETWORK_BUDGET", 0.15), self.assertLogs("learnpilot.oidc", logging.WARNING) as logs:
            answered = self.return_from_google(started, client, subject="late")
        self.assert_redirect(answered, "/login?google=unavailable")
        self.assertIn("reason=overrun", logs.output[0])
        # The late result changes nothing: no account, no session.
        held.set()
        self.finish_workers()
        expected = dict(database, oidc_consumed_transactions=database["oidc_consumed_transactions"] + 1)
        self.assertEqual(table_counts(self.db_path), expected)
        self.assertEqual(self.session(client), {})


class CallbackMethodTest(RoutesTestCase):
    """Methods on the callback address (design 1.2)."""

    def connections(self):
        opened, real = [], web_db.connect_db

        def connect(*args, **kwargs):
            opened.append(args)
            return real(*args, **kwargs)

        return patch.object(web_db, "connect_db", connect), opened

    def test_head_and_options_answer_405_from_the_hook_and_touch_nothing(self):
        teacher = self.signed_in("teacher")
        started = self.start_link()
        transaction = self.session()[oauth_state.TX_SLOT]
        code = self.google.authorize(started.headers["Location"])
        for matching in (True, False):
            query = {"state": transaction["state"] if matching else "z" * 43, "code": code}
            for method in ("HEAD", "OPTIONS"):
                with self.subTest(method=method, matching=matching):
                    spy, opened = self.connections()
                    with spy, patch.object(oidc_blueprint, "_finish_callback", side_effect=AssertionError("the view ran")):
                        answered = self.client.open(CALLBACK, method=method, query_string=query)
                    self.assertEqual((answered.status_code, answered.headers["Allow"]), (405, "GET"))
                    self.assertEqual((answered.headers.getlist("Set-Cookie"), opened), ([], []))
        # A HEAD never redeems a code, and the transaction is still there for the GET.
        self.assertEqual((self.google.requests, self.consumption_rows()), ([], []))
        self.assertEqual(self.session()[oauth_state.TX_SLOT], transaction)
        self.assertEqual(self.session()["uid"], teacher["id"])

    def test_a_post_resolves_to_no_view(self):
        self.start()
        transaction = self.session()[oauth_state.TX_SLOT]
        path = f"{CALLBACK}?state={transaction['state']}&code=abc"
        with patch.object(oauth_state, "_spend", side_effect=AssertionError("the hook acted")):
            for method in ("POST", "PUT", "PATCH", "DELETE"):
                with self.subTest(method=method):
                    self.assertEqual(self.client.open(path, method=method, headers=SAME_ORIGIN).status_code, 405)
                    self.assertEqual(self.client.open(path, method=method, headers=CROSS_ORIGIN).status_code, 403)
        self.assertEqual(self.session()[oauth_state.TX_SLOT], transaction)
        self.assertEqual((self.google.requests, self.consumption_rows()), ([], []))

    def test_a_callback_without_a_matching_transaction_writes_nothing_for_any_visitor(self):
        visitors = {"anonymous": self.app.test_client()}
        for role in ("student", "teacher", "admin"):
            visitors[role] = self.app.test_client()
            account = create_account(self.db_path, role)
            set_account_session(visitors[role], account["id"], account["session_key"])
        other_state = "o" * 43
        for name, client in visitors.items():
            for stored in (None, "a sign-in in progress"):
                with self.subTest(visitor=name, stored=stored):
                    if stored:
                        with client.session_transaction() as sess:
                            sess[oauth_state.TX_SLOT] = {
                                "v": 1, "intent": "signin", "state": "s" * 43, "nonce": "n" * 43, "cv": "c" * 86,
                                "next": "", "inst": oauth_state.INSTANCE, "exp": self.clock + 600}
                    before, rows = self.session(client), table_counts(self.db_path)
                    spy, opened = self.connections()
                    with spy:
                        answered = client.get(CALLBACK, query_string={"state": other_state, "code": "abc"})
                    self.assert_redirect(answered, "/login?google=failed")
                    # No cookie, no row in any table, no database access and no outbound request.
                    self.assertEqual((answered.headers.getlist("Set-Cookie"), opened), ([], []))
                    self.assertEqual((self.session(client), table_counts(self.db_path)), (before, rows))
        self.assertEqual(self.google.requests, [])

    def test_the_hook_runs_between_the_same_origin_check_and_the_account_loader(self):
        hooks = [func.__name__ for func in self.app.before_request_funcs[None]]
        self.assertEqual(hooks, ["protect_same_origin_unsafe_methods", "verify_oauth_state", "load_current_user",
                                 "enforce_access_policy"])
        self.assertIs(self.app.before_request_funcs[None][1], oauth_state.verify_oauth_state)

    def test_the_view_does_not_proceed_without_a_matched_transaction(self):
        # Called without the hook having matched anything: it refuses.
        with self.app.test_request_context(f"{CALLBACK}?state={'s' * 43}&code=abc"):
            with self.assertRaises(NotFound):
                self.app.view_functions["oidc.google_callback"]()
        self.assertEqual(self.google.requests, [])


class SessionAfterSignInTest(RoutesTestCase):
    def test_the_session_is_exactly_the_account_identity_and_the_carried_attempt_markers(self):
        with self.client.session_transaction() as sess:
            sess.update({"quiz_attempt_owner:7": True, "ui_profile": "admin", "mfa_uid": 5, "something": "else"})
        before = int(time.time())
        self.assert_redirect(self.google_sign_in(subject="subject-1"), "/")
        state = self.session()
        self.assertEqual(set(state), {"uid", "sk", "iat", "quiz_attempt_owner:7"})
        self.assertEqual(state["sk"], self.user(state["uid"], "session_key"))
        self.assertTrue(before <= state["iat"] <= int(time.time()))
        # No provider name, no flag and no claim.
        serialized = repr(state)
        for leaked in ("google", "subject-1", "pupil@example.bg", "Ученик", "student", "role", "oidc"):
            self.assertNotIn(leaked, serialized)

    def test_the_absolute_lifetime_and_key_rotation_behave_as_for_a_password_session(self):
        self.google_sign_in(subject="subject-1")
        user_id = self.session()["uid"]
        self.assertEqual(self.client.get("/account/attempts").status_code, 200)
        # "Sign out everywhere" ends it.
        with closing(sqlite3.connect(self.db_path)) as conn:
            accounts.rotate_session_key(conn, user_id)
            conn.commit()
        self.assert_redirect(self.client.get("/account/attempts"), "/login?next=/account/attempts")
        # Thirty days for a student, from the session's iat.
        self.google_sign_in(subject="subject-1")
        with self.client.session_transaction() as sess:
            sess["iat"] = int(time.time()) - 31 * 24 * 3600
        self.assert_redirect(self.client.get("/account/attempts"), "/login?next=/account/attempts")
        self.assertEqual(self.session(), {})

    def test_a_disabled_account_is_refused_and_enabled_it_signs_in_again(self):
        self.google_sign_in(subject="subject-1")
        user_id = self.session()["uid"]
        self.execute("UPDATE users SET is_active = 0 WHERE id = ?", (user_id,))
        client = self.app.test_client()
        with self.assertLogs("learnpilot.oidc", logging.WARNING):
            refused = self.google_sign_in(client, subject="subject-1")
        self.assert_redirect(refused, "/login?google=not_allowed")
        self.assertEqual((self.session(client), self.user_count()), ({}, 1))
        self.assertIn(TEXTS["not_allowed"], self.html(client.get("/login?google=not_allowed")))
        self.execute("UPDATE users SET is_active = 1 WHERE id = ?", (user_id,))
        self.assert_redirect(self.google_sign_in(client, subject="subject-1"), "/")


class ReturnTargetTest(RoutesTestCase):
    def test_a_safe_target_is_followed_after_the_sign_in(self):
        self.assert_redirect(self.google_sign_in(next="/account/attempts", subject="subject-1"), "/account/attempts")

    def test_unsafe_targets_fall_back_by_role(self):
        teacher = self.make("teacher")
        self.give_identity(teacher["id"], "teachers-google")
        for target in ("https://evil.example/", "//evil.example/", "/\\evil.example", "/a\r\nSet-Cookie: x=1",
                       "javascript:alert(1)", "evil.example", "/" + "a" * 600):
            with self.subTest(target=target[:30]):
                student, linked = self.app.test_client(), self.app.test_client()
                self.assert_redirect(self.google_sign_in(student, next=target, subject="a-student"), "/")
                self.assert_redirect(self.google_sign_in(linked, next=target, subject="teachers-google"), "/teacher/new")

    def test_the_target_is_validated_again_when_it_is_used(self):
        # A transaction that holds an unsafe target (it cannot come from the start
        # route) is not followed either.
        self.start()
        with self.client.session_transaction() as sess:
            transaction = dict(sess[oauth_state.TX_SLOT], next="//evil.example/")
            sess[oauth_state.TX_SLOT] = transaction
        url = google_oidc.authorization_url(
            self.app.config[oauth_state.PROVIDER_CONFIG_KEY], state=transaction["state"], nonce=transaction["nonce"],
            code_verifier=transaction["cv"])
        code = self.google.authorize(url)
        self.assert_redirect(self.callback(code=code, state=transaction["state"]), "/")

    def test_a_link_returns_to_the_confirmation_and_then_to_the_account_page(self):
        self.signed_in("teacher")
        self.link_until_pending()
        self.assert_redirect(self.confirm(), "/account?google=linked")


class AdminTest(RoutesTestCase):
    def test_an_admin_cannot_start_or_confirm_a_link(self):
        admin = self.make("admin")
        set_account_session(self.client, admin["id"], admin["session_key"])
        refused = self.start_link()
        self.assertEqual(refused.status_code, 403)
        self.assertNotIn("Location", refused.headers)
        self.assertEqual(self.slots(), set())
        self.assertEqual(self.confirm().status_code, 403)
        # A pending link of another account in the admin's cookie changes nothing.
        with self.client.session_transaction() as sess:
            sess[oauth_state.LINK_SLOT] = {
                "v": 1, "id": "i" * 43, "inst": oauth_state.INSTANCE, "exp": self.clock + 100, "uid": admin["id"],
                "skd": oidc_accounts.session_key_digest(self.app.config["SECRET_KEY"], admin["session_key"]),
                "siat": self.session()["iat"], "sub": "admins-google", "email": "", "name": ""}
        self.assertEqual(self.confirm().status_code, 403)
        self.assert_redirect(self.client.get("/auth/google/link/confirm"), "/account?google=expired")
        self.assertEqual((self.identity_rows(), self.google.requests), ([], []))
        page = self.account_page()
        self.assertIn("Администраторските профили влизат само с парола и код.", page)
        self.assertNotIn("Свържи с Google", page)
        self.assertNotIn(PENDING_NOTICE, page)

    def test_an_admin_identity_cannot_sign_in(self):
        admin = self.make("admin")
        self.give_identity(admin["id"], "admins-google")
        with self.assertLogs("learnpilot.oidc", logging.WARNING):
            refused = self.google_sign_in(subject="admins-google")
        self.assert_redirect(refused, "/login?google=not_allowed")
        self.assertEqual(self.session(), {})
        self.assert_redirect(self.client.get("/teacher"), "/login?next=/teacher")
        self.assertIsNone(self.user(admin["id"], "last_login_at"))

    def test_a_pending_second_factor_is_not_bypassed(self):
        admin = self.make("admin")
        self.give_identity(admin["id"], "admins-google")
        self.now += timedelta(seconds=31)
        self.assertEqual(self.login(admin["email"]).status_code, 200)
        self.assertEqual(set(self.session()), {"mfa_uid", "mfa_sk", "mfa_iat"})
        # The pending state is not a principal, so a sign-in may start; the admin's
        # own Google identity still signs nobody in.
        with self.assertLogs("learnpilot.oidc", logging.WARNING):
            refused = self.google_sign_in(subject="admins-google")
        self.assert_redirect(refused, "/login?google=not_allowed")
        self.assertNotIn("uid", self.session())
        self.assert_redirect(self.client.get("/teacher"), "/login?next=/teacher")
        # Another person's Google sign-in replaces the pending keys: a student session.
        self.assert_redirect(self.google_sign_in(subject="a-student"), "/")
        self.assertEqual(set(self.session()), {"uid", "sk", "iat"})
        self.assertEqual(self.client.get("/teacher").status_code, 403)

    def test_a_teacher_promoted_after_linking_is_refused_from_then_on(self):
        teacher = self.signed_in("teacher")
        self.link_until_pending(subject="teachers-google")
        self.assert_redirect(self.confirm(), "/account?google=linked")
        self.execute("UPDATE users SET role = 'admin' WHERE id = ?", (teacher["id"],))
        client = self.app.test_client()
        with self.assertLogs("learnpilot.oidc", logging.WARNING):
            self.assert_redirect(self.google_sign_in(client, subject="teachers-google"), "/login?google=not_allowed")
        self.assertEqual(self.session(client), {})


# --------------------------------------------------------------------------- linking


class LinkStartTest(RoutesTestCase):
    def test_it_asks_for_no_password_and_reads_no_form_field(self):
        self.signed_in("teacher")
        started = self.client.post("/auth/google/link", headers=SAME_ORIGIN,
                                   data={"current_password": "ignored", "next": "/teacher", "intent": "signin"})
        self.assertEqual(started.status_code, 303)
        transaction = self.session()[oauth_state.TX_SLOT]
        self.assertEqual((transaction["intent"], transaction["next"]), ("link", ""))
        self.assertEqual(self.query("SELECT COUNT(*) FROM login_throttle"), [(0,)])

    def test_anonymous_is_sent_to_the_sign_in_and_only_a_same_origin_post_starts(self):
        self.assert_redirect(self.start_link(), "/login?next=/auth/google/link")
        self.signed_in("teacher")
        self.assertEqual(self.client.get("/auth/google/link").status_code, 405)
        self.assertEqual(self.client.post("/auth/google/link", headers=CROSS_ORIGIN).status_code, 403)
        self.assertEqual(self.slots(), set())

    def test_an_account_without_a_password_or_with_a_google_identity_is_refused(self):
        self.google_sign_in(subject="google-only")
        self.assert_redirect(self.start_link(), "/account?google=not_allowed")
        page = self.account_page(code="not_allowed")
        self.assertIn(TEXTS["not_allowed"], page)
        self.assertIn("Този профил влиза само с Google. Засега към него не може да се добави парола "
                      "или друг начин за вход.", page)
        self.assertNotIn("Свържи с Google", page)

        client = self.app.test_client()
        teacher = self.signed_in("teacher", client=client)
        self.give_identity(teacher["id"], "already-linked")
        asked = len(self.google.requests)
        self.assert_redirect(self.start_link(client), "/account?google=not_allowed")
        self.assertEqual((self.slots(), self.slots(client), len(self.google.requests)), (set(), set(), asked))

    def test_a_link_callback_writes_no_identity(self):
        teacher = self.signed_in("teacher")
        started = self.start_link()
        transaction = self.session()[oauth_state.TX_SLOT]
        before = table_counts(self.db_path)
        with self.assertLogs("learnpilot.oidc", logging.INFO) as logs:
            returned = self.return_from_google(started, subject="teachers-google")
        self.assert_redirect(returned, "/auth/google/link/confirm")
        # Nothing new in the database but the callback's own record.
        self.assertEqual(table_counts(self.db_path),
                         dict(before, oidc_consumed_transactions=before["oidc_consumed_transactions"] + 1))
        self.assertEqual(self.consumption_rows(),
                         [(self.consumption_key("callback", transaction["state"]), transaction["exp"])])
        pending = self.session()[oauth_state.LINK_SLOT]
        self.assertEqual(set(pending), {"v", "id", "inst", "exp", "uid", "skd", "siat", "sub", "email", "name"})
        self.assertEqual(logs.output, [f"INFO:learnpilot.oidc:callback outcome=pending_link intent=link user={teacher['id']} reason=-"])

    def test_a_link_callback_never_signs_anyone_in(self):
        # The bound session is gone when the browser returns: it does not fall back to a sign-in.
        self.signed_in("teacher")
        started = self.start_link()
        transaction = self.session()[oauth_state.TX_SLOT]
        self.client.post("/logout", headers=SAME_ORIGIN)
        with self.client.session_transaction() as sess:
            sess[oauth_state.TX_SLOT] = transaction  # a copy of the start cookie without the account
        code = self.google.authorize(started.headers["Location"], subject="someone")
        answered = self.callback(code=code, state=transaction["state"])
        self.assert_redirect(answered, "/account?google=failed")
        self.assertEqual((self.session(), self.user_count(), self.identity_rows()), ({}, 1, []))

    def test_a_link_callback_for_another_account_session_is_refused(self):
        first = self.signed_in("teacher")
        started = self.start_link()
        transaction = self.session()[oauth_state.TX_SLOT]
        cases = {"another account": self.make("teacher"), "a later session of the same account": first}
        for name, account in cases.items():
            with self.subTest(case=name):
                client = self.app.test_client()
                set_account_session(client, account["id"], account["session_key"], iat=int(time.time()) - 50)
                with client.session_transaction() as sess:
                    sess[oauth_state.TX_SLOT] = transaction
                with self.assertLogs("learnpilot.oidc", logging.WARNING) as logs:
                    answered = self.callback(client, code="abc", state=transaction["state"])
                self.assert_redirect(answered, "/account?google=failed")
                self.assertIn("reason=not_bound", logs.output[0])
                self.assertNotIn(oauth_state.LINK_SLOT, self.session(client))
                self.execute("DELETE FROM oidc_consumed_transactions")
        self.assertEqual(self.google.requests, [])

    def test_what_the_callback_refuses_before_anyone_is_asked_for_a_password(self):
        teacher = self.signed_in("teacher")
        other = self.make("teacher", email="other@example.bg")
        self.give_identity(other["id"], "others-google")
        cases = (
            ({"subject": "others-google"}, "identity_taken"),
            ({"subject": "fresh", "email": "Other@Example.BG"}, "email_other_account"),   # decision P1
        )
        for identity, code in cases:
            with self.subTest(code=code):
                started = self.start_link()
                with self.assertLogs("learnpilot.oidc", logging.WARNING):
                    answered = self.return_from_google(started, **identity)
                self.assert_redirect(answered, f"/account?google={code}")
                self.assertEqual(self.slots(), set())
                self.assertIn(TEXTS[code], self.account_page(code=code))
        self.assertEqual(self.identity_rows(teacher["id"]), [])
        # The linking account's own address is no objection.
        started = self.start_link()
        self.assert_redirect(self.return_from_google(started, subject="fresh", email=teacher["email"].upper()),
                             "/auth/google/link/confirm")

    def test_an_existing_pending_link_is_retired_by_a_new_start(self):
        self.signed_in("teacher")
        self.link_until_pending(subject="first")
        earlier = self.copy_of()
        first = self.session()[oauth_state.LINK_SLOT]
        started = self.start_link()
        self.assertEqual(started.status_code, 303)
        self.assertNotIn(oauth_state.LINK_SLOT, self.session())
        self.assertIn((self.consumption_key("link-confirm", first["id"]), first["exp"]), self.consumption_rows())
        # A copy of the earlier cookie is refused, with the right password too.
        self.assert_redirect(self.confirm(earlier), "/account?google=failed")
        self.assertEqual(self.identity_rows(), [])
        # The new flow is unaffected.
        self.return_from_google(started, subject="second")
        self.assert_redirect(self.confirm(), "/account?google=linked")

    def test_a_retirement_that_fails_does_not_stop_a_new_start(self):
        for fault_class in RETIREMENT_FAULTS:
            with self.subTest(fault=fault_class.name):
                self.fresh_database()
                client = self.app.test_client()
                self.signed_in("teacher", client=client)
                self.link_until_pending(client, subject="first")
                earlier = self.session(client)[oauth_state.LINK_SLOT]
                with self.failing_retirement(fault_class(self)) as results, \
                        self.assertLogs("learnpilot.oidc", logging.WARNING) as logs:
                    started = self.start_link(client)
                # 303 to Google with a new transaction; the earlier slot is gone; one line.
                self.assertEqual(started.status_code, 303)
                state = self.session(client)
                self.assertNotIn(oauth_state.LINK_SLOT, state)
                self.assertEqual(state[oauth_state.TX_SLOT]["intent"], "link")
                self.assertEqual(results[0].pending, oauth_state.NOT_RECORDED)
                lines = [line for line in logs.output if "retire_not_recorded" in line]
                self.assertEqual(len(lines), 1)
                self.assertNotIn(earlier["id"], "\n".join(logs.output))


class ConfirmationPageTest(RoutesTestCase):
    def test_it_names_both_profiles(self):
        teacher = self.signed_in("teacher", display_name="Петър Учителов")
        self.link_until_pending(subject="teachers-google", email="petar.google@example.bg", name="Петър Г.")
        cookie = self.cookie()
        response = self.client.get("/auth/google/link/confirm")
        self.assertEqual(response.status_code, 200)
        # A GET writes nothing and does not change the cookie.
        self.assertEqual((response.headers.getlist("Set-Cookie"), self.cookie()), ([], cookie))
        page = " ".join(self.html(response).split())
        self.assertIn("Ще свържеш Google профила <strong>Петър Г. (petar.google@example.bg)</strong> с профила в "
                      f"LearnPilot <strong>Петър Учителов ({teacher['email']}, Учител)</strong>.", page)
        self.assertIn("След това всеки, който влезе с този Google профил, ще влиза в този профил в LearnPilot.", page)
        self.assertIn("Ако това не е твоят профил в LearnPilot, натисни „Отказ“ и излез от Google на този компютър.", page)
        self.assertIn("Текуща парола за LearnPilot", page)
        self.assertIn('action="/auth/google/link/confirm"', page)
        self.assertIn('action="/auth/google/link/cancel"', page)
        self.assertRegex(page, r'<button type="submit">Свържи</button>')
        self.assertRegex(page, r'<button type="submit" class="secondary">Отказ</button>')
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        # The subject is not shown: it is an identifier, not a name.
        self.assertNotIn("teachers-google", page)

    def test_a_missing_email_reads_bez_imeyl(self):
        self.signed_in("teacher")
        self.link_until_pending(email="not-an-email", name=7)
        page = " ".join(self.html(self.client.get("/auth/google/link/confirm")).split())
        self.assertIn("<strong>без име (без имейл)</strong>", page)

    def test_a_hostile_google_name_and_email_are_escaped(self):
        self.signed_in("teacher")
        self.link_until_pending(name='<script>alert("n")</script>', email='"><svg/onload=alert(1)>@example.bg')
        raw = self.body(self.client.get("/auth/google/link/confirm"))
        self.assertNotIn("<script>alert", raw)
        self.assertNotIn("<svg/onload", raw)
        self.assertIn("&lt;script&gt;alert(&#34;n&#34;)&lt;/script&gt;", raw)
        self.assertIn("&#34;&gt;&lt;svg/onload=alert(1)&gt;@example.bg", raw)
        # And once it is linked, on the account page.
        self.assert_redirect(self.confirm(), "/account?google=linked")
        raw = self.body(self.client.get("/account"))
        self.assertNotIn("<svg/onload", raw)
        self.assertIn("&#34;&gt;&lt;svg/onload=alert(1)&gt;@example.bg", raw)

    def test_without_a_pending_link_it_answers_expired_and_changes_no_cookie(self):
        self.assert_redirect(self.client.get("/auth/google/link/confirm"), "/login?next=/auth/google/link/confirm")
        self.signed_in("teacher")
        cookie = self.cookie()
        answered = self.client.get("/auth/google/link/confirm")
        self.assert_redirect(answered, "/account?google=expired")
        self.assertEqual((answered.headers.getlist("Set-Cookie"), self.cookie()), ([], cookie))
        self.assertIn(TEXTS["expired"], self.account_page(code="expired"))

    def test_a_pending_link_of_another_session_is_not_this_sessions(self):
        first = self.signed_in("teacher")
        self.link_until_pending()
        pending = self.session()[oauth_state.LINK_SLOT]
        cases = {"another account": self.make("teacher"), "another session of the account": first}
        for name, account in cases.items():
            with self.subTest(case=name):
                client = self.app.test_client()
                set_account_session(client, account["id"], account["session_key"], iat=int(time.time()) - 77)
                with client.session_transaction() as sess:
                    sess[oauth_state.LINK_SLOT] = pending
                self.assert_redirect(client.get("/auth/google/link/confirm"), "/account?google=expired")
                self.assertNotIn(PENDING_NOTICE, self.account_page(client))
                self.assert_redirect(self.confirm(client), "/account?google=expired")
                self.assertNotIn(oauth_state.LINK_SLOT, self.session(client))
        self.assertEqual(self.identity_rows(), [])


class ConfirmTest(RoutesTestCase):
    def setUp(self):
        super().setUp()
        self.teacher = self.signed_in("teacher")

    def test_the_right_password_links_clears_the_counter_and_removes_the_pending_link(self):
        self.link_until_pending(subject="teachers-google", email="Teacher.G@Example.BG")
        self.confirm(password="not the password")
        self.assertEqual(self.failures(self.teacher["email"]), 1)
        pending = self.session()[oauth_state.LINK_SLOT]
        key_before = self.user(self.teacher["id"], "session_key")
        with self.assertLogs("learnpilot.oidc", logging.INFO) as logs:
            done = self.confirm()
        self.assert_redirect(done, "/account?google=linked")
        self.assertEqual(self.identity_rows(),
                         [(self.teacher["id"], ISSUER, "teachers-google", "google", "Teacher.G@Example.BG")])
        self.assertIn((self.consumption_key("link-confirm", pending["id"]), pending["exp"]), self.consumption_rows())
        self.assertEqual(self.failures(self.teacher["email"]), 0)
        # A link changes no session key and issues no session: only the pending link goes.
        state = self.session()
        self.assertEqual(set(state), {"uid", "sk", "iat"})
        self.assertEqual((state["sk"], self.user(self.teacher["id"], "session_key")), (key_before, key_before))
        self.assertEqual(logs.output, [f"INFO:learnpilot.oidc:link_confirm outcome=linked user={self.teacher['id']} reason=-"])
        page = self.account_page(code="linked")
        self.assertIn(TEXTS["linked"], page)
        self.assertIn("Google — Teacher.G@Example.BG", page)
        self.assertIn("Изходът от LearnPilot не те отписва от Google.", page)
        self.assertNotIn("Свържи с Google", page)

    def test_a_wrong_password_keeps_the_pending_link_and_counts_one_failure(self):
        self.link_until_pending()
        pending = self.session()[oauth_state.LINK_SLOT]
        rows = self.consumption_rows()
        for password in ("not the password", "", "x" * 257):   # an over-long one counts as wrong
            with self.subTest(password=password[:20]):
                failures = self.failures(self.teacher["email"])
                answered = self.confirm(password=password)
                self.assertEqual(answered.status_code, 200)
                page = self.html(answered)
                self.assertIn("Грешна текуща парола.", page)
                self.assertNotIn(password or "placeholder-not-in-page", page)
                self.assertEqual(self.failures(self.teacher["email"]), failures + 1)
                self.assertEqual(self.session()[oauth_state.LINK_SLOT], pending)
        self.assertEqual((self.identity_rows(), self.consumption_rows()), ([], rows))
        self.assert_redirect(self.confirm(), "/account?google=linked")

    def test_an_over_long_password_is_not_hashed(self):
        self.link_until_pending()
        with patch.object(accounts, "check_password_hash") as verify:
            self.confirm(password="x" * 257)
        verify.assert_not_called()

    def test_the_counter_is_shared_with_the_sign_in_and_the_password_forms(self):
        self.link_until_pending()
        self.confirm(password="wrong one")
        self.login(self.teacher["email"], "wrong two", client=self.app.test_client())
        self.client.post("/account/password", headers=SAME_ORIGIN, data={
            "current_password": "wrong three", "new_password": NEW_PASSWORD, "confirm_password": NEW_PASSWORD})
        self.assertEqual(self.failures(self.teacher["email"]), 3)
        self.assertEqual(len(self.throttle_rows()), 1)

    def test_a_blocked_counter_answers_429_and_hashes_nothing(self):
        self.link_until_pending()
        self.fail_login(self.teacher["email"], 10)
        pending = self.session()[oauth_state.LINK_SLOT]
        with patch.object(accounts, "check_password_hash") as verify:
            blocked = self.confirm()
        verify.assert_not_called()
        self.assertEqual(blocked.status_code, 429)
        self.assertIn(THROTTLED, self.html(blocked))
        self.assertEqual((self.session()[oauth_state.LINK_SLOT], self.identity_rows()), (pending, []))
        # The tenth wrong confirmation blocks too.
        self.execute("DELETE FROM login_throttle")
        with patch.object(accounts, "check_password_hash", return_value=False):
            for _ in range(10):
                self.confirm(password="wrong")
            self.assertEqual(self.confirm(password="wrong").status_code, 429)
        self.assertEqual(self.login(self.teacher["email"], client=self.app.test_client()).status_code, 429)

    def test_only_a_same_origin_post_confirms(self):
        self.link_until_pending()
        forged = self.confirm(headers=CROSS_ORIGIN)
        self.assertEqual(forged.status_code, 403)
        self.assertEqual((self.identity_rows(), self.failures(self.teacher["email"])), ([], 0))
        self.assertIn(oauth_state.LINK_SLOT, self.session())

    def test_a_second_confirmation_stores_nothing_more(self):
        self.link_until_pending()
        copy = self.copy_of()
        self.assert_redirect(self.confirm(), "/account?google=linked")
        rows = self.identity_rows(), self.consumption_rows()
        # The same browser no longer holds the link; a copy of the cookie is told that it is linked.
        self.assert_redirect(self.confirm(), "/account?google=expired")
        with self.assertLogs("learnpilot.oidc", logging.WARNING):
            self.assert_redirect(self.confirm(copy), "/account?google=already_linked")
        self.assertEqual((self.identity_rows(), self.consumption_rows()), rows)
        self.assertIn(TEXTS["already_linked"], self.account_page(copy, "already_linked"))

    def test_what_changed_between_the_callback_and_the_confirmation_is_caught_at_the_write(self):
        other = self.make("teacher", email="other@example.bg")
        cases = {
            "identity_taken": lambda: self.give_identity(other["id"], "contested"),
            "email_other_account": lambda: self.execute(
                "UPDATE users SET email = 'contested@example.bg' WHERE id = ?", (other["id"],)),
            "not_allowed": lambda: self.give_identity(self.teacher["id"], "another-of-mine"),
        }
        for code, change in cases.items():
            with self.subTest(code=code):
                self.execute("DELETE FROM user_identities")
                self.execute("UPDATE users SET email = 'other@example.bg' WHERE id = ?", (other["id"],))
                self.link_until_pending(subject="contested", email="contested@example.bg")
                pending = self.session()[oauth_state.LINK_SLOT]
                change()
                with self.assertLogs("learnpilot.oidc", logging.WARNING):
                    self.assert_redirect(self.confirm(), f"/account?google={code}")
                # Refused after the record: the pending link is spent and nothing is linked.
                self.assertNotIn(oauth_state.LINK_SLOT, self.session())
                self.assertIn((self.consumption_key("link-confirm", pending["id"]), pending["exp"]),
                              self.consumption_rows())
                self.assertNotIn((self.teacher["id"], ISSUER, "contested", "google", "contested@example.bg"),
                                 self.identity_rows())
                self.assertIn(TEXTS[code], self.account_page(code=code))


class ConfirmFailureTest(RoutesTestCase):
    """A confirmation that was rolled back keeps the pending link (design 1.6)."""

    def setUp(self):
        super().setUp()
        self.teacher = self.signed_in("teacher")
        self.link_until_pending(subject="teachers-google")
        self.pending = self.session()[oauth_state.LINK_SLOT]
        # A committed failure, so that "the counter as it was" can be told from "cleared".
        with patch.object(accounts, "check_password_hash", return_value=False):
            self.confirm(password="wrong")
        self.assertEqual(self.failures(self.teacher["email"]), 1)
        self.rows = self.consumption_rows()

    def assert_nothing_was_written_and_the_link_is_kept(self, answered, code):
        self.assert_redirect(answered, f"/account?google={code}")
        self.assertEqual((self.identity_rows(), self.consumption_rows()), ([], self.rows))
        self.assertEqual(self.failures(self.teacher["email"]), 1)
        self.assertEqual(self.session()[oauth_state.LINK_SLOT], self.pending)
        # The account page still offers the confirmation.
        page = self.account_page(code=code)
        self.assertIn(PENDING_NOTICE, page)
        self.assertIn('href="/auth/google/link/confirm"', page)
        self.assertNotIn(TEXTS["linked"], page)

    def assert_a_second_post_links(self):
        self.assert_redirect(self.confirm(), "/account?google=linked")
        self.assertEqual(len(self.identity_rows(self.teacher["id"])), 1)
        self.assertEqual(self.failures(self.teacher["email"]), 0)

    def test_a_failure_inside_the_transaction(self):
        key = self.consumption_key("link-confirm", self.pending["id"])
        fault = FailingInsert(self, key)
        with fault, self.assertLogs("learnpilot.oidc", logging.WARNING) as logs:
            answered = self.confirm()
        self.assert_nothing_was_written_and_the_link_is_kept(answered, "unavailable")
        self.assertEqual((len(fault.executed("ROLLBACK")), len(fault.executed("COMMIT"))), (1, 0))
        self.assertEqual(logs.output, [
            f"WARNING:learnpilot.oidc:link_confirm outcome=unavailable user={self.teacher['id']} reason=IntegrityError"])
        self.assert_a_second_post_links()

    def test_an_error_that_is_no_database_error_answers_failed(self):
        with patch.object(oidc_accounts, "confirm_link", side_effect=RuntimeError("the id is SECRET-TEXT")), \
                self.assertLogs("learnpilot.oidc", logging.WARNING) as logs:
            answered = self.confirm()
        self.assert_nothing_was_written_and_the_link_is_kept(answered, "failed")
        self.assertIn("reason=RuntimeError", logs.output[0])
        self.assertNotIn("SECRET-TEXT", logs.output[0])
        self.assert_a_second_post_links()

    def test_a_begin_immediate_that_fails_issues_neither_a_commit_nor_a_rollback(self):
        for fault_class in (HeldLock, QueryOnly):
            with self.subTest(fault=fault_class.name):
                fault = fault_class(self)
                with fault, self.assertLogs("learnpilot.oidc", logging.WARNING) as logs:
                    answered = self.confirm()
                self.assert_nothing_was_written_and_the_link_is_kept(answered, "unavailable")
                self.assertEqual(fault.executed("BEGIN"), ["BEGIN IMMEDIATE"])
                self.assertEqual((fault.executed("COMMIT"), fault.executed("ROLLBACK")), ([], []))
                self.assertIn("reason=OperationalError", logs.output[0])
        self.assert_a_second_post_links()

    def test_a_read_only_database_file_fails_at_the_clearing_of_the_counter(self):
        fault = ReadOnlyFile(self)
        with fault, self.assertLogs("learnpilot.oidc", logging.WARNING):
            answered = self.confirm()
        # The route owned its transaction by then, and rolls it back once.
        writes = [s for s in fault.statements if not s.lstrip().upper().startswith(("SELECT", "PRAGMA"))]
        self.assertEqual(writes[0], "BEGIN IMMEDIATE")
        self.assertTrue(writes[1].startswith("DELETE FROM login_throttle"))
        self.assertEqual(writes[2:], ["ROLLBACK"])
        self.assert_nothing_was_written_and_the_link_is_kept(answered, "unavailable")
        self.assert_a_second_post_links()

    def test_a_commit_that_fails(self):
        for fault_class in (FailingCommit, CommitBlockedByReader):
            with self.subTest(fault=fault_class.name):
                fault = fault_class(self)
                with fault, self.assertLogs("learnpilot.oidc", logging.WARNING) as logs:
                    answered = self.confirm()
                # One ROLLBACK after the failed COMMIT, and no second COMMIT.
                self.assertEqual(len(fault.executed("ROLLBACK")), 1)
                self.assertLessEqual(len(fault.executed("COMMIT")), 1)
                self.assertEqual(fault.statements[-1], "ROLLBACK")
                self.assertTrue(any(s.startswith("INSERT INTO user_identities") for s in fault.statements))
                self.assert_nothing_was_written_and_the_link_is_kept(answered, "unavailable")
                self.assertIn("reason=OperationalError", logs.output[0])
        self.assert_a_second_post_links()

    def test_a_rollback_that_fails_as_well(self):
        fault = FailingCommitAndRollback(self)
        with fault, self.assertLogs("learnpilot.oidc", logging.WARNING) as logs:
            answered = self.confirm()   # nothing is raised
            executed = len(fault.statements)
        self.assertIn("WARNING:learnpilot.oidc:rollback_failed error=OperationalError", logs.output)
        # Nothing further was executed on that connection, and it took its open
        # transaction with it: no identity and no record.
        self.assertTrue(fault.statements[executed - 1].startswith("INSERT INTO user_identities"))
        self.assert_nothing_was_written_and_the_link_is_kept(answered, "unavailable")
        self.assert_a_second_post_links()

    def test_a_commit_that_took_effect_and_still_reported_an_error(self):
        with CommitThenError(self), self.assertLogs("learnpilot.oidc", logging.WARNING):
            answered = self.confirm()
        # Never "linked", and it does not say that nothing was linked either.
        self.assert_redirect(answered, "/account?google=unavailable")
        self.assertEqual(self.session()[oauth_state.LINK_SLOT], self.pending)
        page = self.account_page(code="unavailable")
        self.assertNotIn(TEXTS["linked"], page)
        # The truth is read from the database by the next request.
        self.assertIn("Google — pupil@example.bg", page)
        self.assertEqual(len(self.identity_rows(self.teacher["id"])), 1)
        with self.assertLogs("learnpilot.oidc", logging.WARNING):
            self.assert_redirect(self.confirm(), "/account?google=already_linked")
        self.assertEqual(len(self.identity_rows(self.teacher["id"])), 1)
        self.assertNotIn(oauth_state.LINK_SLOT, self.session())

    def test_an_older_python_reports_a_repeated_key_as_unavailable(self):
        with patch.object(oidc_consumption, "consume", side_effect=sqlite3.IntegrityError("UNIQUE constraint failed")), \
                self.assertLogs("learnpilot.oidc", logging.WARNING):
            answered = self.confirm()
        self.assert_nothing_was_written_and_the_link_is_kept(answered, "unavailable")


# --------------------------------------------------------------------------- cancelling


class CancelTest(RoutesTestCase):
    def setUp(self):
        super().setUp()
        self.teacher = self.signed_in("teacher")

    def test_a_clean_cancellation_of_a_pending_link(self):
        self.link_until_pending()
        pending = self.session()[oauth_state.LINK_SLOT]
        earlier = self.copy_of()
        page = self.assert_cancel(self.cancel(), "link_cancelled")
        self.assertNotIn(PENDING_NOTICE, page)
        self.assertIn((self.consumption_key("link-confirm", pending["id"]), pending["exp"]), self.consumption_rows())
        # A replay of the earlier cookie is refused, with the right password too.
        self.assert_redirect(self.confirm(earlier), "/account?google=failed")
        self.assertEqual(self.identity_rows(), [])

    def test_a_clean_cancellation_of_a_link_transaction(self):
        started = self.start_link()
        transaction = self.session()[oauth_state.TX_SLOT]
        earlier = self.copy_of()
        self.assert_cancel(self.cancel(), "link_cancelled")
        self.assertEqual(self.consumption_rows(),
                         [(self.consumption_key("callback", transaction["state"]), transaction["exp"])])
        # The copy's callback is refused, and no pending link is created.
        refused = self.return_from_google(started, earlier)
        self.assert_redirect(refused, "/account?google=failed")
        self.assertEqual((self.slots(earlier), self.google.requests), (set(), []))

    def test_only_a_same_origin_post_cancels(self):
        self.link_until_pending()
        self.assertEqual(self.client.get("/auth/google/link/cancel").status_code, 405)
        self.assertEqual(self.client.post("/auth/google/link/cancel", headers=CROSS_ORIGIN).status_code, 403)
        self.assertIn(oauth_state.LINK_SLOT, self.session())
        self.assert_redirect(self.app.test_client().post("/auth/google/link/cancel", headers=SAME_ORIGIN),
                             "/login?next=/auth/google/link/cancel")

    def test_two_tabs_the_first_confirmed(self):
        # (a) The session holds no slot any more. An empty slot decides nothing: the
        # read of the account does.
        self.link_until_pending()
        self.assert_redirect(self.confirm(), "/account?google=linked")
        rows = self.identity_rows(), self.consumption_rows()
        with self.failing_retirement(Tracing(self)) as results, \
                patch.object(oauth_state, "get_request_db", wraps=oauth_state.get_request_db) as accessor:
            answered = self.cancel()
        accessor.assert_not_called()
        self.assertEqual((results[0].transaction, results[0].pending), (oauth_state.NONE, oauth_state.NONE))
        page = self.assert_cancel(answered, "account_linked")
        self.assertIn("Google — pupil@example.bg", page)
        self.assertEqual((self.identity_rows(), self.consumption_rows()), rows)

    def test_a_stale_pending_link_is_cancelled_after_it_was_confirmed(self):
        # (b) The cookie that reaches the cancel route still holds the confirmed link.
        self.link_until_pending()
        stale = self.copy_of()
        self.assert_redirect(self.confirm(), "/account?google=linked")
        rows = self.identity_rows(), self.consumption_rows()
        with self.failing_retirement(Tracing(self)) as results:
            answered = self.cancel(stale)
        self.assertEqual(results[0].pending, oauth_state.CONSUMED)
        self.assert_cancel(answered, "account_linked", stale)
        self.assertEqual((self.identity_rows(), self.consumption_rows()), rows)

    def test_a_confirmation_and_a_cancellation_at_once_never_end_as_linked_and_told_cancelled(self):
        for attempt in range(6):
            with self.subTest(attempt=attempt):
                self.fresh_database()
                self.client = self.app.test_client()
                teacher = self.signed_in("teacher")
                self.link_until_pending()
                other = self.copy_of()
                barrier = threading.Barrier(2)

                def run(action):
                    barrier.wait(10)
                    return action()

                with ThreadPoolExecutor(max_workers=2) as pool, self.assertLogs("learnpilot.oidc", logging.INFO):
                    confirmation = pool.submit(run, self.confirm if attempt % 2 else lambda: self.confirm(other))
                    cancellation = pool.submit(run, (lambda: self.cancel(other)) if attempt % 2 else self.cancel)
                    confirmed = confirmation.result(30).headers["Location"]
                    cancelled = cancellation.result(30).headers["Location"]
                linked = len(self.identity_rows(teacher["id"])) == 1
                self.assertEqual(linked, confirmed == "/account?google=linked")
                if linked:
                    self.assertEqual(cancelled, "/account?google=account_linked")
                else:
                    self.assertEqual(cancelled, "/account?google=link_cancelled")
                    self.assertEqual(confirmed, "/account?google=failed")

    def test_a_confirmation_that_recorded_the_use_and_stored_nothing(self):
        # (b') The object is consumed, the account has no identity: nothing is established.
        other = self.make("teacher", email="other@example.bg")
        cases = {
            "identity_taken": lambda: self.give_identity(other["id"], "contested"),
            "email_other_account": lambda: self.execute(
                "UPDATE users SET email = 'contested@example.bg' WHERE id = ?", (other["id"],)),
        }
        for code, change in cases.items():
            with self.subTest(refused=code):
                self.execute("DELETE FROM user_identities")
                self.link_until_pending(subject="contested", email="contested@example.bg")
                stale = self.copy_of()
                change()
                with self.assertLogs("learnpilot.oidc", logging.WARNING):
                    self.assert_redirect(self.confirm(), f"/account?google={code}")
                rows = self.consumption_rows()
                with self.failing_retirement(Tracing(self)) as results:
                    answered = self.cancel(stale)
                self.assertEqual(results[0].pending, oauth_state.CONSUMED)
                self.assert_cancel(answered, "link_not_pending", stale)
                self.assertEqual(self.consumption_rows(), rows)

    def test_a_copy_of_the_start_cookie_reached_the_callback_first(self):
        # (c) The original session still holds the transaction, and its key is recorded.
        started = self.start_link()
        copy = self.copy_of()
        self.assert_redirect(self.return_from_google(started, copy), "/auth/google/link/confirm")
        self.assertIn(oauth_state.TX_SLOT, self.session())
        with self.failing_retirement(Tracing(self)) as results:
            answered = self.cancel()
        self.assertEqual(results[0].transaction, oauth_state.CONSUMED)
        self.assert_cancel(answered, "link_not_pending")

    def test_the_account_cannot_be_read(self):
        # (d) Never "cancelled", also when the retirement invalidated the object.
        for name, prepare in (("a live pending link", self.link_until_pending), ("an empty session", lambda: None)):
            with self.subTest(session=name):
                prepare()
                with patch.object(identities, "list_identities", side_effect=sqlite3.OperationalError("SECRET-TEXT")), \
                        self.failing_retirement(Tracing(self)) as results, \
                        self.assertLogs("learnpilot.oidc", logging.WARNING) as logs:
                    answered = self.cancel()
                expected = oauth_state.INVALIDATED if name == "a live pending link" else oauth_state.NONE
                self.assertEqual(results[0].pending, expected)
                self.assert_cancel(answered, "cancel_unconfirmed")
                self.assertEqual(logs.output, [
                    f"WARNING:learnpilot.oidc:cancel_account_read_failed user={self.teacher['id']} error=OperationalError"])

    def test_an_empty_session_and_a_second_cancellation_are_not_pending(self):
        self.assert_cancel(self.cancel(), "link_not_pending")
        self.link_until_pending()
        self.assert_cancel(self.cancel(), "link_cancelled")
        # A second cancellation after any first one never says "cancelled".
        self.assert_cancel(self.cancel(), "link_not_pending")
        self.link_until_pending()
        self.confirm()
        self.assert_cancel(self.cancel(), "account_linked")
        self.assert_cancel(self.cancel(), "account_linked")

    def test_a_dead_pending_link_is_not_pending(self):
        cases = {
            "malformed": lambda pending: dict(pending, v=2),
            "another instance": lambda pending: dict(pending, inst="0" * 32),
            "expired": lambda pending: pending,
        }
        for name, change in cases.items():
            with self.subTest(case=name):
                self.clock = 5000
                self.link_until_pending()
                with self.client.session_transaction() as sess:
                    sess[oauth_state.LINK_SLOT] = change(sess[oauth_state.LINK_SLOT])
                if name == "expired":
                    self.clock += 180
                rows = self.consumption_rows()
                with self.failing_retirement(Tracing(self)) as results:
                    answered = self.cancel()
                self.assertEqual(results[0].pending, oauth_state.DEAD)
                self.assert_cancel(answered, "link_not_pending")
                self.assertEqual(self.consumption_rows(), rows)

    def test_another_session_of_the_account_linked_meanwhile(self):
        # Row 2 comes before row 4: the object is invalidated, and the link is what
        # the person has to know.
        other_browser = self.app.test_client()
        set_account_session(other_browser, self.teacher["id"], self.teacher["session_key"])
        self.link_until_pending(subject="mine")
        self.link_until_pending(other_browser, subject="theirs")
        mine = self.session()[oauth_state.LINK_SLOT]
        self.assert_redirect(self.confirm(other_browser), "/account?google=linked")
        with self.failing_retirement(Tracing(self)) as results:
            answered = self.cancel()
        self.assertEqual(results[0].pending, oauth_state.INVALIDATED)
        self.assertIn((self.consumption_key("link-confirm", mine["id"]), mine["exp"]), self.consumption_rows())
        self.assert_cancel(answered, "account_linked")


class CancelNotRecordedTest(RoutesTestCase):
    """Row 3 of design 1.6: the record could not be written."""

    def test_every_storage_failure_answers_cancel_unconfirmed(self):
        for fault_class in RETIREMENT_FAULTS:
            with self.subTest(fault=fault_class.name):
                self.fresh_database()
                self.client = self.app.test_client()
                self.signed_in("teacher")
                self.link_until_pending()
                pending = self.session()[oauth_state.LINK_SLOT]
                with self.failing_retirement(fault_class(self)) as results, \
                        self.assertLogs("learnpilot.oidc", logging.WARNING) as logs:
                    answered = self.cancel()
                self.assertEqual(results[0].pending, oauth_state.NOT_RECORDED)
                # Never "cancelled", and not the sign-in text of "unavailable" either.
                page = self.assert_cancel(answered, "cancel_unconfirmed")
                self.assertNotIn(TEXTS["unavailable"], page)
                self.assertNotIn(PENDING_NOTICE, page)
                lines = [line for line in logs.output if "retire_not_recorded" in line]
                self.assertEqual(len(lines), 1)
                self.assertNotIn(pending["id"], "\n".join(logs.output))
                self.assertEqual(self.identity_rows(), [])

    def test_with_a_google_identity_on_the_account_it_answers_account_linked_and_logs_all_the_same(self):
        for fault_class in RETIREMENT_FAULTS:
            with self.subTest(fault=fault_class.name):
                self.fresh_database()
                self.client = self.app.test_client()
                teacher = self.signed_in("teacher")
                self.link_until_pending(subject="mine")
                self.give_identity(teacher["id"], "linked-from-elsewhere")
                with self.failing_retirement(fault_class(self)) as results, \
                        self.assertLogs("learnpilot.oidc", logging.WARNING) as logs:
                    answered = self.cancel()
                self.assertEqual(results[0].pending, oauth_state.NOT_RECORDED)
                self.assert_cancel(answered, "account_linked")
                self.assertEqual(len([line for line in logs.output if "retire_not_recorded" in line]), 1)

    def test_a_copy_of_the_cookie_stays_usable_and_the_person_was_not_told_otherwise(self):
        teacher = self.signed_in("teacher")
        self.link_until_pending()
        copy = self.copy_of()
        with self.failing_retirement(HeldLock(self)), self.assertLogs("learnpilot.oidc", logging.WARNING):
            self.assert_cancel(self.cancel(), "cancel_unconfirmed")
        # Deleting the slots revoked nothing: the stated residual.
        self.assert_redirect(self.confirm(copy), "/account?google=linked")
        self.assertEqual(len(self.identity_rows(teacher["id"])), 1)


class CancelRowOrderTest(RoutesTestCase):
    """The first row of design 1.6 that applies, over every combination."""

    OUTCOMES = (oauth_state.NONE, oauth_state.INVALIDATED, oauth_state.CONSUMED, oauth_state.DEAD,
                oauth_state.NOT_RECORDED)

    def expected(self, transaction, pending, read):
        if read == "error":
            return "cancel_unconfirmed"                                    # row 1
        if read == "linked":
            return "account_linked"                                        # row 2
        both = (transaction, pending)
        if oauth_state.NOT_RECORDED in both:
            return "cancel_unconfirmed"                                    # row 3
        if oauth_state.INVALIDATED in both and set(both) <= {oauth_state.INVALIDATED, oauth_state.NONE}:
            return "link_cancelled"                                        # row 4
        return "link_not_pending"                                          # row 5

    def test_the_decision_table(self):
        reads = {"linked": True, "not linked": False, "error": None}
        cancelled = []
        for transaction, pending, (read, value) in product(self.OUTCOMES, self.OUTCOMES, reads.items()):
            with self.subTest(transaction=transaction, pending=pending, read=read):
                code = oidc_blueprint.cancel_code(oauth_state.Retirement(transaction, pending), value)
                self.assertEqual(code, self.expected(transaction, pending, read))
                if code == "link_cancelled":
                    cancelled.append((transaction, pending, read))
        # "Cancelled" in exactly the combinations of row 4.
        self.assertEqual(sorted(cancelled), sorted([
            (oauth_state.INVALIDATED, oauth_state.NONE, "not linked"),
            (oauth_state.NONE, oauth_state.INVALIDATED, "not linked"),
            (oauth_state.INVALIDATED, oauth_state.INVALIDATED, "not linked"),
        ]))

    def test_the_route_answers_by_the_table_and_reads_the_account_after_the_retirement(self):
        self.signed_in("teacher")
        reads = {"linked": lambda: True, "not linked": lambda: False,
                 "error": lambda: (_ for _ in ()).throw(sqlite3.OperationalError("unavailable"))}
        for transaction, pending, (read, behaviour) in product(self.OUTCOMES, self.OUTCOMES, reads.items()):
            with self.subTest(transaction=transaction, pending=pending, read=read):
                order = []

                def retire(transaction=transaction, pending=pending):
                    order.append("retire")
                    return oauth_state.Retirement(transaction, pending)

                def has_identity(conn, user_id, behaviour=behaviour):
                    order.append("read")
                    return behaviour()

                with patch.object(oauth_state, "retire_linking", retire), \
                        patch.object(oidc_accounts, "has_google_identity", has_identity), \
                        self.assertLogs("learnpilot.oidc", logging.WARNING) if read == "error" else self.assertNoLogs():
                    answered = self.cancel()
                self.assert_redirect(answered, f"/account?google={self.expected(transaction, pending, read)}")
                self.assertEqual(order, ["retire", "read"])

    def test_the_exact_texts_of_a_cancellation(self):
        teacher = self.signed_in("teacher")
        self.give_identity(teacher["id"], "mine", "mine@example.bg")
        for code in CANCEL_CODES:
            with self.subTest(code=code):
                page = self.account_page(code=code)
                for other in CANCEL_CODES:
                    (self.assertIn if other == code else self.assertNotIn)(TEXTS[other], page)
        # Below "already linked" the page lists the identity.
        page = self.account_page(code="account_linked")
        self.assertLess(page.index(TEXTS["account_linked"]), page.index("Google — mine@example.bg"))


# --------------------------------------------------------------------------- sign-out and change of account


class SignOutTest(RoutesTestCase):
    def test_it_retires_linking_in_progress_and_lands_on_the_home_page_for_every_account(self):
        # Decision P3: the home page, for every account.
        for role in ("student", "teacher"):
            with self.subTest(role=role):
                client = self.app.test_client()
                self.signed_in(role, client=client)
                self.link_until_pending(client, subject=f"{role}-google")
                pending = self.session(client)[oauth_state.LINK_SLOT]
                copy = self.copy_of(client)
                answered = client.post("/logout", headers=SAME_ORIGIN)
                self.assert_redirect(answered, "/")
                self.assertEqual(self.session(client), {})
                self.assertIn((self.consumption_key("link-confirm", pending["id"]), pending["exp"]),
                              self.consumption_rows())
                # A copy of the earlier cookie is refused.
                self.assert_redirect(self.confirm(copy), "/account?google=failed")
        admin = self.make("admin")
        set_account_session(self.client, admin["id"], admin["session_key"])
        self.assert_redirect(self.client.post("/logout", headers=SAME_ORIGIN), "/")
        self.assertEqual(self.identity_rows(), [])

    def test_a_sign_out_while_the_browser_is_at_google(self):
        self.signed_in("teacher")
        started = self.start_link()
        copy = self.copy_of()
        self.client.post("/logout", headers=SAME_ORIGIN)
        refused = self.return_from_google(started, copy)
        self.assert_redirect(refused, "/account?google=failed")
        self.assertEqual((self.slots(copy), self.google.requests), (set(), []))

    def test_with_the_provider_disabled_it_behaves_exactly_as_before(self):
        self.signed_in("teacher")
        self.link_until_pending()
        rows = self.consumption_rows()
        with patch.dict(self.app.config, {oauth_state.PROVIDER_CONFIG_KEY: None}), \
                patch.object(oauth_state, "get_request_db", wraps=oauth_state.get_request_db) as accessor, \
                self.assertNoLogs("learnpilot.oidc"):
            answered = self.client.post("/logout", headers=SAME_ORIGIN)
        self.assert_redirect(answered, "/")
        # Retirement did not take the request's connection, and nothing was retired.
        accessor.assert_not_called()
        self.assertEqual((self.session(), self.consumption_rows()), ({}, rows))

    def test_a_sign_out_without_linking_takes_no_connection_for_retiring(self):
        self.signed_in("teacher")
        with patch.object(oauth_state, "get_request_db", wraps=oauth_state.get_request_db) as accessor:
            self.assert_redirect(self.client.post("/logout", headers=SAME_ORIGIN), "/")
        accessor.assert_not_called()
        self.assertEqual(self.consumption_rows(), [])

    def test_not_recorded_differs_from_an_ordinary_sign_out_by_one_log_line_only(self):
        self.signed_in("teacher")
        plain = self.client.post("/logout", headers=SAME_ORIGIN)
        for fault_class in RETIREMENT_FAULTS:
            with self.subTest(fault=fault_class.name):
                self.fresh_database()
                client = self.app.test_client()
                self.signed_in("teacher", client=client)
                self.link_until_pending(client)
                with self.failing_retirement(fault_class(self)) as results, \
                        self.assertLogs("learnpilot.oidc", logging.WARNING) as logs:
                    answered = client.post("/logout", headers=SAME_ORIGIN)
                self.assertEqual(results[0].pending, oauth_state.NOT_RECORDED)
                # The same redirect, with no message and no error page.
                self.assertEqual((answered.status_code, answered.headers["Location"], answered.get_data()),
                                 (plain.status_code, plain.headers["Location"], plain.get_data()))
                self.assertEqual(self.session(client), {})
                self.assertEqual(len(logs.output), 1)
                self.assertIn("retire_not_recorded", logs.output[0])


class ConsumedOrDeadObjectTest(RoutesTestCase):
    """A sign-out, a new link and a change of account claim nothing about the link they found."""

    def session_with(self, kind):
        client = self.app.test_client()
        account = self.signed_in("teacher", client=client)
        self.link_until_pending(client)
        if kind == "consumed":
            self.cancel(self.copy_of(client))          # used elsewhere; this cookie still holds it
        else:
            self.clock += 180                          # expired
        return client, account

    def test_each_goes_on_exactly_as_with_an_empty_session_and_logs_nothing(self):
        other = self.make("student")
        actions = {
            "sign-out": (lambda client: client.post("/logout", headers=SAME_ORIGIN), "/", 302),
            "a new link": (lambda client: self.start_link(client), None, 303),
            "a change of account": (lambda client: self.login(other["email"], client=client), "/", 302),
        }
        for kind in ("consumed", "dead"):
            for name, (action, location, status) in actions.items():
                with self.subTest(object=kind, action=name):
                    self.clock = 5000
                    client, _ = self.session_with(kind)
                    rows = self.consumption_rows()
                    with self.failing_retirement(Tracing(self)) as results, self.assertNoLogs("learnpilot.oidc"):
                        answered = action(client)
                    self.assertEqual(results[0].pending, kind)
                    self.assertEqual(answered.status_code, status)
                    if location:
                        self.assertEqual(answered.headers["Location"], location)
                    self.assertNotIn(oauth_state.LINK_SLOT, self.session(client))
                    self.assertEqual(self.consumption_rows(), rows)


class ChangeOfAccountNotRecordedTest(RoutesTestCase):
    """A sign-in or a password change is neither failed nor undone because a record
    could not be written (design 4.4)."""

    def linking_client(self):
        client = self.app.test_client()
        account = self.signed_in("teacher", client=client)
        self.link_until_pending(client)
        return client, account

    def run_with_every_fault(self, prepare, act, check):
        for fault_class in RETIREMENT_FAULTS:
            with self.subTest(fault=fault_class.name):
                self.fresh_database()
                self.now += timedelta(minutes=1)
                context = prepare()
                with self.failing_retirement(fault_class(self)) as results, \
                        self.assertLogs("learnpilot.oidc", logging.WARNING) as logs:
                    answered = act(context)
                self.assertEqual(len(results), 1)
                self.assertEqual(results[0].pending, oauth_state.NOT_RECORDED)
                self.assertEqual(len([line for line in logs.output if "retire_not_recorded" in line]), 1)
                check(context, answered)

    def test_a_password_sign_in_as_another_account(self):
        def prepare():
            client, _ = self.linking_client()
            other = self.make("student")
            self.login(other["email"], "wrong password", client=self.app.test_client())  # a counter to clear
            return client, other

        def check(context, answered):
            client, other = context
            self.assert_redirect(answered, "/")
            state = self.session(client)
            self.assertEqual((set(state), state["uid"]), ({"uid", "sk", "iat"}, other["id"]))
            # What the route committed before it retired is in the database.
            self.assertIsNotNone(self.user(other["id"], "last_login_at"))
            self.assertEqual(self.failures(other["email"]), 0)

        self.run_with_every_fault(prepare, lambda context: self.login(context[1]["email"], client=context[0]), check)

    def test_an_admins_password_step(self):
        def prepare():
            client, _ = self.linking_client()
            return client, self.make("admin")

        def check(context, answered):
            client, _ = context
            self.assertEqual(answered.status_code, 200)
            self.assertIn('action="/login/totp"', self.body(answered))
            self.assertEqual(set(self.session(client)), {"mfa_uid", "mfa_sk", "mfa_iat"})

        self.run_with_every_fault(prepare, lambda context: self.login(context[1]["email"], client=context[0]), check)

    def test_a_completed_second_factor(self):
        def prepare():
            client = self.app.test_client()
            admin = self.make("admin")
            self.now += timedelta(seconds=31)
            self.assertEqual(self.login(admin["email"], client=client).status_code, 200)
            with client.session_transaction() as sess:
                # Linking in progress beside the pending second factor.
                sess[oauth_state.LINK_SLOT] = {
                    "v": 1, "id": "p" * 43, "inst": oauth_state.INSTANCE, "exp": self.clock + 100, "uid": admin["id"],
                    "skd": "a" * 64, "siat": 1_700_000_000, "sub": "someone", "email": "", "name": ""}
            return client, admin

        def check(context, answered):
            client, admin = context
            self.assert_redirect(answered, "/teacher")
            state = self.session(client)
            self.assertEqual((set(state), state["uid"]), ({"uid", "sk", "iat"}, admin["id"]))
            self.assertIsNotNone(self.user(admin["id"], "last_login_at"))
            self.assertEqual(client.get("/teacher").status_code, 200)

        self.run_with_every_fault(
            prepare, lambda context: self.submit_code(self.code(context[1]), client=context[0]), check)

    def test_a_password_change(self):
        def prepare():
            client, account = self.linking_client()
            return client, account, self.user(account["id"], "password_hash")

        def act(context):
            return context[0].post("/account/password", headers=SAME_ORIGIN, data={
                "current_password": PASSWORD, "new_password": NEW_PASSWORD, "confirm_password": NEW_PASSWORD})

        def check(context, answered):
            client, account, old_hash = context
            self.assertEqual(answered.status_code, 200)
            self.assertIn("Паролата е сменена", self.body(answered))
            new_key = self.user(account["id"], "session_key")
            # The new password and its new session key are in the database.
            self.assertNotEqual((new_key, self.user(account["id"], "password_hash")), (account["session_key"], old_hash))
            state = self.session(client)
            self.assertEqual((set(state), state["sk"]), ({"uid", "sk", "iat"}, new_key))
            self.assertEqual(client.get("/teacher/new").status_code, 200)

        self.run_with_every_fault(prepare, act, check)

    def test_a_google_sign_in(self):
        def prepare():
            # A pending link left in a session whose account no longer loads.
            client, account = self.linking_client()
            self.execute("UPDATE users SET is_active = 0 WHERE id = ?", (account["id"],))
            started = self.start(client)
            self.assertEqual(started.status_code, 303)
            self.assertIn(oauth_state.LINK_SLOT, self.session(client))
            return client, started, self.user_count()

        def check(context, answered):
            client, _, users = context
            self.assert_redirect(answered, "/")
            state = self.session(client)
            self.assertEqual(set(state), {"uid", "sk", "iat"})
            self.assertEqual(self.user_count(), users + 1)
            self.assertEqual(self.identity_rows(state["uid"])[0][2], "a-new-student")

        self.run_with_every_fault(
            prepare, lambda context: self.return_from_google(context[1], context[0], subject="a-new-student"), check)


# --------------------------------------------------------------------------- pages


class PagesTest(RoutesTestCase):
    def test_the_sign_in_page_offers_google_with_its_three_notices(self):
        response = self.client.get("/login?next=/account/attempts")
        page = " ".join(self.html(response).split())
        self.assertIn('<form method="post" action="/auth/google/start"', page)
        self.assertIn('<input type="hidden" name="next" value="/account/attempts">', page)
        self.assertIn("<span>Вход с Google</span>", page)
        for notice in (
            "При вход с Google получаваме името ти, имейл адреса ти и идентификатор на профила ти в Google. "
            "Паролата ти не стига до нас. Ако влизаш за първи път, ще ти създадем ученически профил.",
            "Учител си и вече имаш профил? Влез с имейл и парола и свържи Google от „Профил“.",
            "Изходът от LearnPilot не те отписва от Google. На общ компютър излез и от Google.",
        ):
            self.assertIn(notice, page)
        # The password form is unchanged beside it, and nothing is loaded from Google.
        self.assertIn('<form method="post" action="/login"', page)
        self.assertRegex(page, r'src="/static/img/brand/google\-g\.svg\?v=[0-9a-f]{12}"')
        for external in ("googleapis.com", "gstatic.com", "accounts.google.com", "google.com/"):
            self.assertNotIn(external, page)
        self.assertNotIn("Set-Cookie", response.headers)

    def test_an_unsafe_next_is_not_carried_into_the_google_form(self):
        page = self.html(self.client.get("/login?next=//evil.example/"))
        self.assertNotIn("evil.example", page)
        self.assertEqual(page.count('name="next" value=""'), 2)

    def test_the_google_asset_is_local_and_unmodified(self):
        asset = Path(self.app.static_folder) / "img" / "brand" / "google-g.svg"
        # Google's own file from its sign-in branding bundle ("Neutral", icon only), byte for byte.
        self.assertEqual(hashlib.sha256(asset.read_bytes()).hexdigest(),
                         "99fab3fa571a27e7a5e01b781cade619f94bab0cd0aa179f56e093b1492880cd")
        text = asset.read_text(encoding="utf-8")
        for needle in ("<script", "href=", "<image", "@import"):
            self.assertNotIn(needle, text)
        external = set(re.findall(r"https?://[^\"'\s)]+", text)) - {
            "http://www.w3.org/2000/svg", "http://www.w3.org/1999/xhtml"}
        self.assertEqual(external, set())
        served = self.client.get("/static/img/brand/google-g.svg")
        self.assertEqual(served.status_code, 200)
        served.close()

    def test_each_code_is_shown_on_its_pages_only_and_an_unknown_code_shows_nothing(self):
        plain = self.html(self.client.get("/login"))
        for code, text in TEXTS.items():
            with self.subTest(page="sign-in", code=code):
                page = self.html(self.client.get(f"/login?google={code}"))
                (self.assertIn if code in SIGN_IN_PAGE_CODES else self.assertNotIn)(text, page)
                if code not in SIGN_IN_PAGE_CODES:
                    self.assertEqual(page, plain)
        self.signed_in("teacher")
        plain_account = self.account_page()
        for code, text in TEXTS.items():
            with self.subTest(page="account", code=code):
                page = self.account_page(code=code)
                (self.assertIn if code in ACCOUNT_PAGE_CODES else self.assertNotIn)(text, page)
        for unknown in ("", "nonsense", "LINKED", "linked ", "<script>alert(1)</script>", "failed&x=1"):
            with self.subTest(unknown=unknown):
                response = self.client.get("/account", query_string={"google": unknown})
                self.assertEqual(self.html(response), plain_account)
                # A value is looked up and never echoed.
                self.assertNotIn("<script>alert(1)</script>", self.body(response))
        anonymous = self.app.test_client()
        self.assertEqual(self.html(anonymous.get("/login?google=nonsense")), plain)

    def test_a_signed_in_get_of_the_sign_in_page_carries_a_known_code_to_the_account_page(self):
        self.signed_in("teacher")
        for code in TEXTS:
            with self.subTest(code=code):
                self.assert_redirect(self.client.get(f"/login?google={code}"), f"/account?google={code}")
                self.assert_redirect(self.client.get(f"/login?google={code}&next=/teacher"), f"/account?google={code}")
        # An unknown code is dropped: the redirect is the one of today.
        for query in ("google=nonsense", "google=", "google=linked&google=failed"):
            with self.subTest(query=query):
                answered = self.client.get(f"/login?{query}")
                self.assertEqual(answered.status_code, 302)
                self.assertNotIn("nonsense", answered.headers["Location"])
        self.assert_redirect(self.client.get("/login?google=nonsense"), "/teacher/new")
        self.assert_redirect(self.client.get("/login?google=nonsense&next=/account/attempts"), "/account/attempts")

    def test_the_account_page_of_each_kind_of_account(self):
        teacher = self.signed_in("teacher", display_name="Петър Учителов")
        page = " ".join(self.account_page().split())
        self.assertIn("<h1>Петър Учителов</h1>", page)
        self.assertIn(f"Учител · {teacher['email']}", page)
        self.assertIn("<li>Имейл и парола</li>", page)
        self.assertIn('<form method="post" action="/auth/google/link"', page)
        self.assertIn("<span>Свържи с Google</span>", page)
        self.assertNotIn("Google —", page)
        self.assertNotIn(PENDING_NOTICE, page)
        self.assertNotIn("Изходът от LearnPilot не те отписва от Google.", page)

        self.link_until_pending()
        page = " ".join(self.account_page().split())
        self.assertIn(PENDING_NOTICE, page)
        self.assertIn('href="/auth/google/link/confirm"', page)
        self.assertIn('<form method="post" action="/auth/google/link/cancel">', page)
        self.clock += 180
        self.assertNotIn(PENDING_NOTICE, self.account_page())

        student = self.app.test_client()
        self.google_sign_in(student, subject="google-only", email="Pupil.G@Example.BG", name="Мария")
        page = " ".join(self.account_page(student).split())
        self.assertIn("<h1>Мария</h1>", page)
        self.assertIn('<p class="subtitle">Ученик</p>', page)   # no email of its own
        self.assertNotIn("Имейл и парола", page)
        self.assertRegex(page, r"<li>Google — Pupil\.G@Example\.BG, свързан на \d{4}-\d\d-\d\d</li>")
        self.assertIn("Този профил влиза само с Google. Засега към него не може да се добави парола "
                      "или друг начин за вход.", page)
        self.assertIn("Изходът от LearnPilot не те отписва от Google.", page)
        self.assertNotIn("Свържи с Google", page)
        # The subject is never shown.
        self.assertNotIn("google-only", page)

    def test_the_account_page_reads_only_and_runs_one_extra_query(self):
        teacher = self.signed_in("teacher")
        self.give_identity(teacher["id"], "mine", "mine@example.bg")
        fault = Tracing(self)
        with fault:
            response = self.client.get("/account")
        self.assertEqual((response.status_code, response.headers.getlist("Set-Cookie")), (200, []))
        reads = [s for s in fault.statements if "FROM users" in s or "user_identities" in s]
        self.assertEqual(len(reads), 2, reads)
        self.assertRegex(reads[0], r"FROM users WHERE id = \d+$")
        self.assertIn("FROM user_identities WHERE user_id =", reads[1])
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assert_redirect(self.app.test_client().get("/account"), "/login?next=/account")

    def test_the_password_page_shows_a_notice_to_an_account_without_a_password(self):
        self.google_sign_in(subject="google-only")
        user_id = self.session()["uid"]
        page = self.html(self.client.get("/account/password"))
        self.assertIn("Този профил влиза само с Google и няма парола за смяна.", page)
        self.assertNotIn('name="current_password"', page)
        # A POST is not treated as a wrong password: no throttle failure is counted.
        answered = self.client.post("/account/password", headers=SAME_ORIGIN, data={
            "current_password": "anything", "new_password": NEW_PASSWORD, "confirm_password": NEW_PASSWORD})
        self.assertEqual(answered.status_code, 200)
        self.assertNotIn("Грешна текуща парола.", self.html(answered))
        self.assertEqual(self.query("SELECT COUNT(*) FROM login_throttle"), [(0,)])
        self.assertIsNone(self.user(user_id, "password_hash"))

    def test_the_navigation_links_to_the_account_page(self):
        self.signed_in("teacher")
        page = self.html(self.client.get("/"))
        self.assertEqual(page.count('<a class="profile-link" href="/account">Профил</a>'), 1)   # one account menu, every width
        self.assertNotIn('href="/account"', self.html(self.app.test_client().get("/")))


class HostileNameSinkTest(RoutesTestCase):
    """The name is chosen by the student, through the Google profile. Every sink
    already treats names as untrusted."""

    NAME = '=HYPERLINK("https://evil.example")<script>alert(1)</script>/../x'

    def test_it_is_escaped_in_pages_neutralized_in_the_csv_and_slugged_in_note_filenames(self):
        teacher_client = self.app.test_client()
        self.signed_in("teacher", client=teacher_client)
        with closing(web_app.quiz_db()) as conn:
            section_id, _ = eligible_section(conn)
        created = teacher_client.post("/teacher/new", headers=SAME_ORIGIN,
                                      data={**NO_DEADLINE_FORM, "section_id": section_id, "question_count": 3})
        self.assertEqual(created.status_code, 302, self.body(created))
        assignment_id = int(created.headers["Location"].rsplit("/", 1)[1])

        self.assert_redirect(self.google_sign_in(subject="hostile", name=self.NAME), "/")
        self.assertEqual(self.user(self.session()["uid"], "display_name"), self.NAME)
        started = self.client.post(student_entry_path(assignment_id), headers=SAME_ORIGIN)
        self.assertEqual(started.status_code, 302, self.body(started))
        attempt_path = started.headers["Location"]
        attempt_id = int(attempt_path.rsplit("/", 1)[1])
        plan = json.loads(self.query("SELECT question_ids_json FROM quiz_attempts WHERE id = ?", (attempt_id,))[0][0])
        question_ids = plan["question_ids"] if isinstance(plan, dict) else plan
        submitted = self.client.post(attempt_path, headers=SAME_ORIGIN, data={f"q_{int(question_ids[0])}": "A"})
        self.assertEqual(submitted.status_code, 302, self.body(submitted))

        for client, path in ((self.client, "/"), (self.client, "/account"),
                             (teacher_client, f"/teacher/assignment/{assignment_id}/results")):
            with self.subTest(page=path):
                raw = self.body(client.get(path))
                self.assertNotIn("<script>alert(1)</script>", raw)
                self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", raw)

        exported = teacher_client.get(f"/teacher/assignment/{assignment_id}/results.csv")
        rows = list(csv.reader(io.StringIO(exported.get_data(as_text=True).lstrip("﻿"))))
        cells = [cell for row in rows for cell in row if "HYPERLINK" in cell]
        self.assertTrue(cells)
        # A formula prefix is neutralized: no cell begins with "=".
        self.assertTrue(all(cell.startswith("'=") for cell in cells), cells)

        notes = [path for path in Path(web_app.QUIZ_VAULT_PATH).rglob("*.md") if "attempts" in path.parts]
        self.assertTrue(notes)
        for note in notes:
            self.assertRegex(note.name, r"^[a-z0-9.-]+\.md$")
            self.assertEqual(note.parent.name, "attempts")


# --------------------------------------------------------------------------- a disabled provider


class DisabledProviderTest(RoutesTestCase):
    google_enabled = False

    def test_there_is_no_button_and_no_form(self):
        page = self.html(self.client.get("/login"))
        for absent in ("Google", "/auth/google/", "google-g.svg"):
            self.assertNotIn(absent, page)
        self.signed_in("teacher")
        page = self.account_page()
        for absent in ("Google", "/auth/google/", "Свържи"):
            self.assertNotIn(absent, page)

    def test_every_route_answers_the_generic_404(self):
        unknown = self.client.get("/no-such-page").get_data(as_text=True)
        visitors = {"anonymous": self.app.test_client()}
        for role in ("student", "teacher", "admin"):
            visitors[role] = self.app.test_client()
            account = create_account(self.db_path, role)
            set_account_session(visitors[role], account["id"], account["session_key"])
        for name, client in visitors.items():
            signed_in = name != "anonymous"
            for path in POST_ROUTES:
                with self.subTest(visitor=name, path=path):
                    answered = client.post(path, headers=SAME_ORIGIN)
                    if signed_in or path == "/auth/google/start":
                        self.assertEqual(answered.status_code, 404)
                    else:
                        # The access policy is that of any signed-in page.
                        self.assert_redirect(answered, f"/login?next={path}")
                    # A cross-origin POST is still refused first.
                    self.assertEqual(client.post(path, headers=CROSS_ORIGIN).status_code, 403)
            with self.subTest(visitor=name, path=CALLBACK):
                answered = client.get(CALLBACK, query_string={"code": "abc", "state": "s" * 43})
                self.assertEqual(answered.status_code, 404)
                if not signed_in:
                    self.assertEqual(answered.get_data(as_text=True).replace(CALLBACK, "/no-such-page"), unknown)
            if signed_in:
                self.assertEqual(client.get("/auth/google/link/confirm").status_code, 404)
        self.assertEqual((self.google.requests, self.consumption_rows()), ([], []))

    def test_a_transaction_or_a_pending_link_in_the_cookie_is_ignored_and_nothing_is_retired(self):
        teacher = self.signed_in("teacher")
        with patch.dict(self.app.config, {oauth_state.PROVIDER_CONFIG_KEY: provider()}):
            started = self.start_link()
            transaction = self.session()[oauth_state.TX_SLOT]
            code = self.google.authorize(started.headers["Location"])
        # Disabled between the steps.
        self.assertEqual(self.callback(code=code, state=transaction["state"]).status_code, 404)
        self.assertEqual(self.session()[oauth_state.TX_SLOT], transaction)
        with patch.dict(self.app.config, {oauth_state.PROVIDER_CONFIG_KEY: provider()}):
            self.assert_redirect(self.callback(code=code, state=transaction["state"]), "/auth/google/link/confirm")
            pending = self.session()[oauth_state.LINK_SLOT]
        rows = self.consumption_rows()
        self.assertEqual(self.client.get("/auth/google/link/confirm").status_code, 404)
        self.assertEqual(self.confirm().status_code, 404)
        self.assertEqual(self.cancel().status_code, 404)
        self.assertNotIn(PENDING_NOTICE, self.account_page())
        self.assertEqual(self.session()[oauth_state.LINK_SLOT], pending)
        # A password sign-in and a sign-out do not read the slots either.
        with patch.object(oauth_state, "get_request_db", wraps=oauth_state.get_request_db) as accessor:
            self.client.post("/logout", headers=SAME_ORIGIN)
        accessor.assert_not_called()
        self.assertEqual((self.consumption_rows(), self.identity_rows(teacher["id"])), (rows, []))

    def test_no_page_shows_the_outcome_of_a_google_sign_in(self):
        plain = self.client.get("/login").get_data()
        # The address Nginx redirects to, and every other code: byte for byte the ordinary page.
        for code in (*TEXTS, "nonsense"):
            with self.subTest(page="sign-in", code=code):
                self.assertEqual(self.client.get(f"/login?google={code}").get_data(), plain)
        teacher = self.signed_in("teacher")
        plain_account = self.client.get("/account").get_data()
        for code in (*TEXTS, "nonsense"):
            with self.subTest(page="account", code=code):
                self.assertEqual(self.client.get(f"/account?google={code}").get_data(), plain_account)
        # A signed-in GET of the sign-in page redirects as today, without the parameter.
        self.assert_redirect(self.client.get("/login?google=unavailable"), "/teacher/new")
        self.assert_redirect(self.client.get("/login?google=unavailable&next=/account/attempts"), "/account/attempts")

    def test_an_account_that_has_a_google_identity_is_told_that_it_is_switched_off(self):
        teacher = self.signed_in("teacher")
        self.give_identity(teacher["id"], "mine", "mine@example.bg")
        page = self.account_page()
        # Linked identities are still listed: the one text about Google that remains.
        self.assertIn("Google — mine@example.bg", page)
        self.assertIn("Входът с Google в момента е изключен.", page)
        self.assertNotIn("Свържи с Google", page)

    def test_an_account_created_through_google_cannot_sign_in_until_it_is_enabled_again(self):
        with patch.dict(self.app.config, {oauth_state.PROVIDER_CONFIG_KEY: provider()}):
            self.google_sign_in(subject="google-only")
            user_id = self.session()["uid"]
        # Its session is an ordinary account session and stays valid.
        self.assertEqual(self.client.get("/account/attempts").status_code, 200)
        self.client.post("/logout", headers=SAME_ORIGIN)
        self.assertEqual(self.start().status_code, 404)
        self.assertEqual(self.identity_rows(user_id)[0][2], "google-only")
        # Password sign-in is unaffected.
        teacher = self.make("teacher")
        self.assert_redirect(self.login(teacher["email"]), "/teacher/new")


# --------------------------------------------------------------------------- logs


class NoSecretInAnyLogTest(RoutesTestCase):
    def test_a_whole_flow_with_failures_logs_no_secret(self):
        secrets_seen = {CLIENT_SECRET}
        output = io.StringIO()
        handler = logging.StreamHandler(output)
        handler.setLevel(logging.DEBUG)
        root = logging.getLogger()
        loggers = [logging.getLogger(name) for name in (
            "", "learnpilot.oidc", "web.blueprints.oidc", "web.blueprints.account", "web.oauth_state", "authlib",
            "urllib3", "requests", "werkzeug", "joserfc")]
        levels = [(logger, logger.level, logger.propagate) for logger in loggers]
        root.addHandler(handler)
        for logger in loggers:
            logger.setLevel(logging.DEBUG)
            logger.propagate = True

        def remember(client=None, response=None):
            state = self.session(client)
            for slot in (oauth_state.TX_SLOT, oauth_state.LINK_SLOT):
                obj = state.get(slot) or {}
                for name in ("state", "nonce", "cv", "id"):
                    if obj.get(name):
                        secrets_seen.add(obj[name])
                if obj.get("state"):
                    secrets_seen.add(self.consumption_key("callback", obj["state"]))
                if obj.get("id"):
                    secrets_seen.add(self.consumption_key("link-confirm", obj["id"]))
            for value in (self.google.last_id_token, self.google.last_access_token):
                if value:
                    secrets_seen.add(value)
            secrets_seen.update(self.google.codes)

        stdout, stderr = io.StringIO(), io.StringIO()
        try:
            with redirect_stdout(stdout), redirect_stderr(stderr):
                # A first sign-in, a cancellation at Google, a provider failure, a
                # token that fails validation and a timeout.
                started = self.start()
                remember()
                self.return_from_google(started, subject="SUBJECT-ONE", email="first.person@example.bg", name="Първи")
                remember()
                for outcome in ("cancel", "outage", "bad token", "timeout", "wrong credentials"):
                    client = self.app.test_client()
                    started = self.start(client)
                    remember(client)
                    state = self.state_of(started.headers["Location"])
                    if outcome == "cancel":
                        self.callback(client, state=state, error="access_denied", error_description="SECRET-DESCRIPTION")
                    elif outcome == "outage":
                        self.google.token_response = lambda request: json_response(503, {"detail": "SECRET-BODY"})
                        self.return_from_google(started, client, subject="SUBJECT-TWO", email="second@example.bg")
                    elif outcome == "wrong credentials":
                        self.google.token_response = lambda request: json_response(
                            401, {"error": "invalid_client", "error_description": "SECRET-DESCRIPTION"})
                        self.return_from_google(started, client, subject="SUBJECT-TWO", email="second@example.bg")
                    elif outcome == "bad token":
                        self.return_from_google(started, client, subject="SUBJECT-TWO", email="second@example.bg",
                                                nonce="x" * 43)
                    else:
                        held = self.google.hold()
                        with patch.object(google_oidc, "NETWORK_BUDGET", 0.15):
                            self.return_from_google(started, client, subject="SUBJECT-TWO", email="second@example.bg")
                        held.set()
                        self.finish_workers()
                        self.google._held = None
                        with google_oidc.GATE.lock:
                            google_oidc.GATE.state.cooldown_until = 0.0
                    self.google.token_response = None
                    remember(client)

                # A link: a wrong password, a retirement that is not recorded, a
                # cancellation whose account read fails, a confirmation whose COMMIT
                # and rollback both fail, and at last the link itself.
                teacher_client = self.app.test_client()
                self.signed_in("teacher", client=teacher_client, email="the.teacher@example.bg")
                self.link_until_pending(teacher_client, subject="SUBJECT-THREE", email="third.person@example.bg",
                                        name="Трети")
                remember(teacher_client)
                self.confirm(teacher_client, password="a wrong PASSWORD-SECRET")
                with self.failing_retirement(HeldLock(self)):
                    self.cancel(teacher_client)
                self.link_until_pending(teacher_client, subject="SUBJECT-THREE", email="third.person@example.bg")
                remember(teacher_client)
                with patch.object(identities, "list_identities", side_effect=sqlite3.OperationalError("SECRET-TEXT")):
                    self.cancel(teacher_client)
                self.link_until_pending(teacher_client, subject="SUBJECT-THREE", email="third.person@example.bg")
                remember(teacher_client)
                with FailingCommitAndRollback(self):
                    self.confirm(teacher_client)
                self.confirm(teacher_client)
                remember(teacher_client)
                teacher_client.post("/logout", headers=SAME_ORIGIN)
        finally:
            root.removeHandler(handler)
            for logger, level, propagate in levels:
                logger.setLevel(level)
                logger.propagate = propagate

        captured = output.getvalue() + stdout.getvalue() + stderr.getvalue()
        # The flow did what it was meant to do, and it was logged.
        self.assertEqual(len(self.identity_rows()), 2)
        for expected in ("callback outcome=created", "callback outcome=cancelled", "reason=status", "reason=overrun",
                         "provider_rejected_client error=invalid_client", "retire_not_recorded",
                         "cancel_account_read_failed", "rollback_failed", "link_confirm outcome=linked"):
            self.assertIn(expected, captured)
        secrets_seen.update({
            "SUBJECT-ONE", "SUBJECT-TWO", "SUBJECT-THREE", "first.person@example.bg", "second@example.bg",
            "third.person@example.bg", "the.teacher@example.bg", "SECRET-DESCRIPTION", "SECRET-BODY", "SECRET-TEXT",
            "PASSWORD-SECRET", PASSWORD})
        self.assertGreater(len(secrets_seen), 30)
        for secret in secrets_seen:
            self.assertFalse(secret in captured, f"a secret reached a log: {secret[:12]}…")
        # No query string of the callback address either.
        self.assertNotIn("code=", captured)
        self.assertNotIn("state=", captured)


if __name__ == "__main__":
    unittest.main()
