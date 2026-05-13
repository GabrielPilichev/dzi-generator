import tempfile
import unittest
from pathlib import Path

from src.practical_uploads import (
    ALLOWED_EXTENSIONS,
    MAX_UPLOAD_BYTES,
    PracticalUploadError,
    build_practical_upload_path,
    sanitize_original_filename,
    validate_upload_extension,
    validate_upload_size,
)


class PracticalUploadSafetyTest(unittest.TestCase):
    def test_normal_filename_accepted(self):
        self.assertEqual(sanitize_original_filename("result.xlsx"), "result.xlsx")
        self.assertEqual(validate_upload_extension("result.xlsx"), ".xlsx")

    def test_cyrillic_filename_accepted_safely(self):
        filename = "решение задача 26.xlsx"

        self.assertEqual(sanitize_original_filename(filename), filename)
        self.assertEqual(validate_upload_extension(filename), ".xlsx")

    def test_path_traversal_filename_uses_safe_basename(self):
        self.assertEqual(sanitize_original_filename("../../evil.xlsx"), "evil.xlsx")
        self.assertEqual(sanitize_original_filename("..\\..\\evil.xlsx"), "evil.xlsx")

    def test_empty_filename_rejected(self):
        for filename in ("", "   ", ".", "..", "////"):
            with self.subTest(filename=filename):
                with self.assertRaises(PracticalUploadError):
                    sanitize_original_filename(filename)

    def test_dangerous_extension_rejected(self):
        for filename in ("payload.exe", "script.sh", "tool.py", "shell.PHP"):
            with self.subTest(filename=filename):
                with self.assertRaises(PracticalUploadError):
                    validate_upload_extension(filename)

    def test_uppercase_allowed_extension_accepted(self):
        self.assertEqual(validate_upload_extension("PHOTO.JPG"), ".jpg")
        self.assertEqual(validate_upload_extension("INDEX.HTML"), ".html")

    def test_allowed_extension_set_is_conservative(self):
        self.assertIn(".xlsx", ALLOWED_EXTENSIONS)
        self.assertIn(".zip", ALLOWED_EXTENSIONS)
        self.assertNotIn(".exe", ALLOWED_EXTENSIONS)
        self.assertNotIn(".php", ALLOWED_EXTENSIONS)

    def test_oversized_file_rejected(self):
        with self.assertRaises(PracticalUploadError):
            validate_upload_size(MAX_UPLOAD_BYTES + 1)

    def test_zero_byte_file_rejected(self):
        with self.assertRaises(PracticalUploadError):
            validate_upload_size(0)

    def test_generated_stored_path_stays_under_configured_base_directory(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            upload_root = Path(temp_dir) / "uploads" / "practical"
            result = build_practical_upload_path(
                upload_root=upload_root,
                attempt_id=12,
                exam_task_id=26,
                submission_id=5,
                original_filename="solution.xlsx",
                size_bytes=128,
                token="abc123",
            )

            self.assertEqual(result.stored_path, "data/uploads/practical/12/26/5/abc123.xlsx")
            self.assertEqual(result.absolute_path, upload_root.resolve() / "12" / "26" / "5" / "abc123.xlsx")

    def test_generated_stored_filename_does_not_equal_raw_original_filename(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            result = build_practical_upload_path(
                upload_root=Path(temp_dir) / "uploads",
                attempt_id=1,
                exam_task_id=26,
                submission_id=1,
                original_filename="abc123.xlsx",
                size_bytes=1,
                token="different",
            )

            self.assertEqual(result.original_filename, "abc123.xlsx")
            self.assertEqual(result.stored_filename, "different.xlsx")
            self.assertNotEqual(result.stored_filename, result.original_filename)

    def test_path_generation_does_not_create_persistent_directories(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            upload_root = Path(temp_dir) / "uploads" / "practical"
            result = build_practical_upload_path(
                upload_root=upload_root,
                attempt_id=1,
                exam_task_id=28,
                submission_id=3,
                original_filename="site.zip",
                size_bytes=10,
                token="sitebundle",
            )

            self.assertFalse(upload_root.exists())
            self.assertFalse(result.absolute_path.exists())

    def test_unsafe_identifier_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaises(PracticalUploadError):
                build_practical_upload_path(
                    upload_root=Path(temp_dir) / "uploads",
                    attempt_id="../1",
                    exam_task_id=26,
                    submission_id=1,
                    original_filename="solution.xlsx",
                    size_bytes=10,
                    token="abc",
                )


if __name__ == "__main__":
    unittest.main()
