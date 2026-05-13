import io
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

os.environ["DZI_ADMIN_PASSWORD"] = "admin-pass"

from web import app as web_app  # noqa: E402


class PracticalStudentUploadsTest(unittest.TestCase):
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

        self.old_project_root = web_app.PROJECT_ROOT
        self.old_db_path = web_app.app.config["DB_PATH"]
        self.old_batch_dir = web_app.app.config["PRACTICAL_TASK_BATCH_DIR"]
        self.old_upload_root = web_app.app.config["PRACTICAL_UPLOAD_ROOT"]
        self.old_max_upload_bytes = web_app.app.config["PRACTICAL_MAX_UPLOAD_BYTES"]
        self.addCleanup(self._restore_app_config)

        web_app.PROJECT_ROOT = self.project_root
        web_app.app.config.update(
            TESTING=True,
            DB_PATH=str(self.db_path),
            PRACTICAL_TASK_BATCH_DIR=str(self.batch_dir),
            PRACTICAL_UPLOAD_ROOT=str(self.upload_root),
            PRACTICAL_MAX_UPLOAD_BYTES=1024,
        )
        self.client = web_app.app.test_client()

    def _restore_app_config(self):
        web_app.PROJECT_ROOT = self.old_project_root
        web_app.app.config["DB_PATH"] = self.old_db_path
        web_app.app.config["PRACTICAL_TASK_BATCH_DIR"] = self.old_batch_dir
        web_app.app.config["PRACTICAL_UPLOAD_ROOT"] = self.old_upload_root
        web_app.app.config["PRACTICAL_MAX_UPLOAD_BYTES"] = self.old_max_upload_bytes

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
                    points INTEGER NOT NULL,
                    prompt TEXT,
                    rubric TEXT
                );
                CREATE TABLE practical_tasks (
                    task_id INTEGER PRIMARY KEY,
                    work_environment TEXT,
                    expected_outputs_json TEXT,
                    notes TEXT
                );
                CREATE TABLE practical_task_resources (
                    id INTEGER PRIMARY KEY,
                    exam_task_id INTEGER NOT NULL,
                    resource_path TEXT NOT NULL,
                    original_filename TEXT,
                    label_bg TEXT,
                    file_size_bytes INTEGER,
                    sha256 TEXT
                );
                CREATE TABLE practical_submissions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    quiz_attempt_id INTEGER NOT NULL,
                    exam_task_id INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'draft',
                    manual_score REAL,
                    manual_score_max REAL,
                    teacher_note TEXT,
                    reviewed_at TEXT,
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
            conn.execute("""
                INSERT INTO quiz_attempts (
                    id, assignment_id, student_name, seed, question_ids_json
                )
                VALUES (101, 1, 'Student One', 'seed', '[]')
            """)
            conn.executemany(
                """
                INSERT INTO exam_tasks (id, exam_id, task_number, task_kind, points)
                VALUES (?, 6, ?, ?, ?)
                """,
                [
                    (260, 26, "practical_spreadsheet", 15),
                    (270, 27, "practical_graphics", 20),
                    (150, 15, "multiple_choice", 1),
                ],
            )
            conn.executemany(
                "INSERT INTO practical_tasks (task_id, work_environment) VALUES (?, ?)",
                [(260, "spreadsheet"), (270, "graphics")],
            )
            conn.commit()
        finally:
            conn.close()

    def _write_batch(self):
        payload = {
            "source_slug": "may_2025_v2",
            "tasks": [
                {
                    "task_number": 26,
                    "prompt_bg": "Task 26 prompt",
                    "instructions_bg": "Task 26 instructions",
                    "expected_outputs": ["solution.xlsx"],
                },
                {
                    "task_number": 27,
                    "prompt_bg": "Task 27 prompt",
                    "instructions_bg": "Task 27 instructions",
                    "expected_outputs": ["graphics.zip"],
                },
            ],
        }
        (self.batch_dir / "may_2025_v2_practical_tasks_26_28.json").write_text(
            json.dumps(payload, ensure_ascii=False),
            encoding="utf-8",
        )

    def _upload(self, exam_task_id=260, filename="solution.xlsx", content=b"xlsx bytes"):
        return self.client.post(
            f"/quiz/attempt/101/practical/may_2025_v2/task/{exam_task_id}/upload",
            data={"files": (io.BytesIO(content), filename)},
            content_type="multipart/form-data",
            follow_redirects=False,
        )

    def _rows(self, table):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            return [dict(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY id").fetchall()]
        finally:
            conn.close()

    def test_upload_form_renders_in_attempt_context(self):
        response = self.client.get("/quiz/attempt/101/practical/may_2025_v2")

        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("Student One", html)
        self.assertIn('enctype="multipart/form-data"', html)
        self.assertIn("/quiz/attempt/101/practical/may_2025_v2/task/260/upload", html)

    def test_page_shows_not_submitted_status_before_upload(self):
        response = self.client.get("/quiz/attempt/101/practical/may_2025_v2")

        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("Статус: няма качени файлове.", html)

    def test_valid_upload_creates_submission_and_file_rows(self):
        response = self._upload()

        self.assertEqual(response.status_code, 302)
        submissions = self._rows("practical_submissions")
        files = self._rows("practical_submission_files")
        self.assertEqual(len(submissions), 1)
        self.assertEqual(submissions[0]["quiz_attempt_id"], 101)
        self.assertEqual(submissions[0]["exam_task_id"], 260)
        self.assertEqual(submissions[0]["status"], "draft")
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0]["practical_submission_id"], submissions[0]["id"])
        self.assertEqual(files[0]["original_filename"], "solution.xlsx")
        self.assertEqual(files[0]["size_bytes"], len(b"xlsx bytes"))

    def test_valid_upload_stores_file_under_temp_upload_root(self):
        self._upload()
        files = self._rows("practical_submission_files")
        stored_path = files[0]["stored_path"]

        stored_files = list(self.upload_root.rglob("*"))
        stored_regular_files = [path for path in stored_files if path.is_file()]
        self.assertEqual(len(stored_regular_files), 1)
        self.assertTrue(stored_regular_files[0].resolve().is_relative_to(self.upload_root.resolve()))
        self.assertEqual(stored_regular_files[0].read_bytes(), b"xlsx bytes")
        self.assertTrue(stored_path.startswith("data/uploads/practical/101/260/"))

    def test_original_filename_is_shown_but_stored_path_is_not(self):
        self._upload()

        response = self.client.get("/quiz/attempt/101/practical/may_2025_v2")
        html = response.get_data(as_text=True)
        stored_path = self._rows("practical_submission_files")[0]["stored_path"]
        self.assertIn("solution.xlsx", html)
        self.assertNotIn(stored_path, html)
        self.assertNotIn(str(self.upload_root), html)

    def test_page_shows_pending_review_status_after_upload(self):
        self._upload()

        response = self.client.get("/quiz/attempt/101/practical/may_2025_v2")
        html = response.get_data(as_text=True)
        self.assertIn("Статус: качено, очаква преглед.", html)

    def test_page_shows_saved_teacher_score_and_note(self):
        self._upload()
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("""
                UPDATE practical_submissions
                SET
                    status = 'reviewed',
                    manual_score = 12.5,
                    manual_score_max = 15,
                    teacher_note = 'Добре структуриран файл.',
                    reviewed_at = '2026-05-13 10:00:00'
                WHERE quiz_attempt_id = 101
                  AND exam_task_id = 260
            """)
            conn.commit()
        finally:
            conn.close()

        response = self.client.get("/quiz/attempt/101/practical/may_2025_v2")
        html = response.get_data(as_text=True)
        self.assertIn("прегледано", html)
        self.assertIn("12.5/15", html)
        self.assertIn("Добре структуриран файл.", html)
        self.assertNotIn('name="manual_score"', html)
        self.assertNotIn('name="teacher_note"', html)

    def test_page_does_not_show_another_students_submission(self):
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("""
                INSERT INTO quiz_attempts (
                    id, assignment_id, student_name, seed, question_ids_json
                )
                VALUES (202, 1, 'Student Two', 'seed-2', '[]')
            """)
            conn.execute("""
                INSERT INTO practical_submissions (
                    id, quiz_attempt_id, exam_task_id, status
                )
                VALUES (2020, 202, 260, 'draft')
            """)
            conn.execute("""
                INSERT INTO practical_submission_files (
                    practical_submission_id,
                    stored_path,
                    original_filename,
                    size_bytes
                )
                VALUES (
                    2020,
                    'data/uploads/practical/202/260/private.xlsx',
                    'private.xlsx',
                    9
                )
            """)
            conn.commit()
        finally:
            conn.close()

        response = self.client.get("/quiz/attempt/101/practical/may_2025_v2")
        html = response.get_data(as_text=True)
        self.assertNotIn("Student Two", html)
        self.assertNotIn("private.xlsx", html)
        self.assertNotIn("data/uploads/practical/202/260/private.xlsx", html)

    def test_disallowed_extension_rejected(self):
        response = self._upload(filename="payload.exe", content=b"exe")

        self.assertEqual(response.status_code, 302)
        self.assertIn("upload_error=invalid", response.headers["Location"])
        self.assertEqual(self._rows("practical_submissions"), [])
        self.assertFalse(self.upload_root.exists())

    def test_empty_upload_rejected(self):
        response = self._upload(filename="empty.xlsx", content=b"")

        self.assertEqual(response.status_code, 302)
        self.assertIn("upload_error=invalid", response.headers["Location"])
        self.assertEqual(self._rows("practical_submission_files"), [])

    def test_oversized_upload_rejected(self):
        response = self._upload(filename="large.xlsx", content=b"x" * 1025)

        self.assertEqual(response.status_code, 302)
        self.assertIn("upload_error=invalid", response.headers["Location"])
        self.assertEqual(self._rows("practical_submission_files"), [])

    def test_non_practical_exam_task_id_rejected(self):
        response = self._upload(exam_task_id=150)

        self.assertEqual(response.status_code, 404)
        self.assertEqual(self._rows("practical_submissions"), [])

    def test_unknown_attempt_rejected(self):
        response = self.client.post(
            "/quiz/attempt/999/practical/may_2025_v2/task/260/upload",
            data={"files": (io.BytesIO(b"x"), "solution.xlsx")},
            content_type="multipart/form-data",
        )

        self.assertEqual(response.status_code, 404)

    def test_unknown_task_rejected(self):
        response = self._upload(exam_task_id=999)

        self.assertEqual(response.status_code, 404)

    def test_missing_attempt_context_rejected(self):
        response = self.client.get("/quiz/attempt/999/practical/may_2025_v2")

        self.assertEqual(response.status_code, 404)

    # ------------------------------------------------------------------
    # Tester-reported scenarios at the route level
    # ------------------------------------------------------------------

    def test_double_extension_virus_pdf_dot_exe_rejected_by_route(self):
        response = self._upload(filename="virus.pdf.exe", content=b"malware")

        self.assertEqual(response.status_code, 302)
        self.assertIn("upload_error=invalid", response.headers["Location"])
        self.assertEqual(self._rows("practical_submission_files"), [])
        self.assertEqual(self._rows("practical_submissions"), [])
        self.assertFalse(self.upload_root.exists())

    def test_double_extension_archive_zip_dot_exe_rejected_by_route(self):
        response = self._upload(filename="archive.zip.exe", content=b"PK\x03\x04junk")

        self.assertEqual(response.status_code, 302)
        self.assertIn("upload_error=invalid", response.headers["Location"])
        self.assertEqual(self._rows("practical_submission_files"), [])

    def test_normal_zip_upload_accepted_by_route(self):
        response = self._upload(
            exam_task_id=270,
            filename="graphics.zip",
            content=b"PK\x03\x04normal-zip-bytes",
        )

        self.assertEqual(response.status_code, 302)
        self.assertIn("uploaded=1", response.headers["Location"])
        files = self._rows("practical_submission_files")
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0]["original_filename"], "graphics.zip")
        self.assertTrue(files[0]["stored_path"].endswith(".zip"))

    def test_oversized_zip_upload_rejected_by_route(self):
        # PRACTICAL_MAX_UPLOAD_BYTES is set to 1024 in setUp.
        response = self._upload(
            exam_task_id=270,
            filename="huge.zip",
            content=b"x" * 1025,
        )

        self.assertEqual(response.status_code, 302)
        self.assertIn("upload_error=invalid", response.headers["Location"])
        self.assertEqual(self._rows("practical_submission_files"), [])

    def test_duplicate_uploads_produce_distinct_stored_paths_without_overwrite(self):
        first = self._upload(filename="solution.xlsx", content=b"first version")
        second = self._upload(filename="solution.xlsx", content=b"second version!")

        self.assertEqual(first.status_code, 302)
        self.assertEqual(second.status_code, 302)
        self.assertIn("uploaded=1", second.headers["Location"])

        files = self._rows("practical_submission_files")
        self.assertEqual(len(files), 2)

        stored_paths = {row["stored_path"] for row in files}
        self.assertEqual(len(stored_paths), 2, "stored_paths must be unique per upload")
        self.assertEqual(
            {row["original_filename"] for row in files},
            {"solution.xlsx"},
            "the original filename is preserved across duplicates",
        )

        # Both physical files must exist and retain their respective payloads —
        # i.e. the second upload did not overwrite the first.
        stored_regular_files = sorted(
            (path for path in self.upload_root.rglob("*") if path.is_file()),
            key=lambda p: p.stat().st_mtime,
        )
        self.assertEqual(len(stored_regular_files), 2)
        payloads = {p.read_bytes() for p in stored_regular_files}
        self.assertEqual(payloads, {b"first version", b"second version!"})

        # The submission row is reused (single (attempt, task) submission),
        # but the file rows are appended without violating UNIQUE(stored_path).
        submissions = self._rows("practical_submissions")
        self.assertEqual(len(submissions), 1)

    def test_stored_filename_differs_from_original_at_route_level(self):
        self._upload(filename="solution.xlsx", content=b"xlsx bytes")
        files = self._rows("practical_submission_files")
        self.assertEqual(len(files), 1)
        stored_path = files[0]["stored_path"]
        stored_basename = Path(stored_path).name
        self.assertNotEqual(stored_basename, "solution.xlsx")
        # Stored filename keeps the original extension but uses a token name.
        self.assertTrue(stored_basename.endswith(".xlsx"))


if __name__ == "__main__":
    unittest.main()
