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


class AssignmentResultsSortTest(unittest.TestCase):
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

    def _create_assignment(self, *, question_plan_json=None, title="Sort Quiz"):
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

    def _student_order(self, body, names):
        positions = []
        attempts_block = body[body.find('<th>Ученик</th>'):]
        for name in names:
            idx = attempts_block.find(f"<strong>{name}</strong>")
            self.assertGreater(idx, -1, f"Expected to find {name} in attempts table")
            positions.append((idx, name))
        positions.sort()
        return [name for _, name in positions]

    def _seed_alpha_set(self, assignment_id):
        # Three submitted attempts with different scores and timestamps.
        self._seed_attempt(
            assignment_id,
            student_name="Charlie",
            score_correct=4,
            score_total=4,
            submitted_at="2026-05-01 09:00:00",
            started_at="2026-05-01 08:00:00",
        )
        self._seed_attempt(
            assignment_id,
            student_name="Alice",
            score_correct=2,
            score_total=4,
            submitted_at="2026-05-02 09:00:00",
            started_at="2026-05-02 08:00:00",
        )
        self._seed_attempt(
            assignment_id,
            student_name="Bob",
            score_correct=1,
            score_total=4,
            submitted_at="2026-05-03 09:00:00",
            started_at="2026-05-03 08:00:00",
        )

    # --- form rendering -------------------------------------------------

    def test_sort_select_present_with_default_selected(self):
        self._login_admin()
        assignment_id = self._create_assignment(title="Sort Form")
        try:
            response = self.client.get(f"/teacher/assignment/{assignment_id}/results")
            body = response.data.decode("utf-8")
            self.assertIn('id="results-sort"', body)
            self.assertIn('name="sort"', body)
            self.assertIn('value="default" selected', body)
            # Open sort options not shown for MC-only
            self.assertNotIn('value="open_desc"', body)
            self.assertNotIn('value="open_asc"', body)
        finally:
            self._delete_assignment(assignment_id)

    def test_sort_select_open_options_only_for_mixed(self):
        self._login_admin()
        mixed_id = self._create_assignment(
            question_plan_json=self._mixed_assignment_plan(),
            title="Sort Mixed Form",
        )
        try:
            response = self.client.get(f"/teacher/assignment/{mixed_id}/results")
            body = response.data.decode("utf-8")
            self.assertIn('value="open_desc"', body)
            self.assertIn('value="open_asc"', body)
        finally:
            self._delete_assignment(mixed_id)

    # --- default order --------------------------------------------------

    def test_default_order_matches_existing_sql_order(self):
        self._login_admin()
        assignment_id = self._create_assignment(title="Default Order")
        # SQL orders submitted by submitted_at DESC. Bob is newest.
        self._seed_alpha_set(assignment_id)
        try:
            response = self.client.get(f"/teacher/assignment/{assignment_id}/results")
            body = response.data.decode("utf-8")
            order = self._student_order(body, ["Alice", "Bob", "Charlie"])
            self.assertEqual(order, ["Bob", "Alice", "Charlie"])
        finally:
            self._delete_assignment(assignment_id)

    # --- name sort ------------------------------------------------------

    def test_sort_name_asc(self):
        self._login_admin()
        assignment_id = self._create_assignment(title="Name Asc")
        self._seed_alpha_set(assignment_id)
        try:
            response = self.client.get(
                f"/teacher/assignment/{assignment_id}/results?sort=name_asc"
            )
            body = response.data.decode("utf-8")
            order = self._student_order(body, ["Alice", "Bob", "Charlie"])
            self.assertEqual(order, ["Alice", "Bob", "Charlie"])
        finally:
            self._delete_assignment(assignment_id)

    def test_sort_name_desc(self):
        self._login_admin()
        assignment_id = self._create_assignment(title="Name Desc")
        self._seed_alpha_set(assignment_id)
        try:
            response = self.client.get(
                f"/teacher/assignment/{assignment_id}/results?sort=name_desc"
            )
            body = response.data.decode("utf-8")
            order = self._student_order(body, ["Alice", "Bob", "Charlie"])
            self.assertEqual(order, ["Charlie", "Bob", "Alice"])
        finally:
            self._delete_assignment(assignment_id)

    # --- mc sort --------------------------------------------------------

    def test_sort_mc_desc(self):
        self._login_admin()
        assignment_id = self._create_assignment(title="MC Desc")
        self._seed_alpha_set(assignment_id)
        try:
            response = self.client.get(
                f"/teacher/assignment/{assignment_id}/results?sort=mc_desc"
            )
            body = response.data.decode("utf-8")
            # Charlie 4/4=100%, Alice 2/4=50%, Bob 1/4=25%
            order = self._student_order(body, ["Alice", "Bob", "Charlie"])
            self.assertEqual(order, ["Charlie", "Alice", "Bob"])
        finally:
            self._delete_assignment(assignment_id)

    def test_sort_mc_asc(self):
        self._login_admin()
        assignment_id = self._create_assignment(title="MC Asc")
        self._seed_alpha_set(assignment_id)
        try:
            response = self.client.get(
                f"/teacher/assignment/{assignment_id}/results?sort=mc_asc"
            )
            body = response.data.decode("utf-8")
            order = self._student_order(body, ["Alice", "Bob", "Charlie"])
            self.assertEqual(order, ["Bob", "Alice", "Charlie"])
        finally:
            self._delete_assignment(assignment_id)

    def test_sort_mc_places_unsubmitted_after_submitted(self):
        self._login_admin()
        assignment_id = self._create_assignment(title="MC With Unsubmitted")
        self._seed_attempt(
            assignment_id,
            student_name="Done",
            score_correct=2,
            score_total=4,
            submitted_at="2026-05-01 09:00:00",
        )
        self._seed_attempt(
            assignment_id,
            student_name="Pending",
            score_correct=0,
            score_total=4,
            submitted_at=None,
        )
        try:
            response = self.client.get(
                f"/teacher/assignment/{assignment_id}/results?sort=mc_desc"
            )
            body = response.data.decode("utf-8")
            order = self._student_order(body, ["Done", "Pending"])
            self.assertEqual(order, ["Done", "Pending"])
        finally:
            self._delete_assignment(assignment_id)

    # --- submitted sort -------------------------------------------------

    def test_sort_submitted_desc(self):
        self._login_admin()
        assignment_id = self._create_assignment(title="Submitted Desc")
        self._seed_alpha_set(assignment_id)
        try:
            response = self.client.get(
                f"/teacher/assignment/{assignment_id}/results?sort=submitted_desc"
            )
            body = response.data.decode("utf-8")
            order = self._student_order(body, ["Alice", "Bob", "Charlie"])
            self.assertEqual(order, ["Bob", "Alice", "Charlie"])
        finally:
            self._delete_assignment(assignment_id)

    def test_sort_submitted_asc(self):
        self._login_admin()
        assignment_id = self._create_assignment(title="Submitted Asc")
        self._seed_alpha_set(assignment_id)
        try:
            response = self.client.get(
                f"/teacher/assignment/{assignment_id}/results?sort=submitted_asc"
            )
            body = response.data.decode("utf-8")
            order = self._student_order(body, ["Alice", "Bob", "Charlie"])
            self.assertEqual(order, ["Charlie", "Alice", "Bob"])
        finally:
            self._delete_assignment(assignment_id)

    # --- open sort ------------------------------------------------------

    def test_sort_open_desc_uses_open_subtotal(self):
        self._login_admin()
        assignment_id = self._create_assignment(
            question_plan_json=self._mixed_assignment_plan(),
            title="Open Desc",
        )
        a_high = self._seed_attempt(
            assignment_id,
            student_name="HighOpen",
            score_correct=1,
            score_total=1,
            question_ids_json=self._attempt_plan(),
            submitted_at="2026-05-01 09:00:00",
        )
        a_low = self._seed_attempt(
            assignment_id,
            student_name="LowOpen",
            score_correct=1,
            score_total=1,
            question_ids_json=self._attempt_plan(),
            submitted_at="2026-05-02 09:00:00",
        )
        a_zero = self._seed_attempt(
            assignment_id,
            student_name="NoOpenRows",
            score_correct=1,
            score_total=1,
            question_ids_json=self._attempt_plan(),
            submitted_at="2026-05-03 09:00:00",
        )
        # HighOpen: two awarded points
        self._seed_open_answer(a_high, subquestion_number=1, points_awarded=1.0)
        self._seed_open_answer(a_high, subquestion_number=2, points_awarded=1.0)
        # LowOpen: one awarded point
        self._seed_open_answer(a_low, subquestion_number=1, points_awarded=1.0)
        # NoOpenRows: no recorded text answers
        try:
            response = self.client.get(
                f"/teacher/assignment/{assignment_id}/results?sort=open_desc"
            )
            body = response.data.decode("utf-8")
            order = self._student_order(body, ["HighOpen", "LowOpen", "NoOpenRows"])
            self.assertEqual(order, ["HighOpen", "LowOpen", "NoOpenRows"])
        finally:
            self._delete_assignment(assignment_id)

    def test_sort_open_asc_uses_open_subtotal(self):
        self._login_admin()
        assignment_id = self._create_assignment(
            question_plan_json=self._mixed_assignment_plan(),
            title="Open Asc",
        )
        a_high = self._seed_attempt(
            assignment_id,
            student_name="HighOpen",
            question_ids_json=self._attempt_plan(),
            submitted_at="2026-05-01 09:00:00",
        )
        a_low = self._seed_attempt(
            assignment_id,
            student_name="LowOpen",
            question_ids_json=self._attempt_plan(),
            submitted_at="2026-05-02 09:00:00",
        )
        self._seed_open_answer(a_high, points_awarded=1.0)
        self._seed_open_answer(a_high, subquestion_number=2, points_awarded=1.0)
        self._seed_open_answer(a_low, points_awarded=1.0)
        try:
            response = self.client.get(
                f"/teacher/assignment/{assignment_id}/results?sort=open_asc"
            )
            body = response.data.decode("utf-8")
            order = self._student_order(body, ["HighOpen", "LowOpen"])
            self.assertEqual(order, ["LowOpen", "HighOpen"])
        finally:
            self._delete_assignment(assignment_id)

    def test_open_sort_on_mc_only_falls_back_to_default(self):
        self._login_admin()
        assignment_id = self._create_assignment(title="Open Sort MC Fallback")
        self._seed_alpha_set(assignment_id)
        try:
            response = self.client.get(
                f"/teacher/assignment/{assignment_id}/results?sort=open_desc"
            )
            self.assertEqual(response.status_code, 200)
            body = response.data.decode("utf-8")
            # Falls back to default SQL order.
            order = self._student_order(body, ["Alice", "Bob", "Charlie"])
            self.assertEqual(order, ["Bob", "Alice", "Charlie"])
            # Sort dropdown should show "default" selected, not the rejected value.
            self.assertIn('value="default" selected', body)
        finally:
            self._delete_assignment(assignment_id)

    # --- invalid sort ---------------------------------------------------

    def test_invalid_sort_falls_back_to_default(self):
        self._login_admin()
        assignment_id = self._create_assignment(title="Invalid Sort")
        self._seed_alpha_set(assignment_id)
        try:
            response = self.client.get(
                f"/teacher/assignment/{assignment_id}/results?sort=bogus"
            )
            self.assertEqual(response.status_code, 200)
            body = response.data.decode("utf-8")
            order = self._student_order(body, ["Alice", "Bob", "Charlie"])
            self.assertEqual(order, ["Bob", "Alice", "Charlie"])
            self.assertIn('value="default" selected', body)
        finally:
            self._delete_assignment(assignment_id)

    # --- combined with filters ------------------------------------------

    def test_filters_and_sort_compose(self):
        self._login_admin()
        assignment_id = self._create_assignment(title="Filter Sort Combo")
        self._seed_alpha_set(assignment_id)
        # Add a fourth student who matches the q filter
        self._seed_attempt(
            assignment_id,
            student_name="Alice2",
            score_correct=3,
            score_total=4,
            submitted_at="2026-05-04 09:00:00",
        )
        try:
            response = self.client.get(
                f"/teacher/assignment/{assignment_id}/results?q=ali&sort=mc_desc"
            )
            body = response.data.decode("utf-8")
            attempts_block = body[body.find('<th>Ученик</th>'):]
            self.assertNotIn("Bob", attempts_block)
            self.assertNotIn("Charlie", attempts_block)
            order = self._student_order(body, ["Alice", "Alice2"])
            # Alice2 = 75%, Alice = 50% → desc: Alice2, Alice
            self.assertEqual(order, ["Alice2", "Alice"])
        finally:
            self._delete_assignment(assignment_id)

    def test_clear_link_resets_filters_and_sort(self):
        self._login_admin()
        assignment_id = self._create_assignment(title="Clear Link")
        self._seed_alpha_set(assignment_id)
        try:
            response = self.client.get(
                f"/teacher/assignment/{assignment_id}/results?q=alice&sort=name_desc"
            )
            body = response.data.decode("utf-8")
            self.assertIn("Изчисти", body)
            # The clear link should be the bare results URL with no params.
            self.assertIn(
                f'href="/teacher/assignment/{assignment_id}/results"',
                body,
            )
        finally:
            self._delete_assignment(assignment_id)

    # --- safety / read-only --------------------------------------------

    def test_sort_does_not_modify_db(self):
        self._login_admin()
        assignment_id = self._create_assignment(
            question_plan_json=self._mixed_assignment_plan(),
            title="Sort No Writes",
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
                f"/teacher/assignment/{assignment_id}/results?sort=open_desc"
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

    def test_sort_does_not_expose_accepted_answers_json(self):
        self._login_admin()
        assignment_id = self._create_assignment(
            question_plan_json=self._mixed_assignment_plan(),
            title="Sort No Accepted",
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
                f"/teacher/assignment/{assignment_id}/results?sort=open_desc"
            )
            self.assertNotIn(b"accepted_answers_json", response.data)
            self.assertNotIn(b"accepted_answers", response.data)
        finally:
            self._delete_assignment(assignment_id)

    # --- auth ----------------------------------------------------------

    def test_unauthenticated_blocked(self):
        assignment_id = self._create_assignment(title="Auth Sort")
        self._seed_alpha_set(assignment_id)
        try:
            response = self.client.get(
                f"/teacher/assignment/{assignment_id}/results?sort=mc_desc"
            )
            self.assertEqual(response.status_code, 302)
            self.assertIn("/admin/login", response.headers.get("Location", ""))
        finally:
            self._delete_assignment(assignment_id)

    def test_tester_blocked(self):
        self._login_tester()
        assignment_id = self._create_assignment(title="Tester Sort")
        self._seed_alpha_set(assignment_id)
        try:
            response = self.client.get(
                f"/teacher/assignment/{assignment_id}/results?sort=mc_desc"
            )
            self.assertEqual(response.status_code, 302)
            self.assertIn("/admin/login", response.headers.get("Location", ""))
        finally:
            self._delete_assignment(assignment_id)


if __name__ == "__main__":
    unittest.main()
