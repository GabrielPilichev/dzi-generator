"""Account-owned quiz attempts (Accounts phase 2).

A new attempt belongs to the account that started it
(``quiz_attempts.user_id``); attempts from before accounts keep ``user_id`` NULL
and stay bound to the browser that started them. The two never mix:

* account-owned: only that account, from any browser, and never through a
  browser marker;
* historical: only the browser marker, whoever is signed in, and never linked to
  an account by name.

Students, teachers and admins may all take a quiz, each with an attempt of
their own. Managing an assignment or reviewing its answers is a separate
authority: it never opens or answers another account's attempt.

Everything runs on the class's temporary database, vault and upload root.
"""

import io
import threading
import time
import unittest
from contextlib import closing
from pathlib import Path

from tests import runtime_fixtures
from tests.auth_helpers import create_account, set_account_session
from tests.support import ROOT, IsolatedAppTestCase, web_app, student_entry_path
from tests.test_account_login import SAME_ORIGIN
from web import access
from web.access import ANONYMOUS, Principal, account_principal
from web.assignment_tokens import new_public_token
from web.services import accounts as accounts_service
from web.services import dzi_exams
from web.services import quiz

PASSWORD = "correct horse battery"
MARKER_PREFIX = "quiz_attempt_owner:"


class AttemptAccessPredicateTest(unittest.TestCase):
    """The pure rules in web.access, without routes or a database."""

    student = account_principal(7, "student", "Ученик")
    teacher = account_principal(8, "teacher", "Учител")
    admin = account_principal(9, "admin", "Админ")
    accounts = (student, teacher, admin)
    # Nobody: anonymous, an unknown role (which loads as anonymous), a role
    # without an account id, an account id without a role.
    non_accounts = (
        ANONYMOUS,
        account_principal(7, "superuser", "?"),
        Principal(is_teacher=False, is_admin=False, role="student"),
        Principal(is_teacher=True, is_admin=True, role="admin"),
        Principal(is_teacher=True, is_admin=True, user_id=7),
    )

    def test_every_account_role_takes_quizzes_and_nobody_else(self):
        for principal in self.accounts:
            with self.subTest(role=principal.role):
                self.assertTrue(access.can_take_quizzes(principal))
        for principal in self.non_accounts:
            with self.subTest(principal=principal):
                self.assertFalse(access.can_take_quizzes(principal))

    def test_account_owned_attempt_is_its_own_accounts_only(self):
        for owner in self.accounts:
            for principal in (*self.accounts, *self.non_accounts):
                for marker in (False, True):
                    with self.subTest(owner=owner.role, principal=principal, marker=marker):
                        # The account itself, whatever its role; the marker changes nothing.
                        self.assertEqual(
                            access.can_access_attempt(principal, owner.user_id, marker),
                            principal is owner)

    def test_management_capability_is_not_attempt_ownership(self):
        # Teacher and admin capability (is_teacher / is_admin) are never consulted:
        # the student's attempt 7 stays closed to them, with or without a marker.
        for principal in (self.teacher, self.admin):
            for marker in (False, True):
                with self.subTest(role=principal.role, marker=marker):
                    self.assertFalse(access.can_access_attempt(principal, self.student.user_id, marker))
        # An id that matches is not enough without being a real account.
        self.assertFalse(access.can_access_attempt(
            Principal(is_teacher=True, is_admin=True, user_id=7), 7, True))

    def test_historical_attempt_follows_the_marker_and_nothing_else(self):
        for principal in (ANONYMOUS, *self.accounts):
            with self.subTest(principal=principal):
                self.assertTrue(access.can_access_attempt(principal, None, True))
                self.assertFalse(access.can_access_attempt(principal, None, False))

    def test_a_missing_account_id_never_matches_a_missing_owner(self):
        # NULL owner and anonymous principal are both "None": that is not ownership.
        for principal in self.non_accounts:
            with self.subTest(principal=principal):
                self.assertFalse(access.can_access_attempt(principal, None, False))
        # Only the literal True the application writes counts as a marker.
        for marker in (1, "1", "true", None):
            with self.subTest(marker=marker):
                self.assertFalse(access.can_access_attempt(ANONYMOUS, None, marker))

    def test_quiz_taker_level_admits_exactly_the_accounts(self):
        for principal in self.accounts:
            with self.subTest(role=principal.role):
                self.assertTrue(access.is_route_allowed(access.QUIZ_TAKER, principal))
        for principal in self.non_accounts:
            with self.subTest(principal=principal):
                self.assertFalse(access.is_route_allowed(access.QUIZ_TAKER, principal))


class AccountAttemptTestCase(IsolatedAppTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        web_app.app.config.update(TESTING=True)
        with closing(web_app.quiz_db()) as conn:
            for row in conn.execute("SELECT id FROM curriculum_sections ORDER BY id"):
                if len(quiz.quiz_section_question_ids(conn, row["id"])) >= 3:
                    cls.section_id = int(row["id"])
                    break
            else:
                raise AssertionError("No canonical section with eligible MC questions")
            task = conn.execute("""
                SELECT et.id AS task_id, e.*
                FROM exam_tasks et
                JOIN exams e ON e.id = et.exam_id
                WHERE et.task_number = 26 AND et.task_kind LIKE 'practical_%'
                ORDER BY et.id LIMIT 1
            """).fetchone()
            if task is None:
                raise AssertionError("No canonical practical task 26")
            cls.exam_task_id = int(task["task_id"])
            cls.source_slug = dzi_exams.dzi_source_slug(task)

    @property
    def db_path(self):
        return web_app.app.config["DB_PATH"]

    def query(self, sql, params=()):
        with closing(web_app.quiz_db()) as conn:
            return conn.execute(sql, params).fetchall()

    def execute(self, sql, params=()):
        with closing(web_app.quiz_db()) as conn:
            conn.execute(sql, params)
            conn.commit()

    def account(self, role="student", **options):
        return create_account(self.db_path, role, **options)

    def client(self, account=None, *, markers=()):
        """A separate browser: optionally signed in, optionally holding attempt markers."""
        client = web_app.app.test_client()
        if account:
            set_account_session(client, account["id"], account["session_key"])
        if markers:
            with client.session_transaction() as session:
                for attempt_id in markers:
                    session[web_app.quiz_attempt_owner_session_key(attempt_id)] = True
        return client

    def assignment(self, *, owner=None, title="Тест за профили"):
        with closing(web_app.quiz_db()) as conn:
            # Like every assignment the application creates, it has a public token.
            assignment_id = runtime_fixtures.add_assignment(
                conn, section_id=self.section_id, title=title, question_count=2,
                public_token=new_public_token())
            if owner is not None:
                conn.execute("UPDATE quiz_assignments SET owner_user_id = ? WHERE id = ?",
                             (owner["id"], assignment_id))
            conn.commit()
        return assignment_id

    def public_token(self, assignment_id):
        return self.query("SELECT public_token FROM quiz_assignments WHERE id = ?", (assignment_id,))[0][0]

    def historical_attempt(self, assignment_id, student_name="Исторически Ученик", *, submitted=False):
        """An attempt from before accounts: no user_id."""
        with closing(web_app.quiz_db()) as conn:
            ids = quiz.quiz_section_question_ids(conn, self.section_id)[:2]
            attempt_id = runtime_fixtures.add_attempt(
                conn, assignment_id, student_name=student_name, question_ids=ids, submitted=submitted)
            conn.commit()
        return attempt_id

    def start(self, client, assignment_id, data=None):
        response = client.post(student_entry_path(assignment_id), data=data or {}, headers=SAME_ORIGIN)
        self.assertEqual(response.status_code, 302, response.get_data(as_text=True)[:300])
        self.assertRegex(response.headers["Location"], r"^/quiz/attempt/\d+(/result)?$")
        return int(response.headers["Location"].split("/")[3])

    def attempt(self, attempt_id):
        return self.query("SELECT * FROM quiz_attempts WHERE id = ?", (attempt_id,))[0]

    def attempts_of(self, assignment_id):
        return self.query("SELECT * FROM quiz_attempts WHERE assignment_id = ? ORDER BY id", (assignment_id,))

    def submit(self, client, attempt_id):
        question_id = quiz.quiz_parse_attempt_question_plan(
            self.attempt(attempt_id)["question_ids_json"])["question_ids"][0]
        return client.post(f"/quiz/attempt/{attempt_id}", data={f"q_{question_id}": "A"}, headers=SAME_ORIGIN)

    def markers(self, client):
        with client.session_transaction() as session:
            return sorted(key for key in session if key.startswith(MARKER_PREFIX))

    def practical_base(self, attempt_id):
        return f"/quiz/attempt/{attempt_id}/practical/{self.source_slug}"

    def upload(self, client, attempt_id):
        return client.post(
            f"{self.practical_base(attempt_id)}/task/{self.exam_task_id}/upload",
            data={"files": (io.BytesIO(b"practical work"), "answer.xlsx")},
            headers=SAME_ORIGIN, content_type="multipart/form-data")

    def student_surfaces(self, attempt_id):
        """(label, callable(client) -> response) for every student-side attempt surface."""
        return (
            ("attempt page", lambda c: c.get(f"/quiz/attempt/{attempt_id}")),
            ("submit answers", lambda c: self.submit(c, attempt_id)),
            ("result", lambda c: c.get(f"/quiz/attempt/{attempt_id}/result")),
            ("practical page", lambda c: c.get(self.practical_base(attempt_id))),
            ("practical upload", lambda c: self.upload(c, attempt_id)),
        )

    def child_row_counts(self, attempt_id):
        return (
            self.query("SELECT COUNT(*) FROM quiz_answers WHERE attempt_id = ?", (attempt_id,))[0][0],
            self.query("SELECT COUNT(*) FROM quiz_text_answers WHERE attempt_id = ?", (attempt_id,))[0][0],
            self.query("SELECT COUNT(*) FROM practical_submissions WHERE quiz_attempt_id = ?",
                       (attempt_id,))[0][0],
        )

    def uploaded_files(self):
        root = Path(web_app.app.config["PRACTICAL_UPLOAD_ROOT"])
        return sorted(str(path.relative_to(root)) for path in root.rglob("*") if path.is_file())

    def assert_denied_everywhere(self, client, attempt_id, label):
        """404 on every surface, and nothing about the attempt changes."""
        before = (tuple(self.attempt(attempt_id)), self.child_row_counts(attempt_id), self.uploaded_files())
        missing = client.get("/quiz/attempt/987654321")
        self.assertEqual(missing.status_code, 404)
        # The page's own sign-in link repeats the requested path; nothing else may differ.
        missing_page = missing.get_data(as_text=True).replace("/quiz/attempt/987654321", "/quiz/attempt/N")
        for surface, request in self.student_surfaces(attempt_id):
            with self.subTest(who=label, surface=surface):
                response = request(client)
                self.assertEqual(response.status_code, 404)
                self.assertNotIn("Location", response.headers)
                if surface == "attempt page":
                    # Indistinguishable from an attempt that does not exist.
                    self.assertEqual(
                        response.get_data(as_text=True).replace(f"/quiz/attempt/{attempt_id}", "/quiz/attempt/N"),
                        missing_page)
        self.assertEqual(
            (tuple(self.attempt(attempt_id)), self.child_row_counts(attempt_id), self.uploaded_files()),
            before, label)


class AccountAttemptCreationTest(AccountAttemptTestCase):
    def test_start_creates_an_attempt_owned_by_the_account(self):
        student = self.account(display_name="Мария Иванова")
        client = self.client(student)
        assignment_id = self.assignment()

        attempt_id = self.start(client, assignment_id)

        row = self.attempt(attempt_id)
        self.assertEqual(row["user_id"], student["id"])
        self.assertEqual(row["student_name"], "Мария Иванова")  # display snapshot
        self.assertEqual(row["seed"], quiz.quiz_seed(assignment_id, f"user:{student['id']}"))
        self.assertNotEqual(row["seed"], quiz.quiz_seed(assignment_id, "Мария Иванова"))
        self.assertIsNone(row["submitted_at"])
        # Ownership is in the row, not in the browser: no marker is written.
        self.assertEqual(self.markers(client), [])
        self.assertEqual(client.get(f"/quiz/attempt/{attempt_id}").status_code, 200)

    def test_submitted_identity_fields_cannot_change_the_owner(self):
        student = self.account(display_name="Истински Ученик")
        victim = self.account(display_name="Друг Ученик")
        assignment_id = self.assignment()
        spoof = {
            "user_id": str(victim["id"]), "uid": str(victim["id"]), "account_id": str(victim["id"]),
            "owner_user_id": str(victim["id"]), "student_name": "Друг Ученик", "email": "other@example.bg",
        }
        with self.client(student) as client:
            attempt_id = self.start(client, assignment_id, spoof)
            response = client.post(f"{student_entry_path(assignment_id)}?user_id={victim['id']}", data=spoof,
                                   headers=SAME_ORIGIN)
        self.assertEqual(response.headers["Location"], f"/quiz/attempt/{attempt_id}")
        rows = self.attempts_of(assignment_id)
        self.assertEqual([(row["user_id"], row["student_name"]) for row in rows],
                         [(student["id"], "Истински Ученик")])
        self.assertEqual(self.client(victim).get(f"/quiz/attempt/{attempt_id}").status_code, 404)

    def test_resume_from_the_same_and_from_another_browser(self):
        student = self.account()
        assignment_id = self.assignment()
        first_browser = self.client(student)
        attempt_id = self.start(first_browser, assignment_id)

        self.assertEqual(self.start(first_browser, assignment_id), attempt_id)
        # Another device: same account, a session that never saw this attempt.
        second_browser = self.client(student)
        self.assertEqual(self.markers(second_browser), [])
        self.assertEqual(self.start(second_browser, assignment_id), attempt_id)
        page = second_browser.get(f"/quiz/attempt/{attempt_id}")
        self.assertEqual(page.status_code, 200)
        self.assertEqual(len(self.attempts_of(assignment_id)), 1)

        # Questions and their order are the stored ones on both devices.
        self.assertEqual(page.get_data(as_text=True).count('class="quiz-option-card"'),
                         first_browser.get(f"/quiz/attempt/{attempt_id}").get_data(as_text=True)
                         .count('class="quiz-option-card"'))
        self.assertEqual(self.submit(second_browser, attempt_id).status_code, 302)
        # Submitted: starting again leads to the result, from either browser.
        for browser in (first_browser, second_browser):
            response = browser.post(student_entry_path(assignment_id), headers=SAME_ORIGIN)
            self.assertEqual(response.headers["Location"], f"/quiz/attempt/{attempt_id}/result")
            self.assertEqual(browser.get(response.headers["Location"]).status_code, 200)

    def test_renaming_the_account_neither_duplicates_nor_reshuffles(self):
        student = self.account(display_name="Старо Име")
        assignment_id = self.assignment()
        client = self.client(student)
        attempt_id = self.start(client, assignment_id)
        before = tuple(self.attempt(attempt_id))

        self.execute("UPDATE users SET display_name = 'Ново Име' WHERE id = ?", (student["id"],))

        self.assertIn("Ново Име", client.get(student_entry_path(assignment_id)).get_data(as_text=True))
        self.assertEqual(self.start(client, assignment_id), attempt_id)
        self.assertEqual(self.start(self.client(student), assignment_id), attempt_id)
        self.assertEqual(len(self.attempts_of(assignment_id)), 1)
        # Seed, question plan and the name snapshot are untouched.
        self.assertEqual(tuple(self.attempt(attempt_id)), before)
        self.assertEqual(self.attempt(attempt_id)["student_name"], "Старо Име")
        # A later attempt elsewhere takes the new name and still seeds from the id.
        other = self.attempt(self.start(client, self.assignment()))
        self.assertEqual(other["student_name"], "Ново Име")
        self.assertEqual(other["seed"], quiz.quiz_seed(other["assignment_id"], f"user:{student['id']}"))

    def test_two_accounts_with_one_display_name_get_independent_attempts(self):
        first = self.account(display_name="Иван Петров")
        second = self.account(display_name="Иван Петров")
        assignment_id = self.assignment()

        first_attempt = self.start(self.client(first), assignment_id)
        second_attempt = self.start(self.client(second), assignment_id)

        self.assertNotEqual(first_attempt, second_attempt)
        self.assertEqual(
            [(row["id"], row["user_id"], row["student_name"]) for row in self.attempts_of(assignment_id)],
            [(first_attempt, first["id"], "Иван Петров"), (second_attempt, second["id"], "Иван Петров")])
        self.assertNotEqual(self.attempt(first_attempt)["seed"], self.attempt(second_attempt)["seed"])
        self.assertEqual(self.client(first).get(f"/quiz/attempt/{second_attempt}").status_code, 404)
        self.assertEqual(self.client(second).get(f"/quiz/attempt/{first_attempt}").status_code, 404)

    def test_service_refuses_to_create_an_attempt_without_an_account_id(self):
        assignment_id = self.assignment()
        with closing(web_app.quiz_db()) as conn:
            assignment = quiz.quiz_fetch_assignment(conn, assignment_id)
            for user_id in (None, 0, -1, True, "7", 7.0):
                with self.subTest(user_id=user_id), self.assertRaises(ValueError):
                    quiz.quiz_start_or_resume_attempt(conn, assignment, user_id=user_id)
        self.assertEqual(self.attempts_of(assignment_id), [])


class AnonymousStartTest(AccountAttemptTestCase):
    def test_anonymous_sees_the_assignment_and_a_way_to_sign_in_but_cannot_start(self):
        assignment_id = self.assignment(title="Публична връзка")
        client = self.client()

        page = client.get(student_entry_path(assignment_id))
        self.assertEqual(page.status_code, 200)
        html = page.get_data(as_text=True)
        self.assertIn("Публична връзка", html)
        self.assertIn("влез в своя профил", html)
        self.assertIn(f'href=\"/login?next={student_entry_path(assignment_id)}\"', html)
        self.assertNotIn("<form method=\"post\" class=\"card form-card quiz-start-card", html)
        self.assertNotIn('name="student_name"', html)

        response = client.post(student_entry_path(assignment_id), data={"student_name": "Анонимен"},
                               headers=SAME_ORIGIN)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], f"/login?next={student_entry_path(assignment_id)}")
        self.assertEqual(self.attempts_of(assignment_id), [])
        self.assertEqual(self.markers(client), [])

    def test_signing_in_returns_to_the_assignment_and_starts_the_accounts_attempt(self):
        student = self.account(email="start@example.bg", password=PASSWORD, display_name="Влязъл Ученик")
        assignment_id = self.assignment()
        client = self.client()

        login = client.post("/login", data={"email": "start@example.bg", "password": PASSWORD,
                                            "next": student_entry_path(assignment_id)}, headers=SAME_ORIGIN)
        self.assertEqual(login.status_code, 302)
        self.assertEqual(login.headers["Location"], student_entry_path(assignment_id))
        html = client.get(student_entry_path(assignment_id)).get_data(as_text=True)
        self.assertIn("Решаваш като <strong>Влязъл Ученик</strong>", html)
        self.assertEqual(self.attempt(self.start(client, assignment_id))["user_id"], student["id"])

    def test_sign_in_link_never_leaves_the_site(self):
        assignment_id = self.assignment()
        client = self.client()
        self.account(email="safe@example.bg", password=PASSWORD)
        login = client.post("/login", data={"email": "safe@example.bg", "password": PASSWORD,
                                            "next": f"//evil.example/quiz/{assignment_id}"},
                            headers=SAME_ORIGIN)
        self.assertEqual(login.headers["Location"], "/")

    def test_start_post_stays_same_origin_protected(self):
        assignment_id = self.assignment()
        client = self.client(self.account())
        response = client.post(student_entry_path(assignment_id), headers={"Origin": "http://evil.example"})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.attempts_of(assignment_id), [])

    def test_missing_assignment_is_404_for_everyone(self):
        for label, client in (("anonymous", self.client()), ("student", self.client(self.account())),
                              ("teacher", self.client(self.account("teacher"))),
                              ("admin", self.client(self.account("admin")))):
            for method in ("get", "post"):
                with self.subTest(who=label, method=method):
                    self.assertEqual(
                        getattr(client, method)("/quiz/s/" + "Z" * 43, headers=SAME_ORIGIN).status_code, 404)


class RoleMatrixTest(AccountAttemptTestCase):
    """Every account role takes quizzes; none answers another account's attempt."""

    def setUp(self):
        self.teacher = self.account("teacher", display_name="Учител Собственик")
        self.other_teacher = self.account("teacher", display_name="Друг Учител")
        self.admin = self.account("admin", display_name="Админ Профил")
        self.other_admin = self.account("admin", display_name="Друг Админ")
        self.student = self.account(display_name="Ученик Собственик")
        self.other_student = self.account(display_name="Друг Ученик")
        self.assignment_id = self.assignment(owner=self.teacher)
        self.roles = (
            ("student", self.student, "Ученик Собственик"),
            ("owning teacher", self.teacher, "Учител Собственик"),
            ("other teacher", self.other_teacher, "Друг Учител"),
            ("admin", self.admin, "Админ Профил"),
        )

    def start_all(self):
        """One attempt per role on the same assignment: {label: (account, attempt_id)}."""
        return {label: (account, self.start(self.client(account), self.assignment_id))
                for label, account, _name in self.roles}

    def test_each_role_starts_resumes_and_answers_its_own_attempt(self):
        for label, account, display_name in self.roles:
            with self.subTest(role=label):
                client = self.client(account)
                page = client.get(student_entry_path(self.assignment_id))
                self.assertEqual(page.status_code, 200)
                self.assertIn(f"Решаваш като <strong>{display_name}</strong>", page.get_data(as_text=True))
                self.assertIn('type="submit">Продължи към теста', page.get_data(as_text=True))

                attempt_id = self.start(client, self.assignment_id)
                row = self.attempt(attempt_id)
                self.assertEqual(row["user_id"], account["id"])
                self.assertEqual(row["student_name"], display_name)  # snapshot, whatever the role
                self.assertEqual(row["seed"], quiz.quiz_seed(self.assignment_id, f"user:{account['id']}"))
                self.assertEqual(self.markers(client), [])

                # Resume: same browser and another one, without any marker.
                self.assertEqual(self.start(client, self.assignment_id), attempt_id)
                other_browser = self.client(account)
                self.assertEqual(self.start(other_browser, self.assignment_id), attempt_id)
                self.assertEqual(other_browser.get(f"/quiz/attempt/{attempt_id}").status_code, 200)
                self.assertEqual(other_browser.get(self.practical_base(attempt_id)).status_code, 200)
                self.assertIn("uploaded=1", self.upload(other_browser, attempt_id).headers["Location"])

                # Answer and see the result.
                self.assertEqual(self.submit(other_browser, attempt_id).status_code, 302)
                self.assertIsNotNone(self.attempt(attempt_id)["submitted_at"])
                self.assertEqual(client.get(f"/quiz/attempt/{attempt_id}/result").status_code, 200)
                again = client.post(student_entry_path(self.assignment_id), headers=SAME_ORIGIN)
                self.assertEqual(again.headers["Location"], f"/quiz/attempt/{attempt_id}/result")
        # One attempt per account, each its own.
        self.assertEqual(sorted(row["user_id"] for row in self.attempts_of(self.assignment_id)),
                         sorted(account["id"] for _label, account, _name in self.roles))

    def test_no_role_reaches_another_accounts_attempt(self):
        attempts = self.start_all()
        visitors = (*self.roles, ("other admin", self.other_admin, ""), ("other student", self.other_student, ""))
        for owner_label, (owner, attempt_id) in attempts.items():
            for visitor_label, visitor, _name in visitors:
                if visitor["id"] == owner["id"]:
                    continue
                label = f"{visitor_label} on the attempt of {owner_label}"
                self.assert_denied_everywhere(self.client(visitor), attempt_id, label)
                # ...not even while holding the browser marker of that attempt.
                self.assert_denied_everywhere(
                    self.client(visitor, markers=[attempt_id]), attempt_id, f"{label}, with marker")
        # The owners were not affected by any of it.
        for owner_label, (owner, attempt_id) in attempts.items():
            with self.subTest(owner=owner_label):
                self.assertEqual(self.client(owner).get(f"/quiz/attempt/{attempt_id}").status_code, 200)

    def test_assignment_owner_and_admin_cannot_answer_a_students_attempt(self):
        # The two strongest management authorities over this very assignment.
        attempt_id = self.start(self.client(self.student), self.assignment_id)
        for label, account in (("teacher who owns the assignment", self.teacher), ("admin", self.admin)):
            client = self.client(account)
            # They do manage it...
            self.assertEqual(client.get(f"/teacher/assignment/{self.assignment_id}/results").status_code, 200)
            # ...and still cannot open, answer or upload to the student's attempt.
            self.assert_denied_everywhere(client, attempt_id, label)
            self.assert_denied_everywhere(self.client(account, markers=[attempt_id]), attempt_id,
                                          f"{label}, with marker")
            # Starting gives them an attempt of their own, never the student's.
            own_attempt = self.start(client, self.assignment_id)
            self.assertNotEqual(own_attempt, attempt_id)
            self.assertEqual(self.attempt(own_attempt)["user_id"], account["id"])
        row = self.attempt(attempt_id)
        self.assertEqual((row["user_id"], row["submitted_at"]), (self.student["id"], None))
        self.assertEqual(self.child_row_counts(attempt_id), (0, 0, 0))

    def test_teacher_and_admin_keep_managing_and_reviewing(self):
        attempt_id = self.start(self.client(self.student), self.assignment_id)
        self.assertEqual(self.submit(self.client(self.student), attempt_id).status_code, 302)
        base = f"/teacher/assignment/{self.assignment_id}"
        for label, account in (("owning teacher", self.teacher), ("admin", self.admin)):
            with self.subTest(who=label):
                client = self.client(account)
                results = client.get(f"{base}/results")
                self.assertEqual(results.status_code, 200)
                self.assertIn("Ученик Собственик", results.get_data(as_text=True))
                export = client.get(f"{base}/results.csv")
                self.assertEqual(export.status_code, 200)
                self.assertIn("Ученик Собственик", export.get_data(as_text=True))
                self.assertEqual(client.get(base).status_code, 200)
        # A teacher who does not own the assignment still manages nothing of it,
        # having an attempt of their own on it included.
        other = self.client(self.other_teacher)
        self.start(other, self.assignment_id)
        self.assertEqual(other.get(f"{base}/results").status_code, 404)
        self.assertEqual(other.get(base).status_code, 404)
        # The student has no management surface.
        self.assertEqual(self.client(self.student).get(f"{base}/results").status_code, 403)

    def test_open_the_test_action_leads_the_teacher_and_admin_into_their_own_attempt(self):
        for label, account in (("owning teacher", self.teacher), ("admin", self.admin)):
            with self.subTest(who=label):
                client = self.client(account)
                html = client.get(f"/teacher/assignment/{self.assignment_id}").get_data(as_text=True)
                token = self.public_token(self.assignment_id)
                self.assertIn(f'<a class="button" href="/quiz/s/{token}">Отвори теста</a>', html)
                # The link students receive is still there to copy, with honest copy.
                self.assertIn("Линк за ученици", html)
                self.assertIn(f'value="http://localhost/quiz/s/{token}"', html)
                self.assertNotIn(f'"/quiz/{self.assignment_id}"', html)
                self.assertIn("Всеки потребител с активен профил може да отвори теста от този линк.", html)
                self.assertNotIn("Всеки с този линк може да реши теста", html)
                self.assertIn("Виж резултати", html)
                self.assertIn("Дублирай теста", html)

                # Following the action: the start page, then the account's own attempt.
                start_page = client.get(f"/quiz/s/{token}")
                self.assertEqual(start_page.status_code, 200)
                self.assertIn('type="submit">Продължи към теста', start_page.get_data(as_text=True))
                attempt_id = self.start(client, self.assignment_id)
                self.assertEqual(self.attempt(attempt_id)["user_id"], account["id"])
                self.assertEqual(client.get(f"/quiz/attempt/{attempt_id}").status_code, 200)

    def test_anonymous_is_the_only_one_sent_to_the_sign_in(self):
        response = self.client().post(student_entry_path(self.assignment_id), headers=SAME_ORIGIN)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], f"/login?next={student_entry_path(self.assignment_id)}")
        self.assertEqual(self.attempts_of(self.assignment_id), [])


class ForeignAccountTest(AccountAttemptTestCase):
    def setUp(self):
        self.owner = self.account(display_name="Собственик")
        self.assignment_id = self.assignment()
        self.owner_client = self.client(self.owner)
        self.attempt_id = self.start(self.owner_client, self.assignment_id)

    def test_another_account_of_any_role_is_denied_everywhere(self):
        for role in ("student", "teacher", "admin"):
            self.assert_denied_everywhere(self.client(self.account(role)), self.attempt_id, f"other {role}")

    def test_same_display_name_is_not_ownership(self):
        self.assert_denied_everywhere(
            self.client(self.account(display_name="Собственик")), self.attempt_id, "namesake")

    def test_the_browser_marker_grants_nothing_on_an_account_owned_attempt(self):
        for role in ("student", "teacher", "admin"):
            self.assert_denied_everywhere(
                self.client(self.account(role), markers=[self.attempt_id]), self.attempt_id,
                f"other {role} with marker")
        self.assert_denied_everywhere(
            self.client(markers=[self.attempt_id]), self.attempt_id, "anonymous with marker")

    def test_anonymous_is_denied_everywhere(self):
        self.assert_denied_everywhere(self.client(), self.attempt_id, "anonymous")

    def test_submitted_attempt_result_is_the_owners_only(self):
        self.assertEqual(self.submit(self.owner_client, self.attempt_id).status_code, 302)
        self.assertEqual(self.owner_client.get(f"/quiz/attempt/{self.attempt_id}/result").status_code, 200)
        for label, client in (("other student", self.client(self.account())), ("anonymous", self.client()),
                              ("marker only", self.client(markers=[self.attempt_id]))):
            with self.subTest(who=label):
                self.assertEqual(client.get(f"/quiz/attempt/{self.attempt_id}/result").status_code, 404)
                self.assertEqual(client.get(f"/quiz/attempt/{self.attempt_id}").status_code, 404)

    def test_foreign_start_never_resolves_to_the_owners_attempt(self):
        other = self.account()
        other_attempt = self.start(self.client(other), self.assignment_id)
        self.assertNotEqual(other_attempt, self.attempt_id)
        self.assertEqual(self.attempt(other_attempt)["user_id"], other["id"])
        self.assertEqual(self.attempt(self.attempt_id)["user_id"], self.owner["id"])

    def test_owner_is_unaffected_by_the_denied_requests(self):
        self.assert_denied_everywhere(self.client(self.account()), self.attempt_id, "other student")
        self.assertEqual(self.owner_client.get(f"/quiz/attempt/{self.attempt_id}").status_code, 200)
        self.assertEqual(self.submit(self.owner_client, self.attempt_id).status_code, 302)
        self.assertIsNotNone(self.attempt(self.attempt_id)["submitted_at"])


class RevokedAuthorityTest(AccountAttemptTestCase):
    def setUp(self):
        self.student = self.account(display_name="Спрян Ученик")
        self.assignment_id = self.assignment()
        self.client_ = self.client(self.student)
        self.attempt_id = self.start(self.client_, self.assignment_id)

    def set_active(self, active):
        with closing(web_app.quiz_db()) as conn:
            accounts_service.set_active(conn, self.student["id"], active)
            conn.commit()

    def test_disabled_account_loses_its_attempt_with_the_old_session(self):
        self.set_active(False)
        self.assert_denied_everywhere(self.client_, self.attempt_id, "disabled student")
        # The stale identity is anonymous: it cannot start, and the list asks for a sign-in.
        start = self.client_.post(student_entry_path(self.assignment_id), headers=SAME_ORIGIN)
        self.assertEqual(start.headers["Location"], f"/login?next={student_entry_path(self.assignment_id)}")
        self.assertEqual(self.client_.get("/account/attempts").headers["Location"],
                         "/login?next=/account/attempts")
        self.assertEqual(len(self.attempts_of(self.assignment_id)), 1)

    def test_disabled_flag_alone_revokes_even_with_the_current_session_key(self):
        # Not merely the key rotation: the validated principal checks is_active.
        self.execute("UPDATE users SET is_active = 0 WHERE id = ?", (self.student["id"],))
        self.assert_denied_everywhere(self.client(self.student), self.attempt_id, "inactive, same key")

    def test_re_enabling_does_not_revive_the_old_session(self):
        self.set_active(False)
        self.set_active(True)
        self.assert_denied_everywhere(self.client_, self.attempt_id, "old session after re-enable")
        key = self.query("SELECT session_key FROM users WHERE id = ?", (self.student["id"],))[0][0]
        fresh = self.client({"id": self.student["id"], "session_key": key})
        self.assertEqual(fresh.get(f"/quiz/attempt/{self.attempt_id}").status_code, 200)

    def test_a_raw_account_id_in_the_cookie_is_not_authority(self):
        for label, session_state in (
            ("uid only", {"uid": self.student["id"]}),
            ("wrong key", {"uid": self.student["id"], "sk": "x" * 43, "iat": int(time.time())}),
            ("expired", {"uid": self.student["id"], "sk": self.student["session_key"], "iat": 1}),
            ("user_id key", {"user_id": self.student["id"]}),
        ):
            client = web_app.app.test_client()
            with client.session_transaction() as session:
                session.update(session_state)
            self.assert_denied_everywhere(client, self.attempt_id, label)

    def test_a_role_change_ends_the_old_session_and_the_attempt_stays_the_accounts(self):
        with closing(web_app.quiz_db()) as conn:
            key = accounts_service.set_role(conn, self.student["id"], "teacher")
            conn.commit()
        # The re-keyed old session is nobody; the attempt follows the account id.
        self.assert_denied_everywhere(self.client_, self.attempt_id, "old session after role change")
        fresh = self.client({"id": self.student["id"], "session_key": key})
        self.assertEqual(fresh.get(f"/quiz/attempt/{self.attempt_id}").status_code, 200)
        self.assertEqual(self.attempt(self.attempt_id)["user_id"], self.student["id"])

    def test_pending_second_factor_has_no_attempt_authority(self):
        # A password-verified admin who has not given a code is nobody yet: it can
        # neither start a quiz nor reach the attempt the admin account already owns.
        # The pending keys never name any other account either.
        admin = self.account("admin")
        admin_attempt = self.start(self.client(admin), self.assignment_id)
        self.assertEqual(self.client(admin).get(f"/quiz/attempt/{admin_attempt}").status_code, 200)
        attempts_before = len(self.attempts_of(self.assignment_id))
        for label, account, attempt_id in (("pending admin", admin, admin_attempt),
                                           ("pending keys naming the student", self.student, self.attempt_id)):
            client = web_app.app.test_client()
            with client.session_transaction() as session:
                session.update(mfa_uid=account["id"], mfa_sk=account["session_key"], mfa_iat=int(time.time()))
            self.assert_denied_everywhere(client, attempt_id, label)
            start = client.post(student_entry_path(self.assignment_id), headers=SAME_ORIGIN)
            self.assertEqual(start.headers["Location"], f"/login?next={student_entry_path(self.assignment_id)}")
            self.assertEqual(client.get("/account/attempts").headers["Location"],
                             "/login?next=/account/attempts")
            self.assertIn("влез в своя профил", client.get(student_entry_path(self.assignment_id)).get_data(as_text=True))
        self.assertEqual(len(self.attempts_of(self.assignment_id)), attempts_before)

    def test_disabled_teacher_and_admin_lose_their_own_attempts_too(self):
        for role in ("teacher", "admin"):
            account = self.account(role)
            client = self.client(account)
            attempt_id = self.start(client, self.assignment_id)
            with closing(web_app.quiz_db()) as conn:
                accounts_service.set_active(conn, account["id"], False)
                conn.commit()
            self.assert_denied_everywhere(client, attempt_id, f"disabled {role}")
            start = client.post(student_entry_path(self.assignment_id), headers=SAME_ORIGIN)
            self.assertEqual(start.headers["Location"], f"/login?next={student_entry_path(self.assignment_id)}")
            self.assertEqual(self.attempt(attempt_id)["user_id"], account["id"])


class HistoricalAttemptTest(AccountAttemptTestCase):
    """user_id NULL: compatibility with the browser marker, never an account's."""

    def setUp(self):
        self.assignment_id = self.assignment()
        self.attempt_id = self.historical_attempt(self.assignment_id, "Иван Петров")

    def test_the_marker_keeps_working_for_an_anonymous_browser(self):
        client = self.client(markers=[self.attempt_id])
        self.assertEqual(client.get(f"/quiz/attempt/{self.attempt_id}").status_code, 200)
        self.assertEqual(client.get(self.practical_base(self.attempt_id)).status_code, 200)
        upload = self.upload(client, self.attempt_id)
        self.assertEqual(upload.status_code, 302)
        self.assertIn("uploaded=1", upload.headers["Location"])
        self.assertEqual(self.submit(client, self.attempt_id).status_code, 302)
        self.assertEqual(client.get(f"/quiz/attempt/{self.attempt_id}/result").status_code, 200)
        row = self.attempt(self.attempt_id)
        self.assertIsNotNone(row["submitted_at"])
        self.assertIsNone(row["user_id"])  # using it does not give it an owner

    def test_no_marker_or_another_attempts_marker_is_denied(self):
        other_attempt = self.historical_attempt(self.assignment_id, "Друг")
        self.assert_denied_everywhere(self.client(), self.attempt_id, "no marker")
        self.assert_denied_everywhere(self.client(markers=[other_attempt]), self.attempt_id, "wrong marker")

    def test_signing_in_with_the_same_name_does_not_claim_it(self):
        namesake = self.account(display_name="Иван Петров", email="ivan.petrov@example.bg", password=PASSWORD)
        client = self.client(namesake)
        self.assert_denied_everywhere(client, self.attempt_id, "namesake account")

        # Starting the assignment gives the account its own, new attempt.
        own_attempt = self.start(client, self.assignment_id)
        self.assertNotEqual(own_attempt, self.attempt_id)
        self.assertEqual(
            [(row["id"], row["user_id"], row["student_name"]) for row in self.attempts_of(self.assignment_id)],
            [(self.attempt_id, None, "Иван Петров"), (own_attempt, namesake["id"], "Иван Петров")])
        self.assert_denied_everywhere(client, self.attempt_id, "namesake account after starting")
        self.assertNotIn(f"/quiz/attempt/{self.attempt_id}", client.get("/account/attempts").get_data(as_text=True))

    def test_a_real_sign_in_does_not_claim_it_either(self):
        self.account(display_name="Иван Петров", email="ivan@example.bg", password=PASSWORD)
        client = self.client()
        login = client.post("/login", data={"email": "ivan@example.bg", "password": PASSWORD,
                                            "next": f"/quiz/attempt/{self.attempt_id}"}, headers=SAME_ORIGIN)
        self.assertEqual(login.headers["Location"], f"/quiz/attempt/{self.attempt_id}")
        self.assertEqual(client.get(login.headers["Location"]).status_code, 404)
        self.assertIsNone(self.attempt(self.attempt_id)["user_id"])

    def test_names_that_look_like_an_account_identity_do_not_claim_it(self):
        student = self.account(email="lookalike@example.bg", password=PASSWORD)
        for name in (f"user:{student['id']}", str(student["id"]), "lookalike@example.bg"):
            attempt_id = self.historical_attempt(self.assignment(), name)
            with self.subTest(name=name):
                self.assertEqual(self.client(student).get(f"/quiz/attempt/{attempt_id}").status_code, 404)
                self.assertIsNone(self.attempt(attempt_id)["user_id"])

    def test_a_signed_in_browser_still_needs_and_keeps_the_marker(self):
        student = self.account(display_name="Съвсем Друг", email="marker@example.bg", password=PASSWORD)
        client = self.client(markers=[self.attempt_id])
        # The marker survives a real sign-in (as before) and is still what decides.
        client.post("/login", data={"email": "marker@example.bg", "password": PASSWORD}, headers=SAME_ORIGIN)
        self.assertEqual(self.markers(client), [f"{MARKER_PREFIX}{self.attempt_id}"])
        self.assertEqual(client.get(f"/quiz/attempt/{self.attempt_id}").status_code, 200)
        self.assertEqual(self.submit(client, self.attempt_id).status_code, 302)
        self.assertIsNone(self.attempt(self.attempt_id)["user_id"])
        # The same account in a browser without the marker has nothing.
        self.assert_denied_everywhere(self.client(student), self.attempt_id, "same account, no marker")
        self.assertEqual(self.query(
            "SELECT COUNT(*) FROM quiz_attempts WHERE user_id = ?", (student["id"],))[0][0], 0)

    def test_teacher_and_admin_do_not_bypass_the_marker(self):
        for role in ("teacher", "admin"):
            self.assert_denied_everywhere(self.client(self.account(role)), self.attempt_id, role)

    def test_no_role_claims_it_by_starting_the_assignment(self):
        for role in ("student", "teacher", "admin"):
            with self.subTest(role=role):
                account = self.account(role, display_name="Иван Петров")
                client = self.client(account)
                own_attempt = self.start(client, self.assignment_id)
                self.assertNotEqual(own_attempt, self.attempt_id)
                self.assertEqual(client.get(f"/quiz/attempt/{self.attempt_id}").status_code, 404)
                self.assertNotIn(f"/quiz/attempt/{self.attempt_id}",
                                 client.get("/account/attempts").get_data(as_text=True))
        self.assertIsNone(self.attempt(self.attempt_id)["user_id"])

    def test_no_new_historical_attempt_can_be_started(self):
        before = self.query("SELECT COUNT(*) FROM quiz_attempts WHERE user_id IS NULL")[0][0]
        for client in (self.client(), self.client(markers=[self.attempt_id]), self.client(self.account()),
                       self.client(self.account("teacher")), self.client(self.account("admin"))):
            client.post(student_entry_path(self.assignment()), data={"student_name": "Нов Анонимен"},
                        headers=SAME_ORIGIN)
        self.assertEqual(self.query("SELECT COUNT(*) FROM quiz_attempts WHERE user_id IS NULL")[0][0], before)


class PracticalOwnershipTest(AccountAttemptTestCase):
    def setUp(self):
        self.teacher = self.account("teacher")
        self.student = self.account(display_name="Практик")
        self.assignment_id = self.assignment(owner=self.teacher)
        self.attempt_id = self.start(self.client(self.student), self.assignment_id)

    def test_owner_uploads_and_sees_the_work_from_another_browser(self):
        first = self.upload(self.client(self.student), self.attempt_id)
        self.assertEqual(first.status_code, 302)
        self.assertIn("uploaded=1", first.headers["Location"])

        other_browser = self.client(self.student)
        page = other_browser.get(self.practical_base(self.attempt_id))
        self.assertEqual(page.status_code, 200)
        self.assertIn("answer.xlsx", page.get_data(as_text=True))
        second = self.upload(other_browser, self.attempt_id)
        self.assertIn("uploaded=1", second.headers["Location"])

        submissions = self.query(
            "SELECT id, quiz_attempt_id FROM practical_submissions WHERE quiz_attempt_id = ?",
            (self.attempt_id,))
        self.assertEqual(len(submissions), 1)  # one submission, owned through the attempt
        self.assertEqual(self.query(
            "SELECT COUNT(*) FROM practical_submission_files WHERE practical_submission_id = ?",
            (submissions[0]["id"],))[0][0], 2)
        self.assertEqual(
            {row[1] for row in self.query("PRAGMA table_info(practical_submissions)")} & {"user_id", "owner_user_id"},
            set())

    def test_another_account_cannot_see_or_upload(self):
        self.assertIn("uploaded=1", self.upload(self.client(self.student), self.attempt_id).headers["Location"])
        files_before = self.uploaded_files()
        for label, client in (("other student", self.client(self.account())),
                              ("other student with marker", self.client(self.account(), markers=[self.attempt_id])),
                              ("anonymous", self.client()), ("owning teacher", self.client(self.teacher))):
            with self.subTest(who=label):
                self.assertEqual(client.get(self.practical_base(self.attempt_id)).status_code, 404)
                self.assertEqual(self.upload(client, self.attempt_id).status_code, 404)
        self.assertEqual(self.uploaded_files(), files_before)
        self.assertEqual(self.query(
            "SELECT COUNT(*) FROM practical_submission_files psf JOIN practical_submissions ps "
            "ON ps.id = psf.practical_submission_id WHERE ps.quiz_attempt_id = ?", (self.attempt_id,))[0][0], 1)

    def test_teacher_review_still_follows_the_assignment_owner(self):
        self.upload(self.client(self.student), self.attempt_id)
        file_id = self.query(
            "SELECT psf.id FROM practical_submission_files psf JOIN practical_submissions ps "
            "ON ps.id = psf.practical_submission_id WHERE ps.quiz_attempt_id = ?", (self.attempt_id,))[0][0]
        download = f"/teacher/practical/submission-file/{file_id}/download"
        owner = self.client(self.teacher)
        self.assertIn("Практик", owner.get("/teacher/practical-submissions").get_data(as_text=True))
        self.assertEqual(owner.get(download).status_code, 200)
        self.assertEqual(self.client(self.account("admin")).get(download).status_code, 200)
        other_teacher = self.client(self.account("teacher"))
        self.assertNotIn("Практик", other_teacher.get("/teacher/practical-submissions").get_data(as_text=True))
        self.assertEqual(other_teacher.get(download).status_code, 404)
        # The student who uploaded it has no review surface.
        self.assertEqual(self.client(self.student).get(download).status_code, 403)
        self.assertEqual(self.client(self.student).get("/teacher/practical-submissions").status_code, 403)


class MyAttemptsTest(AccountAttemptTestCase):
    def setUp(self):
        self.student = self.account(display_name="Списък Ученик")
        self.client_ = self.client(self.student)

    def page(self, client=None):
        response = (client or self.client_).get("/account/attempts")
        self.assertEqual(response.status_code, 200)
        return response.get_data(as_text=True)

    def test_empty_state(self):
        html = self.page()
        self.assertIn("Моите опити", html)
        self.assertIn("Още нямаш започнати тестове.", html)
        self.assertNotIn("<table", html)

    def test_lists_only_the_accounts_own_attempts(self):
        mine = self.start(self.client_, self.assignment(title="Моят тест"))
        other = self.account(display_name="Списък Ученик")  # even with the same name
        theirs = self.start(self.client(other), self.assignment(title="Чужд тест"))
        historical = self.historical_attempt(self.assignment(title="Исторически тест"), "Списък Ученик")

        html = self.page()

        self.assertIn("Моят тест", html)
        self.assertIn(f'href="/quiz/attempt/{mine}"', html)
        for title, attempt_id in (("Чужд тест", theirs), ("Исторически тест", historical)):
            self.assertNotIn(title, html)
            self.assertNotIn(f"/quiz/attempt/{attempt_id}", html)
        # A marker for the historical attempt does not put it in the account's list.
        self.assertNotIn("Исторически тест", self.page(self.client(self.student, markers=[historical])))
        self.assertIn("Чужд тест", self.page(self.client(other)))
        self.assertNotIn("Моят тест", self.page(self.client(other)))

    def test_newest_first_with_resume_and_result_links(self):
        attempts = [self.start(self.client_, self.assignment(title=f"Тест {n}")) for n in (1, 2, 3)]
        for attempt_id, started in zip(attempts, ("2026-03-01 10:00:00", "2026-03-03 10:00:00",
                                                  "2026-03-02 10:00:00")):
            self.execute("UPDATE quiz_attempts SET started_at = ? WHERE id = ?", (started, attempt_id))
        self.assertEqual(self.submit(self.client_, attempts[0]).status_code, 302)

        html = self.page()

        self.assertLess(html.index("Тест 2"), html.index("Тест 3"))
        self.assertLess(html.index("Тест 3"), html.index("Тест 1"))
        self.assertIn(f'<a href="/quiz/attempt/{attempts[1]}">Продължи</a>', html)
        self.assertIn(f'<a href="/quiz/attempt/{attempts[2]}">Продължи</a>', html)
        self.assertIn(f'<a href="/quiz/attempt/{attempts[0]}/result">Виж резултата</a>', html)
        self.assertIn("в процес", html)
        self.assertIn("предаден", html)
        row = self.attempt(attempts[0])
        self.assertIn(f"{row['score_correct']}/{row['score_total']}", html)
        self.assertIn("2026-03-03 10:00:00", html)
        # Every link leads to a page the account can open.
        self.assertEqual(self.client_.get(f"/quiz/attempt/{attempts[1]}").status_code, 200)
        self.assertEqual(self.client_.get(f"/quiz/attempt/{attempts[0]}/result").status_code, 200)

    def test_same_start_time_orders_by_newest_id(self):
        attempts = [self.start(self.client_, self.assignment(title=f"Едновременен {n}")) for n in (1, 2)]
        self.execute("UPDATE quiz_attempts SET started_at = '2026-04-01 09:00:00' WHERE user_id = ?",
                     (self.student["id"],))
        with closing(web_app.quiz_db()) as conn:
            listed = [row["id"] for row in quiz.fetch_account_attempts(conn, self.student["id"])]
        self.assertEqual(listed, sorted(attempts, reverse=True))

    def test_page_shows_no_management_data(self):
        teacher = self.account("teacher", display_name="Таен Учител")
        assignment_id = self.assignment(owner=teacher, title="Тест с учител")
        self.start(self.client(self.account(display_name="Съученик Таен")), assignment_id)
        self.start(self.client_, assignment_id)
        html = self.page()
        self.assertIn("Тест с учител", html)
        for hidden in ("Таен Учител", "Съученик Таен", f"/teacher/assignment/{assignment_id}", "results"):
            self.assertNotIn(hidden, html)

    def test_one_indexed_query_whatever_the_number_of_attempts(self):
        for n in range(4):
            self.start(self.client_, self.assignment(title=f"Много {n}"))
        with closing(web_app.quiz_db()) as conn:
            statements = []
            conn.set_trace_callback(statements.append)
            listed = quiz.fetch_account_attempts(conn, self.student["id"])
            conn.set_trace_callback(None)
            self.assertEqual(len(listed), 4)
            self.assertEqual(len(statements), 1, statements)  # no per-attempt lookups

            plan = " | ".join(row[3] for row in conn.execute("EXPLAIN QUERY PLAN " + statements[0]))
            self.assertIn("SEARCH qt USING INDEX idx_quiz_attempts_user (user_id=?)", plan)
            self.assertNotIn("SCAN", plan)
            self.assertNotIn("TEMP B-TREE", plan)  # the index already gives the order

            resume = " | ".join(row[3] for row in conn.execute(
                "EXPLAIN QUERY PLAN SELECT id, submitted_at FROM quiz_attempts "
                "WHERE assignment_id = ? AND user_id = ?", (1, self.student["id"])))
            self.assertIn("uniq_quiz_attempts_user (assignment_id=? AND user_id=?)", resume)
            self.assertNotIn("SCAN", resume)

    def test_listing_never_matches_missing_ids(self):
        self.historical_attempt(self.assignment(), "Без профил")
        with closing(web_app.quiz_db()) as conn:
            for user_id in (None, True, "1", 0):
                with self.subTest(user_id=user_id):
                    self.assertEqual(quiz.fetch_account_attempts(conn, user_id), [])

    def test_every_role_sees_only_its_own_attempts(self):
        assignment_id = self.assignment(title="Общ тест за всички роли")
        historical = self.historical_attempt(assignment_id, "Общо Име")
        accounts = {role: self.account(role, display_name="Общо Име") for role in ("student", "teacher", "admin")}
        attempts = {role: self.start(self.client(account), assignment_id) for role, account in accounts.items()}
        for role, account in accounts.items():
            with self.subTest(role=role):
                # Even holding the markers of every other attempt changes nothing.
                html = self.page(self.client(account, markers=[historical, *attempts.values()]))
                self.assertIn("Общ тест за всички роли", html)
                self.assertEqual(html.count("/quiz/attempt/"), 1)
                self.assertIn(f'href="/quiz/attempt/{attempts[role]}"', html)
                self.assertNotIn(f"/quiz/attempt/{historical}", html)
                with closing(web_app.quiz_db()) as conn:
                    self.assertEqual([row["id"] for row in quiz.fetch_account_attempts(conn, account["id"])],
                                     [attempts[role]])

    def test_empty_state_for_teacher_and_admin(self):
        for role in ("teacher", "admin"):
            with self.subTest(role=role):
                self.assertIn("Още нямаш започнати тестове.", self.page(self.client(self.account(role))))

    def test_anonymous_is_sent_to_the_sign_in(self):
        response = self.client().get("/account/attempts")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/login?next=/account/attempts")

    def test_navigation_link_is_shown_to_every_signed_in_role(self):
        for label, client in (("student", self.client_), ("teacher", self.client(self.account("teacher"))),
                              ("admin", self.client(self.account("admin")))):
            with self.subTest(who=label):
                html = client.get("/").get_data(as_text=True)
                self.assertIn('href="/account/attempts"', html)
                self.assertIn(">Моите опити</a>", html)
        anonymous = self.client().get("/").get_data(as_text=True)
        self.assertNotIn('href="/account/attempts"', anonymous)
        self.assertNotIn("Моите опити", anonymous)


class AttemptNotePathTest(AccountAttemptTestCase):
    def notes_dir(self):
        return Path(web_app.QUIZ_VAULT_PATH) / "Generated" / "Quizzes" / "attempts"

    def test_notes_are_written_to_the_temporary_vault_only(self):
        vault = Path(web_app.QUIZ_VAULT_PATH).resolve()
        self.assertNotIn((ROOT / "vault").resolve(), (vault, *vault.parents))
        self.assertEqual(Path(quiz.quiz_vault_path()).resolve(), vault)

    def test_historical_path_is_unchanged(self):
        path = quiz.quiz_attempt_note_path(7, "Иван Петров", "2026-05-06 10:00:00")
        self.assertEqual(path, self.notes_dir() / "2026-05-06-assignment-7-ivan-petrov.md")
        assignment_id = self.assignment()
        row = self.attempt(self.historical_attempt(assignment_id, "Иван Петров", submitted=True))
        self.assertEqual(
            quiz.quiz_attempt_row_note_path(row).name,
            f"{runtime_fixtures.FIXTURE_SUBMITTED_AT[:10]}-assignment-{assignment_id}-ivan-petrov.md")

    def test_account_attempt_path_is_derived_from_the_attempt_id(self):
        first = quiz.quiz_attempt_note_path(7, "Иван Петров", "2026-05-06", attempt_id=41)
        second = quiz.quiz_attempt_note_path(7, "Иван Петров", "2026-05-06", attempt_id=42)
        self.assertEqual(first.name, "2026-05-06-assignment-7-attempt-41-ivan-petrov.md")
        self.assertNotEqual(first, second)
        # A different name does not move it onto another attempt's path either.
        self.assertNotEqual(
            quiz.quiz_attempt_note_path(7, "attempt 42 Иван Петров", "2026-05-06", attempt_id=41), second)

    def test_two_accounts_with_one_name_write_two_notes(self):
        assignment_id = self.assignment(title="Сблъсък на имена")
        attempts = []
        for _ in range(2):
            client = self.client(self.account(display_name="Иван Петров"))
            attempt_id = self.start(client, assignment_id)
            self.assertEqual(self.submit(client, attempt_id).status_code, 302)
            attempts.append(attempt_id)
        historical = self.historical_attempt(assignment_id, "Иван Петров", submitted=True)

        paths = [quiz.quiz_attempt_row_note_path(self.attempt(attempt_id)) for attempt_id in attempts]
        self.assertEqual(len(set(paths)), 2)
        for attempt_id, path in zip(attempts, paths):
            self.assertIn(f"-attempt-{attempt_id}-", path.name)
            self.assertTrue(path.is_file(), path)
            self.assertEqual(path.parent, self.notes_dir())
        historical_path = quiz.quiz_attempt_row_note_path(self.attempt(historical))
        self.assertNotIn("-attempt-", historical_path.name)
        self.assertNotIn(historical_path, paths)

        # The assignment note links each attempt to its own note.
        with web_app.app.test_request_context("/"):
            quiz.quiz_write_assignment_note(assignment_id, "http://localhost/")
        note = next((Path(web_app.QUIZ_VAULT_PATH) / "Generated" / "Quizzes").glob(
            f"*-assignment-{assignment_id}-*.md")).read_text(encoding="utf-8")
        for path in (*paths, historical_path):
            self.assertEqual(note.count(f"[[{path.stem}|Иван Петров]]"), 1)


class ConcurrentStartTest(AccountAttemptTestCase):
    def test_simultaneous_starts_of_one_account_end_in_one_attempt(self):
        student = self.account(display_name="Нетърпелив")
        assignment_id = self.assignment()
        workers = 6
        barrier = threading.Barrier(workers)
        results, errors = [], []

        def start():
            try:
                with closing(web_app.quiz_db()) as conn:
                    assignment = quiz.quiz_fetch_assignment(conn, assignment_id)
                    barrier.wait(timeout=10)
                    results.append(quiz.quiz_start_or_resume_attempt(
                        conn, assignment, user_id=student["id"]))
            except Exception as exc:  # noqa: BLE001 - reported below
                errors.append(exc)

        threads = [threading.Thread(target=start) for _ in range(workers)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        self.assertEqual(errors, [])
        self.assertEqual(len(results), workers)
        rows = self.attempts_of(assignment_id)
        self.assertEqual(len(rows), 1)
        self.assertEqual({result.attempt_id for result in results}, {rows[0]["id"]})
        self.assertEqual(sum(result.created for result in results), 1)
        self.assertEqual(rows[0]["user_id"], student["id"])


if __name__ == "__main__":
    unittest.main()
