import unittest
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qs, urlparse


_ROOT = Path(__file__).resolve().parents[1]

from src.subject_registry import get_subject
from tests.support import IsolatedAppTestCase, web_app, IT_ONLY

PHYSICS_PLACEHOLDER_GROUPS = get_subject("physics").placeholder_topic_groups


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


class HomepageSearchTest(IsolatedAppTestCase):
    content_profile = IT_ONLY  # these tests pin Physics/Biology as subjects without content

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
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
            {"home-search-dzi", "home-search-subjects"},
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

    def test_homepage_renders_subject_entry_cards(self):
        html = self._get_home_html()
        parsed = _HTMLProbe(html)
        section = parsed.first("section", id="home-search-subjects")

        self.assertIsNotNone(section)
        self.assertIn("home-subject-section", _classes(section))
        self.assertEqual(section.get("data-home-search-group"), "subjects")
        for grade in (8, 9, 10, 11, 12):
            self.assertIn(f"/subjects/information-technologies/{grade}", parsed.hrefs())
        for slug in (
            "information-technologies",
            "physics",
            "informatics",
            "biology",
            "mathematics",
            "chemistry",
        ):
            self.assertIn(f"/subjects/{slug}", parsed.hrefs())
        self.assertIn("Предмети", parsed.text)
        self.assertIn("6 предмета", parsed.text)
        self.assertIn("Информационни технологии", parsed.text)
        for subject in ("Физика", "Информатика", "Биология", "Математика", "Химия"):
            self.assertIn(subject, parsed.text)
        self.assertIn("В подготовка", parsed.text)

    def test_homepage_search_includes_subject_searchable_text(self):
        html = self._get_home_html()
        parsed = _HTMLProbe(html)
        it_items = [
            attrs
            for _, attrs in parsed.elements()
            if "data-search-item" in attrs
            and "Информационни технологии" in (attrs.get("data-search-text") or "")
        ]
        physics_items = [
            attrs
            for _, attrs in parsed.elements()
            if "data-search-item" in attrs
            and "Физика" in (attrs.get("data-search-text") or "")
        ]
        future_subject_items = {
            subject: [
                attrs
                for _, attrs in parsed.elements()
                if "data-search-item" in attrs
                and subject in (attrs.get("data-search-text") or "")
            ]
            for subject in ("Информатика", "Биология", "Математика", "Химия")
        }

        self.assertTrue(it_items)
        self.assertTrue(physics_items)
        for items in future_subject_items.values():
            self.assertTrue(items)
        self.assertTrue(
            any(
                all(token in attrs.get("data-search-text", "") for token in ("Информационни технологии", "8. клас", "12. клас"))
                for attrs in it_items
            )
        )
        self.assertTrue(
            any(
                all(token in attrs.get("data-search-text", "") for token in ("Физика", "8. клас", "10. клас"))
                and "12. клас" not in attrs.get("data-search-text", "")
                for attrs in physics_items
            )
        )
        # Search text lists the grades LearnPilot supports, per subject.
        last_supported_grade = {"Информатика": 8, "Биология": 10, "Математика": 12, "Химия": 10}
        for subject, items in future_subject_items.items():
            tokens = (subject, "8. клас", f"{last_supported_grade[subject]}. клас", "в подготовка")
            self.assertTrue(
                any(all(token in attrs.get("data-search-text", "") for token in tokens) for attrs in items),
                subject,
            )

    def test_physics_subject_page_renders_class_first_framework(self):
        response = self.client.get("/subjects/physics")

        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        parsed = _HTMLProbe(html)
        self.assertIn("Физика", parsed.text)
        self.assertIn("Съдържанието идва скоро", parsed.text)
        self.assertIn("Избери клас", parsed.text)
        self.assertLess(parsed.text.index("Избери клас"), parsed.text.index("Съдържанието идва скоро"))
        for grade in (8, 9, 10):
            self.assertIn(f"{grade}. клас", parsed.text)
            self.assertIn(f"/subjects/physics/{grade}", parsed.hrefs())
            self.assertIn(f"4 тематични групи в подготовка за {grade}. клас", parsed.text)
        for grade in (11, 12):
            self.assertNotIn(f"/subjects/physics/{grade}", parsed.hrefs())
        self.assertIn("11.–12. клас — профилирана подготовка", parsed.text)
        self.assertNotIn("Теми за 8. клас", parsed.text)
        self.assertNotIn("Движение и сили", parsed.text)
        self.assertIn("все още няма реални въпроси", parsed.text)

    def test_legacy_physics_routes_redirect_permanently_to_the_subject_pages(self):
        response = self.client.get("/physics")
        self.assertEqual(response.status_code, 301)
        self.assertEqual(urlparse(response.headers["Location"]).path, "/subjects/physics")

        for grade in (8, 9, 10):
            response = self.client.get(f"/physics/grade/{grade}")
            self.assertEqual(response.status_code, 301)
            self.assertEqual(
                urlparse(response.headers["Location"]).path, f"/subjects/physics/{grade}"
            )
        # Profiled grades are outside LearnPilot's scope: no redirect, no page.
        for grade in (11, 12):
            self.assertEqual(self.client.get(f"/physics/grade/{grade}").status_code, 404)

        response = self.client.get("/physics/grade/8", follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        parsed = _HTMLProbe(response.get_data(as_text=True))
        self.assertIn("Механика", parsed.text)
        self.assertIn("/subjects/physics/8/mechanics", parsed.hrefs())

    def test_physics_grade_pages_render_configured_topic_groups(self):
        for grade, topic_groups in PHYSICS_PLACEHOLDER_GROUPS.items():
            with self.subTest(grade=grade):
                response = self.client.get(f"/subjects/physics/{grade}")

                self.assertEqual(response.status_code, 200)
                parsed = _HTMLProbe(response.get_data(as_text=True))
                self.assertIn("Физика", parsed.text)
                self.assertIn(f"{grade}. клас", parsed.text)
                self.assertIn(f"Теми за {grade}. клас", parsed.text)
                self.assertIn("Тематични групи", parsed.text)
                self.assertIn("Темите за", parsed.text)
                self.assertIn("В подготовка", parsed.text)
                self.assertNotIn("Какво ще се добави", parsed.text)
                for topic_group in topic_groups:
                    self.assertEqual(topic_group["status"], "in_preparation")
                    self.assertIn(topic_group["title"], parsed.text)
                    self.assertIn(f"/subjects/physics/{grade}/{topic_group['slug']}", parsed.hrefs())
                self.assertIn("Реални въпроси, оценки и тестове по физика още не са добавени", parsed.text)
                self.assertIn("/subjects/physics", parsed.hrefs())

    def test_physics_topic_group_page_renders_preparation_placeholder(self):
        topic_group = PHYSICS_PLACEHOLDER_GROUPS[8][0]
        response = self.client.get(f"/subjects/physics/8/{topic_group['slug']}")

        self.assertEqual(response.status_code, 200)
        parsed = _HTMLProbe(response.get_data(as_text=True))
        self.assertIn("Физика", parsed.text)
        self.assertIn("8. клас", parsed.text)
        self.assertIn(topic_group["title"], parsed.text)
        self.assertIn("Тематична група в подготовка", parsed.text)
        self.assertIn("В подготовка", parsed.text)
        self.assertIn("Няма добавена база с въпроси", parsed.text)
        self.assertIn("/subjects/physics/8", parsed.hrefs())
        self.assertIn("/subjects/physics", parsed.hrefs())

    def test_invalid_physics_grade_returns_404(self):
        response = self.client.get("/subjects/physics/7")

        self.assertEqual(response.status_code, 404)

        response = self.client.get("/physics/grade/7")
        self.assertEqual(response.status_code, 404)

    def test_invalid_physics_topic_group_returns_404(self):
        response = self.client.get("/subjects/physics/8/not-a-topic-group")

        self.assertEqual(response.status_code, 404)

        response = self.client.get("/subjects/physics/7/mechanics")
        self.assertEqual(response.status_code, 404)

    def test_subject_hub_includes_all_configured_subjects(self):
        response = self.client.get("/")

        self.assertEqual(response.status_code, 200)
        parsed = _HTMLProbe(response.get_data(as_text=True))
        for subject in (
            "Информационни технологии",
            "Физика",
            "Информатика",
            "Биология",
            "Математика",
            "Химия",
        ):
            self.assertIn(subject, parsed.text)

    def test_active_it_subject_page_renders_existing_class_links(self):
        response = self.client.get("/subjects/information-technologies")

        self.assertEqual(response.status_code, 200)
        parsed = _HTMLProbe(response.get_data(as_text=True))
        self.assertIn("Информационни технологии", parsed.text)
        self.assertIn("Съдържанието вече е налично", parsed.text)
        for grade in (8, 9, 10, 11, 12):
            self.assertIn(f"{grade}. клас", parsed.text)
            self.assertIn(f"/subjects/information-technologies/{grade}", parsed.hrefs())

    def test_future_subject_page_renders_placeholder_state(self):
        response = self.client.get("/subjects/biology")

        self.assertEqual(response.status_code, 200)
        parsed = _HTMLProbe(response.get_data(as_text=True))
        self.assertIn("Биология", parsed.text)
        self.assertIn("Съдържанието идва скоро", parsed.text)
        self.assertIn("В подготовка", parsed.text)
        for grade in (8, 9, 10):
            self.assertIn(f"/subjects/biology/{grade}", parsed.hrefs())
        # Profiled grades are explained, never linked.
        for grade in (11, 12):
            self.assertNotIn(f"/subjects/biology/{grade}", parsed.hrefs())
        self.assertIn("11.–12. клас — профилирана подготовка", parsed.text)

    def test_future_subject_class_page_renders_placeholder_state(self):
        response = self.client.get("/subjects/biology/8")

        self.assertEqual(response.status_code, 200)
        parsed = _HTMLProbe(response.get_data(as_text=True))
        self.assertIn("Биология", parsed.text)
        self.assertIn("8. клас", parsed.text)
        self.assertIn("Темите за 8. клас са в подготовка", parsed.text)
        self.assertIn("Какво ще се добави", parsed.text)
        self.assertIn("В подготовка", parsed.text)

    def test_invalid_subject_returns_404(self):
        response = self.client.get("/subjects/unknown-subject")

        self.assertEqual(response.status_code, 404)

    def test_invalid_future_subject_grade_returns_404(self):
        response = self.client.get("/subjects/physics/7")

        self.assertEqual(response.status_code, 404)

    def test_existing_grade_routes_still_work(self):
        response = self.client.get("/grade/8")

        self.assertEqual(response.status_code, 200)
        parsed = _HTMLProbe(response.get_data(as_text=True))
        self.assertIn("8. клас", parsed.text)

    def test_homepage_omits_old_mode_class_and_topic_sections(self):
        html = self._get_home_html()
        parsed = _HTMLProbe(html)

        self.assertNotIn("Класове и раздели", parsed.text)
        self.assertNotIn("Теми и раздели", parsed.text)
        self.assertNotIn("Учебно съдържание", parsed.text)
        self.assertIsNone(parsed.first("section", id="grades"))
        self.assertIsNone(parsed.first("section", id="home-search-topics"))

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
        self._assert_login_link(html, "/login", "/")

    def test_desktop_profile_login_entries_match_mobile_choices(self):
        html = self._get_home_html()
        self._assert_login_link(html, "/login", "/", min_count=2)
        for removed in ("/tester/login", "/admin/login"):
            self.assertNotIn(removed, html)

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

    def test_it_subject_card_links_to_its_canonical_grade_pages(self):
        html = self._get_home_html()
        hrefs = _HTMLProbe(html).hrefs()
        for grade in (8, 9, 10, 11, 12):
            self.assertIn(f"/subjects/information-technologies/{grade}", hrefs)
            # /grade/<n> is the subject overview of a grade, not an IT page.
            self.assertNotIn(f"/grade/{grade}", hrefs)

    def test_homepage_no_longer_lists_topic_titles(self):
        html = self._get_home_html()
        # Existing topic routes remain available, but topic listings are no longer
        # rendered directly on the subject-first homepage.
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
        self.assertNotIn(row["title_bg"], parsed.text)
        self.assertNotIn(f"/section/{row['section_slug']}", parsed.hrefs())

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
            {"home-search-dzi", "home-search-subjects"},
        )

    def test_topics_section_is_not_rendered_on_homepage(self):
        html = self._get_home_html()
        parsed = _HTMLProbe(html)
        self.assertIsNone(parsed.first("section", id="home-search-topics"))
        self.assertFalse(
            any(attrs.get("data-home-search-group") == "topics" for _, attrs in parsed.elements())
        )
        self.assertFalse(
            any("topic-list-title" in _classes(attrs) for _, attrs in parsed.elements("span"))
        )


if __name__ == "__main__":
    unittest.main()
