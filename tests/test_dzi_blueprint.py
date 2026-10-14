"""Roadmap PR E1 — the DZI routes live on their own blueprint.

Pins the application route map (rules, methods, access and request-integrity
policy; endpoint names are deliberately not part of it), the ownership of the
four E1 views, the dependency direction of the new modules, and the behavior of
the moved pages for anonymous visitors and teacher and admin accounts.
"""

import ast
import subprocess
import sys
import unittest

from tests.auth_helpers import client_as
from tests.support import ROOT, IsolatedAppTestCase, web_app
from web import access, request_integrity

# (rule, methods without HEAD/OPTIONS, access policy, request-integrity policy)
EXPECTED_URL_MAP = (
    ('/', ('GET',), 'public', None),
    ('/account', ('GET',), 'signed_in', None),
    ('/account/attempts', ('GET',), 'quiz_taker', None),
    ('/account/password', ('GET', 'POST'), 'signed_in', 'same_origin'),
    # Google sign-in (PR #331). The callback is the one GET with an integrity
    # mechanism of its own; the other four are ordinary same-origin POST routes.
    ('/auth/google/callback', ('GET',), 'public', 'oauth_state'),
    ('/auth/google/link', ('POST',), 'signed_in', 'same_origin'),
    ('/auth/google/link/cancel', ('POST',), 'signed_in', 'same_origin'),
    ('/auth/google/link/confirm', ('GET', 'POST'), 'signed_in', 'same_origin'),
    ('/auth/google/start', ('POST',), 'public', 'same_origin'),
    # Student registration (registration PR 4); 404 while it is disabled.
    ('/register', ('GET',), 'public', None),
    ('/register/cancel', ('POST',), 'public', 'same_origin'),
    ('/register/resend', ('POST',), 'public', 'same_origin'),
    ('/register/student', ('GET', 'POST'), 'public', 'same_origin'),
    ('/register/teacher', ('GET', 'POST'), 'public', 'same_origin'),
    ('/register/verify', ('GET', 'POST'), 'public', 'same_origin'),
    ('/admin/open-question-candidates', ('GET',), 'admin', None),
    ('/dzi', ('GET',), 'admin', None),
    ('/dzi/practical/resource/<int:resource_id>/download', ('GET',), 'admin', None),
    ('/dzi/source/<source_slug>', ('GET',), 'admin', None),
    ('/dzi/source/<source_slug>/practical', ('GET',), 'admin', None),
    ('/favicon.ico', ('GET',), 'public', None),
    ('/grade/<int:grade>', ('GET',), 'public', None),
    ('/healthz', ('GET',), 'public', None),
    ('/login', ('GET', 'POST'), 'public', 'same_origin'),
    ('/login/totp', ('POST',), 'public', 'same_origin'),
    ('/logout', ('POST',), 'public', 'same_origin'),
    ('/physics', ('GET',), 'public', None),
    ('/physics/grade/<int:grade>', ('GET',), 'public', None),
    ('/quiz/attempt/<int:attempt_id>', ('GET', 'POST'), 'attempt_owner', 'same_origin'),
    ('/quiz/s/<public_token>', ('GET', 'POST'), 'public', 'same_origin'),
    ('/quiz/attempt/<int:attempt_id>/practical/<source_slug>', ('GET',), 'attempt_owner', None),
    ('/quiz/attempt/<int:attempt_id>/practical/<source_slug>/task/<int:exam_task_id>/upload', ('POST',), 'attempt_owner', 'same_origin'),
    ('/quiz/attempt/<int:attempt_id>/result', ('GET',), 'attempt_owner', None),
    ('/section/<slug>', ('GET',), 'public', None),
    ('/static/<path:filename>', ('GET',), None, None),
    ('/subjects/<subject_slug>', ('GET',), 'public', None),
    ('/subjects/<subject_slug>/<int:grade>', ('GET',), 'public', None),
    ('/subjects/<subject_slug>/<int:grade>/<topic_group_slug>', ('GET',), 'public', None),
    ('/subjects/<subject_slug>/<int:grade>/<topic_group_slug>/<topic_slug>', ('GET',), 'public', None),
    ('/teacher', ('GET',), 'admin', None),
    ('/teacher/assignment/<int:assignment_id>', ('GET',), 'teacher', None),
    ('/teacher/assignment/<int:assignment_id>/close', ('POST',), 'assignment_owner', 'same_origin'),
    ('/teacher/assignment/<int:assignment_id>/duplicate', ('POST',), 'assignment_owner', 'same_origin'),
    ('/teacher/assignment/<int:assignment_id>/edit', ('POST',), 'assignment_owner', 'same_origin'),
    ('/teacher/assignment/<int:assignment_id>/qr.svg', ('GET',), 'assignment_owner', None),
    ('/teacher/assignment/<int:assignment_id>/results', ('GET', 'POST'), 'assignment_owner', 'same_origin'),
    ('/teacher/assignment/<int:assignment_id>/results.csv', ('GET',), 'assignment_owner', None),
    ('/teacher/assignments', ('GET',), 'assignment_owner', None),
    ('/teacher/dzi-training', ('GET', 'POST'), 'admin', 'same_origin'),
    ('/teacher/new', ('GET', 'POST'), 'teacher', 'same_origin'),
    ('/teacher/practical-submissions', ('GET',), 'assignment_owner', None),
    ('/teacher/practical/submission-file/<int:file_id>/download', ('GET',), 'assignment_owner', None),
)

E1_ENDPOINTS = {
    "/dzi": "dzi.dzi_index",
    "/dzi/source/<source_slug>": "dzi.dzi_source_view",
    "/dzi/source/<source_slug>/practical": "dzi.dzi_practical_source",
    "/admin/open-question-candidates": "dzi.admin_open_question_candidates",
}


def current_url_map():
    rows = []
    for rule in web_app.app.url_map.iter_rules():
        if rule.rule.startswith("/__test__/"):
            continue
        view = web_app.app.view_functions[rule.endpoint]
        rows.append((
            rule.rule,
            tuple(sorted(rule.methods - {"HEAD", "OPTIONS"})),
            access.policy_for_view(view),
            request_integrity.integrity_policy_for_view(view),
        ))
    return sorted(rows)


def imported_modules(relative):
    tree = ast.parse((ROOT / relative).read_text(encoding="utf-8"))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.add(node.module or "")
    return names


class UrlMapSnapshotTest(unittest.TestCase):
    def test_route_map_is_unchanged(self):
        self.assertEqual(current_url_map(), sorted(EXPECTED_URL_MAP))

    def test_e1_routes_are_owned_by_the_dzi_blueprint(self):
        rules = {rule.rule: rule for rule in web_app.app.url_map.iter_rules()}
        for url, endpoint in E1_ENDPOINTS.items():
            with self.subTest(url=url):
                self.assertEqual(rules[url].endpoint, endpoint)
                view = web_app.app.view_functions[endpoint]
                self.assertEqual(view.__module__, "web.blueprints.dzi")
                self.assertEqual(access.policy_for_view(view), access.ADMIN)
                self.assertEqual(rules[url].methods - {"HEAD", "OPTIONS"}, {"GET"})

    def test_no_legacy_endpoint_alias_was_added(self):
        endpoints = {rule.endpoint for rule in web_app.app.url_map.iter_rules()}
        for legacy in ("dzi_index", "dzi_source_view", "dzi_practical_source",
                       "admin_open_question_candidates"):
            self.assertNotIn(legacy, endpoints)

    def test_e3_dzi_training_route_moved_to_the_teacher_blueprint(self):
        self.assertEqual(
            web_app.app.view_functions["teacher.teacher_dzi_training"].__module__,
            "web.blueprints.teacher")


class DziBlueprintDependencyTest(unittest.TestCase):
    def test_blueprint_and_service_import_neither_web_app_nor_other_controllers(self):
        for relative in ("web/blueprints/dzi.py", "web/services/dzi_exams.py"):
            with self.subTest(module=relative):
                imported = imported_modules(relative)
                self.assertNotIn("web.app", imported)
                self.assertFalse({n for n in imported if n.startswith("web.blueprints")}, imported)
                self.assertFalse({n for n in imported if n.startswith("src")}, imported)

    def test_blueprint_has_no_function_local_import(self):
        tree = ast.parse((ROOT / "web/blueprints/dzi.py").read_text(encoding="utf-8"))
        for func in ast.walk(tree):
            if isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for node in ast.walk(func):
                    self.assertNotIsInstance(node, (ast.Import, ast.ImportFrom), func.name)

    def test_blueprint_opens_connections_through_the_shared_policy(self):
        source = (ROOT / "web/blueprints/dzi.py").read_text(encoding="utf-8")
        self.assertNotIn("sqlite3", source)
        self.assertIn("web_db.connect_db", source)

    def test_importing_the_blueprint_does_not_load_web_app(self):
        code = (
            "import sys, web.blueprints.dzi, web.services.dzi_exams\n"
            "print('web.app' in sys.modules)\n"
        )
        result = subprocess.run(
            [sys.executable, "-B", "-c", code], cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "False")

    def test_content_identity_service_is_untouched_by_the_route_move(self):
        imported = imported_modules("web/services/dzi.py")
        self.assertFalse({n for n in imported if n.startswith("web")}, imported)


class DziRoutesBehaviorTest(IsolatedAppTestCase):
    SLUG = "may_2025_v2"

    def get(self, who, path):
        return client_as(web_app.app, web_app.app.config["DB_PATH"], who).get(path)

    def test_admin_sees_every_e1_page(self):
        for path in ("/dzi", f"/dzi/source/{self.SLUG}",
                     f"/dzi/source/{self.SLUG}/practical", "/admin/open-question-candidates"):
            with self.subTest(path=path):
                response = self.get("admin", path)
                self.assertEqual(response.status_code, 200)

    def test_anonymous_is_sent_to_the_account_login_and_a_teacher_is_refused(self):
        for path in ("/dzi", f"/dzi/source/{self.SLUG}",
                     f"/dzi/source/{self.SLUG}/practical", "/admin/open-question-candidates"):
            with self.subTest(path=path):
                for who in ("anonymous", "historical"):
                    response = self.get(who, path)
                    self.assertEqual(response.status_code, 302)
                    self.assertEqual(response.headers["Location"], f"/login?next={path}")
                self.assertEqual(self.get("teacher", path).status_code, 403)

    def test_unknown_or_malformed_source_is_404_for_admin(self):
        for slug in ("bogus", "may_2099_v9", "may_2025", "x_y_z"):
            for suffix in ("", "/practical"):
                with self.subTest(slug=slug, suffix=suffix):
                    self.assertEqual(
                        self.get("admin", f"/dzi/source/{slug}{suffix}").status_code, 404)

    def test_dzi_source_page_lists_part_one_and_part_two(self):
        html = self.get("admin", f"/dzi/source/{self.SLUG}").get_data(as_text=True)
        self.assertIn("/dzi/source/may_2025_v2/practical", html)
        self.assertIn('href="/dzi"', html)

    def test_index_links_each_source_by_its_slug(self):
        html = self.get("admin", "/dzi").get_data(as_text=True)
        self.assertIn(f'href="/dzi/source/{self.SLUG}"', html)

    def test_unsafe_methods_are_not_allowed(self):
        client = client_as(web_app.app, web_app.app.config["DB_PATH"], "admin")
        for path in ("/dzi", f"/dzi/source/{self.SLUG}", "/admin/open-question-candidates"):
            with self.subTest(path=path):
                response = client.post(path, headers={"Origin": "http://localhost"})
                self.assertEqual(response.status_code, 405)

    def test_cross_origin_post_is_still_refused_before_routing(self):
        client = client_as(web_app.app, web_app.app.config["DB_PATH"], "admin")
        response = client.post("/dzi", headers={"Origin": "http://evil.example"})
        self.assertIn(response.status_code, (400, 403))

    def test_nav_highlight_is_unchanged_by_the_endpoint_rename(self):
        marker = 'aria-current="page">ДЗИ</a>'
        self.assertIn(marker, self.get("admin", "/dzi").get_data(as_text=True))
        self.assertNotIn(
            marker, self.get("admin", "/admin/open-question-candidates").get_data(as_text=True))


class DziExamsServiceTest(IsolatedAppTestCase):
    def test_slug_round_trip_and_exam_lookup(self):
        from web.db import connect_db
        from web.services import dzi_exams

        self.assertEqual(dzi_exams.dzi_parse_source_slug("may_2025_v2"), (2025, "may", 2))
        self.assertEqual(dzi_exams.dzi_parse_source_slug("aug_2023_v1"), (2023, "august", 1))
        self.assertIsNone(dzi_exams.dzi_parse_source_slug("may_2025"))
        self.assertIsNone(dzi_exams.dzi_parse_source_slug("may_2025_x2"))
        conn = connect_db(web_app.app.config["DB_PATH"], query_only=True)
        try:
            exam = dzi_exams.dzi_find_exam(conn, "may_2025_v2")
            self.assertEqual(exam["source_slug"], "may_2025_v2")
            self.assertIsNone(dzi_exams.dzi_find_exam(conn, "may_2099_v9"))
            self.assertEqual(
                {s["source_slug"] for s in dzi_exams.fetch_dzi_sources(conn)} >= {"may_2025_v2"}, True)
        finally:
            conn.close()

    def test_app_keeps_its_historic_helper_names_but_not_the_thin_wrappers(self):
        for name in ("dzi_source_slug", "dzi_parse_source_slug", "dzi_json_text_list",
                     "DZI_FORMAT_VERSION"):
            self.assertTrue(hasattr(web_app, name), name)
        for name in ("dzi_find_exam", "fetch_dzi_tasks"):
            self.assertFalse(hasattr(web_app, name), name)


if __name__ == "__main__":
    unittest.main()
