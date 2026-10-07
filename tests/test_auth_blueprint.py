"""Roadmap PR E5 — auth/tester/profile routes live on the auth blueprint, the
legacy quiz endpoint aliases are gone, and ``web.app`` is a composition root.

Pins ownership, endpoint names, the unchanged public URLs/methods/policies,
login/logout/profile behavior (session keys, redirects, same-origin integrity),
that ``ui_profile`` never authorizes, the removed aliases and wrappers, the
dependency direction between controllers, and fresh-interpreter startup.
"""

import ast
import os
import subprocess
import sys
import unittest
from unittest.mock import patch

from tests.support import ROOT, IsolatedAppTestCase, web_app
from web import access, request_integrity

E5_ROUTES = {
    "/tester": ("auth.tester_home", {"GET"}, access.PUBLIC),
    "/tester/login": ("auth.tester_login", {"GET", "POST"}, access.PUBLIC),
    "/tester/logout": ("auth.tester_logout", {"GET"}, access.PUBLIC),
    "/admin/login": ("auth.admin_login", {"GET", "POST"}, access.PUBLIC),
    "/admin/logout": ("auth.admin_logout", {"GET"}, access.ADMIN),
    "/profile/<profile>": ("auth.switch_profile", {"GET"}, access.PROFILE_SWITCH),
}
REMOVED_ENDPOINTS = (
    "tester_home", "tester_login", "tester_logout", "admin_login", "admin_logout",
    "switch_profile", "quiz_start", "quiz_attempt", "quiz_result",
)
CONTROLLER_MODULES = (
    "web/blueprints/auth.py", "web/blueprints/browsing.py", "web/blueprints/dzi.py",
    "web/blueprints/practical.py", "web/blueprints/quiz.py", "web/blueprints/teacher.py",
)
CANONICAL_QUIZ_URLS = {
    "/quiz/<int:assignment_id>": ("quiz.quiz_start", {"GET", "POST"}),
    "/quiz/attempt/<int:attempt_id>": ("quiz.quiz_attempt", {"GET", "POST"}),
    "/quiz/attempt/<int:attempt_id>/result": ("quiz.quiz_result", {"GET"}),
}
PASSWORDS = {"DZI_TESTER_PASSWORD": "tester-pw", "DZI_ADMIN_PASSWORD": "admin-pw"}


def imported_modules(relative):
    tree = ast.parse((ROOT / relative).read_text(encoding="utf-8"))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.add(node.module or "")
            names.update(f"{node.module}.{alias.name}" for alias in node.names)
    return names


def rules_by_url():
    grouped = {}
    for rule in web_app.app.url_map.iter_rules():
        grouped.setdefault(rule.rule, []).append(rule)
    return grouped


class AuthOwnershipTest(unittest.TestCase):
    def test_e5_routes_keep_url_methods_and_policy_under_auth_endpoints(self):
        grouped = rules_by_url()
        for url, (endpoint, methods, policy) in E5_ROUTES.items():
            with self.subTest(url=url):
                self.assertEqual([r.endpoint for r in grouped[url]], [endpoint])
                self.assertEqual(grouped[url][0].methods - {"HEAD", "OPTIONS"}, methods)
                view = web_app.app.view_functions[endpoint]
                self.assertEqual(view.__module__, "web.blueprints.auth")
                self.assertEqual(access.policy_for_view(view), policy)

    def test_blueprint_owns_exactly_the_e5_routes_without_a_prefix(self):
        owned = {r.rule for r in web_app.app.url_map.iter_rules() if r.endpoint.startswith("auth.")}
        self.assertEqual(owned, set(E5_ROUTES))

    def test_unsafe_login_posts_declare_same_origin_and_logouts_are_get_only(self):
        for endpoint in ("auth.tester_login", "auth.admin_login"):
            view = web_app.app.view_functions[endpoint]
            self.assertEqual(request_integrity.integrity_policy_for_view(view), request_integrity.SAME_ORIGIN)

    def test_old_endpoint_names_and_quiz_aliases_are_gone(self):
        endpoints = {r.endpoint for r in web_app.app.url_map.iter_rules()}
        for legacy in REMOVED_ENDPOINTS:
            with self.subTest(endpoint=legacy):
                self.assertNotIn(legacy, endpoints)
                self.assertNotIn(legacy, web_app.app.view_functions)

    def test_canonical_quiz_urls_have_exactly_one_rule_each(self):
        grouped = rules_by_url()
        for url, (endpoint, methods) in CANONICAL_QUIZ_URLS.items():
            with self.subTest(url=url):
                self.assertEqual([r.endpoint for r in grouped[url]], [endpoint])
                self.assertEqual(grouped[url][0].methods - {"HEAD", "OPTIONS"}, methods)

    def test_no_url_for_references_a_removed_endpoint(self):
        import re
        names = "|".join(REMOVED_ENDPOINTS)
        pattern = re.compile(rf"""url_for\(\s*['"](?:{names})['"]""")
        sources = list((ROOT / "web" / "templates").glob("*.html"))
        sources += (ROOT / "web").glob("*.py")
        sources += (ROOT / "web" / "blueprints").glob("*.py")
        sources += (ROOT / "web" / "services").glob("*.py")
        for path in sources:
            with self.subTest(path=path.name):
                self.assertIsNone(pattern.search(path.read_text(encoding="utf-8")))

    def test_app_module_no_longer_holds_auth_helpers_or_compat_constants(self):
        for name in ("tester_login", "admin_login", "switch_profile", "tester_home",
                     "password_matches", "is_admin_authenticated",
                     "dzi_find_exam", "fetch_dzi_tasks", "PRACTICAL_REFERENCE_ROOT",
                     "PRACTICAL_RESOURCE_ROOTS", "register_legacy_quiz_endpoint_aliases"):
            self.assertFalse(hasattr(web_app, name), name)

    def test_every_blueprint_is_registered_exactly_once(self):
        self.assertEqual(
            sorted(web_app.app.blueprints),
            ["account", "auth", "browsing", "dzi", "practical", "quiz", "teacher"],
        )


class AuthDependencyDirectionTest(unittest.TestCase):
    def test_no_controller_imports_web_app_or_a_sibling_controller(self):
        for relative in CONTROLLER_MODULES:
            with self.subTest(module=relative):
                imported = imported_modules(relative)
                self.assertNotIn("web.app", imported)
                self.assertFalse(
                    {n for n in imported if n.startswith("web.blueprints")}, imported)

    def test_no_controller_hides_an_import_inside_a_function(self):
        for relative in CONTROLLER_MODULES:
            tree = ast.parse((ROOT / relative).read_text(encoding="utf-8"))
            for func in ast.walk(tree):
                if isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    for node in ast.walk(func):
                        with self.subTest(module=relative, function=func.name):
                            self.assertNotIsInstance(node, (ast.Import, ast.ImportFrom))

    def test_auth_blueprint_uses_no_database(self):
        source = (ROOT / "web/blueprints/auth.py").read_text(encoding="utf-8")
        for needle in ("sqlite3", "get_request_db", "get_db"):
            self.assertNotIn(needle, source)

    def test_auth_blueprint_imports_in_a_fresh_interpreter_without_web_app(self):
        code = "import sys, web.blueprints.auth\nprint('web.app' in sys.modules)\n"
        result = subprocess.run(
            [sys.executable, "-B", "-c", code], cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "False")

    def test_web_app_imports_once_with_a_complete_route_map_in_a_fresh_interpreter(self):
        code = (
            "import warnings; warnings.simplefilter('ignore')\n"
            "from web.app import app\n"
            "print(len(app.blueprints), len(list(app.url_map.iter_rules())))\n"
        )
        env = {**os.environ, "DZI_SECRET_KEY": "x" * 40}
        result = subprocess.run(
            [sys.executable, "-B", "-c", code], cwd=ROOT, capture_output=True, text=True, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(result.stdout.strip().startswith("7 "), result.stdout)


class AuthBehaviorTest(IsolatedAppTestCase):
    def setUp(self):
        super().setUp()
        patcher = patch.dict(os.environ, PASSWORDS)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client = web_app.app.test_client()

    def session(self):
        with self.client.session_transaction() as sess:
            return dict(sess)

    def test_login_pages_render_for_anonymous_visitors(self):
        for url in ("/tester", "/tester/login", "/admin/login"):
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 200)

    def test_tester_login_success_sets_the_same_session_keys_and_redirects(self):
        response = self.client.post(
            "/tester/login?next=/teacher/new", data={"password": "tester-pw"})
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith("/teacher/new"))
        self.assertEqual(self.session(), {"tester_authenticated": True, "ui_profile": "tester"})

    def test_tester_login_default_redirect_is_the_tester_page(self):
        response = self.client.post("/tester/login", data={"password": "tester-pw"})
        self.assertTrue(response.headers["Location"].endswith("/tester"))

    def test_admin_login_success_sets_the_same_session_keys_and_redirects(self):
        response = self.client.post("/admin/login", data={"password": "admin-pw"})
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith("/teacher"))
        self.assertEqual(self.session(), {"admin_authenticated": True, "ui_profile": "admin"})

    def test_failed_login_renders_the_error_and_leaves_the_session_empty(self):
        for url, error in (("/tester/login", "Грешна парола."), ("/admin/login", "Грешна парола.")):
            with self.subTest(url=url):
                response = self.client.post(url, data={"password": "nope"})
                self.assertEqual(response.status_code, 200)
                self.assertIn(error, response.get_data(as_text=True))
                self.assertNotIn("tester_authenticated", self.session())
                self.assertNotIn("admin_authenticated", self.session())

    def test_login_with_no_configured_password_fails_closed(self):
        with patch.dict(os.environ, {"DZI_ADMIN_PASSWORD": "", "DZI_TESTER_PASSWORD": ""}):
            for url in ("/tester/login", "/admin/login"):
                response = self.client.post(url, data={"password": ""})
                self.assertEqual(response.status_code, 200)
                self.assertIn("не е зададена", response.get_data(as_text=True))
        self.assertEqual(self.session().get("admin_authenticated"), None)

    def test_unsafe_next_targets_fall_back_to_the_default(self):
        for bad in ("https://evil.example/x", "//evil.example/x", "javascript:alert(1)", "relative"):
            with self.subTest(next=bad):
                client = web_app.app.test_client()
                response = client.post(
                    "/admin/login", data={"password": "admin-pw", "next": bad})
                location = response.headers["Location"]
                self.assertTrue(location.endswith("/teacher"), location)

    def test_cross_origin_login_posts_are_blocked_before_any_session_write(self):
        for url, password in (("/tester/login", "tester-pw"), ("/admin/login", "admin-pw")):
            with self.subTest(url=url):
                response = self.client.post(
                    url, data={"password": password}, headers={"Origin": "http://evil.example"})
                self.assertEqual(response.status_code, 403)
        self.assertEqual(self.session(), {})
        ok = self.client.post(
            "/tester/login", data={"password": "tester-pw"}, headers={"Origin": "http://localhost"})
        self.assertEqual(ok.status_code, 302)

    def test_tester_logout_keeps_an_admin_profile(self):
        self.client.post("/tester/login", data={"password": "tester-pw"})
        self.client.post("/admin/login", data={"password": "admin-pw"})
        response = self.client.get("/tester/logout")
        self.assertTrue(response.headers["Location"].endswith("/tester"))
        self.assertEqual(self.session(), {"admin_authenticated": True, "ui_profile": "admin"})

    def test_tester_logout_resets_the_profile_for_a_tester(self):
        self.client.post("/tester/login", data={"password": "tester-pw"})
        self.client.get("/tester/logout")
        self.assertEqual(self.session(), {"ui_profile": "tester"})

    def test_admin_logout_requires_admin_and_clears_only_admin_state(self):
        anonymous = self.client.get("/admin/logout")
        self.assertEqual(anonymous.status_code, 302)
        self.assertIn("/admin/login", anonymous.headers["Location"])
        self.client.post("/tester/login", data={"password": "tester-pw"})
        self.client.post("/admin/login", data={"password": "admin-pw"})
        response = self.client.get("/admin/logout")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.session(), {"tester_authenticated": True, "ui_profile": "tester"})

    def test_denied_requests_redirect_to_the_auth_login_endpoints(self):
        teacher = self.client.get("/teacher/new")
        self.assertIn("/tester/login?next=", teacher.headers["Location"])
        admin = self.client.get("/teacher")
        self.assertIn("/admin/login?next=", admin.headers["Location"])

    def test_ui_profile_without_authentication_grants_nothing(self):
        with self.client.session_transaction() as sess:
            sess["ui_profile"] = "admin"
        for url in ("/teacher", "/dzi", "/teacher/new", "/teacher/assignments"):
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 302)
        # An unauthenticated admin profile is also downgraded on the next request.
        self.client.get("/tester")
        self.assertNotIn("ui_profile", self.session())

    def test_switch_profile_is_confined_and_never_authorizes(self):
        tester = self.client.get("/profile/tester?next=/grade/9")
        self.assertTrue(tester.headers["Location"].endswith("/grade/9"))
        self.assertEqual(self.session(), {"ui_profile": "tester"})
        external = self.client.get("/profile/tester?next=https://evil.example/")
        self.assertTrue(external.headers["Location"].endswith("/tester"))
        protocol_relative = self.client.get("/profile/tester?next=//evil.example/")
        self.assertTrue(protocol_relative.headers["Location"].endswith("/tester"))
        # Switching to admin without an admin session is denied and sent to admin login.
        denied = self.client.get("/profile/admin")
        self.assertIn("/admin/login", denied.headers["Location"])
        self.assertNotEqual(self.session().get("ui_profile"), "admin")
        self.assertEqual(self.client.get("/profile/unknown").status_code, 404)
        self.assertEqual(self.client.get("/teacher").status_code, 302)

    def test_admin_may_switch_to_the_admin_profile(self):
        self.client.post("/admin/login", data={"password": "admin-pw"})
        self.client.get("/profile/tester")
        self.assertEqual(self.session()["ui_profile"], "tester")
        response = self.client.get("/profile/admin")
        self.assertTrue(response.headers["Location"].endswith("/"))
        self.assertEqual(self.session()["ui_profile"], "admin")

    def test_anonymous_attempt_pages_stay_owner_protected_on_canonical_routes(self):
        for url in ("/quiz/attempt/999999", "/quiz/attempt/999999/result"):
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 404)

    def test_error_handlers_still_render_their_templates(self):
        response = self.client.get("/definitely-not-a-route")
        self.assertEqual(response.status_code, 404)
        self.assertIn("404", response.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
