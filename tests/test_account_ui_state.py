"""Rendered authentication state comes from the request Principal.

The same Principal that decides route access also decides navigation and role-gated
controls. The account row is the only authority: the session keys of the removed
shared-password logins and profile switch (``tester_authenticated``,
``admin_authenticated``, ``ui_profile``) change nothing that is rendered.
"""

import sqlite3
from unittest.mock import patch

from tests.auth_helpers import (
    HISTORICAL_SHARED_LOGIN_SESSION,
    sign_in_admin,
    sign_in_student,
    sign_in_teacher,
)
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
            cls.assignment_id = cursor.lastrowid
            cls.assignment_path = f"/teacher/assignment/{cls.assignment_id}"
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

    def historical_client(self, **state):
        """An anonymous client whose cookie still holds removed shared-login keys."""
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
            self.assertEqual(self.client.get(path).status_code, 403, path)

    def test_teacher_gets_teacher_ui_and_manages_its_own_assignment(self):
        owner = sign_in_teacher(self.client, self.db_path)
        conn = web_app.quiz_db()
        try:
            conn.execute("UPDATE quiz_assignments SET owner_user_id = ? WHERE id = ?",
                         (owner["id"], self.assignment_id))
            conn.commit()
        finally:
            conn.close()
        html = self.home()
        self.assertIn("Учител", html)
        self.assertNotIn("Вход за тестер", html)
        self.assertIn('aria-controls="nav-menu-classes"', html)
        self.assertNotIn("админ изглед", html)
        self.assertEqual(self.client.get("/teacher/new").status_code, 200)
        # Accounts PR7: the owner gets the management controls and its list.
        detail = self.detail()
        self.assertIn(f"{self.assignment_path}/duplicate", detail)
        self.assertIn(f"{self.assignment_path}/edit", detail)
        self.assertIn(f"{self.assignment_path}/results", detail)
        self.assertIn('href="/teacher/assignments"', html)
        self.assertEqual(self.client.get("/teacher/assignments").status_code, 200)
        self.assertEqual(self.client.get("/teacher").status_code, 403)

    def test_admin_gets_admin_ui_and_edit_duplicate_controls(self):
        sign_in_admin(self.client, self.db_path)
        html = self.home()
        self.assertIn("Админ", html)
        self.assertIn('aria-controls="nav-menu-classes"', html)
        self.assertIn("админ изглед", html)
        detail = self.detail()
        self.assertIn(f"{self.assignment_path}/duplicate", detail)
        self.assertIn(f"{self.assignment_path}/edit", detail)

    def test_account_sessions_use_the_account_logout_only(self):
        for sign_in in (sign_in_student, sign_in_teacher, sign_in_admin):
            with self.subTest(helper=sign_in.__name__):
                client = web_app.app.test_client()
                sign_in(client, self.db_path)
                with client.session_transaction() as sess:
                    # Removed shared-login keys in the same cookie bring no removed link back.
                    sess.update(HISTORICAL_SHARED_LOGIN_SESSION)
                html = self.home(client)
                self.assertIn('method="post" action="/logout"', html)
                self.assertIn('href="/account/password"', html)
                for removed in ("/admin/logout", "/tester/logout", "/admin/login", "/tester/login",
                                "/profile/"):
                    self.assertNotIn(removed, html)

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

    # --- historical shared-login and profile keys ---------------------------

    def test_historical_keys_render_exactly_the_anonymous_page(self):
        anonymous = self.home()
        self.assertIn('href="/login?next=/"', anonymous)
        for absent in ("Вход за тестер", "Вход за админ", "Тестер", "админ изглед",
                       "/tester/login", "/admin/login", "/tester/logout", "/admin/logout", "/profile/",
                       'action="/logout"', 'aria-controls="nav-menu-classes"',
                       'href="/teacher/assignments"'):
            self.assertNotIn(absent, anonymous)
        for state in (
            {"tester_authenticated": True, "ui_profile": "tester"},
            {"admin_authenticated": True, "ui_profile": "admin"},
            {"ui_profile": "admin"},
            HISTORICAL_SHARED_LOGIN_SESSION,
        ):
            with self.subTest(state=state):
                client = self.historical_client(**state)
                self.assertEqual(self.home(client), anonymous)
                for path in ("/teacher", "/teacher/new", "/teacher/assignments", self.assignment_path):
                    response = client.get(path)
                    self.assertEqual(response.status_code, 302, path)
                    self.assertEqual(response.headers["Location"], f"/login?next={path}")

    def test_historical_keys_cannot_elevate_an_account(self):
        for sign_in, new_status in ((sign_in_student, 403), (sign_in_teacher, 200)):
            with self.subTest(helper=sign_in.__name__):
                client = web_app.app.test_client()
                sign_in(client, self.db_path)
                before = self.home(client)
                with client.session_transaction() as sess:
                    sess.update(HISTORICAL_SHARED_LOGIN_SESSION)
                self.assertEqual(self.home(client), before)
                self.assertNotIn("админ изглед", before)
                self.assertEqual(client.get("/teacher").status_code, 403)
                self.assertEqual(client.get("/teacher/new").status_code, new_status)
                # The unowned assignment stays admin-only.
                self.assertEqual(client.get(self.assignment_path).status_code,
                                 403 if new_status == 403 else 404)

    def test_historical_profile_key_does_not_change_an_account_admin(self):
        sign_in_admin(self.client, self.db_path)
        before = self.home()
        with self.client.session_transaction() as sess:
            sess["ui_profile"] = "tester"
        self.assertEqual(self.home(), before)
        self.assertIn("админ изглед", before)
        self.assertEqual(self.client.get("/teacher").status_code, 200)

    def test_removed_profile_switch_route_is_not_found_for_everyone(self):
        clients = {"anonymous": web_app.app.test_client(),
                   "historical": self.historical_client(**HISTORICAL_SHARED_LOGIN_SESSION)}
        for sign_in in (sign_in_student, sign_in_teacher, sign_in_admin):
            clients[sign_in.__name__] = web_app.app.test_client()
            sign_in(clients[sign_in.__name__], self.db_path)
        for who, client in clients.items():
            for path in ("/profile/admin", "/profile/tester", "/tester"):
                with self.subTest(who=who, path=path):
                    with client.session_transaction() as sess:
                        before = dict(sess)
                    self.assertEqual(client.get(path).status_code, 404)
                    with client.session_transaction() as sess:
                        self.assertEqual(dict(sess), before)

    def test_asset_requests_set_no_cookie(self):
        sign_in_admin(self.client, self.db_path)
        response = self.client.get("/static/css/tokens.css")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("Set-Cookie", response.headers)
        response.close()

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
