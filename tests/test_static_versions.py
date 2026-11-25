"""Static URLs carry a content hash, so a cache never pairs old CSS/JS with new HTML."""

import hashlib
import re
import tempfile
import unittest
from pathlib import Path

from flask import Flask, url_for

from tests import support
from tests.auth_helpers import sign_in_admin
from web import static_versions

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "web" / "static"


def expected_version(relative: str) -> str:
    return hashlib.sha256((STATIC / relative).read_bytes()).hexdigest()[:12]


class VersionedUrlTest(unittest.TestCase):
    def test_a_static_url_carries_the_files_content_hash(self):
        with support.web_app.app.test_request_context():
            url = url_for("static", filename="css/ui-pass9.css")
        self.assertEqual(url, f"/static/css/ui-pass9.css?v={expected_version('css/ui-pass9.css')}")

    def test_a_changed_file_gets_a_new_url_and_a_missing_one_gets_none(self):
        with tempfile.TemporaryDirectory() as directory:
            static = Path(directory) / "static"
            static.mkdir()
            app = Flask(__name__, static_folder=str(static))
            static_versions.install(app)
            asset = static / "app.css"
            asset.write_text("a {}", encoding="utf-8")
            with app.test_request_context():
                first = url_for("static", filename="app.css")
                static_versions.file_version.cache_clear()
                asset.write_text("a { color: red }", encoding="utf-8")
                second = url_for("static", filename="app.css")
                missing = url_for("static", filename="nope.css")
                outside = url_for("static", filename="../secret.txt")
        self.assertRegex(first, r"^/static/app\.css\?v=[0-9a-f]{12}$")
        self.assertNotEqual(first, second)
        self.assertEqual(missing, "/static/nope.css")
        self.assertNotIn("?v=", outside)

    def test_an_explicit_version_is_kept(self):
        with support.web_app.app.test_request_context():
            url = url_for("static", filename="css/ui-pass9.css", v="manual")
        self.assertEqual(url, "/static/css/ui-pass9.css?v=manual")

    def test_a_versioned_url_still_serves_the_file(self):
        with support.web_app.app.test_request_context():
            url = url_for("static", filename="css/tokens.css")
        response = support.web_app.app.test_client().get(url)
        try:
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.get_data(), (STATIC / "css/tokens.css").read_bytes())
        finally:
            response.close()


class RenderedPageTest(support.IsolatedAppTestCase):
    def test_every_local_stylesheet_and_script_on_a_signed_in_page_is_versioned(self):
        client = support.web_app.app.test_client()
        sign_in_admin(client, self.isolation_root / "questions.db")
        html = client.get("/").get_data(as_text=True)
        assets = [url for url in re.findall(r'(?:href|src)="(/static/[^"]+)"', html)
                  if re.search(r"\.(?:css|js)(?:\?|$)", url)]
        self.assertTrue(assets)
        for asset in assets:
            self.assertRegex(asset, r"\?v=[0-9a-f]{12}$")
        # The signed-in account menu (with "Изход") is in the page.
        self.assertIn('class="mobile-profile-menu"', html)
        self.assertIn("Изход", html)


if __name__ == "__main__":
    unittest.main()
