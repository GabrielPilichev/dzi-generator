import unittest
from pathlib import Path

from src.validate_confirmed_dzi_image_files import validate_image_dir, validate_image_files
from tests.repo_relative_tempdir import RepoRelativeTemporaryDirectory


def payload(filename="aug_2022_v2_q2.png"):
    return {
        "mappings": [
            {
                "source_slug": "aug_2022_v2",
                "official_question_number": 2,
                "db_question_id": 182,
                "question_text_snippet": "На изображението е показана заявка.",
                "needs_image": True,
                "expected_asset_filename": filename,
                "confidence": "exact",
                "notes": "matched",
            }
        ],
        "explicit_no_image_notes": [
            {
                "source_slug": "aug_2022_v2",
                "official_question_number": 11,
                "db_question_id": 835,
                "question_text_snippet": "No image.",
                "needs_image": False,
                "expected_asset_filename": None,
                "confidence": "exact",
                "notes": "explicit no-image note",
            }
        ],
    }


class ValidateConfirmedDziImageFilesTest(unittest.TestCase):
    def test_missing_files_are_reported_but_non_blocking_by_default(self):
        with RepoRelativeTemporaryDirectory() as tmpdir:
            image_dir = Path(tmpdir) / "web" / "static" / "img" / "questions" / "dzi"

            errors, summary, missing, unexpected = validate_image_files(
                payload(),
                image_dir,
                require_files=False,
            )

        self.assertEqual(errors, [])
        self.assertEqual(summary["ready_mappings"], 1)
        self.assertEqual(summary["files_present"], 0)
        self.assertEqual(summary["files_missing"], 1)
        self.assertEqual(summary["unexpected_files"], 0)
        self.assertEqual(missing, [str(image_dir / "aug_2022_v2_q2.png")])
        self.assertEqual(unexpected, [])

    def test_require_files_makes_missing_images_blocking(self):
        with RepoRelativeTemporaryDirectory() as tmpdir:
            image_dir = Path(tmpdir) / "web" / "static" / "img" / "questions" / "dzi"

            errors, summary, missing, unexpected = validate_image_files(
                payload(),
                image_dir,
                require_files=True,
            )

        self.assertEqual(summary["files_missing"], 1)
        self.assertEqual(missing, [str(image_dir / "aug_2022_v2_q2.png")])
        self.assertEqual(unexpected, [])
        self.assertIn(
            f"aug_2022_v2 Q2: missing expected image file: {image_dir / 'aug_2022_v2_q2.png'}",
            errors,
        )

    def test_existing_file_counts_as_present(self):
        with RepoRelativeTemporaryDirectory() as tmpdir:
            image_dir = Path(tmpdir) / "web" / "static" / "img" / "questions" / "dzi"
            image_dir.mkdir(parents=True)
            (image_dir / "aug_2022_v2_q2.png").write_bytes(b"fake png")

            errors, summary, missing, unexpected = validate_image_files(
                payload(),
                image_dir,
                require_files=True,
            )

        self.assertEqual(errors, [])
        self.assertEqual(missing, [])
        self.assertEqual(unexpected, [])
        self.assertEqual(summary["files_present"], 1)
        self.assertEqual(summary["files_missing"], 0)

    def test_filename_must_match_mapping_convention(self):
        errors, _, _, _ = validate_image_files(
            payload("aug_2022_v2_q3.png"),
            Path("web/static/img/questions/dzi"),
            require_files=False,
        )

        self.assertIn(
            "aug_2022_v2 Q2: expected_asset_filename must be aug_2022_v2_q2.png, got aug_2022_v2_q3.png",
            errors,
        )

    def test_path_traversal_filename_is_rejected(self):
        errors, _, _, _ = validate_image_files(
            payload("../aug_2022_v2_q2.png"),
            Path("web/static/img/questions/dzi"),
            require_files=False,
        )

        self.assertTrue(any("must be a filename, not a path" in error for error in errors))
        self.assertTrue(any("path segment" in error for error in errors))

    def test_absolute_image_dir_is_rejected_even_when_it_exists(self):
        with RepoRelativeTemporaryDirectory() as tmpdir:
            absolute = (Path(tmpdir) / "web" / "static" / "img" / "questions" / "dzi").resolve()
            absolute.mkdir(parents=True)
            (absolute / "aug_2022_v2_q2.png").write_bytes(b"fake png")
            self.assertTrue(absolute.is_absolute())

            errors, _, _, _ = validate_image_files(payload(), absolute, require_files=False)

        self.assertIn("image-dir must be repo-relative", errors)

    def test_image_dir_rejects_absolute_and_traversal_paths(self):
        for candidate in (Path("/tmp/learnpilot-images"), Path("/etc"), Path.cwd() / "web" / "static"):
            with self.subTest(candidate=str(candidate)):
                self.assertIn("image-dir must be repo-relative", validate_image_dir(candidate))
        for candidate in (Path("../outside"), Path("web/../../outside"), Path("web/static/..")):
            with self.subTest(candidate=str(candidate)):
                self.assertIn(
                    "image-dir must not contain empty/current/parent path segments",
                    validate_image_dir(candidate),
                )
        self.assertEqual(validate_image_dir(Path("web/static/img/questions/dzi")), [])

    def test_image_dir_must_stay_out_of_generated_quizzes(self):
        errors, _, _, _ = validate_image_files(
            payload(),
            Path("vault/Generated/Quizzes/questions"),
            require_files=False,
        )

        self.assertTrue(any("vault/Generated/Quizzes" in error for error in errors))

    def test_unexpected_files_are_reported(self):
        with RepoRelativeTemporaryDirectory() as tmpdir:
            image_dir = Path(tmpdir) / "web" / "static" / "img" / "questions" / "dzi"
            image_dir.mkdir(parents=True)
            (image_dir / "unexpected.png").write_bytes(b"fake png")

            errors, summary, missing, unexpected = validate_image_files(
                payload(),
                image_dir,
                require_files=False,
            )

        self.assertEqual(errors, [])
        self.assertEqual(summary["unexpected_files"], 1)
        self.assertEqual(unexpected, [str(image_dir / "unexpected.png")])
        self.assertEqual(summary["files_missing"], 1)


if __name__ == "__main__":
    unittest.main()
