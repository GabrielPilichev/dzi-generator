"""Roadmap PR E2 — the practical-task routes live on their own blueprint.

The application route map itself is pinned in ``test_dzi_blueprint``; this file
pins who owns the five E2 views, their endpoint names, the dependency direction
of the new modules, the removal of the E1 runtime bridge, and the
authorization / request-integrity behavior of the moved routes.
"""

import ast
import io
import re
import subprocess
import sys
import unittest

from tests.support import ROOT, IsolatedAppTestCase, web_app
from web import access, request_integrity

E2_ROUTES = {
    "/dzi/practical/resource/<int:resource_id>/download": (
        "practical.dzi_practical_resource_download", {"GET"}, access.ADMIN, None),
    "/quiz/attempt/<int:attempt_id>/practical/<source_slug>": (
        "practical.quiz_practical_attempt", {"GET"}, access.ATTEMPT_OWNER, None),
    "/quiz/attempt/<int:attempt_id>/practical/<source_slug>/task/<int:exam_task_id>/upload": (
        "practical.quiz_practical_upload", {"POST"}, access.ATTEMPT_OWNER,
        request_integrity.SAME_ORIGIN),
    "/teacher/practical-submissions": (
        "practical.teacher_practical_submissions", {"GET"}, access.ASSIGNMENT_OWNER, None),
    "/teacher/practical/submission-file/<int:file_id>/download": (
        "practical.teacher_practical_submission_file_download", {"GET"}, access.ASSIGNMENT_OWNER, None),
}
OLD_ENDPOINTS = (
    "dzi_practical_resource_download", "quiz_practical_attempt", "quiz_practical_upload",
    "teacher_practical_submissions", "teacher_practical_submission_file_download",
)
CONTROLLER_MODULES = (
    "web/blueprints/dzi.py", "web/blueprints/practical.py", "web/blueprints/quiz.py",
    "web/blueprints/teacher.py",
)


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


class PracticalRouteOwnershipTest(unittest.TestCase):
    def test_every_e2_route_belongs_to_the_practical_blueprint(self):
        rules = {rule.rule: rule for rule in web_app.app.url_map.iter_rules()}
        for url, (endpoint, methods, policy, integrity) in E2_ROUTES.items():
            with self.subTest(url=url):
                rule = rules[url]
                self.assertEqual(rule.endpoint, endpoint)
                self.assertEqual(rule.methods - {"HEAD", "OPTIONS"}, methods)
                view = web_app.app.view_functions[endpoint]
                self.assertEqual(view.__module__, "web.blueprints.practical")
                self.assertEqual(access.policy_for_view(view), policy)
                self.assertEqual(request_integrity.integrity_policy_for_view(view), integrity)

    def test_the_blueprint_owns_exactly_the_e2_routes(self):
        owned = {rule.rule for rule in web_app.app.url_map.iter_rules()
                 if rule.endpoint.startswith("practical.")}
        self.assertEqual(owned, set(E2_ROUTES))

    def test_no_legacy_endpoint_alias_was_added(self):
        endpoints = {rule.endpoint for rule in web_app.app.url_map.iter_rules()}
        for legacy in OLD_ENDPOINTS:
            self.assertNotIn(legacy, endpoints)
            self.assertNotIn(legacy, web_app.app.view_functions)

    def test_later_roadmap_views_have_not_moved(self):
        for endpoint in ("auth.tester_home", "auth.admin_login"):
            with self.subTest(endpoint=endpoint):
                self.assertEqual(web_app.app.view_functions[endpoint].__module__, "web.blueprints.auth")

    def test_templates_reference_only_blueprint_qualified_endpoints(self):
        for template in (ROOT / "web" / "templates").glob("*.html"):
            text = template.read_text(encoding="utf-8")
            for old in OLD_ENDPOINTS:
                with self.subTest(template=template.name, endpoint=old):
                    self.assertIsNone(re.search(rf"url_for\(\s*['\"]{old}['\"]", text))


class PracticalDependencyDirectionTest(unittest.TestCase):
    def test_practical_modules_import_neither_web_app_nor_another_controller(self):
        for relative in ("web/blueprints/practical.py", "web/services/practical.py"):
            with self.subTest(module=relative):
                imported = imported_modules(relative)
                self.assertNotIn("web.app", imported)
                self.assertFalse({n for n in imported if n.startswith("web.blueprints")}, imported)
                self.assertFalse({n for n in imported if n.startswith("web.services.quiz")}, imported)

    def test_no_blueprint_imports_web_app_or_a_sibling_blueprint(self):
        for relative in CONTROLLER_MODULES:
            with self.subTest(module=relative):
                imported = imported_modules(relative)
                self.assertNotIn("web.app", imported)
                self.assertFalse({n for n in imported if n.startswith("web.blueprints")}, imported)

    def test_modules_have_no_function_local_import(self):
        for relative in CONTROLLER_MODULES + ("web/services/practical.py",):
            tree = ast.parse((ROOT / relative).read_text(encoding="utf-8"))
            for func in ast.walk(tree):
                if isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    for node in ast.walk(func):
                        self.assertNotIsInstance(node, (ast.Import, ast.ImportFrom), relative)

    def test_modules_import_in_a_fresh_interpreter_without_web_app(self):
        code = (
            "import sys, web.blueprints.practical, web.blueprints.dzi, web.services.practical\n"
            "print('web.app' in sys.modules)\n"
        )
        result = subprocess.run(
            [sys.executable, "-B", "-c", code], cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "False")

    def test_the_e1_runtime_bridge_is_gone(self):
        import web.blueprints.dzi as dzi_module

        self.assertFalse(hasattr(dzi_module, "configure_runtime"))
        self.assertFalse(hasattr(dzi_module, "_runtime"))
        source = (ROOT / "web/app.py").read_text(encoding="utf-8")
        self.assertNotIn("configure_runtime(practical_tasks", source)
        self.assertIn("web.services.practical", imported_modules("web/blueprints/dzi.py"))

    def test_services_hold_no_module_level_runtime_registry(self):
        tree = ast.parse((ROOT / "web/services/practical.py").read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                self.fail(f"module-level state at line {node.lineno}")


class PracticalRoutesBehaviorTest(IsolatedAppTestCase):
    SLUG = "may_2025_v2"
    SESSIONS = {
        "anonymous": {},
        "tester": {"tester_authenticated": True, "ui_profile": "tester"},
        "admin": {"admin_authenticated": True, "ui_profile": "admin"},
        "profile_only": {"ui_profile": "admin"},
    }

    def client_for(self, who, attempt_ids=()):
        client = web_app.app.test_client()
        with client.session_transaction() as state:
            state.update(self.SESSIONS[who])
            for attempt_id in attempt_ids:
                state[f"quiz_attempt_owner:{attempt_id}"] = True
        return client

    def test_admin_only_routes_deny_everyone_else(self):
        # The official resource download stays admin-only; the review routes
        # (Accounts PR8) are assignment_owner and deny to the account login.
        paths = (
            ("/dzi/practical/resource/1/download", "/admin/login"),
            ("/teacher/practical-submissions", "/login?"),
            ("/teacher/practical/submission-file/1/download", "/login?"),
        )
        for path, login in paths:
            for who in ("anonymous", "tester", "profile_only"):
                with self.subTest(path=path, who=who):
                    response = self.client_for(who).get(path)
                    self.assertEqual(response.status_code, 302)
                    self.assertTrue(response.headers["Location"].startswith(login))

    def test_admin_reaches_the_review_page(self):
        self.assertEqual(self.client_for("admin").get("/teacher/practical-submissions").status_code, 200)

    def test_student_page_needs_the_attempt_owner_session(self):
        path = f"/quiz/attempt/987654/practical/{self.SLUG}"
        self.assertEqual(self.client_for("admin").get(path).status_code, 404)
        self.assertEqual(self.client_for("anonymous").get(path).status_code, 404)

    def test_upload_is_same_origin_protected_before_authorization(self):
        url = f"/quiz/attempt/1/practical/{self.SLUG}/task/1/upload"
        for who in ("anonymous", "admin"):
            with self.subTest(who=who):
                response = self.client_for(who, attempt_ids=(1,)).post(
                    url, data={"files": (io.BytesIO(b"x"), "answer.xlsx")}, headers={"Origin": "http://evil.example"},
                    content_type="multipart/form-data")
                self.assertEqual(response.status_code, 403)

    def test_same_origin_upload_from_a_non_owner_is_not_found(self):
        url = f"/quiz/attempt/1/practical/{self.SLUG}/task/1/upload"
        response = self.client_for("admin").post(
            url, data={"files": (io.BytesIO(b"x"), "answer.xlsx")},
            headers={"Origin": "http://localhost"}, content_type="multipart/form-data")
        self.assertEqual(response.status_code, 404)

    def test_missing_download_keeps_the_dzi_nav_highlight(self):
        response = self.client_for("admin").get("/dzi/practical/resource/999999/download")
        self.assertEqual(response.status_code, 404)
        self.assertIn('aria-current="page">ДЗИ</a>', response.get_data(as_text=True))

    def test_resource_roots_are_application_configuration(self):
        config = web_app.app.config
        for key in ("PROJECT_ROOT", "PRACTICAL_RESOURCE_ROOTS", "PRACTICAL_REFERENCE_ROOT"):
            self.assertIn(key, config)
        self.assertEqual(config["PRACTICAL_RESOURCE_ROOTS"][-1], config["PROJECT_ROOT"] / "data" / "assets")
        for legacy in ("PRACTICAL_RESOURCE_ROOTS", "PRACTICAL_REFERENCE_ROOT"):
            self.assertFalse(hasattr(web_app, legacy), legacy)


if __name__ == "__main__":
    unittest.main()
