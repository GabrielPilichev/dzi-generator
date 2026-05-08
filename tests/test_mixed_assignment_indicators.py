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


class MixedAssignmentIndicatorTest(unittest.TestCase):
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

    def _create_assignment(self, *, question_plan_json=None, title="Test"):
        conn = web_app.quiz_db()
        try:
            cur = conn.execute("""
                INSERT INTO quiz_assignments (
                    section_id, title_bg, question_count, time_limit_minutes, question_plan_json
                )
                VALUES (?, ?, 2, NULL, ?)
            """, (self.section["id"], title, question_plan_json))
            assignment_id = int(cur.lastrowid)
            conn.commit()
            return assignment_id
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
            "question_ids": [101, 202],
            "open_question_ids": [202],
            "include_open_answers_in_final_score": combined_score,
        })

    # --- mixed_status helper ---------------------------------------------

    def test_mixed_status_for_null_plan_is_mc_only(self):
        status = web_app.quiz_assignment_mixed_status(None)
        self.assertFalse(status["is_mixed"])
        self.assertEqual(status["open_count"], 0)
        self.assertFalse(status["combined_score"])
        self.assertFalse(status["plan_invalid"])

    def test_mixed_status_for_valid_plan_reports_open_count_and_flag(self):
        status = web_app.quiz_assignment_mixed_status(self._mixed_plan(combined_score=True))
        self.assertTrue(status["is_mixed"])
        self.assertEqual(status["open_count"], 1)
        self.assertTrue(status["combined_score"])
        self.assertFalse(status["plan_invalid"])

    def test_mixed_status_for_malformed_plan_is_safe_fallback(self):
        status = web_app.quiz_assignment_mixed_status("{not json")
        self.assertFalse(status["is_mixed"])
        self.assertTrue(status["plan_invalid"])

    # --- teacher_assignments list ----------------------------------------

    def test_assignments_list_mc_only_has_no_mixed_badge(self):
        mc_id = self._create_assignment(title="MC Only Indicator")
        try:
            self._login_admin()
            response = self.client.get("/teacher/assignments")
            self.assertEqual(response.status_code, 200)
            body = response.data.decode("utf-8")
            self.assertIn("MC Only Indicator", body)
            mc_block = self._row_block(body, "MC Only Indicator")
            self.assertNotIn("Смесен тест", mc_block)
            self.assertNotIn("Сборен резултат включен", mc_block)
        finally:
            self._delete_assignment(mc_id)

    def test_assignments_list_mixed_shows_badge_and_open_count(self):
        mixed_id = self._create_assignment(
            question_plan_json=self._mixed_plan(combined_score=False),
            title="Mixed Indicator NoCombined",
        )
        try:
            self._login_admin()
            response = self.client.get("/teacher/assignments")
            self.assertEqual(response.status_code, 200)
            body = response.data.decode("utf-8")
            mixed_block = self._row_block(body, "Mixed Indicator NoCombined")
            self.assertIn("Смесен тест · 1 отворени", mixed_block)
            self.assertNotIn("Сборен резултат включен", mixed_block)
        finally:
            self._delete_assignment(mixed_id)

    def test_assignments_list_combined_score_pill_only_when_flag_true(self):
        combined_id = self._create_assignment(
            question_plan_json=self._mixed_plan(combined_score=True),
            title="Mixed Indicator Combined",
        )
        try:
            self._login_admin()
            response = self.client.get("/teacher/assignments")
            self.assertEqual(response.status_code, 200)
            body = response.data.decode("utf-8")
            block = self._row_block(body, "Mixed Indicator Combined")
            self.assertIn("Смесен тест · 1 отворени", block)
            self.assertIn("Сборен резултат включен", block)
        finally:
            self._delete_assignment(combined_id)

    def test_assignments_list_malformed_plan_does_not_crash(self):
        broken_id = self._create_assignment(
            question_plan_json="{this is not json",
            title="Broken Plan Row",
        )
        try:
            self._login_admin()
            response = self.client.get("/teacher/assignments")
            self.assertEqual(response.status_code, 200)
            body = response.data.decode("utf-8")
            self.assertIn("Broken Plan Row", body)
            block = self._row_block(body, "Broken Plan Row")
            self.assertNotIn("Смесен тест", block)
            self.assertIn("Невалиден план", block)
        finally:
            self._delete_assignment(broken_id)

    def test_assignments_list_filter_mc_excludes_mixed(self):
        mc_id = self._create_assignment(title="Filter MC Only")
        mixed_id = self._create_assignment(
            question_plan_json=self._mixed_plan(),
            title="Filter Mixed Row",
        )
        try:
            self._login_admin()
            response = self.client.get("/teacher/assignments?type=mc")
            self.assertEqual(response.status_code, 200)
            body = response.data.decode("utf-8")
            self.assertIn("Filter MC Only", body)
            self.assertNotIn("Filter Mixed Row", body)
        finally:
            self._delete_assignment(mc_id)
            self._delete_assignment(mixed_id)

    def test_assignments_list_filter_mixed_excludes_mc(self):
        mc_id = self._create_assignment(title="Filter MC Hidden")
        mixed_id = self._create_assignment(
            question_plan_json=self._mixed_plan(),
            title="Filter Mixed Visible",
        )
        try:
            self._login_admin()
            response = self.client.get("/teacher/assignments?type=mixed")
            self.assertEqual(response.status_code, 200)
            body = response.data.decode("utf-8")
            self.assertNotIn("Filter MC Hidden", body)
            self.assertIn("Filter Mixed Visible", body)
        finally:
            self._delete_assignment(mc_id)
            self._delete_assignment(mixed_id)

    def test_assignments_list_unknown_filter_falls_back_to_all(self):
        mc_id = self._create_assignment(title="Filter Fallback MC")
        mixed_id = self._create_assignment(
            question_plan_json=self._mixed_plan(),
            title="Filter Fallback Mixed",
        )
        try:
            self._login_admin()
            response = self.client.get("/teacher/assignments?type=bogus")
            self.assertEqual(response.status_code, 200)
            body = response.data.decode("utf-8")
            self.assertIn("Filter Fallback MC", body)
            self.assertIn("Filter Fallback Mixed", body)
        finally:
            self._delete_assignment(mc_id)
            self._delete_assignment(mixed_id)

    # --- teacher dashboard ----------------------------------------------

    def test_teacher_dashboard_shows_mixed_indicator(self):
        mixed_id = self._create_assignment(
            question_plan_json=self._mixed_plan(combined_score=True),
            title="Dashboard Mixed Row",
        )
        try:
            self._login_admin()
            response = self.client.get("/teacher")
            self.assertEqual(response.status_code, 200)
            body = response.data.decode("utf-8")
            self.assertIn("Dashboard Mixed Row", body)
            block = self._row_block(body, "Dashboard Mixed Row")
            self.assertIn("Смесен · 1 отворени", block)
            self.assertIn("Сборен резултат", block)
        finally:
            self._delete_assignment(mixed_id)

    def test_teacher_dashboard_mc_row_has_no_mixed_indicator(self):
        mc_id = self._create_assignment(title="Dashboard MC Row")
        try:
            self._login_admin()
            response = self.client.get("/teacher")
            self.assertEqual(response.status_code, 200)
            body = response.data.decode("utf-8")
            block = self._row_block(body, "Dashboard MC Row")
            self.assertNotIn("Смесен", block)
        finally:
            self._delete_assignment(mc_id)

    # --- teacher_assignment detail --------------------------------------

    def test_teacher_assignment_detail_mixed_status_block(self):
        mixed_id = self._create_assignment(
            question_plan_json=self._mixed_plan(combined_score=True),
            title="Detail Mixed",
        )
        try:
            self._login_admin()
            response = self.client.get(f"/teacher/assignment/{mixed_id}")
            self.assertEqual(response.status_code, 200)
            body = response.data.decode("utf-8")
            self.assertIn("Тип тест", body)
            self.assertIn("Смесен/отворен", body)
            self.assertIn("Отворени въпроси", body)
            self.assertIn("Сборен резултат (показване)", body)
            self.assertIn("включен", body)
        finally:
            self._delete_assignment(mixed_id)

    def test_teacher_assignment_detail_mc_only_shows_mc_label(self):
        mc_id = self._create_assignment(title="Detail MC Only")
        try:
            self._login_admin()
            response = self.client.get(f"/teacher/assignment/{mc_id}")
            self.assertEqual(response.status_code, 200)
            body = response.data.decode("utf-8")
            self.assertIn("Само МСИ", body)
            self.assertNotIn("Отворени въпроси", body)
        finally:
            self._delete_assignment(mc_id)

    # --- teacher_results header -----------------------------------------

    def test_teacher_results_header_shows_mixed_status(self):
        mixed_id = self._create_assignment(
            question_plan_json=self._mixed_plan(combined_score=False),
            title="Results Mixed",
        )
        try:
            self._login_admin()
            response = self.client.get(f"/teacher/assignment/{mixed_id}/results")
            self.assertEqual(response.status_code, 200)
            body = response.data.decode("utf-8")
            self.assertIn("Смесен тест · 1 отворени", body)
            self.assertIn("Сборен резултат изключен", body)
        finally:
            self._delete_assignment(mixed_id)

    def test_teacher_results_header_mc_only_no_mixed_badge(self):
        mc_id = self._create_assignment(title="Results MC")
        try:
            self._login_admin()
            response = self.client.get(f"/teacher/assignment/{mc_id}/results")
            self.assertEqual(response.status_code, 200)
            body = response.data.decode("utf-8")
            self.assertNotIn("Смесен тест", body)
        finally:
            self._delete_assignment(mc_id)

    # --- quiz_start hint ------------------------------------------------

    def test_quiz_start_mixed_assignment_shows_open_hint(self):
        mixed_id = self._create_assignment(
            question_plan_json=self._mixed_plan(combined_score=False),
            title="Start Mixed",
        )
        try:
            response = self.client.get(f"/quiz/{mixed_id}")
            self.assertEqual(response.status_code, 200)
            body = response.data.decode("utf-8")
            # Hint now includes the open question count.
            self.assertIn("Този тест съдържа 1 отворени въпроса.", body)
            self.assertIn("отделно", body)
            self.assertNotIn("Ще виждаш и сборен резултат", body)
        finally:
            self._delete_assignment(mixed_id)

    def test_quiz_start_mixed_with_combined_score_shows_combined_hint(self):
        mixed_id = self._create_assignment(
            question_plan_json=self._mixed_plan(combined_score=True),
            title="Start Mixed Combined",
        )
        try:
            response = self.client.get(f"/quiz/{mixed_id}")
            self.assertEqual(response.status_code, 200)
            body = response.data.decode("utf-8")
            self.assertIn("Този тест съдържа 1 отворени въпроса.", body)
            self.assertIn("Ще виждаш и сборен резултат заедно с MC точките", body)
        finally:
            self._delete_assignment(mixed_id)

    def test_quiz_start_mc_only_has_no_open_hint(self):
        mc_id = self._create_assignment(title="Start MC Only")
        try:
            response = self.client.get(f"/quiz/{mc_id}")
            self.assertEqual(response.status_code, 200)
            body = response.data.decode("utf-8")
            self.assertNotIn("Този тест съдържа отворени въпроси.", body)
        finally:
            self._delete_assignment(mc_id)

    def test_quiz_start_malformed_plan_falls_back_to_no_hint(self):
        broken_id = self._create_assignment(
            question_plan_json="{not valid",
            title="Start Broken Plan",
        )
        try:
            response = self.client.get(f"/quiz/{broken_id}")
            self.assertEqual(response.status_code, 200)
            body = response.data.decode("utf-8")
            self.assertNotIn("Този тест съдържа отворени въпроси.", body)
        finally:
            self._delete_assignment(broken_id)

    # --- helpers --------------------------------------------------------

    @staticmethod
    def _row_block(body: str, marker: str) -> str:
        """Return the slice of body between marker and the next </tr> or </div>."""
        idx = body.find(marker)
        if idx < 0:
            raise AssertionError(f"Marker {marker!r} not found in body")
        end_tr = body.find("</tr>", idx)
        end_div = body.find("</section>", idx)
        candidates = [c for c in (end_tr, end_div) if c >= 0]
        end = min(candidates) if candidates else len(body)
        return body[idx:end]


if __name__ == "__main__":
    unittest.main()
