"""Accounts PR9/PR10 — account authentication is the only authority.

PR9 switched the shared tester/admin password bridge off in production; PR10 removed
it. The shared-password login, logout and profile routes do not exist, no
configuration or environment variable can bring them back, the session keys they
once wrote grant nothing in a cookie signed before their removal, every sign-in
denial goes to ``/login``, and a signed-in account without the needed role gets 403.
An account admin still needs the second factor. The owner-scoped assignment and
practical rules (PR7/PR8) are unchanged, and an unowned (NULL) assignment is
manageable by an account admin only.

The historical session keys appear here only as injected input.
Every password, key, secret and code here is synthetic.
"""

import os
import tempfile
import unittest
from contextlib import closing
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from flask import session as flask_session

from tests.support import web_app
from tests.test_account_login import CROSS_ORIGIN, SAME_ORIGIN, AccountAppTestCase
from web import access
from web.access import ANONYMOUS, Principal
from web.services import dzi_exams, quiz

# Teacher capability without an account id: no request can have this principal; it
# only seeds the unowned assignment.
IDLESS_TEACHER = Principal(is_teacher=True, is_admin=False)
LEGACY_LOGIN_PATHS = ("/tester/login", "/admin/login")
LEGACY_LOGOUT_PATHS = ("/tester/logout", "/admin/logout")
LEGACY_PROFILE_PATHS = ("/profile/admin", "/profile/tester", "/tester")
REMOVED_PATHS = (*LEGACY_LOGIN_PATHS, *LEGACY_LOGOUT_PATHS, *LEGACY_PROFILE_PATHS)
ADMIN_PATHS = ("/teacher", "/teacher/dzi-training", "/dzi", "/admin/open-question-candidates")
TEACHER_PATHS = ("/teacher/new",)
ASSIGNMENT_PATHS = ("/teacher/assignments", "/teacher/practical-submissions")
PROTECTED_PATHS = (*ADMIN_PATHS, *TEACHER_PATHS, *ASSIGNMENT_PATHS, "/account/password")
STALE_SESSIONS = {
    "tester": {"tester_authenticated": True},
    "admin": {"admin_authenticated": True},
    "admin with profile": {"admin_authenticated": True, "ui_profile": "admin"},
    "profile only": {"ui_profile": "admin"},
    "everything": {"tester_authenticated": True, "admin_authenticated": True, "ui_profile": "admin"},
}
FORBIDDEN = "Нямаш достъп до тази страница с този профил."


class CutoverTestCase(AccountAppTestCase):
    """The application as every environment runs it: account authentication only."""

    def stale_client(self, state):
        client = self.app.test_client()
        with client.session_transaction() as sess:
            sess.update(state)
        return client

    def assert_sent_to_login(self, client, path, **kwargs):
        response = client.open(path, **kwargs)
        self.assertEqual(response.status_code, 302, path)
        self.assertEqual(response.headers["Location"], f"/login?next={path}")

    def assert_no_authority(self, client=None):
        client = client or self.client
        for path in PROTECTED_PATHS:
            with self.subTest(path=path):
                self.assert_sent_to_login(client, path)

    def assert_admin_authority(self, client=None):
        client = client or self.client
        for path in (*ADMIN_PATHS, *TEACHER_PATHS, *ASSIGNMENT_PATHS):
            with self.subTest(path=path):
                self.assertEqual(client.get(path).status_code, 200)


class LegacyRoutesAreGoneTest(CutoverTestCase):
    def test_no_rule_endpoint_blueprint_or_template_of_the_bridge_is_left(self):
        rules = {rule.rule for rule in self.app.url_map.iter_rules()}
        for path in (*LEGACY_LOGIN_PATHS, *LEGACY_LOGOUT_PATHS, "/tester", "/profile/<profile>"):
            with self.subTest(path=path):
                self.assertNotIn(path, rules)
        self.assertEqual({rule for rule in rules if "login" in rule or "logout" in rule},
                         {"/login", "/login/totp", "/logout"})
        self.assertFalse({rule for rule in rules if rule.startswith(("/tester", "/profile"))})
        self.assertNotIn("auth", self.app.blueprints)
        self.assertFalse({endpoint for endpoint in self.app.view_functions if endpoint.startswith("auth.")})
        self.assertFalse(Path(self.app.root_path, "blueprints", "auth.py").exists())
        for template in ("tester_login.html", "admin_login.html", "tester_home.html"):
            with self.subTest(template=template):
                self.assertFalse(Path(self.app.root_path, "templates", template).exists())

    def test_profile_and_tester_routes_are_not_found_for_every_session(self):
        admin = self.make("admin")
        signed_in = self.app.test_client()
        self.sign_in(admin, client=signed_in)
        for label, client in (("anonymous", self.app.test_client()),
                              ("stale", self.stale_client(STALE_SESSIONS["everything"])),
                              ("account admin", signed_in)):
            before = self.session(client)
            for path in LEGACY_PROFILE_PATHS:
                with self.subTest(path=path, session=label):
                    response = client.get(path)
                    self.assertEqual(response.status_code, 404)
                    self.assertNotIn("Location", response.headers)
            self.assertEqual(self.session(client), before)

    def test_login_routes_are_not_found_for_get_and_post(self):
        missing = self.body(self.client.get("/no-such-page"))
        for path, password in zip(LEGACY_LOGIN_PATHS, ("tester-pass", "admin-pass")):
            for label, send in (
                ("GET", lambda c: c.get(path)),
                ("GET with next", lambda c: c.get(path, query_string={"next": "/teacher"})),
                ("POST correct password", lambda c: c.post(
                    path, data={"password": password, "next": "/teacher"}, headers=SAME_ORIGIN)),
                ("POST wrong password", lambda c: c.post(
                    path, data={"password": "wrong"}, headers=SAME_ORIGIN)),
                ("POST empty", lambda c: c.post(path, headers=SAME_ORIGIN)),
            ):
                with self.subTest(path=path, request=label):
                    client = self.app.test_client()
                    response = send(client)
                    self.assertEqual(response.status_code, 404)
                    self.assertNotIn("Location", response.headers)
                    # The ordinary not-found page: no login form, no hint of a legacy account.
                    self.assertEqual(self.body(response).replace(path, "/no-such-page"), missing)
                    self.assertNotIn('type="password"', self.body(response))
                    self.assertEqual(self.session(client), {})
                    self.assertIsNone(response.headers.get("Set-Cookie"))

    def test_logout_routes_are_not_found_for_every_session(self):
        admin = self.make("admin")
        for path in LEGACY_LOGOUT_PATHS:
            for label, client in (("anonymous", self.app.test_client()),
                                  ("stale", self.stale_client(STALE_SESSIONS["everything"]))):
                with self.subTest(path=path, session=label):
                    self.assertEqual(client.get(path).status_code, 404)
            with self.subTest(path=path, session="account admin"):
                client = self.app.test_client()
                self.sign_in(admin, client=client)
                self.assertEqual(client.get(path).status_code, 404)
                self.assertEqual(set(self.session(client)), {"uid", "sk", "iat"})

    def test_a_cross_origin_post_is_refused_exactly_like_one_to_an_unknown_url(self):
        for path in (*LEGACY_LOGIN_PATHS, "/no-such-page"):
            with self.subTest(path=path):
                self.assertEqual(
                    self.client.post(path, data={"password": "admin-pass"}, headers=CROSS_ORIGIN).status_code,
                    403)

    def test_no_configuration_or_environment_can_bring_the_bridge_back(self):
        self.assertNotIn("LEGACY_SHARED_LOGIN", self.app.config)
        # Everything that once enabled or fed the bridge, set at once.
        environment = {"DZI_LEGACY_SHARED_LOGIN": "1", "DZI_ADMIN_PASSWORD": "admin-pass",
                       "DZI_TESTER_PASSWORD": "tester-pass"}
        with patch.dict(os.environ, environment), \
                patch.dict(self.app.config, {"LEGACY_SHARED_LOGIN": True}):
            for path, password in zip(LEGACY_LOGIN_PATHS, ("tester-pass", "admin-pass")):
                with self.subTest(path=path):
                    client = self.app.test_client()
                    self.assertEqual(client.get(path).status_code, 404)
                    response = client.post(path, data={"password": password, "next": "/teacher"},
                                           headers=SAME_ORIGIN)
                    self.assertEqual(response.status_code, 404)
                    self.assertEqual(self.session(client), {})
                    self.assert_no_authority(client)
            self.assert_no_authority(self.stale_client(STALE_SESSIONS["everything"]))
            body = self.body(self.client.get("/"))
            for marker in REMOVED_PATHS:
                self.assertNotIn(f'"{marker}', body)


class StaleLegacySessionTest(CutoverTestCase):
    def test_stale_keys_resolve_to_no_principal_without_any_cleanup(self):
        # The decision itself ignores the keys: no hook has touched this session.
        for label, state in STALE_SESSIONS.items():
            with self.subTest(session=label), self.app.test_request_context("/teacher"):
                flask_session.update(state)
                self.assertEqual(access.current_principal(), ANONYMOUS)
                self.assertEqual(dict(flask_session), state)
        # ... and after the whole before-request chain it is the same answer.
        for label, state in STALE_SESSIONS.items():
            with self.subTest(session=label, hooks=True):
                principal, after = self.principal_for(state)
                self.assertEqual(principal, ANONYMOUS)
                self.assertEqual(after, state)

    def test_stale_keys_grant_no_route_and_are_sent_to_the_account_login(self):
        for label, state in STALE_SESSIONS.items():
            with self.subTest(session=label):
                client = self.stale_client(state)
                self.assert_no_authority(client)
                response = client.post("/teacher/dzi-training", headers=SAME_ORIGIN)
                self.assertEqual(response.headers["Location"], "/login?next=/teacher/dzi-training")
                # There is no profile to switch to any more.
                self.assertEqual(client.get("/profile/admin").status_code, 404)

    def test_stale_keys_are_never_read_or_rewritten(self):
        # Nothing reads them, so nothing needs to clean them up: the request does not
        # touch the cookie at all, and the attempt-ownership marker beside them works.
        state = {**STALE_SESSIONS["everything"], "quiz_attempt_owner:7": True}
        client = self.stale_client(state)
        for path in ("/", "/teacher", "/login"):
            with self.subTest(path=path):
                self.assertNotIn("Set-Cookie", client.get(path).headers)
        self.assertEqual(self.session(client), state)
        # Signing in (or out) replaces the whole session, stale keys included.
        self.sign_in(self.make("teacher"), client=client)
        self.assertEqual(self.session(client).keys(), {"uid", "sk", "iat", "quiz_attempt_owner:7"})

    def test_navigation_shows_a_stale_session_as_signed_out(self):
        for label, state in STALE_SESSIONS.items():
            with self.subTest(session=label):
                body = self.body(self.stale_client(state).get("/"))
                self.assertIn('href="/login?next=/"', body)
                for marker in ("/tester/login", "/admin/login", "/tester/logout", "/admin/logout",
                               "/profile/", 'action="/logout"', 'aria-label="Профил"', "Вход за тестер",
                               "Вход за админ", ">Админ<", "Тестер", 'href="/teacher/assignments"',
                               'class="nav-links"', "админ изглед"):
                    self.assertNotIn(marker, body)
                self.assertIn('aria-label="Вход"', body)
                self.assertEqual(body, self.body(self.app.test_client().get("/")))

    def test_a_completed_account_renders_normally(self):
        teacher = self.make("teacher", display_name="Учител Навигация")
        self.sign_in(teacher)
        body = self.body(self.client.get("/"))
        for marker in ("Учител Навигация", 'action="/logout"', 'href="/teacher/assignments"'):
            self.assertIn(marker, body)
        for marker in ("/tester/login", "/admin/login", "/tester/logout", "/admin/logout"):
            self.assertNotIn(marker, body)

    def test_stale_keys_beside_an_account_change_nothing(self):
        teacher = self.make("teacher")
        self.sign_in(teacher)
        with self.client.session_transaction() as sess:
            sess.update(STALE_SESSIONS["everything"])
        self.assertEqual(self.client.get("/teacher/new").status_code, 200)
        self.assertEqual(self.client.get("/teacher").status_code, 403)
        self.assertEqual(self.client.get("/dzi").status_code, 403)
        principal, _ = self.principal_for(self.session())
        self.assertEqual((principal.user_id, principal.role, principal.is_admin), (teacher["id"], "teacher", False))

    def test_stale_keys_do_not_survive_an_account_that_stops_resolving(self):
        teacher = self.make("teacher")
        self.sign_in(teacher)
        with self.client.session_transaction() as sess:
            sess.update(STALE_SESSIONS["everything"])
        self.execute("UPDATE users SET is_active = 0 WHERE id = ?", (teacher["id"],))
        self.assert_no_authority()
        # The stale identity is dropped; what is left is not one.
        self.assertEqual(self.session(), STALE_SESSIONS["everything"])
        self.assert_no_authority()


class AccountDenialTest(CutoverTestCase):
    def test_anonymous_is_sent_to_the_account_login_with_a_safe_next(self):
        self.assert_no_authority()
        page = self.client.get("/login?next=/teacher")
        self.assertIn('name="next" value="/teacher"', self.body(page))
        self.assertIsNone(self.client.get("/teacher").headers.get("Set-Cookie"))

    def test_signed_in_account_without_the_role_gets_403(self):
        cases = (
            ("teacher", ADMIN_PATHS),
            ("student", (*ADMIN_PATHS, *TEACHER_PATHS, *ASSIGNMENT_PATHS)),
        )
        for role, paths in cases:
            client = self.app.test_client()
            self.sign_in(self.make(role), client=client)
            for path in paths:
                with self.subTest(role=role, path=path):
                    response = client.get(path)
                    self.assertEqual(response.status_code, 403)
                    self.assertNotIn("Location", response.headers)
                    self.assertIn(FORBIDDEN, self.body(response))
                    self.assertEqual(response.headers["Cache-Control"], "no-store")
            with self.subTest(role=role, request="POST"):
                self.assertEqual(
                    client.post("/teacher/dzi-training", headers=SAME_ORIGIN).status_code, 403)
            # Still signed in, and what the role does allow keeps working.
            self.assertEqual(client.get("/account/password").status_code, 200)

    def test_an_account_that_no_longer_resolves_is_anonymous_not_forbidden(self):
        teacher = self.make("teacher")
        self.sign_in(teacher)
        self.execute("UPDATE users SET session_key = ? WHERE id = ?", ("r" * 43, teacher["id"]))
        self.assert_sent_to_login(self.client, "/teacher")


class LogoutTest(CutoverTestCase):
    def test_post_logout_clears_everything_and_stale_keys_cannot_bring_it_back(self):
        admin = self.make("admin")
        self.sign_in(admin)
        self.assert_admin_authority()
        with self.client.session_transaction() as sess:
            sess.update(STALE_SESSIONS["everything"])
        self.assertEqual(self.client.get("/logout").status_code, 405)
        self.assertEqual(self.client.post("/logout", headers=CROSS_ORIGIN).status_code, 403)
        response = self.client.post("/logout", headers=SAME_ORIGIN)
        self.assertEqual((response.status_code, response.headers["Location"]), (302, "/"))
        self.assertEqual(self.session(), {})
        self.assert_no_authority()
        # The browser replays a cookie it held before the bridge was removed.
        with self.client.session_transaction() as sess:
            sess.update(STALE_SESSIONS["everything"])
        self.assert_no_authority()


class AdminSecondFactorTest(CutoverTestCase):
    def pending(self, admin):
        self.now += timedelta(seconds=31)
        page = self.login(admin["email"])
        self.assertEqual(page.status_code, 200, self.body(page))
        self.assertIn('action="/login/totp"', self.body(page))
        self.assertEqual({key for key in self.session() if not key.startswith("quiz_")},
                         {"mfa_uid", "mfa_sk", "mfa_iat"})

    def inject_stale_admin(self):
        with self.client.session_transaction() as sess:
            sess.update(STALE_SESSIONS["everything"])

    def test_password_alone_grants_no_admin_authority(self):
        self.pending(self.make("admin"))
        self.assert_no_authority()
        self.assertEqual(self.client.get("/admin/login").status_code, 404)

    def test_pending_second_factor_with_stale_legacy_admin_keys_has_no_authority(self):
        # While the bridge existed this exact session was a shared-password admin: the
        # shared admin login could be completed while the second factor was still pending.
        admin = self.make("admin")
        self.pending(admin)
        self.inject_stale_admin()
        with self.app.test_request_context("/teacher"):
            flask_session.update(self.session())
            self.assertEqual(access.current_principal(), ANONYMOUS)
        self.assert_no_authority()
        self.assertEqual(self.client.get("/profile/admin").status_code, 404)
        # The pending sign-in is untouched by all of that.
        self.assertTrue({"mfa_uid", "mfa_sk", "mfa_iat"} <= set(self.session()))

        # A failed code changes nothing, with the stale keys put back again.
        self.inject_stale_admin()
        wrong = self.submit_code(str((int(self.code(admin)) + 1) % 10**6).zfill(6))
        self.assertEqual(wrong.status_code, 200)
        self.inject_stale_admin()
        self.assert_no_authority()

        # Only the real second factor creates the admin Principal.
        self.inject_stale_admin()
        self.assertEqual(self.submit_code(self.code(admin)).status_code, 302)
        self.assertEqual(set(self.session()), {"uid", "sk", "iat"})
        self.assert_admin_authority()

    def test_the_shared_admin_login_cannot_complete_or_bypass_the_second_factor(self):
        admin = self.make("admin")
        self.pending(admin)
        before = self.session()
        for path, password in zip(LEGACY_LOGIN_PATHS, ("tester-pass", "admin-pass")):
            response = self.client.post(path, data={"password": password, "next": "/teacher"},
                                        headers=SAME_ORIGIN)
            self.assertEqual(response.status_code, 404)
        self.assertEqual(self.session(), before)
        self.assert_no_authority()
        self.assertIsNone(self.user(admin["id"], "last_login_at"))

    def test_totp_code_completes_the_admin_sign_in(self):
        admin = self.make("admin")
        self.pending(admin)
        self.assert_no_authority()
        response = self.submit_code(self.code(admin))
        self.assertEqual((response.status_code, response.headers["Location"]), (302, "/teacher"))
        self.assert_admin_authority()

    def test_recovery_code_completes_the_admin_sign_in_once(self):
        admin = self.make("admin")
        self.pending(admin)
        self.assert_no_authority()
        self.assertEqual(self.submit_code(admin["recovery_codes"][0]).status_code, 302)
        self.assert_admin_authority()
        # The same code is spent: a second sign-in with it stays pending.
        other = self.app.test_client()
        self.now += timedelta(seconds=31)
        self.login(admin["email"], client=other)
        self.submit_code(admin["recovery_codes"][0], client=other)
        self.assert_no_authority(other)

    def test_admin_without_a_confirmed_second_factor_cannot_sign_in_at_all(self):
        admin = self.make("admin", totp=False)
        self.assertEqual(self.login(admin["email"]).status_code, 403)
        self.inject_stale_admin()
        self.assert_no_authority()


class OwnershipCompositionTest(CutoverTestCase):
    """PR7 assignment and PR8 practical ownership with account authority only.

    Assignments A (Teacher A), B (Teacher B) and U (unowned), each with one stored
    practical file.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
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
        super().setUp()
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        self.upload_root = Path(temp_dir.name) / "uploads" / "practical"
        self.upload_root.mkdir(parents=True)
        uploads = patch.dict(self.app.config, {"PRACTICAL_UPLOAD_ROOT": str(self.upload_root)})
        uploads.start()
        self.addCleanup(uploads.stop)
        for name in ("quiz_write_assignment_note", "quiz_write_attempt_note"):
            if hasattr(quiz, name):
                note_patch = patch.object(quiz, name)
                note_patch.start()
                self.addCleanup(note_patch.stop)

        self.teacher_a = self.make("teacher")
        self.teacher_b = self.make("teacher")
        self.admin = self.make("admin")
        self.assignments = {
            "a": self.seed_assignment(self.teacher_a, "PR9 задание А"),
            "b": self.seed_assignment(self.teacher_b, "PR9 задание Б"),
            "u": self.seed_assignment(None, "PR9 задание без собственик"),
        }
        self.files = {key: self.seed_file(assignment_id, f"solution-{key}.xlsx", f"content {key}".encode())
                      for key, assignment_id in self.assignments.items()}

    def seed_assignment(self, account, title):
        principal = (access.account_principal(account["id"], "teacher", "Учител") if account
                     else IDLESS_TEACHER)
        with closing(web_app.quiz_db()) as conn:
            assignment_id = quiz.quiz_create_assignment(
                conn, principal=principal, section_id=self.section_id, title_bg=title, question_count=1)
            conn.commit()
        return assignment_id

    def seed_file(self, assignment_id, filename, content):
        with closing(web_app.quiz_db()) as conn:
            attempt_id = conn.execute(
                "INSERT INTO quiz_attempts (assignment_id, student_name, seed, question_ids_json)"
                " VALUES (?, 'Практик', 'seed', '[]')", (assignment_id,)).lastrowid
            submission_id = conn.execute(
                "INSERT INTO practical_submissions (quiz_attempt_id, exam_task_id) VALUES (?, ?)",
                (attempt_id, self.exam_task_id)).lastrowid
            relative = Path(str(attempt_id)) / str(self.exam_task_id) / str(submission_id) / "stored.xlsx"
            file_id = conn.execute(
                "INSERT INTO practical_submission_files (practical_submission_id, stored_path,"
                " original_filename, size_bytes, is_deleted) VALUES (?, ?, ?, ?, 0)",
                (submission_id, str(Path("data/uploads/practical") / relative), filename,
                 len(content))).lastrowid
            conn.commit()
        disk_path = self.upload_root / relative
        disk_path.parent.mkdir(parents=True)
        disk_path.write_bytes(content)
        return {"id": int(file_id), "filename": filename, "content": content}

    def signed_in(self, account):
        client = self.app.test_client()
        response = self.sign_in(account, client=client)
        self.assertEqual(response.status_code, 302)
        return client

    def assignment_gets(self, key):
        base = f"/teacher/assignment/{self.assignments[key]}"
        return (base, f"{base}/results", f"{base}/results.csv")

    def download_path(self, key):
        return f"/teacher/practical/submission-file/{self.files[key]['id']}/download"

    def assert_manages(self, client, key):
        for path in self.assignment_gets(key):
            with self.subTest(assignment=key, path=path):
                self.assertEqual(client.get(path).status_code, 200)
        with self.subTest(practical=key):
            response = client.get(self.download_path(key))
            try:
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.data, self.files[key]["content"])
            finally:
                response.close()

    def assert_not_found(self, client, key):
        base = f"/teacher/assignment/{self.assignments[key]}"
        title = self.title(key)
        for path in (*self.assignment_gets(key), self.download_path(key)):
            with self.subTest(assignment=key, path=path):
                self.assertEqual(client.get(path).status_code, 404)
        for suffix, data in (("edit", {"title_bg": "Чуждо", "time_limit_minutes": "3"}), ("duplicate", {})):
            with self.subTest(assignment=key, post=suffix):
                self.assertEqual(
                    client.post(f"{base}/{suffix}", data=data, headers=SAME_ORIGIN).status_code, 404)
        self.assertEqual(self.title(key), title)

    def assert_lists(self, client, visible):
        assignments = self.body(client.get("/teacher/assignments"))
        practical = self.body(client.get("/teacher/practical-submissions"))
        for key in self.assignments:
            with self.subTest(listed=key):
                check = self.assertIn if key in visible else self.assertNotIn
                check(f'/teacher/assignment/{self.assignments[key]}"', assignments)
                check(self.files[key]["filename"], practical)

    def title(self, key):
        return self.query("SELECT title_bg FROM quiz_assignments WHERE id = ?", (self.assignments[key],))[0][0]

    def owners(self):
        return {key: self.query("SELECT owner_user_id FROM quiz_assignments WHERE id = ?", (aid,))[0][0]
                for key, aid in self.assignments.items()}

    def test_teacher_manages_only_own_assignment_and_practical_data(self):
        client = self.signed_in(self.teacher_a)
        self.assert_manages(client, "a")
        self.assert_not_found(client, "b")
        self.assert_not_found(client, "u")
        self.assert_lists(client, {"a"})

    def test_account_admin_after_the_second_factor_manages_everything(self):
        client = self.signed_in(self.admin)
        for key in ("a", "b", "u"):
            self.assert_manages(client, key)
        self.assert_lists(client, {"a", "b", "u"})

    def test_pending_admin_has_no_assignment_or_practical_authority(self):
        client = self.app.test_client()
        self.now += timedelta(seconds=31)
        self.login(self.admin["email"], client=client)
        with client.session_transaction() as sess:
            self.assertIn("mfa_uid", sess)
            sess.update(STALE_SESSIONS["everything"])
        self.assert_stale_session_manages_nothing(client)

    def assert_stale_session_manages_nothing(self, client):
        count = self.query("SELECT COUNT(*) FROM quiz_assignments")[0][0]
        for key in ("a", "b", "u"):
            base = f"/teacher/assignment/{self.assignments[key]}"
            for path in (*self.assignment_gets(key), self.download_path(key)):
                with self.subTest(assignment=key, path=path):
                    self.assert_sent_to_login(client, path)
            for suffix in ("edit", "duplicate", "results"):
                with self.subTest(assignment=key, post=suffix):
                    self.assert_sent_to_login(
                        client, f"{base}/{suffix}", method="POST",
                        data={"title_bg": "Старо", "text_answer_id": "1"}, headers=SAME_ORIGIN)
        for path in ASSIGNMENT_PATHS:
            self.assert_sent_to_login(client, path)
        self.assertEqual(self.query("SELECT COUNT(*) FROM quiz_assignments")[0][0], count)
        self.assertEqual({self.title(key) for key in self.assignments},
                         {"PR9 задание А", "PR9 задание Б", "PR9 задание без собственик"})

    def test_stale_legacy_tester_has_no_assignment_or_practical_access(self):
        # Including the unowned row it could open view-only while the bridge existed.
        client = self.stale_client(STALE_SESSIONS["tester"])
        self.assert_stale_session_manages_nothing(client)

    def test_denied_downloads_never_reach_the_file_response(self):
        # Authorization comes first: a denied request is answered before send_file.
        clients = [(302, self.stale_client(state)) for state in STALE_SESSIONS.values()]
        clients.append((404, self.signed_in(self.teacher_a)))
        with patch("web.blueprints.practical.send_file", side_effect=AssertionError("served")):
            for status, client in clients:
                for key in ("b", "u"):
                    with self.subTest(status=status, file=key):
                        self.assertEqual(client.get(self.download_path(key)).status_code, status)

    def test_stale_legacy_admin_has_no_global_management_authority(self):
        for label in ("admin", "admin with profile", "everything"):
            with self.subTest(session=label):
                self.assert_stale_session_manages_nothing(self.stale_client(STALE_SESSIONS[label]))

    def test_unowned_rows_stay_unowned_and_student_flows_are_untouched(self):
        self.assert_manages(self.signed_in(self.admin), "u")
        self.assert_not_found(self.signed_in(self.teacher_a), "u")
        self.assertEqual(self.owners(), {"a": self.teacher_a["id"], "b": self.teacher_b["id"], "u": None})
        # The public quiz start page is not an auth route and needs no account.
        anonymous = self.app.test_client()
        self.assertEqual(anonymous.get(f"/quiz/{self.assignments['u']}").status_code, 200)
        self.assertEqual(anonymous.get("/quiz/attempt/999999").status_code, 404)


if __name__ == "__main__":
    unittest.main()
