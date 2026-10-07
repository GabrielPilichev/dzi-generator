"""Rendered authentication state comes from the request Principal.

The same Principal that decides route access also decides navigation and role-gated
controls. ``ui_profile`` is presentation only and the account row stays authoritative.
"""

import sqlite3
from unittest.mock import patch

from tests.auth_helpers import sign_in_admin, sign_in_student, sign_in_teacher
from tests.support import IsolatedAppTestCase, web_app


class AccountUiStateTest(IsolatedAppTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.db_path = web_app.app.config["DB_PATH"]
        conn = web_app.quiz_db()
        try:
            section = next(
                row for row in conn.execute("SELECT id FROM curriculum_sections ORDER BY id")
                if web_app.quiz_section_question_ids(conn, int(row["id"]))
            )
        finally:
            conn.close()
        conn = web_app.quiz_db()
        try:
            cursor = conn.execute(
                "INSERT INTO quiz_assignments (section_id, title_bg, question_count, time_limit_minutes) "
                "VALUES (?, 'UI състояние', 1, 5)", (section["id"],))
            conn.commit()
            cls.assignment_path = f"/teacher/assignment/{cursor.lastrowid}"
        finally:
            conn.close()

    def setUp(self):
        self.client = web_app.app.test_client()

    def home(self, client=None):
        response = (client or self.client).get("/")
        self.assertEqual(response.status_code, 200)
        return response.get_data(as_text=True)

    def detail(self, client=None):
        response = (client or self.client).get(self.assignment_path)
        self.assertEqual(response.status_code, 200)
        return response.get_data(as_text=True)

    def legacy_client(self, **state):
        client = web_app.app.test_client()
        with client.session_transaction() as sess:
            sess.update(state)
        return client

    # --- account roles -----------------------------------------------------

    def test_student_is_signed_in_without_teacher_or_admin_ui(self):
        sign_in_student(self.client, self.db_path)
        html = self.home()
        self.assertIn("Ученик", html)
        self.assertNotIn("Вход за тестер", html)
        # Content navigation is for every signed-in account; teacher pages are not.
        self.assertIn('aria-controls="nav-menu-classes"', html)
        self.assertNotIn("админ изглед", html)
        for path in ("/teacher/new", self.assignment_path, "/teacher/assignments"):
            self.assertEqual(self.client.get(path).status_code, 302, path)

    def test_teacher_gets_teacher_ui_but_no_admin_controls(self):
        sign_in_teacher(self.client, self.db_path)
        html = self.home()
        self.assertIn("Учител", html)
        self.assertNotIn("Вход за тестер", html)
        self.assertIn('aria-controls="nav-menu-classes"', html)
        self.assertNotIn("админ изглед", html)
        self.assertEqual(self.client.get("/teacher/new").status_code, 200)
        detail = self.detail()
        self.assertNotIn("/duplicate", detail)
        self.assertNotIn(f"{self.assignment_path}/edit", detail)
        self.assertEqual(self.client.get("/teacher/assignments").status_code, 302)

    def test_admin_gets_admin_ui_and_edit_duplicate_controls(self):
        sign_in_admin(self.client, self.db_path)
        html = self.home()
        self.assertIn("Админ", html)
        self.assertIn('aria-controls="nav-menu-classes"', html)
        self.assertIn("админ изглед", html)
        detail = self.detail()
        self.assertIn(f"{self.assignment_path}/duplicate", detail)
        self.assertIn(f"{self.assignment_path}/edit", detail)

    def test_account_sessions_use_the_account_logout_not_the_legacy_one(self):
        for sign_in in (sign_in_student, sign_in_teacher, sign_in_admin):
            with self.subTest(helper=sign_in.__name__):
                client = web_app.app.test_client()
                sign_in(client, self.db_path)
                with client.session_transaction() as sess:
                    # Legacy keys in the same cookie must not bring the legacy links back.
                    sess["admin_authenticated"] = True
                    sess["tester_authenticated"] = True
                html = self.home(client)
                self.assertIn('method="post" action="/logout"', html)
                self.assertIn('href="/account/password"', html)
                for legacy in ("/admin/logout", "/tester/logout", "/admin/login", "/tester/login"):
                    self.assertNotIn(legacy, html)

    def test_account_logout_signs_the_account_out(self):
        sign_in_admin(self.client, self.db_path)
        self.assertEqual(self.client.get("/logout").status_code, 405)
        response = self.client.post("/logout")
        self.assertEqual(response.status_code, 302)
        with self.client.session_transaction() as sess:
            self.assertEqual(dict(sess), {})
        html = self.home()
        self.assertIn('href="/login?next=/"', html)
        self.assertNotIn("админ изглед", html)

    # --- legacy compatibility ----------------------------------------------

    def test_legacy_tester_and_admin_ui_is_unchanged(self):
        tester = self.home(self.legacy_client(tester_authenticated=True, ui_profile="tester"))
        self.assertIn("Тестер", tester)
        self.assertIn("/tester/logout", tester)
        self.assertNotIn('action="/logout"', tester)
        self.assertNotIn("админ изглед", tester)
        admin_client = self.legacy_client(admin_authenticated=True, ui_profile="admin")
        admin = self.home(admin_client)
        self.assertIn("/admin/logout", admin)
        self.assertNotIn('action="/logout"', admin)
        self.assertIn("админ изглед", admin)
        self.assertIn(f"{self.assignment_path}/duplicate", self.detail(admin_client))

    def test_anonymous_sees_the_account_and_legacy_login_links(self):
        html = self.home()
        self.assertIn('href="/login?next=/"', html)
        self.assertIn("Вход за тестер", html)
        self.assertIn("Вход за админ", html)

    # --- precedence and ui_profile -----------------------------------------

    def test_legacy_flags_cannot_override_an_account_principal(self):
        sign_in_student(self.client, self.db_path)
        with self.client.session_transaction() as sess:
            sess["admin_authenticated"] = True
            sess["tester_authenticated"] = True
        self.assertNotIn("админ изглед", self.home())
        self.assertEqual(self.client.get(self.assignment_path).status_code, 302)

    def test_ui_profile_cannot_elevate_an_account(self):
        for sign_in in (sign_in_student, sign_in_teacher):
            with self.subTest(helper=sign_in.__name__):
                client = web_app.app.test_client()
                sign_in(client, self.db_path)
                with client.session_transaction() as sess:
                    sess["ui_profile"] = "admin"
                self.assertEqual(client.get("/teacher/assignments").status_code, 302)
                self.assertEqual(client.get("/profile/admin").status_code, 302)
                self.assertNotIn("админ изглед", self.home(client))
                with client.session_transaction() as sess:
                    self.assertNotEqual(sess.get("ui_profile"), "admin")

    def test_ui_profile_does_not_downgrade_an_account_admin(self):
        sign_in_admin(self.client, self.db_path)
        with self.client.session_transaction() as sess:
            sess["ui_profile"] = "admin"
        self.home()
        with self.client.session_transaction() as sess:
            self.assertEqual(sess["ui_profile"], "admin")
            self.assertEqual(set(sess) - {"ui_profile", "_flashes", "_permanent"}, {"uid", "sk", "iat"})

    def test_asset_requests_leave_an_account_admins_ui_profile_alone(self):
        sign_in_admin(self.client, self.db_path)
        with self.client.session_transaction() as sess:
            sess["ui_profile"] = "admin"
        response = self.client.get("/static/css/tokens.css")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("Set-Cookie", response.headers)
        with self.client.session_transaction() as sess:
            self.assertEqual(sess["ui_profile"], "admin")

    def test_cookie_never_carries_role_authority(self):
        sign_in_admin(self.client, self.db_path)
        self.home()
        with self.client.session_transaction() as sess:
            for key in ("tester_authenticated", "admin_authenticated", "role", "is_admin", "is_teacher"):
                self.assertNotIn(key, sess)

    # --- query budget --------------------------------------------------------

    def test_account_request_does_one_users_lookup(self):
        for sign_in in (sign_in_student, sign_in_teacher, sign_in_admin):
            with self.subTest(helper=sign_in.__name__):
                client = web_app.app.test_client()
                sign_in(client, self.db_path)
                statements, real = [], sqlite3.connect

                def connect(*args, **kwargs):
                    conn = real(*args, **kwargs)
                    conn.set_trace_callback(statements.append)
                    return conn

                with patch.object(sqlite3, "connect", connect):
                    self.assertEqual(client.get("/").status_code, 200)
                users = [s for s in statements if "FROM users" in s]
                self.assertEqual(len(users), 1, users)
