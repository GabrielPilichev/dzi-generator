"""Password recovery (scope 8.2, service level): another browser's polling while the owner
resets (RPoll1–RPoll8), other rows after a success (RV3), the steps before a correct code
(RV6) and polling log lines (RLog5).

Browser A (an observer, or anyone) requests recovery for an address and keeps its row; the
owner O requests in another browser and redeems. Every comparison takes A's observation
before O's redemption and the same observation after it: the ``Result``, A's row, and for
reads the statements run. "Identical" excludes only what every resend changes (code, hash,
generation, identity, digest). Route-level bytes, headers and cookies are scope 8.3.
"""

import logging
from datetime import timedelta

from tests.recovery_service_support import (
    EMAIL, KEY, NEW_PASSWORD, OLD_PASSWORD, RecoveryServiceTestCase, code_of, normalized,
)
from web.services import accounts, recovery, recovery_scrub, registration

Outcome = recovery.Outcome
ROW_STATE = ("state", "is_shadow", "email", "user_id", "session_key_digest", "code_hash", "code_expires_at",
             "code_send_id", "code_attempts", "send_count", "resend_after", "created_at", "expires_at", "closed_at")
STABLE = ("state", "is_shadow", "email", "user_id", "code_attempts", "send_count", "created_at", "expires_at",
          "closed_at")


class PollingTestCase(RecoveryServiceTestCase):
    def setUp(self):
        super().setUp()
        self.user_id = self.verified()

    def scenario(self, email=EMAIL):
        """A's row and O's row for ``email``; O's reset is ``self.owner_resets()``."""
        self.observer = self.request(email)
        self.owner = self.request(email)

    def owner_resets(self):
        if self.owner.kind == "code":
            self.assertIs(self.redeem(self.owner, code_of(self.owner)).outcome, Outcome.REDEEMED)
        else:  # RPoll7: an address without an eligible account; an unrelated account resets instead
            other_id = self.verified(f"unrelated{self.wall.timestamp():.0f}@example.bg")
            self.run_account(accounts.set_password, other_id, NEW_PASSWORD)

    def a_row(self):
        row = self.row(self.observer.recovery_id)
        return {column: row[column] for column in ROW_STATE}


class ReadsTest(PollingTestCase):
    def test_rpoll1_pending_view_is_identical_and_reads_no_account(self):
        self.scenario()
        with self.statements() as before_sql:
            before = self.view(self.observer)
        row_before = self.a_row()
        self.owner_resets()
        with self.statements() as after_sql:
            after = self.view(self.observer)
        self.assertEqual(before, after)
        self.assertEqual(normalized(before_sql), normalized(after_sql))
        self.assertFalse([sql for sql in before_sql + after_sql if "users" in sql])
        self.assertEqual(self.a_row(), row_before)

    def test_rpoll2_wrong_codes_before_and_after_answer_alike(self):
        self.scenario()
        with self.statements() as before_sql:
            before = self.redeem(self.observer, "00000000")
        self.owner_resets()
        with self.statements() as after_sql:
            after = self.redeem(self.observer, "00000000")
        self.assertEqual(before, after)
        self.assertIs(after.outcome, Outcome.WRONG_CODE)
        self.assertEqual(normalized(before_sql), normalized(after_sql))
        self.assertFalse([sql for sql in after_sql if "users" in sql])
        self.assertEqual(self.row(self.observer.recovery_id)["code_attempts"], 2)
        for _ in range(3):
            self.assertIs(self.redeem(self.observer, "00000000").outcome, Outcome.WRONG_CODE)
        self.assertIs(self.redeem(self.observer, "00000000").outcome, Outcome.TOO_MANY)

    def test_rv6_steps_one_to_nine_are_the_same_before_and_after_the_reset_and_for_a_shadow(self):
        self.scenario()
        shadow = self.request("unknown@example.bg")
        sequences = []
        for issued in (self.observer, shadow):
            with self.statements() as seen:
                self.redeem(issued, "00000000")
            sequences.append(normalized(seen))
        self.owner_resets()
        for issued in (self.observer, shadow):
            with self.statements() as seen:
                self.redeem(issued, "00000000")
            sequences.append(normalized(seen))
        self.assertEqual(len(set(map(tuple, sequences))), 1)


class ResendAndCancelTest(PollingTestCase):
    def resend_observation(self):
        with self.statements() as seen:
            result = self.resend(self.observer)
        row = self.row(self.observer.recovery_id)
        return result, row, seen

    def test_rpoll3_a_resend_after_the_reset_is_answered_and_recorded_as_before(self):
        self.scenario()
        self.at(60)
        before, row_before, before_sql = self.resend_observation()
        self.owner_resets()
        self.at(120)
        after, row_after, after_sql = self.resend_observation()
        # Reads that happen, by design: eligibility (RD11) reads the account.
        self.assertTrue(any("FROM users" in sql for sql in before_sql))
        self.assertTrue(any("FROM users" in sql for sql in after_sql))
        self.assertEqual(normalized(before_sql), normalized(after_sql))
        self.assertEqual((before.outcome, before.kind), (after.outcome, after.kind))
        self.assertEqual(self.send(before.send_id)["kind"], self.send(after.send_id)["kind"])
        for column in ("state", "is_shadow", "email", "user_id", "code_attempts"):
            self.assertEqual(row_before[column], row_after[column], column)
        self.assertEqual(row_after["send_count"], row_before["send_count"] + 1)
        self.assertEqual(row_after["resend_after"], recovery._stamp(self.wall + timedelta(seconds=60)))
        # What every resend changes, and the digest now that of the rotated key.
        self.assertNotEqual(row_before["code_hash"], row_after["code_hash"])
        self.assertNotEqual(row_before["session_key_digest"], row_after["session_key_digest"])
        self.assertEqual(row_after["session_key_digest"],
                         recovery.session_key_digest(KEY, self.user(self.user_id)["session_key"]))
        # A later correct code from the mailbox redeems.
        self.assertIs(self.redeem(self.observer, code_of(after), "observer password 1").outcome, Outcome.REDEEMED)

    def test_rpoll3_a_resend_committed_before_the_reset_is_invalidated_after_it(self):
        self.scenario()
        self.at(60)
        before = self.resend(self.observer)
        self.owner_resets()
        self.assertIs(self.redeem(self.observer, code_of(before)).outcome, Outcome.INVALIDATED)

    def test_rpoll4_cancel_after_the_reset_is_the_same(self):
        self.scenario()
        other = self.request()
        with self.statements() as before_sql:
            before = self.cancel(other)
        self.owner_resets()
        with self.statements() as after_sql:
            after = self.cancel(self.observer)
        self.assertEqual(before.outcome, after.outcome)
        self.assertEqual(normalized(before_sql), normalized(after_sql))
        self.assertFalse([sql for sql in after_sql if "users" in sql])
        self.assertScrubbed(self.observer.recovery_id, "void")


class AfterTheResetTest(PollingTestCase):
    def test_rv3_other_rows_are_byte_identical_after_a_success(self):
        self.scenario()
        shadow_same_address = self.request("unknown@example.bg")
        other_account = self.verified("other@example.bg")
        unrelated = self.request("other@example.bg")
        keep = [self.observer, shadow_same_address, unrelated]
        before = {r.recovery_id: self.row(r.recovery_id) for r in keep}
        self.owner_resets()
        for recovery_id, row in before.items():
            self.assertEqual(self.row(recovery_id), row)
        # Only now, at its own correct code, is the observer's row voided.
        self.assertIs(self.redeem(self.observer, code_of(self.observer)).outcome, Outcome.INVALIDATED)
        self.assertScrubbed(self.observer.recovery_id, "void")
        self.assertIs(self.redeem(unrelated, code_of(unrelated)).outcome, Outcome.REDEEMED)
        self.assertIsNotNone(other_account)

    def test_rpoll5_the_observers_correct_code_after_the_reset_is_invalidated(self):
        self.scenario()
        self.owner_resets()
        account = self.user(self.user_id)
        self.assertIs(self.redeem(self.observer, code_of(self.observer)).outcome, Outcome.INVALIDATED)
        self.assertEqual(self.user(self.user_id), account)
        self.assertTrue(self.authenticates(EMAIL, NEW_PASSWORD))

    def test_rpoll6_the_observers_row_expires_on_its_own_schedule(self):
        self.scenario()
        self.owner_resets()
        self.at(29 * 60)
        self.assertEqual(self.row(self.observer.recovery_id)["state"], "pending")
        self.assertIsNotNone(self.view(self.observer))
        self.at(30 * 60)
        self.assertIsNone(self.view(self.observer))
        self.assertEqual(self.row(self.observer.recovery_id)["state"], "pending")  # a GET writes nothing
        # The cleanup scrubs it, as ``registrations-cleanup`` runs it (one transaction, one clock).
        result = self.tx(lambda conn: (registration.cleanup(conn, clock=self.clock),
                                       recovery_scrub.cleanup(conn, self.wall)))
        self.assertFalse(result[0].clock_behind)
        self.assertEqual(result[1].scrubbed, 1)  # the owner's row is already final
        self.assertScrubbed(self.observer.recovery_id, "expired")

    def test_rpoll7_the_same_comparisons_for_a_shadow_row(self):
        self.scenario("unknown@example.bg")
        self.assertEqual(self.observer.kind, "notice")
        view_before = self.view(self.observer)
        wrong_before = self.redeem(self.observer, "00000000")
        self.at(60)
        resend_before = self.resend(self.observer)
        self.owner_resets()
        self.assertEqual(self.view(self.observer), view_before)
        self.assertEqual(self.redeem(self.observer, "00000000"), wrong_before)
        self.at(120)
        resend_after = self.resend(self.observer)
        self.assertEqual((resend_before.outcome, resend_before.kind), (resend_after.outcome, resend_after.kind))


class ThrottleObservationTest(PollingTestCase):
    def test_rpoll8_a_reset_changes_no_throttle_row_and_sign_in_stays_blocked(self):
        self.scenario()
        self.fail_sign_in(EMAIL, times=10)
        throttle = self.table_rows("login_throttle")
        self.owner_resets()
        self.assertEqual(self.table_rows("login_throttle"), throttle)
        self.assertEqual(self.sign_in(EMAIL, NEW_PASSWORD), "blocked")
        # Existing behaviour RD8 (a) does not remove: in a later window, part-way through a
        # count, the owner's successful sign-in clears the row, so ten more failures are needed.
        self.at(16 * 60)
        self.fail_sign_in(EMAIL, times=4)
        self.assertEqual(self.sign_in(EMAIL, NEW_PASSWORD), "ok")
        self.fail_sign_in(EMAIL, times=9)
        self.assertEqual(self.sign_in(EMAIL, "wrong password 000"), "wrong")  # the tenth failure
        self.assertEqual(self.sign_in(EMAIL, NEW_PASSWORD), "blocked")  # relocked after the reset as before
        self.assertFalse(self.authenticates(EMAIL, OLD_PASSWORD))


class PollingLogTest(PollingTestCase):
    def test_rlog5_polling_writes_no_line_that_differs_before_and_after_the_reset(self):
        self.scenario()
        lines = []

        class Keep(logging.Handler):
            def emit(self, record):
                lines.append(record.getMessage())

        handler = Keep(level=logging.DEBUG)
        for name in ("learnpilot.recovery", "learnpilot.registration"):
            log = logging.getLogger(name)
            log.addHandler(handler)
            previous = log.level
            log.setLevel(logging.DEBUG)
            self.addCleanup(log.setLevel, previous)
            self.addCleanup(log.removeHandler, handler)

        def poll():
            lines.clear()
            self.view(self.observer)
            self.redeem(self.observer, "00000000")
            return list(lines)

        before = poll()
        logging.getLogger("learnpilot.recovery").removeHandler(handler)
        self.owner_resets()
        logging.getLogger("learnpilot.recovery").addHandler(handler)
        after = poll()
        self.assertEqual(before, after)
        self.assertTrue(before)


if __name__ == "__main__":
    import unittest
    unittest.main()
