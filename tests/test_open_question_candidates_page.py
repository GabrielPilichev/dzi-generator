import atexit
import os
import shutil
import tempfile
import unittest
from pathlib import Path


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


class OpenQuestionCandidatesPageTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = web_app.app
        _register_dzi_default_denied_test_route(cls.app)
        cls.app.config.update(TESTING=True)

    def setUp(self):
        self.client = self.app.test_client()

    def _login_admin(self):
        with self.client.session_transaction() as sess:
            sess["admin_authenticated"] = True
            sess["ui_profile"] = "admin"

    def _login_tester(self):
        with self.client.session_transaction() as sess:
            sess["tester_authenticated"] = True
            sess["ui_profile"] = "tester"

    def test_admin_can_access_open_question_candidates_page(self):
        self._login_admin()
        response = self.client.get("/admin/open-question-candidates")

        self.assertEqual(response.status_code, 200)
        self.assertIn("Отворени въпроси за смесени тестове".encode("utf-8"), response.data)
        self.assertIn("read-only".encode("utf-8"), response.data)
        self.assertIn("MC-only".encode("utf-8"), response.data)

    def test_tester_cannot_access_open_question_candidates_page(self):
        self._login_tester()
        response = self.client.get("/admin/open-question-candidates")

        self.assertEqual(response.status_code, 302)
        self.assertIn("/admin/login", response.headers["Location"])

    def test_sessionless_user_cannot_access_open_question_candidates_page(self):
        response = self.client.get("/admin/open-question-candidates")

        self.assertEqual(response.status_code, 302)
        self.assertIn("/admin/login", response.headers["Location"])

    def test_page_renders_candidate_count_and_grouping(self):
        conn = web_app.quiz_db()
        try:
            candidates = web_app.fetch_open_question_candidates(conn)
        finally:
            conn.close()

        grouped_sources = sorted({candidate["source_slug"] for candidate in candidates})
        self.assertGreater(len(candidates), 0)

        self._login_admin()
        response = self.client.get("/admin/open-question-candidates")

        self.assertEqual(response.status_code, 200)
        self.assertIn(str(len(candidates)).encode("utf-8"), response.data)
        for source_slug in grouped_sources:
            self.assertIn(source_slug.encode("utf-8"), response.data)
        self.assertIn(b"question_id", response.data)
        self.assertIn("Подвъпроси".encode("utf-8"), response.data)


if __name__ == "__main__":
    unittest.main()
