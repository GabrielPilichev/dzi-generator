"""Registration send ledger, caps, total budget and the mail circuit
(EMAIL_REGISTRATION PR 3; test-plan section D-L and E5).

Fixture: ``tests.registration_support``. No mail is sent: outcomes are settled directly.
"""

import heapq
import logging
import random
import sqlite3
import threading
import unittest
from datetime import timedelta
from unittest.mock import patch

from tests.registration_support import EMAIL, KEY, T0, RegistrationTestCase, code_of, stamp
from web.services import accounts, registration
from web.services.registration import Availability, Outcome

HOUR = 3600
W = int(registration.DAILY_WINDOW.total_seconds())  # 24 h 15 min
GUESSABLE = 86400 + 600  # a code guessable in the 24 h ending at T was issued within this span


def wrong(code: str) -> str:
    return f"{(int(code) + 1) % 10**6:06d}"


class LedgerTest(RegistrationTestCase):
    def available(self, **kwargs):
        kwargs.setdefault("sender_busy", False)
        return registration.availability(self.conn, self.wall, **kwargs)

    def test_every_issuance_is_one_ledger_row_stamped_now(self):
        """DL1."""
        accounts.create_user(self.conn, display_name="Собственик", email="owner@example.bg",
                             password="the-owners-password")
        self.conn.commit()
        self.at(17)
        code = self.submit()
        notice = self.submit("owner@example.bg")
        self.at(90)
        again = self.resend(code)
        ledger = self.sends()
        self.assertEqual([row["id"] for row in ledger], [code.send_id, notice.send_id, again.send_id])
        self.assertEqual([row["kind"] for row in ledger], ["code", "notice", "code"])
        self.assertEqual([row["attempted_at"] for row in ledger],
                         [stamp(T0 + timedelta(seconds=s)) for s in (17, 17, 90)])
        self.assertEqual({row["outcome"] for row in ledger}, {"started"})
        self.assertEqual({row["settled_at"] for row in ledger}, {None})
        self.assertEqual([row["pending_id"] for row in ledger],
                         [code.pending_id, notice.pending_id, code.pending_id])
        self.assertEqual(ledger[0]["email_hash"], registration.email_hash(KEY, EMAIL))
        self.assertEqual(ledger[0]["email_hash"], ledger[2]["email_hash"])
        self.assertNotEqual(ledger[0]["email_hash"], ledger[1]["email_hash"])

    def test_ledger_row_is_in_the_transaction_that_stores_the_code(self):
        self.conn.execute("BEGIN IMMEDIATE")
        issued = registration.create_pending(
            self.conn, cleaned={"account_type": "student", "first_name": "Ана", "last_name": "Иванова",
                                "email": EMAIL, "grade": 9},
            account_type="student", password_hash=accounts.hash_new_password("a-long-enough-password", EMAIL),
            previous_binding=None, secret_key=KEY, clock=self.clock, sender_busy=False)
        self.assertEqual(self.send(issued.send_id)["outcome"], "started")
        self.assertEqual(self.pending(issued.pending_id)["code_send_id"], issued.send_id)
        self.conn.rollback()
        self.assertEqual((self.count("registration_sends"), self.count("pending_registrations")), (0, 0))

    def test_hourly_cap_per_address(self):
        """DL2: across rows, across real and shadow rows, independent of other addresses."""
        accounts.create_user(self.conn, display_name="Собственик", email="owner@example.bg",
                             password="the-owners-password")
        self.conn.commit()
        for base, email in ((0, EMAIL), (10000, "owner@example.bg")):
            with self.subTest(email=email):
                self.at(base)
                first = self.submit(email)
                self.at(base + 60)
                self.assertEqual(self.resend(first).outcome, Outcome.ISSUED)
                for seconds in (100, 200, 300):
                    self.at(base + seconds)
                    self.assertEqual(self.submit(email).outcome, Outcome.ISSUED)
                address = registration.email_hash(KEY, email)
                for seconds in (301, 3599, 3600):
                    self.at(base + seconds)
                    self.assertTrue(registration.address_capped(self.conn, address, self.wall))
                    before = self.digest()
                    self.assertEqual(self.submit(email).outcome, Outcome.ADDRESS_CAPPED)
                    self.assertUnchanged(before)
                self.assertEqual(self.submit(f"someone.else{base}@example.bg").outcome, Outcome.ISSUED)
                self.at(base + 3601)  # one second after the first left the window
                self.assertFalse(registration.address_capped(self.conn, address, self.wall))
                self.assertEqual(self.submit(email).outcome, Outcome.ISSUED)

    def test_resend_is_refused_at_the_address_cap(self):
        first = self.submit()
        for seconds in (1, 2, 3, 4):
            self.at(seconds)
            self.submit()
        self.at(70)
        before = self.digest()
        self.assertEqual(self.resend(first).outcome, Outcome.ADDRESS_CAPPED)
        self.assertUnchanged(before)

    def fill_daily(self):
        """Eight sends of one address: five in the first hour, three after it."""
        for seconds in (0, 1, 2, 3, 4, 3601, 3602, 3603):
            self.at(seconds)
            self.assertEqual(self.submit().outcome, Outcome.ISSUED, seconds)

    def test_daily_cap_by_issuance_time(self):
        """DL3."""
        self.fill_daily()
        for seconds in (3604, 7300, 86400, W - 1, W):
            self.at(seconds)
            self.assertEqual(self.submit().outcome, Outcome.ADDRESS_CAPPED, seconds)
        self.at(W + 1)
        self.assertEqual(self.submit().outcome, Outcome.ISSUED)
        self.assertEqual(self.submit().outcome, Outcome.ADDRESS_CAPPED)  # 8 again inside the window

    def test_first_reviews_boundary_schedule(self):
        """DL4: counting by issuance time refuses what counting by row creation admitted."""
        row_a = self.submit()
        for seconds in (60, 120, 180):
            self.at(seconds)
            self.assertEqual(self.resend(row_a).outcome, Outcome.ISSUED)
        self.at(3601)
        row_b = self.submit()
        for seconds in (3661, 3721, 3781):
            self.at(seconds)
            self.assertEqual(self.resend(row_b).outcome, Outcome.ISSUED)
        for seconds in (86401, 86402, 86403, 86404):
            self.at(seconds)
            self.assertEqual(self.submit().outcome, Outcome.ADDRESS_CAPPED, seconds)
        address = registration.email_hash(KEY, EMAIL)
        self.assertEqual(registration._count_since(self.conn, self.wall, timedelta(hours=24), address), 7)
        self.assertEqual(registration._count_since(self.conn, self.wall, registration.DAILY_WINDOW, address), 8)
        # Counting pending rows by their creation time would have seen two rows in the day
        # and admitted all twelve sends.
        created = self.conn.execute(
            "SELECT COUNT(*) FROM pending_registrations WHERE created_at >= ?",
            (stamp(self.wall - registration.DAILY_WINDOW),),
        ).fetchone()[0]
        self.assertEqual(created, 2)

    def test_guessing_bound_with_a_steady_clock_an_unchanged_key_and_an_intact_ledger(self):
        """DL5. **Assumptions (design 3.3), all three required:** the wall clock only advances and
        is never stepped; the secret key is not changed; no ledger row of the address is removed
        other than by the 48-hour cleanup (no restore, released hold or operator clearing). Under
        them, at most 8 codes of an address are guessable in any 24 hours. Outside them no number
        is claimed: see DC7, D11 and DC4 for the known exceptions."""
        generator = random.Random(20261013)
        for schedule in range(30):
            self.conn.execute("DELETE FROM pending_registrations")
            self.conn.execute("DELETE FROM registration_sends")
            self.conn.execute("DELETE FROM registration_control")
            self.conn.commit()
            issued_at, current = [], None
            probes = [generator.randrange(0, 4 * 86400) for _ in range(40)]
            heapq.heapify(probes)
            last = -1
            while probes:
                seconds = heapq.heappop(probes)
                if seconds <= last or seconds > 4 * 86400 + W:
                    continue
                last = seconds
                self.at(seconds)
                result = None
                if current is not None and generator.random() < 0.6:
                    result = self.resend(current)
                if result is None or result.outcome is not Outcome.ISSUED:
                    result = self.submit()
                    current = result if result.outcome is Outcome.ISSUED else current
                if result.outcome is Outcome.ISSUED:
                    issued_at.append(seconds)
                    for boundary in (60, HOUR, W):  # one second either side of every boundary
                        for near in (-1, 0, 1):
                            heapq.heappush(probes, seconds + boundary + near)
            self.assertGreater(len(issued_at), 16, schedule)
            for index, start in enumerate(issued_at):
                inside = sum(1 for moment in issued_at[index:] if moment < start + GUESSABLE)
                self.assertLessEqual(inside, registration.ADDRESS_DAILY_CAP, (schedule, start))

    def test_future_dated_ledger_row_counts_in_every_window(self):
        """DL6."""
        address = registration.email_hash(KEY, EMAIL)
        for _ in range(5):
            self.add_send(seconds=10 * 86400, address=address)
        self.assertTrue(registration.address_capped(self.conn, address, self.wall))
        self.assertEqual(self.submit().outcome, Outcome.ADDRESS_CAPPED)
        self.at(9 * 86400)
        self.assertEqual(self.submit().outcome, Outcome.ADDRESS_CAPPED)

    def test_total_budget(self):
        """DL7: shared by codes and notices, refused for every address, logged once per refusal."""
        for index in range(300):
            self.conn.execute(
                "INSERT INTO registration_sends (email_hash, kind, attempted_at, outcome) VALUES (?, ?, ?, ?)",
                (registration.email_hash(KEY, f"a{index}@example.bg"), "notice" if index % 3 else "code",
                 stamp(T0 + timedelta(seconds=index)), "refused"),
            )
        self.conn.commit()
        self.at(600)
        self.assertEqual(self.available(), Availability.TOTAL_CAP)
        issued_before = self.digest()
        with self.assertLogs("learnpilot.registration", level=logging.WARNING) as logs:
            result = self.submit("fresh@example.bg")
        self.assertEqual((result.outcome, result.reason), (Outcome.UNAVAILABLE, Availability.TOTAL_CAP))
        self.assertEqual(sum("total_cap_reached" in line for line in logs.output), 1)
        self.assertUnchanged(issued_before)
        self.at(3600)  # the first row is exactly an hour old: still inside
        self.assertEqual(self.submit("fresh@example.bg").outcome, Outcome.UNAVAILABLE)
        self.at(3601)
        self.assertEqual(self.submit("fresh@example.bg").outcome, Outcome.ISSUED)
        self.assertEqual(self.submit("other@example.bg").reason, Availability.TOTAL_CAP)

    def test_total_cap_can_only_be_lowered(self):
        self.assertEqual(self.submit("a@example.bg", total_cap=2).outcome, Outcome.ISSUED)
        first = self.submit("b@example.bg", total_cap=2)
        self.assertEqual(first.outcome, Outcome.ISSUED)
        self.assertEqual(self.submit("c@example.bg", total_cap=2).reason, Availability.TOTAL_CAP)
        self.at(61)
        self.assertEqual(self.resend(first, total_cap=2).reason, Availability.TOTAL_CAP)
        self.assertEqual(self.available(total_cap=2), Availability.TOTAL_CAP)
        self.assertEqual(self.available(total_cap=3), Availability.AVAILABLE)
        for bad in (301, 0, -1, None, "300", 3.0, True):
            with self.subTest(total_cap=bad), self.assertRaises(ValueError):
                self.available(total_cap=bad)
            with self.assertRaises(ValueError):
                self.submit("d@example.bg", total_cap=bad)

    def attempts_in_one_hour(self, outcome):
        admitted = 0
        for index in range(400):
            self.at(index * 8)
            issued = self.submit(f"p{index}@example.bg")
            if issued.outcome is Outcome.ISSUED:
                admitted += 1
                self.settle(issued, outcome)
            else:
                self.assertEqual(issued.reason, Availability.TOTAL_CAP)
        return admitted

    def test_budget_is_not_refunded_by_refusals(self):
        """DL8: 400 attempts in an hour, every one refused by the relay: exactly 300 admitted."""
        self.assertEqual(self.attempts_in_one_hour("refused"), 300)
        self.assertEqual(self.count("registration_sends"), 300)

    def test_budget_is_not_refunded_by_failures(self):
        """DL8, with the circuit taken out so that the budget alone is measured (DL9 has the circuit)."""
        for outcome in ("not_sent", "unknown"):
            with self.subTest(outcome=outcome), patch.object(registration, "_circuit_open", return_value=False):
                self.conn.execute("DELETE FROM registration_sends")
                self.conn.execute("DELETE FROM pending_registrations")
                self.conn.execute("DELETE FROM registration_control")
                self.conn.commit()
                self.assertEqual(self.attempts_in_one_hour(outcome), 300)

    def test_no_outcome_lowers_a_per_address_count(self):
        with patch.object(registration, "_circuit_open", return_value=False):
            for seconds in range(5):
                self.at(seconds)
                issued = self.submit()
                self.assertEqual(issued.outcome, Outcome.ISSUED)
                self.settle(issued, "not_sent")
            self.at(6)
            self.assertEqual(self.submit().outcome, Outcome.ADDRESS_CAPPED)
            self.assertEqual(self.resend(issued).outcome, Outcome.ADDRESS_CAPPED)  # the wait is waived, the cap is not

    def test_checks_before_hashing_are_reads_and_the_service_never_hashes(self):
        """DL10: what a caller reads before scrypt writes nothing; no path of the service hashes a password."""
        issued = self.submit()
        before = self.digest()
        registration.availability(self.conn, self.wall, sender_busy=False)
        registration.address_capped(self.conn, registration.email_hash(KEY, EMAIL), self.wall)
        self.assertUnchanged(before)
        with patch.object(accounts, "generate_password_hash", side_effect=AssertionError("hashed")), \
                patch.object(accounts, "check_password_hash", side_effect=AssertionError("verified")):
            other = self.submit("other@example.bg")
            self.verify(other, wrong(code_of(other)))
            self.at(61)
            self.resend(issued)
            self.verify(issued, code_of(issued))
        with self.assertRaises(ValueError):
            registration.address_capped(self.conn, EMAIL, self.wall)  # a hash, never an address


class CircuitTest(RegistrationTestCase):
    """DL9, DL9a. The circuit is read from the ledger and has no state of its own."""

    def history(self, *outcomes, seconds=0):
        return [self.add_send(outcome, seconds=seconds + index) for index, outcome in enumerate(outcomes)]

    def is_open(self):
        return registration.availability(self.conn, self.wall, sender_busy=False) is Availability.CIRCUIT_OPEN

    def test_three_consecutive_failures_close_it_before_anything_is_written(self):
        issued = self.submit()
        self.settle(issued, "accepted")
        self.history("not_sent", "unknown", "not_sent", seconds=10)
        self.at(100)
        self.assertTrue(self.is_open())
        before = self.digest()
        with patch.object(registration.secrets, "token_urlsafe", side_effect=AssertionError("row")), \
                patch.object(registration, "_new_code", side_effect=AssertionError("code")), \
                self.assertLogs("learnpilot.registration", level=logging.WARNING) as logs:
            submitted = self.submit("new@example.bg")
            resent = self.resend(issued)
        for result in (submitted, resent):
            self.assertEqual((result.outcome, result.reason, result.message),
                             (Outcome.UNAVAILABLE, Availability.CIRCUIT_OPEN, None))
        self.assertTrue(all("circuit_open" in line for line in logs.output))
        self.assertUnchanged(before)
        # Verification is not an issuance and is not stopped by the circuit.
        self.assertEqual(self.verify(issued, code_of(issued)).outcome, Outcome.ACTIVATED)

    def test_five_minutes_reopen_it(self):
        self.history("unknown", "unknown", "unknown")  # the newest attempted at 2 s
        for seconds, closed in ((2, True), (301, True), (302, False)):
            self.at(seconds)
            self.assertEqual(self.is_open(), closed, seconds)
        self.assertEqual(self.submit().outcome, Outcome.ISSUED)

    def test_sequences(self):
        cases = [
            (("not_sent", "not_sent"), False),
            (("not_sent", "accepted", "unknown", "unknown"), False),
            (("accepted", "unknown", "not_sent", "unknown"), True),
            (("refused", "refused", "refused"), False),
            (("started", "started", "started", "started"), False),
            (("unknown", "started", "unknown", "started", "not_sent"), True),
            (("unknown", "unknown", "refused", "not_sent"), True),       # a refusal does not interrupt the run
            (("unknown", "unknown", "accepted", "refused", "refused", "refused"), False),
            (("accepted", "unknown", "unknown", "unknown", "refused", "refused", "refused"), True),
            (("unknown", "unknown", "unknown", "accepted"), False),
        ]
        for outcomes, closed in cases:
            with self.subTest(outcomes=outcomes):
                self.conn.execute("DELETE FROM registration_sends")
                self.conn.commit()
                self.history(*outcomes)
                self.at(60)
                self.assertEqual(self.is_open(), closed)

    def test_order_is_by_generation_not_by_settlement_time(self):
        first, late, third, fourth = self.history("unknown", "started", "not_sent", "started")
        self.at(30)
        self.settle(fourth, "accepted")
        self.at(40)
        self.assertTrue(self.settle(late, "not_sent"))  # generation 2 fails after generation 4 was accepted
        self.assertEqual([self.send(i)["outcome"] for i in (first, late, third, fourth)],
                         ["unknown", "not_sent", "not_sent", "accepted"])
        self.assertFalse(self.is_open())

    def test_stated_limits_of_the_circuit(self):
        """DL9a: with two handlers two attempts are admitted before either failure is settled, and
        attempts that are never settled never close it. The hourly cap is the bound that remains."""
        self.history("unknown", "unknown")
        self.at(10)
        one, two = self.submit("one@example.bg"), self.submit("two@example.bg")
        self.assertEqual((one.outcome, two.outcome), (Outcome.ISSUED, Outcome.ISSUED))
        self.settle(one, "unknown")
        self.assertTrue(self.is_open())
        self.conn.execute("DELETE FROM registration_sends")
        self.conn.commit()
        for index in range(300):
            self.conn.execute(
                "INSERT INTO registration_sends (email_hash, kind, attempted_at) VALUES (?, 'code', ?)",
                (registration.email_hash(KEY, f"s{index}@example.bg"), stamp(self.wall)))
        self.conn.commit()
        self.assertEqual(registration.availability(self.conn, self.wall, sender_busy=False),
                         Availability.TOTAL_CAP)

    def test_unreaped_sender_is_unavailable_before_admission(self):
        """C17, the registration half: no row, no ledger entry and no charge to the address."""
        issued = self.submit()
        self.at(61)
        self.sender_busy = True
        before = self.digest()
        self.assertEqual(registration.availability(self.conn, self.wall, sender_busy=True),
                         Availability.SENDER_BUSY)
        for result in (self.submit("new@example.bg"), self.resend(issued)):
            self.assertEqual((result.outcome, result.reason), (Outcome.UNAVAILABLE, Availability.SENDER_BUSY))
        self.assertUnchanged(before)
        self.sender_busy = False
        self.assertEqual(self.resend(issued).outcome, Outcome.ISSUED)

    def test_order_of_the_reasons(self):
        self.history("unknown", "unknown", "unknown")
        self.assertEqual(registration.availability(self.conn, self.wall, sender_busy=True, total_cap=1),
                         Availability.SENDER_BUSY)
        self.assertEqual(registration.availability(self.conn, self.wall, sender_busy=False, total_cap=1),
                         Availability.CIRCUIT_OPEN)
        self.set_control(high_water=T0 + timedelta(seconds=301))
        self.assertEqual(registration.availability(self.conn, self.wall, sender_busy=True),
                         Availability.CLOCK_BEHIND)
        self.set_control(high_water=T0 + timedelta(seconds=301), hold="restore")
        self.assertEqual(registration.availability(self.conn, self.wall, sender_busy=True), Availability.HOLD)


class ConcurrencyTest(RegistrationTestCase):
    """DL11 and E5: two connections to one file, more than one handler."""

    def submit_on(self, conn, email, clock, **kwargs):
        return self.submit(email, conn=conn, clock=clock, **kwargs)

    def test_issuance_time_is_sampled_under_the_write_lock(self):
        """DL11: a submission that waited for the lock is stamped after the one that held it."""
        for seconds in (0, 1, 2):
            self.at(seconds)
            self.submit()
        self.at(10)
        released = threading.Event()
        second = {}

        def waiting():
            conn = sqlite3.connect(self.path, timeout=30)
            try:
                def clock():
                    # Read only once this connection is the writer: the first has committed by then.
                    second["lock_held"] = conn.in_transaction
                    second["after_first"] = released.is_set()
                    return self.wall

                second["result"] = self.submit_on(conn, EMAIL, clock)
            finally:
                conn.close()

        self.conn.execute("BEGIN IMMEDIATE")
        thread = threading.Thread(target=waiting)
        thread.start()
        first = registration.create_pending(
            self.conn, cleaned={"account_type": "student", "first_name": "Ана", "last_name": "Иванова",
                                "email": EMAIL, "grade": 9},
            account_type="student", password_hash=self.pending_hash(), previous_binding=None,
            secret_key=KEY, clock=self.clock, sender_busy=False)
        self.at(11)
        released.set()
        self.conn.commit()
        thread.join(60)
        self.assertEqual(first.outcome, Outcome.ISSUED)
        self.assertEqual((second["lock_held"], second["after_first"]), (True, True))
        times = [row["attempted_at"] for row in self.sends()]
        self.assertEqual(times, sorted(times))
        self.assertEqual(times[-2:], [stamp(T0 + timedelta(seconds=10)), stamp(T0 + timedelta(seconds=11))])
        self.at(12)
        self.assertEqual(self.submit().outcome, Outcome.ADDRESS_CAPPED)  # five issued; the cap saw all of them

    def test_last_slot_taken_while_another_submission_waits_for_the_lock(self):
        """DL11: the waiting submission saw a free slot before the lock; the holder of the
        lock takes that slot; the waiter decides again under the lock and is refused."""
        for seconds in (0, 1, 2, 3):
            self.at(seconds)
            self.submit()  # four of the five an hour
        self.at(10)
        address = registration.email_hash(KEY, EMAIL)
        ready, released = threading.Event(), threading.Event()
        second = {}

        def waiting():
            conn = sqlite3.connect(self.path, timeout=30)
            try:
                # What a route reads before hashing: the slot is free, the site available.
                second["capped_before"] = registration.address_capped(conn, address, self.wall)
                second["available_before"] = registration.availability(conn, self.wall, sender_busy=False)
                ready.set()

                def clock():
                    second["lock_held"] = conn.in_transaction
                    second["after_first"] = released.is_set()
                    return self.wall

                second["result"] = self.submit_on(conn, EMAIL, clock)
                second["capped_after"] = registration.address_capped(conn, address, self.wall)
            except BaseException as exc:
                second["error"] = exc
            finally:
                conn.close()

        self.conn.execute("BEGIN IMMEDIATE")  # the lock is held from before the waiter's reads
        thread = threading.Thread(target=waiting)
        thread.start()
        self.assertTrue(ready.wait(30))
        first = registration.create_pending(
            self.conn, cleaned={"account_type": "student", "first_name": "Ана", "last_name": "Иванова",
                                "email": EMAIL, "grade": 9},
            account_type="student", password_hash=self.pending_hash(), previous_binding=None,
            secret_key=KEY, clock=self.clock, sender_busy=False)
        self.assertEqual(first.outcome, Outcome.ISSUED)  # the last slot
        before = self.digest()
        self.at(11)
        released.set()
        self.conn.commit()
        thread.join(60)
        self.assertNotIn("error", second)
        self.assertEqual((second["capped_before"], second["available_before"]), (False, Availability.AVAILABLE))
        self.assertEqual((second["lock_held"], second["after_first"]), (True, True))
        self.assertEqual(second["result"].outcome, Outcome.ADDRESS_CAPPED)
        self.assertIsNone(second["result"].message)
        self.assertTrue(second["capped_after"])
        self.assertEqual(self.digest(), before)  # the refused waiter wrote nothing
        self.assertEqual((self.count("registration_sends"), self.count("pending_registrations")), (5, 5))

    def test_last_total_slot_taken_while_another_submission_waits(self):
        """The same for the total budget, with another address."""
        for index in range(2):
            self.submit(f"t{index}@example.bg")
        result = {}

        def waiting():
            conn = sqlite3.connect(self.path, timeout=30)
            try:
                result["before"] = registration.availability(conn, self.wall, sender_busy=False, total_cap=3)
                ready.set()
                result["after"] = self.submit_on(conn, "waiter@example.bg", self.clock, total_cap=3)
            except BaseException as exc:
                result["error"] = exc
            finally:
                conn.close()

        ready = threading.Event()
        self.conn.execute("BEGIN IMMEDIATE")
        thread = threading.Thread(target=waiting)
        thread.start()
        self.assertTrue(ready.wait(30))
        first = registration.create_pending(
            self.conn, cleaned={"account_type": "student", "first_name": "Ана", "last_name": "Иванова",
                                "email": "holder@example.bg", "grade": 9},
            account_type="student", password_hash=self.pending_hash(), previous_binding=None,
            secret_key=KEY, clock=self.clock, sender_busy=False, total_cap=3)
        self.conn.commit()
        thread.join(60)
        self.assertNotIn("error", result)
        self.assertEqual(first.outcome, Outcome.ISSUED)
        self.assertEqual(result["before"], Availability.AVAILABLE)
        self.assertEqual((result["after"].outcome, result["after"].reason),
                         (Outcome.UNAVAILABLE, Availability.TOTAL_CAP))
        self.assertEqual(self.count("registration_sends"), 3)

    def pending_hash(self):
        from tests.registration_support import password_hash

        return password_hash()

    def race(self, emails, **kwargs):
        barrier = threading.Barrier(len(emails))
        results, errors = [], []

        def run(email):
            conn = sqlite3.connect(self.path, timeout=60)
            try:
                barrier.wait()
                results.append(self.submit_on(conn, email, self.clock, **kwargs))
            except BaseException as exc:
                errors.append(exc)
            finally:
                conn.close()

        threads = [threading.Thread(target=run, args=(email,)) for email in emails]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(90)
        self.assertEqual(errors, [])
        return [result.outcome for result in results]

    def test_concurrent_submissions_cannot_exceed_an_address_cap(self):
        outcomes = self.race([EMAIL] * 12)
        self.assertEqual(outcomes.count(Outcome.ISSUED), 5)
        self.assertEqual(outcomes.count(Outcome.ADDRESS_CAPPED), 7)
        self.assertEqual(self.count("registration_sends"), 5)

    def test_concurrent_submissions_cannot_exceed_the_total_budget(self):
        outcomes = self.race([f"c{index}@example.bg" for index in range(12)], total_cap=7)
        self.assertEqual(outcomes.count(Outcome.ISSUED), 7)
        self.assertEqual(outcomes.count(Outcome.UNAVAILABLE), 5)
        self.assertEqual(self.count("registration_sends"), 7)
        self.assertEqual(self.count("pending_registrations"), 7)


if __name__ == "__main__":
    unittest.main()
