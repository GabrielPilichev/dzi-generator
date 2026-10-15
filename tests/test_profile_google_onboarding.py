"""Registration PR 6: the mark at Google onboarding and the redirect after it (test plan J6, J7, J10, J11, J15).

The application runs with Google sign-in enabled against a fake Google (``tests.google_oidc_support``):
real signed tokens, no network. The profile gate is switched on per test through the
application configuration; the gate-off tests leave it at the build's default. Every
database is the test's own.
"""

import logging
import sqlite3
from contextlib import closing
from unittest.mock import patch
from urllib.parse import parse_qsl, urlsplit

from tests.google_oidc_support import FailingCommit, GoogleAppTestCase, Tracing
from tests.test_account_login import SAME_ORIGIN
from web.services import accounts, identities, profiles
from web.services.google_oidc import ISSUER

START = "/quiz/s/" + "B" * 43
PROFILE = "/account/profile"


def mark_of(rows):
    """The mark column of ``SELECT profile_required_at ...`` rows."""
    return [row[0] for row in rows]


class OnboardingTestCase(GoogleAppTestCase):
    gate = True

    def setUp(self):
        super().setUp()
        if self.gate:
            config = patch.dict(self.app.config, {profiles.CONFIG_KEY: True})
            config.start()
            self.addCleanup(config.stop)

    def marks(self):
        return self.query("SELECT id, role, profile_required_at FROM users ORDER BY id")

    def mark_values(self):
        return [row[2] for row in self.marks()]

    def profile_count(self):
        return self.query("SELECT COUNT(*) FROM user_profiles")[0][0]

    def link_identity(self, user_id, subject):
        with closing(sqlite3.connect(self.db_path)) as conn:
            identities.add_identity(conn, user_id, issuer=ISSUER, subject=subject, provider="google",
                                    email_at_link=None)
            conn.commit()

    @staticmethod
    def target_of(response):
        parts = urlsplit(response.headers["Location"])
        return parts.path, dict(parse_qsl(parts.query))


class GateOnTest(OnboardingTestCase):
    def test_a_created_student_is_marked_and_sent_to_the_profile_form_with_the_target(self):  # J7
        with self.assertLogs("learnpilot.oidc", logging.INFO):
            response = self.google_sign_in(subject="new-1", email="pupil@example.bg", name="Мария Петрова", next=START)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.target_of(response), (PROFILE, {"next": START}))
        self.assertEqual(response.headers["Location"], f"{PROFILE}?next={START}")
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(response.headers["Referrer-Policy"], "no-referrer")
        rows = self.marks()
        self.assertEqual([(row[1]) for row in rows], ["student"])
        self.assertRegex(rows[0][2], r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d$")
        # The account is what Google onboarding always made: no email, no password, no profile.
        self.assertEqual(self.query("SELECT email, display_name, password_hash IS NULL, email_verified_at FROM users"),
                         [(None, "Мария Петрова", 1, None)])
        self.assertEqual(self.profile_count(), 0)
        self.assertEqual(set(self.session()), {"uid", "sk", "iat"})

    def test_without_a_target_the_form_carries_the_students_home(self):
        response = self.google_sign_in(subject="new-2")
        self.assertEqual(self.target_of(response), (PROFILE, {"next": "/"}))

    def test_an_unsafe_target_is_dropped_before_it_is_stored_and_never_reaches_the_form(self):  # F13
        for bad in ("//evil.example/x", "https://evil.example/", "/a\\b", "evil"):
            with self.subTest(next=bad):
                client = self.app.test_client()
                response = self.google_sign_in(client, subject=f"new-{bad}", next=bad)
                self.assertEqual(self.target_of(response), (PROFILE, {"next": "/"}))
                self.assertNotIn("evil", response.headers["Location"])

    def test_completing_the_profile_stores_it_updates_the_name_and_returns_by_get(self):  # J7, J9
        response = self.google_sign_in(subject="new-3", name="Google Име", next=START)
        page = self.client.get(response.headers["Location"])
        self.assertEqual(page.status_code, 200)
        text = page.get_data(as_text=True)
        self.assertIn("Попълни профила си, за да започнеш теста.", text)
        self.assertIn(f'name="next" value="{START}"', text)
        for forbidden in ('name="email"', 'type="password"'):
            self.assertNotIn(forbidden, text)
        done = self.client.post(PROFILE, data={"first_name": "Мария", "last_name": "Петрова", "grade": "9",
                                               "next": START}, headers=SAME_ORIGIN)
        self.assertEqual((done.status_code, done.headers["Location"]), (303, START))
        self.assertEqual(self.query("SELECT first_name, last_name, grade FROM user_profiles"), [("Мария", "Петрова", 9)])
        self.assertEqual(self.query("SELECT display_name, email, password_hash IS NULL FROM users"),
                         [("Мария Петрова", None, 1)])
        self.assertEqual(self.query("SELECT COUNT(*) FROM quiz_attempts"), [(0,)])

    def test_the_mark_is_written_in_the_transaction_that_creates_the_account(self):
        with Tracing(self) as trace:
            self.google_sign_in(subject="new-4")
        statements = [" ".join(statement.split()) for statement in trace.statements]
        created = next(i for i, s in enumerate(statements) if s.startswith("INSERT INTO users"))
        begin = max(i for i, s in enumerate(statements[:created]) if s.upper() == "BEGIN IMMEDIATE")
        commit = next(i for i, s in enumerate(statements) if i > created and s.upper() == "COMMIT")
        inside = statements[begin + 1:commit]
        self.assertTrue(any(s.startswith("INSERT INTO users") for s in inside))
        self.assertTrue(any(s.startswith("INSERT INTO user_identities") for s in inside))
        self.assertTrue(any(s.startswith("UPDATE users SET profile_required_at") for s in inside), inside)
        self.assertEqual(sum(s.startswith("UPDATE users SET profile_required_at") for s in statements), 1)

    def test_a_failure_while_marking_creates_no_account_and_no_identity(self):
        started = self.start()
        with patch.object(accounts, "mark_profile_required", side_effect=sqlite3.OperationalError("disk I/O error")), \
                self.assertLogs("learnpilot.oidc", logging.WARNING):
            answered = self.return_from_google(started, subject="new-5")
        self.assert_redirect(answered, "/login?google=failed")
        self.assertEqual((self.user_count(), self.identity_rows(), self.session()), (0, [], {}))

    def test_a_failed_commit_leaves_no_account_and_no_mark(self):
        started = self.start()
        with FailingCommit(self), self.assertLogs("learnpilot.oidc", logging.WARNING):
            answered = self.return_from_google(started, subject="new-6")
        self.assert_redirect(answered, "/login?google=unavailable")
        self.assertEqual((self.user_count(), self.identity_rows(), self.session()), (0, [], {}))

    def test_a_second_sign_in_of_the_same_google_account_does_not_mark_again_or_redirect(self):  # J10
        self.google_sign_in(subject="again-1", name="Мария Петрова")
        mark = self.marks()[0][2]
        other_browser = self.app.test_client()
        again = self.google_sign_in(other_browser, subject="again-1", next=START)
        self.assert_redirect(again, START)             # exactly as before: no profile form at sign-in
        self.assertEqual(self.mark_values()[0], mark)
        self.assertEqual(self.user_count(), 1)

    def test_an_existing_google_account_from_before_the_gate_is_never_marked(self):  # J10
        with patch.dict(self.app.config, {profiles.CONFIG_KEY: False}):
            first = self.google_sign_in(subject="old-1")
        self.assert_redirect(first, "/")
        self.assertEqual(self.mark_values(), [None])
        again = self.google_sign_in(self.app.test_client(), subject="old-1")
        self.assert_redirect(again, "/")
        self.assertEqual(self.mark_values(), [None])

    def test_teachers_and_admins_are_never_marked_or_redirected(self):  # J2, J3
        teacher = self.make("teacher")
        self.link_identity(teacher["id"], "teacher-sub")
        response = self.google_sign_in(subject="teacher-sub")
        self.assert_redirect(response, "/teacher/new")
        self.assertEqual(mark_of(self.query("SELECT profile_required_at FROM users WHERE id = ?", (teacher["id"],))), [None])
        # An admin identity cannot sign in through Google at all.
        admin = self.make("admin")
        self.link_identity(admin["id"], "admin-sub")
        with self.assertLogs("learnpilot.oidc", logging.WARNING):
            refused = self.google_sign_in(self.app.test_client(), subject="admin-sub")
        self.assert_redirect(refused, "/login?google=not_allowed")
        self.assertEqual(mark_of(self.query("SELECT profile_required_at FROM users WHERE id = ?", (admin["id"],))), [None])

    def test_an_existing_student_is_not_marked_by_signing_in_with_google(self):  # J10
        student = self.make("student")
        self.link_identity(student["id"], "student-sub")
        response = self.google_sign_in(subject="student-sub")
        self.assert_redirect(response, "/")
        self.assertEqual(mark_of(self.query("SELECT profile_required_at FROM users WHERE id = ?", (student["id"],))), [None])

    def test_an_email_of_an_existing_account_creates_and_marks_nobody(self):
        self.make("student", email="student@example.bg")
        before = self.marks()
        with self.assertLogs("learnpilot.oidc", logging.WARNING):
            answered = self.google_sign_in(subject="taken-1", email="Student@Example.BG")
        self.assert_redirect(answered, "/login?google=email_exists")
        self.assertEqual(self.marks(), before)

    def test_a_cli_created_student_who_signs_in_with_a_password_is_not_marked(self):  # J10, J11
        student = self.make("student", email="cli@example.bg")
        response = self.login("cli@example.bg")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(mark_of(self.query("SELECT profile_required_at FROM users WHERE id = ?", (student["id"],))), [None])

    def test_no_other_code_writes_the_mark(self):  # J11
        """The mark is written by one function, called from one place."""
        import re
        from tests.support import ROOT
        callers = []
        for directory in ("web", "src"):
            for path in (ROOT / directory).rglob("*.py"):
                text = path.read_text(encoding="utf-8")
                if re.search(r"profile_required_at\s*=|mark_profile_required\(", text):
                    callers.append(path.relative_to(ROOT).as_posix())
        self.assertEqual(sorted(callers), ["web/services/accounts.py", "web/services/oidc_accounts.py"])
        self.assertFalse(any("require-profile" in p.read_text(encoding="utf-8")
                             for p in (ROOT / "src").glob("manage_accounts.py")))


class GateOffTest(OnboardingTestCase):
    """The build's default: Google onboarding redirects exactly as before and marks nobody (J6)."""

    gate = False

    def test_a_created_student_is_not_marked_and_is_redirected_as_before(self):
        self.assertFalse(profiles.enabled_in(self.app.config))
        clients = []
        for next_target, expected in ((None, "/"), (START, START)):
            with self.subTest(next=next_target):
                client = self.app.test_client()
                clients.append(client)
                response = self.google_sign_in(client, subject=f"off-{next_target}", next=next_target)
                self.assert_redirect(response, expected)
                self.assertEqual(set(self.session(client)), {"uid", "sk", "iat"})
        self.assertEqual(self.mark_values(), [None, None])
        self.assertEqual(self.profile_count(), 0)
        # The page is not there either, for the very accounts that were just created.
        for client in clients:
            self.assertEqual(client.get(PROFILE).status_code, 404)

    def test_no_statement_writes_the_mark_while_the_gate_is_off(self):
        with Tracing(self) as trace:
            self.google_sign_in(subject="off-trace")
        self.assertEqual([s for s in trace.statements if "profile_required_at" in s], [])


if __name__ == "__main__":
    import unittest
    unittest.main()
