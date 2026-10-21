"""Password recovery (scope 8.2): redemption (RV1, RV2, RV4, RV5, RV7), password exactness
(RB3), failures and commit failures at the service seam (RF1, RF2, RF4), attempt and send
limits (RT1–RT4), the restore hold (RH1, RH2), the clock (RCl1–RCl5) and the one writer
order (RC8).

RF2 is exercised at the transaction seam a caller owns: a **real** SQLite commit failure (a
deferred foreign-key violation makes ``COMMIT`` itself fail and leave the transaction open)
and a commit that took effect and then raised. The route's texts belong to scope 8.3; here
the evidence is that nothing the service decided takes effect without the commit, and that a
retry is safe in both cases.
"""

import sqlite3
import unicodedata
from datetime import timedelta
from unittest.mock import patch

from tests.recovery_service_support import (
    EMAIL, KEY, NEW_PASSWORD, OLD_PASSWORD, RecoveryServiceTestCase, code_of, stamp,
)
from tests.registration_support import T0
from web.services import accounts, login_throttle, recovery, registration
from web.services.mail import SendOutcome

Outcome = recovery.Outcome
WRITES = ("INSERT", "UPDATE", "DELETE", "REPLACE")
LOCK = "UPDATE registration_control SET id = id WHERE 0"


def writes(statements):
    return [sql for sql in statements if sql.lstrip().upper().startswith(WRITES) and LOCK not in sql]


class FailingConnection(sqlite3.Connection):
    """A real connection whose ``execute`` raises once a statement contains ``fail_on``."""

    fail_on = None

    def execute(self, sql, *args):
        if self.fail_on and self.fail_on in sql:
            raise sqlite3.OperationalError("injected failure")
        return super().execute(sql, *args)


class RedemptionStepsTest(RecoveryServiceTestCase):
    def setUp(self):
        super().setUp()
        self.user_id = self.verified()
        self.issued = self.request()
        self.code = code_of(self.issued)

    def test_rv1_each_step_in_isolation(self):
        # 1. no row
        before = self.digest()
        self.assertIs(self.tx(recovery.redeem, "f" * 64, self.code, NEW_PASSWORD, secret_key=KEY,
                              clock=self.clock).outcome, Outcome.NO_PENDING)
        self.assertUnchanged(before)
        # 3. hold, then clock behind: nothing written, code and attempts kept
        for control in (dict(hold="restore"), dict(high_water=T0 + timedelta(seconds=301))):
            with self.subTest(**control):
                self.set_control(**control)
                before = self.digest()
                result = self.redeem(self.issued, self.code)
                self.assertIs(result.outcome, Outcome.UNAVAILABLE)
                self.assertUnchanged(before)
                self.conn.execute("DELETE FROM registration_control")
                self.conn.commit()
        # 6–8. wrong codes are counted, then attempts are used up
        for attempt in range(1, 6):
            self.assertIs(self.redeem(self.issued, "00000000").outcome, Outcome.WRONG_CODE)
            self.assertEqual(self.row(self.issued.recovery_id)["code_attempts"], attempt)
        before = self.digest()
        self.assertIs(self.redeem(self.issued, self.code).outcome, Outcome.TOO_MANY)
        self.assertUnchanged(before)
        # 5. no live code: past 10 minutes, no attempt counted
        self.at(60)
        again = self.resend(self.issued)
        self.at(60 + 600)
        before = self.digest()
        self.assertIs(self.redeem(self.issued, code_of(again)).outcome, Outcome.CODE_EXPIRED)
        self.assertUnchanged(before)
        # 9–12. a fresh code redeems
        self.at(60 + 601)
        newest = self.resend(self.issued)
        self.assertIs(self.redeem(self.issued, code_of(newest)).outcome, Outcome.REDEEMED)
        # 2. final row: nothing written
        before = self.digest()
        result = self.redeem(self.issued, code_of(newest))
        self.assertEqual((result.outcome, result.final_state), (Outcome.NOT_PENDING, "redeemed"))
        self.assertUnchanged(before)

    def test_rv1_step4_expired_row_is_expired_and_scrubbed(self):
        self.at(30 * 60)
        self.assertIs(self.redeem(self.issued, self.code).outcome, Outcome.EXPIRED)
        self.assertScrubbed(self.issued.recovery_id, "expired")

    def test_rv2_success_sets_the_exact_password_rotates_the_key_and_issues_nothing_else(self):
        before = self.user(self.user_id)
        with patch.object(accounts, "set_password", wraps=accounts.set_password) as set_password:
            result = self.redeem(self.issued, self.code)
        self.assertEqual((result.outcome, result.user_id), (Outcome.REDEEMED, self.user_id))
        set_password.assert_called_once()
        self.assertEqual(set_password.call_args.args[1:], (self.user_id, NEW_PASSWORD))
        after = self.user(self.user_id)
        self.assertNotEqual(after["password_hash"], before["password_hash"])
        self.assertNotEqual(after["password_changed_at"], before["password_changed_at"])
        self.assertNotEqual(after["session_key"], before["session_key"])
        self.assertIsNone(accounts.load_session_user(self.conn, self.user_id, before["session_key"]))
        self.assertTrue(self.authenticates(EMAIL, NEW_PASSWORD))
        self.assertFalse(self.authenticates(EMAIL, OLD_PASSWORD))
        self.assertScrubbed(self.issued.recovery_id, "redeemed")
        for column in ("role", "is_active", "email", "email_verified_at", "display_name", "last_login_at"):
            self.assertEqual(after[column], before[column], column)

    def test_rv4_the_sign_in_throttle_is_neither_read_nor_written(self):
        self.fail_sign_in(EMAIL, times=10)
        key = self.throttle_key(EMAIL)
        self.assertTrue(login_throttle.is_blocked(self.conn, key, self.wall))
        throttle = self.table_rows("login_throttle")
        with self.statements() as seen:
            self.assertIs(self.redeem(self.issued, self.code).outcome, Outcome.REDEEMED)
        self.assertFalse([sql for sql in seen if "login_throttle" in sql])
        self.assertEqual(self.table_rows("login_throttle"), throttle)
        self.assertEqual(self.sign_in(EMAIL, NEW_PASSWORD), "blocked")  # until the block ends
        self.at(15 * 60)
        self.assertEqual(self.sign_in(EMAIL, NEW_PASSWORD), "ok")

    def test_rv5_a_repeat_after_success_writes_nothing(self):
        self.redeem(self.issued, self.code)
        before = self.digest()
        result = self.redeem(self.issued, self.code, "a different password")
        self.assertEqual((result.outcome, result.final_state), (Outcome.NOT_PENDING, "redeemed"))
        self.assertUnchanged(before)
        self.assertTrue(self.authenticates(EMAIL, NEW_PASSWORD))

    def test_rv7_scrypt_only_for_a_correct_code_with_a_passed_recheck(self):
        shadow = self.request("unknown@example.bg")
        with self.scrypt_calls() as counter:
            self.redeem(self.issued, "00000000")
            self.redeem(shadow, "00000000")
            self.tx(lambda conn: conn.execute("UPDATE users SET is_active = 0 WHERE id = ?", (self.user_id,)))
            self.assertIs(self.redeem(self.issued, self.code).outcome, Outcome.INVALIDATED)
            self.redeem(self.issued, self.code)  # NOT_PENDING
        self.assertEqual(counter["hashes"], 0)
        self.tx(lambda conn: conn.execute("UPDATE users SET is_active = 1 WHERE id = ?", (self.user_id,)))
        self.at(1)
        fresh = self.request()
        with self.scrypt_calls() as counter:
            self.assertIs(self.redeem(fresh, code_of(fresh)).outcome, Outcome.REDEEMED)
        self.assertEqual(counter["hashes"], 1)

    def test_rv6_wrong_code_steps_read_no_account_and_match_for_real_and_shadow_rows(self):
        shadow = self.request("unknown@example.bg")
        with self.statements() as real:
            self.redeem(self.issued, "00000000")
        with self.statements() as other:
            self.redeem(shadow, "00000000")
        from tests.recovery_service_support import normalized
        self.assertEqual(normalized(real), normalized(other))
        self.assertFalse([sql for sql in real if "users" in sql])


class PasswordExactnessTest(RecoveryServiceTestCase):
    def test_rb3_the_password_is_stored_exactly_as_typed(self):
        nfkc = "ﬁxed passphrase ２０２６"  # a ligature and full-width digits: NFKC changes it
        self.assertNotEqual(unicodedata.normalize("NFKC", nfkc), nfkc)
        cases = {
            "spaces": ("  leading and trailing  ", ["leading and trailing", "  leading and trailing"]),
            "doubled": ("inner  doubled  spaces", ["inner doubled spaces"]),
            "tab": ("tab\tinside password", ["tab inside password"]),
            "nfkc": (nfkc, [unicodedata.normalize("NFKC", nfkc)]),
            "256": ("p" * 255 + " ", ["p" * 255]),
        }
        for index, (name, (password, variants)) in enumerate(cases.items()):
            with self.subTest(name):
                email = f"exact{index}@example.bg"
                self.verified(email)
                issued = self.request(email)
                self.assertIs(self.redeem(issued, code_of(issued), password).outcome, Outcome.REDEEMED)
                self.assertTrue(self.authenticates(email, password))
                for variant in variants:
                    self.assertFalse(self.authenticates(email, variant), variant)


class FailureTest(RecoveryServiceTestCase):
    def setUp(self):
        super().setUp()
        self.user_id = self.verified()
        self.issued = self.request()
        self.code = code_of(self.issued)

    def test_rf1_an_exception_at_each_statement_then_rollback_leaves_everything_as_before(self):
        self.fail_sign_in(EMAIL, times=3)
        other = self.request(previous=None)  # another browser's row of the same account
        self.redeem(self.issued, "00000000")  # one attempt already counted
        conn = sqlite3.connect(self.path, factory=FailingConnection)
        self.addCleanup(conn.close)
        conn.execute("PRAGMA foreign_keys = ON")
        for fragment in ("code_attempts = code_attempts + 1", "SET code_hash = NULL, code_expires_at = NULL",
                         "FROM users WHERE id", "UPDATE users SET password_hash", "SET state = ?"):
            with self.subTest(fragment):
                before = self.digest()
                conn.fail_on = fragment
                with self.assertRaises(sqlite3.OperationalError):
                    self.redeem(self.issued, self.code, conn=conn)
                self.assertFalse(conn.in_transaction)
                self.assertEqual(self.digest(), before)
                self.assertEqual(self.row(self.issued.recovery_id)["code_attempts"], 1)
        conn.fail_on = None
        self.assertIs(self.redeem(self.issued, self.code, conn=conn).outcome, Outcome.REDEEMED)
        self.assertEqual(self.row(other.recovery_id)["state"], "pending")

    def test_rf2_i_a_real_commit_failure_applies_nothing_and_a_retry_redeems(self):
        self.request("unknown@example.bg")
        before = self.digest()
        self.conn.execute("PRAGMA defer_foreign_keys = ON")
        self.conn.execute("BEGIN IMMEDIATE")
        result = recovery.redeem(self.conn, self.binding(self.issued), self.code, NEW_PASSWORD, secret_key=KEY,
                                 clock=self.clock)
        self.assertIs(result.outcome, Outcome.REDEEMED)  # a decision, not a fact
        # A deferred foreign-key violation: SQLite refuses the COMMIT itself.
        self.conn.execute("UPDATE password_recoveries SET user_id = 999999, is_shadow = 0, "
                          "session_key_digest = ? WHERE state = 'pending'", ("e" * 64,))
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.commit()
        self.assertTrue(self.conn.in_transaction)
        self.conn.rollback()
        self.conn.execute("PRAGMA defer_foreign_keys = OFF")
        self.assertEqual(self.digest(), before)
        self.assertTrue(self.authenticates(EMAIL, OLD_PASSWORD))
        self.assertFalse(self.authenticates(EMAIL, NEW_PASSWORD))
        self.assertIs(self.redeem(self.issued, self.code).outcome, Outcome.REDEEMED)
        self.assertTrue(self.authenticates(EMAIL, NEW_PASSWORD))

    def test_rf2_ii_a_commit_that_took_effect_then_raised_makes_the_retry_write_nothing(self):
        self.conn.execute("BEGIN IMMEDIATE")
        recovery.redeem(self.conn, self.binding(self.issued), self.code, NEW_PASSWORD, secret_key=KEY,
                        clock=self.clock)
        self.conn.commit()  # took effect; the caller saw it raise and must not claim success
        before = self.digest()
        result = self.redeem(self.issued, self.code)
        self.assertEqual((result.outcome, result.final_state), (Outcome.NOT_PENDING, "redeemed"))
        self.assertUnchanged(before)

    def test_rf4_set_password_raising_is_a_rollback_never_a_success(self):
        for error in (ValueError("Парола изисква имейл."), LookupError("Няма потребител.")):
            with self.subTest(error=type(error).__name__):
                before = self.digest()
                with patch.object(accounts, "set_password", side_effect=error):
                    with self.assertRaises(type(error)):
                        self.redeem(self.issued, self.code)
                self.assertUnchanged(before)
        self.assertIs(self.redeem(self.issued, self.code).outcome, Outcome.REDEEMED)

    def test_a_password_equal_to_the_address_is_refused_by_set_password_and_rolled_back(self):
        before = self.digest()
        with self.assertRaises(ValueError):
            self.redeem(self.issued, self.code, EMAIL.upper())
        self.assertUnchanged(before)


class LimitsTest(RecoveryServiceTestCase):
    def setUp(self):
        super().setUp()
        self.verified()
        self.issued = self.request()

    def test_rt1_five_wrong_codes_then_the_correct_one_is_refused_until_a_resend(self):
        for value in ("00000000", "", "abc", None, "1234"):
            self.assertIs(self.redeem(self.issued, value).outcome, Outcome.WRONG_CODE)
        self.assertIs(self.redeem(self.issued, code_of(self.issued)).outcome, Outcome.TOO_MANY)
        self.at(60)
        again = self.resend(self.issued)
        self.assertIs(self.redeem(self.issued, code_of(again)).outcome, Outcome.REDEEMED)

    def test_rt2_code_and_recovery_lifetimes(self):
        self.at(600)
        before = self.digest()
        self.assertIs(self.redeem(self.issued, code_of(self.issued)).outcome, Outcome.CODE_EXPIRED)
        self.assertUnchanged(before)
        self.at(599)
        # (back within the skew: the code is live again; time is the caller's clock)
        self.assertIs(self.redeem(self.issued, "00000000").outcome, Outcome.WRONG_CODE)
        self.at(1800)
        self.assertIs(self.redeem(self.issued, code_of(self.issued)).outcome, Outcome.EXPIRED)
        self.assertScrubbed(self.issued.recovery_id, "expired")

    def test_rt3_the_code_expiry_never_passes_the_recovery_expiry(self):
        for seconds in (60, 120, 1500):
            self.at(seconds)
            self.assertIs(self.resend(self.issued).outcome, Outcome.ISSUED)
        row = self.row(self.issued.recovery_id)
        self.assertEqual(row["code_expires_at"], row["expires_at"])
        self.assertEqual(row["expires_at"], stamp(T0 + timedelta(minutes=30)))

    def test_rt4_wrong_recovery_codes_never_count_toward_the_sign_in_throttle(self):
        with self.statements() as seen:
            for _ in range(100):
                self.redeem(self.issued, "00000000")
        self.assertFalse([sql for sql in seen if "login_throttle" in sql])
        self.assertEqual(self.count("login_throttle"), 0)
        self.assertEqual(self.sign_in(EMAIL, OLD_PASSWORD), "ok")


class HoldAndClockTest(RecoveryServiceTestCase):
    def setUp(self):
        super().setUp()
        self.user_id = self.verified()
        self.issued = self.request()

    def test_rh1_the_hold_refuses_request_resend_and_redemption_with_nothing_written(self):
        self.at(60)
        self.set_control(high_water=T0, hold="restore")
        before = self.digest()
        self.assertIs(self.request().outcome, Outcome.UNAVAILABLE)
        self.assertIs(self.resend(self.issued).outcome, Outcome.UNAVAILABLE)
        result = self.redeem(self.issued, code_of(self.issued))
        self.assertEqual((result.outcome, result.reason), (Outcome.UNAVAILABLE, registration.Availability.HOLD))
        self.assertUnchanged(before)

    def test_rh2_cancel_during_the_hold_voids_and_leaves_hold_and_mark(self):
        self.set_control(high_water=T0 + timedelta(seconds=200), hold="restore")
        control = self.control()
        self.assertIs(self.cancel(self.issued).outcome, Outcome.CANCELLED)
        self.assertScrubbed(self.issued.recovery_id, "void")
        self.assertEqual(self.control(), control)

    def test_rh3_nothing_releases_the_hold_with_time(self):
        self.set_control(high_water=T0, hold="restore")
        for days in (1, 7, 365):
            self.at(days * 86400)
            self.assertIs(self.request().outcome, Outcome.UNAVAILABLE)
        self.assertEqual(self.control()["hold_reason"], "restore")

    def test_rcl1_clock_behind_the_mark_writes_nothing_and_cancel_refuses(self):
        self.at(1900)  # the row would be expired by its own times
        self.tx(lambda conn: registration.raise_mark(conn, self.wall + timedelta(seconds=301)))
        before = self.digest()
        self.assertIs(self.request("other@example.bg").outcome, Outcome.UNAVAILABLE)
        self.assertIs(self.resend(self.issued).outcome, Outcome.UNAVAILABLE)
        self.assertIs(self.redeem(self.issued, code_of(self.issued)).outcome, Outcome.UNAVAILABLE)
        result = self.cancel(self.issued)
        self.assertEqual((result.outcome, result.reason),
                         (Outcome.UNAVAILABLE, registration.Availability.CLOCK_BEHIND))
        self.assertUnchanged(before)
        self.assertEqual(self.row(self.issued.recovery_id)["state"], "pending")

    def test_rcl2_a_row_dated_in_the_future_expires_only_once_the_clock_is_accepted(self):
        self.at(900)
        future = self.request("future@example.bg")
        self.at(0)
        self.assertIs(self.redeem(future, "00000000").outcome, Outcome.UNAVAILABLE)
        self.assertIsNotNone(self.view(future))  # times not judged while the clock is behind
        self.tx(lambda conn: registration.accept_clock(conn, clock=self.clock))
        self.assertIsNone(self.view(future))
        self.assertIs(self.redeem(future, "00000000").outcome, Outcome.EXPIRED)
        self.assertScrubbed(future.recovery_id, "expired")

    def test_rcl3_both_purposes_raise_and_obey_one_mark(self):
        from tests.registration_support import password_hash, student
        self.at(1000)
        self.request("mark@example.bg")
        self.assertEqual(self.control()["high_water_at"], stamp(self.wall))
        self.at(1000 - 301)
        self.conn.execute("BEGIN IMMEDIATE")
        refused = registration.create_pending(
            self.conn, cleaned=student("reg@example.bg"), account_type="student", password_hash=password_hash(),
            previous_binding=None, secret_key=KEY, clock=self.clock, sender_busy=False)
        self.conn.rollback()
        self.assertEqual(refused.reason, registration.Availability.CLOCK_BEHIND)
        self.at(3000)
        self.conn.execute("BEGIN IMMEDIATE")
        registration.create_pending(
            self.conn, cleaned=student("reg@example.bg"), account_type="student", password_hash=password_hash(),
            previous_binding=None, secret_key=KEY, clock=self.clock, sender_busy=False)
        self.conn.commit()
        self.at(3000 - 301)
        result = self.request("after@example.bg")
        self.assertEqual(result.reason, registration.Availability.CLOCK_BEHIND)

    def test_rcl4_a_forward_step_expires_rows_and_codes_early(self):
        self.at(31 * 60)
        self.assertIs(self.redeem(self.issued, code_of(self.issued)).outcome, Outcome.EXPIRED)

    def test_rcl5_a_clock_set_back_does_not_raise_the_attempts_a_code_allows(self):
        self.at(200)
        for _ in range(3):
            self.redeem(self.issued, "00000000")
        self.at(0)
        self.redeem(self.issued, "00000000")
        self.redeem(self.issued, "00000000")
        self.assertIs(self.redeem(self.issued, code_of(self.issued)).outcome, Outcome.TOO_MANY)


class WriterOrderTest(RecoveryServiceTestCase):
    """RC8: one order for resend, redeem and cancel (design 3.5)."""

    def setUp(self):
        super().setUp()
        self.verified()
        self.final = self.request()
        self.cancel(self.final)
        self.pending = self.request()

    def calls(self, issued):
        return {
            "resend": lambda: self.resend(issued),
            "redeem": lambda: self.redeem(issued, "00000000"),
            "cancel": lambda: self.cancel(issued),
        }

    def test_rc8_none_or_final_rows_are_answered_with_nothing_written_even_under_hold_or_clock(self):
        missing = type(self.final)(outcome=self.final.outcome, token="x" * 43)
        for control in (dict(hold="restore"), dict(high_water=T0 + timedelta(seconds=301))):
            self.set_control(**control)
            for name, call in {**self.calls(self.final), **{f"{k} (none)": v for k, v in
                                                            self.calls(missing).items()}}.items():
                with self.subTest(control=control, call=name):
                    before = self.digest()
                    self.assertIn(call().outcome, (Outcome.NOT_PENDING, Outcome.NO_PENDING))
                    self.assertUnchanged(before)

    def test_rc8_with_a_pending_row_the_gate_comes_before_any_write(self):
        self.at(1900)  # past the row's time: every call would otherwise expire it
        for control in (dict(hold="restore", high_water=self.wall), dict(high_water=self.wall + timedelta(seconds=301))):
            self.set_control(**control)
            for name, call in self.calls(self.pending).items():
                if name == "cancel" and "hold" in control:
                    continue  # cancel is allowed during a hold (RH2)
                with self.subTest(control=control, call=name):
                    before = self.digest()
                    with self.statements() as seen:
                        self.assertIs(call().outcome, Outcome.UNAVAILABLE)
                    self.assertUnchanged(before)
                    self.assertEqual(writes(seen), [])
                    self.assertEqual(self.row(self.pending.recovery_id)["state"], "pending")

    def test_rc8_statement_order_lock_clock_row_gate_then_writes(self):
        for index, name in enumerate(("resend", "redeem", "cancel")):
            with self.subTest(name):
                start = 4000 * (index + 1)
                self.at(start)
                issued = self.request("order@example.bg")
                self.at(start + 1900)  # past the row's time: the first write is its expiry
                with self.statements() as seen:
                    self.calls(issued)[name]()
                self.assertIn(LOCK, seen[1])  # right after BEGIN IMMEDIATE
                row = next(i for i, sql in enumerate(seen) if "FROM password_recoveries WHERE binding_hash" in sql)
                gate = next(i for i, sql in enumerate(seen) if "FROM registration_control WHERE id = 1" in sql)
                first_write = next(i for i, sql in enumerate(seen) if sql in writes(seen))
                self.assertLess(row, gate)
                self.assertLess(gate, first_write)

if __name__ == "__main__":
    import unittest
    unittest.main()
