"""Existing confirmed DZI visuals reach both section browsing templates."""

import json
import re

from web.services import browsing as browsing_service
from tests.support import IsolatedAppTestCase, ROOT, web_app


class SectionQuestionImagesTest(IsolatedAppTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.client = web_app.app.test_client()
        cls.section_slug = "grade12-dzi-preparation"
        conn = web_app.quiz_db()
        try:
            cls.section_id = conn.execute(
                "SELECT id FROM curriculum_sections WHERE section_slug = ?",
                (cls.section_slug,),
            ).fetchone()[0]
            mappings = json.loads((
                ROOT / "docs/reviews/confirmed_dzi_image_question_mappings.json"
            ).read_text(encoding="utf-8"))["mappings"]
            confirmed = {
                int(entry["db_question_id"]): entry
                for entry in mappings if entry["needs_image"]
            }
            with web_app.app.app_context():
                questions = browsing_service.fetch_questions_in_section(cls.section_id)
            mapped = [q for q in questions if q["question_id"] in confirmed
                      and q["visual_assets"]]
            cls.mapped = mapped[:2]
            sources = {
                int(row["id"]): row["source_exam"]
                for row in conn.execute("SELECT id, source_exam FROM questions")
            }
            cls.dzi_unmapped = next(
                q for q in questions if not q["visual_assets"]
                and web_app.dzi_parse_source_slug(sources[q["question_id"]]) is not None
            )
            cls.normal_it = next(
                q for q in questions if not q["visual_assets"]
                and sources[q["question_id"]].startswith("classroom_tests")
            )
            cls.expected = confirmed
            if len(cls.mapped) != 2:
                raise AssertionError("Need two confirmed image questions in DZI section")
        finally:
            conn.close()

    @staticmethod
    def article(body, question_id):
        match = re.search(
            rf'<article\b(?=[^>]*data-question-id="{question_id}")[^>]*>.*?</article>',
            body, re.DOTALL,
        )
        if match is None:
            raise AssertionError(f"Question {question_id} not rendered in section")
        return match.group(0)

    def test_confirmed_image_renders_on_legacy_section_page(self):
        response = self.client.get(f"/section/{self.section_slug}")
        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        question = self.mapped[0]
        article = self.article(body, question["question_id"])
        filename = self.expected[question["question_id"]]["expected_asset_filename"]
        self.assertIn(f'/static/img/questions/dzi/{filename}', article)
        self.assertEqual(article.count("<img"), 1)
        self.assertIn('alt="Изображение към ', article)
        self.assertIn('loading="lazy"', article)
        self.assertNotIn("<figcaption", article)  # Confirmed DB caption is debug-only.
        self.assertNotIn("DZI " + self.expected[question["question_id"]]["source_slug"] + " Q", article)
        self.assertNotIn("web/static/img/questions/dzi/", article)

    def test_unmapped_dzi_question_has_no_broken_image_and_keeps_prompt(self):
        body = self.client.get(f"/section/{self.section_slug}").get_data(as_text=True)
        article = self.article(body, self.dzi_unmapped["question_id"])
        self.assertIn(self.dzi_unmapped["prompt"], article)
        self.assertNotIn("<img", article)
        self.assertNotIn("question-visual-assets", article)

    def test_normal_it_question_remains_without_image(self):
        body = self.client.get(f"/section/{self.section_slug}").get_data(as_text=True)
        article = self.article(body, self.normal_it["question_id"])
        self.assertIn(self.normal_it["prompt"], article)
        self.assertNotIn("<img", article)

    def test_two_questions_keep_their_own_images(self):
        body = self.client.get(f"/section/{self.section_slug}").get_data(as_text=True)
        first, second = self.mapped
        first_article = self.article(body, first["question_id"])
        second_article = self.article(body, second["question_id"])
        first_filename = self.expected[first["question_id"]]["expected_asset_filename"]
        second_filename = self.expected[second["question_id"]]["expected_asset_filename"]
        self.assertNotEqual(first_filename, second_filename)
        self.assertIn(first_filename, first_article)
        self.assertNotIn(second_filename, first_article)
        self.assertIn(second_filename, second_article)
        self.assertNotIn(first_filename, second_article)

    def test_subject_scoped_section_page_uses_same_visuals(self):
        url = f"/subjects/information-technologies/12/{self.section_slug}"
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        filename = self.expected[self.mapped[0]["question_id"]]["expected_asset_filename"]
        self.assertIn(f'/static/img/questions/dzi/{filename}', body)
        self.assertIn('alt="Изображение към ', body)
        self.assertNotIn(f'DZI {self.expected[self.mapped[0]["question_id"]]["source_slug"]} Q', body)


if __name__ == "__main__":
    import unittest
    unittest.main()
