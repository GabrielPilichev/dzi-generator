import unittest
from html.parser import HTMLParser

from tests.support import IsolatedAppTestCase, web_app

app = web_app.app


class _LinkParser(HTMLParser):
    def __init__(self, html: str):
        super().__init__()
        self.links = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        if tag == "link":
            self.links.append(dict(attrs))


class FaviconTest(IsolatedAppTestCase):
    def setUp(self):
        self.client = app.test_client()

    def test_favicon_route_serves_icon(self):
        response = self.client.get("/favicon.ico")

        try:
            self.assertEqual(response.status_code, 200)
            self.assertIn(
                response.mimetype,
                {"image/x-icon", "image/vnd.microsoft.icon", "application/octet-stream"},
            )
            self.assertGreater(len(response.data), 0)
        finally:
            response.close()

    def test_homepage_includes_brand_favicon_links(self):
        response = self.client.get("/")
        html = response.get_data(as_text=True)
        icon_links = [
            attrs
            for attrs in _LinkParser(html).links
            if "icon" in (attrs.get("rel") or "").split()
        ]

        self.assertTrue(any(attrs.get("href", "").endswith("/favicon.ico") for attrs in icon_links))
        self.assertTrue(
            any(attrs.get("type") == "image/png" and attrs.get("sizes") == "32x32" for attrs in icon_links)
        )
        for attrs in icon_links:
            with self.subTest(href=attrs.get("href")):
                asset_response = self.client.get(attrs["href"])
                try:
                    self.assertEqual(asset_response.status_code, 200)
                finally:
                    asset_response.close()


if __name__ == "__main__":
    unittest.main()
