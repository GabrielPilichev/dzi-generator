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


class AssignmentResultsExportFilteredTest(unittest.TestCase):
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

    def _create_assignment(self, *, question_plan_json=None, title="Filtered Export"):
        conn = web_app.quiz_db()
        try:
            cur = conn.execute("""
                INSERT INTO quiz_assignments (
                    section_id, title_bg, question_count, time_limit_minutes, question_plan_json
                )
                VALUES (?, ?, ?, ?, ?)
            """, (self.section["id"], title, 4, None, question_plan_json))
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
        score_total=4,
        question_ids_json=None,
        submitted_at=None,
        started_at=None,
    ):
        conn = web_app.quiz_db()
        try:
            cur = conn.execute("""
                INSERT INTO quiz_attempts (
                    assignment_id, student_name, seed, question_ids_json,
                    score_total, score_correct, started_at, submitted_at
                )
                VALUES (?, ?, ?, ?, ?, ?,
                    COALESCE(?, datetime('now')),
                    ?
                )
            """, (
                assignment_id,
                student_name,
                1,
                question_ids_json or "[1, 2, 3, 4]",
                score_total,
                score_correct,
                started_at,
                submitted_at,
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
        points_awarded=1.0,
        points_possible=1.0,
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
                attempt_id, question_id, subquestion_number, subquestion_number,
                "клиент", "клиент", "ordered",
                json.dumps(["клиент"]), "клиент", 1,
                points_awarded, points_possible, "v1", 0, None,
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
    def _attempt_plan():
        return json.dumps({
            "mixed_open_enabled": True,
            "question_ids": [1, 42],
            "open_question_ids": [42],
            "include_open_answers_in_final_score": False,
        })

    @staticmethod
    def _mixed_assignment_plan():
        return json.dumps({
            "mixed_open_enabled": True,
            "question_ids": [1, 42],
            "open_question_ids": [42],
            "include_open_answers_in_final_score": False,
        })

    def _parse_csv(self, body: bytes):
        text = body.decode("utf-8")
        reader = csv.DictReader(io.StringIO(text))
        return list(reader), reader.fieldnames or []

    # --- existing full export still works -------------------------------

    def test_full_export_unchanged_filename(self):
        self._login_admin()
        assignment_id = self._create_assignment(title="Full Export")
        self._seed_attempt(
            assignment_id,
            student_name="Alice",
            submitted_at="2026-05-01 09:00:00",
        )
        self._seed_attempt(
            assignment_id,
            student_name="Bob",
            submitted_at="2026-05-02 09:00:00",
        )
        try:
            response = self.client.get(f"/teacher/assignment/{assignment_id}/results.csv")
            self.assertEqual(response.status_code, 200)
            self.assertIn(
                f'filename="assignment_{assignment_id}_results.csv"',
                response.headers.get("Content-Disposition", ""),
            )
            rows, _ = self._parse_csv(response.data)
            names = {r["student_name"] for r in rows if r["row_type"] == "attempt"}
            self.assertEqual(names, {"Alice", "Bob"})
        finally:
            self._delete_assignment(assignment_id)

    def test_full_export_skips_unsubmitted_even_with_filter_params(self):
        self._login_admin()
        assignment_id = self._create_assignment(title="Full Skips Unsubmitted")
        self._seed_attempt(
            assignment_id,
            student_name="Done",
            submitted_at="2026-05-01 09:00:00",
        )
        self._seed_attempt(assignment_id, student_name="Pending", submitted_at=None)
        try:
            # No filtered=1 → ignore q/status/open even if present.
            response = self.client.get(
                f"/teacher/assignment/{assignment_id}/results.csv?status=unsubmitted"
            )
            self.assertEqual(response.status_code, 200)
            rows, _ = self._parse_csv(response.data)
            names = {r["student_name"] for r in rows if r["row_type"] == "attempt"}
            self.assertEqual(names, {"Done"})
            self.assertIn(
                f'filename="assignment_{assignment_id}_results.csv"',
                response.headers.get("Content-Disposition", ""),
            )
        finally:
            self._delete_assignment(assignment_id)

    # --- filtered export ------------------------------------------------

    def test_filtered_export_uses_filtered_filename(self):
        self._login_admin()
        assignment_id = self._create_assignment(title="Filtered Filename")
        self._seed_attempt(
            assignment_id,
            student_name="Alice",
            submitted_at="2026-05-01 09:00:00",
        )
        try:
            response = self.client.get(
                f"/teacher/assignment/{assignment_id}/results.csv?filtered=1"
            )
            self.assertEqual(response.status_code, 200)
            self.assertIn(
                f'filename="assignment_{assignment_id}_results_filtered.csv"',
                response.headers.get("Content-Disposition", ""),
            )
        finally:
            self._delete_assignment(assignment_id)

    def test_filtered_export_with_q_keeps_only_matching_students(self):
        self._login_admin()
        assignment_id = self._create_assignment(title="Filtered Q")
        self._seed_attempt(
            assignment_id,
            student_name="Alice Anderson",
            submitted_at="2026-05-01 09:00:00",
        )
        self._seed_attempt(
            assignment_id,
            student_name="Bob Brown",
            submitted_at="2026-05-02 09:00:00",
        )
        try:
            response = self.client.get(
                f"/teacher/assignment/{assignment_id}/results.csv?filtered=1&q=ali"
            )
            rows, _ = self._parse_csv(response.data)
            names = {r["student_name"] for r in rows if r["row_type"] == "attempt"}
            self.assertEqual(names, {"Alice Anderson"})
        finally:
            self._delete_assignment(assignment_id)

    def test_filtered_export_open_has_open_keeps_only_attempts_with_open_answers(self):
        self._login_admin()
        assignment_id = self._create_assignment(
            question_plan_json=self._mixed_assignment_plan(),
            title="Filtered Open Has",
        )
        with_open = self._seed_attempt(
            assignment_id,
            student_name="HasOpen",
            question_ids_json=self._attempt_plan(),
            submitted_at="2026-05-01 09:00:00",
        )
        self._seed_open_answer(with_open)
        self._seed_attempt(
            assignment_id,
            student_name="NoOpen",
            question_ids_json=self._attempt_plan(),
            submitted_at="2026-05-02 09:00:00",
        )
        try:
            response = self.client.get(
                f"/teacher/assignment/{assignment_id}/results.csv?filtered=1&open=has_open"
            )
            rows, _ = self._parse_csv(response.data)
            attempt_names = {r["student_name"] for r in rows if r["row_type"] == "attempt"}
            self.assertEqual(attempt_names, {"HasOpen"})
            answer_rows = [r for r in rows if r["row_type"] == "open_answer"]
            self.assertEqual(len(answer_rows), 1)
        finally:
            self._delete_assignment(assignment_id)

    def test_filtered_export_status_unsubmitted_includes_unsubmitted_with_blank_mc(self):
        self._login_admin()
        assignment_id = self._create_assignment(title="Filtered Unsubmitted")
        self._seed_attempt(
            assignment_id,
            student_name="Done",
            submitted_at="2026-05-01 09:00:00",
        )
        self._seed_attempt(assignment_id, student_name="Pending", submitted_at=None)
        try:
            response = self.client.get(
                f"/teacher/assignment/{assignment_id}/results.csv?filtered=1&status=unsubmitted"
            )
            rows, _ = self._parse_csv(response.data)
            self.assertEqual(len(rows), 1)
            row = rows[0]
            self.assertEqual(row["row_type"], "attempt")
            self.assertEqual(row["student_name"], "Pending")
            self.assertEqual(row["submitted_at"], "")
            self.assertEqual(row["mc_score_correct"], "")
            self.assertEqual(row["mc_score_total"], "")
            self.assertEqual(row["mc_percent"], "")
            self.assertEqual(row["open_answer_count"], "0")
        finally:
            self._delete_assignment(assignment_id)

    def test_filtered_export_status_submitted_excludes_unsubmitted(self):
        self._login_admin()
        assignment_id = self._create_assignment(title="Filtered Submitted Only")
        self._seed_attempt(
            assignment_id,
            student_name="Done",
            submitted_at="2026-05-01 09:00:00",
        )
        self._seed_attempt(assignment_id, student_name="Pending", submitted_at=None)
        try:
            response = self.client.get(
                f"/teacher/assignment/{assignment_id}/results.csv?filtered=1&status=submitted"
            )
            rows, _ = self._parse_csv(response.data)
            names = {r["student_name"] for r in rows if r["row_type"] == "attempt"}
            self.assertEqual(names, {"Done"})
        finally:
            self._delete_assignment(assignment_id)

    def test_filtered_export_respects_sort_order(self):
        self._login_admin()
        assignment_id = self._create_assignment(title="Filtered Sort")
        self._seed_attempt(
            assignment_id,
            student_name="Charlie",
            score_correct=4,
            score_total=4,
            submitted_at="2026-05-01 09:00:00",
        )
        self._seed_attempt(
            assignment_id,
            student_name="Alice",
            score_correct=1,
            score_total=4,
            submitted_at="2026-05-02 09:00:00",
        )
        self._seed_attempt(
            assignment_id,
            student_name="Bob",
            score_correct=2,
            score_total=4,
            submitted_at="2026-05-03 09:00:00",
        )
        try:
            response = self.client.get(
                f"/teacher/assignment/{assignment_id}/results.csv?filtered=1&sort=mc_desc"
            )
            rows, _ = self._parse_csv(response.data)
            ordered_names = [r["student_name"] for r in rows if r["row_type"] == "attempt"]
            # Charlie 100%, Bob 50%, Alice 25%
            self.assertEqual(ordered_names, ["Charlie", "Bob", "Alice"])
        finally:
            self._delete_assignment(assignment_id)

    def test_filtered_export_invalid_sort_falls_back_safely(self):
        self._login_admin()
        assignment_id = self._create_assignment(title="Filtered Invalid Sort")
        self._seed_attempt(
            assignment_id,
            student_name="Alice",
            submitted_at="2026-05-01 09:00:00",
        )
        try:
            response = self.client.get(
                f"/teacher/assignment/{assignment_id}/results.csv?filtered=1&sort=bogus&status=zzz"
            )
            self.assertEqual(response.status_code, 200)
            rows, _ = self._parse_csv(response.data)
            names = {r["student_name"] for r in rows if r["row_type"] == "attempt"}
            self.assertEqual(names, {"Alice"})
        finally:
            self._delete_assignment(assignment_id)

    def test_filtered_export_open_sort_on_mc_only_falls_back_safely(self):
        self._login_admin()
        assignment_id = self._create_assignment(title="Filtered MC Open Sort")
        self._seed_attempt(
            assignment_id,
            student_name="Alice",
            score_correct=4,
            score_total=4,
            submitted_at="2026-05-01 09:00:00",
        )
        self._seed_attempt(
            assignment_id,
            student_name="Bob",
            score_correct=1,
            score_total=4,
            submitted_at="2026-05-02 09:00:00",
        )
        try:
            response = self.client.get(
                f"/teacher/assignment/{assignment_id}/results.csv?filtered=1&sort=open_desc"
            )
            self.assertEqual(response.status_code, 200)
            rows, _ = self._parse_csv(response.data)
            # Falls back to default SQL order: most recently submitted first.
            ordered_names = [r["student_name"] for r in rows if r["row_type"] == "attempt"]
            self.assertEqual(ordered_names, ["Bob", "Alice"])
        finally:
            self._delete_assignment(assignment_id)

    # --- safety --------------------------------------------------------

    def test_filtered_export_does_not_expose_accepted_answers_json(self):
        self._login_admin()
        assignment_id = self._create_assignment(
            question_plan_json=self._mixed_assignment_plan(),
            title="Filtered No Accepted",
        )
        attempt_id = self._seed_attempt(
            assignment_id,
            student_name="Acc",
            question_ids_json=self._attempt_plan(),
            submitted_at="2026-05-01 09:00:00",
        )
        self._seed_open_answer(attempt_id)
        try:
            response = self.client.get(
                f"/teacher/assignment/{assignment_id}/results.csv?filtered=1&open=has_open"
            )
            self.assertNotIn(b"accepted_answers_json", response.data)
            self.assertNotIn(b"accepted_answers", response.data)
        finally:
            self._delete_assignment(assignment_id)

    def test_filtered_export_does_not_modify_db(self):
        self._login_admin()
        assignment_id = self._create_assignment(
            question_plan_json=self._mixed_assignment_plan(),
            title="Filtered No Writes",
        )
        attempt_id = self._seed_attempt(
            assignment_id,
            student_name="Persistent",
            question_ids_json=self._attempt_plan(),
            submitted_at="2026-05-01 09:00:00",
        )
        self._seed_open_answer(attempt_id)

        conn = web_app.quiz_db()
        try:
            attempts_before = [
                tuple(r) for r in conn.execute(
                    "SELECT * FROM quiz_attempts WHERE assignment_id = ?",
                    (assignment_id,),
                ).fetchall()
            ]
            answers_before = [
                tuple(r) for r in conn.execute(
                    "SELECT * FROM quiz_text_answers WHERE attempt_id = ?",
                    (attempt_id,),
                ).fetchall()
            ]
        finally:
            conn.close()

        try:
            response = self.client.get(
                f"/teacher/assignment/{assignment_id}/results.csv?filtered=1&q=Persistent&sort=mc_desc"
            )
            self.assertEqual(response.status_code, 200)
            conn = web_app.quiz_db()
            try:
                attempts_after = [
                    tuple(r) for r in conn.execute(
                        "SELECT * FROM quiz_attempts WHERE assignment_id = ?",
                        (assignment_id,),
                    ).fetchall()
                ]
                answers_after = [
                    tuple(r) for r in conn.execute(
                        "SELECT * FROM quiz_text_answers WHERE attempt_id = ?",
                        (attempt_id,),
                    ).fetchall()
                ]
            finally:
                conn.close()
            self.assertEqual(attempts_before, attempts_after)
            self.assertEqual(answers_before, answers_after)
        finally:
            self._delete_assignment(assignment_id)

    # --- auth ----------------------------------------------------------

    def test_unauthenticated_blocked_from_filtered_export(self):
        assignment_id = self._create_assignment(title="Auth Filtered")
        self._seed_attempt(
            assignment_id,
            student_name="A",
            submitted_at="2026-05-01 09:00:00",
        )
        try:
            response = self.client.get(
                f"/teacher/assignment/{assignment_id}/results.csv?filtered=1"
            )
            self.assertEqual(response.status_code, 302)
            self.assertIn("/admin/login", response.headers.get("Location", ""))
        finally:
            self._delete_assignment(assignment_id)

    def test_tester_blocked_from_filtered_export(self):
        self._login_tester()
        assignment_id = self._create_assignment(title="Tester Filtered")
        self._seed_attempt(
            assignment_id,
            student_name="A",
            submitted_at="2026-05-01 09:00:00",
        )
        try:
            response = self.client.get(
                f"/teacher/assignment/{assignment_id}/results.csv?filtered=1"
            )
            self.assertEqual(response.status_code, 302)
            self.assertIn("/admin/login", response.headers.get("Location", ""))
        finally:
            self._delete_assignment(assignment_id)

    # --- UI -------------------------------------------------------------

    def test_results_page_shows_filtered_export_link_only_when_active(self):
        self._login_admin()
        assignment_id = self._create_assignment(title="UI Filtered Link")
        self._seed_attempt(
            assignment_id,
            student_name="A",
            submitted_at="2026-05-01 09:00:00",
        )
        try:
            inactive = self.client.get(f"/teacher/assignment/{assignment_id}/results")
            self.assertEqual(inactive.status_code, 200)
            inactive_body = inactive.data.decode("utf-8")
            # Full export link always present
            self.assertIn(
                f"/teacher/assignment/{assignment_id}/results.csv",
                inactive_body,
            )
            # Filtered export link not shown when no filter/sort active
            self.assertNotIn("filtered=1", inactive_body)
            self.assertNotIn("Експортирай CSV (филтрирано)", inactive_body)

            active = self.client.get(
                f"/teacher/assignment/{assignment_id}/results?q=a"
            )
            self.assertEqual(active.status_code, 200)
            active_body = active.data.decode("utf-8")
            self.assertIn("Експортирай CSV (филтрирано)", active_body)
            self.assertIn("filtered=1", active_body)
            self.assertIn("q=a", active_body)
        finally:
            self._delete_assignment(assignment_id)


if __name__ == "__main__":
    unittest.main()
