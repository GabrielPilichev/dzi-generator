import atexit
import csv
import io
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path


_TMP = tempfile.TemporaryDirectory()
atexit.register(_TMP.cleanup)

_ROOT = Path(__file__).resolve().parents[1]
_TMP_DB = Path(_TMP.name) / "questions.db"
_TMP_VAULT = Path(_TMP.name) / "vault"
shutil.copy2(_ROOT / "data" / "questions.db", _TMP_DB)
_TMP_VAULT.mkdir()

os.environ["DZI_DB"] = str(_TMP_DB)
os.environ["DZI_VAULT"] = str(_TMP_VAULT)
os.environ["DZI_ADMIN_PASSWORD"] = "admin-pass"
os.environ["DZI_TESTER_PASSWORD"] = "tester-pass"

from web import app as web_app  # noqa: E402


def _register_dzi_default_denied_test_route(app):
    endpoint = "dzi_test_only_default_denied"
    if endpoint in app.view_functions:
        return

    def view():
        return "test-only DZI endpoint"

    app.add_url_rule("/__test__/dzi-default-denied", endpoint=endpoint, view_func=view)


class AssignmentResultsExportTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = web_app.app
        _register_dzi_default_denied_test_route(cls.app)
        cls.app.config.update(TESTING=True)
        conn = web_app.quiz_db()
        try:
            cls.section = cls._first_eligible_section(conn)
        finally:
            conn.close()

    @staticmethod
    def _first_eligible_section(conn):
        rows = conn.execute("""
            SELECT id, title_bg
            FROM curriculum_sections
            ORDER BY id
        """).fetchall()
        for row in rows:
            if web_app.quiz_section_question_ids(conn, int(row["id"])):
                return row
        raise AssertionError("No section with eligible MC questions found")

    def setUp(self):
        self.client = self.app.test_client()

    def _login_admin(self):
        with self.client.session_transaction() as sess:
            sess["admin_authenticated"] = True
            sess["ui_profile"] = "admin"

    def _login_tester(self):
        with self.client.session_transaction() as sess:
            sess["tester_authenticated"] = True
            sess["ui_profile"] = "tester"

    def _create_assignment(self, *, question_plan_json=None, title="Export Quiz"):
        conn = web_app.quiz_db()
        try:
            cur = conn.execute("""
                INSERT INTO quiz_assignments (
                    section_id, title_bg, question_count, time_limit_minutes, question_plan_json
                )
                VALUES (?, ?, ?, ?, ?)
            """, (self.section["id"], title, 3, None, question_plan_json))
            assignment_id = int(cur.lastrowid)
            conn.commit()
            return assignment_id
        finally:
            conn.close()

    def _seed_attempt(
        self,
        assignment_id,
        *,
        student_name,
        score_correct=2,
        score_total=3,
        question_ids_json=None,
        submitted=True,
    ):
        conn = web_app.quiz_db()
        try:
            cur = conn.execute("""
                INSERT INTO quiz_attempts (
                    assignment_id, student_name, seed, question_ids_json,
                    score_total, score_correct, submitted_at
                )
                VALUES (?, ?, ?, ?, ?, ?, CASE WHEN ? THEN datetime('now') ELSE NULL END)
            """, (
                assignment_id,
                student_name,
                1,
                question_ids_json or "[1, 2, 3]",
                score_total,
                score_correct,
                1 if submitted else 0,
            ))
            conn.commit()
            return int(cur.lastrowid)
        finally:
            conn.close()

    def _seed_open_answer(
        self,
        attempt_id,
        *,
        question_id=42,
        subquestion_number=1,
        raw_answer="клиент",
        normalized_answer="клиент",
        matched_answer="клиент",
        points_awarded=1.0,
        points_possible=1.0,
        is_correct=1,
        grading_mode="ordered",
        teacher_override=0,
        teacher_note=None,
    ):
        conn = web_app.quiz_db()
        try:
            cur = conn.execute("""
                INSERT INTO quiz_text_answers (
                    attempt_id, question_id, subquestion_id, subquestion_number,
                    response_order, raw_answer, normalized_answer, grading_mode,
                    accepted_answers_json, matched_answer, is_correct,
                    points_awarded, points_possible, grader_version,
                    teacher_override, teacher_note
                )
                VALUES (?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                attempt_id,
                question_id,
                subquestion_number,
                subquestion_number,
                raw_answer,
                normalized_answer,
                grading_mode,
                json.dumps(["клиент"]),
                matched_answer,
                is_correct,
                points_awarded,
                points_possible,
                "v1",
                teacher_override,
                teacher_note,
            ))
            conn.commit()
            return int(cur.lastrowid)
        finally:
            conn.close()

    def _delete_assignment(self, assignment_id):
        conn = web_app.quiz_db()
        try:
            conn.execute("""
                DELETE FROM quiz_text_answers
                WHERE attempt_id IN (SELECT id FROM quiz_attempts WHERE assignment_id = ?)
            """, (assignment_id,))
            conn.execute("DELETE FROM quiz_attempts WHERE assignment_id = ?", (assignment_id,))
            conn.execute("DELETE FROM quiz_assignments WHERE id = ?", (assignment_id,))
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def _attempt_plan(*, combined_score=False):
        return json.dumps({
            "mixed_open_enabled": True,
            "question_ids": [1, 42],
            "open_question_ids": [42],
            "include_open_answers_in_final_score": combined_score,
        })

    @staticmethod
    def _mixed_assignment_plan(*, combined_score=False):
        return json.dumps({
            "mixed_open_enabled": True,
            "question_ids": [1, 42],
            "open_question_ids": [42],
            "include_open_answers_in_final_score": combined_score,
        })

    def _parse_csv(self, body: bytes):
        text = body.decode("utf-8")
        reader = csv.DictReader(io.StringIO(text))
        return list(reader), reader.fieldnames or []

    # --- happy path -----------------------------------------------------

    def test_admin_can_download_csv(self):
        self._login_admin()
        assignment_id = self._create_assignment(title="Download Title")
        self._seed_attempt(assignment_id, student_name="Alice")
        try:
            response = self.client.get(f"/teacher/assignment/{assignment_id}/results.csv")
            self.assertEqual(response.status_code, 200)
            self.assertIn("text/csv", response.headers.get("Content-Type", ""))
            self.assertIn(
                f'filename="assignment_{assignment_id}_results.csv"',
                response.headers.get("Content-Disposition", ""),
            )
        finally:
            self._delete_assignment(assignment_id)

    def test_csv_includes_mc_only_attempt_row(self):
        self._login_admin()
        assignment_id = self._create_assignment(title="MC Export")
        self._seed_attempt(
            assignment_id,
            student_name="Боян Иванов",
            score_correct=2,
            score_total=3,
        )
        try:
            response = self.client.get(f"/teacher/assignment/{assignment_id}/results.csv")
            self.assertEqual(response.status_code, 200)
            rows, fieldnames = self._parse_csv(response.data)
            self.assertIn("row_type", fieldnames)
            self.assertIn("mc_score_correct", fieldnames)
            self.assertEqual(len(rows), 1)
            row = rows[0]
            self.assertEqual(row["row_type"], "attempt")
            self.assertEqual(row["assignment_id"], str(assignment_id))
            self.assertEqual(row["assignment_title"], "MC Export")
            self.assertEqual(row["student_name"], "Боян Иванов")
            self.assertEqual(row["mc_score_correct"], "2")
            self.assertEqual(row["mc_score_total"], "3")
            self.assertTrue(row["mc_percent"].startswith("66"))
            self.assertEqual(row["mixed_open_enabled"], "0")
            self.assertEqual(row["include_open_answers_in_final_score"], "0")
            self.assertEqual(row["open_answer_count"], "0")
            self.assertEqual(row["question_id"], "")
            self.assertEqual(row["raw_answer"], "")
        finally:
            self._delete_assignment(assignment_id)

    def test_csv_skips_unfinished_attempts(self):
        self._login_admin()
        assignment_id = self._create_assignment(title="Unfinished Skip")
        self._seed_attempt(assignment_id, student_name="In Progress", submitted=False)
        self._seed_attempt(assignment_id, student_name="Submitted")
        try:
            response = self.client.get(f"/teacher/assignment/{assignment_id}/results.csv")
            rows, _ = self._parse_csv(response.data)
            names = [r["student_name"] for r in rows]
            self.assertIn("Submitted", names)
            self.assertNotIn("In Progress", names)
        finally:
            self._delete_assignment(assignment_id)

    # --- mixed/open data -----------------------------------------------

    def test_csv_mixed_attempt_has_open_subtotal_and_per_answer_rows(self):
        self._login_admin()
        assignment_id = self._create_assignment(
            question_plan_json=self._mixed_assignment_plan(combined_score=False),
            title="Mixed Export",
        )
        attempt_id = self._seed_attempt(
            assignment_id,
            student_name="Open Student",
            score_correct=1,
            score_total=1,
            question_ids_json=self._attempt_plan(combined_score=False),
        )
        self._seed_open_answer(
            attempt_id,
            question_id=42,
            subquestion_number=1,
            raw_answer="клиент",
            normalized_answer="клиент",
            matched_answer="клиент",
            points_awarded=1.0,
            points_possible=1.0,
            is_correct=1,
        )
        self._seed_open_answer(
            attempt_id,
            question_id=42,
            subquestion_number=2,
            raw_answer="нещо друго",
            normalized_answer="нещо друго",
            matched_answer=None,
            points_awarded=0.0,
            points_possible=1.0,
            is_correct=0,
        )
        try:
            response = self.client.get(f"/teacher/assignment/{assignment_id}/results.csv")
            self.assertEqual(response.status_code, 200)
            rows, _ = self._parse_csv(response.data)
            self.assertEqual(len(rows), 3)

            attempt_rows = [r for r in rows if r["row_type"] == "attempt"]
            answer_rows = [r for r in rows if r["row_type"] == "open_answer"]
            self.assertEqual(len(attempt_rows), 1)
            self.assertEqual(len(answer_rows), 2)

            attempt_row = attempt_rows[0]
            self.assertEqual(attempt_row["mixed_open_enabled"], "1")
            self.assertEqual(attempt_row["include_open_answers_in_final_score"], "0")
            self.assertEqual(attempt_row["open_answer_count"], "2")
            self.assertEqual(float(attempt_row["open_subtotal_awarded"]), 1.0)
            self.assertEqual(float(attempt_row["open_subtotal_possible"]), 2.0)
            self.assertEqual(attempt_row["combined_awarded"], "")
            self.assertEqual(attempt_row["combined_possible"], "")

            sq1 = next(r for r in answer_rows if r["subquestion_number"] == "1")
            sq2 = next(r for r in answer_rows if r["subquestion_number"] == "2")
            self.assertEqual(sq1["raw_answer"], "клиент")
            self.assertEqual(sq1["matched_answer"], "клиент")
            self.assertEqual(sq1["is_correct"], "1")
            self.assertEqual(sq1["grading_mode"], "ordered")
            self.assertEqual(float(sq1["points_awarded"]), 1.0)
            self.assertEqual(sq2["matched_answer"], "")
            self.assertEqual(sq2["is_correct"], "0")
        finally:
            self._delete_assignment(assignment_id)

    def test_csv_combined_score_populated_when_enabled(self):
        self._login_admin()
        assignment_id = self._create_assignment(
            question_plan_json=self._mixed_assignment_plan(combined_score=True),
            title="Combined Export",
        )
        attempt_id = self._seed_attempt(
            assignment_id,
            student_name="Combined Student",
            score_correct=1,
            score_total=1,
            question_ids_json=self._attempt_plan(combined_score=True),
        )
        self._seed_open_answer(
            attempt_id,
            question_id=42,
            subquestion_number=1,
            points_awarded=1.0,
            points_possible=1.0,
        )
        try:
            response = self.client.get(f"/teacher/assignment/{assignment_id}/results.csv")
            rows, _ = self._parse_csv(response.data)
            attempt_row = next(r for r in rows if r["row_type"] == "attempt")
            self.assertEqual(attempt_row["include_open_answers_in_final_score"], "1")
            self.assertEqual(float(attempt_row["combined_awarded"]), 2.0)
            self.assertEqual(float(attempt_row["combined_possible"]), 2.0)
        finally:
            self._delete_assignment(assignment_id)

    def test_csv_exposes_teacher_override_and_note(self):
        self._login_admin()
        assignment_id = self._create_assignment(
            question_plan_json=self._mixed_assignment_plan(),
            title="Override Export",
        )
        attempt_id = self._seed_attempt(
            assignment_id,
            student_name="Override Student",
            question_ids_json=self._attempt_plan(),
        )
        self._seed_open_answer(
            attempt_id,
            teacher_override=1,
            teacher_note="Прието след преглед",
        )
        try:
            response = self.client.get(f"/teacher/assignment/{assignment_id}/results.csv")
            rows, _ = self._parse_csv(response.data)
            answer_row = next(r for r in rows if r["row_type"] == "open_answer")
            self.assertEqual(answer_row["teacher_override"], "1")
            self.assertEqual(answer_row["teacher_note"], "Прието след преглед")
        finally:
            self._delete_assignment(assignment_id)

    def test_csv_does_not_expose_accepted_answers_json(self):
        self._login_admin()
        assignment_id = self._create_assignment(
            question_plan_json=self._mixed_assignment_plan(),
            title="No Accepted",
        )
        attempt_id = self._seed_attempt(
            assignment_id,
            student_name="No Accepted Student",
            question_ids_json=self._attempt_plan(),
        )
        self._seed_open_answer(attempt_id)
        try:
            response = self.client.get(f"/teacher/assignment/{assignment_id}/results.csv")
            self.assertNotIn(b"accepted_answers_json", response.data)
            self.assertNotIn(b"accepted_answers", response.data)
        finally:
            self._delete_assignment(assignment_id)

    # --- auth -----------------------------------------------------------

    def test_unauthenticated_blocked(self):
        assignment_id = self._create_assignment(title="Auth Export")
        self._seed_attempt(assignment_id, student_name="A")
        try:
            response = self.client.get(f"/teacher/assignment/{assignment_id}/results.csv")
            self.assertEqual(response.status_code, 302)
            self.assertIn("/admin/login", response.headers.get("Location", ""))
        finally:
            self._delete_assignment(assignment_id)

    def test_tester_blocked(self):
        self._login_tester()
        assignment_id = self._create_assignment(title="Tester Export")
        self._seed_attempt(assignment_id, student_name="A")
        try:
            response = self.client.get(f"/teacher/assignment/{assignment_id}/results.csv")
            self.assertEqual(response.status_code, 302)
            self.assertIn("/admin/login", response.headers.get("Location", ""))
        finally:
            self._delete_assignment(assignment_id)

    def test_missing_assignment_returns_404(self):
        self._login_admin()
        response = self.client.get("/teacher/assignment/99999999/results.csv")
        self.assertEqual(response.status_code, 404)

    # --- read-only -----------------------------------------------------

    def test_export_does_not_modify_attempts_or_text_answers(self):
        self._login_admin()
        assignment_id = self._create_assignment(
            question_plan_json=self._mixed_assignment_plan(),
            title="Read Only",
        )
        attempt_id = self._seed_attempt(
            assignment_id,
            student_name="ReadOnly Student",
            question_ids_json=self._attempt_plan(),
        )
        answer_id = self._seed_open_answer(attempt_id, teacher_note="Untouched")

        conn = web_app.quiz_db()
        try:
            attempts_before = conn.execute(
                "SELECT * FROM quiz_attempts WHERE assignment_id = ?",
                (assignment_id,),
            ).fetchall()
            answers_before = conn.execute(
                "SELECT * FROM quiz_text_answers WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchall()
        finally:
            conn.close()

        try:
            response = self.client.get(f"/teacher/assignment/{assignment_id}/results.csv")
            self.assertEqual(response.status_code, 200)

            conn = web_app.quiz_db()
            try:
                attempts_after = conn.execute(
                    "SELECT * FROM quiz_attempts WHERE assignment_id = ?",
                    (assignment_id,),
                ).fetchall()
                answers_after = conn.execute(
                    "SELECT * FROM quiz_text_answers WHERE attempt_id = ?",
                    (attempt_id,),
                ).fetchall()
            finally:
                conn.close()

            self.assertEqual(
                [tuple(r) for r in attempts_before],
                [tuple(r) for r in attempts_after],
            )
            self.assertEqual(
                [tuple(r) for r in answers_before],
                [tuple(r) for r in answers_after],
            )
        finally:
            self._delete_assignment(assignment_id)

    # --- UI surface ----------------------------------------------------

    def test_results_page_shows_export_link(self):
        self._login_admin()
        assignment_id = self._create_assignment(title="Link Visible")
        try:
            response = self.client.get(f"/teacher/assignment/{assignment_id}/results")
            self.assertEqual(response.status_code, 200)
            body = response.data.decode("utf-8")
            self.assertIn(f"/teacher/assignment/{assignment_id}/results.csv", body)
            self.assertIn("Експортирай CSV", body)
        finally:
            self._delete_assignment(assignment_id)


if __name__ == "__main__":
    unittest.main()
