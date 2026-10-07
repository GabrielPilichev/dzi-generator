"""Roadmap PR E3 — the teacher routes live on their own blueprint.

The application route map (rule, methods, policy, integrity) is pinned in
``test_dzi_blueprint``; this file pins who owns the nine E3 views, their
endpoint names, the dependency direction of the new modules, and the behavior
of the moved routes that the route map cannot show.
"""

import ast
import re
import subprocess
import sys
import unittest

from tests.auth_helpers import sign_in_admin, sign_in_student, sign_in_teacher
from tests.support import ROOT, IsolatedAppTestCase, web_app
from web import access, request_integrity

E3_ROUTES = {
    "/teacher": ("teacher.teacher_dashboard", {"GET"}, access.ADMIN, None),
    "/teacher/assignments": ("teacher.teacher_assignments", {"GET"}, access.ASSIGNMENT_OWNER, None),
    "/teacher/new": (
        "teacher.teacher_new", {"GET", "POST"}, access.TEACHER, request_integrity.SAME_ORIGIN),
    "/teacher/dzi-training": (
        "teacher.teacher_dzi_training", {"GET", "POST"}, access.ADMIN,
        request_integrity.SAME_ORIGIN),
    "/teacher/assignment/<int:assignment_id>": (
        "teacher.teacher_assignment", {"GET"}, access.TEACHER, None),
    "/teacher/assignment/<int:assignment_id>/edit": (
        "teacher.teacher_assignment_edit", {"POST"}, access.ASSIGNMENT_OWNER,
        request_integrity.SAME_ORIGIN),
    "/teacher/assignment/<int:assignment_id>/duplicate": (
        "teacher.teacher_assignment_duplicate", {"POST"}, access.ASSIGNMENT_OWNER,
        request_integrity.SAME_ORIGIN),
    "/teacher/assignment/<int:assignment_id>/results": (
        "teacher.teacher_assignment_results", {"GET", "POST"}, access.ASSIGNMENT_OWNER,
        request_integrity.SAME_ORIGIN),
    "/teacher/assignment/<int:assignment_id>/results.csv": (
        "teacher.teacher_assignment_results_export", {"GET"}, access.ASSIGNMENT_OWNER, None),
}
OLD_ENDPOINTS = tuple(endpoint.split(".", 1)[1] for endpoint, *_ in E3_ROUTES.values())
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


class TeacherRouteOwnershipTest(unittest.TestCase):
    def test_every_e3_route_belongs_to_the_teacher_blueprint(self):
        rules = {rule.rule: rule for rule in web_app.app.url_map.iter_rules()}
        for url, (endpoint, methods, policy, integrity) in E3_ROUTES.items():
            with self.subTest(url=url):
                rule = rules[url]
                self.assertEqual(rule.endpoint, endpoint)
                self.assertEqual(rule.methods - {"HEAD", "OPTIONS"}, methods)
                view = web_app.app.view_functions[endpoint]
                self.assertEqual(view.__module__, "web.blueprints.teacher")
                self.assertEqual(access.policy_for_view(view), policy)
                self.assertEqual(request_integrity.integrity_policy_for_view(view), integrity)

    def test_the_blueprint_owns_exactly_the_e3_routes(self):
        owned = {rule.rule for rule in web_app.app.url_map.iter_rules()
                 if rule.endpoint.startswith("teacher.")}
        self.assertEqual(owned, set(E3_ROUTES))

    def test_old_endpoint_names_are_gone_and_not_aliased(self):
        endpoints = {rule.endpoint for rule in web_app.app.url_map.iter_rules()}
        for legacy in OLD_ENDPOINTS:
            self.assertNotIn(legacy, endpoints)
            self.assertNotIn(legacy, web_app.app.view_functions)

    def test_no_reference_uses_a_bare_teacher_endpoint(self):
        names = "|".join(OLD_ENDPOINTS)
        pattern = re.compile(rf"""url_for\(\s*['"](?:{names})['"]""")
        sources = list((ROOT / "web" / "templates").glob("*.html")) + [ROOT / "web/app.py"]
        sources += (ROOT / "web" / "blueprints").glob("*.py")
        for path in sources:
            with self.subTest(path=path.name):
                self.assertIsNone(pattern.search(path.read_text(encoding="utf-8")))

    def test_e5_views_live_in_the_auth_blueprint(self):
        for endpoint in ("auth.tester_home", "auth.admin_login"):
            with self.subTest(endpoint=endpoint):
                self.assertEqual(web_app.app.view_functions[endpoint].__module__, "web.blueprints.auth")

    def test_moved_helpers_left_the_application_module(self):
        for name in ("fetch_dzi_pool_health", "fetch_practical_assignment_options",
                     "teacher_new", "quiz_duplicate_title"):
            self.assertFalse(hasattr(web_app, name), name)


class TeacherDependencyDirectionTest(unittest.TestCase):
    def test_teacher_blueprint_imports_neither_web_app_nor_another_controller(self):
        imported = imported_modules("web/blueprints/teacher.py")
        self.assertNotIn("web.app", imported)
        self.assertFalse({n for n in imported if n.startswith("web.blueprints")}, imported)

    def test_new_services_never_import_web_app_or_a_controller(self):
        for relative in ("web/services/subject_catalog.py", "web/request_db.py",
                         "web/services/practical.py", "web/services/quiz.py"):
            with self.subTest(module=relative):
                imported = imported_modules(relative)
                self.assertNotIn("web.app", imported)
                self.assertFalse({n for n in imported if n.startswith("web.blueprints")}, imported)

    def test_teacher_blueprint_imports_in_a_fresh_interpreter_without_web_app(self):
        code = (
            "import sys, web.blueprints.teacher, web.services.subject_catalog\n"
            "print('web.app' in sys.modules)\n"
        )
        result = subprocess.run(
            [sys.executable, "-B", "-c", code], cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "False")

    def test_teacher_views_do_not_read_the_session_directly(self):
        tree = ast.parse((ROOT / "web/blueprints/teacher.py").read_text(encoding="utf-8"))
        names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        self.assertNotIn("session", names)


class TeacherRoutesBehaviorTest(IsolatedAppTestCase):
    SESSIONS = {
        "anonymous": {},
        "tester": {"tester_authenticated": True, "ui_profile": "tester"},
        "admin": {"admin_authenticated": True, "ui_profile": "admin"},
        "profile_only": {"ui_profile": "admin"},
    }

    def client_for(self, who):
        client = web_app.app.test_client()
        with client.session_transaction() as state:
            state.update(self.SESSIONS[who])
        return client

    def test_account_roles_reach_the_same_pages_as_the_legacy_sessions(self):
        db_path = web_app.app.config["DB_PATH"]
        for sign_in, new_status, list_status, admin_status in (
            (sign_in_student, 302, 302, 302), (sign_in_teacher, 200, 200, 302),
            (sign_in_admin, 200, 200, 200),
        ):
            client = web_app.app.test_client()
            sign_in(client, db_path)
            with self.subTest(sign_in=sign_in.__name__):
                self.assertEqual(client.get("/teacher/new").status_code, new_status)
                # Accounts PR7: a teacher account lists its own assignments.
                self.assertEqual(client.get("/teacher/assignments").status_code, list_status)
                self.assertEqual(client.get("/teacher").status_code, admin_status)

    def test_teacher_new_is_open_to_tester_and_admin_only(self):
        for who, expected in (("anonymous", 302), ("profile_only", 302),
                              ("tester", 200), ("admin", 200)):
            with self.subTest(who=who):
                self.assertEqual(self.client_for(who).get("/teacher/new").status_code, expected)

    def test_admin_only_pages_deny_a_tester(self):
        for path in ("/teacher", "/teacher/dzi-training"):
            with self.subTest(path=path):
                response = self.client_for("tester").get(path)
                self.assertEqual(response.status_code, 302)
                self.assertTrue(response.headers["Location"].startswith("/admin/login"))
        # The assignment list needs an admin or a teacher account (PR7).
        response = self.client_for("tester").get("/teacher/assignments")
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].startswith("/login?"))

    def test_admin_reaches_every_get_page(self):
        for path in ("/teacher", "/teacher/assignments", "/teacher/dzi-training", "/teacher/new"):
            with self.subTest(path=path):
                self.assertEqual(self.client_for("admin").get(path).status_code, 200)

    def test_missing_assignment_is_not_found(self):
        for path in ("/teacher/assignment/987654", "/teacher/assignment/987654/results",
                     "/teacher/assignment/987654/results.csv"):
            with self.subTest(path=path):
                self.assertEqual(self.client_for("admin").get(path).status_code, 404)

    def test_cross_origin_post_is_refused_before_authorization(self):
        for who in ("anonymous", "tester", "admin"):
            for path in ("/teacher/new", "/teacher/dzi-training",
                         "/teacher/assignment/1/edit", "/teacher/assignment/1/duplicate",
                         "/teacher/assignment/1/results"):
                with self.subTest(who=who, path=path):
                    response = self.client_for(who).post(
                        path, data={}, headers={"Origin": "http://evil.example"})
                    self.assertEqual(response.status_code, 403)

    def test_same_origin_post_by_anonymous_is_sent_to_login(self):
        response = self.client_for("anonymous").post(
            "/teacher/new", data={}, headers={"Origin": "http://localhost"})
        self.assertEqual(response.status_code, 302)


if __name__ == "__main__":
    unittest.main()
