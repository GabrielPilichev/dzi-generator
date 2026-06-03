import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from src.apply_confirmed_dzi_image_mappings import main


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
                "pdf_page": 1,
                "confidence": "exact",
                "notes": "matched",
            },
            {
                "source_slug": "aug_2022_v2",
                "official_question_number": 12,
                "db_question_id": 836,
                "question_text_snippet": "Дадена е таблица.",
                "needs_image": True,
                "expected_asset_filename": "aug_2022_v2_q12.png",
                "pdf_page": 4,
                "confidence": "exact",
                "notes": "matched",
            },
        ],
        "explicit_no_image_notes": [],
    }


def create_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            """
            CREATE TABLE questions (
                id INTEGER PRIMARY KEY,
                source_exam TEXT NOT NULL,
                source_number INTEGER,
                prompt TEXT NOT NULL,
                has_image INTEGER DEFAULT 0,
                image_path TEXT
            );
            CREATE TABLE exams (
                id INTEGER PRIMARY KEY,
                subject TEXT NOT NULL,
                level TEXT NOT NULL,
                year INTEGER NOT NULL,
                session TEXT,
                variant INTEGER DEFAULT 1,
                format_version TEXT
            );
            CREATE TABLE exam_tasks (
                id INTEGER PRIMARY KEY,
                exam_id INTEGER NOT NULL,
                task_number INTEGER NOT NULL,
                has_assets INTEGER DEFAULT 0
            );
            CREATE TABLE exam_task_questions (
                task_id INTEGER NOT NULL,
                question_id INTEGER NOT NULL,
                role TEXT NOT NULL DEFAULT 'primary'
            );
            CREATE TABLE assets (
                id INTEGER PRIMARY KEY,
                asset_type TEXT NOT NULL,
                original_filename TEXT,
                local_path TEXT NOT NULL UNIQUE,
                source_url TEXT,
                sha256 TEXT,
                mime_type TEXT,
                file_size INTEGER,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                notes TEXT
            );
            CREATE TABLE asset_links (
                id INTEGER PRIMARY KEY,
                asset_id INTEGER NOT NULL,
                owner_type TEXT NOT NULL,
                owner_id INTEGER NOT NULL,
                role TEXT,
                display_order INTEGER DEFAULT 0,
                caption_bg TEXT,
                source_page INTEGER,
                source_bbox_json TEXT
            );
            """
        )
        conn.executemany(
            """
            INSERT INTO questions (
                id, source_exam, source_number, prompt, has_image
            )
            VALUES (?, 'aug_2022_v2', ?, ?, 0)
            """,
            [
                (182, 2, "На изображението е показана заявка."),
                (836, 12, "Дадена е таблица."),
            ],
        )
        conn.execute(
            """
            INSERT INTO exams (
                id, subject, level, year, session, variant, format_version
            )
            VALUES (1, 'informatika_it', 'DZI', 2022, 'august', 2, 'dzi_it_pp_2025_format')
            """
        )
        conn.executemany(
            """
            INSERT INTO exam_tasks (id, exam_id, task_number, has_assets)
            VALUES (?, 1, ?, 0)
            """,
            [(20, 2), (120, 12)],
        )
        conn.executemany(
            """
            INSERT INTO exam_task_questions (task_id, question_id, role)
            VALUES (?, ?, 'primary')
            """,
            [(20, 182), (120, 836)],
        )
        conn.commit()
    finally:
        conn.close()


class ApplyConfirmedDziImageMappingsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=".")
        self.root = Path(self.tmp.name)
        self.db_path = self.root / "questions.db"
        self.mapping_path = self.root / "mapping.json"
        self.image_dir = self.root / "web" / "static" / "img" / "questions" / "dzi"
        self.image_dir.mkdir(parents=True)
        self.mapping_path.write_text(json.dumps(payload()), encoding="utf-8")
        (self.image_dir / "aug_2022_v2_q2.png").write_bytes(b"fake png")
        create_db(self.db_path)

    def tearDown(self):
        self.tmp.cleanup()

    def run_main(self, *args: str) -> tuple[int, str]:
        stdout = io.StringIO()
        argv = [
            "--db",
            str(self.db_path),
            "--mapping",
            str(self.mapping_path),
            "--image-dir",
            str(self.image_dir),
            *args,
        ]
        with redirect_stdout(stdout):
            exit_code = main(argv)
        return exit_code, stdout.getvalue()

    def test_default_apply_links_available_and_skips_missing(self):
        exit_code, output = self.run_main()

        self.assertEqual(exit_code, 0)
        self.assertIn("available mappings processed: 1", output)
        self.assertIn("missing/skipped count: 1", output)
        self.assertIn("aug_2022_v2 Q12", output)

        conn = sqlite3.connect(self.db_path)
        try:
            asset_count = conn.execute("SELECT COUNT(*) FROM assets").fetchone()[0]
            link_count = conn.execute("SELECT COUNT(*) FROM asset_links").fetchone()[0]
            q2_has_image = conn.execute("SELECT has_image FROM questions WHERE id = 182").fetchone()[0]
            q12_has_image = conn.execute("SELECT has_image FROM questions WHERE id = 836").fetchone()[0]
            q2_task_has_assets = conn.execute("SELECT has_assets FROM exam_tasks WHERE id = 20").fetchone()[0]
            q12_task_has_assets = conn.execute("SELECT has_assets FROM exam_tasks WHERE id = 120").fetchone()[0]
        finally:
            conn.close()

        self.assertEqual(asset_count, 1)
        self.assertEqual(link_count, 2)
        self.assertEqual(q2_has_image, 1)
        self.assertEqual(q12_has_image, 0)
        self.assertEqual(q2_task_has_assets, 1)
        self.assertEqual(q12_task_has_assets, 0)

        second_exit_code, second_output = self.run_main()

        self.assertEqual(second_exit_code, 0)
        self.assertIn("rows inserted/updated: 0", second_output)

    def test_check_reports_linked_skipped_and_unlinked_available_counts(self):
        apply_exit_code, _ = self.run_main()
        self.assertEqual(apply_exit_code, 0)

        check_exit_code, output = self.run_main("--check")

        self.assertEqual(check_exit_code, 0)
        self.assertIn("linked count: 1", output)
        self.assertIn("missing/skipped count: 1", output)
        self.assertIn("unlinked available count: 0", output)

    def test_strict_fails_when_expected_file_is_missing(self):
        exit_code, output = self.run_main("--strict")

        self.assertEqual(exit_code, 1)
        self.assertIn("missing expected image file", output)
        self.assertIn("aug_2022_v2_q12.png", output)

        conn = sqlite3.connect(self.db_path)
        try:
            asset_count = conn.execute("SELECT COUNT(*) FROM assets").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(asset_count, 0)


if __name__ == "__main__":
    unittest.main()
