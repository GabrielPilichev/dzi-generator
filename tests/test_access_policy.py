"""Roadmap PR D1 — explicit access and request-integrity policies.

The declared policies in ``web.access`` must agree with the prefix guard that
still enforces access (``protect_admin_routes``), cover every endpoint, and
stay independent of cosmetic session state and of DZI content identity.
"""

import ast
import os
import subprocess
import sys
import unittest
from pathlib import Path

from tests.support import ROOT, IsolatedAppTestCase, web_app
from web import access, request_integrity

# Test-only routes other test modules register on the shared app to exercise
# the old guard's DZI default-deny. They are not part of the application's
# route map and deliberately carry no declared policy.
TEST_ONLY_RULE_PREFIX = "/__test__/"

PRINCIPAL_SESSIONS = {
    "anonymous": {},
    "tester": {"tester_authenticated": True, "ui_profile": "tester"},
    "admin": {"admin_authenticated": True, "ui_profile": "admin"},
}

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
        self.assertEqual(access.denial_login_endpoint(access.TEACHER), "tester_login")
        self.assertEqual(access.denial_login_endpoint(access.ADMIN), "admin_login")
        self.assertEqual(access.denial_login_endpoint(None), "admin_login")
        self.assertEqual(
            access.denial_login_endpoint(access.PROFILE_SWITCH, {"profile": "admin"}),
            "admin_login",
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
        view_func = self.app.view_functions["teacher_new"]
        self.assertEqual(access.policy_for_view(view_func), access.TEACHER)
        self.assertEqual(
            access.policy_for_endpoint(self.app, "quiz.quiz_attempt"),
            access.policy_for_endpoint(self.app, "quiz_attempt"),
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

class AccessPolicyAgreementTest(IsolatedAppTestCase):
    """D1 keeps the old guard in place; the declared policies must agree with it."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.app = web_app.app
        cls.app.config.update(TESTING=True)

    def _old_guard_allows(self, rule, session_state, view_args):
        with self.app.test_request_context("/"):
            from flask import request, session as flask_session

            flask_session.update(session_state)
            request.url_rule = rule
            request.view_args = dict(view_args)
            return web_app.protect_admin_routes() is None

    def test_declared_policy_agrees_with_the_prefix_guard(self):
        for rule in application_rules(self.app):
            if rule.endpoint == access.STATIC_ENDPOINT:
                continue
            policy = access.policy_for_endpoint(self.app, rule.endpoint)
            profile_targets = ("tester", "admin") if "profile" in rule.arguments else (None,)
            for target in profile_targets:
                overrides = {"profile": target} if target else None
                view_args = view_args_for(rule, overrides)
                for name, session_state in PRINCIPAL_SESSIONS.items():
                    principal = access.principal_from_session(session_state)
                    with self.subTest(endpoint=rule.endpoint, principal=name, profile=target):
                        self.assertEqual(
                            access.is_route_allowed(policy, principal, view_args),
                            self._old_guard_allows(rule, session_state, view_args),
                        )

    def test_guard_and_policy_send_denials_to_the_same_login(self):
        for endpoint, expected in (("teacher_new", "/tester/login"), ("dzi_index", "/admin/login")):
            rule = next(r for r in application_rules(self.app) if r.endpoint == endpoint)
            policy = access.policy_for_endpoint(self.app, endpoint)
            with self.subTest(endpoint=endpoint):
                with self.app.test_request_context("/"):
                    from flask import request, url_for

                    request.url_rule = rule
                    request.view_args = view_args_for(rule)
                    response = web_app.protect_admin_routes()
                    self.assertIsNotNone(response)
                    self.assertIn(expected, response.headers["Location"])
                    self.assertIn(
                        expected,
                        url_for(access.denial_login_endpoint(policy, request.view_args)),
                    )


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

    def test_legacy_quiz_aliases_behave_like_their_canonical_routes(self):
        with self.app.test_request_context("/"):
            from flask import url_for

            for alias, canonical in (
                ("quiz_attempt", "quiz.quiz_attempt"),
                ("quiz_result", "quiz.quiz_result"),
                ("quiz_start", "quiz.quiz_start"),
            ):
                with self.subTest(alias=alias):
                    self.assertEqual(
                        url_for(alias, attempt_id=1, assignment_id=1, _external=False),
                        url_for(canonical, attempt_id=1, assignment_id=1, _external=False),
                    )

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


if __name__ == "__main__":
    unittest.main()
