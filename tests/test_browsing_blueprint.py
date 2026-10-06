"""Roadmap PR E4 — the browsing routes live on their own blueprint.

The application route map (rule, methods, policy, integrity) is pinned in
``test_dzi_blueprint``; this file pins who owns the fourteen E4 views, their
endpoint names, the dependency direction of the new modules, and the behavior
of the moved routes (hierarchy ownership, grade support, redirects) on the
tracked seed data.
"""

import ast
import re
import subprocess
import sys
import unittest

from tests.support import ROOT, IsolatedAppTestCase, web_app
from web import access, request_integrity

E4_ROUTES = {
    "/": "browsing.index",
    "/grade/<int:grade>": "browsing.grade_view",
    "/section/<slug>": "browsing.section_view",
    "/subjects/<subject_slug>": "browsing.subject_view",
    "/subjects/<subject_slug>/<int:grade>": "browsing.subject_grade_view",
    "/subjects/<subject_slug>/<int:grade>/<topic_group_slug>": "browsing.subject_topic_group_view",
    "/subjects/<subject_slug>/<int:grade>/<topic_group_slug>/<topic_slug>":
        "browsing.subject_topic_view",
    "/physics": "browsing.physics",
    "/physics/grade/<int:grade>": "browsing.physics_grade",
    "/healthz": "browsing.healthz",
    "/favicon.ico": "browsing.favicon",
}
OLD_ENDPOINTS = tuple(endpoint.split(".", 1)[1] for endpoint in E4_ROUTES.values())
CONTROLLER_MODULES = (
    "web/blueprints/dzi.py", "web/blueprints/practical.py", "web/blueprints/quiz.py",
    "web/blueprints/teacher.py", "web/blueprints/browsing.py", "web/blueprints/auth.py",
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


class BrowsingRouteOwnershipTest(unittest.TestCase):
    def test_every_e4_route_belongs_to_the_browsing_blueprint(self):
        rules = {rule.rule: rule for rule in web_app.app.url_map.iter_rules()}
        for url, endpoint in E4_ROUTES.items():
            with self.subTest(url=url):
                rule = rules[url]
                self.assertEqual(rule.endpoint, endpoint)
                # Every browsing route is read-only: GET, plus implicit HEAD/OPTIONS.
                self.assertEqual(rule.methods - {"HEAD", "OPTIONS"}, {"GET"})
                view = web_app.app.view_functions[endpoint]
                self.assertEqual(view.__module__, "web.blueprints.browsing")
                self.assertEqual(access.policy_for_view(view), access.PUBLIC)
                self.assertIsNone(request_integrity.integrity_policy_for_view(view))

    def test_the_blueprint_owns_exactly_the_e4_routes(self):
        owned = {rule.rule for rule in web_app.app.url_map.iter_rules()
                 if rule.endpoint.startswith("browsing.")}
        self.assertEqual(owned, set(E4_ROUTES))

    def test_old_endpoint_names_are_gone_and_not_aliased(self):
        endpoints = {rule.endpoint for rule in web_app.app.url_map.iter_rules()}
        for legacy in OLD_ENDPOINTS:
            self.assertNotIn(legacy, endpoints)
            self.assertNotIn(legacy, web_app.app.view_functions)

    def test_no_reference_uses_a_bare_browsing_endpoint(self):
        names = "|".join(OLD_ENDPOINTS)
        pattern = re.compile(rf"""url_for\(\s*['"](?:{names})['"]|request\.endpoint\s*(?:==|in)[^\n]*['"](?:{names})['"]""")
        sources = list((ROOT / "web" / "templates").glob("*.html")) + [ROOT / "web/app.py"]
        sources += (ROOT / "web" / "blueprints").glob("*.py")
        for path in sources:
            with self.subTest(path=path.name):
                self.assertIsNone(pattern.search(path.read_text(encoding="utf-8")))

    def test_e5_views_live_in_the_auth_blueprint(self):
        for endpoint in ("auth.tester_home", "auth.tester_login", "auth.admin_login", "auth.switch_profile"):
            with self.subTest(endpoint=endpoint):
                self.assertEqual(web_app.app.view_functions[endpoint].__module__, "web.blueprints.auth")

    def test_moved_helpers_left_the_application_module(self):
        for name in ("subject_browser", "fetch_section", "fetch_questions_in_section",
                     "fetch_grades_with_counts", "grade_subject_entries", "render_subject_page",
                     "subject_section_groups", "healthz", "index", "catalog_subject"):
            self.assertFalse(hasattr(web_app, name), name)


class BrowsingDependencyDirectionTest(unittest.TestCase):
    def test_browsing_blueprint_imports_neither_web_app_nor_another_controller(self):
        imported = imported_modules("web/blueprints/browsing.py")
        self.assertNotIn("web.app", imported)
        self.assertFalse({n for n in imported if n.startswith("web.blueprints")}, imported)

    def test_browsing_modules_use_the_shared_request_connection_only(self):
        for relative in ("web/blueprints/browsing.py", "web/services/browsing.py"):
            with self.subTest(module=relative):
                source = (ROOT / relative).read_text(encoding="utf-8")
                self.assertNotIn("sqlite3.connect", source)
                self.assertNotIn("PRAGMA journal_mode", source)
                self.assertNotIn("CREATE TABLE", source.upper())

    def test_services_never_import_web_app_or_a_controller(self):
        for relative in ("web/services/browsing.py", "web/services/subject_catalog.py",
                         "web/request_db.py"):
            with self.subTest(module=relative):
                imported = imported_modules(relative)
                self.assertNotIn("web.app", imported)
                self.assertFalse({n for n in imported if n.startswith("web.blueprints")}, imported)

    def test_no_controller_imports_web_app(self):
        for relative in CONTROLLER_MODULES:
            with self.subTest(module=relative):
                self.assertNotIn("web.app", imported_modules(relative))

    def test_browsing_blueprint_imports_in_a_fresh_interpreter_without_web_app(self):
        code = (
            "import sys, web.blueprints.browsing, web.services.browsing\n"
            "print('web.app' in sys.modules)\n"
        )
        result = subprocess.run(
            [sys.executable, "-B", "-c", code], cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "False")

    def test_browsing_views_do_not_read_the_session_directly(self):
        tree = ast.parse((ROOT / "web/blueprints/browsing.py").read_text(encoding="utf-8"))
        names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        self.assertNotIn("session", names)


class BrowsingRoutesBehaviorTest(IsolatedAppTestCase):
    IT = "/subjects/information-technologies"

    def setUp(self):
        self.client = web_app.app.test_client()

    def status(self, path):
        return self.client.get(path).status_code

    def test_public_pages_render_for_an_anonymous_visitor(self):
        for path in ("/", "/grade/8", "/grade/12", "/section/grade12-dzi-preparation",
                     self.IT, f"{self.IT}/10", f"{self.IT}/10/grade10-review",
                     f"{self.IT}/10/grade10-review/cookies", "/subjects/physics/10/mechanics",
                     "/subjects/biology/8", "/subjects/biology", "/favicon.ico", "/healthz"):
            with self.subTest(path=path):
                self.assertEqual(self.status(path), 200)

    def test_unknown_subject_unsupported_grade_and_missing_section_are_404(self):
        for path in ("/subjects/nope", "/subjects/nope/8", "/subjects/physics/11",
                     "/subjects/biology/12", "/grade/7", "/grade/13", "/section/nonexistent",
                     "/subjects/physics/8/zzz", "/subjects/physics/8/zzz/yyy"):
            with self.subTest(path=path):
                self.assertEqual(self.status(path), 404)

    def test_section_and_topic_must_belong_to_the_resolved_hierarchy(self):
        for path in (
            "/subjects/physics/10/grade10-review",                       # another subject's section
            f"{self.IT}/9/grade10-review",                               # another grade's section
            f"{self.IT}/10/grade10-creating-content/cookies",            # another section's topic
            "/subjects/physics/10/mechanics/cookies",                    # another subject's topic
            f"{self.IT}/10/grade10-review/no-such-topic",                # malformed topic slug
        ):
            with self.subTest(path=path):
                self.assertEqual(self.status(path), 404)

    def test_structure_only_subjects_are_pages_not_404s(self):
        for path in ("/subjects/biology/8", "/subjects/chemistry/8", "/subjects/mathematics/8"):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200)
                self.assertIn("в подготовка", response.get_data(as_text=True).lower())

    def test_physics_legacy_urls_redirect_permanently(self):
        response = self.client.get("/physics")
        self.assertEqual((response.status_code, response.headers["Location"]),
                         (301, "/subjects/physics"))
        response = self.client.get("/physics/grade/8")
        self.assertEqual((response.status_code, response.headers["Location"]),
                         (301, "/subjects/physics/8"))
        self.assertEqual(self.status("/physics/grade/11"), 404)

    def test_browsing_routes_refuse_unsafe_methods(self):
        for path in ("/", "/grade/8", "/healthz", "/physics", f"{self.IT}/10"):
            with self.subTest(path=path):
                self.assertEqual(self.client.post(path).status_code, 405)

    def test_section_page_links_to_the_teacher_creation_contract(self):
        html = self.client.get("/section/grade12-dzi-preparation").get_data(as_text=True)
        self.assertIn('href="/teacher/new?section=grade12-dzi-preparation"', html)

    def test_each_view_reuses_the_request_scoped_connection(self):
        with web_app.app.test_request_context("/"):
            from web.request_db import get_request_db
            from web.services.browsing import subject_browser
            connection = get_request_db()
            self.assertIs(subject_browser(), subject_browser())
            self.assertIs(get_request_db(), connection)
            self.assertIs(web_app.get_db(), connection)


if __name__ == "__main__":
    unittest.main()
