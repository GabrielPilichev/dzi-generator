"""The shared-password auth blueprint (roadmap PR E5) is gone (Accounts PR10).

``/tester``, ``/tester/login``, ``/tester/logout``, ``/admin/login``,
``/admin/logout`` and ``/profile/<profile>`` were the shared tester/admin password
bridge and its cosmetic profile switch. This module pins their physical removal
(module, blueprint, endpoints, rules, templates, helpers), that the URLs answer
like any unknown URL, the removed quiz aliases and wrappers, the dependency
direction between the remaining controllers, and fresh-interpreter startup.
"""

import ast
import os
import subprocess
import sys
import unittest
from unittest.mock import patch

from tests.auth_helpers import HISTORICAL_SHARED_LOGIN_SESSION, client_as
from tests.support import ROOT, IsolatedAppTestCase, web_app

# The rules the removed blueprint owned, and a concrete URL for each.
REMOVED_RULES = {
    "/tester": "/tester",
    "/tester/login": "/tester/login",
    "/tester/logout": "/tester/logout",
    "/admin/login": "/admin/login",
    "/admin/logout": "/admin/logout",
    "/profile/<profile>": "/profile/admin",
}
REMOVED_AUTH_VIEWS = (
    "tester_home", "tester_login", "tester_logout", "admin_login", "admin_logout", "switch_profile",
)
REMOVED_ENDPOINTS = (*REMOVED_AUTH_VIEWS, "quiz_start", "quiz_attempt", "quiz_result")
REMOVED_TEMPLATES = ("tester_home.html", "tester_login.html", "admin_login.html")
CONTROLLER_MODULES = (
    "web/blueprints/account.py", "web/blueprints/browsing.py", "web/blueprints/dzi.py",
    "web/blueprints/oidc.py", "web/blueprints/practical.py", "web/blueprints/quiz.py",
    "web/blueprints/teacher.py",
)
CANONICAL_QUIZ_URLS = {
    "/quiz/s/<public_token>": ("quiz.quiz_start_public", {"GET", "POST"}),
    "/quiz/attempt/<int:attempt_id>": ("quiz.quiz_attempt", {"GET", "POST"}),
    "/quiz/attempt/<int:attempt_id>/result": ("quiz.quiz_result", {"GET"}),
}
PASSWORDS = {"DZI_TESTER_PASSWORD": "tester-pw", "DZI_ADMIN_PASSWORD": "admin-pw",
             "DZI_LEGACY_SHARED_LOGIN": "1"}


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


class AuthRemovalTest(unittest.TestCase):
    def test_the_removed_rules_are_not_in_the_route_map(self):
        grouped = rules_by_url()
        for url in REMOVED_RULES:
            with self.subTest(url=url):
                self.assertNotIn(url, grouped)
        self.assertFalse([url for url in grouped if url.startswith(("/tester", "/profile"))])
        self.assertEqual(sorted(url for url in grouped if "login" in url or "logout" in url),
                         ["/login", "/login/totp", "/logout"])

    def test_the_blueprint_module_and_its_templates_do_not_exist(self):
        self.assertFalse((ROOT / "web/blueprints/auth.py").exists())
        self.assertNotIn("web.blueprints.auth", sys.modules)
        self.assertNotIn("auth", web_app.app.blueprints)
        for template in REMOVED_TEMPLATES:
            with self.subTest(template=template):
                self.assertFalse((ROOT / "web/templates" / template).exists())

    def test_old_endpoint_names_and_quiz_aliases_are_gone(self):
        endpoints = {r.endpoint for r in web_app.app.url_map.iter_rules()}
        for legacy in (*REMOVED_ENDPOINTS, *(f"auth.{name}" for name in REMOVED_AUTH_VIEWS)):
            with self.subTest(endpoint=legacy):
                self.assertNotIn(legacy, endpoints)
                self.assertNotIn(legacy, web_app.app.view_functions)
        self.assertFalse({endpoint for endpoint in web_app.app.view_functions if endpoint.startswith("auth.")})

    def test_canonical_quiz_urls_have_exactly_one_rule_each(self):
        grouped = rules_by_url()
        for url, (endpoint, methods) in CANONICAL_QUIZ_URLS.items():
            with self.subTest(url=url):
                self.assertEqual([r.endpoint for r in grouped[url]], [endpoint])
                self.assertEqual(grouped[url][0].methods - {"HEAD", "OPTIONS"}, methods)

    def test_no_url_for_references_a_removed_endpoint(self):
        import re
        names = "|".join((*REMOVED_ENDPOINTS, r"auth\.[a-z_]+"))
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
                     "password_matches", "is_admin_authenticated", "is_tester_authenticated",
                     "auth_bp", "VALID_UI_PROFILES", "load_ui_profile", "inject_ui_profile",
                     "build_legacy_shared_login", "legacy_shared_login_enabled",
                     "LEGACY_SHARED_LOGIN_ENV", "LEGACY_SHARED_LOGIN_CONFIG", "LEGACY_AUTH_SESSION_KEYS",
                     "dzi_find_exam", "fetch_dzi_tasks", "PRACTICAL_REFERENCE_ROOT",
                     "PRACTICAL_RESOURCE_ROOTS", "register_legacy_quiz_endpoint_aliases"):
            self.assertFalse(hasattr(web_app, name), name)

    def test_every_blueprint_is_registered_exactly_once(self):
        self.assertEqual(
            sorted(web_app.app.blueprints),
            ["account", "browsing", "dzi", "oidc", "practical", "quiz", "registration", "teacher"],
        )
        # Google sign-in is the blueprint "oidc". It is not the removed "auth", and
        # none of its paths says "login" or "logout" (pinned above).
        self.assertNotIn("auth", web_app.app.blueprints)


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

    def test_no_runtime_module_or_template_names_the_removed_machinery(self):
        # The environment variables, session keys and endpoints of the bridge. A test
        # may inject the keys; application, deployment and template code may not name them.
        needles = (
            "DZI_LEGACY_SHARED_LOGIN", "DZI_ADMIN_PASSWORD", "DZI_TESTER_PASSWORD",
            "LEGACY_SHARED_LOGIN", "tester_authenticated", "admin_authenticated", "ui_profile",
            "/tester/login", "/admin/login", "/tester/logout", "/admin/logout", "/profile/",
            "lp_admin_login", "lp_tester_login", "blueprints.auth", "auth_bp",
        )
        sources = [*(ROOT / "web").rglob("*.py"), *(ROOT / "web" / "templates").glob("*.html"),
                   ROOT / "src/run_tunnel_dev.py", ROOT / "src/manage_accounts.py",
                   *(path for path in (ROOT / "deploy").rglob("*") if path.is_file())]
        self.assertGreater(len(sources), 40)
        for path in sources:
            text = path.read_text(encoding="utf-8")
            for needle in needles:
                with self.subTest(path=str(path.relative_to(ROOT)), needle=needle):
                    self.assertNotIn(needle, text)

    def test_the_removed_blueprint_cannot_be_imported(self):
        code = "import web.blueprints.auth\n"
        result = subprocess.run(
            [sys.executable, "-B", "-c", code], cwd=ROOT, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("ModuleNotFoundError", result.stderr)

    def test_web_app_imports_once_with_a_complete_route_map_in_a_fresh_interpreter(self):
        code = (
            "import warnings; warnings.simplefilter('ignore')\n"
            "from web.app import app\n"
            "print(len(app.blueprints), len(list(app.url_map.iter_rules())))\n"
        )
        # Neither the shared passwords nor a bridge flag is needed, and setting them
        # (second run) changes nothing about what the application serves.
        base = {key: value for key, value in os.environ.items() if key not in PASSWORDS}
        outputs = []
        for extra in ({}, PASSWORDS):
            env = {**base, "DZI_SECRET_KEY": "x" * 40, "DZI_DB": ":memory:", **extra}
            result = subprocess.run(
                [sys.executable, "-B", "-c", code], cwd=ROOT, capture_output=True, text=True, env=env)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(result.stdout.strip().startswith("8 "), result.stdout)
            outputs.append(result.stdout)
        self.assertEqual(outputs[0], outputs[1])


class RemovedRoutesBehaviorTest(IsolatedAppTestCase):
    """The removed URLs answer exactly like a URL that never existed."""

    def setUp(self):
        super().setUp()
        # Everything that once enabled or fed the bridge is set: it must change nothing.
        patcher = patch.dict(os.environ, PASSWORDS)
        patcher.start()
        self.addCleanup(patcher.stop)
        config = patch.dict(web_app.app.config, {"LEGACY_SHARED_LOGIN": True})
        config.start()
        self.addCleanup(config.stop)
        self.client = web_app.app.test_client()

    def session(self, client=None):
        with (client or self.client).session_transaction() as sess:
            return dict(sess)

    def clients(self):
        db_path = web_app.app.config["DB_PATH"]
        return {who: client_as(web_app.app, db_path, who)
                for who in ("anonymous", "historical", "student", "teacher", "admin")}

    def test_get_is_not_found_for_every_kind_of_session(self):
        for who, client in self.clients().items():
            before = self.session(client)
            for url in REMOVED_RULES.values():
                with self.subTest(who=who, url=url):
                    response = client.get(url, query_string={"next": "/teacher"})
                    self.assertEqual(response.status_code, 404)
                    self.assertNotIn("Location", response.headers)
                    self.assertNotIn('type="password"', response.get_data(as_text=True))
            self.assertEqual(self.session(client), before)

    def test_post_is_not_found_and_grants_nothing(self):
        same_origin = {"Origin": "http://localhost"}
        for url, password in (("/tester/login", "tester-pw"), ("/admin/login", "admin-pw")):
            for submitted in (password, "wrong", ""):
                with self.subTest(url=url, submitted=submitted):
                    client = web_app.app.test_client()
                    response = client.post(
                        url, data={"password": submitted, "next": "/teacher"}, headers=same_origin)
                    self.assertEqual(response.status_code, 404)
                    self.assertEqual(self.session(client), {})
                    for protected in ("/teacher", "/teacher/new", "/teacher/assignments", "/dzi"):
                        denied = client.get(protected)
                        self.assertEqual((denied.status_code, denied.headers["Location"]),
                                         (302, f"/login?next={protected}"))

    def test_removed_urls_match_an_unknown_url_byte_for_byte(self):
        unknown = self.client.get("/no-such-page").get_data(as_text=True)
        for url in REMOVED_RULES.values():
            with self.subTest(url=url):
                body = self.client.get(url).get_data(as_text=True)
                self.assertEqual(body.replace(url, "/no-such-page"), unknown)

    def test_cross_origin_post_is_refused_like_one_to_an_unknown_url(self):
        for url in ("/tester/login", "/admin/login", "/no-such-page"):
            with self.subTest(url=url):
                response = self.client.post(
                    url, data={"password": "admin-pw"}, headers={"Origin": "http://evil.example"})
                self.assertEqual(response.status_code, 403)
        self.assertEqual(self.session(), {})

    def test_removed_get_logouts_do_not_sign_an_account_out(self):
        client = self.clients()["admin"]
        for url in ("/tester/logout", "/admin/logout"):
            self.assertEqual(client.get(url).status_code, 404)
        self.assertEqual(set(self.session(client)), {"uid", "sk", "iat"})
        self.assertEqual(client.get("/teacher").status_code, 200)
        # The canonical sign-out is POST /logout only.
        self.assertEqual(client.get("/logout").status_code, 405)
        self.assertEqual(client.post("/logout", headers={"Origin": "http://evil.example"}).status_code, 403)
        self.assertEqual(set(self.session(client)), {"uid", "sk", "iat"})
        self.assertEqual(client.post("/logout", headers={"Origin": "http://localhost"}).status_code, 302)
        self.assertEqual(self.session(client), {})

    def test_historical_session_keys_grant_nothing(self):
        with self.client.session_transaction() as sess:
            sess.update(HISTORICAL_SHARED_LOGIN_SESSION)
        for url in ("/teacher", "/dzi", "/teacher/new", "/teacher/assignments",
                    "/teacher/practical-submissions", "/admin/open-question-candidates"):
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual((response.status_code, response.headers["Location"]),
                                 (302, f"/login?next={url}"))
        # Never read, so never rewritten either.
        self.assertEqual(self.session(), HISTORICAL_SHARED_LOGIN_SESSION)

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
