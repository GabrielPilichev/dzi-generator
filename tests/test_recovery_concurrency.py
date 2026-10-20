"""Password recovery (scope 8.2): concurrency (RX1–RX8). Two or more connections to one
database file, threads released together by a barrier; SQLite's one writer serializes
them. Each thread opens and closes its own connection.
"""

import io
import os
import sqlite3
import threading
from contextlib import redirect_stderr, redirect_stdout
from datetime import timedelta
from unittest import mock

from src import manage_accounts
from tests.recovery_service_support import EMAIL, KEY, NEW_PASSWORD, RecoveryServiceTestCase, code_of
from tests.registration_support import password_hash, student
from web.services import recovery, registration

Outcome = recovery.Outcome


class ConcurrencyTestCase(RecoveryServiceTestCase):
    def race(self, *calls):
        """Run each ``call(conn)`` on its own connection, released together; return results
        (or exceptions) in call order."""
        barrier = threading.Barrier(len(calls))
        results = [None] * len(calls)

        def run(index, call):
            conn = sqlite3.connect(self.path, timeout=30)
            conn.execute("PRAGMA foreign_keys = ON")
            try:
                barrier.wait()
                results[index] = call(conn)
            except BaseException as exc:  # reported to the test, never swallowed
                results[index] = exc
            finally:
                conn.close()

        threads = [threading.Thread(target=run, args=(i, c)) for i, c in enumerate(calls)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(60)
        for result in results:
            if isinstance(result, BaseException):
                raise result
        return results

    def redeem_on(self, issued, code, password=NEW_PASSWORD):
        return lambda conn: self.tx(recovery.redeem, self.binding(issued), code, password, conn=conn,
                                    secret_key=KEY, clock=self.clock)

    def cli_on(self, *args, stdin=None):
        def run(conn):
            out, err = io.StringIO(), io.StringIO()
            patches = [mock.patch.dict(os.environ, {"DZI_SECRET_KEY": KEY})]
            if stdin is not None:
                patches.append(mock.patch("sys.stdin", io.StringIO(stdin)))
            for patch in patches:
                patch.start()
            try:
                with redirect_stdout(out), redirect_stderr(err):
                    return manage_accounts.main(["--db", str(self.path), *args])
            finally:
                for patch in reversed(patches):
                    patch.stop()
        return run


class RedemptionRaceTest(ConcurrencyTestCase):
    def test_rx1_two_correct_redemptions_of_one_row(self):
        self.verified()
        issued = self.request()
        code = code_of(issued)
        first, second = self.race(self.redeem_on(issued, code, "first password 111"),
                                  self.redeem_on(issued, code, "second password 222"))
        outcomes = sorted([first.outcome.value, second.outcome.value])
        self.assertEqual(outcomes, ["not_pending", "redeemed"])
        winner = "first password 111" if first.outcome is Outcome.REDEEMED else "second password 222"
        loser = "second password 222" if winner.startswith("first") else "first password 111"
        self.assertTrue(self.authenticates(EMAIL, winner))
        self.assertFalse(self.authenticates(EMAIL, loser))

    def test_rx2_two_rows_of_one_account_both_correct(self):
        self.verified()
        a = self.request()
        b = self.request()
        first, second = self.race(self.redeem_on(a, code_of(a), "first password 111"),
                                  self.redeem_on(b, code_of(b), "second password 222"))
        self.assertEqual(sorted([first.outcome.value, second.outcome.value]), ["invalidated", "redeemed"])
        winner, loser = (a, b) if first.outcome is Outcome.REDEEMED else (b, a)
        self.assertScrubbed(winner.recovery_id, "redeemed")
        self.assertScrubbed(loser.recovery_id, "void")
        expected = "first password 111" if winner is a else "second password 222"
        self.assertTrue(self.authenticates(EMAIL, expected))

    def test_rx3_redemption_racing_operator_commands_in_both_orders(self):
        commands = {
            "set-password": (("set-password", "--email", "{e}", "--password-stdin"), "operator password 9\n"),
            "set-role admin": (("set-role", "--email", "{e}", "--role", "admin"), None),
            "disable": (("disable", "--email", "{e}"), None),
            "sign-out": (("sign-out", "--email", "{e}"), None),
            "totp-reset": (("totp-reset", "--email", "{e}"), None),
        }
        for index, (name, (args, stdin)) in enumerate(commands.items()):
            for order in ("cli first", "redeem first"):
                with self.subTest(name=name, order=order):
                    email = f"race{index}-{order[0]}@example.bg"
                    user_id = self.verified(email)
                    issued = self.request(email)
                    cli = self.cli_on(*[a.format(e=email) for a in args], stdin=stdin)
                    redeem = self.redeem_on(issued, code_of(issued))
                    if order == "cli first":
                        self.assertEqual(cli(None), 0)
                        result = redeem(self.conn)
                        self.assertIs(result.outcome, Outcome.INVALIDATED)
                    else:
                        result = redeem(self.conn)
                        self.assertIs(result.outcome, Outcome.REDEEMED)
                        self.assertEqual(cli(None), 0)
                        # The operator's later action stands.
                        if name == "set-password":
                            self.assertTrue(self.authenticates(email, "operator password 9"))
                    # Released together: either order is a consistent end state.
                    email2 = f"race{index}-{order[0]}-together@example.bg"
                    self.verified(email2)
                    together = self.request(email2)
                    cli2 = self.cli_on(*[a.format(e=email2) for a in args], stdin=stdin)
                    status, outcome = self.race(cli2, self.redeem_on(together, code_of(together)))
                    self.assertEqual(status, 0)
                    self.assertIn(outcome.outcome, (Outcome.REDEEMED, Outcome.INVALIDATED))
                    self.assertIsNotNone(user_id)

    def test_rx4_five_wrong_codes_at_once_count_five(self):
        self.verified()
        issued = self.request()
        results = self.race(*[self.redeem_on(issued, "00000000") for _ in range(5)])
        self.assertEqual([r.outcome for r in results], [Outcome.WRONG_CODE] * 5)
        self.assertEqual(self.row(issued.recovery_id)["code_attempts"], 5)
        self.assertIs(self.redeem(issued, code_of(issued)).outcome, Outcome.TOO_MANY)

    def test_rx5_requests_at_the_cap_minus_one_mixing_purposes(self):
        self.verified()
        for seconds in range(4):
            self.at(seconds)
            self.request()
        self.at(10)
        request = lambda conn: self.tx(recovery.request, conn=conn, email=EMAIL, previous_binding=None,
                                       secret_key=KEY, clock=self.clock, sender_busy=False)
        register = lambda conn: self.tx(registration.create_pending, conn=conn, cleaned=student(EMAIL),
                                        account_type="student", password_hash=password_hash(),
                                        previous_binding=None, secret_key=KEY, clock=self.clock, sender_busy=False)
        results = self.race(request, register, request)
        admitted = [r for r in results if r.outcome.value == "issued"]
        self.assertEqual(len(admitted), 1)
        self.assertEqual(self.count("registration_sends"), 5)

    def test_rx5_many_addresses_at_the_total_budget_minus_one(self):
        for index in range(9):
            self.request(f"a{index}@example.bg", total_cap=10)
        calls = [lambda conn, i=i: self.tx(recovery.request, conn=conn, email=f"b{i}@example.bg",
                                           previous_binding=None, secret_key=KEY, clock=self.clock,
                                           sender_busy=False, total_cap=10) for i in range(3)]
        calls.append(lambda conn: self.tx(registration.create_pending, conn=conn, cleaned=student("c@example.bg"),
                                          account_type="student", password_hash=password_hash(),
                                          previous_binding=None, secret_key=KEY, clock=self.clock,
                                          sender_busy=False, total_cap=10))
        results = self.race(*calls)
        self.assertEqual(sum(r.outcome.value == "issued" for r in results), 1)
        self.assertEqual(self.count("registration_sends"), 10)

    def test_rx6_resend_racing_a_late_settlement_of_the_previous_generation(self):
        for order in ("settle first", "resend first", "together"):
            with self.subTest(order):
                # An accepted send between subtests: the three not_sent settlements of this
                # test must not open the shared circuit and refuse the raced resend.
                self.add_send(outcome="accepted")
                email = f"{order.split()[0]}@example.bg"
                self.verified(email)
                issued = self.request(email)
                self.wall += timedelta(seconds=60)
                settle = lambda conn: self.tx(recovery.settle_send, issued.send_id, issued.issuance_id, "not_sent",
                                              conn=conn, clock=self.clock)
                resend = lambda conn: self.tx(recovery.resend, self.binding(issued), conn=conn, secret_key=KEY,
                                              clock=self.clock, sender_busy=False)
                if order == "settle first":
                    settle(self.conn)
                    newer = resend(self.conn)
                elif order == "resend first":
                    newer = resend(self.conn)
                    settle(self.conn)
                else:
                    _, newer = self.race(settle, resend)
                row = self.row(issued.recovery_id)
                self.assertEqual((row["code_send_id"], row["code_attempts"]), (newer.send_id, 0))
                self.assertIsNotNone(row["resend_after"])
                self.assertIs(self.redeem(issued, code_of(newer)).outcome, Outcome.REDEEMED)

    def test_rx7_recovery_racing_a_registration_activation_of_the_same_address(self):
        registered = self.tx(registration.create_pending, cleaned=student(EMAIL), account_type="student",
                             password_hash=password_hash(), previous_binding=None, secret_key=KEY,
                             clock=self.clock, sender_busy=False)
        user_id = self.verified()
        account = self.user(user_id)
        issued = self.request()
        from tests.registration_support import code_of as registration_code_of
        activate = lambda conn: self.tx(registration.verify, registration.binding_hash(KEY, registered.token),
                                        registration_code_of(registered), conn=conn, secret_key=KEY,
                                        clock=self.clock)
        activation, redemption = self.race(activate, self.redeem_on(issued, code_of(issued)))
        self.assertIs(activation.outcome, registration.Outcome.EMAIL_TAKEN)
        self.assertIs(redemption.outcome, Outcome.REDEEMED)
        after = self.user(user_id)
        for column in ("role", "email", "email_verified_at", "display_name", "is_active"):
            self.assertEqual(after[column], account[column])

    def test_rx8_polling_racing_the_owners_redemption(self):
        self.verified()
        observer = self.request()
        owner = self.request()
        poll = lambda conn: recovery.pending_view(conn, self.binding(observer), self.wall)
        wrong = self.redeem_on(observer, "00000000")
        view, attempt, redeemed = self.race(poll, wrong, self.redeem_on(owner, code_of(owner)))
        self.assertEqual(view, recovery.PendingView(observer.recovery_id, EMAIL))
        self.assertIs(attempt.outcome, Outcome.WRONG_CODE)
        self.assertIs(redeemed.outcome, Outcome.REDEEMED)
        self.wall += timedelta(seconds=60)
        resend = lambda conn: self.tx(recovery.resend, self.binding(observer), conn=conn, secret_key=KEY,
                                      clock=self.clock, sender_busy=False)
        second_owner = self.request()
        again, _ = self.race(resend, self.redeem_on(second_owner, code_of(second_owner), "third password 333"))
        self.assertIs(again.outcome, Outcome.ISSUED)
        self.assertEqual(again.kind, "code")


if __name__ == "__main__":
    import unittest
    unittest.main()
