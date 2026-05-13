import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

os.environ["DZI_ADMIN_PASSWORD"] = "admin-pass"

from web import app as web_app  # noqa: E402


class PracticalSubmissionReviewTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.project_root = Path(self.temp_dir.name)
        self.db_path = self.project_root / "questions.db"
        self.upload_root = self.project_root / "uploads" / "practical"
        self.stored_file = self.upload_root / "101" / "260" / "1" / "stored.xlsx"
        self.stored_file.parent.mkdir(parents=True)
        self.stored_file.write_bytes(b"student solution")

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

    def _restore_app_config(self):
        web_app.PROJECT_ROOT = self.old_project_root
        web_app.app.config["DB_PATH"] = self.old_db_path
        web_app.app.config["PRACTICAL_UPLOAD_ROOT"] = self.old_upload_root

    def _admin_client(self):
        with self.client.session_transaction() as sess:
            sess["admin_authenticated"] = True
            sess["ui_profile"] = "admin"
        return self.client

    def _create_db(self):
        conn = sqlite3.connect(self.db_path)
        try:
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
                CREATE TABLE quiz_assignments (
                    id INTEGER PRIMARY KEY,
                    title_bg TEXT
                );
                CREATE TABLE quiz_attempts (
                    id INTEGER PRIMARY KEY,
                    assignment_id INTEGER,
                    student_name TEXT,
                    seed TEXT,
                    question_ids_json TEXT,
                    submitted_at TEXT
                );
                CREATE TABLE exam_tasks (
                    id INTEGER PRIMARY KEY,
                    exam_id INTEGER NOT NULL,
                    task_number INTEGER NOT NULL,
                    task_kind TEXT NOT NULL,
                    points INTEGER NOT NULL
                );
                CREATE TABLE practical_submissions (
                    id INTEGER PRIMARY KEY,
                    quiz_attempt_id INTEGER NOT NULL,
                    exam_task_id INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'draft',
                    submitted_at TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE practical_submission_files (
                    id INTEGER PRIMARY KEY,
                    practical_submission_id INTEGER NOT NULL,
                    stored_path TEXT NOT NULL,
                    original_filename TEXT NOT NULL,
                    size_bytes INTEGER,
                    mime_type TEXT,
                    uploaded_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    is_deleted INTEGER NOT NULL DEFAULT 0
                );
            """)
            conn.execute("""
                INSERT INTO exams (
                    id, subject, level, year, session, variant, format_version
                )
                VALUES (6, 'informatika_it', 'DZI', 2025, 'may', 2, 'dzi_it_pp_2025_format')
            """)
            conn.execute("INSERT INTO quiz_assignments (id, title_bg) VALUES (1, 'ДЗИ проба')")
            conn.execute("""
                INSERT INTO quiz_attempts (
                    id, assignment_id, student_name, seed, question_ids_json
                )
                VALUES (101, 1, 'Student One', 'seed', '[]')
            """)
            conn.execute("""
                INSERT INTO exam_tasks (id, exam_id, task_number, task_kind, points)
                VALUES (260, 6, 26, 'practical_spreadsheet', 15)
            """)
            conn.executemany(
                """
                INSERT INTO practical_submissions (
                    id, quiz_attempt_id, exam_task_id, status, updated_at
                )
                VALUES (?, 101, 260, 'draft', ?)
                """,
                [
                    (1, "2026-05-13 09:00:00"),
                    (2, "2026-05-13 09:01:00"),
                    (3, "2026-05-13 09:02:00"),
                    (4, "2026-05-13 09:03:00"),
                ],
            )
            conn.executemany(
                """
                INSERT INTO practical_submission_files (
                    id, practical_submission_id, stored_path, original_filename,
                    size_bytes, mime_type, uploaded_at, is_deleted
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        1,
                        1,
                        "data/uploads/practical/101/260/1/stored.xlsx",
                        "solution.xlsx",
                        len(b"student solution"),
                        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        "2026-05-13 09:00:01",
                        0,
                    ),
                    (
                        2,
                        2,
                        "data/uploads/practical/101/260/2/missing.xlsx",
                        "missing.xlsx",
                        10,
                        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        "2026-05-13 09:01:01",
                        0,
                    ),
                    (
                        3,
                        3,
                        "data/uploads/practical/101/260/3/deleted.xlsx",
                        "deleted.xlsx",
                        10,
                        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        "2026-05-13 09:02:01",
                        1,
                    ),
                    (
                        4,
                        4,
                        "data/uploads/practical/101/260/4/../../evil.xlsx",
                        "evil.xlsx",
                        10,
                        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        "2026-05-13 09:03:01",
                        0,
                    ),
                ],
            )
            conn.commit()
        finally:
            conn.close()

    def test_admin_review_page_lists_practical_submissions(self):
        response = self._admin_client().get("/teacher/practical-submissions")

        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("Практически файлове", html)
        self.assertIn("Student One", html)
        self.assertIn("Задача 26", html)
        self.assertIn("may_2025_v2", html)

    def test_review_page_lists_uploaded_original_filenames_and_sizes(self):
        response = self._admin_client().get("/teacher/practical-submissions")

        html = response.get_data(as_text=True)
        self.assertIn("solution.xlsx", html)
        self.assertIn(f"{len(b'student solution')} байта", html)
        self.assertIn("2026-05-13 09:00:01", html)

    def test_review_page_does_not_expose_stored_path_or_raw_filesystem_path(self):
        response = self._admin_client().get("/teacher/practical-submissions")

        html = response.get_data(as_text=True)
        self.assertNotIn("data/uploads/practical", html)
        self.assertNotIn(str(self.upload_root), html)

    def test_admin_can_download_uploaded_file(self):
        response = self._admin_client().get("/teacher/practical/submission-file/1/download")

        try:
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.data, b"student solution")
            self.assertIn("attachment", response.headers["Content-Disposition"])
            self.assertIn("solution.xlsx", response.headers["Content-Disposition"])
        finally:
            response.close()

    def test_nonexistent_file_id_returns_404(self):
        response = self._admin_client().get("/teacher/practical/submission-file/999/download")

        self.assertEqual(response.status_code, 404)

    def test_missing_stored_file_returns_404_not_500(self):
        response = self._admin_client().get("/teacher/practical/submission-file/2/download")

        self.assertEqual(response.status_code, 404)

    def test_deleted_file_returns_404_and_review_shows_deleted_state(self):
        client = self._admin_client()
        download = client.get("/teacher/practical/submission-file/3/download")
        page = client.get("/teacher/practical-submissions")

        self.assertEqual(download.status_code, 404)
        self.assertIn("изтрит", page.get_data(as_text=True))

    def test_path_traversal_stored_path_is_rejected_safely(self):
        response = self._admin_client().get("/teacher/practical/submission-file/4/download")

        self.assertEqual(response.status_code, 404)

    def test_tester_without_admin_cannot_access_review_or_download(self):
        with self.client.session_transaction() as sess:
            sess["tester_authenticated"] = True
            sess["ui_profile"] = "tester"

        page = self.client.get("/teacher/practical-submissions")
        download = self.client.get("/teacher/practical/submission-file/1/download")

        self.assertEqual(page.status_code, 302)
        self.assertIn("/admin/login", page.headers["Location"])
        self.assertEqual(download.status_code, 302)
        self.assertIn("/admin/login", download.headers["Location"])


if __name__ == "__main__":
    unittest.main()
