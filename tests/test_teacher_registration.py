"""Registration PR 7: gated teacher registration (design 7.1 and 7.3, decisions D3, D4, D5, D14).

Two layers, both on temporary databases built from migrations and canonical data (none is
``data/questions.db``), with ``InMemoryMailer`` (nothing is sent, no socket is opened) and an
injected registration clock:

* ``TeacherActivationServiceTest`` calls the registration service the way the blueprint does and
  injects failures at every write of an activation (the account, the profile, each subject row,
  the final state), with real SQLite triggers;
* ``TeacherRouteTest`` and the classes after it go through the application: the gate, the form,
  its validation, the journey, dispatch and settlement, and what a registered teacher can then do.
"""

import json
import logging
import re
import sqlite3
from contextlib import closing
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from src import subject_registry
from tests.registration_support import (EMAIL, KEY, REGISTRATION_TABLES, RegistrationTestCase, T0, code_of,
                                        password_hash, student, teacher)
from tests.support import NO_DEADLINE_FORM
from tests.test_account_login import PASSWORD, SAME_ORIGIN
from tests.test_registration_routes import (CROSS_ORIGIN, STUDENT_PASSWORD, RegistrationRouteTestCase,
                                            form as student_form)
from web.services import accounts, profiles
from web.services import registration as registration_service
from web.services.mail import SendOutcome
from web.services.registration import Availability, Outcome

TEACHER_EMAIL = "ivan.petrov@example.bg"
SCHOOL = "СУ „Васил Левски“, Пловдив"
PROFILE_TABLES = REGISTRATION_TABLES + ("users", "user_profiles", "teacher_subjects")


def wrong(code):
    return "000000" if code != "000000" else "111111"


def teacher_form(email=TEACHER_EMAIL, **overrides):
    values = {"first_name": "Иван", "last_name": "Петров", "email": email, "school_name": SCHOOL,
              "subject": ["it", "mathematics"], "password": STUDENT_PASSWORD, "confirm_password": STUDENT_PASSWORD}
    values.update(overrides)
    return values


THREE = [{"subject_id": "it", "label": "Информационни технологии"},
         {"subject_id": "mathematics", "label": "Математика"},
         {"subject_id": None, "label": "Астрономия"}]


# --------------------------------------------------------------------------- the service


class TeacherServiceTestCase(RegistrationTestCase):
    def submit_teacher(self, email=TEACHER_EMAIL, **overrides):
        return self.submit(cleaned=teacher(email, **overrides), teacher_enabled=True)

    def verify_teacher(self, issued, code, *, enabled=True, **kwargs):
        return self.verify(issued, code, teacher_enabled=enabled, **kwargs)

    def account(self, email, role, *, active=True):
        self.conn.execute("BEGIN IMMEDIATE")
        user_id = accounts.create_user(self.conn, display_name="Друг Човек", email=email,
                                       password="another-password", role=role, is_active=active)
        self.conn.commit()
        return user_id

    def user(self, email):
        found = self.rows("SELECT * FROM users WHERE email = ?", email)
        return found[0] if found else None

    def profile_rows(self):
        return (self.rows("SELECT * FROM user_profiles ORDER BY user_id"),
                self.rows("SELECT * FROM teacher_subjects ORDER BY user_id, position"))


class TeacherActivationServiceTest(TeacherServiceTestCase):
    def test_a_correct_code_creates_the_teacher_account_profile_and_subjects_together(self):
        issued = self.submit_teacher(subjects=THREE)
        self.assertEqual(self.pending(issued.pending_id)["account_type"], "teacher")
        result = self.verify_teacher(issued, code_of(issued))
        self.assertEqual(result.outcome, Outcome.ACTIVATED)

        user = self.user(TEACHER_EMAIL)
        self.assertEqual((user["id"], user["role"], user["is_active"]), (result.user_id, "teacher", 1))
        self.assertEqual(user["display_name"], "Иван Петров")
        self.assertIsNotNone(user["email_verified_at"])
        self.assertIsNone(user["profile_required_at"])
        profile_rows, subject_rows = self.profile_rows()
        self.assertEqual(len(profile_rows), 1)
        profile = profile_rows[0]
        self.assertEqual((profile["user_id"], profile["account_type"], profile["first_name"], profile["last_name"],
                          profile["school_name"], profile["grade"], profile["grade_set_at"]),
                         (result.user_id, "teacher", "Иван", "Петров", "СУ „Васил Левски“, Пловдив", None, None))
        self.assertEqual([(row["position"], row["subject_id"], row["label"]) for row in subject_rows],
                         [(1, "it", "Информационни технологии"), (2, "mathematics", "Математика"), (3, None, "Астрономия")])
        self.assertTrue(all(row["user_id"] == result.user_id for row in subject_rows))
        self.assertScrubbed(issued.pending_id, "activated")
        # The account signs in with the exact password that was hashed at submission.
        self.assertEqual(accounts.authenticate(self.conn, TEACHER_EMAIL, "  pupil  passphrase  ")["id"], result.user_id)

    def test_the_subject_labels_come_from_the_registry_at_activation(self):
        issued = self.submit_teacher(subjects=[{"subject_id": "it", "label": "Информационни технологии"}])
        self.verify_teacher(issued, code_of(issued))
        stored = self.rows("SELECT label FROM teacher_subjects")[0]["label"]
        self.assertEqual(stored, subject_registry.get_subject("it").title)

    def test_one_subject_and_each_registered_subject_are_accepted(self):
        for index, subject in enumerate(subject_registry.all_subjects()):
            email = f"t{index}@example.bg"
            issued = self.submit_teacher(email, subjects=[{"subject_id": subject.id, "label": subject.title}])
            self.assertEqual(self.verify_teacher(issued, code_of(issued)).outcome, Outcome.ACTIVATED, subject.id)
        self.assertEqual(self.count("teacher_subjects"), len(subject_registry.all_subjects()))

    def test_the_role_is_the_literal_of_the_account_type_never_submitted_data(self):
        for extra in ({"role": "admin"}, {"is_admin": True}, {"email_verified_at": "2020-01-01 00:00:00"},
                      {"is_active": 0}, {"user_id": 1}):
            with self.subTest(extra=sorted(extra)):
                email = f"x{abs(hash(tuple(extra))) % 10**6}@example.bg"
                issued = self.submit(cleaned={**teacher(email), **extra}, account_type="teacher", teacher_enabled=True)
                self.verify_teacher(issued, code_of(issued))
                user = self.user(email)
                self.assertEqual((user["role"], user["is_active"]), ("teacher", 1))
                self.assertNotEqual(user["email_verified_at"], "2020-01-01 00:00:00")
        # A route that says "admin" (or anything else) is refused before a row exists.
        for account_type in ("admin", "tester", "", None):
            with self.subTest(account_type=account_type), self.assertRaises(ValueError):
                self.tx(registration_service.create_pending, cleaned=teacher("y@example.bg"), account_type=account_type,
                        password_hash=password_hash(), previous_binding=None, secret_key=KEY, clock=self.clock,
                        sender_busy=False, teacher_enabled=True)
        # The cleaned values must agree with the route's literal.
        with self.assertRaises(ValueError):
            self.submit(cleaned={**teacher("z@example.bg"), "account_type": "student"}, account_type="teacher",
                        teacher_enabled=True)
        with self.assertRaises(ValueError):
            self.submit(cleaned={**student("w@example.bg"), "account_type": "teacher"}, account_type="student")
        self.assertEqual(self.users()[-1]["role"], "teacher")

    def test_a_student_row_is_still_a_student_with_the_teacher_gate_on(self):
        issued = self.submit(cleaned=student())
        result = self.verify(issued, code_of(issued), teacher_enabled=True)
        self.assertEqual(result.outcome, Outcome.ACTIVATED)
        user = self.user(EMAIL)
        self.assertEqual(user["role"], "student")
        profile_rows, subject_rows = self.profile_rows()
        self.assertEqual([(row["account_type"], row["grade"], row["school_name"]) for row in profile_rows],
                         [("student", 9, None)])
        self.assertEqual(subject_rows, [])

    def test_a_student_row_does_not_need_the_teacher_gate(self):
        issued = self.submit(cleaned=student())
        self.assertEqual(self.verify(issued, code_of(issued), teacher_enabled=False).outcome, Outcome.ACTIVATED)


class ActivationAtomicityTest(TeacherServiceTestCase):
    """An exception anywhere in an activation leaves nothing of it, and the same code still works."""

    def setUp(self):
        super().setUp()
        self.issued = self.submit_teacher(subjects=THREE)
        self.code = code_of(self.issued)
        self.verify_teacher(self.issued, wrong(self.code))          # one counted attempt, to be preserved
        self.row_before = self.pending(self.issued.pending_id)
        self.before = self.digest(PROFILE_TABLES)

    def assertStillPending(self):
        self.assertEqual(self.digest(PROFILE_TABLES), self.before)
        self.assertEqual(self.pending(self.issued.pending_id), self.row_before)
        self.assertFalse(self.conn.in_transaction)
        self.assertEqual(self.count("users"), 0)

    def trigger(self, name, event, table, when=None):
        condition = f"WHEN {when} " if when else ""
        self.conn.execute(f"CREATE TRIGGER {name} BEFORE {event} ON {table} {condition}"
                          "BEGIN SELECT RAISE(ABORT, 'injected failure'); END")
        self.conn.commit()

    def test_a_failure_at_each_subject_row_leaves_no_account_profile_or_subject(self):
        for position in (1, 2, 3):
            with self.subTest(position=position):
                self.trigger("inject_subject", "INSERT", "teacher_subjects", f"NEW.position = {position}")
                with self.assertRaises(sqlite3.IntegrityError) as caught:
                    self.verify_teacher(self.issued, self.code)
                self.assertIn("injected failure", str(caught.exception))
                self.assertStillPending()
                self.conn.execute("DROP TRIGGER inject_subject")
                self.conn.commit()
        # Nothing was consumed by the failures: the same code activates the whole unit.
        self.assertEqual(self.verify_teacher(self.issued, self.code).outcome, Outcome.ACTIVATED)
        self.assertEqual((self.count("users"), self.count("user_profiles"), self.count("teacher_subjects")), (1, 1, 3))

    def test_a_failure_at_the_account_the_profile_and_the_final_state_leaves_nothing(self):
        cases = [("users", "users", None), ("user_profiles", "user_profiles", None),
                 ("pending_registrations", "pending_registrations", "NEW.state = 'activated'")]
        for name, table, when in cases:
            with self.subTest(table=table):
                event = "UPDATE" if table == "pending_registrations" else "INSERT"
                self.trigger("inject_write", event, table, when)
                with self.assertRaises(sqlite3.IntegrityError):
                    self.verify_teacher(self.issued, self.code)
                self.assertStillPending()
                self.conn.execute("DROP TRIGGER inject_write")
                self.conn.commit()
        self.assertEqual(self.verify_teacher(self.issued, self.code).outcome, Outcome.ACTIVATED)

    def test_a_failure_in_the_service_calls_leaves_nothing(self):
        injections = [
            patch.object(accounts, "create_verified_user", side_effect=sqlite3.OperationalError("disk full")),
            patch.object(profiles, "create_profile", side_effect=sqlite3.OperationalError("disk full")),
            patch.object(profiles, "_insert_subjects", side_effect=RuntimeError("bug")),
            patch.object(registration_service, "_close", side_effect=RuntimeError("bug")),
        ]
        for injection in injections:
            with self.subTest(target=injection.attribute), injection:
                with self.assertRaises((sqlite3.OperationalError, RuntimeError)):
                    self.verify_teacher(self.issued, self.code)
                self.assertStillPending()

    def test_a_subject_row_written_after_the_profile_is_rolled_back_with_it(self):
        real = profiles._insert_subjects
        written = []

        def two_then_fail(conn, user_id, subjects):
            real(conn, user_id, subjects[:2])
            written.append(conn.execute("SELECT COUNT(*) FROM teacher_subjects").fetchone()[0])
            raise RuntimeError("the third write failed")

        with patch.object(profiles, "_insert_subjects", two_then_fail):
            with self.assertRaises(RuntimeError):
                self.verify_teacher(self.issued, self.code)
        self.assertEqual(written, [2])                              # two rows had been written in the transaction
        self.assertStillPending()

    def test_a_subject_the_registry_no_longer_knows_fails_closed(self):
        with patch.object(subject_registry, "get_subject", return_value=None):
            with self.assertRaises(ValueError):
                self.verify_teacher(self.issued, self.code)
        self.assertStillPending()
        self.assertEqual(self.verify_teacher(self.issued, self.code).outcome, Outcome.ACTIVATED)

    def test_stored_subjects_that_are_not_json_fail_closed(self):
        self.conn.execute("UPDATE pending_registrations SET subjects_json = 'not json' WHERE id = ?",
                          (self.issued.pending_id,))
        self.conn.commit()
        self.row_before = self.pending(self.issued.pending_id)
        self.before = self.digest(PROFILE_TABLES)
        with self.assertRaises(ValueError):
            self.verify_teacher(self.issued, self.code)
        self.assertStillPending()

    def test_without_the_creation_ledger_no_teacher_role_is_granted(self):
        self.conn.execute("DROP TABLE assignment_creations")
        self.conn.commit()
        self.before = self.digest(PROFILE_TABLES)
        with self.assertRaises(RuntimeError) as caught:
            self.verify_teacher(self.issued, self.code)
        self.assertNotIn("example", str(caught.exception))
        self.assertStillPending()

    def test_the_ledger_check_does_not_affect_a_student(self):
        self.conn.execute("DROP TABLE assignment_creations")
        self.conn.commit()
        pupil = self.submit(cleaned=student("pupil@example.bg"))
        self.assertEqual(self.verify(pupil, code_of(pupil)).outcome, Outcome.ACTIVATED)


class TeacherAddressTest(TeacherServiceTestCase):
    """Existing addresses: the same answers, one notice, and no existing account is ever changed."""

    def snapshot(self, email):
        row = self.user(email)
        return {key: row[key] for key in ("id", "role", "is_active", "display_name", "password_hash",
                                           "session_key", "email_verified_at")}

    def test_an_address_that_already_has_an_account_is_a_shadow_and_nothing_changes(self):
        for role in ("student", "teacher", "admin"):
            for active in (True, False):
                email = f"{role}{int(active)}@example.bg"
                with self.subTest(role=role, active=active):
                    self.account(email, role, active=active)
                    before = self.snapshot(email)
                    issued = self.submit_teacher(email)
                    self.assertEqual(issued.kind, "notice")
                    self.assertTrue(self.pending(issued.pending_id)["is_shadow"])
                    self.assertIsNone(self.pending(issued.pending_id)["password_hash"])
                    self.assertNotRegex(issued.message.text, r"(?<!\d)\d{6}(?!\d)")  # a notice carries no code
                    for guess in ("000000", "123456"):
                        result = self.verify_teacher(issued, guess)
                        self.assertNotEqual(result.outcome, Outcome.ACTIVATED)
                    self.assertEqual(self.snapshot(email), before)
        self.assertEqual(self.count("user_profiles"), 0)
        self.assertEqual(self.count("teacher_subjects"), 0)

    def test_an_account_created_after_the_submission_wins_and_is_not_changed(self):
        for role in ("student", "teacher", "admin"):
            with self.subTest(role=role):
                email = f"late-{role}@example.bg"
                issued = self.submit_teacher(email)
                self.account(email, role)
                before = self.snapshot(email)
                result = self.verify_teacher(issued, code_of(issued))
                self.assertEqual(result.outcome, Outcome.EMAIL_TAKEN)
                self.assertScrubbed(issued.pending_id, "void")
                self.assertEqual(self.snapshot(email), before)
                self.assertEqual(self.rows("SELECT * FROM user_profiles WHERE user_id = ?", before["id"]), [])
                self.assertEqual(self.rows("SELECT * FROM teacher_subjects WHERE user_id = ?", before["id"]), [])
                self.assertEqual(len([u for u in self.users() if u["email"] == email]), 1)

    def test_two_registrations_of_one_address_make_one_account(self):
        first = self.submit_teacher()
        second = self.submit_teacher()                              # a second browser: a second pending row
        self.assertEqual(self.verify_teacher(first, code_of(first)).outcome, Outcome.ACTIVATED)
        self.assertEqual(self.verify_teacher(second, code_of(second)).outcome, Outcome.EMAIL_TAKEN)
        self.assertEqual(len([u for u in self.users() if u["email"] == TEACHER_EMAIL]), 1)
        self.assertEqual(self.count("teacher_subjects"), 1)

    def test_a_student_and_a_teacher_registration_of_one_address_never_merge(self):
        as_student = self.submit(cleaned=student(TEACHER_EMAIL))
        as_teacher = self.submit_teacher()
        self.assertEqual(self.verify_teacher(as_teacher, code_of(as_teacher)).outcome, Outcome.ACTIVATED)
        self.assertEqual(self.verify_teacher(as_student, code_of(as_student)).outcome, Outcome.EMAIL_TAKEN)
        self.assertEqual(self.user(TEACHER_EMAIL)["role"], "teacher")


class TeacherGateServiceTest(TeacherServiceTestCase):
    def test_a_submission_with_the_gate_off_creates_nothing(self):
        before = self.digest(PROFILE_TABLES)
        result = self.submit(cleaned=teacher(), teacher_enabled=False)
        self.assertEqual((result.outcome, result.reason), (Outcome.UNAVAILABLE, Availability.TEACHER_OFF))
        self.assertEqual(self.digest(PROFILE_TABLES), before)
        self.assertEqual(self.count("registration_sends"), 0)

    def test_a_pending_teacher_row_is_voided_at_verification_and_never_becomes_a_student(self):
        issued = self.submit_teacher()
        result = self.verify_teacher(issued, code_of(issued), enabled=False)
        self.assertEqual((result.outcome, result.reason), (Outcome.UNAVAILABLE, Availability.TEACHER_OFF))
        self.assertScrubbed(issued.pending_id, "void")
        self.assertEqual(self.pending(issued.pending_id)["code_attempts"], 0)
        # Turning the gate on again does not bring it back, and nothing was created in the meantime.
        self.assertEqual(self.verify_teacher(issued, code_of(issued), enabled=True).outcome, Outcome.NOT_PENDING)
        self.assertEqual((self.count("users"), self.count("user_profiles"), self.count("teacher_subjects")), (0, 0, 0))

    def test_a_pending_teacher_row_is_voided_at_resend_when_the_gate_is_off(self):
        issued = self.submit_teacher()
        self.at(61)
        result = self.resend(issued, teacher_enabled=False)
        self.assertEqual((result.outcome, result.reason), (Outcome.UNAVAILABLE, Availability.TEACHER_OFF))
        self.assertScrubbed(issued.pending_id, "void")
        self.assertEqual(len(self.sends()), 1)                      # no second code

    def test_a_teacher_row_is_scrubbed_at_expiry_whatever_the_gate(self):
        issued = self.submit_teacher()
        self.at(1800)
        self.assertEqual(self.cleanup().scrubbed, 1)
        self.assertScrubbed(issued.pending_id, "expired")

    def test_the_gate_off_does_not_stop_the_hold_or_the_clock_checks_from_coming_first(self):
        issued = self.submit_teacher()
        self.set_control(hold="restore")
        result = self.verify_teacher(issued, code_of(issued), enabled=False)
        self.assertEqual(result.outcome, Outcome.UNAVAILABLE)
        self.assertEqual(result.reason, Availability.HOLD)
        self.assertEqual(self.pending(issued.pending_id)["state"], "pending")   # not voided under a hold


class TeacherValidatorTest(TeacherServiceTestCase):
    """The form validator feeds `create_pending`; what it refuses never reaches the service."""

    def validate(self, **overrides):
        from werkzeug.datastructures import MultiDict

        data = teacher_form(**overrides)
        subjects = data.pop("subject")
        items = [(key, value) for key, value in data.items()] + [("subject", item) for item in subjects]
        from web.services import registration_forms

        return registration_forms.validate_teacher(MultiDict(items))

    def test_a_valid_form_gives_the_service_exactly_the_cleaned_fields(self):
        result = self.validate(subject=["it", "other"], subject_other="Астрономия")
        self.assertTrue(result.ok, result.errors)
        issued = self.submit(cleaned=result.values, account_type="teacher", teacher_enabled=True)
        self.assertEqual(json.loads(self.pending(issued.pending_id)["subjects_json"]),
                         [{"subject_id": "it", "label": "Информационни технологии"},
                          {"subject_id": None, "label": "Астрономия"}])

    def test_invalid_input_is_reported_field_by_field(self):
        cases = {
            "first_name": {"first_name": ""}, "last_name": {"last_name": "  "}, "email": {"email": "no-at"},
            "school_name": {"school_name": "ab"}, "subject": {"subject": []},
        }
        for field, change in cases.items():
            with self.subTest(field=field):
                self.assertIn(field, self.validate(**change).errors)
        self.assertIn("confirm_password", self.validate(confirm_password="different").errors)
        self.assertIn("password", self.validate(password="short", confirm_password="short").errors)
        self.assertIn("subject", self.validate(subject=["it", "mathematics", "physics", "chemistry"]).errors)
        self.assertIn("subject", self.validate(subject=["it", "it"]).errors)
        self.assertIn("subject", self.validate(subject=["astrology"]).errors)
        self.assertIn("subject_other", self.validate(subject=["other"], subject_other="").errors)
        self.assertIn("subject_other", self.validate(subject=["other"], subject_other="Физика2").errors)
        self.assertNotIn("subject_other", self.validate(subject=["it"], subject_other="Физика2").errors)


# --------------------------------------------------------------------------- the routes


class TeacherRouteTestCase(RegistrationRouteTestCase):
    """Registration enabled, the teacher gate on unless a test turns it off."""

    def setUp(self):
        super().setUp()
        self.set_gate(True)

    def set_gate(self, value):
        patcher = patch.object(registration_service, "TEACHER_ACTIVATION", value)
        patcher.start()
        self.addCleanup(patcher.stop)

    def submit_teacher(self, email=TEACHER_EMAIL, *, client=None, headers=SAME_ORIGIN, **overrides):
        return (client or self.client).post("/register/teacher", data=teacher_form(email, **overrides),
                                            headers=headers)

    def register_teacher(self, email=TEACHER_EMAIL, *, client=None, **overrides):
        submitted = self.submit_teacher(email, client=client, **overrides)
        self.assertEqual(submitted.status_code, 303, self.body(submitted))
        return self.verify(self.last_code(), client=client)

    def profile_state(self):
        return (self.rows("SELECT * FROM users"), self.rows("SELECT * FROM user_profiles"),
                self.rows("SELECT * FROM teacher_subjects"))


class TeacherGateRouteTest(TeacherRouteTestCase):
    def test_with_the_gate_on_the_chooser_offers_both_and_the_form_renders(self):
        chooser = self.body(self.client.get("/register?next=/account/attempts"))
        self.assertIn("Ученик съм", chooser)
        self.assertIn("Учител съм", chooser)
        self.assertIn('href="/register/teacher?next=/account/attempts"', chooser)
        page = self.client.get("/register/teacher?next=/account/attempts")
        self.assertEqual(page.status_code, 200)
        html_text = self.body(page)
        for name in ("first_name", "last_name", "email", "school_name", "password", "confirm_password"):
            self.assertRegex(html_text, rf'<label for="{name}">[^<]+</label>\s*<input id="{name}" name="{name}"')
        for subject in subject_registry.all_subjects():
            self.assertIn(f'name="subject" value="{subject.id}"', html_text)
            self.assertIn(subject.title, html_text)
        self.assertIn('name="subject" value="other"', html_text)
        self.assertIn("Друг предмет", html_text)
        self.assertIn('name="subject_other"', html_text)
        self.assertIn('name="next" value="/account/attempts"', html_text)
        self.assertIn("Име и населено място", html_text)
        self.assertIn('action="/register/teacher"', html_text)
        self.assertNotIn('name="grade"', html_text)
        # No new privacy or consent claim is made by this form.
        for word in ("съгласие", "лични данни", "GDPR", "поверителност", "проверка на учител"):
            self.assertNotIn(word, html_text)
        self.assertEqual(page.headers["Cache-Control"], "no-store")
        self.assertEqual(page.headers["Referrer-Policy"], "no-referrer")

    def test_with_the_gate_off_everything_is_the_generic_404_and_nothing_links_to_it(self):
        self.set_gate(False)
        self.assertEqual(self.client.get("/register/teacher").status_code, 404)
        self.assertEqual(self.client.post("/register/teacher", data=teacher_form(), headers=SAME_ORIGIN).status_code, 404)
        self.assertNotIn("/register/teacher", self.body(self.client.get("/register")))
        self.assertNotIn("Учител съм", self.body(self.client.get("/register")))
        self.assertEqual(self.pending(), [])
        self.assertEqual(self.mailer.messages, [])
        self.assertEqual(self.sends(), [])

    def test_the_gate_is_only_the_value_true(self):
        for value in (None, 0, 1, "1", "yes", [1]):
            with self.subTest(value=value), patch.object(registration_service, "TEACHER_ACTIVATION", value):
                self.assertEqual(self.client.get("/register/teacher").status_code, 404)

    def test_registration_itself_off_is_a_404_whatever_the_teacher_gate_says(self):
        self.app.config.pop("REGISTRATION", None)
        self.assertEqual(self.client.get("/register/teacher").status_code, 404)

    def test_cross_origin_posts_are_refused_before_the_gate(self):
        for gate in (True, False):
            self.set_gate(gate)
            response = self.client.post("/register/teacher", data=teacher_form(), headers=CROSS_ORIGIN)
            self.assertEqual(response.status_code, 403, gate)
        self.assertEqual((self.pending(), self.mailer.messages), ([], []))

    def test_a_signed_in_account_is_sent_home_and_starts_nothing(self):
        account = self.make("teacher")
        self.sign_in(account)
        before = self.digest()
        self.assertEqual(self.client.get("/register/teacher").headers["Location"], "/teacher/new")
        response = self.client.post("/register/teacher", data=teacher_form(), headers=SAME_ORIGIN)
        self.assertEqual((response.status_code, response.headers["Location"]), (303, "/teacher/new"))
        self.assertEqual(self.digest(), before)
        self.assertEqual(self.mailer.messages, [])

    def test_a_forged_account_type_role_or_grade_is_ignored_and_the_route_decides(self):
        response = self.submit_teacher(account_type="student", role="admin", is_admin="1", grade="9",
                                       email_verified_at="2020-01-01 00:00:00")
        self.assertEqual(response.status_code, 303)
        row = self.pending()[0]
        self.assertEqual((row["account_type"], row["grade"]), ("teacher", None))
        verified = self.verify(self.last_code())
        self.assertEqual(verified.status_code, 303)
        user = self.users_with(TEACHER_EMAIL)[0]
        self.assertEqual((user["role"], user["is_active"]), ("teacher", 1))
        self.assertNotEqual(user["email_verified_at"], "2020-01-01 00:00:00")
        # The student form likewise: a posted school and role change nothing there.
        self.client = self.app.test_client()
        self.submit("child@example.bg", role="teacher", school_name=SCHOOL, subject="it")
        self.assertEqual(self.pending()[-1]["account_type"], "student")
        self.assertIsNone(self.pending()[-1]["school_name"])


class TeacherSubmissionRouteTest(TeacherRouteTestCase):
    def test_a_valid_submission_stores_a_pending_teacher_row_and_sends_one_code(self):
        response = self.submit_teacher(subject=["it", "other"], subject_other="Астрономия")
        self.assertEqual((response.status_code, response.headers["Location"]), (303, "/register/verify"))
        row = self.pending()[0]
        self.assertEqual((row["account_type"], row["state"], row["is_shadow"], row["school_name"], row["grade"]),
                         ("teacher", "pending", 0, SCHOOL, None))
        self.assertEqual(json.loads(row["subjects_json"]),
                         [{"subject_id": "it", "label": "Информационни технологии"},
                          {"subject_id": None, "label": "Астрономия"}])
        self.assertEqual(self.users_with(TEACHER_EMAIL), [])        # a pending row grants nothing
        self.assertEqual(self.rows("SELECT * FROM user_profiles"), [])
        self.assertEqual(len(self.mailer.messages), 1)
        self.assertEqual(self.mailer.messages[0].to, TEACHER_EMAIL)
        self.assertEqual([send["outcome"] for send in self.sends()], ["accepted"])
        self.assertIsNotNone(self.slot())
        self.assertNoAccountSession()

    def test_each_invalid_field_is_reported_in_bulgarian_and_nothing_is_created(self):
        cases = {
            "first_name": ({"first_name": ""}, "Въведи име."),
            "last_name": ({"last_name": "Иванов1"}, "Името може да съдържа само"),
            "email": ({"email": "not-an-address"}, "Невалиден имейл."),
            "school_name": ({"school_name": "x"}, "Въведи име на училището"),
            "subject": ({"subject": []}, "Избери поне един предмет."),
            "confirm_password": ({"confirm_password": "other"}, "Паролите не съвпадат."),
        }
        for field, (change, message) in cases.items():
            with self.subTest(field=field):
                before = self.digest()
                response = self.submit_teacher(**change)
                page = self.body(response)
                self.assertEqual(response.status_code, 200)
                self.assertIn(message, page)
                self.assertIn(f'id="{field}-error"', page)
                self.assertEqual(self.digest(), before)
                self.assertEqual(self.mailer.messages, [])
                self.assertNotIn("Set-Cookie", response.headers)
        self.assertEqual(self.pending(), [])

    def test_subject_rules(self):
        cases = [
            (["it", "mathematics", "physics", "chemistry"], "", "Избери най-много 3 предмета."),
            (["it", "it"], "", "Избери предмети от списъка."),
            (["astrology"], "", "Избери предмети от списъка."),
            (["other"], "", "Въведи името на предмета."),
            (["other"], "Физика2", "Въведи името на предмета."),
            (["other"], "x", "Въведи името на предмета."),
        ]
        for subjects, other, message in cases:
            with self.subTest(subjects=subjects, other=other):
                response = self.submit_teacher(subject=subjects, subject_other=other)
                self.assertEqual(response.status_code, 200)
                self.assertIn(message, self.body(response))
        self.assertEqual((self.pending(), self.mailer.messages), ([], []))

    def test_subject_other_text_without_other_selected_is_ignored_not_stored_and_not_an_error(self):
        response = self.submit_teacher(subject=["it"], subject_other="Физика2")
        self.assertEqual(response.status_code, 303)
        self.assertEqual(json.loads(self.pending()[0]["subjects_json"]),
                         [{"subject_id": "it", "label": "Информационни технологии"}])
        self.assertNotIn("Физика2", json.dumps(self.rows("SELECT * FROM pending_registrations")))

    def test_the_form_is_shown_again_with_what_was_typed_but_never_a_password(self):
        response = self.submit_teacher(first_name="Иван", email="bad", subject=["mathematics", "other"],
                                       subject_other="Астрономия")
        page = self.body(response)
        self.assertIn('value="Иван"', page)
        self.assertIn(f'value="{SCHOOL}"', page)
        self.assertRegex(page, r'value="mathematics" checked')
        self.assertRegex(page, r'value="other" checked')
        self.assertIn('value="Астрономия"', page)
        self.assertNotIn("passphrase", page)
        self.assertNotRegex(page, r'type="password"[^>]*value=')

    def test_the_markup_is_escaped_and_the_fields_are_accessible(self):
        response = self.submit_teacher(first_name='<script>alert(1)</script>', school_name='"><<')
        page = self.body(response)
        self.assertNotIn("<script>alert(1)</script>", page)
        self.assertNotIn('"><<', page)
        self.assertIn("&#34;&gt;&lt;&lt;", page)
        self.assertIn("&lt;script&gt;", page)
        for name in ("first_name", "school_name"):
            tag = re.search(rf'<input id="{name}"[^>]*>', page).group(0)
            self.assertIn('aria-invalid="true"', tag)
            self.assertRegex(tag, rf'aria-describedby="[^"]*\b{name}-error\b')
        self.assertRegex(page, r'<fieldset class="profile-subjects"')
        self.assertRegex(page, r"<legend>Предмети, по които преподаваш</legend>")

    def test_the_subject_error_is_tied_to_its_fieldset_and_other_name_field(self):
        page = self.body(self.submit_teacher(subject=[]))
        self.assertRegex(page, r'<fieldset class="profile-subjects" aria-describedby="subject-error"')
        self.assertIn('id="subject-error"', page)
        page = self.body(self.submit_teacher(subject=["other"], subject_other=""))
        self.assertRegex(page, r'id="subject_other"[^>]*aria-invalid="true"[^>]*aria-describedby="subject_other-error"')

    def test_next_is_kept_only_when_accepted_and_the_login_link_carries_it(self):
        response = self.submit_teacher(next="/quiz/s/" + "A" * 43)
        self.assertEqual(response.status_code, 303)
        self.assertEqual(self.slot()["next"], "/quiz/s/" + "A" * 43)
        verified = self.verify(self.last_code())
        self.assertEqual(verified.headers["Location"],
                         "/login?next=/quiz/s/" + "A" * 43 + "&registration=activated")
        refused = self.submit_teacher("other@example.bg", next="//evil.example/x", subject=[])
        self.assertNotIn("evil.example", self.body(refused))
        self.assertIn('name="next" value=""', self.body(refused))

    def test_an_address_that_has_an_account_gets_the_same_page_and_a_notice_only(self):
        admin = self.make("admin", email="boss@example.bg")
        before = self.users_with("boss@example.bg")
        response = self.submit_teacher("boss@example.bg")
        self.assertEqual((response.status_code, response.headers["Location"]), (303, "/register/verify"))
        self.assertTrue(self.pending()[0]["is_shadow"])
        self.assertEqual(len(self.mailer.messages), 1)
        self.assertNotRegex(self.mailer.messages[0].text, r"(?<!\d)\d{6}(?!\d)")
        for code in ("000000", "123456"):
            self.verify(code)
        self.assertEqual(self.users_with("boss@example.bg"), before)   # role admin, same hash, same key
        self.assertEqual(self.users_with("boss@example.bg")[0]["role"], "admin")
        self.assertEqual(self.rows("SELECT * FROM user_profiles"), [])
        self.assertEqual(admin["role"], "admin")


class TeacherJourneyRouteTest(TeacherRouteTestCase):
    def test_a_teacher_registers_verifies_and_then_signs_in_with_the_exact_password(self):
        verified = self.register_teacher(subject=["it", "mathematics", "other"], subject_other="Астрономия")
        self.assertEqual((verified.status_code, verified.headers["Location"]), (303, "/login?registration=activated"))
        self.assertNoAccountSession()                                 # verification signs nobody in (D7)
        users, profiles_rows, subject_rows = self.profile_state()
        self.assertEqual([(u["email"], u["role"], u["is_active"]) for u in users], [(TEACHER_EMAIL, "teacher", 1)])
        self.assertEqual([(p["account_type"], p["school_name"], p["grade"]) for p in profiles_rows],
                         [("teacher", SCHOOL, None)])
        self.assertEqual([(s["position"], s["subject_id"], s["label"]) for s in subject_rows],
                         [(1, "it", "Информационни технологии"), (2, "mathematics", "Математика"),
                          (3, None, "Астрономия")])
        self.assertEqual(self.pending()[0]["state"], "activated")
        for column in ("email", "first_name", "school_name", "subjects_json", "password_hash", "code_hash"):
            self.assertIsNone(self.pending()[0][column], column)
        self.assertIsNone(self.slot())

        wrong_password = self.login(TEACHER_EMAIL, "wrong password here")
        self.assertIn("Грешен имейл или парола.", self.body(wrong_password))
        self.assertNoAccountSession()
        signed_in = self.login(TEACHER_EMAIL, STUDENT_PASSWORD)
        self.assertEqual((signed_in.status_code, signed_in.headers["Location"]), (302, "/teacher/new"))

    def test_the_new_teacher_has_exactly_the_teacher_role_and_nothing_more(self):
        self.register_teacher()
        self.login(TEACHER_EMAIL, STUDENT_PASSWORD)
        self.assertEqual(self.client.get("/teacher/new").status_code, 200)
        self.assertEqual(self.client.get("/teacher/assignments").status_code, 200)
        for path in ("/teacher", "/teacher/dzi-training", "/dzi"):
            self.assertEqual(self.client.get(path).status_code, 403, path)
        self.assertEqual(self.client.get("/account").status_code, 200)

    def test_a_new_teacher_owns_only_what_they_create_and_the_quotas_apply_to_them(self):
        self.register_teacher()
        self.login(TEACHER_EMAIL, STUDENT_PASSWORD)
        section = self.eligible_section()
        created = []
        for _ in range(10):
            response = self.client.post("/teacher/new", data={**NO_DEADLINE_FORM, "section_id": section,
                                                              "question_count": 2, "subject": "it"}, headers=SAME_ORIGIN)
            self.assertEqual(response.status_code, 302, self.body(response)[:200])
            created.append(int(response.headers["Location"].rsplit("/", 1)[1]))
        refused = self.client.post("/teacher/new", data={**NO_DEADLINE_FORM, "section_id": section,
                                                         "question_count": 2, "subject": "it"}, headers=SAME_ORIGIN)
        self.assertEqual(refused.status_code, 409)
        self.assertEqual(self.rows("SELECT COUNT(*) AS n FROM assignment_creations")[0]["n"], 10)
        # Another teacher's assignment is a 404 for the new account.
        other = self.make("teacher")
        other_id = self.seed_assignment(other, section)
        self.assertEqual(self.client.get(f"/teacher/assignment/{other_id}").status_code, 404)
        self.assertEqual(self.client.get(f"/teacher/assignment/{other_id}/results").status_code, 404)
        self.assertEqual(self.client.get(f"/teacher/assignment/{created[0]}").status_code, 200)

    def eligible_section(self):
        from tests.support import web_app
        from web.services import quiz

        with closing(web_app.quiz_db()) as conn:
            for row in conn.execute("SELECT id FROM curriculum_sections ORDER BY id"):
                if quiz.quiz_section_question_ids(conn, row["id"]):
                    return row["id"]
        raise AssertionError("no eligible section")

    def seed_assignment(self, account, section):
        from tests.support import web_app
        from web.access import account_principal
        from web.services import quiz

        with closing(web_app.quiz_db()) as conn:
            assignment_id = quiz.quiz_create_assignment(
                conn, principal=account_principal(account["id"], "teacher", "Друг"), section_id=section,
                title_bg="Чужд", question_count=1)
            conn.commit()
        return assignment_id

    def test_verification_is_once_and_a_repeat_writes_nothing(self):
        self.register_teacher()
        before = self.digest()
        again = self.verify(self.last_code())              # the binding was spent: no usable registration
        self.assertEqual(again.status_code, 200)
        self.assertIn("Регистрацията е изтекла", self.body(again))
        self.assertEqual(self.digest(), before)
        self.assertEqual(len(self.users_with(TEACHER_EMAIL)), 1)

    def test_a_wrong_code_is_counted_and_creates_nothing(self):
        self.submit_teacher()
        code = self.last_code()
        response = self.verify(wrong(code))
        self.assertEqual(response.status_code, 200)
        self.assertIn("Грешен или изтекъл код.", self.body(response))
        self.assertEqual(self.pending()[0]["code_attempts"], 1)
        self.assertEqual(self.profile_state(), ([], [], []))
        self.assertEqual(self.verify(code).status_code, 303)

    def test_a_student_journey_with_the_teacher_gate_on_is_unchanged(self):
        verified = self.register("child@example.bg")
        self.assertEqual(verified.headers["Location"], "/login?registration=activated")
        user = self.users_with("child@example.bg")[0]
        self.assertEqual(user["role"], "student")
        self.assertEqual(self.rows("SELECT account_type, grade, school_name FROM user_profiles"),
                         [{"account_type": "student", "grade": 9, "school_name": None}])
        self.assertEqual(self.rows("SELECT * FROM teacher_subjects"), [])

    def test_the_two_forms_share_one_slot_and_a_new_submission_voids_the_old_pending_row(self):
        self.submit_teacher("first@example.bg")
        self.submit("second@example.bg")
        states = {row["account_type"]: row["state"] for row in self.pending()}
        self.assertEqual(states, {"teacher": "void", "student": "pending"})
        self.verify(self.last_code())
        self.assertEqual([u["role"] for u in self.rows("SELECT role FROM users")], ["student"])

    def test_resend_for_a_teacher_issues_one_new_code_and_sends_it_once(self):
        self.submit_teacher()
        first = self.last_code()
        self.at(61)
        response = self.resend()
        self.assertEqual(response.headers["Location"], "/register/verify?resent=1")
        self.assertEqual(len(self.mailer.messages), 2)
        self.assertEqual([send["outcome"] for send in self.sends()], ["accepted", "accepted"])
        self.assertEqual(self.verify(first).status_code, 200)         # replaced: the old code no longer works
        self.assertEqual(self.verify(self.last_code()).headers["Location"], "/login?registration=activated")
        self.assertEqual(self.users_with(TEACHER_EMAIL)[0]["role"], "teacher")


class TeacherGateOffPendingRouteTest(TeacherRouteTestCase):
    def test_a_pending_teacher_is_voided_at_verification_and_is_never_a_student(self):
        self.submit_teacher()
        code = self.last_code()
        self.set_gate(False)
        response = self.verify(code)
        self.assertEqual(response.status_code, 503)
        self.assertIn("Регистрацията временно не е достъпна.", self.body(response))
        self.assertIsNone(self.slot())                                # the binding is spent
        row = self.pending()[0]
        self.assertEqual(row["state"], "void")
        self.assertIsNone(row["email"])
        self.assertEqual(self.profile_state(), ([], [], []))
        self.set_gate(True)                                           # turning the gate on again changes nothing
        self.assertEqual(self.verify(code).status_code, 200)
        self.assertEqual(self.profile_state(), ([], [], []))
        self.assertEqual(self.users_with(TEACHER_EMAIL), [])

    def test_a_pending_teacher_is_voided_at_resend_too(self):
        self.submit_teacher()
        self.at(61)
        self.set_gate(False)
        response = self.resend()
        self.assertEqual(response.status_code, 503)
        self.assertEqual(self.pending()[0]["state"], "void")
        self.assertEqual(len(self.mailer.messages), 1)               # no second code
        self.assertEqual(self.profile_state(), ([], [], []))

    def test_a_pending_student_is_unaffected_by_the_teacher_gate(self):
        self.submit("child@example.bg")
        self.set_gate(False)
        self.assertEqual(self.verify(self.last_code()).headers["Location"], "/login?registration=activated")
        self.assertEqual(self.users_with("child@example.bg")[0]["role"], "student")

    def test_cancel_still_scrubs_a_teacher_registration_with_the_gate_off(self):
        self.submit_teacher()
        self.set_gate(False)
        response = self.cancel()
        self.assertEqual(response.status_code, 303)
        self.assertEqual(self.pending()[0]["state"], "void")
        self.assertIsNone(self.pending()[0]["school_name"])


class TeacherActivationFailureRouteTest(TeacherRouteTestCase):
    def test_a_failure_at_each_subject_write_is_the_generic_failure_and_nothing_is_created(self):
        self.submit_teacher(subject=["it", "mathematics", "other"], subject_other="Астрономия")
        code = self.last_code()
        before = self.digest()
        for position in (1, 2, 3):
            with self.subTest(position=position):
                with closing(sqlite3.connect(self.db_path)) as conn:
                    conn.execute("CREATE TRIGGER inject_subject BEFORE INSERT ON teacher_subjects "
                                 f"WHEN NEW.position = {position} BEGIN SELECT RAISE(ABORT, 'injected'); END")
                    conn.commit()
                with self.assertLogs("learnpilot.registration", logging.INFO) as logs:
                    response = self.verify(code)
                self.assertEqual(response.status_code, 500)
                page = self.body(response)
                self.assertIn("Нещо се обърка и нищо не беше записано.", page)
                self.assertNotIn("потвърден", page)
                self.assertNotIn("Вече има профил", page)
                self.assertNoAccountSession()
                self.assertIsNotNone(self.slot())
                self.assertEqual(self.digest(), before)
                row = self.pending()[0]
                self.assertEqual((row["state"], row["code_attempts"]), ("pending", 0))
                self.assertIsNotNone(row["code_hash"])
                self.assertIn("activation_failed error=IntegrityError", "\n".join(logs.output))
                with closing(sqlite3.connect(self.db_path)) as conn:
                    conn.execute("DROP TRIGGER inject_subject")
                    conn.commit()
        retried = self.verify(code)
        self.assertEqual(retried.headers["Location"], "/login?registration=activated")
        self.assertEqual(len(self.rows("SELECT * FROM teacher_subjects")), 3)

    def test_a_failed_commit_claims_nothing_keeps_the_slot_and_a_retry_activates(self):
        self.submit_teacher()
        code = self.last_code()
        state = self.flaky(1)
        response = self.verify(code)
        self.assertEqual(response.status_code, 500)
        self.assertNotIn("потвърден", self.body(response))
        self.assertEqual(self.profile_state(), ([], [], []))
        self.assertIsNotNone(self.slot())
        state["fail"].clear()
        self.assertEqual(self.verify(code).headers["Location"], "/login?registration=activated")
        self.assertEqual(self.users_with(TEACHER_EMAIL)[0]["role"], "teacher")

    def test_an_address_taken_meanwhile_is_reported_as_taken_and_no_role_is_overwritten(self):
        self.submit_teacher("race@example.bg")
        code = self.last_code()
        existing = self.make("student", email="race@example.bg")
        response = self.verify(code)
        self.assertEqual(response.headers["Location"], "/login?registration=email_taken")
        self.assertEqual([(u["id"], u["role"]) for u in self.users_with("race@example.bg")],
                         [(existing["id"], "student")])
        self.assertEqual(self.rows("SELECT * FROM user_profiles"), [])
        self.assertEqual(self.rows("SELECT * FROM teacher_subjects"), [])


class TeacherDispatchRouteTest(TeacherRouteTestCase):
    """Commit before dispatch, one dispatch per issuing handler result, no retries, matching settlement."""

    def test_a_submission_dispatches_once_and_settles_the_same_issuance(self):
        dispatched = self.spy(registration_service, "dispatch")
        settled = self.spy(registration_service, "settle_send")
        order = []
        real_send = self.mailer.send
        self.mailer.send = lambda message: (order.append(("send", self.sends()[0]["outcome"])), real_send(message))[1]
        self.assertEqual(self.submit_teacher().status_code, 303)
        self.assertEqual(len(dispatched), 1)
        issued = dispatched[0][0][2]
        self.assertEqual([call[0][1:3] for call in settled], [(issued.send_id, issued.issuance_id)])
        self.assertEqual(order, [("send", "started")])                 # the issuance was committed before the send
        self.assertEqual(self.sends()[0]["outcome"], "accepted")
        self.assertEqual(len(self.mailer.messages), 1)

    def test_every_issuance_is_dispatched_exactly_once(self):
        dispatched = self.spy(registration_service, "dispatch")
        self.submit_teacher()
        self.resend()                                                  # too early: no issuance, no dispatch
        self.at(61)
        self.resend()                                                  # a new generation
        self.resend()                                                  # too early again
        identities = [call[0][2].issuance_id for call in dispatched]
        self.assertEqual(len(identities), 2)
        self.assertEqual(sorted(identities), sorted(row["issuance_id"] for row in self.sends()))
        self.assertEqual(len(self.mailer.messages), 2)

    def test_a_mailer_exception_is_unknown_and_never_retried(self):
        def boom(message):
            raise RuntimeError("cannot reach the relay")

        self.mailer.outcome = boom
        self.assertEqual(self.submit_teacher().status_code, 303)
        self.assertEqual(self.sends()[0]["outcome"], "unknown")
        self.assertEqual(len(self.mailer.messages), 1)

    def test_not_sent_retires_the_code_and_a_resend_may_follow_at_once(self):
        self.mailer.outcome = SendOutcome.NOT_SENT
        self.submit_teacher()
        self.assertEqual(self.sends()[0]["outcome"], "not_sent")
        self.mailer.outcome = SendOutcome.ACCEPTED
        response = self.resend()                                       # allowed before the 60 s: the first was not sent
        self.assertEqual(response.headers["Location"], "/register/verify?resent=1")
        self.assertEqual(self.verify(self.last_code()).headers["Location"], "/login?registration=activated")

    def test_not_sendable_is_never_settled(self):
        settled = self.spy(registration_service, "settle_send")
        with patch.object(registration_service, "_is_committed_issuance", return_value=False), \
                self.assertLogs("learnpilot.registration", logging.WARNING) as logs:
            response = self.submit_teacher()
        self.assertEqual(response.status_code, 303)
        self.assertEqual((settled, self.mailer.messages), ([], []))
        self.assertEqual(self.sends()[0]["outcome"], "started")
        self.assertIn("dispatch_refused pending=1 send=1", "\n".join(logs.output))

    def test_a_failed_issuance_commit_sends_nothing_and_claims_nothing(self):
        dispatched = self.spy(registration_service, "dispatch")
        self.flaky(1)
        with self.assertLogs("learnpilot.registration", logging.INFO):
            response = self.submit_teacher()
        self.assertEqual(response.status_code, 500)
        self.assertEqual((dispatched, self.mailer.messages, self.pending(), self.sends()), ([], [], [], []))
        self.assertIsNone(self.slot())

    def test_a_failed_settlement_leaves_the_row_started_and_the_flow_normal(self):
        self.flaky(2)
        with self.assertLogs("learnpilot.registration", logging.WARNING) as logs:
            response = self.submit_teacher()
        self.assertEqual((response.status_code, response.headers["Location"]), (303, "/register/verify"))
        self.assertEqual(self.sends()[0]["outcome"], "started")
        self.assertIn("settlement_failed send=1", "\n".join(logs.output))
        self.assertEqual(len(self.mailer.messages), 1)
        self.assertEqual(self.verify(self.last_code()).status_code, 303)

    def test_logs_hold_ids_only(self):
        with self.assertLogs("learnpilot.registration", logging.INFO) as logs:
            self.register_teacher()
        text = "\n".join(logs.output)
        for secret in (TEACHER_EMAIL, "Иван", "Петров", SCHOOL, "passphrase", self.pending()[0]["binding_hash"]):
            self.assertNotIn(secret, text)
        self.assertIn("activated pending=1 user=1", text)


class TeacherEnvironmentTest(TeacherRouteTestCase):
    def test_the_build_gates_are_off_in_the_source_and_nothing_else_assigns_them(self):
        # The patch of setUp is the only thing that turns the teacher gate on in this module.
        root = Path(registration_service.__file__).resolve().parents[2]
        source = (root / "web/services/registration.py").read_text(encoding="utf-8")
        self.assertIn("\nPRODUCTION_ACTIVATION = False\n", source)
        self.assertIn("\nTEACHER_ACTIVATION = False\n", source)
        self.assertIn("\nPRODUCTION_ACTIVATION = False\n", (root / "web/services/profiles.py").read_text(encoding="utf-8"))
        self.assertIn("\nPRODUCTION_ACTIVATION = False\n", (root / "web/services/google_oidc.py").read_text(encoding="utf-8"))
        for path in sorted((root / "web").rglob("*.py")):
            text = path.read_text(encoding="utf-8")
            if path.name != "registration.py":
                self.assertNotRegex(text, r"^\s*TEACHER_ACTIVATION\s*=", path.name)


# --------------------------------------------------------------------------- restart navigation


class RestartNavigationTest(TeacherRouteTestCase):
    """Review finding 3: a restart never lands on a form the person did not start.

    After a cancellation, an expiry, a missing or ended binding, an unavailable service or a failure the
    person is sent to the chooser, with the validated ``next``; the binding and lifecycle rules are untouched.
    """

    NEXT = "/account/attempts"

    def begin(self, kind, **extra):
        if kind == "student":
            return self.submit(**extra)
        return self.submit_teacher(**extra)

    def restart_link(self, response):
        page = response.get_data(as_text=True)
        found = re.search(r'<a class="button" href="([^"]*)">Започни регистрацията отначало</a>', page)
        self.assertIsNotNone(found, "the restart link is on the page")
        return found.group(1).replace("&amp;", "&")

    def assertRestartsAtChooser(self, response, next_target=None):
        link = self.restart_link(response)
        self.assertEqual(link, "/register" + (f"?next={next_target}" if next_target else ""))
        self.assertNotIn("/register/student", link)
        self.assertNotIn("/register/teacher", link)

    def test_cancel_returns_to_the_chooser_for_both_flows_and_keeps_the_rules(self):
        for kind in ("student", "teacher"):
            with self.subTest(kind=kind):
                self.client = self.app.test_client()
                email = f"{kind}.cancel@example.bg"
                self.assertEqual(self.begin(kind, email=email, next=self.NEXT).status_code, 303)
                cancelled = self.cancel()
                self.assertEqual((cancelled.status_code, cancelled.headers["Location"]), (303, f"/register?next={self.NEXT}"))
                self.assertIsNone(self.slot())
                row = [r for r in self.pending() if r["id"] == max(p["id"] for p in self.pending())][0]
                self.assertEqual(row["state"], "void")                       # the lifecycle rule is unchanged
                self.assertIsNone(row["email"])
                self.assertEqual(self.users_with(email), [])
                # Nothing is left to cancel: still the chooser, with nothing invented.
                self.assertEqual(self.cancel().headers["Location"], "/register")

    def test_the_chooser_after_a_restart_offers_what_the_gate_allows(self):
        self.begin("teacher", next=self.NEXT)
        page = self.body(self.client.get(self.cancel().headers["Location"]))
        self.assertIn(f'href="/register/student?next={self.NEXT}"', page)
        self.assertIn(f'href="/register/teacher?next={self.NEXT}"', page)
        self.set_gate(False)
        page = self.body(self.client.get(f"/register?next={self.NEXT}"))
        self.assertIn("Ученик съм", page)
        self.assertNotIn("/register/teacher", page)

    def test_an_expired_registration_restarts_at_the_chooser_for_both_flows(self):
        for kind in ("student", "teacher"):
            with self.subTest(kind=kind):
                self.client = self.app.test_client()
                self.begin(kind, email=f"{kind}.expired@example.bg", next=self.NEXT)
                self.at(self.wall_seconds() + 1801)
                response = self.verify("000000")
                self.assertEqual(response.status_code, 200)
                self.assertIn("Регистрацията е изтекла", self.body(response))
                self.assertRestartsAtChooser(response, self.NEXT)
                self.assertIsNone(self.slot())

    def wall_seconds(self):
        return int((self.wall - T0).total_seconds())

    def test_a_registration_cancelled_in_another_tab_restarts_at_the_chooser(self):
        for kind in ("student", "teacher"):
            with self.subTest(kind=kind):
                self.client = self.app.test_client()
                self.begin(kind, email=f"{kind}.tab@example.bg", next=self.NEXT)
                copy = self.app.test_client()
                self.set_slot(dict(self.slot()), client=copy)
                self.cancel(client=copy)
                response = self.verify("000000")
                self.assertRestartsAtChooser(response, self.NEXT)

    def test_a_missing_binding_restarts_at_the_chooser_with_no_next(self):
        fresh = self.app.test_client()
        for response in (self.verify("123456", client=fresh), self.resend(client=fresh)):
            self.assertEqual(response.status_code, 200)
            self.assertRestartsAtChooser(response)

    def test_a_replaced_registration_restarts_at_the_chooser(self):
        for kind in ("student", "teacher"):
            with self.subTest(kind=kind):
                self.client = self.app.test_client()
                self.begin(kind, email=f"{kind}.old@example.bg", next=self.NEXT)
                copy = self.app.test_client()
                self.set_slot(dict(self.slot()), client=copy)
                self.begin(kind, email=f"{kind}.new@example.bg", next=self.NEXT)    # voids the first browser's row
                response = self.verify("000000", client=copy)
                self.assertRestartsAtChooser(response, self.NEXT)

    def test_the_gate_off_page_for_a_teacher_registration_restarts_at_the_chooser(self):
        self.submit_teacher(next=self.NEXT)
        self.set_gate(False)
        response = self.verify(self.last_code())
        self.assertEqual(response.status_code, 503)
        self.assertRestartsAtChooser(response, self.NEXT)                    # the validated target survives the spent slot
        self.assertIsNone(self.slot())
        self.assertNotIn("/register/teacher", self.body(self.client.get(self.restart_link(response))))

    def test_the_unavailable_and_failure_pages_restart_at_the_chooser(self):
        self.set_control(hold="restore")
        response = self.submit_teacher()
        self.assertEqual(response.status_code, 503)
        self.assertRestartsAtChooser(response)
        self.set_control()
        self.flaky(1)
        failed = self.submit_teacher("fail@example.bg")
        self.assertEqual(failed.status_code, 500)
        self.assertRestartsAtChooser(failed)

    # ---- review of 3537f4f, finding 2: the unavailable, failure and gate-off pages keep the validated target

    KINDS = ("student", "teacher")

    def fresh(self):
        """A browser of its own for the next account type."""
        self.client = self.app.test_client()

    def test_a_failed_verification_keeps_the_return_target_and_the_binding(self):
        for kind in self.KINDS:
            self.fresh()
            self.begin(kind, email=f"{kind}.vfail@example.bg", next=self.NEXT)
            code = self.last_code()
            self.flaky(1)
            response = self.verify(code)
            self.assertEqual(response.status_code, 500)
            self.assertRestartsAtChooser(response, self.NEXT)
            self.assertEqual(self.slot()["next"], self.NEXT)                 # the binding is untouched
            self.assertEqual(self.users_with(f"{kind}.vfail@example.bg"), [])
            self.assertNoAccountSession()

    def test_a_failed_resend_and_a_failed_cancel_keep_the_return_target(self):
        for kind in self.KINDS:
            self.fresh()
            self.begin(kind, email=f"{kind}.rfail@example.bg", next=self.NEXT)
            self.flaky(1, 2)                                                 # the resend's commit, then the cancel's
            for response in (self.resend(), self.cancel()):
                self.assertEqual(response.status_code, 500)
                self.assertRestartsAtChooser(response, self.NEXT)
            self.assertIsNotNone(self.slot())

    def test_a_failed_submission_keeps_the_validated_form_target(self):
        for kind in self.KINDS:
            self.fresh()
            self.flaky(1)
            response = self.begin(kind, email=f"{kind}.sfail@example.bg", next=self.NEXT)
            self.assertEqual(response.status_code, 500)
            self.assertRestartsAtChooser(response, self.NEXT)
            self.assertIsNone(self.slot())

    def test_a_submission_during_a_restore_hold_keeps_the_validated_form_target(self):
        self.set_control(hold="restore")
        for kind in self.KINDS:
            self.fresh()
            response = self.begin(kind, email=f"{kind}.hold@example.bg", next=self.NEXT)
            self.assertEqual(response.status_code, 503)
            self.assertRestartsAtChooser(response, self.NEXT)
            self.assertIsNone(self.slot())

    def test_a_hold_that_appears_between_the_read_and_the_write_keeps_the_form_target(self):
        # The pre-check passes, the service refuses under the write lock: the second ``_unavailable`` of the submit.
        self.set_control(hold="restore")
        with patch.object(registration_service, "availability", return_value=Availability.AVAILABLE):
            for kind in self.KINDS:
                self.fresh()
                response = self.begin(kind, email=f"{kind}.race@example.bg", next=self.NEXT)
                self.assertEqual(response.status_code, 503)
                self.assertRestartsAtChooser(response, self.NEXT)
                self.assertIsNone(self.slot())

    def test_resend_and_verify_during_a_restore_hold_keep_the_return_target_and_the_binding(self):
        for kind in self.KINDS:
            self.fresh()
            self.begin(kind, email=f"{kind}.rhold@example.bg", next=self.NEXT)
            code = self.last_code()
            self.set_control(hold="restore")
            for response in (self.resend(), self.verify(code)):
                self.assertEqual(response.status_code, 503)
                self.assertRestartsAtChooser(response, self.NEXT)
            self.assertEqual(self.slot()["next"], self.NEXT)                 # a hold does not spend the binding
            self.set_control()

    def test_a_cancel_refused_by_the_clock_keeps_the_return_target(self):
        for kind in self.KINDS:
            self.fresh()
            self.begin(kind, email=f"{kind}.clock@example.bg", next=self.NEXT)
            self.set_control(high_water=self.wall + timedelta(hours=1))
            response = self.cancel()
            self.assertEqual(response.status_code, 503)
            self.assertRestartsAtChooser(response, self.NEXT)
            self.assertIsNotNone(self.slot())
            self.set_control()

    def test_gate_off_after_a_teacher_began_keeps_the_saved_target_on_verify_and_resend(self):
        for action in ("verify", "resend"):
            with self.subTest(action=action):
                self.client = self.app.test_client()
                self.submit_teacher(f"gate.{action}@example.bg", next="/teacher/new")
                code = self.last_code()
                patcher = patch.object(registration_service, "TEACHER_ACTIVATION", False)
                patcher.start()
                try:
                    response = self.verify(code) if action == "verify" else self.resend()
                finally:
                    patcher.stop()
                self.assertEqual(response.status_code, 503)
                self.assertRestartsAtChooser(response, "/teacher/new")      # captured before the binding was retired
                self.assertIsNone(self.slot())
                self.assertEqual(self.users_with(f"gate.{action}@example.bg"), [])
                # The binding is spent: asking again has no target to offer.
                self.assertRestartsAtChooser(self.verify("000000"))
                self.assertRestartsAtChooser(self.resend())

    def test_retiring_the_binding_does_not_lose_the_target_of_the_ended_page(self):
        for kind in self.KINDS:
            self.fresh()
            self.begin(kind, email=f"{kind}.retire@example.bg", next=self.NEXT)
            self.at(self.wall_seconds() + 1801)
            response = self.resend()
            self.assertEqual(response.status_code, 200)
            self.assertRestartsAtChooser(response, self.NEXT)
            self.assertIsNone(self.slot())

    def test_an_unsafe_target_is_never_carried_to_these_pages(self):
        self.set_control(hold="restore")
        for bad in ("//evil.example/x", "https://evil.example", "javascript:alert(1)", "/a b", "/x\r\nSet-Cookie: a=b"):
            for kind in self.KINDS:
                self.fresh()
                response = self.begin(kind, email=f"{kind}.bad@example.bg", next=bad)
                self.assertEqual(response.status_code, 503)
                self.assertRestartsAtChooser(response)                       # targetless, and nothing echoed
                self.assertNotIn("evil.example", self.body(response))
                self.assertNotIn("javascript:", self.body(response))
        self.set_control()
        self.begin("teacher", next=self.NEXT)
        slot = dict(self.slot())
        slot["next"] = "//evil.example/x"                                    # a forged cookie: ignored whole
        self.set_slot(slot)
        self.set_control(hold="restore")
        for response in (self.resend(), self.verify("000000"), self.cancel()):
            self.assertNotIn("evil.example", self.body(response))
        self.assertRestartsAtChooser(self.resend())
        self.assertNotIn("next=", self.restart_link(self.resend()))

    def test_the_stored_next_is_revalidated_and_never_echoed_when_unsafe(self):
        self.begin("teacher", next=self.NEXT)
        slot = dict(self.slot())
        slot["next"] = "//evil.example/x"                                    # a forged cookie value is ignored whole
        self.set_slot(slot)
        response = self.verify("000000")
        self.assertRestartsAtChooser(response)
        self.assertNotIn("evil.example", self.body(response))
        for bad in ("//evil.example", "https://evil.example", "/x\r\nSet-Cookie: a=b", "/a b", "javascript:alert(1)"):
            page = self.body(self.client.get("/register", query_string={"next": bad}))
            self.assertNotIn("evil.example", page)
            self.assertNotIn("javascript:", page)
            self.assertIn('href="/register/student"', page)

    def test_the_restart_page_links_nothing_to_a_form_by_itself(self):
        self.begin("teacher", next=self.NEXT)
        verify_page = self.body(self.client.get("/register/verify"))
        self.assertIn('action="/register/cancel"', verify_page)             # cancel is a POST, then the chooser
        self.assertNotIn('href="/register/student', verify_page)
