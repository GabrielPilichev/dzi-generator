import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from src.map_confirmed_dzi_image_questions import (
    build_mapping_payload,
    map_one_question,
    write_mapping,
)


def make_conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE questions (
            id INTEGER PRIMARY KEY,
            source_exam TEXT NOT NULL,
            source_number INTEGER,
            question_type TEXT NOT NULL,
            prompt TEXT NOT NULL
        );
        CREATE TABLE exams (
            id INTEGER PRIMARY KEY,
            subject TEXT NOT NULL,
            level TEXT NOT NULL,
            year INTEGER NOT NULL,
            session TEXT,
            variant INTEGER DEFAULT 1,
            source_file TEXT NOT NULL
        );
        CREATE TABLE exam_tasks (
            id INTEGER PRIMARY KEY,
            exam_id INTEGER NOT NULL,
            task_number INTEGER NOT NULL,
            exam_part INTEGER NOT NULL,
            task_kind TEXT NOT NULL,
            points INTEGER NOT NULL
        );
        CREATE TABLE exam_task_questions (
            task_id INTEGER NOT NULL,
            question_id INTEGER NOT NULL,
            role TEXT NOT NULL DEFAULT 'primary',
            sub_number INTEGER
        );
        """
    )
    return conn


class MapConfirmedDziImageQuestionsTest(unittest.TestCase):
    def test_exact_mapping_uses_source_slug_and_official_number(self):
        conn = make_conn()
        try:
            conn.execute(
                """
                INSERT INTO questions (id, source_exam, source_number, question_type, prompt)
                VALUES (182, 'aug_2022_v2', 2, 'multiple_choice', 'На изображението е показана заявка.')
                """
            )

            entry = map_one_question(conn, "aug_2022_v2", 2, needs_image=True)

            self.assertEqual(entry["confidence"], "exact")
            self.assertEqual(entry["db_question_id"], 182)
            self.assertEqual(entry["official_question_number"], 2)
            self.assertEqual(entry["expected_asset_filename"], "aug_2022_v2_q2.png")
            self.assertIs(entry["needs_image"], True)
            self.assertIn("изображението", entry["question_text_snippet"])
        finally:
            conn.close()

    def test_no_image_note_is_preserved_as_non_mapping(self):
        conn = make_conn()
        try:
            conn.execute(
                """
                INSERT INTO questions (id, source_exam, source_number, question_type, prompt)
                VALUES (835, 'aug_2022_v2', 11, 'fill_in', 'No visual dependency.')
                """
            )
            payload = build_mapping_payload(
                conn,
                image_questions={"aug_2022_v2": (2,)},
                no_image_questions={"aug_2022_v2": (11,)},
            )

            note = payload["explicit_no_image_notes"][0]

            self.assertIs(note["needs_image"], False)
            self.assertIsNone(note["expected_asset_filename"])
            self.assertEqual(note["db_question_id"], 835)
            self.assertIn("explicit no-image note", note["notes"])
            self.assertEqual(payload["summary"]["explicit_no_image_notes"], 1)
        finally:
            conn.close()

    def test_missing_direct_match_can_fall_back_to_exam_task_link(self):
        conn = make_conn()
        try:
            conn.execute(
                """
                INSERT INTO exams (id, subject, level, year, session, variant, source_file)
                VALUES (1, 'informatika_it', 'DZI', 2025, 'may', 2, 'may_2025_v2_exam.pdf')
                """
            )
            conn.execute(
                """
                INSERT INTO exam_tasks (id, exam_id, task_number, exam_part, task_kind, points)
                VALUES (116, 1, 16, 1, 'short_answer', 3)
                """
            )
            conn.execute(
                """
                INSERT INTO questions (id, source_exam, source_number, question_type, prompt)
                VALUES (16, 'legacy_slug', NULL, 'fill_in', 'На диаграмата са представени суми.')
                """
            )
            conn.execute(
                """
                INSERT INTO exam_task_questions (task_id, question_id, role, sub_number)
                VALUES (116, 16, 'primary', NULL)
                """
            )

            entry = map_one_question(conn, "may_2025_v2", 16, needs_image=True)

            self.assertEqual(entry["confidence"], "likely")
            self.assertEqual(entry["db_question_id"], 16)
            self.assertEqual(entry["expected_asset_filename"], "may_2025_v2_q16.png")
            self.assertIn("exam_tasks", entry["notes"])
        finally:
            conn.close()

    def test_ambiguous_match_lists_candidates_without_selecting_one(self):
        conn = make_conn()
        try:
            conn.executemany(
                """
                INSERT INTO questions (id, source_exam, source_number, question_type, prompt)
                VALUES (?, 'may_2024_v1', 18, 'fill_in', ?)
                """,
                [(108, "First prompt"), (109, "Second prompt")],
            )

            entry = map_one_question(conn, "may_2024_v1", 18, needs_image=True)

            self.assertEqual(entry["confidence"], "ambiguous")
            self.assertIsNone(entry["db_question_id"])
            self.assertEqual(
                [candidate["db_question_id"] for candidate in entry["candidate_matches"]],
                [108, 109],
            )
        finally:
            conn.close()

    def test_json_output_is_deterministic_and_readable(self):
        payload = {
            "generated_by": "test",
            "review_status": "review mapping only",
            "summary": {"confirmed_image_questions": 0},
            "mappings": [],
            "explicit_no_image_notes": [],
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "mapping.json"
            write_mapping(path, payload)

            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), payload)
            self.assertTrue(path.read_text(encoding="utf-8").endswith("\n"))


if __name__ == "__main__":
    unittest.main()
