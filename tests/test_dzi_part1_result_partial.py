"""The shared multiple-choice result partial after the DZI Part 1 slot-number opt-in (C3.4).

``_multiple_choice_result_review.html`` numbers its cards by ``loop.index``. The DZI Part 1 result has to
show the *saved* slot number instead (a submission can leave gaps), so the partial gained one explicit
opt-in: a question row that carries ``saved_slot_number`` is numbered by it. It is a property of the row, not a new
context variable, because ``tests/test_quiz_result_review_partial.py`` pins the partial's undeclared variables
to exactly three names. Nobody sets the key but the Part 1 result loader.

What is proved, and how (in-process only: no database, no server, no ``isolated_app``):

* **Legacy render equality.** The assignment result page (``quiz_result.html``) and the practice result page
  (``practice_result.html``) are rendered through the application's real Jinja environment from fixed
  in-memory contexts, as are the bare partial's contexts. Every rendering is compared with the SHA-256 of the
  bytes the template produced at the base ``2a9560d`` (the merged #378), taken before the opt-in existed. The
  fixtures include skipped questions, stale results (with and without questions), unanswered questions, a
  retired scope, escaped text, visuals and an unrelated ``slot_number`` key that a legacy caller may carry.
* **The opt-in.** A row with ``saved_slot_number`` shows it: a gap stays a gap, two-digit slots render, a mix of
  rows with and without it works, and nothing else in a card changes (the output equals the legacy output with
  only the numbers replaced). A missing, ``None`` or zero value is numbered by position.
* **The variable contract.** The partial still reads exactly ``questions``, ``stale_attempt_message`` and the
  application's ``question_difficulty_label``, which is what the pinned test of the legacy partial asserts; that
  assertion is also run here, unchanged, against the edited template.
"""

from __future__ import annotations

import hashlib
import re
import unittest
from pathlib import Path

from flask import render_template
from jinja2 import meta

from tests.test_quiz_result_review_partial import (
    CONTENT_END,
    CONTENT_START,
    STALE_MESSAGE,
    _question,
    _questions,
    fixture_contexts,
)
from tests.support import web_app
from web.services import practice

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "web" / "templates"
PARTIAL = "_multiple_choice_result_review.html"
NUMBER = re.compile(r"<h2>Въпрос (\S+?)</h2>")

# SHA-256 of each legacy rendering below, produced by the unmodified template at the base 2a9560d.
BASE_SHA256 = {
    "partial_empty": "3441b5696d6abf2bd959d18bcb31dde8239a6ad8185bfa659af60bc764c238a8",
    "partial_no_stale_variable": "98b1f594763dfca53bea67e8c75f692e55c32a25ea51e4e63f1e7fb345083ac5",
    "partial_normal": "98b1f594763dfca53bea67e8c75f692e55c32a25ea51e4e63f1e7fb345083ac5",
    "partial_stale": "bcb50df9617945b83f9d364245c7ac0e3e263a21952f2d21150d214641f52403",
    "partial_unanswered": "92275601b556bd8c999f9bea3d7285acebe7ecf2f614296afeaf8d7b0fcce025",
    "partial_with_slot_keys_ignored": "98b1f594763dfca53bea67e8c75f692e55c32a25ea51e4e63f1e7fb345083ac5",
    "practice_all_correct": "abc8a1128ce7d1d295194d435f7043b7165ab550a583bdbcb00bc09bb1d099b1",
    "practice_normal": "fdbf9e71e47e8476092129175c6fba3386e3feab23d499d1eddcf136c07d3534",
    "practice_retired_scope": "5a8d1651b534b4193fb301fc2e580cd627d2e046b93f8fe834f362512f605c42",
    "practice_skipped": "1f84280772858d23be785ca74eaa82c2afde51db25444c88770745651f248399",
    "practice_stale": "bc106cfebbb73bba1f7fb287d8c760f4fcc537284c62603f80c744d0d36089c9",
    "practice_stale_escaped": "ae6ee24fce8ab6f643e0adcf296d6b45127e9a5d64b30f04acfe9d24ac6f08ee",
    "practice_stale_with_questions": "9961f1653e421dc5219d6d4a4ea3a7ec0f8c221ee9dcb91262773c02118aa3f4",
    "practice_unanswered_only": "211e7c3883f3855a7fef0e972f266b69e7ab3f344e193424f8e8a682947b717f",
    "quiz_empty": "c8336de21e563ffbf9a1bbaaef89f86ea54ff4204d160deed6dabc9c7c4beb34",
    "quiz_mixed_combined": "7300a1408f2dd3ec1aad223d3d00d1d3313e11a7192a45e8b2517ba2bea0f422",
    "quiz_normal": "bd041d1919f243587c457f5a731662ef8f8c284f1e3c88e1bc99c24b9e5fefd8",
    "quiz_open_not_combined": "9bf1e3e8a70d06696a7b3c7658023b238b0a396ab567150f1408346ebbe0eb46",
    "quiz_stale": "4cd97a451e1fa5e20f92b5a8b730a56be1421ddc3b56cb0651386c46e073ef73",
    "quiz_stale_escaped": "206c35dc02902a015c5141c6c73f8ffc70e9d253b48846bd5b000345e265af1e",
    "quiz_stale_with_questions": "5785f1593bbbe5df45310eadb1d7a681d75b679d7eb8cfcc551d10118b6ef2fb",
}


def sha256_text(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _visual(name, alt):
    return {"static_filename": f"img/questions/{name}.png", "alt_text": alt, "caption": f"Подпис {alt}"}


def _practice_questions():
    """Correct, wrong, unanswered, a visual, an escaped prompt and a non-A-D stored key."""
    return [
        _question("Верен практически въпрос <с> знаци.", correct="B", chosen="B", difficulty="easy",
                  visuals=[_visual("practice_one", "Схема")], order=("B", "A", "C", "D")),
        _question("Грешен практически въпрос.", correct="C", chosen="A", difficulty=2, order=("C", "D", "A", "B")),
        _question("Пропуснат практически въпрос.", correct="D", chosen=None, difficulty="hard"),
        _question("Още един верен въпрос.", correct="A", chosen="A"),
    ]


def _practice_result(*, questions, skipped=0, stale_message=None, score=(2, 4), scope_available=True):
    return practice.PracticeResult(
        practice.OUTCOME_LOADED, 7, 3, None, "2026-11-01 10:00:00", score[0], score[1], tuple(questions),
        len(questions) + skipped, len(questions), 0, skipped, skipped, stale_message, scope_available)


def practice_contexts():
    stale = "Въпросите от това упражнение вече не могат да бъдат показани, но резултатът ти е запазен."

    def context(result, *, url="/practice/section/3/start"):
        score_total = result.score_total
        return dict(result=result, success_rate=(200 * result.score_correct + score_total) // (2 * score_total),
                    new_attempt_url=url, **result.review_context())

    return {
        "practice_normal": context(_practice_result(questions=_practice_questions())),
        "practice_skipped": context(_practice_result(questions=_practice_questions()[1:], skipped=2, score=(1, 4))),
        "practice_all_correct": context(_practice_result(
            questions=[_question("Единствен верен.", correct="A", chosen="A")], score=(1, 1))),
        "practice_unanswered_only": context(_practice_result(
            questions=[_question("Без отговор.", correct="B", chosen=None)], score=(0, 1))),
        "practice_stale": context(_practice_result(
            questions=[], skipped=4, stale_message=stale, score=(3, 4)), url=None),
        "practice_stale_with_questions": context(_practice_result(
            questions=_practice_questions(), skipped=1, stale_message=stale)),
        "practice_retired_scope": context(_practice_result(
            questions=_practice_questions(), skipped=1, scope_available=False), url=None),
        "practice_stale_escaped": context(_practice_result(
            questions=[], skipped=1, stale_message="<i>Стар</i> & непълен", score=(0, 1))),
    }


def partial_contexts():
    """The bare partial, as every legacy caller supplies it. Some questions carry a ``slot_number`` key, which is
    not the opt-in (that is ``saved_slot_number``) and must be ignored."""
    with_slots = _questions()
    for index, question in enumerate(with_slots):
        question["slot_number"] = (3, 7, 8, 20, 25)[index]
    return {
        "partial_normal": dict(questions=_questions(), stale_attempt_message=None),
        "partial_no_stale_variable": dict(questions=_questions()),
        "partial_with_slot_keys_ignored": dict(questions=with_slots, stale_attempt_message=None),
        "partial_stale": dict(questions=_questions(), stale_attempt_message=STALE_MESSAGE),
        "partial_empty": dict(questions=[], stale_attempt_message=None),
        "partial_unanswered": dict(
            questions=[_question("Само пропуснати.", correct="A", chosen=None, difficulty="medium"),
                       _question("Втори пропуснат.", correct="C", chosen=None)],
            stale_attempt_message=None),
    }


def render_partial(**context):
    app = web_app.app
    with app.test_request_context("/"):
        app.update_template_context(context)
        return app.jinja_env.get_template(PARTIAL).render(**context)


def _content_block(page):
    start = page.index(CONTENT_START)
    return page[start:page.index(CONTENT_END, start)]


def legacy_renders() -> dict[str, str]:
    """Every legacy rendering, by name: the two result pages' content blocks and the bare partial."""
    app = web_app.app
    renders = {}
    for name, context in fixture_contexts().items():
        with app.test_request_context("/quiz/attempt/1/result"):
            renders[f"quiz_{name}"] = _content_block(render_template("quiz_result.html", **context))
    for name, context in practice_contexts().items():
        with app.test_request_context("/practice/result/7"):
            renders[name] = _content_block(render_template("practice_result.html", **context))
    for name, context in partial_contexts().items():
        renders[name] = render_partial(**context)
    return renders


def with_saved_numbers(questions, numbers):
    """Copies of ``questions`` where each row with a number in ``numbers`` carries it as ``saved_slot_number``."""
    rows = []
    for question, number in zip(questions, numbers):
        row = dict(question)
        if number is not None:
            row["saved_slot_number"] = number
        rows.append(row)
    return rows


class LegacyRenderEqualityTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        web_app.app.config.update(TESTING=True)
        cls.renders = legacy_renders()

    def test_every_legacy_rendering_is_byte_identical_to_the_base_template(self):
        self.assertEqual(set(self.renders), set(BASE_SHA256))
        for name, text in self.renders.items():
            with self.subTest(rendering=name):
                self.assertEqual(sha256_text(text), BASE_SHA256[name])

    def test_the_renderings_are_deterministic(self):
        self.assertEqual(legacy_renders(), self.renders)

    def test_the_fixtures_cover_skipped_stale_unanswered_and_retired_cases(self):
        for name, fragments in {
            "practice_skipped": ("Пропуснати въпроси (не могат да бъдат показани): 2", "answer-note--unanswered"),
            "practice_stale": ("Въпросите от това упражнение вече не могат да бъдат показани", 'role="alert"'),
            "practice_stale_with_questions": ("Въпросите от това упражнение вече не могат да бъдат показани",),
            "practice_unanswered_only": ("Няма избран отговор.", "Правилен отговор"),
            "practice_retired_scope": ("Към началото",),
            "practice_stale_escaped": ("&lt;i&gt;Стар&lt;/i&gt;",),
            "quiz_stale": ("Този тест съдържа стари или непълни въпроси",),
            "quiz_normal": ("Въпрос 5", "Няма избран отговор.", 'class="question-visual-assets"'),
            "partial_unanswered": ("Въпрос 2", "Няма избран отговор."),
        }.items():
            for fragment in fragments:
                with self.subTest(rendering=name, fragment=fragment):
                    self.assertIn(fragment, self.renders[name])
        for name in ("practice_stale", "practice_stale_with_questions", "quiz_stale", "partial_stale", "partial_empty"):
            with self.subTest(rendering=name):
                self.assertEqual(self.renders[name].count("<article"), 0)
        self.assertNotIn("Нов опит", self.renders["practice_retired_scope"])
        self.assertIn("Нов опит", self.renders["practice_normal"])

    def test_a_legacy_row_with_an_unrelated_slot_number_key_is_still_numbered_by_position(self):
        self.assertEqual(self.renders["partial_with_slot_keys_ignored"], self.renders["partial_normal"])
        self.assertEqual(NUMBER.findall(self.renders["partial_with_slot_keys_ignored"]), ["1", "2", "3", "4", "5"])

    def test_the_practice_and_quiz_pages_still_number_by_position(self):
        self.assertEqual(NUMBER.findall(self.renders["practice_normal"]), ["1", "2", "3", "4"])
        self.assertEqual(NUMBER.findall(self.renders["practice_skipped"]), ["1", "2", "3"])
        self.assertEqual(NUMBER.findall(self.renders["quiz_normal"]), ["1", "2", "3", "4", "5"])


class SavedSlotNumberOptInTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        web_app.app.config.update(TESTING=True)

    def test_saved_numbers_replace_positions_and_a_gap_stays_a_gap(self):
        questions = with_saved_numbers(_questions(), (1, 2, 4, 5, 25))
        self.assertEqual(NUMBER.findall(render_partial(questions=questions, stale_attempt_message=None)),
                         ["1", "2", "4", "5", "25"])

    def test_a_first_slot_gap_and_two_digit_slots_keep_their_numbers(self):
        questions = with_saved_numbers(_questions(), (7, 9, 10, 14, 15))
        self.assertEqual(NUMBER.findall(render_partial(questions=questions)), ["7", "9", "10", "14", "15"])

    def test_the_numbers_are_the_only_difference_from_the_legacy_output(self):
        legacy = render_partial(questions=_questions(), stale_attempt_message=None)
        numbered = render_partial(questions=with_saved_numbers(_questions(), (3, 6, 8, 12, 24)),
                                  stale_attempt_message=None)
        self.assertNotEqual(legacy, numbered)
        renumber = iter(["3", "6", "8", "12", "24"])
        expected = NUMBER.sub(lambda match: f"<h2>Въпрос {next(renumber)}</h2>", legacy)
        self.assertEqual(numbered, expected)
        self.assertEqual(len(NUMBER.findall(legacy)), 5)

    def test_a_missing_none_or_zero_number_falls_back_to_the_position(self):
        questions = with_saved_numbers(_questions(), (None, 6, None, 0, None))
        questions[2]["saved_slot_number"] = None
        self.assertEqual(NUMBER.findall(render_partial(questions=questions)), ["1", "6", "3", "4", "5"])

    def test_a_stale_warning_still_suppresses_every_card_with_the_opt_in(self):
        questions = with_saved_numbers(_questions(), (1, 2, 3, 4, 5))
        html = render_partial(questions=questions, stale_attempt_message=STALE_MESSAGE)
        self.assertIn(STALE_MESSAGE, html)
        self.assertNotIn("<article", html)
        self.assertEqual(html, render_partial(questions=_questions(), stale_attempt_message=STALE_MESSAGE))

    def test_an_empty_list_renders_nothing_either_way(self):
        self.assertEqual(render_partial(questions=[]), render_partial(questions=[], stale_attempt_message=None))
        self.assertEqual(render_partial(questions=[]).strip(), "")


class VariableContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        web_app.app.config.update(TESTING=True)

    def test_the_partial_still_reads_exactly_the_three_variables_the_legacy_test_pins(self):
        source = (TEMPLATES / PARTIAL).read_text(encoding="utf-8")
        required = meta.find_undeclared_variables(web_app.app.jinja_env.parse(source))
        self.assertEqual(required, {"questions", "stale_attempt_message", "question_difficulty_label"})

    def test_the_legacy_partial_test_body_passes_against_the_edited_template(self):
        """``QuizResultReviewPartialTest`` needs the isolated application; its assertion about the partial's
        variables does not, so that method body is run here, as written, with a plain test case."""
        from tests.test_quiz_result_review_partial import QuizResultReviewPartialTest

        QuizResultReviewPartialTest.test_partial_reads_only_question_data_and_the_stale_message(self)

    def test_the_templates_compile_in_the_application_environment(self):
        for name in (PARTIAL, "quiz_result.html", "practice_result.html"):
            with self.subTest(template=name):
                web_app.app.jinja_env.get_template(name)

    def test_the_opt_in_is_documented_in_the_partials_own_comment(self):
        source = (TEMPLATES / PARTIAL).read_text(encoding="utf-8")
        self.assertIn("saved_slot_number", source.split("-#}", 1)[0])


if __name__ == "__main__":
    unittest.main()
