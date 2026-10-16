"""Registration service (EMAIL_REGISTRATION PR 3): pending lifecycle, codes, activation,
collisions, the clock policy and cleanup. Test-plan sections D, D-A, D-C, E and N.

Ledger and caps: ``test_registration_ledger``. Settlement and delivery:
``test_registration_settlement``. Fixture: ``tests.registration_support``.
"""

import logging
import sqlite3
import subprocess
import sys
import threading
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from tests.registration_support import (
    EMAIL, KEY, PASSWORD, REGISTRATION_TABLES, T0, RegistrationTestCase, code_of, password_hash, stamp,
    student, teacher,
)
from tests.support import ROOT
from web.services import accounts, oidc_accounts, profiles, registration
from web.services.registration import Availability, Outcome

W = int(registration.DAILY_WINDOW.total_seconds())


def wrong(code: str) -> str:
    return f"{(int(code) + 1) % 10**6:06d}"


class AccountsAdditionsTest(RegistrationTestCase):
    """D10 and the exact-password half of B8."""

    def create(self, **overrides):
        values = {"email": EMAIL, "display_name": "Ана Иванова", "role": "student",
                  "password_hash": password_hash(), "verified_at": T0}
        values.update(overrides)
        return accounts.create_verified_user(self.conn, **values)

    def test_creates_a_verified_student(self):
        user_id = self.create()
        row = self.rows("SELECT * FROM users WHERE id = ?", user_id)[0]
        self.assertEqual((row["email"], row["role"], row["is_active"]), (EMAIL, "student", 1))
        self.assertEqual(row["email_verified_at"], stamp(T0))
        self.assertEqual(row["password_changed_at"], stamp(T0))
        self.assertEqual(row["password_hash"], password_hash())
        self.assertTrue(row["session_key"])
        self.assertTrue(self.conn.in_transaction)  # never commits

    def test_refuses_every_role_but_student_and_teacher(self):
        # A verified registration can end in these two roles only (PR 7); never admin.
        for role in ("admin", "", None, "Student", "student ", "Teacher", "tester"):
            with self.subTest(role=role), self.assertRaises(ValueError):
                self.create(role=role)
        self.assertEqual(self.count("users"), 0)
        self.assertEqual(accounts.VERIFIED_REGISTRATION_ROLES, ("student", "teacher"))

    def test_refuses_a_missing_or_unnormalised_email(self):
        for email in (None, "", "Ana@Example.bg", " ana@example.bg", "no-at", 7):
            with self.subTest(email=email), self.assertRaises(ValueError):
                self.create(email=email)
        self.assertEqual(self.count("users"), 0)

    def test_refuses_a_hash_that_is_not_of_the_current_method(self):
        from werkzeug.security import generate_password_hash

        old = generate_password_hash("a-long-enough-password", method="pbkdf2:sha256:1000")
        weaker = generate_password_hash("a-long-enough-password", method="scrypt:16384:8:1")
        for bad in (None, "", "plain-password", old, weaker, password_hash()[:-2], PASSWORD):
            with self.subTest(), self.assertRaises(ValueError) as caught:
                self.create(password_hash=bad)
            self.assertNotIn(str(bad or "-"), str(caught.exception))
        self.assertEqual(self.count("users"), 0)

    def test_refuses_a_naive_time_and_a_bad_name(self):
        with self.assertRaises(ValueError):
            self.create(verified_at=T0.replace(tzinfo=None))
        with self.assertRaises(ValueError):
            self.create(display_name="x")

    def test_duplicate_email_is_reported_as_none_and_writes_nothing(self):
        first = self.create()
        before = self.users()
        self.assertIsNone(self.create())
        self.assertIsNone(self.create(display_name="Друг Човек"))
        self.assertEqual(self.users(), before)
        self.assertIsInstance(first, int)
        # A refused insert may use up an id (AUTOINCREMENT): a gap, never a reused or changed id.
        self.assertGreater(self.create(email="other@example.bg"), first)
        self.assertEqual(self.users()[0], before[0])

    def test_only_the_uniqueness_of_the_email_is_reported_as_none(self):
        """Every other integrity failure raises, with the address present or not."""
        self.create()
        self.conn.commit()
        short_key = patch.object(accounts, "_new_session_key", return_value="short")
        for email in (EMAIL, "new@example.bg"):
            with self.subTest(failure="check", email=email), short_key:
                with self.assertRaises(sqlite3.IntegrityError) as caught:
                    self.create(email=email)
                self.assertEqual(caught.exception.sqlite_errorcode, sqlite3.SQLITE_CONSTRAINT_CHECK)
        self.conn.execute("CREATE UNIQUE INDEX test_users_session_key ON users(session_key)")
        taken_key = self.conn.execute("SELECT session_key FROM users").fetchone()[0]
        with patch.object(accounts, "_new_session_key", return_value=taken_key):
            with self.assertRaises(sqlite3.IntegrityError) as caught:
                self.create(email="new@example.bg")
            self.assertEqual(caught.exception.sqlite_errorcode, sqlite3.SQLITE_CONSTRAINT_UNIQUE)
            self.assertIn("users.session_key", str(caught.exception))
        self.conn.execute("DROP INDEX test_users_session_key")
        self.conn.execute(
            "CREATE TRIGGER test_users_refuse BEFORE INSERT ON users BEGIN "
            "SELECT RAISE(ABORT, 'UNIQUE constraint failed: users.email'); END")
        for email in (EMAIL, "new@example.bg"):
            with self.subTest(failure="trigger", email=email):
                with self.assertRaises(sqlite3.IntegrityError) as caught:
                    self.create(email=email)
                self.assertEqual(caught.exception.sqlite_errorcode, sqlite3.SQLITE_CONSTRAINT_TRIGGER)
                self.assertEqual(caught.exception.sqlite_errorcode, 1811)
                self.assertEqual(str(caught.exception), "UNIQUE constraint failed: users.email")
        self.assertEqual(self.count("users"), 1)

    def test_statement_names_the_email_conflict_and_nothing_else(self):
        statements = []
        self.conn.set_trace_callback(statements.append)
        self.create()
        self.conn.set_trace_callback(None)
        inserts = [text for text in statements if text.startswith("INSERT INTO users")]
        self.assertEqual(len(inserts), 1)
        self.assertTrue(inserts[0].endswith("ON CONFLICT(email) DO NOTHING"))
        self.assertNotIn("OR IGNORE", inserts[0])
        self.assertGreaterEqual(sqlite3.sqlite_version_info, (3, 24, 0))

    def test_hash_new_password_hashes_the_exact_string(self):
        from werkzeug.security import check_password_hash

        for password in (PASSWORD, " leading-space", "trailing-space ", "inner  double", "tab\tinside!",
                         "ｆullwidth-pass", "x" * 256):
            with self.subTest(password=password[:12]):
                hashed = accounts.hash_new_password(password, EMAIL)
                self.assertTrue(accounts.password_hash_is_current(hashed))
                self.assertTrue(check_password_hash(hashed, password))
                self.assertFalse(check_password_hash(hashed, password.strip() + "!"))
        for bad in ("short", "x" * 257, EMAIL):
            with self.assertRaises(ValueError):
                accounts.hash_new_password(bad, EMAIL)
        with self.assertRaises(ValueError):
            accounts.hash_new_password(PASSWORD, "not an email")

    def test_existing_account_readers_are_unchanged(self):
        self.assertNotIn("email_verified_at", accounts._USER_COLUMNS)
        user_id = self.create()
        self.assertNotIn("email_verified_at", accounts.get_user_by_id(self.conn, user_id))


class PendingTest(RegistrationTestCase):
    """D1, D2, D9 and the "no privilege, no reservation" rules of design 2.2 and 2.4."""

    def test_stores_hashes_and_never_the_password_or_the_code(self):
        issued = self.submit()
        code = code_of(issued)
        row = self.pending(issued.pending_id)
        self.assertEqual(row["password_hash"], password_hash())
        self.assertEqual(row["code_hash"], registration._code_hash(KEY, issued.pending_id, code.encode()))
        self.assertEqual(row["binding_hash"], registration.binding_hash(KEY, issued.token))
        self.assertEqual((row["state"], row["is_shadow"], row["send_count"], row["code_attempts"]),
                         ("pending", 0, 1, 0))
        self.assertEqual(row["created_at"], stamp(T0))
        self.assertEqual(row["expires_at"], stamp(T0 + timedelta(minutes=30)))
        self.assertEqual(row["code_expires_at"], stamp(T0 + timedelta(minutes=10)))
        self.assertEqual(row["resend_after"], stamp(T0 + timedelta(seconds=60)))
        self.assertEqual(row["code_send_id"], issued.send_id)
        tables = [name for (name,) in self.conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")]
        for table in tables:
            for stored in self.conn.execute(f'SELECT * FROM "{table}"'):
                for value in stored:
                    if isinstance(value, str):
                        self.assertNotIn(PASSWORD, value, table)
                        self.assertNotEqual(value, code, table)
                        self.assertNotIn(issued.token, value, table)
        self.assertNotIn(EMAIL, {send["email_hash"] for send in self.sends()})

    def test_result_and_log_lines_show_no_secret(self):
        with self.assertLogs("learnpilot.registration", level=logging.DEBUG) as logs:
            issued = self.submit()
            code = code_of(issued)
            self.verify(issued, wrong(code))
            self.at(61)
            again = self.resend(issued)
            self.verify(issued, code_of(again))
        row_hashes = {registration.binding_hash(KEY, issued.token), password_hash()}
        shown = repr(issued) + repr(again) + "\n".join(logs.output)
        for secret in (issued.token, code, code_of(again), EMAIL, "Ана", "Иванова", PASSWORD, KEY, *row_hashes):
            self.assertNotIn(secret, shown)
        self.assertIn("activation_decided", shown)
        self.assertNotRegex(shown, r"\b(activated|submitted|resent)\b")  # a decision, not a durable fact

    def test_codes_are_six_digits_and_come_from_secrets(self):
        codes = {registration._new_code() for _ in range(1000)}
        self.assertGreater(len(codes), 900)
        self.assertTrue(all(len(code) == 6 and code.isascii() and code.isdigit() for code in codes))
        with patch.object(registration.secrets, "randbelow", return_value=7) as randbelow:
            self.assertEqual(registration._new_code(), "000007")
        randbelow.assert_called_once_with(10**6)
        issued = self.submit()
        with patch.object(registration.secrets, "randbelow", return_value=42):
            self.at(61)
            self.assertEqual(code_of(self.resend(issued)), "000042")

    def test_every_issuance_has_its_own_random_identity(self):
        """32 lower-case hexadecimal characters from ``secrets``; new for every issuance and
        resend; stored with the ledger row; derived from nothing."""
        self.codes("111111", "111111", "111111", "111111")
        with patch.object(registration.secrets, "token_hex", wraps=registration.secrets.token_hex) as token_hex:
            first = self.submit()
            self.at(61)
            resent = self.resend(first)
            # The same address, digits and second as another issuance: still another identity.
            other = self.submit()
            again = self.submit()
        self.assertEqual([call.args for call in token_hex.call_args_list], [(16,)] * 4)
        issued = (first, resent, other, again)
        identities = [result.issuance_id for result in issued]
        self.assertEqual(len(set(identities)), 4)
        for result in issued:
            self.assertRegex(result.issuance_id, r"\A[0-9a-f]{32}\Z")
            self.assertEqual(self.send(result.send_id)["issuance_id"], result.issuance_id)
            self.assertNotIn(result.issuance_id, repr(result))
        self.assertEqual(other.message, again.message)  # everything a message holds coincides

    def test_identity_that_starts_with_the_code_digits_is_valid(self):
        """Random hexadecimal may contain any digits, the code's among them; that is no defect."""
        self.codes("111111")
        with patch.object(registration.secrets, "token_hex", return_value="111111" + "a" * 26):
            issued = self.submit()
        self.assertEqual(issued.issuance_id, "111111" + "a" * 26)
        self.assertRegex(issued.issuance_id, r"\A[0-9a-f]{32}\Z")
        self.assertEqual(self.send(issued.send_id)["issuance_id"], issued.issuance_id)

    def test_refused_issuance_creates_no_row_and_no_identity(self):
        for _ in range(5):
            self.submit()
        before = self.digest()
        with patch.object(registration, "_new_issuance_id", side_effect=AssertionError("identity made")):
            capped = self.submit()
            unavailable = self.submit("other@example.bg", total_cap=5)
            busy = self.submit("other@example.bg", sender_busy=True)
        for refused in (capped, unavailable, busy):
            self.assertNotEqual(refused.outcome, Outcome.ISSUED)
            self.assertEqual((refused.send_id, refused.issuance_id, refused.message, refused.token),
                             (None, None, None, None))
        self.assertUnchanged(before)

    def test_repeated_identity_is_refused_by_the_database(self):
        """Impossible rather than unlikely: the unique index refuses it and the caller rolls back."""
        first = self.submit()
        before = self.digest()
        with patch.object(registration, "_new_issuance_id", return_value=first.issuance_id):
            with self.assertRaises(sqlite3.IntegrityError):
                self.submit("other@example.bg")
            self.at(61)
            with self.assertRaises(sqlite3.IntegrityError):
                self.resend(first)
        self.assertUnchanged(before)

    def test_pending_grants_nothing_and_reserves_nothing(self):
        first = self.submit()
        second = self.submit()  # the same address again: no uniqueness, no reservation
        self.assertEqual(second.outcome, Outcome.ISSUED)
        self.assertNotEqual(first.pending_id, second.pending_id)
        self.assertEqual(self.count("users"), 0)
        self.assertEqual(self.count("user_profiles"), 0)
        self.assertIsNone(accounts.get_user_by_email(self.conn, EMAIL))
        self.assertIsNone(accounts.authenticate(self.conn, EMAIL, PASSWORD))
        self.assertFalse(self.conn.in_transaction)

    def test_code_of_one_registration_does_not_verify_another(self):
        self.codes("111111", "222222")
        first, second = self.submit(), self.submit()
        self.assertEqual(self.verify(second, code_of(first)).outcome, Outcome.WRONG_CODE)
        self.assertEqual(self.verify(first, code_of(second)).outcome, Outcome.WRONG_CODE)
        self.assertEqual(self.verify(first, code_of(first)).outcome, Outcome.ACTIVATED)

    def test_another_browser_has_no_registration(self):
        issued = self.submit()
        before = self.digest()
        for binding in (registration.binding_hash(KEY, "another-token"), None, "", "x" * 64, 7, "A" * 64):
            with self.subTest(binding=binding):
                result = self.tx(registration.verify, binding, code_of(issued), secret_key=KEY, clock=self.clock)
                self.assertEqual(result.outcome, Outcome.NO_PENDING)
        self.assertUnchanged(before)

    def test_new_submission_voids_this_browsers_previous_row(self):
        first = self.submit()
        second = self.submit("other@example.bg", previous=self.binding(first))
        self.assertScrubbed(first.pending_id, "void")
        self.assertEqual(self.verify(first, code_of(first)).outcome, Outcome.NOT_PENDING)
        self.assertEqual(self.verify(second, code_of(second)).outcome, Outcome.ACTIVATED)
        # A final previous row is left exactly as it is.
        third = self.submit("third@example.bg", previous=self.binding(second))
        self.assertEqual(third.outcome, Outcome.ISSUED)
        self.assertEqual(self.pending(second.pending_id)["state"], "activated")

    def test_input_is_refused_never_repaired(self):
        bad = [
            student(email="Ana@Example.bg"), student(email="ana@localhost"), student(first_name=" Ана"),
            student(last_name=""), student(last_name=None), student(grade=7), student(grade="9"),
            student(grade=True), student(account_type="teacher"), teacher(school_name="x"),
            teacher(subjects=[]), teacher(subjects=[{"subject_id": "it"}]), teacher(subjects="it"),
        ]
        def create(cleaned, account_type):
            return self.tx(registration.create_pending, cleaned=cleaned, account_type=account_type,
                           password_hash=password_hash(), previous_binding=None, secret_key=KEY,
                           clock=self.clock, sender_busy=False, teacher_enabled=True)

        for cleaned in bad:
            with self.subTest(cleaned=sorted(cleaned)), self.assertRaises(ValueError) as caught:
                create(cleaned, "teacher" if "school_name" in cleaned else "student")
            self.assertNotIn("example", str(caught.exception))
        for account_type in ("admin", "", None):
            with self.assertRaises(ValueError):
                create(student(), account_type)
        with self.assertRaises(ValueError):
            self.tx(registration.create_pending, cleaned=student(), account_type="student",
                    password_hash="not-a-hash", previous_binding=None, secret_key=KEY, clock=self.clock,
                    sender_busy=False)
        for key in ("", None, b""):
            with self.assertRaises(ValueError):
                self.submit(key=key)
        self.assertEqual(self.count("pending_registrations"), 0)
        self.assertEqual(self.count("registration_sends"), 0)

    def test_writers_need_the_callers_transaction_and_never_end_it(self):
        binding = registration.binding_hash(KEY, "t")
        calls = {
            "create_pending": lambda: registration.create_pending(
                self.conn, cleaned=student(), account_type="student", password_hash=password_hash(),
                previous_binding=None, secret_key=KEY, clock=self.clock, sender_busy=False),
            "resend": lambda: registration.resend(self.conn, binding, secret_key=KEY, clock=self.clock,
                                                  sender_busy=False),
            "verify": lambda: registration.verify(self.conn, binding, "123456", secret_key=KEY, clock=self.clock),
            "cancel": lambda: registration.cancel(self.conn, binding, clock=self.clock),
            "settle_send": lambda: registration.settle_send(self.conn, 1, "ab" * 16, "accepted", clock=self.clock),
            "cleanup": lambda: registration.cleanup(self.conn, clock=self.clock),
        }
        before = self.digest()
        for name, call in calls.items():
            with self.subTest(name=name):
                with self.assertRaises(registration.TransactionRequired):
                    call()
                self.assertFalse(self.conn.in_transaction)
                self.conn.execute("BEGIN IMMEDIATE")
                call()
                self.assertTrue(self.conn.in_transaction)  # DA6: left open for the caller
                self.conn.rollback()
        self.assertUnchanged(before)

    def test_clock_must_be_an_aware_callable(self):
        self.conn.execute("BEGIN IMMEDIATE")
        with self.assertRaises(TypeError):
            registration.cleanup(self.conn, clock=T0)
        with self.assertRaises(ValueError):
            registration.cleanup(self.conn, clock=lambda: T0.replace(tzinfo=None))
        self.conn.rollback()


class VerificationTest(RegistrationTestCase):
    """D3 to D8, B8 after activation."""

    def test_correct_code_activates_a_student(self):
        issued = self.submit(cleaned=student(first_name="Ана-Мария", last_name="О’Нийл", grade=12))
        self.at(120)
        result = self.verify(issued, f"  {code_of(issued)}\r\n")  # the ends are trimmed, nothing else
        self.assertEqual(result.outcome, Outcome.ACTIVATED)
        user = self.users()[0]
        self.assertEqual(user["id"], result.user_id)
        self.assertEqual((user["email"], user["role"], user["is_active"]), (EMAIL, "student", 1))
        self.assertEqual(user["display_name"], "Ана-Мария О’Нийл")
        self.assertEqual(user["email_verified_at"], stamp(self.wall))
        self.assertIsNone(user["last_login_at"])  # nobody was signed in
        profile = profiles.get_profile(self.conn, user["id"])
        self.assertEqual((profile["account_type"], profile["first_name"], profile["last_name"], profile["grade"]),
                         ("student", "Ана-Мария", "О’Нийл", 12))
        self.assertEqual(profile["grade_set_at"], stamp(self.wall))
        self.assertEqual(self.count("teacher_subjects"), 0)
        self.assertScrubbed(issued.pending_id, "activated")
        self.assertEqual(accounts.authenticate(self.conn, EMAIL, PASSWORD)["id"], user["id"])
        self.conn.rollback()
        for variant in (PASSWORD.strip(), " ".join(PASSWORD.split()), PASSWORD + " ", PASSWORD.upper()):
            self.assertIsNone(accounts.authenticate(self.conn, EMAIL, variant))
        self.conn.rollback()

    def test_final_states_are_kept_and_nothing_is_written(self):
        activated = self.submit("a@example.bg")
        self.verify(activated, code_of(activated))
        cancelled = self.submit("b@example.bg")
        self.cancel(cancelled)
        replaced = self.submit("c@example.bg")
        self.submit("d@example.bg", previous=self.binding(replaced))
        taken = self.submit("a@example.bg")
        self.assertEqual(self.verify(taken, wrong(code_of(activated))).outcome, Outcome.WRONG_CODE)
        self.assertEqual(self.pending(taken.pending_id)["is_shadow"], 1)
        expired = self.submit("e@example.bg")
        self.at(1800)
        self.assertEqual(self.verify(expired, code_of(expired)).outcome, Outcome.EXPIRED)
        self.assertScrubbed(expired.pending_id, "expired")
        states = {activated: "activated", cancelled: "void", replaced: "void", expired: "expired"}
        before = self.digest()
        for issued, state in states.items():
            for call in (lambda: self.verify(issued, "123456"), lambda: self.resend(issued),
                         lambda: self.cancel(issued)):
                result = call()
                self.assertEqual(result.outcome, Outcome.NOT_PENDING)
                # The caller can tell a finished registration from one that ended without an account.
                self.assertEqual(result.final_state, state)
            self.assertScrubbed(issued.pending_id, state)
        self.assertUnchanged(before)

    def test_final_state_accompanies_not_pending_and_nothing_else(self):
        issued = self.submit()
        self.assertIsNone(self.verify(issued, "000000").final_state)  # wrong code, still pending
        other = registration.binding_hash(KEY, "another-token")
        absent = self.tx(registration.verify, other, "000000", secret_key=KEY, clock=self.clock)
        self.assertEqual((absent.outcome, absent.final_state), (Outcome.NO_PENDING, None))
        self.at(1800)
        expiring = self.verify(issued, code_of(issued))  # this call closes it: EXPIRED, not NOT_PENDING
        self.assertEqual((expiring.outcome, expiring.final_state), (Outcome.EXPIRED, None))
        again = self.verify(issued, code_of(issued))
        self.assertEqual((again.outcome, again.final_state), (Outcome.NOT_PENDING, "expired"))

    def test_five_wrong_codes_then_the_right_one_is_refused_until_a_resend(self):
        issued = self.submit()
        code = code_of(issued)
        for attempt in range(1, 6):
            self.assertEqual(self.verify(issued, wrong(code)).outcome, Outcome.WRONG_CODE)
            self.assertEqual(self.pending(issued.pending_id)["code_attempts"], attempt)
        self.assertEqual(self.verify(issued, code).outcome, Outcome.TOO_MANY)
        self.assertEqual(self.pending(issued.pending_id)["code_attempts"], 5)
        self.at(61)
        again = self.resend(issued)
        self.assertEqual(self.pending(issued.pending_id)["code_attempts"], 0)
        self.assertEqual(self.verify(issued, code).outcome,
                         Outcome.ACTIVATED if code == code_of(again) else Outcome.WRONG_CODE)
        if code != code_of(again):
            self.assertEqual(self.verify(issued, code_of(again)).outcome, Outcome.ACTIVATED)

    def test_malformed_and_missing_codes_count(self):
        issued = self.submit()
        for index, bad in enumerate(("", None, "12345", "1234567", "１２３４５６", ["123456"]), start=1):
            if index > 5:
                self.assertEqual(self.verify(issued, bad).outcome, Outcome.TOO_MANY)
                break
            self.assertEqual(self.verify(issued, bad).outcome, Outcome.WRONG_CODE)
            self.assertEqual(self.pending(issued.pending_id)["code_attempts"], index)

    def test_exact_code_input_policy(self):
        """Approved: trim leading and trailing whitespace only, then exactly six ASCII digits."""
        accepted = ("123456", " 123456", "123456 ", "\t123456\n", "\r\n 123456 \r\n", "000000", "\u00a0123456\u2003")
        for value in accepted:
            with self.subTest(accepted=value):
                self.assertEqual(registration._submitted_code(value), value.strip())
                self.assertEqual(len(value.strip()), 6)
        rejected = (
            "123 456", "123\t456", "123\n456", "1 2 3 4 5 6", "12 3456", "123\u00a0456", "123\u200b456",
            "12345", "1234567", "", "   ", "12345a", "+12345", "-12345", "12.345", "1e5000",
            "\u0661\u0662\u0663\u0664\u0665\u0666",  # Arabic-Indic digits
            "\uff11\uff12\uff13\uff14\uff15\uff16",  # fullwidth digits
            "12345\u0666", "12345\u00b2", "\u0967\u0968\u0969\u096a\u096b\u096c",  # mixed, superscript, Devanagari
            "123456\x00", "\x00123456", None, 123456, b"123456", ["123456"],
        )
        for value in rejected:
            with self.subTest(rejected=value):
                self.assertIsNone(registration._submitted_code(value))
        # Through verify: each rejected form is a counted wrong code, even with the right digits.
        self.codes("123456", "123456")
        issued = self.submit()
        for index, value in enumerate(("123 456", "123\t456", "123\n456", "\uff11\uff12\uff13\uff14\uff15\uff16"), 1):
            with self.subTest(submitted=value):
                self.assertEqual(self.verify(issued, value).outcome, Outcome.WRONG_CODE)
                self.assertEqual(self.pending(issued.pending_id)["code_attempts"], index)
        self.assertEqual(self.count("users"), 0)
        self.assertEqual(self.verify(issued, "\t123456 \n").outcome, Outcome.ACTIVATED)
        # The password of the account is untouched by any of this: exact, spaces included.
        self.assertIsNotNone(accounts.authenticate(self.conn, EMAIL, PASSWORD))
        self.conn.rollback()
        self.assertIsNone(accounts.authenticate(self.conn, EMAIL, PASSWORD.strip()))
        self.conn.rollback()

    def test_code_lifetime_and_registration_lifetime(self):
        issued = self.submit()
        code = code_of(issued)
        self.at(599)
        self.assertEqual(self.verify(issued, wrong(code)).outcome, Outcome.WRONG_CODE)
        self.at(600)
        before = self.digest()
        self.assertEqual(self.verify(issued, code).outcome, Outcome.CODE_EXPIRED)
        self.assertUnchanged(before)  # no attempt counted
        self.at(1799)
        self.assertEqual(self.verify(issued, code).outcome, Outcome.CODE_EXPIRED)
        self.assertEqual(self.pending(issued.pending_id)["state"], "pending")
        self.at(1800)
        self.assertEqual(self.verify(issued, code).outcome, Outcome.EXPIRED)
        self.assertScrubbed(issued.pending_id, "expired")
        self.assertEqual(self.count("users"), 0)

    def test_resend_rules(self):
        issued = self.submit()
        first = code_of(issued)
        self.at(59)
        before = self.digest()
        self.assertEqual(self.resend(issued).outcome, Outcome.TOO_EARLY)
        self.assertUnchanged(before)
        self.at(60)
        second = self.resend(issued)
        self.assertEqual((second.outcome, second.kind, second.token), (Outcome.ISSUED, "code", None))
        self.assertNotEqual(second.send_id, issued.send_id)
        row = self.pending(issued.pending_id)
        self.assertEqual((row["send_count"], row["code_send_id"]), (2, second.send_id))
        if first != code_of(second):
            self.assertEqual(self.verify(issued, first).outcome, Outcome.WRONG_CODE)
        # Never a code expiry past the registration's own.
        self.at(1500)
        third = self.resend(issued)
        self.assertEqual(self.pending(issued.pending_id)["code_expires_at"], stamp(T0 + timedelta(minutes=30)))
        self.at(1700)
        fourth = self.resend(issued)
        self.assertEqual(self.pending(issued.pending_id)["send_count"], 4)
        self.at(1790)
        before = self.digest()
        self.assertEqual(self.resend(issued).outcome, Outcome.RESEND_EXHAUSTED)
        self.assertUnchanged(before)
        self.assertEqual(len(self.sends()), 4)
        self.assertEqual(len({code_of(r) for r in (issued, second, third, fourth)} | {""}) > 1, True)
        self.assertEqual(self.verify(issued, code_of(fourth)).outcome, Outcome.ACTIVATED)

    def test_resend_of_an_overdue_row_expires_it(self):
        issued = self.submit()
        self.at(1800)
        self.assertEqual(self.resend(issued).outcome, Outcome.EXPIRED)
        self.assertScrubbed(issued.pending_id, "expired")
        self.assertEqual(len(self.sends()), 1)

    def test_cancel_voids_and_scrubs(self):
        issued = self.submit()
        self.assertEqual(self.cancel(issued).outcome, Outcome.CANCELLED)
        self.assertScrubbed(issued.pending_id, "void")
        self.assertEqual(len(self.sends()), 1)  # the attempt stays counted
        self.assertEqual(self.verify(issued, code_of(issued)).outcome, Outcome.NOT_PENDING)
        self.assertEqual(self.count("users"), 0)

    def test_key_rotation_ends_pending_registrations_and_resets_the_ledger_key(self):
        """D11: a pinned exception to the guessing bound (design 3.3), not a guarantee."""
        issued = None
        for seconds in (0, 1, 2, 3, 4, 3605, 3606, 3607):
            self.at(seconds)
            issued = self.submit()
            self.assertEqual(issued.outcome, Outcome.ISSUED)
        self.at(3608)
        self.assertEqual(self.submit().outcome, Outcome.ADDRESS_CAPPED)
        self.assertNotEqual(registration.email_hash(KEY, EMAIL), registration.email_hash("rotated", EMAIL))
        self.assertEqual(self.verify(issued, code_of(issued), key="rotated").outcome, Outcome.NO_PENDING)
        result = self.tx(registration.verify, self.binding(issued), code_of(issued), secret_key="rotated",
                         clock=self.clock)
        self.assertEqual(result.outcome, Outcome.WRONG_CODE)
        admitted = 0
        for seconds in (3609, 3610, 3611, 3612, 3613, 7214, 7215, 7216, 7217):
            self.at(seconds)
            admitted += self.submit(key="rotated").outcome is Outcome.ISSUED
        self.assertEqual(admitted, 8)


class TeacherTest(RegistrationTestCase):
    """A teacher registration is activated only behind the teacher gate (design 6.3 step 5, K5);
    the activation itself is tested in ``tests.test_teacher_registration``."""

    def test_no_teacher_row_without_the_gate(self):
        before = self.digest()
        result = self.submit(cleaned=teacher())
        self.assertEqual((result.outcome, result.reason), (Outcome.UNAVAILABLE, Availability.TEACHER_OFF))
        self.assertUnchanged(before)

    def test_teacher_row_is_voided_at_verification_while_the_gate_is_off(self):
        issued = self.submit(cleaned=teacher("t0@example.bg"), teacher_enabled=True)
        self.assertEqual(self.pending(issued.pending_id)["account_type"], "teacher")
        pupil = self.submit("s0@example.bg")
        result = self.verify(issued, code_of(issued), teacher_enabled=False)
        self.assertEqual((result.outcome, result.reason), (Outcome.UNAVAILABLE, Availability.TEACHER_OFF))
        self.assertScrubbed(issued.pending_id, "void")
        self.assertEqual(self.pending(issued.pending_id)["code_attempts"], 0)
        self.assertEqual(self.verify(pupil, code_of(pupil)).outcome, Outcome.ACTIVATED)
        # Never turned into a student, and the voided row cannot be completed later.
        self.assertEqual([user["role"] for user in self.users()], ["student"])
        self.assertEqual(self.verify(issued, code_of(issued), teacher_enabled=True).outcome, Outcome.NOT_PENDING)
        self.assertEqual(self.count("teacher_subjects"), 0)

    def test_resend_voids_a_teacher_row_while_the_gate_is_off_and_issues_nothing(self):
        issued = self.submit(cleaned=teacher(), teacher_enabled=True)
        self.at(61)
        result = self.resend(issued, teacher_enabled=False)
        self.assertEqual((result.outcome, result.message), (Outcome.UNAVAILABLE, None))
        self.assertScrubbed(issued.pending_id, "void")
        self.assertEqual(len(self.sends()), 1)

    def test_cleanup_scrubs_teacher_rows_whatever_the_gate(self):
        issued = self.submit(cleaned=teacher(), teacher_enabled=True)
        self.at(1800)
        self.assertEqual(self.cleanup().scrubbed, 1)
        self.assertScrubbed(issued.pending_id, "expired")


class ActivationErrorTest(RegistrationTestCase):
    """D-A: only a real email collision is "address taken"; everything else fails closed."""

    def setUp(self):
        super().setUp()
        self.issued = self.submit()
        self.code = code_of(self.issued)
        self.verify(self.issued, wrong(self.code))  # one counted attempt to preserve
        self.row_before = self.pending(self.issued.pending_id)
        self.before = self.digest()

    def assertStillPending(self):
        self.assertUnchanged(self.before)
        self.assertEqual(self.pending(self.issued.pending_id), self.row_before)

    def test_failure_at_each_write_leaves_everything_as_it_was(self):
        real_close = registration._close

        def failing_close(conn, pending_id, state, now):
            if state == "activated":
                raise sqlite3.OperationalError("disk I/O error")
            return real_close(conn, pending_id, state, now)

        injections = [
            patch.object(accounts, "create_verified_user", side_effect=sqlite3.OperationalError("disk full")),
            patch.object(profiles, "create_profile", side_effect=sqlite3.OperationalError("disk full")),
            patch.object(profiles, "create_profile", side_effect=RuntimeError("bug")),
            patch.object(registration, "_close", side_effect=failing_close),
        ]
        for injection in injections:
            with self.subTest(target=injection.attribute), injection:
                with self.assertRaises((sqlite3.OperationalError, RuntimeError)):
                    self.verify(self.issued, self.code)
                self.assertStillPending()
        # The same code then activates: nothing was consumed by the failures.
        self.assertEqual(self.verify(self.issued, self.code).outcome, Outcome.ACTIVATED)

    def test_other_integrity_errors_are_not_address_taken(self):
        def bad_profile(conn, user_id, **_):
            conn.execute("INSERT INTO user_profiles (user_id, account_type, first_name, last_name, grade, "
                         "grade_set_at) VALUES (?, 'student', 'А', 'Б', 7, 'x')", (user_id,))

        def bad_subject(conn, user_id, **_):
            conn.execute("INSERT INTO teacher_subjects (user_id, position, subject_id, label) "
                         "VALUES (?, 1, 'it', 'Информатика')", (user_id,))  # no profile: foreign key

        def bad_name(conn, **_):
            conn.execute("INSERT INTO users (email, display_name, role, session_key) VALUES (?, 'x', 'student', 'k')",
                         (EMAIL,))

        def other_account_then_bad_name(conn, **kwargs):
            # A users row with the address appears, yet the failure is not the uniqueness of the email.
            accounts.create_user(conn, display_name="Друг Човек", email=EMAIL, password="another-password")
            conn.execute("INSERT INTO users (email, display_name, role, session_key) VALUES (?, 'x', 'student', 'k')",
                         ("unrelated@example.bg",))

        cases = [
            (profiles, "create_profile", bad_profile), (profiles, "create_profile", bad_subject),
            (accounts, "create_verified_user", bad_name),
            (accounts, "create_verified_user", other_account_then_bad_name),
        ]
        for module, name, replacement in cases:
            with self.subTest(case=replacement.__name__), patch.object(module, name, side_effect=replacement):
                with self.assertRaises(sqlite3.IntegrityError) as caught:
                    self.verify(self.issued, self.code)
                self.assertNotIn("users.email", str(caught.exception))
                self.assertStillPending()

    def test_error_text_never_decides(self):
        """An integrity error that only reads like the uniqueness of the email propagates,
        with a row of that address present or not."""
        forced = sqlite3.IntegrityError("UNIQUE constraint failed: users.email")
        with patch.object(accounts, "create_verified_user", side_effect=forced):
            with self.assertRaises(sqlite3.IntegrityError):
                self.verify(self.issued, self.code)
        self.assertStillPending()
        self.add_account()
        with patch.object(accounts, "create_verified_user", side_effect=forced):
            with self.assertRaises(sqlite3.IntegrityError):
                self.verify(self.issued, self.code)
        self.assertStillPending()

    def add_account(self, email=EMAIL):
        """An account that takes the address after the registration was submitted."""
        self.conn.execute("BEGIN IMMEDIATE")
        user_id = accounts.create_user(self.conn, display_name="Друг Човек", email=email,
                                       password="another-password", role="teacher")
        self.conn.commit()
        self.before = self.digest()
        return user_id

    def test_trigger_error_with_matching_text_and_an_existing_address_propagates(self):
        """SQLITE_CONSTRAINT_TRIGGER (1811) is not an email collision, whatever it says."""
        self.add_account()
        self.conn.execute(
            "CREATE TRIGGER test_users_refuse BEFORE INSERT ON users BEGIN "
            "SELECT RAISE(ABORT, 'UNIQUE constraint failed: users.email'); END")
        self.conn.commit()
        with self.assertRaises(sqlite3.IntegrityError) as caught:
            self.verify(self.issued, self.code)
        self.assertEqual(caught.exception.sqlite_errorcode, 1811)
        self.assertEqual(str(caught.exception), "UNIQUE constraint failed: users.email")
        self.assertStillPending()
        self.conn.execute("DROP TRIGGER test_users_refuse")
        self.conn.commit()
        # The same code is still usable, and now meets the real collision.
        self.assertEqual(self.verify(self.issued, self.code).outcome, Outcome.EMAIL_TAKEN)

    def test_check_and_unrelated_uniqueness_in_the_real_insert_propagate(self):
        for with_account in (False, True):
            if with_account:
                self.add_account()
            with self.subTest(failure="check", account=with_account), \
                    patch.object(accounts, "_new_session_key", return_value="short"):
                with self.assertRaises(sqlite3.IntegrityError) as caught:
                    self.verify(self.issued, self.code)
                self.assertEqual(caught.exception.sqlite_errorcode, sqlite3.SQLITE_CONSTRAINT_CHECK)
                self.assertStillPending()
        other = self.add_account("unrelated@example.bg")
        self.conn.execute("CREATE UNIQUE INDEX test_users_session_key ON users(session_key)")
        self.conn.commit()
        taken_key = self.conn.execute("SELECT session_key FROM users WHERE id = ?", (other,)).fetchone()[0]
        self.conn.execute("DELETE FROM users WHERE email = ?", (EMAIL,))
        self.conn.commit()
        self.before = self.digest()
        with patch.object(accounts, "_new_session_key", return_value=taken_key):
            with self.assertRaises(sqlite3.IntegrityError) as caught:
                self.verify(self.issued, self.code)
        self.assertEqual(caught.exception.sqlite_errorcode, sqlite3.SQLITE_CONSTRAINT_UNIQUE)
        self.assertIn("users.session_key", str(caught.exception))
        self.assertStillPending()

    def test_foreign_key_failure_propagates(self):
        """With an account of the address present: a foreign-key failure is not "taken"."""
        self.conn.execute(
            "CREATE TRIGGER test_users_fk BEFORE INSERT ON users BEGIN "
            "INSERT INTO user_profiles (user_id, account_type, first_name, last_name, grade, grade_set_at) "
            "VALUES (987654, 'student', 'Ана', 'Иванова', 9, 'x'); END")
        self.conn.commit()
        for with_account in (False, True):
            with self.subTest(account=with_account):
                if with_account:
                    self.conn.execute("DROP TRIGGER test_users_fk")
                    self.add_account()
                    self.conn.execute(
                        "CREATE TRIGGER test_users_fk BEFORE INSERT ON users BEGIN "
                        "INSERT INTO user_profiles (user_id, account_type, first_name, last_name, grade, "
                        "grade_set_at) VALUES (987654, 'student', 'Ана', 'Иванова', 9, 'x'); END")
                    self.conn.commit()
                with self.assertRaises(sqlite3.IntegrityError) as caught:
                    self.verify(self.issued, self.code)
                self.assertEqual(caught.exception.sqlite_errorcode, sqlite3.SQLITE_CONSTRAINT_FOREIGNKEY)
                self.assertStillPending()

    def test_real_collision_after_submission_is_address_taken(self):
        """The unpatched path: an account took the address between submission and verification."""
        user_id = self.add_account()
        account = self.users()
        result = self.verify(self.issued, self.code)
        self.assertEqual((result.outcome, result.user_id), (Outcome.EMAIL_TAKEN, None))
        self.assertScrubbed(self.issued.pending_id, "void")
        self.assertEqual(self.users(), account)  # id, role, password and every other column as they were
        self.assertEqual(account[0]["id"], user_id)
        self.assertEqual(self.count("user_profiles"), 0)
        self.assertIsNone(accounts.authenticate(self.conn, EMAIL, PASSWORD))
        self.conn.rollback()

    def test_collision_at_the_insert_is_address_taken(self):
        real = accounts.create_verified_user
        seen = {}

        def racing(conn, **kwargs):
            accounts.create_user(conn, display_name="Друг Човек", email=EMAIL, password="another-password",
                                 role="teacher")
            seen["row"] = conn.execute("SELECT * FROM users WHERE email = ?", (EMAIL,)).fetchone()
            return real(conn, **kwargs)

        with patch.object(accounts, "create_verified_user", side_effect=racing):
            result = self.verify(self.issued, self.code)
        self.assertEqual(result.outcome, Outcome.EMAIL_TAKEN)
        self.assertIsNone(result.user_id)
        self.assertScrubbed(self.issued.pending_id, "void")
        self.assertEqual(self.conn.execute("SELECT * FROM users").fetchall(), [seen["row"]])
        self.assertEqual(self.count("user_profiles"), 0)

    def test_activation_is_a_decision_until_the_commit_returns(self):
        self.conn.execute("BEGIN IMMEDIATE")
        result = registration.verify(self.conn, self.binding(self.issued), self.code, secret_key=KEY,
                                     clock=self.clock)
        self.assertEqual(result.outcome, Outcome.ACTIVATED)
        self.assertTrue(self.conn.in_transaction)
        other = self.connect(timeout=0)
        self.assertEqual(other.execute("SELECT COUNT(*) FROM users").fetchone()[0], 0)
        self.conn.rollback()
        self.assertStillPending()

    def test_commit_that_fails_with_sqlite_busy_activates_nothing(self):
        """DA5 with a real failure: a rollback-journal file, a reader holding its shared lock,
        and COMMIT refused with SQLITE_BUSY. The caller's rollback restores the pending row.
        What a route answers and what its session holds then is PR 4's to assert."""
        self.assertEqual(self.conn.execute("PRAGMA journal_mode").fetchone()[0], "delete")
        reader = self.connect(timeout=0)
        reader.execute("BEGIN")
        self.assertEqual(reader.execute("SELECT COUNT(*) FROM users").fetchone()[0], 0)  # shared lock held
        writer = self.connect(timeout=0)

        accepted, failure = None, None
        writer.execute("BEGIN IMMEDIATE")
        try:
            decided = registration.verify(writer, self.binding(self.issued), self.code, secret_key=KEY,
                                          clock=self.clock)
            # Every write of the activation is made, inside the writer's transaction only.
            self.assertEqual((decided.outcome, decided.user_id), (Outcome.ACTIVATED, 1))
            self.assertEqual(writer.execute("SELECT COUNT(*) FROM users").fetchone()[0], 1)
            self.assertEqual(writer.execute("SELECT COUNT(*) FROM user_profiles").fetchone()[0], 1)
            self.assertEqual(
                writer.execute("SELECT state FROM pending_registrations WHERE id = ?",
                               (self.issued.pending_id,)).fetchone()[0], "activated")
            writer.commit()
            accepted = decided  # reached only when the commit returned
        except sqlite3.OperationalError as exc:
            failure = exc
            self.assertTrue(writer.in_transaction)  # a failed COMMIT ends nothing by itself
            writer.rollback()

        self.assertIsNone(accepted)
        self.assertEqual(failure.sqlite_errorcode, sqlite3.SQLITE_BUSY)
        self.assertFalse(writer.in_transaction)
        self.assertEqual(reader.execute("SELECT COUNT(*) FROM users").fetchone()[0], 0)
        reader.rollback()
        self.assertStillPending()  # row, code and counted attempt as before; no user, no profile
        self.assertIsNone(accounts.authenticate(self.conn, EMAIL, PASSWORD))
        self.conn.rollback()
        self.assertEqual(self.conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        # Nothing was consumed: with the reader gone the same code activates, with the same id.
        result = self.verify(self.issued, self.code, conn=writer)
        self.assertEqual((result.outcome, result.user_id), (Outcome.ACTIVATED, 1))
        self.assertEqual(self.count("users"), 1)

    def test_issuance_commit_that_fails_leaves_nothing_to_send(self):
        """The same failure at the commit of a submission: no row, no ledger entry, no send."""
        from web.services.mail import InMemoryMailer

        reader = self.connect(timeout=0)
        reader.execute("BEGIN")
        reader.execute("SELECT COUNT(*) FROM users").fetchone()
        writer, mailer = self.connect(timeout=0), InMemoryMailer()
        writer.execute("BEGIN IMMEDIATE")
        issued = registration.create_pending(
            writer, cleaned=student("late@example.bg"), account_type="student", password_hash=password_hash(),
            previous_binding=None, secret_key=KEY, clock=self.clock, sender_busy=False)
        self.assertEqual(issued.outcome, Outcome.ISSUED)
        with self.assertRaises(sqlite3.OperationalError) as caught:
            writer.commit()
        self.assertEqual(caught.exception.sqlite_errorcode, sqlite3.SQLITE_BUSY)
        writer.rollback()
        reader.rollback()
        with self.assertRaises(registration.NotSendable):
            registration.dispatch(writer, mailer, issued, secret_key=KEY)
        self.assertEqual(mailer.messages, [])
        self.assertStillPending()


class CollisionTest(RegistrationTestCase):
    """E1 to E4, E6 to E10."""

    def in_threads(self, calls):
        """Run each call on its own connection, released together; return their results."""
        barrier = threading.Barrier(len(calls))
        results, errors = [None] * len(calls), []

        def run(index, call):
            conn = sqlite3.connect(self.path, timeout=30)
            try:
                conn.execute("PRAGMA foreign_keys = ON")
                barrier.wait()
                results[index] = call(conn)
            except BaseException as exc:  # reported below, never swallowed
                errors.append(exc)
            finally:
                conn.close()

        threads = [threading.Thread(target=run, args=(index, call)) for index, call in enumerate(calls)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(60)
        self.assertEqual(errors, [])
        return results

    def test_two_registrations_of_one_address_first_wins(self):
        first, second = self.submit(), self.submit()
        self.assertEqual(self.verify(first, code_of(first)).outcome, Outcome.ACTIVATED)
        account = self.users()
        # Decided by the insert itself: SQLite reports the conflict on users.email and writes nothing.
        result = self.verify(second, code_of(second))
        self.assertEqual(result.outcome, Outcome.EMAIL_TAKEN)
        self.assertScrubbed(second.pending_id, "void")
        self.assertEqual(self.users(), account)
        self.assertEqual(self.count("user_profiles"), 1)

    def test_simultaneous_verifications_of_two_registrations(self):
        first, second = self.submit(), self.submit()
        results = self.in_threads([
            lambda conn, issued=issued: self.verify(issued, code_of(issued), conn=conn)
            for issued in (first, second)
        ])
        self.assertEqual(sorted(r.outcome.value for r in results), ["activated", "email_taken"])
        self.assertEqual((self.count("users"), self.count("user_profiles")), (1, 1))
        self.assertEqual(sorted(self.pending(i.pending_id)["state"] for i in (first, second)),
                         ["activated", "void"])

    def test_simultaneous_verifications_of_one_registration(self):
        issued = self.submit()
        results = self.in_threads([lambda conn: self.verify(issued, code_of(issued), conn=conn)] * 4)
        self.assertEqual(sorted(r.outcome.value for r in results),
                         ["activated", "not_pending", "not_pending", "not_pending"])
        self.assertEqual((self.count("users"), self.count("user_profiles")), (1, 1))

    def test_concurrent_wrong_codes_count_exactly(self):
        issued = self.submit()
        bad = wrong(code_of(issued))
        results = self.in_threads([lambda conn: self.verify(issued, bad, conn=conn)] * 7)
        self.assertEqual(sorted(r.outcome.value for r in results), ["too_many"] * 2 + ["wrong_code"] * 5)
        self.assertEqual(self.pending(issued.pending_id)["code_attempts"], 5)
        self.assertEqual(self.verify(issued, code_of(issued)).outcome, Outcome.TOO_MANY)

    def run_cli_create(self):
        return subprocess.run(
            [sys.executable, "-B", "-m", "src.manage_accounts", "--db", str(self.path), "create", "--email",
             EMAIL.upper(), "--name", "Оператор Създаден", "--role", "teacher", "--password-stdin"],
            input="operator-chosen-pass\n", capture_output=True, text=True, cwd=ROOT, timeout=60,
        )

    def test_cli_create_while_an_activation_holds_the_lock(self):
        issued = self.submit()
        self.conn.execute("BEGIN IMMEDIATE")
        result = registration.verify(self.conn, self.binding(issued), code_of(issued), secret_key=KEY,
                                     clock=self.clock)
        self.assertEqual(result.outcome, Outcome.ACTIVATED)
        outcome = {}
        thread = threading.Thread(target=lambda: outcome.update(cli=self.run_cli_create()))
        thread.start()  # its INSERT waits for this writer, or runs after the commit: one result either way
        self.conn.commit()
        thread.join(90)
        self.assertEqual(outcome["cli"].returncode, 1)
        self.assertIn("already exists", outcome["cli"].stderr)
        self.assertEqual([(u["email"], u["role"]) for u in self.users()], [(EMAIL, "student")])

    def test_cli_create_before_the_activation(self):
        issued = self.submit()
        created = self.run_cli_create()
        self.assertEqual(created.returncode, 0, created.stderr)
        account = self.users()
        self.assertEqual(self.verify(issued, code_of(issued)).outcome, Outcome.EMAIL_TAKEN)
        self.assertScrubbed(issued.pending_id, "void")
        self.assertEqual(self.users(), account)
        self.assertEqual(self.count("user_profiles"), 0)

    def test_shadow_registration_for_every_kind_of_existing_account(self):
        owners = []
        for index, (role, active) in enumerate((r, a) for r in ("student", "teacher", "admin") for a in (1, 0)):
            email = f"owner{index}@example.bg"
            accounts.create_user(self.conn, display_name="Собственик Профилов", email=email,
                                 password="the-owners-own-password", role=role, is_active=bool(active))
            owners.append(email)
        self.conn.commit()
        accounts_before = self.users()
        for index, email in enumerate(owners):
            with self.subTest(email=email):
                self.at(index * 2000)
                issued = self.submit(email)
                self.assertEqual((issued.outcome, issued.kind), (Outcome.ISSUED, "notice"))
                self.assertEqual(issued.message.to, email)
                self.assertEqual(issued.message.subject, registration.NOTICE_SUBJECT)
                self.assertNotRegex(issued.message.text, r"\d{6}|http|Собственик|student|teacher|admin")
                row = self.pending(issued.pending_id)
                self.assertEqual((row["is_shadow"], row["password_hash"], row["send_count"]), (1, None, 1))
                self.assertEqual(len(row["code_hash"]), 64)
                self.assertEqual(self.send(issued.send_id)["kind"], "notice")
                # Attempt counting, the limit and a resend behave as for a real row.
                for attempt in range(1, 6):
                    self.assertEqual(self.verify(issued, f"{attempt:06d}").outcome, Outcome.WRONG_CODE)
                self.assertEqual(self.verify(issued, "000006").outcome, Outcome.TOO_MANY)
                self.at(index * 2000 + 30)
                self.assertEqual(self.resend(issued).outcome, Outcome.TOO_EARLY)
                self.at(index * 2000 + 61)
                again = self.resend(issued)
                self.assertEqual((again.outcome, again.kind), (Outcome.ISSUED, "notice"))
                self.assertNotEqual(self.pending(issued.pending_id)["code_hash"], row["code_hash"])
                # Even a code forced to match activates nothing.
                self.conn.execute("UPDATE pending_registrations SET code_hash = ? WHERE id = ?",
                                  (registration._code_hash(KEY, issued.pending_id, b"123456"), issued.pending_id))
                self.conn.commit()
                self.assertEqual(self.verify(issued, "123456").outcome, Outcome.EMAIL_TAKEN)
                self.assertScrubbed(issued.pending_id, "void")
        self.assertEqual(self.users(), accounts_before)
        self.assertEqual(self.count("user_profiles"), 0)

    def test_shadow_expires_like_a_real_row(self):
        accounts.create_user(self.conn, display_name="Собственик", email=EMAIL, password="the-owners-password")
        self.conn.commit()
        issued = self.submit()
        self.at(600)
        self.assertEqual(self.verify(issued, "123456").outcome, Outcome.CODE_EXPIRED)
        self.at(1800)
        self.assertEqual(self.verify(issued, "123456").outcome, Outcome.EXPIRED)

    def google(self, subject, email):
        self.conn.execute("BEGIN IMMEDIATE")
        outcome = oidc_accounts.sign_in_or_onboard(
            self.conn, oidc_accounts.GoogleProfile(subject=subject, email=email, name="Гугъл Ученик"))
        self.conn.commit()
        return outcome

    def test_google_email_metadata_is_not_a_collision(self):
        created = self.google("google-subject-1", EMAIL)
        self.assertEqual(created.outcome, oidc_accounts.CREATED)
        identities_before = self.rows("SELECT * FROM user_identities")
        self.assertEqual(identities_before[0]["email_at_link"], EMAIL)
        google_user = self.users()
        issued = self.submit()
        self.assertEqual((issued.kind, self.pending(issued.pending_id)["is_shadow"]), ("code", 0))
        result = self.verify(issued, code_of(issued))
        self.assertEqual(result.outcome, Outcome.ACTIVATED)
        self.assertNotEqual(result.user_id, created.user["id"])
        self.assertEqual(self.users()[0], google_user[0])
        self.assertEqual(self.rows("SELECT * FROM user_identities"), identities_before)
        self.assertIsNone(self.users()[0]["email"])  # the Google account gained no password identifier

    def test_google_onboarding_against_pending_and_registered_addresses(self):
        issued = self.submit()
        self.assertEqual(self.google("google-subject-2", EMAIL).outcome, oidc_accounts.CREATED)
        self.assertEqual(self.verify(issued, code_of(issued)).outcome, Outcome.ACTIVATED)
        self.assertEqual(self.count("users"), 2)
        self.assertEqual(self.google("google-subject-3", EMAIL).outcome, oidc_accounts.EMAIL_EXISTS)
        self.assertEqual(self.count("users"), 2)
        self.assertEqual(self.count("user_identities"), 1)

    def test_service_never_touches_identities(self):
        source = Path(registration.__file__).read_text(encoding="utf-8")
        for word in ("user_identities", "identities", "email_at_link", "oidc"):
            self.assertNotIn(word, source)
        for word in (".commit(", ".rollback(", "isolation_level", "autocommit"):
            self.assertNotIn(word, source)


class ClockPolicyTest(RegistrationTestCase):
    """D-C. Wall time is ``self.wall``; where real time differs the test says so."""

    def test_small_future_dating_works(self):
        self.at(300)
        issued = self.submit()
        self.at(0)  # the row and its code are dated 300 s in the future
        self.assertEqual(self.verify(issued, wrong(code_of(issued))).outcome, Outcome.WRONG_CODE)
        self.assertEqual(self.verify(issued, code_of(issued)).outcome, Outcome.ACTIVATED)

    def test_far_future_dating_expires_only_once_the_mark_allows(self):
        self.at(301)
        issued = self.submit()
        self.at(0)
        before = self.digest()
        self.assertEqual(self.verify(issued, code_of(issued)).outcome, Outcome.UNAVAILABLE)
        self.assertUnchanged(before)
        self.set_control(high_water=self.wall)  # what registrations-clock-accept will do (PR 4)
        self.assertEqual(self.verify(issued, code_of(issued)).outcome, Outcome.EXPIRED)
        self.assertScrubbed(issued.pending_id, "expired")

    def late_resend(self, at=1700):
        """A registration of T0 whose code was replaced at ``at`` seconds; returns both issuances."""
        self.codes("111111", "222222")
        first = self.submit()
        self.at(at)
        return first, self.resend(first)

    def test_code_issued_ahead_of_an_accepted_clock_is_not_live(self):
        """The expiry of a late code is cut at the registration's deadline, so it cannot say
        when the code was issued; the ledger row can. Issued 800 s ahead of the clock."""
        first, second = self.late_resend(1700)
        row = self.pending(first.pending_id)
        self.assertEqual(row["code_expires_at"], row["expires_at"])  # 100 s of life, not 600
        self.assertEqual(self.send(second.send_id)["attempted_at"], stamp(self.wall))
        self.at(900)
        self.assertEqual(self.verify(first, "222222").reason, Availability.CLOCK_BEHIND)
        self.set_control(high_water=self.wall)  # the operator accepts the clock (PR 4)
        before = self.digest()
        with self.assertNoLogs("learnpilot.registration", level=logging.WARNING):
            for code in ("222222", "000000"):
                self.assertEqual(self.verify(first, code).outcome, Outcome.CODE_EXPIRED)
        self.assertUnchanged(before)  # no activation, no attempt counted, the row stays pending
        self.assertEqual(self.count("users"), 0)

    def test_skew_boundary_of_a_shortened_code(self):
        first, _ = self.late_resend(1700)
        self.set_control(high_water=T0)
        for seconds, live in ((1399, False), (1400, True), (1500, True)):
            self.at(seconds)  # the code is 301, 300 and 200 seconds ahead
            with self.subTest(ahead=1700 - seconds):
                expected = Outcome.WRONG_CODE if live else Outcome.CODE_EXPIRED
                self.assertEqual(self.verify(first, "000000").outcome, expected)
        self.at(1400)
        self.assertEqual(self.verify(first, "222222").outcome, Outcome.ACTIVATED)

    def test_end_of_a_shortened_code(self):
        """Issued at 1700 with 100 s left: live to 1799, and at 1800 the registration itself ends."""
        first, _ = self.late_resend(1700)
        self.at(1799)
        self.assertEqual(self.verify(first, "000000").outcome, Outcome.WRONG_CODE)
        self.at(1800)
        self.assertEqual(self.verify(first, "222222").outcome, Outcome.EXPIRED)

    def test_end_of_a_code_that_is_cut_by_one_second(self):
        first, second = self.late_resend(1201)  # 600 s would end at 1801
        self.assertEqual(self.pending(first.pending_id)["code_expires_at"], stamp(T0 + timedelta(seconds=1800)))
        self.at(1799)
        self.assertEqual(self.verify(first, "222222").outcome, Outcome.ACTIVATED)

    def test_end_of_a_full_length_code(self):
        first, _ = self.late_resend(1000)
        self.at(1599)
        self.assertEqual(self.verify(first, "000000").outcome, Outcome.WRONG_CODE)
        self.at(1600)
        before = self.digest()
        self.assertEqual(self.verify(first, "222222").outcome, Outcome.CODE_EXPIRED)
        self.assertUnchanged(before)

    def test_missing_or_inconsistent_issuance_evidence_fails_closed(self):
        self.codes("111111", "222222", "333333", "444444")
        issued = self.submit()
        other = self.submit("other@example.bg")
        self.at(61)
        send, row = self.send(issued.send_id), self.pending(issued.pending_id)
        ahead = stamp(T0 + timedelta(seconds=30))
        damage = {
            "ledger row deleted": ("DELETE FROM registration_sends WHERE id = ?", (issued.send_id,)),
            "ledger row of no registration": (
                "UPDATE registration_sends SET pending_id = NULL WHERE id = ?", (issued.send_id,)),
            "ledger row of another registration": (
                "UPDATE registration_sends SET pending_id = ? WHERE id = ?", (other.pending_id, issued.send_id)),
            "generation of another registration": (
                "UPDATE pending_registrations SET code_send_id = ? WHERE id = ?",
                (other.send_id, issued.pending_id)),
            "no generation": (
                "UPDATE pending_registrations SET code_send_id = NULL WHERE id = ?", (issued.pending_id,)),
            "a notice, not a code": (
                "UPDATE registration_sends SET kind = 'notice' WHERE id = ?", (issued.send_id,)),
            "issuance time moved": (
                "UPDATE registration_sends SET attempted_at = ? WHERE id = ?", (ahead, issued.send_id)),
            "expiry moved": (
                "UPDATE pending_registrations SET code_expires_at = ? WHERE id = ?", (ahead, issued.pending_id)),
            "code hash removed": (
                "UPDATE pending_registrations SET code_hash = NULL WHERE id = ?", (issued.pending_id,)),
            "time without padding": (
                "UPDATE registration_sends SET attempted_at = ? WHERE id = ?",
                (send["attempted_at"].replace(":00:00", ":0:0"), issued.send_id)),
            "expiry removed": (
                "UPDATE pending_registrations SET code_expires_at = NULL WHERE id = ?", (issued.pending_id,)),
            "time in another layout": (
                "UPDATE registration_sends SET attempted_at = ? WHERE id = ?",
                (send["attempted_at"].replace(" ", "T"), issued.send_id)),
            "time with a fraction": (
                "UPDATE registration_sends SET attempted_at = ? WHERE id = ?",
                (send["attempted_at"] + ".000", issued.send_id)),
            "time that is no time": (
                "UPDATE registration_sends SET attempted_at = '' WHERE id = ?", (issued.send_id,)),
            "time that is a number": (
                "UPDATE registration_sends SET attempted_at = 20261013120000 WHERE id = ?", (issued.send_id,)),
        }
        for name, (sql, params) in damage.items():
            with self.subTest(evidence=name):
                self.conn.execute(sql, params)
                self.conn.commit()
                before = self.digest()
                with self.assertLogs("learnpilot.registration", level=logging.WARNING) as logs:
                    self.assertEqual(self.verify(issued, "111111").outcome, Outcome.CODE_EXPIRED)
                self.assertEqual([line.split(":", 2)[2] for line in logs.output],
                                 [f"code_evidence_invalid pending={issued.pending_id}"])
                self.assertUnchanged(before)
                # Put back exactly what was there.
                self.conn.execute("DELETE FROM registration_sends WHERE id = ?", (issued.send_id,))
                self.conn.execute(
                    "INSERT INTO registration_sends (id, pending_id, email_hash, kind, attempted_at, outcome, "
                    "settled_at) VALUES (:id, :pending_id, :email_hash, :kind, :attempted_at, :outcome, "
                    ":settled_at)", send)
                self.conn.execute(
                    "UPDATE pending_registrations SET code_send_id = ?, code_expires_at = ?, code_hash = ? "
                    "WHERE id = ?",
                    (row["code_send_id"], row["code_expires_at"], row["code_hash"], issued.pending_id))
                self.conn.commit()
                self.assertEqual(self.pending(issued.pending_id), row)
        # The evidence intact again, the code is live; and a resend replaces damaged evidence.
        self.assertEqual(self.verify(issued, "000000").outcome, Outcome.WRONG_CODE)
        self.conn.execute("DELETE FROM registration_sends WHERE id = ?", (other.send_id,))
        self.conn.commit()
        self.assertEqual(self.verify(other, "222222").outcome, Outcome.CODE_EXPIRED)
        again = self.resend(other)
        self.assertEqual(self.verify(other, code_of(again)).outcome, Outcome.ACTIVATED)

    def test_cleanup_never_removes_the_evidence_of_a_live_code(self):
        """A ledger row is deleted 48 h after its attempt; a registration lives 30 minutes."""
        issued = self.submit()
        self.at(599)
        self.cleanup()
        self.assertEqual(self.verify(issued, code_of(issued)).outcome, Outcome.ACTIVATED)
        self.assertGreater(registration.LEDGER_RETENTION, registration.PENDING_LIFETIME + registration.CLOCK_SKEW)

    def test_future_dated_code_on_a_current_row_is_not_live(self):
        issued = self.submit()
        self.conn.execute("UPDATE pending_registrations SET code_expires_at = ? WHERE id = ?",
                          (stamp(T0 + timedelta(seconds=600 + 301)), issued.pending_id))
        self.conn.commit()
        self.assertEqual(self.verify(issued, code_of(issued)).outcome, Outcome.CODE_EXPIRED)

    def regressed(self):
        """A pending row, the mark at its issuance, and a clock more than 300 s behind it."""
        self.at(1000)
        issued = self.submit()
        self.assertEqual(self.control()["high_water_at"], stamp(self.wall))
        self.at(1000 - 301)
        return issued

    def test_clock_behind_the_mark_stops_every_writer_before_any_change(self):
        issued = self.regressed()
        before = self.digest()
        with self.assertLogs("learnpilot.registration", level=logging.WARNING) as logs:
            self.assertEqual(registration.availability(self.conn, self.wall, sender_busy=False),
                             Availability.CLOCK_BEHIND)
            calls = {
                "verify": lambda: self.verify(issued, code_of(issued)),
                "wrong code": lambda: self.verify(issued, wrong(code_of(issued))),
                "submit": lambda: self.submit("new@example.bg", previous=self.binding(issued)),
                "resend": lambda: self.resend(issued),
                "cancel": lambda: self.cancel(issued),
            }
            for name, call in calls.items():
                with self.subTest(writer=name):
                    result = call()
                    self.assertEqual((result.outcome, result.reason),
                                     (Outcome.UNAVAILABLE, Availability.CLOCK_BEHIND))
                    self.assertUnchanged(before)
            self.assertEqual(self.cleanup(), registration.CleanupResult(True))
            self.assertUnchanged(before)
        self.assertTrue(all("clock_regressed" in line for line in logs.output))
        self.assertEqual(self.pending(issued.pending_id)["state"], "pending")
        # It ends when the clock passes the mark again; nothing was lost meanwhile.
        self.at(1000 - 300)
        self.assertEqual(self.verify(issued, code_of(issued)).outcome, Outcome.ACTIVATED)

    def test_overdue_row_is_not_expired_by_a_regressed_clock(self):
        issued = self.submit()
        self.at(5000)
        other = self.submit("other@example.bg")  # raises the mark; the first row is now long overdue
        self.assertEqual(self.pending(issued.pending_id)["state"], "expired")  # the inline cleanup
        self.at(4000)
        before = self.digest()
        self.assertEqual(self.verify(other, code_of(other)).outcome, Outcome.UNAVAILABLE)
        self.assertEqual(self.cleanup().clock_behind, True)
        self.assertUnchanged(before)

    def test_hold_stops_requests_and_is_never_released_here(self):
        issued = self.submit()
        self.set_control(high_water=T0, hold="restore")
        held = self.control()
        before = self.digest()
        self.assertEqual(registration.availability(self.conn, self.wall, sender_busy=False), Availability.HOLD)
        for call in (lambda: self.verify(issued, code_of(issued)), lambda: self.resend(issued),
                     lambda: self.submit("new@example.bg", previous=self.binding(issued))):
            result = call()
            self.assertEqual((result.outcome, result.reason), (Outcome.UNAVAILABLE, Availability.HOLD))
            self.assertUnchanged(before)
        # Cleanup only removes data and is not stopped by the hold; it keeps the hold as it found it.
        self.at(1800)
        result = self.cleanup()
        self.assertEqual((result.clock_behind, result.scrubbed), (False, 1))
        self.assertScrubbed(issued.pending_id, "expired")
        # No automatic release: not by time passing, not by a forward step of a year.
        for seconds in (W, 7 * 86400, 365 * 86400):
            self.at(seconds)
            self.assertEqual(self.submit("later@example.bg").reason, Availability.HOLD)
            self.cleanup()
            control = self.control()
            self.assertEqual((control["hold_reason"], control["hold_set_at"]),
                             (held["hold_reason"], held["hold_set_at"]))
        self.assertEqual(registration.availability(self.conn, self.wall, sender_busy=False), Availability.HOLD)

    def test_cancel_is_not_stopped_by_a_hold(self):
        issued = self.submit()
        self.set_control(high_water=T0, hold="restore")
        held = self.control()
        self.assertEqual(self.cancel(issued).outcome, Outcome.CANCELLED)
        self.assertScrubbed(issued.pending_id, "void")
        self.assertEqual(self.control(), held)  # the hold and the mark exactly as they were

    def test_cancel_during_a_hold_is_still_stopped_by_a_regressed_clock(self):
        """Both at once: the hold is passed over, the clock is not."""
        issued = self.regressed()
        self.set_control(high_water=T0 + timedelta(seconds=1000), hold="restore")
        before = self.digest()
        result = self.cancel(issued)
        self.assertEqual((result.outcome, result.reason), (Outcome.UNAVAILABLE, Availability.CLOCK_BEHIND))
        self.assertUnchanged(before)
        self.at(1000 - 300)
        self.assertEqual(self.cancel(issued).outcome, Outcome.CANCELLED)
        self.assertEqual(self.control()["hold_reason"], "restore")

    def test_final_state_of_an_overdue_row_that_is_cancelled_or_replaced(self):
        """An overdue row that is cancelled becomes ``void`` (cancel checks no expiry); one
        that is replaced becomes ``expired``, by the inline cleanup that runs before the
        voiding of the browser's previous row."""
        cancelled, replaced = self.submit("a@example.bg"), self.submit("b@example.bg")
        self.at(1800)
        self.assertEqual(self.cancel(cancelled).outcome, Outcome.CANCELLED)
        self.assertScrubbed(cancelled.pending_id, "void")
        self.assertEqual(self.submit("c@example.bg", previous=self.binding(replaced)).outcome, Outcome.ISSUED)
        self.assertScrubbed(replaced.pending_id, "expired")

    def test_clock_set_back_gives_no_extra_attempts(self):
        issued = self.submit()
        for _ in range(5):
            self.verify(issued, wrong(code_of(issued)))
        self.at(-250)
        self.assertEqual(self.verify(issued, code_of(issued)).outcome, Outcome.TOO_MANY)
        self.assertEqual(self.pending(issued.pending_id)["code_attempts"], 5)

    def test_forward_step_expires_early_and_releases_budgets_without_bound(self):
        """DC4. The second half documents the **unbounded** case of design 3.8: it is not a guarantee."""
        issued = self.submit()
        self.at(601)
        self.assertEqual(self.verify(issued, code_of(issued)).outcome, Outcome.CODE_EXPIRED)
        base = 0
        for step in range(3):
            admitted = 0
            for offset in (0, 1, 2, 3, 3601, 3602, 3603, 3604, 3605, 7300):
                self.at(base + offset + (10 if step == 0 else 0))
                admitted += self.submit().outcome is Outcome.ISSUED
            self.assertEqual(admitted, 8 if step else 7, step)  # the first round already holds one send
            base += W + 7400  # a forward step of more than 24 h 15 min: a fresh daily budget each time

    def test_forward_then_back_with_a_cleanup_in_between(self):
        issued = self.submit()
        self.at(900)
        self.cleanup()  # the timer saw the later time
        self.assertEqual(self.control()["high_water_at"], stamp(self.wall))
        self.at(500)
        self.assertEqual(self.verify(issued, code_of(issued)).outcome, Outcome.UNAVAILABLE)

    def test_mark_is_raised_by_issuance_and_cleanup_and_never_lowered(self):
        self.assertIsNone(self.control())
        self.at(100)
        issued = self.submit()
        self.assertEqual(self.control()["high_water_at"], stamp(self.wall))
        self.at(50)  # inside the tolerated skew: admitted, the mark stays
        self.assertEqual(self.submit("b@example.bg").outcome, Outcome.ISSUED)
        self.cleanup()
        self.verify(issued, wrong(code_of(issued)))
        self.assertEqual(self.control()["high_water_at"], stamp(T0 + timedelta(seconds=100)))
        self.at(161)
        self.assertEqual(self.resend(issued).outcome, Outcome.ISSUED)
        self.assertEqual(self.control()["high_water_at"], stamp(self.wall))
        self.at(400)
        self.cleanup()
        self.assertEqual(self.control()["high_water_at"], stamp(self.wall))
        self.at(450)
        self.verify(issued, wrong(code_of(issued)))  # verification is not an issuance: it does not move the mark
        self.assertEqual(self.control()["high_water_at"], stamp(T0 + timedelta(seconds=400)))

    def test_small_steps_break_the_bound_thirteen_codes_in_one_real_day(self):
        """DC7, the second review's schedule. **This is why the bound of design 3.3 is stated only
        for a clock that is not stepped**: no step here exceeds the 300 s that DC1 and DC2 tolerate,
        none is refused, and 13 codes (65 guesses) of one address are guessable inside one real 24 h."""
        offset = 0
        issued, guessable_at = [], []

        def real(seconds):
            self.at(seconds + offset)

        for seconds in range(5):
            real(seconds)
            issued.append(self.submit())
        offset = -300
        real(899)  # wall 599: all five codes are still accepted
        for result in issued:
            self.assertEqual(self.verify(result, wrong(code_of(result))).outcome, Outcome.WRONG_CODE)
            guessable_at.append(899)
        offset = 0
        real(900)
        offset = 300
        for seconds in (3601, 3602, 3603, 87006, 87007, 87008, 87009, 87010):
            real(seconds)
            issued.append(self.submit())
            self.assertEqual(self.verify(issued[-1], wrong(code_of(issued[-1]))).outcome, Outcome.WRONG_CODE)
            guessable_at.append(seconds)
        self.assertEqual([r.outcome for r in issued], [Outcome.ISSUED] * 13)
        self.assertLess(max(guessable_at) - min(guessable_at), 86400)
        self.assertEqual(len(issued) * registration.MAX_CODE_ATTEMPTS, 65)

    def test_each_small_step_alone(self):
        """DC8."""
        issued = self.submit()
        self.at(600)
        self.assertEqual(self.verify(issued, wrong(code_of(issued))).outcome, Outcome.CODE_EXPIRED)
        self.at(899 - 300)  # set back 300 s: real 899 s, and the code verifies 300 s longer
        self.assertEqual(self.verify(issued, wrong(code_of(issued))).outcome, Outcome.WRONG_CODE)
        for seconds in (1, 2, 3, 4):
            self.at(seconds)
            self.submit()
        self.at(3300)
        self.assertEqual(self.submit().outcome, Outcome.ADDRESS_CAPPED)
        self.at(3301 + 300)  # advanced 300 s: the hourly window ends 300 real seconds early
        self.assertEqual(self.submit().outcome, Outcome.ISSUED)


class CleanupTest(RegistrationTestCase):
    """N1 to N4 (b): logical retention, what a query returns. Nothing here is about bytes on disk."""

    def test_quiet_site(self):
        issued = self.submit()
        self.at(31 * 60)
        result = self.cleanup()  # as the timer would: no request was needed
        self.assertEqual((result.clock_behind, result.scrubbed, result.deleted, result.deleted_sends),
                         (False, 1, 0, 0))
        self.assertScrubbed(issued.pending_id, "expired")
        self.assertEqual(self.pending(issued.pending_id)["binding_hash"], self.binding(issued))

    def test_boundaries_of_scrub_and_deletion(self):
        issued = self.submit()
        self.at(1799)
        self.assertEqual(self.cleanup().scrubbed, 0)
        self.at(1800)
        self.assertEqual(self.cleanup().scrubbed, 1)
        self.at(86399)
        self.assertEqual(self.cleanup().deleted, 0)
        self.at(86400)
        self.assertEqual(self.cleanup().deleted, 1)
        self.assertEqual(self.count("pending_registrations"), 0)
        self.at(2 * 86400 - 1)
        self.assertEqual(self.cleanup().deleted_sends, 0)
        self.assertEqual(self.send(issued.send_id)["outcome"], "started")
        self.at(2 * 86400)
        self.assertEqual(self.cleanup().deleted_sends, 1)
        self.assertEqual(self.count("registration_sends"), 0)
        self.assertEqual(self.control()["high_water_at"], stamp(self.wall))

    def test_touches_no_other_table_and_keeps_pending_rows(self):
        accounts.create_user(self.conn, display_name="Собственик", email="owner@example.bg",
                             password="the-owners-password")
        self.conn.commit()
        old = self.submit("old@example.bg")
        self.verify(old, code_of(old))
        self.at(86400 - 60)
        fresh = self.submit("fresh@example.bg")
        self.at(86400)
        others = [name for (name,) in self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")
            if name not in REGISTRATION_TABLES]
        before = self.digest(tuple(others))
        result = self.cleanup()
        self.assertEqual((result.scrubbed, result.deleted), (0, 1))
        self.assertEqual(self.digest(tuple(others)), before)
        self.assertEqual(self.pending(fresh.pending_id)["state"], "pending")
        self.assertEqual(self.verify(fresh, code_of(fresh)).outcome, Outcome.ACTIVATED)

    def test_clock_set_back_postpones_the_scrub(self):
        """N4 (b): the retention target depends on the clock, and the application does not repair it."""
        issued = self.submit()
        self.at(31 * 60 - 20 * 60)  # real 31 minutes on a clock set back by 20
        result = self.cleanup()
        self.assertEqual((result.clock_behind, result.scrubbed), (False, 0))
        self.assertEqual(self.pending(issued.pending_id)["state"], "pending")
        self.at(1800)
        self.assertEqual(self.cleanup().scrubbed, 1)

    def test_run_that_cannot_take_the_lock_writes_nothing(self):
        """N4 (a), the service half: the failure is the caller's ``BEGIN IMMEDIATE``."""
        issued = self.submit()
        self.at(1800)
        other = self.connect(timeout=0)
        self.conn.execute("BEGIN IMMEDIATE")
        with self.assertRaises(sqlite3.OperationalError):
            other.execute("BEGIN IMMEDIATE")
        self.conn.rollback()
        self.assertEqual(self.pending(issued.pending_id)["state"], "pending")

    def test_deferred_transaction_still_holds_the_write_lock_before_the_clock_is_read(self):
        self.lock_is_held_when_the_clock_is_read()

    def test_the_same_in_wal_mode(self):
        self.assertEqual(self.conn.execute("PRAGMA journal_mode = WAL").fetchone()[0], "wal")
        self.lock_is_held_when_the_clock_is_read()

    def lock_is_held_when_the_clock_is_read(self):
        other = self.connect(timeout=0)

        def clock():
            with self.assertRaises(sqlite3.OperationalError):
                other.execute("BEGIN IMMEDIATE")
            return self.wall

        self.conn.execute("BEGIN")  # deferred: a caller that forgot IMMEDIATE
        registration.cleanup(self.conn, clock=clock)
        self.conn.commit()
        other.execute("BEGIN IMMEDIATE")
        other.rollback()


class PreservationTest(RegistrationTestCase):
    """Existing accounts, ownership and canonical content are not written by any of it."""

    def test_full_lifecycle_changes_only_its_own_tables(self):
        owner = accounts.create_user(self.conn, display_name="Иван Петров", email="ivan@example.bg",
                                     password="a-long-enough-password", role="teacher")
        admin = accounts.create_user(self.conn, display_name="Админ Админов", email="admin@example.bg",
                                     password="a-much-longer-admin-password", role="admin")
        self.conn.commit()
        own = REGISTRATION_TABLES + ("users", "user_profiles", "sqlite_sequence")
        others = tuple(name for (name,) in self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'") if name not in own)
        self.assertIn("quiz_assignments", others)
        self.assertIn("quiz_attempts", others)
        self.assertIn("questions", others)
        before = self.digest(others)
        existing = self.rows("SELECT * FROM users WHERE id IN (?, ?) ORDER BY id", owner, admin)

        activated = self.submit()
        self.verify(activated, code_of(activated))
        for email in ("ivan@example.bg", "admin@example.bg"):
            shadow = self.submit(email)
            self.verify(shadow, "123456")
            self.at(61)
            self.resend(shadow)
        gone = self.submit("gone@example.bg")
        self.cancel(gone)
        self.settle(gone, "not_sent")
        self.at(3 * 86400)
        self.cleanup()

        self.assertEqual(self.digest(others), before)
        self.assertEqual(self.rows("SELECT * FROM users WHERE id IN (?, ?) ORDER BY id", owner, admin), existing)
        self.assertEqual([(u["email"], u["role"]) for u in self.users()],
                         [("ivan@example.bg", "teacher"), ("admin@example.bg", "admin"), (EMAIL, "student")])
        self.assertEqual(self.conn.execute("PRAGMA foreign_key_check").fetchall(), [])


if __name__ == "__main__":
    unittest.main()
