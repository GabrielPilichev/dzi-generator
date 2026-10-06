import unittest


from tests.support import web_app
from web.services import quiz as quiz_service

quiz_normalize_text_answer = quiz_service.quiz_normalize_text_answer


class QuizTextNormalizationTest(unittest.TestCase):
    def test_none_becomes_empty_string(self):
        self.assertEqual(quiz_normalize_text_answer(None), "")

    def test_converts_input_to_string(self):
        self.assertEqual(quiz_normalize_text_answer(300), "300")

    def test_leading_and_trailing_whitespace_is_trimmed(self):
        self.assertEqual(quiz_normalize_text_answer("  отговор  "), "отговор")

    def test_repeated_spaces_newlines_and_tabs_collapse(self):
        self.assertEqual(
            quiz_normalize_text_answer("един   \n\t  два"),
            "един два",
        )

    def test_cyrillic_and_latin_casefolding_works(self):
        self.assertEqual(quiz_normalize_text_answer("Да TEST"), "да test")

    def test_unicode_composed_and_decomposed_forms_normalize_consistently(self):
        composed = "й"
        decomposed = "и\u0306"
        self.assertEqual(quiz_normalize_text_answer(composed), quiz_normalize_text_answer(decomposed))

    def test_smart_quotes_normalize_to_straight_quotes(self):
        self.assertEqual(
            quiz_normalize_text_answer("“Да” и ‘Не’"),
            '"да" и \'не\'',
        )

    def test_bulgarian_diacritics_are_preserved(self):
        self.assertEqual(quiz_normalize_text_answer("Й ѝ"), "й ѝ")

    def test_punctuation_is_not_stripped(self):
        self.assertEqual(quiz_normalize_text_answer("=IF(B2>=3; \"Да\"; \"Не\")"), '=if(b2>=3; "да"; "не")')

    def test_cyrillic_and_latin_homoglyphs_are_not_converted(self):
        self.assertEqual(quiz_normalize_text_answer("AА"), "aа")


if __name__ == "__main__":
    unittest.main()
