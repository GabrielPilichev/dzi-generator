import os
import unittest

os.environ["DZI_ADMIN_PASSWORD"] = "admin-pass"

from web import app as web_app  # noqa: E402


class ErrorHandlerTest(unittest.TestCase):
    def test_internal_server_error_renders_safe_500_page(self):
        with web_app.app.test_request_context("/__test__/500"):
            html, status = web_app.internal_server_error(Exception("boom"))

        self.assertEqual(status, 500)
        self.assertIn("500", html)
        self.assertIn("Възникна вътрешна грешка", html)
        self.assertNotIn("boom", html)


if __name__ == "__main__":
    unittest.main()
