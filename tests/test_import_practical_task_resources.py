import os
import sqlite3
import tempfile
import unittest
import zipfile
from pathlib import Path

from src.import_practical_task_resources import import_practical_task_resources


def make_conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
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
            exam_id INTEGER NOT NULL REFERENCES exams(id) ON DELETE CASCADE,
            task_number INTEGER NOT NULL,
            task_kind TEXT NOT NULL,
            points INTEGER NOT NULL
        );
        CREATE TABLE practical_task_resources (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            exam_task_id INTEGER NOT NULL,
            resource_path TEXT NOT NULL CHECK (length(resource_path) > 0),
            original_filename TEXT,
            label_bg TEXT,
            file_size_bytes INTEGER CHECK (file_size_bytes IS NULL OR file_size_bytes >= 0),
            sha256 TEXT CHECK (sha256 IS NULL OR length(sha256) = 64),
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (exam_task_id) REFERENCES exam_tasks(id) ON DELETE CASCADE,
            UNIQUE (exam_task_id, resource_path)
        );
        CREATE TABLE practical_submissions (
            id INTEGER PRIMARY KEY,
            marker TEXT NOT NULL
        );
    """)
    conn.execute("""
        INSERT INTO exams (
            id, subject, level, year, session, variant, format_version
        )
        VALUES (1, 'informatika_it', 'DZI', 2025, 'may', 2, 'dzi_it_pp_2025_format')
    """)
    conn.executemany(
        "INSERT INTO exam_tasks (id, exam_id, task_number, task_kind, points) VALUES (?, ?, ?, ?, ?)",
        [
            (126, 1, 26, "practical_spreadsheet", 15),
            (127, 1, 27, "practical_graphics", 20),
            (128, 1, 28, "practical_web", 20),
        ],
    )
    conn.execute("INSERT INTO practical_submissions (id, marker) VALUES (1, 'untouched')")
    conn.commit()
    return conn


def payload(resource_path):
    return {
        "source_slug": "may_2025_v2",
        "tasks": [
            {
                "task_number": 26,
                "task_kind": "practical_spreadsheet",
                "points": 15,
                "prompt_bg": "Spreadsheet task",
                "grading_mode": "manual",
                "resource_files": [resource_path],
            }
        ],
    }


class ImportPracticalTaskResourcesTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.orig_cwd = os.getcwd()
        os.chdir(self.tmpdir.name)
        self.addCleanup(os.chdir, self.orig_cwd)

        self.reference_root = Path("data/reference/may_2025_v2/practical")
        self.reference_root.mkdir(parents=True)
        self.resource = self.reference_root / "Shipments.xlsx"
        self.resource.write_bytes(b"fake xlsx")
        self.resource_path = str(self.resource)
        self.zip_file = self.reference_root / "resources.zip"
        with zipfile.ZipFile(self.zip_file, "w") as archive:
            archive.writestr("task_26/Zoomag.xlsx", b"zip xlsx")
        self.zip_resource_path = f"{self.zip_file}::task_26/Zoomag.xlsx"

    def test_valid_import_inserts_resource_rows(self):
        conn = make_conn()
        try:
            summary = import_practical_task_resources(conn, payload(self.resource_path))

            self.assertEqual(summary.inserted, 1)
            self.assertEqual(summary.updated, 0)
            self.assertEqual(summary.unchanged, 0)
            row = conn.execute("""
                SELECT exam_task_id, resource_path, original_filename,
                       label_bg, file_size_bytes, sha256
                FROM practical_task_resources
            """).fetchone()
            self.assertEqual(row["exam_task_id"], 126)
            self.assertEqual(row["resource_path"], self.resource_path)
            self.assertEqual(row["original_filename"], "Shipments.xlsx")
            self.assertIsNone(row["label_bg"])
            self.assertEqual(row["file_size_bytes"], len(b"fake xlsx"))
            self.assertEqual(len(row["sha256"]), 64)
        finally:
            conn.close()

    def test_zip_internal_resource_import_is_idempotent(self):
        conn = make_conn()
        try:
            first = import_practical_task_resources(conn, payload(self.zip_resource_path))
            self.assertEqual(first.inserted, 1)
            self.assertEqual(first.updated, 0)
            self.assertEqual(first.unchanged, 0)

            second = import_practical_task_resources(conn, payload(self.zip_resource_path))
            self.assertEqual(second.inserted, 0)
            self.assertEqual(second.updated, 0)
            self.assertEqual(second.unchanged, 1)

            rows = conn.execute("""
                SELECT exam_task_id, resource_path, original_filename,
                       label_bg, file_size_bytes, sha256
                FROM practical_task_resources
            """).fetchall()
            self.assertEqual(len(rows), 1)
            row = rows[0]
            self.assertEqual(row["exam_task_id"], 126)
            self.assertEqual(row["resource_path"], self.zip_resource_path)
            self.assertEqual(row["original_filename"], "Zoomag.xlsx")
            self.assertIsNone(row["label_bg"])
            self.assertEqual(row["file_size_bytes"], len(b"zip xlsx"))
            self.assertIsNone(row["sha256"])
        finally:
            conn.close()

    def test_rerun_is_idempotent_and_updates_without_duplicates(self):
        conn = make_conn()
        try:
            first = import_practical_task_resources(conn, payload(self.resource_path))
            self.assertEqual(first.inserted, 1)

            second = import_practical_task_resources(conn, payload(self.resource_path))
            self.assertEqual(second.inserted, 0)
            self.assertEqual(second.updated, 0)
            self.assertEqual(second.unchanged, 1)

            self.resource.write_bytes(b"changed xlsx bytes")
            third = import_practical_task_resources(conn, payload(self.resource_path))
            self.assertEqual(third.inserted, 0)
            self.assertEqual(third.updated, 1)
            self.assertEqual(third.unchanged, 0)

            count = conn.execute("SELECT COUNT(*) FROM practical_task_resources").fetchone()[0]
            size = conn.execute("SELECT file_size_bytes FROM practical_task_resources").fetchone()[0]
            self.assertEqual(count, 1)
            self.assertEqual(size, len(b"changed xlsx bytes"))
        finally:
            conn.close()

    def test_unknown_source_is_rejected(self):
        conn = make_conn()
        try:
            batch = payload(self.resource_path)
            batch["source_slug"] = "may_2030_v2"
            with self.assertRaisesRegex(ValueError, "No matching exam row"):
                import_practical_task_resources(conn, batch)
            count = conn.execute("SELECT COUNT(*) FROM practical_task_resources").fetchone()[0]
            self.assertEqual(count, 0)
        finally:
            conn.close()

    def test_unknown_exam_task_is_rejected(self):
        conn = make_conn()
        try:
            conn.execute("DELETE FROM exam_tasks WHERE task_number = 26")
            with self.assertRaisesRegex(ValueError, "does not exist in exam_tasks"):
                import_practical_task_resources(conn, payload(self.resource_path))
        finally:
            conn.close()

    def test_invalid_resource_path_is_rejected_by_validator(self):
        conn = make_conn()
        try:
            outside = Path("outside/Shipments.xlsx")
            outside.parent.mkdir()
            outside.write_bytes(b"outside")
            with self.assertRaisesRegex(ValueError, "must resolve under one of"):
                import_practical_task_resources(conn, payload(str(outside)))
            count = conn.execute("SELECT COUNT(*) FROM practical_task_resources").fetchone()[0]
            self.assertEqual(count, 0)
        finally:
            conn.close()

    def test_upload_submission_tables_are_not_touched(self):
        conn = make_conn()
        try:
            import_practical_task_resources(conn, payload(self.resource_path))
            row = conn.execute("SELECT id, marker FROM practical_submissions").fetchone()
            self.assertEqual(tuple(row), (1, "untouched"))
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
