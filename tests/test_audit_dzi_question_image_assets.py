import io
import struct
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from src.audit_dzi_question_image_assets import (
    PNG_SIGNATURE,
    audit_image_assets,
    main,
    read_png_dimensions,
)


def png_header(width: int, height: int) -> bytes:
    return PNG_SIGNATURE + struct.pack(">I4sII", 13, b"IHDR", width, height)


def payload():
    return {
        "mappings": [
            {
                "source_slug": "aug_2022_v2",
                "official_question_number": 2,
                "db_question_id": 182,
                "needs_image": True,
                "expected_asset_filename": "aug_2022_v2_q2.png",
                "confidence": "exact",
            },
            {
                "source_slug": "aug_2022_v2",
                "official_question_number": 12,
                "db_question_id": 836,
                "needs_image": True,
                "expected_asset_filename": "aug_2022_v2_q12.png",
                "confidence": "exact",
            },
        ],
        "explicit_no_image_notes": [],
    }


class AuditDziQuestionImageAssetsTest(unittest.TestCase):
    def test_png_dimensions_are_read_from_header(self):
        with tempfile.TemporaryDirectory(dir=".") as tmpdir:
            path = Path(tmpdir) / "sample.png"
            path.write_bytes(png_header(640, 480))

            width, height = read_png_dimensions(path)

        self.assertEqual((width, height), (640, 480))

    def test_audit_reports_dimensions_mapping_status_and_suspicious_flags(self):
        with tempfile.TemporaryDirectory(dir=".") as tmpdir:
            image_dir = Path(tmpdir) / "web" / "static" / "img" / "questions" / "dzi"
            image_dir.mkdir(parents=True)
            (image_dir / "aug_2022_v2_q2.png").write_bytes(png_header(100, 300))
            (image_dir / ".DS_Store").write_bytes(b"metadata")

            report = audit_image_assets(
                payload(),
                image_dir,
                min_width=200,
                min_height=200,
                min_file_size=1,
            )

        rows = {row.filename: row for row in report.rows}
        self.assertEqual(report.expected_count, 2)
        self.assertEqual(report.present_expected_count, 1)
        self.assertEqual(report.unexpected_count, 1)
        self.assertEqual(report.missing_expected, ["aug_2022_v2_q12.png"])
        self.assertEqual(rows["aug_2022_v2_q2.png"].dimensions_label, "100x300")
        self.assertEqual(rows["aug_2022_v2_q2.png"].mapping_status, "matches confirmed mapping")
        self.assertIn("small_dimensions", rows["aug_2022_v2_q2.png"].suspicious)
        self.assertEqual(rows[".DS_Store"].dimensions_label, "n/a")
        self.assertEqual(rows[".DS_Store"].mapping_status, "not in confirmed mapping")
        self.assertIn("not_png", rows[".DS_Store"].suspicious)

    def test_main_prints_read_only_report(self):
        with tempfile.TemporaryDirectory(dir=".") as tmpdir:
            root = Path(tmpdir)
            image_dir = root / "web" / "static" / "img" / "questions" / "dzi"
            image_dir.mkdir(parents=True)
            (image_dir / "aug_2022_v2_q2.png").write_bytes(png_header(640, 480))
            mapping_path = root / "mapping.json"
            mapping_path.write_text(
                """
                {
                  "mappings": [
                    {
                      "source_slug": "aug_2022_v2",
                      "official_question_number": 2,
                      "db_question_id": 182,
                      "needs_image": true,
                      "expected_asset_filename": "aug_2022_v2_q2.png",
                      "confidence": "exact"
                    }
                  ],
                  "explicit_no_image_notes": []
                }
                """,
                encoding="utf-8",
            )
            stdout = io.StringIO()

            with redirect_stdout(stdout):
                exit_code = main([
                    "--mapping",
                    str(mapping_path),
                    "--image-dir",
                    str(image_dir),
                    "--min-file-size",
                    "1",
                ])

        self.assertEqual(exit_code, 0)
        output = stdout.getvalue()
        self.assertIn("DZI question image QA report", output)
        self.assertIn("aug_2022_v2_q2.png | dimensions=640x480", output)
        self.assertIn("mapping=matches confirmed mapping", output)


if __name__ == "__main__":
    unittest.main()
