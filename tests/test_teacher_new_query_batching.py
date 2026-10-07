"""/teacher/new loads its page with a bounded set of queries (roadmap PR F).

The per-section/per-question helpers (``AssessmentScope``,
``quiz_section_question_ids``, ``question_ownership``) stay the reference
implementation; these tests pin that the batched versions agree with them and
that the page's statement count no longer grows with the amount of content.
"""

import copy
import re
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from flask import template_rendered

from src.dzi_open_questions import (
    fetch_dzi_open_questions, is_open_question_generation_ready,
)
from src.subject_assessment import AssessmentScope
from src.subject_content import (
    IT, canonical_subject, question_ownership, question_ownerships, section_ownership,
    section_ownerships, topic_ownership, topic_ownerships,
)
from tests.auth_helpers import sign_in_teacher
from tests.subject_fixtures import (
    add_curriculum, add_membership, add_question, add_section, add_topic, four_options,
)
from tests.support import IsolatedAppTestCase, web_app, web_db, IT_ONLY
from web.services import practical as practical_service
from web.services import quiz as quiz_service

# Statements on a seed-sized page: ~12 set-based families plus one extra statement per
# 900 ids in each id-list family. The per-question implementation issued ~52,000.
SEED_IT_PAGE_STATEMENT_CEILING = 40
# Fixed families only (no per-section, per-topic or per-question statements).
SYNTHETIC_PAGE_STATEMENT_CEILING = 12

VISUAL_PROMPT = "Даденото изображение показва уеб страница. Кой е верният извод?"


@contextmanager
def traced_connections():
    """Count every statement run on connections opened through ``web.db``."""
    statements: list[str] = []
    real_connect = web_db.connect_db

    def connect(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        conn.set_trace_callback(statements.append)
        return conn

    with patch.object(web_db, "connect_db", connect):
        yield statements


def statement_families(statements):
    families: dict[str, int] = {}
    for statement in statements:
        key = re.sub(r"\s+", " ", re.sub(r"\b\d+\b", "N", statement)).strip()[:90]
        families[key] = families.get(key, 0) + 1
    return families


class TeacherNewPageMixin:
    def _tester_client(self):
        client = web_app.app.test_client()
        sign_in_teacher(client, web_app.app.config["DB_PATH"])
        return client

    def get_page(self, url="/teacher/new?subject=physics", client=None):
        client = client or self._tester_client()
        contexts = []

        def record(sender, template, context, **extra):
            contexts.append(dict(context))

        template_rendered.connect(record, web_app.app)
        try:
            with traced_connections() as statements:
                response = client.get(url)
        finally:
            template_rendered.disconnect(record, web_app.app)
        return response, (contexts[-1] if contexts else None), statements


class BatchedHelpersMatchReferenceTest(IsolatedAppTestCase, TeacherNewPageMixin):
    """Batched results equal the per-row reference over every row of the canonical content."""

    def setUp(self):
        self.conn = web_app.quiz_db()
        self.addCleanup(self.conn.close)

    def test_ownerships_equal_per_row_functions_for_every_seed_row(self):
        conn = self.conn
        section_ids = [row[0] for row in conn.execute("SELECT id FROM curriculum_sections")]
        topic_ids = [row[0] for row in conn.execute("SELECT id FROM curriculum_topics")]
        question_ids = [row[0] for row in conn.execute("SELECT id FROM questions")]
        self.assertGreater(len(question_ids), 500)

        sections = section_ownerships(conn)
        self.assertEqual(set(sections), set(section_ids))
        for section_id in section_ids:
            self.assertEqual(sections[section_id], section_ownership(conn, section_id))

        topics = topic_ownerships(conn, topic_ids + [987654321])
        for topic_id in topic_ids + [987654321]:
            self.assertEqual(topics[topic_id], topic_ownership(conn, topic_id), topic_id)

        questions = question_ownerships(conn, question_ids + [987654321])
        for question_id in question_ids + [987654321]:
            self.assertEqual(questions[question_id], question_ownership(conn, question_id), question_id)

    def test_ownerships_cover_conflicting_synthetic_rows(self):
        conn = self.conn
        it = add_curriculum(conn, "IT", 8, "qb-eq-it")
        physics = add_curriculum(conn, "physics", 8, "qb-eq-physics")
        it_section = add_section(conn, it, "qb-eq-it-s", grade=8)
        physics_section = add_section(conn, physics, "qb-eq-ph-s", grade=8)
        orphan_section = add_section(conn, None, "qb-eq-orphan", grade=8)
        crossing = add_topic(conn, it_section, "qb-eq-cross")
        add_membership(conn, crossing, physics_section)
        unclassified = add_topic(conn, orphan_section, "qb-eq-unclassified")
        not_primary = add_topic(conn, it_section, "qb-eq-np")
        conn.execute(
            "UPDATE topic_section_assignments SET is_primary = 0 WHERE topic_id = ?", (not_primary,)
        )
        unassigned = add_topic(conn, it_section, "qb-eq-unassigned")
        conn.execute("DELETE FROM topic_section_assignments WHERE topic_id = ?", (unassigned,))
        no_primary_section = int(conn.execute(
            "INSERT INTO curriculum_topics (section_id, topic_slug, title_bg) VALUES (NULL, 'qb-eq-nops', 'x')"
        ).lastrowid)
        clean = add_topic(conn, it_section, "qb-eq-clean")
        questions = [
            add_question(conn, topic, subject, number, source="qb-eq", options=four_options())
            for number, (topic, subject) in enumerate([
                (crossing, "IT"), (unclassified, "IT"), (not_primary, "IT"), (unassigned, "IT"),
                (no_primary_section, "IT"), (clean, "physics"), (clean, "IT"), (clean, None),
                (clean, "not-a-subject"), (None, "IT"),
            ], start=1)
        ]
        conn.commit()

        topics = [crossing, unclassified, not_primary, unassigned, no_primary_section, clean]
        batched_topics = topic_ownerships(conn, topics)
        for topic_id in topics:
            self.assertEqual(batched_topics[topic_id], topic_ownership(conn, topic_id), topic_id)
        batched_questions = question_ownerships(conn, questions)
        for question_id in questions:
            self.assertEqual(batched_questions[question_id], question_ownership(conn, question_id))
        statuses = {owner.detail or owner.status for owner in batched_questions.values()}
        self.assertGreaterEqual(len(statuses), 6)

    def test_scopes_equal_per_section_construction(self):
        conn = self.conn
        scopes = AssessmentScope.for_all_sections(conn)
        expected = {}
        for (section_id,) in conn.execute("SELECT id FROM curriculum_sections"):
            try:
                expected[section_id] = AssessmentScope(conn, section_id)
            except ValueError:
                continue
        self.assertEqual(set(scopes), set(expected))
        for section_id, scope in scopes.items():
            self.assertEqual(
                (scope.section_id, scope.subject, scope.section_type,
                 scope.allows_dzi_content, scope.allows_dzi_open_questions),
                (expected[section_id].section_id, expected[section_id].subject,
                 expected[section_id].section_type, expected[section_id].allows_dzi_content,
                 expected[section_id].allows_dzi_open_questions),
            )

    def test_eligible_ids_equal_reference_for_every_seed_section(self):
        conn = self.conn
        scopes = AssessmentScope.for_all_sections(conn)
        batched = quiz_service.quiz_section_question_ids_by_section(conn, scopes)
        self.assertEqual(set(batched), set(scopes))
        non_empty = 0
        for section_id, scope in scopes.items():
            reference = quiz_service.quiz_section_question_ids(conn, section_id, scope=scope)
            self.assertEqual(batched[section_id], reference, f"section {section_id}")
            self.assertEqual(len(batched[section_id]), len(reference))
            non_empty += bool(reference)
        self.assertGreater(non_empty, 5)

    def test_dzi_open_pool_equals_per_question_reference(self):
        conn = self.conn

        def reference(source_slug=None):
            rows = conn.execute("""
                SELECT q.*, e.subject AS exam_subject, e.year AS exam_year
                FROM questions q JOIN exams e ON e.id = q.exam_id
                WHERE q.question_type IN ('fill_in', 'short_answer')
                  AND q.level = 'DZI' AND e.level = 'DZI'
                ORDER BY q.source_exam, q.source_number, q.id
            """).fetchall()
            pool = []
            for row in rows:
                if (canonical_subject(row["subject"]) != IT
                        or canonical_subject(row["exam_subject"]) != IT
                        or row["year"] != row["exam_year"]):
                    continue
                subquestions = [dict(sub) for sub in conn.execute("""
                    SELECT id, subquestion_number, correct_answer, answer_alternatives
                    FROM fill_in_subquestions WHERE question_id = ?
                    ORDER BY subquestion_number
                """, (row["id"],))]
                if not is_open_question_generation_ready(row, subquestions):
                    continue
                if source_slug is not None and row["source_exam"] != source_slug:
                    continue
                pool.append({**dict(row), "subquestions": subquestions})
            return pool

        pool = fetch_dzi_open_questions(conn)
        self.assertGreater(len(pool), 10)
        self.assertEqual(pool, reference())
        slug = pool[0]["source_exam"]
        self.assertEqual(fetch_dzi_open_questions(conn, source_slug=slug), reference(slug))

    def test_seed_it_page_context_equals_reference_loop_and_stays_bounded(self):
        reference_sections = []
        conn = self.conn
        practical = None
        for row in conn.execute("""
            SELECT id, class, title_bg FROM curriculum_sections
            ORDER BY class, display_order, title_bg
        """).fetchall():
            section = dict(row)
            try:
                scope = AssessmentScope(conn, int(section["id"]))
            except ValueError:
                continue
            if scope.subject != IT:
                continue
            count = len(quiz_service.quiz_section_question_ids(conn, int(section["id"]), scope=scope))
            if count <= 0:
                continue
            section["question_count"] = count
            section["open"] = quiz_service.open_question_scope_summary(conn, int(section["id"]), scope)
            section["allows_dzi_open_questions"] = scope.allows_dzi_open_questions
            section["allows_dzi_content"] = scope.allows_dzi_content
            if scope.allows_dzi_content and practical is None:
                practical = practical_service.fetch_practical_assignment_options(conn, scope=scope)
            reference_sections.append(section)
        self.assertGreater(len(reference_sections), 5)
        self.assertTrue(any(section["allows_dzi_content"] for section in reference_sections))

        response, context, statements = self.get_page("/teacher/new?subject=it")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(context["sections"], reference_sections)
        self.assertEqual(context["practical_source_options"], practical or [])
        self.assertLessEqual(
            len(statements), SEED_IT_PAGE_STATEMENT_CEILING,
            statement_families(statements),
        )


class SyntheticPhysicsContent(IsolatedAppTestCase, TeacherNewPageMixin):
    """Synthetic physics sections/questions built through the real tables (no tests here)."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
    def setUp(self):
        self.conn = web_app.quiz_db()
        self.addCleanup(self.conn.close)

    def build_content(self, sections: int, questions_per_section: int, tag: str):
        """Return ``{section_id: expected eligible ids}`` in page order."""
        conn = self.conn
        curriculum = add_curriculum(conn, "physics", 8, f"qb-{tag}")
        expected = {}
        number = 0
        for index in range(sections):
            section = add_section(
                conn, curriculum, f"qb-{tag}-{index}", f"Раздел {tag} {index:03d}",
                grade=8, display_order=index,
            )
            topic = add_topic(conn, section, f"qb-{tag}-t{index}", grade=8)
            ids = []
            for _ in range(questions_per_section):
                number += 1
                ids.append(add_question(
                    conn, topic, "physics", number, source=f"qb-{tag}", options=four_options()))
            # Excluded by the eligibility rules, still present in the section.
            number += 1
            add_question(conn, topic, "physics", number, source=f"qb-{tag}",
                         options={"A": "а", "B": "б", "C": "в"})
            number += 1
            add_question(conn, topic, "physics", number, source=f"qb-{tag}", prompt=VISUAL_PROMPT,
                         options=four_options())
            number += 1
            add_question(conn, topic, "IT", number, source=f"qb-{tag}", options=four_options())
            # Included: visual phrase with an existing linked image.
            number += 1
            visual = add_question(conn, topic, "physics", number, source=f"qb-{tag}",
                                  prompt=VISUAL_PROMPT, options=four_options())
            image = Path(self.isolation_root) / f"visual-{visual}.png"
            image.write_bytes(b"png")
            asset = conn.execute(
                "INSERT INTO assets (asset_type, original_filename, local_path, mime_type, file_size) "
                "VALUES ('pdf_crop', ?, ?, 'image/png', 1)", (image.name, str(image))
            ).lastrowid
            conn.execute(
                "INSERT INTO asset_links (asset_id, owner_type, owner_id, role, display_order) "
                "VALUES (?, 'question', ?, 'pdf_crop', 0)", (asset, visual))
            ids.append(visual)
            expected[section] = ids
        # Sparse structure: a section with no questions, and one with only ineligible ones.
        empty = add_section(conn, curriculum, f"qb-{tag}-empty", "Празен раздел", grade=8,
                            display_order=sections)
        add_topic(conn, empty, f"qb-{tag}-empty-t", grade=8)
        weak = add_section(conn, curriculum, f"qb-{tag}-weak", "Слаб раздел", grade=8,
                           display_order=sections + 1)
        weak_topic = add_topic(conn, weak, f"qb-{tag}-weak-t", grade=8)
        number += 1
        add_question(conn, weak_topic, "physics", number, source=f"qb-{tag}", options={"A": "а"})
        conn.commit()
        return expected

    def page_sections(self):
        response, context, statements = self.get_page("/teacher/new?subject=physics")
        self.assertEqual(response.status_code, 200)
        return context, statements

    @staticmethod
    def tagged(context, tag):
        return [row for row in context["sections"] if f" {tag} " in row["title_bg"]]


class TeacherNewCardinalityAndOrderTest(SyntheticPhysicsContent):
    def test_cardinality_with_cross_section_membership(self):
        expected = self.build_content(4, 3, "cross")
        first, second, *_ = expected
        # One topic is also a member of another section; each section counts its own
        # questions once, not once per membership.
        topic = self.conn.execute(
            "SELECT topic_id FROM topic_section_assignments WHERE section_id = ?", (first,)
        ).fetchone()[0]
        add_membership(self.conn, topic, second)
        self.conn.commit()
        context, statements = self.page_sections()
        counts = {row["id"]: row["question_count"] for row in context["sections"]}
        self.assertEqual(counts[first], len(expected[first]))
        self.assertEqual(counts[second], len(expected[second]) + len(expected[first]))
        ids = [row["id"] for row in context["sections"]]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertLessEqual(len(statements), SYNTHETIC_PAGE_STATEMENT_CEILING)

        conn = self.conn
        scopes = AssessmentScope.for_all_sections(conn)
        batched = quiz_service.quiz_section_question_ids_by_section(conn, scopes)
        for section_id, scope in scopes.items():
            self.assertEqual(
                batched[section_id],
                quiz_service.quiz_section_question_ids(conn, section_id, scope=scope),
            )

    def test_rendered_section_options_keep_page_order(self):
        expected = self.build_content(5, 2, "html")
        response, context, _ = self.get_page("/teacher/new?subject=physics")
        html = response.get_data(as_text=True)
        positions = [html.index(f"Раздел html {index:03d}") for index in range(5)]
        self.assertEqual(positions, sorted(positions))
        self.assertEqual(len(self.tagged(context, "html")), len(expected))


class TeacherNewQueryCeilingTest(SyntheticPhysicsContent):
    """Own database so accumulated fixtures never reach the 900-id chunk size."""
    content_profile = IT_ONLY  # these tests pin Physics/Biology as subjects without content

    def test_page_statement_count_does_not_grow_with_content(self):
        small = self.build_content(3, 5, "small")
        small_context, small_statements = self.page_sections()
        self.assertEqual(len(self.tagged(small_context, "small")), 3)

        large = self.build_content(20, 25, "large")
        large_context, large_statements = self.page_sections()
        self.assertEqual(len(self.tagged(large_context, "large")), 20)

        families = statement_families(large_statements)
        self.assertLessEqual(len(large_statements), SYNTHETIC_PAGE_STATEMENT_CEILING, families)
        self.assertLessEqual(len(large_statements), len(small_statements), families)
        self.assertLessEqual(max(families.values()), 2, families)

        # Output: only eligible sections, in display order, with exactly the eligible counts.
        for tag, expected in (("small", small), ("large", large)):
            listed = [(row["id"], row["question_count"]) for row in self.tagged(large_context, tag)]
            self.assertEqual(listed, [(section, len(ids)) for section, ids in expected.items()])
        sparse_titles = {row["title_bg"] for row in large_context["sections"]}
        self.assertNotIn("Празен раздел", sparse_titles)
        self.assertNotIn("Слаб раздел", sparse_titles)


class TeacherNewCreateAssignmentTest(SyntheticPhysicsContent):
    """The POST availability check uses the same batched eligibility."""

    def test_create_assignment_count_is_eligible_count_with_bounded_statements(self):
        expected = self.build_content(6, 30, "post")
        section, ids = next(iter(expected.items()))
        client = self._tester_client()
        with traced_connections() as statements:
            response = client.post(
                "/teacher/new",
                data={"subject": "physics", "section_id": str(section), "question_count": "500",
                      "time_limit_minutes": ""},
                headers={"Origin": "http://localhost"},
            )
        self.assertEqual(response.status_code, 302)
        row = self.conn.execute(
            "SELECT question_count FROM quiz_assignments WHERE section_id = ?", (section,)
        ).fetchone()
        self.assertEqual(row["question_count"], len(ids))
        self.assertLessEqual(len(statements), 40, statement_families(statements))

        # Page filter is untouched: a mismatching subject is still rejected.
        mismatch = client.post(
            "/teacher/new",
            data={"subject": "it", "section_id": str(section), "question_count": "1",
                  "time_limit_minutes": ""},
            headers={"Origin": "http://localhost"},
        )
        self.assertEqual(mismatch.status_code, 400)


class TeacherNewSparseSubjectTest(IsolatedAppTestCase, TeacherNewPageMixin):
    """A subject with no sections or questions keeps its empty state."""
    content_profile = IT_ONLY  # these tests pin Physics/Biology as subjects without content

    def test_subject_without_content_and_unknown_subject(self):
        response, context, statements = self.get_page("/teacher/new?subject=physics")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(context["sections"], [])
        self.assertEqual(context["selected_subject"], "physics")
        self.assertIsNone(context["selected_section_open"])
        self.assertEqual(context["practical_source_options"], [])
        self.assertFalse(context["any_dzi_sections"])
        self.assertEqual(context["selected_open_available"], 0)
        self.assertLessEqual(len(statements), 6)
        response, _, _ = self.get_page("/teacher/new?subject=not-a-subject")
        self.assertEqual(response.status_code, 404)


class TeacherNewAccessAndPoliciesTest(IsolatedAppTestCase, TeacherNewPageMixin):
    def test_teacher_policy_is_unchanged(self):
        client = web_app.app.test_client()
        anonymous = client.get("/teacher/new")
        self.assertEqual(anonymous.status_code, 302)
        self.assertEqual(anonymous.headers["Location"], "/tester/login?next=/teacher/new")
        response, _, _ = self.get_page("/teacher/new?subject=it")
        self.assertEqual(response.status_code, 200)
        tester = self._tester_client()
        self.assertEqual(tester.get("/dzi").status_code, 302)

    def test_page_runs_read_only_statements_on_policy_connections(self):
        with traced_connections() as statements:
            self._tester_client().get("/teacher/new?subject=it")
        verbs = {statement.split(None, 1)[0].upper() for statement in statements}
        self.assertEqual(verbs, {"SELECT"})
