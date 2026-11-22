"""Pins the multiple-choice result review shared by quiz and practice results.

``quiz_result.html`` renders the review through ``_multiple_choice_result_review.html``.
The page's content block is rendered here from deterministic in-memory contexts
(no database rows, no runtime state) with the application's real Jinja
environment, context processors and ``render_question_visuals`` macro. Each
rendering is compared with the SHA-256 of the output produced by the template
before the extraction, so the page stays byte-identical, whitespace included.
"""

import hashlib
import re
import unittest
from pathlib import Path

from flask import render_template
from jinja2 import meta

from tests.support import IsolatedAppTestCase, web_app


ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "web" / "templates"
PARTIAL = "_multiple_choice_result_review.html"
CONTENT_START = '<section class="quiz-hero result-summary">'
CONTENT_END = '<footer class="site-footer">'

STALE_MESSAGE = (
    "Този тест съдържа стари или непълни въпроси и не може да бъде показан коректно. "
    "Моля, създайте нов тест."
)

# SHA-256 of the content block of the rendered quiz_result.html for each fixture below,
# produced by the template at base 0cb22d0 (before the partial was extracted).
CONTENT_SHA256 = {
    "normal": "bd041d1919f243587c457f5a731662ef8f8c284f1e3c88e1bc99c24b9e5fefd8",
    "mixed_combined": "7300a1408f2dd3ec1aad223d3d00d1d3313e11a7192a45e8b2517ba2bea0f422",
    "open_not_combined": "9bf1e3e8a70d06696a7b3c7658023b238b0a396ab567150f1408346ebbe0eb46",
    "stale": "4cd97a451e1fa5e20f92b5a8b730a56be1421ddc3b56cb0651386c46e073ef73",
    "stale_with_questions": "5785f1593bbbe5df45310eadb1d7a681d75b679d7eb8cfcc551d10118b6ef2fb",
    "empty": "c8336de21e563ffbf9a1bbaaef89f86ea54ff4204d160deed6dabc9c7c4beb34",
    "stale_escaped": "206c35dc02902a015c5141c6c73f8ffc70e9d253b48846bd5b000345e265af1e",
}


def _options(correct_letter, order=("A", "B", "C", "D")):
    """Options in display order: stored letters keep their identity, display letters follow position."""
    texts = {"A": "Първи вариант", "B": "Втори <вариант>", "C": "Трети & четвърти", "D": "Пети вариант"}
    return [
        {
            "option_letter": letter,
            "display_letter": "ABCD"[position],
            "option_text": texts[letter],
            "is_correct": letter == correct_letter,
        }
        for position, letter in enumerate(order)
    ]


def _question(prompt, *, correct, chosen, difficulty=None, visuals=(), order=("A", "B", "C", "D")):
    options = _options(correct, order)
    by_letter = {opt["option_letter"]: opt for opt in options}
    return {
        "prompt": prompt,
        "difficulty": difficulty,
        "visual_assets": list(visuals),
        "is_correct": chosen == correct,
        "chosen_letter": chosen,
        "chosen_text": by_letter[chosen]["option_text"] if chosen else None,
        "correct_text": by_letter[correct]["option_text"],
        "feedback_explanation": f"Обяснение: верният отговор е {by_letter[correct]['option_text']}.",
        "options": options,
    }


def _questions():
    return [
        _question(
            "Верен отговор <с> изображение.",
            correct="B",
            chosen="B",
            difficulty="hard",
            visuals=[
                {"static_filename": "img/questions/fixture_one.png", "alt_text": "Схема едно",
                 "caption": "Подпис към схемата"},
                {"static_filename": "img/questions/fixture_two.png", "alt_text": "Схема две",
                 "caption": "DZI may_2025 Q7"},
            ],
            order=("C", "B", "A", "D"),
        ),
        _question("Грешен отговор с числова трудност.", correct="A", chosen="C", difficulty=1,
                  order=("C", "A", "D", "B")),
        _question("Без избран отговор.", correct="D", chosen=None, difficulty="medium", order=("D", "A", "B", "C")),
        _question("Верен отговор без трудност.", correct="C", chosen="C"),
        _question("Грешен отговор с непозната трудност.", correct="B", chosen="A", difficulty="mystery"),
    ]


def _open_text_answers():
    base = {
        "question_id": 0, "subquestion_number": 1, "raw_answer": "", "is_correct": None,
        "points_awarded": None, "points_possible": None, "grading_mode": None,
        "teacher_reviewed": False, "teacher_override_status": None,
        "teacher_override_status_label": None, "teacher_note": None,
        "accepted_answers": [], "feedback_explanation": "",
    }
    return [
        dict(base, question_id=501, raw_answer="42", is_correct=True,
             points_awarded=2.0, points_possible=2.0, grading_mode="exact"),
        dict(base, question_id=501, subquestion_number=2, raw_answer="грешно", is_correct=False,
             points_awarded=0.0, points_possible=2.0, grading_mode="exact",
             accepted_answers=["верно", "вярно"], feedback_explanation="Приемат се две форми."),
        dict(base, question_id=502, raw_answer="", is_correct=None),
        dict(base, question_id=503, raw_answer="почти", is_correct=False,
             points_awarded=1.0, points_possible=2.0, grading_mode="manual",
             teacher_reviewed=True, teacher_override_status="partially_correct",
             teacher_override_status_label="Частично вярно", teacher_note="Липсва единица.",
             feedback_explanation="Допълнително обяснение."),
    ]


def _context(**overrides):
    questions = _questions()
    ctx = {
        "assignment": {"title_bg": "Тест <за> резултат", "public_token": None},
        "attempt": {"student_name": "Ана Иванова", "score_correct": 2, "score_total": 5},
        "questions": questions,
        "time_taken": "03:21",
        "success_rate": "40%",
        "difficulty_breakdown": [
            {"label": "труден", "class": "badge-hard", "correct": 1, "total": 1,
             "percent": 100, "percent_display": "100%"},
            {"label": "много лесен", "class": "badge-easy", "correct": 0, "total": 1,
             "percent": 0, "percent_display": "0%"},
        ],
        "open_text_answers": [],
        "open_text_subtotal": None,
        "combined_score": None,
        "stale_attempt_message": None,
        "original_question_count": len(questions),
        "renderable_question_count": len(questions),
        "skipped_question_count": 0,
    }
    ctx.update(overrides)
    return ctx


def fixture_contexts():
    mixed = {
        "open_text_answers": _open_text_answers(),
        "open_text_subtotal": {"awarded": 3.0, "possible": 6.0},
    }
    combined = {"mc_awarded": 2.0, "mc_possible": 5.0, "open_awarded": 3.0,
                "open_possible": 6.0, "combined_awarded": 5.0, "combined_possible": 11.0}
    return {
        "normal": _context(),
        "mixed_combined": _context(**mixed, combined_score=combined),
        "open_not_combined": _context(**mixed),
        "stale": _context(
            questions=[], stale_attempt_message=STALE_MESSAGE,
            open_text_answers=_open_text_answers(), open_text_subtotal=mixed["open_text_subtotal"],
            original_question_count=5, renderable_question_count=0, skipped_question_count=5,
        ),
        # The stale message wins over any question or open answer still in the context.
        "stale_with_questions": _context(**mixed, stale_attempt_message=STALE_MESSAGE),
        "empty": _context(questions=[], original_question_count=0, renderable_question_count=0),
        "stale_escaped": _context(stale_attempt_message="<b>Стар</b> тест"),
    }


def render_page(name):
    """The whole quiz_result.html page for fixture ``name``, through the application's render path."""
    app = web_app.app
    with app.test_request_context("/quiz/attempt/1/result"):
        return render_template("quiz_result.html", **fixture_contexts()[name])


def render_content_block(name):
    """The page's content block: from the summary section to the site footer base.html adds."""
    page = render_page(name)
    start = page.index(CONTENT_START)
    end = page.index(CONTENT_END, start)
    return page[start:end]


def render_partial(**context):
    """The shared partial on its own, with exactly ``context`` and nothing assignment-specific."""
    app = web_app.app
    with app.test_request_context("/"):
        app.update_template_context(context)
        return app.jinja_env.get_template(PARTIAL).render(**context)


def sha256_text(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class QuizResultReviewPartialTest(IsolatedAppTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        web_app.app.config.update(TESTING=True)

    def test_content_block_is_byte_identical_to_the_pre_extraction_template(self):
        for name in CONTENT_SHA256:
            with self.subTest(fixture=name):
                self.assertEqual(sha256_text(render_content_block(name)), CONTENT_SHA256[name])

    def test_render_is_deterministic(self):
        for name in CONTENT_SHA256:
            with self.subTest(fixture=name):
                self.assertEqual(render_content_block(name), render_content_block(name))

    def test_fixtures_cover_each_result_presentation(self):
        normal = render_content_block("normal")
        for fragment in (
            'class="card question-card correct"',
            'class="card question-card incorrect"',
            "status-pill--correct", "status-pill--wrong",
            "answer-note--correct", "answer-note--wrong", "answer-note--unanswered",
            "Няма избран отговор.", "Правилен отговор", "Обяснение",
            "твоят отговор", "правилен",
            "is-wrong-chosen", "is-chosen", "is-correct",
            'class="question-visual-assets"', "Подпис към схемата",
            "Трудност: труден", "Трудност: много лесен", "Трудност: среден",
            "Въпрос 5",
            "Втори &lt;вариант&gt;",
            "Верен отговор &lt;с&gt; изображение.",
        ):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, normal)
        self.assertNotIn("DZI may_2025 Q7", normal)  # debug caption stays hidden
        self.assertNotIn("Трудност: mystery", normal)
        self.assertNotIn("Отворени отговори", normal)
        self.assertNotIn(STALE_MESSAGE, normal)

    def test_mixed_attempts_keep_open_answer_analysis_in_the_page(self):
        combined = render_content_block("mixed_combined")
        self.assertIn("Комбиниран резултат", combined)
        self.assertIn("Отворените точки са включени в комбинирания резултат", combined)
        self.assertIn("Учителски преглед", combined)
        self.assertIn("Приет отговор", combined)
        plain = render_content_block("open_not_combined")
        self.assertNotIn("Комбиниран резултат", plain)
        self.assertIn("Автоматичните точки са само информативни", plain)

    def test_stale_attempt_shows_only_the_warning(self):
        for name in ("stale", "stale_with_questions"):
            with self.subTest(fixture=name):
                html = render_content_block(name)
                self.assertIn(STALE_MESSAGE, html)
                self.assertIn('role="alert"', html)
                for absent in ("question-card", "Отворени отговори", "Въпрос 1", "Учителски преглед"):
                    self.assertNotIn(absent, html)

    def test_stale_message_is_escaped(self):
        html = render_content_block("stale_escaped")
        self.assertIn("&lt;b&gt;Стар&lt;/b&gt; тест", html)
        self.assertNotIn("<b>Стар</b>", html)

    def test_chosen_option_is_matched_by_stored_letter_not_display_position(self):
        question = _question("Разбъркани варианти.", correct="A", chosen="C", order=("C", "A", "D", "B"))
        cards = re.findall(r'<div class="result-option-card.*?</div>', render_partial(questions=[question]), re.S)
        self.assertEqual(len(cards), 4)
        self.assertIn("Трети &amp; четвърти", cards[0])
        self.assertIn("твоят отговор", cards[0])
        self.assertIn("is-wrong-chosen", cards[0])
        self.assertIn("правилен", cards[1])
        for card in cards[2:]:
            self.assertNotIn("result-option-tag", card)

    def test_empty_question_list_shows_no_review(self):
        html = render_content_block("empty")
        self.assertNotIn("question-card", html)
        self.assertNotIn(STALE_MESSAGE, html)

    def test_page_embeds_the_partial_output(self):
        for name in ("normal", "stale"):
            with self.subTest(fixture=name):
                context = fixture_contexts()[name]
                partial = render_partial(
                    questions=context["questions"],
                    stale_attempt_message=context["stale_attempt_message"],
                )
                self.assertTrue(partial.strip())
                self.assertIn(partial.strip(), render_content_block(name))

    def test_partial_renders_without_assignment_attempt_or_scores(self):
        questions = _questions()
        html = render_partial(questions=questions)
        self.assertEqual(html.count("<article"), len(questions))
        self.assertIn("Въпрос 1", html)
        self.assertIn("Трудност: труден", html)
        self.assertNotIn("Отворени отговори", html)
        self.assertNotIn(STALE_MESSAGE, html)

    def test_partial_shows_a_stale_warning_and_no_questions(self):
        html = render_partial(questions=_questions(), stale_attempt_message=STALE_MESSAGE)
        self.assertIn(STALE_MESSAGE, html)
        self.assertNotIn("<article", html)

    def test_partial_reads_only_question_data_and_the_stale_message(self):
        source = (TEMPLATES / PARTIAL).read_text(encoding="utf-8")
        required = meta.find_undeclared_variables(web_app.app.jinja_env.parse(source))
        # question_difficulty_label comes from the application's context processor. Nothing about the
        # assignment, attempt, scores, open answers or routes is read, so practice results can include it.
        self.assertEqual(required, {"questions", "stale_attempt_message", "question_difficulty_label"})

    def test_page_includes_the_partial_and_keeps_open_answers_outside_it(self):
        page = (TEMPLATES / "quiz_result.html").read_text(encoding="utf-8")
        self.assertIn(PARTIAL, page)
        self.assertIn("open_text_answers", page)
        self.assertNotIn("question-card {{", page)
        # The shared visual macro is the one the other templates use.
        self.assertIn('from "_question_visuals.html" import render_question_visuals',
                      (TEMPLATES / PARTIAL).read_text(encoding="utf-8"))

    def test_templates_compile_in_the_application_environment(self):
        for name in (PARTIAL, "quiz_result.html"):
            with self.subTest(template=name):
                web_app.app.jinja_env.get_template(name)


if __name__ == "__main__":
    unittest.main()
