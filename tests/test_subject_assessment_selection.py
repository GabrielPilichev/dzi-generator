"""New assessment selection is isolated by F1 ownership, not by class alone."""

import json

from src.generate_dzi_training_set import get_pool
from src.subject_assessment import AssessmentScope
from tests.subject_fixtures import (
    add_curriculum, add_membership, add_question, add_section, add_topic, four_options,
)
from tests.support import IsolatedAppTestCase, web_app
from tests.dzi_open_fixtures import mark_dzi_question


class SubjectAssessmentSelectionTest(IsolatedAppTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.app = web_app.app
        conn = web_app.quiz_db()
        try:
            it_curriculum = add_curriculum(conn, "IT", 8, "f3-it")
            physics_curriculum = add_curriculum(conn, "physics", 8, "f3-physics")
            physics12_curriculum = add_curriculum(conn, "physics", 12, "f3-physics-12")
            cls.it_section = add_section(conn, it_curriculum, "f3-it-8", "F3 IT раздел", grade=8)
            cls.physics_section = add_section(
                conn, physics_curriculum, "f3-physics-8", "F3 Физика раздел", grade=8
            )
            physics12_section = add_section(
                conn, physics12_curriculum, "f3-physics-12", "F3 Физика 12 раздел", grade=12
            )
            it_topic = add_topic(conn, cls.it_section, "f3-it-topic", grade=8)
            physics_topic = add_topic(conn, cls.physics_section, "f3-physics-topic", grade=8)
            physics12_topic = add_topic(
                conn, physics12_section, "f3-physics-12-topic", grade=12
            )
            cls.it_mc = cls._question(conn, it_topic, "IT", "multiple_choice", 1)
            cls.it_open = cls._question(conn, it_topic, "informatika_it", "fill_in", 2)
            mark_dzi_question(conn, cls.it_open)
            it12 = add_curriculum(conn, "IT", 12, "f3-it-dzi")
            cls.it_dzi_section = add_section(conn, it12, "f3-dzi", "F3 DZI", grade=12,
                                            section_type="dzi_training")
            add_membership(conn, it_topic, cls.it_dzi_section)
            cls.physics_mc = cls._question(conn, physics_topic, "physics", "multiple_choice", 3)
            cls.physics_open = cls._question(conn, physics_topic, "physics", "fill_in", 4)
            cls.wrong_subject = cls._question(conn, physics_topic, "IT", "multiple_choice", 5)
            cls.unknown = cls._question(conn, physics_topic, "not-a-subject", "multiple_choice", 6)
            cls.unclassified = cls._question(conn, physics_topic, None, "multiple_choice", 7)
            cross_topic = add_topic(conn, cls.physics_section, "f3-cross-topic", grade=8)
            add_membership(conn, cross_topic, cls.it_section)
            cls.cross_membership = cls._question(
                conn, cross_topic, "physics", "multiple_choice", 8
            )
            cls.physics12_mc = cls._question(
                conn, physics12_topic, "physics", "multiple_choice", 9
            )
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def _question(conn, topic_id, subject, kind, number):
        multiple_choice = kind == "multiple_choice"
        return add_question(
            conn, topic_id, subject, number, source="f3-synthetic", kind=kind,
            prompt=f"F3 question {number}", link_topic=True,
            options=four_options("F3 option") if multiple_choice else None,
            accepted_answer=None if multiple_choice else "text answer",
        )

    def setUp(self):
        self.client = self.app.test_client()
        with self.client.session_transaction() as session:
            session["tester_authenticated"] = True
            session["admin_authenticated"] = True

    def _create(self, section_id, subject, *, count=1, open_count=0, source=""):
        response = self.client.post("/teacher/new", data={
            "section_id": str(section_id), "subject": subject,
            "question_count": str(count), "open_count": str(open_count),
            "source_slug": source,
        })
        self.assertEqual(response.status_code, 302, response.get_data(as_text=True)[:500])
        return int(response.headers["Location"].rstrip("/").split("/")[-1])

    def _assignment(self, assignment_id):
        conn = web_app.quiz_db()
        try:
            return dict(conn.execute(
                "SELECT * FROM quiz_assignments WHERE id = ?", (assignment_id,)
            ).fetchone())
        finally:
            conn.close()

    def test_teacher_subject_selectors_do_not_mix_same_grade_sections(self):
        it_page = self.client.get("/teacher/new").get_data(as_text=True)
        physics_page = self.client.get("/teacher/new?subject=physics").get_data(as_text=True)
        self.assertIn("F3 IT раздел", it_page)
        self.assertNotIn("F3 Физика раздел", it_page)
        self.assertIn("F3 Физика раздел", physics_page)
        self.assertNotIn("F3 IT раздел", physics_page)
        self.assertIn('name="subject" value="physics"', physics_page)
        self.assertIn("Физика — 8. клас", physics_page)
        self.assertIn("Практически задачи 26–28", it_page)
        self.assertNotIn("Практически задачи 26–28", physics_page)

    def test_mc_and_open_candidate_pools_are_subject_owned(self):
        conn = web_app.quiz_db()
        try:
            it_scope = AssessmentScope(conn, self.it_section)
            physics_scope = AssessmentScope(conn, self.physics_section)
            it_mc = web_app.quiz_section_question_ids(conn, self.it_section, scope=it_scope)
            physics_mc = web_app.quiz_section_question_ids(
                conn, self.physics_section, scope=physics_scope
            )
            it_open = web_app.fetch_open_question_candidates(
                conn, section_id=self.it_section, scope=it_scope
            )
            physics_open = web_app.fetch_open_question_candidates(
                conn, section_id=self.physics_section, scope=physics_scope
            )
            physics_plan = web_app.build_mixed_quiz_plan(
                conn, closed_count=1, open_count=1,
                section_id=self.physics_section, scope=physics_scope,
            )
        finally:
            conn.close()
        self.assertIn(self.it_mc, it_mc)
        self.assertNotIn(self.physics_mc, it_mc)
        self.assertEqual(physics_mc, [self.physics_mc])
        self.assertEqual(it_open, [])
        self.assertEqual(physics_open, [])
        self.assertEqual(
            [item["question_id"] for item in physics_plan["closed_questions"]],
            [self.physics_mc],
        )
        self.assertEqual(
            [item["question_id"] for item in physics_plan["open_questions"]],
            [],
        )

    def test_plan_validation_rejects_wrong_unknown_unclassified_and_conflicting_ids(self):
        conn = web_app.quiz_db()
        try:
            scope = AssessmentScope(conn, self.physics_section)
            scope.validate_plan([self.physics_mc], [])
            with self.assertRaises(ValueError):
                scope.validate_plan([self.physics_mc, self.physics_open], [self.physics_open])
            for bad_id in (
                self.it_mc, self.it_open, self.wrong_subject,
                self.unknown, self.unclassified, self.cross_membership,
            ):
                with self.subTest(bad_id=bad_id), self.assertRaises(ValueError):
                    scope.validate_plan([self.physics_mc, bad_id], [])
            with self.assertRaises(ValueError):
                scope.validate_plan([self.physics_mc, self.physics_open],
                                    [self.physics_open], open_source_slug="wrong-source")
        finally:
            conn.close()

    def test_manual_plan_and_wrong_section_subject_posts_are_rejected(self):
        conn = web_app.quiz_db()
        try:
            before = conn.execute("SELECT COUNT(*) FROM quiz_assignments").fetchone()[0]
        finally:
            conn.close()
        for data in (
            {"section_id": str(self.physics_section), "subject": "physics",
             "question_count": "1", "question_ids": str(self.it_mc)},
            {"section_id": str(self.physics_section), "subject": "physics",
             "question_count": "1", "question_plan_json": json.dumps({"question_ids": [self.it_mc]})},
            {"section_id": str(self.it_section), "subject": "physics", "question_count": "1"},
        ):
            with self.subTest(data=data):
                self.assertEqual(self.client.post("/teacher/new", data=data).status_code, 400)
        conn = web_app.quiz_db()
        try:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM quiz_assignments").fetchone()[0], before)
        finally:
            conn.close()

    def test_unknown_subject_is_not_treated_as_it(self):
        self.assertEqual(self.client.get("/teacher/new?subject=not-a-subject").status_code, 404)
        self.assertEqual(self.client.post("/teacher/new", data={
            "subject": "not-a-subject", "section_id": str(self.it_section),
            "question_count": "1",
        }).status_code, 400)

    def test_physics_mc_assignment_full_lifecycle_stays_physics(self):
        assignment_id = self._create(self.physics_section, "physics")
        assignment = self._assignment(assignment_id)
        self.assertIsNone(assignment["question_plan_json"])
        start = self.client.post(f"/quiz/{assignment_id}", data={"student_name": "F3 Physics Student"})
        self.assertEqual(start.status_code, 302)
        attempt_id = int(start.headers["Location"].split("/")[-1])
        conn = web_app.quiz_db()
        try:
            snapshot = conn.execute(
                "SELECT question_ids_json FROM quiz_attempts WHERE id = ?", (attempt_id,)
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(json.loads(snapshot), [self.physics_mc])
        attempt = self.client.get(f"/quiz/attempt/{attempt_id}")
        self.assertIn(b"F3 question 3", attempt.data)
        self.assertNotIn(b"F3 question 1", attempt.data)
        submitted = self.client.post(
            f"/quiz/attempt/{attempt_id}", data={f"q_{self.physics_mc}": "A"}
        )
        self.assertEqual(submitted.status_code, 302)
        result = self.client.get(f"/quiz/attempt/{attempt_id}/result")
        self.assertEqual(result.status_code, 200)
        self.assertIn(b"F3 question 3", result.data)
        self.assertNotIn(b"F3 question 1", result.data)

    def test_existing_it_creation_and_physics_mixed_plan_are_separate(self):
        it_id = self._create(self.it_section, "it")
        self.assertIsNone(self._assignment(it_id)["question_plan_json"])
        response = self.client.post("/teacher/new", data={
            "section_id": self.physics_section, "subject": "physics",
            "question_count": "2", "open_count": "1", "source_slug": "f3-synthetic",
        })
        self.assertEqual(response.status_code, 400)

    def test_existing_plan_and_started_snapshot_are_not_reselected_by_edit_or_duplicate(self):
        assignment_id = self._create(
            self.it_dzi_section, "it", count=2, open_count=1, source="f3-synthetic"
        )
        plan_before = self._assignment(assignment_id)["question_plan_json"]
        started = self.client.post(
            f"/quiz/{assignment_id}", data={"student_name": "F3 Snapshot Student"}
        )
        attempt_id = int(started.headers["Location"].split("/")[-1])
        conn = web_app.quiz_db()
        try:
            snapshot_before = conn.execute(
                "SELECT question_ids_json FROM quiz_attempts WHERE id = ?", (attempt_id,)
            ).fetchone()[0]
        finally:
            conn.close()
        edited = self.client.post(f"/teacher/assignment/{assignment_id}/edit", data={
            "title_bg": "F3 edited", "time_limit_minutes": "",
            "question_plan_json": json.dumps({"question_ids": [self.it_mc]}),
        })
        self.assertEqual(edited.status_code, 302)
        duplicated = self.client.post(
            f"/teacher/assignment/{assignment_id}/duplicate",
            data={"question_plan_json": json.dumps({"question_ids": [self.it_mc]})},
        )
        self.assertEqual(duplicated.status_code, 302)
        duplicate_id = int(duplicated.headers["Location"].split("/")[-1])
        self.assertEqual(self._assignment(assignment_id)["question_plan_json"], plan_before)
        self.assertEqual(self._assignment(duplicate_id)["question_plan_json"], plan_before)
        conn = web_app.quiz_db()
        try:
            snapshot_after = conn.execute(
                "SELECT question_ids_json FROM quiz_attempts WHERE id = ?", (attempt_id,)
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(snapshot_after, snapshot_before)

    def test_dzi_generator_pool_does_not_include_physics_or_conflicting_content(self):
        conn = web_app.quiz_db()
        try:
            pools = get_pool(conn, [8, 12])
        finally:
            conn.close()
        pool = pools[8]
        self.assertIn(self.it_mc, pool)
        for bad_id in (
            self.physics_mc, self.physics_open, self.wrong_subject,
            self.unknown, self.unclassified, self.cross_membership,
        ):
            self.assertNotIn(bad_id, pool)
        self.assertNotIn(self.physics12_mc, pools[12])
