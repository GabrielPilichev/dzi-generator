import atexit
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


class AssignmentResultsFiltersTest(unittest.TestCase):
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

    def _create_assignment(self, *, question_plan_json=None, title="Filters Quiz"):
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
                question_ids_json or "[1, 2, 3, 4]",
                score_total,
                score_correct,
                1 if submitted else 0,
            ))
            conn.commit()
            return int(cur.lastrowid)
        finally:
            conn.close()

    def _seed_open_answer(self, attempt_id, *, question_id=42, subquestion_number=1):
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
                1.0, 1.0, "v1", 0, None,
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

    # --- default behavior ----------------------------------------------

    def test_default_view_shows_all_attempts(self):
        self._login_admin()
        assignment_id = self._create_assignment(title="Default View")
        self._seed_attempt(assignment_id, student_name="Alice", score_correct=4, score_total=4)
        self._seed_attempt(assignment_id, student_name="Bob", score_correct=1, score_total=4)
        self._seed_attempt(assignment_id, student_name="Carol", submitted=False)
        try:
            response = self.client.get(f"/teacher/assignment/{assignment_id}/results")
            self.assertEqual(response.status_code, 200)
            body = response.data.decode("utf-8")
            self.assertIn("Alice", body)
            self.assertIn("Bob", body)
            self.assertIn("Carol", body)
            # No filter pill on the analytics summary heading.
            summary_idx = body.find("Аналитика на резултатите")
            self.assertGreater(summary_idx, -1)
            summary_block = body[summary_idx:summary_idx + 800]
            self.assertNotIn("филтрирано", summary_block)
        finally:
            self._delete_assignment(assignment_id)

    def test_filter_form_is_rendered(self):
        self._login_admin()
        assignment_id = self._create_assignment(title="Form Visible")
        try:
            response = self.client.get(f"/teacher/assignment/{assignment_id}/results")
            body = response.data.decode("utf-8")
            self.assertIn('id="results-q"', body)
            self.assertIn('name="status"', body)
            # Status radios always available
            self.assertIn('value="submitted"', body)
            self.assertIn('value="unsubmitted"', body)
            # Open filter not shown for MC-only
            self.assertNotIn('name="open"', body)
        finally:
            self._delete_assignment(assignment_id)

    # --- q (student name) ----------------------------------------------

    def test_q_filters_by_student_name_case_insensitive(self):
        self._login_admin()
        assignment_id = self._create_assignment(title="Q Filter")
        self._seed_attempt(assignment_id, student_name="Alice Anderson")
        self._seed_attempt(assignment_id, student_name="Bob Brown")
        try:
            response = self.client.get(
                f"/teacher/assignment/{assignment_id}/results?q=ali"
            )
            self.assertEqual(response.status_code, 200)
            body = response.data.decode("utf-8")
            self.assertIn("Alice Anderson", body)
            self.assertNotIn("Bob Brown", body)
            # Filter active label appears on summary heading
            self.assertIn("филтрирано", body)
            # Clear link present (now also resets sort; bare label "Изчисти")
            self.assertIn("Изчисти", body)
        finally:
            self._delete_assignment(assignment_id)

    def test_q_unicode_case_insensitive(self):
        self._login_admin()
        assignment_id = self._create_assignment(title="Unicode Q")
        self._seed_attempt(assignment_id, student_name="Иван Петров")
        self._seed_attempt(assignment_id, student_name="Мария Иванова")
        try:
            response = self.client.get(
                f"/teacher/assignment/{assignment_id}/results?q=иван"
            )
            body = response.data.decode("utf-8")
            self.assertIn("Иван Петров", body)
            self.assertIn("Мария Иванова", body)  # contains "Иванова"
            response = self.client.get(
                f"/teacher/assignment/{assignment_id}/results?q=мария"
            )
            body = response.data.decode("utf-8")
            self.assertNotIn("Иван Петров", body)
            self.assertIn("Мария Иванова", body)
        finally:
            self._delete_assignment(assignment_id)

    # --- status --------------------------------------------------------

    def test_status_submitted_excludes_unsubmitted(self):
        self._login_admin()
        assignment_id = self._create_assignment(title="Status Submitted")
        self._seed_attempt(assignment_id, student_name="Done")
        self._seed_attempt(assignment_id, student_name="Pending", submitted=False)
        try:
            response = self.client.get(
                f"/teacher/assignment/{assignment_id}/results?status=submitted"
            )
            body = response.data.decode("utf-8")
            self.assertIn("Done", body)
            # "Pending" must not appear in the attempts table — but the form's
            # value attribute won't accidentally match.
            attempts_idx = body.find('<th>Ученик</th>')
            self.assertGreater(attempts_idx, -1)
            attempts_block = body[attempts_idx:]
            self.assertNotIn("Pending", attempts_block)
        finally:
            self._delete_assignment(assignment_id)

    def test_status_unsubmitted_excludes_submitted(self):
        self._login_admin()
        assignment_id = self._create_assignment(title="Status Unsubmitted")
        self._seed_attempt(assignment_id, student_name="Done")
        self._seed_attempt(assignment_id, student_name="Pending", submitted=False)
        try:
            response = self.client.get(
                f"/teacher/assignment/{assignment_id}/results?status=unsubmitted"
            )
            body = response.data.decode("utf-8")
            attempts_idx = body.find('<th>Ученик</th>')
            attempts_block = body[attempts_idx:]
            self.assertIn("Pending", attempts_block)
            self.assertNotIn("Done", attempts_block)
        finally:
            self._delete_assignment(assignment_id)

    def test_invalid_status_falls_back_to_all(self):
        self._login_admin()
        assignment_id = self._create_assignment(title="Bad Status")
        self._seed_attempt(assignment_id, student_name="Done")
        self._seed_attempt(assignment_id, student_name="Pending", submitted=False)
        try:
            response = self.client.get(
                f"/teacher/assignment/{assignment_id}/results?status=bogus"
            )
            self.assertEqual(response.status_code, 200)
            body = response.data.decode("utf-8")
            self.assertIn("Done", body)
            self.assertIn("Pending", body)
        finally:
            self._delete_assignment(assignment_id)

    # --- open filter ---------------------------------------------------

    def test_open_filter_only_shown_for_mixed(self):
        self._login_admin()
        mixed_id = self._create_assignment(
            question_plan_json=self._mixed_assignment_plan(),
            title="Mixed Form",
        )
        try:
            response = self.client.get(f"/teacher/assignment/{mixed_id}/results")
            body = response.data.decode("utf-8")
            self.assertIn('name="open"', body)
            self.assertIn('value="has_open"', body)
            self.assertIn('value="no_open"', body)
        finally:
            self._delete_assignment(mixed_id)

    def test_open_has_open_keeps_attempts_with_recorded_open_answers(self):
        self._login_admin()
        assignment_id = self._create_assignment(
            question_plan_json=self._mixed_assignment_plan(),
            title="Has Open Filter",
        )
        with_open_id = self._seed_attempt(
            assignment_id,
            student_name="With Open",
            question_ids_json=self._attempt_plan(),
            score_correct=1,
            score_total=1,
        )
        self._seed_open_answer(with_open_id)
        self._seed_attempt(
            assignment_id,
            student_name="Without Open",
            question_ids_json=self._attempt_plan(),
            score_correct=1,
            score_total=1,
        )
        try:
            response = self.client.get(
                f"/teacher/assignment/{assignment_id}/results?open=has_open"
            )
            body = response.data.decode("utf-8")
            attempts_block = body[body.find('<th>Ученик</th>'):]
            self.assertIn("With Open", attempts_block)
            self.assertNotIn("Without Open", attempts_block)
        finally:
            self._delete_assignment(assignment_id)

    def test_open_no_open_keeps_attempts_without_recorded_open_answers(self):
        self._login_admin()
        assignment_id = self._create_assignment(
            question_plan_json=self._mixed_assignment_plan(),
            title="No Open Filter",
        )
        with_open_id = self._seed_attempt(
            assignment_id,
            student_name="With Open",
            question_ids_json=self._attempt_plan(),
            score_correct=1,
            score_total=1,
        )
        self._seed_open_answer(with_open_id)
        self._seed_attempt(
            assignment_id,
            student_name="Without Open",
            question_ids_json=self._attempt_plan(),
            score_correct=1,
            score_total=1,
        )
        try:
            response = self.client.get(
                f"/teacher/assignment/{assignment_id}/results?open=no_open"
            )
            body = response.data.decode("utf-8")
            attempts_block = body[body.find('<th>Ученик</th>'):]
            self.assertIn("Without Open", attempts_block)
            self.assertNotIn("With Open", attempts_block)
        finally:
            self._delete_assignment(assignment_id)

    # --- analytics follow filter ---------------------------------------

    def test_analytics_summary_follows_filter(self):
        self._login_admin()
        assignment_id = self._create_assignment(title="Analytics Filter")
        self._seed_attempt(assignment_id, student_name="High", score_correct=4, score_total=4)
        self._seed_attempt(assignment_id, student_name="Low", score_correct=1, score_total=4)
        try:
            response = self.client.get(
                f"/teacher/assignment/{assignment_id}/results?q=high"
            )
            body = response.data.decode("utf-8")
            # Summary should reflect filtered set: only the 100% attempt.
            high_label = "MC: най-нисък %"
            idx = body.find(high_label)
            self.assertGreater(idx, -1)
            slice_ = body[idx:idx + 400]
            self.assertIn("100.0%", slice_)
            self.assertNotIn(">25.0%<", slice_)
        finally:
            self._delete_assignment(assignment_id)

    def test_filter_with_no_matches_shows_empty_state_with_clear_link(self):
        self._login_admin()
        assignment_id = self._create_assignment(title="No Match")
        self._seed_attempt(assignment_id, student_name="Alice")
        try:
            response = self.client.get(
                f"/teacher/assignment/{assignment_id}/results?q=zzz_no_such"
            )
            self.assertEqual(response.status_code, 200)
            body = response.data.decode("utf-8")
            self.assertIn("Няма опити за избраните филтри.", body)
            self.assertIn("Изчисти филтрите", body)
            self.assertIn("Няма предадени опити за избраните филтри.", body)
        finally:
            self._delete_assignment(assignment_id)

    # --- safety / read-only -------------------------------------------

    def test_filtering_does_not_modify_db(self):
        self._login_admin()
        assignment_id = self._create_assignment(
            question_plan_json=self._mixed_assignment_plan(),
            title="No Writes",
        )
        attempt_id = self._seed_attempt(
            assignment_id,
            student_name="Persistent",
            question_ids_json=self._attempt_plan(),
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
                f"/teacher/assignment/{assignment_id}/results?q=Persistent&open=has_open"
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

    def test_filtered_page_still_does_not_expose_accepted_answers_json(self):
        self._login_admin()
        assignment_id = self._create_assignment(
            question_plan_json=self._mixed_assignment_plan(),
            title="No Accepted Filtered",
        )
        attempt_id = self._seed_attempt(
            assignment_id,
            student_name="Accepted Test",
            question_ids_json=self._attempt_plan(),
        )
        self._seed_open_answer(attempt_id)
        try:
            response = self.client.get(
                f"/teacher/assignment/{assignment_id}/results?q=Accepted&open=has_open"
            )
            self.assertNotIn(b"accepted_answers_json", response.data)
            self.assertNotIn(b"accepted_answers", response.data)
        finally:
            self._delete_assignment(assignment_id)

    # --- auth ---------------------------------------------------------

    def test_unauthenticated_blocked(self):
        assignment_id = self._create_assignment(title="Auth Filter")
        self._seed_attempt(assignment_id, student_name="A")
        try:
            response = self.client.get(
                f"/teacher/assignment/{assignment_id}/results?q=a"
            )
            self.assertEqual(response.status_code, 302)
            self.assertIn("/admin/login", response.headers.get("Location", ""))
        finally:
            self._delete_assignment(assignment_id)

    def test_tester_blocked(self):
        self._login_tester()
        assignment_id = self._create_assignment(title="Tester Filter")
        self._seed_attempt(assignment_id, student_name="A")
        try:
            response = self.client.get(
                f"/teacher/assignment/{assignment_id}/results?q=a"
            )
            self.assertEqual(response.status_code, 302)
            self.assertIn("/admin/login", response.headers.get("Location", ""))
        finally:
            self._delete_assignment(assignment_id)


if __name__ == "__main__":
    unittest.main()
