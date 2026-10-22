"""Password recovery 8.3: the order of the checks, the transactions, the dispatch and the failures.

Test plan RF1 to RF4 at the routes, RI3 and the caller obligations carried from scope 8.2
(PASSWORD_RECOVERY_8_2_STATUS.md): the validation order of a redemption, commit before any
browser state, logging or dispatch, one dispatch per committed issuance, no settlement after
``NotSendable``, no retry, a failed commit that is never presented as a reset, and what
concurrent or repeated POSTs do. Faults are real exceptions raised in place of a statement or of
``COMMIT`` on the request's own connection; the code under test rolls back, as in production.
Temporary databases and an in-memory mailer only.
"""

import threading
import unittest
from unittest.mock import patch

from tests.recovery_route_support import EMAIL, NEW_PASSWORD, OLD_PASSWORD, RecoveryRouteTestCase
from web.blueprints import recovery as recovery_blueprint
from web.recovery_settings import CONFIG_KEY
from web.services import accounts, recovery as recovery_service
from web.services.mail import SendOutcome

WRONG = "00000000"
FAILED = "Нещо се обърка и паролата не беше сменена. Опитай отново."
UNCERTAIN = ("Не успяхме да потвърдим дали паролата е сменена. Опитай да влезеш с новата парола; "
             "ако не става, започни отначало.")
REQUEST_FAILED = "Нещо се обърка и нищо не беше записано. Опитай отново."
REQUEST_UNCERTAIN = "Не успяхме да потвърдим дали заявката е записана. Опитай отново след малко."
NO_RECOVERY = "Заявката за нова парола е изтекла или е започната от друг браузър. Започни отново."
SUCCESS_TEXT = "Паролата е сменена"


class Recorder:
    """The calls of the collaborators a route makes, in order, with the arguments that matter."""

    def __init__(self, test):
        self.calls = []
        for module, name, label in ((recovery_service, "pending_view", "pending_view"),
                                    (accounts, "password_problem", "password_problem"),
                                    (recovery_service, "was_redeemed", "was_redeemed"),
                                    (recovery_service, "redeem", "redeem"),
                                    (recovery_service, "request", "request"),
                                    (recovery_service, "resend", "resend"),
                                    (recovery_service, "cancel", "cancel"),
                                    (recovery_service, "dispatch", "dispatch"),
                                    (recovery_service, "settle_send", "settle")):
            real = getattr(module, name)

            def wrapper(*args, _real=real, _label=label, **kwargs):
                self.calls.append((_label, args, kwargs))
                return _real(*args, **kwargs)

            patcher = patch.object(module, name, wrapper)
            patcher.start()
            test.addCleanup(patcher.stop)

    def names(self):
        return [call[0] for call in self.calls]

    def count(self, label):
        return self.names().count(label)

    def clear(self):
        self.calls.clear()


class ValidationOrderTest(RecoveryRouteTestCase):
    """The order of design 2.4 and caller obligation 5: gate, signed-in redirect, slot, ``pending_view``,
    the password against the address the row holds, the exact confirmation, and only then the
    transaction. A refusal at an earlier step answers before a later step reads anything."""

    def setUp(self):
        super().setUp()
        self.recorder = Recorder(self)

    def opened(self):
        """Count the request connections opened."""
        counter = {"opened": 0}
        real = recovery_blueprint.get_request_db

        def counting():
            counter["opened"] += 1
            return real()

        patcher = patch.object(recovery_blueprint, "get_request_db", counting)
        patcher.start()
        self.addCleanup(patcher.stop)
        return counter

    def test_the_gate_answers_before_anything_else_is_read(self):
        self.verified()
        self.recover()
        code = self.last_code()
        self.recorder.clear()
        opened = self.opened()
        self.app.config[CONFIG_KEY] = None
        self.assertEqual(self.redeem(code).status_code, 404)
        self.assertEqual(self.begin().status_code, 404)
        self.assertEqual(self.resend().status_code, 404)
        self.assertEqual(self.cancel().status_code, 404)
        self.assertEqual(self.recorder.calls, [])
        self.assertEqual(opened["opened"], 0)

    def test_the_signed_in_redirect_comes_before_the_slot_and_reads_nothing_of_recovery(self):
        self.verified()
        self.recover()
        code = self.last_code()
        slot = self.slot()
        client = self.app.test_client()
        self.sign_in(self.make("student"), client=client)
        self.set_slot(slot, client)
        self.recorder.clear()
        opened = self.opened()
        for response in (self.redeem(code, client=client), self.begin(client=client), self.resend(client=client),
                         self.cancel(client=client)):
            self.assertEqual(response.status_code, 303)
        self.assertEqual(self.recorder.calls, [])
        self.assertEqual(opened["opened"], 0)       # the principal is loaded by the application's own hook

    def test_no_slot_answers_before_any_row_is_read(self):
        self.verified()
        self.recover()
        code = self.last_code()
        self.recorder.clear()
        opened = self.opened()
        other = self.app.test_client()
        self.assertIn(NO_RECOVERY, self.body(self.redeem(code, client=other)))
        self.assertEqual(self.recorder.calls, [])
        self.assertEqual(opened["opened"], 0)

    def test_the_row_is_read_first_and_a_missing_one_stops_before_the_password_is_judged(self):
        self.verified()
        self.recover()
        self.execute("DELETE FROM password_recoveries")
        self.recorder.clear()
        self.assertIn(NO_RECOVERY, self.body(self.redeem(WRONG, "short")))
        self.assertEqual(self.recorder.names(), ["pending_view", "was_redeemed"])

    def test_the_password_is_judged_against_the_address_of_the_row_before_the_confirmation_and_the_service(self):
        self.verified()
        self.recover()
        code = self.last_code()
        self.recorder.clear()
        self.redeem(code, EMAIL, EMAIL)                          # equal to the address: a password error
        self.assertEqual(self.recorder.names(), ["pending_view", "password_problem"])
        self.assertEqual(self.recorder.calls[1][1][1], EMAIL)     # judged with view.email, the address the row holds
        self.recorder.clear()
        self.redeem(code, NEW_PASSWORD, "different")              # a mismatch: after the password rules
        self.assertEqual(self.recorder.names(), ["pending_view", "password_problem"])
        self.recorder.clear()
        self.redeem(code, NEW_PASSWORD)                           # all clear: the transaction
        self.assertEqual(self.recorder.names(), ["pending_view", "password_problem", "redeem", "password_problem",
                                                 "password_problem"])
        # The two after redeem are the service's own: its pre-check and the hashing in set_password.

    def test_the_service_is_called_with_the_exact_submitted_strings_and_the_row_binding(self):
        self.verified()
        self.recover()
        code = self.last_code()
        slot_token = self.slot()["t"]
        self.recorder.clear()
        self.redeem(f"  {code}  ", "  exact   spaces\tand tab  ")
        redeem, = [call for call in self.recorder.calls if call[0] == "redeem"]
        _, args, kwargs = redeem
        self.assertEqual(args[2:], (f"  {code}  ", "  exact   spaces\tand tab  "))
        self.assertEqual(args[1], recovery_service.binding_hash(self.app.config["SECRET_KEY"], slot_token))
        self.assertTrue(self.authenticates(EMAIL, "  exact   spaces\tand tab  "))


class FailureTest(RecoveryRouteTestCase):
    """RF1 to RF4 at the routes: certain rollback and unknown outcome are told apart, and no failed
    commit is ever presented as a reset."""

    def reset(self):
        account = self.verified()
        self.recover()
        return account, self.last_code()

    def assertNeverASuccess(self, response):
        page = self.body(response)
        self.assertNotIn(SUCCESS_TEXT, page)
        self.assertNotEqual(response.status_code, 303)
        self.assertNoAccountSession()

    def test_an_exception_at_each_statement_of_the_redemption_rolls_everything_back(self):
        """RF1: the account, the key, the throttle, this row and the others are exactly as before;
        the text says the password was not changed; the slot is kept; a retry then redeems."""
        account, code = self.reset()
        statements = {
            "the attempt is counted": "code_attempts = code_attempts + 1",
            "the code is cleared": "SET code_hash = NULL",
            "the account is read": "FROM users",
            "the password is set": "UPDATE users SET",
            "the row is closed": "closed_at = ?",
        }
        for name, fragment in statements.items():
            before = self.digest()
            state = self.flaky(fail_sql=[fragment])
            response = self.redeem(code)
            with self.subTest(statement=name):
                self.assertEqual(state["failed_sql"], [fragment])
                self.assertEqual(response.status_code, 500)
                self.assertIn(FAILED, self.body(response))
                self.assertNotIn(UNCERTAIN, self.body(response))
                self.assertNeverASuccess(response)
                self.assertEqual(self.digest(), before)
                self.assertIsNotNone(self.slot())
                self.assertTrue(self.authenticates(EMAIL, OLD_PASSWORD))
            self.lift_fault()
        response = self.redeem(code)                          # the same code, afterwards
        self.assertEqual(response.status_code, 303, self.body(response))
        self.assertTrue(self.authenticates(EMAIL, NEW_PASSWORD))
        self.assertEqual(self.account(account["id"])["email"], EMAIL)

    def test_a_commit_that_raised_without_effect_is_told_as_unknown_never_as_a_reset(self):
        """RF2 (i): the failure is injected before anything is written."""
        _, code = self.reset()
        before = self.digest()
        self.flaky(1)                                           # the redemption's commit
        response = self.redeem(code)
        self.assertEqual(response.status_code, 500)
        self.assertIn(UNCERTAIN, self.body(response))
        self.assertNotIn(FAILED, self.body(response))
        self.assertNeverASuccess(response)
        self.assertEqual(self.digest(), before)
        self.assertIsNotNone(self.slot())
        self.lift_fault()
        retry = self.redeem(code)                                     # a retry redeems, as the first would have
        self.assertEqual((retry.status_code, retry.headers["Location"]), (303, "/login?recovery=done"))
        self.assertTrue(self.authenticates(EMAIL, NEW_PASSWORD))

    def test_a_commit_that_took_effect_and_then_raised_is_told_as_unknown_and_its_retry_goes_to_login(self):
        """RF2 (ii): the commit reached the database and reported an error. The first page does not say
        the password was not changed and does not claim it was; the slot is kept. The retry finds its
        own row redeemed: a ``303`` to ``/login`` (the validated ``next`` kept, no ``recovery=done``),
        and it writes, sends, hashes and signs in nothing."""
        account = self.verified()
        self.recover(next="/account/attempts")
        code = self.last_code()
        self.flaky(commits_then_raise=[1])
        response = self.redeem(code)
        self.assertEqual(response.status_code, 500)
        self.assertIn(UNCERTAIN, self.body(response))
        self.assertNotIn(FAILED, self.body(response))
        self.assertNeverASuccess(response)
        self.assertIsNotNone(self.slot())
        self.assertTrue(self.authenticates(EMAIL, NEW_PASSWORD))         # it did take effect
        self.assertEqual(self.recoveries()[0]["state"], "redeemed")
        self.lift_fault()
        before, sent = self.digest(), len(self.mailer.messages)
        hashed = self.spy(accounts, "generate_password_hash")
        redeems = self.spy(recovery_service, "redeem")
        retry = self.redeem(code)
        self.assertEqual((retry.status_code, retry.headers["Location"]), (303, "/login?next=/account/attempts"))
        self.assertNotIn("recovery", retry.headers["Location"])
        self.assertNotIn(NO_RECOVERY, self.body(retry))
        self.assertEqual(self.digest(), before)
        self.assertEqual(len(self.mailer.messages), sent)
        self.assertEqual((hashed, redeems), ([], []))
        self.assertIsNone(self.slot())
        self.assertNoAccountSession()
        self.assertTrue(self.authenticates(EMAIL, NEW_PASSWORD))
        self.assertEqual(self.account(account["id"])["email"], EMAIL)

    def test_a_rollback_that_raised_is_told_as_unknown_too(self):
        """RF2: an exception, then a rollback that also raises: the connection's state is unknown."""
        _, code = self.reset()
        self.flaky(fail_sql=["UPDATE users SET"], fail_rollback=True)
        response = self.redeem(code)
        self.assertEqual(response.status_code, 500)
        self.assertIn(UNCERTAIN, self.body(response))
        self.assertNotIn(FAILED, self.body(response))
        self.assertNeverASuccess(response)

    def test_set_password_raising_a_value_or_lookup_error_is_a_rollback_and_never_a_success(self):
        """RF4."""
        _, code = self.reset()
        for error in (ValueError("forced"), LookupError("forced")):
            before = self.digest()
            with patch.object(accounts, "set_password", side_effect=error):
                response = self.redeem(code)
            with self.subTest(error=type(error).__name__):
                self.assertEqual(response.status_code, 500)
                self.assertIn(FAILED, self.body(response))
                self.assertNeverASuccess(response)
                self.assertEqual(self.digest(), before)
        self.assertEqual(self.redeem(code).status_code, 303)

    def test_a_failed_request_commit_sets_no_slot_and_sends_nothing(self):
        """RF3: nothing the failed request decided is acted on."""
        self.verified()
        for variant, kwargs, effective in (("commit failed", {"fail": (1,)}, False),
                                           ("commit took effect", {"commits_then_raise": (1,)}, True)):
            client = self.app.test_client()
            state = self.flaky(*kwargs.get("fail", ()), commits_then_raise=kwargs.get("commits_then_raise", ()))
            sent = len(self.mailer.messages)
            response = self.begin(client=client)
            with self.subTest(variant=variant):
                self.assertEqual(response.status_code, 500)
                self.assertIn(REQUEST_UNCERTAIN, self.body(response))
                self.assertNotIn("Изпратихме", self.body(response))
                self.assertEqual(self.session(client), {})
                self.assertEqual(len(self.mailer.messages), sent)
                self.assertEqual(state["commits"], 1)         # no settlement either
                rows = self.recoveries()
                self.assertEqual(len(rows) > 0, effective)
                if effective:
                    self.assertEqual([send["outcome"] for send in self.sends()], ["started"])   # counts, never sent
                    self.execute("DELETE FROM registration_sends")
                    self.execute("DELETE FROM password_recoveries")
            self.lift_fault()
            self.advance(1)

    def test_a_failed_request_writer_is_a_certain_rollback_with_the_first_failure_text(self):
        self.verified()
        before = self.digest()
        with patch.object(recovery_service, "request", side_effect=RuntimeError("forced")):
            response = self.begin()
        self.assertEqual(response.status_code, 500)
        self.assertIn(REQUEST_FAILED, self.body(response))
        self.assertNotIn(REQUEST_UNCERTAIN, self.body(response))
        self.assertEqual(self.digest(), before)
        self.assertEqual(self.session(), {})
        self.assertEqual(self.mailer.messages, [])

    def test_a_failed_resend_or_cancel_keeps_the_slot_and_sends_nothing(self):
        self.verified()
        self.recover()
        self.advance(61)
        sent = len(self.mailer.messages)
        for name, call in (("resend", self.resend), ("cancel", self.cancel)):
            for variant, kwargs in (("commit failed", {"fail": (1,)}), ("commit took effect", {"commits_then_raise": (1,)})):
                self.flaky(*kwargs.get("fail", ()), commits_then_raise=kwargs.get("commits_then_raise", ()))
                response = call()
                with self.subTest(route=name, variant=variant):
                    self.assertEqual(response.status_code, 500)
                    self.assertIn(REQUEST_UNCERTAIN, self.body(response))
                    self.assertEqual(len(self.mailer.messages), sent)
                    self.assertIsNotNone(self.slot())
                self.lift_fault()
                if self.recoveries()[0]["state"] != "pending":
                    break
                self.advance(61)
                sent = len(self.mailer.messages)

    def test_an_unavailable_service_during_the_transaction_is_an_answer_not_a_failure(self):
        """The hold or the clock decided under the lock: 503, the code and attempts kept."""
        self.verified()
        self.recover()
        code = self.last_code()
        self.set_control(hold="restore")
        before = self.digest()
        response = self.redeem(code)
        self.assertEqual(response.status_code, 503)
        self.assertIn("Възстановяването на парола временно не е достъпно. Опитай по-късно.", self.body(response))
        self.assertEqual(self.digest(), before)
        self.assertIsNotNone(self.slot())
        self.assertEqual(self.recoveries()[0]["code_attempts"], 0)
        self.assertTrue(self.authenticates(EMAIL, OLD_PASSWORD))


class DispatchTest(RecoveryRouteTestCase):
    """Caller obligations 1 to 4: commit first, dispatch each issuance's own result once, settle it
    separately with both identifiers, never after ``NotSendable``, never retry."""

    def setUp(self):
        super().setUp()
        self.recorder = Recorder(self)
        self.verified()

    def test_a_request_dispatches_its_own_committed_result_once_and_settles_it_with_both_identifiers(self):
        self.recover()
        dispatch, = [call for call in self.recorder.calls if call[0] == "dispatch"]
        settle, = [call for call in self.recorder.calls if call[0] == "settle"]
        issued = dispatch[1][2]
        self.assertIs(issued.outcome, recovery_service.Outcome.ISSUED)
        self.assertIsNotNone(issued.token)                       # the first issuance carries the token
        send, = self.sends()
        self.assertEqual((settle[1][1], settle[1][2]), (send["id"], send["issuance_id"]))
        self.assertEqual((issued.send_id, issued.issuance_id), (send["id"], send["issuance_id"]))
        self.assertEqual(settle[1][3], SendOutcome.ACCEPTED)
        self.assertEqual(self.recorder.names().count("dispatch"), 1)
        self.assertEqual(len(self.mailer.messages), 1)

    def test_a_resend_dispatches_its_own_new_generation_not_the_previous_one(self):
        self.recover()
        self.recorder.clear()
        self.advance(61)
        self.assertEqual(self.resend().status_code, 303)
        dispatch, = [call for call in self.recorder.calls if call[0] == "dispatch"]
        settle, = [call for call in self.recorder.calls if call[0] == "settle"]
        issued = dispatch[1][2]
        first, second = self.sends()
        self.assertIsNone(issued.token)
        self.assertEqual((issued.send_id, issued.issuance_id), (second["id"], second["issuance_id"]))
        self.assertEqual((settle[1][1], settle[1][2]), (second["id"], second["issuance_id"]))
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(len(self.mailer.messages), 2)

    def test_nothing_is_dispatched_before_the_commit_and_the_connection_is_idle_when_it_is(self):
        """The dispatch sees no open transaction, and the slot is not yet readable by the mail step."""
        seen = []
        real = recovery_service.dispatch

        def watching(conn, mailer, issued, *, secret_key):
            seen.append(conn.in_transaction)
            return real(conn, mailer, issued, secret_key=secret_key)

        with patch.object(recovery_service, "dispatch", watching):
            self.recover()
            self.advance(61)
            self.resend()
        self.assertEqual(seen, [False, False])

    def test_not_sendable_means_nothing_sent_nothing_settled_and_no_second_try(self):
        with patch.object(recovery_service, "dispatch", side_effect=recovery_service.NotSendable("refused")):
            response = self.begin()
            self.assertEqual(response.status_code, 303)           # the request was committed
        self.assertEqual(self.recorder.count("settle"), 0)
        self.assertEqual(self.mailer.messages, [])
        send, = self.sends()
        self.assertEqual(send["outcome"], "started")              # counted, never sent
        # Nothing later reconstructs and sends it: a new request is a new issuance.
        self.advance(61)
        self.assertEqual(self.resend().status_code, 303)
        self.assertEqual(len(self.mailer.messages), 1)
        self.assertEqual([row["outcome"] for row in self.sends()], ["started", "accepted"])

    def test_the_real_dispatch_refuses_a_result_that_is_not_the_committed_current_one(self):
        """A real NotSendable from the service (a stale generation), through the route's own call."""
        self.recover()
        calls = [call for call in self.recorder.calls if call[0] == "dispatch"]
        issued = calls[0][1][2]
        self.recorder.clear()
        sends_before = len(self.mailer.messages)
        # The generation is settled; dispatching it again is refused and settles nothing.
        from web.db import connect_db
        connection = connect_db(self.db_path)
        try:
            with self.assertRaises(recovery_service.NotSendable):
                recovery_service.dispatch(connection, self.mailer, issued,
                                          secret_key=self.app.config["SECRET_KEY"])
        finally:
            connection.close()
        self.assertEqual(len(self.mailer.messages), sends_before)
        self.assertEqual(self.recorder.count("settle"), 0)

    def test_any_other_dispatch_failure_is_logged_not_settled_and_not_retried(self):
        with patch.object(recovery_service, "dispatch", side_effect=RuntimeError("forced")):
            with self.assertLogs("learnpilot.recovery", "WARNING") as logs:
                response = self.begin()
        self.assertEqual(response.status_code, 303)
        self.assertEqual(self.recorder.count("settle"), 0)
        self.assertEqual(len([line for line in logs.output if "dispatch_failed error=RuntimeError" in line]), 1)
        self.assertEqual(self.sends()[0]["outcome"], "started")

    def test_a_mailer_that_raises_is_unknown_and_called_once(self):
        calls = []

        def failing(message):
            calls.append(message)
            raise OSError("down")

        self.mailer.outcome = failing
        response = self.begin()
        self.assertEqual(response.status_code, 303)
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.sends()[0]["outcome"], "unknown")

    def test_a_failed_settlement_is_left_started_and_not_retried(self):
        state = self.flaky(2)                                      # the settlement's commit
        with self.assertLogs("learnpilot.recovery", "WARNING") as logs:
            response = self.begin()
        self.assertEqual(response.status_code, 303)                # the committed request still succeeds
        self.assertEqual(state["commits"], 2)                      # one try
        self.assertEqual(self.recorder.count("settle"), 1)
        self.assertEqual(self.recorder.count("dispatch"), 1)
        self.assertEqual(len(self.mailer.messages), 1)
        self.assertEqual(self.sends()[0]["outcome"], "started")   # safe: counted, code usable
        self.assertTrue([line for line in logs.output if "settlement_failed error=OperationalError" in line])
        # The code still redeems.
        self.assertEqual(self.redeem(self.last_code()).status_code, 303)

    def test_a_resend_failure_after_the_commit_does_not_send_a_second_message(self):
        self.recover()
        self.advance(61)
        self.recorder.clear()
        self.flaky(2)                                              # the settlement of the resend
        self.assertEqual(self.resend().status_code, 303)
        self.assertEqual(self.recorder.count("dispatch"), 1)
        self.assertEqual(len(self.mailer.messages), 2)

    def test_repeated_posts_are_new_issuances_each_dispatched_once(self):
        """A double click is two requests: two issuances, two dispatches, never one issuance twice."""
        client = self.app.test_client()
        self.assertEqual(self.begin(client=client).status_code, 303)
        self.advance(61)
        self.assertEqual(self.begin(client=client).status_code, 303)
        self.assertEqual(self.recorder.count("dispatch"), 2)
        self.assertEqual(len(self.mailer.messages), 2)
        self.assertEqual([row["state"] for row in self.recoveries()], ["void", "pending"])
        self.assertEqual(len({row["issuance_id"] for row in self.sends()}), 2)

    def test_a_refusal_dispatches_nothing(self):
        self.recover()
        self.recorder.clear()
        self.assertIn("Можеш да поискаш нов код след малко.", self.body(self.resend()))   # inside the wait
        self.cancel()
        self.assertEqual(self.recorder.count("dispatch"), 0)
        self.assertEqual(self.recorder.count("settle"), 0)


class ConcurrencyTest(RecoveryRouteTestCase):
    """Concurrent POSTs through the routes: SQLite's one writer serializes them, and nothing escapes."""

    def race(self, *tasks):
        barrier = threading.Barrier(len(tasks))
        results = [None] * len(tasks)

        def run(index, task):
            barrier.wait(timeout=10)
            try:
                results[index] = task()
            except BaseException as exc:                      # a test failure, reported below
                results[index] = exc

        threads = [threading.Thread(target=run, args=(index, task)) for index, task in enumerate(tasks)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        for result in results:
            self.assertNotIsInstance(result, BaseException, repr(result))
        return results

    def test_two_correct_redemptions_from_two_copies_of_one_browser_redeem_once(self):
        """Two tabs post the correct code together: the first redeems and commits; the second finds a
        final row (``NOT_PENDING (redeemed)``), writes nothing and its password is not applied."""
        self.verified()
        self.recover()
        code = self.last_code()
        slot = self.slot()
        clients = [self.app.test_client() for _ in range(2)]
        for client in clients:
            self.set_slot(slot, client)
        results = self.race(
            lambda: self.redeem(code, "winner password one", client=clients[0]),
            lambda: self.redeem(code, "winner password two", client=clients[1]))
        locations = sorted(result.headers.get("Location", "") for result in results)
        self.assertEqual(locations, ["/login", "/login?recovery=done"], [self.body(r)[:80] for r in results])
        winner = ("winner password one" if results[0].headers["Location"] == "/login?recovery=done"
                  else "winner password two")
        other = "winner password two" if winner == "winner password one" else "winner password one"
        self.assertTrue(self.authenticates(EMAIL, winner))
        self.assertFalse(self.authenticates(EMAIL, other))
        self.assertFalse(self.authenticates(EMAIL, OLD_PASSWORD))
        self.assertEqual(self.recoveries()[0]["state"], "redeemed")

    def test_two_rows_of_one_account_both_correct_only_one_redeems_the_other_is_invalidated(self):
        self.verified()
        first, second = self.app.test_client(), self.app.test_client()
        self.recover(client=first)
        code_first = self.last_code()
        self.advance(1)
        self.recover(client=second)
        code_second = self.last_code()
        results = self.race(
            lambda: self.redeem(code_first, "first browser password", client=first),
            lambda: self.redeem(code_second, "second browser password", client=second))
        statuses = sorted(result.status_code for result in results)
        self.assertEqual(statuses, [200, 303])
        loser = next(result for result in results if result.status_code == 200)
        self.assertIn("Не успяхме да зададем нова парола. Започни отново.", self.body(loser))
        winner = "first browser password" if results[0].status_code == 303 else "second browser password"
        self.assertTrue(self.authenticates(EMAIL, winner))

    def test_five_wrong_codes_at_once_count_five_and_a_sixth_is_refused(self):
        self.verified()
        self.recover()
        code = self.last_code()
        slot = self.slot()
        clients = [self.app.test_client() for _ in range(5)]
        for client in clients:
            self.set_slot(slot, client)
        results = self.race(*[(lambda c=client: self.redeem(WRONG, client=c)) for client in clients])
        self.assertTrue(all("Грешен или изтекъл код." in self.body(result) for result in results))
        self.assertEqual(self.recoveries()[0]["code_attempts"], 5)
        self.assertIn("Твърде много грешни опити. Поискай нов код.", self.body(self.redeem(code)))
        self.assertTrue(self.authenticates(EMAIL, OLD_PASSWORD))

    def test_simultaneous_resends_admit_one_and_every_admitted_one_is_sent_once(self):
        self.verified()
        self.recover()
        self.advance(61)
        slot = self.slot()
        clients = [self.app.test_client() for _ in range(4)]
        for client in clients:
            self.set_slot(slot, client)
        spy = Recorder(self)
        results = self.race(*[(lambda c=client: self.resend(client=c)) for client in clients])
        admitted = [r for r in results if r.status_code == 303]
        self.assertEqual(len(admitted), 1)
        self.assertEqual(spy.count("dispatch"), 1)
        self.assertEqual(len(self.mailer.messages), 2)
        self.assertEqual([row["send_count"] for row in self.recoveries()], [2])

    def test_simultaneous_requests_for_one_address_are_each_one_issuance_within_the_caps(self):
        self.verified()
        clients = [self.app.test_client() for _ in range(7)]
        spy = Recorder(self)
        results = self.race(*[(lambda c=client: self.begin(client=c)) for client in clients])
        admitted = [r for r in results if r.status_code == 303]
        refused = [r for r in results if r.status_code == 429]
        self.assertEqual(len(admitted) + len(refused), 7, [r.status_code for r in results])
        self.assertEqual(len(admitted), 5)                         # five an hour for one address
        self.assertEqual(spy.count("dispatch"), 5)
        self.assertEqual(len(self.mailer.messages), 5)
        self.assertEqual(len(self.sends()), 5)

    def test_an_observer_polling_and_guessing_while_the_owner_resets_gets_only_ordinary_answers(self):
        self.verified()
        observer, owner = self.app.test_client(), self.app.test_client()
        self.recover(client=observer)
        self.advance(1)
        self.recover(client=owner)
        code = self.last_code()
        results = self.race(
            lambda: self.redeem(code, NEW_PASSWORD, client=owner),
            lambda: observer.get("/recover/verify"),
            lambda: self.redeem(WRONG, client=observer),
            lambda: self.resend(client=observer))
        self.assertEqual(results[0].status_code, 303)
        for result in results[1:3]:
            self.assertEqual(result.status_code, 200)
        self.assertIn(results[3].status_code, (200, 303))
        for result in results[1:]:
            self.assertNotIn(NO_RECOVERY, self.body(result))


if __name__ == "__main__":
    unittest.main()
