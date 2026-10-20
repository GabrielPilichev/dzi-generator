"""Password recovery (scope 8.2): the shared send ledger, budgets and circuit (RL1–RL9),
settlement (RS1–RS5), purpose isolation and one-purpose rows (RA3, RA6 service parts).

Registration's own suites stay unchanged and keep pinning its six-digit codes and its
``not_sent`` settlement; here both services write into one ledger and are counted by
registration's code, with no counter of recovery's own.
"""

import heapq
import random
import sqlite3
from datetime import timedelta
from unittest.mock import patch

from tests.recovery_service_support import (
    EMAIL, KEY, RecoveryServiceTestCase, code_of, stamp,
)
from tests.registration_support import T0, password_hash, student
from tests.registration_support import code_of as registration_code_of
from web.services import recovery, registration
from web.services.mail import InMemoryMailer, SendOutcome

Outcome = recovery.Outcome
W = registration.DAILY_WINDOW
GUESSABLE = timedelta(hours=24) + registration.CODE_LIFETIME  # issued inside it: guessable in one day


class MixedPurposeTestCase(RecoveryServiceTestCase):
    def register(self, email=EMAIL, *, key=KEY, **kwargs):
        kwargs.setdefault("sender_busy", self.sender_busy)
        return self.tx(registration.create_pending, cleaned=student(email), account_type="student",
                       password_hash=password_hash(), previous_binding=None, secret_key=key, clock=self.clock,
                       **kwargs)

    def register_resend(self, issued, *, key=KEY):
        return self.tx(registration.resend, registration.binding_hash(key, issued.token), secret_key=key,
                       clock=self.clock, sender_busy=False)

    def register_settle(self, issued, outcome):
        return self.tx(registration.settle_send, issued.send_id, issued.issuance_id, outcome, clock=self.clock)

    def admitted(self, result):
        return result.outcome is registration.Outcome.ISSUED or result.outcome is Outcome.ISSUED


class AddressBudgetTest(MixedPurposeTestCase):
    def test_rl1_one_per_address_budget_for_both_purposes_and_both_kinds(self):
        kinds = []
        self.at(0)
        kinds.append(("registration", self.register().kind))       # no account yet: a code
        self.at(60)
        kinds.append(("recovery", self.request().kind))            # no account: a notice
        self.verified()
        self.at(120)
        kinds.append(("registration", self.register().kind))       # account exists: a notice
        self.at(180)
        kinds.append(("recovery", self.request().kind))            # eligible: a code
        self.at(240)
        kinds.append(("recovery", self.request().kind))
        self.assertEqual(kinds, [("registration", "code"), ("recovery", "notice"), ("registration", "notice"),
                                 ("recovery", "code"), ("recovery", "code")])
        self.at(300)
        self.assertIs(self.register().outcome, registration.Outcome.ADDRESS_CAPPED)
        self.assertIs(self.request().outcome, Outcome.ADDRESS_CAPPED)
        self.at(3600)  # the oldest is still inside the hour
        self.assertIs(self.request().outcome, Outcome.ADDRESS_CAPPED)
        self.at(3601)
        self.assertIs(self.request().outcome, Outcome.ISSUED)          # sixth in the day
        self.at(3661)
        self.assertIs(self.register().outcome, registration.Outcome.ISSUED)  # seventh
        self.at(3721)
        self.assertIs(self.request().outcome, Outcome.ISSUED)          # eighth
        self.at(7300)
        self.assertIs(self.request().outcome, Outcome.ADDRESS_CAPPED)  # ninth in 24 h 15 min
        self.assertIs(self.register().outcome, registration.Outcome.ADDRESS_CAPPED)
        self.at(int(W.total_seconds()))
        self.assertIs(self.request().outcome, Outcome.ADDRESS_CAPPED)
        self.at(int(W.total_seconds()) + 1)
        self.assertIs(self.request().outcome, Outcome.ISSUED)

    def test_rl1_recovery_resends_count_in_the_shared_hourly_cap(self):
        self.verified()
        issued = self.request()
        for seconds in (60, 120, 180):
            self.at(seconds)
            self.assertIs(self.resend(issued).outcome, Outcome.ISSUED)
        self.at(240)
        self.assertIs(self.register().outcome, registration.Outcome.ISSUED)  # fifth send of the address
        self.at(300)
        self.assertIs(self.register().outcome, registration.Outcome.ADDRESS_CAPPED)

    def test_rl4_not_sent_recovery_sends_are_not_refunded(self):
        self.verified()
        for index in range(5):
            self.at(301 * index)  # past each circuit cooldown: three failures open the circuit
            issued = self.request()
            self.assertIs(issued.outcome, Outcome.ISSUED)
            self.deliver(issued, SendOutcome.NOT_SENT)
        self.at(301 * 5)
        self.assertIs(self.request().outcome, Outcome.ADDRESS_CAPPED)
        self.assertIs(self.register().outcome, registration.Outcome.ADDRESS_CAPPED)

    def test_rl8_clear_registration_limit_releases_the_recovery_budget_and_kills_the_live_code(self):
        from tests.recovery_support import cli
        import os
        self.verified()
        for seconds in range(5):
            self.at(seconds)
            issued = self.request()
        self.assertIs(self.request().outcome, Outcome.ADDRESS_CAPPED)
        with patch.dict(os.environ, {"DZI_SECRET_KEY": KEY}):
            status, output = cli(self.path, "clear-registration-limit", "--email", EMAIL)
        self.assertEqual(status, 0, output)
        self.assertIn("recovery", output)
        self.assertIn("5 record(s) deleted", output)
        self.assertEqual(self.count("registration_sends"), 0)
        self.assertIs(self.redeem(issued, code_of(issued)).outcome, Outcome.CODE_EXPIRED)  # fail closed
        self.at(120)
        again = self.resend(issued)
        self.assertIs(self.redeem(issued, code_of(again)).outcome, Outcome.REDEEMED)


class TotalBudgetTest(MixedPurposeTestCase):
    def test_rl2_rl3_one_total_budget_across_purposes(self):
        for index in range(300):
            self.at(index)
            if index % 2:
                result = self.request(f"r{index}@example.bg")
            else:
                result = self.register(f"g{index}@example.bg")
            self.assertTrue(self.admitted(result), index)
        self.at(300)
        with self.assertLogs("learnpilot.registration", "WARNING") as logged:
            refused_recovery = self.request("late-r@example.bg")
            refused_registration = self.register("late-g@example.bg")
        self.assertEqual(refused_recovery.reason, registration.Availability.TOTAL_CAP)
        self.assertEqual(refused_registration.reason, registration.Availability.TOTAL_CAP)
        self.assertEqual(sum("total_cap_reached" in line for line in logged.output), 2)  # once per refusal
        self.assertEqual(self.count("registration_sends"), 300)

    def test_rl2_a_lower_configured_cap_applies_to_both(self):
        for index in range(5):
            self.register(f"g{index}@example.bg", total_cap=10)
            self.request(f"r{index}@example.bg", total_cap=10)
        self.assertEqual(self.request("x@example.bg", total_cap=10).reason, registration.Availability.TOTAL_CAP)
        self.assertEqual(self.register("y@example.bg", total_cap=10).reason, registration.Availability.TOTAL_CAP)

    def test_rl3_registration_traffic_closes_recovery_and_the_reverse(self):
        for index in range(10):
            self.register(f"g{index}@example.bg", total_cap=10)
        self.assertEqual(self.request("r@example.bg", total_cap=10).reason, registration.Availability.TOTAL_CAP)
        self.at(3601)
        for index in range(10):
            self.request(f"r{index}@example.bg", total_cap=10)
        self.assertEqual(self.register("g@example.bg", total_cap=10).reason, registration.Availability.TOTAL_CAP)

    def test_rl4_four_hundred_refused_recovery_requests_admit_exactly_three_hundred(self):
        admitted = 0
        for index in range(400):
            self.at(index)
            result = self.request(f"a{index}@example.bg")
            if result.outcome is Outcome.ISSUED:
                admitted += 1
                self.deliver(result, SendOutcome.REFUSED)
        self.assertEqual(admitted, 300)


class CircuitTest(MixedPurposeTestCase):
    def test_rl5_failures_across_purposes_close_both_and_an_accepted_reopens_both(self):
        self.verified()
        first = self.register("g1@example.bg")
        self.register_settle(first, SendOutcome.NOT_SENT)
        self.at(1)
        second = self.request()
        self.assertIs(self.settle(second, SendOutcome.UNKNOWN), recovery.Settlement.SETTLED)
        self.at(2)
        third = self.register("g2@example.bg")
        self.register_settle(third, SendOutcome.NOT_SENT)
        self.at(3)
        self.assertEqual(self.request("r@example.bg").reason, registration.Availability.CIRCUIT_OPEN)
        self.assertEqual(self.register("g3@example.bg").reason, registration.Availability.CIRCUIT_OPEN)
        self.at(3 + 300)  # the cooldown after the newest failure
        probe = self.request("probe@example.bg")
        self.assertIs(probe.outcome, Outcome.ISSUED)
        self.settle(probe, SendOutcome.ACCEPTED)
        self.assertIs(self.register("g4@example.bg").outcome, registration.Outcome.ISSUED)

    def test_rl5_refused_and_started_are_left_out_and_order_is_by_generation_id(self):
        older = self.request("older@example.bg")  # stays started for now
        self.at(1)
        a = self.register("g1@example.bg")
        self.register_settle(a, SendOutcome.NOT_SENT)
        self.at(2)
        refused = self.request("refused@example.bg")
        self.settle(refused, SendOutcome.REFUSED)
        self.at(3)
        b = self.register("g2@example.bg")
        self.register_settle(b, SendOutcome.UNKNOWN)
        self.at(4)
        self.assertIs(self.request("x@example.bg").outcome, Outcome.ISSUED)  # two failures + started + refused
        # A late failure of the older recovery generation takes its place by id: three failures.
        self.settle(older, SendOutcome.NOT_SENT)
        self.at(5)
        self.assertEqual(self.request("y@example.bg").reason, registration.Availability.CIRCUIT_OPEN)

    def test_rl5_a_late_accepted_of_an_older_generation_does_not_reopen_over_newer_failures(self):
        older = self.request("older@example.bg")
        self.at(1)
        for index in range(3):
            failing = self.register(f"g{index}@example.bg")
            self.register_settle(failing, SendOutcome.NOT_SENT)
        self.settle(older, SendOutcome.ACCEPTED)
        self.at(2)
        self.assertEqual(self.request("z@example.bg").reason, registration.Availability.CIRCUIT_OPEN)

    def test_rl9_redemption_ignores_circuit_budget_and_sender(self):
        self.verified()
        issued = self.request()
        for _ in range(3):
            self.add_send(outcome="not_sent")
        self.sender_busy = True
        self.assertEqual(self.request("other@example.bg").reason, registration.Availability.SENDER_BUSY)
        self.sender_busy = False
        self.assertEqual(self.request("other@example.bg").reason, registration.Availability.CIRCUIT_OPEN)
        self.assertIs(self.redeem(issued, code_of(issued)).outcome, Outcome.REDEEMED)


class GuessingBoundTest(MixedPurposeTestCase):
    def test_rl6_mixed_purposes_at_most_eight_guessable_codes_a_day(self):
        """**Assumptions (design 5.4, registration design 3.3), all three required:** the wall
        clock only advances and is never stepped; the secret key is not changed; no ledger
        row of the address is removed other than by the 48-hour cleanup. Under them, at most
        eight codes of one address, of either purpose, are guessable in any 24 hours, and so
        at most eight recovery codes."""
        generator = random.Random(20261020)
        for schedule in range(12):
            for table in ("password_recoveries", "pending_registrations", "registration_sends",
                          "registration_control", "user_profiles", "users"):
                self.conn.execute(f"DELETE FROM {table}")
            self.conn.commit()
            account_at = generator.randrange(0, 2 * 86400)
            has_account = False
            codes, recovery_codes, last = [], [], -1
            current = {"recovery": None, "registration": None}
            probes = [generator.randrange(0, 4 * 86400) for _ in range(50)]
            heapq.heapify(probes)
            while probes:
                seconds = heapq.heappop(probes)
                if seconds <= last or seconds > 4 * 86400 + int(W.total_seconds()):
                    continue
                last = seconds
                self.at(seconds)
                if not has_account and seconds >= account_at:
                    self.verified()
                    has_account = True
                purpose = generator.choice(("recovery", "registration"))
                result = None
                if current[purpose] is not None and generator.random() < 0.5:
                    result = (self.resend(current[purpose]) if purpose == "recovery"
                              else self.register_resend(current[purpose]))
                if result is None or not self.admitted(result):
                    result = self.request() if purpose == "recovery" else self.register()
                    if self.admitted(result):
                        current[purpose] = result
                if self.admitted(result):
                    if result.kind == "code":
                        codes.append(seconds)
                        if purpose == "recovery":
                            recovery_codes.append(seconds)
                    for boundary in (60, 3600, int(W.total_seconds())):
                        for near in (-1, 0, 1):
                            heapq.heappush(probes, seconds + boundary + near)
            self.assertGreater(len(codes), 8, schedule)
            for issued in (codes, recovery_codes):
                for index, start in enumerate(issued):
                    inside = sum(1 for moment in issued[index:] if moment < start + GUESSABLE.total_seconds())
                    self.assertLessEqual(inside, registration.ADDRESS_DAILY_CAP, (schedule, start))

    def test_rl6_a_real_registration_begun_before_the_account_shares_the_eight(self):
        pending = self.register()
        self.assertEqual(pending.kind, "code")  # real: the account did not exist yet
        user_id = self.verified()
        account = self.user(user_id)
        issued = [pending]
        moment = 0
        for _ in range(3):
            moment += 61
            self.at(moment)
            issued.append(self.register_resend(pending))
        self.assertEqual([r.kind for r in issued], ["code"] * 4)  # registration codes after the account exists
        # Those codes are live and guessable; a correct one ends EMAIL_TAKEN and changes no account.
        binding = registration.binding_hash(KEY, pending.token)
        self.assertIs(self.tx(registration.verify, binding, "000000", secret_key=KEY, clock=self.clock).outcome,
                      registration.Outcome.WRONG_CODE)
        result = self.tx(registration.verify, binding, registration_code_of(issued[-1]), secret_key=KEY,
                         clock=self.clock)
        self.assertIs(result.outcome, registration.Outcome.EMAIL_TAKEN)
        self.assertEqual(self.user(user_id), account)
        # Recovery codes take only what is left of the eight.
        for _ in range(12):
            moment += 1201
            self.at(moment)
            result = self.request()
            if result.outcome is Outcome.ISSUED:
                issued.append(result)
        self.assertEqual(len(issued), 8)
        self.assertEqual(sum(1 for r in issued if isinstance(r, recovery.Result)), 4)
        self.assertLess(moment, int(W.total_seconds()))


class ExceptionsTest(MixedPurposeTestCase):
    """RL7: the pinned exceptions to the bound; they accumulate. No test asserts a bound
    across repeated events."""

    def issue_until_capped(self, key=KEY, start=0):
        issued, moment = 0, start
        while True:
            self.at(moment)
            result = self.request(key=key)
            if result.outcome is not Outcome.ISSUED:
                break
            issued += 1
            moment += 3601 if issued % 5 == 0 else 1
        return issued, moment

    def test_rl7_two_small_clock_steps_admit_thirteen_recovery_codes_in_a_real_day(self):
        self.verified()
        offset, issued, guessable_at = 0, [], []

        def real(seconds):
            self.at(seconds + offset)

        for seconds in range(5):
            real(seconds)
            issued.append(self.request())
        offset = -300
        real(899)  # wall 599: the five codes are still live
        for result in issued:
            self.assertIs(self.redeem(result, "00000000").outcome, Outcome.WRONG_CODE)
            guessable_at.append(899)
        offset = 0
        real(900)
        offset = 300
        for seconds in (3601, 3602, 3603, 87006, 87007, 87008, 87009, 87010):
            real(seconds)
            issued.append(self.request())
            self.assertIs(self.redeem(issued[-1], "00000000").outcome, Outcome.WRONG_CODE)
            guessable_at.append(seconds)
        self.assertEqual([r.outcome for r in issued], [Outcome.ISSUED] * 13)
        self.assertEqual([r.kind for r in issued], ["code"] * 13)
        self.assertLess(max(guessable_at) - min(guessable_at), 86400)

    def test_rl7_key_rotations_and_limit_clearings_each_admit_eight_more_and_add_up(self):
        import os
        from tests.recovery_support import cli
        self.verified()
        first, moment = self.issue_until_capped()
        self.assertEqual(first, 8)
        # Two key rotations: a fresh address key each time (and every binding/digest void).
        for key in ("rotated-1", "rotated-2"):
            more, moment = self.issue_until_capped(key=key, start=moment + 1)
            self.assertEqual(more, 8)
        # Two operator clearings under the original key, within the same real day.
        for _ in range(2):
            with patch.dict(os.environ, {"DZI_SECRET_KEY": KEY}):
                status, output = cli(self.path, "clear-registration-limit", "--email", EMAIL)
            self.assertEqual(status, 0, output)
            more, moment = self.issue_until_capped(start=moment + 1)
            self.assertEqual(more, 8)
        self.assertLess(moment, 86400)

    def test_rl7_a_restore_hold_release_with_lost_history_admits_eight_more(self):
        import os
        from tests.recovery_support import cli
        self.verified()
        snapshot = self.root / "snapshot.db"
        with self.connect() as source:
            import sqlite3
            target = sqlite3.connect(snapshot)
            source.backup(target)
            target.close()
        first, moment = self.issue_until_capped()
        self.assertEqual(first, 8)
        # The restored snapshot predates those eight sends; the gate holds, the release forgets.
        self.path = snapshot
        self.conn = self.connect()
        status, output = cli(snapshot, "registrations-restore-gate")
        self.assertEqual(status, 0, output)
        status, output = cli(snapshot, "registrations-release-hold", "--acknowledge-lost-history")
        self.assertEqual(status, 0, output)
        self.assertIn("recovery", output)
        self.at(moment + 1)
        more, _ = self.issue_until_capped(start=moment + 1)
        self.assertEqual(more, 8)


class SettlementTest(MixedPurposeTestCase):
    def setUp(self):
        super().setUp()
        self.verified()

    def test_rs1_each_outcome_settles_the_ledger_once_and_changes_no_recovery_row(self):
        for outcome in SendOutcome:
            with self.subTest(outcome.value):
                self.wall += timedelta(seconds=3601)
                issued = self.request()
                row = self.row(issued.recovery_id)
                self.assertIs(self.settle(issued, outcome), recovery.Settlement.SETTLED)
                send = self.send(issued.send_id)
                self.assertEqual((send["outcome"], send["settled_at"]), (outcome.value, stamp(self.wall)))
                self.assertEqual(self.row(issued.recovery_id), row)

    def test_rs1_not_sent_keeps_the_code_its_attempts_and_the_wait(self):
        issued = self.request()
        self.redeem(issued, "00000000")
        row = self.row(issued.recovery_id)
        self.settle(issued, SendOutcome.NOT_SENT)
        self.assertEqual(self.row(issued.recovery_id), row)
        self.assertIs(self.resend(issued).outcome, Outcome.TOO_EARLY)  # the wait is not waived
        self.assertEqual(self.view(issued), recovery.PendingView(issued.recovery_id, EMAIL))
        for _ in range(4):
            self.assertIs(self.redeem(issued, "00000000").outcome, Outcome.WRONG_CODE)
        self.assertIs(self.redeem(issued, code_of(issued)).outcome, Outcome.TOO_MANY)

    def test_rs1_a_not_sent_code_still_redeems(self):
        issued = self.request()
        self.settle(issued, SendOutcome.NOT_SENT)
        self.assertIs(self.redeem(issued, code_of(issued)).outcome, Outcome.REDEEMED)

    def test_rs2_first_settlement_wins_on_one_and_two_connections(self):
        issued = self.request()
        self.assertIs(self.settle(issued, SendOutcome.ACCEPTED), recovery.Settlement.SETTLED)
        before = self.digest()
        self.assertIs(self.settle(issued, SendOutcome.NOT_SENT), recovery.Settlement.ALREADY_SETTLED)
        self.assertUnchanged(before)
        import threading
        other = self.request("other@example.bg")
        results, barrier = [], threading.Barrier(2)

        def settle(outcome):
            conn = sqlite3.connect(self.path, timeout=10)
            try:
                barrier.wait()
                results.append(self.settle(other, outcome, conn=conn))
            finally:
                conn.close()

        threads = [threading.Thread(target=settle, args=(o,)) for o in (SendOutcome.ACCEPTED, SendOutcome.UNKNOWN)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sorted(r.value for r in results), ["already_settled", "settled"])

    def test_rs3_late_settlement_after_resend_redemption_cancellation_and_expiry(self):
        for ending in ("resend", "redeem", "cancel", "expiry"):
            with self.subTest(ending):
                self.wall += timedelta(hours=2)
                issued = self.request()
                if ending == "resend":
                    self.wall += timedelta(seconds=60)
                    self.resend(issued)
                elif ending == "redeem":
                    self.redeem(issued, code_of(issued))
                elif ending == "cancel":
                    self.cancel(issued)
                else:
                    self.wall += timedelta(minutes=31)
                    self.redeem(issued, "00000000")
                row = self.row(issued.recovery_id)
                others = self.table_rows("registration_sends", "id != ?", issued.send_id)
                self.assertIs(self.settle(issued, SendOutcome.NOT_SENT), recovery.Settlement.SETTLED)
                self.assertEqual(self.row(issued.recovery_id), row)
                self.assertEqual(self.table_rows("registration_sends", "id != ?", issued.send_id), others)

    def test_rs4_cross_purpose_settlement_is_refused_both_ways(self):
        reg = self.register("g@example.bg")
        rec = self.request()
        before = self.digest()
        self.assertIs(self.tx(recovery.settle_send, reg.send_id, reg.issuance_id, SendOutcome.NOT_SENT,
                              clock=self.clock), recovery.Settlement.NOT_THIS_ISSUANCE)
        self.assertIs(self.tx(registration.settle_send, rec.send_id, rec.issuance_id, SendOutcome.NOT_SENT,
                              clock=self.clock), registration.Settlement.NOT_THIS_ISSUANCE)
        self.assertUnchanged(before)
        # A purpose-less ledger row is never settled by recovery.
        legacy = self.add_send()
        identity = self.send(legacy)["issuance_id"]
        self.assertIs(self.tx(recovery.settle_send, legacy, identity, SendOutcome.ACCEPTED, clock=self.clock),
                      recovery.Settlement.NOT_THIS_ISSUANCE)

    def test_rs4_registration_not_sent_still_retires_its_own_code(self):
        reg = self.register("g@example.bg")
        self.register_settle(reg, SendOutcome.NOT_SENT)
        row = self.rows("SELECT * FROM pending_registrations WHERE id = ?", reg.pending_id)[0]
        self.assertIsNone(row["code_hash"])
        self.assertIsNone(row["resend_after"])

    def test_rs5_a_settlement_that_never_runs(self):
        issued = self.request()
        self.dispatch(InMemoryMailer(), issued)
        self.assertEqual(self.send(issued.send_id)["outcome"], "started")
        for _ in range(3):
            self.add_send(outcome="not_sent")
        older = self.send(issued.send_id)
        self.assertEqual(older["outcome"], "started")
        self.assertIs(self.redeem(issued, code_of(issued)).outcome, Outcome.REDEEMED)
        for _ in range(4):
            self.add_send(email=EMAIL)
        self.assertTrue(registration.address_capped(self.conn, registration.email_hash(KEY, EMAIL), self.wall))

    def test_settlement_validates_before_reading(self):
        issued = self.request()
        for args in ((issued.send_id, issued.issuance_id, "delivered"), (str(issued.send_id), issued.issuance_id,
                     "accepted"), (issued.send_id, None, "accepted"), (issued.send_id, "X" * 32, "accepted")):
            with self.subTest(args=args[2]):
                with self.assertRaises(ValueError):
                    self.tx(recovery.settle_send, *args, clock=self.clock)


class OnePurposeTest(MixedPurposeTestCase):
    def test_ra3_every_service_ledger_row_has_exactly_one_id(self):
        self.verified()
        issued = self.request()
        self.at(60)
        self.resend(issued)
        self.request("unknown@example.bg")
        self.register("g@example.bg")
        self.register("ana@example.bg")
        for row in self.sends():
            self.assertTrue((row["pending_id"] is None) != (row["recovery_id"] is None), row)

    def test_ra6_purpose_less_rows_count_everywhere_and_are_live_for_neither(self):
        self.verified()
        for _ in range(5):
            self.add_send(email=EMAIL)
        self.assertIs(self.request().outcome, Outcome.ADDRESS_CAPPED)
        self.conn.execute("DELETE FROM registration_sends")
        self.conn.commit()
        issued = self.request()
        legacy = self.add_send(email=EMAIL)
        # Pointing the row at a purpose-less ledger row: its code is not live (fail closed).
        self.tx(lambda conn: conn.execute("UPDATE password_recoveries SET code_send_id = ? WHERE id = ?",
                                          (legacy, issued.recovery_id)))
        self.assertIs(self.redeem(issued, code_of(issued)).outcome, Outcome.CODE_EXPIRED)
        # Pointing it at a registration's ledger row is not live either.
        reg = self.register("g@example.bg")
        self.tx(lambda conn: conn.execute("UPDATE password_recoveries SET code_send_id = ? WHERE id = ?",
                                          (reg.send_id, issued.recovery_id)))
        self.assertIs(self.redeem(issued, code_of(issued)).outcome, Outcome.CODE_EXPIRED)
        self.assertEqual(self.row(issued.recovery_id)["code_attempts"], 0)


if __name__ == "__main__":
    import unittest
    unittest.main()
