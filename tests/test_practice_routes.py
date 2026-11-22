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

import json
import os
import re
import subprocess
import sys
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from tests.auth_helpers import set_account_session
from tests.support import ROOT, IsolatedAppTestCase, isolated_app, web_app
from tests.test_practice_start_service import STAMP, PracticeCase, Scope  # noqa: F401  (fixtures only)
from web import db as web_db
from web.access import ANONYMOUS, policy_for_view
from web.request_integrity import integrity_policy_for_view
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
)
ARMED_TEST_COUNT = 61

EXPECTED_RULES = {
    ("/practice/section/<int:section_id>/start", ("OPTIONS", "POST"), "practice.start_section"),
    ("/practice/section/<int:section_id>/topic/<int:topic_id>/start", ("OPTIONS", "POST"),
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
        response = self.post(self.client(self.student), self.start_url(scope))
        html = self.page(response)
        self.assertEqual(response.status_code, 409)
        self.assertIn("Това упражнение не може да бъде показано в момента.", html)
        self.assert_no_state_leak(html)
        self.assertEqual(self.table_digests(), before)

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

    def test_start_urls_accept_post_only(self):
        scope = self.make_scope(8)
        before = self.table_digests()
        client = self.client(self.student)
        for path in (self.start_url(scope), self.start_url(scope, 0)):
            for method in ("get", "put", "patch", "delete"):
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
        response = self.client(self.student).get(f"/practice/session/{session_id}/result")
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

    def test_browse_entry_points_do_not_link_to_practice(self):
        scope = self.make_scope(8)
        client = self.client(self.student)
        slug = self.conn.execute("SELECT section_slug FROM curriculum_sections WHERE id = ?",
                                 (scope.section_id,)).fetchone()[0]
        for path in ("/", f"/section/{slug}", "/account", "/account/attempts"):
            response = client.get(path)
            with self.subTest(path=path):
                self.assertIn(response.status_code, (200, 302, 404))
                self.assertNotIn("/practice", self.page(response))

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
