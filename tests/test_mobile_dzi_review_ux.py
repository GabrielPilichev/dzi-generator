import atexit
import os
import re
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


DZI_SECTION_SLUG = "grade12-dzi-preparation"


class MobileDziReviewUxTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = web_app.app
        cls.app.config.update(TESTING=True)

    def setUp(self):
        self.client = self.app.test_client()

    def _get_html(self, path: str) -> str:
        response = self.client.get(path)
        self.assertEqual(response.status_code, 200, f"GET {path} returned {response.status_code}")
        return response.get_data(as_text=True)

    # --- Scroll-to-top on navigation ---------------------------------------

    def test_base_disables_browser_scroll_restoration(self):
        html = self._get_html("/")
        self.assertIn('history.scrollRestoration = "manual"', html)
        # Pageshow handler scrolls to top when there is no hash
        self.assertIn("window.scrollTo(0, 0)", html)
        self.assertIn("location.hash", html)

    def test_scroll_reset_present_on_grade_page(self):
        html = self._get_html("/grade/8")
        self.assertIn("scrollRestoration", html)
        self.assertIn("window.scrollTo(0, 0)", html)

    # --- DZI / "Матура по ИТ" card on homepage -----------------------------

    def test_dzi_card_is_a_single_tappable_link(self):
        html = self._get_html("/")
        # The DZI card itself must be an anchor (not a div with nested buttons)
        # so the whole tile is tappable on mobile.
        match = re.search(
            r'<a[^>]*class="[^"]*mode-card--dzi[^"]*"[^>]*href="([^"]+)"',
            html,
        )
        self.assertIsNotNone(match, "Expected <a class='mode-card--dzi'> on homepage")
        href = match.group(1)
        self.assertIn(f"/section/{DZI_SECTION_SLUG}", href)

    def test_dzi_card_links_to_a_working_section_route(self):
        # The link must actually resolve (not 404).
        response = self.client.get(f"/section/{DZI_SECTION_SLUG}")
        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("Подготовка за матура", body)

    def test_dzi_card_label_is_bulgarian(self):
        html = self._get_html("/")
        # Title and CTA remain in Bulgarian
        self.assertIn("Матура по ИТ", html)
        self.assertIn("Към прегледа", html)

    # --- Mobile filter (section toolbar) -----------------------------------

    def test_section_toolbar_is_static_on_mobile(self):
        css_path = _ROOT / "web" / "static" / "css" / "ui-pass9.css"
        css = css_path.read_text(encoding="utf-8")

        # Sanity: mobile media query exists
        self.assertIn("@media (max-width: 768px)", css)

        # Locate the mobile breakpoint block and assert section-toolbar is
        # explicitly de-stickied inside it.
        media_match = re.search(
            r"@media \(max-width: 768px\)\s*\{(.*)\n\}\s*",
            css,
            flags=re.DOTALL,
        )
        self.assertIsNotNone(media_match, "Expected mobile media query block")
        mobile_block = media_match.group(1)
        self.assertIn(".section-toolbar", mobile_block)
        self.assertIn("position: static", mobile_block)

    def test_section_filter_controls_still_render(self):
        # Reveal-answer and filter controls must not have been removed.
        # Pick the DZI section, which is known to have content.
        html = self._get_html(f"/section/{DZI_SECTION_SLUG}")
        self.assertIn('id="question-search"', html)
        self.assertIn('id="question-type-filter"', html)
        self.assertIn('id="question-difficulty-filter"', html)
        self.assertIn('id="toggle-correct"', html)
        # Reveal-answer summary copy unchanged
        self.assertIn("Покажи отговорите", html)

    # --- Top navigation overflow ------------------------------------------

    def test_top_nav_links_can_scroll_horizontally(self):
        css_path = _ROOT / "web" / "static" / "css" / "ui-pass9.css"
        css = css_path.read_text(encoding="utf-8")
        # Horizontal overflow scroll is preserved on the nav-links row.
        self.assertRegex(css, r"\.nav-links\s*\{\s*overflow-x:\s*auto")


if __name__ == "__main__":
    unittest.main()
