"""Homepage showcase remains decorative, scoped, and progressively enhanced."""

from pathlib import Path

from tests.support import IsolatedAppTestCase, web_app, IT_ONLY
from tests.test_homepage_search import _HTMLProbe


ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "web" / "templates"
STATIC = ROOT / "web" / "static"


class HomepageShowcaseMotionTest(IsolatedAppTestCase):
    content_profile = IT_ONLY  # these tests pin Physics/Biology as subjects without content

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.app = web_app.app
        cls.app.config.update(TESTING=True)

    def setUp(self):
        self.client = self.app.test_client()

    def test_hero_text_is_semantic_and_signature_is_decorative(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        page = _HTMLProbe(html)
        self.assertEqual(len(page.elements("h1")), 1)
        self.assertEqual(page.first("h1")["data-motion-item"], "headline")
        self.assertIn("Учи по-умно. Подготвяй се уверено.", page.text)
        self.assertIn('<div class="home-headline-reveal"><h1 data-motion-item="headline">', html)
        signature = page.first("span", **{"class": "home-hero-signature"})
        self.assertEqual(signature["aria-hidden"], "true")
        self.assertIsNotNone(page.first("span", **{"class": "home-hero-signature-line"}))
        self.assertIsNotNone(page.first("span", **{"class": "home-hero-signature-tip"}))
        self.assertNotIn("aria-hidden=\"true\"><h1", html)
        self.assertNotIn("data-motion-letter", html)
        self.assertNotIn("data-motion-word", html)

    def test_search_and_discovery_remain_ready_without_animation(self):
        html = self.client.get("/").get_data(as_text=True)
        page = _HTMLProbe(html)
        search = page.first("input", id="home-search-input")
        self.assertEqual(search["type"], "search")
        self.assertNotIn("disabled", search)
        self.assertNotIn("hidden", search)
        self.assertEqual(page.first("form", role="search")["data-motion-item"], "search")
        self.assertEqual(page.first("section", id="home-search-dzi")["data-motion"], "reveal")
        self.assertIsNotNone(page.first("div", **{"data-motion": "stagger"}))
        self.assertFalse(any("data-motion" in attrs for _, attrs in page.elements("article")
                             if "home-subject-card" in (attrs.get("class") or "")))
        self.assertIn("/section/grade12-dzi-preparation", page.hrefs())
        search_js = (STATIC / "js" / "home-search.js").read_text(encoding="utf-8")
        self.assertIn('input.addEventListener("input", applyFilter)', search_js)
        self.assertNotIn("data-motion", search_js)

    def test_showcase_targets_inner_content_not_glass_or_question_images(self):
        template = (TEMPLATES / "index.html").read_text(encoding="utf-8")
        motion = (STATIC / "js" / "motion.js").read_text(encoding="utf-8")
        self.assertIn('class="hero home-hero" data-motion="intro"', template)
        self.assertIn('class="home-section home-dzi-section"', template)
        self.assertIn('<div data-motion-item>', template)
        self.assertIn('card.querySelector("[data-motion-item]")', motion)
        self.assertNotIn("data-motion", (TEMPLATES / "_question_visuals.html").read_text(encoding="utf-8"))
        self.assertNotIn("data-showcase", (TEMPLATES / "_question_visuals.html").read_text(encoding="utf-8"))
        self.assertNotIn('data-motion="stagger"', (TEMPLATES / "section.html").read_text(encoding="utf-8"))

    def test_showcase_repeat_suppression_and_late_skip(self):
        motion = (STATIC / "js" / "motion.js").read_text(encoding="utf-8")
        self.assertIn('sessionStorage.getItem("learnpilot:home-intro-seen")', motion)
        self.assertIn('sessionStorage.setItem("learnpilot:home-intro-seen", "1")', motion)
        self.assertIn("window.location.hash", motion)
        self.assertIn('navigation.type === "back_forward"', motion)
        self.assertIn("performance.now() - firstPaint.startTime > 80", motion)
        self.assertIn("stopForSearch", motion)
        self.assertIn("stopViewportMotion()", motion)
        self.assertIn("completed.has(target)", motion)
        self.assertIn('fill = "none"', motion)
        self.assertIn('"backwards"', motion)
        self.assertNotIn('addEventListener("scroll"', motion)
        self.assertNotIn("requestAnimationFrame", motion)

    def test_home_only_cta_mobile_reduced_motion_and_no_external_library(self):
        html = self.client.get("/").get_data(as_text=True)
        page = _HTMLProbe(html)
        self.assertEqual(len([1 for _, attrs in page.elements() if "data-showcase-cta" in attrs]), 2)
        css = (STATIC / "css" / "motion.css").read_text(encoding="utf-8")
        motion = (STATIC / "js" / "motion.js").read_text(encoding="utf-8")
        self.assertIn(".home-page .home-headline-reveal", css)
        self.assertIn(".home-page [data-showcase-cta]::after", css)
        self.assertIn("(hover: hover) and (pointer: fine)", css)
        self.assertIn("(prefers-reduced-motion: reduce)", css)
        self.assertIn("(max-width: 768px)", css)
        self.assertIn("pointer-events: none", css)
        self.assertNotRegex(css, r"(?m)^\s*\[data-motion(?:\]|=)")
        self.assertNotRegex(css, r"opacity:\s*0\b")
        self.assertIn('if (!document.body.classList.contains("home-page")) return', motion)
        self.assertIn("if (reduced.matches)", motion)
        self.assertNotRegex(motion, r"\b(?:gsap|ScrollTrigger|Lenis|THREE|Rive)\b")
        self.assertNotRegex(html, r'<(?:script|link)[^>]+(?:src|href)="https?://')
        for name in ("quiz_start.html", "quiz_attempt.html", "quiz_result.html",
                     "subject.html", "teacher_dashboard.html", "practical_tasks.html"):
            self.assertNotIn("data-showcase", (TEMPLATES / name).read_text(encoding="utf-8"))


if __name__ == "__main__":
    import unittest

    unittest.main()
