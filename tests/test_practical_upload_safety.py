import inspect
import tempfile
import unittest
from pathlib import Path

import src.practical_uploads as practical_uploads_module
from src.practical_uploads import (
    ALLOWED_EXTENSIONS,
    DANGEROUS_EXTENSIONS,
    MAX_FILENAME_LENGTH,
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

    # ------------------------------------------------------------------
    # Tester-reported scenarios: double extensions, ZIPs, ZIP bombs
    # ------------------------------------------------------------------

    def test_virus_pdf_dot_exe_double_extension_rejected(self):
        with self.assertRaises(PracticalUploadError):
            validate_upload_extension("virus.pdf.exe")

    def test_archive_zip_dot_exe_double_extension_rejected(self):
        with self.assertRaises(PracticalUploadError):
            validate_upload_extension("archive.zip.exe")

    def test_normal_zip_extension_accepted(self):
        self.assertEqual(validate_upload_extension("normal.zip"), ".zip")
        self.assertEqual(sanitize_original_filename("normal.zip"), "normal.zip")

    def test_uppercase_double_extension_rejected(self):
        # Defense-in-depth: case must not bypass the dangerous-suffix check.
        with self.assertRaises(PracticalUploadError):
            validate_upload_extension("Resume.PDF.EXE")

    def test_dangerous_and_allowed_extension_sets_are_disjoint(self):
        # Protects against future edits accidentally allowing an executable.
        self.assertFalse(ALLOWED_EXTENSIONS & DANGEROUS_EXTENSIONS)

    def test_oversized_zip_rejected(self):
        with self.assertRaises(PracticalUploadError):
            validate_upload_size(MAX_UPLOAD_BYTES + 1)

    def test_filename_length_limit_enforced(self):
        too_long = ("a" * (MAX_FILENAME_LENGTH + 1)) + ".xlsx"
        with self.assertRaises(PracticalUploadError):
            sanitize_original_filename(too_long)

    def test_helper_source_never_extracts_zip_archives(self):
        # The practical upload pipeline must store ZIPs as opaque blobs.
        # Importing zipfile or invoking extract* in this module would be
        # a regression that opens zip-bomb / zip-slip risks.
        source = inspect.getsource(practical_uploads_module)
        self.assertNotIn("zipfile", source)
        self.assertNotIn("extractall", source)
        self.assertNotIn("ZipFile", source)


if __name__ == "__main__":
    unittest.main()
