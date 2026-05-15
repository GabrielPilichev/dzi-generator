import atexit
import os
import shutil
import tempfile
import unittest
import warnings
from pathlib import Path
from unittest.mock import patch


_TMP = tempfile.TemporaryDirectory()
atexit.register(_TMP.cleanup)

_ROOT = Path(__file__).resolve().parents[1]
_TMP_DB = Path(_TMP.name) / "questions.db"
_TMP_VAULT = Path(_TMP.name) / "vault"
shutil.copy2(_ROOT / "data" / "questions.db", _TMP_DB)
_TMP_VAULT.mkdir()

os.environ["DZI_DB"] = str(_TMP_DB)
os.environ["DZI_VAULT"] = str(_TMP_VAULT)
os.environ["DZI_ADMIN_PASSWORD"] = "admin-pass"
os.environ["DZI_TESTER_PASSWORD"] = "tester-pass"

from web import app as web_app  # noqa: E402


def _register_dzi_default_denied_test_route(app):
    endpoint = "dzi_test_only_default_denied"
    if endpoint in app.view_functions:
        return

    def view():
        return "test-only DZI endpoint"

    app.add_url_rule("/__test__/dzi-default-denied", endpoint=endpoint, view_func=view)


class AuthGuardTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = web_app.app
        _register_dzi_default_denied_test_route(cls.app)
        cls.app.config.update(TESTING=True)
        conn = web_app.quiz_db()
        try:
            cls.section = cls._first_eligible_section(conn)
            cls.dzi_source_slug = cls._first_dzi_source_slug(conn)
        finally:
            conn.close()

    @staticmethod
    def _first_eligible_section(conn):
        rows = conn.execute("""
            SELECT id, section_slug
            FROM curriculum_sections
            ORDER BY id
        """).fetchall()
        for row in rows:
            if web_app.quiz_section_question_ids(conn, int(row["id"])):
                return row
        raise AssertionError("No section with eligible quiz questions found")

    @staticmethod
    def _first_dzi_source_slug(conn):
        row = conn.execute("""
            SELECT year, session, variant
            FROM exams
            WHERE format_version = ?
              AND year = 2025
              AND session = 'may'
              AND variant = 2
            LIMIT 1
        """, (web_app.DZI_FORMAT_VERSION,)).fetchone()
        if row is not None:
            return web_app.dzi_source_slug(row)

        row = conn.execute("""
            SELECT year, session, variant
            FROM exams
            WHERE format_version = ?
            ORDER BY year DESC, session, variant
            LIMIT 1
        """, (web_app.DZI_FORMAT_VERSION,)).fetchone()
        if row is None:
            return "may_2025_v1"
        return web_app.dzi_source_slug(row)

    def setUp(self):
        self.client = self.app.test_client()

    def _login_tester(self):
        with self.client.session_transaction() as sess:
            sess["tester_authenticated"] = True
            sess["ui_profile"] = "tester"

    def _login_admin(self):
        with self.client.session_transaction() as sess:
            sess["admin_authenticated"] = True
            sess["ui_profile"] = "admin"

    def _create_assignment_as_tester(self):
        self._login_tester()
        response = self.client.post("/teacher/new", data={
            "section_id": str(self.section["id"]),
            "question_count": "1",
            "time_limit_minutes": "",
        })
        self.assertEqual(response.status_code, 302)
        location = response.headers["Location"]
        self.assertIn("/teacher/assignment/", location)
        return location

    def _assignment_count(self):
        conn = web_app.quiz_db()
        try:
            return conn.execute("SELECT COUNT(*) FROM quiz_assignments").fetchone()[0]
        finally:
            conn.close()

    def test_sessionless_access_is_redirected_to_login(self):
        response = self.client.get("/teacher/new")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/tester/login", response.headers["Location"])

        for path in (
            "/teacher",
            "/teacher/assignments",
            "/teacher/dzi-training",
            "/dzi",
            f"/dzi/source/{self.dzi_source_slug}",
        ):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 302, path)
            self.assertIn("/admin/login", response.headers["Location"], path)

    def test_tester_can_only_use_normal_assignment_creation_flow(self):
        response = self.client.get(f"/teacher/new?section={self.section['section_slug']}")
        self.assertEqual(response.status_code, 302)

        self._login_tester()
        response = self.client.get(f"/teacher/new?section={self.section['section_slug']}")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'id="new-assignment-form"', response.data)

        assignment_url = self._create_assignment_as_tester()
        response = self.client.get(assignment_url)
        self.assertEqual(response.status_code, 200)

        forbidden_paths = (
            "/teacher",
            "/teacher/assignments",
            "/teacher/dzi-training",
            f"{assignment_url}/results",
            "/dzi",
            f"/dzi/source/{self.dzi_source_slug}",
        )
        for path in forbidden_paths:
            response = self.client.get(path)
            self.assertEqual(response.status_code, 302, path)
            self.assertIn("/admin/login", response.headers["Location"], path)

    def test_admin_can_access_teacher_and_dzi_pages(self):
        assignment_url = self._create_assignment_as_tester()
        self.client = self.app.test_client()
        self._login_admin()

        paths = (
            "/teacher",
            "/teacher/assignments",
            "/teacher/dzi-training",
            assignment_url,
            f"{assignment_url}/results",
            "/dzi",
            f"/dzi/source/{self.dzi_source_slug}",
        )
        for path in paths:
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200, path)

    def test_teacher_dashboard_groups_recent_tests_by_class_with_teacher_fallback(self):
        assignment_url = self._create_assignment_as_tester()
        assignment_id = assignment_url.rstrip("/").split("/")[-1]
        self.client = self.app.test_client()
        self._login_admin()

        response = self.client.get("/teacher")

        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("Последни тестове", body)
        self.assertIn("assignment-groups", body)
        self.assertIn(f'href="/teacher/assignment/{assignment_id}"', body)
        self.assertIn(f'href="/teacher/assignment/{assignment_id}/results"', body)
        self.assertIn("клас", body)
        self.assertIn("Учител: не е посочен", body)

    def test_teacher_assignments_list_shows_class_and_teacher_fallback(self):
        assignment_url = self._create_assignment_as_tester()
        assignment_id = assignment_url.rstrip("/").split("/")[-1]
        self.client = self.app.test_client()
        self._login_admin()

        response = self.client.get("/teacher/assignments")

        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn(f'href="/teacher/assignment/{assignment_id}"', body)
        self.assertIn(f'href="/teacher/assignment/{assignment_id}/results"', body)
        self.assertIn("клас", body)
        self.assertIn("Учител: не е посочен", body)

    def test_future_dzi_named_endpoint_is_admin_only_by_default(self):
        path = "/__test__/dzi-default-denied"

        response = self.client.get(path)
        self.assertEqual(response.status_code, 302)
        self.assertIn("/admin/login", response.headers["Location"])

        self._login_tester()
        response = self.client.get(path)
        self.assertEqual(response.status_code, 302)
        self.assertIn("/admin/login", response.headers["Location"])

        self.client = self.app.test_client()
        self._login_admin()
        response = self.client.get(path)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, b"test-only DZI endpoint")

    def test_admin_login_rejects_external_next_after_successful_login(self):
        for next_url in ("//evil.example/login", "https://evil.example/login"):
            with self.subTest(next_url=next_url):
                client = self.app.test_client()
                response = client.post(
                    f"/admin/login?next={next_url}",
                    data={"password": "admin-pass"},
                )
                self.assertEqual(response.status_code, 302)
                self.assertEqual(response.headers["Location"], "/teacher")

    def test_tester_login_rejects_external_next_after_successful_login(self):
        for next_url in ("//evil.example/login", "https://evil.example/login"):
            with self.subTest(next_url=next_url):
                client = self.app.test_client()
                response = client.post(
                    f"/tester/login?next={next_url}",
                    data={"password": "tester-pass"},
                )
                self.assertEqual(response.status_code, 302)
                self.assertEqual(response.headers["Location"], "/tester")

    def test_login_accepts_normal_local_next_after_successful_login(self):
        admin_response = self.client.post(
            "/admin/login?next=/teacher/assignments",
            data={"password": "admin-pass"},
        )
        self.assertEqual(admin_response.status_code, 302)
        self.assertEqual(admin_response.headers["Location"], "/teacher/assignments")

        client = self.app.test_client()
        tester_response = client.post(
            "/tester/login?next=/teacher/new",
            data={"password": "tester-pass"},
        )
        self.assertEqual(tester_response.status_code, 302)
        self.assertEqual(tester_response.headers["Location"], "/teacher/new")

    def test_mobile_profile_menu_reflects_tester_session(self):
        self._login_tester()
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        body = response.data.decode("utf-8")
        self.assertIn('class="mobile-profile-menu"', body)
        self.assertIn("<summary>Профил</summary>", body)
        self.assertIn("Тестер", body)
        self.assertIn('href="/tester/logout"', body)
        self.assertIn("Вход за админ", body)

    def test_mobile_profile_menu_reflects_admin_session(self):
        self._login_admin()
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        body = response.data.decode("utf-8")
        self.assertIn('class="mobile-profile-menu"', body)
        self.assertIn("<summary>Профил</summary>", body)
        self.assertIn("Админ", body)
        self.assertIn('href="/admin/logout"', body)

    def test_admin_login_with_cyrillic_wrong_password_returns_normal_error(self):
        response = self.client.post(
            "/admin/login",
            data={"password": "грешна-парола"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("Грешна парола.".encode("utf-8"), response.data)
        self.assertNotIn("грешна-парола".encode("utf-8"), response.data)

    def test_tester_login_with_cyrillic_wrong_password_returns_normal_error(self):
        response = self.client.post(
            "/tester/login",
            data={"password": "грешна-парола"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("Грешна парола.".encode("utf-8"), response.data)
        self.assertNotIn("грешна-парола".encode("utf-8"), response.data)

    def test_cross_origin_post_to_admin_login_is_rejected(self):
        response = self.client.post(
            "/admin/login",
            data={"password": "admin-pass"},
            headers={"Origin": "http://evil.example"},
        )

        self.assertEqual(response.status_code, 403)

    def test_scheme_mismatch_post_to_admin_login_is_rejected(self):
        response = self.client.post(
            "/admin/login",
            data={"password": "admin-pass"},
            headers={"Origin": "https://localhost"},
        )

        self.assertEqual(response.status_code, 403)

    def test_same_origin_post_to_admin_login_still_works(self):
        response = self.client.post(
            "/admin/login?next=/teacher/assignments",
            data={"password": "admin-pass"},
            headers={"Origin": "http://localhost"},
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/teacher/assignments")

    def test_cross_origin_post_to_teacher_mutation_is_rejected_after_login(self):
        self._login_tester()
        before_assignments = self._assignment_count()

        response = self.client.post(
            "/teacher/new",
            data={
                "section_id": str(self.section["id"]),
                "question_count": "1",
                "time_limit_minutes": "",
            },
            headers={"Origin": "http://evil.example"},
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(self._assignment_count(), before_assignments)

    def test_same_origin_post_to_teacher_new_still_works(self):
        self._login_tester()
        before_assignments = self._assignment_count()

        response = self.client.post(
            "/teacher/new",
            data={
                "section_id": str(self.section["id"]),
                "question_count": "1",
                "time_limit_minutes": "",
            },
            headers={"Origin": "http://localhost"},
        )

        self.assertEqual(response.status_code, 302)
        self.assertIn("/teacher/assignment/", response.headers["Location"])
        self.assertEqual(self._assignment_count(), before_assignments + 1)

    def test_get_routes_are_not_blocked_by_cross_origin_headers(self):
        response = self.client.get(
            "/admin/login",
            headers={"Origin": "http://evil.example"},
        )

        self.assertEqual(response.status_code, 200)

    def test_missing_origin_and_referer_is_allowed_in_testing_mode(self):
        self.assertTrue(self.app.config["TESTING"])
        response = self.client.post(
            "/admin/login?next=/teacher/assignments",
            data={"password": "admin-pass"},
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/teacher/assignments")

    def test_missing_origin_and_referer_is_rejected_outside_testing_mode(self):
        previous_testing = self.app.config["TESTING"]
        self.app.config["TESTING"] = False
        try:
            response = self.client.post(
                "/admin/login",
                data={"password": "admin-pass"},
            )
        finally:
            self.app.config["TESTING"] = previous_testing

        self.assertEqual(response.status_code, 403)

    def test_same_origin_referer_is_allowed_when_origin_is_missing(self):
        response = self.client.post(
            "/admin/login?next=/teacher/assignments",
            data={"password": "admin-pass"},
            headers={"Referer": "http://localhost/admin/login"},
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/teacher/assignments")

    def test_cross_origin_referer_is_rejected_when_origin_is_missing(self):
        response = self.client.post(
            "/admin/login",
            data={"password": "admin-pass"},
            headers={"Referer": "http://evil.example/admin/login"},
        )

        self.assertEqual(response.status_code, 403)

    def test_session_cookie_security_defaults_are_set(self):
        self.assertEqual(self.app.config["SESSION_COOKIE_SAMESITE"], "Strict")
        self.assertTrue(self.app.config["SESSION_COOKIE_HTTPONLY"])
        self.assertFalse(self.app.config.get("SESSION_COOKIE_SECURE", False))

    def test_switch_profile_rejects_external_next_and_referrer(self):
        self._login_admin()
        response = self.client.get("/profile/tester?next=//evil.example/login")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/tester")

        self.client = self.app.test_client()
        response = self.client.get(
            "/profile/admin",
            headers={"Referer": "https://evil.example/from-referrer"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertNotIn("evil.example", response.headers["Location"])
        self.assertIn("/admin/login?next=/", response.headers["Location"])

    def test_secret_key_without_env_is_not_static_fallback(self):
        with patch.dict(os.environ, {}, clear=True):
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                secret_key = web_app.build_secret_key()

        self.assertNotEqual(secret_key, "local-learnpilot-dev-key")
        self.assertGreaterEqual(len(secret_key), 32)
        self.assertTrue(any("DZI_SECRET_KEY is not set" in str(item.message) for item in caught))


if __name__ == "__main__":
    unittest.main()
