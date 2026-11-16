"""Connection lifetimes of the eight quiz consumers of ``quiz_connection()`` (A3).

``teacher_dashboard``, ``teacher_assignments``, ``teacher_new``,
``teacher_assignment_qr``, ``teacher_assignment_results``,
``teacher_assignment_results_export``, ``quiz_write_assignment_note`` and
``quiz_write_attempt_note`` each opened a raw ``quiz_db()`` connection and closed it by
hand on the success path only: an exception or an early exit left it open. These tests
drive the real consumers on a disposable database and look at what actually happened to
the connections they opened: whether each one is closed afterwards, whether a failed
request still holds a read lock, and in what order the statements ran. Faults are injected
through a recording wrapper around the one factory (``web.services.quiz.quiz_db``) that
``quiz_connection()`` consults; nothing here asserts on the helper's source text.

``tests/test_quiz_services.py`` keeps the helper's own contract (closes after a block and
after an error, never commits).
"""

import io
import sqlite3
import unittest
from contextlib import closing, redirect_stderr
from datetime import timedelta
from unittest.mock import patch

from tests.lifecycle_support import GRACE, SAME_ORIGIN, LifecycleTestCase, ts
from tests.support import web_app
from web import public_url
from web.blueprints import teacher as teacher_module
from web.services import quiz as quiz_service

PUBLIC_BASE = "https://learnpilot.test"
FAULT = sqlite3.OperationalError("injected fault")
# The lifecycle clock read turns any sqlite3.Error into a degraded-clock answer by design,
# so a fault there must be some other failure to reach the handler.
CLOCK_FAULT = RuntimeError("injected clock fault")


def is_closed(conn: sqlite3.Connection) -> bool:
    try:
        conn.execute("SELECT 1")
    except sqlite3.ProgrammingError:
        return True
    return False


class FaultingConnection:
    """Delegates to a real connection, but raises ``error`` for the statements containing ``needle``."""

    def __init__(self, conn, needle: str, error: Exception):
        self._conn, self._needle, self._error = conn, needle, error

    def execute(self, sql, *args):
        if self._needle in " ".join(sql.split()):
            raise self._error
        return self._conn.execute(sql, *args)

    def __getattr__(self, name):
        return getattr(self._conn, name)


class QuizConnections:
    """Replaces the factory ``quiz_connection()`` uses; records every real connection it opens.

    ``statements`` is the normalized statement trace of all of them, in execution order.
    With ``fault=(needle, error)`` the handlers get a wrapper that raises on that statement.
    """

    def __init__(self, fault=None):
        self.fault = fault
        self.connections: list[sqlite3.Connection] = []
        self.statements: list[str] = []

    def __enter__(self):
        real = quiz_service.quiz_db

        def open_connection():
            conn = real()
            conn.set_trace_callback(lambda text: self.statements.append(" ".join(text.split())))
            self.connections.append(conn)
            return FaultingConnection(conn, *self.fault) if self.fault else conn

        self._patch = patch.object(quiz_service, "quiz_db", side_effect=open_connection)
        self._patch.start()
        return self

    def __exit__(self, *exc):
        self._patch.stop()

    def states(self) -> list[bool]:
        return [is_closed(conn) for conn in self.connections]


class LifetimeTestCase(LifecycleTestCase):
    def setUp(self):
        super().setUp()
        self.teacher = self.account("teacher")
        self.other_teacher = self.account("teacher")
        self.admin = self.account("admin")
        self.student = self.account("student")
        self.owner = self.client(self.teacher)
        config = patch.dict(web_app.app.config, {public_url.PUBLIC_BASE_URL_CONFIG: PUBLIC_BASE})
        config.start()
        self.addCleanup(config.stop)

    def assertClosed(self, connections: QuizConnections, message: str = ""):
        self.assertTrue(connections.connections, "the request opened no connection: nothing was observed")
        self.assertEqual(connections.states(), [True] * len(connections.connections), message)

    def assertWriterNotBlocked(self):
        """A leaked read transaction would hold a shared lock and refuse this commit."""
        with closing(sqlite3.connect(str(self.db_path), timeout=0.2)) as writer:
            writer.execute("UPDATE quiz_assignments SET title_bg = title_bg")
            writer.commit()

    def assignment_count(self) -> int:
        return self.query("SELECT COUNT(*) FROM quiz_assignments")[0][0]

    def request_failing(self, request, error=FAULT):
        """Run ``request`` expecting the injected fault: raised, or a 500 answer."""
        try:
            response = request()
        except (sqlite3.OperationalError, RuntimeError) as exc:
            self.assertIs(exc, error)
            return
        self.assertEqual(response.status_code, 500)


# ---------------------------------------------------------------------------- teacher: simple views

class DashboardAndListTest(LifetimeTestCase):
    def test_dashboard_closes_after_rendering(self):
        self.assignment(self.teacher)
        with QuizConnections() as spy:
            response = self.client(self.admin).get("/teacher")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(spy.connections), 1)
        self.assertClosed(spy)

    def test_dashboard_closes_when_a_query_fails(self):
        with QuizConnections(("quiz_attempts qt", FAULT)) as spy:
            self.request_failing(lambda: self.client(self.admin).get("/teacher"))
        self.assertClosed(spy)

    def test_assignment_list_closes_after_rendering_for_teacher_and_admin(self):
        self.assignment(self.teacher)
        for label, account in (("teacher", self.teacher), ("admin", self.admin)):
            with self.subTest(account=label), QuizConnections() as spy:
                response = self.client(account).get("/teacher/assignments?type=mc")
                self.assertEqual(response.status_code, 200)
                self.assertEqual(len(spy.connections), 1)
                self.assertClosed(spy)

    def test_assignment_list_closes_when_its_query_fails(self):
        with QuizConnections(("FROM quiz_assignments qa", FAULT)) as spy:
            self.request_failing(lambda: self.owner.get("/teacher/assignments"))
        self.assertClosed(spy)

    def test_assignment_list_closes_when_the_clock_read_fails(self):
        self.assignment(self.teacher)
        with QuizConnections(("FROM lifecycle_clock", CLOCK_FAULT)) as spy:
            self.request_failing(lambda: self.owner.get("/teacher/assignments"), CLOCK_FAULT)
        self.assertClosed(spy)


# ---------------------------------------------------------------------------- teacher: QR

class QrTest(LifetimeTestCase):
    def test_qr_closes_after_generating_the_code(self):
        assignment_id = self.assignment(self.teacher)
        with QuizConnections() as spy:
            response = self.owner.get(f"/teacher/assignment/{assignment_id}/qr.svg")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "image/svg+xml")
        self.assertClosed(spy)

    def test_qr_closes_for_a_foreign_and_a_missing_assignment(self):
        assignment_id = self.assignment(self.other_teacher)
        for label, target in (("foreign", assignment_id), ("missing", 999999)):
            with self.subTest(assignment=label), QuizConnections() as spy:
                self.assertEqual(self.owner.get(f"/teacher/assignment/{target}/qr.svg").status_code, 404)
                self.assertClosed(spy)

    def test_qr_closes_when_the_assignment_is_held_or_has_no_public_link(self):
        held = self.assignment(self.teacher, lifecycle_hold="clock_review")
        with QuizConnections() as spy:
            response = self.owner.get(f"/teacher/assignment/{held}/qr.svg")
            self.assertEqual(response.status_code, 503)
            self.assertClosed(spy)
        no_token = self.assignment(self.teacher)
        self.execute("UPDATE quiz_assignments SET public_token = NULL WHERE id = ?", (no_token,))
        with QuizConnections() as spy:
            self.assertEqual(self.owner.get(f"/teacher/assignment/{no_token}/qr.svg").status_code, 404)
            self.assertClosed(spy)

    def test_qr_closes_when_the_fetch_fails(self):
        assignment_id = self.assignment(self.teacher)
        with QuizConnections(("FROM quiz_assignments", FAULT)) as spy:
            self.request_failing(lambda: self.owner.get(f"/teacher/assignment/{assignment_id}/qr.svg"))
        self.assertClosed(spy)


# ---------------------------------------------------------------------------- teacher: results and CSV

class ResultsAndCsvTest(LifetimeTestCase):
    def setUp(self):
        super().setUp()
        self.assignment_id = self.assignment(self.teacher)
        self.attempt(self.assignment_id, self.student, started_at=self.T0 - timedelta(hours=2),
                     submitted_at=self.T0 - timedelta(hours=1))
        self.urls = {
            "results": f"/teacher/assignment/{self.assignment_id}/results",
            "csv": f"/teacher/assignment/{self.assignment_id}/results.csv",
            "filtered csv": f"/teacher/assignment/{self.assignment_id}/results.csv?filtered=1",
        }

    def test_success_is_one_connection_one_snapshot_and_closed(self):
        for label, url in self.urls.items():
            with self.subTest(view=label), QuizConnections() as spy:
                response = self.owner.get(url)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(len(spy.connections), 1)
                begins = [i for i, s in enumerate(spy.statements) if s.startswith("BEGIN")]
                ends = [i for i, s in enumerate(spy.statements) if s.startswith(("ROLLBACK", "COMMIT"))]
                self.assertEqual(len(begins), 1)
                self.assertEqual(len(ends), 1)
                self.assertTrue(spy.statements[ends[0]].startswith("ROLLBACK"))  # a read: nothing committed
                reads = [s for s in spy.statements[begins[0]:ends[0]] if s.startswith("SELECT")]
                self.assertGreater(len(reads), 2)
                self.assertClosed(spy)

    def test_a_failure_after_the_snapshot_began_closes_and_releases_the_lock(self):
        needles = {"results": "AVG(CASE", "csv": "FROM quiz_attempts", "filtered csv": "FROM quiz_attempts"}
        for label, url in self.urls.items():
            with self.subTest(view=label), QuizConnections((needles[label], FAULT)) as spy:
                self.request_failing(lambda: self.owner.get(url))
                self.assertIn("BEGIN", spy.statements)  # the snapshot had begun
                self.assertClosed(spy)
                self.assertWriterNotBlocked()

    def test_denied_missing_and_held_requests_close_without_serving_anything(self):
        held = self.assignment(self.teacher, lifecycle_hold="clock_review")
        for label, target, status in (("foreign owner", self.client(self.other_teacher), 404),
                                      ("anonymous", self.client(), 302)):
            for view, url in self.urls.items():
                with self.subTest(requester=label, view=view), QuizConnections() as spy:
                    self.assertEqual(target.get(url).status_code, status)
                    self.assertTrue(all(spy.states()))  # whatever was opened is closed
        for suffix in ("results", "results.csv", "results.csv?filtered=1"):
            with self.subTest(held=suffix), QuizConnections() as spy:
                self.assertEqual(self.owner.get(f"/teacher/assignment/{held}/{suffix}").status_code, 503)
                self.assertClosed(spy)
                self.assertWriterNotBlocked()
            with self.subTest(missing=suffix), QuizConnections() as spy:
                self.assertEqual(self.owner.get(f"/teacher/assignment/999999/{suffix}").status_code, 404)
                self.assertClosed(spy)
                self.assertWriterNotBlocked()

    def test_results_observation_stays_after_the_rollback_and_before_the_close(self):
        expired = self.assignment(self.teacher, deadline_at=self.T0 - GRACE - timedelta(hours=1))
        self.assertIsNone(self.row(expired)["expiry_observed_at"])
        with QuizConnections() as spy:
            response = self.owner.get(f"/teacher/assignment/{expired}/results")
        self.assertEqual(response.status_code, 200)
        statements = spy.statements
        rollback = statements.index("ROLLBACK")
        observation = next(i for i, s in enumerate(statements)
                           if s.startswith("UPDATE quiz_assignments") and "expiry_observed_at" in s)
        self.assertLess(statements.index("BEGIN"), rollback)
        self.assertLess(rollback, observation)  # after the read transaction ended
        self.assertEqual(self.row(expired)["expiry_observed_at"], ts(self.T0))  # written, then committed
        self.assertClosed(spy)

    def test_nothing_is_written_by_a_normal_results_read(self):
        before = self.table_counts(), self.lifecycle_columns(self.assignment_id)
        for url in self.urls.values():
            self.assertEqual(self.owner.get(url).status_code, 200)
        self.assertEqual((self.table_counts(), self.lifecycle_columns(self.assignment_id)), before)


# ---------------------------------------------------------------------------- teacher: creation

class CreationTest(LifetimeTestCase):
    _mixed: dict = {}

    def mixed_fields(self) -> dict:
        """A canonical section that offers open questions, and a source with candidates for it."""
        if not self._mixed:
            with closing(web_app.quiz_db()) as conn:
                for row in conn.execute("SELECT id FROM curriculum_sections ORDER BY id"):
                    if not quiz_service.quiz_section_question_ids(conn, int(row["id"])):
                        continue
                    candidates = web_app.fetch_open_question_candidates(conn, section_id=row["id"])
                    if candidates:
                        self._mixed.update(section_id=int(row["id"]), source_slug=candidates[0]["source_slug"])
                        break
                else:
                    raise AssertionError("no canonical section with open-question candidates")
        return dict(self._mixed)

    def submit(self, client, **fields):
        return client.post("/teacher/new", data={**self.no_deadline_form(), **fields}, headers=SAME_ORIGIN)

    def forms(self):
        mixed = self.mixed_fields()
        return {
            "multiple choice": self.new_assignment_form(),
            "mixed": {"section_id": mixed["section_id"], "question_count": 2, "open_count": 1,
                      "source_slug": mixed["source_slug"]},
        }

    def test_the_creating_connection_is_closed_before_the_note_is_written(self):
        for label, form in self.forms().items():
            with self.subTest(creation=label), QuizConnections() as spy:
                at_note = []

                def note(assignment_id, base_url, spy=spy, at_note=at_note):
                    # What the note writer finds: the creation committed, its connection closed.
                    at_note.append((assignment_id, spy.states(), self.row(assignment_id)["id"]))

                with patch.object(teacher_module, "quiz_write_assignment_note", side_effect=note):
                    response = self.submit(self.owner, **form)
                self.assertEqual(response.status_code, 302)
                assignment_id = self.created_id(response)
                self.assertEqual(response.headers["Location"], f"/teacher/assignment/{assignment_id}")
                self.assertEqual(at_note, [(assignment_id, [True], assignment_id)])
                self.assertEqual(bool(self.row(assignment_id)["question_plan_json"]), label == "mixed")
                self.assertClosed(spy)

    def test_a_real_note_is_written_by_a_second_connection_after_the_first_closed(self):
        with QuizConnections() as spy:
            response = self.submit(self.owner, **self.new_assignment_form())
        self.assertEqual(response.status_code, 302)
        self.assertEqual(len(spy.connections), 2)  # creation, then the note writer's own
        self.assertClosed(spy)
        notes = list((web_app.QUIZ_VAULT_PATH / "Generated" / "Quizzes").glob("*-assignment-*.md"))
        self.assertEqual(len(notes), 1)
        self.assertIn("type: \"quiz_assignment\"", notes[0].read_text(encoding="utf-8"))
        self.assertLess(spy.statements.index("COMMIT"),
                        max(i for i, s in enumerate(spy.statements) if s.startswith("SELECT")))

    def test_invalid_and_refused_posts_render_or_abort_and_close(self):
        mixed = self.mixed_fields()
        requests = {
            "open count above total": lambda: self.submit(
                self.owner, section_id=mixed["section_id"], question_count=1, open_count=3),
            "not a number": lambda: self.submit(self.owner, section_id="x"),
            "forged question plan": lambda: self.submit(self.owner, **self.new_assignment_form(), question_ids="1"),
            "unknown section": lambda: self.submit(self.owner, section_id=999999, question_count=1),
            "unknown subject (POST)": lambda: self.submit(self.owner, subject="astrology", **self.new_assignment_form()),
            "unknown subject (GET)": lambda: self.owner.get("/teacher/new?subject=astrology"),
            "form page": lambda: self.owner.get("/teacher/new"),
            "preselected section": lambda: self.owner.get("/teacher/new?section=x"),
        }
        statuses = {"open count above total": 400, "not a number": 400, "forged question plan": 400,
                    "unknown section": 404, "unknown subject (POST)": 400, "unknown subject (GET)": 404,
                    "form page": 200, "preselected section": 200}
        before = self.assignment_count()
        for label, request in requests.items():
            with self.subTest(request=label), QuizConnections() as spy, \
                    patch.object(teacher_module, "quiz_write_assignment_note") as note:
                self.assertEqual(request().status_code, statuses[label])
                note.assert_not_called()
                self.assertClosed(spy)
        self.assertEqual(self.assignment_count(), before)

    def test_a_query_failure_while_loading_the_form_closes(self):
        for label, needle, error in (("section list", "FROM curriculum_sections", FAULT),
                                     ("clock", "FROM lifecycle_clock", CLOCK_FAULT)):
            with self.subTest(failing=label), QuizConnections((needle, error)) as spy:
                self.request_failing(lambda: self.owner.get("/teacher/new"), error)
                self.assertClosed(spy)

    def test_a_failed_creation_closes_stores_nothing_and_writes_no_note(self):
        for label, form in self.forms().items():
            with self.subTest(creation=label), QuizConnections(("INSERT INTO quiz_assignments", FAULT)) as spy, \
                    patch.object(teacher_module, "quiz_write_assignment_note") as note:
                self.request_failing(lambda: self.submit(self.owner, **form))
                note.assert_not_called()
                self.assertClosed(spy)
                self.assertEqual(self.assignment_count(), 0)

    def test_a_refused_creation_closes_and_answers_the_clock_notice(self):
        self.execute("UPDATE lifecycle_clock SET state = 'suspect', state_reason = 'clock_step'")
        for label, form in self.forms().items():
            with self.subTest(creation=label), QuizConnections() as spy, \
                    patch.object(teacher_module, "quiz_write_assignment_note") as note:
                response = self.submit(self.owner, **form)
                self.assertEqual(response.status_code, 503)
                note.assert_not_called()
                self.assertClosed(spy)
        self.assertEqual(self.assignment_count(), 0)

    def test_a_creation_that_stores_nothing_still_loads_the_form_it_renders(self):
        mixed = self.mixed_fields()
        with QuizConnections() as spy, patch.object(teacher_module, "quiz_write_assignment_note") as note:
            response = self.submit(self.owner, section_id=mixed["section_id"], question_count=1, open_count=3)
        self.assertEqual(response.status_code, 400)
        note.assert_not_called()
        self.assertEqual(len(spy.connections), 1)
        self.assertTrue(any("ORDER BY class, display_order" in s for s in spy.statements), spy.statements)
        self.assertClosed(spy)

    def test_success_does_not_load_the_page_it_does_not_render(self):
        # A created assignment redirects: the section list and the deadline offer are not read.
        with QuizConnections() as spy, patch.object(teacher_module, "quiz_write_assignment_note"):
            self.assertEqual(self.submit(self.owner, **self.new_assignment_form()).status_code, 302)
        self.assertFalse(any("ORDER BY class, display_order" in s for s in spy.statements), spy.statements)
        self.assertClosed(spy)


# ---------------------------------------------------------------------------- vault note writers

class NoteWriterTest(LifetimeTestCase):
    def setUp(self):
        super().setUp()
        self.assignment_id = self.assignment(self.teacher)
        self.attempt_id = self.attempt(self.assignment_id, self.student, started_at=self.T0 - timedelta(hours=2),
                                       submitted_at=self.T0 - timedelta(hours=1), name="Ана Тестова")
        self.quizzes = web_app.QUIZ_VAULT_PATH / "Generated" / "Quizzes"
        self.assertTrue(self.quizzes.is_relative_to(self.root))  # a scratch vault, never the repository's

    def write(self, kind, ident=None):
        """Run a writer inside a request context; return what it printed to stderr."""
        errors = io.StringIO()
        with web_app.app.test_request_context("/"), redirect_stderr(errors):
            if kind == "assignment":
                quiz_service.quiz_write_assignment_note(self.assignment_id if ident is None else ident,
                                                        "http://localhost/")
            else:
                quiz_service.quiz_write_attempt_note(self.attempt_id if ident is None else ident)
        return errors.getvalue()

    def test_both_writers_close_after_writing_their_note(self):
        for kind, pattern in (("assignment", "*-assignment-*.md"), ("attempt", "attempts/*.md")):
            with self.subTest(writer=kind), QuizConnections() as spy:
                self.assertEqual(self.write(kind), "")
                self.assertEqual(len(spy.connections), 1)
                self.assertClosed(spy)
                self.assertEqual(len(list(self.quizzes.glob(pattern))), 1)

    def test_both_writers_close_when_the_row_is_missing(self):
        for kind in ("assignment", "attempt"):
            with self.subTest(writer=kind), QuizConnections() as spy:
                self.assertEqual(self.write(kind, 999999), "")
                self.assertClosed(spy)
        self.assertFalse(self.quizzes.exists())

    def test_the_attempt_writer_closes_when_its_assignment_is_missing(self):
        with closing(sqlite3.connect(str(self.db_path))) as conn:  # keep the attempt, drop the parent row
            conn.execute("PRAGMA foreign_keys = OFF")
            conn.execute("DELETE FROM quiz_assignments WHERE id = ?", (self.assignment_id,))
            conn.commit()
        with QuizConnections() as spy:
            self.assertEqual(self.write("attempt"), "")
            self.assertClosed(spy)
        self.assertFalse(self.quizzes.exists())

    def test_a_failing_query_is_reported_as_before_and_the_connection_closes(self):
        expected = {"assignment": "[quiz vault] assignment note failed: injected fault\n",
                    "attempt": "[quiz vault] attempt note failed: injected fault\n"}
        needles = {"assignment": "FROM quiz_attempts", "attempt": "quiz_attempts WHERE id"}
        for kind in ("assignment", "attempt"):
            with self.subTest(writer=kind), QuizConnections((needles[kind], FAULT)) as spy:
                self.assertEqual(self.write(kind), expected[kind])
                self.assertClosed(spy)
        self.assertFalse(self.quizzes.exists())

    def test_a_failing_filesystem_write_is_reported_as_before_and_the_connection_closes(self):
        self.quizzes.parent.mkdir(parents=True)
        self.quizzes.write_text("a file where the notes directory belongs", encoding="utf-8")
        for kind in ("assignment", "attempt"):
            with self.subTest(writer=kind), QuizConnections() as spy:
                message = self.write(kind)
                self.assertRegex(message, rf"^\[quiz vault\] {kind} note failed: .+\n$")
                self.assertClosed(spy)
        self.assertEqual(self.quizzes.read_text(encoding="utf-8"), "a file where the notes directory belongs")

    def test_a_failure_to_open_the_connection_is_still_the_best_effort_message(self):
        # The managed scope sits inside the writers' own try: opening is covered by it too.
        for kind, label in (("assignment", "assignment"), ("attempt", "attempt")):
            with self.subTest(writer=kind), patch.object(
                    quiz_service, "quiz_db", side_effect=sqlite3.OperationalError("unable to open database file")):
                self.assertEqual(self.write(kind),
                                 f"[quiz vault] {label} note failed: unable to open database file\n")

    def test_the_notes_record_the_computed_assignment_state_on_the_scratch_vault(self):
        self.write("assignment")
        (note,) = self.quizzes.glob("*-assignment-*.md")
        text = note.read_text(encoding="utf-8")
        self.assertIn('type: "quiz_assignment"', text)
        self.assertIn('status: "open"', text)
        self.assertIn("Ана Тестова", text)


if __name__ == "__main__":
    unittest.main()
