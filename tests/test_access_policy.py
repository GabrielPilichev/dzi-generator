"""Roadmap PRs D1/D2 — explicit access and request-integrity policies.

The declared policies in ``web.access`` are the only route-level authorization
(``enforce_access_policy`` in ``web.app``; the endpoint-prefix guard is gone).
They must cover every endpoint, keep the D1 access matrix, deny undeclared
views, follow the view function rather than its endpoint name, and stay
independent of request integrity and of DZI content identity. The principal is
always an account (Accounts PR10): an anonymous visitor is sent to ``/login``, a
signed-in account without the role is refused (403), and the session keys of the
removed shared-password logins are not an identity.
"""

import ast
import os
import subprocess
import sys
import time
import unittest
from pathlib import Path

from tests.auth_helpers import client_as, create_account
from tests.support import ROOT, IsolatedAppTestCase, web_app
from tests.support import NO_DEADLINE_FORM
from web import access, oauth_state, request_integrity

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
    "browsing.healthz": access.PUBLIC,
    "browsing.favicon": access.PUBLIC,
    # Student entry by public token: the only one (the numeric entry is retired).
    "quiz.quiz_start_public": access.PUBLIC,
    "teacher.teacher_new": access.TEACHER,
    "teacher.teacher_assignment": access.TEACHER,
    "dzi.admin_open_question_candidates": access.ADMIN,
    "dzi.dzi_index": access.ADMIN,
    "dzi.dzi_source_view": access.ADMIN,
    "dzi.dzi_practical_source": access.ADMIN,
    "practical.dzi_practical_resource_download": access.ADMIN,
    "teacher.teacher_dashboard": access.ADMIN,
    "teacher.teacher_dzi_training": access.ADMIN,
    # Accounts PR7: admin or teacher account; ownership is decided in the view.
    "teacher.teacher_assignments": access.ASSIGNMENT_OWNER,
    "teacher.teacher_assignment_results": access.ASSIGNMENT_OWNER,
    "teacher.teacher_assignment_results_export": access.ASSIGNMENT_OWNER,
    "teacher.teacher_assignment_qr": access.ASSIGNMENT_OWNER,
    "teacher.teacher_assignment_edit": access.ASSIGNMENT_OWNER,
    "teacher.teacher_assignment_duplicate": access.ASSIGNMENT_OWNER,
    # Assignment lifecycle (PR #326): manual close, owner or admin like edit and duplicate.
    "teacher.teacher_assignment_close": access.ASSIGNMENT_OWNER,
    # Accounts PR8: same route policy; ownership follows the submission's assignment.
    "practical.teacher_practical_submissions": access.ASSIGNMENT_OWNER,
    "practical.teacher_practical_submission_file_download": access.ASSIGNMENT_OWNER,
    "quiz.quiz_attempt": access.ATTEMPT_OWNER,
    "quiz.quiz_result": access.ATTEMPT_OWNER,
    "practical.quiz_practical_attempt": access.ATTEMPT_OWNER,
    "practical.quiz_practical_upload": access.ATTEMPT_OWNER,
    # Accounts PR 4.
    "account.login": access.PUBLIC,
    "account.logout": access.PUBLIC,
    # Admin TOTP (PR T): public because a pending second factor is not a principal.
    "account.login_totp": access.PUBLIC,
    "account.password": access.SIGNED_IN,
    # Accounts phase 2: the signed-in account's own attempts (any role).
    "quiz.my_attempts": access.QUIZ_TAKER,
    # Google sign-in (PR #331). The callback is public and has its own integrity
    # mechanism; linking needs an account, and the view refuses an admin itself.
    "account.overview": access.SIGNED_IN,
    "oidc.google_start": access.PUBLIC,
    "oidc.google_callback": access.PUBLIC,
    "oidc.google_link": access.SIGNED_IN,
    "oidc.google_link_confirm": access.SIGNED_IN,
    "oidc.google_link_cancel": access.SIGNED_IN,
}

# Who makes the request. The first three have no account: "historical_*" are
# anonymous cookies still holding keys of the removed shared-password logins and
# profile switch, which must behave exactly like "anonymous".
HISTORICAL_SESSIONS = {
    "historical_profile": {"ui_profile": "admin"},
    "historical_tester": {"tester_authenticated": True, "ui_profile": "tester"},
    "historical_admin": {"tester_authenticated": True, "admin_authenticated": True, "ui_profile": "admin"},
}
ANONYMOUS_PRINCIPALS = ("anonymous", *HISTORICAL_SESSIONS)
ACCOUNT_ROLES = ("student", "teacher", "admin")
PRINCIPALS = (*ANONYMOUS_PRINCIPALS, *ACCOUNT_ROLES)
# Who the D1 matrix lets through, written out independently of web.access.
SIGNED_IN_PRINCIPALS = {"student", "teacher", "admin"}
QUIZ_TAKER_PRINCIPALS = {"student", "teacher", "admin"}
TEACHER_PRINCIPALS = {"teacher", "admin"}
ADMIN_PRINCIPALS = {"admin"}

SAMPLE_VIEW_ARGS = {
    "grade": 8,
    "slug": "sample-section",
    "subject_slug": "information-technologies",
    "topic_group_slug": "sample-group",
    "topic_slug": "sample-topic",
    "source_slug": "may_2025_v2",
    "assignment_id": 1,
    "public_token": "A" * 43,
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

    def test_signed_in_requires_an_account_id_not_a_capability(self):
        self.assertFalse(access.is_route_allowed(access.SIGNED_IN, access.ANONYMOUS))
        self.assertFalse(access.is_route_allowed(
            access.SIGNED_IN, access.Principal(is_teacher=True, is_admin=True)))
        self.assertTrue(access.is_route_allowed(
            access.SIGNED_IN, access.account_principal(7, "student", "Ученик")))

    def test_attempt_owner_passes_the_route_and_is_decided_in_the_view(self):
        # Object-level ownership stays in the quiz blueprint (404, not 403).
        self.assertTrue(access.is_route_allowed(access.ATTEMPT_OWNER, access.ANONYMOUS))

    def test_assignment_owner_admits_admins_and_teacher_accounts_only(self):
        for name, principal, allowed in (
            ("anonymous", access.ANONYMOUS, False),
            ("teacher capability without an account id",
             access.Principal(is_teacher=True, is_admin=False), False),
            ("student", access.account_principal(7, "student", "Ученик"), False),
            ("teacher", access.account_principal(8, "teacher", "Учител"), True),
            ("admin", access.account_principal(9, "admin", "Админ"), True),
        ):
            with self.subTest(principal=name):
                self.assertEqual(access.is_route_allowed(access.ASSIGNMENT_OWNER, principal), allowed)
                self.assertEqual(access.can_list_assignments(principal), allowed)

    def test_the_profile_switch_level_no_longer_exists(self):
        self.assertFalse(hasattr(access, "PROFILE_SWITCH"))
        self.assertNotIn("profile_switch", access.ACCESS_LEVELS)
        with self.assertRaises(ValueError):
            access.access_policy("profile_switch")
        # As an undeclared level it is denied, to an admin too.
        self.assertFalse(access.is_route_allowed("profile_switch", access.account_principal(9, "admin", "Админ")))

    def test_undeclared_and_unknown_policies_are_denied(self):
        admin = access.account_principal(9, "admin", "Админ")
        for level in (None, "", "teacher_or_whatever"):
            with self.subTest(level=level):
                self.assertFalse(access.is_route_allowed(level, admin))

    def test_anonymous_satisfies_only_public_and_object_level_policies(self):
        for level in access.ACCESS_LEVELS:
            with self.subTest(level=level):
                self.assertEqual(access.is_route_allowed(level, access.ANONYMOUS),
                                 level in (access.PUBLIC, access.ATTEMPT_OWNER))

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

    def views_declaring(self, mechanism):
        return sorted(
            endpoint for endpoint, view in self.app.view_functions.items()
            if request_integrity.integrity_policy_for_view(view) == mechanism
        )

    def test_exactly_one_view_declares_oauth_state_and_it_accepts_no_unsafe_method(self):
        # OAUTH_STATE is an additional mechanism for one safe-method route, never an
        # alternative to SAME_ORIGIN for an unsafe one: the callback passes through no
        # exemption of the same-origin check, because it accepts no unsafe method.
        self.assertEqual(self.views_declaring(request_integrity.OAUTH_STATE), ["oidc.google_callback"])
        rules = [rule for rule in application_rules(self.app) if rule.endpoint == "oidc.google_callback"]
        self.assertEqual(len(rules), 1)
        self.assertEqual(rules[0].methods, {"GET", "HEAD", "OPTIONS"})
        self.assertFalse(rules[0].methods & request_integrity.UNSAFE_METHODS)

    def test_the_provider_signature_mechanism_stays_unused(self):
        self.assertEqual(self.views_declaring(request_integrity.PROVIDER_SIGNATURE), [])

    def test_the_oauth_state_verifier_imports_no_blueprint(self):
        tree = ast.parse(Path(ROOT, "web/oauth_state.py").read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add(node.module or "")
        self.assertNotIn("web.app", imported)
        self.assertFalse({name for name in imported if name.startswith("web.blueprints")})
        # It is registered in web.app between the same-origin check and the account
        # loader, and it is the function of that module.
        hooks = self.app.before_request_funcs[None]
        names = [func.__name__ for func in hooks]
        self.assertIs(hooks[names.index("verify_oauth_state")], oauth_state.verify_oauth_state)
        self.assertLess(names.index("protect_same_origin_unsafe_methods"), names.index("verify_oauth_state"))
        self.assertLess(names.index("verify_oauth_state"), names.index("load_current_user"))

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

def expected_decision(policy, principal_name):
    """Whether the D1 matrix lets ``principal_name`` through, without consulting web.access."""
    if policy in (access.PUBLIC, access.ATTEMPT_OWNER):
        return True
    if policy == access.SIGNED_IN:
        return principal_name in SIGNED_IN_PRINCIPALS
    if policy == access.QUIZ_TAKER:
        return principal_name in QUIZ_TAKER_PRINCIPALS
    if policy in (access.TEACHER, access.ASSIGNMENT_OWNER):
        return principal_name in TEACHER_PRINCIPALS
    return policy == access.ADMIN and principal_name in ADMIN_PRINCIPALS


def session_for(db_path, principal_name):
    """The session state of ``principal_name``: historical keys, or a new account's identity."""
    if principal_name == "anonymous":
        return {}
    if principal_name in HISTORICAL_SESSIONS:
        return dict(HISTORICAL_SESSIONS[principal_name])
    account = create_account(db_path, principal_name)
    return {"uid": account["id"], "sk": account["session_key"], "iat": int(time.time())}


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
            response = self.app.preprocess_request()
            if response is not None:
                response = self.app.make_response(response)
            return response

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
        db_path = self.app.config["DB_PATH"]
        sessions = {name: session_for(db_path, name) for name in PRINCIPALS}
        for rule in application_rules(self.app):
            policy = EXPECTED_ACCESS_MATRIX[rule.endpoint]
            method = "GET" if "GET" in rule.methods else "POST"
            with self.app.test_request_context("/"):
                from flask import url_for

                path = url_for(rule.endpoint, **view_args_for(rule))
            for name in PRINCIPALS:
                with self.subTest(endpoint=rule.endpoint, principal=name):
                    response = self._enforce(path, method, sessions[name])
                    if expected_decision(policy, name):
                        self.assertIsNone(response)
                    elif name in ACCOUNT_ROLES:
                        # Signed in without the role: refused, never sent to a login.
                        self.assertEqual(response.status_code, 403)
                        self.assertNotIn("Location", response.headers)
                    else:
                        # No account (historical keys included): the one account login.
                        self.assertEqual(response.status_code, 302)
                        self.assertEqual(response.headers["Location"], f"/login?next={path}")

    def test_historical_session_keys_decide_exactly_like_no_session(self):
        for rule in application_rules(self.app):
            policy = EXPECTED_ACCESS_MATRIX[rule.endpoint]
            for name in HISTORICAL_SESSIONS:
                with self.subTest(endpoint=rule.endpoint, principal=name):
                    self.assertEqual(expected_decision(policy, name), expected_decision(policy, "anonymous"))
                    self.assertEqual(expected_decision(policy, name),
                                     policy in (access.PUBLIC, access.ATTEMPT_OWNER))

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

    def _client(self, principal_name):
        client = self.app.test_client()
        with client.session_transaction() as sess:
            sess.update(session_for(self.app.config["DB_PATH"], principal_name))
        return client

    def assertDenied(self, principal_name, path, body=None):
        """Denied the way the contract says: login for no account, 403 for an account."""
        response = self._client(principal_name).get(path)
        if principal_name in ACCOUNT_ROLES:
            self.assertEqual(response.status_code, 403)
            self.assertNotIn("Location", response.headers)
        else:
            self.assertEqual(response.status_code, 302)
            self.assertEqual(response.headers["Location"], f"/login?next={path}")
        if body is not None:
            self.assertNotIn(body, response.data)

    def test_undeclared_view_is_denied_to_every_principal(self):
        for name in PRINCIPALS:
            with self.subTest(principal=name):
                self.assertDenied(name, "/__test__/undeclared-report", b"undeclared view body")

    def test_declared_admin_view_with_a_public_name_stays_admin_only(self):
        for name in (*ANONYMOUS_PRINCIPALS, "student", "teacher"):
            with self.subTest(principal=name):
                self.assertDenied(name, "/__test__/declared-admin", b"declared admin view body")
        response = self._client("admin").get("/__test__/declared-admin")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, b"declared admin view body")

    def test_alias_with_a_public_name_cannot_bypass_an_admin_view(self):
        for name in (*ANONYMOUS_PRINCIPALS, "student", "teacher"):
            with self.subTest(principal=name):
                self.assertDenied(name, "/__test__/alias/dashboard")
        response = self._client("admin").get("/__test__/alias/dashboard")
        self.assertEqual(response.status_code, 200)

    def test_alias_with_an_admin_name_keeps_the_teacher_policy(self):
        for name in (*ANONYMOUS_PRINCIPALS, "student"):
            with self.subTest(principal=name):
                self.assertDenied(name, "/__test__/alias/new")
        self.assertEqual(self._client("teacher").get("/__test__/alias/new").status_code, 200)


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

    def _as(self, who):
        self.client = client_as(self.app, self.app.config["DB_PATH"], who)

    def _as_teacher(self):
        self._as("teacher")

    def _as_admin(self):
        self._as("admin")

    def assert_sent_to_login(self, response, path):
        self.assertEqual(response.status_code, 302, path)
        self.assertEqual(response.headers["Location"], f"/login?next={path}")

    def test_public_route_stays_public(self):
        self.assertEqual(self.client.get("/").status_code, 200)
        self.assertEqual(self.client.get("/healthz").status_code, 200)

    def test_teacher_route_allows_a_teacher_account_and_denies_anonymous(self):
        self.assert_sent_to_login(self.client.get("/teacher/new"), "/teacher/new")
        self._as("student")
        self.assertEqual(self.client.get("/teacher/new").status_code, 403)
        self._as_teacher()
        self.assertEqual(self.client.get("/teacher/new").status_code, 200)

    def test_admin_route_refuses_a_teacher_and_allows_an_admin(self):
        self._as_teacher()
        for path in ("/teacher", "/dzi"):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 403, path)
                self.assertNotIn("Location", response.headers)
        # A teacher account lists its own assignments (PR7) and practical work (PR8);
        # an object it does not own is not found rather than forbidden.
        for path in ("/teacher/assignments", "/teacher/practical-submissions"):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 200, path)
        self.assertEqual(
            self.client.get("/teacher/practical/submission-file/1/download").status_code, 404)

        self._as_admin()
        for path in ("/teacher", "/teacher/assignments", "/teacher/practical-submissions", "/dzi"):
            with self.subTest(path=path, principal="admin"):
                self.assertEqual(self.client.get(path).status_code, 200, path)

    def test_historical_session_keys_alone_grant_no_access(self):
        for who, state in HISTORICAL_SESSIONS.items():
            client = self.app.test_client()
            with client.session_transaction() as sess:
                sess.update(state)
            for path in ("/dzi", "/teacher", "/teacher/new", "/teacher/assignments",
                         "/teacher/practical-submissions", "/teacher/practical/submission-file/1/download",
                         "/admin/open-question-candidates", "/account/password"):
                with self.subTest(principal=who, path=path):
                    self.assert_sent_to_login(client.get(path), path)

    def test_removed_profile_switch_is_not_found(self):
        for who in ("anonymous", "teacher", "admin"):
            self._as(who)
            for path in ("/profile/admin", "/profile/tester"):
                with self.subTest(principal=who, path=path):
                    self.assertEqual(self.client.get(path).status_code, 404)

    def test_attempt_routes_answer_404_for_a_foreign_attempt(self):
        for path in (
            "/quiz/attempt/999999",
            "/quiz/attempt/999999/result",
            "/quiz/attempt/999999/practical/may_2025_v2",
        ):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 404, path)

    def test_unsafe_requests_still_require_same_origin(self):
        self._as_teacher()
        data = {"section_id": str(self.section_id), "question_count": "1", "time_limit_minutes": ""}
        rejected = self.client.post(
            "/teacher/new", data={**NO_DEADLINE_FORM, **data}, headers={"Origin": "http://evil.example"})
        self.assertEqual(rejected.status_code, 403)

        accepted = self.client.post(
            "/teacher/new", data={**NO_DEADLINE_FORM, **data}, headers={"Origin": "http://localhost"})
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
        anonymous = self.client.post("/teacher/new", data={**NO_DEADLINE_FORM, **data}, headers=same_origin)
        self.assert_sent_to_login(anonymous, "/teacher/new")

        self._as_teacher()
        teacher = self.client.post("/teacher/dzi-training", data={}, headers=same_origin)
        self.assertEqual(teacher.status_code, 403)
        self.assertNotIn("Location", teacher.headers)

    def test_historical_session_keys_gain_nothing_on_unsafe_requests(self):
        same_origin = {"Origin": "http://localhost"}
        data = {"section_id": str(self.section_id), "question_count": "1", "time_limit_minutes": ""}
        conn = web_app.quiz_db()
        try:
            before = conn.execute("SELECT COUNT(*) FROM quiz_assignments").fetchone()[0]
        finally:
            conn.close()
        for who, state in HISTORICAL_SESSIONS.items():
            with self.client.session_transaction() as sess:
                sess.clear()
                sess.update(state)
            for path, payload in (("/teacher/dzi-training", {}), ("/teacher/new", data)):
                with self.subTest(principal=who, path=path):
                    self.assert_sent_to_login(
                        self.client.post(path, data=payload, headers=same_origin), path)
        conn = web_app.quiz_db()
        try:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM quiz_assignments").fetchone()[0], before)
        finally:
            conn.close()

    def test_cross_origin_unsafe_request_is_refused_before_authorization(self):
        # Integrity runs first: an unauthorized cross-origin post is a 403,
        # not a login redirect.
        response = self.client.post(
            "/teacher/dzi-training", data={}, headers={"Origin": "http://evil.example"})
        self.assertEqual(response.status_code, 403)


if __name__ == "__main__":
    unittest.main()
