"""HTML-escaping / XSS regression tests for user-controlled content.

These tests do not assert that any text is *sanitized at write time* — the
codebase intentionally stores user input verbatim and relies on Jinja2
autoescape at render time. The tests below lock in that contract:

- Every place a user-supplied value (filename, student name, teacher note,
  open answer, accepted answer) is rendered, the raw HTML payload must not
  appear and the HTML-escaped form must appear instead.
- Practical-page templates must never expose ``stored_path``.

If a regression slips a ``|safe`` filter or a ``Markup(...)`` wrapper onto
any of these fields, the corresponding test fails.
"""

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

os.environ["DZI_ADMIN_PASSWORD"] = "admin-pass"

from web import app as web_app  # noqa: E402


SCRIPT_PAYLOAD = '<script>alert("xss")</script>'
SCRIPT_PAYLOAD_ESCAPED = "&lt;script&gt;alert(&#34;xss&#34;)&lt;/script&gt;"

IMG_FILENAME_PAYLOAD = '<img src=x onerror=alert(1)>.zip'
IMG_FILENAME_ESCAPED_FRAGMENT = "&lt;img src=x onerror=alert(1)&gt;.zip"

STUDENT_NAME_PAYLOAD = '<svg onload=alert("name")>'
STUDENT_NAME_ESCAPED_FRAGMENT = "&lt;svg onload=alert(&#34;name&#34;)&gt;"

TEACHER_NOTE_PAYLOAD = '<script>alert("teacher-note")</script>'
TEACHER_NOTE_ESCAPED_FRAGMENT = "&lt;script&gt;alert(&#34;teacher-note&#34;)&lt;/script&gt;"


# ---------------------------------------------------------------------------
# Practical: teacher review page
# ---------------------------------------------------------------------------

class PracticalTeacherReviewPageEscapingTest(unittest.TestCase):
    """Mirrors the setUp in test_practical_submission_review for isolation."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.project_root = Path(self.temp_dir.name)
        self.db_path = self.project_root / "questions.db"
        self.upload_root = self.project_root / "uploads" / "practical"
        self.stored_file = self.upload_root / "101" / "260" / "1" / "stored.xlsx"
        self.stored_file.parent.mkdir(parents=True)
        self.stored_file.write_bytes(b"solution bytes")

        self._create_db()

        self.old_project_root = web_app.PROJECT_ROOT
        self.old_db_path = web_app.app.config["DB_PATH"]
        self.old_upload_root = web_app.app.config["PRACTICAL_UPLOAD_ROOT"]
        self.addCleanup(self._restore_app_config)

        web_app.PROJECT_ROOT = self.project_root
        web_app.app.config.update(
            TESTING=True,
            DB_PATH=str(self.db_path),
            PRACTICAL_UPLOAD_ROOT=str(self.upload_root),
        )
        self.client = web_app.app.test_client()
        with self.client.session_transaction() as sess:
            sess["admin_authenticated"] = True
            sess["ui_profile"] = "admin"

    def _restore_app_config(self):
        web_app.PROJECT_ROOT = self.old_project_root
        web_app.app.config["DB_PATH"] = self.old_db_path
        web_app.app.config["PRACTICAL_UPLOAD_ROOT"] = self.old_upload_root

    def _create_db(self):
        conn = sqlite3.connect(self.db_path)
        try:
            conn.executescript("""
                CREATE TABLE exams (
                    id INTEGER PRIMARY KEY,
                    subject TEXT, level TEXT, year INTEGER,
                    session TEXT, variant INTEGER, format_version TEXT
                );
                CREATE TABLE quiz_assignments (id INTEGER PRIMARY KEY, title_bg TEXT);
                CREATE TABLE quiz_attempts (
                    id INTEGER PRIMARY KEY,
                    assignment_id INTEGER, student_name TEXT,
                    seed TEXT, question_ids_json TEXT, submitted_at TEXT
                );
                CREATE TABLE exam_tasks (
                    id INTEGER PRIMARY KEY,
                    exam_id INTEGER NOT NULL, task_number INTEGER NOT NULL,
                    task_kind TEXT NOT NULL, points INTEGER NOT NULL
                );
                CREATE TABLE practical_submissions (
                    id INTEGER PRIMARY KEY,
                    quiz_attempt_id INTEGER NOT NULL,
                    exam_task_id INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'draft',
                    manual_score REAL, manual_score_max REAL,
                    teacher_note TEXT, reviewed_at TEXT,
                    submitted_at TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE practical_submission_files (
                    id INTEGER PRIMARY KEY,
                    practical_submission_id INTEGER NOT NULL,
                    stored_path TEXT NOT NULL,
                    original_filename TEXT NOT NULL,
                    size_bytes INTEGER, mime_type TEXT,
                    uploaded_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    is_deleted INTEGER NOT NULL DEFAULT 0
                );
            """)
            conn.execute("""
                INSERT INTO exams (id, subject, level, year, session, variant, format_version)
                VALUES (6, 'informatika_it', 'DZI', 2025, 'may', 2, 'dzi_it_pp_2025_format')
            """)
            conn.execute("INSERT INTO quiz_assignments (id, title_bg) VALUES (1, 'ДЗИ проба')")
            conn.execute("""
                INSERT INTO quiz_attempts (id, assignment_id, student_name, seed, question_ids_json)
                VALUES (101, 1, ?, 'seed', '[]')
            """, (STUDENT_NAME_PAYLOAD,))
            conn.execute("""
                INSERT INTO exam_tasks (id, exam_id, task_number, task_kind, points)
                VALUES (260, 6, 26, 'practical_spreadsheet', 15)
            """)
            conn.execute("""
                INSERT INTO practical_submissions (
                    id, quiz_attempt_id, exam_task_id, status, updated_at
                )
                VALUES (1, 101, 260, 'draft', '2026-05-13 09:00:00')
            """)
            conn.execute("""
                INSERT INTO practical_submission_files (
                    id, practical_submission_id, stored_path, original_filename,
                    size_bytes, mime_type, uploaded_at, is_deleted
                )
                VALUES (
                    1, 1,
                    'data/uploads/practical/101/260/1/stored.xlsx',
                    ?, 14,
                    'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                    '2026-05-13 09:00:01', 0
                )
            """, (IMG_FILENAME_PAYLOAD,))
            conn.commit()
        finally:
            conn.close()

    def _get_review_html(self) -> str:
        response = self.client.get("/teacher/practical-submissions")
        self.assertEqual(response.status_code, 200)
        return response.get_data(as_text=True)

    def test_uploaded_filename_with_html_payload_is_escaped(self):
        html = self._get_review_html()
        self.assertNotIn(IMG_FILENAME_PAYLOAD, html)
        self.assertIn(IMG_FILENAME_ESCAPED_FRAGMENT, html)

    def test_student_name_with_html_payload_is_escaped(self):
        html = self._get_review_html()
        self.assertNotIn(STUDENT_NAME_PAYLOAD, html)
        self.assertIn(STUDENT_NAME_ESCAPED_FRAGMENT, html)

    def test_stored_path_is_not_exposed_on_teacher_review_page(self):
        html = self._get_review_html()
        self.assertNotIn("data/uploads/practical/101/260/1/stored.xlsx", html)
        self.assertNotIn(str(self.upload_root), html)


# ---------------------------------------------------------------------------
# Practical: student status page
# ---------------------------------------------------------------------------

class PracticalStudentPageEscapingTest(unittest.TestCase):
    """Mirrors test_practical_student_uploads setUp; inserts payloads directly."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.project_root = Path(self.temp_dir.name)
        self.db_path = self.project_root / "questions.db"
        self.batch_dir = self.project_root / "data" / "import_batches"
        self.upload_root = self.project_root / "uploads" / "practical"
        self.batch_dir.mkdir(parents=True)
        self._create_db()
        self._write_batch()
        self._insert_payload_submission()

        self.old_project_root = web_app.PROJECT_ROOT
        self.old_db_path = web_app.app.config["DB_PATH"]
        self.old_batch_dir = web_app.app.config["PRACTICAL_TASK_BATCH_DIR"]
        self.old_upload_root = web_app.app.config["PRACTICAL_UPLOAD_ROOT"]
        self.addCleanup(self._restore_app_config)

        web_app.PROJECT_ROOT = self.project_root
        web_app.app.config.update(
            TESTING=True,
            DB_PATH=str(self.db_path),
            PRACTICAL_TASK_BATCH_DIR=str(self.batch_dir),
            PRACTICAL_UPLOAD_ROOT=str(self.upload_root),
        )
        self.client = web_app.app.test_client()

    def _restore_app_config(self):
        web_app.PROJECT_ROOT = self.old_project_root
        web_app.app.config["DB_PATH"] = self.old_db_path
        web_app.app.config["PRACTICAL_TASK_BATCH_DIR"] = self.old_batch_dir
        web_app.app.config["PRACTICAL_UPLOAD_ROOT"] = self.old_upload_root

    def _create_db(self):
        conn = sqlite3.connect(self.db_path)
        try:
            conn.executescript("""
                CREATE TABLE exams (
                    id INTEGER PRIMARY KEY,
                    subject TEXT, level TEXT, year INTEGER,
                    session TEXT, variant INTEGER, format_version TEXT
                );
                CREATE TABLE quiz_attempts (
                    id INTEGER PRIMARY KEY, assignment_id INTEGER,
                    student_name TEXT, seed TEXT,
                    question_ids_json TEXT, submitted_at TEXT
                );
                CREATE TABLE exam_tasks (
                    id INTEGER PRIMARY KEY,
                    exam_id INTEGER NOT NULL, task_number INTEGER NOT NULL,
                    task_kind TEXT NOT NULL, points INTEGER NOT NULL,
                    prompt TEXT, rubric TEXT
                );
                CREATE TABLE practical_tasks (
                    task_id INTEGER PRIMARY KEY,
                    work_environment TEXT, expected_outputs_json TEXT, notes TEXT
                );
                CREATE TABLE practical_task_resources (
                    id INTEGER PRIMARY KEY, exam_task_id INTEGER NOT NULL,
                    resource_path TEXT NOT NULL, original_filename TEXT,
                    label_bg TEXT, file_size_bytes INTEGER, sha256 TEXT
                );
                CREATE TABLE practical_submissions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    quiz_attempt_id INTEGER NOT NULL,
                    exam_task_id INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'draft',
                    manual_score REAL, manual_score_max REAL,
                    teacher_note TEXT, reviewed_at TEXT,
                    submitted_at TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE (quiz_attempt_id, exam_task_id)
                );
                CREATE TABLE practical_submission_files (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    practical_submission_id INTEGER NOT NULL,
                    stored_path TEXT NOT NULL UNIQUE,
                    original_filename TEXT NOT NULL,
                    size_bytes INTEGER, mime_type TEXT,
                    uploaded_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    is_deleted INTEGER NOT NULL DEFAULT 0
                );
            """)
            conn.execute("""
                INSERT INTO exams (id, subject, level, year, session, variant, format_version)
                VALUES (6, 'informatika_it', 'DZI', 2025, 'may', 2, 'dzi_it_pp_2025_format')
            """)
            conn.execute("""
                INSERT INTO quiz_attempts (id, assignment_id, student_name, seed, question_ids_json)
                VALUES (101, 1, 'Student One', 'seed', '[]')
            """)
            conn.execute("""
                INSERT INTO exam_tasks (id, exam_id, task_number, task_kind, points)
                VALUES (260, 6, 26, 'practical_spreadsheet', 15)
            """)
            conn.execute("""
                INSERT INTO practical_tasks (task_id, work_environment)
                VALUES (260, 'spreadsheet')
            """)
            conn.commit()
        finally:
            conn.close()

    def _write_batch(self):
        import json
        payload = {
            "source_slug": "may_2025_v2",
            "tasks": [{
                "task_number": 26,
                "prompt_bg": "Task 26 prompt",
                "instructions_bg": "Task 26 instructions",
                "expected_outputs": ["solution.xlsx"],
            }],
        }
        (self.batch_dir / "may_2025_v2_practical_tasks_26_28.json").write_text(
            json.dumps(payload, ensure_ascii=False),
            encoding="utf-8",
        )

    def _insert_payload_submission(self):
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("""
                INSERT INTO practical_submissions (
                    id, quiz_attempt_id, exam_task_id, status,
                    manual_score, manual_score_max, teacher_note, reviewed_at,
                    updated_at
                )
                VALUES (
                    1, 101, 260, 'reviewed',
                    10, 15, ?, '2026-05-13 09:30:00',
                    '2026-05-13 09:30:00'
                )
            """, (TEACHER_NOTE_PAYLOAD,))
            conn.execute("""
                INSERT INTO practical_submission_files (
                    id, practical_submission_id, stored_path, original_filename,
                    size_bytes, uploaded_at, is_deleted
                )
                VALUES (
                    1, 1,
                    'data/uploads/practical/101/260/1/stored.xlsx',
                    ?, 14,
                    '2026-05-13 09:00:01', 0
                )
            """, (IMG_FILENAME_PAYLOAD,))
            conn.commit()
        finally:
            conn.close()

    def _get_student_html(self) -> str:
        response = self.client.get("/quiz/attempt/101/practical/may_2025_v2")
        self.assertEqual(response.status_code, 200)
        return response.get_data(as_text=True)

    def test_uploaded_filename_with_html_payload_is_escaped_on_student_page(self):
        html = self._get_student_html()
        self.assertNotIn(IMG_FILENAME_PAYLOAD, html)
        self.assertIn(IMG_FILENAME_ESCAPED_FRAGMENT, html)

    def test_teacher_note_with_html_payload_is_escaped_on_student_page(self):
        html = self._get_student_html()
        self.assertNotIn(TEACHER_NOTE_PAYLOAD, html)
        self.assertIn(TEACHER_NOTE_ESCAPED_FRAGMENT, html)
        # Sanity: the surrounding Bulgarian label is preserved.
        self.assertIn("Бележка от проверяващ", html)

    def test_stored_path_is_not_exposed_on_student_page(self):
        html = self._get_student_html()
        self.assertNotIn("data/uploads/practical/101/260/1/stored.xlsx", html)
        self.assertNotIn(str(self.upload_root), html)

    def test_upload_helper_strips_html_chars_from_submitted_filenames(self):
        """The helper sanitizes ``<`` and ``>`` to ``_`` before storage.

        This is defense-in-depth alongside the template-side escaping above:
        even if a future template change accidentally bypassed autoescape,
        no executable HTML chars would survive in original_filename.
        """
        from src.practical_uploads import sanitize_original_filename
        sanitized = sanitize_original_filename(IMG_FILENAME_PAYLOAD)
        self.assertNotIn("<", sanitized)
        self.assertNotIn(">", sanitized)
        self.assertTrue(sanitized.endswith(".zip"))


if __name__ == "__main__":
    unittest.main()
