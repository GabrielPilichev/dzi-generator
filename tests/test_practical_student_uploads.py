import io
import json
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from types import SimpleNamespace
from unittest.mock import patch


from werkzeug.datastructures import FileStorage, MultiDict
from werkzeug.test import encode_multipart

from src.practical_uploads import MAX_FILES_PER_REQUEST, MAX_REQUEST_BYTES, MAX_UPLOAD_BYTES
import src.practical_uploads as upload_policy
from tests.support import IsolatedAppTestCase, web_app


class PracticalStudentUploadsTest(IsolatedAppTestCase):
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
        with self.client.session_transaction() as sess:
            sess[web_app.quiz_attempt_owner_session_key(101)] = True

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
        self.assertNotIn("management-page", html)

    def test_fresh_session_cannot_access_other_practical_attempt_page(self):
        other_client = web_app.app.test_client()

        response = other_client.get("/quiz/attempt/101/practical/may_2025_v2")

        self.assertEqual(response.status_code, 404)

    def test_fresh_session_cannot_upload_to_other_practical_attempt(self):
        other_client = web_app.app.test_client()

        response = other_client.post(
            "/quiz/attempt/101/practical/may_2025_v2/task/260/upload",
            data={"files": (io.BytesIO(b"xlsx bytes"), "solution.xlsx")},
            content_type="multipart/form-data",
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 404)
        self.assertEqual(self._rows("practical_submissions"), [])
        self.assertEqual(self._rows("practical_submission_files"), [])
        self.assertFalse(self.upload_root.exists())

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

    # --- request-wide limits (file count and body size) ---------------------

    UPLOAD_URL = "/quiz/attempt/101/practical/may_2025_v2/task/260/upload"

    def _upload_many(self, files):
        return self.client.post(
            self.UPLOAD_URL,
            data={"files": [(io.BytesIO(content), name) for name, content in files]},
            content_type="multipart/form-data",
        )

    def _multipart(self, files):
        boundary, body = encode_multipart(
            MultiDict(("files", FileStorage(io.BytesIO(content), filename=name))
                      for name, content in files),
            boundary="learnpilot-test-boundary",
        )
        return body, f"multipart/form-data; boundary={boundary}"

    def test_default_request_limits_come_from_upload_policy(self):
        self.assertEqual(web_app.app.config["MAX_CONTENT_LENGTH"], MAX_REQUEST_BYTES)
        self.assertEqual(web_app.app.config["PRACTICAL_MAX_FILES_PER_REQUEST"], MAX_FILES_PER_REQUEST)
        # Four maximum-size files always fit in one request, with room for overhead.
        self.assertGreater(MAX_REQUEST_BYTES, 4 * MAX_UPLOAD_BYTES)
        self.assertLess(MAX_REQUEST_BYTES, 5 * MAX_UPLOAD_BYTES)
        self.assertGreaterEqual(MAX_FILES_PER_REQUEST, 4)

    def test_multi_file_upload_is_stored_in_one_submission(self):
        response = self._upload_many([
            ("index.html", b"<html></html>"), ("style.css", b"body{}"),
            ("logo.png", b"png bytes"), ("data.csv", b"a,b"),
        ])

        self.assertEqual(response.status_code, 302)
        self.assertIn("uploaded=1", response.headers["Location"])
        self.assertEqual(len(self._rows("practical_submissions")), 1)
        names = [row["original_filename"] for row in self._rows("practical_submission_files")]
        self.assertEqual(sorted(names), ["data.csv", "index.html", "logo.png", "style.css"])

    def test_file_count_boundary(self):
        web_app.app.config["PRACTICAL_MAX_FILES_PER_REQUEST"] = 3
        too_many = self._upload_many([(f"f{i}.csv", b"x") for i in range(4)])

        self.assertEqual(too_many.status_code, 302)
        self.assertIn("upload_error=invalid", too_many.headers["Location"])
        self.assertEqual(self._rows("practical_submission_files"), [])
        self.assertFalse(self.upload_root.exists())

        at_limit = self._upload_many([(f"f{i}.csv", b"x") for i in range(3)])
        self.assertIn("uploaded=1", at_limit.headers["Location"])
        self.assertEqual(len(self._rows("practical_submission_files")), 3)

    def test_request_size_boundary_is_enforced_by_flask(self):
        body, content_type = self._multipart([("a.csv", b"a" * 300), ("b.csv", b"b" * 300)])

        web_app.app.config["MAX_CONTENT_LENGTH"] = len(body) - 1
        rejected = self.client.post(self.UPLOAD_URL, data=body, content_type=content_type)
        self.assertEqual(rejected.status_code, 302)
        self.assertIn("upload_error=too_large", rejected.headers["Location"])
        self.assertEqual(self._rows("practical_submissions"), [])
        self.assertEqual(self._rows("practical_submission_files"), [])
        self.assertFalse(self.upload_root.exists())
        page = self.client.get(rejected.headers["Location"])
        self.assertEqual(page.status_code, 200)
        self.assertIn("Файлът не беше приет", page.get_data(as_text=True))

        web_app.app.config["MAX_CONTENT_LENGTH"] = len(body)
        accepted = self.client.post(self.UPLOAD_URL, data=body, content_type=content_type)
        self.assertEqual(accepted.status_code, 302)
        self.assertIn("uploaded=1", accepted.headers["Location"])
        self.assertEqual(len(self._rows("practical_submission_files")), 2)

    def test_per_file_limit_still_applies_inside_an_allowed_request(self):
        # PRACTICAL_MAX_UPLOAD_BYTES is 1024 in setUp; the request itself is small enough.
        response = self._upload_many([("ok.csv", b"x" * 1024), ("big.csv", b"x" * 1025)])

        self.assertIn("upload_error=invalid", response.headers["Location"])
        self.assertEqual(self._rows("practical_submission_files"), [])

    def test_free_space_floor_rejects_low_space_and_whole_request_crossing(self):
        floor = upload_policy.MIN_FREE_BYTES
        for free, files in ((floor - 1, [("a.csv", b"a")]),
                            (floor + 2, [("a.csv", b"ab"), ("b.csv", b"c")])):
            with self.subTest(free=free), patch.object(upload_policy.shutil, "disk_usage",
                                                  return_value=SimpleNamespace(free=free)):
                response = self._upload_many(files)
            self.assertIn("upload_error=invalid", response.headers["Location"])
            self.assertEqual(self._rows("practical_submissions"), [])
            self.assertEqual(self._rows("practical_submission_files"), [])
            self.assertFalse(self.upload_root.exists())
        with patch.object(upload_policy.shutil, "disk_usage",
                          return_value=SimpleNamespace(free=floor + 3)):
            self.assertIn("uploaded=1", self._upload_many([
                ("a.csv", b"ab"), ("b.csv", b"c")]).headers["Location"])

    def test_cumulative_byte_quota_is_inclusive_and_rejection_atomic(self):
        with patch.object(upload_policy, "MAX_STORED_BYTES_PER_TASK", 10):
            self.assertIn("uploaded=1", self._upload(content=b"12345").headers["Location"])
            self.assertIn("uploaded=1", self._upload(content=b"67890").headers["Location"])
            rejected = self._upload_many([("a.csv", b"x"), ("b.csv", b"y")])
        self.assertIn("upload_error=invalid", rejected.headers["Location"])
        self.assertEqual(len(self._rows("practical_submission_files")), 2)
        self.assertEqual(len(list(self.upload_root.rglob("*.csv"))), 0)

    def test_cumulative_file_quota_is_inclusive_and_scoped_to_task(self):
        with patch.object(upload_policy, "MAX_STORED_FILES_PER_TASK", 2):
            self._upload()
            self._upload()
            rejected = self._upload_many([("a.csv", b"x"), ("b.csv", b"y")])
            other_task = self._upload(exam_task_id=270, filename="other.zip")
        self.assertIn("upload_error=invalid", rejected.headers["Location"])
        self.assertIn("uploaded=1", other_task.headers["Location"])
        self.assertEqual(len(self._rows("practical_submission_files")), 3)

    def test_another_attempt_does_not_count_against_quota(self):
        conn = sqlite3.connect(self.db_path)
        with conn:
            conn.execute("INSERT INTO quiz_attempts (id, assignment_id, student_name, seed, question_ids_json) "
                         "VALUES (202, 1, 'Student Two', 'seed', '[]')")
        conn.close()
        with patch.object(upload_policy, "MAX_STORED_FILES_PER_TASK", 1):
            self._upload()
            with self.client.session_transaction() as sess:
                sess[web_app.quiz_attempt_owner_session_key(202)] = True
            response = self.client.post(
                "/quiz/attempt/202/practical/may_2025_v2/task/260/upload",
                data={"files": (io.BytesIO(b"second"), "second.xlsx")},
                content_type="multipart/form-data",
            )
        self.assertIn("uploaded=1", response.headers["Location"])
        self.assertEqual(len(self._rows("practical_submission_files")), 2)

    def test_concurrent_uploads_cannot_cross_one_file_quota(self):
        clients = [web_app.app.test_client(), web_app.app.test_client()]
        for client in clients:
            with client.session_transaction() as sess:
                sess[web_app.quiz_attempt_owner_session_key(101)] = True
        barrier = Barrier(2)

        def upload(client):
            barrier.wait(timeout=5)
            return client.post(self.UPLOAD_URL,
                               data={"files": (io.BytesIO(b"content"), "answer.csv")},
                               content_type="multipart/form-data").headers["Location"]

        with patch.object(upload_policy, "MAX_STORED_FILES_PER_TASK", 1):
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(upload, clients))
        self.assertEqual(sum("uploaded=1" in location for location in results), 1)
        self.assertEqual(sum("upload_error=invalid" in location for location in results), 1)
        self.assertEqual(len(self._rows("practical_submission_files")), 1)
        self.assertEqual(len([p for p in self.upload_root.rglob("*") if p.is_file()]), 1)

    def test_partial_disk_save_is_cleaned_up_and_database_rolled_back(self):
        original_save = FileStorage.save
        calls = 0

        def fail_after_partial_save(storage, destination, *args, **kwargs):
            nonlocal calls
            calls += 1
            original_save(storage, destination, *args, **kwargs)
            if calls == 2:
                raise OSError("simulated disk failure")

        with patch.object(FileStorage, "save", fail_after_partial_save):
            response = self._upload_many([("a.csv", b"first"), ("b.csv", b"second")])
        self.assertIn("upload_error=invalid", response.headers["Location"])
        self.assertEqual(self._rows("practical_submissions"), [])
        self.assertEqual(self._rows("practical_submission_files"), [])
        self.assertEqual([p for p in self.upload_root.rglob("*") if p.is_file()], [])

    def test_completed_attempt_cannot_upload(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("UPDATE quiz_attempts SET submitted_at = CURRENT_TIMESTAMP WHERE id = 101")
        page = self.client.get("/quiz/attempt/101/practical/may_2025_v2")
        self.assertEqual(page.status_code, 200)
        self.assertNotIn('class="practical-upload-form"', page.get_data(as_text=True))
        response = self._upload()
        self.assertIn("upload_error=invalid", response.headers["Location"])
        self.assertEqual(self._rows("practical_submission_files"), [])
        self.assertFalse(self.upload_root.exists())

    def test_closed_practical_submission_has_no_upload_form(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                INSERT INTO practical_submissions (quiz_attempt_id, exam_task_id, status)
                VALUES (101, 260, 'submitted')
            """)
        page = self.client.get("/quiz/attempt/101/practical/may_2025_v2")
        self.assertEqual(page.status_code, 200)
        html = page.get_data(as_text=True)
        self.assertNotIn('/task/260/upload', html)
        self.assertIn('/task/270/upload', html)
        response = self._upload()
        self.assertIn("upload_error=invalid", response.headers["Location"])
        self.assertEqual(self._rows("practical_submission_files"), [])

    def test_oversized_non_upload_request_gets_413(self):
        web_app.app.config["MAX_CONTENT_LENGTH"] = 512
        response = self.client.post("/tester/login", data={"password": "x" * 2048})
        self.assertEqual(response.status_code, 413)


if __name__ == "__main__":
    unittest.main()
