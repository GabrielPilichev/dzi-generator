import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from src.extract_dzi_image_candidates import (
    collect_extraction_plans,
    locate_pdf,
    main,
    mapping_pdf_page,
)
from tests.repo_relative_tempdir import RepoRelativeTemporaryDirectory


def payload():
    return {
        "mappings": [
            {
                "source_slug": "aug_2022_v2",
                "official_question_number": 2,
                "db_question_id": 182,
                "question_text_snippet": "На изображението е показана заявка.",
                "needs_image": True,
                "expected_asset_filename": "aug_2022_v2_q2.png",
                "confidence": "exact",
                "notes": "matched",
                "pdf_page": 3,
            },
            {
                "source_slug": "may_2025_v2",
                "official_question_number": 16,
                "db_question_id": 16,
                "question_text_snippet": "На изображението е показана диаграма.",
                "needs_image": True,
                "expected_asset_filename": "may_2025_v2_q16.png",
                "confidence": "likely",
                "notes": "matched",
            },
            {
                "source_slug": "may_2025_v2",
                "official_question_number": 13,
                "db_question_id": 13,
                "question_text_snippet": "No image.",
                "needs_image": False,
                "expected_asset_filename": None,
                "confidence": "exact",
                "notes": "explicit no-image note",
            },
        ],
        "explicit_no_image_notes": [],
    }


class ExtractDziImageCandidatesTest(unittest.TestCase):
    def test_collect_plans_uses_canonical_output_name_and_pdf_page(self):
        with RepoRelativeTemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            pdf_dir = root / "data" / "source_pdfs"
            image_dir = root / "web" / "static" / "img" / "questions" / "dzi"
            pdf_dir.mkdir(parents=True)
            (pdf_dir / "aug_2022_v2_exam.pdf").write_bytes(b"%PDF fake")

            plans = collect_extraction_plans(
                payload(),
                pdf_dir=pdf_dir,
                image_dir=image_dir,
                source_slug="aug_2022_v2",
                question=2,
            )

        self.assertEqual(len(plans), 1)
        self.assertEqual(plans[0].pdf_path.name, "aug_2022_v2_exam.pdf")
        self.assertEqual(plans[0].pdf_page, 3)
        self.assertEqual(plans[0].output_path.name, "aug_2022_v2_q2.png")
        self.assertIsNone(plans[0].skip_reason)
        self.assertFalse(plans[0].output_exists)

    def test_existing_png_is_marked_without_overwrite(self):
        with RepoRelativeTemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            pdf_dir = root / "data" / "source_pdfs"
            image_dir = root / "web" / "static" / "img" / "questions" / "dzi"
            pdf_dir.mkdir(parents=True)
            image_dir.mkdir(parents=True)
            (pdf_dir / "may_2025_v2_exam.pdf").write_bytes(b"%PDF fake")
            (image_dir / "may_2025_v2_q16.png").write_bytes(b"existing")

            plans = collect_extraction_plans(
                payload(),
                pdf_dir=pdf_dir,
                image_dir=image_dir,
                source_slug="may_2025_v2",
                question=16,
            )

        self.assertEqual(len(plans), 1)
        self.assertTrue(plans[0].output_exists)

    def test_locate_pdf_uses_only_exact_exam_filename(self):
        with RepoRelativeTemporaryDirectory() as tmpdir:
            pdf_dir = Path(tmpdir) / "data" / "source_pdfs"
            pdf_dir.mkdir(parents=True)
            fallback = pdf_dir / "may_2025_v2.pdf"
            fallback.write_bytes(b"%PDF fallback")

            pdf_path, issue = locate_pdf(pdf_dir, "may_2025_v2")

        self.assertIsNone(pdf_path)
        self.assertIn("may_2025_v2_exam.pdf", issue)

    def test_locate_pdf_accepts_exact_exam_filename(self):
        with RepoRelativeTemporaryDirectory() as tmpdir:
            pdf_dir = Path(tmpdir) / "data" / "source_pdfs"
            pdf_dir.mkdir(parents=True)
            exact = pdf_dir / "may_2025_v2_exam.pdf"
            exact.write_bytes(b"%PDF exact")

            pdf_path, issue = locate_pdf(pdf_dir, "may_2025_v2")

        self.assertEqual(pdf_path, exact)
        self.assertIsNone(issue)

    def test_missing_pdf_page_is_reported_in_dry_run(self):
        with RepoRelativeTemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            mapping_path = root / "mapping.json"
            image_dir = root / "web" / "static" / "img" / "questions" / "dzi"
            mapping_path.write_text(json.dumps(payload()), encoding="utf-8")
            stdout = io.StringIO()

            with redirect_stdout(stdout):
                exit_code = main([
                    "--mapping",
                    str(mapping_path),
                    "--pdf-dir",
                    str(root / "data" / "source_pdfs"),
                    "--image-dir",
                    str(image_dir),
                    "--source-slug",
                    "may_2025_v2",
                    "--question",
                    "16",
                    "--dry-run",
                ])

        self.assertEqual(exit_code, 0)
        self.assertIn("missing pdf_page in confirmed mapping", stdout.getvalue())
        self.assertIn("missing_pages: 1", stdout.getvalue())
        self.assertFalse(image_dir.exists())

    def test_absolute_or_traversal_image_dir_is_rejected_before_any_work(self):
        with RepoRelativeTemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            mapping_path = root / "mapping.json"
            mapping_path.write_text(json.dumps(payload()), encoding="utf-8")
            absolute = (root / "web" / "static" / "img" / "questions" / "dzi").resolve()
            cases = {
                str(absolute): "image-dir must be repo-relative",
                str(root / ".." / "escaped-images"): "parent path segments",
            }
            for image_dir, expected in cases.items():
                with self.subTest(image_dir=image_dir):
                    stderr = io.StringIO()
                    with redirect_stdout(io.StringIO()), redirect_stderr(stderr):
                        exit_code = main([
                            "--mapping", str(mapping_path),
                            "--pdf-dir", str(root / "data" / "source_pdfs"),
                            "--image-dir", image_dir,
                            "--source-slug", "may_2025_v2",
                            "--question", "16",
                            "--dry-run",
                        ])
                    self.assertEqual(exit_code, 1)
                    self.assertIn(expected, stderr.getvalue())
            self.assertFalse(absolute.exists())
            self.assertFalse((root.resolve().parent / "escaped-images").exists())

    def test_pdf_page_accepts_numeric_strings(self):
        self.assertEqual(mapping_pdf_page({"pdf_page": "5"}), 5)
        self.assertIsNone(mapping_pdf_page({"pdf_page": 0}))
        self.assertIsNone(mapping_pdf_page({"source_page": 5}))


if __name__ == "__main__":
    unittest.main()
