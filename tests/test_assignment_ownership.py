"""PR6 creation and detail boundary (PR7 management is in test_assignment_owner_routes)."""

from contextlib import closing
from unittest.mock import patch

from tests.auth_helpers import create_account, set_account_session
from tests.support import IsolatedAppTestCase, web_app
from web.access import ANONYMOUS, Principal, account_principal
from web.blueprints import teacher
from web.services import quiz


class AssignmentOwnershipTest(IsolatedAppTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        with closing(web_app.quiz_db()) as conn:
            # A real canonical section supports both creation branches.
            for row in conn.execute("SELECT id FROM curriculum_sections ORDER BY id"):
                if not quiz.quiz_section_question_ids(conn, row["id"]):
                    continue
                candidates = web_app.fetch_open_question_candidates(conn, section_id=row["id"])
                if candidates:
                    cls.section_id = row["id"]
                    cls.source_slug = candidates[0]["source_slug"]
                    break
            else:
                raise AssertionError("No canonical mixed section")

    def setUp(self):
        self.owner = create_account(web_app.QUIZ_DB_PATH, "teacher")
        self.other = create_account(web_app.QUIZ_DB_PATH, "teacher")
        self.admin = create_account(web_app.QUIZ_DB_PATH, "admin")
        self.student = create_account(web_app.QUIZ_DB_PATH, "student")
        note_patch = patch.object(teacher, "quiz_write_assignment_note")
        note_patch.start()
        self.addCleanup(note_patch.stop)

    def client(self, account=None, legacy=None):
        client = web_app.app.test_client()
        if account:
            set_account_session(client, account["id"], account["session_key"])
        if legacy:
            with client.session_transaction() as session:
                session[f"{legacy}_authenticated"] = True
        return client

    def seed_assignment(self, account=None):
        principal = (account_principal(account["id"], "teacher", "Учител") if account
                     else Principal(is_teacher=True, is_admin=False))
        with closing(web_app.quiz_db()) as conn:
            assignment_id = quiz.quiz_create_assignment(
                conn, principal=principal, section_id=self.section_id,
                title_bg="Тест", question_count=1)
            conn.commit()
        return assignment_id

    def assignment(self, assignment_id):
        with closing(web_app.quiz_db()) as conn:
            return quiz.quiz_fetch_assignment(conn, assignment_id)

    def post_new(self, client, *, mixed=False):
        data = {"section_id": self.section_id, "question_count": 2,
                "owner_user_id": self.other["id"], "owner_id": self.other["id"]}
        if mixed:
            data.update(open_count=1, source_slug=self.source_slug)
        response = client.post("/teacher/new?owner_user_id=999999", data=data,
                               headers={"Origin": "http://localhost"})
        self.assertEqual(response.status_code, 302)
        return int(response.headers["Location"].rsplit("/", 1)[1])

    def test_create_stamps_teacher_and_admin_principals_in_both_paths(self):
        for account in (self.owner, self.admin):
            for mixed in (False, True):
                with self.subTest(account=account["id"], mixed=mixed):
                    client = self.client(account)
                    # Valid uid overrides these forged legacy/role hints.
                    with client.session_transaction() as session:
                        session.update(admin_authenticated=True, role="admin", owner_user_id=self.other["id"])
                    assignment_id = self.post_new(client, mixed=mixed)
                    row = self.assignment(assignment_id)
                    self.assertEqual(row["owner_user_id"], account["id"])
                    self.assertEqual(bool(row["question_plan_json"]), mixed)
                    self.assertEqual(client.get(f"/teacher/assignment/{assignment_id}").status_code, 200)

    def test_legacy_creators_stay_unowned_in_both_paths(self):
        for legacy in ("tester", "admin"):
            for mixed in (False, True):
                with self.subTest(legacy=legacy, mixed=mixed):
                    client = self.client(legacy=legacy)
                    assignment_id = self.post_new(client, mixed=mixed)
                    self.assertIsNone(self.assignment(assignment_id)["owner_user_id"])
                    self.assertEqual(client.get(f"/teacher/assignment/{assignment_id}").status_code, 200)

    def test_creation_keeps_only_account_identity_in_session(self):
        client = self.client(self.owner)
        self.post_new(client)
        with client.session_transaction() as session:
            self.assertEqual(set(session), {"uid", "sk", "iat"})

    def test_duplicate_belongs_to_creator_not_source_or_form_owner(self):
        source_id = self.seed_assignment(self.owner)
        for account, legacy in ((self.admin, None), (None, "admin")):
            client = self.client(account, legacy)
            response = client.post(f"/teacher/assignment/{source_id}/duplicate",
                                   data={"owner_user_id": self.other["id"]},
                                   headers={"Origin": "http://localhost"})
            self.assertEqual(response.status_code, 302)
            new_id = int(response.headers["Location"].rsplit("/", 1)[1])
            self.assertNotEqual(new_id, source_id)
            self.assertEqual(self.assignment(new_id)["owner_user_id"], account["id"] if account else None)
            self.assertEqual(self.assignment(source_id)["owner_user_id"], self.owner["id"])

    def test_detail_matrix_and_missing_ids(self):
        owned = self.seed_assignment(self.owner)
        unowned = self.seed_assignment()
        for label, client, expected in (
            ("owner", self.client(self.owner), (200, 404)),
            ("other", self.client(self.other, "admin"), (404, 404)),
            ("admin", self.client(self.admin), (200, 200)),
            ("legacy tester", self.client(legacy="tester"), (404, 200)),
            ("legacy admin", self.client(legacy="admin"), (200, 200)),
        ):
            with self.subTest(label=label):
                for assignment_id, status in zip((owned, unowned), expected):
                    self.assertEqual(client.get(f"/teacher/assignment/{assignment_id}").status_code, status)
                self.assertEqual(client.get("/teacher/assignment/9999999").status_code, 404)

    def test_denied_detail_never_reads_attempts(self):
        assignment_id = self.seed_assignment(self.owner)
        with closing(web_app.quiz_db()) as conn:
            statements = []
            conn.set_trace_callback(statements.append)
            # View closes its connection, so no further DB access below.
            with patch.object(teacher, "quiz_db", return_value=conn):
                response = self.client(self.other).get(f"/teacher/assignment/{assignment_id}")
            self.assertEqual(response.status_code, 404)
            self.assertFalse(any("quiz_attempts" in s.lower() for s in statements))

    def test_disabled_owner_cannot_use_legacy_admin_keys(self):
        assignment_id = self.seed_assignment(self.owner)
        client = self.client(self.owner, "admin")
        with closing(web_app.quiz_db()) as conn:
            conn.execute("UPDATE users SET is_active = 0 WHERE id = ?", (self.owner["id"],))
            conn.commit()
        response = client.get(f"/teacher/assignment/{assignment_id}")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/tester/login", response.headers["Location"])
        self.assertEqual(self.assignment(assignment_id)["owner_user_id"], self.owner["id"])

    def test_student_and_anonymous_cannot_create_or_open_detail(self):
        assignment_id = self.seed_assignment(self.owner)
        for client in (self.client(), self.client(self.student, "admin")):
            self.assertEqual(client.get(f"/teacher/assignment/{assignment_id}").status_code, 302)
            response = client.post("/teacher/new", data={"section_id": self.section_id},
                                   headers={"Origin": "http://localhost"})
            self.assertEqual(response.status_code, 302)

    def test_reads_include_owner_and_predicate_adds_no_queries(self):
        assignment_id = self.seed_assignment(self.owner)
        with closing(web_app.quiz_db()) as conn:
            statements = []
            conn.set_trace_callback(statements.append)
            row = quiz.quiz_fetch_assignment(conn, assignment_id)
            self.assertEqual(row["owner_user_id"], self.owner["id"])
            for _ in range(20):
                self.assertTrue(quiz.quiz_can_access_assignment(
                    row, account_principal(self.owner["id"], "teacher", "Учител")))
                self.assertFalse(quiz.quiz_can_access_assignment(row, ANONYMOUS))
            self.assertEqual(len(statements), 1)
            plan = conn.execute("EXPLAIN QUERY PLAN SELECT * FROM quiz_assignments WHERE id = ?",
                                (assignment_id,)).fetchall()
            self.assertIn("INTEGER PRIMARY KEY", " ".join(r[3] for r in plan))

    def test_service_rejects_creation_without_teacher_capability(self):
        with closing(web_app.quiz_db()) as conn:
            with self.assertRaises(ValueError):
                quiz.quiz_create_assignment(conn, principal=ANONYMOUS, section_id=self.section_id,
                                            title_bg="Тест", question_count=1)
