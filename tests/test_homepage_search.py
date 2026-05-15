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


class HomepageSearchTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = web_app.app
        cls.app.config.update(TESTING=True)

    def setUp(self):
        self.client = self.app.test_client()

    def _get_home_html(self) -> str:
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        return response.get_data(as_text=True)

    def test_search_input_is_rendered(self):
        html = self._get_home_html()
        self.assertIn('id="home-search-input"', html)
        self.assertIn('type="search"', html)
        # Bulgarian placeholder/label copy
        self.assertIn("Търсене по теми", html)

    def test_mobile_profile_login_entry_is_rendered(self):
        html = self._get_home_html()
        self.assertIn('class="mobile-profile-menu"', html)
        self.assertIn("<summary>Вход</summary>", html)
        self.assertIn("Вход за тестер", html)
        self.assertIn('href="/tester/login?next=/"', html)
        self.assertIn("Вход за админ", html)
        self.assertIn('href="/admin/login?next=/"', html)

    def test_mobile_profile_css_is_present(self):
        css_path = _ROOT / "web" / "static" / "css" / "ui-pass9.css"
        css = css_path.read_text(encoding="utf-8")
        self.assertIn(".mobile-profile-menu", css)
        self.assertIn(".mobile-profile-panel", css)
        self.assertIn(".profile-switch", css)
        self.assertIn("display: none", css)

    def test_search_script_is_included(self):
        html = self._get_home_html()
        self.assertIn("js/home-search.js", html)
        # Static asset is actually served by Flask
        asset = self.client.get("/static/js/home-search.js")
        self.assertEqual(asset.status_code, 200)
        body = asset.get_data(as_text=True)
        self.assertIn("home-search-input", body)

    def test_searchable_items_have_data_attributes(self):
        html = self._get_home_html()
        self.assertIn("data-search-item", html)
        # Items expose search text so filtering can run client-side
        self.assertIn("data-search-text", html)

    def test_grade_links_still_render(self):
        html = self._get_home_html()
        # Existing grade cards still link to /grade/<n>
        for grade in (8, 9, 10, 11, 12):
            self.assertIn(f'/grade/{grade}"', html)

    def test_searchable_sections_include_topic_titles(self):
        html = self._get_home_html()
        # Pull one real section title from the DB and assert it appears
        conn = web_app.quiz_db()
        try:
            row = conn.execute(
                """
                SELECT title_bg, section_slug
                FROM curriculum_sections
                WHERE class BETWEEN 8 AND 12
                ORDER BY id
                LIMIT 1
                """
            ).fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(row, "expected at least one curriculum section in grades 8-12")
        self.assertIn(row["title_bg"], html)
        self.assertIn(f"/section/{row['section_slug']}", html)

    def test_no_results_message_present(self):
        html = self._get_home_html()
        # Hidden by default; revealed by JS when query has no matches
        self.assertIn('id="home-search-empty"', html)
        self.assertIn("Няма съвпадения", html)

    def test_topics_section_heading(self):
        html = self._get_home_html()
        self.assertIn("Теми и раздели", html)


if __name__ == "__main__":
    unittest.main()
