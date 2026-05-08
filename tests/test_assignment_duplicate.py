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


class AssignmentDuplicateTest(unittest.TestCase):
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
        title="Source Quiz",
        question_count=3,
        time_limit_minutes=15,
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

    def _seed_attempt(self, assignment_id, *, student_name="Seed Student"):
        conn = web_app.quiz_db()
        try:
            cur = conn.execute("""
                INSERT INTO quiz_attempts (
                    assignment_id, student_name, seed, question_ids_json, score_total
                )
                VALUES (?, ?, ?, ?, ?)
            """, (assignment_id, student_name, 0, "[]", 0))
            conn.commit()
            return int(cur.lastrowid)
        finally:
            conn.close()

    def _fetch_assignment(self, assignment_id):
        conn = web_app.quiz_db()
        try:
            return conn.execute("""
                SELECT *
                FROM quiz_assignments
                WHERE id = ?
            """, (assignment_id,)).fetchone()
        finally:
            conn.close()

    def _attempts_for(self, assignment_id):
        conn = web_app.quiz_db()
        try:
            return conn.execute("""
                SELECT id
                FROM quiz_attempts
                WHERE assignment_id = ?
            """, (assignment_id,)).fetchall()
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

    def _new_id_from_redirect(self, response):
        self.assertEqual(response.status_code, 302)
        location = response.headers.get("Location", "")
        self.assertIn("/teacher/assignment/", location)
        tail = location.rsplit("/teacher/assignment/", 1)[-1]
        new_id_str = tail.split("/", 1)[0].split("?", 1)[0]
        return int(new_id_str)

    # --- duplication semantics ------------------------------------------

    def test_duplicate_mc_only_keeps_question_plan_null(self):
        self._login_admin()
        source_id = self._create_assignment(title="MC Source")
        new_id = None
        try:
            response = self.client.post(f"/teacher/assignment/{source_id}/duplicate")
            new_id = self._new_id_from_redirect(response)
            self.assertNotEqual(new_id, source_id)

            copy = self._fetch_assignment(new_id)
            source = self._fetch_assignment(source_id)
            self.assertIsNone(copy["question_plan_json"])
            self.assertEqual(copy["section_id"], source["section_id"])
            self.assertEqual(copy["question_count"], source["question_count"])
            self.assertEqual(copy["time_limit_minutes"], source["time_limit_minutes"])
            self.assertEqual(copy["title_bg"], "MC Source (копие)")
        finally:
            if new_id is not None:
                self._delete_assignment(new_id)
            self._delete_assignment(source_id)

    def test_duplicate_caps_title_length_and_preserves_suffix(self):
        self._login_admin()
        source_title = "Д" * web_app.QUIZ_TITLE_MAX_LENGTH
        source_id = self._create_assignment(title=source_title)
        new_id = None
        try:
            response = self.client.post(f"/teacher/assignment/{source_id}/duplicate")
            new_id = self._new_id_from_redirect(response)

            copy = self._fetch_assignment(new_id)
            self.assertLessEqual(len(copy["title_bg"]), web_app.QUIZ_TITLE_MAX_LENGTH)
            self.assertTrue(copy["title_bg"].endswith(web_app.QUIZ_DUPLICATE_TITLE_SUFFIX))
        finally:
            if new_id is not None:
                self._delete_assignment(new_id)
            self._delete_assignment(source_id)

    def test_duplicate_mixed_preserves_question_plan_exactly(self):
        self._login_admin()
        plan = self._mixed_plan(combined_score=True)
        source_id = self._create_assignment(question_plan_json=plan, title="Mixed Source")
        new_id = None
        try:
            response = self.client.post(f"/teacher/assignment/{source_id}/duplicate")
            new_id = self._new_id_from_redirect(response)

            copy = self._fetch_assignment(new_id)
            self.assertEqual(copy["question_plan_json"], plan)
            parsed = web_app.quiz_parse_assignment_question_plan(copy["question_plan_json"])
            self.assertIsNotNone(parsed)
            self.assertEqual(parsed["question_ids"], [11, 22, 33])
            self.assertEqual(parsed["open_question_ids"], [22, 33])
            self.assertTrue(parsed["include_open_answers_in_final_score"])
        finally:
            if new_id is not None:
                self._delete_assignment(new_id)
            self._delete_assignment(source_id)

    def test_duplicate_does_not_copy_attempts(self):
        self._login_admin()
        source_id = self._create_assignment(title="Source With Attempts")
        self._seed_attempt(source_id, student_name="Seed Student A")
        self._seed_attempt(source_id, student_name="Seed Student B")
        new_id = None
        try:
            response = self.client.post(f"/teacher/assignment/{source_id}/duplicate")
            new_id = self._new_id_from_redirect(response)

            self.assertEqual(len(self._attempts_for(source_id)), 2)
            self.assertEqual(self._attempts_for(new_id), [])
        finally:
            if new_id is not None:
                self._delete_assignment(new_id)
            self._delete_assignment(source_id)

    def test_duplicate_malformed_plan_is_copied_verbatim(self):
        self._login_admin()
        broken = "{not valid json"
        source_id = self._create_assignment(question_plan_json=broken, title="Broken Source")
        new_id = None
        try:
            response = self.client.post(f"/teacher/assignment/{source_id}/duplicate")
            new_id = self._new_id_from_redirect(response)

            copy = self._fetch_assignment(new_id)
            self.assertEqual(copy["question_plan_json"], broken)
            self.assertIsNone(web_app.quiz_parse_assignment_question_plan(copy["question_plan_json"]))
        finally:
            if new_id is not None:
                self._delete_assignment(new_id)
            self._delete_assignment(source_id)

    def test_duplicate_missing_source_returns_404(self):
        self._login_admin()
        response = self.client.post("/teacher/assignment/99999999/duplicate")
        self.assertEqual(response.status_code, 404)

    # --- auth -----------------------------------------------------------

    def test_duplicate_requires_admin_unauthenticated_redirects(self):
        source_id = self._create_assignment(title="Auth Guard MC")
        try:
            response = self.client.post(f"/teacher/assignment/{source_id}/duplicate")
            self.assertEqual(response.status_code, 302)
            self.assertIn("/admin/login", response.headers.get("Location", ""))
            attempts_after = self._fetch_assignment(source_id)
            self.assertIsNotNone(attempts_after)
            conn = web_app.quiz_db()
            try:
                count = conn.execute(
                    "SELECT COUNT(*) FROM quiz_assignments WHERE title_bg LIKE ?",
                    ("Auth Guard MC%",),
                ).fetchone()[0]
            finally:
                conn.close()
            self.assertEqual(count, 1)
        finally:
            self._delete_assignment(source_id)

    def test_duplicate_blocked_for_tester_only(self):
        self._login_tester()
        source_id = self._create_assignment(title="Tester Blocked Source")
        try:
            response = self.client.post(f"/teacher/assignment/{source_id}/duplicate")
            self.assertEqual(response.status_code, 302)
            self.assertIn("/admin/login", response.headers.get("Location", ""))
            conn = web_app.quiz_db()
            try:
                count = conn.execute(
                    "SELECT COUNT(*) FROM quiz_assignments WHERE title_bg LIKE ?",
                    ("Tester Blocked Source%",),
                ).fetchone()[0]
            finally:
                conn.close()
            self.assertEqual(count, 1)
        finally:
            self._delete_assignment(source_id)

    # --- end-to-end indicator continuity --------------------------------

    def test_duplicated_mixed_assignment_shows_mixed_indicator(self):
        self._login_admin()
        source_id = self._create_assignment(
            question_plan_json=self._mixed_plan(combined_score=True),
            title="Indicator Source",
        )
        new_id = None
        try:
            response = self.client.post(f"/teacher/assignment/{source_id}/duplicate")
            new_id = self._new_id_from_redirect(response)

            detail = self.client.get(f"/teacher/assignment/{new_id}")
            self.assertEqual(detail.status_code, 200)
            body = detail.data.decode("utf-8")
            self.assertIn("Indicator Source (копие)", body)
            self.assertIn("Смесен/отворен", body)
            self.assertIn("Отворени въпроси", body)
            self.assertIn("включен", body)
        finally:
            if new_id is not None:
                self._delete_assignment(new_id)
            self._delete_assignment(source_id)

    # --- UI surfaces ----------------------------------------------------

    def test_assignments_list_shows_duplicate_button(self):
        self._login_admin()
        source_id = self._create_assignment(title="List Duplicate Visible")
        try:
            response = self.client.get("/teacher/assignments")
            self.assertEqual(response.status_code, 200)
            body = response.data.decode("utf-8")
            self.assertIn(f"/teacher/assignment/{source_id}/duplicate", body)
            self.assertIn(">Дублирай<", body)
        finally:
            self._delete_assignment(source_id)

    def test_detail_shows_duplicate_button_for_admin_only(self):
        source_id = self._create_assignment(title="Detail Duplicate Visibility")
        try:
            self._login_admin()
            admin_resp = self.client.get(f"/teacher/assignment/{source_id}")
            self.assertEqual(admin_resp.status_code, 200)
            self.assertIn(
                f"/teacher/assignment/{source_id}/duplicate",
                admin_resp.data.decode("utf-8"),
            )

            self.setUp()  # fresh client without admin session
            self._login_tester()
            tester_resp = self.client.get(f"/teacher/assignment/{source_id}")
            self.assertEqual(tester_resp.status_code, 200)
            self.assertNotIn(
                f"/teacher/assignment/{source_id}/duplicate",
                tester_resp.data.decode("utf-8"),
            )
        finally:
            self._delete_assignment(source_id)


if __name__ == "__main__":
    unittest.main()
