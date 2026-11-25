"""Server-rendered motion hooks and progressive enhancement contracts."""

from pathlib import Path

from tests.auth_helpers import sign_in_admin, sign_in_teacher
from tests.support import IsolatedAppTestCase, web_app
from tests.test_homepage_search import _HTMLProbe


ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "web" / "static"
TEMPLATES = ROOT / "web" / "templates"


class MotionFoundationTest(IsolatedAppTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.app = web_app.app
        cls.app.config.update(TESTING=True)

    def setUp(self):
        self.client = self.app.test_client()

    def test_assets_are_local_and_loaded_after_main_ui_css(self):
        html = self.client.get("/").get_data(as_text=True)
        self.assertLess(html.index("css/ui-pass9.css"), html.index("css/motion.css"))
        self.assertRegex(html, r'<script src="/static/js/motion\.js\?v=[0-9a-f]{12}" defer></script>')
        self.assertNotRegex(html, r'<(?:script|link)[^>]+(?:src|href)="https?://')
        for path in ("/static/js/motion.js", "/static/css/motion.css"):
            response = self.client.get(path)
            try:
                self.assertEqual(response.status_code, 200)
            finally:
                response.close()

    def test_homepage_hooks_and_essential_content_render_without_javascript(self):
        html = self.client.get("/").get_data(as_text=True)
        parsed = _HTMLProbe(html)
        self.assertIn("home-page", parsed.first("body")["class"].split())
        self.assertEqual(parsed.first("section", **{"data-motion": "intro"})["class"], "hero home-hero")
        self.assertEqual(
            {attrs.get("data-motion-item") for _, attrs in parsed.elements() if attrs.get("data-motion-item")},
            {"eyebrow", "headline", "description", "stats", "search", "subject-eyebrow", "subject-heading"},
        )
        self.assertEqual(parsed.first("section", id="home-search-dzi")["data-motion"], "reveal")
        self.assertIsNotNone(parsed.first("div", **{"data-motion": "stagger"}))
        self.assertIn("Учи по-умно. Подготвяй се уверено.", parsed.text)
        self.assertIn("Избери предмет", parsed.text)
        self.assertIsNotNone(parsed.first("input", id="home-search-input"))
        self.assertIn("/section/grade12-dzi-preparation", parsed.hrefs())

    def test_homepage_and_browsing_hooks_stay_in_their_scopes(self):
        for path in ("/grade/8", "/subjects/information-technologies"):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200)
            parsed = _HTMLProbe(response.get_data(as_text=True))
            self.assertNotIn("home-page", parsed.first("body")["class"].split())
            self.assertIn("content-browse-page", parsed.first("body")["class"].split())
            self.assertTrue(any(attrs.get("data-motion") == "intro" for _, attrs in parsed.elements()))
            self.assertFalse(any(attrs.get("data-motion") == "reveal" for _, attrs in parsed.elements()))

        sign_in_admin(self.client, web_app.app.config["DB_PATH"])
        response = self.client.get("/teacher")
        self.assertEqual(response.status_code, 200)
        parsed = _HTMLProbe(response.get_data(as_text=True))
        self.assertNotIn("home-page", parsed.first("body")["class"].split())
        self.assertEqual(
            [(tag, attrs.get("data-motion")) for tag, attrs in parsed.elements() if "data-motion" in attrs],
            [("h1", "feedback")],
        )
        for name in ("quiz_attempt.html",):
            self.assertNotIn("data-motion", (TEMPLATES / name).read_text(encoding="utf-8"))

    def test_navigation_keeps_immediate_state_and_has_native_no_js_fallback(self):
        sign_in_teacher(self.client, web_app.app.config["DB_PATH"])
        html = self.client.get("/").get_data(as_text=True)
        self.assertIn('aria-controls="nav-menu-classes"', html)
        self.assertIn('aria-controls="nav-menu-subjects"', html)
        self.assertIn('aria-expanded="false"', html)
        self.assertIn("panel.hidden = !isOpen", html)
        self.assertIn('event.key === "Escape"', html)
        self.assertEqual(html.count('<details class="nojs-nav-menu">'), 2)
        self.assertIn('<noscript>', html)
        self.assertIn('href="/grade/12"', html)
        self.assertIn('href="/subjects/information-technologies"', html)
        self.assertIn('<details class="mobile-profile-menu">', html)

    def test_reduced_motion_and_static_fallback_are_explicit(self):
        css = (STATIC / "css" / "motion.css").read_text(encoding="utf-8")
        js = (STATIC / "js" / "motion.js").read_text(encoding="utf-8")
        self.assertIn("@media (prefers-reduced-motion: reduce)", css)
        self.assertIn("(pointer: coarse)", css)
        self.assertNotRegex(css, r"(?m)^\s*\[data-motion(?:\]|=)")
        self.assertNotRegex(css, r"opacity:\s*0\b")
        self.assertIn('window.matchMedia("(prefers-reduced-motion: reduce)")', js)
        self.assertIn("IntersectionObserver", js)
        self.assertIn("Element.prototype.animate", js)
        self.assertIn("event.persisted", js)
        self.assertIn("sessionStorage", js)
        self.assertNotRegex(js, r"\b(?:gsap|ScrollTrigger|Lenis|THREE|rive)\b")
        self.assertNotIn("requestAnimationFrame", js)
        self.assertNotIn("addEventListener(\"scroll\"", js)
        self.assertNotIn("backdropFilter", js)


if __name__ == "__main__":
    import unittest

    unittest.main()
