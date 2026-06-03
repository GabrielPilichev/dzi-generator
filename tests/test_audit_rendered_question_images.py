import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from src.audit_rendered_question_images import audit_rendered_question_images


ROOT = Path(__file__).resolve().parents[1]
TEMPLATES_DIR = ROOT / "web" / "templates"


def mapping_payload():
    return """{
  "mappings": [
    {
      "source_slug": "aug_2022_v2",
      "official_question_number": 2,
      "db_question_id": 182,
      "question_text_snippet": "На изображението е показана заявка.",
      "needs_image": true,
      "expected_asset_filename": "aug_2022_v2_q2.png",
      "pdf_page": 1,
      "confidence": "exact",
      "notes": "matched"
    }
  ],
  "explicit_no_image_notes": []
}
"""


def create_db(
    path: Path,
    *,
    include_asset: bool = True,
    include_question_link: bool = True,
    has_image: int = 1,
    has_assets: int = 1,
) -> None:
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
        conn.execute(
            """
            INSERT INTO questions (
                id, source_exam, source_number, prompt, has_image
            )
            VALUES (182, 'aug_2022_v2', 2, 'На изображението е показана заявка.', ?)
            """,
            (has_image,),
        )
        conn.execute(
            """
            INSERT INTO exams (
                id, subject, level, year, session, variant, format_version
            )
            VALUES (1, 'informatika_it', 'DZI', 2022, 'august', 2, 'dzi_it_pp_2025_format')
            """
        )
        conn.execute(
            """
            INSERT INTO exam_tasks (id, exam_id, task_number, has_assets)
            VALUES (10, 1, 2, ?)
            """,
            (has_assets,),
        )
        conn.execute(
            """
            INSERT INTO exam_task_questions (task_id, question_id, role)
            VALUES (10, 182, 'primary')
            """
        )
        if include_asset:
            conn.execute(
                """
                INSERT INTO assets (
                    id, asset_type, original_filename, local_path, mime_type, file_size
                )
                VALUES (
                    5,
                    'pdf_crop',
                    'aug_2022_v2_q2.png',
                    'web/static/img/questions/dzi/aug_2022_v2_q2.png',
                    'image/png',
                    8
                )
                """
            )
            if include_question_link:
                conn.execute(
                    """
                    INSERT INTO asset_links (
                        asset_id, owner_type, owner_id, role, display_order, caption_bg
                    )
                    VALUES (5, 'question', 182, 'pdf_crop', 0, 'DZI aug_2022_v2 Q2')
                    """
                )
            conn.execute(
                """
                INSERT INTO asset_links (
                    asset_id, owner_type, owner_id, role, display_order, caption_bg
                )
                VALUES (5, 'exam_task', 10, 'pdf_crop', 0, 'DZI aug_2022_v2 Q2')
                """
            )
        conn.commit()
    finally:
        conn.close()


class AuditRenderedQuestionImagesTest(unittest.TestCase):
    def test_complete_linked_mapping_passes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            old_cwd = Path.cwd()
            os.chdir(root)
            try:
                image_dir = Path("web/static/img/questions/dzi")
                image_dir.mkdir(parents=True)
                (image_dir / "aug_2022_v2_q2.png").write_bytes(b"fake png")
                mapping_path = Path("mapping.json")
                mapping_path.write_text(mapping_payload(), encoding="utf-8")
                db_path = Path("questions.db")
                create_db(db_path)

                errors, summary = audit_rendered_question_images(
                    db_path=db_path,
                    mapping_path=mapping_path,
                    image_dir=image_dir,
                    templates_dir=TEMPLATES_DIR,
                )
            finally:
                os.chdir(old_cwd)

        self.assertEqual(errors, [])
        self.assertEqual(summary["ready_mappings"], 1)
        self.assertEqual(summary["ready_mappings_linked"], 1)
        self.assertEqual(summary["assets_found"], 1)
        self.assertEqual(summary["assets_missing"], 0)
        self.assertEqual(summary["render_markup_checks"], 1)

    def test_missing_question_link_is_reported(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            old_cwd = Path.cwd()
            os.chdir(root)
            try:
                image_dir = Path("web/static/img/questions/dzi")
                image_dir.mkdir(parents=True)
                (image_dir / "aug_2022_v2_q2.png").write_bytes(b"fake png")
                mapping_path = Path("mapping.json")
                mapping_path.write_text(mapping_payload(), encoding="utf-8")
                db_path = Path("questions.db")
                create_db(db_path, include_question_link=False)

                errors, summary = audit_rendered_question_images(
                    db_path=db_path,
                    mapping_path=mapping_path,
                    image_dir=image_dir,
                    templates_dir=TEMPLATES_DIR,
                )
            finally:
                os.chdir(old_cwd)

        self.assertEqual(summary["ready_mappings"], 1)
        self.assertTrue(any("missing question asset_link" in error for error in errors))
        self.assertEqual(summary["validation_errors"], len(errors))

    def test_missing_unlinked_mapping_is_skipped(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            old_cwd = Path.cwd()
            os.chdir(root)
            try:
                image_dir = Path("web/static/img/questions/dzi")
                image_dir.mkdir(parents=True)
                mapping_path = Path("mapping.json")
                mapping_path.write_text(mapping_payload(), encoding="utf-8")
                db_path = Path("questions.db")
                create_db(db_path, include_asset=False, has_image=0, has_assets=0)

                errors, summary = audit_rendered_question_images(
                    db_path=db_path,
                    mapping_path=mapping_path,
                    image_dir=image_dir,
                    templates_dir=TEMPLATES_DIR,
                )
            finally:
                os.chdir(old_cwd)

        self.assertEqual(errors, [])
        self.assertEqual(summary["ready_mappings"], 1)
        self.assertEqual(summary["ready_mappings_linked"], 0)
        self.assertEqual(summary["assets_found"], 0)
        self.assertEqual(summary["assets_missing"], 1)
        self.assertEqual(summary["missing_skipped"], 1)
        self.assertEqual(summary["render_markup_checks"], 0)


if __name__ == "__main__":
    unittest.main()
