import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

os.environ["DZI_ADMIN_PASSWORD"] = "admin-pass"

from web import app as web_app  # noqa: E402


class PracticalTaskDownloadsTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.project_root = Path(self.temp_dir.name)
        self.db_path = self.project_root / "questions.db"
        self.batch_dir = self.project_root / "data" / "import_batches"
        self.resource_dir = self.project_root / "data" / "reference" / "may_2025_v2"
        self.batch_dir.mkdir(parents=True)
        self.resource_dir.mkdir(parents=True)

        self.resource_path = Path("data/reference/may_2025_v2/Shipments.xlsx")
        (self.project_root / self.resource_path).write_bytes(b"fake practical resource")
        self.missing_resource_path = Path("data/reference/may_2025_v2/missing.xlsx")
        self.unicode_resource_path = Path("data/reference/may_2025_v2/данни.xlsx")
        (self.project_root / self.unicode_resource_path).write_bytes(b"unicode resource")

        self._create_db()
        self._write_batch()

        self.old_project_root = web_app.PROJECT_ROOT
        self.old_resource_roots = web_app.PRACTICAL_RESOURCE_ROOTS
        self.old_db_path = web_app.app.config["DB_PATH"]
        self.old_batch_dir = web_app.app.config["PRACTICAL_TASK_BATCH_DIR"]
        self.addCleanup(self._restore_app_config)

        web_app.PROJECT_ROOT = self.project_root
        web_app.PRACTICAL_RESOURCE_ROOTS = (
            self.project_root / "data" / "reference",
            self.project_root / "data" / "assets",
        )
        web_app.app.config.update(
            TESTING=True,
            DB_PATH=str(self.db_path),
            PRACTICAL_TASK_BATCH_DIR=str(self.batch_dir),
        )
        self.client = web_app.app.test_client()
        with self.client.session_transaction() as sess:
            sess["admin_authenticated"] = True
            sess["ui_profile"] = "admin"

    def _restore_app_config(self):
        web_app.PROJECT_ROOT = self.old_project_root
        web_app.PRACTICAL_RESOURCE_ROOTS = self.old_resource_roots
        web_app.app.config["DB_PATH"] = self.old_db_path
        web_app.app.config["PRACTICAL_TASK_BATCH_DIR"] = self.old_batch_dir

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
                CREATE TABLE exam_tasks (
                    id INTEGER PRIMARY KEY,
                    exam_id INTEGER NOT NULL,
                    task_number INTEGER NOT NULL,
                    task_kind TEXT NOT NULL,
                    points INTEGER NOT NULL,
                    has_assets INTEGER DEFAULT 0,
                    prompt TEXT,
                    rubric TEXT,
                    topic_id INTEGER
                );
                CREATE TABLE exam_task_questions (
                    task_id INTEGER NOT NULL,
                    question_id INTEGER NOT NULL,
                    role TEXT NOT NULL
                );
                CREATE TABLE questions (
                    id INTEGER PRIMARY KEY,
                    prompt TEXT,
                    question_type TEXT,
                    topic_id INTEGER,
                    is_ai_generated INTEGER DEFAULT 0,
                    quality_score REAL
                );
                CREATE TABLE curriculum_topics (
                    id INTEGER PRIMARY KEY,
                    title_bg TEXT
                );
                CREATE TABLE asset_links (
                    id INTEGER PRIMARY KEY,
                    asset_id INTEGER,
                    owner_type TEXT,
                    owner_id INTEGER,
                    role TEXT
                );
                CREATE TABLE practical_tasks (
                    task_id INTEGER PRIMARY KEY,
                    work_environment TEXT,
                    expected_outputs_json TEXT,
                    notes TEXT
                );
                CREATE TABLE practical_task_resources (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    exam_task_id INTEGER NOT NULL,
                    resource_path TEXT NOT NULL,
                    original_filename TEXT,
                    label_bg TEXT,
                    file_size_bytes INTEGER,
                    sha256 TEXT,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE (exam_task_id, resource_path)
                );
            """)
            conn.execute("""
                INSERT INTO exams (
                    id, subject, level, year, session, variant, format_version
                )
                VALUES (6, 'informatika_it', 'DZI', 2025, 'may', 2, 'dzi_it_pp_2025_format')
            """)
            conn.executemany(
                """
                INSERT INTO exam_tasks (id, exam_id, task_number, task_kind, points)
                VALUES (?, 6, ?, ?, ?)
                """,
                [
                    (260, 26, "practical_spreadsheet", 15),
                    (270, 27, "practical_graphics", 20),
                    (280, 28, "practical_web", 20),
                ],
            )
            conn.executemany(
                """
                INSERT INTO practical_tasks (task_id, work_environment)
                VALUES (?, ?)
                """,
                [(260, "spreadsheet"), (270, "graphics"), (280, "web")],
            )
            conn.executemany(
                """
                INSERT INTO practical_task_resources (
                    id, exam_task_id, resource_path, original_filename, label_bg, file_size_bytes, sha256
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (1, 260, str(self.resource_path), "Shipments.xlsx", None, 23, "a" * 64),
                    (2, 270, str(self.missing_resource_path), "missing.xlsx", None, 10, "b" * 64),
                    (3, 280, str(self.unicode_resource_path), "данни.xlsx", "Данни", 16, "d" * 64),
                ],
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
                    "prompt_bg": "Задача 26 prompt",
                    "instructions_bg": "Instruction line one\nInstruction line two",
                    "expected_outputs": ["it_23.05.2025_zad_26.xlsx"],
                },
                {
                    "task_number": 27,
                    "prompt_bg": "Task 27 prompt",
                    "instructions_bg": "Task 27 instructions",
                    "expected_outputs": ["task27.zip"],
                },
                {
                    "task_number": 28,
                    "prompt_bg": "Task 28 prompt",
                    "instructions_bg": "Task 28 instructions",
                    "expected_outputs": ["index.html"],
                },
            ],
        }
        (self.batch_dir / "may_2025_v2_practical_tasks_26_28.json").write_text(
            json.dumps(payload, ensure_ascii=False),
            encoding="utf-8",
        )

    def _insert_resource(self, resource_path: str, filename: str = "bad.txt") -> int:
        conn = sqlite3.connect(self.db_path)
        try:
            cur = conn.execute(
                """
                INSERT INTO practical_task_resources (
                    exam_task_id, resource_path, original_filename, file_size_bytes, sha256
                )
                VALUES (260, ?, ?, 1, ?)
                """,
                (resource_path, filename, "c" * 64),
            )
            conn.commit()
            return int(cur.lastrowid)
        finally:
            conn.close()

    def test_page_renders_may_2025_practical_tasks_and_resources(self):
        response = self.client.get("/dzi/source/may_2025_v2/practical")

        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("Практически задачи", html)
        self.assertIn("Задача 26", html)
        self.assertIn("Задача 27", html)
        self.assertIn("Задача 28", html)
        self.assertIn("Instruction line one", html)
        self.assertIn("Shipments.xlsx", html)
        self.assertIn("/dzi/practical/resource/1/download", html)

    def test_dzi_source_page_links_to_practical_tasks_when_resources_exist(self):
        response = self.client.get("/dzi/source/may_2025_v2")

        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("Практически задачи 26–28", html)
        self.assertIn("/dzi/source/may_2025_v2/practical", html)
        self.assertIn("3 файла за изтегляне", html)

    def test_dzi_source_page_does_not_expose_raw_resource_paths(self):
        response = self.client.get("/dzi/source/may_2025_v2")

        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertNotIn("data/reference/", html)
        self.assertNotIn(str(self.project_root), html)

    def test_download_route_returns_valid_resource_file(self):
        response = self.client.get("/dzi/practical/resource/1/download")

        try:
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.data, b"fake practical resource")
            self.assertIn("attachment", response.headers["Content-Disposition"])
            self.assertIn("Shipments.xlsx", response.headers["Content-Disposition"])
        finally:
            response.close()

    def test_download_route_handles_unicode_original_filename(self):
        response = self.client.get("/dzi/practical/resource/3/download")

        try:
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.data, b"unicode resource")
            disposition = response.headers["Content-Disposition"]
            self.assertIn("attachment", disposition)
            self.assertIn("filename", disposition)
            self.assertNotIn(str(self.project_root), disposition)
        finally:
            response.close()

    def test_nonexistent_resource_id_returns_404(self):
        response = self.client.get("/dzi/practical/resource/999/download")

        self.assertEqual(response.status_code, 404)

    def test_path_traversal_resource_row_is_rejected(self):
        resource_id = self._insert_resource("../secret.txt")

        response = self.client.get(f"/dzi/practical/resource/{resource_id}/download")

        self.assertEqual(response.status_code, 404)

    def test_absolute_path_resource_row_is_rejected(self):
        resource_id = self._insert_resource("/etc/passwd", "passwd")

        response = self.client.get(f"/dzi/practical/resource/{resource_id}/download")

        self.assertEqual(response.status_code, 404)

    def test_missing_file_does_not_crash_page_or_download_route(self):
        page = self.client.get("/dzi/source/may_2025_v2/practical")
        download = self.client.get("/dzi/practical/resource/2/download")

        self.assertEqual(page.status_code, 200)
        self.assertIn("файлът липсва", page.get_data(as_text=True))
        self.assertEqual(download.status_code, 404)

    def test_zip_internal_resource_row_does_not_crash_download_route(self):
        resource_id = self._insert_resource(
            "data/reference/may_2025_v2/resources.zip::task_26/Shipments.xlsx",
            "Shipments.xlsx",
        )

        response = self.client.get(f"/dzi/practical/resource/{resource_id}/download")

        self.assertEqual(response.status_code, 404)

    def test_page_does_not_expose_raw_filesystem_paths(self):
        response = self.client.get("/dzi/source/may_2025_v2/practical")

        html = response.get_data(as_text=True)
        self.assertNotIn("data/reference/", html)
        self.assertNotIn(str(self.project_root), html)


if __name__ == "__main__":
    unittest.main()
