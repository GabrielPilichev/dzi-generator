import contextlib
import io
import sqlite3
import unittest
from pathlib import Path

from src.validate_question_batch import readonly_uri, validate_batch


def make_conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE exams (
            id INTEGER PRIMARY KEY,
            subject TEXT,
            level TEXT,
            year INTEGER,
            session TEXT,
            variant INTEGER,
            format_version TEXT
        );
        CREATE TABLE exam_tasks (
            id INTEGER PRIMARY KEY,
            exam_id INTEGER,
            task_number INTEGER,
            task_kind TEXT,
            points INTEGER,
            has_assets INTEGER DEFAULT 0
        );
        CREATE TABLE questions (
            id INTEGER PRIMARY KEY,
            source_exam TEXT,
            source_number INTEGER
        );
        CREATE TABLE multiple_choice_options (
            id INTEGER PRIMARY KEY,
            question_id INTEGER,
            option_letter TEXT,
            option_text TEXT,
            is_correct INTEGER
        );
        CREATE TABLE fill_in_subquestions (
            id INTEGER PRIMARY KEY,
            question_id INTEGER,
            subquestion_number INTEGER,
            subquestion_text TEXT,
            correct_answer TEXT,
            answer_alternatives TEXT,
            points INTEGER
        );
        CREATE TABLE exam_task_questions (
            id INTEGER PRIMARY KEY,
            task_id INTEGER,
            question_id INTEGER,
            role TEXT
        );
        CREATE TABLE assets (
            id INTEGER PRIMARY KEY,
            local_path TEXT
        );
        CREATE TABLE asset_links (
            id INTEGER PRIMARY KEY,
            asset_id INTEGER,
            owner_type TEXT,
            owner_id INTEGER,
            role TEXT,
            display_order INTEGER,
            caption_bg TEXT,
            source_page INTEGER,
            source_bbox_json TEXT
        );
        CREATE TABLE curriculum_topics (
            id INTEGER PRIMARY KEY,
            topic_slug TEXT
        );
        CREATE TABLE curriculum_sections (
            id INTEGER PRIMARY KEY,
            section_slug TEXT,
            class INTEGER
        );
    """)
    conn.execute("""
        INSERT INTO exams (
            id, subject, level, year, session, variant, format_version
        )
        VALUES (1, 'informatika_it', 'DZI', 2025, 'may', 2, 'dzi_it_pp_2025_format')
    """)
    conn.execute("""
        INSERT INTO exam_tasks (id, exam_id, task_number, task_kind, points)
        VALUES
            (101, 1, 1, 'multiple_choice', 1),
            (116, 1, 16, 'short_answer', 3)
    """)
    conn.execute("INSERT INTO curriculum_topics (id, topic_slug) VALUES (1, 'sql-select')")
    conn.execute("INSERT INTO curriculum_sections (id, section_slug, class) VALUES (1, 'grade11-m1-databases-and-information-systems', 11)")
    conn.commit()
    return conn


def make_source_layout_conn(source_slug):
    parts = source_slug.split("_")
    session = "august" if parts[0] == "aug" else "may"
    year = int(parts[1])
    variant = int(parts[2][1:])
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE exams (
            id INTEGER PRIMARY KEY,
            subject TEXT,
            level TEXT,
            year INTEGER,
            session TEXT,
            variant INTEGER,
            format_version TEXT
        );
        CREATE TABLE exam_tasks (
            id INTEGER PRIMARY KEY,
            exam_id INTEGER,
            task_number INTEGER,
            task_kind TEXT,
            points INTEGER,
            has_assets INTEGER DEFAULT 0
        );
        CREATE TABLE questions (
            id INTEGER PRIMARY KEY,
            source_exam TEXT,
            source_number INTEGER
        );
        CREATE TABLE multiple_choice_options (
            id INTEGER PRIMARY KEY,
            question_id INTEGER,
            option_letter TEXT,
            option_text TEXT,
            is_correct INTEGER
        );
        CREATE TABLE fill_in_subquestions (
            id INTEGER PRIMARY KEY,
            question_id INTEGER,
            subquestion_number INTEGER,
            subquestion_text TEXT,
            correct_answer TEXT,
            answer_alternatives TEXT,
            points INTEGER
        );
        CREATE TABLE exam_task_questions (
            id INTEGER PRIMARY KEY,
            task_id INTEGER,
            question_id INTEGER,
            role TEXT
        );
        CREATE TABLE assets (
            id INTEGER PRIMARY KEY,
            local_path TEXT
        );
        CREATE TABLE asset_links (
            id INTEGER PRIMARY KEY,
            asset_id INTEGER,
            owner_type TEXT,
            owner_id INTEGER,
            role TEXT,
            display_order INTEGER,
            caption_bg TEXT,
            source_page INTEGER,
            source_bbox_json TEXT
        );
        CREATE TABLE curriculum_topics (
            id INTEGER PRIMARY KEY,
            topic_slug TEXT
        );
        CREATE TABLE curriculum_sections (
            id INTEGER PRIMARY KEY,
            section_slug TEXT,
            class INTEGER
        );
    """)
    conn.execute("""
        INSERT INTO exams (
            id, subject, level, year, session, variant, format_version
        )
        VALUES (1, 'informatika_it', 'DZI', ?, ?, ?, 'dzi_it_pp_2025_format')
    """, (year, session, variant))
    conn.execute("""
        INSERT INTO exam_tasks (id, exam_id, task_number, task_kind, points)
        VALUES
            (111, 1, 11, 'multiple_choice', 1),
            (116, 1, 16, 'short_answer', 3)
    """)
    conn.execute("INSERT INTO curriculum_topics (id, topic_slug) VALUES (1, 'sql-select')")
    conn.execute("INSERT INTO curriculum_sections (id, section_slug, class) VALUES (1, 'grade11-m1-databases-and-information-systems', 11)")
    conn.commit()
    return conn


def make_aug_2023_conn():
    return make_source_layout_conn("aug_2023_v2")


def valid_payload():
    return {
        "source_slug": "may_2025_v2",
        "tasks": [
            {
                "task_number": 1,
                "task_kind": "multiple_choice",
                "points": 1,
                "grade": 11,
                "topic_slug": "sql-select",
                "section_slug": "grade11-m1-databases-and-information-systems",
                "prompt": "Коя SQL команда извлича данни от таблица?",
                "options": [
                    {"letter": "А", "text": "SELECT", "is_correct": True},
                    {"letter": "Б", "text": "DELETE", "is_correct": False},
                    {"letter": "В", "text": "DROP", "is_correct": False},
                    {"letter": "Г", "text": "ALTER", "is_correct": False},
                ],
            },
            {
                "task_number": 16,
                "task_kind": "short_answer",
                "points": 3,
                "grade": 11,
                "topic_slug": "sql-select",
                "section_slug": "grade11-m1-databases-and-information-systems",
                "prompt": "Запишете ключовата дума за извличане на данни.",
                "answers": ["SELECT"],
                "answer_alternatives": ["select"],
            },
        ],
    }


def aug_2023_layout_payload():
    payload = source_layout_payload("aug_2023_v2")
    payload["tasks"][0]["prompt"] = "Запишете HTML кода за хипервръзка."
    payload["tasks"][0]["answers"] = ['<a href="contacts.html">Контакти</a>']
    return payload


def source_layout_payload(source_slug):
    return {
        "source_slug": source_slug,
        "tasks": [
            {
                "task_number": 11,
                "task_kind": "short_answer",
                "points": 3,
                "grade": 11,
                "topic_slug": "sql-select",
                "section_slug": "grade11-m1-databases-and-information-systems",
                "prompt": "Запишете кратък отговор.",
                "answers": ["отговор"],
            },
            {
                "task_number": 16,
                "task_kind": "multiple_choice",
                "points": 1,
                "grade": 11,
                "topic_slug": "sql-select",
                "section_slug": "grade11-m1-databases-and-information-systems",
                "prompt": "Кое от изброените е вярно?",
                "options": [
                    {"letter": "А", "text": "Първи отговор", "is_correct": False},
                    {"letter": "Б", "text": "Втори отговор", "is_correct": False},
                    {"letter": "В", "text": "Трети отговор", "is_correct": True},
                    {"letter": "Г", "text": "Четвърти отговор", "is_correct": False},
                ],
            },
        ],
    }


class ValidateQuestionBatchTest(unittest.TestCase):
    def test_valid_batch_reports_dry_run_plan_without_db_changes(self):
        conn = make_conn()
        try:
            before = conn.total_changes
            with contextlib.redirect_stdout(io.StringIO()) as stdout:
                summary = validate_batch(conn, valid_payload())

            self.assertIn("plan: task_number=1", stdout.getvalue())
            self.assertEqual(summary.tasks_read, 2)
            self.assertEqual(summary.questions_inserted, 2)
            self.assertEqual(summary.options_inserted, 4)
            self.assertEqual(summary.fill_in_subquestions_inserted, 1)
            self.assertEqual(conn.total_changes, before)
        finally:
            conn.close()

    def test_existing_question_is_reported_as_update(self):
        conn = make_conn()
        try:
            conn.execute(
                "INSERT INTO questions (id, source_exam, source_number) VALUES (10, 'may_2025_v2', 1)"
            )
            conn.commit()
            before = conn.total_changes

            with contextlib.redirect_stdout(io.StringIO()):
                summary = validate_batch(conn, valid_payload())

            self.assertEqual(summary.questions_inserted, 1)
            self.assertEqual(summary.questions_updated, 1)
            self.assertEqual(conn.total_changes, before)
        finally:
            conn.close()

    def test_invalid_mc_shape_is_rejected(self):
        conn = make_conn()
        try:
            payload = valid_payload()
            payload["tasks"][0]["options"][1]["is_correct"] = True

            with self.assertRaisesRegex(ValueError, "exactly 1 correct option"):
                validate_batch(conn, payload)
        finally:
            conn.close()

    def test_unknown_topic_is_rejected_by_default(self):
        conn = make_conn()
        try:
            payload = valid_payload()
            payload["tasks"][0]["topic_slug"] = "missing-topic"

            with self.assertRaisesRegex(ValueError, "Unknown topic_slug"):
                validate_batch(conn, payload)
        finally:
            conn.close()

    def test_short_answer_requires_accepted_answers(self):
        conn = make_conn()
        try:
            payload = valid_payload()
            del payload["tasks"][1]["answers"]
            del payload["tasks"][1]["answer_alternatives"]

            with self.assertRaisesRegex(ValueError, "require answers or subquestions"):
                validate_batch(conn, payload)
        finally:
            conn.close()

    def test_grade_must_match_section_class(self):
        conn = make_conn()
        try:
            payload = valid_payload()
            payload["tasks"][0]["grade"] = 12

            with self.assertRaisesRegex(ValueError, "does not match section class"):
                validate_batch(conn, payload)
        finally:
            conn.close()

    def test_readonly_uri_uses_sqlite_readonly_mode(self):
        self.assertEqual(
            readonly_uri(Path("data/questions.db")),
            "file:data/questions.db?mode=ro",
        )

    def test_aug_2023_v2_layout_override_allows_official_task_kinds(self):
        conn = make_aug_2023_conn()
        try:
            before = conn.total_changes
            with contextlib.redirect_stdout(io.StringIO()):
                summary = validate_batch(conn, aug_2023_layout_payload())

            self.assertEqual(summary.tasks_read, 2)
            self.assertEqual(summary.questions_inserted, 2)
            self.assertEqual(summary.options_inserted, 4)
            self.assertEqual(summary.fill_in_subquestions_inserted, 1)
            self.assertEqual(conn.total_changes, before)
        finally:
            conn.close()

    def test_may_2023_v2_layout_override_allows_official_task_kinds(self):
        conn = make_source_layout_conn("may_2023_v2")
        try:
            before = conn.total_changes
            with contextlib.redirect_stdout(io.StringIO()):
                summary = validate_batch(conn, source_layout_payload("may_2023_v2"))

            self.assertEqual(summary.tasks_read, 2)
            self.assertEqual(summary.questions_inserted, 2)
            self.assertEqual(summary.options_inserted, 4)
            self.assertEqual(summary.fill_in_subquestions_inserted, 1)
            self.assertEqual(conn.total_changes, before)
        finally:
            conn.close()

    def test_may_2022_v1_layout_override_allows_official_task_kinds(self):
        conn = make_source_layout_conn("may_2022_v1")
        try:
            before = conn.total_changes
            with contextlib.redirect_stdout(io.StringIO()):
                summary = validate_batch(conn, source_layout_payload("may_2022_v1"))

            self.assertEqual(summary.tasks_read, 2)
            self.assertEqual(summary.questions_inserted, 2)
            self.assertEqual(summary.options_inserted, 4)
            self.assertEqual(summary.fill_in_subquestions_inserted, 1)
            self.assertEqual(conn.total_changes, before)
        finally:
            conn.close()

    def test_may_2022_v1_layout_override_rejects_default_skeleton_kinds(self):
        conn = make_source_layout_conn("may_2022_v1")
        try:
            payload = {
                "source_slug": "may_2022_v1",
                "tasks": [
                    {
                        "task_number": 11,
                        "task_kind": "multiple_choice",
                        "points": 1,
                        "grade": 11,
                        "topic_slug": "sql-select",
                        "section_slug": "grade11-m1-databases-and-information-systems",
                        "prompt": "Кое от изброените е вярно?",
                        "options": [
                            {"letter": "А", "text": "Първи отговор", "is_correct": True},
                            {"letter": "Б", "text": "Втори отговор", "is_correct": False},
                            {"letter": "В", "text": "Трети отговор", "is_correct": False},
                            {"letter": "Г", "text": "Четвърти отговор", "is_correct": False},
                        ],
                    }
                ],
            }

            with self.assertRaisesRegex(ValueError, "does not match expected task_kind 'short_answer'"):
                validate_batch(conn, payload)
        finally:
            conn.close()

    def test_non_aug_2023_source_still_rejects_skeleton_task_kind_mismatch(self):
        conn = make_conn()
        try:
            payload = valid_payload()
            payload["tasks"] = [
                {
                    "task_number": 16,
                    "task_kind": "multiple_choice",
                    "points": 1,
                    "grade": 11,
                    "topic_slug": "sql-select",
                    "section_slug": "grade11-m1-databases-and-information-systems",
                    "prompt": "Кое от изброените е вярно?",
                    "options": [
                        {"letter": "А", "text": "Първи отговор", "is_correct": True},
                        {"letter": "Б", "text": "Втори отговор", "is_correct": False},
                        {"letter": "В", "text": "Трети отговор", "is_correct": False},
                        {"letter": "Г", "text": "Четвърти отговор", "is_correct": False},
                    ],
                }
            ]

            with self.assertRaisesRegex(ValueError, "does not match expected task_kind 'short_answer'"):
                validate_batch(conn, payload)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
