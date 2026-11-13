"""A1: integrity guard for approved canonical questions.

Scans every question that passes the real approval predicate
(``web.services.quiz.QUIZ_APPROVED_FILTER``), not only the questions the quiz
selector would serve, and compares the violations with an explicit exception map.

Invariants (nothing else: no placeholder, visual, scoring or grading policy):

* ``multiple_choice`` and ``true_false``: exactly one correct option;
* ``multiple_choice``: exactly four options;
* ``fill_in``: at least one sub-answer, and every stored ``correct_answer`` is
  nonblank after Python whitespace trimming (NULL counts as blank).

The 52 known defects are exact shapes, not blanket id or source exemptions. The
observed and expected maps must match exactly, so an unknown violation, a
changed shape on a known id and a resolved (stale) entry all fail with the
question id and rule. Every database here is a temporary clone built from
migrations + data/canonical by ``tests.support``; no runtime database is read.
"""

import sqlite3
import tempfile
import unittest
from collections import namedtuple
from contextlib import closing
from pathlib import Path

from tests.support import build_test_database  # first: safe in-memory app initialisation
from web.services import quiz as quiz_service
from web.services.quiz import QUIZ_APPROVED_FILTER

ONE_CORRECT = "one_correct_option"
FOUR_OPTIONS = "four_options"
FILL_IN_ANSWERS = "fill_in_answers"

Known = namedtuple("Known", "rules shape reason")


def _known(ids, rules, shape, reason):
    return {question_id: Known(rules, shape, reason) for question_id in ids}


# Remove an entry only when its question stops violating (repaired or no longer
# approved); the guard fails until it is removed. Shapes are written out here and
# never derived from the database.
KNOWN_EXCEPTIONS = {
    **_known(
        [329], (ONE_CORRECT,), "true_false, 2 options, 0 correct",
        "True/false question stored without a correct option; unresolved source defect "
        "(A2 quarantines it by unapproving, which is not a repair of the key).",
    ),
    **_known(
        [268, 326, 336, 354, 389, 392, 393, 394, 407, 452, 471, 485, 486, 487, 492, 493,
         499, 500, 504, 530],
        (FOUR_OPTIONS,), "multiple_choice, 3 options, 1 correct",
        "Approved multiple-choice question stored with three options; the quiz selector skips it.",
    ),
    **_known(
        [400], (FOUR_OPTIONS,), "multiple_choice, 2 options, 1 correct",
        "Approved multiple-choice question stored with two options; the quiz selector skips it.",
    ),
    **_known(
        [*range(37, 47), *range(81, 91), *range(171, 181)],
        (FILL_IN_ANSWERS,), "fill_in, 0 sub-answers, blank none",
        "Approved fill-in question stored without sub-answers; the quiz selector skips it.",
    ),
}


def scan_approved_questions(conn):
    """``{question_id: (rules, shape)}`` for every approved question that violates an invariant."""
    approved = conn.execute(
        f"SELECT q.id, q.question_type FROM questions q WHERE {QUIZ_APPROVED_FILTER} "
        "AND q.question_type IN ('multiple_choice', 'true_false', 'fill_in') ORDER BY q.id"
    ).fetchall()
    option_counts = {
        row[0]: (row[1], row[2]) for row in conn.execute(
            f"SELECT q.id, COUNT(o.id), COALESCE(SUM(CASE WHEN COALESCE(o.is_correct, 0) = 1 THEN 1 ELSE 0 END), 0) "
            f"FROM questions q LEFT JOIN multiple_choice_options o ON o.question_id = q.id "
            f"WHERE {QUIZ_APPROVED_FILTER} GROUP BY q.id")
    }
    answers = {}
    for question_id, number, answer in conn.execute(
            f"SELECT q.id, s.subquestion_number, s.correct_answer FROM questions q "
            f"JOIN fill_in_subquestions s ON s.question_id = q.id WHERE {QUIZ_APPROVED_FILTER} "
            "ORDER BY q.id, s.subquestion_number"):
        answers.setdefault(question_id, []).append((number, answer))

    found = {}
    for question_id, question_type in approved:
        if question_type == "fill_in":
            rows = answers.get(question_id, [])
            blank = [number for number, answer in rows if not (answer or "").strip()]
            if rows and not blank:
                continue
            blank_text = ", ".join(f"#{number}" for number in blank) or "none"
            found[question_id] = ((FILL_IN_ANSWERS,), f"fill_in, {len(rows)} sub-answers, blank {blank_text}")
            continue
        options, correct = option_counts[question_id]
        rules = []
        if correct != 1:
            rules.append(ONE_CORRECT)
        if question_type == "multiple_choice" and options != 4:
            rules.append(FOUR_OPTIONS)
        if rules:
            found[question_id] = (tuple(rules), f"{question_type}, {options} options, {correct} correct")
    return found


def integrity_failure(conn):
    """The guard: ``None`` when the approved questions match the exceptions exactly, else the diagnostics."""
    observed = scan_approved_questions(conn)
    problems = []
    for question_id in sorted(observed.keys() - KNOWN_EXCEPTIONS.keys()):
        rules, shape = observed[question_id]
        problems.append(f"question {question_id}: new approved-question violation of {', '.join(rules)} "
                        f"({shape}); repair or unapprove it")
    for question_id in sorted(observed.keys() & KNOWN_EXCEPTIONS.keys()):
        known = KNOWN_EXCEPTIONS[question_id]
        if observed[question_id] != (known.rules, known.shape):
            rules, shape = observed[question_id]
            problems.append(f"question {question_id}: known exception changed: expected "
                            f"{', '.join(known.rules)} ({known.shape}), now {', '.join(rules)} ({shape})")
    for question_id in sorted(KNOWN_EXCEPTIONS.keys() - observed.keys()):
        known = KNOWN_EXCEPTIONS[question_id]
        problems.append(f"question {question_id}: known exception for {', '.join(known.rules)} no longer "
                        "applies (repaired, unapproved or removed); retire its KNOWN_EXCEPTIONS entry")
    return "\n".join(problems) or None


class _TemporaryCanonical(unittest.TestCase):
    """Each test gets its own writable canonical clone; the template is never opened."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="learnpilot-integrity-")
        self.addCleanup(directory.cleanup)
        self.conn = sqlite3.connect(build_test_database(Path(directory.name) / "questions.db"))
        self.conn.row_factory = sqlite3.Row
        self.addCleanup(self.conn.close)

    def add_question(self, question_type, *, ai=0, quality=None, options=(), answers=()):
        """Insert a question. ``options`` are is_correct flags; ``answers`` are sub-answer texts."""
        question_id = self.conn.execute(
            "INSERT INTO questions (source_exam, question_type, points, prompt, is_ai_generated, quality_score) "
            "VALUES ('integrity-test', ?, 1, 'Test prompt', ?, ?)", (question_type, ai, quality)).lastrowid
        for index, is_correct in enumerate(options):
            letter = "ABCDEFGH"[index]
            self.conn.execute(
                "INSERT INTO multiple_choice_options (question_id, option_letter, option_text, is_correct) "
                "VALUES (?, ?, ?, ?)", (question_id, letter, f"Option {letter}", int(is_correct)))
        for number, answer in enumerate(answers, start=1):
            self.conn.execute("INSERT INTO fill_in_subquestions (question_id, subquestion_number, correct_answer) "
                              "VALUES (?, ?, ?)", (question_id, number, answer))
        return question_id

    def problem_lines(self):
        """The guard's diagnostics as ``{question_id: line}`` (empty when the guard passes)."""
        message = integrity_failure(self.conn)
        lines = {}
        for line in (message or "").splitlines():
            lines[int(line.split()[1].rstrip(":"))] = line
        return lines

    def assert_problems(self, expected):
        """Exactly these questions fail, each with the given text in its diagnostic."""
        lines = self.problem_lines()
        self.assertEqual(set(lines), set(expected), integrity_failure(self.conn))
        for question_id, fragments in expected.items():
            for fragment in fragments:
                self.assertIn(fragment, lines[question_id])


class CanonicalApprovedQuestionIntegrityTest(_TemporaryCanonical):
    def test_pristine_canonical_matches_exact_exception_map(self):
        message = integrity_failure(self.conn)
        if message:
            self.fail(f"approved canonical questions differ from KNOWN_EXCEPTIONS:\n{message}")

    def test_known_exceptions_are_approved_questions_hidden_from_the_quiz_selector(self):
        # Why the guard scans approval and not the quiz pool: these rows are approved
        # but the selector's own eligibility rules already skip them.
        for question_id, known in KNOWN_EXCEPTIONS.items():
            question_type = known.shape.split(",")[0]
            if question_type == "true_false":
                continue  # the selector never serves true/false questions
            with self.subTest(question=question_id):
                eligible = (quiz_service.quiz_multiple_choice_is_eligible if question_type == "multiple_choice"
                            else quiz_service.quiz_fill_in_is_eligible)
                self.assertFalse(eligible(self.conn, question_id))

    def test_new_malformed_choice_questions_fail_each_invariant(self):
        zero_correct = self.add_question("multiple_choice", options=[0, 0, 0, 0])
        two_correct = self.add_question("multiple_choice", options=[1, 1, 0, 0])
        five_options = self.add_question("multiple_choice", options=[1, 0, 0, 0, 0])
        both_broken = self.add_question("multiple_choice", options=[1, 1, 0])
        tf_zero = self.add_question("true_false", options=[0, 0])
        tf_two = self.add_question("true_false", options=[1, 1])
        self.assert_problems({
            zero_correct: ("new approved-question violation", ONE_CORRECT, "4 options, 0 correct"),
            two_correct: (ONE_CORRECT, "4 options, 2 correct"),
            five_options: (FOUR_OPTIONS, "5 options, 1 correct"),
            both_broken: (ONE_CORRECT, FOUR_OPTIONS, "3 options, 2 correct"),
            tf_zero: (ONE_CORRECT, "true_false, 2 options, 0 correct"),
            tf_two: (ONE_CORRECT, "true_false, 2 options, 2 correct"),
        })
        self.assertNotIn(FOUR_OPTIONS, self.problem_lines()[tf_zero])  # the option count is a multiple-choice rule

    def test_new_malformed_fill_in_questions_fail(self):
        no_answers = self.add_question("fill_in")
        empty = self.add_question("fill_in", answers=[""])
        whitespace = self.add_question("fill_in", answers=["  \t\n"])
        mixed = self.add_question("fill_in", answers=["42", " ", "7"])
        self.assert_problems({
            no_answers: (FILL_IN_ANSWERS, "0 sub-answers, blank none"),
            empty: (FILL_IN_ANSWERS, "1 sub-answers, blank #1"),
            whitespace: (FILL_IN_ANSWERS, "1 sub-answers, blank #1"),
            mixed: (FILL_IN_ANSWERS, "3 sub-answers, blank #2"),
        })

    def test_known_exception_that_gains_a_defect_or_changes_shape_fails(self):
        execute = self.conn.execute
        # 268: three options, one correct; a second correct option adds a defect.
        execute("UPDATE multiple_choice_options SET is_correct = 1 WHERE id = (SELECT MIN(id) "
                "FROM multiple_choice_options WHERE question_id = 268 AND is_correct = 0)")
        # 400: two options become three, still a four-option violation but another shape.
        execute("INSERT INTO multiple_choice_options (question_id, option_letter, option_text, is_correct) "
                "VALUES (400, 'Z', 'Extra', 0)")
        # 37: no sub-answers becomes one blank sub-answer.
        execute("INSERT INTO fill_in_subquestions (question_id, subquestion_number, correct_answer) "
                "VALUES (37, 1, '  ')")
        # 329: a third option while still having no correct one.
        execute("INSERT INTO multiple_choice_options (question_id, option_letter, option_text, is_correct) "
                "VALUES (329, 'Z', 'Extra', 0)")
        lines = self.problem_lines()
        self.assertEqual(set(lines), {268, 329, 400, 37}, integrity_failure(self.conn))
        for question_id in lines:
            self.assertIn("known exception changed", lines[question_id])
        self.assertIn(f"now {ONE_CORRECT}, {FOUR_OPTIONS} (multiple_choice, 3 options, 2 correct)", lines[268])
        self.assertIn("multiple_choice, 3 options, 1 correct", lines[400])
        self.assertIn("1 sub-answers, blank #1", lines[37])
        self.assertIn("true_false, 3 options, 0 correct", lines[329])

    def test_resolved_exception_fails_until_its_entry_is_retired(self):
        execute = self.conn.execute
        execute("UPDATE multiple_choice_options SET is_correct = 1 WHERE question_id = 329 AND option_letter = "
                "(SELECT MIN(option_letter) FROM multiple_choice_options WHERE question_id = 329)")  # repaired
        execute("INSERT INTO multiple_choice_options (question_id, option_letter, option_text, is_correct) "
                "VALUES (268, 'Z', 'Fourth', 0)")                                                    # repaired
        execute("INSERT INTO fill_in_subquestions (question_id, subquestion_number, correct_answer) "
                "VALUES (37, 1, 'answer')")                                                          # repaired
        execute("UPDATE questions SET is_ai_generated = 1, quality_score = 0.0 WHERE id = 394")      # unapproved
        execute("DELETE FROM questions WHERE id = 400")                                              # removed
        lines = self.problem_lines()
        self.assertEqual(set(lines), {329, 268, 37, 394, 400}, integrity_failure(self.conn))
        for question_id in lines:
            self.assertIn("no longer applies", lines[question_id])
            self.assertIn("retire its KNOWN_EXCEPTIONS entry", lines[question_id])

    def test_valid_new_approved_questions_pass_without_touching_totals(self):
        self.add_question("multiple_choice", options=[0, 0, 1, 0])
        self.add_question("true_false", options=[1, 0])
        self.add_question("fill_in", answers=["42", "0"])
        self.assertIsNone(integrity_failure(self.conn))

    def test_malformed_ai_questions_below_the_approval_threshold_are_excluded(self):
        for quality in (None, 0.0, 0.99):
            self.add_question("multiple_choice", ai=1, quality=quality, options=[0, 0, 0])
            self.add_question("true_false", ai=1, quality=quality, options=[0, 0])
            self.add_question("fill_in", ai=1, quality=quality)
        self.assertIsNone(integrity_failure(self.conn))
        # At the threshold the same AI question is approved and is scanned.
        at_threshold = self.add_question("multiple_choice", ai=1, quality=1.0, options=[0, 0, 0])
        self.assert_problems({at_threshold: (ONE_CORRECT, FOUR_OPTIONS)})

    def test_malformed_non_ai_question_with_low_quality_is_still_scanned(self):
        added = {}
        for quality in (None, 0.0, 0.5):
            added[self.add_question("multiple_choice", ai=0, quality=quality, options=[1, 0, 0])] = (FOUR_OPTIONS,)
            added[self.add_question("fill_in", ai=0, quality=quality, answers=[""])] = (FILL_IN_ANSWERS,)
        self.assert_problems(added)


if __name__ == "__main__":
    unittest.main()
