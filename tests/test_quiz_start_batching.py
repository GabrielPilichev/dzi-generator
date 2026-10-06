"""Quiz attempt start resolves the candidate pool in batches (not one lookup set per candidate).

Runs on the full CANONICAL database. The batched selection must return exactly the ids, in exactly
the order, of the per-question reference implementation.
"""

import sqlite3
import unittest
from unittest.mock import patch

from src.subject_assessment import AssessmentScope
from tests.subject_fixtures import add_curriculum, add_membership, add_question, add_section, add_topic, four_options
from tests.support import CANONICAL, IT_ONLY, IsolatedAppTestCase, isolated_app, web_app
from web.services import quiz as quiz_service

ORIGIN = {"Origin": "http://localhost"}
START_CEILING = 150          # a 20-question DZI start measured 11,163 before; the batched path is ~40


def traced(fn):
    statements = []
    real = sqlite3.connect

    def connect(*args, **kwargs):
        conn = real(*args, **kwargs)
        conn.set_trace_callback(statements.append)
        return conn

    with patch.object(sqlite3, "connect", connect):
        response = fn()
    return response, statements


def admin():
    client = web_app.app.test_client()
    with client.session_transaction() as session:
        session.update(tester_authenticated=True, admin_authenticated=True, ui_profile="admin")
    return client


def create_assignment(subject, section_id, count):
    response = admin().post("/teacher/new", data={
        "subject": subject, "section_id": str(section_id), "question_count": str(count),
        "time_limit_minutes": ""}, headers=ORIGIN)
    assert response.status_code == 302, response.status_code
    return int(response.headers["Location"].rsplit("/", 1)[1])


def start(assignment_id, name="Batch Student"):
    return traced(lambda: web_app.app.test_client().post(
        f"/quiz/{assignment_id}", data={"student_name": name}, headers=ORIGIN))


class QuizStartOnCanonicalDataTest(IsolatedAppTestCase):
    content_profile = CANONICAL

    def conn(self):
        conn = sqlite3.connect(web_app.app.config["DB_PATH"])
        conn.row_factory = sqlite3.Row
        self.addCleanup(conn.close)
        return conn

    def section(self, conn, where):
        return conn.execute(
            "SELECT cs.id, cs.section_slug FROM curriculum_sections cs JOIN curricula c ON c.id = cs.curriculum_id "
            f"WHERE {where} ORDER BY cs.id LIMIT 1").fetchone()

    def sections(self):
        conn = self.conn()
        return {
            "it": ("it", self.section(conn, "cs.section_slug = 'grade8-computer-systems'")["id"]),
            "physics": ("physics", self.section(conn, "c.subject = 'physics' AND c.grade = 8")["id"]),
            "dzi": ("it", self.section(conn, "cs.section_slug = 'grade12-dzi-preparation'")["id"]),
        }

    def test_batched_candidates_equal_the_per_question_reference(self):
        conn = self.conn()
        sizes = {}
        for name, (_, section_id) in self.sections().items():
            scope = AssessmentScope(conn, section_id)
            batched = quiz_service.quiz_section_question_ids(conn, section_id, scope=scope)
            reference = quiz_service._quiz_section_question_ids_reference(conn, section_id, scope=scope)
            self.assertEqual(batched, reference, name)
            sizes[name] = len(batched)
        self.assertGreater(sizes["dzi"], 500)
        self.assertTrue(all(sizes.values()))

    def test_every_canonical_section_matches_the_reference(self):
        conn = self.conn()
        checked = 0
        for scope in AssessmentScope.for_all_sections(conn).values():
            self.assertEqual(
                quiz_service.quiz_section_question_ids(conn, scope.section_id, scope=scope),
                quiz_service._quiz_section_question_ids_reference(conn, scope.section_id, scope=scope),
                scope.section_id)
            checked += 1
        self.assertGreater(checked, 60)

    def test_empty_pool_and_scope_less_legacy_path(self):
        conn = self.conn()
        add = conn.execute("INSERT INTO curricula (grade, subject, education_type, title_bg) VALUES (8,'physics','empty-pool','x')")
        sid = conn.execute("INSERT INTO curriculum_sections (curriculum_id, section_slug, title_bg, class, section_type) "
                           "VALUES (?, 'physics-g8-empty-pool', 'x', 8, 'content')", (add.lastrowid,)).lastrowid
        conn.commit()
        scope = AssessmentScope(conn, sid)
        self.assertEqual(quiz_service.quiz_section_question_ids(conn, sid, scope=scope), [])
        any_section = self.sections()["it"][1]
        self.assertEqual(quiz_service.quiz_section_question_ids(conn, any_section),
                         quiz_service._quiz_section_question_ids_reference(conn, any_section))
        with self.assertRaises(ValueError):
            quiz_service.quiz_section_question_ids(conn, any_section, scope=scope)

    def test_dzi_start_does_not_scale_with_the_candidate_pool(self):
        subject, section_id = self.sections()["dzi"]
        assignment = create_assignment(subject, section_id, 20)
        response, statements = start(assignment)
        self.assertEqual(response.status_code, 302)
        self.assertLessEqual(len(statements), START_CEILING, len(statements))
        repeated = [s for s in statements if "FROM curriculum_sections WHERE id" in s or "FROM curricula WHERE id" in s]
        self.assertLess(len(repeated), 10)
        conn = self.conn()
        ids = conn.execute("SELECT question_ids_json FROM quiz_attempts").fetchone()[0]
        self.assertEqual(len(__import__("json").loads(ids)), 20)

    def test_it_and_physics_starts_are_bounded_too(self):
        for name in ("it", "physics"):
            subject, section_id = self.sections()[name]
            response, statements = start(create_assignment(subject, section_id, 10), f"Student {name}")
            self.assertEqual(response.status_code, 302, name)
            self.assertLessEqual(len(statements), START_CEILING, (name, len(statements)))


class QuizStartScalingTest(unittest.TestCase):
    def statements_for(self, extra):
        with isolated_app(IT_ONLY):
            conn = sqlite3.connect(web_app.app.config["DB_PATH"])
            conn.execute("PRAGMA foreign_keys = ON")
            curriculum = add_curriculum(conn, "physics", 8, "scale-test")
            section = add_section(conn, curriculum, "physics-g8-scale", "Scale", grade=8)
            topic = add_topic(conn, section, "physics-g8-scale-topic", "Scale", grade=8)
            for number in range(extra):
                add_question(conn, topic, "physics", number + 1, source="reviewed:physics:scale",
                             options=four_options())
            conn.commit()
            conn.close()
            response, statements = start(create_assignment("physics", section, 10))
            self.assertEqual(response.status_code, 302)
            return len(statements)

    def test_candidate_growth_adds_no_per_candidate_queries(self):
        small, large = self.statements_for(50), self.statements_for(400)
        self.assertLess(large - small, 350 * 0.5, (small, large))   # was ~9 statements per candidate


if __name__ == "__main__":
    unittest.main()
