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


class AssignmentResultsSummaryTest(unittest.TestCase):
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

    def _create_assignment(self, *, question_plan_json=None, title="Summary Quiz"):
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

    def _get_results(self, assignment_id):
        return self.client.get(f"/teacher/assignment/{assignment_id}/results")

    # --- MC-only --------------------------------------------------------

    def test_mc_only_summary_shows_high_low_and_no_open_stats(self):
        self._login_admin()
        assignment_id = self._create_assignment(title="MC Summary")
        self._seed_attempt(assignment_id, student_name="High", score_correct=4, score_total=4)
        self._seed_attempt(assignment_id, student_name="Mid", score_correct=2, score_total=4)
        self._seed_attempt(assignment_id, student_name="Low", score_correct=1, score_total=4)
        try:
            response = self._get_results(assignment_id)
            self.assertEqual(response.status_code, 200)
            body = response.data.decode("utf-8")
            self.assertIn("Аналитика на резултатите", body)
            self.assertIn("MC: най-висок %", body)
            self.assertIn("MC: най-нисък %", body)
            self.assertIn("100.0%", body)
            self.assertIn("25.0%", body)
            self.assertNotIn("Опити с отворени отговори", body)
            self.assertNotIn("Записани отворени редове", body)
            self.assertNotIn("Авто-съвпадения", body)
            self.assertNotIn("Учителски override", body)
        finally:
            self._delete_assignment(assignment_id)

    def test_no_submitted_attempts_shows_empty_summary_message(self):
        self._login_admin()
        assignment_id = self._create_assignment(title="Empty Summary")
        self._seed_attempt(assignment_id, student_name="Pending", submitted=False)
        try:
            response = self._get_results(assignment_id)
            self.assertEqual(response.status_code, 200)
            body = response.data.decode("utf-8")
            self.assertIn("Аналитика на резултатите", body)
            self.assertIn("Все още няма предадени опити за анализ.", body)
            self.assertNotIn("MC: най-висок %", body)
        finally:
            self._delete_assignment(assignment_id)

    def test_unsubmitted_attempts_excluded_from_min_max(self):
        self._login_admin()
        assignment_id = self._create_assignment(title="Skip Unfinished")
        self._seed_attempt(assignment_id, student_name="Done High", score_correct=4, score_total=4)
        # Unsubmitted with extreme score that should be ignored
        self._seed_attempt(
            assignment_id,
            student_name="Pending Low",
            score_correct=0,
            score_total=4,
            submitted=False,
        )
        try:
            response = self._get_results(assignment_id)
            body = response.data.decode("utf-8")
            # Only one submitted attempt exists; the unfinished 0/4 row must
            # not contribute a 0% lowest. Highest and lowest both come from
            # the single submitted 4/4 attempt.
            low_label = "MC: най-нисък %"
            low_idx = body.find(low_label)
            self.assertGreater(low_idx, -1)
            low_card_slice = body[low_idx:low_idx + 400]
            self.assertIn("100.0%", low_card_slice)
            self.assertNotIn(">0.0%<", low_card_slice)
            self.assertNotIn(">0%<", low_card_slice)
        finally:
            self._delete_assignment(assignment_id)

    # --- Mixed/open -----------------------------------------------------

    def test_mixed_summary_shows_open_stats_and_override_count(self):
        self._login_admin()
        assignment_id = self._create_assignment(
            question_plan_json=self._mixed_assignment_plan(combined_score=False),
            title="Mixed Summary",
        )
        attempt_id = self._seed_attempt(
            assignment_id,
            student_name="Mixed Student",
            score_correct=1,
            score_total=1,
            question_ids_json=self._attempt_plan(),
        )
        # Two open rows: one auto-matched, one not; one teacher-overridden.
        self._seed_open_answer(
            attempt_id,
            subquestion_number=1,
            is_correct=1,
            points_awarded=1.0,
            points_possible=1.0,
        )
        self._seed_open_answer(
            attempt_id,
            subquestion_number=2,
            matched_answer=None,
            is_correct=0,
            points_awarded=0.0,
            points_possible=1.0,
            teacher_override=1,
            teacher_note="Прието",
        )
        try:
            response = self._get_results(assignment_id)
            self.assertEqual(response.status_code, 200)
            body = response.data.decode("utf-8")
            self.assertIn("Опити с отворени отговори", body)
            self.assertIn("Записани отворени редове", body)
            self.assertIn("Авто-съвпадения", body)
            self.assertIn("Учителски override", body)
            # Open subtotal: override credits possible (1.0) + auto match (1.0) = 2.0/2.0
            self.assertIn("2.00/2.00", body)
            # Honest wording about open answers
            self.assertIn("преглед/сборни данни", body)
            self.assertIn("не променят съхранения MC резултат", body)
            # Combined score off for this assignment
            self.assertIn("Сборният резултат не е активиран", body)
        finally:
            self._delete_assignment(assignment_id)

    def test_mixed_summary_combined_score_wording_when_enabled(self):
        self._login_admin()
        assignment_id = self._create_assignment(
            question_plan_json=self._mixed_assignment_plan(combined_score=True),
            title="Mixed Combined Summary",
        )
        attempt_id = self._seed_attempt(
            assignment_id,
            student_name="Combined Student",
            score_correct=1,
            score_total=1,
            question_ids_json=self._attempt_plan(combined_score=True),
        )
        self._seed_open_answer(attempt_id)
        try:
            response = self._get_results(assignment_id)
            body = response.data.decode("utf-8")
            self.assertIn("Сборният резултат е активен само за визуализация.", body)
        finally:
            self._delete_assignment(assignment_id)

    # --- safety / read-only --------------------------------------------

    def test_results_page_does_not_expose_accepted_answers_json(self):
        self._login_admin()
        assignment_id = self._create_assignment(
            question_plan_json=self._mixed_assignment_plan(),
            title="No Accepted In Summary",
        )
        attempt_id = self._seed_attempt(
            assignment_id,
            student_name="No Accepted",
            question_ids_json=self._attempt_plan(),
        )
        self._seed_open_answer(attempt_id)
        try:
            response = self._get_results(assignment_id)
            self.assertNotIn(b"accepted_answers_json", response.data)
            self.assertNotIn(b"accepted_answers", response.data)
        finally:
            self._delete_assignment(assignment_id)

    def test_results_page_render_does_not_modify_db(self):
        self._login_admin()
        assignment_id = self._create_assignment(
            question_plan_json=self._mixed_assignment_plan(),
            title="Read Only Render",
        )
        attempt_id = self._seed_attempt(
            assignment_id,
            student_name="ReadOnly",
            question_ids_json=self._attempt_plan(),
        )
        self._seed_open_answer(attempt_id, teacher_note="Untouched")

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
            response = self._get_results(assignment_id)
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

    # --- auth -----------------------------------------------------------

    def test_unauthenticated_blocked_from_results(self):
        assignment_id = self._create_assignment(title="Auth Summary")
        self._seed_attempt(assignment_id, student_name="A")
        try:
            response = self._get_results(assignment_id)
            self.assertEqual(response.status_code, 302)
            self.assertIn("/admin/login", response.headers.get("Location", ""))
        finally:
            self._delete_assignment(assignment_id)

    def test_tester_blocked_from_results(self):
        self._login_tester()
        assignment_id = self._create_assignment(title="Tester Summary")
        self._seed_attempt(assignment_id, student_name="A")
        try:
            response = self._get_results(assignment_id)
            self.assertEqual(response.status_code, 302)
            self.assertIn("/admin/login", response.headers.get("Location", ""))
        finally:
            self._delete_assignment(assignment_id)


if __name__ == "__main__":
    unittest.main()
