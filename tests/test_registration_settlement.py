"""Registration send settlement and the hand-over to a mailer
(EMAIL_REGISTRATION PR 3; test-plan section D-S, E11 and the service half of H3, H4 and H7).

Mail is ``InMemoryMailer`` or a small fake; nothing is sent and nothing is connected to.
Fixture: ``tests.registration_support``.
"""

import logging
import sqlite3
import threading
import unittest
from dataclasses import replace
from unittest.mock import patch

from tests.registration_support import (
    EMAIL, KEY, RegistrationTestCase, code_of, password_hash, stamp, student,
)
from web.services import accounts, registration
from web.services.mail import InMemoryMailer, OutboundEmail, SendOutcome
from web.services.registration import Outcome, Settlement


class SettlementTest(RegistrationTestCase):
    def test_not_sent_retires_the_code_it_issued_and_waives_the_wait(self):
        """DS1."""
        issued = self.submit()
        counts = (self.count("registration_sends"), self.pending(issued.pending_id)["send_count"])
        self.at(5)
        self.assertTrue(self.settle(issued, SendOutcome.NOT_SENT))
        row = self.pending(issued.pending_id)
        self.assertEqual((row["state"], row["code_hash"], row["code_expires_at"], row["resend_after"]),
                         ("pending", None, None, None))
        self.assertEqual((self.count("registration_sends"), row["send_count"]), counts)
        self.assertIsNotNone(row["password_hash"])
        ledger = self.send(issued.send_id)
        self.assertEqual((ledger["outcome"], ledger["settled_at"]), ("not_sent", stamp(self.wall)))
        self.assertEqual(self.verify(issued, code_of(issued)).outcome, Outcome.CODE_EXPIRED)
        # A resend is admitted at once, and is a second attempt of the address, not a refund.
        again = self.resend(issued)
        self.assertEqual(again.outcome, Outcome.ISSUED)
        self.assertEqual(self.count("registration_sends"), 2)
        self.assertEqual(self.pending(issued.pending_id)["send_count"], 2)
        self.assertEqual(self.verify(issued, code_of(again)).outcome, Outcome.ACTIVATED)

    def test_delayed_failure_after_a_resend_does_not_touch_the_newer_code(self):
        """DS2."""
        self.codes("111111", "222222")
        first = self.submit()
        self.at(61)
        second = self.resend(first)
        self.verify(first, "000000")
        before = self.pending(first.pending_id)
        self.at(70)
        self.assertTrue(self.settle(first, "not_sent"))
        self.assertEqual(self.pending(first.pending_id), before)
        self.assertEqual(before["code_send_id"], second.send_id)
        self.at(100)
        self.assertEqual(self.resend(first).outcome, Outcome.TOO_EARLY)  # the newer wait still applies
        self.assertEqual(self.verify(first, code_of(second)).outcome, Outcome.ACTIVATED)

    def test_delayed_failure_after_verification(self):
        """DS3."""
        issued = self.submit()
        self.verify(issued, code_of(issued))
        row, users = self.pending(issued.pending_id), self.users()
        self.assertTrue(self.settle(issued, "not_sent"))
        self.assertEqual((self.pending(issued.pending_id), self.users()), (row, users))

    def test_delayed_failure_after_cancellation_replacement_and_expiry(self):
        """DS4."""
        cancelled = self.submit("a@example.bg")
        self.cancel(cancelled)
        replaced = self.submit("b@example.bg")
        self.submit("c@example.bg", previous=self.binding(replaced))
        expired = self.submit("d@example.bg")
        self.at(1800)
        self.cleanup()
        for issued, state in ((cancelled, "void"), (replaced, "void"), (expired, "expired")):
            row = self.pending(issued.pending_id)
            self.assertTrue(self.settle(issued, "not_sent"))
            self.assertEqual(self.pending(issued.pending_id), row)
            self.assertScrubbed(issued.pending_id, state)

    def test_first_settlement_wins(self):
        """DS5."""
        for first, second in (("accepted", "accepted"), ("accepted", "not_sent"), ("not_sent", "accepted"),
                              ("unknown", "not_sent"), ("refused", "unknown")):
            with self.subTest(first=first, second=second):
                issued = self.submit(f"{first}.{second}@example.bg")
                self.assertTrue(self.settle(issued, first))
                before = self.digest()
                self.at(30)
                self.assertFalse(self.settle(issued, second))
                self.assertUnchanged(before)
                self.assertEqual(self.send(issued.send_id)["outcome"], first)
                self.at(0)

    def test_refused_and_unknown_change_nothing_in_the_pending_row(self):
        """DS6: the code and the wait stay, the budget is spent."""
        for outcome in ("refused", "unknown", "accepted"):
            with self.subTest(outcome=outcome):
                issued = self.submit(f"{outcome}@example.bg")
                row = self.pending(issued.pending_id)
                self.assertTrue(self.settle(issued, outcome))
                self.assertEqual(self.pending(issued.pending_id), row)
                self.assertEqual(self.send(issued.send_id)["outcome"], outcome)
                self.at(30)
                self.assertEqual(self.resend(issued).outcome, Outcome.TOO_EARLY)
                self.assertEqual(self.verify(issued, code_of(issued)).outcome, Outcome.ACTIVATED)
                self.at(0)

    def test_settlement_that_never_runs(self):
        """DS7: the row stays ``started``, still counts in every cap, and the code still verifies."""
        issued = [self.submit() for _ in range(5)]
        self.assertEqual({row["outcome"] for row in self.sends()}, {"started"})
        self.assertTrue(registration.address_capped(self.conn, registration.email_hash(KEY, EMAIL), self.wall))
        self.assertEqual(self.verify(issued[0], code_of(issued[0])).outcome, Outcome.ACTIVATED)

    def test_two_settlements_released_together(self):
        """DS8."""
        issued = self.submit()
        barrier = threading.Barrier(2)
        results, errors = [], []

        def run(outcome):
            conn = sqlite3.connect(self.path, timeout=30)
            try:
                barrier.wait()
                results.append(self.settle(issued, outcome, conn=conn))
            except BaseException as exc:
                errors.append(exc)
            finally:
                conn.close()

        threads = [threading.Thread(target=run, args=(outcome,)) for outcome in ("not_sent", "accepted")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(60)
        self.assertEqual(errors, [])
        self.assertEqual(sorted(results), [False, True])
        outcome = self.send(issued.send_id)["outcome"]
        self.assertEqual(self.pending(issued.pending_id)["code_hash"] is None, outcome == "not_sent")

    def test_resend_racing_a_late_settlement_in_both_orders(self):
        """E11: on two connections, the newer code survives whichever commits first."""
        other = self.connect()
        for settlement_first in (True, False):
            with self.subTest(settlement_first=settlement_first):
                self.at(0)
                issued = self.submit(f"race{int(settlement_first)}@example.bg")
                self.at(61)
                if settlement_first:
                    self.assertTrue(self.settle(issued, "not_sent", conn=other))
                    again = self.resend(issued)
                else:
                    again = self.resend(issued)
                    self.assertTrue(self.settle(issued, "not_sent", conn=other))
                row = self.pending(issued.pending_id)
                self.assertEqual((row["code_send_id"], row["send_count"]), (again.send_id, 2))
                self.assertIsNotNone(row["code_hash"])
                self.assertEqual(row["resend_after"], stamp(self.wall + registration.RESEND_INTERVAL))
                self.assertEqual(self.verify(issued, code_of(again)).outcome, Outcome.ACTIVATED)

    def test_resend_and_late_settlement_released_together(self):
        issued = self.submit()
        self.at(61)
        barrier = threading.Barrier(2)
        results, errors = {}, []

        def run(name, call):
            conn = sqlite3.connect(self.path, timeout=30)
            try:
                barrier.wait()
                results[name] = call(conn)
            except BaseException as exc:
                errors.append(exc)
            finally:
                conn.close()

        threads = [
            threading.Thread(target=run, args=("resend", lambda conn: self.resend(issued, conn=conn))),
            threading.Thread(target=run, args=("settle",
                                               lambda conn: self.settle(issued, "not_sent", conn=conn))),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(60)
        self.assertEqual(errors, [])
        self.assertEqual((results["resend"].outcome, results["settle"]), (Outcome.ISSUED, True))
        self.assertEqual(self.verify(issued, code_of(results["resend"])).outcome, Outcome.ACTIVATED)

    def test_refuses_what_is_not_a_settlement(self):
        issued = self.submit()
        before = self.digest()
        for outcome in ("started", "delivered", "", None, 0, Outcome.ISSUED):
            with self.subTest(outcome=outcome), self.assertRaises(ValueError):
                self.settle(issued, outcome)
        for send_id in ("1", None, 1.0, True):
            with self.assertRaises(ValueError):
                self.settle_raw(send_id, issued.issuance_id, "accepted")
        self.assertIs(self.settle_raw(issued.send_id + 100, issued.issuance_id, "not_sent"),
                      Settlement.NOT_THIS_ISSUANCE)  # an unknown generation: nothing
        self.assertUnchanged(before)

    def test_settlement_is_not_stopped_by_a_hold_or_a_regressed_clock(self):
        """The ledger's own outcome is recorded whenever it is known; nothing is released by it."""
        first, second = self.submit("a@example.bg"), self.submit("b@example.bg")
        self.set_control(high_water=self.wall, hold="restore")
        self.assertTrue(self.settle(first, "unknown"))
        self.at(-1000)
        self.assertTrue(self.settle(second, "accepted"))
        control = self.control()
        self.assertEqual((control["hold_reason"], control["high_water_at"]), ("restore", stamp(self.at(0))))


class Raising:
    def __init__(self, exception):
        self.exception, self.calls = exception, 0

    def send(self, message):
        self.calls += 1
        raise self.exception


class DispatchTest(RegistrationTestCase):
    """The order of design 4.3: commit, send with no transaction open, settle."""

    def test_sends_the_committed_message(self):
        issued = self.submit()
        mailer = InMemoryMailer(SendOutcome.ACCEPTED)
        outcome = self.dispatch(mailer, issued)
        self.assertEqual(outcome, SendOutcome.ACCEPTED)
        self.assertFalse(self.conn.in_transaction)
        self.assertEqual(len(mailer.messages), 1)
        message = mailer.messages[0]
        self.assertEqual((message.to, message.subject), (EMAIL, registration.CODE_SUBJECT))
        self.assertIn(code_of(issued), message.text)
        self.assertNotRegex(message.text, r"http|Ана|Иванова")
        self.assertEqual(self.send(issued.send_id)["outcome"], "started")  # dispatch records nothing
        self.assertTrue(self.settle(issued, outcome))
        self.assertEqual(self.send(issued.send_id)["outcome"], "accepted")

    def test_never_sends_with_a_transaction_open(self):
        mailer = InMemoryMailer()
        self.conn.execute("BEGIN IMMEDIATE")
        issued = registration.create_pending(
            self.conn, cleaned={"account_type": "student", "first_name": "Ана", "last_name": "Иванова",
                                "email": EMAIL, "grade": 9},
            account_type="student", password_hash=self.submit_hash(), previous_binding=None, secret_key=KEY,
            clock=self.clock, sender_busy=False)
        self.assertEqual(issued.outcome, Outcome.ISSUED)
        with self.assertRaises(registration.NotSendable):
            self.dispatch(mailer, issued)  # decided, not yet committed
        self.conn.rollback()
        with self.assertRaises(registration.NotSendable):
            self.dispatch(mailer, issued)  # the commit never happened: no such row
        self.assertEqual(mailer.messages, [])

    def submit_hash(self):
        from tests.registration_support import password_hash

        return password_hash()

    def test_refuses_what_is_not_an_unsettled_issuance(self):
        mailer = InMemoryMailer()
        self.codes("111111")
        issued = self.submit()
        self.assertTrue(self.settle(issued, "unknown"))
        refused = self.submit("capped@example.bg", total_cap=1)
        self.assertEqual(refused.outcome, Outcome.UNAVAILABLE)
        for result in (issued, refused, None, self.verify(issued, "000000")):
            with self.subTest(result=result), self.assertRaises(registration.NotSendable):
                self.dispatch(mailer, result)
        self.assertEqual(mailer.messages, [])

    def test_exception_from_a_mailer_is_unknown_and_never_retried(self):
        issued = self.submit()
        mailer = Raising(RuntimeError(f"cannot reach {EMAIL}"))
        with self.assertLogs("learnpilot.registration", level=logging.WARNING) as logs:
            outcome = self.dispatch(mailer, issued)
        self.assertEqual((outcome, mailer.calls), (SendOutcome.UNKNOWN, 1))
        self.assertEqual(sum("sender_error error=RuntimeError" in line for line in logs.output), 1)
        self.assertNotIn(EMAIL, "\n".join(logs.output))
        self.settle(issued, outcome)
        # Unknown keeps the code and the wait; the next send is the person's own resend, a new generation.
        self.at(30)
        self.assertEqual(self.resend(issued).outcome, Outcome.TOO_EARLY)
        self.at(61)
        again = self.resend(issued)
        self.assertNotEqual(again.send_id, issued.send_id)
        self.assertEqual(self.count("registration_sends"), 2)

    def test_interrupts_propagate_and_a_wrong_return_is_unknown(self):
        issued = self.submit()
        for interrupt in (KeyboardInterrupt(), SystemExit(1)):
            with self.assertRaises(type(interrupt)):
                self.dispatch(Raising(interrupt), issued)
        self.assertEqual(self.dispatch(InMemoryMailer(lambda message: "accepted"), issued),
                         SendOutcome.UNKNOWN)

    def test_every_outcome_through_the_whole_order(self):
        """Partial failure by outcome: what the row and the ledger hold afterwards."""
        expected = {
            SendOutcome.ACCEPTED: (True, True), SendOutcome.REFUSED: (True, True),
            SendOutcome.UNKNOWN: (True, True), SendOutcome.NOT_SENT: (False, False),
        }
        for index, (outcome, (code_kept, wait_kept)) in enumerate(expected.items()):
            with self.subTest(outcome=outcome):
                issued = self.submit(f"o{index}@example.bg")
                sent = self.dispatch(InMemoryMailer(outcome), issued)
                self.assertTrue(self.settle(issued, sent))
                row = self.pending(issued.pending_id)
                self.assertEqual((row["code_hash"] is not None, row["resend_after"] is not None),
                                 (code_kept, wait_kept))
                self.assertEqual(self.send(issued.send_id)["outcome"], outcome.value)
        self.assertEqual(self.count("registration_sends"), 4)  # every attempt stays counted

    def test_failed_settlement_transaction_leaves_a_safe_state(self):
        """H5, the service half: the settlement is rolled back, the row stays ``started``."""
        issued = self.submit()
        sent = self.dispatch(InMemoryMailer(SendOutcome.NOT_SENT), issued)
        self.conn.execute("BEGIN IMMEDIATE")
        self.assertIs(registration.settle_send(self.conn, issued.send_id, issued.issuance_id, sent,
                                               clock=self.clock), Settlement.SETTLED)
        self.conn.rollback()  # the commit failed
        self.assertEqual(self.send(issued.send_id)["outcome"], "started")
        self.assertEqual(self.verify(issued, code_of(issued)).outcome, Outcome.ACTIVATED)


class DispatchBindingTest(RegistrationTestCase):
    """What ``dispatch`` sends is the committed issuance itself, not whatever carries its ids."""

    def uncommitted(self, email=EMAIL):
        """An issuance decided and then rolled back: its result object outlives it."""
        self.conn.execute("BEGIN IMMEDIATE")
        issued = registration.create_pending(
            self.conn, cleaned=student(email), account_type="student", password_hash=password_hash(),
            previous_binding=None, secret_key=KEY, clock=self.clock, sender_busy=False)
        self.conn.rollback()
        self.assertEqual(issued.outcome, Outcome.ISSUED)
        return issued

    def assertRefused(self, issued, **kwargs):
        mailer = InMemoryMailer()
        with self.assertRaises(registration.NotSendable):
            self.dispatch(mailer, issued, **kwargs)
        self.assertEqual(mailer.messages, [])
        self.assertFalse(self.conn.in_transaction)

    def assertSent(self, issued):
        mailer = InMemoryMailer()
        self.assertEqual(self.dispatch(mailer, issued), SendOutcome.ACCEPTED)
        self.assertEqual(mailer.messages, [issued.message])

    def test_rolled_back_issuance_whose_ids_were_reused(self):
        self.codes("111111", "222222")
        stale = self.uncommitted("a@example.bg")
        committed = self.submit("b@example.bg")
        self.assertEqual((stale.pending_id, stale.send_id), (committed.pending_id, committed.send_id))
        self.assertRefused(stale)  # the same numbers, another recipient and another code
        self.assertSent(committed)

    def test_reused_ids_and_the_same_address(self):
        self.codes("111111", "222222", "333333", "333333")
        stale = self.uncommitted()
        committed = self.submit()
        self.assertEqual((stale.pending_id, stale.send_id, stale.message.to),
                         (committed.pending_id, committed.send_id, committed.message.to))
        self.assertRefused(stale)  # another code: the stored hash is of the committed one
        # Even the same six digits: a first issuance is tied to its own browser token.
        self.cancel(committed)
        stale = self.uncommitted()
        committed = self.submit()
        self.assertEqual((stale.pending_id, stale.send_id, stale.message),
                         (committed.pending_id, committed.send_id, committed.message))
        self.assertRefused(stale)
        self.assertSent(committed)

    def test_rolled_back_resend_identical_in_everything_but_identity(self):
        """The same ids, recipient, digits and message, and no token: only the identity differs."""
        self.codes("111111", "222222", "222222")
        first = self.submit()
        self.at(61)
        self.conn.execute("BEGIN IMMEDIATE")
        stale = registration.resend(self.conn, self.binding(first), secret_key=KEY, clock=self.clock,
                                    sender_busy=False)
        self.conn.rollback()
        committed = self.resend(first)
        self.assertEqual((stale.pending_id, stale.send_id, stale.kind, stale.token, stale.message),
                         (committed.pending_id, committed.send_id, committed.kind, None, committed.message))
        self.assertNotEqual(stale.issuance_id, committed.issuance_id)
        self.assertRefused(stale)
        self.assertSent(committed)

    def test_rolled_back_first_issuance_identical_in_everything_but_identity(self):
        """The token and the binding made the same as well: the identity alone refuses it."""
        self.codes("111111", "111111")
        with patch.object(registration.secrets, "token_urlsafe", return_value="t" * 43):
            stale = self.uncommitted()
            committed = self.submit()
        self.assertEqual((stale.pending_id, stale.send_id, stale.kind, stale.token, stale.message),
                         (committed.pending_id, committed.send_id, committed.kind, committed.token,
                          committed.message))
        self.assertNotEqual(stale.issuance_id, committed.issuance_id)
        self.assertRefused(stale)
        self.assertSent(committed)

    def test_rolled_back_notice_identical_in_everything_but_identity(self):
        self.conn.execute("BEGIN IMMEDIATE")
        accounts.create_user(self.conn, display_name="Мария Петрова", email=EMAIL, password="existing-password")
        self.conn.commit()
        first = self.submit()
        self.at(61)
        self.conn.execute("BEGIN IMMEDIATE")
        stale = registration.resend(self.conn, self.binding(first), secret_key=KEY, clock=self.clock,
                                    sender_busy=False)
        self.conn.rollback()
        committed = self.resend(first)
        self.assertEqual((stale.send_id, stale.kind, stale.message), (committed.send_id, "notice", committed.message))
        self.assertRefused(stale)
        self.assertSent(committed)

    def test_altered_or_missing_identity(self):
        self.codes("111111", "222222")
        issued, other = self.submit(), self.submit("other@example.bg")
        identity = issued.issuance_id
        for name, value in {
            "missing": None, "empty": "", "of another issuance": other.issuance_id,
            "one character changed": ("0" if identity[0] != "0" else "1") + identity[1:],
            "upper case": identity.upper() if identity.upper() != identity else "A" * 32,
            "shortened": identity[:31], "lengthened": identity + "0", "with a space": identity + " ",
            "bytes": identity.encode("ascii"), "a number": int(identity, 16), "the number of the row": issued.send_id,
            "a LIKE pattern": "%", "random": registration._new_issuance_id(),
        }.items():
            with self.subTest(identity=name):
                self.assertRefused(replace(issued, issuance_id=value))
        self.assertSent(issued)
        self.assertSent(other)

    def test_ledger_row_without_an_identity_is_never_sent(self):
        """A row older than migration 022: it fails closed, and no identity is invented for it."""
        self.codes("111111")
        issued = self.submit()
        self.conn.execute("UPDATE registration_sends SET issuance_id = NULL WHERE id = ?", (issued.send_id,))
        self.conn.commit()
        before = self.digest()
        self.assertRefused(issued)
        self.assertRefused(replace(issued, issuance_id=None))
        self.assertUnchanged(before)
        self.assertIsNone(self.send(issued.send_id)["issuance_id"])

    def test_altered_payload(self):
        self.codes("111111", "222222")
        issued = self.submit()
        other = self.submit("other@example.bg")
        message = issued.message
        altered = {
            "recipient": replace(issued, message=replace(message, to="eve@example.bg")),
            "recipient of another row": replace(issued, message=replace(message, to="other@example.bg")),
            "subject": replace(issued, message=replace(message, subject=registration.NOTICE_SUBJECT)),
            "subject text": replace(issued, message=replace(message, subject=message.subject + " ")),
            "body appended": replace(issued, message=replace(message, text=message.text + "http://example.bg\n")),
            "body replaced": replace(issued, message=replace(message, text="111111")),
            "another code": replace(issued, message=replace(message, text=message.text.replace("111111", "222222"))),
            "notice body": replace(issued, message=registration._notice_message(EMAIL)),
            "kind": replace(issued, kind="notice"),
            "no kind": replace(issued, kind=None),
            "token": replace(issued, token=other.token),
            "message of another row": replace(issued, message=other.message),
            "not a message": replace(issued, message="111111"),
            "no message": replace(issued, message=None),
        }
        for name, result in altered.items():
            with self.subTest(altered=name):
                self.assertRefused(result)
        self.assertRefused(issued, key="another-key")
        with self.assertRaises(ValueError):
            self.dispatch(InMemoryMailer(), issued, key="")
        self.assertSent(issued)
        self.assertSent(other)

    def test_wrong_generation(self):
        self.codes("111111", "222222", "333333")
        first = self.submit()
        other = self.submit("other@example.bg")
        self.at(61)
        second = self.resend(first)
        self.assertRefused(first)  # replaced by a newer code: the old one is not sent any more
        for name, result in {
            "generation of another row": replace(second, send_id=other.send_id),
            "another row": replace(second, pending_id=other.pending_id),
            "unknown generation": replace(second, send_id=second.send_id + 100),
            "old generation, new message": replace(second, send_id=first.send_id),
            "no generation": replace(second, send_id=None),
            "generation as text": replace(second, send_id=str(second.send_id)),
        }.items():
            with self.subTest(altered=name):
                self.assertRefused(result)
        self.assertSent(second)
        self.assertSent(other)

    def test_uncommitted_and_settled_generations(self):
        self.codes("111111", "222222", "333333", "444444")
        mailer = InMemoryMailer()
        self.conn.execute("BEGIN IMMEDIATE")
        issued = registration.create_pending(
            self.conn, cleaned=student(), account_type="student", password_hash=password_hash(),
            previous_binding=None, secret_key=KEY, clock=self.clock, sender_busy=False)
        with self.assertRaises(registration.NotSendable):
            self.dispatch(mailer, issued)  # this connection sees the rows, and refuses for the open transaction
        with self.assertRaises(registration.NotSendable):
            self.dispatch(mailer, issued, conn=self.connect())  # another connection sees no such rows
        self.conn.commit()
        self.assertSent(issued)
        for outcome in ("accepted", "refused", "unknown", "not_sent"):
            with self.subTest(settled=outcome):
                issued = self.submit(f"{outcome}@example.bg") if outcome != "accepted" else issued
                self.assertTrue(self.settle(issued, outcome))
                self.assertRefused(issued)
        self.assertEqual(mailer.messages, [])

    def test_registration_that_is_no_longer_pending(self):
        self.codes("111111", "222222", "333333")
        cancelled, activated, expired = (self.submit(f"{name}@example.bg") for name in "abc")
        self.cancel(cancelled)
        self.verify(activated, "222222")
        self.at(1800)
        self.cleanup()
        for issued in (cancelled, activated, expired):
            self.assertRefused(issued)

    def test_legitimate_code_notice_and_resend(self):
        self.codes("111111", "222222")
        self.conn.execute("BEGIN IMMEDIATE")
        accounts.create_user(self.conn, display_name="Мария Петрова", email="taken@example.bg",
                             password="existing-password")
        self.conn.commit()
        code = self.submit()
        notice = self.submit("taken@example.bg")
        self.assertEqual((code.kind, notice.kind), ("code", "notice"))
        self.assertSent(code)
        self.assertSent(notice)
        self.assertEqual(notice.message, registration._notice_message("taken@example.bg"))
        self.assertRefused(replace(notice, kind="code"))
        self.assertRefused(replace(notice, pending_id=code.pending_id))
        self.assertRefused(replace(notice, message=replace(notice.message, text=code.message.text)))
        self.at(61)
        resent, renoticed = self.resend(code), self.resend(notice)
        self.assertIsNone(resent.token)
        self.assertSent(resent)
        self.assertSent(renoticed)
        self.assertEqual(self.verify(code, "222222").outcome, Outcome.ACTIVATED)

    def test_committed_rows_that_disagree_with_each_other(self):
        """Fail closed when the ledger row and the registration do not describe one issuance."""
        self.codes("111111")
        issued = self.submit()
        as_notice = replace(issued, kind="notice", message=registration._notice_message(EMAIL))
        self.assertRefused(as_notice)
        self.conn.execute("UPDATE registration_sends SET kind = 'notice' WHERE id = ?", (issued.send_id,))
        self.conn.commit()
        self.assertRefused(as_notice)  # the ledger says notice, the registration is not a shadow
        self.assertRefused(issued)
        self.conn.execute("UPDATE registration_sends SET kind = 'code', email_hash = ? WHERE id = ?",
                          (registration.email_hash(KEY, "other@example.bg"), issued.send_id))
        self.conn.commit()
        self.assertRefused(issued)  # the attempt was counted against another address
        self.conn.execute("UPDATE registration_sends SET email_hash = ? WHERE id = ?",
                          (registration.email_hash(KEY, EMAIL), issued.send_id))
        self.conn.commit()
        self.assertSent(issued)

    def test_not_single_send_before_settlement(self):
        """Pinned, not a guarantee: nothing here stops a second hand-over of one generation.
        Calling ``dispatch`` once is the caller's obligation until a decision says otherwise."""
        issued = self.submit()
        mailer = InMemoryMailer()
        self.assertEqual([self.dispatch(mailer, issued) for _ in range(2)], [SendOutcome.ACCEPTED] * 2)
        self.assertEqual(self.dispatch(mailer, issued, conn=self.connect()), SendOutcome.ACCEPTED)
        self.assertEqual(mailer.messages, [issued.message] * 3)
        self.assertEqual(self.send(issued.send_id)["outcome"], "started")
        self.assertTrue(self.settle(issued, "accepted"))
        self.assertRefused(issued)

    def test_refusals_log_nothing_and_store_nothing(self):
        self.codes("111111")
        issued = self.submit()
        before = self.digest()
        with self.assertNoLogs("learnpilot.registration", level=logging.DEBUG):
            self.assertRefused(replace(issued, message=replace(issued.message, to="eve@example.bg")))
        with self.assertLogs("learnpilot.registration", level=logging.DEBUG) as logs:
            self.assertSent(issued)
        self.assertEqual([line.split(":", 2)[2] for line in logs.output],
                         [f"send_started pending={issued.pending_id} send={issued.send_id}"])
        self.assertUnchanged(before)
        with self.assertRaises(registration.NotSendable) as caught:
            self.dispatch(InMemoryMailer(), replace(issued, kind="notice"))
        for secret in (EMAIL, "111111", issued.token, KEY):
            self.assertNotIn(secret, str(caught.exception))


class SettlementIdentityTest(RegistrationTestCase):
    """Settlement names an issuance by its number and its identity, never by the number alone."""

    def stale_and_committed(self):
        """A rolled-back issuance and the committed one that got its numbers, alike in everything else."""
        self.codes("111111", "111111")
        self.conn.execute("BEGIN IMMEDIATE")
        stale = registration.create_pending(
            self.conn, cleaned=student(), account_type="student", password_hash=password_hash(),
            previous_binding=None, secret_key=KEY, clock=self.clock, sender_busy=False)
        self.conn.rollback()
        committed = self.submit()
        self.assertEqual((stale.pending_id, stale.send_id, stale.message),
                         (committed.pending_id, committed.send_id, committed.message))
        return stale, committed

    def test_stale_result_settles_nothing_of_the_issuance_that_reused_its_number(self):
        stale, committed = self.stale_and_committed()
        before = self.digest()
        for outcome in ("not_sent", "unknown", "refused", "accepted"):
            with self.subTest(outcome=outcome), \
                    self.assertLogs("learnpilot.registration", level=logging.WARNING) as logs:
                self.assertIs(self.settle_raw(stale.send_id, stale.issuance_id, outcome),
                              Settlement.NOT_THIS_ISSUANCE)
                self.assertEqual([line.split(":", 2)[2] for line in logs.output],
                                 [f"settlement_refused send={stale.send_id}"])
                self.assertNotIn(stale.issuance_id, "\n".join(logs.output))
                self.assertUnchanged(before)
        # Not retired, not counted in the circuit, still unsettled, still usable.
        ledger = self.send(committed.send_id)
        self.assertEqual((ledger["outcome"], ledger["settled_at"]), ("started", None))
        self.assertIsNotNone(self.pending(committed.pending_id)["code_hash"])
        self.assertIs(self.settle_raw(committed.send_id, committed.issuance_id, "accepted"), Settlement.SETTLED)
        self.assertEqual(self.verify(committed, "111111").outcome, Outcome.ACTIVATED)

    def test_three_stale_failures_do_not_open_the_circuit(self):
        stale, committed = self.stale_and_committed()
        for _ in range(3):
            self.settle_raw(stale.send_id, stale.issuance_id, "unknown")
        self.assertEqual(registration.availability(self.conn, self.wall, sender_busy=False),
                         registration.Availability.AVAILABLE)
        self.assertEqual(self.send(committed.send_id)["outcome"], "started")

    def test_caller_does_not_settle_after_not_sendable(self):
        """The sequence PR 4 must follow: commit, dispatch, and settle only what dispatch returned."""
        def deliver(issued, mailer):
            try:
                outcome = self.dispatch(mailer, issued)
            except registration.NotSendable:
                return None  # nothing was handed over: there is no outcome to record
            return self.settle_raw(issued.send_id, issued.issuance_id, outcome)

        stale, committed = self.stale_and_committed()
        mailer = InMemoryMailer(SendOutcome.NOT_SENT)
        before = self.digest()
        self.assertIsNone(deliver(stale, mailer))
        self.assertEqual(mailer.messages, [])
        self.assertUnchanged(before)
        self.assertIs(deliver(committed, mailer), Settlement.SETTLED)
        self.assertEqual(mailer.messages, [committed.message])
        self.assertEqual(self.send(committed.send_id)["outcome"], "not_sent")
        self.assertIsNone(deliver(committed, mailer))  # settled: not sendable, and nothing settled again
        self.assertEqual(len(mailer.messages), 1)

    def test_wrong_identity_for_an_existing_number(self):
        self.codes("111111", "222222")
        issued, other = self.submit(), self.submit("other@example.bg")
        before = self.digest()
        for name, (send_id, identity) in {
            "identity of another issuance": (issued.send_id, other.issuance_id),
            "number of another issuance": (other.send_id, issued.issuance_id),
            "random identity": (issued.send_id, registration._new_issuance_id()),
            "unknown number": (issued.send_id + 100, issued.issuance_id),
            "negative number": (-1, issued.issuance_id),
        }.items():
            with self.subTest(case=name):
                self.assertIs(self.settle_raw(send_id, identity, "not_sent"), Settlement.NOT_THIS_ISSUANCE)
                self.assertUnchanged(before)

    def test_missing_or_malformed_identity_is_refused_before_anything_is_read(self):
        issued = self.submit()
        identity = issued.issuance_id
        before = self.digest()
        with patch.object(registration, "_writer_now", side_effect=AssertionError("transaction used")):
            for value in (None, "", identity.upper() if identity.upper() != identity else "A" * 32,
                          identity[:31], identity + "0", " " + identity, identity.encode("ascii"),
                          int(identity, 16), issued.send_id, "%", "g" * 32, ["a" * 32]):
                with self.subTest(identity=value), self.assertRaises(ValueError) as caught:
                    self.settle_raw(issued.send_id, value, "accepted")
                self.assertNotIn(identity, str(caught.exception))
        self.assertUnchanged(before)
        with self.assertRaises(TypeError):
            registration.settle_send(self.conn, issued.send_id, "accepted", clock=self.clock)  # the old call

    def test_ledger_row_without_an_identity_is_never_settled(self):
        """Older than migration 022: it stays ``started``, counted in the caps and not in the circuit."""
        issued = self.submit()
        self.conn.execute("UPDATE registration_sends SET issuance_id = NULL WHERE id = ?", (issued.send_id,))
        self.conn.commit()
        before = self.digest()
        for identity in (issued.issuance_id, registration._new_issuance_id()):
            self.assertIs(self.settle_raw(issued.send_id, identity, "not_sent"), Settlement.NOT_THIS_ISSUANCE)
        self.assertUnchanged(before)
        self.assertEqual(self.send(issued.send_id)["outcome"], "started")
        self.assertEqual(registration._count_since(self.conn, self.wall, registration.HOUR), 1)

    def test_duplicate_legitimate_settlement(self):
        issued = self.submit()
        self.assertIs(self.settle_raw(issued.send_id, issued.issuance_id, "unknown"), Settlement.SETTLED)
        before = self.digest()
        self.at(30)
        for outcome in ("unknown", "not_sent", "accepted"):
            with self.assertNoLogs("learnpilot.registration", level=logging.WARNING):
                self.assertIs(self.settle_raw(issued.send_id, issued.issuance_id, outcome),
                              Settlement.ALREADY_SETTLED)
            self.assertUnchanged(before)
        self.assertIsNotNone(self.pending(issued.pending_id)["code_hash"])

    def test_late_settlement_of_an_older_generation_after_a_resend(self):
        self.codes("111111", "222222")
        first = self.submit()
        self.at(61)
        second = self.resend(first)
        row = self.pending(first.pending_id)
        self.at(70)
        self.assertIs(self.settle_raw(first.send_id, first.issuance_id, "not_sent"), Settlement.SETTLED)
        self.assertEqual(self.pending(first.pending_id), row)  # the newer code, its attempts and its wait
        self.assertEqual((self.send(first.send_id)["outcome"], self.send(second.send_id)["outcome"]),
                         ("not_sent", "started"))
        # The older identity with the newer number, and the reverse, settle nothing.
        before = self.digest()
        self.assertIs(self.settle_raw(second.send_id, first.issuance_id, "not_sent"), Settlement.NOT_THIS_ISSUANCE)
        self.assertUnchanged(before)
        self.assertEqual(self.verify(first, "222222").outcome, Outcome.ACTIVATED)
        self.assertIs(self.settle_raw(second.send_id, second.issuance_id, "accepted"), Settlement.SETTLED)

    def test_late_settlement_after_cancellation_and_activation(self):
        """Settlement belongs to the ledger row: it does not need the registration to be current."""
        self.codes("111111", "222222")
        cancelled, activated = self.submit("a@example.bg"), self.submit("b@example.bg")
        self.cancel(cancelled)
        self.verify(activated, "222222")
        users = self.users()
        for issued, state in ((cancelled, "void"), (activated, "activated")):
            row = self.pending(issued.pending_id)
            self.assertIs(self.settle_raw(issued.send_id, issued.issuance_id, "not_sent"), Settlement.SETTLED)
            self.assertEqual(self.pending(issued.pending_id), row)
            self.assertScrubbed(issued.pending_id, state)
            self.assertEqual(self.send(issued.send_id)["outcome"], "not_sent")
        self.assertEqual(self.users(), users)


if __name__ == "__main__":
    unittest.main()
