"""Roadmap PRs D1/D2 — explicit access and request-integrity policies.

The declared policies in ``web.access`` are the only route-level authorization
(``enforce_access_policy`` in ``web.app``; the endpoint-prefix guard is gone).
They must cover every endpoint, keep the D1 access matrix, deny undeclared
views, follow the view function rather than its endpoint name, and stay
independent of cosmetic session state, of request integrity and of DZI content
identity.
"""

import ast
import os
import subprocess
import sys
import unittest
from pathlib import Path

from tests.support import ROOT, IsolatedAppTestCase, web_app
from web import access, request_integrity

# Test-only routes test modules register on the shared app to exercise the
# enforcement path (some deliberately undeclared). They are not part of the
# application's route map.
TEST_ONLY_RULE_PREFIX = "/__test__/"

# The D1 access matrix: the behavioral contract D2 enforces.
EXPECTED_ACCESS_MATRIX = {
    "static": access.PUBLIC,
    "browsing.index": access.PUBLIC,
    "browsing.grade_view": access.PUBLIC,
    "browsing.section_view": access.PUBLIC,
    "browsing.subject_view": access.PUBLIC,
    "browsing.subject_grade_view": access.PUBLIC,
    "browsing.subject_topic_group_view": access.PUBLIC,
    "browsing.subject_topic_view": access.PUBLIC,
    "browsing.physics": access.PUBLIC,
    "browsing.physics_grade": access.PUBLIC,
    "auth.tester_home": access.PUBLIC,
    "browsing.healthz": access.PUBLIC,
    "browsing.favicon": access.PUBLIC,
    "auth.admin_login": access.PUBLIC,
    "auth.tester_login": access.PUBLIC,
    "auth.tester_logout": access.PUBLIC,
    "quiz.quiz_start": access.PUBLIC,
    "teacher.teacher_new": access.TEACHER,
    "teacher.teacher_assignment": access.TEACHER,
    "auth.admin_logout": access.ADMIN,
    "dzi.admin_open_question_candidates": access.ADMIN,
    "dzi.dzi_index": access.ADMIN,
    "dzi.dzi_source_view": access.ADMIN,
    "dzi.dzi_practical_source": access.ADMIN,
    "practical.dzi_practical_resource_download": access.ADMIN,
    "teacher.teacher_dashboard": access.ADMIN,
    "teacher.teacher_assignments": access.ADMIN,
    "teacher.teacher_dzi_training": access.ADMIN,
    "teacher.teacher_assignment_results": access.ADMIN,
    "teacher.teacher_assignment_results_export": access.ADMIN,
    "teacher.teacher_assignment_edit": access.ADMIN,
    "teacher.teacher_assignment_duplicate": access.ADMIN,
    "practical.teacher_practical_submissions": access.ADMIN,
    "practical.teacher_practical_submission_file_download": access.ADMIN,
    "quiz.quiz_attempt": access.ATTEMPT_OWNER,
    "quiz.quiz_result": access.ATTEMPT_OWNER,
    "practical.quiz_practical_attempt": access.ATTEMPT_OWNER,
    "practical.quiz_practical_upload": access.ATTEMPT_OWNER,
    "auth.switch_profile": access.PROFILE_SWITCH,
}

PRINCIPAL_SESSIONS = {
    "anonymous": {},
    "cosmetic_admin_profile": {"ui_profile": "admin"},
    "tester": {"tester_authenticated": True, "ui_profile": "tester"},
    "tester_with_admin_profile": {"tester_authenticated": True, "ui_profile": "admin"},
    "admin": {"admin_authenticated": True, "ui_profile": "admin"},
}
# Who the D1 matrix lets through, written out independently of web.access.
TEACHER_PRINCIPALS = {"tester", "tester_with_admin_profile", "admin"}
ADMIN_PRINCIPALS = {"admin"}

SAMPLE_VIEW_ARGS = {
    "profile": "tester",
    "grade": 8,
    "slug": "sample-section",
    "subject_slug": "information-technologies",
    "topic_group_slug": "sample-group",
    "topic_slug": "sample-topic",
    "source_slug": "may_2025_v2",
    "assignment_id": 1,
    "attempt_id": 1,
    "exam_task_id": 1,
    "resource_id": 1,
    "file_id": 1,
    "filename": "css/tokens.css",
}


def application_rules(app):
    for rule in app.url_map.iter_rules():
        if rule.rule.startswith(TEST_ONLY_RULE_PREFIX):
            continue
        yield rule


def view_args_for(rule, overrides=None):
    args = {name: SAMPLE_VIEW_ARGS[name] for name in rule.arguments}
    if overrides:
        args.update(overrides)
    return args


class AccessPolicyUnitTest(unittest.TestCase):
    """The access matrix, tested without routes."""

    def test_public_is_allowed_for_every_principal(self):
        for name, principal in (
            ("anonymous", access.ANONYMOUS),
            ("tester", access.Principal(is_teacher=True, is_admin=False)),
            ("admin", access.Principal(is_teacher=True, is_admin=True)),
        ):
            with self.subTest(principal=name):
                self.assertTrue(access.is_route_allowed(access.PUBLIC, principal))

    def test_teacher_requires_a_teacher_or_admin_session(self):
        self.assertFalse(access.is_route_allowed(access.TEACHER, access.ANONYMOUS))
        self.assertTrue(access.is_route_allowed(
            access.TEACHER, access.Principal(is_teacher=True, is_admin=False)))
        self.assertTrue(access.is_route_allowed(
            access.TEACHER, access.Principal(is_teacher=True, is_admin=True)))

    def test_admin_requires_an_admin_session(self):
        self.assertFalse(access.is_route_allowed(access.ADMIN, access.ANONYMOUS))
        self.assertFalse(access.is_route_allowed(
            access.ADMIN, access.Principal(is_teacher=True, is_admin=False)))
        self.assertTrue(access.is_route_allowed(
            access.ADMIN, access.Principal(is_teacher=True, is_admin=True)))

    def test_attempt_owner_passes_the_route_and_is_decided_in_the_view(self):
        # Object-level ownership stays in the quiz blueprint (404, not 403).
        self.assertTrue(access.is_route_allowed(access.ATTEMPT_OWNER, access.ANONYMOUS))

    def test_profile_switch_depends_on_the_target_profile(self):
        admin_session = access.Principal(is_teacher=True, is_admin=True)
        self.assertTrue(access.is_route_allowed(
            access.PROFILE_SWITCH, access.ANONYMOUS, {"profile": "tester"}))
        self.assertFalse(access.is_route_allowed(
            access.PROFILE_SWITCH, access.ANONYMOUS, {"profile": "admin"}))
        self.assertTrue(access.is_route_allowed(
            access.PROFILE_SWITCH, admin_session, {"profile": "admin"}))

    def test_undeclared_and_unknown_policies_are_denied(self):
        admin_session = access.Principal(is_teacher=True, is_admin=True)
        for level in (None, "", "teacher_or_whatever"):
            with self.subTest(level=level):
                self.assertFalse(access.is_route_allowed(level, admin_session))

    def test_denied_requests_are_sent_to_the_matching_login(self):
        self.assertEqual(access.denial_login_endpoint(access.TEACHER), "auth.tester_login")
        self.assertEqual(access.denial_login_endpoint(access.ADMIN), "auth.admin_login")
        self.assertEqual(access.denial_login_endpoint(None), "auth.admin_login")
        self.assertEqual(
            access.denial_login_endpoint(access.PROFILE_SWITCH, {"profile": "admin"}),
            "auth.admin_login",
        )

    def test_principal_reads_authentication_state_only(self):
        self.assertEqual(access.principal_from_session({}), access.ANONYMOUS)
        self.assertEqual(
            access.principal_from_session({"tester_authenticated": True}),
            access.Principal(is_teacher=True, is_admin=False),
        )
        # An admin session also satisfies teacher today.
        self.assertEqual(
            access.principal_from_session({"admin_authenticated": True}),
            access.Principal(is_teacher=True, is_admin=True),
        )

    def test_cosmetic_ui_profile_alone_grants_nothing(self):
        for profile in ("admin", "tester", "unexpected"):
            with self.subTest(ui_profile=profile):
                principal = access.principal_from_session({"ui_profile": profile})
                self.assertEqual(principal, access.ANONYMOUS)
                self.assertFalse(access.is_route_allowed(access.ADMIN, principal))
                self.assertFalse(access.is_route_allowed(access.TEACHER, principal))

    def test_unknown_policy_names_cannot_be_declared(self):
        with self.assertRaises(ValueError):
            access.access_policy("superuser")
        with self.assertRaises(ValueError):
            request_integrity.integrity_policy("trust_me")


class AccessPolicyCoverageTest(unittest.TestCase):
    """Default deny: every endpoint declares a policy, aliases included."""

    @classmethod
    def setUpClass(cls):
        cls.app = web_app.app

    def test_every_endpoint_declares_an_access_policy(self):
        undeclared = sorted(
            rule.endpoint
            for rule in application_rules(self.app)
            if access.policy_for_endpoint(self.app, rule.endpoint) is None
        )
        self.assertEqual(undeclared, [])

    def test_every_unsafe_route_declares_a_request_integrity_policy(self):
        missing = []
        for rule in application_rules(self.app):
            if not (rule.methods & request_integrity.UNSAFE_METHODS):
                continue
            view_func = self.app.view_functions[rule.endpoint]
            if request_integrity.integrity_policy_for_view(view_func) is None:
                missing.append(rule.endpoint)
        self.assertEqual(sorted(missing), [])

    def test_ordinary_unsafe_routes_use_the_same_origin_mechanism(self):
        for rule in application_rules(self.app):
            if not (rule.methods & request_integrity.UNSAFE_METHODS):
                continue
            view_func = self.app.view_functions[rule.endpoint]
            with self.subTest(endpoint=rule.endpoint):
                self.assertEqual(
                    request_integrity.integrity_policy_for_view(view_func),
                    request_integrity.SAME_ORIGIN,
                )

    def test_aliases_and_canonical_endpoints_share_one_policy(self):
        by_rule = {}
        for rule in application_rules(self.app):
            by_rule.setdefault(rule.rule, set()).add(
                access.policy_for_endpoint(self.app, rule.endpoint)
            )
        disagreeing = {rule: policies for rule, policies in by_rule.items() if len(policies) > 1}
        self.assertEqual(disagreeing, {})

    def test_a_renamed_endpoint_keeps_its_policy(self):
        # Policies live on view functions, so a blueprint-style rename of the
        # endpoint string cannot change the access decision.
        view_func = self.app.view_functions["teacher.teacher_new"]
        self.assertEqual(access.policy_for_view(view_func), access.TEACHER)
        self.assertEqual(
            access.policy_for_endpoint(self.app, "quiz.quiz_attempt"),
            access.ATTEMPT_OWNER,
        )

    def test_access_policy_modules_import_neither_web_app_nor_services(self):
        for module_path in ("web/access.py", "web/request_integrity.py"):
            tree = ast.parse(Path(ROOT, module_path).read_text(encoding="utf-8"))
            imported = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported.update(alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom):
                    imported.add(node.module or "")
            with self.subTest(module=module_path):
                self.assertNotIn("web.app", imported)
                # The C2 DZI content predicate is not an access decision.
                self.assertFalse({name for name in imported if name.startswith("web.services")})

    def test_importing_the_policy_does_not_pull_in_web_app(self):
        probe = (
            "import sys\n"
            "import web.access, web.request_integrity\n"
            "assert 'web.app' not in sys.modules, sorted(sys.modules)\n"
        )
        env = dict(os.environ, PYTHONPATH=str(ROOT))
        result = subprocess.run(
            [sys.executable, "-B", "-c", probe],
            cwd=str(ROOT), capture_output=True, text=True, env=env,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_access_policy_does_not_call_the_dzi_content_predicate(self):
        tree = ast.parse(Path(ROOT, "web/access.py").read_text(encoding="utf-8"))
        called = {
            node.func.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        self.assertNotIn("is_dzi_question", called)

def expected_decision(policy, principal_name, view_args, path):
    """(allowed, login redirect) per the D1 matrix, without consulting web.access."""
    if policy == access.PROFILE_SWITCH:
        if view_args.get("profile") != "admin" or principal_name in ADMIN_PRINCIPALS:
            return True, None
        return False, "/admin/login?next=/"
    if policy in (access.PUBLIC, access.ATTEMPT_OWNER):
        return True, None
    if policy == access.TEACHER:
        allowed = principal_name in TEACHER_PRINCIPALS
        return allowed, None if allowed else f"/tester/login?next={path}"
    allowed = policy == access.ADMIN and principal_name in ADMIN_PRINCIPALS
    return allowed, None if allowed else f"/admin/login?next={path}"


class AccessEnforcementMatrixTest(IsolatedAppTestCase):
    """The declared policy is the enforcement path and keeps the D1 matrix."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.app = web_app.app
        cls.app.config.update(TESTING=True)

    def _enforce(self, path, method, session_state):
        """Run the app's before_request chain for a same-origin request."""
        headers = {"Origin": "http://localhost"} if method in request_integrity.UNSAFE_METHODS else {}
        with self.app.test_request_context(path, method=method, headers=headers):
            from flask import session as flask_session

            flask_session.update(session_state)
            return self.app.preprocess_request()

    def test_route_map_policies_match_the_d1_access_matrix(self):
        actual = {
            rule.endpoint: access.policy_for_endpoint(self.app, rule.endpoint)
            for rule in application_rules(self.app)
        }
        self.assertEqual(actual, EXPECTED_ACCESS_MATRIX)

    def test_prefix_guard_is_gone_and_one_access_hook_enforces(self):
        self.assertFalse(hasattr(web_app, "protect_admin_routes"))
        self.assertFalse(hasattr(web_app, "TESTER_TEACHER_ENDPOINTS"))
        self.assertFalse(hasattr(web_app, "PUBLIC_DZI_ENDPOINTS"))
        hooks = [func.__name__ for func in self.app.before_request_funcs[None]]
        self.assertEqual(hooks.count("enforce_access_policy"), 1)
        # Integrity runs before, and separately from, authorization.
        self.assertLess(
            hooks.index("protect_same_origin_unsafe_methods"),
            hooks.index("enforce_access_policy"),
        )

    def test_enforcement_matches_the_d1_matrix_for_every_route(self):
        for rule in application_rules(self.app):
            policy = EXPECTED_ACCESS_MATRIX[rule.endpoint]
            method = "GET" if "GET" in rule.methods else "POST"
            profile_targets = ("tester", "admin") if "profile" in rule.arguments else (None,)
            for target in profile_targets:
                view_args = view_args_for(rule, {"profile": target} if target else None)
                with self.app.test_request_context("/"):
                    from flask import url_for

                    path = url_for(rule.endpoint, **view_args)
                for name, session_state in PRINCIPAL_SESSIONS.items():
                    allowed, login = expected_decision(policy, name, view_args, path)
                    with self.subTest(endpoint=rule.endpoint, principal=name, profile=target):
                        response = self._enforce(path, method, session_state)
                        if allowed:
                            self.assertIsNone(response)
                        else:
                            self.assertIsNotNone(response)
                            self.assertEqual(response.status_code, 302)
                            self.assertEqual(response.headers["Location"], login)

    def test_unmatched_urls_keep_flask_404(self):
        # No view resolves, so there is no policy to apply; Flask answers 404
        # exactly as before instead of a login redirect.
        client = self.app.test_client()
        for path in ("/no/such/route", "/teacher/no-such-page", "/dzi/no/such/page"):
            with self.subTest(path=path):
                self.assertEqual(client.get(path).status_code, 404)


def _undeclared_view():
    return "undeclared view body"


def _declared_admin_view():
    return "declared admin view body"


class AccessEnforcementBypassTest(IsolatedAppTestCase):
    """Authorization attaches to the view, not to an endpoint string.

    Every case here would have been wrong under the removed prefix guard: an
    undeclared or public-looking endpoint name was public unless it started
    with admin/dzi/teacher, and an alias name decided access for the view.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.app = web_app.app
        cls.app.config.update(TESTING=True)
        rules = (
            # A new view someone forgot to declare, with an innocent name.
            ("/__test__/undeclared-report", "reports_overview", _undeclared_view),
            # A declared admin view whose endpoint name says nothing about admin.
            ("/__test__/declared-admin", "public_summary",
             access.access_policy(access.ADMIN)(_declared_admin_view)),
            # Aliases registering existing views under misleading names.
            ("/__test__/alias/dashboard", "public_dashboard_alias",
             cls.app.view_functions["teacher.teacher_dashboard"]),
            ("/__test__/alias/new", "admin_teacher_new_alias",
             cls.app.view_functions["teacher.teacher_new"]),
        )
        for path, endpoint, view in rules:
            if endpoint not in cls.app.view_functions:
                cls.app.add_url_rule(path, endpoint=endpoint, view_func=view)

    def _client(self, session_state):
        client = self.app.test_client()
        with client.session_transaction() as sess:
            sess.update(session_state)
        return client

    def assertSentToLogin(self, response, login_path):
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].startswith(login_path), response.headers["Location"])

    def test_undeclared_view_is_denied_to_every_principal(self):
        for name, session_state in PRINCIPAL_SESSIONS.items():
            with self.subTest(principal=name):
                response = self._client(session_state).get("/__test__/undeclared-report")
                self.assertSentToLogin(response, "/admin/login")
                self.assertNotIn(b"undeclared view body", response.data)

    def test_declared_admin_view_with_a_public_name_stays_admin_only(self):
        for name in ("anonymous", "cosmetic_admin_profile", "tester", "tester_with_admin_profile"):
            with self.subTest(principal=name):
                response = self._client(PRINCIPAL_SESSIONS[name]).get("/__test__/declared-admin")
                self.assertSentToLogin(response, "/admin/login")
        response = self._client(PRINCIPAL_SESSIONS["admin"]).get("/__test__/declared-admin")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, b"declared admin view body")

    def test_alias_with_a_public_name_cannot_bypass_an_admin_view(self):
        for name in ("anonymous", "cosmetic_admin_profile", "tester"):
            with self.subTest(principal=name):
                response = self._client(PRINCIPAL_SESSIONS[name]).get("/__test__/alias/dashboard")
                self.assertSentToLogin(response, "/admin/login")
        response = self._client(PRINCIPAL_SESSIONS["admin"]).get("/__test__/alias/dashboard")
        self.assertEqual(response.status_code, 200)

    def test_alias_with_an_admin_name_keeps_the_teacher_policy(self):
        anonymous = self._client({}).get("/__test__/alias/new")
        self.assertSentToLogin(anonymous, "/tester/login")
        tester = self._client(PRINCIPAL_SESSIONS["tester"]).get("/__test__/alias/new")
        self.assertEqual(tester.status_code, 200)


class AccessPolicyRouteTest(IsolatedAppTestCase):
    """Representative routes keep their exact status/redirect semantics."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.app = web_app.app
        cls.app.config.update(TESTING=True)
        conn = web_app.quiz_db()
        try:
            row = conn.execute("""
                SELECT id FROM curriculum_sections ORDER BY id
            """).fetchall()
            cls.section_id = None
            for candidate in row:
                if web_app.quiz_section_question_ids(conn, int(candidate["id"])):
                    cls.section_id = int(candidate["id"])
                    break
        finally:
            conn.close()
        if cls.section_id is None:
            raise AssertionError("No section with eligible quiz questions found")

    def setUp(self):
        self.client = self.app.test_client()

    def _as_tester(self):
        with self.client.session_transaction() as sess:
            sess["tester_authenticated"] = True
            sess["ui_profile"] = "tester"

    def _as_admin(self):
        with self.client.session_transaction() as sess:
            sess["admin_authenticated"] = True
            sess["ui_profile"] = "admin"

    def test_public_route_stays_public(self):
        self.assertEqual(self.client.get("/").status_code, 200)
        self.assertEqual(self.client.get("/healthz").status_code, 200)

    def test_teacher_route_allows_tester_and_denies_anonymous(self):
        self.assertIn("/tester/login", self.client.get("/teacher/new").headers["Location"])
        self._as_tester()
        self.assertEqual(self.client.get("/teacher/new").status_code, 200)

    def test_admin_route_denies_tester_and_allows_admin(self):
        for path in ("/teacher", "/teacher/assignments", "/teacher/practical-submissions", "/dzi"):
            with self.subTest(path=path):
                self._as_tester()
                response = self.client.get(path)
                self.assertEqual(response.status_code, 302, path)
                self.assertIn("/admin/login", response.headers["Location"], path)

        self.client = self.app.test_client()
        self._as_admin()
        for path in ("/teacher", "/teacher/assignments", "/dzi"):
            with self.subTest(path=path, principal="admin"):
                self.assertEqual(self.client.get(path).status_code, 200, path)

    def test_cosmetic_admin_profile_alone_grants_no_admin_access(self):
        with self.client.session_transaction() as sess:
            sess["ui_profile"] = "admin"
        response = self.client.get("/dzi")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/admin/login", response.headers["Location"])

        with self.client.session_transaction() as sess:
            sess["tester_authenticated"] = True
            sess["ui_profile"] = "admin"
        response = self.client.get("/teacher/assignments")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/admin/login", response.headers["Location"])

    def test_profile_switch_to_admin_needs_an_admin_session(self):
        response = self.client.get("/profile/admin")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/admin/login", response.headers["Location"])
        self.assertEqual(self.client.get("/profile/tester").status_code, 302)

    def test_attempt_routes_answer_404_for_a_foreign_attempt(self):
        for path in (
            "/quiz/attempt/999999",
            "/quiz/attempt/999999/result",
            "/quiz/attempt/999999/practical/may_2025_v2",
        ):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 404, path)

    def test_unsafe_requests_still_require_same_origin(self):
        self._as_tester()
        data = {"section_id": str(self.section_id), "question_count": "1", "time_limit_minutes": ""}
        rejected = self.client.post(
            "/teacher/new", data=data, headers={"Origin": "http://evil.example"})
        self.assertEqual(rejected.status_code, 403)

        accepted = self.client.post(
            "/teacher/new", data=data, headers={"Origin": "http://localhost"})
        self.assertEqual(accepted.status_code, 302)
        self.assertIn("/teacher/assignment/", accepted.headers["Location"])

    def test_authorized_session_still_needs_a_same_origin_request(self):
        # Authorization never implies request integrity.
        self._as_admin()
        for headers in (
            {"Origin": "http://evil.example"},
            {"Referer": "http://evil.example/teacher/dzi-training"},
        ):
            with self.subTest(headers=headers):
                response = self.client.post("/teacher/dzi-training", data={}, headers=headers)
                self.assertEqual(response.status_code, 403)

    def test_same_origin_request_is_still_authorized_separately(self):
        # Request integrity never implies authorization.
        same_origin = {"Origin": "http://localhost"}
        data = {"section_id": str(self.section_id), "question_count": "1", "time_limit_minutes": ""}
        anonymous = self.client.post("/teacher/new", data=data, headers=same_origin)
        self.assertEqual(anonymous.status_code, 302)
        self.assertEqual(anonymous.headers["Location"], "/tester/login?next=/teacher/new")

        self._as_tester()
        tester = self.client.post("/teacher/dzi-training", data={}, headers=same_origin)
        self.assertEqual(tester.status_code, 302)
        self.assertEqual(tester.headers["Location"], "/admin/login?next=/teacher/dzi-training")

    def test_cosmetic_admin_profile_gains_nothing_on_unsafe_requests(self):
        with self.client.session_transaction() as sess:
            sess["tester_authenticated"] = True
            sess["ui_profile"] = "admin"
        response = self.client.post(
            "/teacher/dzi-training", data={}, headers={"Origin": "http://localhost"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/admin/login?next=/teacher/dzi-training")

    def test_cross_origin_unsafe_request_is_refused_before_authorization(self):
        # Integrity runs first: an unauthorized cross-origin post is a 403,
        # exactly as before the cutover, not a login redirect.
        response = self.client.post(
            "/teacher/dzi-training", data={}, headers={"Origin": "http://evil.example"})
        self.assertEqual(response.status_code, 403)


if __name__ == "__main__":
    unittest.main()
