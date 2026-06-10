import atexit
import os
import shutil
import tempfile
import unittest
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qs, urlparse


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


class _HTMLProbe(HTMLParser):
    def __init__(self, html: str):
        super().__init__()
        self.tags = []
        self.text_parts = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)

    def handle_data(self, data):
        if data.strip():
            self.text_parts.append(data.strip())

    def elements(self, tag=None):
        return [
            (element_tag, attrs)
            for element_tag, attrs in self.tags
            if tag is None or element_tag == tag
        ]

    def first(self, tag=None, **expected_attrs):
        for element_tag, attrs in self.elements(tag):
            if all(attrs.get(name) == value for name, value in expected_attrs.items()):
                return attrs
        return None

    def hrefs(self):
        return [attrs["href"] for _, attrs in self.elements("a") if attrs.get("href")]

    def ids(self):
        return {attrs["id"] for _, attrs in self.elements() if attrs.get("id")}

    @property
    def text(self):
        return " ".join(self.text_parts)


def _classes(attrs):
    return set((attrs.get("class") or "").split())


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

    def _assert_login_link(self, html: str, path: str, next_path: str, *, min_count: int = 1):
        matches = []
        for href in _HTMLProbe(html).hrefs():
            parsed = urlparse(href)
            if parsed.path == path and parse_qs(parsed.query).get("next") == [next_path]:
                matches.append(href)
        self.assertGreaterEqual(len(matches), min_count)

    def test_search_input_is_rendered(self):
        html = self._get_home_html()
        parsed = _HTMLProbe(html)
        search_form = parsed.first("form", role="search")
        search_input = parsed.first("input", id="home-search-input")

        self.assertIsNotNone(search_form)
        self.assertTrue(search_form.get("aria-label"))
        self.assertIsNotNone(search_input)
        self.assertEqual(search_input.get("type"), "search")
        self.assertEqual(
            set((search_input.get("aria-controls") or "").split()),
            {"home-search-dzi", "home-search-physics", "home-search-results", "home-search-topics"},
        )
        self.assertTrue(
            any(attrs.get("for") == "home-search-input" for _, attrs in parsed.elements("label"))
        )

    def test_homepage_renders_distinct_dzi_preparation_section(self):
        html = self._get_home_html()
        parsed = _HTMLProbe(html)
        section = parsed.first("section", id="home-search-dzi")

        self.assertIsNotNone(section)
        self.assertIn("home-dzi-section", _classes(section))
        self.assertEqual(section.get("data-home-search-group"), "dzi")
        self.assertIn("ДЗИ", parsed.text)
        self.assertIn("Матура", parsed.text)

    def test_homepage_dzi_preparation_link_points_to_existing_route(self):
        html = self._get_home_html()
        self.assertIn("/section/grade12-dzi-preparation", _HTMLProbe(html).hrefs())
        response = self.client.get("/section/grade12-dzi-preparation")
        self.assertEqual(response.status_code, 200)

    def test_homepage_search_includes_dzi_searchable_text(self):
        html = self._get_home_html()
        parsed = _HTMLProbe(html)
        dzi_items = [
            attrs
            for _, attrs in parsed.elements()
            if "data-search-item" in attrs
            and "ДЗИ" in (attrs.get("data-search-text") or "")
        ]

        self.assertTrue(dzi_items)
        self.assertTrue(
            any(
                all(token in attrs.get("data-search-text", "") for token in ("ДЗИ", "матура", "26", "27", "28"))
                for attrs in dzi_items
            )
        )

    def test_homepage_renders_physics_subject_entry(self):
        html = self._get_home_html()
        parsed = _HTMLProbe(html)
        section = parsed.first("section", id="home-search-physics")

        self.assertIsNotNone(section)
        self.assertIn("home-physics-section", _classes(section))
        self.assertEqual(section.get("data-home-search-group"), "physics")
        self.assertIn("/physics", parsed.hrefs())
        self.assertIn("Физика", parsed.text)
        self.assertIn("в подготовка", parsed.text)

    def test_homepage_search_includes_physics_searchable_text(self):
        html = self._get_home_html()
        parsed = _HTMLProbe(html)
        physics_items = [
            attrs
            for _, attrs in parsed.elements()
            if "data-search-item" in attrs
            and "Физика" in (attrs.get("data-search-text") or "")
        ]

        self.assertTrue(physics_items)
        self.assertTrue(
            any(
                all(token in attrs.get("data-search-text", "") for token in ("Физика", "8. клас", "12. клас"))
                for attrs in physics_items
            )
        )

    def test_physics_page_renders_class_first_placeholder_shell(self):
        response = self.client.get("/physics")

        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        parsed = _HTMLProbe(html)
        self.assertIn("Физика", parsed.text)
        self.assertIn("Съдържанието идва скоро", parsed.text)
        self.assertIn("Избери клас", parsed.text)
        for grade in (8, 9, 10, 11, 12):
            self.assertIn(f"{grade}. клас", parsed.text)
            self.assertIn(f"/physics/grade/{grade}", parsed.hrefs())
        self.assertNotIn("Теми за 8. клас", parsed.text)
        self.assertNotIn("Движение и сили", parsed.text)
        self.assertIn("все още няма реални въпроси", parsed.text)

    def test_physics_grade_page_renders_topic_placeholders(self):
        response = self.client.get("/physics/grade/8")

        self.assertEqual(response.status_code, 200)
        parsed = _HTMLProbe(response.get_data(as_text=True))
        self.assertIn("Физика", parsed.text)
        self.assertIn("8. клас", parsed.text)
        self.assertIn("Теми за 8. клас", parsed.text)
        self.assertIn("Физични величини и измерване", parsed.text)
        self.assertIn("Движение и сили", parsed.text)
        self.assertIn("в подготовка", parsed.text)
        self.assertIn("Реални въпроси, оценки и тестове по физика още не са добавени", parsed.text)
        self.assertIn("/physics", parsed.hrefs())

    def test_invalid_physics_grade_returns_404(self):
        response = self.client.get("/physics/grade/7")

        self.assertEqual(response.status_code, 404)

    def test_logged_out_teacher_card_points_to_tester_login(self):
        html = self._get_home_html()
        self._assert_login_link(html, "/tester/login", "/teacher/new")
        self.assertIn("Учител", _HTMLProbe(html).text)

    def test_mobile_profile_login_entry_is_rendered(self):
        html = self._get_home_html()
        parsed = _HTMLProbe(html)
        mobile_menu = next(
            (
                attrs
                for _, attrs in parsed.elements("details")
                if "mobile-profile-menu" in _classes(attrs)
            ),
            None,
        )

        self.assertIsNotNone(mobile_menu)
        self.assertTrue(parsed.elements("summary"))
        self._assert_login_link(html, "/tester/login", "/")
        self._assert_login_link(html, "/admin/login", "/")

    def test_desktop_profile_login_entries_match_mobile_choices(self):
        html = self._get_home_html()
        self._assert_login_link(html, "/tester/login", "/", min_count=2)
        self._assert_login_link(html, "/admin/login", "/", min_count=2)

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
        try:
            self.assertEqual(asset.status_code, 200)
            body = asset.get_data(as_text=True)
        finally:
            asset.close()
        self.assertIn("home-search-input", body)

    def test_searchable_items_have_data_attributes(self):
        html = self._get_home_html()
        searchable_items = [
            attrs
            for _, attrs in _HTMLProbe(html).elements()
            if "data-search-item" in attrs
        ]

        self.assertTrue(searchable_items)
        self.assertTrue(all(attrs.get("data-search-text") for attrs in searchable_items))

    def test_grade_links_still_render(self):
        html = self._get_home_html()
        hrefs = _HTMLProbe(html).hrefs()
        for grade in (8, 9, 10, 11, 12):
            self.assertIn(f"/grade/{grade}", hrefs)

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
        parsed = _HTMLProbe(html)
        self.assertIn(row["title_bg"], parsed.text)
        self.assertIn(f"/section/{row['section_slug']}", parsed.hrefs())
        self.assertTrue(
            any(
                row["title_bg"] in (attrs.get("data-search-text") or "")
                and row["section_slug"] in (attrs.get("data-search-text") or "")
                for _, attrs in parsed.elements()
            )
        )

    def test_no_results_message_present(self):
        html = self._get_home_html()
        parsed = _HTMLProbe(html)
        empty_message = parsed.first("p", id="home-search-empty")

        self.assertIsNotNone(empty_message)
        self.assertEqual(empty_message.get("role"), "status")
        self.assertEqual(empty_message.get("aria-live"), "polite")
        self.assertIn("hidden", empty_message)
        self.assertIn("Няма съвпадения", parsed.text)

    def test_search_controlled_regions_are_present(self):
        html = self._get_home_html()
        self.assertGreaterEqual(
            _HTMLProbe(html).ids(),
            {"home-search-dzi", "home-search-physics", "home-search-results", "home-search-topics"},
        )

    def test_topics_section_heading(self):
        html = self._get_home_html()
        parsed = _HTMLProbe(html)
        topics_section = parsed.first("section", id="home-search-topics")
        topic_list = next(
            (
                attrs
                for _, attrs in parsed.elements("ul")
                if attrs.get("data-home-search-group") == "topics"
            ),
            None,
        )

        self.assertIsNotNone(topics_section)
        self.assertIn("home-section--topics", _classes(topics_section))
        self.assertIsNotNone(topic_list)
        self.assertTrue(
            any("topic-list-title" in _classes(attrs) for _, attrs in parsed.elements("span"))
        )


if __name__ == "__main__":
    unittest.main()
