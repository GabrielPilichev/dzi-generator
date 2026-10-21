"""Password recovery 8.3: the hold, the clock and the budgets, through the real routes.

Carried from scope 8.2 (PASSWORD_RECOVERY_8_2_STATUS.md, obligation 2): the service tests drove
the calls a route makes; these requests go through the routes. Test plan RH1 to RH3, RCl1,
RL1 to RL4 and RL7 at the HTTP level. RL7 is the pinned **exceptions** to the guessing bound,
not a bound: a released hold with lost history, a key rotation and an operator clearing each
admit eight more codes in the same real day, and they add up. No test asserts a bound across
repeated events. The restore script itself is Linux evidence (design 9), not exercised here.
Temporary databases only; the sign-in throttle is neither read nor changed by any of this.
"""

import shutil
import sqlite3
import unittest
from contextlib import closing
from datetime import timedelta
from unittest.mock import patch

from tests.recovery_route_support import EMAIL, NEW_PASSWORD, OLD_PASSWORD, RecoveryRouteTestCase
from web.blueprints import registration as registration_blueprint
from web.recovery_settings import CONFIG_KEY, RecoverySettings
from web.registration_settings import CONFIG_KEY as REGISTRATION_KEY, RegistrationSettings

WRONG = "00000000"
UNAVAILABLE = "Възстановяването на парола временно не е достъпно. Опитай по-късно."
CAPPED = "Твърде много имейли към този адрес. Опитай отново по-късно."


class HoldAndClockRouteTest(RecoveryRouteTestCase):
    def test_a_restore_hold_closes_every_write_but_cancel_and_changes_nothing(self):
        """RH1, RH2: request, resend and redemption answer 'unavailable' with nothing written, the code
        and attempts kept; cancel voids the row and leaves the hold and the mark exactly as they were."""
        self.verified()
        self.recover()
        code = self.last_code()
        self.set_control(hold="restore")
        control, before = self.control(), self.digest()
        statuses = {
            "request": self.begin(client=self.app.test_client()),
            "redeem": self.redeem(code),
            "resend": self.resend(),
        }
        for name, response in statuses.items():
            with self.subTest(route=name):
                self.assertEqual(response.status_code, 503)
                self.assertIn(UNAVAILABLE, self.body(response))
        self.assertEqual(self.digest(), before)
        self.assertEqual(self.recoveries()[0]["code_attempts"], 0)
        self.assertIsNotNone(self.slot())
        self.assertEqual(len(self.mailer.messages), 1)
        self.assertEqual(self.client.get("/recover/verify").status_code, 200)     # a read, as always
        self.assertEqual(self.cancel().status_code, 303)
        self.assertEqual(self.recoveries()[0]["state"], "void")
        self.assertEqual(self.control(), control)
        self.assertTrue(self.authenticates(EMAIL, OLD_PASSWORD))

    def test_a_clock_behind_the_mark_refuses_every_write_including_cancel_and_writes_nothing(self):
        """RCl1: nothing is expired, scrubbed, voided or counted, whatever the row would otherwise be."""
        self.verified()
        self.recover()
        code = self.last_code()
        self.set_control(high_water=self.now + timedelta(hours=2))
        control, before = self.control(), self.digest()
        self.advance(40 * 60)       # the row would have expired: it must not be touched
        for name, response in (("request", self.begin(client=self.app.test_client())), ("redeem", self.redeem(code)),
                               ("resend", self.resend()), ("cancel", self.cancel())):
            with self.subTest(route=name):
                self.assertEqual(response.status_code, 503)
        self.assertEqual(self.digest(), before)
        self.assertEqual(self.control(), control)
        self.assertEqual(self.recoveries()[0]["state"], "pending")
        self.assertIsNotNone(self.slot())

    def test_nothing_releases_a_hold_automatically(self):
        """RH3: a day, a week and a year later it is still held."""
        self.verified()
        self.set_control(hold="restore")
        for days in (1, 7, 365):
            self.advance(days * 86400)
            self.set_control_keep_hold()
            self.assertEqual(self.begin(client=self.app.test_client()).status_code, 503)
        self.assertEqual(self.control()["hold_reason"], "restore")
        self.assertEqual(self.mailer.messages, [])

    def set_control_keep_hold(self):
        """Move the shared mark to the test clock without touching the hold (a year-long gap would
        otherwise leave the mark behind by being unchanged, which is a different refusal)."""
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute("UPDATE registration_control SET high_water_at = ? WHERE id = 1",
                         (self.now.strftime("%Y-%m-%d %H:%M:%S"),))
            conn.commit()

    def test_a_gate_turned_off_leaves_the_hold_and_the_budgets_alone(self):
        self.verified()
        self.recover()
        control = self.control()
        self.app.config[CONFIG_KEY] = None
        self.assertEqual(self.begin().status_code, 404)
        self.assertEqual(self.control(), control)


class SharedBudgetRouteTest(RecoveryRouteTestCase):
    """RL1 to RL4 at the routes: one ledger, one total budget, one per-address budget."""

    def test_the_fifth_request_in_an_hour_is_admitted_the_sixth_is_refused_and_a_refusal_costs_nothing(self):
        self.verified()
        for _ in range(5):
            self.advance(1)
            self.recover(client=self.app.test_client())
        before = self.digest()
        response = self.begin(client=self.app.test_client())
        self.assertEqual(response.status_code, 429)
        self.assertIn(CAPPED, self.body(response))
        self.assertEqual(self.digest(), before)
        self.assertEqual(len(self.mailer.messages), 5)
        self.advance(3601)      # the oldest left the hour window
        self.assertEqual(self.begin(client=self.app.test_client()).status_code, 303)

    def test_recovery_and_registration_draw_on_one_total_budget_and_one_address_budget(self):
        with patch.dict(self.app.config, {
                REGISTRATION_KEY: RegistrationSettings(mailer=self.mailer, total_cap=3),
                CONFIG_KEY: RecoverySettings(mailer=self.mailer, total_cap=3)}), \
                patch.object(registration_blueprint, "_utcnow", lambda: self.now):
            self.verified()
            self.recover("someone@example.bg", client=self.app.test_client())
            self.advance(1)
            self.recover(client=self.app.test_client())
            self.advance(1)
            # A registration submission for a third address uses the last of the shared total.
            submitted = self.client.post("/register/student", headers={"Origin": "http://localhost"}, data={
                "first_name": "Ана", "last_name": "Иванова", "email": "pupil@example.bg", "grade": "9",
                "password": "a long enough passphrase", "confirm_password": "a long enough passphrase"})
            self.assertEqual(submitted.status_code, 303)
            self.advance(1)
            # The total is used up for both purposes, in both directions.
            refused = self.begin("fourth@example.bg", client=self.app.test_client())
            self.assertEqual(refused.status_code, 503)
            self.assertIn(UNAVAILABLE, self.body(refused))
            again = self.client.post("/register/student", headers={"Origin": "http://localhost"}, data={
                "first_name": "Ана", "last_name": "Иванова", "email": "other.pupil@example.bg", "grade": "9",
                "password": "a long enough passphrase", "confirm_password": "a long enough passphrase"})
            self.assertEqual(again.status_code, 503)
            # Redemption is not stopped by the budget (RL9): only by the hold and the clock.
            client = self.app.test_client()
            self.assertEqual(self.begin(EMAIL, client=client).status_code, 503)

    def test_redemption_is_not_stopped_by_an_exhausted_budget_or_an_open_circuit(self):
        """RL9."""
        self.verified()
        self.recover()
        code = self.last_code()
        with patch.dict(self.app.config, {CONFIG_KEY: RecoverySettings(mailer=self.mailer, total_cap=1)}):
            self.assertEqual(self.begin("busy@example.bg", client=self.app.test_client()).status_code, 503)
            self.assertEqual(self.redeem(code).status_code, 303)
        self.assertTrue(self.authenticates(EMAIL, NEW_PASSWORD))


class ReleasedHoldBudgetExceptionsTest(RecoveryRouteTestCase):
    """RL7 at the routes: exceptions to the guessing bound, pinned, accumulating, and never a bound."""

    def issue_until_capped(self, start=0):
        """Requests from fresh browsers until the address is refused; the count of admitted ones and the moment."""
        admitted, moment = 0, start
        while True:
            self.at(moment)
            response = self.begin(client=self.app.test_client())
            if response.status_code != 303:
                self.assertEqual(response.status_code, 429, self.body(response)[:200])
                return admitted, moment
            admitted += 1
            moment += 3601 if admitted % 5 == 0 else 1

    def test_eight_codes_a_day_then_a_key_rotation_a_clearing_and_a_released_hold_each_admit_eight_more(self):
        self.verified()
        snapshot = self.root_path("snapshot.db")
        with closing(sqlite3.connect(self.db_path)) as source, closing(sqlite3.connect(snapshot)) as target:
            source.backup(target)
        first, moment = self.issue_until_capped()
        self.assertEqual(first, 8)                                   # five an hour, eight in 24 h 15 min
        self.assertEqual(self.begin(client=self.app.test_client()).status_code, 429)
        # A key rotation: a fresh address key (and every binding, code and digest stops matching).
        original_key = self.app.config["SECRET_KEY"]
        with patch.dict(self.app.config, {"SECRET_KEY": original_key + "-rotated"}):
            rotated, moment = self.issue_until_capped(start=moment + 1)
        self.assertEqual(rotated, 8)
        # An operator clearing of the address's limit, under the original key.
        self.cli("clear-registration-limit", "--email", EMAIL)
        cleared, moment = self.issue_until_capped(start=moment + 1)
        self.assertEqual(cleared, 8)
        # A released hold with lost history: the restored snapshot predates those sends.
        shutil.copyfile(snapshot, self.db_path)
        output = self.cli("registrations-restore-gate")
        self.assertIn("recovery", output.lower())
        self.at(moment + 1)
        self.assertEqual(self.begin(client=self.app.test_client()).status_code, 503)    # held
        released = self.cli("registrations-release-hold", "--acknowledge-lost-history")
        self.assertIn("recovery", released)
        restored, moment = self.issue_until_capped(start=moment + 2)
        self.assertEqual(restored, 8)
        # They add up: 32 codes of one address inside a real day. This is the documented
        # accumulation of exceptions (design 5.4), pinned so that no one reads a bound into it.
        self.assertLess(moment, 86400)
        self.assertEqual(first + rotated + cleared + restored, 32)

    def root_path(self, name):
        import os
        import tempfile
        directory = tempfile.TemporaryDirectory(prefix="recovery-limits-")
        self.addCleanup(directory.cleanup)
        return os.path.join(directory.name, name)


if __name__ == "__main__":
    unittest.main()
