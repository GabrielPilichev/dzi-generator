"""Password recovery service (scope 8.2): request and resend (RC), input rules (RB1, RB2),
issuance identity and dispatch (RI), transaction contract (RF3, RF5), logging and result
representation (RLog1–RLog3), and the public helpers the service uses (design 4.2).
"""

import logging
import re
import sqlite3
from datetime import timedelta
from unittest.mock import patch

from tests.recovery_service_support import (
    EMAIL, KEY, NEW_PASSWORD, OLD_PASSWORD, RecoveryServiceTestCase, code_of, normalized, stamp,
)
from tests.registration_support import T0
from web.services import accounts, identities, recovery, registration
from web.services.mail import InMemoryMailer, OutboundEmail, SendOutcome

Outcome = recovery.Outcome


class PublicHelpersTest(RecoveryServiceTestCase):
    """Design 4.2: thin public wrappers in ``registration`` and ``accounts.recovery_candidate``."""

    def test_writer_now_takes_the_lock_then_reads_the_clock(self):
        with self.assertRaises(registration.TransactionRequired):
            registration.writer_now(self.conn, self.clock)
        calls = []
        self.conn.execute("BEGIN")
        with self.statements() as seen:
            now = registration.writer_now(self.conn, lambda: calls.append(len(seen)) or self.wall)
        self.conn.rollback()
        self.assertEqual(now, T0)
        self.assertEqual(calls, [1])  # read once, after the locking statement
        self.assertIn("UPDATE registration_control SET id = id WHERE 0", seen[0])

    def test_control_blocked_is_the_shared_hold_and_mark(self):
        self.assertIsNone(registration.control_blocked(self.conn, self.wall))
        self.set_control(high_water=T0 + timedelta(seconds=301))
        self.assertIs(registration.control_blocked(self.conn, self.wall), registration.Availability.CLOCK_BEHIND)
        self.set_control(high_water=T0, hold="restore")
        self.assertIs(registration.control_blocked(self.conn, self.wall), registration.Availability.HOLD)
        self.assertIsNone(registration.control_blocked(self.conn, self.wall, hold=False))
        with self.assertRaises(ValueError):
            registration.control_blocked(self.conn, T0.replace(tzinfo=None))

    def test_raise_mark_never_lowers_and_needs_a_transaction(self):
        with self.assertRaises(registration.TransactionRequired):
            registration.raise_mark(self.conn, self.wall)
        self.tx(lambda conn: registration.raise_mark(conn, T0 + timedelta(minutes=5)))
        self.tx(lambda conn: registration.raise_mark(conn, T0))
        self.assertEqual(self.control()["high_water_at"], stamp(T0 + timedelta(minutes=5)))
        self.assertIsNone(self.control()["hold_reason"])

    def test_recovery_candidate_reads_eligibility_columns_and_never_the_hash(self):
        user_id = self.verified()
        with self.assertRaises(RuntimeError):
            accounts.recovery_candidate(self.conn, email=EMAIL)
        self.conn.execute("BEGIN IMMEDIATE")
        by_email = accounts.recovery_candidate(self.conn, email=EMAIL)
        by_id = accounts.recovery_candidate(self.conn, user_id=user_id)
        self.assertIsNone(accounts.recovery_candidate(self.conn, email=EMAIL.upper()))  # exact stored form
        self.assertIsNone(accounts.recovery_candidate(self.conn, user_id=True))
        self.assertIsNone(accounts.recovery_candidate(self.conn, user_id=999))
        with self.assertRaises(ValueError):
            accounts.recovery_candidate(self.conn, email=EMAIL, user_id=user_id)
        with self.assertRaises(ValueError):
            accounts.recovery_candidate(self.conn)
        self.conn.rollback()
        self.assertEqual(by_email, by_id)
        self.assertEqual(set(by_email), {"id", "email", "role", "is_active", "has_password",
                                         "email_verified_at", "session_key"})
        self.assertEqual(by_email["has_password"], 1)
        self.assertNotIn(self.user(user_id)["password_hash"], by_email.values())

    def test_set_password_is_unchanged(self):
        # The service calls it as it is: same rules, same writes (design 4.2).
        user_id = self.cli_account("plain@example.bg")
        with self.assertRaises(ValueError):
            accounts.set_password(self.conn, user_id, "short")
        before = self.user(user_id)
        key = self.run_account(accounts.set_password, user_id, NEW_PASSWORD)
        after = self.user(user_id)
        self.assertEqual(after["session_key"], key)
        self.assertNotEqual(after["password_hash"], before["password_hash"])
        self.assertTrue(self.authenticates("plain@example.bg", NEW_PASSWORD))


class RequestTest(RecoveryServiceTestCase):
    def drop_password(self, user_id):
        # Direct SQL: no API removes a password; the CHECK of migration 012 allows it.
        self.tx(lambda conn: conn.execute("UPDATE users SET password_hash = NULL WHERE id = ?", (user_id,)))

    def test_rc1_eligible_request_creates_a_bound_real_row_and_one_ledger_row(self):
        user_id = self.verified()
        self.codes("01234567")
        issued = self.request()
        self.assertIs(issued.outcome, Outcome.ISSUED)
        self.assertEqual(issued.kind, "code")
        row = self.row(issued.recovery_id)
        user = self.user(user_id)
        self.assertEqual((row["state"], row["is_shadow"], row["email"], row["user_id"]), ("pending", 0, EMAIL, user_id))
        self.assertEqual(row["session_key_digest"], recovery.session_key_digest(KEY, user["session_key"]))
        self.assertEqual(row["binding_hash"], recovery.binding_hash(KEY, issued.token))
        self.assertEqual(row["code_hash"], recovery._code_hash(KEY, issued.recovery_id, b"01234567"))
        self.assertEqual(row["code_expires_at"], stamp(T0 + timedelta(minutes=10)))
        self.assertEqual(row["expires_at"], stamp(T0 + timedelta(minutes=30)))
        self.assertEqual(row["resend_after"], stamp(T0 + timedelta(seconds=60)))
        self.assertEqual((row["code_attempts"], row["send_count"], row["code_send_id"]), (0, 1, issued.send_id))
        send = self.send(issued.send_id)
        self.assertEqual((send["recovery_id"], send["pending_id"], send["kind"], send["outcome"]),
                         (issued.recovery_id, None, "code", "started"))
        self.assertEqual(send["attempted_at"], stamp(T0))
        self.assertEqual(send["email_hash"], registration.email_hash(KEY, EMAIL))
        self.assertEqual(send["issuance_id"], issued.issuance_id)
        self.assertRegex(issued.issuance_id, r"^[0-9a-f]{32}$")
        self.assertEqual(self.control()["high_water_at"], stamp(T0))
        self.assertEqual(issued.message.to, EMAIL)
        self.assertEqual(code_of(issued), "01234567")
        # The code exists nowhere in clear.
        for table in ("password_recoveries", "registration_sends", "users", "registration_control"):
            for values in self.table_rows(table):
                self.assertNotIn("01234567", repr(values))

    def test_rc2_each_non_eligible_fixture_gets_a_shadow_and_one_notice_to_the_typed_address(self):
        fixtures = {
            "cli-student@example.bg": lambda e: self.cli_account(e),
            "cli-teacher@example.bg": lambda e: self.cli_account(e, role="teacher"),
            "admin@example.bg": lambda e: self.cli_account(e, role="admin", password="admin password 12345"),
            "verified-admin@example.bg": lambda e: self.run_account(
                accounts.set_role, self.verified(e), "admin"),
            "disabled@example.bg": lambda e: self.run_account(accounts.set_active, self.verified(e), False),
            "nopassword@example.bg": lambda e: self.cli_account(e, password=None),
            "verified-nopassword@example.bg": lambda e: self.drop_password(self.verified(e)),
            "unknown@example.bg": lambda e: None,
        }
        for email, make in fixtures.items():
            with self.subTest(email=email):
                make(email)
                user = self.rows("SELECT * FROM users WHERE email = ?", email)
                issued = self.request(email)
                self.assertIs(issued.outcome, Outcome.ISSUED)
                self.assertEqual(issued.kind, "notice")
                row = self.row(issued.recovery_id)
                self.assertEqual((row["is_shadow"], row["user_id"], row["session_key_digest"]), (1, None, None))
                self.assertIsNotNone(row["code_hash"])
                self.assertEqual(self.send(issued.send_id)["kind"], "notice")
                self.assertEqual(issued.message.to, email)
                self.assertEqual(issued.message.subject, recovery.NOTICE_SUBJECT)
                self.assertNotRegex(issued.message.text, r"\d|https?://|www\.")
                mailer = self.deliver(issued)
                self.assertEqual(len(mailer.messages), 1)
                self.assertEqual(self.rows("SELECT * FROM users WHERE email = ?", email), user)

    def test_google_only_account_finds_nothing_and_email_at_link_gets_no_code(self):
        # RE9: users.email is NULL for a Google-created account; recovery never reads identities.
        user_id = accounts.create_user(self.conn, display_name="Гугъл Акаунт")
        identities.add_identity(self.conn, user_id, issuer="https://accounts.google.com", subject="sub-1",
                                provider="google", email_at_link="linked@example.bg")
        self.conn.commit()
        linked = self.table_rows("user_identities")
        with self.statements() as seen:
            issued = self.request("linked@example.bg")
        self.assertEqual(issued.kind, "notice")
        self.assertFalse([sql for sql in seen if "user_identities" in sql])
        self.assertEqual(self.table_rows("user_identities"), linked)

    def test_rc3_a_second_request_in_the_same_browser_voids_and_scrubs_the_first(self):
        self.verified()
        first = self.request()
        second = self.request(previous=self.binding(first))
        self.assertScrubbed(first.recovery_id, "void")
        self.assertIs(self.redeem(first, code_of(first)).outcome, Outcome.NOT_PENDING)
        self.assertEqual(self.row(second.recovery_id)["state"], "pending")

    def test_rc7_a_code_of_one_recovery_does_not_verify_another_of_the_same_address(self):
        self.verified()
        self.codes("11112222", "11112222")
        first = self.request()
        second = self.request()
        self.assertNotEqual(self.row(first.recovery_id)["code_hash"], self.row(second.recovery_id)["code_hash"])
        self.at(1)
        # Same digits, different row id in the hash: each verifies only its own row.
        self.assertIs(self.redeem(second, code_of(second)).outcome, Outcome.REDEEMED)

    def test_rc5_request_and_resend_never_hash_a_password(self):
        self.verified()
        with self.scrypt_calls() as counter:
            real = self.request()
            shadow = self.request("unknown@example.bg")
            self.at(61)
            self.resend(real)
            self.resend(shadow)
        self.assertEqual(counter["hashes"], 0)

    def test_rc6_every_refusal_writes_nothing(self):
        self.verified()
        self.at(-29 * 60)  # expires at +60 s: still pending after the request at 0
        overdue = self.request("old@example.bg")  # pending and past its time: the inline cleanup's work
        self.at(0)
        previous = self.request()
        self.at(400)
        cases = {
            "hold": lambda: self.set_control(high_water=self.wall, hold="restore"),
            "clock": lambda: self.set_control(high_water=self.wall + timedelta(seconds=301)),
            "sender": lambda: setattr(self, "sender_busy", True),
            "circuit": lambda: [self.add_send(outcome="not_sent") for _ in range(3)],
            "total": lambda: None,
            "address": lambda: [self.add_send(email=EMAIL) for _ in range(5)],
        }
        for name, arrange in cases.items():
            with self.subTest(name):
                self.conn.execute("DELETE FROM registration_control")
                self.conn.execute("DELETE FROM registration_sends WHERE recovery_id IS NULL")
                self.conn.commit()
                self.sender_busy = False
                arrange()
                total_cap = 1 if name == "total" else recovery.TOTAL_HOURLY_CAP
                before = self.digest()
                result = self.request(previous=self.binding(previous), total_cap=total_cap)
                self.assertIn(result.outcome, (Outcome.UNAVAILABLE, Outcome.ADDRESS_CAPPED))
                self.assertIsNone(result.recovery_id)
                self.assertUnchanged(before)
                self.assertEqual(self.row(previous.recovery_id)["state"], "pending")
                self.assertEqual(self.row(overdue.recovery_id)["state"], "pending")
        # Admitted, the same request runs the inline cleanup and voids this browser's previous row.
        self.conn.execute("DELETE FROM registration_control")
        self.conn.execute("DELETE FROM registration_sends WHERE recovery_id IS NULL")
        self.conn.commit()
        self.assertIs(self.request(previous=self.binding(previous)).outcome, Outcome.ISSUED)
        self.assertScrubbed(overdue.recovery_id, "expired")
        self.assertScrubbed(previous.recovery_id, "void")

    def test_rb1_the_address_must_already_be_the_public_stored_form(self):
        for email in ("Ana@Example.bg", " ana@example.bg", "ana@localhost", "ана@example.bg", "a b@example.bg",
                      "ana@example.bg\n", None, 5):
            with self.subTest(email=email):
                before = self.digest()
                with self.assertRaises(ValueError):
                    self.request(email)
                self.assertUnchanged(before)

    def test_a_secret_key_is_required(self):
        with self.assertRaises(ValueError):
            self.request(key="")


class ResendTest(RecoveryServiceTestCase):
    def test_rc4_limits_and_fresh_binding(self):
        self.verified()
        issued = self.request()
        self.assertIs(self.resend(issued).outcome, Outcome.TOO_EARLY)
        self.at(59)
        self.assertIs(self.resend(issued).outcome, Outcome.TOO_EARLY)
        results = []
        for seconds in (60, 120, 180):
            self.at(seconds)
            results.append(self.resend(issued))
        self.assertTrue(all(r.outcome is Outcome.ISSUED and r.token is None for r in results))
        self.at(240)
        self.assertIs(self.resend(issued).outcome, Outcome.RESEND_EXHAUSTED)
        row = self.row(issued.recovery_id)
        self.assertEqual((row["send_count"], row["code_send_id"]), (4, results[-1].send_id))
        # Only the newest code works.
        self.assertIs(self.redeem(issued, code_of(results[0])).outcome, Outcome.WRONG_CODE)
        self.assertIs(self.redeem(issued, code_of(results[-1])).outcome, Outcome.REDEEMED)

    def test_rc4_resend_resets_attempts_and_replaces_the_code(self):
        self.verified()
        issued = self.request()
        for _ in range(5):
            self.redeem(issued, "00000000")
        self.assertIs(self.redeem(issued, code_of(issued)).outcome, Outcome.TOO_MANY)
        self.at(60)
        again = self.resend(issued)
        self.assertEqual(self.row(issued.recovery_id)["code_attempts"], 0)
        self.assertIs(self.redeem(issued, code_of(again)).outcome, Outcome.REDEEMED)

    def test_rc4_a_password_change_between_request_and_resend_does_not_strand_the_owner(self):
        user_id = self.verified()
        issued = self.request()
        self.run_account(accounts.set_password, user_id, "another password 99")
        self.at(60)
        again = self.resend(issued)
        self.assertEqual(again.kind, "code")
        self.assertIs(self.redeem(issued, code_of(again)).outcome, Outcome.REDEEMED)
        self.assertTrue(self.authenticates(EMAIL, NEW_PASSWORD))

    def test_rc4_rd11_promotion_or_disable_turns_the_row_into_a_shadow(self):
        for change in ("promote", "disable"):
            with self.subTest(change):
                email = f"{change}@example.bg"
                user_id = self.verified(email)
                issued = self.request(email)
                if change == "promote":
                    self.run_account(accounts.set_role, user_id, "admin")
                else:
                    self.run_account(accounts.set_active, user_id, False)
                self.wall += timedelta(seconds=60)
                again = self.resend(issued)
                self.assertEqual(again.kind, "notice")
                row = self.row(issued.recovery_id)
                self.assertEqual((row["is_shadow"], row["user_id"], row["session_key_digest"]), (1, None, None))
                self.assertEqual(again.message.to, email)
                self.assertNotRegex(again.message.text, r"\d")
                self.wall += timedelta(seconds=1)
                self.assertIs(self.redeem(issued, code_of(issued)).outcome, Outcome.WRONG_CODE)

    def test_rc4_rd11_an_account_created_by_registration_turns_a_shadow_into_a_real_row(self):
        issued = self.request()
        self.assertEqual(issued.kind, "notice")
        user_id = self.verified()
        self.at(60)
        again = self.resend(issued)
        self.assertEqual(again.kind, "code")
        row = self.row(issued.recovery_id)
        self.assertEqual((row["is_shadow"], row["user_id"]), (0, user_id))
        self.assertIs(self.redeem(issued, code_of(again)).outcome, Outcome.REDEEMED)

    def test_resend_order_and_refusals_write_nothing(self):
        self.verified()
        issued = self.request()
        self.at(60)
        for name, arrange, expected in (
            ("hold", lambda: self.set_control(high_water=self.wall, hold="restore"), Outcome.UNAVAILABLE),
            ("clock", lambda: self.set_control(high_water=self.wall + timedelta(seconds=301)), Outcome.UNAVAILABLE),
            ("sender", lambda: setattr(self, "sender_busy", True), Outcome.UNAVAILABLE),
            ("circuit", lambda: [self.add_send(outcome="unknown") for _ in range(3)], Outcome.UNAVAILABLE),
            ("address", lambda: [self.add_send(email=EMAIL) for _ in range(4)], Outcome.ADDRESS_CAPPED),
        ):
            with self.subTest(name):
                self.conn.execute("DELETE FROM registration_control")
                self.conn.execute("DELETE FROM registration_sends WHERE recovery_id IS NULL")
                self.conn.commit()
                self.sender_busy = False
                arrange()
                before = self.digest()
                self.assertIs(self.resend(issued).outcome, expected)
                self.assertUnchanged(before)

    def test_resend_of_a_final_or_absent_row_writes_nothing(self):
        self.verified()
        issued = self.request()
        self.cancel(issued)
        before = self.digest()
        result = self.resend(issued)
        self.assertEqual((result.outcome, result.final_state), (Outcome.NOT_PENDING, "void"))
        self.assertUnchanged(before)
        self.assertIs(self.tx(recovery.resend, "0" * 64, secret_key=KEY, clock=self.clock,
                              sender_busy=False).outcome, Outcome.NO_PENDING)
        self.assertIs(self.tx(recovery.resend, "not a hash", secret_key=KEY, clock=self.clock,
                              sender_busy=False).outcome, Outcome.NO_PENDING)

    def test_resend_of_an_expired_row_expires_and_scrubs_it(self):
        self.verified()
        issued = self.request()
        self.at(30 * 60)
        self.assertIs(self.resend(issued).outcome, Outcome.EXPIRED)
        self.assertScrubbed(issued.recovery_id, "expired")


class InputRuleTest(RecoveryServiceTestCase):
    def test_rb2_eight_ascii_digits_trimmed_at_the_ends_only(self):
        self.verified()
        self.codes("00123456")
        issued = self.request()
        wrong = ["0012 3456", "001234567", "0012345", "123456", "００１２３４５６", "٠٠١٢٣٤٥٦", None, ["00123456"],
                 ""]
        for value in wrong[:5]:
            with self.subTest(value=value):
                self.assertIs(self.redeem(issued, value).outcome, Outcome.WRONG_CODE)
        self.assertIs(self.redeem(issued, "00123456").outcome, Outcome.TOO_MANY)
        self.assertEqual(self.row(issued.recovery_id)["code_attempts"], 5)
        self.at(60)
        self.codes("00123456")
        again = self.resend(issued)
        for value in wrong[5:]:
            with self.subTest(value=value):
                self.assertIs(self.redeem(issued, value).outcome, Outcome.WRONG_CODE)
        self.assertIs(self.redeem(issued, " \t00123456\n ").outcome, Outcome.REDEEMED)
        self.assertEqual(code_of(again), "00123456")

    def test_rb2_generation_keeps_leading_zeros(self):
        with patch.object(recovery.secrets, "randbelow", return_value=42) as randbelow:
            self.assertEqual(recovery._new_code(), "00000042")
        randbelow.assert_called_once_with(10**8)

    def test_rb2_registration_codes_stay_six_digits(self):
        self.assertRegex(registration._new_code(), r"^\d{6}$")
        self.assertRegex(recovery._new_code(), r"^\d{8}$")

    def test_a_malformed_password_is_refused_before_anything_is_read(self):
        self.verified()
        issued = self.request()
        for password in ("short", "x" * 257, None, b"bytes password 123"):
            with self.subTest(password=password):
                before = self.digest()
                with self.assertRaises(ValueError):
                    self.redeem(issued, code_of(issued), password)
                self.assertUnchanged(before)


class IssuanceIdentityTest(RecoveryServiceTestCase):
    def test_ri1_rolled_back_result_matches_nothing_after_id_reuse(self):
        self.verified()
        self.codes("12345678", "12345678")
        self.conn.execute("BEGIN IMMEDIATE")
        stale = recovery.request(self.conn, email=EMAIL, previous_binding=None, secret_key=KEY,
                                 clock=self.clock, sender_busy=False)
        self.conn.rollback()
        fresh = self.request()
        self.assertEqual((stale.recovery_id, stale.send_id), (fresh.recovery_id, fresh.send_id))
        self.assertEqual(stale.message, fresh.message)
        mailer = InMemoryMailer()
        with self.assertRaises(recovery.NotSendable):
            self.dispatch(mailer, stale)
        self.assertEqual(mailer.messages, [])
        self.assertIs(self.settle(stale, SendOutcome.ACCEPTED), recovery.Settlement.NOT_THIS_ISSUANCE)
        self.assertEqual(self.send(fresh.send_id)["outcome"], "started")
        self.assertIs(self.dispatch(mailer, fresh), SendOutcome.ACCEPTED)

    def test_ri2_dispatch_refuses_everything_but_the_committed_current_issuance(self):
        user_id = self.verified()
        issued = self.request()
        mailer = InMemoryMailer()

        def refused(result, conn=None):
            with self.assertRaises(recovery.NotSendable):
                self.dispatch(mailer, result, conn=conn)

        self.conn.execute("BEGIN")
        refused(issued)
        self.conn.rollback()
        replace = issued.__class__
        fields = dict(outcome=issued.outcome, recovery_id=issued.recovery_id, send_id=issued.send_id,
                      kind=issued.kind, issuance_id=issued.issuance_id, token=issued.token, message=issued.message)
        variants = {
            "identity": dict(issuance_id="0" * 32),
            "no identity": dict(issuance_id=None),
            "kind": dict(kind="notice"),
            "recipient": dict(message=OutboundEmail(to="other@example.bg", subject=issued.message.subject,
                                                    text=issued.message.text)),
            "subject": dict(message=OutboundEmail(to=EMAIL, subject="Друго", text=issued.message.text)),
            "body": dict(message=OutboundEmail(to=EMAIL, subject=issued.message.subject,
                                               text=issued.message.text + "още")),
            "digits": dict(message=recovery._code_message(EMAIL, f"{(int(code_of(issued)) + 1) % 10**8:08d}")),
            "token": dict(token="x" * 43),
            "recovery": dict(recovery_id=issued.recovery_id + 1),
            "send": dict(send_id=issued.send_id + 1),
            "outcome": dict(outcome=Outcome.REDEEMED),
        }
        for name, change in variants.items():
            with self.subTest(name):
                refused(replace(**{**fields, **change}))
        # A registration result, ledger row or purpose-less row is never sent.
        refused(registration.Result(registration.Outcome.ISSUED, pending_id=issued.recovery_id,
                                    send_id=issued.send_id, kind="code", issuance_id=issued.issuance_id,
                                    message=issued.message))
        purposeless = self.add_send(email=EMAIL)
        identity = self.send(purposeless)["issuance_id"]
        refused(replace(**{**fields, "send_id": purposeless, "issuance_id": identity}))
        # Another key: the address hash and the code hash no longer match.
        with self.assertRaises(recovery.NotSendable):
            recovery.dispatch(self.conn, mailer, issued, secret_key="another key")
        # The account's users.email changed (A4 at send time).
        self.tx(lambda conn: conn.execute("UPDATE users SET email = 'moved@example.bg' WHERE id = ?", (user_id,)))
        refused(issued)
        self.tx(lambda conn: conn.execute("UPDATE users SET email = ? WHERE id = ?", (EMAIL, user_id)))
        self.assertEqual(mailer.messages, [])
        # An older generation after a resend, a settled generation, a final row.
        self.at(60)
        newer = self.resend(issued)
        refused(issued)
        self.assertIs(self.dispatch(mailer, newer), SendOutcome.ACCEPTED)
        self.settle(newer, SendOutcome.ACCEPTED)
        refused(newer)
        self.at(120)
        newest = self.resend(issued)
        self.cancel(issued)
        refused(newest)
        self.assertEqual(len(mailer.messages), 1)

    def test_ri2_a_first_issuance_token_must_hash_to_the_binding(self):
        self.verified()
        first = self.request()
        other = self.request("unknown@example.bg")
        swapped = first.__class__(outcome=first.outcome, recovery_id=first.recovery_id, send_id=first.send_id,
                                  kind=first.kind, issuance_id=first.issuance_id, token=other.token,
                                  message=first.message)
        with self.assertRaises(recovery.NotSendable):
            self.dispatch(InMemoryMailer(), swapped)
        # A resend result carries no token and is checked without one.
        self.at(60)
        again = self.resend(first)
        self.assertIsNone(again.token)
        self.assertIs(self.dispatch(InMemoryMailer(), again), SendOutcome.ACCEPTED)

    def test_ri3_one_mailer_call_per_dispatch_and_exceptions_are_unknown(self):
        self.verified()
        issued = self.request()

        class Exploding:
            calls = 0

            def send(self, message):
                Exploding.calls += 1
                raise ConnectionError("relay down")

        self.assertIs(self.dispatch(Exploding(), issued), SendOutcome.UNKNOWN)
        self.assertEqual(Exploding.calls, 1)
        weird = InMemoryMailer(lambda message: "accepted")
        self.assertIs(self.dispatch(weird, issued), SendOutcome.UNKNOWN)
        # No retry and no claim: until settled a second call sends again (caller obligation).
        twice = InMemoryMailer()
        self.dispatch(twice, issued)
        self.dispatch(twice, issued)
        self.assertEqual(len(twice.messages), 2)
        with self.assertRaises(KeyboardInterrupt):
            self.dispatch(InMemoryMailer(lambda message: (_ for _ in ()).throw(KeyboardInterrupt())), issued)

    def test_ri4_a_ledger_row_without_identity_is_neither_sent_nor_settled(self):
        self.verified()
        issued = self.request()
        # Pre-022 shape: the identity column NULL (direct SQL; no production path writes it).
        self.tx(lambda conn: conn.execute("UPDATE registration_sends SET issuance_id = NULL WHERE id = ?",
                                          (issued.send_id,)))
        with self.assertRaises(recovery.NotSendable):
            self.dispatch(InMemoryMailer(), issued)
        self.assertIs(self.settle(issued, SendOutcome.ACCEPTED), recovery.Settlement.NOT_THIS_ISSUANCE)
        # And its code is still checked against the ledger row, which is intact otherwise.
        self.assertEqual(self.send(issued.send_id)["outcome"], "started")


class TransactionContractTest(RecoveryServiceTestCase):
    def test_rf5_every_writer_needs_a_transaction_and_never_ends_it(self):
        self.verified()
        issued = self.request()
        binding = self.binding(issued)
        calls = {
            "request": lambda conn: recovery.request(conn, email=EMAIL, previous_binding=None, secret_key=KEY,
                                                     clock=self.clock, sender_busy=False),
            "resend": lambda conn: recovery.resend(conn, binding, secret_key=KEY, clock=self.clock,
                                                   sender_busy=False),
            "redeem": lambda conn: recovery.redeem(conn, binding, "00000000", NEW_PASSWORD, secret_key=KEY,
                                                   clock=self.clock),
            "cancel": lambda conn: recovery.cancel(conn, binding, clock=self.clock),
            "settle_send": lambda conn: recovery.settle_send(conn, issued.send_id, issued.issuance_id,
                                                             SendOutcome.ACCEPTED, clock=self.clock),
        }
        for name, call in calls.items():
            with self.subTest(name):
                before = self.digest()
                with self.assertRaises(registration.TransactionRequired):
                    call(self.conn)
                self.assertUnchanged(before)
                self.conn.execute("BEGIN IMMEDIATE")
                call(self.conn)
                self.assertTrue(self.conn.in_transaction)
                self.conn.rollback()
                self.assertEqual(self.digest(), before)

    def test_writers_read_the_clock_once_after_the_lock(self):
        self.verified()
        reads = []

        def clock():
            reads.append(self.conn.in_transaction)
            return self.wall

        issued = self.request(clock=clock)
        self.at(60)
        self.resend(issued, clock=clock)
        self.redeem(issued, "00000000", clock=clock)
        self.cancel(issued, clock=clock)
        self.assertEqual(reads, [True] * 4)

    def test_rf3_an_exception_in_request_or_resend_leaves_nothing(self):
        self.verified()
        issued = self.request()
        self.at(60)
        for target in ("recovery_candidate", "raise_mark"):
            for call in ("request", "resend"):
                with self.subTest(target=target, call=call):
                    before = self.digest()
                    module = accounts if target == "recovery_candidate" else registration
                    with patch.object(module, target, side_effect=sqlite3.OperationalError("disk I/O error")):
                        with self.assertRaises(sqlite3.OperationalError):
                            self.request() if call == "request" else self.resend(issued)
                    self.assertUnchanged(before)

    def test_rf3_a_failed_commit_that_took_effect_leaves_a_counted_started_row_and_sends_nothing(self):
        self.verified()
        self.conn.execute("BEGIN IMMEDIATE")
        issued = recovery.request(self.conn, email=EMAIL, previous_binding=None, secret_key=KEY,
                                  clock=self.clock, sender_busy=False)
        self.conn.commit()  # the commit took effect, but the caller saw it raise: it acts on nothing
        send = self.send(issued.send_id)
        self.assertEqual(send["outcome"], "started")
        # The unsent issuance is counted like any other: four more sends cap the address.
        for _ in range(4):
            self.add_send(email=EMAIL)
        self.assertTrue(registration.address_capped(self.conn, send["email_hash"], self.wall))
        self.assertIs(self.request().outcome, Outcome.ADDRESS_CAPPED)


class LoggingTest(RecoveryServiceTestCase):
    def capture(self):
        records = []

        class Keep(logging.Handler):
            def emit(self, record):
                records.append(record.getMessage())

        handler = Keep(level=logging.DEBUG)
        for name in ("learnpilot.recovery", "learnpilot.registration", "learnpilot.mail"):
            log = logging.getLogger(name)
            log.addHandler(handler)
            previous = log.level
            log.setLevel(logging.DEBUG)
            self.addCleanup(log.setLevel, previous)
            self.addCleanup(log.removeHandler, handler)
        return records

    def journey(self, email):
        issued = self.request(email)
        self.deliver(issued)
        self.redeem(issued, "00000000")
        self.wall += timedelta(seconds=60)
        again = self.resend(issued)
        self.deliver(again, SendOutcome.NOT_SENT)
        return issued, again

    def test_rlog1_no_secret_in_any_line_of_a_full_journey(self):
        user_id = self.verified()
        records = self.capture()
        issued, again = self.journey(EMAIL)
        self.redeem(issued, code_of(again))
        second = self.request()
        self.cancel(second)
        user = self.user(user_id)
        secrets_ = [EMAIL, code_of(issued), code_of(again), issued.token, issued.issuance_id, again.issuance_id,
                    NEW_PASSWORD, NEW_PASSWORD.strip(), user["password_hash"], user["session_key"],
                    recovery.binding_hash(KEY, issued.token), registration.email_hash(KEY, EMAIL),
                    issued.message.text, again.message.text]
        self.assertTrue(records)
        for line in records:
            for secret in secrets_:
                self.assertNotIn(secret, line)
            self.assertNotIn("shadow", line.lower())
        self.assertTrue(any(line.startswith("redeemed_decided") and "user=" in line for line in records))
        self.assertEqual([line for line in records if "user=" in line and not line.startswith("redeemed")], [])

    def test_rlog2_the_event_sequence_is_the_same_for_a_real_and_a_shadow_journey(self):
        self.verified()
        records = self.capture()
        self.journey(EMAIL)
        real = [line.split()[0] for line in records]
        records.clear()
        self.wall = T0 + timedelta(hours=2)
        self.journey("unknown@example.bg")
        shadow = [line.split()[0] for line in records]
        self.assertEqual(real, shadow)
        self.assertIn("send_started", real)
        self.assertIn("wrong_code_decided", real)

    def test_rlog3_repr_omits_token_message_address_identity_and_kind(self):
        self.verified()
        issued = self.request()
        view = self.view(issued)
        for text in (repr(issued), str(issued), repr(view)):
            for secret in (issued.token, issued.issuance_id, EMAIL, code_of(issued), "code", "notice"):
                self.assertNotIn(secret, text)


class StatementSequenceTest(RecoveryServiceTestCase):
    def test_request_and_resend_run_the_same_statements_for_real_and_shadow_rows(self):
        self.verified()
        with self.statements() as real:
            first = self.request()
        with self.statements() as shadow:
            other = self.request("unknown@example.bg")
        self.assertEqual(normalized(real), normalized(shadow))
        self.at(60)
        with self.statements() as real:
            self.resend(first)
        with self.statements() as shadow:
            self.resend(other)
        self.assertEqual(normalized(real), normalized(shadow))
        self.assertTrue(any("FROM users" in sql for sql in real))  # resend decides eligibility (RD11)

    def test_dispatch_runs_the_same_statements_for_real_and_shadow_rows(self):
        self.verified()
        first = self.request()
        other = self.request("unknown@example.bg")
        with self.statements() as real:
            self.dispatch(InMemoryMailer(), first)
        with self.statements() as shadow:
            self.dispatch(InMemoryMailer(), other)
        self.assertEqual(normalized(real), normalized(shadow))


if __name__ == "__main__":
    import unittest
    unittest.main()
