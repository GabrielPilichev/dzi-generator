"""Accounts PR8 — owner-scoped practical review and submission-file download.

The assignment is the ownership boundary: file -> submission -> attempt ->
assignment -> ``owner_user_id``, decided by the PR7 predicate
(``web.access.can_manage_assignment``) and its SQL counterpart. An admin sees and
downloads everything; a teacher account only what belongs to assignments it
owns; an unowned (NULL) assignment is admin-only. Foreign, unowned and missing
files answer 404 before the file is looked at on disk; a wrong role is refused by
the central route policy (403) and an anonymous visitor is sent to ``/login``. The
session keys of the removed shared-password logins grant no practical-review authority.
Student practical pages and uploads are not owner-scoped.

Accounts: Teacher A, Teacher B, an account admin, a student. Assignments: A
(owned by Teacher A), B (owned by Teacher B) and U (unowned).
"""

import io
import tempfile
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from tests.auth_helpers import create_account, set_account_session
from tests.support import IsolatedAppTestCase, web_app
from tests.test_account_login import SAME_ORIGIN
from web import access
from web.access import ANONYMOUS, Principal, account_principal
from web.blueprints import practical as practical_blueprint
from web.services import dzi_exams
from web.services import practical as practical_service
from web.services import quiz

# Teacher capability without an account id. No request can have this principal;
# it seeds the unowned assignment and proves the scope never matches None with None.
IDLESS_TEACHER = Principal(is_teacher=True, is_admin=False)
LIST_PATH = "/teacher/practical-submissions"


def download_path(file_id):
    return f"/teacher/practical/submission-file/{file_id}/download"


class PracticalReviewOwnershipTest(IsolatedAppTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        web_app.app.config.update(TESTING=True)
        with closing(web_app.quiz_db()) as conn:
            for row in conn.execute("SELECT id FROM curriculum_sections ORDER BY id"):
                if quiz.quiz_section_question_ids(conn, row["id"]):
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

    def setUp(self):
        db_path = web_app.QUIZ_DB_PATH
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        self.upload_root = Path(temp_dir.name) / "uploads" / "practical"
        self.upload_root.mkdir(parents=True)
        old_upload_root = web_app.app.config["PRACTICAL_UPLOAD_ROOT"]
        web_app.app.config["PRACTICAL_UPLOAD_ROOT"] = str(self.upload_root)
        self.addCleanup(web_app.app.config.__setitem__, "PRACTICAL_UPLOAD_ROOT", old_upload_root)
        for name in ("quiz_write_assignment_note", "quiz_write_attempt_note"):
            if hasattr(quiz, name):
                note_patch = patch.object(quiz, name)
                note_patch.start()
                self.addCleanup(note_patch.stop)

        self.teacher_a = create_account(db_path, "teacher", display_name="Учител А")
        self.teacher_b = create_account(db_path, "teacher", display_name="Учител Б")
        self.admin = create_account(db_path, "admin", display_name="Админ")
        self.student = create_account(db_path, "student", display_name="Ученик")
        self.a = self.seed_assignment(self.teacher_a, "PR8 задание А")
        self.b = self.seed_assignment(self.teacher_b, "PR8 задание Б")
        self.u = self.seed_assignment(None, "PR8 задание без собственик")
        self.file_a = self.seed_file(self.a, "Практик А", "solution-a.xlsx", b"content of A")
        self.file_b = self.seed_file(self.b, "Практик Б", "solution-b.xlsx", b"content of B")
        self.file_u = self.seed_file(self.u, "Практик U", "solution-u.xlsx", b"content of U")
        self.files = {"a": self.file_a, "b": self.file_b, "u": self.file_u}

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

    def seed_file(self, assignment_id, student_name, filename, content, *, is_deleted=0):
        """An attempt with one practical submission and one stored file on disk."""
        with closing(web_app.quiz_db()) as conn:
            attempt_id = conn.execute(
                "INSERT INTO quiz_attempts (assignment_id, student_name, seed, question_ids_json)"
                " VALUES (?, ?, 'seed', '[]')", (assignment_id, student_name)).lastrowid
            submission_id = conn.execute(
                "INSERT INTO practical_submissions (quiz_attempt_id, exam_task_id) VALUES (?, ?)",
                (attempt_id, self.exam_task_id)).lastrowid
            relative = Path(str(attempt_id)) / str(self.exam_task_id) / str(submission_id) / "stored.xlsx"
            file_id = conn.execute(
                "INSERT INTO practical_submission_files (practical_submission_id, stored_path,"
                " original_filename, size_bytes, is_deleted) VALUES (?, ?, ?, ?, ?)",
                (submission_id, str(Path("data/uploads/practical") / relative), filename,
                 len(content), is_deleted)).lastrowid
            conn.commit()
        disk_path = self.upload_root / relative
        disk_path.parent.mkdir(parents=True)
        disk_path.write_bytes(content)
        return {"id": int(file_id), "attempt_id": int(attempt_id), "student": student_name,
                "filename": filename, "content": content, "path": disk_path}

    def client(self, account=None, historical=None):
        """``historical`` injects a session key of the removed shared-password logins
        (``tester``/``admin``), which must never change what the client may do."""
        client = web_app.app.test_client()
        if account:
            set_account_session(client, account["id"], account["session_key"])
        if historical:
            with client.session_transaction() as session:
                session[f"{historical}_authenticated"] = True
        return client

    def update_user(self, account, assignment_sql, *params):
        with closing(web_app.quiz_db()) as conn:
            conn.execute(f"UPDATE users SET {assignment_sql} WHERE id = ?", (*params, account["id"]))
            conn.commit()

    def list_body(self, client):
        response = client.get(LIST_PATH)
        self.assertEqual(response.status_code, 200)
        return response.get_data(as_text=True)

    def assert_lists_exactly(self, client, visible):
        body = self.list_body(client)
        for key, stored in self.files.items():
            for marker in (stored["student"], stored["filename"], download_path(stored["id"])):
                with self.subTest(file=key, marker=marker):
                    if key in visible:
                        self.assertIn(marker, body)
                    else:
                        self.assertNotIn(marker, body)

    def assert_downloads(self, client, stored):
        response = client.get(download_path(stored["id"]))
        try:
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.data, stored["content"])
            self.assertIn(stored["filename"], response.headers["Content-Disposition"])
        finally:
            response.close()

    def assert_login_redirect(self, client, path):
        response = client.get(path)
        self.assertEqual(response.status_code, 302, path)
        self.assertEqual(response.headers["Location"], f"/login?next={path}")

    def assert_refused(self, client, path):
        response = client.get(path)
        self.assertEqual(response.status_code, 403, path)
        self.assertNotIn("Location", response.headers)

    def record_filesystem_access(self):
        """Record every look at the upload tree: the path helper, ``send_file``
        and the ``Path`` calls the download and the list rely on."""
        touched = []
        root = str(self.upload_root)

        def recording(name):
            original = getattr(Path, name)

            def call(path, *args, **kwargs):
                if root in str(path):
                    touched.append((name, str(path)))
                return original(path, *args, **kwargs)

            return call

        for name in ("is_file", "exists", "stat", "resolve", "open", "read_bytes"):
            method_patch = patch.object(Path, name, recording(name))
            method_patch.start()
            self.addCleanup(method_patch.stop)

        real_helper = practical_service.practical_uploaded_file_safe_path

        def helper(stored_path, upload_root):
            touched.append(("safe_path", stored_path))
            return real_helper(stored_path, upload_root)

        real_send_file = practical_blueprint.send_file

        def send_file(path, *args, **kwargs):
            touched.append(("send_file", str(path)))
            return real_send_file(path, *args, **kwargs)

        for target, name, replacement in (
            (practical_service, "practical_uploaded_file_safe_path", helper),
            (practical_blueprint, "send_file", send_file),
        ):
            target_patch = patch.object(target, name, replacement)
            target_patch.start()
            self.addCleanup(target_patch.stop)
        return touched

    # --- list --------------------------------------------------------------

    def test_list_is_owner_scoped_in_both_directions(self):
        self.assert_lists_exactly(self.client(self.teacher_a), {"a"})
        self.assert_lists_exactly(self.client(self.teacher_b), {"b"})

    def test_admin_lists_every_submission(self):
        self.assert_lists_exactly(self.client(self.admin), {"a", "b", "u"})
        # The key of the removed shared admin login lists nothing: it is not signed in.
        self.assert_login_redirect(self.client(historical="admin"), LIST_PATH)

    def test_teacher_without_assignments_gets_an_empty_list(self):
        newcomer = create_account(web_app.QUIZ_DB_PATH, "teacher", display_name="Нов учител")
        self.assert_lists_exactly(self.client(newcomer), set())

    def test_list_scope_is_one_sql_query_whatever_the_row_count(self):
        def traced_list(account):
            conn = web_app.quiz_db()
            statements = []
            conn.set_trace_callback(statements.append)
            with patch.object(practical_blueprint, "_request_db", return_value=conn):
                self.list_body(self.client(account))
            return [s for s in statements if s.lstrip().upper().startswith("SELECT")]

        before = traced_list(self.teacher_a)
        for index in range(5):
            self.seed_file(self.a, f"Още ученик {index}", f"more-{index}.xlsx", b"x")
            self.seed_file(self.b, f"Чужд ученик {index}", f"foreign-{index}.xlsx", b"y")
        after = traced_list(self.teacher_a)
        self.assertEqual(len(before), 1, before)
        self.assertEqual(len(after), 1, after)
        self.assertIn("qa.owner_user_id =", after[0])
        self.assertNotIn("IS ?", after[0])

        admin_statements = traced_list(self.admin)
        self.assertEqual(len(admin_statements), 1, admin_statements)
        self.assertNotIn("owner_user_id =", admin_statements[0])

    def test_list_never_looks_at_foreign_or_unowned_files_on_disk(self):
        touched = self.record_filesystem_access()
        self.list_body(self.client(self.teacher_a))
        seen = " ".join(path for _, path in touched)
        self.assertIn(str(self.file_a["path"]), seen)
        for key in ("b", "u"):
            self.assertNotIn(str(self.files[key]["path"]), seen)
            self.assertNotIn(f"/{self.files[key]['attempt_id']}/", seen)

    def test_service_scope_denies_a_principal_without_an_account_id(self):
        # None == None must not match: no account id never selects NULL-owner rows.
        for principal in (IDLESS_TEACHER, ANONYMOUS, account_principal(self.student["id"], "student", "У"),
                          Principal(is_teacher=True, is_admin=False, user_id=None, role="teacher")):
            with self.subTest(principal=principal), closing(web_app.quiz_db()) as conn:
                self.assertEqual(practical_service.fetch_practical_submission_review_rows(
                    conn, self.upload_root, principal=principal), [])
        with closing(web_app.quiz_db()) as conn:
            rows = practical_service.fetch_practical_submission_review_rows(
                conn, self.upload_root,
                principal=account_principal(self.teacher_a["id"], "teacher", "А"))
        self.assertEqual([row["assignment_id"] for row in rows], [self.a])

    # --- file download -----------------------------------------------------

    def test_teacher_downloads_only_files_of_own_assignments(self):
        for account, own, others in ((self.teacher_a, "a", ("b", "u")),
                                     (self.teacher_b, "b", ("a", "u"))):
            client = self.client(account)
            with self.subTest(account=account["id"], file=own):
                self.assert_downloads(client, self.files[own])
            for key in others:
                with self.subTest(account=account["id"], file=key):
                    # Direct substitution of a valid foreign/unowned file id.
                    self.assertEqual(client.get(download_path(self.files[key]["id"])).status_code, 404)
            with self.subTest(account=account["id"], file="missing"):
                self.assertEqual(client.get(download_path(9999999)).status_code, 404)

    def test_admin_downloads_every_file(self):
        client = self.client(self.admin)
        for key, stored in self.files.items():
            with self.subTest(file=key):
                self.assert_downloads(client, stored)
        self.assertEqual(client.get(download_path(9999999)).status_code, 404)

    def test_foreign_and_missing_files_are_indistinguishable(self):
        client = self.client(self.teacher_a)
        foreign = client.get(download_path(self.file_b["id"]))
        unowned = client.get(download_path(self.file_u["id"]))
        missing = client.get(download_path(9999999))
        self.assertEqual({foreign.status_code, unowned.status_code, missing.status_code}, {404})
        self.assertEqual(foreign.get_data(), missing.get_data())
        self.assertEqual(unowned.get_data(), missing.get_data())

    def test_denied_download_never_touches_the_filesystem(self):
        touched = self.record_filesystem_access()
        client = self.client(self.teacher_a)
        for key in ("b", "u"):
            with self.subTest(file=key):
                self.assertEqual(client.get(download_path(self.files[key]["id"])).status_code, 404)
                self.assertEqual(touched, [])
        # The instrumentation is live: an allowed download is recorded.
        self.assert_downloads(client, self.file_a)
        self.assertIn("safe_path", {name for name, _ in touched})
        self.assertIn(("send_file", str(self.file_a["path"].resolve())), touched)

    def test_denial_precedes_deleted_and_unsafe_path_handling(self):
        # A foreign row with a traversal path or a deleted flag is denied on
        # ownership alone; the same rows stay 404 for the owner and the admin.
        deleted = self.seed_file(self.b, "Изтрит Б", "deleted.xlsx", b"gone", is_deleted=1)
        unsafe = self.seed_file(self.b, "Опасен Б", "evil.xlsx", b"evil")
        with closing(web_app.quiz_db()) as conn:
            conn.execute("UPDATE practical_submission_files SET stored_path = ? WHERE id = ?",
                         ("data/uploads/practical/../../evil.xlsx", unsafe["id"]))
            conn.commit()
        missing_on_disk = self.seed_file(self.a, "Липсващ А", "missing.xlsx", b"lost")
        missing_on_disk["path"].unlink()

        touched = self.record_filesystem_access()
        foreign = self.client(self.teacher_a)
        for stored in (deleted, unsafe):
            self.assertEqual(foreign.get(download_path(stored["id"])).status_code, 404)
        self.assertEqual(touched, [])

        for client in (self.client(self.teacher_b), self.client(self.admin)):
            for stored in (deleted, unsafe):
                self.assertEqual(client.get(download_path(stored["id"])).status_code, 404)
        self.assertNotIn("send_file", {name for name, _ in touched})
        for client in (self.client(self.teacher_a), self.client(self.admin)):
            self.assertEqual(client.get(download_path(missing_on_disk["id"])).status_code, 404)

    def test_symlink_escaping_the_upload_root_is_not_served(self):
        outside = self.upload_root.parent / "outside.xlsx"
        outside.write_bytes(b"outside the upload root")
        self.file_a["path"].unlink()
        self.file_a["path"].symlink_to(outside)
        for client in (self.client(self.teacher_a), self.client(self.admin)):
            self.assertEqual(client.get(download_path(self.file_a["id"])).status_code, 404)

    # --- wrong role, historical keys, anonymous, pending second factor ------

    def test_everyone_without_the_role_is_denied_by_the_route_policy(self):
        pending = web_app.app.test_client()
        with pending.session_transaction() as session:
            session.update(mfa_uid=self.admin["id"], mfa_sk=self.admin["session_key"], mfa_iat=1)
        pending_with_keys = web_app.app.test_client()
        with pending_with_keys.session_transaction() as session:
            session.update(mfa_uid=self.admin["id"], mfa_sk=self.admin["session_key"], mfa_iat=1,
                           admin_authenticated=True, tester_authenticated=True, ui_profile="admin")
        touched = self.record_filesystem_access()
        # Not signed in: asked to sign in. Signed in without the role: refused (403).
        for label, client, check in (
            ("anonymous", self.client(), self.assert_login_redirect),
            ("historical tester key", self.client(historical="tester"), self.assert_login_redirect),
            ("historical admin key", self.client(historical="admin"), self.assert_login_redirect),
            ("pending admin second factor", pending, self.assert_login_redirect),
            ("pending second factor with historical keys", pending_with_keys, self.assert_login_redirect),
            ("student", self.client(self.student), self.assert_refused),
            ("student with a historical admin key", self.client(self.student, "admin"), self.assert_refused),
        ):
            # Missing ids are denied by role too: no ownership 404 before the policy ran.
            for path in (LIST_PATH, download_path(self.file_a["id"]),
                         download_path(self.file_u["id"]), download_path(9999999)):
                with self.subTest(principal=label, path=path):
                    check(client, path)
        self.assertEqual(touched, [])

    def test_route_policy_is_the_shared_assignment_owner_policy(self):
        for endpoint in ("practical.teacher_practical_submissions",
                         "practical.teacher_practical_submission_file_download"):
            self.assertEqual(access.policy_for_endpoint(web_app.app, endpoint), access.ASSIGNMENT_OWNER)
        # The official resource download is a different object and stays admin-only.
        self.assertEqual(
            access.policy_for_endpoint(web_app.app, "practical.dzi_practical_resource_download"),
            access.ADMIN)

    # --- principal follows the current account row -------------------------

    def test_disabled_accounts_lose_practical_authority(self):
        for account, stored in ((self.teacher_a, self.file_a), (self.admin, self.file_u)):
            client = self.client(account)
            self.assert_downloads(client, stored)
            self.update_user(account, "is_active = 0")
            with self.subTest(account=account["id"]):
                self.assert_login_redirect(client, LIST_PATH)
                self.assert_login_redirect(client, download_path(stored["id"]))

    def test_role_changes_apply_on_the_next_request(self):
        teacher_client = self.client(self.teacher_a)
        self.assert_downloads(teacher_client, self.file_a)
        self.update_user(self.teacher_a, "role = 'student'")
        self.assert_refused(teacher_client, LIST_PATH)
        self.assert_refused(teacher_client, download_path(self.file_a["id"]))

        admin_client = self.client(self.admin)
        self.assert_downloads(admin_client, self.file_b)
        self.update_user(self.admin, "role = 'teacher'")
        # A former admin is an ordinary teacher: it owns nothing here.
        self.assert_lists_exactly(admin_client, set())
        for key in ("a", "b", "u"):
            with self.subTest(file=key):
                self.assertEqual(admin_client.get(download_path(self.files[key]["id"])).status_code, 404)

    # --- navigation --------------------------------------------------------

    def test_navigation_matches_what_the_server_allows(self):
        link = f'href="{LIST_PATH}"'
        teacher_client = self.client(self.teacher_a)
        self.assertIn(link, teacher_client.get("/teacher/assignments").get_data(as_text=True))
        body = self.list_body(teacher_client)
        # A teacher account cannot open the admin dashboard, so the page does not link to it.
        self.assertNotIn('href="/teacher"', body)
        self.assertIn('href="/teacher/assignments"', body)
        self.assertIn('href="/teacher"', self.list_body(self.client(self.admin)))
        # A historical shared-login key gets no practical-review link and no teacher page.
        historical = self.client(historical="tester")
        self.assertNotIn(LIST_PATH, historical.get("/").get_data(as_text=True))
        for path in ("/teacher/new", f"/teacher/assignment/{self.u}"):
            with self.subTest(path=path):
                self.assert_login_redirect(historical, path)

    # --- student flow and official resources are not owner-scoped ----------

    def test_student_practical_page_and_upload_ignore_assignment_ownership(self):
        for key in ("a", "b", "u"):
            stored = self.files[key]
            student = web_app.app.test_client()
            with student.session_transaction() as session:
                session[web_app.quiz_attempt_owner_session_key(stored["attempt_id"])] = True
            base = f"/quiz/attempt/{stored['attempt_id']}/practical/{self.source_slug}"
            with self.subTest(assignment=key, surface="page"):
                page = student.get(base)
                self.assertEqual(page.status_code, 200)
                self.assertIn(stored["filename"], page.get_data(as_text=True))
            with self.subTest(assignment=key, surface="upload"):
                upload = student.post(
                    f"{base}/task/{self.exam_task_id}/upload",
                    data={"files": (io.BytesIO(b"new work"), "answer.xlsx")},
                    headers=SAME_ORIGIN, content_type="multipart/form-data")
                self.assertEqual(upload.status_code, 302)
                self.assertIn("uploaded=1", upload.headers["Location"])
            with self.subTest(assignment=key, surface="foreign attempt"):
                self.assertEqual(web_app.app.test_client().get(base).status_code, 404)
        # The owning teacher has no attempt session: the student page stays an attempt-owner page.
        self.assertEqual(self.client(self.teacher_a).get(
            f"/quiz/attempt/{self.file_a['attempt_id']}/practical/{self.source_slug}").status_code, 404)

    def test_official_resource_and_dzi_practical_pages_stay_admin_only(self):
        for path in ("/dzi/practical/resource/1/download", f"/dzi/source/{self.source_slug}/practical"):
            with self.subTest(path=path, principal="teacher"):
                self.assert_refused(self.client(self.teacher_a), path)
        self.assertEqual(self.client(self.admin).get(
            f"/dzi/source/{self.source_slug}/practical").status_code, 200)
