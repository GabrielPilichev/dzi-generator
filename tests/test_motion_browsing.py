"""Browsing motion stays on navigation content, away from questions and tools."""

from pathlib import Path

from tests.support import IsolatedAppTestCase, web_app
from tests.test_homepage_search import _HTMLProbe


ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "web" / "templates"
STATIC = ROOT / "web" / "static"


class BrowsingMotionTest(IsolatedAppTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.app = web_app.app
        cls.app.config.update(TESTING=True)

    def setUp(self):
        self.client = self.app.test_client()

    def _page(self, path):
        response = self.client.get(path)
        self.assertEqual(response.status_code, 200, path)
        html = response.get_data(as_text=True)
        return html, _HTMLProbe(html)

    def test_subject_landing_animates_only_inner_navigation_content(self):
        _, page = self._page("/subjects/information-technologies")
        self.assertIn("content-browse-page", page.first("body")["class"].split())
        hero = page.first("section", **{"data-motion": "intro"})
        self.assertIn("subject-hero", hero["class"])
        self.assertIsNotNone(page.first("h1", **{"data-motion-item": "headline"}))
        self.assertIsNotNone(page.first("p", **{"data-motion-item": "description"}))
        grid = page.first("div", **{"data-motion": "stagger"})
        self.assertIn("subject-class-grid", grid["class"])
        cards = [attrs for _, attrs in page.elements("a")
                 if "subject-class-card" in (attrs.get("class") or "")]
        self.assertTrue(cards)
        self.assertTrue(all("data-motion" not in card for card in cards))
        self.assertTrue(any("data-motion-accent" in attrs for _, attrs in page.elements("span")))

    def test_class_and_grade_limit_reveals_to_first_navigation_group(self):
        _, subject_class = self._page("/subjects/information-technologies/8")
        self.assertIsNotNone(subject_class.first("h1", **{"data-motion-item": "headline"}))
        self.assertEqual(
            len([1 for _, attrs in subject_class.elements() if attrs.get("data-motion") == "stagger"]),
            1,
        )
        _, grade = self._page("/grade/12")
        self.assertIsNotNone(grade.first("h1", **{"data-motion-item": "headline"}))
        self.assertFalse(any("data-motion" in attrs for _, attrs in grade.elements("li")))
        self.assertFalse(any("data-motion" in attrs for _, attrs in grade.elements("section")
                             if "module-card" in (attrs.get("class") or "")))
        self.assertIn('class="pinned-grid" data-motion="stagger"',
                      (TEMPLATES / "grade.html").read_text(encoding="utf-8"))

    def test_topic_and_section_questions_remain_static_with_native_answers(self):
        class_html, class_page = self._page("/subjects/information-technologies/8")
        group_links = [attrs["href"] for _, attrs in class_page.elements("a")
                       if "subject-topic-group-card" in (attrs.get("class") or "")]
        self.assertTrue(group_links, class_html[:200])
        _, group = self._page(group_links[0])
        self.assertIsNotNone(group.first("h1", **{"data-motion-item": "headline"}))
        self.assertFalse(any("data-motion" in attrs for _, attrs in group.elements("article")
                             if "question" in (attrs.get("class") or "")))

        html, section = self._page("/section/grade12-dzi-preparation")
        self.assertIsNotNone(section.first("h1", **{"data-motion-item": "headline"}))
        self.assertFalse(any("data-motion" in attrs for _, attrs in section.elements("article")
                             if "question" in (attrs.get("class") or "")))
        self.assertFalse(any(any(key.startswith("data-motion") for key in attrs)
                             for _, attrs in section.elements("img")))
        self.assertNotIn(
            "data-motion",
            (TEMPLATES / "_question_visuals.html").read_text(encoding="utf-8"),
        )
        for control_id in ("question-search", "question-type-filter",
                           "question-difficulty-filter", "shuffle-questions",
                           "reset-view", "bookmarks-only"):
            self.assertIn(control_id, section.ids())
        self.assertIn('<details class="answer-details">', html)
        self.assertIn("<summary>Покажи отговорите</summary>", html)
        self.assertTrue(any("data-motion-disclosure" in attrs
                            for _, attrs in section.elements("ol")))
        self.assertIn('js/section-tools.js', html)
        topic_template = (TEMPLATES / "subject_topic_group.html").read_text(encoding="utf-8")
        self.assertNotIn('data-motion="stagger"', topic_template)

    def test_disclosure_motion_keeps_native_open_state(self):
        css = (STATIC / "css" / "motion.css").read_text(encoding="utf-8")
        js = (STATIC / "js" / "motion.js").read_text(encoding="utf-8")
        section_tools = (STATIC / "js" / "section-tools.js").read_text(encoding="utf-8")
        self.assertIn(".content-browse-page .answer-details::details-content", css)
        self.assertIn("transition: none", css)
        self.assertIn('document.addEventListener("toggle"', js)
        self.assertIn("armed.has(details)", js)
        self.assertIn("details.open", js)
        self.assertIn("details.open = answersShown", section_tools)

    def test_accent_scope_reduced_motion_and_other_pages(self):
        css = (STATIC / "css" / "motion.css").read_text(encoding="utf-8")
        js = (STATIC / "js" / "motion.js").read_text(encoding="utf-8")
        self.assertIn(".content-browse-page [data-motion-accent]::after", css)
        self.assertIn("transform: scaleX(0)", css)
        self.assertIn("160ms", css)
        self.assertIn("@media (prefers-reduced-motion: reduce)", css)
        self.assertIn("(pointer: coarse)", css)
        self.assertIn("if (!document.body.classList.contains(\"content-browse-page\")) return", js)
        self.assertNotIn("requestAnimationFrame", js)
        self.assertNotIn('addEventListener("scroll"', js)
        for name in ("quiz_start.html", "quiz_attempt.html", "quiz_result.html",
                     "teacher_dashboard.html", "teacher_new.html", "practical_tasks.html"):
            template = (TEMPLATES / name).read_text(encoding="utf-8")
            self.assertNotIn("data-motion-accent", template)
            self.assertNotIn("data-motion-disclosure", template)
        home_html, home = self._page("/")
        self.assertIn("home-page", home.first("body")["class"].split())
        self.assertNotIn("data-motion-accent", home_html)


if __name__ == "__main__":
    import unittest

    unittest.main()
