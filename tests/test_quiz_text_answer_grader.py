import atexit
import os
import shutil
import tempfile
import unittest
from pathlib import Path


_TMP = tempfile.TemporaryDirectory()
atexit.register(_TMP.cleanup)

_ROOT = Path(__file__).resolve().parents[1]
_TMP_DB = Path(_TMP.name) / "questions.db"
_TMP_VAULT = Path(_TMP.name) / "vault"
shutil.copy2(_ROOT / "data" / "questions.db", _TMP_DB)
_TMP_VAULT.mkdir()

os.environ["DZI_DB"] = str(_TMP_DB)
os.environ["DZI_VAULT"] = str(_TMP_VAULT)

from web.app import grade_quiz_text_answers  # noqa: E402


class QuizTextAnswerGraderTest(unittest.TestCase):
    def test_ordered_correct_answer(self):
        results = grade_quiz_text_answers({1: "Да"}, {1: ["да"]})
        self.assertTrue(results[0]["is_correct"])
        self.assertEqual(results[0]["matched_answer"], "да")
        self.assertEqual(results[0]["points_awarded"], 1)

    def test_ordered_incorrect_answer(self):
        results = grade_quiz_text_answers({1: "Не"}, {1: ["да"]})
        self.assertFalse(results[0]["is_correct"])
        self.assertIsNone(results[0]["matched_answer"])
        self.assertEqual(results[0]["points_awarded"], 0)

    def test_ordered_multiple_accepted_alternatives(self):
        results = grade_quiz_text_answers({1: "JPG"}, {1: ["jpeg", "jpg"]})
        self.assertTrue(results[0]["is_correct"])
        self.assertEqual(results[0]["matched_answer"], "jpg")

    def test_missing_submitted_answer_is_empty_and_incorrect(self):
        results = grade_quiz_text_answers({}, {1: ["да"]})
        self.assertEqual(results[0]["raw_answer"], "")
        self.assertEqual(results[0]["normalized_answer"], "")
        self.assertFalse(results[0]["is_correct"])

    def test_empty_accepted_answer_list_is_incorrect(self):
        results = grade_quiz_text_answers({1: "да"}, {1: []})
        self.assertEqual(results[0]["accepted_answers"], [])
        self.assertFalse(results[0]["is_correct"])

    def test_order_independent_answers_in_different_order(self):
        results = grade_quiz_text_answers(
            {1: "рецепционист", 2: "клиент", 3: "мениджър"},
            {1: ["клиент"], 2: ["рецепционист"], 3: ["мениджър"]},
            grading_mode="order_independent",
        )
        self.assertEqual([row["is_correct"] for row in results], [True, True, True])

    def test_order_independent_duplicate_submitted_answer_gets_credit_once(self):
        results = grade_quiz_text_answers(
            {1: "клиент", 2: "клиент", 3: "рецепционист"},
            {1: ["клиент"], 2: ["рецепционист"], 3: ["мениджър"]},
            grading_mode="order_independent",
        )
        self.assertEqual([row["is_correct"] for row in results], [True, False, True])

    def test_order_independent_accepted_duplicate_can_give_duplicate_credit(self):
        results = grade_quiz_text_answers(
            {1: "клиент", 2: "клиент"},
            {1: ["клиент"], 2: ["клиент"]},
            grading_mode="order_independent",
        )
        self.assertEqual([row["is_correct"] for row in results], [True, True])

    def test_invalid_grading_mode_raises(self):
        with self.assertRaises(ValueError):
            grade_quiz_text_answers({1: "да"}, {1: ["да"]}, grading_mode="regex")

    def test_normalization_is_used(self):
        results = grade_quiz_text_answers({1: "  ДА\t\n"}, {1: ["да"]})
        self.assertTrue(results[0]["is_correct"])
        self.assertEqual(results[0]["normalized_answer"], "да")

    def test_punctuation_is_not_stripped(self):
        correct = grade_quiz_text_answers({1: "text-align"}, {1: ["text-align"]})
        incorrect = grade_quiz_text_answers({1: "text align"}, {1: ["text-align"]})
        self.assertTrue(correct[0]["is_correct"])
        self.assertFalse(incorrect[0]["is_correct"])

    def test_formula_like_strings_are_plain_strings(self):
        accepted = '=IF(B2>=3; "Да"; "Не")'
        correct = grade_quiz_text_answers({1: '=IF(B2>=3; "Да"; "Не")'}, {1: [accepted]})
        equivalent_but_unlisted = grade_quiz_text_answers({1: '=IF(B2>2; "Да"; "Не")'}, {1: [accepted]})
        self.assertTrue(correct[0]["is_correct"])
        self.assertFalse(equivalent_but_unlisted[0]["is_correct"])


if __name__ == "__main__":
    unittest.main()
