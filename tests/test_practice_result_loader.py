"""The practice result loader (C2, roadmap PR #370): ``web.services.practice.load_practice_result``.

Every database is a temporary clone of the migrations + canonical template with synthetic curriculum
rows on top (the fixtures of the start and submit service tests), so every plan, pool and verdict is
exactly known. Nothing here reads, copies or hashes ``data/questions.db``.

What is proved, and how:

* the outcome contract and validation, with ownership taken from the trusted principal only: a
  missing, a foreign, an open and an abandoned session are one indistinguishable value, for a teacher
  and an admin too, and an inactive account is its own outcome;
* start -> submit -> read round trips, the stored order and seed, original option keys that are not
  A-D, and the data shape of the shared multiple-choice partial (rendered, and compared key for key
  with the rows of the legacy quiz result loader);
* fail-closed stored state: every malformed plan and every plan/answer/score inconsistency raises,
  without a repair and without a write;
* persisted score, timestamp and verdicts are the only grading data: they survive every content
  change, while prompts, options, correct answers and explanations follow current content;
* stale and unavailable content: each eligibility exclusion as a pair with a control, an unavailable
  scope, an all-stale session that still returns its saved score, and questions that were ungraded at
  submission, which never reappear;
* one coherent read snapshot (a real writer on a separate connection between the loader's reads, in
  WAL mode, with a pass-through negative control and a rollback-journal lock check);
* read-only behavior: a ``query_only`` connection, explicit ``autocommit=True`` and
  ``isolation_level=None`` connections, a statement trace with no write, unchanged tables;
* transactions: a caller transaction is reused and never ended, on success and on error, while a
  transaction the loader began is always ended with explicit SQL, errors included.
"""

import hashlib
import json
import sqlite3
import unittest
from datetime import datetime
from typing import NamedTuple
from unittest.mock import patch

from tests.test_practice_start_service import MalformedPlanTest, STAMP
from tests.test_practice_submit_service import SubmitCase
from tests.support import web_app
from web import db as web_db
from web.access import ANONYMOUS, Principal, account_principal
from web.services import lifecycle, practice
from web.services.quiz import (
    QUIZ_NO_EXPLANATION_MESSAGE,
    quiz_load_questions,
    quiz_load_result_questions,
)

PARTIAL = "_multiple_choice_result_review.html"
VISUAL_PROMPT = "Разгледайте графиката и отговорете?"


class Done(NamedTuple):
    session_id: int
    plan: list
    submission: object
    scope: object


class ResultCase(SubmitCase):
    """Helpers over the start and submit fixtures."""

    def finish(self, who=None, scope=None, *, count=5, correct=3, wrong=1, seed="result-seed", topic_id=None,
               plan=None) -> Done:
        """A session of ``who`` stored, then really submitted: ``correct`` right answers, ``wrong`` wrong
        ones, the rest unanswered."""
        who = who or self.student
        scope = scope or self.make_scope(8)
        session_id, plan = self.open_session(who, scope, count, topic_id=topic_id, plan=plan, seed=seed)
        choices = {}
        for index, question_id in enumerate(plan):
            if index < correct:
                choices[question_id] = self.key(question_id)
            elif index < correct + wrong:
                choices[question_id] = self.key(question_id, correct=False)
        submission = self.submit(who, session_id, choices)
        self.assertEqual(submission.outcome, practice.OUTCOME_SUBMITTED)
        return Done(session_id, plan, submission, scope)

    def load(self, who, session_id, conn=None):
        return practice.load_practice_result(conn or self.conn, who, session_id)

    def change(self, sql, params=(), *, check_constraints=True, foreign_keys=False):
        """A committed write on its own connection (tampering included) as an operator or an old build might do."""
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(f"PRAGMA foreign_keys={'ON' if foreign_keys else 'OFF'}")
            if not check_constraints:
                conn.execute("PRAGMA ignore_check_constraints=ON")
            conn.execute(sql, params)
            conn.commit()
        finally:
            conn.close()

    def prompt_of(self, question_id):
        return self.raw().execute("SELECT prompt FROM questions WHERE id = ?", (question_id,)).fetchone()[0]

    def assert_released(self, conn=None):
        """No transaction on ``conn`` and no lock a writer on another connection would wait for."""
        self.assertFalse((conn or self.conn).in_transaction)
        probe = sqlite3.connect(self.db_path, timeout=0)
        self.addCleanup(probe.close)
        probe.execute("UPDATE users SET display_name = display_name WHERE id = ?", (self.student.user_id,))
        probe.commit()

    def every_table(self):
        return self.table_digests()

    def shown(self, result):
        return [question["id"] for question in result.questions]

    def autocommit_connection(self, *, query_only=False):
        conn = sqlite3.connect(self.db_path, timeout=30, autocommit=True)
        self.addCleanup(conn.close)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        if query_only:
            conn.execute("PRAGMA query_only=ON")
        return conn


# ====================================================================== outcome contract
class ContractTest(ResultCase):
    def test_a_loaded_result_is_the_persisted_session_and_nothing_else(self):
        scope = self.make_scope(8)
        session_id, plan = self.open_session(scope=scope, seed="the-seed")
        choices = {plan[0]: self.key(plan[0]), plan[1]: self.key(plan[1], correct=False),
                   plan[2]: self.key(plan[2])}                      # plan[3], plan[4]: unanswered
        with patch.object(lifecycle, "naive_utc_now", return_value=datetime(2026, 11, 20, 12, 0, 0)):
            submission = self.submit(self.student, session_id, choices)
        before = self.every_table()
        result = self.load(self.student, session_id)
        self.assertEqual(self.every_table(), before)
        self.assertEqual(result.outcome, practice.OUTCOME_LOADED)
        self.assertTrue(result.loaded)
        self.assertEqual((result.session_id, result.section_id, result.topic_id, result.submitted_at),
                         (session_id, scope.section_id, None, "2026-11-20 12:00:00"))
        self.assertEqual((result.score_correct, result.score_total), (2, 5))
        self.assertEqual((result.score_correct, result.score_total),
                         (submission.score_correct, submission.score_total))
        self.assertEqual(self.shown(result), plan)
        self.assertEqual([q["is_correct"] for q in result.questions], [True, False, True, False, False])
        self.assertEqual([q["chosen_letter"] for q in result.questions],
                         [choices.get(qid) for qid in plan])
        self.assertEqual((result.planned_question_count, result.graded_question_count,
                          result.ungraded_question_count, result.unavailable_question_count,
                          result.skipped_question_count, result.renderable_question_count), (5, 5, 0, 0, 0, 5))
        self.assertFalse(result.only_saved_score)
        self.assertIsNone(result.stale_attempt_message)
        self.assertEqual(self.load(self.student, session_id), result)            # repeatable

    def test_a_topic_scope_is_returned_as_stored(self):
        scope = self.make_scope(8, topics=2)
        done = self.finish(scope=scope, topic_id=scope.topic_ids[1], plan=scope.question_ids[1][:5])
        result = self.load(self.student, done.session_id)
        self.assertEqual((result.section_id, result.topic_id), (scope.section_id, scope.topic_ids[1]))
        self.assertEqual(self.shown(result), scope.question_ids[1][:5])

    def test_session_ids_must_be_positive_integers(self):
        done = self.finish()
        for bad in (0, -1, True, False, "1", 1.0, None, 2 ** 63, b"1", [1]):
            with self.subTest(session_id=bad):
                with self.assertRaises(ValueError):
                    self.load(self.student, bad)
        self.assertFalse(self.conn.in_transaction)
        self.assertEqual(self.load(self.student, done.session_id).outcome, practice.OUTCOME_LOADED)

    def test_only_an_account_principal_can_load_a_result(self):
        done = self.finish()
        principals = {
            "anonymous": ANONYMOUS,
            "a string": "student",
            "none": None,
            "a dict": {"user_id": self.student.user_id},
            "teacher capability without an id": Principal(is_teacher=True, is_admin=False),
            "admin capability without an id": Principal(is_teacher=False, is_admin=True),
            "unknown role": Principal(is_teacher=False, is_admin=False, user_id=self.student.user_id, role="parent"),
            "no role": Principal(is_teacher=False, is_admin=False, user_id=self.student.user_id),
            "boolean id": Principal(is_teacher=False, is_admin=False, user_id=True, role="student"),
            "zero id": Principal(is_teacher=False, is_admin=False, user_id=0, role="student"),
            "negative id": Principal(is_teacher=False, is_admin=False, user_id=-4, role="student"),
            "oversized id": Principal(is_teacher=False, is_admin=False, user_id=2 ** 63, role="student"),
        }
        for name, principal in principals.items():
            with self.subTest(name):
                with self.assertRaises(practice.PracticeAccessError):
                    self.load(principal, done.session_id)
                self.assertFalse(self.conn.in_transaction)

    def test_an_unconfigured_connection_is_refused_before_any_transaction(self):
        done = self.finish()
        plain = sqlite3.connect(self.db_path)                     # no sqlite3.Row
        self.addCleanup(plain.close)
        no_foreign_keys = sqlite3.connect(self.db_path)
        self.addCleanup(no_foreign_keys.close)
        no_foreign_keys.row_factory = sqlite3.Row
        for name, conn in (("plain rows", plain), ("foreign keys off", no_foreign_keys)):
            with self.subTest(name):
                with self.assertRaises(RuntimeError):
                    self.load(self.student, done.session_id, conn=conn)
                self.assertFalse(conn.in_transaction)

    def test_the_principal_is_validated_before_the_connection_is_touched(self):
        class Boom:
            def __getattr__(self, name):
                raise AssertionError("the connection must not be touched")
        with self.assertRaises(practice.PracticeAccessError):
            practice.load_practice_result(Boom(), ANONYMOUS, 1)
        with self.assertRaises(ValueError):
            practice.load_practice_result(Boom(), self.student, 0)

    def test_the_result_properties_and_defaults(self):
        empty = practice.PracticeResult(practice.OUTCOME_NOT_FOUND)
        self.assertFalse(empty.loaded)
        self.assertFalse(empty.only_saved_score)               # not loaded: nothing to say about a score
        self.assertEqual(empty.questions, ())
        self.assertEqual(empty.review_context(), {"questions": [], "stale_attempt_message": None})


# ====================================================================== ownership and accounts
class OwnershipTest(ResultCase):
    def test_missing_foreign_open_and_abandoned_sessions_are_one_indistinguishable_value(self):
        scope = self.make_scope(8)
        theirs = self.finish(self.other_student, scope)
        mine_open, _plan = self.open_session(self.student, scope)
        self.assertEqual(mine_open, theirs.session_id + 1)
        # one open session per account and scope: the abandoned one lives in a section of its own
        other_scope = self.make_scope(8)
        mine_abandoned = self.insert_session(self.student, other_scope, other_scope.all_question_ids[:5],
                                             abandoned="2026-01-02 10:00:00")
        unavailable = practice.PracticeResult(practice.OUTCOME_NOT_FOUND)
        cases = {
            "missing": self.load(self.student, 99999),
            "foreign student": self.load(self.student, theirs.session_id),
            "teacher on a student's session": self.load(self.teacher, theirs.session_id),
            "admin on a student's session": self.load(self.admin, theirs.session_id),
            "own open session": self.load(self.student, mine_open),
            "own abandoned session": self.load(self.student, mine_abandoned),
        }
        for name, result in cases.items():
            with self.subTest(name):
                self.assertEqual(result, unavailable)
                self.assertEqual(tuple(result), tuple(unavailable))
        self.assertEqual(self.load(self.other_student, theirs.session_id).outcome, practice.OUTCOME_LOADED)

    def test_a_forged_capability_flag_is_no_shortcut_ownership_is_the_id(self):
        done = self.finish(self.student)
        forged = Principal(is_teacher=True, is_admin=True, user_id=self.other_student.user_id, role="admin")
        self.assertEqual(self.load(forged, done.session_id), practice.PracticeResult(practice.OUTCOME_NOT_FOUND))

    def test_a_teacher_and_an_admin_load_their_own_sessions_without_a_profile(self):
        for who in (self.teacher, self.admin, self.student):
            with self.subTest(role=who.role):
                done = self.finish(who)
                self.assertEqual(self.load(who, done.session_id).session_id, done.session_id)
        with self.raw() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM users WHERE profile_required_at IS NOT NULL")
                             .fetchone()[0], 0)

    def test_each_account_reads_only_its_own_answers_for_the_same_scope(self):
        scope = self.make_scope(8)
        mine = self.finish(self.student, scope, correct=5, wrong=0)
        theirs = self.finish(self.other_student, scope, correct=0, wrong=5)
        self.assertEqual(self.load(self.student, mine.session_id).score_correct, 5)
        self.assertEqual(self.load(self.other_student, theirs.session_id).score_correct, 0)
        self.assertEqual([q["is_correct"] for q in self.load(self.other_student, theirs.session_id).questions],
                         [False] * 5)

    def test_a_repeat_session_shows_its_own_answers(self):
        scope = self.make_scope(8)
        first = self.finish(self.student, scope, correct=5, wrong=0)
        second = self.finish(self.student, scope, correct=1, wrong=4, plan=list(reversed(first.plan)))
        self.assertEqual([q["is_correct"] for q in self.load(self.student, first.session_id).questions], [True] * 5)
        reread = self.load(self.student, second.session_id)
        self.assertEqual(self.shown(reread), list(reversed(first.plan)))
        self.assertEqual([q["is_correct"] for q in reread.questions], [True, False, False, False, False])


class AccountTest(ResultCase):
    def test_an_inactive_account_gets_its_own_outcome_and_nothing_else(self):
        done = self.finish()
        self.change("UPDATE users SET is_active = 0 WHERE id = ?", (self.student.user_id,))
        self.assertEqual(self.load(self.student, done.session_id),
                         practice.PracticeResult(practice.OUTCOME_ACCOUNT_UNAVAILABLE))
        self.change("UPDATE users SET is_active = 1 WHERE id = ?", (self.student.user_id,))
        self.assertEqual(self.load(self.student, done.session_id).outcome, practice.OUTCOME_LOADED)

    def test_a_missing_account_and_an_unknown_stored_role_are_unavailable(self):
        done = self.finish()
        ghost = account_principal(987654, "student", "x")
        self.assertEqual(self.load(ghost, done.session_id).outcome, practice.OUTCOME_ACCOUNT_UNAVAILABLE)
        self.change("UPDATE users SET role = 'parent' WHERE id = ?", (self.student.user_id,), check_constraints=False)
        self.assertEqual(self.load(self.student, done.session_id).outcome, practice.OUTCOME_ACCOUNT_UNAVAILABLE)

    def test_activity_is_the_stored_one_not_the_principals_claim(self):
        done = self.finish(self.teacher)
        self.change("UPDATE users SET is_active = 0 WHERE id = ?", (self.teacher.user_id,))
        stale_principal = account_principal(self.teacher.user_id, "teacher", "x")      # still claims to be signed in
        self.assertEqual(self.load(stale_principal, done.session_id).outcome, practice.OUTCOME_ACCOUNT_UNAVAILABLE)

    def test_an_inactive_account_is_checked_before_the_session_is_looked_up(self):
        done = self.finish(self.other_student)
        self.change("UPDATE users SET is_active = 0 WHERE id = ?", (self.student.user_id,))
        self.assertEqual(self.load(self.student, done.session_id).outcome, practice.OUTCOME_ACCOUNT_UNAVAILABLE)
        self.assertEqual(self.load(self.student, 99999).outcome, practice.OUTCOME_ACCOUNT_UNAVAILABLE)


# ====================================================================== start -> submit -> read
class SubmitReadTest(ResultCase):
    def test_the_session_made_by_the_start_service_and_submitted_reads_back(self):
        scope = self.make_scope(12)
        started = self.start(self.student, scope)
        choices = {qid: self.key(qid, correct=index % 2 == 0) for index, qid in enumerate(started.question_ids)}
        submission = self.submit(self.student, started.session_id, choices)
        result = self.load(self.student, started.session_id)
        self.assertEqual(result.session_id, started.session_id)
        self.assertEqual(self.shown(result), list(started.question_ids))
        self.assertEqual((result.score_correct, result.score_total, result.submitted_at),
                         (submission.score_correct, submission.score_total, submission.submitted_at))
        self.assertEqual([(q["id"], q["chosen_letter"], q["is_correct"]) for q in result.questions],
                         [(a.question_id, a.chosen_letter, a.is_correct) for a in submission.answers])
        self.assertEqual(result.score_correct, 5)
        self.assertEqual(self.start(self.student, scope).outcome, practice.OUTCOME_CREATED)   # "Нов опит"

    def test_a_topic_session_started_submitted_and_read(self):
        scope = self.make_scope(7, topics=2)
        started = self.start(self.student, scope, scope.topic_ids[0])
        self.submit(self.student, started.session_id, self.all_correct(started.question_ids))
        result = self.load(self.student, started.session_id)
        self.assertEqual((result.topic_id, result.score_correct, result.score_total), (scope.topic_ids[0], 7, 7))
        self.assertEqual(set(self.shown(result)), set(scope.question_ids[0]))

    def test_open_then_submitted_is_unavailable_then_loaded(self):
        scope = self.make_scope(8)
        started = self.start(self.student, scope)
        self.assertEqual(self.load(self.student, started.session_id).outcome, practice.OUTCOME_NOT_FOUND)
        self.submit(self.student, started.session_id, self.all_correct(started.question_ids))
        self.assertEqual(self.load(self.student, started.session_id).outcome, practice.OUTCOME_LOADED)

    def test_a_double_submit_reads_the_one_stored_result(self):
        done = self.finish()
        again = self.submit(self.student, done.session_id, self.all_correct(done.plan))
        self.assertEqual(again.outcome, practice.OUTCOME_ALREADY_SUBMITTED)
        result = self.load(self.student, done.session_id)
        self.assertEqual((result.score_correct, result.score_total), (3, 5))

    def test_the_stored_plan_order_is_kept_not_id_order_or_a_reshuffle(self):
        scope = self.make_scope(8)
        plan = [scope.all_question_ids[i] for i in (4, 0, 3, 1, 2)]
        done = self.finish(scope=scope, plan=plan, correct=2, wrong=1)
        self.assertEqual(self.shown(self.load(self.student, done.session_id)), plan)

    def test_options_follow_the_stored_seed_with_the_original_keys(self):
        scope = self.make_scope(8)
        done = self.finish(scope=scope, seed="seed-one", correct=5, wrong=0)
        result = self.load(self.student, done.session_id)
        expected = quiz_load_questions(self.conn, done.plan, "seed-one", include_correct=True)
        self.assertEqual([[o["option_letter"] for o in q["options"]] for q in result.questions],
                         [[o["option_letter"] for o in q["options"]] for q in expected])
        self.assertEqual([[o["display_letter"] for o in q["options"]] for q in result.questions],
                         [["А", "Б", "В", "Г"]] * 5)
        other = quiz_load_questions(self.conn, done.plan, "seed-two", include_correct=True)
        self.assertNotEqual([[o["option_letter"] for o in q["options"]] for q in other],
                            [[o["option_letter"] for o in q["options"]] for q in expected])    # the seed is what matters

    def test_option_keys_that_are_not_a_to_d_are_the_identity_and_display_letters_play_no_part(self):
        scope = self.make_scope(4)
        odd = self.keyed_question(scope.topic_ids[0], ["w", "x", "y", "z"], "y")
        self.conn.commit()
        plan = scope.all_question_ids + [odd]
        session_id, _ = self.open_session(scope=scope, plan=plan)
        self.submit(self.student, session_id, {odd: "x", plan[0]: self.key(plan[0])})
        result = self.load(self.student, session_id)
        row = result.questions[-1]
        self.assertEqual((row["id"], row["chosen_letter"], row["is_correct"]), (odd, "x", False))
        self.assertEqual(sorted(o["option_letter"] for o in row["options"]), ["w", "x", "y", "z"])
        self.assertEqual(row["chosen_text"], next(o["option_text"] for o in row["options"] if o["option_letter"] == "x"))
        self.assertEqual(row["correct_text"], next(o["option_text"] for o in row["options"] if o["option_letter"] == "y"))

    def test_unanswered_questions_show_no_chosen_key_and_the_stored_wrong_verdict(self):
        done = self.finish(correct=1, wrong=0)
        rows = self.load(self.student, done.session_id).questions
        self.assertEqual([(q["chosen_letter"], q["chosen_text"], q["is_correct"]) for q in rows[1:]],
                         [(None, None, False)] * 4)

    def test_a_session_of_fewer_than_five_questions_reads_back(self):
        done = self.finish(count=1, correct=1, wrong=0)
        result = self.load(self.student, done.session_id)
        self.assertEqual((result.score_correct, result.score_total, len(result.questions)), (1, 1, 1))


# ====================================================================== the shared partial's data shape
class PartialShapeTest(ResultCase):
    def test_a_row_has_exactly_the_keys_of_the_legacy_result_loader(self):
        done = self.finish()
        legacy = quiz_load_result_questions(self.conn, {"id": -1}, done.plan, "result-seed")
        result = self.load(self.student, done.session_id)
        self.assertEqual({tuple(sorted(row)) for row in result.questions}, {tuple(sorted(row)) for row in legacy})
        for row in result.questions:
            self.assertEqual(sorted(row["options"][0]), sorted(legacy[0]["options"][0]))
            self.assertIsInstance(row["is_correct"], bool)
            self.assertIsInstance(row["feedback_explanation"], str)

    def test_nothing_assignment_profile_token_or_teacher_shaped_is_returned(self):
        done = self.finish()
        result = self.load(self.student, done.session_id)
        forbidden = ("assignment", "token", "student_name", "profile", "teacher", "user")
        for field in result._fields:
            self.assertFalse(any(word in field for word in forbidden), field)
        self.assertEqual(set(result.review_context()), {"questions", "stale_attempt_message"})

    def render(self, result):
        app = web_app.app
        context = dict(result.review_context(), question_difficulty_label=web_app.question_difficulty_label)
        with app.test_request_context("/"):
            return app.jinja_env.get_template(PARTIAL).render(**context)

    def test_the_shared_partial_renders_the_stored_verdicts_and_the_current_content(self):
        done = self.finish(correct=2, wrong=1)
        result = self.load(self.student, done.session_id)
        html = self.render(result)
        self.assertEqual(html.count('class="card question-card correct"'), 2)
        self.assertEqual(html.count('class="card question-card incorrect"'), 3)
        self.assertEqual(html.count("Вярно"), 2)
        self.assertEqual(html.count("Грешно"), 3)
        for question in result.questions:
            self.assertIn(question["prompt"], html)
        self.assertEqual(html.count("Няма избран отговор."), 2)
        self.assertNotIn("Моля, създайте нов тест", html)

    def test_an_only_saved_score_result_renders_just_the_warning(self):
        done = self.finish()
        for question_id in done.plan:
            self.break_question(question_id)
        self.conn.commit()
        result = self.load(self.student, done.session_id)
        html = self.render(result)
        self.assertIn(practice.PRACTICE_STALE_RESULT_MESSAGE, html)
        self.assertNotIn("question-card", html)


# ====================================================================== stored state fails closed
class StoredStateTest(ResultCase):
    def assert_fails_closed(self, error, session_id, who=None):
        before = self.every_table()
        with self.assertRaises(error):
            self.load(who or self.student, session_id)
        self.assertEqual(self.every_table(), before)
        self.assert_released()

    def test_every_malformed_plan_of_a_submitted_session_fails_closed(self):
        for name, raw in MalformedPlanTest.MALFORMED.items():
            with self.subTest(name):
                done = self.finish(scope=self.make_scope(8), correct=1, wrong=0, count=2)
                self.change("UPDATE practice_sessions SET question_ids_json = ? WHERE id = ?", (raw, done.session_id))
                self.assert_fails_closed(practice.PracticePlanError, done.session_id)

    def test_a_malformed_plan_is_a_state_error_too_and_names_its_session(self):
        done = self.finish()
        self.change("UPDATE practice_sessions SET question_ids_json = 'nope' WHERE id = ?", (done.session_id,))
        with self.assertRaises(practice.PracticeStateError) as caught:
            self.load(self.student, done.session_id)
        self.assertEqual(caught.exception.session_id, done.session_id)

    def test_an_open_or_abandoned_session_with_a_malformed_plan_is_still_just_unavailable(self):
        scope = self.make_scope(8)
        opened = self.insert_session(self.student, scope, "garbage")
        other = self.make_scope(8)
        abandoned = self.insert_session(self.student, other, "garbage", abandoned="2026-01-02 10:00:00")
        self.assertEqual(self.load(self.student, opened).outcome, practice.OUTCOME_NOT_FOUND)
        self.assertEqual(self.load(self.student, abandoned).outcome, practice.OUTCOME_NOT_FOUND)

    def test_a_malformed_plan_of_a_foreign_session_is_the_same_unavailable_outcome(self):
        done = self.finish(self.other_student)
        self.change("UPDATE practice_sessions SET question_ids_json = 'nope' WHERE id = ?", (done.session_id,))
        self.assertEqual(self.load(self.student, done.session_id), practice.PracticeResult(practice.OUTCOME_NOT_FOUND))

    def test_a_well_formed_but_stale_plan_is_not_malformed(self):
        done = self.finish(count=2, correct=1, wrong=0)
        self.change("UPDATE practice_sessions SET question_ids_json = '[2147483647, 2147483646]' WHERE id = ?",
                    (done.session_id,))
        # the answers are outside this plan: that is an inconsistency, not a stale plan
        self.assert_fails_closed(practice.PracticeStateError, done.session_id)

    def test_an_answer_outside_the_plan_is_refused(self):
        done = self.finish(count=4, correct=1, wrong=0)
        self.change("UPDATE practice_sessions SET question_ids_json = ? WHERE id = ?",
                    (json.dumps(done.plan[:3]), done.session_id))
        self.assert_fails_closed(practice.PracticeStateError, done.session_id)

    def test_no_answers_at_all_is_refused(self):
        done = self.finish()
        self.change("DELETE FROM practice_answers WHERE session_id = ?", (done.session_id,))
        self.assert_fails_closed(practice.PracticeStateError, done.session_id)

    def test_a_missing_answer_row_is_refused_not_read_as_unanswered(self):
        done = self.finish()
        self.change("DELETE FROM practice_answers WHERE session_id = ? AND question_id = ?",
                    (done.session_id, done.plan[2]))
        self.assert_fails_closed(practice.PracticeStateError, done.session_id)

    def test_an_extra_answer_row_is_refused(self):
        scope = self.make_scope(8)
        done = self.finish(scope=scope, count=4, correct=1, wrong=0)
        extra = scope.all_question_ids[5]
        self.change("UPDATE practice_sessions SET question_ids_json = ? WHERE id = ?",
                    (json.dumps(done.plan + [extra]), done.session_id))
        self.change("INSERT INTO practice_answers VALUES (?, ?, 'A', 0)", (done.session_id, extra))
        self.assert_fails_closed(practice.PracticeStateError, done.session_id)

    def test_a_score_that_disagrees_with_the_stored_verdicts_is_refused_not_rewritten(self):
        for name, column, value in (("correct too high", "score_correct", 4), ("correct too low", "score_correct", 1),
                                    ("total too high", "score_total", 6), ("total too low", "score_total", 4)):
            with self.subTest(name):
                done = self.finish()
                self.change(f"UPDATE practice_sessions SET {column} = ? WHERE id = ?", (value, done.session_id))
                self.assert_fails_closed(practice.PracticeStateError, done.session_id)
                with self.raw() as conn:
                    self.assertEqual(conn.execute(f"SELECT {column} FROM practice_sessions WHERE id = ?",
                                                  (done.session_id,)).fetchone()[0], value)       # not repaired

    def test_a_score_the_schema_would_refuse_is_refused_here_too(self):
        for name, sets in (("zero total", "score_correct = 0, score_total = 0"),
                           ("correct above total", "score_correct = 9, score_total = 5"),
                           ("negative correct", "score_correct = -1, score_total = 5"),
                           ("fractional total", "score_total = 5.5"),
                           ("text total", "score_total = 'five'"),
                           ("null score", "score_correct = NULL, score_total = NULL")):
            with self.subTest(name):
                done = self.finish()
                self.change(f"UPDATE practice_sessions SET {sets} WHERE id = ?", (done.session_id,),
                            check_constraints=False)
                self.assert_fails_closed(practice.PracticeStateError, done.session_id)

    def test_a_submitted_and_abandoned_session_is_refused(self):
        done = self.finish()
        self.change("UPDATE practice_sessions SET abandoned_at = ? WHERE id = ?", (STAMP, done.session_id),
                    check_constraints=False)
        self.assert_fails_closed(practice.PracticeStateError, done.session_id)

    def test_a_submitted_session_with_a_non_text_timestamp_is_refused(self):
        done = self.finish()
        self.change("UPDATE practice_sessions SET submitted_at = X'35' WHERE id = ?", (done.session_id,),
                    check_constraints=False)
        self.assert_fails_closed(practice.PracticeStateError, done.session_id)

    def test_a_stored_verdict_that_is_not_zero_or_one_is_refused(self):
        for bad in (2, -1):
            with self.subTest(verdict=bad):
                done = self.finish(correct=1, wrong=0, count=2)
                self.change("UPDATE practice_answers SET is_correct = ? WHERE session_id = ? AND question_id = ?",
                            (bad, done.session_id, done.plan[0]), check_constraints=False)
                self.assert_fails_closed(practice.PracticeStateError, done.session_id)

    def test_a_correct_verdict_without_a_chosen_key_is_refused(self):
        done = self.finish(correct=1, wrong=0, count=2)
        self.change("UPDATE practice_answers SET chosen_letter = NULL WHERE session_id = ? AND question_id = ?",
                    (done.session_id, done.plan[0]))
        self.assert_fails_closed(practice.PracticeStateError, done.session_id)

    def test_a_chosen_key_that_is_not_text_is_refused(self):
        done = self.finish(correct=0, wrong=1, count=2)
        self.change("UPDATE practice_answers SET chosen_letter = X'41' WHERE session_id = ? AND question_id = ?",
                    (done.session_id, done.plan[0]), check_constraints=False)
        self.assert_fails_closed(practice.PracticeStateError, done.session_id)

    def test_one_damaged_session_does_not_affect_another_of_the_same_account(self):
        scope = self.make_scope(8)
        good = self.finish(scope=scope)
        bad = self.finish(scope=self.make_scope(8))
        self.change("UPDATE practice_sessions SET question_ids_json = 'nope' WHERE id = ?", (bad.session_id,))
        self.assert_fails_closed(practice.PracticePlanError, bad.session_id)
        self.assertEqual(self.load(self.student, good.session_id).outcome, practice.OUTCOME_LOADED)


# ====================================================================== persisted grading, current content
class ContentChangeTest(ResultCase):
    def add_explanation_columns(self):
        self.change("ALTER TABLE questions ADD COLUMN explanation TEXT")
        self.change("ALTER TABLE multiple_choice_options ADD COLUMN explanation TEXT")

    def test_score_timestamp_and_verdicts_survive_every_content_change(self):
        done = self.finish(correct=3, wrong=2)
        before = self.load(self.student, done.session_id)
        for qid in done.plan:                                    # every correct option moves, every text changes
            self.change("UPDATE multiple_choice_options SET is_correct = 1 - is_correct WHERE question_id = ?"
                        " AND option_letter IN (SELECT option_letter FROM multiple_choice_options WHERE question_id = ?"
                        " AND option_letter IN ('A', 'B'))", (qid, qid))
        self.change("UPDATE questions SET prompt = 'Нов текст ' || id, difficulty = 'hard'")
        self.change("UPDATE multiple_choice_options SET option_text = 'Нов ' || option_letter || question_id")
        after = self.load(self.student, done.session_id)
        self.assertEqual((after.session_id, after.submitted_at, after.score_correct, after.score_total),
                         (before.session_id, before.submitted_at, before.score_correct, before.score_total))
        self.assertEqual([(q["id"], q["chosen_letter"], q["is_correct"]) for q in after.questions],
                         [(q["id"], q["chosen_letter"], q["is_correct"]) for q in before.questions])
        # ...while the content is the current one
        self.assertEqual([q["prompt"] for q in after.questions], [f"Нов текст {qid}" for qid in done.plan])
        self.assertTrue(all(o["option_text"].startswith("Нов ") for q in after.questions for o in q["options"]))
        self.assertNotEqual([q["prompt"] for q in before.questions], [q["prompt"] for q in after.questions])

    def test_a_verdict_stays_even_when_the_chosen_option_is_no_longer_the_correct_one(self):
        done = self.finish(correct=1, wrong=0, count=2)
        question = done.plan[0]
        chosen = self.key(question)
        self.change("UPDATE multiple_choice_options SET is_correct = (option_letter != ?) AND option_letter = "
                    "(SELECT MIN(option_letter) FROM multiple_choice_options WHERE question_id = ? AND option_letter != ?)"
                    " WHERE question_id = ?", (chosen, question, chosen, question))
        row = self.load(self.student, done.session_id).questions[0]
        correct_option = [o for o in row["options"] if o["is_correct"]]
        self.assertEqual(len(correct_option), 1)
        self.assertNotEqual(correct_option[0]["option_letter"], chosen)         # the current correct key moved
        self.assertTrue(row["is_correct"])                                      # the stored verdict did not
        self.assertEqual(row["correct_text"], correct_option[0]["option_text"])

    def test_a_chosen_key_that_no_longer_exists_keeps_its_verdict_and_shows_no_text(self):
        done = self.finish(correct=1, wrong=0, count=2)
        question, chosen = done.plan[0], self.key(done.plan[0])
        self.change("UPDATE multiple_choice_options SET option_letter = 'Z' WHERE question_id = ? AND option_letter = ?",
                    (question, chosen))
        row = self.load(self.student, done.session_id).questions[0]
        self.assertEqual((row["id"], row["chosen_letter"], row["chosen_text"], row["is_correct"]),
                         (question, chosen, None, True))

    def test_explanations_come_from_current_content_option_first_then_question_then_the_default(self):
        self.add_explanation_columns()
        done = self.finish(correct=0, wrong=3, count=4)
        wrong_keys = {qid: self.key(qid, correct=False) for qid in done.plan[:3]}
        first, second, third, unanswered = done.plan
        self.change("UPDATE multiple_choice_options SET explanation = 'Обяснение на избора' "
                    "WHERE question_id = ? AND option_letter = ?", (first, wrong_keys[first]))
        self.change("UPDATE questions SET explanation = 'Общо обяснение' WHERE id IN (?, ?, ?)",
                    (first, second, unanswered))
        rows = {q["id"]: q for q in self.load(self.student, done.session_id).questions}
        self.assertEqual(rows[first]["feedback_explanation"], "Обяснение на избора")
        self.assertEqual(rows[second]["feedback_explanation"], "Общо обяснение")
        self.assertEqual(rows[third]["feedback_explanation"], QUIZ_NO_EXPLANATION_MESSAGE)
        self.assertEqual(rows[unanswered]["feedback_explanation"], "Общо обяснение")
        self.change("UPDATE questions SET explanation = 'Променено обяснение' WHERE id = ?", (second,))
        again = {q["id"]: q for q in self.load(self.student, done.session_id).questions}
        self.assertEqual(again[second]["feedback_explanation"], "Променено обяснение")      # not a snapshot

    def test_no_explanation_columns_is_the_default_message(self):
        done = self.finish()
        self.assertEqual({q["feedback_explanation"] for q in self.load(self.student, done.session_id).questions},
                         {QUIZ_NO_EXPLANATION_MESSAGE})

    def test_a_visual_question_follows_its_current_figure(self):
        image = self.directory / "figure.png"
        image.write_bytes(b"not-a-real-png-but-it-exists")
        scope = self.make_scope(4)
        visual = self.good_question(scope.topic_ids[0], prompt=VISUAL_PROMPT)
        self.conn.commit()
        self.change("UPDATE questions SET image_path = ? WHERE id = ?", (str(image), visual))
        plan = scope.all_question_ids + [visual]
        session_id, _ = self.open_session(scope=scope, plan=plan)
        self.assertEqual(self.submit(self.student, session_id, self.all_correct(plan)).outcome, practice.OUTCOME_SUBMITTED)
        rows = {q["id"]: q for q in self.load(self.student, session_id).questions}
        self.assertEqual(sorted(rows), sorted(plan))
        self.assertTrue(all(isinstance(row["visual_assets"], list) for row in rows.values()))   # the partial's input
        image.unlink()                                                    # the figure vanishes: the question is unrenderable
        result = self.load(self.student, session_id)
        self.assertNotIn(visual, self.shown(result))
        self.assertEqual((result.score_correct, result.score_total, result.skipped_question_count), (5, 5, 1))


# ====================================================================== stale and unavailable content
class StaleContentTest(ResultCase):
    """Each exclusion as a pair: applied, the question is omitted and the saved score is untouched; reverted, it is back."""

    def mutations(self, qid, scope):
        other = self.make_scope(1)
        original_topic = scope.topic_ids[0]

        def sql(apply_sql, revert_sql, params=()):
            return (lambda: self.change(apply_sql, params), lambda: self.change(revert_sql, params))

        def dzi_legacy():
            self.make_dzi_legacy(qid)
            self.conn.commit()

        def dzi_task():
            self.make_dzi_task(qid)
            self.conn.commit()

        return {
            "unapproved AI question": sql(
                f"UPDATE questions SET is_ai_generated = 1, quality_score = 0.4 WHERE id = {qid}",
                f"UPDATE questions SET is_ai_generated = 0, quality_score = NULL WHERE id = {qid}"),
            "no longer multiple choice": sql(
                f"UPDATE questions SET question_type = 'fill_in' WHERE id = {qid}",
                f"UPDATE questions SET question_type = 'multiple_choice' WHERE id = {qid}"),
            "blank option text": sql(
                f"UPDATE multiple_choice_options SET option_text = '-' WHERE question_id = {qid} AND option_letter = 'B'",
                f"UPDATE multiple_choice_options SET option_text = 'back' WHERE question_id = {qid} AND option_letter = 'B'"),
            "no correct option": sql(
                f"UPDATE multiple_choice_options SET is_correct = 0 WHERE question_id = {qid}",
                f"UPDATE multiple_choice_options SET is_correct = (option_letter = 'A') WHERE question_id = {qid}"),
            "two correct options": sql(
                f"UPDATE multiple_choice_options SET is_correct = 1 WHERE question_id = {qid}",
                f"UPDATE multiple_choice_options SET is_correct = (option_letter = 'A') WHERE question_id = {qid}"),
            "official DZI question (legacy exam)": (dzi_legacy, lambda: self.change(
                f"UPDATE questions SET exam_id = NULL WHERE id = {qid}")),
            "official DZI question (task link)": (dzi_task, lambda: self.change(
                f"DELETE FROM exam_task_questions WHERE question_id = {qid}")),
            "moved to another section": sql(
                f"UPDATE questions SET topic_id = {other.topic_ids[0]} WHERE id = {qid}",
                f"UPDATE questions SET topic_id = {original_topic} WHERE id = {qid}"),
            "owned by another subject": sql(
                f"UPDATE questions SET subject = 'physics' WHERE id = {qid}",
                f"UPDATE questions SET subject = 'it' WHERE id = {qid}"),
            "visual prompt without an image": sql(
                f"UPDATE questions SET prompt = '{VISUAL_PROMPT}', image_path = NULL WHERE id = {qid}",
                f"UPDATE questions SET prompt = 'restored' WHERE id = {qid}"),
        }

    def test_each_exclusion_omits_only_that_question_and_never_changes_the_saved_score(self):
        scope = self.make_scope(8)
        done = self.finish(scope=scope, correct=3, wrong=1)
        target = done.plan[1]
        before = self.load(self.student, done.session_id)
        self.assertEqual(self.shown(before), done.plan)
        for name, (apply, revert) in self.mutations(target, scope).items():
            with self.subTest(name):
                apply()
                after = self.load(self.student, done.session_id)
                self.assertEqual(self.shown(after), [qid for qid in done.plan if qid != target])
                self.assertEqual((after.session_id, after.submitted_at, after.score_correct, after.score_total),
                                 (before.session_id, before.submitted_at, before.score_correct, before.score_total))
                self.assertEqual((after.planned_question_count, after.graded_question_count,
                                  after.ungraded_question_count, after.unavailable_question_count,
                                  after.skipped_question_count), (5, 5, 0, 1, 1))
                self.assertFalse(after.only_saved_score)
                self.assertIsNone(after.stale_attempt_message)
                kept = {q["id"]: q for q in before.questions}
                for question in after.questions:                         # the others keep their stored verdicts
                    self.assertEqual((question["chosen_letter"], question["is_correct"]),
                                     (kept[question["id"]]["chosen_letter"], kept[question["id"]]["is_correct"]))
                revert()
                back = self.load(self.student, done.session_id)
                self.assertEqual(self.shown(back), done.plan)                # the control: it was this change
                self.assertEqual(back.skipped_question_count, 0)

    def test_a_topic_session_drops_a_question_moved_to_a_sibling_topic(self):
        scope = self.make_scope(8, topics=2)
        done = self.finish(scope=scope, topic_id=scope.topic_ids[0], plan=scope.question_ids[0][:5])
        target = done.plan[2]
        self.change("UPDATE questions SET topic_id = ? WHERE id = ?", (scope.topic_ids[1], target))
        result = self.load(self.student, done.session_id)
        self.assertEqual(self.shown(result), [qid for qid in done.plan if qid != target])
        self.change("UPDATE questions SET topic_id = ? WHERE id = ?", (scope.topic_ids[0], target))
        self.assertEqual(self.shown(self.load(self.student, done.session_id)), done.plan)

    def test_a_question_deleted_from_the_database_is_omitted_and_the_score_stays(self):
        done = self.finish()
        target = done.plan[0]
        self.change("DELETE FROM multiple_choice_options WHERE question_id = ?", (target,))
        self.change("DELETE FROM questions WHERE id = ?", (target,))          # foreign keys off: an old build / operator
        result = self.load(self.student, done.session_id)
        self.assertEqual(self.shown(result), done.plan[1:])
        self.assertEqual((result.score_correct, result.score_total, result.skipped_question_count), (3, 5, 1))

    def test_when_nothing_can_be_shown_only_the_saved_score_survives(self):
        done = self.finish(correct=4, wrong=1)
        for qid in done.plan:
            self.break_question(qid)
        self.conn.commit()
        result = self.load(self.student, done.session_id)
        self.assertEqual(result.outcome, practice.OUTCOME_LOADED)
        self.assertEqual((result.score_correct, result.score_total), (4, 5))
        self.assertEqual(result.questions, ())
        self.assertTrue(result.only_saved_score)
        self.assertEqual(result.stale_attempt_message, practice.PRACTICE_STALE_RESULT_MESSAGE)
        self.assertEqual((result.planned_question_count, result.unavailable_question_count,
                          result.skipped_question_count), (5, 5, 5))
        self.assertEqual(result.review_context(),
                         {"questions": [], "stale_attempt_message": practice.PRACTICE_STALE_RESULT_MESSAGE})

    def test_an_unavailable_scope_yields_no_review_but_keeps_the_saved_score(self):
        cases = {
            "DZI training section": ("UPDATE curriculum_sections SET section_type = 'dzi_training' WHERE id = ?",
                                     "UPDATE curriculum_sections SET section_type = 'content' WHERE id = ?"),
        }
        for name, (apply_sql, revert_sql) in cases.items():
            with self.subTest(name):
                scope = self.make_scope(8)
                done = self.finish(scope=scope, correct=2, wrong=2)
                self.change(apply_sql, (scope.section_id,))
                result = self.load(self.student, done.session_id)
                self.assertEqual((result.outcome, result.score_correct, result.score_total),
                                 (practice.OUTCOME_LOADED, 2, 5))
                self.assertEqual(result.questions, ())
                self.assertTrue(result.only_saved_score)
                self.assertEqual((result.skipped_question_count, result.unavailable_question_count), (5, 5))
                self.change(revert_sql, (scope.section_id,))
                self.assertEqual(len(self.load(self.student, done.session_id).questions), 5)

    def test_a_topic_that_left_the_section_or_changed_subject_makes_the_scope_unavailable(self):
        scope = self.make_scope(8, topics=2)
        done = self.finish(scope=scope, topic_id=scope.topic_ids[0], plan=scope.question_ids[0][:5])
        self.change("DELETE FROM topic_section_assignments WHERE topic_id = ? AND section_id = ?",
                    (scope.topic_ids[0], scope.section_id))
        gone = self.load(self.student, done.session_id)
        self.assertEqual((gone.questions, gone.score_correct, gone.score_total), ((), 3, 5))
        self.assertTrue(gone.only_saved_score)

    def test_a_section_whose_subject_is_no_longer_known_keeps_only_the_saved_score(self):
        scope = self.make_scope(8)
        done = self.finish(scope=scope)
        self.change("UPDATE curricula SET subject = 'astrology' WHERE id = ?", (scope.curriculum_id,),
                    check_constraints=False)
        result = self.load(self.student, done.session_id)
        self.assertEqual((result.outcome, result.score_correct, result.score_total, result.questions),
                         (practice.OUTCOME_LOADED, 3, 5, ()))
        self.assertTrue(result.only_saved_score)

    def test_a_question_planned_but_ungraded_at_submission_never_reappears(self):
        scope = self.make_scope(8)
        plan = scope.all_question_ids[:6]
        session_id, _ = self.open_session(scope=scope, plan=plan)
        skipped = plan[2]
        self.change("UPDATE multiple_choice_options SET option_text = '-' WHERE question_id = ? AND option_letter = 'B'",
                    (skipped,))
        submission = self.submit(self.student, session_id, self.all_correct(plan))
        self.assertEqual((submission.score_correct, submission.score_total), (5, 5))     # the stale one was not counted
        expected_order = [qid for qid in plan if qid != skipped]
        pending = self.load(self.student, session_id)
        self.assertEqual(self.shown(pending), expected_order)
        self.assertEqual((pending.planned_question_count, pending.graded_question_count,
                          pending.ungraded_question_count, pending.unavailable_question_count,
                          pending.skipped_question_count), (6, 5, 1, 0, 1))
        # the question becomes fully eligible again: it was not graded, so it must not show up as graded
        self.change("UPDATE multiple_choice_options SET option_text = 'repaired' WHERE question_id = ? AND option_letter = 'B'",
                    (skipped,))
        with self.raw() as conn:
            self.assertEqual(len(practice._eligible_question_ids(conn, practice._resolve_scope(conn, scope.section_id, None),
                                                                 None)), 8)
            self.assertIn(skipped, practice._eligible_question_ids(
                conn, practice._resolve_scope(conn, scope.section_id, None), None))
        again = self.load(self.student, session_id)
        self.assertEqual(self.shown(again), expected_order)
        self.assertNotIn(skipped, self.shown(again))
        self.assertEqual((again.score_correct, again.score_total, again.ungraded_question_count,
                          again.skipped_question_count), (5, 5, 1, 1))

    def test_ungraded_and_unavailable_questions_add_up_to_the_skipped_count(self):
        scope = self.make_scope(8)
        plan = scope.all_question_ids[:6]
        session_id, _ = self.open_session(scope=scope, plan=plan)
        self.break_question(plan[0])
        self.conn.commit()
        self.submit(self.student, session_id, self.all_correct(plan))         # plan[0] is ungraded
        self.break_question(plan[3])                                          # plan[3] is graded but unavailable now
        self.conn.commit()
        result = self.load(self.student, session_id)
        self.assertEqual(self.shown(result), [plan[1], plan[2], plan[4], plan[5]])
        self.assertEqual((result.planned_question_count, result.graded_question_count, result.ungraded_question_count,
                          result.unavailable_question_count, result.skipped_question_count,
                          result.renderable_question_count), (6, 5, 1, 1, 2, 4))
        self.assertEqual((result.score_correct, result.score_total), (5, 5))


# ====================================================================== one coherent read snapshot
class SnapshotTest(ResultCase):
    def make_wal(self):
        conn = sqlite3.connect(self.db_path)
        try:
            self.assertEqual(conn.execute("PRAGMA journal_mode=WAL").fetchone()[0], "wal")
        finally:
            conn.close()

    def foreign_writer(self, session_id, plan):
        """What another connection commits in the middle of a load: the account, content and score."""
        def write():
            conn = sqlite3.connect(self.db_path, timeout=0)
            try:
                conn.execute("UPDATE users SET is_active = 0 WHERE id = ?", (self.student.user_id,))
                conn.execute("UPDATE questions SET prompt = 'CHANGED MID LOAD' WHERE id = ?", (plan[0],))
                conn.execute("UPDATE multiple_choice_options SET option_text = 'CHANGED OPTION' WHERE question_id = ?",
                             (plan[0],))
                conn.execute("UPDATE practice_sessions SET score_correct = 0 WHERE id = ?", (session_id,))
                conn.commit()
            finally:
                conn.close()
        return write

    def inject_after_session_reads(self, write):
        real = practice._eligible_question_ids

        def wrapper(conn, scope, topic_id):
            write()
            return real(conn, scope, topic_id)
        return patch.object(practice, "_eligible_question_ids", wrapper)

    def test_a_commit_between_the_reads_is_not_seen(self):
        self.make_wal()
        done = self.finish()
        before = self.load(self.student, done.session_id)
        loader = self.connect()
        with self.inject_after_session_reads(self.foreign_writer(done.session_id, done.plan)):
            during = self.load(self.student, done.session_id, conn=loader)
        self.assertEqual(during.outcome, practice.OUTCOME_LOADED)           # the account is still the active one it was
        self.assertEqual((during.score_correct, during.score_total), (3, 5))   # the score read before the write
        self.assertEqual([q["prompt"] for q in during.questions], [q["prompt"] for q in before.questions])
        self.assertEqual(during.questions, before.questions)                # the whole result is one point in time
        self.assert_released(loader)
        after = self.load(self.student, done.session_id, conn=loader)       # the next load sees the new world
        self.assertEqual(after.outcome, practice.OUTCOME_ACCOUNT_UNAVAILABLE)

    def test_without_the_snapshot_the_same_writer_would_be_seen_a_negative_control(self):
        self.make_wal()
        done = self.finish()
        loader = self.connect()
        passthrough = patch.object(practice, "_in_read_snapshot", lambda conn, read: read())
        with passthrough, self.inject_after_session_reads(self.foreign_writer(done.session_id, done.plan)):
            during = self.load(self.student, done.session_id, conn=loader)
        self.assertEqual(during.questions[0]["prompt"], "CHANGED MID LOAD")  # mixed old and new: what a snapshot prevents

    def test_a_caller_snapshot_is_the_one_used_and_a_later_commit_is_not_seen(self):
        self.make_wal()
        done = self.finish()
        before = self.load(self.student, done.session_id)
        loader = self.connect()
        loader.execute("BEGIN")
        loader.execute("SELECT COUNT(*) FROM users").fetchone()              # the caller's snapshot starts here
        self.foreign_writer(done.session_id, done.plan)()
        during = self.load(self.student, done.session_id, conn=loader)
        self.assertEqual(during, before)
        self.assertTrue(loader.in_transaction)
        loader.execute("ROLLBACK")
        self.assertEqual(self.load(self.student, done.session_id, conn=loader).outcome,
                         practice.OUTCOME_ACCOUNT_UNAVAILABLE)

    def test_in_a_rollback_journal_database_a_writer_cannot_commit_until_the_load_ends(self):
        done = self.finish()
        attempts = []

        def write():
            conn = sqlite3.connect(self.db_path, timeout=0)
            try:
                conn.execute("UPDATE questions SET prompt = 'CHANGED MID LOAD' WHERE id = ?", (done.plan[0],))
                try:
                    conn.commit()
                    attempts.append("committed")
                except sqlite3.OperationalError as error:
                    attempts.append(str(error))
            finally:
                conn.close()

        with self.inject_after_session_reads(write):
            result = self.load(self.student, done.session_id)
        self.assertEqual(attempts, ["database is locked"])
        self.assertNotEqual(result.questions[0]["prompt"], "CHANGED MID LOAD")
        write()                                                              # released: the same write now commits
        self.assertEqual(attempts[-1], "committed")
        self.assertEqual(self.load(self.student, done.session_id).questions[0]["prompt"], "CHANGED MID LOAD")

    def test_the_account_and_the_session_are_read_inside_the_snapshot(self):
        done = self.finish()
        statements = []
        loader = self.connect()
        loader.set_trace_callback(statements.append)
        self.load(self.student, done.session_id, conn=loader)
        loader.set_trace_callback(None)
        begin = statements.index("BEGIN")
        reads = [i for i, text in enumerate(statements) if "FROM users" in text]
        sessions = [i for i, text in enumerate(statements) if "FROM practice_sessions" in text]
        self.assertTrue(reads and sessions)
        self.assertLess(begin, min(reads))
        self.assertLess(min(reads), min(sessions))
        self.assertEqual(statements[-1], "ROLLBACK")
        content = [i for i, text in enumerate(statements) if "FROM questions" in text
                   or "FROM multiple_choice_options" in text]
        self.assertTrue(content)
        self.assertLess(max(sessions), min(content))
        self.assertEqual(sum(text == "BEGIN" for text in statements), 1)


# ====================================================================== read-only behavior
class ReadOnlyTest(ResultCase):
    WRITE_WORDS = ("INSERT", "UPDATE", "DELETE", "REPLACE", "CREATE", "DROP", "ALTER", "COMMIT", "VACUUM")

    def file_hash(self):
        return hashlib.sha256(self.db_path.read_bytes()).hexdigest()

    def test_a_query_only_connection_loads_the_result(self):
        done = self.finish()
        expected = self.load(self.student, done.session_id)
        conn = web_db.connect_db(self.db_path, query_only=True)
        self.addCleanup(conn.close)
        with self.assertRaises(sqlite3.OperationalError):                    # the control: it really refuses writes
            conn.execute("UPDATE users SET display_name = display_name")
        conn.rollback()                                                      # (Python had begun a transaction for it)
        tables, file_hash, changes = self.every_table(), self.file_hash(), conn.total_changes
        self.assertEqual(self.load(self.student, done.session_id, conn=conn), expected)
        self.assertEqual(self.load(self.teacher, done.session_id, conn=conn).outcome, practice.OUTCOME_NOT_FOUND)
        self.assertEqual((self.every_table(), self.file_hash(), conn.total_changes), (tables, file_hash, changes))
        self.assert_released(conn)

    def test_a_query_only_connection_reads_every_outcome_and_still_raises_for_bad_state(self):
        scope = self.make_scope(8)
        done = self.finish(scope=scope)
        opened, _plan = self.open_session(self.other_student, scope)
        conn = web_db.connect_db(self.db_path, query_only=True)
        self.addCleanup(conn.close)
        self.assertEqual(self.load(self.other_student, opened, conn=conn).outcome, practice.OUTCOME_NOT_FOUND)
        self.change("UPDATE users SET is_active = 0 WHERE id = ?", (self.student.user_id,))
        self.assertEqual(self.load(self.student, done.session_id, conn=conn).outcome,
                         practice.OUTCOME_ACCOUNT_UNAVAILABLE)
        self.change("UPDATE users SET is_active = 1 WHERE id = ?", (self.student.user_id,))
        self.change("UPDATE practice_sessions SET question_ids_json = 'nope' WHERE id = ?", (done.session_id,))
        with self.assertRaises(practice.PracticePlanError):
            self.load(self.student, done.session_id, conn=conn)
        self.assert_released(conn)

    def test_an_autocommit_connection_is_released_by_explicit_sql(self):
        done = self.finish()
        conn = self.autocommit_connection()
        self.assertEqual(self.load(self.student, done.session_id, conn=conn).outcome, practice.OUTCOME_LOADED)
        self.assert_released(conn)
        for who, session_id in ((self.teacher, done.session_id), (self.student, 99999)):
            self.assertEqual(self.load(who, session_id, conn=conn).outcome, practice.OUTCOME_NOT_FOUND)
            self.assert_released(conn)
        self.change("UPDATE practice_sessions SET question_ids_json = 'nope' WHERE id = ?", (done.session_id,))
        with self.assertRaises(practice.PracticePlanError):
            self.load(self.student, done.session_id, conn=conn)
        self.assert_released(conn)

    def test_autocommit_rollback_method_is_a_no_op_which_is_why_explicit_sql_is_used(self):
        conn = self.autocommit_connection()
        conn.execute("BEGIN")
        conn.rollback()
        self.assertTrue(conn.in_transaction)                                  # the control: the method would leak it
        conn.execute("ROLLBACK")
        self.assertFalse(conn.in_transaction)

    def test_a_query_only_autocommit_connection_loads_and_is_released(self):
        done = self.finish()
        expected = self.load(self.student, done.session_id)
        conn = self.autocommit_connection(query_only=True)
        tables = self.every_table()
        self.assertEqual(self.load(self.student, done.session_id, conn=conn), expected)
        self.assertEqual(self.every_table(), tables)
        self.assert_released(conn)

    def test_an_isolation_level_none_connection_is_released_too(self):
        done = self.finish()
        conn = web_db.connect_db(self.db_path)
        self.addCleanup(conn.close)
        conn.isolation_level = None
        self.assertEqual(self.load(self.student, done.session_id, conn=conn).outcome, practice.OUTCOME_LOADED)
        self.assert_released(conn)

    def test_the_load_executes_no_write_statement(self):
        done = self.finish()
        statements = []
        for conn in (self.connect(), self.autocommit_connection()):
            conn.set_trace_callback(statements.append)
            self.load(self.student, done.session_id, conn=conn)
            self.load(self.teacher, done.session_id, conn=conn)
            conn.set_trace_callback(None)
        self.assertTrue(statements)
        for text in statements:
            head = text.lstrip().split(None, 1)[0].upper()
            self.assertNotIn(head, self.WRITE_WORDS, text)
            self.assertIn(head, ("SELECT", "PRAGMA", "BEGIN", "ROLLBACK", "WITH"), text)

    def test_loads_change_no_table_runtime_or_legacy_and_no_file_byte(self):
        scope = self.make_scope(8)
        done = self.finish(scope=scope)
        opened, _plan = self.open_session(self.other_student, scope)
        self.insert_session(self.teacher, scope, scope.all_question_ids[:5], abandoned="2026-01-02 10:00:00")
        tables, file_hash = self.every_table(), self.file_hash()
        for who, session_id in ((self.student, done.session_id), (self.teacher, done.session_id),
                                (self.admin, done.session_id), (self.other_student, opened),
                                (self.student, 99999)):
            self.load(who, session_id)
        self.assertEqual(self.every_table(), tables)                          # quiz_*, assignments, users, clock...
        self.assertEqual(self.file_hash(), file_hash)
        for table in ("quiz_attempts", "quiz_answers", "quiz_assignments"):
            with self.raw() as conn:
                self.assertEqual(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0, table)


# ====================================================================== transactions and errors
class TransactionTest(ResultCase):
    def test_a_loader_owned_transaction_is_ended_on_every_outcome(self):
        done = self.finish()
        opened, _plan = self.open_session(self.other_student, self.make_scope(8))
        for who, session_id in ((self.student, done.session_id), (self.teacher, done.session_id),
                                (self.other_student, opened), (self.student, 99999)):
            self.load(who, session_id)
            self.assert_released()
        self.change("UPDATE users SET is_active = 0 WHERE id = ?", (self.student.user_id,))
        self.load(self.student, done.session_id)
        self.assert_released()

    def test_a_caller_transaction_is_reused_and_never_ended(self):
        done = self.finish()
        for begin in ("BEGIN", "BEGIN IMMEDIATE", "BEGIN EXCLUSIVE"):
            with self.subTest(begin):
                conn = self.connect()
                conn.execute(begin)
                for who, session_id in ((self.student, done.session_id), (self.teacher, done.session_id),
                                        (self.student, 99999)):
                    self.load(who, session_id, conn=conn)
                    self.assertTrue(conn.in_transaction)
                conn.execute("ROLLBACK")
                self.assert_released(conn)

    def test_a_caller_transaction_stays_open_when_the_load_raises(self):
        done = self.finish()
        self.change("UPDATE practice_sessions SET question_ids_json = 'nope' WHERE id = ?", (done.session_id,))
        conn = self.connect()
        conn.execute("BEGIN")
        with self.assertRaises(practice.PracticePlanError):
            self.load(self.student, done.session_id, conn=conn)
        self.assertTrue(conn.in_transaction)
        conn.execute("ROLLBACK")

    def test_a_caller_transaction_with_a_pending_write_is_seen_and_never_committed(self):
        done = self.finish()
        conn = self.connect()
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("UPDATE questions SET prompt = 'UNCOMMITTED' WHERE id = ?", (done.plan[0],))
        result = self.load(self.student, done.session_id, conn=conn)
        self.assertEqual(result.questions[0]["prompt"], "UNCOMMITTED")        # the caller's own snapshot is the one read
        self.assertTrue(conn.in_transaction)
        self.assertNotEqual(self.prompt_of(done.plan[0]), "UNCOMMITTED")       # and nothing was committed on its behalf
        conn.execute("ROLLBACK")
        self.assertNotEqual(self.load(self.student, done.session_id).questions[0]["prompt"], "UNCOMMITTED")

    def test_an_implicit_legacy_transaction_is_left_alone(self):
        done = self.finish()
        conn = self.connect()
        conn.execute("UPDATE users SET display_name = display_name WHERE id = ?", (self.student.user_id,))
        self.assertTrue(conn.in_transaction)                                   # Python began it for the caller's DML
        self.load(self.student, done.session_id, conn=conn)
        self.assertTrue(conn.in_transaction)
        conn.commit()
        self.assert_released(conn)

    def test_an_autocommit_connection_with_a_caller_begin_is_left_alone(self):
        done = self.finish()
        conn = self.autocommit_connection()
        conn.execute("BEGIN")
        self.load(self.student, done.session_id, conn=conn)
        self.assertTrue(conn.in_transaction)
        conn.execute("ROLLBACK")
        self.assert_released(conn)

    def test_an_unexpected_database_error_propagates_and_the_loader_transaction_is_ended(self):
        done = self.finish()
        for error in (sqlite3.OperationalError("disk I/O error"), sqlite3.DatabaseError("malformed"),
                      sqlite3.ProgrammingError("closed cursor")):
            for conn in (self.connect(), self.autocommit_connection()):
                with self.subTest(error=type(error).__name__, autocommit=conn.autocommit is True):
                    with patch.object(practice, "quiz_load_questions", side_effect=error):
                        with self.assertRaises(type(error)) as caught:
                            self.load(self.student, done.session_id, conn=conn)
                    self.assertIs(caught.exception, error)
                    self.assert_released(conn)
                    self.assertEqual(conn.execute("SELECT 1").fetchone()[0], 1)    # still the caller's, still open
                    self.assertEqual(self.load(self.student, done.session_id, conn=conn).outcome,
                                     practice.OUTCOME_LOADED)

    def test_a_base_exception_also_ends_the_loader_transaction(self):
        class Stop(BaseException):
            pass
        done = self.finish()
        conn = self.autocommit_connection()
        with patch.object(practice, "quiz_load_questions", side_effect=Stop()):
            with self.assertRaises(Stop):
                self.load(self.student, done.session_id, conn=conn)
        self.assert_released(conn)

    def test_an_error_inside_a_caller_transaction_leaves_it_to_the_caller(self):
        done = self.finish()
        conn = self.connect()
        conn.execute("BEGIN")
        with patch.object(practice, "quiz_load_questions", side_effect=sqlite3.OperationalError("boom")):
            with self.assertRaises(sqlite3.OperationalError):
                self.load(self.student, done.session_id, conn=conn)
        self.assertTrue(conn.in_transaction)
        conn.execute("ROLLBACK")

    def test_the_connection_is_never_closed_or_reconfigured(self):
        done = self.finish()
        conn = self.connect()
        before = (conn.row_factory, conn.isolation_level, conn.execute("PRAGMA foreign_keys").fetchone()[0])
        self.load(self.student, done.session_id, conn=conn)
        self.load(self.teacher, done.session_id, conn=conn)
        self.assertEqual((conn.row_factory, conn.isolation_level, conn.execute("PRAGMA foreign_keys").fetchone()[0]),
                         before)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM users").fetchone()[0], 4)

    def test_a_lock_held_by_another_writer_does_not_stop_a_read(self):
        done = self.finish()
        writer = sqlite3.connect(self.db_path, timeout=0)
        self.addCleanup(writer.close)
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("UPDATE users SET display_name = display_name WHERE id = ?", (self.student.user_id,))
        try:
            self.assertEqual(self.load(self.student, done.session_id).outcome, practice.OUTCOME_LOADED)
        finally:
            writer.execute("ROLLBACK")


class ModuleSurfaceTest(unittest.TestCase):
    def test_the_start_and_submit_surfaces_are_unchanged(self):
        for name in ("start_or_resume_practice", "submit_practice", "PracticeStart", "PracticeSubmission",
                     "PracticeAnswer", "PracticeAccessError", "PracticeStateError", "PracticePlanError",
                     "MIN_QUESTIONS", "MAX_QUESTIONS", "OUTCOME_CREATED", "OUTCOME_SUBMITTED", "OUTCOME_NOT_FOUND"):
            self.assertTrue(hasattr(practice, name), name)
        self.assertEqual((practice.MIN_QUESTIONS, practice.MAX_QUESTIONS), (5, 10))
        self.assertEqual(practice.OUTCOME_NOT_FOUND, "not_found")
        self.assertEqual(practice.PracticeSubmission._fields,
                         ("outcome", "session_id", "score_correct", "score_total", "submitted_at", "answers"))

    def test_the_loader_defines_no_route_and_no_write_statement(self):
        import inspect
        source = "\n".join(inspect.getsource(getattr(practice, name)) for name in (
            "load_practice_result", "_load_result", "_stored_answers", "_review_rows", "_in_read_snapshot"))
        for word in ("INSERT", "UPDATE ", "DELETE", "COMMIT", "conn.commit", "conn.close"):
            self.assertNotIn(word, source, word)
        self.assertEqual(source.count('"BEGIN"'), 1)                          # deferred, never IMMEDIATE
        self.assertNotIn("BEGIN IMMEDIATE", source)


if __name__ == "__main__":
    unittest.main()
