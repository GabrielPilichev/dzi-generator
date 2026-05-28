import tempfile
import unittest
from pathlib import Path

from src.audit_question_image_dependencies import (
    Candidate,
    markdown_review_content,
    parse_existing_review_fields,
)
from src.validate_question_image_review import ReviewEntry, validate_asset_filename, validate_entries


def candidate(**overrides):
    values = {
        "question_id": 182,
        "source_exam": "aug_2022_v2",
        "source_number": "2",
        "question_type": "multiple_choice",
        "topic_slug": "access-select-queries",
        "topic_title": "Access queries",
        "section_refs": "11. class",
        "category": "screenshot/UI",
        "confidence": "high",
        "asset_status": "needs_mapping",
        "visual_asset_count": 0,
        "missing_visual_asset_count": 0,
        "reason_text": "screenshot/UI: shown query reference",
        "prompt_preview": "Shown query",
    }
    values.update(overrides)
    return Candidate(**values)


class QuestionImageReviewSuggestionsTest(unittest.TestCase):
    def test_high_confidence_candidate_gets_safe_prefill(self):
        row = candidate()

        content = markdown_review_content(
            db_path=Path("data/questions.db"),
            questions=[],
            candidates=[row],
            visible_candidates=[row],
        )

        self.assertIn("- Tool prefilled suggestions in this file: 1", content)
        self.assertIn("- needs_image: yes", content)
        self.assertIn("- asset_source: official_exam_pdf", content)
        self.assertIn("- expected_asset_filename: aug_2022_v2_q2.png", content)
        self.assertIn("- suggested_by_tool: yes", content)

    def test_medium_confidence_candidate_stays_blank(self):
        row = candidate(confidence="medium")

        content = markdown_review_content(
            db_path=Path("data/questions.db"),
            questions=[],
            candidates=[row],
            visible_candidates=[row],
        )

        self.assertIn("- Tool prefilled suggestions in this file: 0", content)
        self.assertIn("- needs_image: ", content)
        self.assertIn("- asset_source: ", content)
        self.assertIn("- expected_asset_filename: ", content)
        self.assertIn("- suggested_by_tool: ", content)
        self.assertNotIn("- asset_source: official_exam_pdf", content)

    def test_existing_review_decision_is_preserved(self):
        row = candidate()

        content = markdown_review_content(
            db_path=Path("data/questions.db"),
            questions=[],
            candidates=[row],
            visible_candidates=[row],
            existing_review_fields={
                182: {
                    "needs_image": "no",
                    "asset_source": "manual_pdf_check",
                    "expected_asset_filename": "manual_q2.png",
                    "notes": "reviewed manually",
                }
            },
        )

        self.assertIn("- Tool prefilled suggestions in this file: 0", content)
        self.assertIn("- needs_image: no", content)
        self.assertIn("- asset_source: manual_pdf_check", content)
        self.assertIn("- expected_asset_filename: manual_q2.png", content)
        self.assertIn("- notes: reviewed manually", content)
        self.assertNotIn("aug_2022_v2_q2.png", content)

    def test_existing_review_fields_are_parsed_by_question_id(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "review.md"
            path.write_text(
                "\n".join(
                    [
                        "### 1. Q182 - source #2",
                        "",
                        "- question_id: `182`",
                        "- needs_image: no",
                        "- asset_source: manual_pdf_check",
                        "- expected_asset_filename: manual_q2.png",
                        "- notes: reviewed manually",
                        "- suggested_by_tool: yes",
                    ]
                ),
                encoding="utf-8",
            )

            fields = parse_existing_review_fields(path)

        self.assertEqual(fields[182]["needs_image"], "no")
        self.assertEqual(fields[182]["asset_source"], "manual_pdf_check")
        self.assertEqual(fields[182]["expected_asset_filename"], "manual_q2.png")
        self.assertEqual(fields[182]["notes"], "reviewed manually")
        self.assertEqual(fields[182]["suggested_by_tool"], "yes")

    def test_validator_accepts_suggested_bare_filename(self):
        entry = ReviewEntry(
            question_id=182,
            line_number=1,
            fields={
                "question_id": "182",
                "needs_image": "yes",
                "asset_source": "official_exam_pdf",
                "expected_asset_filename": "aug_2022_v2_q2.png",
                "notes": "",
                "suggested_by_tool": "yes",
            },
            field_lines={},
        )

        errors, summary = validate_entries([entry], {182}, require_reviewed=False)

        self.assertEqual(errors, [])
        self.assertEqual(summary["reviewed"], 0)
        self.assertEqual(summary["ready_mappings"], 0)
        self.assertEqual(summary["tool_suggested"], 1)

    def test_require_reviewed_rejects_unapproved_tool_suggestion(self):
        entry = ReviewEntry(
            question_id=182,
            line_number=1,
            fields={
                "question_id": "182",
                "needs_image": "yes",
                "asset_source": "official_exam_pdf",
                "expected_asset_filename": "aug_2022_v2_q2.png",
                "notes": "",
                "suggested_by_tool": "yes",
            },
            field_lines={},
        )

        errors, summary = validate_entries([entry], {182}, require_reviewed=True)

        self.assertEqual(summary["tool_suggested"], 1)
        self.assertIn("Q182 line 1: tool suggestion still needs human approval", errors)

    def test_human_approved_suggestion_counts_as_ready_mapping(self):
        entry = ReviewEntry(
            question_id=182,
            line_number=1,
            fields={
                "question_id": "182",
                "needs_image": "yes",
                "asset_source": "official_exam_pdf",
                "expected_asset_filename": "aug_2022_v2_q2.png",
                "notes": "",
                "suggested_by_tool": "no",
            },
            field_lines={},
        )

        errors, summary = validate_entries([entry], {182}, require_reviewed=True)

        self.assertEqual(errors, [])
        self.assertEqual(summary["reviewed"], 1)
        self.assertEqual(summary["ready_mappings"], 1)
        self.assertEqual(summary["tool_suggested"], 0)

    def test_validator_still_accepts_repo_relative_asset_paths(self):
        self.assertEqual(
            validate_asset_filename("data/assets/exams/may_2025_v2/pdf_crops/task_18_fig_1.png"),
            [],
        )


if __name__ == "__main__":
    unittest.main()
