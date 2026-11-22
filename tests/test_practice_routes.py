"""The default-off student practice routes (C2, roadmap PR #371): ``web.blueprints.practice``.

Two worlds, kept apart on purpose:

* **Disabled (this interpreter).** The build constant ``practice.ROUTES_ACTIVATION`` is ``False`` and
  nothing overrides it, so the genuine ``web.app`` has no ``/practice`` rule, endpoint or blueprint
  and answers every such URL with the generic 404. The ordinary URL map, the existing gates and the
  legacy numeric quiz 404 are pinned. The read-only attempt helper of the service is tested here too:
  it needs no route.
* **Armed (a separate interpreter).** An interpreter is started that sets the constant to ``True``
  *before* it imports the genuine ``web.app`` (through ``tests.support``), then runs the classes
  named in ``ARMED_CLASSES`` against the real application: its real request-integrity and access
  hooks, signed-in test clients over disposable migrations + canonical databases, fixture accounts
  only. ``ArmedInterpreterTest`` runs that interpreter and checks its counts; the armed classes
  exist only inside it (``armed_only``), so this file never leaves skipped duplicates behind.

Nothing here reads, copies, hashes or lists ``data/questions.db`` or its sidecars (every database is a
temporary clone of the template, and the child interpreters are given ``DZI_DB=:memory:``).
"""

import html as html_module
import json
import os
import re
import secrets
import sqlite3
import subprocess
import sys
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from flask import g

from src.subject_browsing import public_slug
from tests.auth_helpers import set_account_session
from tests.support import ROOT, IsolatedAppTestCase, isolated_app, web_app
from tests.test_practice_start_service import STAMP, PracticeCase, Scope  # noqa: F401  (fixtures only)
from web import db as web_db
from web.access import ANONYMOUS, account_principal, policy_for_view
from web.blueprints import browsing
from web.request_integrity import integrity_policy_for_view
from web.services import accounts as accounts_service
from web.services import practice
from web.services import profiles, recovery, registration
from web.services import google_oidc
from web.services.quiz import quiz_shuffle_options

ARMED = practice.ROUTES_ACTIVATION is True

SAME = {"Origin": "http://localhost"}
CROSS = {"Origin": "http://evil.example"}

# The classes the separate, armed interpreter runs, and how many tests they hold. The count is
# pinned so that a deleted or renamed test cannot make the armed run silently smaller.
ARMED_CLASSES = (
    "ArmedRegistrationTest",
    "StartRouteTest",
    "AttemptRouteTest",
    "SubmitRouteTest",
    "ResultRouteTest",
    "FlowAndPriorRoutesTest",
    "BrowseEntryTest",
    "BrowseProgressTest",
    "ConfirmRouteTest",
    "SignInReturnTest",
)
ARMED_TEST_COUNT = 114

IT_SLUG = "information-technologies"

EXPECTED_RULES = {
    ("/practice/section/<int:section_id>/start", ("GET", "HEAD", "OPTIONS", "POST"), "practice.start_section"),
    ("/practice/section/<int:section_id>/topic/<int:topic_id>/start", ("GET", "HEAD", "OPTIONS", "POST"),
     "practice.start_topic"),
    ("/practice/session/<int:session_id>", ("GET", "HEAD", "OPTIONS", "POST"), "practice.attempt"),
    ("/practice/session/<int:session_id>/result", ("GET", "HEAD", "OPTIONS"), "practice.result"),
}


def armed_only(cls):
    """Keep ``cls`` only in the armed interpreter: elsewhere it is not defined, so not collected."""
    return cls if ARMED else None


def disabled_only(cls):
    return None if ARMED else cls


# ====================================================================== interpreters
# Dumps the URL map of the genuine application, armed (``arm``) or not, with each view's policies.
_DUMP = """
import json, os, sys
import web.services.practice as practice
if sys.argv[1] == "arm":
    practice.ROUTES_ACTIVATION = True
os.environ["DZI_DB"] = ":memory:"
from web import app as web_app
from web.access import policy_for_view
from web.request_integrity import integrity_policy_for_view
rows = []
for rule in web_app.app.url_map.iter_rules():
    view = web_app.app.view_functions[rule.endpoint]
    rows.append([rule.rule, sorted(rule.methods), rule.endpoint, policy_for_view(view),
                 integrity_policy_for_view(view)])
print(json.dumps(sorted(rows)))
"""

# Arms the constant, imports the genuine application through the test support, and runs the armed classes.
_RUN_ARMED = """
import sys, unittest
import web.services.practice as practice
practice.ROUTES_ACTIVATION = True
from tests import test_practice_routes as module
assert module.ARMED, "the constant was not armed before the application was imported"
suite = unittest.defaultTestLoader.loadTestsFromNames(list(module.ARMED_CLASSES), module)
result = unittest.TextTestRunner(stream=sys.stderr, verbosity=2).run(suite)
print("RAN", result.testsRun, "FAILURES", len(result.failures), "ERRORS", len(result.errors),
      "SKIPPED", len(result.skipped))
sys.exit(0 if result.wasSuccessful() else 1)
"""

_ENVIRONMENT_NAMES = (
    "DZI_PRACTICE", "DZI_PRACTICE_ROUTES", "DZI_PRACTICE_ENABLED", "DZI_PRACTICE_ROUTES_ACTIVATION",
    "DZI_ROUTES_ACTIVATION", "PRACTICE_ROUTES_ACTIVATION", "ROUTES_ACTIVATION", "LEARNPILOT_PRACTICE",
    "LEARNPILOT_PRACTICE_ROUTES", "FLASK_PRACTICE", "DZI_ENABLE_PRACTICE",
)


def _child_environment(**extra):
    environment = dict(os.environ)
    environment.update(PYTHONDONTWRITEBYTECODE="1", DZI_DB=":memory:")
    environment.update(extra)
    return environment


def _run_child(code, *arguments, environment=None):
    # Ordinary site initialization (no -S, no -I): the child is the real, site-enabled interpreter.
    return subprocess.run(
        [sys.executable, "-B", "-c", code, *arguments], cwd=ROOT, env=environment or _child_environment(),
        capture_output=True, text=True, timeout=900)


def _url_map(mode, environment=None):
    completed = _run_child(_DUMP, mode, environment=environment)
    if completed.returncode != 0:
        raise AssertionError(f"url-map interpreter ({mode}) failed:\n{completed.stderr[-3000:]}")
    return json.loads(completed.stdout.strip().splitlines()[-1])


def _live_url_map():
    rows = []
    for rule in web_app.app.url_map.iter_rules():
        view = web_app.app.view_functions[rule.endpoint]
        rows.append([rule.rule, sorted(rule.methods), rule.endpoint, policy_for_view(view),
                     integrity_policy_for_view(view)])
    return sorted(rows)


# ====================================================================== disabled build
@disabled_only
class DisabledBuildTest(IsolatedAppTestCase):
    def test_the_build_constant_is_off_and_assigned_exactly_once(self):
        self.assertIs(practice.ROUTES_ACTIVATION, False)
        holders = {}
        for path in sorted((ROOT / "web").rglob("*.py")) + sorted((ROOT / "src").rglob("*.py")):
            text = path.read_text(encoding="utf-8")
            if "ROUTES_ACTIVATION" in text:
                holders[path.relative_to(ROOT).as_posix()] = [
                    line.strip() for line in text.splitlines() if re.search(r"ROUTES_ACTIVATION\s*=[^=]", line)]
        self.assertEqual(sorted(holders), ["web/app.py", "web/blueprints/practice.py", "web/services/practice.py"])
        self.assertEqual(holders["web/services/practice.py"], ["ROUTES_ACTIVATION = False"])
        self.assertEqual(holders["web/app.py"], [])
        self.assertEqual(holders["web/blueprints/practice.py"], [])

    def test_nothing_reads_the_constant_from_settings(self):
        # The one reader is the registration in web.app, and it reads the attribute only.
        source = (ROOT / "web" / "app.py").read_text(encoding="utf-8")
        self.assertEqual(source.count("practice_service.ROUTES_ACTIVATION"), 1)
        for path in (ROOT / "web" / "services" / "practice.py", ROOT / "web" / "blueprints" / "practice.py"):
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("os.environ", text)
            self.assertNotIn("getenv", text)
        self.assertRegex(source, r"if practice_service\.ROUTES_ACTIVATION:\n    from web\.blueprints\.practice import "
                                 r"practice_bp\n\n    app\.register_blueprint\(practice_bp\)")

    def test_the_ordinary_application_has_no_practice_route_endpoint_or_blueprint(self):
        rules = [rule.rule for rule in web_app.app.url_map.iter_rules()]
        self.assertFalse([rule for rule in rules if rule.startswith("/practice")])
        self.assertFalse([endpoint for endpoint in web_app.app.view_functions if endpoint.startswith("practice")])
        self.assertNotIn("practice", web_app.app.blueprints)
        self.assertNotIn("web.blueprints.practice", sys.modules)

    def test_every_practice_url_is_the_generic_404_for_everyone(self):
        paths = ("/practice", "/practice/", "/practice/section/1/start", "/practice/section/1/topic/2/start",
                 "/practice/session/1", "/practice/session/1/result")
        client = web_app.app.test_client()
        for path in paths:
            for method in ("get", "head", "post", "put", "delete"):
                with self.subTest(path=path, method=method):
                    response = getattr(client, method)(path, headers=SAME)
                    self.assertEqual(response.status_code, 404)
                    self.assertNotIn("Location", response.headers)

    def test_existing_gates_keep_their_values(self):
        for gate in (profiles.PRODUCTION_ACTIVATION, recovery.PRODUCTION_ACTIVATION,
                     registration.PRODUCTION_ACTIVATION, registration.TEACHER_ACTIVATION,
                     google_oidc.PRODUCTION_ACTIVATION, practice.ROUTES_ACTIVATION):
            self.assertIs(gate, False)

    def test_the_legacy_numeric_quiz_entry_is_still_a_generic_404(self):
        rules = [rule.rule for rule in web_app.app.url_map.iter_rules()]
        self.assertNotIn("/quiz/<int:assignment_id>", rules)
        client = web_app.app.test_client()
        for method in ("get", "post"):
            response = getattr(client, method)("/quiz/1", headers=SAME)
            self.assertEqual(response.status_code, 404)
            self.assertNotIn("Location", response.headers)


@disabled_only
class DisabledBuildAccountsTest(PracticeCase):
    """Like the class above, but with real accounts in a disposable database."""

    def setUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        super().setUp()
        stack.enter_context(patch.dict(web_app.app.config, {"TESTING": True, "DB_PATH": str(self.db_path)}))
        stack.enter_context(patch.object(web_app, "QUIZ_DB_PATH", self.db_path))

    def test_signed_in_requests_to_practice_urls_are_404_and_write_nothing(self):
        scope = self.make_scope(6)
        before = self.table_digests()
        for who in (self.student, self.teacher, self.admin):
            client = _client_for(self, who)
            for method, path in (("post", f"/practice/section/{scope.section_id}/start"),
                                 ("get", "/practice/session/1"), ("get", "/practice/session/1/result")):
                with self.subTest(role=who.role, path=path):
                    response = getattr(client, method)(path, headers=SAME)
                    self.assertEqual(response.status_code, 404)
                    self.assertNotIn("Location", response.headers)
            self.assertEqual(getattr(client, "post")("/quiz/1", headers=SAME).status_code, 404)
        self.assertEqual(self.table_digests(), before)


def _client_for(case, who=None):
    client = web_app.app.test_client()
    if who is not None:
        key = case.conn.execute("SELECT session_key FROM users WHERE id = ?", (who.user_id,)).fetchone()[0]
        set_account_session(client, who.user_id, key)
    return client


# ====================================================================== the attempt helper
@disabled_only
class AttemptHelperTest(PracticeCase):
    """``practice.load_practice_attempt``: owner-scoped, read-only, no correct markers."""

    def started(self, who=None, scope=None, topic_id=None, good=8):
        scope = scope or self.make_scope(good)
        result = self.start(who or self.student, scope, topic_id)
        return scope, result

    def test_an_open_session_is_loaded_in_stored_order_and_seed_without_correct_markers(self):
        scope = self.make_scope(8)
        ids = scope.all_question_ids
        plan = [ids[3], ids[0], ids[5], ids[1]]
        session_id = self.insert_session(self.student, scope, plan, seed="stored-seed-77")
        view = practice.load_practice_attempt(self.conn, self.student, session_id)
        self.assertEqual(view.outcome, practice.OUTCOME_OPEN)
        self.assertTrue(view.is_open)
        self.assertEqual((view.session_id, view.section_id, view.topic_id), (session_id, scope.section_id, None))
        self.assertEqual([question["id"] for question in view.questions], plan)
        self.assertEqual((view.planned_question_count, view.skipped_question_count), (4, 0))
        for question in view.questions:
            self.assertNotIn("is_correct", question)
            expected = quiz_shuffle_options("stored-seed-77", question["id"], [
                {"option_letter": letter, "option_text": f"Synthetic option {letter}"} for letter in "ABCD"])
            self.assertEqual([option["option_letter"] for option in question["options"]],
                             [option["option_letter"] for option in expected])
            for option in question["options"]:
                self.assertNotIn("is_correct", option)

    def test_unrenderable_stored_questions_are_counted_as_skipped(self):
        scope = self.make_scope(8)
        ids = scope.all_question_ids
        session_id = self.insert_session(self.student, scope, ids[:4])
        self.break_question(ids[1])
        self.conn.commit()
        view = practice.load_practice_attempt(self.conn, self.student, session_id)
        self.assertEqual([question["id"] for question in view.questions], [ids[0], ids[2], ids[3]])
        self.assertEqual((view.planned_question_count, view.skipped_question_count), (4, 1))

    def test_missing_foreign_teacher_and_admin_are_one_indistinguishable_outcome(self):
        scope, started = self.started()
        expected = practice.PracticeAttempt(practice.OUTCOME_NOT_FOUND)
        for who in (self.other_student, self.teacher, self.admin):
            self.assertEqual(practice.load_practice_attempt(self.conn, who, started.session_id), expected)
        self.assertEqual(practice.load_practice_attempt(self.conn, self.student, 987654), expected)

    def test_a_submitted_an_abandoned_and_a_stale_session_of_the_owner(self):
        scope, started = self.started(good=8)
        done = practice.submit_practice(self.conn, self.student, started.session_id,
                                        {started.question_ids[0]: "A"})
        self.assertTrue(done.submitted)
        view = practice.load_practice_attempt(self.conn, self.student, started.session_id)
        self.assertEqual(view, practice.PracticeAttempt(practice.OUTCOME_ALREADY_SUBMITTED, started.session_id,
                                                        scope.section_id, None))
        old = self.insert_session(self.other_student, scope, scope.all_question_ids[:3], abandoned=STAMP)
        view = practice.load_practice_attempt(self.conn, self.other_student, old)
        self.assertEqual(view, practice.PracticeAttempt(practice.OUTCOME_ABANDONED, old, scope.section_id, None))
        stale = self.insert_session(self.teacher, scope, [987001, 987002])
        view = practice.load_practice_attempt(self.conn, self.teacher, stale)
        self.assertEqual(view, practice.PracticeAttempt(practice.OUTCOME_STALE, None, scope.section_id, None))

    def test_an_unavailable_scope_and_a_malformed_plan(self):
        scope = self.make_scope(8)
        session_id = self.insert_session(self.student, scope, scope.all_question_ids[:3])
        self.conn.execute("UPDATE curriculum_sections SET section_type = 'dzi_training' WHERE id = ?",
                          (scope.section_id,))
        self.conn.commit()
        view = practice.load_practice_attempt(self.conn, self.student, session_id)
        self.assertEqual(view.outcome, practice.OUTCOME_SCOPE_UNAVAILABLE)
        self.assertEqual(view.questions, ())
        other = self.make_scope(8)
        bad = self.insert_session(self.teacher, other, "not json")
        with self.assertRaises(practice.PracticePlanError):
            practice.load_practice_attempt(self.conn, self.teacher, bad)

    def test_validation_and_accounts(self):
        scope, started = self.started()
        for bad in (0, -1, True, "1", None, 2 ** 63):
            with self.assertRaises(ValueError):
                practice.load_practice_attempt(self.conn, self.student, bad)
        with self.assertRaises(practice.PracticeAccessError):
            practice.load_practice_attempt(self.conn, ANONYMOUS, started.session_id)
        inactive = self.add_account("student", active=False)
        self.conn.commit()
        self.assertEqual(practice.load_practice_attempt(self.conn, inactive, started.session_id),
                         practice.PracticeAttempt(practice.OUTCOME_ACCOUNT_UNAVAILABLE))

    def test_reading_writes_nothing_even_on_a_query_only_connection(self):
        scope, started = self.started()
        before = self.table_digests()
        reader = web_db.connect_db(self.db_path, query_only=True)
        self.addCleanup(reader.close)
        changes = reader.total_changes
        view = practice.load_practice_attempt(reader, self.student, started.session_id)
        self.assertTrue(view.is_open)
        self.assertEqual(reader.total_changes, changes)
        self.assertFalse(reader.in_transaction)
        self.assertEqual(self.table_digests(), before)

    def test_a_caller_transaction_is_reused_and_left_open(self):
        scope, started = self.started()
        self.conn.execute("BEGIN")
        try:
            view = practice.load_practice_attempt(self.conn, self.student, started.session_id)
            self.assertTrue(view.is_open)
            self.assertTrue(self.conn.in_transaction)
        finally:
            self.conn.execute("ROLLBACK")

    def test_a_topic_session_shows_only_its_topic(self):
        scope = self.make_scope(6, topics=2)
        started = self.start(self.student, scope, scope.topic_ids[1])
        view = practice.load_practice_attempt(self.conn, self.student, started.session_id)
        self.assertEqual(view.topic_id, scope.topic_ids[1])
        self.assertTrue({question["id"] for question in view.questions} <= set(scope.question_ids[1]))


# ====================================================================== the separate interpreter
@disabled_only
class ArmedInterpreterTest(unittest.TestCase):
    """Runs the armed classes in a separate interpreter and compares what arming changes."""

    @classmethod
    def setUpClass(cls):
        cls.plain = _url_map("plain")
        cls.armed = _url_map("arm")

    def test_the_live_ordinary_map_is_the_unarmed_map(self):
        # Other test modules add test-only rules to the shared in-process application, so the live map
        # may hold more than the pristine one; it never holds less, and never a practice rule.
        live = _live_url_map()
        self.assertEqual([row for row in self.plain if row not in live], [])
        self.assertEqual([row for row in live if row[0].startswith("/practice")], [])

    def test_arming_adds_exactly_the_four_practice_rules_and_changes_nothing_else(self):
        added = [row for row in self.armed if row not in self.plain]
        self.assertEqual({(row[0], tuple(row[1]), row[2]) for row in added}, EXPECTED_RULES)
        self.assertEqual([row for row in self.armed if not row[0].startswith("/practice")], self.plain)
        for row in added:
            self.assertEqual(row[3], "signed_in")
            self.assertEqual(row[4], None if row[0].endswith("/result") else "same_origin")

    def test_no_environment_variable_arms_the_routes(self):
        environment = _child_environment(**{name: "1" for name in _ENVIRONMENT_NAMES},
                                         DZI_PROFILES_ENABLED="1", DZI_SESSION_COOKIE_SECURE="0",
                                         DZI_SECRET_KEY="x" * 32)
        rows = _url_map("plain", environment=environment)
        self.assertFalse([row for row in rows if row[0].startswith("/practice")])
        self.assertEqual([row for row in rows if not row[0].startswith("/practice")], self.plain)

    def test_the_armed_suite_passes_in_a_separate_interpreter(self):
        completed = _run_child(_RUN_ARMED)
        tail = completed.stderr[-6000:]
        self.assertEqual(completed.returncode, 0, tail)
        summary = completed.stdout.strip().splitlines()[-1]
        self.assertEqual(summary, f"RAN {ARMED_TEST_COUNT} FAILURES 0 ERRORS 0 SKIPPED 0", tail)


# ====================================================================== armed fixtures
class RouteCase(PracticeCase):
    """A disposable database wired into the genuine application, plus synthetic scopes and accounts."""

    def setUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        root = stack.enter_context(isolated_app())
        self.directory = root
        self.db_path = root / "questions.db"
        self.conn = self.connect()
        self.counter = 1000
        self.student = self.add_account("student")
        self.other_student = self.add_account("student")
        self.teacher = self.add_account("teacher")
        self.admin = self.add_account("admin")
        self.conn.commit()

    def client(self, who=None):
        return _client_for(self, who)

    def post(self, client, path, data=None, headers=SAME):
        return client.post(path, data=data or {}, headers=headers)

    def start_url(self, scope, topic_index=None):
        if topic_index is None:
            return f"/practice/section/{scope.section_id}/start"
        return f"/practice/section/{scope.section_id}/topic/{scope.topic_ids[topic_index]}/start"

    def open_via_route(self, who, scope, topic_index=None):
        """Start through the route; the id of the session its 303 points at."""
        response = self.post(self.client(who), self.start_url(scope, topic_index))
        self.assertEqual(response.status_code, 303, response.get_data(as_text=True)[:400])
        match = re.fullmatch(r"/practice/session/(\d+)", response.headers["Location"])
        self.assertIsNotNone(match, response.headers["Location"])
        return int(match.group(1))

    def stored(self, session_id):
        with self.raw() as conn:
            row = conn.execute("SELECT * FROM practice_sessions WHERE id = ?", (session_id,)).fetchone()
            answers = conn.execute("SELECT question_id, chosen_letter, is_correct FROM practice_answers "
                                   "WHERE session_id = ? ORDER BY question_id", (session_id,)).fetchall()
        return dict(row), [tuple(answer) for answer in answers]

    def plan_of(self, session_id):
        return json.loads(self.stored(session_id)[0]["question_ids_json"])

    def answer_all(self, session_id, letter="A", *, only=None):
        fields = {f"q_{question_id}": letter for question_id in self.plan_of(session_id)}
        if only is not None:
            fields = {name: fields[name] for name in only if name in fields}
        return fields

    def assert_login_redirect(self, response, path):
        self.assertEqual(response.status_code, 302)
        location = urlparse(response.headers["Location"])
        self.assertEqual(location.path, "/login")
        self.assertEqual(parse_qs(location.query), {"next": [path]})

    def page(self, response):
        return response.get_data(as_text=True)

    def questions_in(self, html):
        return re.findall(r'<article class="card question-card quiz-card" data-question-id="(\d+)">(.*?)</article>',
                          html, re.S)

    def assert_plan_refusal_logged(self, logs, endpoint):
        """One log line: the endpoint and the exception class, with no id, label or plan content."""
        self.assertEqual([record.getMessage() for record in logs.records],
                         [f"Practice plan refused: endpoint={endpoint} error=PracticePlanError"])
        self.assertEqual([record.args for record in logs.records], [(endpoint, "PracticePlanError")])
        self.assertEqual([record.exc_info for record in logs.records], [None])

    def assert_no_state_leak(self, html):
        for needle in ("Traceback", "PracticePlanError", "PracticeStateError", "not json", "malformed",
                       "question_ids_json", "stored-seed", "sqlite", "Error"):
            self.assertNotIn(needle, html)


# ====================================================================== armed: registration
@armed_only
class ArmedRegistrationTest(unittest.TestCase):
    def test_exactly_the_four_practice_rules_are_registered(self):
        rows = [(rule.rule, tuple(sorted(rule.methods)), rule.endpoint)
                for rule in web_app.app.url_map.iter_rules() if rule.rule.startswith("/practice")]
        self.assertEqual(set(rows), EXPECTED_RULES)
        self.assertEqual(len(rows), 4)
        self.assertIn("practice", web_app.app.blueprints)

    def test_every_view_declares_signed_in_and_unsafe_views_declare_same_origin(self):
        for endpoint, view in web_app.app.view_functions.items():
            if not endpoint.startswith("practice."):
                continue
            with self.subTest(endpoint=endpoint):
                self.assertEqual(policy_for_view(view), "signed_in")
        unsafe = {"practice.start_section", "practice.start_topic", "practice.attempt"}
        for endpoint in unsafe:
            self.assertEqual(integrity_policy_for_view(web_app.app.view_functions[endpoint]), "same_origin")
        for rule in web_app.app.url_map.iter_rules():
            if rule.endpoint.startswith("practice."):
                self.assertFalse(rule.methods & {"PUT", "PATCH", "DELETE"}, rule.rule)
                if rule.methods & {"POST"}:
                    self.assertIn(rule.endpoint, unsafe)

    def test_the_legacy_routes_are_all_still_registered(self):
        rules = {rule.rule for rule in web_app.app.url_map.iter_rules()}
        for expected in ("/", "/grade/<int:grade>", "/section/<slug>", "/dzi", "/dzi/source/<source_slug>",
                         "/teacher", "/teacher/new", "/teacher/assignments", "/teacher/dzi-training",
                         "/teacher/assignment/<int:assignment_id>",
                         "/teacher/assignment/<int:assignment_id>/results", "/quiz/s/<public_token>",
                         "/quiz/attempt/<int:attempt_id>", "/quiz/attempt/<int:attempt_id>/result"):
            self.assertIn(expected, rules)
        self.assertNotIn("/quiz/<int:assignment_id>", rules)


# ====================================================================== armed: start
@armed_only
class StartRouteTest(RouteCase):
    def test_signed_out_and_pending_second_factor_are_sent_to_login_and_write_nothing(self):
        scope = self.make_scope(6)
        before = self.table_digests()
        path = self.start_url(scope)
        self.assert_login_redirect(self.post(self.client(), path), path)
        pending = self.client()
        with pending.session_transaction() as session:
            session.update(mfa_uid=self.admin.user_id, mfa_sk="x", mfa_iat=1_700_000_000)
        self.assert_login_redirect(self.post(pending, path), path)
        self.assertEqual(self.table_digests(), before)

    def test_every_role_starts_its_own_session_and_nothing_submitted_is_trusted(self):
        scope = self.make_scope(12)
        forged = {"user_id": "1", "role": "admin", "seed": "forged", "question_ids_json": "[1,2,3]",
                  "score_correct": "10", "score_total": "10", "started_at": "1999-01-01 00:00:00",
                  "submitted_at": "1999-01-01 00:00:00", "label": "x", "plan": "x", "section_id": "999",
                  "topic_id": "999"}
        for who in (self.student, self.teacher, self.admin):
            with self.subTest(role=who.role):
                response = self.post(self.client(who), self.start_url(scope), forged)
                self.assertEqual(response.status_code, 303)
                session_id = int(response.headers["Location"].rsplit("/", 1)[1])
                row, answers = self.stored(session_id)
                self.assertEqual((row["user_id"], row["section_id"], row["topic_id"]),
                                 (who.user_id, scope.section_id, None))
                self.assertRegex(row["seed"], r"^[0-9a-f]{32}$")
                self.assertEqual(len(json.loads(row["question_ids_json"])), 10)
                self.assertNotEqual(row["started_at"], "1999-01-01 00:00:00")
                self.assertEqual((row["submitted_at"], row["abandoned_at"], row["score_correct"], row["score_total"]),
                                 (None, None, None, None))
                self.assertEqual(answers, [])
        self.assertEqual(len(self.sessions()), 3)

    def test_a_second_start_resumes_the_same_open_session(self):
        scope = self.make_scope(12)
        client = self.client(self.student)
        first = self.post(client, self.start_url(scope))
        before = self.table_digests()
        second = self.post(client, self.start_url(scope))
        self.assertEqual((first.status_code, second.status_code), (303, 303))
        self.assertEqual(first.headers["Location"], second.headers["Location"])
        self.assertEqual(self.table_digests(), before)

    def test_topic_start_restricts_the_pool_and_is_independent_of_the_section_session(self):
        scope = self.make_scope(6, topics=2)
        client = self.client(self.student)
        topic_session = int(self.post(client, self.start_url(scope, 1)).headers["Location"].rsplit("/", 1)[1])
        section_session = int(self.post(client, self.start_url(scope)).headers["Location"].rsplit("/", 1)[1])
        self.assertNotEqual(topic_session, section_session)
        row, _ = self.stored(topic_session)
        self.assertEqual(row["topic_id"], scope.topic_ids[1])
        self.assertLessEqual(set(self.plan_of(topic_session)), set(scope.question_ids[1]))
        self.assertEqual(self.stored(section_session)[0]["topic_id"], None)
        # Resuming the topic scope returns the topic session, never the section one.
        again = self.post(client, self.start_url(scope, 1))
        self.assertEqual(again.headers["Location"], f"/practice/session/{topic_session}")
        other_topic = self.post(client, self.start_url(scope, 0))
        self.assertNotIn(other_topic.headers["Location"], (f"/practice/session/{topic_session}",
                                                           f"/practice/session/{section_session}"))

    def test_topic_ownership_a_topic_of_another_section_or_a_missing_one_is_unavailable(self):
        scope, other = self.make_scope(6), self.make_scope(6)
        before = self.table_digests()
        client = self.client(self.student)
        for path in (f"/practice/section/{scope.section_id}/topic/{other.topic_ids[0]}/start",
                     f"/practice/section/{scope.section_id}/topic/987654/start",
                     f"/practice/section/987654/topic/{scope.topic_ids[0]}/start"):
            with self.subTest(path=path):
                response = self.post(client, path)
                self.assertEqual(response.status_code, 404)
                self.assertIn("Това упражнение не е налично.", self.page(response))
                self.assertNotIn("Location", response.headers)
        self.assertEqual(self.table_digests(), before)

    def test_missing_zero_oversized_and_dzi_training_scopes_are_unavailable(self):
        dzi = self.make_scope(8, section_type="dzi_training")
        before = self.table_digests()
        client = self.client(self.student)
        for path in (f"/practice/section/{dzi.section_id}/start", "/practice/section/987654/start",
                     "/practice/section/0/start", "/practice/section/99999999999999999999/start",
                     f"/practice/section/{dzi.section_id}/topic/0/start"):
            with self.subTest(path=path):
                response = self.post(client, path)
                self.assertEqual(response.status_code, 404)
                self.assertIn("Това упражнение не е налично.", self.page(response))
        self.assertEqual(self.table_digests(), before)

    def test_a_thin_pool_creates_nothing_and_says_so(self):
        scope = self.make_scope(4)
        before = self.table_digests()
        response = self.post(self.client(self.student), self.start_url(scope))
        self.assertEqual(response.status_code, 409)
        html = self.page(response)
        self.assertIn("Все още няма достатъчно въпроси", html)
        self.assert_no_state_leak(html)
        self.assertEqual(self.table_digests(), before)

    def test_a_stale_session_is_replaced_or_abandoned_without_a_replacement(self):
        scope = self.make_scope(12)
        ids = scope.all_question_ids
        stale = self.insert_session(self.student, scope, ids[:3])
        for question_id in ids[:3]:
            self.break_question(question_id)
        self.conn.commit()
        response = self.post(self.client(self.student), self.start_url(scope))
        self.assertEqual(response.status_code, 303)
        replacement = int(response.headers["Location"].rsplit("/", 1)[1])
        self.assertNotEqual(replacement, stale)
        self.assertIsNotNone(self.stored(stale)[0]["abandoned_at"])
        self.assertIsNone(self.stored(replacement)[0]["abandoned_at"])
        # A scope thinned below the minimum: the stale session is abandoned and nothing replaces it.
        thin = self.make_scope(6)
        old = self.insert_session(self.teacher, thin, thin.all_question_ids[:2])
        for question_id in thin.all_question_ids[:2]:
            self.break_question(question_id)
        self.conn.commit()
        response = self.post(self.client(self.teacher), self.start_url(thin))
        self.assertEqual(response.status_code, 409)
        self.assertIsNotNone(self.stored(old)[0]["abandoned_at"])
        self.assertEqual(len([row for row in self.sessions() if row["section_id"] == thin.section_id]), 1)

    def test_a_malformed_stored_plan_is_a_safe_bulgarian_refusal_that_writes_nothing(self):
        scope = self.make_scope(8)
        self.insert_session(self.student, scope, "not json", seed="stored-seed")
        before = self.table_digests()
        with self.assertLogs(web_app.app.logger, level="ERROR") as logs:
            response = self.post(self.client(self.student), self.start_url(scope))
        html = self.page(response)
        self.assertEqual(response.status_code, 409)
        self.assertIn("Това упражнение не може да бъде показано в момента.", html)
        self.assert_no_state_leak(html)
        self.assertEqual(self.table_digests(), before)
        self.assert_plan_refusal_logged(logs, "practice.start_section")

    def test_a_retired_scope_is_a_404_on_start_and_a_malformed_plan_is_logged_by_class_only(self):
        # The retired scope: no session is adopted, resumed or abandoned, whatever the stored plan.
        scope = self.make_scope(8, topics=2)
        topic_session = self.insert_session(self.student, scope, scope.question_ids[1][:3],
                                            topic_id=scope.topic_ids[1])
        self.conn.execute("UPDATE curriculum_sections SET section_type = 'dzi_training' WHERE id = ?",
                          (scope.section_id,))
        self.conn.commit()
        before = self.table_digests()
        client = self.client(self.student)
        for path in (self.start_url(scope), self.start_url(scope, 1)):
            response = self.post(client, path)
            self.assertEqual(response.status_code, 404)
            self.assertIn("Това упражнение не е налично.", self.page(response))
        self.assertEqual(self.table_digests(), before)
        self.assertIsNone(self.stored(topic_session)[0]["abandoned_at"])
        # A plan with hostile text: the log carries the endpoint and the exception class, nothing else.
        other = self.make_scope(8)
        self.insert_session(self.teacher, other, '[1, 2, "leak-marker-77"]', seed="leak-seed-88")
        with self.assertLogs(web_app.app.logger, level="ERROR") as logs:
            response = self.post(self.client(self.teacher), self.start_url(other))
        self.assertEqual(response.status_code, 409)
        self.assert_plan_refusal_logged(logs, "practice.start_section")
        for needle in ("leak-marker-77", "leak-seed-88", "Synthetic", "practice-section"):
            self.assertNotIn(needle, "\n".join(logs.output))

    def test_unexpected_stored_state_is_a_server_error_without_detail(self):
        scope = self.make_scope(8)
        before = self.table_digests()
        # The service refuses stored state it cannot interpret; the route turns that into the generic 500.
        with patch.object(practice, "start_or_resume_practice",
                          side_effect=practice.PracticeStateError("internal detail 12345")):
            response = self.post(self.client(self.student), self.start_url(scope))
        html = self.page(response)
        self.assertEqual(response.status_code, 500)
        self.assertNotIn("internal detail", html)
        self.assertNotIn("12345", html)
        self.assertEqual(self.table_digests(), before)

    def test_cross_origin_posts_are_refused_before_any_write(self):
        scope = self.make_scope(8)
        before = self.table_digests()
        for who in (None, self.student, self.teacher, self.admin):
            client = self.client(who)
            for headers in (CROSS, {"Referer": "http://evil.example/x"}, {"Origin": "http://localhost.evil.example"},
                            {"Origin": "https://localhost"}):
                for path in (self.start_url(scope), self.start_url(scope, 0)):
                    with self.subTest(role=getattr(who, "role", None), headers=headers, path=path):
                        self.assertEqual(self.post(client, path, headers=headers).status_code, 403)
        self.assertEqual(self.table_digests(), before)

    def test_a_post_with_no_origin_or_referer_is_refused_outside_testing(self):
        scope = self.make_scope(8)
        before = self.table_digests()
        with patch.dict(web_app.app.config, {"TESTING": False}):
            response = self.client(self.student).post(self.start_url(scope))
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.table_digests(), before)

    def test_a_same_origin_referer_is_accepted(self):
        scope = self.make_scope(8)
        response = self.post(self.client(self.student), self.start_url(scope),
                             headers={"Referer": "http://localhost/section/x"})
        self.assertEqual(response.status_code, 303)

    def test_start_urls_accept_get_head_and_post_only(self):
        # GET and HEAD are the read-only confirmation of the sign-in return (ConfirmRouteTest); the
        # other unsafe methods are still not routed.
        scope = self.make_scope(8)
        before = self.table_digests()
        client = self.client(self.student)
        for path in (self.start_url(scope), self.start_url(scope, 0)):
            for method in ("get", "head"):
                with self.subTest(path=path, method=method):
                    self.assertEqual(getattr(client, method)(path, headers=SAME).status_code, 200)
            for method in ("put", "patch", "delete"):
                with self.subTest(path=path, method=method):
                    self.assertEqual(getattr(client, method)(path, headers=SAME).status_code, 405)
        self.assertEqual(self.table_digests(), before)

    def test_an_unavailable_account_is_refused_with_the_403_page(self):
        scope = self.make_scope(8)
        outcome = practice.PracticeStart(practice.OUTCOME_ACCOUNT_UNAVAILABLE)
        with patch.object(practice, "start_or_resume_practice", return_value=outcome):
            response = self.post(self.client(self.student), self.start_url(scope))
        self.assertEqual(response.status_code, 403)
        self.assertIn("Нямаш достъп", self.page(response))


# ====================================================================== armed: attempt
@armed_only
class AttemptRouteTest(RouteCase):
    def test_signed_out_and_pending_requests_are_sent_to_login(self):
        scope = self.make_scope(8)
        session_id = self.open_via_route(self.student, scope)
        path = f"/practice/session/{session_id}"
        for method in ("get", "head"):
            self.assert_login_redirect(getattr(self.client(), method)(path), path)
        self.assert_login_redirect(self.post(self.client(), path), path)
        self.assert_login_redirect(self.client().get(path + "/result"), path + "/result")
        self.assertEqual(self.stored(session_id)[1], [])

    def test_the_owner_sees_the_questions_with_radio_choices_and_no_correct_marker(self):
        scope = self.make_scope(8)
        session_id = self.open_via_route(self.student, scope)
        response = self.client(self.student).get(f"/practice/session/{session_id}")
        html = self.page(response)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        blocks = self.questions_in(html)
        self.assertEqual([int(question_id) for question_id, _ in blocks], self.plan_of(session_id))
        for question_id, block in blocks:
            self.assertEqual(re.findall(r'<input type="radio" name="(q_\d+)"', block), [f"q_{question_id}"] * 4)
            self.assertEqual(sorted(re.findall(r'value="([^"]+)"', block)), list("ABCD"))
            self.assertEqual(re.findall(r'quiz-option-letter">([^<]+)<', block), ["А)", "Б)", "В)", "Г)"])
        for marker in ("is-correct", "правилен", "Правилен отговор", "Вярно", "Грешно", "is_correct",
                       "Обяснение", "result-option-card"):
            self.assertNotIn(marker, html)
        self.assertNotRegex(html, r"<input[^>]*\bchecked\b")
        self.assertIn(f'action="/practice/session/{session_id}"', html)
        self.assertIn("Предай упражнението", html)
        self.assertNotIn("countdown", html)
        self.assertNotIn("<script src", html.split("</nav>")[-1] if "</nav>" in html else html[-3000:])

    def test_the_stored_order_and_seeded_option_order_are_shown_and_stable(self):
        scope = self.make_scope(8)
        ids = scope.all_question_ids
        plan = [ids[4], ids[1], ids[6], ids[0], ids[3]]
        orders = {}
        for seed in ("seed-order-one", "seed-order-two"):
            who = self.add_account("student")
            self.conn.commit()
            session_id = self.insert_session(who, scope, plan, seed=seed)
            client = self.client(who)
            first, second = (self.page(client.get(f"/practice/session/{session_id}")) for _ in range(2))
            self.assertEqual(first, second)
            blocks = self.questions_in(first)
            self.assertEqual([int(question_id) for question_id, _ in blocks], plan)
            orders[seed] = {}
            for question_id, block in blocks:
                shown = re.findall(r'value="([A-D])"', block)
                expected = [option["option_letter"] for option in quiz_shuffle_options(seed, int(question_id), [
                    {"option_letter": letter, "option_text": letter} for letter in "ABCD"])]
                self.assertEqual(shown, expected)
                orders[seed][question_id] = shown
        self.assertNotEqual(orders["seed-order-one"], orders["seed-order-two"])

    def test_a_topic_session_shows_only_the_questions_of_its_topic(self):
        scope = self.make_scope(6, topics=2)
        session_id = self.open_via_route(self.student, scope, 0)
        html = self.page(self.client(self.student).get(f"/practice/session/{session_id}"))
        shown = {int(question_id) for question_id, _ in self.questions_in(html)}
        self.assertTrue(shown)
        self.assertLessEqual(shown, set(scope.question_ids[0]))

    def test_get_and_head_write_nothing(self):
        scope = self.make_scope(8)
        session_id = self.open_via_route(self.student, scope)
        client = self.client(self.student)
        before = self.table_digests()
        for method in ("get", "head", "options"):
            with self.subTest(method=method):
                response = getattr(client, method)(f"/practice/session/{session_id}")
                self.assertEqual(response.status_code, 200)
        self.assertEqual(self.table_digests(), before)

    def test_another_account_a_teacher_an_admin_and_a_missing_id_get_the_same_404(self):
        scope = self.make_scope(8)
        session_id = self.open_via_route(self.student, scope)
        before = self.table_digests()
        for who in (self.other_student, self.teacher, self.admin):
            client = self.client(who)
            missing = client.get("/practice/session/987654")
            for path in (f"/practice/session/{session_id}", f"/practice/session/{session_id}/result"):
                with self.subTest(role=who.role, path=path):
                    response = client.get(path)
                    self.assertEqual(response.status_code, 404)
                    self.assertEqual(self.page(response), self.page(client.get(path.replace(str(session_id), "987654"))))
                    self.assertNotIn("Location", response.headers)
            posted = self.post(client, f"/practice/session/{session_id}", {f"q_{self.plan_of(session_id)[0]}": "A"})
            self.assertEqual(posted.status_code, 404)
            self.assertEqual(self.page(posted), self.page(self.post(client, "/practice/session/987654")))
            self.assertEqual(self.page(missing), self.page(client.get("/practice/session/0")))
        self.assertEqual(self.table_digests(), before)
        self.assertEqual(self.stored(session_id)[1], [])

    def test_a_submitted_own_session_leads_to_its_own_result(self):
        scope = self.make_scope(8)
        session_id = self.open_via_route(self.student, scope)
        client = self.client(self.student)
        self.post(client, f"/practice/session/{session_id}", self.answer_all(session_id))
        for method in ("get", "head"):
            response = getattr(client, method)(f"/practice/session/{session_id}")
            self.assertEqual(response.status_code, 302)
            self.assertEqual(response.headers["Location"], f"/practice/session/{session_id}/result")

    def test_an_abandoned_old_tab_is_refused_and_offers_the_scope_again(self):
        scope = self.make_scope(12)
        ids = scope.all_question_ids
        old = self.insert_session(self.student, scope, ids[:3])
        for question_id in ids[:3]:
            self.break_question(question_id)
        self.conn.commit()
        new = self.open_via_route(self.student, scope)
        self.assertNotEqual(old, new)
        client = self.client(self.student)
        before = self.table_digests()
        for response in (client.get(f"/practice/session/{old}"),
                         self.post(client, f"/practice/session/{old}", {f"q_{ids[5]}": "A"})):
            html = self.page(response)
            self.assertEqual(response.status_code, 409)
            self.assertIn("Този опит е заменен и не може да бъде предаден.", html)
            self.assertIn(f'action="/practice/section/{scope.section_id}/start"', html)
            self.assertIn("Към упражнението", html)
            self.assertNotIn("quiz-option-card", html)
        self.assertEqual(self.table_digests(), before)
        self.assertEqual(self.stored(new)[1], [])

    def test_a_stale_open_session_is_a_bulgarian_page_that_never_grades(self):
        scope = self.make_scope(12)
        ids = scope.all_question_ids
        stale = self.insert_session(self.student, scope, ids[:3])
        for question_id in ids[:3]:
            self.break_question(question_id)
        self.conn.commit()
        client = self.client(self.student)
        before = self.table_digests()
        for response in (client.get(f"/practice/session/{stale}"),
                         self.post(client, f"/practice/session/{stale}", {f"q_{ids[0]}": "A"})):
            html = self.page(response)
            self.assertEqual(response.status_code, 409)
            self.assertIn("Въпросите от това упражнение вече не могат да бъдат показани.", html)
            self.assertIn("Ново упражнение", html)
            self.assertIn(f'action="/practice/section/{scope.section_id}/start"', html)
        self.assertEqual(self.table_digests(), before)
        # The offered action abandons the stale session and starts a fresh one.
        response = self.post(client, f"/practice/section/{scope.section_id}/start")
        self.assertEqual(response.status_code, 303)
        self.assertIsNotNone(self.stored(stale)[0]["abandoned_at"])

    def test_a_stale_topic_session_offers_the_topic_start(self):
        scope = self.make_scope(6, topics=2)
        stale = self.insert_session(self.student, scope, [987001], topic_id=scope.topic_ids[1])
        html = self.page(self.client(self.student).get(f"/practice/session/{stale}"))
        self.assertIn(f'action="/practice/section/{scope.section_id}/topic/{scope.topic_ids[1]}/start"', html)

    def test_a_malformed_plan_is_a_safe_page_on_get_and_post(self):
        scope = self.make_scope(8)
        bad = self.insert_session(self.student, scope, "not json", seed="stored-seed")
        client = self.client(self.student)
        before = self.table_digests()
        for response in (client.get(f"/practice/session/{bad}"), self.post(client, f"/practice/session/{bad}",
                                                                           {"q_1": "A"})):
            html = self.page(response)
            self.assertEqual(response.status_code, 409)
            self.assertIn("Това упражнение не може да бъде показано в момента.", html)
            self.assert_no_state_leak(html)
            self.assertNotIn("<form method=\"post\" action=\"/practice/section", html)
        with self.assertLogs(web_app.app.logger, level="ERROR") as logs:
            client.get(f"/practice/session/{bad}")
            self.post(client, f"/practice/session/{bad}", {"q_1": "A"})
        self.assertEqual([record.getMessage() for record in logs.records],
                         ["Practice plan refused: endpoint=practice.attempt error=PracticePlanError"] * 2)
        self.assertNotIn(str(bad), "\n".join(logs.output))
        self.assertEqual(self.table_digests(), before)
        self.assertEqual(self.stored(bad)[0]["question_ids_json"], "not json")

    def test_an_unavailable_scope_of_an_open_session_is_a_safe_page(self):
        scope = self.make_scope(8)
        session_id = self.open_via_route(self.student, scope)
        self.conn.execute("UPDATE curriculum_sections SET section_type = 'dzi_training' WHERE id = ?",
                          (scope.section_id,))
        self.conn.commit()
        response = self.client(self.student).get(f"/practice/session/{session_id}")
        html = self.page(response)
        self.assertEqual(response.status_code, 409)
        self.assertIn("Това упражнение не може да бъде показано в момента.", html)
        self.assertNotIn("quiz-option-card", html)
        # The submit of the same retired scope is the same 409 and writes nothing.
        before = self.table_digests()
        posted = self.post(self.client(self.student), f"/practice/session/{session_id}",
                           {f"q_{self.plan_of(session_id)[0]}": "A"})
        self.assertEqual(posted.status_code, 409)
        self.assertIn("Това упражнение не може да бъде показано в момента.", self.page(posted))
        self.assertEqual(self.table_digests(), before)

    def test_prompts_and_options_are_escaped(self):
        scope = self.make_scope(6)
        question_id = scope.all_question_ids[0]
        self.conn.execute("UPDATE questions SET prompt = ? WHERE id = ?", ("<script>alert(1)</script> &", question_id))
        self.conn.execute("UPDATE multiple_choice_options SET option_text = ? WHERE question_id = ? AND option_letter = 'B'",
                          ("<img src=x onerror=alert(2)>", question_id))
        self.conn.commit()
        session_id = self.insert_session(self.student, scope, [question_id] + scope.all_question_ids[1:4])
        html = self.page(self.client(self.student).get(f"/practice/session/{session_id}"))
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertNotIn("<img src=x", html)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt; &amp;", html)

    def test_a_planned_question_that_cannot_be_shown_is_counted_not_shown(self):
        scope = self.make_scope(8)
        ids = scope.all_question_ids
        session_id = self.insert_session(self.student, scope, ids[:4])
        self.break_question(ids[2])
        self.conn.commit()
        html = self.page(self.client(self.student).get(f"/practice/session/{session_id}"))
        self.assertEqual([int(question_id) for question_id, _ in self.questions_in(html)], [ids[0], ids[1], ids[3]])
        self.assertIn("Въпроси, които не могат да бъдат показани и няма да се оценяват: 1.", html)

    def test_account_unavailable_is_the_403_page(self):
        scope = self.make_scope(8)
        session_id = self.open_via_route(self.student, scope)
        outcome = practice.PracticeAttempt(practice.OUTCOME_ACCOUNT_UNAVAILABLE)
        with patch.object(practice, "load_practice_attempt", return_value=outcome):
            response = self.client(self.student).get(f"/practice/session/{session_id}")
        self.assertEqual(response.status_code, 403)
        self.assertIn("Нямаш достъп", self.page(response))


# ====================================================================== armed: submit
@armed_only
class SubmitRouteTest(RouteCase):
    def opened(self, good=8):
        scope = self.make_scope(good)
        return scope, self.open_via_route(self.student, scope)

    def test_choices_are_translated_by_question_id_to_the_original_option_keys(self):
        scope, session_id = self.opened()
        plan = self.plan_of(session_id)
        fields = {f"q_{plan[0]}": "A", f"q_{plan[1]}": "B", f"q_{plan[2]}": "C"}
        response = self.post(self.client(self.student), f"/practice/session/{session_id}", fields)
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["Location"], f"/practice/session/{session_id}/result")
        row, answers = self.stored(session_id)
        self.assertEqual(answers, sorted([(plan[0], "A", 1), (plan[1], "B", 0), (plan[2], "C", 0)] +
                                         [(question_id, None, 0) for question_id in plan[3:]]))
        self.assertEqual((row["score_correct"], row["score_total"]), (1, len(plan)))
        self.assertRegex(row["submitted_at"], r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d$")

    def test_nothing_but_the_choices_is_read_from_the_form(self):
        scope, session_id = self.opened()
        plan = self.plan_of(session_id)
        fields = {f"q_{plan[0]}": "B", "score_correct": "10", "score_total": "10", "user_id": "1",
                  "is_correct": "1", f"correct_{plan[0]}": "B", "submitted_at": "1999-01-01 00:00:00",
                  "question_ids_json": "[1]", "seed": "x", "q_999999": "A", "q_0": "A", "q_01": "A",
                  f"q_{plan[1]}x": "A", f"Q_{plan[1]}": "A", f" q_{plan[1]}": "A"}
        self.post(self.client(self.student), f"/practice/session/{session_id}", fields)
        row, answers = self.stored(session_id)
        self.assertEqual((row["score_correct"], row["score_total"]), (0, len(plan)))
        self.assertEqual(row["user_id"], self.student.user_id)
        self.assertEqual({question_id: letter for question_id, letter, _ in answers if letter},
                         {plan[0]: "B"})
        self.assertNotEqual(row["submitted_at"], "1999-01-01 00:00:00")

    def test_a_display_letter_or_an_unknown_key_is_not_a_choice(self):
        scope, session_id = self.opened()
        plan = self.plan_of(session_id)
        client = self.client(self.student)
        for value in ("А", "Б", "a", "E", "", " A", "AB"):
            response = self.post(client, f"/practice/session/{session_id}", {f"q_{plan[0]}": value})
            with self.subTest(value=value):
                self.assertEqual(response.status_code, 400)
        self.assertEqual(self.stored(session_id)[1], [])

    def test_a_field_sent_twice_is_unanswered(self):
        scope, session_id = self.opened()
        plan = self.plan_of(session_id)
        response = self.client(self.student).post(
            f"/practice/session/{session_id}", headers=SAME,
            data={f"q_{plan[0]}": ["A", "A"], f"q_{plan[1]}": "A"})
        self.assertEqual(response.status_code, 303)
        answers = dict((question_id, letter) for question_id, letter, _ in self.stored(session_id)[1])
        self.assertIsNone(answers[plan[0]])
        self.assertEqual(answers[plan[1]], "A")

    def test_an_empty_submission_leaves_the_form_available_with_a_bulgarian_message(self):
        scope, session_id = self.opened()
        client = self.client(self.student)
        before = self.table_digests()
        for fields in ({}, {"q_999999": "A"}, {"x": "y"}):
            response = self.post(client, f"/practice/session/{session_id}", fields)
            html = self.page(response)
            with self.subTest(fields=fields):
                self.assertEqual(response.status_code, 400)
                self.assertIn("Избери поне един отговор, преди да предадеш упражнението.", html)
                self.assertEqual(len(self.questions_in(html)), len(self.plan_of(session_id)))
                self.assertIn("Предай упражнението", html)
        self.assertEqual(self.table_digests(), before)
        self.assertIsNone(self.stored(session_id)[0]["submitted_at"])
        # The same session can still be submitted afterwards.
        self.assertEqual(self.post(client, f"/practice/session/{session_id}", self.answer_all(session_id)).status_code,
                         303)

    def test_a_duplicate_submission_leads_to_the_stored_result_and_changes_nothing(self):
        scope, session_id = self.opened()
        client = self.client(self.student)
        first = self.post(client, f"/practice/session/{session_id}", self.answer_all(session_id, "A"))
        stored = self.stored(session_id)
        before = self.table_digests()
        for fields in (self.answer_all(session_id, "A"), self.answer_all(session_id, "B"), {}):
            again = self.post(client, f"/practice/session/{session_id}", fields)
            self.assertEqual(again.status_code, 303)
            self.assertEqual(again.headers["Location"], first.headers["Location"])
        self.assertEqual(self.stored(session_id), stored)
        self.assertEqual(self.table_digests(), before)

    def test_cross_origin_and_signed_out_submissions_write_nothing(self):
        scope, session_id = self.opened()
        before = self.table_digests()
        path = f"/practice/session/{session_id}"
        fields = self.answer_all(session_id)
        for who in (None, self.student, self.teacher, self.admin):
            self.assertEqual(self.post(self.client(who), path, fields, headers=CROSS).status_code, 403)
        self.assert_login_redirect(self.post(self.client(), path, fields), path)
        self.assertEqual(self.table_digests(), before)

    def test_unsafe_methods_other_than_post_are_not_routed(self):
        scope, session_id = self.opened()
        before = self.table_digests()
        client = self.client(self.student)
        for method in ("put", "patch", "delete"):
            self.assertEqual(getattr(client, method)(f"/practice/session/{session_id}", headers=SAME).status_code, 405)
        self.assertEqual(client.post(f"/practice/session/{session_id}/result", headers=SAME).status_code, 405)
        self.assertEqual(self.table_digests(), before)

    def test_questions_that_went_away_are_not_graded_and_the_rest_are(self):
        scope, session_id = self.opened(good=12)
        plan = self.plan_of(session_id)
        self.break_question(plan[0])
        self.conn.commit()
        fields = {f"q_{question_id}": "A" for question_id in plan}
        self.post(self.client(self.student), f"/practice/session/{session_id}", fields)
        row, answers = self.stored(session_id)
        self.assertEqual(len(answers), len(plan) - 1)
        self.assertNotIn(plan[0], [question_id for question_id, _, _ in answers])
        self.assertEqual((row["score_correct"], row["score_total"]), (len(plan) - 1, len(plan) - 1))

    def test_unexpected_stored_state_on_submit_is_a_server_error_without_detail(self):
        scope, session_id = self.opened()
        self.conn.execute("INSERT INTO practice_answers (session_id, question_id, chosen_letter, is_correct) "
                          "VALUES (?, ?, 'A', 1)", (session_id, self.plan_of(session_id)[0]))
        self.conn.commit()
        before = self.table_digests()
        response = self.post(self.client(self.student), f"/practice/session/{session_id}",
                             self.answer_all(session_id))
        html = self.page(response)
        self.assertEqual(response.status_code, 500)
        self.assertNotIn("Traceback", html)
        self.assertNotIn("already has stored answers", html)
        self.assertEqual(self.table_digests(), before)

    def test_account_unavailable_on_submit_is_the_403_page(self):
        scope, session_id = self.opened()
        with patch.object(practice, "submit_practice",
                          return_value=practice.PracticeSubmission(practice.OUTCOME_ACCOUNT_UNAVAILABLE)):
            response = self.post(self.client(self.student), f"/practice/session/{session_id}", {"q_1": "A"})
        self.assertEqual(response.status_code, 403)


# ====================================================================== armed: result
@armed_only
class ResultRouteTest(RouteCase):
    def submitted(self, who=None, *, good=8, topic_index=None, letter="A", only=None):
        who = who or self.student
        scope = self.make_scope(good, topics=2 if topic_index is not None else 1)
        session_id = self.open_via_route(who, scope, topic_index)
        fields = self.answer_all(session_id, letter, only=only)
        response = self.post(self.client(who), f"/practice/session/{session_id}", fields)
        self.assertEqual(response.status_code, 303)
        return scope, session_id

    def test_the_owner_sees_the_saved_score_the_shared_review_and_the_repeat_action(self):
        scope, session_id = self.submitted(good=8)
        plan = self.plan_of(session_id)
        response = self.client(self.student).get(f"/practice/session/{session_id}/result")
        html = self.page(response)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertIn(f'<span class="result-score">{len(plan)}/{len(plan)}</span>', html)
        self.assertIn("Успеваемост: 100%", html)
        self.assertIn("Всички отговори са верни.", html)
        self.assertEqual(html.count("status-pill status-pill--correct"), len(plan))
        self.assertIn('class="card question-card correct"', html)
        self.assertIn(f'<form method="post" action="/practice/section/{scope.section_id}/start">', html)
        self.assertIn("Нов опит", html)
        self.assertNotIn("practice-skipped-count", html)

    def test_a_partial_score_wrong_and_unanswered_review(self):
        scope, session_id = self.submitted(good=8, letter="B")
        plan = self.plan_of(session_id)
        html = self.page(self.client(self.student).get(f"/practice/session/{session_id}/result"))
        self.assertIn(f'<span class="result-score">0/{len(plan)}</span>', html)
        self.assertIn("Нужен е преговор на темата.", html)
        self.assertEqual(html.count("status-pill status-pill--wrong"), len(plan))
        self.assertIn("Правилен отговор", html)

    def test_partially_answered_unanswered_questions_are_shown_as_such(self):
        scope = self.make_scope(8)
        session_id = self.open_via_route(self.student, scope)
        plan = self.plan_of(session_id)
        self.post(self.client(self.student), f"/practice/session/{session_id}", {f"q_{plan[0]}": "A"})
        html = self.page(self.client(self.student).get(f"/practice/session/{session_id}/result"))
        self.assertIn(f'<span class="result-score">1/{len(plan)}</span>', html)
        self.assertEqual(html.count("Няма избран отговор."), len(plan) - 1)
        self.assertIn("Има верни отговори и въпроси за преговор.", html)

    def test_the_topic_result_repeats_the_topic_scope(self):
        scope, session_id = self.submitted(good=6, topic_index=1)
        html = self.page(self.client(self.student).get(f"/practice/session/{session_id}/result"))
        self.assertIn(f'<form method="post" action="/practice/section/{scope.section_id}/topic/'
                      f'{scope.topic_ids[1]}/start">', html)

    def test_the_saved_score_survives_content_changes_and_skipped_questions_are_counted(self):
        scope, session_id = self.submitted(good=8)
        plan = self.plan_of(session_id)
        saved = self.stored(session_id)[0]
        self.break_question(plan[0])
        self.conn.commit()
        html = self.page(self.client(self.student).get(f"/practice/session/{session_id}/result"))
        self.assertIn(f'<span class="result-score">{saved["score_correct"]}/{saved["score_total"]}</span>', html)
        self.assertIn('id="practice-skipped-count"', html)
        self.assertIn("Пропуснати въпроси (не могат да бъдат показани): 1", html)
        self.assertEqual(html.count("status-pill status-pill--correct"), len(plan) - 1)

    def test_only_the_saved_score_is_shown_when_no_question_can_be_reviewed(self):
        scope, session_id = self.submitted(good=8)
        for question_id in self.plan_of(session_id):
            self.break_question(question_id)
        self.conn.commit()
        response = self.client(self.student).get(f"/practice/session/{session_id}/result")
        html = self.page(response)
        self.assertEqual(response.status_code, 200)
        self.assertIn(practice.PRACTICE_STALE_RESULT_MESSAGE, html)
        self.assertRegex(html, r'<span class="result-score">\d+/\d+</span>')
        self.assertNotIn("question-card", html)

    def test_the_result_says_the_saved_score_is_from_submission_and_the_review_is_current(self):
        scope, session_id = self.submitted(good=8)
        saved = self.stored(session_id)
        response = self.client(self.student).get(f"/practice/session/{session_id}/result")
        html = self.page(response)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(html.count('id="practice-current-content-note"'), 1)
        self.assertIn("Резултатът е запазен при предаването. Прегледът по-долу показва въпросите с тяхното "
                      "текущо съдържание.", html)
        self.assertIn(f'<span class="result-score">{saved[0]["score_correct"]}/{saved[0]["score_total"]}</span>', html)
        # Changed content shows as it is now; the stored verdict and score are untouched.
        question_id = self.plan_of(session_id)[0]
        self.conn.execute("UPDATE questions SET prompt = ? WHERE id = ?", ("Променен текст на въпроса 4242", question_id))
        self.conn.commit()
        again = self.page(self.client(self.student).get(f"/practice/session/{session_id}/result"))
        self.assertIn("Променен текст на въпроса 4242", again)
        self.assertIn('id="practice-current-content-note"', again)
        self.assertEqual(self.stored(session_id), saved)

    def test_no_content_note_when_only_the_saved_score_can_be_shown_and_a_retired_scope_still_scores(self):
        scope, session_id = self.submitted(good=8)
        saved = self.stored(session_id)[0]
        for question_id in self.plan_of(session_id):
            self.break_question(question_id)
        self.conn.commit()
        html = self.page(self.client(self.student).get(f"/practice/session/{session_id}/result"))
        self.assertNotIn("practice-current-content-note", html)
        # A retired scope: still the owner's 200 with the saved score, an empty review and no note.
        scope, retired = self.submitted(good=8)
        saved = self.stored(retired)[0]
        self.conn.execute("UPDATE curriculum_sections SET section_type = 'dzi_training' WHERE id = ?",
                          (scope.section_id,))
        self.conn.commit()
        response = self.client(self.student).get(f"/practice/session/{retired}/result")
        html = self.page(response)
        self.assertEqual(response.status_code, 200)
        self.assertIn(f'<span class="result-score">{saved["score_correct"]}/{saved["score_total"]}</span>', html)
        self.assertIn(practice.PRACTICE_STALE_RESULT_MESSAGE, html)
        self.assertNotIn("practice-current-content-note", html)
        self.assertNotIn("question-card", html)

    def test_foreign_teacher_admin_missing_open_and_abandoned_results_are_the_generic_404(self):
        scope, session_id = self.submitted()
        open_id = self.open_via_route(self.other_student, self.make_scope(8))
        abandoned = self.insert_session(self.teacher, scope, scope.all_question_ids[:3], abandoned=STAMP)
        before = self.table_digests()
        client = self.client(self.other_student)
        missing = self.page(client.get("/practice/session/987654/result"))
        for who, path in ((self.other_student, f"/practice/session/{session_id}/result"),
                          (self.teacher, f"/practice/session/{session_id}/result"),
                          (self.admin, f"/practice/session/{session_id}/result"),
                          (self.other_student, f"/practice/session/{open_id}/result"),
                          (self.teacher, f"/practice/session/{abandoned}/result")):
            with self.subTest(role=who.role, path=path):
                response = self.client(who).get(path)
                self.assertEqual(response.status_code, 404)
                self.assertNotIn("Location", response.headers)
                self.assertNotIn("result-score", self.page(response))
        self.assertEqual(self.page(self.client(self.admin).get(f"/practice/session/{session_id}/result")),
                         self.page(self.client(self.admin).get("/practice/session/987654/result")))
        self.assertTrue(missing)
        self.assertEqual(self.table_digests(), before)

    def test_get_and_head_of_a_result_write_nothing(self):
        scope, session_id = self.submitted()
        client = self.client(self.student)
        before = self.table_digests()
        for method in ("get", "head", "options"):
            self.assertEqual(getattr(client, method)(f"/practice/session/{session_id}/result").status_code, 200)
        self.assertEqual(self.table_digests(), before)

    def test_a_malformed_stored_result_plan_is_a_safe_page_and_other_damage_is_a_server_error(self):
        scope, session_id = self.submitted()
        self.conn.execute("UPDATE practice_sessions SET question_ids_json = 'not json' WHERE id = ?", (session_id,))
        self.conn.commit()
        with self.assertLogs(web_app.app.logger, level="ERROR") as logs:
            response = self.client(self.student).get(f"/practice/session/{session_id}/result")
        self.assert_plan_refusal_logged(logs, "practice.result")
        html = self.page(response)
        self.assertEqual(response.status_code, 409)
        self.assertIn("Това упражнение не може да бъде показано в момента.", html)
        self.assert_no_state_leak(html)
        scope, other = self.submitted(self.teacher)
        self.conn.execute("DELETE FROM practice_answers WHERE session_id = ? AND question_id = ?",
                          (other, self.plan_of(other)[0]))
        self.conn.commit()
        response = self.client(self.teacher).get(f"/practice/session/{other}/result")
        self.assertEqual(response.status_code, 500)
        self.assertNotIn("stores an inconsistent", self.page(response))

    def test_account_unavailable_on_result_is_the_403_page(self):
        scope, session_id = self.submitted()
        with patch.object(practice, "load_practice_result",
                          return_value=practice.PracticeResult(practice.OUTCOME_ACCOUNT_UNAVAILABLE)):
            response = self.client(self.student).get(f"/practice/session/{session_id}/result")
        self.assertEqual(response.status_code, 403)

    def test_the_repeat_action_starts_a_fresh_session_and_keeps_the_saved_result(self):
        scope, first = self.submitted(good=12)
        client = self.client(self.student)
        saved = self.stored(first)
        repeat = self.post(client, f"/practice/section/{scope.section_id}/start")
        self.assertEqual(repeat.status_code, 303)
        second = int(repeat.headers["Location"].rsplit("/", 1)[1])
        self.assertNotEqual(first, second)
        self.assertNotEqual(self.stored(first)[0]["seed"], self.stored(second)[0]["seed"])
        self.assertEqual(self.stored(first), saved)
        self.assertEqual(self.stored(second)[1], [])
        self.assertEqual(client.get(f"/practice/session/{second}").status_code, 200)
        self.assertEqual(client.get(f"/practice/session/{first}/result").status_code, 200)


# ====================================================================== armed: whole flow and prior routes
@armed_only
class FlowAndPriorRoutesTest(RouteCase):
    def test_start_attempt_submit_result_repeat_end_to_end_for_each_role(self):
        scope = self.make_scope(12)
        for who in (self.student, self.teacher, self.admin):
            with self.subTest(role=who.role):
                client = self.client(who)
                started = self.post(client, self.start_url(scope))
                path = started.headers["Location"]
                attempt = client.get(path)
                self.assertEqual(attempt.status_code, 200)
                session_id = int(path.rsplit("/", 1)[1])
                submitted = self.post(client, path, self.answer_all(session_id))
                self.assertEqual(submitted.headers["Location"], path + "/result")
                result = client.get(path + "/result")
                self.assertEqual(result.status_code, 200)
                repeat = self.post(client, self.start_url(scope))
                self.assertEqual(repeat.status_code, 303)
                self.assertNotEqual(repeat.headers["Location"], path)

    def test_the_legacy_numeric_quiz_entry_stays_a_generic_404(self):
        for who in (None, self.student, self.teacher, self.admin):
            client = self.client(who)
            for method in ("get", "post"):
                response = getattr(client, method)("/quiz/1", headers=SAME)
                with self.subTest(role=getattr(who, "role", None), method=method):
                    self.assertEqual(response.status_code, 404)
                    self.assertNotIn("Location", response.headers)
        self.assertEqual(self.client(self.student).post("/quiz/987654", headers=SAME).status_code, 404)

    def test_existing_routes_keep_their_policies_beside_the_practice_routes(self):
        public, student, teacher = self.client(), self.client(self.student), self.client(self.teacher)
        self.assertEqual(public.get("/").status_code, 200)
        self.assertEqual(public.get("/quiz/s/not-a-token").status_code, 404)
        self.assertEqual(public.get("/healthz").status_code, 200)
        self.assert_login_redirect(public.get("/dzi"), "/dzi")
        self.assertEqual(student.get("/dzi").status_code, 403)
        self.assertEqual(teacher.get("/dzi").status_code, 403)
        self.assertEqual(teacher.get("/teacher/dzi-training").status_code, 403)
        self.assertEqual(student.get("/teacher/new").status_code, 403)
        self.assertEqual(student.get("/account/attempts").status_code, 200)
        self.assertEqual(self.client(self.admin).get("/dzi").status_code, 200)

    def test_only_the_section_and_topic_pages_link_to_practice(self):
        # The browse entry is in BrowseEntryTest. No other page grows a practice link: not the home page,
        # the account pages, the grade and subject pages, nor a result or the legacy numeric quiz entry.
        scope = self.make_scope(8)
        client = self.client(self.student)
        for path in ("/", "/account", "/account/attempts", "/grade/8", "/subjects/information-technologies",
                     "/subjects/information-technologies/8", "/quiz/s/not-a-token", "/quiz/1"):
            response = client.get(path)
            with self.subTest(path=path):
                self.assertIn(response.status_code, (200, 302, 404))
                self.assertNotIn("/practice/", self.page(response))

    def test_the_session_and_assignment_tables_are_not_mixed(self):
        scope = self.make_scope(8)
        quiz_tables = ("quiz_assignments", "quiz_attempts", "quiz_answers", "lifecycle_clock")
        before = {name: self.table_digests().get(name) for name in quiz_tables}
        session_id = self.open_via_route(self.student, scope)
        self.post(self.client(self.student), f"/practice/session/{session_id}", self.answer_all(session_id))
        self.client(self.student).get(f"/practice/session/{session_id}/result")
        after = self.table_digests()
        self.assertEqual({name: after.get(name) for name in quiz_tables}, before)

    def test_the_html_of_every_page_is_bulgarian_and_free_of_framework_scripts(self):
        scope = self.make_scope(8)
        client = self.client(self.student)
        session_id = self.open_via_route(self.student, scope)
        pages = [client.get(f"/practice/session/{session_id}")]
        self.post(client, f"/practice/session/{session_id}", self.answer_all(session_id))
        pages.append(client.get(f"/practice/session/{session_id}/result"))
        pages.append(self.post(client, "/practice/section/987654/start"))
        for response in pages:
            html = self.page(response)
            self.assertIn('<html lang="bg">', html)
            self.assertNotRegex(html, r"<script[^>]+src=[^>]*(react|vue|jquery|angular)")


# ====================================================================== armed: browse entry
class BrowseUrls:
    """Browse URLs of the synthetic scopes, built the way the templates build them."""

    def section_slug(self, scope):
        return self.conn.execute("SELECT section_slug FROM curriculum_sections WHERE id = ?",
                                 (scope.section_id,)).fetchone()[0]

    def topic_slug(self, scope, index=0):
        return self.conn.execute("SELECT topic_slug FROM curriculum_topics WHERE id = ?",
                                 (scope.topic_ids[index],)).fetchone()[0]

    def subject_section_path(self, scope):
        return f"/subjects/{IT_SLUG}/8/{public_slug('it', 8, self.section_slug(scope))}"

    def subject_topic_path(self, scope, index=0):
        return (f"{self.subject_section_path(scope)}/"
                f"{public_slug('it', 8, self.topic_slug(scope, index))}")

    def legacy_section_path(self, scope):
        return f"/section/{self.section_slug(scope)}"

    FORM = re.compile(r'<form method="post" action="(/practice/[^"]*)">(.*?)</form>', re.S)

    def entry_forms(self, html):
        return self.FORM.findall(html)


@armed_only
class BrowseEntryTest(RouteCase, BrowseUrls):
    def test_a_signed_in_account_gets_one_post_form_on_a_section_page_for_the_resolved_section(self):
        scope = self.make_scope(8, topics=2)
        paths = (self.subject_section_path(scope), self.legacy_section_path(scope))
        for who in (self.student, self.teacher, self.admin):
            for path in paths:
                with self.subTest(role=who.role, path=path):
                    response = self.client(who).get(path)
                    html = self.page(response)
                    self.assertEqual(response.status_code, 200)
                    forms = self.entry_forms(html)
                    self.assertEqual([action for action, _ in forms], [self.start_url(scope)])
                    self.assertIn('<button type="submit">Упражнявай се</button>', forms[0][1])
                    self.assertNotIn("<input", forms[0][1])
                    self.assertEqual(html.count("Упражнявай се"), 1)

    def test_a_signed_in_account_gets_one_post_form_on_a_topic_page_for_the_resolved_topic(self):
        scope = self.make_scope(6, topics=2)
        for index in (0, 1):
            path = self.subject_topic_path(scope, index)
            for who in (self.student, self.teacher, self.admin):
                with self.subTest(role=who.role, topic=index):
                    response = self.client(who).get(path)
                    self.assertEqual(response.status_code, 200)
                    forms = self.entry_forms(self.page(response))
                    self.assertEqual([action for action, _ in forms], [self.start_url(scope, index)])

    def test_the_route_of_every_form_is_the_start_route_of_that_scope(self):
        scope = self.make_scope(6, topics=2)
        client = self.client(self.student)
        for path, expected_rule in ((self.subject_section_path(scope), "practice.start_section"),
                                    (self.legacy_section_path(scope), "practice.start_section"),
                                    (self.subject_topic_path(scope, 1), "practice.start_topic")):
            action = self.entry_forms(self.page(client.get(path)))[0][0]
            adapter = web_app.app.url_map.bind("localhost")
            endpoint, values = adapter.match(action, method="POST")
            self.assertEqual(endpoint, expected_rule)
            self.assertEqual(values["section_id"], scope.section_id)
            if expected_rule == "practice.start_topic":
                self.assertEqual(values["topic_id"], scope.topic_ids[1])

    def test_a_signed_out_visitor_gets_the_public_page_without_any_entry(self):
        scope = self.make_scope(8, topics=2)
        pending = self.client()
        with pending.session_transaction() as session:
            session.update(mfa_uid=self.admin.user_id, mfa_sk="x", mfa_iat=1_700_000_000)
        for client in (self.client(), pending):
            for path in (self.subject_section_path(scope), self.subject_topic_path(scope, 1),
                         self.legacy_section_path(scope)):
                with self.subTest(path=path):
                    response = client.get(path)
                    html = self.page(response)
                    self.assertEqual(response.status_code, 200)
                    self.assertNotIn("/practice/", html)
                    self.assertNotIn("Упражнявай се", html)

    def test_signed_out_pages_never_reach_the_practice_scope_check(self):
        scope = self.make_scope(8)
        with patch.object(practice, "practice_scope_available", side_effect=AssertionError("queried")), \
                patch.object(practice, "read_practice_progress", side_effect=AssertionError("queried")):
            response = self.client().get(self.subject_section_path(scope))
        self.assertEqual(response.status_code, 200)

    def test_dzi_training_and_unknown_subject_sections_get_no_entry(self):
        dzi = self.make_scope(8, section_type="dzi_training")
        content = self.make_scope(8)
        # A section whose subject is unknown: its curriculum is removed from the section.
        orphan = self.make_scope(8)
        self.conn.execute("UPDATE curriculum_sections SET curriculum_id = NULL WHERE id = ?", (orphan.section_id,))
        self.conn.commit()
        client = self.client(self.admin)
        # The pages themselves exist (200), so the missing form is the scope check's doing, not a 404. The
        # orphan has no curriculum, so only the legacy section page still serves it.
        expected = {(dzi.section_id, "section"): 200, (dzi.section_id, "legacy"): 200, (dzi.section_id, "topic"): 200,
                    (orphan.section_id, "section"): 404, (orphan.section_id, "legacy"): 200,
                    (orphan.section_id, "topic"): 404}
        for scope in (dzi, orphan):
            for kind, path in (("section", self.subject_section_path(scope)),
                               ("legacy", self.legacy_section_path(scope)),
                               ("topic", self.subject_topic_path(scope))):
                with self.subTest(section=scope.section_id, path=path):
                    response = client.get(path)
                    self.assertEqual(response.status_code, expected[(scope.section_id, kind)])
                    self.assertNotIn("/practice/", self.page(response))
        # The control: a normal section of the same shape does show it.
        self.assertTrue(self.entry_forms(self.page(client.get(self.subject_section_path(content)))))

    def test_the_real_dzi_preparation_section_and_placeholder_groups_get_no_entry(self):
        client = self.client(self.admin)
        paths = [f"/subjects/{IT_SLUG}/12/grade12-dzi-preparation"]
        # A placeholder group has no stored section: the page is the configured shell, with no section id.
        with patch.object(browsing, "subject_content_browser", return_value=None), \
                patch.object(practice, "practice_scope_available", side_effect=AssertionError("queried")), \
                patch.object(practice, "read_practice_progress", side_effect=AssertionError("queried")):
            placeholder = client.get("/subjects/physics/9/mechanics")
        self.assertEqual(placeholder.status_code, 200)
        self.assertIn("Тематична група в подготовка", self.page(placeholder))
        self.assertNotIn("/practice/", self.page(placeholder))
        for path in paths:
            with self.subTest(path=path):
                response = client.get(path)
                self.assertEqual(response.status_code, 200)
                self.assertNotIn("/practice/", self.page(response))
        dzi_slug = self.conn.execute("SELECT section_slug FROM curriculum_sections WHERE section_type = 'dzi_training' "
                                     "AND section_slug NOT LIKE 'practice-%' LIMIT 1").fetchone()
        self.assertIsNotNone(dzi_slug)
        legacy = client.get(f"/section/{dzi_slug[0]}")
        self.assertEqual(legacy.status_code, 200)
        self.assertNotIn("/practice/", self.page(legacy))

    def test_invalid_scopes_have_no_entry_url(self):
        scope, other = self.make_scope(6), self.make_scope(6)
        dzi = self.make_scope(6, section_type="dzi_training")
        none = {"practice_start_url": None, "practice_progress": None}
        with web_app.app.test_request_context("/"):
            g.principal = self.student
            self.assertEqual(browsing.practice_entry_context(scope.section_id), {
                "practice_start_url": f"/practice/section/{scope.section_id}/start",
                "practice_progress": practice.PracticeProgress(scope.section_id, None)})
            self.assertEqual(browsing.practice_entry_context(scope.section_id, scope.topic_ids[0]), {
                "practice_start_url": f"/practice/section/{scope.section_id}/topic/{scope.topic_ids[0]}/start",
                "practice_progress": practice.PracticeProgress(scope.section_id, scope.topic_ids[0])})
            for section_id, topic_id in ((987654, None), (0, None), (-1, None), (True, None), ("1", None),
                                         (2 ** 63, None), (dzi.section_id, None), (dzi.section_id, dzi.topic_ids[0]),
                                         (scope.section_id, other.topic_ids[0]), (scope.section_id, 987654),
                                         (scope.section_id, 0), (scope.section_id, True)):
                with self.subTest(section=section_id, topic=topic_id):
                    self.assertEqual(browsing.practice_entry_context(section_id, topic_id), none)
            g.principal = ANONYMOUS
            self.assertEqual(browsing.practice_entry_context(scope.section_id), none)

    def test_the_entry_does_not_count_questions_so_thin_scopes_still_show_it(self):
        thin = self.make_scope(2, topics=2)
        empty = self.make_scope(0)
        client = self.client(self.student)
        with patch.object(practice, "_eligible_question_ids", side_effect=AssertionError("counted")), \
                patch.object(practice, "start_or_resume_practice", side_effect=AssertionError("started")):
            for scope, paths in ((thin, (self.subject_section_path(thin), self.subject_topic_path(thin, 1),
                                         self.legacy_section_path(thin))),
                                 (empty, (self.subject_section_path(empty), self.legacy_section_path(empty)))):
                for path in paths:
                    with self.subTest(path=path):
                        response = client.get(path)
                        self.assertEqual(response.status_code, 200)
                        self.assertEqual(len(self.entry_forms(self.page(response))), 1)

    def test_an_account_with_an_open_attempt_still_sees_the_entry_and_nothing_is_written(self):
        scope = self.make_scope(8)
        self.open_via_route(self.student, scope)
        before = self.table_digests()
        html = self.page(self.client(self.student).get(self.subject_section_path(scope)))
        self.assertEqual([action for action, _ in self.entry_forms(html)], [self.start_url(scope)])
        self.assertEqual(self.table_digests(), before)

    def test_public_browsing_and_the_teacher_actions_are_unchanged_beside_the_entry(self):
        scope = self.make_scope(8)
        anonymous = self.page(self.client().get(self.legacy_section_path(scope)))
        teacher = self.page(self.client(self.teacher).get(self.legacy_section_path(scope)))
        for html in (anonymous, teacher):
            self.assertIn("/teacher/new?section=", html)
            self.assertEqual(len(re.findall(r'<article\s+class="question"\s+data-question-id="\d+"', html)), 8)
        self.assertEqual(len(self.entry_forms(teacher)), 1)
        self.assertEqual(self.entry_forms(anonymous), [])

    def test_the_account_and_the_other_navigation_pages_stay_free_of_the_form(self):
        scope = self.make_scope(8)
        client = self.client(self.student)
        for path in ("/", "/grade/8", f"/subjects/{IT_SLUG}", f"/subjects/{IT_SLUG}/8", "/account"):
            with self.subTest(path=path):
                self.assertEqual(self.entry_forms(self.page(client.get(path))), [])

    def test_the_browse_page_and_the_form_write_nothing(self):
        scope = self.make_scope(8, topics=2)
        before = self.table_digests()
        client = self.client(self.student)
        for path in (self.subject_section_path(scope), self.subject_topic_path(scope, 1),
                     self.legacy_section_path(scope)):
            self.assertEqual(client.get(path).status_code, 200)
        self.assertEqual(self.table_digests(), before)


@armed_only
class BrowseProgressTest(RouteCase, BrowseUrls):
    """The account's own saved progress beside the entry, on the real application with signed-in clients."""

    COUNT = re.compile(r'<span class="pill">Приключени упражнения: (\d+)</span>')
    LAST = re.compile(r'<span class="pill">Последен резултат: (\d+)/(\d+)</span>')
    PILLS = re.compile(r'\n<span class="pill">(?:Приключени упражнения|Последен резултат): [^<]*</span>')

    def add_session(self, who, scope, topic_index=None, *, submitted=None, abandoned=None, score=None):
        topic_id = None if topic_index is None else scope.topic_ids[topic_index]
        correct, total = score if score else (None, None)
        self.conn.execute(
            "INSERT INTO practice_sessions (user_id, section_id, topic_id, seed, question_ids_json, started_at, "
            "submitted_at, abandoned_at, score_correct, score_total) VALUES (?, ?, ?, 's', '[]', ?, ?, ?, ?, ?)",
            (who.user_id, scope.section_id, topic_id, STAMP, submitted, abandoned, correct, total))
        self.conn.commit()

    def paths(self, scope, topic_index=None):
        if topic_index is None:
            return (self.subject_section_path(scope), self.legacy_section_path(scope))
        return (self.subject_topic_path(scope, topic_index),)

    def shown(self, html):
        """``(count, correct, total)`` of the one progress display of a page, ``None`` when it shows none."""
        counts, lasts = self.COUNT.findall(html), self.LAST.findall(html)
        self.assertEqual(len(counts), len(lasts))
        self.assertLessEqual(len(counts), 1)
        return (int(counts[0]), int(lasts[0][0]), int(lasts[0][1])) if counts else None

    def get(self, who, path):
        response = self.client(who).get(path)
        self.assertEqual(response.status_code, 200)
        return self.page(response)

    def test_nothing_is_shown_while_nothing_was_submitted_and_the_entry_stays(self):
        scope = self.make_scope(8, topics=2)
        self.add_session(self.student, scope)                                              # open
        self.add_session(self.student, scope, 0, abandoned="2026-02-01 10:00:00")         # abandoned
        for who in (self.student, self.teacher, self.admin):
            for path in self.paths(scope) + self.paths(scope, 0) + self.paths(scope, 1):
                with self.subTest(role=who.role, path=path):
                    html = self.get(who, path)
                    self.assertIsNone(self.shown(html))
                    self.assertNotIn("Приключени упражнения", html)
                    self.assertNotIn("Последен резултат", html)
                    self.assertEqual(len(self.entry_forms(html)), 1)

    def test_a_submitted_session_shows_its_count_and_saved_score_on_every_page_of_its_scope(self):
        scope = self.make_scope(8, topics=2)
        self.add_session(self.student, scope, submitted="2026-01-01 10:00:00", score=(6, 8))
        for path in self.paths(scope):
            with self.subTest(path=path):
                html = self.get(self.student, path)
                self.assertEqual(self.shown(html), (1, 6, 8))
                form = self.entry_forms(html)
                self.assertEqual([action for action, _ in form], [self.start_url(scope)])
                self.assertIn("Приключени упражнения: 1", html)
                self.assertIn("Последен резултат: 6/8", html)
        for path in self.paths(scope, 0) + self.paths(scope, 1):
            self.assertIsNone(self.shown(self.get(self.student, path)))

    def test_a_real_submission_and_a_repeat_through_the_routes_show_the_count_and_the_latest_saved_score(self):
        scope = self.make_scope(8)
        path = self.subject_section_path(scope)
        first = self.open_via_route(self.student, scope)
        fields = self.answer_all(first, "A")
        self.assertEqual(self.post(self.client(self.student), f"/practice/session/{first}", fields).status_code, 303)
        total = len(self.plan_of(first))
        self.assertEqual(self.shown(self.get(self.student, path)), (1, total, total))
        repeat = self.open_via_route(self.student, scope)
        self.assertNotEqual(repeat, first)
        self.assertEqual(self.shown(self.get(self.student, path)), (1, total, total))     # open: not counted yet
        fields = self.answer_all(repeat, "B")
        self.assertEqual(self.post(self.client(self.student), f"/practice/session/{repeat}", fields).status_code, 303)
        # Both submissions may fall in the same stored second: the larger id is the latest.
        self.assertEqual(self.shown(self.get(self.student, path)), (2, 0, total))
        for scope_path in self.paths(scope):
            self.assertEqual(self.shown(self.get(self.student, scope_path)), (2, 0, total))

    def test_a_topic_is_its_own_scope_and_there_is_no_roll_up_into_its_section(self):
        scope = self.make_scope(8, topics=2)
        self.add_session(self.student, scope, submitted="2026-01-01 10:00:00", score=(1, 5))
        self.add_session(self.student, scope, 0, submitted="2026-01-02 10:00:00", score=(2, 6))
        self.add_session(self.student, scope, 0, submitted="2026-01-03 10:00:00", score=(3, 7))
        self.add_session(self.student, scope, 1, submitted="2026-01-04 10:00:00", score=(4, 8))
        expected = {None: (1, 1, 5), 0: (2, 3, 7), 1: (1, 4, 8)}
        for index, numbers in expected.items():
            for path in self.paths(scope, index):
                with self.subTest(topic=index, path=path):
                    self.assertEqual(self.shown(self.get(self.student, path)), numbers)
        only_topics = self.make_scope(8, topics=2)
        self.add_session(self.student, only_topics, 0, submitted=STAMP, score=(5, 5))
        for path in self.paths(only_topics):
            self.assertIsNone(self.shown(self.get(self.student, path)))
        self.assertEqual(self.shown(self.get(self.student, self.subject_topic_path(only_topics, 0))), (1, 5, 5))

    def test_the_latest_is_the_stored_utc_time_and_then_the_larger_id(self):
        scope = self.make_scope(8)
        self.add_session(self.student, scope, submitted="2026-03-01 12:00:00", score=(3, 4))
        self.add_session(self.student, scope, submitted="2026-01-01 12:00:00", score=(9, 10))
        self.add_session(self.student, scope, submitted="2026-03-01 12:00:00", score=(2, 4))   # tie: the larger id
        self.add_session(self.student, scope, submitted="2025-12-31 23:59:59", score=(1, 10))
        for path in self.paths(scope):
            self.assertEqual(self.shown(self.get(self.student, path)), (4, 2, 4))

    def test_each_account_and_role_sees_only_its_own_numbers(self):
        scope = self.make_scope(8)
        self.add_session(self.student, scope, submitted="2026-01-01 10:00:00", score=(3, 5))
        self.add_session(self.student, scope, submitted="2026-01-02 10:00:00", score=(4, 5))
        self.add_session(self.other_student, scope, submitted="2026-02-01 10:00:00", score=(9, 11))
        self.add_session(self.teacher, scope, submitted="2026-03-01 10:00:00", score=(1, 3))
        self.add_session(self.teacher, scope, submitted="2026-03-02 10:00:00", score=(2, 3))
        self.add_session(self.teacher, scope, submitted="2026-03-03 10:00:00", score=(3, 3))
        expected = {self.student: (2, 4, 5), self.other_student: (1, 9, 11), self.teacher: (3, 3, 3), self.admin: None}
        for path in self.paths(scope):
            for who, numbers in expected.items():
                with self.subTest(role=who.role, user=who.user_id, path=path):
                    self.assertEqual(self.shown(self.get(who, path)), numbers)

    def test_a_page_is_byte_identical_whatever_another_account_has_submitted(self):
        scope = self.make_scope(8, topics=2)
        self.add_session(self.student, scope, submitted="2026-01-01 10:00:00", score=(3, 5))
        paths = self.paths(scope) + self.paths(scope, 0)
        before = {path: self.get(self.student, path) for path in paths}
        for index in (None, 0, 1):
            for number in range(6):
                self.add_session(self.other_student, scope, index, submitted=f"2026-02-0{number + 1} 10:00:00",
                                 score=(number, 17))
            self.add_session(self.teacher, scope, index, submitted="2026-04-01 10:00:00", score=(7, 19))
            self.add_session(self.admin, scope, index, abandoned="2026-04-01 10:00:00")
            self.add_session(self.other_student, scope, index)                                   # open
        after = {path: self.get(self.student, path) for path in paths}
        self.assertEqual(after, before)
        for html in after.values():
            self.assertNotIn("/17", html)
            self.assertNotIn("/19", html)

    def test_the_open_and_abandoned_state_of_the_account_is_not_exposed(self):
        scope = self.make_scope(8, topics=2)
        self.add_session(self.student, scope, submitted="2026-01-01 10:00:00", score=(3, 5))
        paths = self.paths(scope)
        before = {path: self.get(self.student, path) for path in paths}
        self.add_session(self.student, scope)                                                      # open
        self.add_session(self.student, scope, abandoned="2026-05-01 10:00:00")
        self.add_session(self.student, scope, 0)
        self.assertEqual({path: self.get(self.student, path) for path in paths}, before)

    def test_a_saved_score_survives_every_content_change_on_the_page(self):
        scope = self.make_scope(8, topics=2)
        self.add_session(self.student, scope, submitted="2026-01-01 10:00:00", score=(6, 8))
        self.add_session(self.student, scope, 1, submitted="2026-01-02 10:00:00", score=(7, 8))
        paths = (self.subject_section_path(scope), self.subject_topic_path(scope, 1))
        before = {path: self.get(self.student, path) for path in paths}
        self.assertEqual([self.shown(html) for html in before.values()], [(1, 6, 8), (1, 7, 8)])
        for question_id in scope.question_ids[1]:
            self.break_question(question_id)
        self.conn.execute("UPDATE questions SET prompt = 'changed' WHERE id = ?", (scope.question_ids[0][0],))
        self.conn.commit()
        for path in paths:
            self.assertEqual(self.shown(self.get(self.student, path)), self.shown(before[path]))
        self.conn.execute("DELETE FROM multiple_choice_options")
        self.conn.commit()   # no question is left usable at all: the entry and the saved score both stay
        for path in paths:
            html = self.get(self.student, path)
            self.assertEqual(self.shown(html), self.shown(before[path]))
            self.assertEqual(len(self.entry_forms(html)), 1)

    def test_a_thin_scope_still_shows_its_saved_progress_and_the_entry(self):
        scope = self.make_scope(2, topics=2)
        self.add_session(self.student, scope, 1, submitted=STAMP, score=(2, 2))
        html = self.get(self.student, self.subject_topic_path(scope, 1))
        self.assertEqual(self.shown(html), (1, 2, 2))
        self.assertEqual(len(self.entry_forms(html)), 1)

    def test_unavailable_scopes_show_neither_the_entry_nor_stored_progress(self):
        dzi = self.make_scope(6, section_type="dzi_training")
        self.add_session(self.student, dzi, submitted=STAMP, score=(3, 3))
        html = self.get(self.student, self.legacy_section_path(dzi))
        self.assertIsNone(self.shown(html))
        self.assertEqual(self.entry_forms(html), [])
        self.assertNotIn("Приключени упражнения", html)

    def test_the_form_and_the_page_are_unchanged_apart_from_the_two_pills(self):
        scope = self.make_scope(8, topics=2)
        paths = self.paths(scope) + self.paths(scope, 1)
        before = {path: self.get(self.student, path) for path in paths}
        self.add_session(self.student, scope, submitted=STAMP, score=(3, 5))
        self.add_session(self.student, scope, 1, submitted=STAMP, score=(4, 5))
        for path in paths:
            with self.subTest(path=path):
                after = self.get(self.student, path)
                self.assertEqual(len(self.PILLS.findall(after)), 2)
                self.assertEqual(self.PILLS.sub("", after), before[path])
                self.assertEqual(self.entry_forms(after), self.entry_forms(before[path]))
                self.assertEqual(after.count("Упражнявай се"), 1)

    def test_the_progress_text_is_two_short_bulgarian_pills_beside_the_form(self):
        scope = self.make_scope(8)
        self.add_session(self.student, scope, submitted=STAMP, score=(0, 5))
        html = self.get(self.student, self.subject_section_path(scope))
        self.assertIn('<span class="pill">Приключени упражнения: 1</span>', html)
        self.assertIn('<span class="pill">Последен резултат: 0/5</span>', html)
        block = re.search(r"</form>\s*(<span.*?)</div>", html, re.S).group(1)
        self.assertEqual(len(re.findall(r"<span", block)), 2)
        self.assertFalse(re.search(r"[A-Za-z]{4,}", re.sub(r"<[^>]+>", "", block)))   # no English words

    def test_one_service_read_decides_the_scope_and_the_progress_so_the_scope_is_checked_once(self):
        scope = self.make_scope(8, topics=2)
        self.add_session(self.student, scope, 1, submitted=STAMP, score=(3, 5))
        client = self.client(self.student)
        for path in self.paths(scope) + self.paths(scope, 1):
            with self.subTest(path=path):
                with patch.object(practice, "practice_scope_available", side_effect=AssertionError("second check")), \
                        patch.object(practice, "read_practice_scope", side_effect=AssertionError("second check")), \
                        patch.object(practice, "_resolve_scope", wraps=practice._resolve_scope) as resolve, \
                        patch.object(practice, "read_practice_progress", wraps=practice.read_practice_progress) as read:
                    self.assertEqual(client.get(path).status_code, 200)
                self.assertEqual(resolve.call_count, 1)
                self.assertEqual(read.call_count, 1)
                self.assertEqual(read.call_args.args[1].user_id, self.student.user_id)

    def test_the_account_id_of_the_read_is_the_trusted_principal_and_a_query_parameter_changes_nothing(self):
        scope = self.make_scope(8)
        self.add_session(self.student, scope, submitted=STAMP, score=(3, 5))
        self.add_session(self.other_student, scope, submitted=STAMP, score=(5, 5))
        path = self.subject_section_path(scope)
        for query in (f"?user_id={self.other_student.user_id}", f"?account={self.other_student.user_id}",
                      f"?student={self.other_student.user_id}&practice_progress=5/5"):
            with self.subTest(query=query):
                self.assertEqual(self.shown(self.get(self.student, path + query)), (1, 3, 5))
        client = self.client(self.student)
        response = client.get(path, headers={"X-User-Id": str(self.other_student.user_id)})
        self.assertEqual(self.shown(self.page(response)), (1, 3, 5))

    def test_signed_in_pages_are_never_cacheable_and_no_account_data_enters_the_public_page(self):
        scope = self.make_scope(8)
        path = self.subject_section_path(scope)
        public_before = self.get(None, path)
        self.add_session(self.student, scope, submitted=STAMP, score=(3, 5))
        signed = self.client(self.student).get(path)
        self.assertEqual(signed.headers["Cache-Control"], "no-store")
        self.assertEqual(self.shown(self.page(signed)), (1, 3, 5))
        anonymous = self.client().get(path)
        self.assertEqual(self.page(anonymous), public_before)
        self.assertNotIn("no-store", anonymous.headers.get("Cache-Control", ""))
        self.assertIsNone(self.shown(self.page(anonymous)))
        self.assertEqual(self.shown(self.get(self.other_student, path)), None)
        # The second signed-in render of the other account after the first still carries only its own numbers.
        self.add_session(self.other_student, scope, submitted=STAMP, score=(5, 5))
        self.assertEqual(self.shown(self.get(self.other_student, path)), (1, 5, 5))
        self.assertEqual(self.shown(self.get(self.student, path)), (1, 3, 5))

    def test_a_signed_out_or_pending_visitor_gets_exactly_the_page_without_any_practice_sql(self):
        scope = self.make_scope(8, topics=2)
        self.add_session(self.student, scope, submitted=STAMP, score=(3, 5))
        pending = self.client()
        with pending.session_transaction() as session:
            session.update(mfa_uid=self.admin.user_id, mfa_sk="x", mfa_iat=1_700_000_000)
        paths = self.paths(scope) + self.paths(scope, 1)
        for client in (self.client(), pending):
            for path in paths:
                with self.subTest(path=path):
                    statements = self.record_sql(lambda: client.get(path).get_data())
                    self.assertFalse([s for s in statements if "practice_" in s.lower()], statements)

    def test_the_signed_in_page_adds_only_the_two_scoped_reads_and_the_scope_decision(self):
        scope = self.make_scope(8, topics=2)
        self.add_session(self.student, scope, 1, submitted=STAMP, score=(3, 5))
        client = self.client(self.student)
        # Topic 0 was never practised: the count alone answers it. The others also read the latest saved score.
        for path, reads in ((self.subject_section_path(scope), 1), (self.legacy_section_path(scope), 1),
                            (self.subject_topic_path(scope, 0), 1), (self.subject_topic_path(scope, 1), 2)):
            with self.subTest(path=path):
                statements = self.record_sql(lambda: client.get(path).get_data())
                practice_sql = [s for s in statements if "practice_" in s.lower()]
                self.assertEqual(len(practice_sql), reads, practice_sql)
                self.assertTrue(practice_sql[0].lstrip().upper().startswith("SELECT COUNT(*)"))
                if reads == 2:
                    self.assertTrue(practice_sql[1].lstrip().upper().startswith("SELECT SCORE_CORRECT"))
                for statement in practice_sql:
                    self.assertIn("user_id =", statement)
                    self.assertIn("section_id =", statement)
                    self.assertIn("topic_id IS", statement)
                    self.assertNotIn("practice_answers", statement)
                keywords = [s.split()[0].upper() for s in statements]
                self.assertFalse({"INSERT", "UPDATE", "DELETE", "REPLACE", "COMMIT"} & set(keywords), statements)
                self.assertEqual(keywords.count("BEGIN"), 1)
                self.assertEqual(keywords.count("ROLLBACK"), 1)

    def test_the_progress_pages_write_nothing(self):
        scope = self.make_scope(8, topics=2)
        self.add_session(self.student, scope, submitted=STAMP, score=(3, 5))
        self.add_session(self.student, scope, 1)
        before = self.table_digests()
        client = self.client(self.student)
        for path in self.paths(scope) + self.paths(scope, 0) + self.paths(scope, 1):
            self.assertEqual(client.get(path).status_code, 200)
            self.assertEqual(client.head(path).status_code, 200)
        self.assertEqual(self.table_digests(), before)

    def test_a_deactivated_account_is_signed_out_and_sees_neither_the_entry_nor_its_progress(self):
        scope = self.make_scope(8)
        self.add_session(self.student, scope, submitted=STAMP, score=(3, 5))
        path = self.subject_section_path(scope)
        client = self.client(self.student)
        self.assertEqual(self.shown(self.page(client.get(path))), (1, 3, 5))
        self.conn.execute("UPDATE users SET is_active = 0 WHERE id = ?", (self.student.user_id,))
        self.conn.commit()
        html = self.page(client.get(path))
        self.assertIsNone(self.shown(html))
        self.assertEqual(self.entry_forms(html), [])

    def test_refused_stored_state_shows_neither_a_number_nor_a_server_error_and_logs_the_class_only(self):
        scope = self.make_scope(8)
        self.add_session(self.student, scope, submitted=STAMP, score=(3, 5))
        self.conn.execute("PRAGMA ignore_check_constraints = ON")
        self.conn.execute("UPDATE practice_sessions SET score_total = 0")
        self.conn.execute("PRAGMA ignore_check_constraints = OFF")
        self.conn.commit()
        with self.assertLogs(web_app.app.logger, level="ERROR") as logs:
            response = self.client(self.student).get(self.subject_section_path(scope))
        self.assertEqual(response.status_code, 200)
        html = self.page(response)
        self.assertIsNone(self.shown(html))
        self.assertEqual(self.entry_forms(html), [])
        self.assertEqual([record.args for record in logs.records],
                         [("browsing.subject_topic_group_view", "PracticeStateError")])
        self.assertNotIn("whole and bounded", "\n".join(record.getMessage() for record in logs.records))
        self.assertEqual(self.entry_forms(self.page(self.client().get(self.subject_section_path(scope)))), [])

    def record_sql(self, action):
        """Every SQL statement the request connection runs while ``action`` performs one page request."""
        statements, original = [], browsing.get_request_db

        def traced():
            conn = original()
            conn.set_trace_callback(statements.append)
            return conn
        with patch.object(browsing, "get_request_db", traced):
            action()
        return statements


# ====================================================================== armed: the read-only confirmation
@armed_only
class ConfirmRouteTest(RouteCase, BrowseUrls):
    def test_a_signed_in_account_gets_the_confirmation_of_the_exact_scope(self):
        scope = self.make_scope(8, topics=2)
        section_title = "Synthetic section"
        for who in (self.student, self.teacher, self.admin):
            for index, expected_topic in ((None, None), (1, self.topic_slug(scope, 1))):
                path = self.start_url(scope, index)
                with self.subTest(role=who.role, path=path):
                    response = self.client(who).get(path)
                    html = self.page(response)
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.headers["Cache-Control"], "no-store")
                    self.assertIn('id="practice-start-confirm"', html)
                    self.assertIn(f"<strong>{section_title}</strong>", html)
                    if expected_topic:
                        self.assertIn(f"<strong>{expected_topic}</strong>", html)
                    else:
                        self.assertNotIn("Тема:", html)
                    forms = self.entry_forms(html)
                    self.assertEqual([action for action, _ in forms], [path])
                    self.assertNotIn("<input", forms[0][1])
                    self.assertIn("Упражнявай се", forms[0][1])
                    self.assertIn('<html lang="bg">', html)

    def test_get_and_head_and_options_write_nothing_and_start_nothing(self):
        scope = self.make_scope(8, topics=2)
        before = self.table_digests()
        client = self.client(self.student)
        with patch.object(practice, "start_or_resume_practice", side_effect=AssertionError("started")), \
                patch.object(practice, "submit_practice", side_effect=AssertionError("submitted")):
            for path in (self.start_url(scope), self.start_url(scope, 0)):
                for method in ("get", "head", "options"):
                    with self.subTest(path=path, method=method):
                        response = getattr(client, method)(path)
                        self.assertEqual(response.status_code, 200)
                        if method == "head":
                            self.assertEqual(response.get_data(), b"")
        self.assertEqual(self.table_digests(), before)
        self.assertEqual(self.sessions(), [])

    def test_the_connection_of_the_confirmation_refuses_writes(self):
        scope = self.make_scope(8)
        before = self.table_digests()

        def writes(conn, section_id, topic_id=None):
            conn.execute("DELETE FROM practice_sessions")

        # The engine refuses the write (the test client re-raises; production answers its generic 500).
        with patch.object(practice, "read_practice_scope", side_effect=writes), \
                self.assertRaisesRegex(sqlite3.OperationalError, "readonly"):
            self.client(self.student).get(self.start_url(scope))
        self.assertEqual(self.table_digests(), before)

    def test_an_open_stale_or_thin_state_is_neither_changed_nor_revealed(self):
        scope = self.make_scope(12)
        client = self.client(self.student)
        open_id = self.open_via_route(self.student, scope)
        stale_scope = self.make_scope(8)
        stale = self.insert_session(self.teacher, stale_scope, stale_scope.all_question_ids[:3])
        for question_id in stale_scope.all_question_ids[:3]:
            self.break_question(question_id)
        thin = self.make_scope(3)
        self.conn.commit()
        before = self.table_digests()
        pages = []
        for who, target in ((self.student, scope), (self.teacher, stale_scope), (self.student, thin)):
            for method in ("get", "head"):
                response = getattr(self.client(who), method)(self.start_url(target))
                self.assertEqual(response.status_code, 200, (who.role, method))
                pages.append(self.page(response))
        self.assertEqual(self.table_digests(), before)
        self.assertIsNone(self.stored(stale)[0]["abandoned_at"])
        self.assertIsNone(self.stored(open_id)[0]["abandoned_at"])
        for html in pages:
            self.assertNotIn(f"/practice/session/{open_id}", html)
            self.assertNotIn("достатъчно въпроси", html)
            self.assertNotIn("не може да бъде показано", html)
        # The confirmation of an account with an open attempt and of one without is the same content.
        def content(response):
            return self.page(response).split('<section class="page-header">')[1].split("<footer")[0]

        self.assertEqual(content(client.get(self.start_url(scope))),
                         content(self.client(self.admin).get(self.start_url(scope))))

    def test_signed_out_and_pending_second_factor_are_sent_to_login(self):
        scope = self.make_scope(8, topics=2)
        before = self.table_digests()
        pending = self.client()
        with pending.session_transaction() as session:
            session.update(mfa_uid=self.admin.user_id, mfa_sk="x", mfa_iat=1_700_000_000)
        for client in (self.client(), pending):
            for path in (self.start_url(scope), self.start_url(scope, 1)):
                for method in ("get", "head"):
                    with self.subTest(path=path, method=method):
                        self.assert_login_redirect(getattr(client, method)(path), path)
        self.assertEqual(self.table_digests(), before)

    def test_an_unavailable_scope_is_the_same_404_page_and_writes_nothing(self):
        scope, other = self.make_scope(6), self.make_scope(6)
        dzi = self.make_scope(8, section_type="dzi_training")
        retired = self.make_scope(8)
        session_id = self.insert_session(self.student, retired, retired.all_question_ids[:3])
        self.conn.execute("UPDATE curriculum_sections SET section_type = 'dzi_training' WHERE id = ?",
                          (retired.section_id,))
        self.conn.commit()
        before = self.table_digests()
        client = self.client(self.student)
        paths = (f"/practice/section/{dzi.section_id}/start", f"/practice/section/{retired.section_id}/start",
                 "/practice/section/987654/start", "/practice/section/0/start",
                 "/practice/section/99999999999999999999/start",
                 f"/practice/section/{scope.section_id}/topic/{other.topic_ids[0]}/start",
                 f"/practice/section/{scope.section_id}/topic/987654/start",
                 f"/practice/section/{scope.section_id}/topic/0/start",
                 f"/practice/section/987654/topic/{scope.topic_ids[0]}/start")
        for path in paths:
            for method in ("get", "head"):
                with self.subTest(path=path, method=method):
                    response = getattr(client, method)(path)
                    self.assertEqual(response.status_code, 404)
                    if method == "get":
                        self.assertIn("Това упражнение не е налично.", self.page(response))
                        self.assertEqual(self.entry_forms(self.page(response)), [])
        self.assertEqual(self.table_digests(), before)
        self.assertIsNone(self.stored(session_id)[0]["abandoned_at"])

    def test_nothing_in_the_request_changes_the_scope_the_page_offers(self):
        scope = self.make_scope(8, topics=2)
        other = self.make_scope(8)
        path = self.start_url(scope, 1)
        query = {"section_id": other.section_id, "topic_id": other.topic_ids[0], "user_id": 1, "role": "admin",
                 "next": "//evil.example/x", "action": "/practice/section/1/start"}
        response = self.client(self.student).get(path, query_string=query,
                                                 headers={"Referer": "http://evil.example/x"})
        html = self.page(response)
        self.assertEqual(response.status_code, 200)
        self.assertEqual([action for action, _ in self.entry_forms(html)], [path])
        self.assertNotIn("evil.example", html.split("<main")[-1] if "<main" in html else html[-4000:])

    def test_the_confirmation_post_is_the_unchanged_start_behind_the_same_origin_check(self):
        scope = self.make_scope(8)
        path = self.start_url(scope)
        client = self.client(self.student)
        self.assertEqual(client.get(path).status_code, 200)
        before = self.table_digests()
        for headers in (CROSS, {"Referer": "http://evil.example/x"}):
            self.assertEqual(self.post(client, path, headers=headers).status_code, 403)
        self.assertEqual(self.table_digests(), before)
        response = self.post(client, path)
        self.assertEqual(response.status_code, 303)
        self.assertRegex(response.headers["Location"], r"^/practice/session/\d+$")

    def test_the_confirmation_page_is_escaped_and_bulgarian(self):
        scope = self.make_scope(8, topics=2)
        self.conn.execute("UPDATE curriculum_sections SET title_bg = ? WHERE id = ?",
                          ("<b>Раздел</b> & \"x\"", scope.section_id))
        self.conn.execute("UPDATE curriculum_topics SET title_bg = ? WHERE id = ?",
                          ("<script>alert(3)</script>", scope.topic_ids[1]))
        self.conn.commit()
        html = self.page(self.client(self.student).get(self.start_url(scope, 1)))
        self.assertNotIn("<b>Раздел</b>", html)
        self.assertNotIn("<script>alert(3)</script>", html)
        self.assertIn("&lt;b&gt;Раздел&lt;/b&gt; &amp; &#34;x&#34;", html)
        self.assertIn("Упражнението е от въпроси с избор на отговор.", html)
        self.assertIn("ще продължиш него", html)


# ====================================================================== armed: the sign-in return
@armed_only
class SignInReturnTest(RouteCase, BrowseUrls):
    """The existing flow, end to end: signed-out POST, real sign-in with fixture credentials, GET, POST."""

    def account_with_password(self, role):
        password = secrets.token_urlsafe(18)  # fixture credential: generated per run, never stored or printed
        email = f"practice-return-{self.next()}@example.com"
        user_id = accounts_service.create_user(self.conn, display_name=f"Return {role}", email=email,
                                               password=password, role=role)
        self.conn.commit()
        return user_id, email, password

    def sign_in_return(self, client, path, email, password):
        """Signed-out same-origin POST, then the sign-in the redirect leads to; the redirect it ends with."""
        first = client.post(path, headers=SAME)
        self.assert_login_redirect(first, path)
        page = client.get(first.headers["Location"])
        self.assertEqual(page.status_code, 200)
        next_value = html_module.unescape(re.search(r'name="next" value="([^"]*)"', self.page(page)).group(1))
        self.assertEqual(next_value, path)
        return client.post("/login", data={"email": email, "password": password, "next": next_value},
                           headers=SAME)

    def test_post_then_login_then_get_confirmation_then_explicit_post(self):
        scope = self.make_scope(10, topics=2)
        for role in ("student", "teacher"):
            user_id, email, password = self.account_with_password(role)
            for index in (None, 1):
                path = self.start_url(scope, index)
                with self.subTest(role=role, path=path):
                    client = self.client()
                    sessions_before = self.sessions()
                    signed_in = self.sign_in_return(client, path, email, password)
                    self.assertEqual(signed_in.status_code, 302)
                    self.assertEqual(signed_in.headers["Location"], path)
                    # The sign-in did not replay the POST: still no session.
                    self.assertEqual(self.sessions(), sessions_before)
                    tables = self.table_digests()
                    confirmation = client.get(path)
                    self.assertEqual(confirmation.status_code, 200)  # the former 405
                    self.assertEqual([action for action, _ in self.entry_forms(self.page(confirmation))], [path])
                    self.assertEqual(client.head(path).status_code, 200)
                    self.assertEqual(self.table_digests(), tables)
                    self.assertEqual(self.sessions(), sessions_before)
                    started = self.post(client, path)
                    self.assertEqual(started.status_code, 303)
                    session_id = int(started.headers["Location"].rsplit("/", 1)[1])
                    row, _ = self.stored(session_id)
                    self.assertEqual((row["user_id"], row["section_id"], row["topic_id"]),
                                     (user_id, scope.section_id, None if index is None else scope.topic_ids[index]))
                    self.assertEqual(client.get(started.headers["Location"]).status_code, 200)

    def test_the_wrong_password_does_not_reach_the_confirmation_and_next_stays_a_local_path(self):
        scope = self.make_scope(8)
        _, email, _password = self.account_with_password("student")
        path = self.start_url(scope)
        client = self.client()
        failed = self.sign_in_return(client, path, email, secrets.token_urlsafe(18))
        self.assertEqual(failed.status_code, 200)
        self.assertNotIn("Location", failed.headers)
        self.assert_login_redirect(client.get(path), path)
        self.assertEqual(self.sessions(), [])
        # An off-site next is dropped by the unchanged login, never followed.
        loose = self.client().post("/login", data={"email": email, "password": "x" * 20,
                                                   "next": "//evil.example/x"}, headers=SAME)
        self.assertNotIn("evil.example", loose.headers.get("Location", ""))

    def test_an_open_attempt_is_resumed_only_by_the_explicit_post(self):
        scope = self.make_scope(12)
        user_id, email, password = self.account_with_password("student")
        client = self.client()
        path = self.start_url(scope)
        self.assertEqual(self.sign_in_return(client, path, email, password).status_code, 302)
        first = self.post(client, path)
        session_id = int(first.headers["Location"].rsplit("/", 1)[1])
        other_browser = self.client()
        self.assertEqual(self.sign_in_return(other_browser, path, email, password).status_code, 302)
        before = self.table_digests()
        self.assertEqual(other_browser.get(path).status_code, 200)
        self.assertEqual(self.table_digests(), before)
        resumed = self.post(other_browser, path)
        self.assertEqual(resumed.headers["Location"], f"/practice/session/{session_id}")
        self.assertEqual(len(self.sessions()), 1)

    def test_stale_and_thin_sessions_are_judged_by_the_post_alone(self):
        stale_scope = self.make_scope(8)
        user_id, email, password = self.account_with_password("student")
        stale = self.insert_session(account_principal(user_id, "student", "x"), stale_scope,
                                    stale_scope.all_question_ids[:3])
        for question_id in stale_scope.all_question_ids[:3]:
            self.break_question(question_id)
        thin = self.make_scope(3)
        self.conn.commit()
        client = self.client()
        stale_path, thin_path = self.start_url(stale_scope), self.start_url(thin)
        self.assertEqual(self.sign_in_return(client, stale_path, email, password).status_code, 302)
        before = self.table_digests()
        for path in (stale_path, thin_path):
            for method in ("get", "head"):
                self.assertEqual(getattr(client, method)(path).status_code, 200)
        self.assertEqual(self.table_digests(), before)
        self.assertIsNone(self.stored(stale)[0]["abandoned_at"])
        # Only the explicit POST decides: a replacement for the stale one, the message for the thin pool.
        replaced = self.post(client, stale_path)
        self.assertEqual(replaced.status_code, 303)
        self.assertIsNotNone(self.stored(stale)[0]["abandoned_at"])
        self.assertEqual(self.post(client, thin_path).status_code, 409)

    def test_a_thinned_scope_with_an_open_attempt_still_resumes_by_post(self):
        scope = self.make_scope(6)
        user_id, email, password = self.account_with_password("student")
        client = self.client()
        path = self.start_url(scope)
        self.assertEqual(self.sign_in_return(client, path, email, password).status_code, 302)
        session_id = int(self.post(client, path).headers["Location"].rsplit("/", 1)[1])
        for question_id in scope.all_question_ids[:3]:  # the pool is now below the minimum for a new session
            self.break_question(question_id)
        self.conn.commit()
        before = self.table_digests()
        self.assertEqual(client.get(path).status_code, 200)
        self.assertEqual(self.table_digests(), before)
        resumed = self.post(client, path)
        self.assertEqual(resumed.status_code, 303)
        self.assertEqual(resumed.headers["Location"], f"/practice/session/{session_id}")

    def test_a_cross_origin_post_after_the_sign_in_is_refused_and_a_signed_out_one_still_redirects(self):
        scope = self.make_scope(8)
        user_id, email, password = self.account_with_password("student")
        client = self.client()
        path = self.start_url(scope)
        self.sign_in_return(client, path, email, password)
        before = self.table_digests()
        self.assertEqual(client.get(path).status_code, 200)
        self.assertEqual(self.post(client, path, headers=CROSS).status_code, 403)
        self.assertEqual(self.sessions(), [])
        self.assertEqual(self.post(self.client(), path, headers=CROSS).status_code, 403)
        self.assert_login_redirect(self.post(self.client(), path), path)
        self.assertEqual(self.table_digests(), before)

    def test_submitted_sessions_the_result_and_the_repeat_are_unchanged_by_the_return(self):
        scope = self.make_scope(10)
        user_id, email, password = self.account_with_password("student")
        client = self.client()
        path = self.start_url(scope)
        self.sign_in_return(client, path, email, password)
        started = self.post(client, path)
        attempt = started.headers["Location"]
        session_id = int(attempt.rsplit("/", 1)[1])
        submitted = self.post(client, attempt, self.answer_all(session_id))
        self.assertEqual(submitted.headers["Location"], attempt + "/result")
        saved = self.stored(session_id)
        self.assertEqual(client.get(attempt + "/result").status_code, 200)
        self.assertEqual(self.post(client, attempt, self.answer_all(session_id)).status_code, 303)
        self.assertEqual(self.stored(session_id), saved)
        self.assertEqual(client.get(path).status_code, 200)  # the confirmation of a finished scope: still nothing
        self.assertEqual(self.stored(session_id), saved)
        repeat = self.post(client, path)
        self.assertEqual(repeat.status_code, 303)
        self.assertNotEqual(repeat.headers["Location"], attempt)
        self.assertEqual(self.stored(session_id), saved)


# ====================================================================== disabled: browse is untouched
@disabled_only
class DisabledBrowseTest(PracticeCase, BrowseUrls):
    """In this build the browse pages never look at practice: no route, no query, no form."""

    def setUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        super().setUp()
        stack.enter_context(patch.dict(web_app.app.config, {"TESTING": True, "DB_PATH": str(self.db_path)}))
        stack.enter_context(patch.object(web_app, "QUIZ_DB_PATH", self.db_path))

    def test_signed_in_pages_have_no_entry_and_never_reach_the_practice_service(self):
        scope = self.make_scope(8, topics=2)
        paths = (self.subject_section_path(scope), self.subject_topic_path(scope, 1), self.legacy_section_path(scope))
        with patch.object(practice, "practice_scope_available", side_effect=AssertionError("queried")), \
                patch.object(practice, "read_practice_scope", side_effect=AssertionError("queried")), \
                patch.object(practice, "read_practice_progress", side_effect=AssertionError("queried")):
            for who in (None, self.student, self.teacher, self.admin):
                client = _client_for(self, who)
                for path in paths:
                    with self.subTest(role=getattr(who, "role", None), path=path):
                        response = client.get(path)
                        self.assertEqual(response.status_code, 200)
                        html = response.get_data(as_text=True)
                        self.assertNotIn("/practice/", html)
                        self.assertNotIn("Упражнявай се", html)

    def test_the_entry_url_is_none_without_a_lookup_even_if_the_blueprint_were_armed_by_hand(self):
        scope = self.make_scope(6)
        with web_app.app.test_request_context("/"):
            g.principal = self.student
            none = {"practice_start_url": None, "practice_progress": None}
            self.assertFalse(practice.routes_armed())
            self.assertEqual(browsing.practice_entry_context(scope.section_id), none)
            # Armed by hand without the blueprint: still no entry and no url_for to an unregistered route.
            with patch.object(practice, "ROUTES_ACTIVATION", True), \
                    patch.object(practice, "read_practice_progress", side_effect=AssertionError("queried")):
                self.assertTrue(practice.routes_armed())
                self.assertEqual(browsing.practice_entry_context(scope.section_id), none)

    def test_the_browse_module_does_not_read_the_gate_or_the_environment(self):
        source = (ROOT / "web" / "blueprints" / "browsing.py").read_text(encoding="utf-8")
        self.assertNotIn("ROUTES_ACTIVATION", source)
        self.assertNotIn("os.environ", source)
        self.assertNotIn("getenv", source)
        self.assertNotIn("web.blueprints.practice", source)
