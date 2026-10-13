"""Google OIDC, test plan B1: the transaction, the pending link and their retirement.

``web.oauth_state`` keeps two objects in the signed session and records their use in
the consumption table. Pinned here: their exact shape; their lifetimes on the
monotonic clock; that only the instance that issued them accepts them; the cookie
budget; that the clock is read after the write lock is held and that the expiry and the
key are the object's own; and what a retirement achieves, object by object, for every
way its record can fail to be written. A failure is never reported as a retirement.

Databases are temporary and of one test each; faults are real SQLite failures wherever
SQLite can produce them (``tests.google_oidc_support``).
"""

import ast
import logging
import secrets
import sqlite3
import time
import unittest
from contextlib import closing, contextmanager
from datetime import timedelta
from itertools import product
from unittest.mock import patch

from flask import session

from tests.google_oidc_support import (
    CALLBACK,
    CLIENT_SECRET,
    RETIREMENT_FAULTS,
    START_CLOCK,
    CommitBlockedByReader,
    FailingCommit,
    FailingCommitAndRollback,
    FailingInsert,
    GoogleAppTestCase,
    HeldLock,
    MissingTable,
    QueryOnly,
    ReadOnlyFile,
    Tracing,
)
from tests.support import ROOT
from tests.test_account_login import PASSWORD, SAME_ORIGIN
from tests.test_session_cookie_samesite import SessionCookieAssertions
from web import account_session, oauth_state
from web.request_db import get_request_db, request_db_if_open
from web.services import login_throttle, oidc_consumption

TX_KEYS = {"v", "intent", "state", "nonce", "cv", "next", "inst", "exp"}
BINDING_KEYS = {"uid", "skd", "siat"}
PENDING_KEYS = {"v", "id", "inst", "exp", "uid", "skd", "siat", "sub", "email", "name"}
URLSAFE = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")
OTHER_INSTANCE = "0" * 32
PR_MODULES = (
    "web/oauth_state.py", "web/account_session.py", "web/services/google_oidc.py",
    "web/services/oidc_accounts.py", "web/blueprints/oidc.py", "web/blueprints/account.py",
)


def signin_transaction(**overrides) -> dict:
    transaction = {
        "v": 1, "intent": "signin", "state": secrets.token_urlsafe(32), "nonce": secrets.token_urlsafe(32),
        "cv": secrets.token_urlsafe(64), "next": "", "inst": oauth_state.INSTANCE, "exp": START_CLOCK + 600,
    }
    transaction.update(overrides)
    return transaction


def link_transaction(**overrides) -> dict:
    return signin_transaction(**{
        "intent": "link", "exp": START_CLOCK + 300, "uid": 7, "skd": "a" * 64, "siat": 1_700_000_000, **overrides})


def pending_link(**overrides) -> dict:
    pending = {
        "v": 1, "id": secrets.token_urlsafe(32), "inst": oauth_state.INSTANCE, "exp": START_CLOCK + 180,
        "uid": 7, "skd": "a" * 64, "siat": 1_700_000_000, "sub": "google-subject", "email": "a@example.bg",
        "name": "Ана",
    }
    pending.update(overrides)
    return pending


def without(obj: dict, key: str) -> dict:
    return {name: value for name, value in obj.items() if name != key}


# Values an expiry must never be: each would reach the storage as a ValueError.
UNUSABLE_EXPIRIES = (True, False, 5600.0, "5600", -1, 2**62, None, [5600])


class ShapeTest(unittest.TestCase):
    """Well-formed means exactly the documented keys with usable values (design 2.1, 2.2)."""

    def test_the_documented_objects_are_well_formed(self):
        self.assertEqual(set(signin_transaction()), TX_KEYS)
        self.assertEqual(set(link_transaction()), TX_KEYS | BINDING_KEYS)
        self.assertEqual(set(pending_link()), PENDING_KEYS)
        for obj in (signin_transaction(), signin_transaction(next="/account/attempts"), link_transaction()):
            self.assertTrue(oauth_state.is_transaction(obj))
        self.assertTrue(oauth_state.is_pending_link(pending_link()))
        self.assertTrue(oauth_state.is_pending_link(pending_link(email="", name="")))

    def test_a_transaction_with_a_missing_or_an_extra_key_is_malformed(self):
        for base in (signin_transaction(), link_transaction()):
            for key in base:
                with self.subTest(intent=base["intent"], missing=key):
                    self.assertFalse(oauth_state.is_transaction(without(base, key)))
            self.assertFalse(oauth_state.is_transaction({**base, "code": "x"}))
        # The binding belongs to a link and to nothing else.
        self.assertFalse(oauth_state.is_transaction({**signin_transaction(), "uid": 7, "skd": "a" * 64, "siat": 5}))
        self.assertFalse(oauth_state.is_transaction(link_transaction(next="/teacher")))

    def test_malformed_transaction_values(self):
        cases = {
            "v": (2, "1", True, 1.0, None),
            "intent": ("SIGNIN", "", None, 1, "admin"),
            "state": ("s" * 42, "s" * 44, "s" * 42 + "!", 5, None, b"s" * 43),
            "nonce": ("n" * 42, "n" * 42 + " ", None),
            "cv": ("v" * 85, "v" * 87, "v" * 85 + "~", None),
            "next": (None, 5, "/" + "a" * 512),
            "inst": ("0" * 31, "0" * 33, "G" * 32, "A" * 32, None),
            "exp": UNUSABLE_EXPIRIES,
        }
        for key, values in cases.items():
            for value in values:
                with self.subTest(key=key, value=repr(value)[:30]):
                    self.assertFalse(oauth_state.is_transaction(signin_transaction(**{key: value})))
        for wrong in ("text", ["v"], None, 5, (("v", 1),)):
            self.assertFalse(oauth_state.is_transaction(wrong))

    def test_malformed_bindings(self):
        cases = {
            "uid": (True, 0, -1, "7", 7.0, 2**63, None),
            "skd": ("a" * 63, "a" * 65, "A" * 64, "g" * 64, None),
            "siat": (True, 0, -5, "1", 1.5, None),
        }
        for key, values in cases.items():
            for value in values:
                with self.subTest(key=key, value=repr(value)[:30]):
                    self.assertFalse(oauth_state.is_transaction(link_transaction(**{key: value})))
                    self.assertFalse(oauth_state.is_pending_link(pending_link(**{key: value})))

    def test_malformed_pending_links(self):
        base = pending_link()
        for key in base:
            with self.subTest(missing=key):
                self.assertFalse(oauth_state.is_pending_link(without(base, key)))
        self.assertFalse(oauth_state.is_pending_link({**base, "id_token": "x"}))
        cases = {
            "v": (2, True, "1"),
            "id": ("i" * 42, "i" * 44, "i" * 42 + ".", None),
            "inst": ("0" * 31, "Z" * 32, None),
            "exp": UNUSABLE_EXPIRIES,
            "sub": ("", "s" * 256, "with space", "tab\there", "nul\0", "кирилица", 5, None),
            "email": (None, 5, "e" * 255),
            "name": (None, 5, "n" * 81),
        }
        for key, values in cases.items():
            for value in values:
                with self.subTest(key=key, value=repr(value)[:30]):
                    self.assertFalse(oauth_state.is_pending_link(pending_link(**{key: value})))
        for wrong in ("text", [], None):
            self.assertFalse(oauth_state.is_pending_link(wrong))

    def test_every_bound_of_a_pending_link_is_accepted(self):
        self.assertTrue(oauth_state.is_pending_link(pending_link(sub="s" * 255, email="e" * 254, name="n" * 80)))


class CreationTest(SessionCookieAssertions, GoogleAppTestCase):
    def test_a_sign_in_transaction_holds_exactly_the_documented_keys(self):
        started = self.start(next="/account/attempts")
        self.assertEqual(started.status_code, 303)
        transaction = self.session()[oauth_state.TX_SLOT]
        self.assertEqual(set(self.session()), {oauth_state.TX_SLOT})
        self.assertEqual(set(transaction), TX_KEYS)
        self.assertEqual((transaction["v"], transaction["intent"], transaction["next"]),
                         (1, "signin", "/account/attempts"))
        self.assertEqual((len(transaction["state"]), len(transaction["nonce"]), len(transaction["cv"])), (43, 43, 86))
        for value in (transaction["state"], transaction["nonce"], transaction["cv"]):
            self.assertLessEqual(set(value), URLSAFE)
        self.assertEqual(transaction["inst"], oauth_state.INSTANCE)
        self.assertIs(type(transaction["exp"]), int)
        self.assertTrue(oauth_state.is_transaction(transaction))
        self.assertNotIn(CLIENT_SECRET, repr(self.session()))

    def test_a_link_transaction_is_bound_to_the_account_session_without_holding_its_key(self):
        account = self.signed_in("teacher")
        state_before = self.session()
        self.assertEqual(self.start_link().status_code, 303)
        transaction = self.session()[oauth_state.TX_SLOT]
        self.assertEqual(set(transaction), TX_KEYS | BINDING_KEYS)
        self.assertEqual((transaction["intent"], transaction["next"]), ("link", ""))
        self.assertEqual((transaction["uid"], transaction["siat"]), (account["id"], state_before["iat"]))
        self.assertRegex(transaction["skd"], r"^[0-9a-f]{64}$")
        # A digest, so that the session key is not in the cookie twice.
        self.assertNotIn(account["session_key"], repr(transaction))
        self.assertNotIn(CLIENT_SECRET, repr(self.session()))

    def test_every_secret_value_differs_on_every_creation(self):
        seen = {"state": set(), "nonce": set(), "cv": set()}
        for _ in range(5):
            self.start()
            transaction = self.session()[oauth_state.TX_SLOT]
            for key in seen:
                seen[key].add(transaction[key])
        self.assertEqual({key: len(values) for key, values in seen.items()}, {"state": 5, "nonce": 5, "cv": 5})
        self.signed_in("teacher")
        ids = set()
        for number in range(3):
            self.link_until_pending(subject=f"subject-{number}")
            ids.add(self.session()[oauth_state.LINK_SLOT]["id"])
        self.assertEqual(len(ids), 3)

    def test_lifetimes_are_seconds_of_the_monotonic_clock(self):
        self.start()
        self.assertEqual(self.session()[oauth_state.TX_SLOT]["exp"], START_CLOCK + 600)
        self.client = self.app.test_client()
        self.signed_in("teacher")
        self.clock = START_CLOCK + 50
        started = self.start_link()
        self.assertEqual(self.session()[oauth_state.TX_SLOT]["exp"], START_CLOCK + 50 + 300)
        self.clock = START_CLOCK + 90
        self.return_from_google(started)
        self.assertEqual(self.session()[oauth_state.LINK_SLOT]["exp"], START_CLOCK + 90 + 180)

    def test_a_pending_link_holds_exactly_the_documented_keys_and_no_token(self):
        account = self.signed_in("teacher")
        started = self.start_link()
        transaction = self.session()[oauth_state.TX_SLOT]
        code = self.google.authorize(started.headers["Location"], subject="subject-9", email="g@example.bg",
                                     name="Гео Милев")
        self.callback(code=code, state=transaction["state"])
        state = self.session()
        self.assertEqual(set(state), {"uid", "sk", "iat", oauth_state.LINK_SLOT})
        pending = state[oauth_state.LINK_SLOT]
        self.assertEqual(set(pending), PENDING_KEYS)
        self.assertEqual((pending["v"], pending["sub"], pending["email"], pending["name"]),
                         (1, "subject-9", "g@example.bg", "Гео Милев"))
        self.assertEqual((pending["uid"], pending["skd"], pending["siat"]),
                         (account["id"], transaction["skd"], transaction["siat"]))
        self.assertEqual((len(pending["id"]), pending["inst"]), (43, oauth_state.INSTANCE))
        # Once the pending link exists, the state, nonce and verifier are gone with the
        # transaction, and no code or token was ever in the cookie.
        serialized = repr(state)
        for secret in (transaction["state"], transaction["nonce"], transaction["cv"], code,
                       self.google.last_id_token, self.google.last_access_token, CLIENT_SECRET):
            self.assertNotIn(secret, serialized)

    def test_a_second_start_replaces_the_first(self):
        first = self.start()
        first_state = self.state_of(first.headers["Location"])
        second = self.start()
        stored = self.session()[oauth_state.TX_SLOT]
        self.assertEqual(stored["state"], self.state_of(second.headers["Location"]))
        self.assertNotEqual(stored["state"], first_state)
        # The earlier tab's callback is not this transaction's: nothing changes.
        code = self.google.authorize(first.headers["Location"])
        earlier = self.callback(code=code, state=first_state)
        self.assert_redirect(earlier, "/login?google=failed")
        self.assertEqual(earlier.headers.getlist("Set-Cookie"), [])
        self.assertEqual(self.session()[oauth_state.TX_SLOT], stored)
        self.assertEqual((self.consumption_rows(), self.google.requests), ([], []))
        # The newer flow is unaffected.
        self.assert_redirect(self.return_from_google(second), "/")
        self.assertEqual(set(self.session()), {"uid", "sk", "iat"})

    def test_an_oversized_session_keeps_no_transaction(self):
        with self.client.session_transaction() as sess:
            sess["quiz_attempt_owner:1"] = secrets.token_urlsafe(2400)
        started = self.start()
        self.assert_redirect(started, "/login?google=failed")
        self.assertNotIn(oauth_state.TX_SLOT, self.session())
        self.assertEqual(self.google.requests, [])

    def test_an_oversized_session_keeps_no_pending_link(self):
        self.signed_in("teacher")
        with self.client.session_transaction() as sess:
            sess["quiz_attempt_owner:1"] = secrets.token_urlsafe(1500)
        started = self.start_link()
        self.assertEqual(started.status_code, 303)
        returned = self.return_from_google(
            started, subject=secrets.token_urlsafe(190)[:255], email=secrets.token_urlsafe(180)[:240] + "@example.bg",
            name=secrets.token_urlsafe(60)[:80])
        self.assert_redirect(returned, "/account?google=failed")
        state = self.session()
        self.assertNotIn(oauth_state.LINK_SLOT, state)
        self.assertNotIn(oauth_state.TX_SLOT, state)
        self.assertEqual(self.identity_rows(), [])

    def test_a_pending_link_at_every_bound_fits_the_cookie(self):
        self.signed_in("teacher")
        subject = secrets.token_urlsafe(200)[:255]
        email = secrets.token_urlsafe(200)[:243] + "@example.bg"
        name = secrets.token_urlsafe(80)[:80]
        self.assertEqual((len(subject), len(email), len(name)), (255, 254, 80))
        self.link_until_pending(subject=subject, email=email, name=name)
        pending = self.session()[oauth_state.LINK_SLOT]
        self.assertEqual((pending["sub"], pending["email"], pending["name"]), (subject, email, name))
        self.assertLess(len(self.cookie()), oauth_state.COOKIE_BUDGET)

    def test_the_cookie_that_carries_either_object_has_the_session_attributes(self):
        with patch.dict(self.app.config, {"SESSION_COOKIE_SECURE": True}):
            started = self.start()
            self.assert_session_cookie(started)
            self.client = self.app.test_client()
            self.signed_in("teacher")
            link_started = self.start_link()
            self.assert_session_cookie(link_started)
            returned = self.return_from_google(link_started)
            self.assert_redirect(returned, "/auth/google/link/confirm")
            self.assert_session_cookie(returned)


class ClockAndInstanceTest(GoogleAppTestCase):
    """Accepted only while ``now < exp`` on the monotonic clock, and only by the
    instance that issued the object."""

    def test_a_sign_in_transaction_is_accepted_at_exp_minus_one_and_refused_at_exp(self):
        started = self.start()
        self.clock = START_CLOCK + 599
        self.assert_redirect(self.return_from_google(started), "/")

        client = self.app.test_client()
        started = self.start(client)
        expires_at = self.session(client)[oauth_state.TX_SLOT]["exp"]
        rows = self.consumption_rows()
        self.clock = expires_at
        expired = self.return_from_google(started, client)
        self.assert_redirect(expired, "/login?google=expired")
        self.assertEqual(self.session(client), {})
        # Nothing was recorded, and nothing needs to be.
        self.assertEqual(self.consumption_rows(), rows)
        self.assertEqual(len(self.google.token_requests), 1)

    def test_a_link_transaction_and_a_pending_link_expire_on_the_same_clock(self):
        account = self.signed_in("teacher")
        started = self.start_link()
        self.clock = START_CLOCK + 300
        self.assert_redirect(self.return_from_google(started), "/account?google=expired")

        self.clock = START_CLOCK
        self.link_until_pending()
        expires_at = self.session()[oauth_state.LINK_SLOT]["exp"]
        self.clock = expires_at - 1
        self.assertEqual(self.client.get("/auth/google/link/confirm").status_code, 200)
        self.clock = expires_at
        self.assert_redirect(self.client.get("/auth/google/link/confirm"), "/account?google=expired")
        self.assert_redirect(self.confirm(), "/account?google=expired")
        self.assertEqual(self.identity_rows(account["id"]), [])

        self.clock = START_CLOCK
        self.link_until_pending()
        self.clock = self.session()[oauth_state.LINK_SLOT]["exp"] - 1
        self.assert_redirect(self.confirm(), "/account?google=linked")

    def test_the_expiry_that_binds_is_checked_under_the_write_lock(self):
        # The first look passes and the clock reaches the expiry before the write.
        account = self.signed_in("teacher")
        self.link_until_pending()
        expires_at = self.session()[oauth_state.LINK_SLOT]["exp"]
        rows = self.consumption_rows()
        self.clock = expires_at - 1
        real_begin = oauth_state.begin_write

        def begin_late(conn):
            self.clock = expires_at
            return real_begin(conn)

        with patch.object(oauth_state, "begin_write", begin_late):
            self.assert_redirect(self.confirm(), "/account?google=expired")
        self.assertEqual((self.identity_rows(account["id"]), self.consumption_rows()), ([], rows))
        self.assertNotIn(oauth_state.LINK_SLOT, self.session())

    def test_a_wall_clock_set_forward_changes_no_outcome(self):
        real = time.time
        started = self.start()
        with patch("time.time", lambda: real() + 86400):
            done = self.return_from_google(started)
            self.assert_redirect(done, "/")
            self.assertEqual(set(self.session()), {"uid", "sk", "iat"})
        # The same day's step on the clock that counts ends the transaction.
        client = self.app.test_client()
        started = self.start(client)
        self.clock += 86400
        self.assert_redirect(self.return_from_google(started, client), "/login?google=expired")

    def test_the_state_logic_reads_no_wall_clock_in_either_direction(self):
        # Matching and spending a transaction with the wall clock a day ahead and a day
        # behind. Run on the hook itself, because Flask would not hand over a cookie
        # signed "in the future" at all (the characterisation below).
        real = time.time
        for shift in (86400, -86400):
            with self.subTest(shift=shift):
                transaction = signin_transaction()
                path = f"{CALLBACK}?state={transaction['state']}&code=abc"
                with self.app.test_request_context(path):
                    session[oauth_state.TX_SLOT] = transaction
                    with patch("time.time", lambda: real() + shift):
                        self.assertIsNone(oauth_state.verify_oauth_state())
                    self.assertEqual(oauth_state.matched_transaction(), transaction)
                    self.assertNotIn(oauth_state.TX_SLOT, session)
                key = self.consumption_key("callback", transaction["state"])
                self.assertIn((key, transaction["exp"]), self.consumption_rows())

    def test_a_wall_clock_set_back_fails_closed_before_the_state_is_read(self):
        # A characterisation of the session cookie, not of this flow: Flask refuses a
        # cookie whose signature is newer than the wall clock. A sign-in in flight
        # when the clock is set back therefore fails, and nothing is accepted.
        real = time.time
        started = self.start()
        with patch("time.time", lambda: real() - 86400):
            failed = self.return_from_google(started)
        self.assert_redirect(failed, "/login?google=failed")
        self.assertEqual((self.consumption_rows(), self.google.requests, self.user_count()), ([], [], 0))

    def test_a_transaction_of_another_instance_is_refused_and_not_recorded(self):
        started = self.start()
        with patch.object(oauth_state, "INSTANCE", OTHER_INSTANCE):
            refused = self.return_from_google(started)
        self.assert_redirect(refused, "/login?google=expired")
        self.assertNotIn(oauth_state.TX_SLOT, self.session())
        self.assertEqual((self.consumption_rows(), self.google.requests), ([], []))

    def test_a_pending_link_of_another_instance_is_refused_and_not_recorded(self):
        account = self.signed_in("teacher")
        self.link_until_pending()
        rows = self.consumption_rows()
        with patch.object(oauth_state, "INSTANCE", OTHER_INSTANCE):
            self.assert_redirect(self.client.get("/auth/google/link/confirm"), "/account?google=expired")
            self.assertIn(oauth_state.LINK_SLOT, self.session())  # a GET changes nothing
            self.assert_redirect(self.confirm(), "/account?google=expired")
        self.assertNotIn(oauth_state.LINK_SLOT, self.session())
        self.assertEqual((self.consumption_rows(), self.identity_rows(account["id"])), (rows, []))


class MalformedInputTest(GoogleAppTestCase):
    """Every row of design 2.5."""

    def assert_no_match(self, response, stored):
        self.assert_redirect(response, "/login?google=failed")
        self.assertEqual(response.headers.getlist("Set-Cookie"), [])
        self.assertEqual(self.session().get(oauth_state.TX_SLOT), stored)
        self.assertEqual((self.consumption_rows(), self.google.requests), ([], []))

    def test_a_missing_repeated_or_malformed_state_does_not_match(self):
        self.start()
        stored = self.session()[oauth_state.TX_SLOT]
        state = stored["state"]
        queries = (
            "code=abc",
            f"code=abc&state={state}&state={state}",
            f"code=abc&state={state[:-1]}",
            f"code=abc&state={state}x",
            f"code=abc&state={state[:-1]}%21",
            "code=abc&state=",
            f"code=abc&state={'x' * 43}",
        )
        for query in queries:
            with self.subTest(query=query[:40]):
                self.assert_no_match(self.client.get(f"{CALLBACK}?{query}"), stored)

    def test_a_query_string_over_the_limit_does_not_match(self):
        self.start()
        stored = self.session()[oauth_state.TX_SLOT]
        fits = f"state={stored['state']}&code=abc&scope="
        over = fits + "s" * (oauth_state.MAX_QUERY_BYTES + 1 - len(fits))
        self.assertEqual(len(over), oauth_state.MAX_QUERY_BYTES + 1)
        self.assert_no_match(self.client.get(f"{CALLBACK}?{over}"), stored)

    def test_a_malformed_stored_transaction_does_not_match_and_is_not_touched(self):
        state = secrets.token_urlsafe(32)
        for stored in ("text", {"state": state}, signin_transaction(state=state, v=2),
                       signin_transaction(state=state, exp=True), {**signin_transaction(state=state), "extra": 1}):
            with self.subTest(stored=repr(stored)[:40]):
                client = self.app.test_client()
                with client.session_transaction() as sess:
                    sess[oauth_state.TX_SLOT] = stored
                response = client.get(CALLBACK, query_string={"state": state, "code": "abc"})
                self.assert_redirect(response, "/login?google=failed")
                self.assertEqual(response.headers.getlist("Set-Cookie"), [])
                self.assertEqual(self.session(client)[oauth_state.TX_SLOT], stored)
        self.assertEqual((self.consumption_rows(), self.google.requests), ([], []))

    def test_a_missing_repeated_or_malformed_code_fails_after_the_transaction_is_spent(self):
        for query in ({}, {"code": ""}, {"code": "c" * 257}, {"code": "with space"}, {"code": "кирилица"},
                      {"code": ["one", "two"]}):
            with self.subTest(query=repr(query)[:40]):
                client = self.app.test_client()
                started = self.start(client)
                state = self.state_of(started.headers["Location"])
                rows = len(self.consumption_rows())
                self.assert_redirect(self.callback(client, state=state, **query), "/login?google=failed")
                self.assertEqual(len(self.consumption_rows()), rows + 1)
                self.assertEqual(self.session(client), {})
        self.assertEqual(self.google.requests, [])

    def test_an_error_beside_a_code_is_treated_as_an_error(self):
        started = self.start()
        code = self.google.authorize(started.headers["Location"])
        state = self.state_of(started.headers["Location"])
        self.assert_redirect(self.callback(code=code, state=state, error="access_denied"), "/login?google=cancelled")
        self.assertEqual((self.google.requests, self.user_count()), ([], 0))

    def test_unknown_parameters_are_ignored(self):
        started = self.start()
        code = self.google.authorize(started.headers["Location"])
        done = self.callback(code=code, state=self.state_of(started.headers["Location"]), scope="openid email",
                             authuser="0", hd="school.example", prompt="none")
        self.assert_redirect(done, "/")

    def test_an_unsafe_or_overlong_next_is_stored_as_empty(self):
        for target in ("//evil.example/", "https://evil.example/", "/\\evil.example", "/a\nb", "relative",
                       "/" + "a" * 512):
            with self.subTest(target=target[:30]):
                client = self.app.test_client()
                self.assertEqual(self.start(client, next=target).status_code, 303)
                self.assertEqual(self.session(client)[oauth_state.TX_SLOT]["next"], "")
        client = self.app.test_client()
        longest = "/" + "a" * 511
        self.start(client, next=longest)
        self.assertEqual(self.session(client)[oauth_state.TX_SLOT]["next"], longest)

    def test_a_malformed_pending_link_answers_expired_and_only_a_post_removes_it(self):
        self.signed_in("teacher")
        for stored in ("text", {"id": "x"}, pending_link(v=2), pending_link(exp="soon")):
            with self.subTest(stored=repr(stored)[:40]):
                with self.client.session_transaction() as sess:
                    sess[oauth_state.LINK_SLOT] = stored
                looked = self.client.get("/auth/google/link/confirm")
                self.assert_redirect(looked, "/account?google=expired")
                self.assertEqual(looked.headers.getlist("Set-Cookie"), [])
                self.assertEqual(self.session()[oauth_state.LINK_SLOT], stored)
                self.assert_redirect(self.confirm(), "/account?google=expired")
                self.assertNotIn(oauth_state.LINK_SLOT, self.session())
        self.assertEqual((self.consumption_rows(), self.identity_rows()), ([], []))


# --------------------------------------------------------------------------- the three call sites


class CallSiteContractTest(GoogleAppTestCase):
    """What every caller owes the consumption storage (design section 9)."""

    def numbered_clock(self):
        """A clock whose every reading is its own value, and a probe that tries to take
        the write lock from a second connection, without waiting, at each reading."""
        probe = sqlite3.connect(self.db_path, timeout=0)
        self.addCleanup(probe.close)
        readings = {}

        def clock():
            value = self.clock + len(readings)
            try:
                probe.execute("BEGIN IMMEDIATE")
                probe.rollback()
                readings[value] = "lock was free"
            except sqlite3.OperationalError:
                readings[value] = "lock was held"
            return value

        return clock, readings

    def consumed(self):
        """A spy on the storage: every (key, now, expires_at) that reached ``consume``."""
        calls, real = [], oidc_consumption.consume

        def consume(conn, key, *, now, expires_at):
            calls.append((key, now, expires_at))
            return real(conn, key, now=now, expires_at=expires_at)

        return patch.object(oidc_consumption, "consume", consume), calls

    def test_the_clock_is_read_after_the_write_lock_is_held_at_all_three_call_sites(self):
        clock, readings = self.numbered_clock()
        spy, calls = self.consumed()
        self.signed_in("teacher")
        with patch.object(oauth_state, "monotonic_seconds", clock), spy:
            self.link_until_pending(subject="one")              # the callback
            self.assert_redirect(self.confirm(), "/account?google=linked")  # the confirmation
            client = self.app.test_client()
            self.signed_in("teacher", client=client)
            self.link_until_pending(client, subject="two")
            self.assert_redirect(self.cancel(client), "/account?google=link_cancelled")  # retiring
        self.assertEqual(len(calls), 4)
        for _, now, _ in calls:
            self.assertEqual(readings[now], "lock was held")
        # Readings that never reach the storage (an expiry being computed, a first
        # look) are taken without the lock, which is how the probe is known to work.
        self.assertIn("lock was free", readings.values())

    def test_no_clock_is_read_when_retiring_finds_a_caller_transaction_or_cannot_begin(self):
        cases = {"caller transaction": None, "held lock": HeldLock, "query_only": QueryOnly}
        for name, fault_class in cases.items():
            with self.subTest(case=name):
                readings = []

                def clock():
                    readings.append(1)
                    return self.clock

                fault = Tracing(self) if fault_class is None else fault_class(self)
                with fault, self.app.test_request_context("/"), patch.object(oauth_state, "monotonic_seconds", clock):
                    session[oauth_state.LINK_SLOT] = pending_link()
                    if fault_class is None:
                        login_throttle.record_failure(get_request_db(), "k" * 64, self.now)
                    result = oauth_state.retire_linking()
                self.assertEqual(result.pending, oauth_state.NOT_RECORDED)
                self.assertEqual(readings, [])

    def test_the_expiry_and_the_key_are_the_objects_own_at_all_three_call_sites(self):
        spy, calls = self.consumed()
        self.signed_in("teacher")
        with spy:
            started = self.start_link()
            transaction = self.session()[oauth_state.TX_SLOT]
            self.clock += 40
            self.return_from_google(started)
            first = self.session()[oauth_state.LINK_SLOT]
            self.clock += 40
            self.confirm()
            client = self.app.test_client()
            self.signed_in("teacher", client=client)
            self.link_until_pending(client, subject="two")
            second = self.session(client)[oauth_state.LINK_SLOT]
            self.clock += 40
            self.cancel(client)
        expected = [
            (self.consumption_key("callback", transaction["state"]), transaction["exp"]),
            (self.consumption_key("link-confirm", first["id"]), first["exp"]),
            (self.consumption_key("link-confirm", second["id"]), second["exp"]),
        ]
        observed = [(key, expires_at) for key, _, expires_at in calls]
        self.assertEqual([observed[0], observed[1], observed[3]], expected)
        # Never computed at the time of use: it is the expiry the object was issued with.
        self.assertEqual(transaction["exp"], START_CLOCK + 300)
        self.assertEqual(first["exp"], START_CLOCK + 40 + 180)

    def test_a_malformed_expiry_never_reaches_the_storage(self):
        spy, calls = self.consumed()
        with spy:
            for value in UNUSABLE_EXPIRIES:
                with self.subTest(exp=repr(value)):
                    transaction = link_transaction(exp=value)
                    with self.app.test_request_context(f"{CALLBACK}?state={transaction['state']}&code=abc"):
                        session[oauth_state.TX_SLOT] = transaction
                        response = oauth_state.verify_oauth_state()
                        self.assertEqual(response.headers["Location"], "/login?google=failed")
                    with self.app.test_request_context("/"):
                        session[oauth_state.TX_SLOT] = transaction
                        session[oauth_state.LINK_SLOT] = pending_link(exp=value)
                        result = oauth_state.retire_linking()
                        self.assertEqual((result.transaction, result.pending), (oauth_state.DEAD, oauth_state.DEAD))
        self.assertEqual((calls, self.consumption_rows()), ([], []))


class StaticContractTest(unittest.TestCase):
    def source(self, relative):
        return (ROOT / relative).read_text(encoding="utf-8")

    def functions(self, relative) -> dict:
        tree = ast.parse(self.source(relative))
        return {node.name: node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)}

    def calls(self, node) -> list[str]:
        names = []
        for call in ast.walk(node):
            if isinstance(call, ast.Call):
                func = call.func
                names.append(func.id if isinstance(func, ast.Name) else getattr(func, "attr", ""))
        return names

    def test_the_one_clock_is_time_monotonic_in_whole_seconds(self):
        with patch("time.monotonic", return_value=1234.987):
            self.assertEqual(oauth_state.monotonic_seconds(), 1234)
        self.assertIs(type(oauth_state.monotonic_seconds()), int)
        body = self.functions("web/oauth_state.py")["monotonic_seconds"].body
        self.assertEqual(ast.unparse(body[-1]), "return int(time.monotonic())")

    def test_the_module_has_no_platform_branch_and_names_no_other_clock(self):
        source = self.source("web/oauth_state.py")
        for needle in ("time.time", "datetime", "perf_counter", "process_time", "clock_gettime", "CLOCK_",
                       "platform", "os.name", "uname", "time_ns", "monotonic_ns"):
            with self.subTest(needle=needle):
                self.assertNotIn(needle, source)
        self.assertEqual(source.count("time.monotonic"), 1)

    def test_among_the_call_sites_only_begin_write_reads_the_clock(self):
        functions = self.functions("web/oauth_state.py")
        readers = {name for name, node in functions.items() if "monotonic_seconds" in self.calls(node)}
        # begin_write for every reading that is passed to the storage; the other three
        # compute an expiry at creation or take a first look for a page.
        self.assertEqual(readers, {"begin_write", "start_transaction", "store_pending_link", "live_pending_link"})
        self.assertEqual(self.calls(functions["begin_write"])[:2], ["execute", "monotonic_seconds"])
        for relative in ("web/blueprints/oidc.py", "web/services/oidc_accounts.py", "web/account_session.py",
                         "web/blueprints/account.py"):
            with self.subTest(module=relative):
                self.assertNotIn("monotonic_seconds", self.source(relative))
                self.assertNotIn("time.monotonic", self.source(relative))

    def test_the_instance_is_drawn_once_at_import(self):
        self.assertRegex(oauth_state.INSTANCE, r"^[0-9a-f]{32}$")
        self.assertIn("INSTANCE = secrets.token_hex(16)", self.source("web/oauth_state.py"))

    def test_every_replacement_of_a_session_tries_to_retire_first_and_reads_no_result(self):
        functions = self.functions("web/account_session.py")
        for name in ("issue_account_session", "issue_pending_second_factor", "end_session"):
            with self.subTest(function=name):
                statements = [node for node in functions[name].body
                              if not (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant))]
                # A bare call: its result is neither kept nor tested.
                self.assertEqual(ast.unparse(statements[0]), "retire_linking()")
                self.assertIsInstance(statements[0], ast.Expr)
                source = ast.unparse(functions[name])
                self.assertEqual(source.count("retire_linking"), 1)
                self.assertLess(source.index("retire_linking()"), source.index("session.clear()"))

    def test_no_other_module_clears_the_session(self):
        clearing = []
        for directory in ("web", "src"):
            for path in sorted((ROOT / directory).rglob("*.py")):
                if "session.clear()" in path.read_text(encoding="utf-8"):
                    clearing.append(str(path.relative_to(ROOT)))
        self.assertEqual(clearing, ["web/account_session.py"])
        self.assertEqual(self.source("web/account_session.py").count("session.clear()"), 3)

    def test_nothing_here_deletes_a_consumption_record(self):
        # Rows leave the table by the cleanup inside consume() and by nothing else.
        for directory in ("web", "src", "deploy"):
            for path in sorted((ROOT / directory).rglob("*")):
                if not path.is_file() or path.suffix not in {".py", ".sh", ".sql", ".conf"} \
                        or path.name in ("oidc_consumption.py", "020_oidc_consumed_transactions.sql"):
                    continue
                text = " ".join(path.read_text(encoding="utf-8", errors="replace").split()).upper()
                with self.subTest(path=str(path.relative_to(ROOT))):
                    self.assertNotIn("DELETE FROM OIDC_CONSUMED_TRANSACTIONS", text)
                    self.assertNotIn("DROP TABLE OIDC_CONSUMED_TRANSACTIONS", text)
        for relative in PR_MODULES:
            with self.subTest(module=relative):
                self.assertNotIn("oidc_consumed_transactions", self.source(relative))

    def test_the_module_imports_no_blueprint_no_account_service_and_no_provider_code(self):
        tree = ast.parse(self.source("web/oauth_state.py"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add(node.module or "")
                imported.update(f"{node.module}.{alias.name}" for alias in node.names)
        web = {name for name in imported if name.startswith("web")}
        self.assertEqual(web, {
            "web.request_db", "web.request_db.get_request_db", "web.request_integrity",
            "web.request_integrity.OAUTH_STATE", "web.request_integrity.integrity_policy_for_view",
            "web.services", "web.services.oidc_consumption"})


# --------------------------------------------------------------------------- retiring


OBJECT_CASES = ("transaction", "pending", "both")


class RetirementTestCase(GoogleAppTestCase):
    """Runs ``retire_linking`` inside a request, with chosen objects in the session."""

    OTHER_KEYS = {"uid": 7, "sk": "session-key", "iat": 1_700_000_000, "quiz_attempt_owner:3": True}

    def objects(self, case, **overrides) -> dict:
        slots = {}
        if case in ("transaction", "both"):
            slots[oauth_state.TX_SLOT] = link_transaction(**overrides)
        if case in ("pending", "both"):
            slots[oauth_state.LINK_SLOT] = pending_link(**overrides)
        return slots

    def keys_of(self, slots) -> dict:
        keys = {}
        if oauth_state.TX_SLOT in slots:
            keys["transaction"] = self.consumption_key("callback", slots[oauth_state.TX_SLOT]["state"])
        if oauth_state.LINK_SLOT in slots:
            keys["pending"] = self.consumption_key("link-confirm", slots[oauth_state.LINK_SLOT]["id"])
        return keys

    @contextmanager
    def request(self, slots):
        """A request whose session holds ``slots`` beside an account session."""
        with self.app.test_request_context("/"):
            session.update(self.OTHER_KEYS)
            session.update(slots)
            yield

    def retire(self, slots, *, fault=None):
        """Retire in a fresh request; returns the result, the spy on the connection
        accessor and what the session holds afterwards."""
        fault = fault or Tracing(self)
        with fault, self.request(slots), patch.object(
                oauth_state, "get_request_db", wraps=oauth_state.get_request_db) as accessor:
            result = oauth_state.retire_linking()
            left = dict(session)
            in_transaction = (request_db_if_open() or sqlite3.connect(":memory:")).in_transaction
        self.assertEqual(left, self.OTHER_KEYS, "both slots go, and every other key stays as it was")
        self.assertFalse(in_transaction, "nothing is left pending on the connection")
        return result, accessor, fault

    def outcomes(self, result) -> tuple:
        return result.transaction, result.pending

    def expected(self, case, outcome) -> tuple:
        return (outcome if case in ("transaction", "both") else oauth_state.NONE,
                outcome if case in ("pending", "both") else oauth_state.NONE)

    def record(self, key, expires_at):
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute("INSERT INTO oidc_consumed_transactions (tx_hash, expires_at) VALUES (?, ?)", (key, expires_at))
            conn.commit()


class RetirementOutcomeTest(RetirementTestCase):
    def test_an_empty_session_and_a_sign_in_transaction_are_none(self):
        for slots in ({}, {oauth_state.TX_SLOT: signin_transaction()}):
            with self.subTest(slots=sorted(slots)):
                result, accessor, _ = self.retire(slots)
                self.assertEqual(self.outcomes(result), (oauth_state.NONE, oauth_state.NONE))
                # A sign-in is not linking: it leaves with its slot and is not recorded.
                accessor.assert_not_called()
                self.assertEqual(self.consumption_rows(), [])
                self.assertTrue(result.complete)
                self.assertFalse(result.all_invalidated)

    def test_a_malformed_object_and_one_of_another_instance_are_dead_without_the_database(self):
        for case in OBJECT_CASES:
            for name, overrides in (("malformed", {"v": 2}), ("another instance", {"inst": OTHER_INSTANCE})):
                with self.subTest(case=case, cause=name):
                    result, accessor, _ = self.retire(self.objects(case, **overrides))
                    self.assertEqual(self.outcomes(result), self.expected(case, oauth_state.DEAD))
                    accessor.assert_not_called()
                    self.assertEqual(self.consumption_rows(), [])
        with self.subTest(case="not an object at all"):
            result, accessor, _ = self.retire({oauth_state.TX_SLOT: "text", oauth_state.LINK_SLOT: ["x"]})
            self.assertEqual(self.outcomes(result), (oauth_state.DEAD, oauth_state.DEAD))
            accessor.assert_not_called()

    def test_an_expired_object_is_dead_and_at_exp_minus_one_it_is_invalidated(self):
        for case in OBJECT_CASES:
            for offset in (0, 1, 500):
                with self.subTest(case=case, offset=offset):
                    slots = self.objects(case, exp=START_CLOCK + 100)
                    self.clock = START_CLOCK + 100 + offset
                    result, _, fault = self.retire(slots)
                    self.assertEqual(self.outcomes(result), self.expected(case, oauth_state.DEAD))
                    self.assertEqual(self.consumption_rows(), [])
                    # The storage executed no statement for it.
                    self.assertEqual(fault.statements, ["BEGIN IMMEDIATE", "COMMIT"])
            with self.subTest(case=case, offset=-1):
                slots = self.objects(case, exp=START_CLOCK + 100)
                self.clock = START_CLOCK + 99
                result, _, _ = self.retire(slots)
                self.assertEqual(self.outcomes(result), self.expected(case, oauth_state.INVALIDATED))
                self.assertEqual({row[0] for row in self.consumption_rows()}, set(self.keys_of(slots).values()))
                self.fresh_database()

    def test_an_invalidated_object_is_recorded_under_its_own_key_and_expiry(self):
        for case in OBJECT_CASES:
            with self.subTest(case=case):
                slots = self.objects(case)
                result, accessor, _ = self.retire(slots)
                self.assertEqual(self.outcomes(result), self.expected(case, oauth_state.INVALIDATED))
                accessor.assert_called_once()
                expected = sorted((key, slots[slot]["exp"]) for key, slot in (
                    (self.keys_of(slots).get("transaction"), oauth_state.TX_SLOT),
                    (self.keys_of(slots).get("pending"), oauth_state.LINK_SLOT)) if key)
                self.assertEqual(self.consumption_rows(), expected)  # read from a second connection
                self.assertTrue(result.complete)
                self.assertTrue(result.all_invalidated)
                self.fresh_database()

    def test_an_already_recorded_key_is_consumed_whichever_use_wrote_the_row(self):
        for case in OBJECT_CASES:
            with self.subTest(case=case):
                slots = self.objects(case)
                for name, key in self.keys_of(slots).items():
                    slot = oauth_state.TX_SLOT if name == "transaction" else oauth_state.LINK_SLOT
                    self.record(key, slots[slot]["exp"])
                rows = self.consumption_rows()
                result, _, _ = self.retire(slots)
                self.assertEqual(self.outcomes(result), self.expected(case, oauth_state.CONSUMED))
                # The stored row is unchanged and the retirement adds none.
                self.assertEqual(self.consumption_rows(), rows)
                self.assertTrue(result.complete)
                self.assertFalse(result.all_invalidated)
                self.fresh_database()

    def test_consumed_is_what_a_second_retirement_of_the_same_object_reports(self):
        slots = self.objects("pending")
        first, _, _ = self.retire(slots)
        second, _, _ = self.retire(slots)
        self.assertEqual((first.pending, second.pending), (oauth_state.INVALIDATED, oauth_state.CONSUMED))
        self.assertEqual(len(self.consumption_rows()), 1)

    def test_dead_does_not_prove_that_an_object_was_unused(self):
        slots = self.objects("transaction", exp=START_CLOCK + 100)
        key = self.keys_of(slots)["transaction"]
        self.record(key, START_CLOCK + 100)  # used before its expiry
        for moment in (START_CLOCK + 100, START_CLOCK + 130):
            with self.subTest(clock=moment):
                self.clock = moment
                result, _, fault = self.retire(slots)
                self.assertEqual(result.transaction, oauth_state.DEAD)
                self.assertEqual(fault.statements, ["BEGIN IMMEDIATE", "COMMIT"])
        self.assertEqual(self.consumption_rows(), [(key, START_CLOCK + 100)])

    def test_invalidated_proves_that_the_object_was_unused(self):
        # With the key recorded at any earlier value of the clock, a retirement before
        # the expiry never reports the object as invalidated.
        for used_at, retired_at in product((0, 1, 60, 298), (1, 150, 299)):
            if retired_at < used_at:
                continue
            with self.subTest(used_at=used_at, retired_at=retired_at):
                slots = self.objects("transaction", exp=START_CLOCK + 300)
                self.clock = START_CLOCK + used_at
                with self.app.test_request_context("/"):
                    conn = get_request_db()
                    now = oauth_state.begin_write(conn)
                    oauth_state.record_use(conn, "callback", slots[oauth_state.TX_SLOT], now)
                    conn.commit()
                self.clock = START_CLOCK + retired_at
                result, _, _ = self.retire(slots)
                self.assertEqual(result.transaction, oauth_state.CONSUMED)

    def test_both_objects_are_recorded_in_one_transaction_the_link_transaction_first(self):
        slots = self.objects("both")
        result, _, fault = self.retire(slots)
        self.assertEqual(self.outcomes(result), (oauth_state.INVALIDATED, oauth_state.INVALIDATED))
        keys = self.keys_of(slots)
        inserts = fault.executed("INSERT")
        self.assertEqual(len(inserts), 2)
        self.assertIn(keys["transaction"], inserts[0])
        self.assertIn(keys["pending"], inserts[1])
        self.assertEqual((len(fault.executed("BEGIN IMMEDIATE")), len(fault.executed("COMMIT")),
                          len(fault.executed("ROLLBACK"))), (1, 1, 0))
        self.assertEqual((fault.statements[0], fault.statements[-1]), ("BEGIN IMMEDIATE", "COMMIT"))

    def test_mixed_results_occur_in_both_orders(self):
        for first, second in product((oauth_state.DEAD, oauth_state.CONSUMED, oauth_state.INVALIDATED), repeat=2):
            if first == second:
                continue
            with self.subTest(transaction=first, pending=second):
                self.clock = START_CLOCK
                slots = {
                    oauth_state.TX_SLOT: link_transaction(exp=START_CLOCK + (5 if first == oauth_state.DEAD else 300)),
                    oauth_state.LINK_SLOT: pending_link(exp=START_CLOCK + (5 if second == oauth_state.DEAD else 180)),
                }
                keys = self.keys_of(slots)
                if first == oauth_state.CONSUMED:
                    self.record(keys["transaction"], slots[oauth_state.TX_SLOT]["exp"])
                if second == oauth_state.CONSUMED:
                    self.record(keys["pending"], slots[oauth_state.LINK_SLOT]["exp"])
                self.clock = START_CLOCK + 10
                result, _, _ = self.retire(slots)
                self.assertEqual(self.outcomes(result), (first, second))
                self.assertTrue(result.complete)
                self.assertFalse(result.all_invalidated)
                self.fresh_database()

    def test_the_derived_values_over_every_combination_of_two_outcomes(self):
        outcomes = (oauth_state.NONE, oauth_state.INVALIDATED, oauth_state.CONSUMED, oauth_state.DEAD,
                    oauth_state.NOT_RECORDED)
        for transaction, pending in product(outcomes, repeat=2):
            with self.subTest(transaction=transaction, pending=pending):
                result = oauth_state.Retirement(transaction, pending)
                both = (transaction, pending)
                self.assertEqual(result.complete, oauth_state.NOT_RECORDED not in both)
                self.assertEqual(
                    result.all_invalidated,
                    oauth_state.INVALIDATED in both
                    and all(outcome in (oauth_state.INVALIDATED, oauth_state.NONE) for outcome in both))
        # An empty session is complete and nothing was invalidated; so is a session
        # whose only object had been used, and one whose only object was dead.
        for only in (oauth_state.NONE, oauth_state.CONSUMED, oauth_state.DEAD):
            result = oauth_state.Retirement(pending=only)
            self.assertEqual((result.complete, result.all_invalidated), (True, False))

    def test_with_the_provider_disabled_the_slots_are_not_read_and_nothing_is_taken(self):
        slots = self.objects("both")
        with patch.dict(self.app.config, {oauth_state.PROVIDER_CONFIG_KEY: None}), self.request(slots), \
                patch.object(oauth_state, "get_request_db", wraps=oauth_state.get_request_db) as accessor:
            result = oauth_state.retire_linking()
            self.assertEqual(dict(session), {**self.OTHER_KEYS, **slots})
        self.assertEqual(self.outcomes(result), (oauth_state.NONE, oauth_state.NONE))
        accessor.assert_not_called()
        self.assertEqual(self.consumption_rows(), [])


class RetirementCallerTransactionTest(RetirementTestCase):
    """The caller has a pending write on the request's connection (design 4.4, step 3)."""

    THROTTLE_KEY = "c" * 64

    def retire_inside_a_caller_transaction(self, slots, finish):
        statements = []
        with self.request(slots), self.assertLogs("learnpilot.oidc", logging.WARNING) as logs:
            conn = get_request_db()
            login_throttle.record_failure(conn, self.THROTTLE_KEY, self.now)  # not committed
            self.assertTrue(conn.in_transaction)
            conn.set_trace_callback(statements.append)
            result = oauth_state.retire_linking()
            conn.set_trace_callback(None)
            self.assertTrue(conn.in_transaction, "the caller's transaction is still open")
            self.assertEqual(dict(session), self.OTHER_KEYS)
            finish(conn)
        return result, statements, logs.output

    def throttle_rows(self):
        return self.query("SELECT identifier_hash FROM login_throttle")

    def test_no_statement_is_executed_and_every_live_object_is_not_recorded(self):
        for case in OBJECT_CASES:
            with self.subTest(case=case):
                slots = self.objects(case)
                result, statements, logs = self.retire_inside_a_caller_transaction(slots, lambda conn: conn.rollback())
                self.assertEqual(self.outcomes(result), self.expected(case, oauth_state.NOT_RECORDED))
                self.assertEqual((result.complete, result.all_invalidated), (False, False))
                # No BEGIN, no DELETE, no INSERT, no COMMIT and no ROLLBACK.
                self.assertEqual(statements, [])
                self.assertEqual(self.consumption_rows(), [])
                self.assertEqual(len(logs), 1)
                self.assertIn("retire_not_recorded", logs[0])
                self.assertIn("reason=caller_transaction", logs[0])

    def test_the_callers_write_is_preserved_whether_it_then_commits_or_rolls_back(self):
        # The regression test for "a sqlite3.Error anywhere: roll back", under which a
        # failed BEGIN inside the caller's transaction discarded the caller's write.
        slots = self.objects("both")
        self.retire_inside_a_caller_transaction(slots, lambda conn: conn.commit())
        self.assertEqual(self.throttle_rows(), [(self.THROTTLE_KEY,)])
        self.assertEqual(self.consumption_rows(), [])

        self.fresh_database()
        self.retire_inside_a_caller_transaction(slots, lambda conn: conn.rollback())
        self.assertEqual(self.throttle_rows(), [])
        self.assertEqual(self.consumption_rows(), [])

    def test_a_dead_object_beside_a_live_one_keeps_its_outcome(self):
        slots = {oauth_state.TX_SLOT: link_transaction(inst=OTHER_INSTANCE), oauth_state.LINK_SLOT: pending_link()}
        result, statements, logs = self.retire_inside_a_caller_transaction(slots, lambda conn: conn.rollback())
        self.assertEqual(self.outcomes(result), (oauth_state.DEAD, oauth_state.NOT_RECORDED))
        self.assertEqual(statements, [])
        self.assertIn("objects=pending ", logs[0])


class RetirementStorageFailureTest(RetirementTestCase):
    """Every error leaves the live objects ``not_recorded`` (design 4.4, step 7)."""

    def assert_never_a_false_success(self, result, case, logs, slots):
        self.assertEqual(self.outcomes(result), self.expected(case, oauth_state.NOT_RECORDED))
        self.assertNotIn(oauth_state.INVALIDATED, self.outcomes(result))
        self.assertEqual((result.complete, result.all_invalidated), (False, False))
        lines = [line for line in logs if "retire_not_recorded" in line]
        self.assertEqual(len(lines), 1, logs)
        names = ",".join(name for name in ("transaction", "pending") if case in (name, "both"))
        self.assertIn(f"objects={names} ", lines[0])
        self.assertRegex(lines[0], r"reason=(caller_transaction|[A-Z][A-Za-z]+) user=7$")
        # Never the state, the pending-link id, the key or the exception's text.
        text = "\n".join(logs)
        for obj in slots.values():
            for secret in (obj.get("state"), obj.get("id"), obj.get("nonce"), obj.get("cv")):
                if secret:
                    self.assertNotIn(secret, text)
        for key in self.keys_of(slots).values():
            self.assertNotIn(key, text)
        for fragment in ("database is locked", "readonly", "no such table", "forced by the test", "disk I/O"):
            self.assertNotIn(fragment, text)

    def retire_under(self, fault, case, **overrides):
        slots = self.objects(case, **overrides)
        with self.assertLogs("learnpilot.oidc", logging.WARNING) as logs:
            result, accessor, fault = self.retire(slots, fault=fault)
        self.assert_never_a_false_success(result, case, logs.output, slots)
        accessor.assert_called_once()
        return slots, fault

    def test_begin_immediate_fails_and_no_rollback_is_issued(self):
        for fault_class in (HeldLock, QueryOnly):
            for case in OBJECT_CASES:
                with self.subTest(fault=fault_class.name, case=case):
                    readings = []

                    def clock():
                        readings.append(1)
                        return self.clock

                    with patch.object(oauth_state, "monotonic_seconds", clock):
                        _, fault = self.retire_under(fault_class(self), case)
                    # Retirement never owned a transaction: no ROLLBACK and no COMMIT,
                    # and the clock was not read.
                    self.assertEqual(fault.statements, ["BEGIN IMMEDIATE"])
                    self.assertEqual(readings, [])
                    self.assertEqual(self.consumption_rows(), [])

    def test_a_read_only_database_file_fails_at_the_first_write_and_is_rolled_back_once(self):
        for case in OBJECT_CASES:
            with self.subTest(case=case):
                readings = []

                def clock():
                    readings.append(1)
                    return self.clock

                with patch.object(oauth_state, "monotonic_seconds", clock):
                    _, fault = self.retire_under(ReadOnlyFile(self), case)
                # BEGIN IMMEDIATE returned, so retirement owned a transaction and read
                # the clock once. The cleanup DELETE of consume() is the first write.
                self.assertEqual(len(fault.statements), 3, fault.statements)
                self.assertEqual(fault.statements[0], "BEGIN IMMEDIATE")
                self.assertTrue(fault.statements[1].startswith("DELETE FROM oidc_consumed_transactions"))
                self.assertEqual(fault.statements[2], "ROLLBACK")
                self.assertEqual(readings, [1])
                self.assertEqual(self.consumption_rows(), [])

    def test_a_missing_table_fails_inside_the_transaction_and_is_rolled_back_once(self):
        for case in OBJECT_CASES:
            with self.subTest(case=case):
                _, fault = self.retire_under(MissingTable(self), case)
                self.assertEqual((len(fault.executed("ROLLBACK")), len(fault.executed("COMMIT"))), (1, 0))
                self.assertEqual(fault.statements[0], "BEGIN IMMEDIATE")

    def test_a_failure_between_two_records_leaves_neither(self):
        slots = self.objects("both")
        keys = self.keys_of(slots)
        with self.assertLogs("learnpilot.oidc", logging.WARNING) as logs:
            result, _, fault = self.retire(slots, fault=FailingInsert(self, keys["pending"]))
        # The transaction's record had already been written, in a transaction that did
        # not commit.
        self.assert_never_a_false_success(result, "both", logs.output, slots)
        # Both inserts were attempted, the transaction's first. (With a trigger on the
        # table the trace repeats a statement for the trigger's own steps.)
        attempted = list(dict.fromkeys(fault.executed("INSERT")))
        self.assertEqual(len(attempted), 2)
        self.assertIn(keys["transaction"], attempted[0])
        self.assertIn(keys["pending"], attempted[1])
        self.assertEqual(len(fault.executed("ROLLBACK")), 1)
        self.assertEqual(self.consumption_rows(), [])
        # A copy of either object is accepted afterwards: the first record did not survive.
        second, _, _ = self.retire(slots)
        self.assertEqual(self.outcomes(second), (oauth_state.INVALIDATED, oauth_state.INVALIDATED))

    def test_an_already_recorded_object_keeps_its_outcome_beside_a_failure(self):
        slots = self.objects("both")
        keys = self.keys_of(slots)
        self.record(keys["transaction"], slots[oauth_state.TX_SLOT]["exp"])
        with self.assertLogs("learnpilot.oidc", logging.WARNING) as logs:
            result, _, _ = self.retire(slots, fault=FailingInsert(self, keys["pending"]))
        self.assertEqual(self.outcomes(result), (oauth_state.CONSUMED, oauth_state.NOT_RECORDED))
        self.assertEqual((result.complete, result.all_invalidated), (False, False))
        self.assertIn("objects=pending ", logs.output[0])
        # The earlier row is still there.
        self.assertEqual(self.consumption_rows(), [(keys["transaction"], slots[oauth_state.TX_SLOT]["exp"])])

    def test_a_commit_that_fails_leaves_no_row_and_no_open_transaction(self):
        for fault_class in (FailingCommit, CommitBlockedByReader):
            for case in OBJECT_CASES:
                with self.subTest(fault=fault_class.name, case=case):
                    slots, fault = self.retire_under(fault_class(self), case)
                    self.assertEqual(self.consumption_rows(), [])
                    self.assertEqual(len(fault.executed("INSERT")), len(slots))
                    # A copy of the object is accepted afterwards.
                    again, _, _ = self.retire(slots)
                    self.assertEqual(self.outcomes(again), self.expected(case, oauth_state.INVALIDATED))
                    self.fresh_database()

    def test_a_rollback_that_fails_as_well_changes_nothing_and_is_logged_by_its_class(self):
        for case in OBJECT_CASES:
            with self.subTest(case=case):
                slots = self.objects(case)
                fault = FailingCommitAndRollback(self)
                with fault, self.assertLogs("learnpilot.oidc", logging.WARNING) as logs, self.request(slots):
                    result = oauth_state.retire_linking()  # nothing is raised
                    self.assertEqual(dict(session), self.OTHER_KEYS)
                    conn = request_db_if_open()
                    count = len(fault.statements)
                    # What is still open is retirement's own; nothing further is executed on it.
                    self.assertTrue(conn.in_transaction)
                self.assert_never_a_false_success(result, case, logs.output, slots)
                self.assertIn("WARNING:learnpilot.oidc:rollback_failed error=OperationalError", logs.output)
                self.assertEqual(len(fault.statements), count)
                # The open transaction went with the request's connection.
                self.assertEqual(self.consumption_rows(), [])

    def test_an_exception_that_is_no_database_error_is_not_raised_to_the_caller(self):
        for case in OBJECT_CASES:
            with self.subTest(case=case):
                with patch.object(oauth_state, "record_use", side_effect=RuntimeError("the state is SECRET-TEXT")):
                    slots = self.objects(case)
                    with self.assertLogs("learnpilot.oidc", logging.WARNING) as logs:
                        result, _, fault = self.retire(slots)
                self.assert_never_a_false_success(result, case, logs.output, slots)
                self.assertIn("reason=RuntimeError", logs.output[0])
                self.assertNotIn("SECRET-TEXT", "\n".join(logs.output))
                self.assertEqual(fault.statements, ["BEGIN IMMEDIATE", "ROLLBACK"])

    def test_a_connection_that_cannot_be_opened_is_not_recorded(self):
        slots = self.objects("pending")
        with self.request(slots), self.assertLogs("learnpilot.oidc", logging.WARNING) as logs, \
                patch.object(oauth_state, "get_request_db", side_effect=sqlite3.OperationalError("unable to open")):
            result = oauth_state.retire_linking()
            self.assertEqual(dict(session), self.OTHER_KEYS)
        self.assert_never_a_false_success(result, "pending", logs.output, slots)
        self.assertIn("reason=OperationalError", logs.output[0])

    def test_system_exit_is_rolled_back_and_propagates(self):
        slots = self.objects("both")
        fault = Tracing(self)
        with fault, self.request(slots), patch.object(oauth_state, "record_use", side_effect=SystemExit(1)), \
                self.assertLogs("learnpilot.oidc", logging.WARNING):
            with self.assertRaises(SystemExit):
                oauth_state.retire_linking()
            self.assertFalse(request_db_if_open().in_transaction)
        self.assertEqual(fault.statements, ["BEGIN IMMEDIATE", "ROLLBACK"])
        self.assertEqual(self.consumption_rows(), [])

    def test_an_older_python_reports_a_repeated_key_as_not_recorded_never_as_consumed(self):
        # Before Python 3.11 the storage cannot tell a duplicate from another integrity
        # error and passes the IntegrityError on. That errs towards claiming less.
        slots = self.objects("pending")
        key = self.keys_of(slots)["pending"]
        self.record(key, slots[oauth_state.LINK_SLOT]["exp"])
        with patch.object(oidc_consumption, "consume", side_effect=sqlite3.IntegrityError("UNIQUE constraint failed")), \
                self.assertLogs("learnpilot.oidc", logging.WARNING) as logs:
            result, _, _ = self.retire(slots)
        self.assertEqual(result.pending, oauth_state.NOT_RECORDED)
        self.assertIn("reason=IntegrityError", logs.output[0])

    def test_every_fault_is_covered_for_every_object_case(self):
        # The sweep the route tests rely on: each fault, each case, never a success.
        for fault_class in RETIREMENT_FAULTS:
            for case in OBJECT_CASES:
                with self.subTest(fault=fault_class.name, case=case):
                    self.retire_under(fault_class(self), case)
                    self.assertEqual(self.consumption_rows(), [])


class CallSitePreconditionTest(GoogleAppTestCase):
    """Each caller has ended its own transaction before it retires (design 4.4)."""

    def spy(self):
        states, real = [], oauth_state.retire_linking

        def retire():
            conn = request_db_if_open()
            states.append(None if conn is None else conn.in_transaction)
            return real()

        return (patch.object(oauth_state, "retire_linking", retire),
                patch.object(account_session, "retire_linking", retire)), states

    def with_a_live_pending_link(self):
        account = self.signed_in("teacher")
        self.link_until_pending()
        return account

    def assert_retired_outside_a_transaction(self, action, *, own_records=0):
        (first, second), states = self.spy()
        rows = len(self.consumption_rows())
        with first, second:
            action()
        self.assertEqual(len(states), 1)
        self.assertNotIn(True, states)
        # The path that opens the database is the one taken: the pending link was
        # recorded, beside whatever the request records for itself.
        self.assertEqual(len(self.consumption_rows()), rows + 1 + own_records)

    def test_the_password_sign_in(self):
        self.with_a_live_pending_link()
        other = self.make("student")
        self.assert_retired_outside_a_transaction(lambda: self.assertEqual(self.login(other["email"]).status_code, 302))

    def test_the_admins_password_step_and_the_completed_second_factor(self):
        self.with_a_live_pending_link()
        admin = self.make("admin")
        self.now += timedelta(seconds=31)
        self.assert_retired_outside_a_transaction(
            lambda: self.assertEqual(self.login(admin["email"]).status_code, 200))
        # The second factor, with a live pending link put back into the session.
        with self.client.session_transaction() as sess:
            sess[oauth_state.LINK_SLOT] = pending_link()
        self.assert_retired_outside_a_transaction(
            lambda: self.assertEqual(self.submit_code(self.code(admin)).status_code, 302))

    def test_the_password_change(self):
        self.with_a_live_pending_link()
        change = {"current_password": PASSWORD, "new_password": "an entirely new passphrase",
                  "confirm_password": "an entirely new passphrase"}
        self.assert_retired_outside_a_transaction(
            lambda: self.assertEqual(self.client.post("/account/password", data=change, headers=SAME_ORIGIN).status_code, 200))

    def test_the_sign_out(self):
        self.with_a_live_pending_link()
        self.assert_retired_outside_a_transaction(
            lambda: self.assertEqual(self.client.post("/logout", headers=SAME_ORIGIN).status_code, 302))

    def test_the_google_sign_in(self):
        # A pending link left in a session whose account no longer loads.
        account = self.with_a_live_pending_link()
        self.execute("UPDATE users SET is_active = 0 WHERE id = ?", (account["id"],))
        started = self.start()
        self.assertEqual(started.status_code, 303)
        self.assertIn(oauth_state.LINK_SLOT, self.session())
        self.assert_retired_outside_a_transaction(
            lambda: self.assert_redirect(self.return_from_google(started, subject="another"), "/"),
            own_records=1)  # the callback's own record of its transaction

    def test_the_cancel_route_and_the_link_start(self):
        self.with_a_live_pending_link()
        self.assert_retired_outside_a_transaction(
            lambda: self.assert_redirect(self.cancel(), "/account?google=link_cancelled"))
        self.link_until_pending()
        self.assert_retired_outside_a_transaction(lambda: self.assertEqual(self.start_link().status_code, 303))


if __name__ == "__main__":
    unittest.main()
