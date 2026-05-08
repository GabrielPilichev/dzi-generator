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


class AssignmentEditMetadataTest(unittest.TestCase):
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

    def _create_assignment(
        self,
        *,
        question_plan_json=None,
        title="Editable Quiz",
        question_count=3,
        time_limit_minutes=10,
    ):
        conn = web_app.quiz_db()
        try:
            cur = conn.execute("""
                INSERT INTO quiz_assignments (
                    section_id, title_bg, question_count, time_limit_minutes, question_plan_json
                )
                VALUES (?, ?, ?, ?, ?)
            """, (
                self.section["id"],
                title,
                question_count,
                time_limit_minutes,
                question_plan_json,
            ))
            assignment_id = int(cur.lastrowid)
            conn.commit()
            return assignment_id
        finally:
            conn.close()

    def _seed_attempt(self, assignment_id, *, student_name="Edit Test Student"):
        conn = web_app.quiz_db()
        try:
            cur = conn.execute("""
                INSERT INTO quiz_attempts (
                    assignment_id, student_name, seed, question_ids_json,
                    score_total, score_correct, submitted_at
                )
                VALUES (?, ?, ?, ?, ?, ?, datetime('now'))
            """, (assignment_id, student_name, 1, "[1,2,3]", 3, 2))
            conn.commit()
            return int(cur.lastrowid)
        finally:
            conn.close()

    def _fetch_assignment(self, assignment_id):
        conn = web_app.quiz_db()
        try:
            return conn.execute(
                "SELECT * FROM quiz_assignments WHERE id = ?",
                (assignment_id,),
            ).fetchone()
        finally:
            conn.close()

    def _fetch_attempt(self, attempt_id):
        conn = web_app.quiz_db()
        try:
            return conn.execute(
                "SELECT * FROM quiz_attempts WHERE id = ?",
                (attempt_id,),
            ).fetchone()
        finally:
            conn.close()

    def _delete_assignment(self, assignment_id):
        conn = web_app.quiz_db()
        try:
            conn.execute("DELETE FROM quiz_attempts WHERE assignment_id = ?", (assignment_id,))
            conn.execute("DELETE FROM quiz_assignments WHERE id = ?", (assignment_id,))
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def _mixed_plan(*, combined_score=False):
        return json.dumps({
            "mixed_open_enabled": True,
            "question_ids": [11, 22, 33],
            "open_question_ids": [22, 33],
            "include_open_answers_in_final_score": combined_score,
        })

    # --- happy paths ----------------------------------------------------

    def test_admin_can_edit_mc_only_title_and_time(self):
        self._login_admin()
        assignment_id = self._create_assignment(title="Old MC Title", time_limit_minutes=10)
        try:
            response = self.client.post(
                f"/teacher/assignment/{assignment_id}/edit",
                data={"title_bg": "Нов МСИ заглавие", "time_limit_minutes": "25"},
            )
            self.assertEqual(response.status_code, 302)
            self.assertIn(
                f"/teacher/assignment/{assignment_id}",
                response.headers.get("Location", ""),
            )
            row = self._fetch_assignment(assignment_id)
            self.assertEqual(row["title_bg"], "Нов МСИ заглавие")
            self.assertEqual(row["time_limit_minutes"], 25)
            self.assertIsNone(row["question_plan_json"])
            self.assertEqual(row["question_count"], 3)
        finally:
            self._delete_assignment(assignment_id)

    def test_admin_edit_mixed_preserves_question_plan_exactly(self):
        self._login_admin()
        plan = self._mixed_plan(combined_score=True)
        assignment_id = self._create_assignment(
            question_plan_json=plan,
            title="Old Mixed",
            time_limit_minutes=20,
        )
        try:
            response = self.client.post(
                f"/teacher/assignment/{assignment_id}/edit",
                data={"title_bg": "Edited Mixed", "time_limit_minutes": ""},
            )
            self.assertEqual(response.status_code, 302)
            row = self._fetch_assignment(assignment_id)
            self.assertEqual(row["title_bg"], "Edited Mixed")
            self.assertIsNone(row["time_limit_minutes"])
            self.assertEqual(row["question_plan_json"], plan)
            parsed = web_app.quiz_parse_assignment_question_plan(row["question_plan_json"])
            self.assertIsNotNone(parsed)
            self.assertEqual(parsed["question_ids"], [11, 22, 33])
            self.assertEqual(parsed["open_question_ids"], [22, 33])
            self.assertTrue(parsed["include_open_answers_in_final_score"])
        finally:
            self._delete_assignment(assignment_id)

    def test_blank_time_clears_limit_to_null(self):
        self._login_admin()
        assignment_id = self._create_assignment(title="With Time", time_limit_minutes=30)
        try:
            response = self.client.post(
                f"/teacher/assignment/{assignment_id}/edit",
                data={"title_bg": "With Time", "time_limit_minutes": ""},
            )
            self.assertEqual(response.status_code, 302)
            row = self._fetch_assignment(assignment_id)
            self.assertIsNone(row["time_limit_minutes"])
        finally:
            self._delete_assignment(assignment_id)

    # --- preservation of attempts/results -------------------------------

    def test_edit_does_not_modify_attempts(self):
        self._login_admin()
        assignment_id = self._create_assignment(title="With Attempts", time_limit_minutes=15)
        attempt_id = self._seed_attempt(assignment_id)
        try:
            before = self._fetch_attempt(attempt_id)
            response = self.client.post(
                f"/teacher/assignment/{assignment_id}/edit",
                data={"title_bg": "Renamed", "time_limit_minutes": "45"},
            )
            self.assertEqual(response.status_code, 302)
            after = self._fetch_attempt(attempt_id)
            self.assertIsNotNone(after)
            self.assertEqual(after["student_name"], before["student_name"])
            self.assertEqual(after["score_correct"], before["score_correct"])
            self.assertEqual(after["score_total"], before["score_total"])
            self.assertEqual(after["question_ids_json"], before["question_ids_json"])
            self.assertEqual(after["submitted_at"], before["submitted_at"])
        finally:
            self._delete_assignment(assignment_id)

    # --- validation -----------------------------------------------------

    def test_empty_title_rejected_and_original_preserved(self):
        self._login_admin()
        assignment_id = self._create_assignment(title="Original Title", time_limit_minutes=10)
        try:
            response = self.client.post(
                f"/teacher/assignment/{assignment_id}/edit",
                data={"title_bg": "   ", "time_limit_minutes": "20"},
            )
            self.assertEqual(response.status_code, 400)
            self.assertIn(b"\xd0\x97\xd0\xb0\xd0\xb3\xd0\xbb\xd0\xb0\xd0\xb2\xd0\xb8\xd0\xb5\xd1\x82\xd0\xbe", response.data)
            row = self._fetch_assignment(assignment_id)
            self.assertEqual(row["title_bg"], "Original Title")
            self.assertEqual(row["time_limit_minutes"], 10)
        finally:
            self._delete_assignment(assignment_id)

    def test_overlong_title_rejected(self):
        self._login_admin()
        assignment_id = self._create_assignment(title="Original")
        try:
            response = self.client.post(
                f"/teacher/assignment/{assignment_id}/edit",
                data={"title_bg": "x" * 201, "time_limit_minutes": "10"},
            )
            self.assertEqual(response.status_code, 400)
            row = self._fetch_assignment(assignment_id)
            self.assertEqual(row["title_bg"], "Original")
        finally:
            self._delete_assignment(assignment_id)

    def test_non_integer_time_rejected(self):
        self._login_admin()
        assignment_id = self._create_assignment(title="Keep", time_limit_minutes=12)
        try:
            response = self.client.post(
                f"/teacher/assignment/{assignment_id}/edit",
                data={"title_bg": "Keep", "time_limit_minutes": "abc"},
            )
            self.assertEqual(response.status_code, 400)
            row = self._fetch_assignment(assignment_id)
            self.assertEqual(row["time_limit_minutes"], 12)
        finally:
            self._delete_assignment(assignment_id)

    def test_zero_time_rejected(self):
        self._login_admin()
        assignment_id = self._create_assignment(title="Keep", time_limit_minutes=12)
        try:
            response = self.client.post(
                f"/teacher/assignment/{assignment_id}/edit",
                data={"title_bg": "Keep", "time_limit_minutes": "0"},
            )
            self.assertEqual(response.status_code, 400)
            row = self._fetch_assignment(assignment_id)
            self.assertEqual(row["time_limit_minutes"], 12)
        finally:
            self._delete_assignment(assignment_id)

    def test_overlarge_time_rejected(self):
        self._login_admin()
        assignment_id = self._create_assignment(title="Keep", time_limit_minutes=12)
        try:
            response = self.client.post(
                f"/teacher/assignment/{assignment_id}/edit",
                data={"title_bg": "Keep", "time_limit_minutes": "601"},
            )
            self.assertEqual(response.status_code, 400)
            row = self._fetch_assignment(assignment_id)
            self.assertEqual(row["time_limit_minutes"], 12)
        finally:
            self._delete_assignment(assignment_id)

    def test_question_count_form_field_is_ignored(self):
        self._login_admin()
        assignment_id = self._create_assignment(title="Fixed Count", question_count=3)
        try:
            response = self.client.post(
                f"/teacher/assignment/{assignment_id}/edit",
                data={
                    "title_bg": "Fixed Count",
                    "time_limit_minutes": "5",
                    "question_count": "999",
                },
            )
            self.assertEqual(response.status_code, 302)
            row = self._fetch_assignment(assignment_id)
            self.assertEqual(row["question_count"], 3)
        finally:
            self._delete_assignment(assignment_id)

    # --- auth -----------------------------------------------------------

    def test_unauthenticated_edit_blocked(self):
        assignment_id = self._create_assignment(title="Auth MC", time_limit_minutes=10)
        try:
            response = self.client.post(
                f"/teacher/assignment/{assignment_id}/edit",
                data={"title_bg": "Hijacked", "time_limit_minutes": "10"},
            )
            self.assertEqual(response.status_code, 302)
            self.assertIn("/admin/login", response.headers.get("Location", ""))
            row = self._fetch_assignment(assignment_id)
            self.assertEqual(row["title_bg"], "Auth MC")
        finally:
            self._delete_assignment(assignment_id)

    def test_tester_edit_blocked(self):
        self._login_tester()
        assignment_id = self._create_assignment(title="Tester Source")
        try:
            response = self.client.post(
                f"/teacher/assignment/{assignment_id}/edit",
                data={"title_bg": "Hijacked", "time_limit_minutes": "10"},
            )
            self.assertEqual(response.status_code, 302)
            self.assertIn("/admin/login", response.headers.get("Location", ""))
            row = self._fetch_assignment(assignment_id)
            self.assertEqual(row["title_bg"], "Tester Source")
        finally:
            self._delete_assignment(assignment_id)

    def test_edit_missing_assignment_returns_404(self):
        self._login_admin()
        response = self.client.post(
            "/teacher/assignment/99999999/edit",
            data={"title_bg": "x", "time_limit_minutes": "5"},
        )
        self.assertEqual(response.status_code, 404)

    # --- UI surfaces ----------------------------------------------------

    def test_detail_shows_edit_form_for_admin_only(self):
        assignment_id = self._create_assignment(title="UI Source")
        try:
            self._login_admin()
            admin_resp = self.client.get(f"/teacher/assignment/{assignment_id}")
            self.assertEqual(admin_resp.status_code, 200)
            admin_body = admin_resp.data.decode("utf-8")
            self.assertIn(
                f"/teacher/assignment/{assignment_id}/edit",
                admin_body,
            )
            self.assertIn("Редакция на метаданни", admin_body)
            self.assertIn("фиксиран при създаване", admin_body)

            self.setUp()
            self._login_tester()
            tester_resp = self.client.get(f"/teacher/assignment/{assignment_id}")
            self.assertEqual(tester_resp.status_code, 200)
            self.assertNotIn(
                f"/teacher/assignment/{assignment_id}/edit",
                tester_resp.data.decode("utf-8"),
            )
        finally:
            self._delete_assignment(assignment_id)

    def test_mixed_indicators_render_after_edit(self):
        self._login_admin()
        assignment_id = self._create_assignment(
            question_plan_json=self._mixed_plan(combined_score=True),
            title="Indicator Source",
        )
        try:
            response = self.client.post(
                f"/teacher/assignment/{assignment_id}/edit",
                data={"title_bg": "Indicator Renamed", "time_limit_minutes": "15"},
            )
            self.assertEqual(response.status_code, 302)
            detail = self.client.get(f"/teacher/assignment/{assignment_id}")
            self.assertEqual(detail.status_code, 200)
            body = detail.data.decode("utf-8")
            self.assertIn("Indicator Renamed", body)
            self.assertIn("Смесен/отворен", body)
            self.assertIn("Отворени въпроси", body)
        finally:
            self._delete_assignment(assignment_id)


if __name__ == "__main__":
    unittest.main()
