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

from web.app import is_fill_in_question_auto_gradable  # noqa: E402


class FillInEligibilityTest(unittest.TestCase):
    def test_multiple_choice_returns_false(self):
        question = {"question_type": "multiple_choice", "prompt": "Text prompt"}
        self.assertFalse(is_fill_in_question_auto_gradable(question, [{"correct_answer": "А"}]))

    def test_fill_in_with_no_blanks_returns_false(self):
        question = {"question_type": "fill_in", "prompt": "Text prompt"}
        self.assertFalse(is_fill_in_question_auto_gradable(question, []))

    def test_ordered_fill_in_with_accepted_answers_returns_true(self):
        question = {"question_type": "fill_in", "prompt": "Text prompt"}
        subquestions = [
            {"subquestion_number": 1, "correct_answer": "300"},
            {"subquestion_number": 2, "correct_answer": '["540", "540 лв."]'},
        ]
        self.assertTrue(is_fill_in_question_auto_gradable(question, subquestions))

    def test_order_independent_fill_in_with_accepted_answer_sets_returns_true(self):
        question = {"question_type": "fill_in", "prompt": "Text prompt"}
        accepted = ["клиент", "рецепционист", "мениджър на хотела"]
        subquestions = [
            {"subquestion_number": 1, "accepted_answers": accepted},
            {"subquestion_number": 2, "accepted_answers": accepted},
            {"subquestion_number": 3, "accepted_answers": accepted},
        ]
        self.assertTrue(is_fill_in_question_auto_gradable(question, subquestions))

    def test_missing_accepted_answer_returns_false(self):
        question = {"question_type": "fill_in", "prompt": "Text prompt"}
        subquestions = [
            {"subquestion_number": 1, "correct_answer": "клиент"},
            {"subquestion_number": 2, "correct_answer": ""},
        ]
        self.assertFalse(is_fill_in_question_auto_gradable(question, subquestions))

    def test_visual_dependent_fill_in_returns_false(self):
        question = {
            "question_type": "fill_in",
            "prompt": "Според показаната диаграма попълнете стойността.",
        }
        self.assertFalse(is_fill_in_question_auto_gradable(question, [{"correct_answer": "42"}]))

    def test_practical_task_returns_false(self):
        question = {
            "question_type": "fill_in",
            "prompt": "Text prompt",
            "source_number": "26",
        }
        self.assertFalse(is_fill_in_question_auto_gradable(question, [{"correct_answer": "42"}]))

    def test_formula_like_answers_are_plain_accepted_strings(self):
        question = {"question_type": "fill_in", "prompt": "Text prompt"}
        subquestions = [
            {"subquestion_number": 1, "correct_answer": '=IF(B2>=3; "Да"; "Не")'},
        ]
        self.assertTrue(is_fill_in_question_auto_gradable(question, subquestions))


if __name__ == "__main__":
    unittest.main()
