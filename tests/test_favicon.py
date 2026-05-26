import unittest

from web.app import app


class FaviconTest(unittest.TestCase):
    def setUp(self):
        self.client = app.test_client()

    def test_favicon_route_serves_icon(self):
        response = self.client.get("/favicon.ico")

        self.assertEqual(response.status_code, 200)
        self.assertIn(
            response.mimetype,
            {"image/x-icon", "image/vnd.microsoft.icon", "application/octet-stream"},
        )
        self.assertGreater(len(response.data), 0)

    def test_homepage_includes_brand_favicon_links(self):
        response = self.client.get("/")
        html = response.get_data(as_text=True)

        self.assertIn("img/brand/favicon.ico", html)
        self.assertIn("img/brand/favicon-32x32.png", html)


if __name__ == "__main__":
    unittest.main()
