"""Accounts PR7 — owner-scoped teacher assignment management.

An admin manages every assignment; a teacher account manages only the rows it
owns; an unowned (NULL-owner) row is admin-only, for viewing as well as for
management. Foreign, unowned and missing rows answer 404; a wrong role is refused
by the central route policy (403), an anonymous visitor is sent to ``/login``, and
the session keys of the removed shared-password logins grant nothing.
Student/public quiz loading is not scoped.

Accounts: Teacher A, Teacher B, an account admin, a student. Assignments: A
(owned by Teacher A), B (owned by Teacher B) and U (unowned).
"""

import ast
import os
import sqlite3
import subprocess
import sys
import unittest
from contextlib import closing
from datetime import timedelta
from unittest.mock import patch

from tests.auth_helpers import create_account, set_account_session
from tests.support import ROOT, IsolatedAppTestCase, web_app, student_entry_path
from tests.support import NO_DEADLINE_FORM
from tests.test_account_login import SAME_ORIGIN, AccountAppTestCase
from web import access
from web.access import ANONYMOUS, Principal, account_principal
from web.blueprints import teacher
from web.services import quiz

# Teacher capability without an account id. No request can have this principal;
# it seeds unowned rows and proves the predicates never match None with None.
IDLESS_TEACHER = Principal(is_teacher=True, is_admin=False)


def first_section_with_mc_questions(conn):
    for row in conn.execute("SELECT id FROM curriculum_sections ORDER BY id"):
        question_ids = quiz.quiz_section_question_ids(conn, row["id"])
        if question_ids:
            return int(row["id"]), [int(qid) for qid in question_ids]
    raise AssertionError("No canonical section with eligible MC questions")


class AssignmentManagementPredicateTest(unittest.TestCase):
    """The strict predicate in web.access, without routes or a database."""

    def test_admin_teacher_owner_and_everything_else(self):
        teacher_a = account_principal(11, "teacher", "А")
        cases = (
            ("account admin, any owner", account_principal(1, "admin", "Админ"), (11, 12, None)),
            ("teacher, own row", teacher_a, (11,)),
        )
        for label, principal, owners in cases:
            for owner in owners:
                with self.subTest(label=label, owner=owner):
                    self.assertTrue(access.can_manage_assignment(principal, owner))
        denied = (
            ("teacher, foreign row", teacher_a, 12),
            ("teacher, unowned row", teacher_a, None),
            ("teacher capability without an account id, unowned row", IDLESS_TEACHER, None),
            ("teacher capability without an account id, owned row", IDLESS_TEACHER, 11),
            ("anonymous, unowned row", ANONYMOUS, None),
            ("anonymous, owned row", ANONYMOUS, 11),
            ("student, unowned row", account_principal(13, "student", "У"), None),
            ("student, own id as owner", account_principal(13, "student", "У"), 13),
            ("unknown role", account_principal(14, "superuser", "X"), 14),
        )
        for label, principal, owner in denied:
            with self.subTest(label=label):
                self.assertFalse(access.can_manage_assignment(principal, owner))

    def test_a_missing_account_id_never_matches_a_missing_owner(self):
        # None == None must not authorize, whatever the capability flags say.
        for principal in (IDLESS_TEACHER, ANONYMOUS,
                          Principal(is_teacher=True, is_admin=False, user_id=None, role="teacher")):
            with self.subTest(principal=principal):
                self.assertFalse(access.can_manage_assignment(principal, None))
                self.assertEqual(access.assignment_management_scope(principal, "qa.owner_user_id"),
                                 ("1 = 0", ()))

    def test_sql_scope_mirrors_the_predicate(self):
        self.assertEqual(access.assignment_management_scope(IDLESS_TEACHER, "qa.owner_user_id"),
                         ("1 = 0", ()))
        self.assertEqual(
            access.assignment_management_scope(account_principal(1, "admin", "А"), "qa.owner_user_id"),
            ("1 = 1", ()))
        self.assertEqual(
            access.assignment_management_scope(account_principal(11, "teacher", "А"), "qa.owner_user_id"),
            ("qa.owner_user_id = ?", (11,)))
        self.assertEqual(
            access.assignment_management_scope(account_principal(13, "student", "У"), "qa.owner_user_id"),
            ("1 = 0", ()))

    def test_detail_view_is_exactly_the_management_predicate(self):
        owned = {"owner_user_id": 11}
        unowned = {"owner_user_id": None}
        teacher_a = account_principal(11, "teacher", "А")
        admin = account_principal(1, "admin", "Админ")
        self.assertTrue(quiz.quiz_can_access_assignment(owned, teacher_a))
        self.assertTrue(quiz.quiz_can_access_assignment(owned, admin))
        self.assertTrue(quiz.quiz_can_access_assignment(unowned, admin))
        # There is no view-only exception: an unowned row is not anyone's but an admin's.
        self.assertFalse(quiz.quiz_can_access_assignment(unowned, IDLESS_TEACHER))
        self.assertFalse(quiz.quiz_can_access_assignment(owned, IDLESS_TEACHER))
        self.assertFalse(quiz.quiz_can_access_assignment(unowned, teacher_a))
        self.assertFalse(quiz.quiz_can_access_assignment(unowned, ANONYMOUS))
        self.assertFalse(quiz.quiz_can_access_assignment(
            unowned, account_principal(13, "student", "У")))
        self.assertFalse(quiz.quiz_can_access_assignment(None, admin))

    def test_the_predicate_is_importable_without_controllers_or_services(self):
        # PR8 (practical review) reuses it from web.access without import cycles.
        tree = ast.parse((ROOT / "web" / "access.py").read_text(encoding="utf-8"))
        imported = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
        self.assertFalse({name for name in imported if name and name.startswith(("web.", "src."))})
        probe = (
            "import sys\n"
            "from web.access import ASSIGNMENT_OWNER, can_manage_assignment\n"
            "loaded = [m for m in sys.modules if m.startswith(('web.blueprints', 'web.services', 'web.app'))]\n"
            "assert not loaded, loaded\n"
        )
        result = subprocess.run([sys.executable, "-B", "-c", probe], cwd=str(ROOT),
                                capture_output=True, text=True,
                                env=dict(os.environ, PYTHONPATH=str(ROOT)))
        self.assertEqual(result.returncode, 0, result.stderr)


class AssignmentOwnerRoutesTest(IsolatedAppTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        web_app.app.config.update(TESTING=True)
        with closing(web_app.quiz_db()) as conn:
            cls.section_id, cls.question_ids = first_section_with_mc_questions(conn)

    def setUp(self):
        db_path = web_app.QUIZ_DB_PATH
        self.teacher_a = create_account(db_path, "teacher", display_name="Учител А")
        self.teacher_b = create_account(db_path, "teacher", display_name="Учител Б")
        self.admin = create_account(db_path, "admin", display_name="Админ")
        self.student = create_account(db_path, "student", display_name="Ученик")
        for module in (teacher, quiz):
            for name in ("quiz_write_assignment_note", "quiz_write_attempt_note"):
                if hasattr(module, name):
                    note_patch = patch.object(module, name)
                    note_patch.start()
                    self.addCleanup(note_patch.stop)
        self.a = self.seed_assignment(self.teacher_a, "PR7 задание А")
        self.b = self.seed_assignment(self.teacher_b, "PR7 задание Б")
        self.u = self.seed_assignment(None, "PR7 задание без собственик")
        self.answer_a = self.seed_text_answer(self.a, "Ученик А")
        self.answer_b = self.seed_text_answer(self.b, "Ученик Б")
        self.answer_u = self.seed_text_answer(self.u, "Ученик U")

    # --- fixtures ----------------------------------------------------------

    def seed_assignment(self, account, title):
        principal = (account_principal(account["id"], "teacher", "Учител") if account
                     else IDLESS_TEACHER)
        with closing(web_app.quiz_db()) as conn:
            assignment_id = quiz.quiz_create_assignment(
                conn, principal=principal, section_id=self.section_id,
                title_bg=title, question_count=1)
            conn.commit()
        return assignment_id

    def seed_text_answer(self, assignment_id, student_name):
        """A submitted attempt with one open answer; returns the answer id."""
        with closing(web_app.quiz_db()) as conn:
            attempt_id = conn.execute(
                "INSERT INTO quiz_attempts (assignment_id, student_name, seed, question_ids_json,"
                " submitted_at, score_correct, score_total)"
                " VALUES (?, ?, 'seed', ?, CURRENT_TIMESTAMP, 0, 1)",
                (assignment_id, student_name, f"[{self.question_ids[0]}]")).lastrowid
            answer_id = conn.execute(
                "INSERT INTO quiz_text_answers (attempt_id, question_id, subquestion_number,"
                " raw_answer, normalized_answer, points_possible)"
                " VALUES (?, ?, 1, 'отговор', 'отговор', 3)",
                (attempt_id, self.question_ids[0])).lastrowid
            conn.commit()
        return int(answer_id)

    def client(self, account=None, historical=None):
        client = web_app.app.test_client()
        if account:
            set_account_session(client, account["id"], account["session_key"])
        if historical:  # a session key of the removed shared-password logins
            with client.session_transaction() as session:
                session[f"{historical}_authenticated"] = True
        return client

    def row(self, assignment_id):
        with closing(web_app.quiz_db()) as conn:
            return conn.execute("SELECT * FROM quiz_assignments WHERE id = ?", (assignment_id,)).fetchone()

    def answer(self, answer_id):
        with closing(web_app.quiz_db()) as conn:
            return conn.execute("SELECT * FROM quiz_text_answers WHERE id = ?", (answer_id,)).fetchone()

    def assignment_count(self):
        with closing(web_app.quiz_db()) as conn:
            return conn.execute("SELECT COUNT(*) FROM quiz_assignments").fetchone()[0]

    def post(self, client, path, data=None):
        data = dict(data or {})
        if path.endswith(("/edit", "/duplicate")):
            # These forms must state their deadline choice; "no deadline" here.
            data = {**NO_DEADLINE_FORM, **data}
        return client.post(path, data=data, headers=SAME_ORIGIN)

    def management_requests(self, assignment_id, answer_id):
        """(label, callable(client) -> response) for every management surface."""
        base = f"/teacher/assignment/{assignment_id}"
        return (
            ("results", lambda c: c.get(f"{base}/results")),
            ("results.csv", lambda c: c.get(f"{base}/results.csv")),
            ("results.csv filtered", lambda c: c.get(f"{base}/results.csv?filtered=1")),
            ("edit", lambda c: self.post(c, f"{base}/edit",
                                         {"title_bg": "Променено", "time_limit_minutes": "5"})),
            ("duplicate", lambda c: self.post(c, f"{base}/duplicate")),
            ("review", lambda c: self.post(c, f"{base}/results",
                                           {"text_answer_id": answer_id, "teacher_override": "correct",
                                            "teacher_note": "бележка"})),
        )

    def assert_untouched(self, assignment_id, title, answer_id, count_before):
        self.assertEqual(self.row(assignment_id)["title_bg"], title)
        answer = self.answer(answer_id)
        self.assertEqual((answer["teacher_override"], answer["teacher_note"]), (0, None))
        self.assertEqual(self.assignment_count(), count_before)

    def list_body(self, client, query=""):
        response = client.get(f"/teacher/assignments{query}")
        self.assertEqual(response.status_code, 200)
        return response.get_data(as_text=True)

    # --- list --------------------------------------------------------------

    def test_list_is_owner_scoped_in_both_directions(self):
        detail = "/teacher/assignment/{}\""
        for account, own, others in ((self.teacher_a, self.a, (self.b, self.u)),
                                     (self.teacher_b, self.b, (self.a, self.u))):
            for query in ("", "?type=mc", "?type=all"):
                with self.subTest(account=account["id"], query=query):
                    body = self.list_body(self.client(account), query)
                    self.assertIn(detail.format(own), body)
                    for other in others:
                        self.assertNotIn(detail.format(other), body)
            # A mixed filter on top of the owner scope cannot widen it.
            body = self.list_body(self.client(account), "?type=mixed")
            for other in (self.a, self.b, self.u):
                self.assertNotIn(detail.format(other), body)

    def test_admin_lists_every_assignment(self):
        body = self.list_body(self.client(self.admin))
        for assignment_id in (self.a, self.b, self.u):
            self.assertIn(f"/teacher/assignment/{assignment_id}\"", body)
        # The key of the removed shared admin login lists nothing: it is not signed in.
        response = self.client(historical="admin").get("/teacher/assignments")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/login?next=/teacher/assignments")

    def test_list_query_is_bounded_and_uses_the_owner_index(self):
        def traced_list(account):
            conn = web_app.quiz_db()
            statements = []
            conn.set_trace_callback(statements.append)
            with patch.object(quiz, "quiz_db", return_value=conn):
                self.list_body(self.client(account))
            return [s for s in statements if s.lstrip().upper().startswith("SELECT")]

        before = traced_list(self.teacher_a)
        for index in range(6):
            self.seed_assignment(self.teacher_a, f"Още {index}")
        after = traced_list(self.teacher_a)
        # One list query, plus the single lifecycle clock row the listed states are
        # computed with. Neither grows with the number of assignments.
        self.assertEqual(len(before), 2, before)
        self.assertEqual(len(after), len(before))
        list_queries = [s for s in after if "FROM quiz_assignments" in s]
        self.assertEqual(len(list_queries), 1, after)
        self.assertIn("owner_user_id =", list_queries[0])
        self.assertEqual([s for s in after if s not in list_queries],
                         [s for s in after if "FROM lifecycle_clock" in s])

        with closing(web_app.quiz_db()) as conn:
            sql = list_queries[0]
            params = (self.teacher_a["id"],) if "?" in sql else ()
            plan = " ".join(row[3] for row in conn.execute(f"EXPLAIN QUERY PLAN {sql}", params))
        self.assertIn("idx_quiz_assignments_owner", plan)

    # --- teacher matrix ----------------------------------------------------

    def test_teacher_manages_own_assignment(self):
        client = self.client(self.teacher_a)
        detail = client.get(f"/teacher/assignment/{self.a}")
        self.assertEqual(detail.status_code, 200)
        body = detail.get_data(as_text=True)
        for control in ("/edit", "/duplicate", "/results"):
            self.assertIn(f"/teacher/assignment/{self.a}{control}", body)

        results = client.get(f"/teacher/assignment/{self.a}/results")
        self.assertEqual(results.status_code, 200)
        self.assertIn("Ученик А", results.get_data(as_text=True))
        export = client.get(f"/teacher/assignment/{self.a}/results.csv")
        self.assertEqual(export.status_code, 200)
        self.assertIn("Ученик А", export.get_data(as_text=True))
        self.assertNotIn("Ученик Б", export.get_data(as_text=True))

        review = self.post(client, f"/teacher/assignment/{self.a}/results",
                           {"text_answer_id": self.answer_a, "teacher_override": "correct"})
        self.assertEqual(review.status_code, 302)
        self.assertEqual(self.answer(self.answer_a)["teacher_override_status"], "correct")

        edit = self.post(client, f"/teacher/assignment/{self.a}/edit",
                         {"title_bg": "Ново заглавие", "time_limit_minutes": "7"})
        self.assertEqual(edit.status_code, 302)
        self.assertEqual(self.row(self.a)["title_bg"], "Ново заглавие")

    def test_teacher_gets_404_for_foreign_and_unowned_assignments(self):
        for account, targets in ((self.teacher_a, ((self.b, self.answer_b), (self.u, self.answer_u))),
                                 (self.teacher_b, ((self.a, self.answer_a), (self.u, self.answer_u)))):
            client = self.client(account)
            for assignment_id, answer_id in targets:
                title = self.row(assignment_id)["title_bg"]
                count = self.assignment_count()
                with self.subTest(account=account["id"], assignment=assignment_id, surface="detail"):
                    self.assertEqual(client.get(f"/teacher/assignment/{assignment_id}").status_code, 404)
                for label, request in self.management_requests(assignment_id, answer_id):
                    with self.subTest(account=account["id"], assignment=assignment_id, surface=label):
                        self.assertEqual(request(client).status_code, 404)
                self.assert_untouched(assignment_id, title, answer_id, count)
            with self.subTest(account=account["id"], surface="missing"):
                for label, request in self.management_requests(9999999, self.answer_a):
                    self.assertEqual(request(client).status_code, 404, label)

    def test_admin_manages_every_assignment(self):
        client = self.client(self.admin)
        for assignment_id, answer_id in ((self.a, self.answer_a), (self.b, self.answer_b),
                                         (self.u, self.answer_u)):
            with self.subTest(assignment=assignment_id):
                self.assertEqual(client.get(f"/teacher/assignment/{assignment_id}").status_code, 200)
                for label, request in self.management_requests(assignment_id, answer_id):
                    expected = 302 if label in ("edit", "duplicate", "review") else 200
                    self.assertEqual(request(client).status_code, expected, label)

    # --- historical shared-login session keys --------------------------------

    def test_historical_shared_login_keys_grant_no_view_and_no_management(self):
        # Including the unowned row the removed tester login could once open view-only.
        for historical in ("tester", "admin"):
            client = self.client(historical=historical)
            for assignment_id in (self.u, self.a, self.b):
                with self.subTest(historical=historical, assignment=assignment_id, surface="detail"):
                    self.assert_sent_to_login(client.get(f"/teacher/assignment/{assignment_id}"))
            self.assert_sent_to_login(client.get("/teacher/assignments"))
            for assignment_id, answer_id in ((self.u, self.answer_u), (self.a, self.answer_a)):
                title = self.row(assignment_id)["title_bg"]
                count = self.assignment_count()
                for label, request in self.management_requests(assignment_id, answer_id):
                    with self.subTest(historical=historical, assignment=assignment_id, surface=label):
                        self.assert_sent_to_login(request(client))
                self.assert_untouched(assignment_id, title, answer_id, count)

    # --- wrong role, anonymous, disabled ----------------------------------

    def assert_sent_to_login(self, response):
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].startswith("/login?next=/teacher/"))

    def assert_refused(self, response):
        self.assertEqual(response.status_code, 403)
        self.assertNotIn("Location", response.headers)

    def test_student_and_anonymous_are_denied_before_any_lookup(self):
        # Anonymous is asked to sign in; a signed-in student is refused (403), and the
        # removed shared admin key next to its account changes nothing.
        for label, client, check in (
            ("anonymous", self.client(), self.assert_sent_to_login),
            ("student", self.client(self.student), self.assert_refused),
            ("student with a historical admin key", self.client(self.student, "admin"), self.assert_refused),
        ):
            with self.subTest(principal=label, surface="list"):
                check(client.get("/teacher/assignments"))
            title = self.row(self.a)["title_bg"]
            count = self.assignment_count()
            # Missing ids are denied by role too: no ownership 404 before the policy ran.
            for assignment_id in (self.a, self.u, 9999999):
                for surface, request in self.management_requests(assignment_id, self.answer_a):
                    with self.subTest(principal=label, assignment=assignment_id, surface=surface):
                        check(request(client))
            self.assert_untouched(self.a, title, self.answer_a, count)

    def test_disabled_teacher_has_no_stale_management_authority(self):
        client = self.client(self.teacher_a)
        self.assertEqual(client.get("/teacher/assignments").status_code, 200)
        with closing(web_app.quiz_db()) as conn:
            conn.execute("UPDATE users SET is_active = 0 WHERE id = ?", (self.teacher_a["id"],))
            conn.commit()
        title = self.row(self.a)["title_bg"]
        count = self.assignment_count()
        self.assertEqual(client.get("/teacher/assignments").status_code, 302)
        for label, request in self.management_requests(self.a, self.answer_a):
            with self.subTest(surface=label):
                self.assertEqual(request(self.client(self.teacher_a)).status_code, 302)
        self.assert_untouched(self.a, title, self.answer_a, count)

    # --- forged owner, duplicate owner, child substitution ----------------

    def test_forged_owner_on_edit_never_transfers_ownership(self):
        forged = {"title_bg": "Подправено", "time_limit_minutes": "9",
                  "owner_user_id": self.teacher_b["id"], "owner_id": self.teacher_b["id"]}
        for account, assignment_id, owner in ((self.teacher_a, self.a, self.teacher_a["id"]),
                                              (self.admin, self.u, None)):
            with self.subTest(account=account["id"]):
                response = self.client(account).post(
                    f"/teacher/assignment/{assignment_id}/edit?owner_user_id={self.teacher_b['id']}",
                    data={**NO_DEADLINE_FORM, **forged}, headers=SAME_ORIGIN)
                self.assertEqual(response.status_code, 302)
                row = self.row(assignment_id)
                self.assertEqual(row["title_bg"], "Подправено")
                self.assertEqual(row["owner_user_id"], owner)

    def test_duplicate_belongs_to_the_actor(self):
        for account, source in ((self.teacher_a, self.a), (self.admin, self.a), (self.admin, self.u)):
            with self.subTest(account=account["id"], source=source):
                response = self.post(self.client(account), f"/teacher/assignment/{source}/duplicate",
                                     {"owner_user_id": self.teacher_b["id"]})
                self.assertEqual(response.status_code, 302)
                new_id = int(response.headers["Location"].rsplit("/", 1)[1])
                self.assertNotEqual(new_id, source)
                self.assertEqual(self.row(new_id)["owner_user_id"], account["id"])
        self.assertEqual(self.row(self.a)["owner_user_id"], self.teacher_a["id"])
        self.assertIsNone(self.row(self.u)["owner_user_id"])

        count = self.assignment_count()
        for source in (self.b, self.u):
            response = self.post(self.client(self.teacher_a), f"/teacher/assignment/{source}/duplicate")
            self.assertEqual(response.status_code, 404)
        self.assertEqual(self.assignment_count(), count)

    def test_text_answer_ids_cannot_be_substituted_across_assignments(self):
        review = {"teacher_override": "correct", "teacher_note": "чужд", "points_awarded": "3"}
        cases = (
            # Teacher A's own URL with Teacher B's (or an unowned) answer id.
            (self.teacher_a, self.a, self.answer_b),
            (self.teacher_a, self.a, self.answer_u),
            # Teacher A's own answer id through Teacher B's URL.
            (self.teacher_a, self.b, self.answer_a),
            (self.teacher_a, self.b, self.answer_b),
            # Even an admin cannot review an answer through another assignment's URL.
            (self.admin, self.a, self.answer_b),
        )
        for account, assignment_id, answer_id in cases:
            with self.subTest(account=account["id"], assignment=assignment_id, answer=answer_id):
                response = self.post(self.client(account), f"/teacher/assignment/{assignment_id}/results",
                                     {"text_answer_id": answer_id, **review})
                self.assertEqual(response.status_code, 404)
        for answer_id in (self.answer_a, self.answer_b, self.answer_u):
            answer = self.answer(answer_id)
            self.assertEqual((answer["teacher_override"], answer["teacher_note"]), (0, None))

    # --- navigation --------------------------------------------------------

    def test_navigation_link_follows_the_list_capability(self):
        for label, client, shown in (
            ("teacher", self.client(self.teacher_a), True),
            ("account admin", self.client(self.admin), True),
            ("student", self.client(self.student), False),
            ("historical tester key", self.client(historical="tester"), False),
            ("historical admin key", self.client(historical="admin"), False),
            ("anonymous", self.client(), False),
        ):
            with self.subTest(principal=label):
                body = client.get("/").get_data(as_text=True)
                self.assertEqual('href="/teacher/assignments"' in body, shown)
        self.assertIn("Моите тестове", self.client(self.teacher_a).get("/").get_data(as_text=True))

    # --- student/public quiz flow ------------------------------------------

    def test_quiz_flow_is_not_owner_scoped(self):
        # Any account takes any assignment, whoever owns it (or nobody): a
        # student, a teacher who does not own it, its owner, the admin.
        for label, account in (("student account", self.student), ("teacher B as a visitor", self.teacher_b),
                               ("owning teacher A", self.teacher_a), ("admin", self.admin)):
            for assignment_id in (self.a, self.b, self.u):
                with self.subTest(visitor=label, assignment=assignment_id):
                    client = self.client(account)
                    self.assertEqual(client.get(student_entry_path(assignment_id)).status_code, 200)
                    start = self.post(client, student_entry_path(assignment_id), {"student_name": f"Иван {label}"})
                    self.assertEqual(start.status_code, 302)
                    attempt_path = start.headers["Location"]
                    self.assertRegex(attempt_path, r"^/quiz/attempt/\d+$")
                    with closing(web_app.quiz_db()) as conn:
                        owner = conn.execute("SELECT user_id FROM quiz_attempts WHERE id = ?",
                                             (int(attempt_path.rsplit("/", 1)[1]),)).fetchone()[0]
                    self.assertEqual(owner, account["id"])
                    page = client.get(attempt_path)
                    self.assertEqual(page.status_code, 200)
                    question_id = self.first_rendered_question(attempt_path)
                    submit = self.post(client, attempt_path, {f"q_{question_id}": "A"})
                    self.assertEqual(submit.status_code, 302)
                    self.assertTrue(submit.headers["Location"].endswith("/result"))
                    self.assertEqual(client.get(submit.headers["Location"]).status_code, 200)

    def test_anonymous_cannot_start_an_attempt(self):
        # The entry page is public; starting needs an account.
        client = self.client()
        with closing(web_app.quiz_db()) as conn:
            before = conn.execute("SELECT COUNT(*) FROM quiz_attempts").fetchone()[0]
        self.assertEqual(client.get(student_entry_path(self.a)).status_code, 200)
        start = self.post(client, student_entry_path(self.a), {"student_name": "Иван анонимен"})
        self.assertEqual(start.status_code, 302)
        self.assertEqual(start.headers["Location"], f"/login?next={student_entry_path(self.a)}")
        with closing(web_app.quiz_db()) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM quiz_attempts").fetchone()[0], before)

    def first_rendered_question(self, attempt_path):
        attempt_id = int(attempt_path.rsplit("/", 1)[1])
        with closing(web_app.quiz_db()) as conn:
            attempt = quiz.quiz_fetch_attempt(conn, attempt_id)
            plan = quiz.quiz_parse_attempt_question_plan(attempt["question_ids_json"])
        return plan["question_ids"][0]


class AssignmentOwnerAdminTotpTest(AccountAppTestCase):
    """PR #313: a pending second factor has no assignment authority at all."""

    def setUp(self):
        super().setUp()
        for name in ("quiz_write_assignment_note",):
            note_patch = patch.object(teacher, name)
            note_patch.start()
            self.addCleanup(note_patch.stop)
        with closing(web_app.quiz_db()) as conn:
            section_id, _ = first_section_with_mc_questions(conn)
        self.teacher_a = self.make("teacher")
        self.assignments = []
        for principal in (account_principal(self.teacher_a["id"], "teacher", "А"), IDLESS_TEACHER):
            with closing(web_app.quiz_db()) as conn:
                self.assignments.append(quiz.quiz_create_assignment(
                    conn, principal=principal, section_id=section_id,
                    title_bg="TOTP PR7", question_count=1))
                conn.commit()

    def count(self):
        return self.query("SELECT COUNT(*) FROM quiz_assignments")[0][0]

    def assert_no_authority(self):
        count = self.count()
        self.assertEqual(self.client.get("/teacher/assignments").status_code, 302)
        for assignment_id in self.assignments:
            base = f"/teacher/assignment/{assignment_id}"
            for path in (base, f"{base}/results", f"{base}/results.csv"):
                with self.subTest(path=path):
                    self.assertEqual(self.client.get(path).status_code, 302)
            for suffix, data in (("edit", {"title_bg": "MFA", "time_limit_minutes": "3"}),
                                 ("duplicate", {}), ("results", {"text_answer_id": "1"})):
                with self.subTest(path=f"{base}/{suffix}"):
                    response = self.client.post(f"{base}/{suffix}", data=data, headers=SAME_ORIGIN)
                    self.assertEqual(response.status_code, 302)
                    self.assertIn("login", response.headers["Location"])
            self.assertEqual(self.query("SELECT title_bg FROM quiz_assignments WHERE id = ?",
                                        (assignment_id,))[0][0], "TOTP PR7")
        self.assertEqual(self.count(), count)

    def assert_global_authority(self, admin):
        body = self.client.get("/teacher/assignments").get_data(as_text=True)
        for assignment_id in self.assignments:
            self.assertIn(f"/teacher/assignment/{assignment_id}\"", body)
            base = f"/teacher/assignment/{assignment_id}"
            for path in (base, f"{base}/results", f"{base}/results.csv"):
                self.assertEqual(self.client.get(path).status_code, 200, path)
        response = self.client.post(f"/teacher/assignment/{self.assignments[0]}/duplicate",
                                    headers=SAME_ORIGIN, data=NO_DEADLINE_FORM)
        self.assertEqual(response.status_code, 302)
        new_id = int(response.headers["Location"].rsplit("/", 1)[1])
        self.assertEqual(self.query("SELECT owner_user_id FROM quiz_assignments WHERE id = ?",
                                    (new_id,))[0][0], admin["id"])
        response = self.client.post(f"/teacher/assignment/{self.assignments[1]}/edit",
                                    data={**NO_DEADLINE_FORM, "title_bg": "След MFA", "time_limit_minutes": ""},
                                    headers=SAME_ORIGIN)
        self.assertEqual(response.status_code, 302)

    def test_pending_mfa_has_no_authority_until_the_totp_code(self):
        admin = self.make("admin")
        self.now += timedelta(seconds=31)
        self.assertEqual(self.login(admin["email"]).status_code, 200)
        self.assertIn("mfa_uid", self.session())
        self.assert_no_authority()
        response = self.submit_code(self.code(admin))
        self.assertEqual(response.status_code, 302)
        self.assert_global_authority(admin)

    def test_pending_mfa_has_no_authority_until_a_recovery_code(self):
        admin = self.make("admin")
        self.now += timedelta(seconds=31)
        self.assertEqual(self.login(admin["email"]).status_code, 200)
        self.assert_no_authority()
        response = self.submit_code(admin["recovery_codes"][0])
        self.assertEqual(response.status_code, 302)
        self.assert_global_authority(admin)


if __name__ == "__main__":
    unittest.main()
