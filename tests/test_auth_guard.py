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
