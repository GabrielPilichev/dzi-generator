"""Quiz service boundary: dependency direction, connection lifecycle, attempt transactions.

HTTP behaviour of the quiz routes is pinned by the existing quiz suites
(quiz_attempt_render, quiz_attempt_race_ownership, ...); these tests cover what
moving the logic out of ``web.app`` must keep true.
"""

import ast
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.support import IsolatedAppTestCase, web_app
from web import db as web_db
from web.services import quiz as quiz_service

ROOT = Path(__file__).resolve().parents[1]
LOW_LEVEL_MODULES = ("web/db.py", "web/db_introspect.py", "web/text_values.py")


def _references_web_app(path: Path) -> list[str]:
    """Every way a module can name ``web.app`` or the old lazy accessor."""
    found = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found += [a.name for a in node.names if a.name == "web.app" or a.name.startswith("web.app.")]
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module == "web.app" or module.startswith("web.app."):
                found.append(module)
            elif module == "web" and any(a.name == "app" for a in node.names):
                found.append("from web import app")
            elif node.level and (module == "app" or any(a.name == "app" for a in node.names)):
                found.append("relative import of app")
        elif isinstance(node, ast.Constant) and node.value == "web.app":
            found.append("'web.app' string (dynamic import?)")
        elif isinstance(node, ast.Name) and node.id == "_app_module":
            found.append("_app_module")
    return found


class QuizDependencyDirectionTest(unittest.TestCase):
    def test_blueprints_and_services_never_reference_web_app(self):
        offenders = {}
        for folder in ("web/blueprints", "web/services"):
            for path in sorted((ROOT / folder).glob("*.py")):
                found = _references_web_app(path)
                if found:
                    offenders[str(path.relative_to(ROOT))] = found
        self.assertEqual(offenders, {})

    def test_low_level_modules_import_no_application_layer(self):
        for relative in LOW_LEVEL_MODULES:
            tree = ast.parse((ROOT / relative).read_text(encoding="utf-8"))
            imported = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported.update(a.name for a in node.names)
                elif isinstance(node, ast.ImportFrom):
                    imported.add(node.module or "")
            offenders = sorted(
                name for name in imported
                if name == "web.app" or name.startswith(("web.app.", "web.blueprints", "web.services"))
            )
            self.assertEqual(offenders, [], relative)

    def test_importing_quiz_blueprint_and_service_does_not_load_web_app(self):
        code = (
            "import sys\n"
            "import web.blueprints.quiz, web.services.quiz\n"
            "print('web.app' in sys.modules)\n"
        )
        result = subprocess.run(
            [sys.executable, "-B", "-c", code], cwd=ROOT, capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "False")


class QuizConnectionLifecycleTest(IsolatedAppTestCase):
    def test_quiz_db_uses_shared_policy_at_the_configured_path(self):
        with patch.object(web_db, "connect_db", wraps=web_db.connect_db) as spy:
            conn = quiz_service.quiz_db()
        try:
            spy.assert_called_once_with(web_app.QUIZ_DB_PATH)
            self.assertEqual(conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)
            self.assertIs(conn.row_factory, web_db.sqlite3.Row)
        finally:
            conn.close()

    def test_quiz_db_follows_the_application_global_on_every_call(self):
        with tempfile.TemporaryDirectory() as directory:
            other = Path(directory) / "other.db"
            with patch.object(web_app, "QUIZ_DB_PATH", other):
                conn = quiz_service.quiz_db()
                try:
                    opened = conn.execute("PRAGMA database_list").fetchone()["file"]
                finally:
                    conn.close()
        self.assertEqual(Path(opened).resolve(), other.resolve())

    def test_unconfigured_service_fails_loudly(self):
        with patch.dict(quiz_service._runtime, clear=True):
            with self.assertRaisesRegex(RuntimeError, "configure_runtime"):
                quiz_service.quiz_db()

    def test_connection_is_closed_after_the_block_and_after_an_error(self):
        with quiz_service.quiz_connection() as conn:
            self.assertEqual(conn.execute("SELECT 1").fetchone()[0], 1)
        with self.assertRaises(web_db.sqlite3.ProgrammingError):
            conn.execute("SELECT 1")

        with self.assertRaises(ValueError):
            with quiz_service.quiz_connection() as failing:
                raise ValueError("boom")
        with self.assertRaises(web_db.sqlite3.ProgrammingError):
            failing.execute("SELECT 1")

    def test_leaving_the_block_does_not_commit(self):
        with quiz_service.quiz_connection() as conn:
            conn.execute(
                "INSERT INTO quiz_assignments (section_id, title_bg, question_count) VALUES (?, ?, 1)",
                (self._section_id(), "uncommitted"),
            )
        with quiz_service.quiz_connection() as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM quiz_assignments WHERE title_bg = 'uncommitted'"
            ).fetchone()[0]
        self.assertEqual(count, 0)

    @staticmethod
    def _section_id() -> int:
        with quiz_service.quiz_connection() as conn:
            return int(conn.execute("SELECT id FROM curriculum_sections ORDER BY id").fetchone()["id"])


class QuizAttemptTransactionTest(IsolatedAppTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        with quiz_service.quiz_connection() as conn:
            for section in conn.execute("SELECT id, title_bg FROM curriculum_sections ORDER BY id"):
                if quiz_service.quiz_section_question_ids(conn, int(section["id"])):
                    cls.section_id = int(section["id"])
                    cls.section_title = section["title_bg"]
                    break
            else:
                raise AssertionError("No section with eligible quiz questions found")

    def _assignment(self, conn, *, plan_json=None):
        cursor = conn.execute(
            """
            INSERT INTO quiz_assignments (section_id, title_bg, question_count, question_plan_json)
            VALUES (?, ?, 3, ?)
            """,
            (self.section_id, self.section_title, plan_json),
        )
        conn.commit()
        return quiz_service.quiz_fetch_assignment(conn, int(cursor.lastrowid))

    def _attempt_rows(self, assignment_id):
        with quiz_service.quiz_connection() as conn:  # a separate connection: sees only commits
            return conn.execute(
                "SELECT * FROM quiz_attempts WHERE assignment_id = ?", (assignment_id,)
            ).fetchall()

    def test_new_attempt_is_committed_and_a_repeat_returns_the_same_attempt(self):
        with quiz_service.quiz_connection() as conn:
            assignment = self._assignment(conn)
            first = quiz_service.quiz_start_or_resume_attempt(conn, assignment, "Ана Тестова")
            self.assertTrue(first.created)
            self.assertFalse(first.submitted)

            rows = self._attempt_rows(int(assignment["id"]))
            self.assertEqual([int(row["id"]) for row in rows], [first.attempt_id])
            self.assertEqual(rows[0]["seed"], quiz_service.quiz_seed(int(assignment["id"]), "Ана Тестова"))

            again = quiz_service.quiz_start_or_resume_attempt(conn, assignment, "Ана Тестова")
        self.assertEqual(again, quiz_service.QuizAttemptStart(first.attempt_id, False, False))
        self.assertEqual(len(self._attempt_rows(int(assignment["id"]))), 1)

    def test_unreadable_plan_raises_and_stores_nothing(self):
        with quiz_service.quiz_connection() as conn:
            assignment = self._assignment(conn, plan_json="{not json")
            with self.assertRaises(quiz_service.QuizPlanError):
                quiz_service.quiz_start_or_resume_attempt(conn, assignment, "Ана Тестова")
        self.assertEqual(self._attempt_rows(int(assignment["id"])), [])

    def test_submission_grades_stores_and_commits_in_one_step(self):
        with quiz_service.quiz_connection() as conn:
            assignment = self._assignment(conn)
            start = quiz_service.quiz_start_or_resume_attempt(conn, assignment, "Ана Тестова")
            attempt = quiz_service.quiz_fetch_attempt(conn, start.attempt_id)
            plan = quiz_service.quiz_parse_attempt_question_plan(attempt["question_ids_json"])
            mc_ids = quiz_service.quiz_multiple_choice_question_ids(conn, plan["question_ids"])
            self.assertTrue(mc_ids)
            correct = quiz_service.quiz_correct_mc_letters_by_id(conn, mc_ids)
            # first answer right, the rest left unanswered
            form = {f"q_{mc_ids[0]}": correct[mc_ids[0]]}

            score, total = quiz_service.quiz_submit_attempt(
                conn, start.attempt_id,
                mc_question_ids=mc_ids, question_ids=plan["question_ids"],
                open_question_ids=[], form=form,
            )

        self.assertEqual((score, total), (1, len(mc_ids)))
        with quiz_service.quiz_connection() as conn:
            stored = quiz_service.quiz_fetch_attempt(conn, start.attempt_id)
            answers = conn.execute(
                "SELECT question_id, is_correct FROM quiz_answers WHERE attempt_id = ? ORDER BY question_id",
                (start.attempt_id,),
            ).fetchall()
        self.assertIsNotNone(stored["submitted_at"])
        self.assertEqual((stored["score_correct"], stored["score_total"]), (1, len(mc_ids)))
        self.assertEqual(sorted(int(r["question_id"]) for r in answers), sorted(mc_ids))
        self.assertEqual(sum(int(r["is_correct"]) for r in answers), 1)

    def test_failed_submission_leaves_no_partial_state(self):
        with quiz_service.quiz_connection() as conn:
            assignment = self._assignment(conn)
            start = quiz_service.quiz_start_or_resume_attempt(conn, assignment, "Ана Тестова")
            attempt = quiz_service.quiz_fetch_attempt(conn, start.attempt_id)
            plan = quiz_service.quiz_parse_attempt_question_plan(attempt["question_ids_json"])
            mc_ids = quiz_service.quiz_multiple_choice_question_ids(conn, plan["question_ids"])
            form = {f"q_{mc_ids[0]}": "A"}

            with patch.object(quiz_service, "quiz_record_planned_open_text_answers",
                              side_effect=RuntimeError("fail after the MC rows")):
                with self.assertRaises(RuntimeError):
                    quiz_service.quiz_submit_attempt(
                        conn, start.attempt_id,
                        mc_question_ids=mc_ids, question_ids=plan["question_ids"],
                        open_question_ids=[], form=form,
                    )

        with quiz_service.quiz_connection() as conn:
            stored = quiz_service.quiz_fetch_attempt(conn, start.attempt_id)
            answers = conn.execute(
                "SELECT COUNT(*) FROM quiz_answers WHERE attempt_id = ?", (start.attempt_id,)
            ).fetchone()[0]
        self.assertIsNone(stored["submitted_at"])
        self.assertEqual(answers, 0)


if __name__ == "__main__":
    unittest.main()
