"""Subject ownership on the isolated seed plus synthetic Physics rows."""

from src import generate_questions, subject_content
from tests.support import IsolatedAppTestCase, web_app


class SubjectContentContractTest(IsolatedAppTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        conn = web_app.quiz_db()
        try:
            it_curriculum = conn.execute(
                "SELECT id FROM curricula WHERE grade = 8 ORDER BY id LIMIT 1"
            ).fetchone()
            cls.it_section_id = int(conn.execute(
                "SELECT id FROM curriculum_sections WHERE curriculum_id = ? ORDER BY id LIMIT 1",
                (it_curriculum[0],),
            ).fetchone()[0])
            cls.unclassified_section_id = int(conn.execute(
                "SELECT id FROM curriculum_sections WHERE curriculum_id IS NULL LIMIT 1"
            ).fetchone()[0])
            cls.legacy_question_id = int(conn.execute(
                "SELECT id FROM questions WHERE topic_id IS NULL LIMIT 1"
            ).fetchone()[0])

            cls.physics_curriculum_id = cls._curriculum(conn, 8, "physics")
            physics_grade9_curriculum_id = cls._curriculum(conn, 9, "physics")
            cls.unknown_curriculum_id = cls._curriculum(conn, 8, "unmapped-subject")
            cls.physics_section_id = cls._section(
                conn, cls.physics_curriculum_id, 8, "test-contract-physics-8"
            )
            cls.physics_grade9_section_id = cls._section(
                conn, physics_grade9_curriculum_id, 9, "test-contract-physics-9"
            )
            cls.unknown_section_id = cls._section(
                conn, cls.unknown_curriculum_id, 8, "test-contract-unknown-8"
            )

            cls.it_topic_id = cls._topic(conn, "test-contract-it", cls.it_section_id)
            cls.physics_topic_id = cls._topic(
                conn, "test-contract-physics", cls.physics_section_id
            )
            cls._membership(conn, cls.physics_topic_id, cls.physics_grade9_section_id)
            cls.cross_subject_topic_id = cls._topic(
                conn, "test-contract-cross", cls.physics_section_id
            )
            cls._membership(conn, cls.cross_subject_topic_id, cls.it_section_id)
            cls.unknown_topic_id = cls._topic(
                conn, "test-contract-unknown", cls.unknown_section_id
            )

            for topic_id in (cls.it_topic_id, cls.physics_topic_id):
                conn.execute(
                    "INSERT INTO topic_classes (topic_id, class) VALUES (?, 8)",
                    (topic_id,),
                )
            cls.physics_question_id = cls._question(
                conn, 1, "physics", cls.physics_topic_id
            )
            cls.legacy_it_question_id = cls._question(
                conn, 2, "informatika_it", cls.it_topic_id
            )
            cls.mismatched_question_id = cls._question(
                conn, 3, "IT", cls.physics_topic_id
            )
            cls.cross_subject_question_id = cls._question(
                conn, 4, "physics", cls.cross_subject_topic_id
            )
            cls.unlinked_question_id = cls._question(conn, 5, "physics", None)
            cls.unknown_question_id = cls._question(
                conn, 6, "unmapped-subject", cls.physics_topic_id
            )
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def _curriculum(conn, grade, subject):
        return int(conn.execute(
            """
            INSERT INTO curricula (grade, subject, education_type, title_bg)
            VALUES (?, ?, 'general', 'Contract test curriculum')
            """,
            (grade, subject),
        ).lastrowid)

    @staticmethod
    def _section(conn, curriculum_id, grade, slug):
        return int(conn.execute(
            """
            INSERT INTO curriculum_sections (
                curriculum_id, section_slug, title_bg, class, section_type
            ) VALUES (?, ?, 'Contract test section', ?, 'theory')
            """,
            (curriculum_id, slug, grade),
        ).lastrowid)

    @classmethod
    def _topic(cls, conn, slug, primary_section_id):
        topic_id = int(conn.execute(
            """
            INSERT INTO curriculum_topics (topic_slug, title_bg, section_id)
            VALUES (?, 'Contract test topic', ?)
            """,
            (slug, primary_section_id),
        ).lastrowid)
        cls._membership(conn, topic_id, primary_section_id, primary=True)
        return topic_id

    @staticmethod
    def _membership(conn, topic_id, section_id, *, primary=False):
        conn.execute(
            """
            INSERT INTO topic_section_assignments (topic_id, section_id, is_primary)
            VALUES (?, ?, ?)
            """,
            (topic_id, section_id, int(primary)),
        )

    @staticmethod
    def _question(conn, source_number, subject, topic_id):
        return int(conn.execute(
            """
            INSERT INTO questions (
                source_exam, source_number, subject, topic_id,
                question_type, points, prompt
            ) VALUES ('test-subject-contract', ?, ?, ?, 'multiple_choice', 1, 'Contract test?')
            """,
            (source_number, subject, topic_id),
        ).lastrowid)

    def test_verified_it_aliases_and_canonical_physics(self):
        for value in (
            "it", "IT", "informatika_it", "Информационни технологии",
            "information-technologies",
        ):
            with self.subTest(value=value):
                self.assertEqual(subject_content.canonical_subject(value), "it")
                self.assertTrue(subject_content.is_known_content_subject(value))
        self.assertEqual(subject_content.canonical_subject("physics"), "physics")

    def test_unknown_values_never_fall_back_to_it_or_future_informatics(self):
        for value in ("unmapped-subject", "informatics", "", None, 8):
            with self.subTest(value=value):
                self.assertIsNone(subject_content.canonical_subject(value))
                self.assertFalse(subject_content.is_known_content_subject(value))
        self.assertEqual(subject_content.canonical_subject("informatika_it"), "it")

    def test_curriculum_and_section_inherit_declared_subject(self):
        conn = web_app.quiz_db()
        try:
            curriculum = subject_content.curriculum_ownership(
                conn, self.physics_curriculum_id
            )
            section = subject_content.section_ownership(conn, self.physics_section_id)
            unknown = subject_content.section_ownership(conn, self.unknown_section_id)
        finally:
            conn.close()
        self.assertEqual((curriculum.status, curriculum.subject), ("owned", "physics"))
        self.assertEqual((section.status, section.subject), ("owned", "physics"))
        self.assertEqual((unknown.status, unknown.subject), ("unknown", None))

    def test_topic_uses_primary_section_and_accepts_other_physics_grade(self):
        conn = web_app.quiz_db()
        try:
            owner = subject_content.topic_ownership(conn, self.physics_topic_id)
            sections = [row[0] for row in conn.execute(
                "SELECT section_id FROM topic_section_assignments WHERE topic_id = ?",
                (self.physics_topic_id,),
            )]
        finally:
            conn.close()
        self.assertEqual(set(sections), {
            self.physics_section_id, self.physics_grade9_section_id,
        })
        self.assertEqual((owner.status, owner.subject), ("owned", "physics"))

    def test_cross_subject_membership_is_explicit_conflict(self):
        conn = web_app.quiz_db()
        try:
            topic = subject_content.topic_ownership(conn, self.cross_subject_topic_id)
            question = subject_content.question_ownership(
                conn, self.cross_subject_question_id
            )
        finally:
            conn.close()
        self.assertEqual((topic.status, topic.detail), (
            "conflict", "cross_subject_topic_membership",
        ))
        self.assertEqual(question.status, "conflict")
        self.assertIsNone(question.subject)

    def test_topic_without_primary_membership_is_explicit_conflict(self):
        conn = web_app.quiz_db()
        try:
            topic_id = int(conn.execute(
                """
                INSERT INTO curriculum_topics (topic_slug, title_bg, section_id)
                VALUES ('test-contract-missing-membership', 'Missing membership', ?)
                """,
                (self.physics_section_id,),
            ).lastrowid)
            owner = subject_content.topic_ownership(conn, topic_id)
        finally:
            conn.close()
        self.assertEqual((owner.status, owner.detail), (
            "conflict", "primary_section_not_assigned",
        ))

    def test_topic_primary_flag_must_agree_with_primary_section(self):
        conn = web_app.quiz_db()
        try:
            topic_id = self._topic(
                conn, "test-contract-other-primary", self.physics_section_id
            )
            self._membership(
                conn, topic_id, self.physics_grade9_section_id, primary=True
            )
            owner = subject_content.topic_ownership(conn, topic_id)
        finally:
            conn.close()
        self.assertEqual((owner.status, owner.detail), (
            "conflict", "primary_assignment_mismatch",
        ))

    def test_matching_and_mismatched_question_subjects(self):
        conn = web_app.quiz_db()
        try:
            physics = subject_content.question_ownership(conn, self.physics_question_id)
            legacy_it = subject_content.question_ownership(
                conn, self.legacy_it_question_id
            )
            mismatch = subject_content.question_ownership(
                conn, self.mismatched_question_id
            )
            unknown = subject_content.question_ownership(conn, self.unknown_question_id)
        finally:
            conn.close()
        self.assertEqual((physics.status, physics.subject), ("owned", "physics"))
        self.assertEqual((legacy_it.status, legacy_it.subject), ("owned", "it"))
        self.assertEqual((mismatch.status, mismatch.detail), (
            "conflict", "question_subject_mismatch",
        ))
        self.assertEqual((unknown.status, unknown.subject), ("unknown", None))

    def test_legacy_rows_remain_explicitly_unclassified(self):
        conn = web_app.quiz_db()
        try:
            section = subject_content.section_ownership(
                conn, self.unclassified_section_id
            )
            existing_question = subject_content.question_ownership(
                conn, self.legacy_question_id
            )
            new_question = subject_content.question_ownership(
                conn, self.unlinked_question_id
            )
        finally:
            conn.close()
        self.assertEqual((section.status, section.detail), (
            "unclassified", "section_without_curriculum",
        ))
        for question in (existing_question, new_question):
            self.assertEqual((question.status, question.detail), (
                "unclassified", "question_without_topic",
            ))
            self.assertIsNone(question.subject)

    def test_class_eight_does_not_decide_ownership(self):
        conn = web_app.quiz_db()
        try:
            rows = conn.execute(
                """
                SELECT topic_id FROM topic_classes
                WHERE class = 8 AND topic_id IN (?, ?)
                """,
                (self.it_topic_id, self.physics_topic_id),
            ).fetchall()
            it_owner = subject_content.topic_ownership(conn, self.it_topic_id)
            physics_owner = subject_content.topic_ownership(conn, self.physics_topic_id)
        finally:
            conn.close()
        self.assertEqual(len(rows), 2)
        self.assertEqual(it_owner.subject, "it")
        self.assertEqual(physics_owner.subject, "physics")

    def test_require_topic_subject_rejects_unknown_and_conflicted_topics(self):
        conn = web_app.quiz_db()
        try:
            with self.assertRaisesRegex(ValueError, "Unknown content subject"):
                subject_content.require_topic_subject(conn, self.it_topic_id, "unknown")
            with self.assertRaisesRegex(ValueError, "no consistent subject"):
                subject_content.require_topic_subject(
                    conn, self.cross_subject_topic_id, "physics"
                )
            self.assertEqual(
                subject_content.require_topic_subject(conn, self.it_topic_id, "it"),
                "it",
            )
        finally:
            conn.close()

    def test_it_only_generator_refuses_physics_before_generation_or_insert(self):
        conn = web_app.quiz_db()
        try:
            with self.assertRaisesRegex(ValueError, "belongs to physics"):
                generate_questions.fetch_topic_info(conn, "test-contract-physics")
            before = conn.execute("SELECT COUNT(*) FROM questions").fetchone()[0]
            with self.assertRaisesRegex(ValueError, "belongs to physics"):
                generate_questions.save_question(
                    conn,
                    {"id": self.physics_topic_id, "area_slug": None},
                    {"prompt": "Should not be saved", "options": []},
                    "test-subject-generator", "medium", "",
                )
            after = conn.execute("SELECT COUNT(*) FROM questions").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(before, after)

    def test_it_generator_still_fetches_and_saves_it_topic(self):
        conn = web_app.quiz_db()
        try:
            info = generate_questions.fetch_topic_info(conn, "test-contract-it")
            self.assertEqual(info["id"], self.it_topic_id)
            question_id = generate_questions.save_question(
                conn,
                info,
                {
                    "prompt": "Generated IT question?",
                    "options": [
                        {"letter": letter, "text": letter, "is_correct": letter == "А"}
                        for letter in ("А", "Б", "В", "Г")
                    ],
                },
                "test-subject-generator", "medium", "",
            )
            stored = conn.execute(
                "SELECT subject, level, topic_id FROM questions WHERE id = ?",
                (question_id,),
            ).fetchone()
            options = conn.execute(
                "SELECT COUNT(*) FROM multiple_choice_options WHERE question_id = ?",
                (question_id,),
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(tuple(stored), ("informatika_it", "DZI", self.it_topic_id))
        self.assertEqual(options, 4)
