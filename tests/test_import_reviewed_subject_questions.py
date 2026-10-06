"""F4 reviewed subject import, exercised only against temporary DB copies."""

import copy
import hashlib
import io
import json
import os
import sqlite3
import tempfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from src.import_reviewed_subject_questions import import_batch, main, validate_batch
from src.subject_assessment import AssessmentScope
from src.subject_browsing import SubjectContentBrowser
from src.subject_content import question_ownership, topic_ownership
from tests.support import IsolatedAppTestCase, ROOT, IT_ONLY, build_test_database, web_app


def synthetic_batch(subject="physics", number=1):
    return {
        "format_version": 1, "subject": subject, "grade": 8,
        "source": f"reviewed:{subject}:synthetic-test-v1",
        "curriculum": {"subject": subject, "grade": 8,
                       "education_type": "synthetic-f4-test", "title": f"{subject} test curriculum"},
        "section": {"slug": f"{subject}-g8-synthetic-section",
                    "title": f"{subject} test section", "grade": 8},
        "topic": {"slug": f"{subject}-g8-synthetic-topic",
                  "title": f"{subject} test topic", "grade": 8},
        "questions": [{"number": number, "subject": subject, "grade": 8,
                       "topic_slug": f"{subject}-g8-synthetic-topic",
                       "type": "multiple_choice", "prompt": f"{subject} question {number}?",
                       "options": {"A": "One", "B": "Two", "C": "Three", "D": "Four"},
                       "correct": "B"}],
    }


class ReviewedSubjectImportTests(IsolatedAppTestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="learnpilot-f4-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.db_path = self.root / "questions.db"
        build_test_database(self.db_path, IT_ONLY)  # Physics/Biology absent: the importer adds them
        web_app.DB_PATH = self.db_path
        web_app.QUIZ_DB_PATH = self.db_path
        web_app.app.config["DB_PATH"] = str(self.db_path)
        os.environ["DZI_DB"] = str(self.db_path)

    def connect(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def test_valid_batch_dry_run_does_not_write_target(self):
        before = hashlib.sha256(self.db_path.read_bytes()).hexdigest()
        summary = import_batch(self.db_path, synthetic_batch())
        self.assertEqual(summary["questions"]["inserted"], 1)
        self.assertEqual(hashlib.sha256(self.db_path.read_bytes()).hexdigest(), before)
        with self.connect() as conn:
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM questions WHERE source_exam LIKE 'reviewed:physics:%'"
            ).fetchone()[0], 0)

    def test_failed_dry_run_does_not_write_target(self):
        batch = synthetic_batch()
        batch["questions"][0]["options"]["B"] = "[]"
        before = hashlib.sha256(self.db_path.read_bytes()).hexdigest()
        with self.assertRaisesRegex(ValueError, "eligible MC answer"):
            import_batch(self.db_path, batch)
        self.assertEqual(hashlib.sha256(self.db_path.read_bytes()).hexdigest(), before)

    def test_apply_populates_every_relationship_and_f1_f2_f3(self):
        batch = synthetic_batch()
        with self.connect() as conn:
            it_before = conn.execute("SELECT COUNT(*) FROM questions WHERE subject = 'IT'").fetchone()[0]
            exams_before = conn.execute("SELECT COUNT(*) FROM exams").fetchone()[0]
        summary = import_batch(self.db_path, batch, apply=True)
        self.assertEqual([summary[k]["inserted"] for k in summary], [1, 1, 1, 1])
        with self.connect() as conn:
            curriculum = conn.execute(
                "SELECT * FROM curricula WHERE subject = 'physics' AND education_type = 'synthetic-f4-test'"
            ).fetchone()
            self.assertEqual((curriculum["grade"], curriculum["title_bg"]),
                             (8, "physics test curriculum"))
            section = conn.execute(
                "SELECT * FROM curriculum_sections WHERE section_slug = ?",
                (batch["section"]["slug"],)).fetchone()
            topic = conn.execute(
                "SELECT * FROM curriculum_topics WHERE topic_slug = ?",
                (batch["topic"]["slug"],)).fetchone()
            question = conn.execute(
                "SELECT * FROM questions WHERE source_exam = ? AND source_number = 1",
                (batch["source"],)).fetchone()
            self.assertEqual((section["curriculum_id"], section["class"]),
                             (curriculum["id"], 8))
            self.assertEqual(topic["section_id"], section["id"])
            self.assertEqual(tuple(conn.execute(
                "SELECT section_id, is_primary FROM topic_section_assignments WHERE topic_id = ?",
                (topic["id"],)).fetchone()), (section["id"], 1))
            self.assertEqual(conn.execute(
                "SELECT class FROM topic_classes WHERE topic_id = ?", (topic["id"],)
            ).fetchone()[0], 8)
            self.assertEqual((question["subject"], question["topic_id"],
                              question["exam_id"], question["question_type"]),
                             ("physics", topic["id"], None, "multiple_choice"))
            options = conn.execute(
                "SELECT option_letter, option_text, is_correct FROM multiple_choice_options "
                "WHERE question_id = ? ORDER BY option_letter", (question["id"],)
            ).fetchall()
            self.assertEqual([tuple(row) for row in options],
                             [("A", "One", 0), ("B", "Two", 1),
                              ("C", "Three", 0), ("D", "Four", 0)])
            assignment = conn.execute(
                "SELECT topic_id, section_id, is_active FROM question_topic_assignments "
                "WHERE question_id = ?", (question["id"],)).fetchone()
            self.assertEqual(tuple(assignment), (topic["id"], section["id"], 1))
            self.assertEqual(topic_ownership(conn, topic["id"]).subject, "physics")
            self.assertEqual(question_ownership(conn, question["id"]).subject, "physics")
            browser = SubjectContentBrowser(conn, "physics")
            self.assertIn(section["id"], [r["section_id"] for r in browser.sections_for_grade(8)])
            self.assertIn(question["id"], [r["question_id"] for r in browser.questions_in_section(section["id"])])
            self.assertTrue(AssessmentScope(conn, section["id"]).accepts(question["id"]))
            self.assertIn(question["id"], web_app.quiz_section_question_ids(
                conn, section["id"], scope=AssessmentScope(conn, section["id"])))
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM exams").fetchone()[0], exams_before)
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM questions WHERE subject = 'IT'").fetchone()[0], it_before)
        client = web_app.app.test_client()
        grade_response = client.get("/subjects/physics/8")
        self.assertEqual(grade_response.status_code, 200)
        self.assertIn(b"physics test section", grade_response.data)
        group_response = client.get("/subjects/physics/8/physics-g8-synthetic-section")
        self.assertEqual(group_response.status_code, 200)
        self.assertIn(b"physics question 1?", group_response.data)

    def test_same_grade_it_and_physics_remain_isolated(self):
        import_batch(self.db_path, synthetic_batch("it"), apply=True)
        import_batch(self.db_path, synthetic_batch("physics"), apply=True)
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT id, section_slug FROM curriculum_sections WHERE section_slug LIKE '%-g8-synthetic-section'"
            ).fetchall()
            sections = {r["section_slug"].split("-")[0]: r["id"] for r in rows}
            it_ids = {r["question_id"] for r in SubjectContentBrowser(conn, "it").questions_in_section(sections["it"])}
            physics_ids = {r["question_id"] for r in SubjectContentBrowser(conn, "physics").questions_in_section(sections["physics"])}
            self.assertEqual(len(it_ids), 1)
            self.assertEqual(len(physics_ids), 1)
            self.assertFalse(it_ids & physics_ids)
            self.assertFalse(AssessmentScope(conn, sections["it"]).accepts(next(iter(physics_ids))))
            self.assertFalse(AssessmentScope(conn, sections["physics"]).accepts(next(iter(it_ids))))

    def test_exact_reimport_is_noop_and_conflicting_reimport_fails(self):
        batch = synthetic_batch()
        import_batch(self.db_path, batch, apply=True)
        before = hashlib.sha256(self.db_path.read_bytes()).hexdigest()
        summary = import_batch(self.db_path, batch, apply=True)
        self.assertEqual(summary["questions"]["reused"], 1)
        self.assertEqual(summary["questions"]["inserted"], 0)
        self.assertEqual(hashlib.sha256(self.db_path.read_bytes()).hexdigest(), before)
        conflict = copy.deepcopy(batch)
        conflict["questions"][0]["prompt"] = "Changed reviewed question?"
        with self.assertRaisesRegex(ValueError, "Conflicting question"):
            import_batch(self.db_path, conflict, apply=True)
        self.assertEqual(hashlib.sha256(self.db_path.read_bytes()).hexdigest(), before)
        incomplete = copy.deepcopy(batch)
        incomplete["questions"][0]["number"] = 2
        with self.assertRaisesRegex(ValueError, "different set"):
            import_batch(self.db_path, incomplete, apply=True)

    def test_unknown_subject_and_legacy_alias(self):
        batch = synthetic_batch()
        batch["subject"] = "unknown"
        with self.assertRaisesRegex(ValueError, "Unknown content subject"):
            validate_batch(batch)
        it_batch = synthetic_batch("it")
        for item in (it_batch, it_batch["curriculum"], it_batch["questions"][0]):
            item["subject"] = "informatika_it"
        self.assertEqual(validate_batch(it_batch), "it")
        import_batch(self.db_path, it_batch, apply=True)
        with self.connect() as conn:
            self.assertEqual(conn.execute(
                "SELECT subject FROM questions WHERE source_exam = ?", (it_batch["source"],)
            ).fetchone()[0], "it")

    def test_cross_subject_existing_section_and_topic_rejected(self):
        it_batch = synthetic_batch("it")
        import_batch(self.db_path, it_batch, apply=True)
        physics = synthetic_batch()
        with self.connect() as conn:
            it_curriculum = conn.execute(
                "SELECT id FROM curricula WHERE subject = 'it' AND education_type = 'synthetic-f4-test'"
            ).fetchone()[0]
            conn.execute(
                "INSERT INTO curriculum_sections "
                "(curriculum_id, section_slug, title_bg, class, section_type) "
                "VALUES (?, ?, ?, 8, 'content')",
                (it_curriculum, physics["section"]["slug"], physics["section"]["title"]),
            )
        with self.assertRaisesRegex(ValueError, "Conflicting section"):
            import_batch(self.db_path, physics, apply=True)
        with self.connect() as conn:
            conn.execute("DELETE FROM curriculum_sections WHERE section_slug = ?",
                         (physics["section"]["slug"],))
            it_section = conn.execute(
                "SELECT id FROM curriculum_sections WHERE section_slug = ?",
                (it_batch["section"]["slug"],)).fetchone()[0]
            conn.execute("INSERT INTO curriculum_topics (topic_slug, title_bg, section_id) "
                         "VALUES (?, ?, ?)",
                         (physics["topic"]["slug"], physics["topic"]["title"], it_section))
        with self.assertRaisesRegex(ValueError, "Conflicting topic"):
            import_batch(self.db_path, physics, apply=True)

    def test_existing_cross_subject_topic_membership_is_rejected(self):
        batch = synthetic_batch()
        import_batch(self.db_path, batch, apply=True)
        import_batch(self.db_path, synthetic_batch("it"), apply=True)
        with self.connect() as conn:
            topic_id = conn.execute("SELECT id FROM curriculum_topics WHERE topic_slug = ?",
                                    (batch["topic"]["slug"],)).fetchone()[0]
            it_section = conn.execute("SELECT id FROM curriculum_sections WHERE section_slug = ?",
                                      (synthetic_batch("it")["section"]["slug"],)).fetchone()[0]
            conn.execute("INSERT INTO topic_section_assignments "
                         "(topic_id, section_id, is_primary) VALUES (?, ?, 0)",
                         (topic_id, it_section))
        with self.assertRaisesRegex(ValueError, "conflicting ownership"):
            import_batch(self.db_path, batch, apply=True)

    def test_grade_mismatch_and_bad_mc_data_rejected(self):
        for location in ("curriculum", "section", "topic", "question"):
            batch = synthetic_batch()
            target = batch["questions"][0] if location == "question" else batch[location]
            target["grade"] = 9
            with self.subTest(location=location), self.assertRaisesRegex(ValueError, "grade"):
                validate_batch(batch)
        for mutation in (lambda q: q["options"].pop("D"),
                         lambda q: q["options"].update(A=" "),
                         lambda q: q["options"].update(A="—"),
                         lambda q: q.update(correct="E"),
                         lambda q: q.update(type="fill_in")):
            batch = synthetic_batch()
            mutation(batch["questions"][0])
            with self.assertRaises(ValueError):
                validate_batch(batch)

    def test_provenance_and_duplicate_numbers_rejected(self):
        batch = synthetic_batch()
        batch["source"] = ""
        with self.assertRaises(ValueError):
            validate_batch(batch)
        batch = synthetic_batch()
        batch["questions"].append(copy.deepcopy(batch["questions"][0]))
        with self.assertRaisesRegex(ValueError, "unique"):
            validate_batch(batch)

    def test_failure_halfway_rolls_back_all_rows(self):
        batch = synthetic_batch()
        second = copy.deepcopy(batch["questions"][0])
        second["number"] = 2
        second["prompt"] = "Second question?"
        batch["questions"].append(second)
        with self.connect() as conn:
            conn.execute("CREATE TRIGGER abort_second_f4_question BEFORE INSERT ON questions "
                         "WHEN NEW.source_exam = 'reviewed:physics:synthetic-test-v1' "
                         "AND NEW.source_number = 2 BEGIN SELECT RAISE(ABORT, 'test failure'); END")
        before = hashlib.sha256(self.db_path.read_bytes()).hexdigest()
        with self.assertRaisesRegex(sqlite3.IntegrityError, "test failure"):
            import_batch(self.db_path, batch, apply=True)
        self.assertEqual(hashlib.sha256(self.db_path.read_bytes()).hexdigest(), before)
        with self.connect() as conn:
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM curricula WHERE subject = 'physics'").fetchone()[0], 0)
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM questions WHERE source_exam = ?", (batch["source"],)
            ).fetchone()[0], 0)

    def test_cli_requires_explicit_paths_and_apply(self):
        batch = synthetic_batch()
        input_path = self.root / "batch.json"
        input_path.write_text(json.dumps(batch), encoding="utf-8")
        before = hashlib.sha256(self.db_path.read_bytes()).hexdigest()
        with redirect_stdout(io.StringIO()) as output:
            main(["--input", str(input_path), "--db", str(self.db_path)])
        self.assertIn('"mode": "dry-run"', output.getvalue())
        self.assertEqual(hashlib.sha256(self.db_path.read_bytes()).hexdigest(), before)
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            main(["--input", str(input_path)])


if __name__ == "__main__":
    import unittest
    unittest.main()
