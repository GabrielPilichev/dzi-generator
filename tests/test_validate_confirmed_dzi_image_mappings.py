import sqlite3
import unittest

from src.validate_confirmed_dzi_image_mappings import (
    collect_ready_question_ids,
    fetch_question_rows,
    validate_entries,
)


def make_conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE questions (
            id INTEGER PRIMARY KEY,
            source_exam TEXT NOT NULL,
            source_number INTEGER,
            prompt TEXT NOT NULL
        );
        """
    )
    conn.executemany(
        """
        INSERT INTO questions (id, source_exam, source_number, prompt)
        VALUES (?, ?, ?, ?)
        """,
        [
            (182, "aug_2022_v2", 2, "На изображението е показана заявка."),
            (836, "aug_2022_v2", 12, "Дадена е таблица."),
            (240, "aug_2023_v2", 20, "Следните таблици са част от база данни."),
        ],
    )
    return conn


def validate_payload(payload):
    conn = make_conn()
    try:
        question_ids = collect_ready_question_ids(payload)
        question_rows = fetch_question_rows(conn, question_ids)
        return validate_entries(payload, question_rows)
    finally:
        conn.close()


class ValidateConfirmedDziImageMappingsTest(unittest.TestCase):
    def test_valid_exact_mapping_passes(self):
        payload = {
            "mappings": [
                {
                    "source_slug": "aug_2022_v2",
                    "official_question_number": 2,
                    "db_question_id": 182,
                    "question_text_snippet": "snippet",
                    "needs_image": True,
                    "expected_asset_filename": "aug_2022_v2_q2.png",
                    "pdf_page": 1,
                    "confidence": "exact",
                    "notes": "matched",
                }
            ],
            "explicit_no_image_notes": [
                {
                    "source_slug": "aug_2022_v2",
                    "official_question_number": 12,
                    "db_question_id": 836,
                    "question_text_snippet": "snippet",
                    "needs_image": False,
                    "expected_asset_filename": None,
                    "confidence": "exact",
                    "notes": "explicit no-image note",
                }
            ],
        }

        errors, summary = validate_payload(payload)

        self.assertEqual(errors, [])
        self.assertEqual(summary["total_entries"], 2)
        self.assertEqual(summary["exact"], 2)
        self.assertEqual(summary["ready_mapping_count"], 1)

    def test_ready_image_mapping_requires_pdf_page(self):
        payload = {
            "mappings": [
                {
                    "source_slug": "aug_2022_v2",
                    "official_question_number": 2,
                    "db_question_id": 182,
                    "needs_image": True,
                    "expected_asset_filename": "aug_2022_v2_q2.png",
                    "confidence": "exact",
                    "notes": "matched",
                },
                {
                    "source_slug": "aug_2023_v2",
                    "official_question_number": 20,
                    "db_question_id": 240,
                    "needs_image": True,
                    "expected_asset_filename": "aug_2023_v2_q20.png",
                    "pdf_page": 0,
                    "confidence": "likely",
                    "notes": "matched",
                },
            ]
        }

        errors, _ = validate_payload(payload)

        self.assertIn("mappings[1] aug_2022_v2 Q2: pdf_page is required for ready image mappings", errors)
        self.assertIn("mappings[2] aug_2023_v2 Q20: pdf_page must be positive", errors)

    def test_expected_asset_filename_must_match_source_and_number(self):
        payload = {
            "mappings": [
                {
                    "source_slug": "aug_2022_v2",
                    "official_question_number": 2,
                    "db_question_id": 182,
                    "needs_image": True,
                    "expected_asset_filename": "aug_2022_v2_q3.png",
                    "confidence": "exact",
                    "notes": "matched",
                }
            ]
        }

        errors, _ = validate_payload(payload)

        self.assertIn(
            "mappings[1] aug_2022_v2 Q2: expected_asset_filename must be aug_2022_v2_q2.png, got aug_2022_v2_q3.png",
            errors,
        )

    def test_filename_path_traversal_is_rejected(self):
        payload = {
            "mappings": [
                {
                    "source_slug": "..",
                    "official_question_number": 2,
                    "db_question_id": 182,
                    "needs_image": True,
                    "expected_asset_filename": "../q2.png",
                    "confidence": "likely",
                    "notes": "matched",
                }
            ]
        }

        errors, _ = validate_payload(payload)

        self.assertTrue(any("must be a filename, not a path" in error for error in errors))
        self.assertTrue(any("path segment" in error for error in errors))

    def test_missing_and_ambiguous_entries_are_blocking_errors(self):
        payload = {
            "mappings": [
                {
                    "source_slug": "aug_2022_v2",
                    "official_question_number": 99,
                    "db_question_id": None,
                    "needs_image": True,
                    "expected_asset_filename": "aug_2022_v2_q99.png",
                    "confidence": "missing",
                    "notes": "not found",
                },
                {
                    "source_slug": "aug_2023_v2",
                    "official_question_number": 20,
                    "db_question_id": None,
                    "needs_image": True,
                    "expected_asset_filename": "aug_2023_v2_q20.png",
                    "confidence": "ambiguous",
                    "notes": "multiple",
                },
            ]
        }

        errors, summary = validate_payload(payload)

        self.assertEqual(summary["missing"], 1)
        self.assertEqual(summary["ambiguous"], 1)
        self.assertIn("mappings[1] aug_2022_v2 Q99: confidence is missing; resolve before asset import", errors)
        self.assertIn("mappings[2] aug_2023_v2 Q20: confidence is ambiguous; resolve before asset import", errors)

    def test_exact_mapping_requires_existing_matching_db_question(self):
        payload = {
            "mappings": [
                {
                    "source_slug": "aug_2022_v2",
                    "official_question_number": 2,
                    "db_question_id": 9999,
                    "needs_image": True,
                    "expected_asset_filename": "aug_2022_v2_q2.png",
                    "confidence": "exact",
                    "notes": "matched",
                },
                {
                    "source_slug": "aug_2022_v2",
                    "official_question_number": 12,
                    "db_question_id": 182,
                    "needs_image": True,
                    "expected_asset_filename": "aug_2022_v2_q12.png",
                    "confidence": "exact",
                    "notes": "wrong id",
                },
            ]
        }

        errors, _ = validate_payload(payload)

        self.assertIn("mappings[1] aug_2022_v2 Q2: db_question_id 9999 does not exist in data/questions.db", errors)
        self.assertIn("mappings[2] aug_2022_v2 Q12: exact db_question_id points to aug_2022_v2 Q2", errors)

    def test_duplicate_source_pair_and_db_conflict_are_rejected(self):
        payload = {
            "mappings": [
                {
                    "source_slug": "aug_2022_v2",
                    "official_question_number": 2,
                    "db_question_id": 182,
                    "needs_image": True,
                    "expected_asset_filename": "aug_2022_v2_q2.png",
                    "confidence": "exact",
                    "notes": "matched",
                },
                {
                    "source_slug": "aug_2022_v2",
                    "official_question_number": 2,
                    "db_question_id": 182,
                    "needs_image": True,
                    "expected_asset_filename": "aug_2022_v2_q2.png",
                    "confidence": "exact",
                    "notes": "duplicate",
                },
                {
                    "source_slug": "aug_2023_v2",
                    "official_question_number": 20,
                    "db_question_id": 182,
                    "needs_image": True,
                    "expected_asset_filename": "aug_2023_v2_q20.png",
                    "confidence": "likely",
                    "notes": "conflict",
                },
            ]
        }

        errors, _ = validate_payload(payload)

        self.assertTrue(
            any(
                "duplicate source_slug + official_question_number entries" in error
                for error in errors
            )
        )
        self.assertTrue(any("duplicate db_question_id conflict" in error for error in errors))


if __name__ == "__main__":
    unittest.main()
