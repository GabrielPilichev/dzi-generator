import copy
import os
import sqlite3
import tempfile
import unittest
import zipfile
from pathlib import Path

from src.validate_practical_task_batch import (
    readonly_uri,
    validate_batch,
)


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
            points INTEGER
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
    conn.commit()
    return conn


def base_payload(resource_path):
    return {
        "source_slug": "may_2025_v2",
        "source_title": "ДЗИ ИТ ПП - май 2025, вариант 2",
        "tasks": [
            {
                "task_number": 26,
                "task_kind": "practical_spreadsheet",
                "points": 15,
                "prompt_bg": "Създайте обобщаваща таблица...",
                "grading_mode": "manual",
                "resource_files": [resource_path],
            }
        ],
    }


class ValidatePracticalTaskBatchTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)

        self._orig_cwd = os.getcwd()
        os.chdir(self.tmpdir.name)
        self.addCleanup(os.chdir, self._orig_cwd)

        self.reference_root = Path("data/reference/may_2025_v2/practical")
        self.reference_root.mkdir(parents=True)
        self.resource_file = self.reference_root / "Shipments.xlsx"
        self.resource_file.write_bytes(b"PK fake xlsx")
        self.resource_relpath = str(self.resource_file)
        self.zip_file = self.reference_root / "resources.zip"
        with zipfile.ZipFile(self.zip_file, "w") as archive:
            archive.writestr("task_26/Zoomag.xlsx", b"fake xlsx")
            archive.writestr("task_27/image.jpg", b"fake jpg")
            archive.writestr("__MACOSX/task_26/Zoomag.xlsx", b"metadata")
        self.zip_resource_ref = f"{self.zip_file}::task_26/Zoomag.xlsx"

    def test_minimal_valid_practical_batch_passes(self):
        conn = make_conn()
        try:
            before = conn.total_changes
            summary = validate_batch(conn, base_payload(self.resource_relpath))
            self.assertEqual(summary.tasks_read, 1)
            self.assertEqual(summary.resource_files_checked, 1)
            self.assertEqual(summary.source_slug, "may_2025_v2")
            self.assertEqual(summary.exam_id, 1)
            self.assertEqual(conn.total_changes, before)
        finally:
            conn.close()

    def test_zip_internal_resource_reference_passes(self):
        conn = make_conn()
        try:
            summary = validate_batch(conn, base_payload(self.zip_resource_ref))
            self.assertEqual(summary.tasks_read, 1)
            self.assertEqual(summary.resource_files_checked, 1)
        finally:
            conn.close()

    def test_missing_zip_file_is_rejected(self):
        conn = make_conn()
        try:
            ref = "data/reference/may_2025_v2/practical/missing.zip::task_26/Zoomag.xlsx"
            with self.assertRaisesRegex(ValueError, "resource file does not exist"):
                validate_batch(conn, base_payload(ref))
        finally:
            conn.close()

    def test_missing_zip_member_is_rejected(self):
        conn = make_conn()
        try:
            ref = f"{self.zip_file}::task_26/missing.xlsx"
            with self.assertRaisesRegex(ValueError, "ZIP member does not exist"):
                validate_batch(conn, base_payload(ref))
        finally:
            conn.close()

    def test_path_traversal_in_zip_path_is_rejected(self):
        conn = make_conn()
        try:
            ref = f"{self.reference_root}/../resources.zip::task_26/Zoomag.xlsx"
            with self.assertRaisesRegex(ValueError, "must not contain '\\.\\.'"):
                validate_batch(conn, base_payload(ref))
        finally:
            conn.close()

    def test_path_traversal_in_zip_member_is_rejected(self):
        conn = make_conn()
        try:
            ref = f"{self.zip_file}::task_26/../secret.xlsx"
            with self.assertRaisesRegex(ValueError, "ZIP member path must not contain traversal"):
                validate_batch(conn, base_payload(ref))
        finally:
            conn.close()

    def test_absolute_zip_path_is_rejected(self):
        conn = make_conn()
        try:
            ref = f"{Path(self.zip_file).resolve()}::task_26/Zoomag.xlsx"
            with self.assertRaisesRegex(ValueError, "resource path must be relative"):
                validate_batch(conn, base_payload(ref))
        finally:
            conn.close()

    def test_absolute_zip_member_path_is_rejected(self):
        conn = make_conn()
        try:
            ref = f"{self.zip_file}::/task_26/Zoomag.xlsx"
            with self.assertRaisesRegex(ValueError, "ZIP member path must be relative"):
                validate_batch(conn, base_payload(ref))
        finally:
            conn.close()

    def test_macosx_zip_member_is_rejected(self):
        conn = make_conn()
        try:
            ref = f"{self.zip_file}::__MACOSX/task_26/Zoomag.xlsx"
            with self.assertRaisesRegex(ValueError, "__MACOSX ZIP metadata"):
                validate_batch(conn, base_payload(ref))
        finally:
            conn.close()

    def test_task_number_outside_26_28_is_rejected(self):
        conn = make_conn()
        try:
            payload = base_payload(self.resource_relpath)
            payload["tasks"][0]["task_number"] = 25
            with self.assertRaisesRegex(ValueError, "task_number must be one of"):
                validate_batch(conn, payload)
        finally:
            conn.close()

    def test_multiple_choice_task_kind_is_rejected(self):
        conn = make_conn()
        try:
            payload = base_payload(self.resource_relpath)
            payload["tasks"][0]["task_kind"] = "multiple_choice"
            with self.assertRaisesRegex(ValueError, "is not allowed for practical tasks"):
                validate_batch(conn, payload)
        finally:
            conn.close()

    def test_short_answer_task_kind_is_rejected(self):
        conn = make_conn()
        try:
            payload = base_payload(self.resource_relpath)
            payload["tasks"][0]["task_kind"] = "short_answer"
            with self.assertRaisesRegex(ValueError, "is not allowed for practical tasks"):
                validate_batch(conn, payload)
        finally:
            conn.close()

    def test_grading_mode_not_manual_is_rejected(self):
        conn = make_conn()
        try:
            payload = base_payload(self.resource_relpath)
            payload["tasks"][0]["grading_mode"] = "auto"
            with self.assertRaisesRegex(ValueError, "grading_mode must be 'manual'"):
                validate_batch(conn, payload)
        finally:
            conn.close()

    def test_missing_grading_mode_is_rejected(self):
        conn = make_conn()
        try:
            payload = base_payload(self.resource_relpath)
            del payload["tasks"][0]["grading_mode"]
            with self.assertRaisesRegex(ValueError, "grading_mode must be 'manual'"):
                validate_batch(conn, payload)
        finally:
            conn.close()

    def test_absolute_resource_path_is_rejected(self):
        conn = make_conn()
        try:
            payload = base_payload("/etc/passwd")
            with self.assertRaisesRegex(ValueError, "must be relative"):
                validate_batch(conn, payload)
        finally:
            conn.close()

    def test_path_traversal_is_rejected(self):
        conn = make_conn()
        try:
            payload = base_payload("../../etc/passwd")
            with self.assertRaisesRegex(
                ValueError, "must not traverse upward|must not contain '\\.\\.'"
            ):
                validate_batch(conn, payload)
        finally:
            conn.close()

    def test_resource_outside_allowed_root_is_rejected(self):
        conn = make_conn()
        try:
            outside = Path("some_other_dir/file.xlsx")
            outside.parent.mkdir(parents=True, exist_ok=True)
            outside.write_bytes(b"x")
            payload = base_payload(str(outside))
            with self.assertRaisesRegex(ValueError, "must resolve under one of"):
                validate_batch(conn, payload)
        finally:
            conn.close()

    def test_missing_resource_file_is_rejected(self):
        conn = make_conn()
        try:
            missing = Path("data/reference/may_2025_v2/practical/does_not_exist.xlsx")
            payload = base_payload(str(missing))
            with self.assertRaisesRegex(ValueError, "does not exist"):
                validate_batch(conn, payload)
        finally:
            conn.close()

    def test_points_must_match_skeleton(self):
        conn = make_conn()
        try:
            payload = base_payload(self.resource_relpath)
            payload["tasks"][0]["points"] = 99
            with self.assertRaisesRegex(ValueError, "do not match expected points"):
                validate_batch(conn, payload)
        finally:
            conn.close()

    def test_task_kind_must_match_skeleton(self):
        conn = make_conn()
        try:
            payload = base_payload(self.resource_relpath)
            payload["tasks"][0]["task_kind"] = "practical_graphics"
            payload["tasks"][0]["points"] = 20
            with self.assertRaisesRegex(
                ValueError, "does not match expected task_kind"
            ):
                validate_batch(conn, payload)
        finally:
            conn.close()

    def test_prompt_or_instructions_required(self):
        conn = make_conn()
        try:
            payload = base_payload(self.resource_relpath)
            del payload["tasks"][0]["prompt_bg"]
            with self.assertRaisesRegex(
                ValueError, "prompt_bg or instructions_bg is required"
            ):
                validate_batch(conn, payload)
        finally:
            conn.close()

    def test_instructions_bg_satisfies_prompt_requirement(self):
        conn = make_conn()
        try:
            payload = base_payload(self.resource_relpath)
            del payload["tasks"][0]["prompt_bg"]
            payload["tasks"][0]["instructions_bg"] = "Подробни инструкции..."
            before = conn.total_changes
            summary = validate_batch(conn, payload)
            self.assertEqual(summary.tasks_read, 1)
            self.assertEqual(conn.total_changes, before)
        finally:
            conn.close()

    def test_duplicate_task_number_is_rejected(self):
        conn = make_conn()
        try:
            payload = base_payload(self.resource_relpath)
            payload["tasks"].append(copy.deepcopy(payload["tasks"][0]))
            with self.assertRaisesRegex(ValueError, "duplicate task_number"):
                validate_batch(conn, payload)
        finally:
            conn.close()

    def test_three_practical_tasks_pass_together(self):
        conn = make_conn()
        try:
            graphics_resource = self.reference_root / "Background.png"
            graphics_resource.write_bytes(b"PNG")
            web_resource = self.reference_root / "site.zip"
            web_resource.write_bytes(b"PK zip")

            payload = {
                "source_slug": "may_2025_v2",
                "tasks": [
                    {
                        "task_number": 26,
                        "task_kind": "practical_spreadsheet",
                        "points": 15,
                        "prompt_bg": "Spreadsheet...",
                        "grading_mode": "manual",
                        "resource_files": [str(self.resource_file)],
                    },
                    {
                        "task_number": 27,
                        "task_kind": "practical_graphics",
                        "points": 20,
                        "prompt_bg": "Graphics...",
                        "grading_mode": "manual",
                        "resource_files": [str(graphics_resource)],
                    },
                    {
                        "task_number": 28,
                        "task_kind": "practical_web",
                        "points": 20,
                        "prompt_bg": "Web...",
                        "grading_mode": "manual",
                        "resource_files": [str(web_resource)],
                    },
                ],
            }
            summary = validate_batch(conn, payload)
            self.assertEqual(summary.tasks_read, 3)
            self.assertEqual(summary.resource_files_checked, 3)
        finally:
            conn.close()

    def test_resource_files_optional(self):
        conn = make_conn()
        try:
            payload = base_payload(self.resource_relpath)
            del payload["tasks"][0]["resource_files"]
            summary = validate_batch(conn, payload)
            self.assertEqual(summary.resource_files_checked, 0)
        finally:
            conn.close()

    def test_resource_files_must_be_list(self):
        conn = make_conn()
        try:
            payload = base_payload(self.resource_relpath)
            payload["tasks"][0]["resource_files"] = "data/reference/foo.xlsx"
            with self.assertRaisesRegex(ValueError, "resource_files must be a list"):
                validate_batch(conn, payload)
        finally:
            conn.close()

    def test_readonly_uri_uses_sqlite_readonly_mode(self):
        self.assertEqual(
            readonly_uri(Path("data/questions.db")),
            "file:data/questions.db?mode=ro",
        )

    def test_validate_batch_does_not_modify_db(self):
        conn = make_conn()
        try:
            before = conn.total_changes
            validate_batch(conn, base_payload(self.resource_relpath))
            self.assertEqual(conn.total_changes, before)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
