"""A HEAD to the registration forms and to verification is a read, whatever it carries.

Flask routes HEAD to the GET/POST handlers of ``/register/student``, ``/register/teacher`` and
``/register/verify``. They once branched on ``request.method == "GET"``, so a HEAD with a form
body reached the submission and the code check. Cross-origin protection and the rate-limit
keys at the proxy cover unsafe methods only, so nothing else stood in the way. These tests
send HEAD requests with real bodies and require that nothing is written in any table, that no
password is hashed, that no mail is dispatched or settled and that no registration slot is
issued; and that a code left untouched by a HEAD is still redeemed by the legitimate POST.

Every database is temporary (migrations plus canonical data); the mailer is in memory.
"""

import hashlib
import sqlite3
from contextlib import closing
from unittest.mock import patch

from tests.test_account_login import SAME_ORIGIN
from tests.test_registration_routes import (
    CROSS_ORIGIN, EMAIL, RegistrationRouteTestCase, form, registration_blueprint, registration_service)
from tests.test_teacher_registration import TEACHER_EMAIL, TeacherRouteTestCase, teacher_form, wrong
from web.registration_settings import CONFIG_KEY

OTHER_EMAIL = "petar.georgiev@example.bg"
WRONG_ORIGIN_REFERER = {"Referer": "http://evil.example/register"}
HEAD_CASES = (
    ("/register/student", lambda: form(OTHER_EMAIL)),
    ("/register/teacher", lambda: teacher_form(OTHER_EMAIL)),
)


class HeadSafetyCase(TeacherRouteTestCase):
    def full_digest(self):
        """A digest of the contents of every table of the database, not only the registration ones."""
        sha = hashlib.sha256()
        with closing(sqlite3.connect(self.db_path)) as conn:
            tables = [name for (name,) in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
            for table in tables:
                for row in conn.execute(f'SELECT * FROM "{table}" ORDER BY 1'):
                    sha.update(repr((table, tuple(row))).encode("utf-8"))
        return sha.hexdigest()

    def guard(self):
        """Spies that must stay empty across a HEAD: hashing, the service writers, dispatch, settlement."""
        return {
            "hash": self.spy(registration_blueprint.accounts_service, "hash_new_password"),
            "create": self.spy(registration_service, "create_pending"),
            "verify": self.spy(registration_service, "verify"),
            "dispatch": self.spy(registration_service, "dispatch"),
            "settle": self.spy(registration_service, "settle_send"),
        }

    def head(self, path, data=None, *, client=None, headers=SAME_ORIGIN):
        return (client or self.client).open(path, method="HEAD", data=data, headers=headers)

    def assertInert(self, calls, before, sent=0, client=None):
        for name, recorded in calls.items():
            self.assertEqual(recorded, [], name)
        self.assertEqual(self.full_digest(), before)
        self.assertEqual(len(self.mailer.messages), sent)

    def assertNoIssuedSlot(self, client=None):
        self.assertNotIn(registration_blueprint.SLOT, self.session(client))


class SubmissionHeadTest(HeadSafetyCase):
    def test_a_head_with_a_valid_submission_body_does_nothing_without_a_slot(self):
        for path, body in HEAD_CASES:
            with self.subTest(path=path):
                calls, before = self.guard(), self.full_digest()
                response = self.head(path, body())
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.get_data(), b"")
                self.assertInert(calls, before)
                self.assertNoIssuedSlot()
                self.assertEqual((self.pending(), self.sends()), ([], []))

    def test_a_head_with_a_valid_submission_body_does_nothing_with_a_valid_slot(self):
        self.assertEqual(self.submit().status_code, 303)
        slot, sent = self.slot(), len(self.mailer.messages)
        for path, body in HEAD_CASES:
            with self.subTest(path=path):
                calls, before = self.guard(), self.full_digest()
                self.assertEqual(self.head(path, body()).status_code, 200)
                self.assertInert(calls, before, sent=sent)
                self.assertEqual(self.slot(), slot)     # neither replaced nor dropped
        self.assertEqual([row["state"] for row in self.pending()], ["pending"])

    def test_a_head_with_an_invalid_body_is_the_same_read(self):
        for path, _ in HEAD_CASES:
            with self.subTest(path=path):
                calls, before = self.guard(), self.full_digest()
                self.assertEqual(self.head(path, {"email": "not an address"}).status_code, 200)
                self.assertInert(calls, before)

    def test_a_head_is_a_read_in_each_gate_state(self):
        # The hold only refuses the POST's work: a HEAD never reaches the availability check.
        self.set_control(hold="restore")
        for path, body in HEAD_CASES:
            with self.subTest(state="held", path=path):
                calls, before = self.guard(), self.full_digest()
                self.assertEqual(self.head(path, body()).status_code, 200)
                self.assertInert(calls, before)
                self.assertNoIssuedSlot()
        # Registration disabled: the generic 404, after the same-origin check, as for any method.
        self.app.config.pop(CONFIG_KEY)
        for path, body in HEAD_CASES:
            with self.subTest(state="disabled", path=path):
                calls, before = self.guard(), self.full_digest()
                self.assertEqual(self.head(path, body()).status_code, 404)
                self.assertInert(calls, before)
                self.assertNoIssuedSlot()

    def test_a_head_to_the_teacher_form_with_the_teacher_gate_off_is_the_404(self):
        self.set_gate(False)
        calls, before = self.guard(), self.full_digest()
        self.assertEqual(self.head("/register/teacher", teacher_form(OTHER_EMAIL)).status_code, 404)
        self.assertInert(calls, before)
        self.assertNoIssuedSlot()

    def test_a_signed_in_account_is_sent_home_by_a_head_and_nothing_starts(self):
        account = self.make("student")
        self.sign_in(account)
        calls, before, state = self.guard(), self.full_digest(), self.session()
        for path, body in HEAD_CASES:
            with self.subTest(path=path):
                response = self.head(path, body())
                self.assertEqual((response.status_code, response.headers["Location"]), (303, "/"))
        self.assertInert(calls, before)
        self.assertEqual(self.session(), state)

    def test_a_cross_origin_head_is_a_read_and_a_cross_origin_post_is_still_refused(self):
        for path, body in HEAD_CASES:
            for headers in (CROSS_ORIGIN, WRONG_ORIGIN_REFERER):
                with self.subTest(path=path, headers=headers):
                    calls, before = self.guard(), self.full_digest()
                    self.assertEqual(self.head(path, body(), headers=headers).status_code, 200)
                    self.assertInert(calls, before)
                    self.assertNoIssuedSlot()
                    # The unsafe method keeps its protection.
                    self.assertEqual(self.client.post(path, data=body(), headers=headers).status_code, 403)
                    self.assertInert(calls, before)
                    self.assertNoIssuedSlot()

    def test_an_unrelated_stale_session_is_not_a_registration_slot(self):
        # Sessions naming an account that no longer exists are cleaned up by the account layer on
        # any request; that is not registration state. All that is claimed here is that the HEAD
        # issues no registration slot and writes nothing.
        with self.client.session_transaction() as sess:
            sess.update({"uid": 987654, "sk": "stale", "iat": 1})
        calls, before = self.guard(), self.full_digest()
        self.assertEqual(self.head("/register/student", form(OTHER_EMAIL)).status_code, 200)
        self.assertInert(calls, before)
        self.assertNoIssuedSlot()

    def test_the_post_submission_still_issues_the_slot_and_sends_once(self):
        calls = self.guard()
        self.assertEqual(self.submit(OTHER_EMAIL).status_code, 303)
        self.assertEqual(len(calls["hash"]), 1)
        self.assertEqual(len(calls["create"]), 1)
        self.assertEqual(len(calls["dispatch"]), 1)
        self.assertEqual(len(calls["settle"]), 1)
        self.assertEqual(len(self.mailer.messages), 1)
        self.assertIn(registration_blueprint.SLOT, self.session())
        self.assertEqual(self.submit_teacher().status_code, 303)
        self.assertEqual(len(self.mailer.messages), 2)


class VerifyHeadTest(HeadSafetyCase):
    def start(self, teacher=False):
        submitted = self.submit_teacher() if teacher else self.submit()
        self.assertEqual(submitted.status_code, 303, self.body(submitted))
        return self.last_code(), self.slot(), self.full_digest()

    def test_a_head_with_a_wrong_code_consumes_no_attempt(self):
        for teacher in (False, True):
            with self.subTest(teacher=teacher):
                code, slot, before = self.start(teacher)
                calls, sent = self.guard(), len(self.mailer.messages)
                for _ in range(registration_service.MAX_CODE_ATTEMPTS + 2):
                    self.assertEqual(self.head("/register/verify", {"code": wrong(code)}).status_code, 200)
                self.assertInert(calls, before, sent=sent)
                self.assertEqual(self.slot(), slot)
                self.assertEqual(self.pending()[-1]["state"], "pending")
                self.assertEqual(self.pending()[-1]["code_attempts"], 0)
                self.cancel()

    def test_a_head_with_the_correct_code_activates_nothing_and_the_code_stays_usable(self):
        for teacher in (False, True):
            with self.subTest(teacher=teacher):
                email = TEACHER_EMAIL if teacher else EMAIL
                code, slot, before = self.start(teacher)
                calls, sent = self.guard(), len(self.mailer.messages)
                self.assertEqual(self.head("/register/verify", {"code": code}).status_code, 200)
                self.assertInert(calls, before, sent=sent)
                self.assertEqual(self.users_with(email), [])
                self.assertEqual(self.slot(), slot)
                verified = self.verify(code)    # the legitimate POST
                self.assertEqual(verified.status_code, 303)
                self.assertEqual(verified.headers["Location"], "/login?registration=activated")
                self.assertEqual(len(self.users_with(email)), 1)
                self.assertEqual(self.session(), {})

    def test_a_head_without_a_slot_or_with_a_slot_of_another_shape_redirects_and_writes_nothing(self):
        code, slot, _ = self.start()
        for label, value in (("absent", None), ("shape", {"v": 1, "t": slot["t"]})):
            with self.subTest(slot=label):
                client = self.app.test_client()
                if value is not None:
                    self.set_slot(value, client)
                calls, before = self.guard(), self.full_digest()
                response = self.head("/register/verify", {"code": code}, client=client)
                self.assertEqual((response.status_code, response.headers["Location"]), (302, "/register"))
                self.assertInert(calls, before, sent=1)
                if value is None:
                    self.assertNoIssuedSlot(client)
        self.assertEqual(self.verify(code).status_code, 303)

    def test_a_cross_origin_head_to_verify_is_a_read_and_a_cross_origin_post_is_refused(self):
        code, slot, before = self.start()
        calls = self.guard()
        for headers in (CROSS_ORIGIN, WRONG_ORIGIN_REFERER):
            with self.subTest(headers=headers):
                self.assertEqual(self.head("/register/verify", {"code": code}, headers=headers).status_code, 200)
                self.assertInert(calls, before, sent=1)
                self.assertEqual(self.verify(code, headers=headers).status_code, 403)
                self.assertInert(calls, before, sent=1)
        self.assertEqual(self.slot(), slot)
        self.assertEqual(self.verify(code).status_code, 303)

    def test_a_head_to_verify_in_each_gate_state(self):
        # A pending teacher registration is voided by a POST while the gate is off, never by a HEAD.
        code, slot, before = self.start(teacher=True)
        self.set_gate(False)
        calls = self.guard()
        self.assertEqual(self.head("/register/verify", {"code": code}).status_code, 200)
        self.assertInert(calls, before, sent=1)
        self.assertEqual(self.slot(), slot)
        # Held: a HEAD reads the page and nothing else.
        self.set_gate(True)
        self.set_control(hold="restore")
        before = self.full_digest()
        self.assertEqual(self.head("/register/verify", {"code": code}).status_code, 200)
        self.assertInert(calls, before, sent=1)
        # Disabled: the generic 404.
        self.app.config.pop(CONFIG_KEY)
        before = self.full_digest()
        self.assertEqual(self.head("/register/verify", {"code": code}).status_code, 404)
        self.assertInert(calls, before, sent=1)

    def test_a_head_to_verify_by_a_signed_in_account_is_sent_home(self):
        code, _, _ = self.start()
        other = self.app.test_client()
        self.sign_in(self.make("teacher"), client=other)
        calls, before = self.guard(), self.full_digest()
        response = self.head("/register/verify", {"code": code}, client=other)
        self.assertEqual((response.status_code, response.headers["Location"]), (303, "/teacher/new"))
        self.assertInert(calls, before, sent=1)

    def test_post_verification_attempts_and_cross_origin_post_are_unchanged(self):
        code, _, _ = self.start()
        self.assertEqual(self.verify(wrong(code)).status_code, 200)
        self.assertEqual(self.pending()[0]["code_attempts"], 1)
        self.assertEqual(self.verify(code, headers=CROSS_ORIGIN).status_code, 403)
        self.assertEqual(self.pending()[0]["code_attempts"], 1)
        self.assertEqual(self.verify(code).status_code, 303)
        self.assertEqual(len(self.users_with(EMAIL)), 1)
