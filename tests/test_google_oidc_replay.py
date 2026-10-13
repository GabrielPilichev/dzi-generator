"""Google OIDC, test plan B5: replay and concurrency (design section 3 and 4.4).

A transaction is accepted at most once, whoever presents it, whenever and through
whichever process: the first matched callback commits a record before it looks at
anything else. A pending link is stored as an identity at most once. Removing a slot
from a response revokes no earlier copy of the cookie, so "a copy" here is always a
second browser that presents the cookie as it was.

The residuals are pinned as the design states them, no smaller and no larger: what a
copy of a cookie can still do after a retirement that was not recorded, and what a
pending link that the session never held (a successor) can still do. Both end at the
account's password, its unchanged session key, the issuing run and the expiry.

"Two instances" are requests served with two different instance values on one
database and one clock; the instance value is the only state that tells two runs of
the service apart.
"""

import logging
import sqlite3
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from datetime import timedelta
from unittest.mock import patch

from tests.auth_helpers import set_account_session
from tests.google_oidc_support import (
    CALLBACK,
    CommitThenError,
    FailingCommit,
    FailingCommitAndRollback,
    GoogleAppTestCase,
    HeldLock,
    MissingTable,
    QueryOnly,
    ReadOnlyFile,
    Tracing,
    json_response,
    table_counts,
)
from tests.test_account_login import NEW_PASSWORD, PASSWORD, SAME_ORIGIN
from web import oauth_state
from web.services import accounts, identities, oidc_accounts, oidc_consumption

ISSUER = "https://accounts.google.com"
OTHER_INSTANCE = "b" * 32
CANCELLED_TEXT = "Свързването с Google беше отказано."


class ReplayTestCase(GoogleAppTestCase):
    def setUp(self):
        super().setUp()
        # Test accounts are hashed cheaply: these suites sign in and confirm hundreds
        # of times, and nothing here is about the strength of a hash.
        cheap = patch.object(accounts, "PASSWORD_METHOD", "pbkdf2:sha256:1")
        cheap.start()
        self.addCleanup(cheap.stop)

    @contextmanager
    def as_instance(self, value):
        """Serve the requests inside as another run of the service."""
        with patch.object(oauth_state, "INSTANCE", value):
            yield

    def give_identity(self, user_id, subject):
        with closing(sqlite3.connect(self.db_path)) as conn:
            identities.add_identity(conn, user_id, issuer=ISSUER, subject=subject, provider="google")
            conn.commit()

    def slots(self, client=None) -> set:
        return {oauth_state.TX_SLOT, oauth_state.LINK_SLOT} & set(self.session(client))

    def started_sign_in(self, client=None, **identity):
        """A sign-in at the point where Google redirects back: the start cookie, the
        state and a code."""
        client = client or self.app.test_client()
        started = self.start(client)
        location = started.headers["Location"]
        return client, self.cookie(client), self.state_of(location), self.google.authorize(location, **identity), location


# --------------------------------------------------------------------------- the transaction


class TransactionReplayTest(ReplayTestCase):
    def test_row_1_the_callback_address_is_requested_again_in_the_same_browser(self):
        client, _, state, code, _ = self.started_sign_in()
        self.assert_redirect(self.callback(client, code=code, state=state), "/")
        signed_in = self.session(client)
        rows, users = self.consumption_rows(), self.user_count()
        for _ in range(2):  # a reload, the back button
            again = self.callback(client, code=code, state=state)
            # The cookie no longer holds the transaction: no match, and nothing changes.
            self.assert_redirect(again, "/login?google=failed")
            self.assertEqual(again.headers.getlist("Set-Cookie"), [])
        self.assertEqual((self.session(client), self.consumption_rows(), self.user_count()), (signed_in, rows, users))
        self.assertEqual(len(self.google.token_requests), 1)

    def test_row_2_a_restored_start_cookie_with_the_same_code_is_refused(self):
        client, start_cookie, state, code, _ = self.started_sign_in()
        self.assert_redirect(self.callback(client, code=code, state=state), "/")
        replay = self.client_with_cookie(start_cookie)
        self.assert_redirect(self.callback(replay, code=code, state=state), "/login?google=failed")
        # Refused before any outbound call: the fake provider received one request in total.
        self.assertEqual(len(self.google.token_requests), 1)
        self.assertEqual((self.session(replay), self.user_count(), len(self.consumption_rows())), ({}, 1, 1))

    def test_row_3_simultaneous_callbacks_on_one_start_cookie(self):
        for codes in ("the same code", "different codes"):
            with self.subTest(codes=codes):
                self.fresh_database()
                _, start_cookie, state, code, location = self.started_sign_in(subject="contested")
                clients = [self.client_with_cookie(start_cookie) for _ in range(8)]
                chosen = [code if codes == "the same code" else self.google.authorize(location, subject="contested")
                          for _ in clients]
                barrier = threading.Barrier(len(clients))
                requests_before = len(self.google.token_requests)

                def callback(pair):
                    client, own_code = pair
                    barrier.wait(10)
                    return self.callback(client, code=own_code, state=state).headers["Location"]

                with ThreadPoolExecutor(max_workers=len(clients)) as pool:
                    answers = sorted(pool.map(callback, zip(clients, chosen)))
                # Exactly one passes; every other is refused.
                self.assertEqual(answers, ["/"] + ["/login?google=failed"] * 7)
                self.assertEqual(len(self.google.token_requests), requests_before + 1)
                self.assertEqual((self.user_count(), len(self.consumption_rows())), (1, 1))
                self.assertEqual(sum(1 for client in clients if "uid" in self.session(client)), 1)

    def test_row_4_a_restored_start_cookie_with_a_fresh_code_is_refused(self):
        client, start_cookie, state, code, location = self.started_sign_in(subject="first")
        self.assert_redirect(self.callback(client, code=code, state=state), "/")
        fresh_code = self.google.authorize(location, subject="another")
        replay = self.client_with_cookie(start_cookie)
        self.assert_redirect(self.callback(replay, code=fresh_code, state=state), "/login?google=failed")
        # No second user, no second session and no second request.
        self.assertEqual((self.user_count(), self.session(replay), len(self.google.token_requests)), (1, {}, 1))
        self.assertNotIn(fresh_code, self.google.redeemed)

    def test_row_5_a_code_from_another_authorization_request_is_refused_by_the_nonce(self):
        victim = self.app.test_client()
        victim_started = self.start(victim)
        _, _, _, attackers_code, _ = self.started_sign_in(subject="the-attacker")
        for pkce in (True, False):
            with self.subTest(provider_enforces_pkce=pkce):
                self.google.enforce_pkce = pkce
                with victim.session_transaction() as sess:
                    transaction = sess.get(oauth_state.TX_SLOT)
                if transaction is None:
                    victim_started = self.start(victim)
                    transaction = self.session(victim)[oauth_state.TX_SLOT]
                answered = self.callback(victim, code=attackers_code, state=transaction["state"])
                self.assert_redirect(answered, "/login?google=failed")
                # The transaction is recorded as used, and nobody is signed in.
                self.assertIn((self.consumption_key("callback", transaction["state"]), transaction["exp"]),
                              self.consumption_rows())
                self.assertEqual((self.session(victim), self.user_count()), ({}, 0))
        # Without PKCE at the provider the code was redeemed, and the nonce still refused the token.
        self.assertIn(attackers_code, self.google.redeemed)

    def test_row_6_an_error_callback_spends_the_transaction(self):
        client, start_cookie, state, code, _ = self.started_sign_in()
        self.assert_redirect(self.callback(client, state=state, error="access_denied"), "/login?google=cancelled")
        second = self.client_with_cookie(start_cookie)
        self.assert_redirect(self.callback(second, code=code, state=state), "/login?google=failed")
        self.assertEqual((self.google.requests, self.user_count()), ([], 0))

    def test_row_7_any_use_after_the_expiry_is_refused_and_leaves_no_record(self):
        _, start_cookie, state, code, _ = self.started_sign_in()
        fault = Tracing(self)
        with fault:
            for later in (600, 601, 660, 100_000):
                with self.subTest(seconds_after_start=later):
                    self.clock = 5000 + later
                    expired = self.callback(self.client_with_cookie(start_cookie), code=code, state=state)
                    self.assert_redirect(expired, "/login?google=expired")
        # Refused before any cleanup runs: the storage executed no statement at all.
        self.assertEqual([s for s in fault.statements if "oidc_consumed" in s], [])
        self.assertEqual((self.consumption_rows(), self.google.requests, self.user_count()), ([], [], 0))
        # One second before, it is still accepted.
        self.clock = 5599
        self.assert_redirect(self.callback(self.client_with_cookie(start_cookie), code=code, state=state), "/")

    def test_row_8_any_use_after_a_restart_is_refused(self):
        _, start_cookie, state, code, _ = self.started_sign_in()
        with self.as_instance(OTHER_INSTANCE):   # a restart, a reboot or a restore: a new run
            replay = self.client_with_cookie(start_cookie)
            self.assert_redirect(self.callback(replay, code=code, state=state), "/login?google=expired")
        self.assertEqual((self.session(replay), self.consumption_rows(), self.google.requests), ({}, [], []))

    def test_row_9_two_browsers_sign_in_the_same_new_identity_at_once(self):
        flows = [self.started_sign_in(subject="the-same-person") for _ in range(2)]
        barrier = threading.Barrier(2)

        def finish(flow):
            client, _, state, code, _ = flow
            barrier.wait(10)
            return self.callback(client, code=code, state=state).headers["Location"]

        with ThreadPoolExecutor(max_workers=2) as pool:
            answers = list(pool.map(finish, flows))
        self.assertEqual(answers, ["/", "/"])
        # One user, one identity, and both are signed in to it.
        self.assertEqual((self.user_count(), len(self.identity_rows())), (1, 1))
        self.assertEqual(len({self.session(flow[0])["uid"] for flow in flows}), 1)

    def test_row_12_a_forged_link_to_the_callback_changes_nothing(self):
        victim = self.app.test_client()
        self.start(victim)
        stored = self.session(victim)
        teacher_client = self.app.test_client()
        self.signed_in("teacher", client=teacher_client)
        before = table_counts(self.db_path)
        for client in (victim, teacher_client, self.app.test_client()):
            for query in ({"code": "attackers-code", "state": "a" * 43}, {"error": "access_denied", "state": "a" * 43},
                          {"code": "x"}, {}):
                forged = client.get(CALLBACK, query_string=query, headers={
                    "Referer": "https://evil.example/", "Sec-Fetch-Site": "cross-site"})
                self.assert_redirect(forged, "/login?google=failed")
                self.assertEqual(forged.headers.getlist("Set-Cookie"), [])
        # No cookie, no row and no outbound request; the sign-in in progress is untouched.
        self.assertEqual((self.session(victim), table_counts(self.db_path), self.google.requests), (stored, before, []))

    def test_row_17_two_instances_on_one_database(self):
        # Rows are deleted by expiry only, never because another instance wrote them.
        _, start_cookie, state, code, _ = self.started_sign_in(subject="first")
        self.assert_redirect(self.callback(self.client_with_cookie(start_cookie), code=code, state=state), "/")
        first_record = self.consumption_rows()
        with self.as_instance(OTHER_INSTANCE):
            self.clock += 30   # the other instance completes a different sign-in, later
            client, _, other_state, other_code, _ = self.started_sign_in(subject="second")
            self.assert_redirect(self.callback(client, code=other_code, state=other_state), "/")
        self.assertEqual(len(self.consumption_rows()), 2)
        # The first transaction is replayed to the instance that issued it, and is refused.
        replay = self.client_with_cookie(start_cookie)
        self.assert_redirect(self.callback(replay, code=code, state=state), "/login?google=failed")
        self.assertTrue(set(first_record) <= set(self.consumption_rows()))
        self.assertEqual((self.user_count(), self.session(replay)), (2, {}))


class RecordPersistsTest(ReplayTestCase):
    """Use is recorded before the outcome is known, and that commit stands."""

    def test_the_record_persists_when_what_follows_fails(self):
        failures = {
            "a provider failure": lambda: setattr(self.google, "token_response", lambda request: json_response(503, {})),
            "a validation failure": lambda: None,
            "an exception in onboarding": lambda: None,
        }
        for name, prepare in failures.items():
            with self.subTest(failure=name):
                self.fresh_database()
                client, start_cookie, state, _, location = self.started_sign_in()
                code = self.google.authorize(location, **({"nonce": "x" * 43} if name == "a validation failure" else {}))
                prepare()
                patcher = patch.object(oidc_accounts, "sign_in_or_onboard", side_effect=RuntimeError("boom"))
                if name == "an exception in onboarding":
                    patcher.start()
                try:
                    first = self.callback(client, code=code, state=state)
                finally:
                    if name == "an exception in onboarding":
                        patcher.stop()
                    self.google.token_response = None
                self.assertIn(first.headers["Location"], ("/login?google=unavailable", "/login?google=failed"))
                self.assertEqual(len(self.consumption_rows()), 1)
                # A replay with a good code is refused: the transaction was spent.
                good = self.google.authorize(location)
                replay = self.client_with_cookie(start_cookie)
                self.assert_redirect(self.callback(replay, code=good, state=state), "/login?google=failed")
                self.assertEqual((self.user_count(), self.session(replay)), (0, {}))

    def test_a_storage_error_at_consumption_ends_unavailable(self):
        for fault_class in (MissingTable, QueryOnly, ReadOnlyFile, FailingCommit):
            with self.subTest(fault=fault_class.name):
                self.fresh_database()
                client, start_cookie, state, code, _ = self.started_sign_in()
                fault = fault_class(self)
                with fault, self.assertLogs("learnpilot.oidc", logging.WARNING) as logs:
                    answered = self.callback(client, code=code, state=state)
                self.assert_redirect(answered, "/login?google=unavailable")
                # No outbound request and no session.
                self.assertEqual((self.google.requests, self.session(client), self.user_count()), ([], {}, 0))
                self.assertRegex(logs.output[0], r"callback_record_failed intent=signin error=[A-Z][A-Za-z]+$")
                if fault.fails_at_begin:
                    # The hook never owned a transaction: it issues no rollback.
                    self.assertEqual(fault.executed("BEGIN"), ["BEGIN IMMEDIATE"])
                    self.assertEqual(fault.executed("ROLLBACK"), [])
                else:
                    self.assertEqual(len(fault.executed("ROLLBACK")), 1)
                if fault_class is ReadOnlyFile:
                    writes = [s for s in fault.statements if not s.upper().startswith(("SELECT", "PRAGMA"))]
                    self.assertEqual(writes[0], "BEGIN IMMEDIATE")
                    self.assertTrue(writes[1].startswith("DELETE FROM oidc_consumed_transactions"))
                    self.assertEqual(writes[2:], ["ROLLBACK"])
                self.assertEqual(self.consumption_rows(), [])

    def test_a_rollback_that_fails_as_well_changes_nothing_and_is_logged_by_its_class(self):
        client, _, state, code, _ = self.started_sign_in()
        with FailingCommitAndRollback(self), self.assertLogs("learnpilot.oidc", logging.WARNING) as logs:
            answered = self.callback(client, code=code, state=state)
        self.assert_redirect(answered, "/login?google=unavailable")
        self.assertIn("WARNING:learnpilot.oidc:rollback_failed error=OperationalError", logs.output)
        self.assertEqual((self.google.requests, self.session(client), self.consumption_rows()), ([], {}, []))

    def test_a_commit_that_took_effect_and_still_reported_an_error(self):
        client, start_cookie, state, code, _ = self.started_sign_in()
        with CommitThenError(self), self.assertLogs("learnpilot.oidc", logging.WARNING):
            answered = self.callback(client, code=code, state=state)
        # The first answer claimed nothing about what reached the disk.
        self.assert_redirect(answered, "/login?google=unavailable")
        self.assertEqual((self.google.requests, self.session(client)), ([], {}))
        # The transaction was spent: a replay is refused, as "failed".
        self.assertEqual(len(self.consumption_rows()), 1)
        replay = self.client_with_cookie(start_cookie)
        self.assert_redirect(self.callback(replay, code=code, state=state), "/login?google=failed")
        self.assertEqual((self.google.requests, self.user_count()), ([], 0))

    def test_an_older_python_refuses_a_replay_as_unavailable(self):
        # Before 3.11 a repeated key is the IntegrityError it is: refused, by another branch.
        _, start_cookie, state, code, _ = self.started_sign_in()
        self.assert_redirect(self.callback(self.client_with_cookie(start_cookie), code=code, state=state), "/")
        with patch.object(oidc_consumption, "consume", side_effect=sqlite3.IntegrityError("UNIQUE constraint failed")), \
                self.assertLogs("learnpilot.oidc", logging.WARNING):
            replay = self.client_with_cookie(start_cookie)
            self.assert_redirect(self.callback(replay, code=code, state=state), "/login?google=unavailable")
        self.assertEqual((self.session(replay), len(self.google.token_requests), self.user_count()), ({}, 1, 1))

    def test_an_error_that_is_no_database_error_at_consumption_answers_failed(self):
        client, _, state, code, _ = self.started_sign_in()
        with patch.object(oauth_state, "record_use", side_effect=RuntimeError("the state is SECRET-TEXT")), \
                self.assertLogs("learnpilot.oidc", logging.WARNING) as logs:
            self.assert_redirect(self.callback(client, code=code, state=state), "/login?google=failed")
        self.assertEqual(logs.output, ["WARNING:learnpilot.oidc:callback_record_failed intent=signin error=RuntimeError"])
        self.assertEqual((self.google.requests, self.consumption_rows(), self.session(client)), ([], [], {}))


# --------------------------------------------------------------------------- intent


class IntentTest(ReplayTestCase):
    def test_a_sign_in_transaction_never_links_whatever_the_session_holds(self):
        started = self.start()
        transaction = self.session()[oauth_state.TX_SLOT]
        teacher = self.make("teacher")
        cases = {
            "an account session beside it": lambda sess: sess.update(
                {"uid": teacher["id"], "sk": teacher["session_key"], "iat": int(time.time())}),
            "binding keys smuggled next to it": lambda sess: sess.update({"link_uid": teacher["id"], "intent": "link"}),
            "a pending link beside it": lambda sess: sess.update({oauth_state.LINK_SLOT: {"uid": teacher["id"]}}),
        }
        for name, change in cases.items():
            with self.subTest(case=name):
                client = self.app.test_client()
                with client.session_transaction() as sess:
                    sess[oauth_state.TX_SLOT] = transaction
                    change(sess)
                code = self.google.authorize(started.headers["Location"], subject=f"someone-{len(name)}")
                self.callback(client, code=code, state=transaction["state"])
                self.execute("DELETE FROM oidc_consumed_transactions")
        # No identity was ever attached to the teacher.
        self.assertEqual(self.identity_rows(teacher["id"]), [])

    def test_a_link_transaction_never_signs_anyone_in(self):
        self.signed_in("teacher")
        started = self.start_link()
        transaction = self.session()[oauth_state.TX_SLOT]
        code = self.google.authorize(started.headers["Location"], subject="a-google-account")
        anonymous = self.app.test_client()
        with anonymous.session_transaction() as sess:
            sess[oauth_state.TX_SLOT] = transaction
        self.assert_redirect(self.callback(anonymous, code=code, state=transaction["state"]), "/account?google=failed")
        self.assertEqual((self.session(anonymous), self.user_count(), self.identity_rows()), ({}, 1, []))
        self.assertEqual(self.google.requests, [])

    def test_an_intent_that_a_client_changed_would_not_verify(self):
        # The transaction travels in the signed cookie: a changed copy is not that cookie.
        self.start()
        value = self.cookie()
        tampered = self.client_with_cookie(value[:5] + ("A" if value[5] != "A" else "B") + value[6:])
        self.assertEqual(self.session(tampered), {})


# --------------------------------------------------------------------------- the pending link


class PendingLinkReplayTest(ReplayTestCase):
    def setUp(self):
        super().setUp()
        self.teacher = self.signed_in("teacher")

    def test_row_10_two_accounts_confirm_the_same_google_identity_at_once(self):
        other_client = self.app.test_client()
        other = self.signed_in("teacher", client=other_client)
        self.link_until_pending(subject="contested", email="")
        self.link_until_pending(other_client, subject="contested", email="")
        barrier = threading.Barrier(2)

        def confirm(client):
            barrier.wait(10)
            return self.confirm(client).headers["Location"]

        with ThreadPoolExecutor(max_workers=2) as pool:
            answers = sorted(pool.map(confirm, (self.client, other_client)))
        self.assertEqual(answers, ["/account?google=identity_taken", "/account?google=linked"])
        rows = self.identity_rows()
        self.assertEqual(len(rows), 1)
        self.assertIn(rows[0][0], (self.teacher["id"], other["id"]))

    def test_row_11_a_first_sign_in_and_a_confirmation_of_the_same_new_identity(self):
        for first in ("the sign-in", "the confirmation"):
            with self.subTest(first=first):
                self.fresh_database()
                self.client = self.app.test_client()
                teacher = self.signed_in("teacher")
                self.link_until_pending(subject="contested", email="")
                signer, _, state, code, _ = self.started_sign_in(subject="contested", email="")
                if first == "the sign-in":
                    self.assert_redirect(self.callback(signer, code=code, state=state), "/")
                    # The identity belongs to a new student: undoing that needs the recovery tooling.
                    self.assert_redirect(self.confirm(), "/account?google=identity_taken")
                    self.assertNotEqual(self.identity_rows()[0][0], teacher["id"])
                else:
                    self.assert_redirect(self.confirm(), "/account?google=linked")
                    self.assert_redirect(self.callback(signer, code=code, state=state), "/teacher/new")
                    self.assertEqual(self.session(signer)["uid"], teacher["id"])
                self.assertEqual(len(self.identity_rows()), 1)

    def test_row_13_a_duplicate_confirmation(self):
        self.link_until_pending()
        copy, late_copy = self.copy_of(), self.copy_of()
        barrier = threading.Barrier(2)

        def confirm(client):
            barrier.wait(10)
            return self.confirm(client).headers["Location"]

        with ThreadPoolExecutor(max_workers=2) as pool:
            answers = sorted(pool.map(confirm, (self.client, copy)))
        self.assertEqual(answers, ["/account?google=already_linked", "/account?google=linked"])
        rows = self.identity_rows(), self.consumption_rows()
        self.assertEqual(len(rows[0]), 1)
        # Sent once more from a copy after the link was made: no identity and no record.
        self.assert_redirect(self.confirm(late_copy), "/account?google=already_linked")
        self.assertEqual((self.identity_rows(), self.consumption_rows()), rows)

    def test_row_14_a_copy_after_a_retirement_that_invalidated_the_pending_link(self):
        events = {
            "a cancellation": lambda: self.cancel(),
            "a sign-out": lambda: self.client.post("/logout", headers=SAME_ORIGIN),
            "a new link start": lambda: self.start_link(),
        }
        for name, event in events.items():
            with self.subTest(event=name):
                self.fresh_database()
                self.client = self.app.test_client()
                teacher = self.signed_in("teacher")
                self.link_until_pending()
                copy = self.copy_of()
                event()
                # Refused, with the right password too.
                self.assert_redirect(self.confirm(copy), "/account?google=failed")
                self.assertEqual(self.identity_rows(teacher["id"]), [])

    def test_row_15_a_copy_after_the_bound_accounts_session_key_rotated(self):
        rotations = {
            "a password change": lambda conn, uid: accounts.set_password(conn, uid, NEW_PASSWORD),
            "an operator sign-out": accounts.rotate_session_key,
            "a disable": lambda conn, uid: accounts.set_active(conn, uid, False),
            "a role change": lambda conn, uid: accounts.set_role(conn, uid, "student"),
        }
        for name, rotate in rotations.items():
            with self.subTest(rotation=name):
                self.fresh_database()
                self.client = self.app.test_client()
                teacher = self.signed_in("teacher")
                self.link_until_pending()
                pending = self.session()[oauth_state.LINK_SLOT]
                copy = self.copy_of()
                with closing(sqlite3.connect(self.db_path)) as conn:
                    rotate(conn, teacher["id"])
                    conn.commit()
                # At the route the copy's session no longer loads.
                for client in (self.client, copy):
                    self.assert_redirect(self.confirm(client, password=NEW_PASSWORD if "password" in name else PASSWORD),
                                         "/login?next=/auth/google/link/confirm")
                # At the service the write refuses at the key, with or without a record.
                with closing(sqlite3.connect(self.db_path)) as conn:
                    conn.execute("BEGIN IMMEDIATE")
                    outcome = oidc_accounts.confirm_link(
                        conn, pending, secret_key=self.app.config["SECRET_KEY"], session_key=teacher["session_key"],
                        session_iat=pending["siat"], now=self.clock, wall_now=time.time())
                    conn.commit()
                self.assertEqual(outcome, oidc_accounts.FAILED)
                self.assertEqual(self.identity_rows(), [])

    def test_row_16_another_account_signs_in_on_the_browser_without_a_sign_out(self):
        steps = {
            "the password form": lambda other: self.login(other["email"]),
            "an admin's password step": lambda other: self.login(other["email"]),
        }
        for name, sign_in in steps.items():
            with self.subTest(step=name):
                self.fresh_database()
                self.client = self.app.test_client()
                self.now += timedelta(seconds=31)
                teacher = self.signed_in("teacher")
                other = self.make("admin" if "admin" in name else "student")
                self.link_until_pending()
                copy = self.copy_of()
                sign_in(other)
                self.assertEqual(self.slots(), set())
                # Refused, with the first account's password too.
                self.assert_redirect(self.confirm(copy), "/account?google=failed")
                self.assertEqual(self.identity_rows(teacher["id"]), [])

    def test_the_key_rotates_during_the_code_exchange(self):
        rotations = {
            "a password change": lambda conn, uid: accounts.set_password(conn, uid, NEW_PASSWORD),
            "a disable": lambda conn, uid: accounts.set_active(conn, uid, False),
            "a role change": lambda conn, uid: accounts.set_role(conn, uid, "student"),
            "an operator sign-out": accounts.rotate_session_key,
        }
        for name, rotate in rotations.items():
            with self.subTest(rotation=name):
                self.fresh_database()
                self.client = self.app.test_client()
                teacher = self.signed_in("teacher")
                started = self.start_link()

                def during_exchange(rotate=rotate, uid=teacher["id"]):
                    with closing(sqlite3.connect(self.db_path)) as conn:
                        rotate(conn, uid)
                        conn.commit()

                self.google.during_exchange = during_exchange
                with self.assertLogs("learnpilot.oidc", logging.WARNING) as logs:
                    answered = self.return_from_google(started)
                # The second validation: the account is read again after the exchange.
                self.assert_redirect(answered, "/account?google=failed")
                self.assertIn("reason=account_changed", logs.output[-1])
                self.assertEqual(self.slots(), set())
                self.assertEqual((self.identity_rows(), len(self.consumption_rows())), ([], 1))

    def test_the_key_rotates_between_the_callback_and_the_confirmation(self):
        self.link_until_pending()
        copy = self.copy_of()
        self.client.post("/account/password", headers=SAME_ORIGIN, data={
            "current_password": PASSWORD, "new_password": NEW_PASSWORD, "confirm_password": NEW_PASSWORD})
        # This browser was re-keyed and its link retired; the copy carries the old key.
        self.assertEqual(self.slots(), set())
        self.assert_redirect(self.confirm(copy), "/login?next=/auth/google/link/confirm")
        self.assert_redirect(self.confirm(copy, password=NEW_PASSWORD), "/login?next=/auth/google/link/confirm")
        self.assertEqual(self.identity_rows(), [])

    def test_a_sign_out_while_the_browser_is_at_google(self):
        started = self.start_link()
        copy = self.copy_of()
        self.client.post("/logout", headers=SAME_ORIGIN)
        refused = self.return_from_google(started, copy)
        self.assert_redirect(refused, "/account?google=failed")
        self.assertEqual((self.slots(copy), self.google.requests), (set(), []))

    def test_the_callbacks_record_does_not_stand_in_for_the_confirmations(self):
        self.link_until_pending()
        pending = self.session()[oauth_state.LINK_SLOT]
        copy, another_copy = self.copy_of(), self.copy_of()
        self.assert_redirect(self.confirm(), "/account?google=linked")
        self.assertEqual(len(self.consumption_rows()), 2)   # the callback's and the confirmation's
        # With both present a second confirmation stores no identity and no record.
        self.assert_redirect(self.confirm(copy), "/account?google=already_linked")
        self.assertEqual((len(self.identity_rows()), len(self.consumption_rows())), (1, 2))
        # The test removes the confirmation's record from its own database (nothing in
        # the application deletes one). The callback's record says only that the
        # callback ran once: the confirmation is accepted again, and the uniqueness
        # rules still allow one identity and no more.
        self.execute("DELETE FROM oidc_consumed_transactions WHERE tx_hash = ?",
                     (self.consumption_key("link-confirm", pending["id"]),))
        self.assert_redirect(self.confirm(another_copy), "/account?google=already_linked")
        self.assertEqual(len(self.identity_rows()), 1)


# --------------------------------------------------------------------------- what a copy can still do


EVENTS = ("a cancellation", "a sign-out", "a sign-in as another account", "a new link start")


class CopyTestCase(ReplayTestCase):
    """Shared by the two residuals: the checks a pending link is held by on its own."""

    def run_event(self, name, client):
        if name == "a cancellation":
            return self.cancel(client)
        if name == "a sign-out":
            return client.post("/logout", headers=SAME_ORIGIN)
        if name == "a sign-in as another account":
            return self.login(self.make("student")["email"], client=client)
        return self.start_link(client)

    def expected_answer(self, name, *, cancel_code):
        return {
            "a cancellation": (302, f"/account?google={cancel_code}"),
            "a sign-out": (302, "/"),
            "a sign-in as another account": (302, "/"),
            "a new link start": (303, None),
        }[name]

    def scenario(self, event):
        """Returns ``(copy, account, pending)``: a browser that holds a pending link
        which nothing has invalidated, after ``event`` in the original browser."""
        raise NotImplementedError

    def rotate(self, how, account):
        actions = {
            "a password change": lambda conn: accounts.set_password(conn, account["id"], NEW_PASSWORD),
            "an operator sign-out": lambda conn: accounts.rotate_session_key(conn, account["id"]),
            "a disable": lambda conn: accounts.set_active(conn, account["id"], False),
            "a role change": lambda conn: accounts.set_role(conn, account["id"], "student"),
        }
        with closing(sqlite3.connect(self.db_path)) as conn:
            actions[how](conn)
            conn.commit()

    def assert_held_by_its_own_checks(self, event):
        # With the bound account's right password, before the expiry, on the unchanged
        # session key: it is linked. This is the residual, pinned so that the
        # documentation cannot understate it.
        copy, account, pending = self.scenario(event)
        self.assert_redirect(self.confirm(copy), "/account?google=linked")
        self.assertEqual(self.identity_rows(account["id"])[0][2], pending["sub"])

        # A wrong password: refused, one failure counted, and a blocked counter answers 429.
        copy, account, pending = self.scenario(event)
        wrong = self.confirm(copy, password="not the password")
        self.assertEqual(wrong.status_code, 200)
        self.assertIn("Грешна текуща парола.", self.body(wrong))
        self.assertEqual((self.failures(account["email"]), self.identity_rows()), (1, []))
        with patch.object(accounts, "check_password_hash", return_value=False):
            for _ in range(9):
                self.confirm(copy, password="wrong")
        self.assertEqual(self.confirm(copy).status_code, 429)
        self.assertEqual(self.identity_rows(), [])

        # After the bound account's session key rotated: the copy's session no longer loads.
        for how in ("a password change", "an operator sign-out", "a disable", "a role change"):
            copy, account, pending = self.scenario(event)
            self.rotate(how, account)
            self.assert_redirect(self.confirm(copy), "/login?next=/auth/google/link/confirm")
            self.assertEqual(self.identity_rows(), [], how)

        # With the bound session past its absolute lifetime.
        copy, account, pending = self.scenario(event)
        real = time.time
        with patch("time.time", lambda: real() + 30 * 24 * 3600 + 5):
            self.assert_redirect(self.confirm(copy), "/login?next=/auth/google/link/confirm")
        self.assertEqual(self.identity_rows(), [])

        # At its expiry and after it: expired. One second before: accepted.
        for offset, expected in ((0, "expired"), (500, "expired"), (-1, "linked")):
            copy, account, pending = self.scenario(event)
            self.clock = pending["exp"] + offset
            self.assert_redirect(self.confirm(copy), f"/account?google={expected}")
            self.assertEqual(len(self.identity_rows()), 1 if expected == "linked" else 0)

        # On another instance: refused in the first validation, before the password
        # is looked at and without a consumption row.
        copy, account, pending = self.scenario(event)
        rows = self.consumption_rows()
        with self.as_instance(OTHER_INSTANCE), patch.object(accounts, "check_password_hash") as verify:
            self.assert_redirect(self.confirm(copy), "/account?google=expired")
        verify.assert_not_called()
        self.assertEqual((self.consumption_rows(), self.identity_rows()), (rows, []))

        # With a link decision that refuses: that outcome, and no identity.
        copy, account, pending = self.scenario(event)
        other = self.make("teacher")
        self.give_identity(other["id"], pending["sub"])
        self.assert_redirect(self.confirm(copy), "/account?google=identity_taken")
        self.assertEqual(self.identity_rows(account["id"]), [])

        copy, account, pending = self.scenario(event, email="contested@example.bg")
        self.make("teacher", email="contested@example.bg")   # decision P1
        self.assert_redirect(self.confirm(copy), "/account?google=email_other_account")
        self.assertEqual(self.identity_rows(), [])


class UnrecordedRetirementTest(CopyTestCase):
    """Design 3.3 row 18: a captured cookie after a retirement that was not recorded."""

    def scenario(self, event, *, recorded=False, **identity):
        self.fresh_database()
        self.clock = 5000
        original = self.app.test_client()
        account = self.signed_in("teacher", client=original)
        self.link_until_pending(original, **identity)
        pending = self.session(original)[oauth_state.LINK_SLOT]
        copy = self.copy_of(original)
        if recorded:
            answered = self.run_event(event, original)
        else:
            # The storage fails during the retirement and works again afterwards.
            with self.failing_retirement(HeldLock(self)) as results:
                answered = self.run_event(event, original)
            self.assertEqual(results[0].pending, oauth_state.NOT_RECORDED)
        self.original, self.answered = original, answered
        return copy, account, pending

    def test_the_pending_link_in_the_copy_is_held_by_its_own_checks_after_a_cancellation(self):
        self.assert_held_by_its_own_checks("a cancellation")

    def test_after_each_event_the_copy_still_links_with_the_right_password(self):
        for event in EVENTS:
            with self.subTest(event=event):
                copy, account, pending = self.scenario(event)
                # The browser itself holds neither linking object of the retired link,
                # whatever the copy can still do: deleting the slots revoked nothing.
                self.assertNotIn(oauth_state.LINK_SLOT, self.session(self.original))
                if event != "a new link start":
                    self.assertEqual(self.slots(self.original), set())
                # What the person was told.
                status, location = self.expected_answer(event, cancel_code="cancel_unconfirmed")
                self.assertEqual(self.answered.status_code, status)
                if location:
                    self.assertEqual(self.answered.headers["Location"], location)
                self.assert_redirect(self.confirm(copy), "/account?google=linked")
                self.assertEqual(self.identity_rows(account["id"])[0][2], pending["sub"])

    def test_the_cancellation_did_not_say_cancelled(self):
        self.scenario("a cancellation")
        self.assert_redirect(self.answered, "/account?google=cancel_unconfirmed")
        page = self.body(self.original.get("/account?google=cancel_unconfirmed"))
        self.assertIn("Не успяхме да потвърдим отказа.", page)
        self.assertNotIn(CANCELLED_TEXT, page)

    def test_the_other_three_answered_exactly_as_without_the_failure(self):
        for event in EVENTS[1:]:
            with self.subTest(event=event):
                self.scenario(event, recorded=True)
                recorded = self.answered
                self.scenario(event)
                self.assertEqual(self.answered.status_code, recorded.status_code)
                if event != "a new link start":
                    self.assertEqual((self.answered.headers["Location"], self.answered.get_data()),
                                     (recorded.headers["Location"], recorded.get_data()))

    def test_the_same_copies_after_a_retirement_that_was_recorded_are_refused(self):
        for event in EVENTS:
            with self.subTest(event=event):
                copy, account, pending = self.scenario(event, recorded=True)
                # Refused in every case, with the right password too.
                self.assert_redirect(self.confirm(copy), "/account?google=failed")
                self.assertEqual(self.identity_rows(account["id"]), [])
                self.assertIn((self.consumption_key("link-confirm", pending["id"]), pending["exp"]),
                              self.consumption_rows())

    def test_a_second_cancellation_finds_nothing_and_the_copy_is_still_confirmed(self):
        copy, account, pending = self.scenario("a cancellation")
        # The storage works again and the same browser cancels once more: its session holds nothing.
        self.assert_redirect(self.cancel(self.original), "/account?google=link_not_pending")
        self.assertNotIn((self.consumption_key("link-confirm", pending["id"]), pending["exp"]), self.consumption_rows())
        self.assert_redirect(self.confirm(copy), "/account?google=linked")

    def test_a_link_transaction_in_a_copy_of_the_start_cookie(self):
        original = self.app.test_client()
        self.signed_in("teacher", client=original)
        started = self.start_link(original)
        location = started.headers["Location"]
        copy, second_copy = self.copy_of(original), self.copy_of(original)
        with self.failing_retirement(HeldLock(self)) as results:
            self.assert_redirect(self.cancel(original), "/account?google=cancel_unconfirmed")
        self.assertEqual(results[0].transaction, oauth_state.NOT_RECORDED)
        self.assertEqual(self.slots(original), set())
        # The copy's callback is accepted once. It yields a pending link in the
        # response to that request, and nothing else: no identity row.
        code = self.google.authorize(location)
        self.assert_redirect(self.callback(copy, code=code, state=self.state_of(location)), "/auth/google/link/confirm")
        self.assertEqual(self.identity_rows(), [])
        # A second callback with the same start cookie is refused by the callback's own record.
        again = self.callback(second_copy, code=self.google.authorize(location), state=self.state_of(location))
        self.assert_redirect(again, "/account?google=failed")
        self.assertEqual(self.slots(second_copy), set())
        # The identity is stored only by a confirmation with the right password.
        self.assertEqual(self.confirm(copy, password="not the password").status_code, 200)
        self.assertEqual(self.identity_rows(), [])
        self.assert_redirect(self.confirm(copy), "/account?google=linked")


class SuccessorTest(CopyTestCase):
    """Design 3.3 row 21: a pending link that a callback created for a copy of the
    start cookie. The original session never held it and cannot invalidate it."""

    def scenario(self, event, *, run=True, **identity):
        self.fresh_database()
        self.clock = 5000
        original = self.app.test_client()
        account = self.signed_in("teacher", client=original)
        started = self.start_link(original)
        copy = self.copy_of(original)
        # The copy reaches the callback first, with a Google authentication of its own.
        self.assert_redirect(self.return_from_google(started, copy, **identity), "/auth/google/link/confirm")
        successor = self.session(copy)[oauth_state.LINK_SLOT]
        self.assertIn(oauth_state.TX_SLOT, self.session(original))   # the original still holds the transaction
        self.original, self.started, self.answered = original, started, None
        if run:
            with self.failing_retirement(Tracing(self)) as results:
                self.answered = self.run_event(event, original)
            self.assertEqual(results[0].transaction, oauth_state.CONSUMED)
        return copy, account, successor

    def test_the_successor_is_held_by_its_own_checks_after_each_of_the_four_events(self):
        for event in EVENTS:
            with self.subTest(event=event):
                self.assert_held_by_its_own_checks(event)

    def test_the_original_is_told_nothing_it_did_not_establish(self):
        for event in EVENTS:
            with self.subTest(event=event):
                copy, account, successor = self.scenario(event)
                # The cancellation never says "cancelled"; the other three answer as without the copy.
                status, location = self.expected_answer(event, cancel_code="link_not_pending")
                self.assertEqual(self.answered.status_code, status)
                if location:
                    self.assertEqual(self.answered.headers["Location"], location)
                self.assertNotIn(oauth_state.LINK_SLOT, self.session(self.original))
                if event != "a new link start":
                    self.assertEqual(self.slots(self.original), set())
                # The successor is not invalidated: no row exists for its id. The
                # retirement of the transaction it came from wrote nothing for it.
                key = self.consumption_key("link-confirm", successor["id"])
                self.assertNotIn(key, [row[0] for row in self.consumption_rows()])

    def test_after_the_successor_was_confirmed_a_cancellation_answers_account_linked(self):
        copy, account, successor = self.scenario("a cancellation", run=False)
        self.assert_redirect(self.confirm(copy), "/account?google=linked")
        self.assert_redirect(self.cancel(self.original), "/account?google=account_linked")
        page = self.body(self.original.get("/account?google=account_linked"))
        self.assertIn("Профилът ти вече е свързан с Google.", page)
        self.assertNotIn(CANCELLED_TEXT, page)

    def test_the_originals_own_callback_arriving_after_the_copys_is_refused(self):
        copy, account, successor = self.scenario("a cancellation", run=False)
        refused = self.return_from_google(self.started, self.original, subject="the-original-person")
        self.assert_redirect(refused, "/account?google=failed")
        # No second pending link is created.
        self.assertEqual(self.slots(self.original), set())
        self.assertEqual(len(self.google.token_requests), 1)

    def test_the_transaction_has_expired_before_the_original_cancels(self):
        original = self.app.test_client()
        self.signed_in("teacher", client=original)
        started = self.start_link(original)
        transaction = self.session(original)[oauth_state.TX_SLOT]
        copy = self.copy_of(original)
        # The copy's callback comes late in the transaction's life, so the successor
        # expires later than the transaction it came from (by up to 180 seconds).
        self.clock = transaction["exp"] - 50
        self.assert_redirect(self.return_from_google(started, copy), "/auth/google/link/confirm")
        successor = self.session(copy)[oauth_state.LINK_SLOT]
        self.clock = transaction["exp"]
        self.assertLess(self.clock, successor["exp"])
        with self.failing_retirement(Tracing(self)) as results:
            answered = self.cancel(original)
        # An expired transaction is dead, although it was used before it expired.
        self.assertEqual(results[0].transaction, oauth_state.DEAD)
        self.assert_redirect(answered, "/account?google=link_not_pending")
        self.assert_redirect(self.confirm(copy), "/account?google=linked")


class TwoLimitsTest(ReplayTestCase):
    """Two limits of a cancellation's answer (design 1.6): stated limits, characterised.

    Neither test is a control. Each shows an ordering in which an answer was true of
    its own response and is overtaken afterwards.
    """

    def test_a_callback_of_the_same_browser_that_answers_after_the_cancellation(self):
        account = self.signed_in("teacher")
        started = self.start_link()
        second = self.copy_of()   # holds the same start cookie
        answers = {}

        def cancel_while_the_callback_waits():
            answers["cancel"] = self.cancel(second)

        self.google.during_exchange = cancel_while_the_callback_waits
        returned = self.return_from_google(started)
        # The cancellation found the transaction consumed. It removed both slots from
        # its own response and did not say "cancelled".
        self.assert_redirect(answers["cancel"], "/account?google=link_not_pending")
        self.assertEqual(self.slots(second), set())
        # The callback then completed, and ITS response holds a pending link.
        self.assert_redirect(returned, "/auth/google/link/confirm")
        self.assertIn(oauth_state.LINK_SLOT, self.session())
        # The first answer was a statement about its own request, and it stays true.
        # That pending link is confirmed only with the password, and cancelling it works.
        wrong = self.confirm(password="not the password")
        self.assertEqual((wrong.status_code, self.identity_rows()), (200, []))
        self.assert_redirect(self.cancel(), "/account?google=link_cancelled")
        self.assertEqual(self.identity_rows(account["id"]), [])

    def test_another_session_of_the_account_links_after_the_read(self):
        account = self.signed_in("teacher")
        other_browser = self.app.test_client()
        set_account_session(other_browser, account["id"], account["session_key"])
        self.link_until_pending(subject="mine")
        self.link_until_pending(other_browser, subject="theirs")
        mine = self.copy_of()
        # The first cancels: its object is invalidated, and the account has no identity at the read.
        self.assert_redirect(self.cancel(), "/account?google=link_cancelled")
        records = len(self.consumption_rows())
        # The second then confirms.
        self.assert_redirect(self.confirm(other_browser), "/account?google=linked")
        # The first browser's account page lists a Google identity below the message.
        # Both statements are true: this session's link was cancelled, and the
        # account was linked from elsewhere. The message never said otherwise.
        page = self.body(self.client.get("/account?google=link_cancelled"))
        self.assertIn(CANCELLED_TEXT, page)
        self.assertIn("Google — pupil@example.bg", page)
        # Nothing is retracted and nothing is written for the cancellation afterwards.
        self.assertEqual(len(self.consumption_rows()), records + 1)
        self.assertEqual([row[2] for row in self.identity_rows(account["id"])], ["theirs"])
        # A copy of the first browser's pending link is still refused.
        self.assert_redirect(self.confirm(mine), "/account?google=failed")


class TwoInstancesAndARetirementTest(ReplayTestCase):
    """Design 3.7: a stated limit, characterised. It is not a control; the deployment
    test pins ``preload_app``, which is what keeps a run to one instance value."""

    def test_a_pending_link_of_one_instance_is_cancelled_at_another(self):
        account = self.signed_in("teacher")
        self.link_until_pending()
        pending = self.session()[oauth_state.LINK_SLOT]
        issuing_instances_copy = self.copy_of()
        rows = self.consumption_rows()
        with self.as_instance(OTHER_INSTANCE), self.failing_retirement(Tracing(self)) as results, \
                self.assertNoLogs("learnpilot.oidc"):
            answered = self.cancel()
        # Dead: no record is written and no line is logged.
        self.assertEqual(results[0].pending, oauth_state.DEAD)
        self.assertEqual(self.consumption_rows(), rows)
        # Not "cancelled": a dead object never gives that.
        self.assert_redirect(answered, "/account?google=link_not_pending")
        # The instance that issued it still confirms it before its expiry.
        self.assert_redirect(self.confirm(issuing_instances_copy), "/account?google=linked")
        self.assertEqual(self.identity_rows(account["id"])[0][2], pending["sub"])

    def test_a_callback_or_a_confirmation_that_reaches_the_other_instance_answers_expired(self):
        self.signed_in("teacher")
        started = self.start_link()
        with self.as_instance(OTHER_INSTANCE):
            self.assert_redirect(self.return_from_google(started), "/account?google=expired")
        self.link_until_pending()
        with self.as_instance(OTHER_INSTANCE):
            self.assert_redirect(self.confirm(), "/account?google=expired")
        self.assertEqual(self.identity_rows(), [])


if __name__ == "__main__":
    unittest.main()
