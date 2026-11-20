"""The practice submit service (C2, roadmap PR #368): ``web.services.practice.submit_practice``.

Every database is a temporary clone of the migrations + canonical template with synthetic
curriculum rows on top (the ``PracticeCase`` fixtures of the start-service tests), so each plan and
pool is exactly known. Nothing here reads, copies or hashes ``data/questions.db``.

What is proved, and how:

* the outcome contract, validation, account rules, ownership, and that a missing and a foreign
  session are one indistinguishable outcome, for a teacher and an admin too;
* idempotence: a submitted session is returned without a write and without looking at content,
  an abandoned session is refused, and an old tab never touches its replacement;
* grading: exact per-question verdicts and stable scores, partial and empty submissions, ids
  outside the plan, option keys that are not A-D, display letters that play no part, options
  or correct answers changed since the start, and a denominator that counts presentable
  questions only (every start-service exclusion, as a pair with a control);
* fail-closed stored state: malformed plans, stale plans, an unavailable scope, inconsistent rows;
* transactions: the lock is held while deciding, every write failure restores the previous rows
  (a real trigger fails the write; the same call succeeds without it), a lock held by another
  connection is not stolen, and the caller's own transaction is never ended;
* configured ``autocommit=True`` connections;
* concurrency over separate real connections, synchronized with barriers and events rather than
  sleeps: duplicate submits, a submit racing a replacing start, with a deferred-transaction
  negative control that does fail.
"""

import sqlite3
import threading
import unittest
from datetime import datetime
from unittest.mock import patch

from tests import runtime_fixtures
from tests.subject_fixtures import add_question, four_options
from tests.support import set_lifecycle_clock_row
from tests.test_practice_start_service import STAMP, TIMESTAMP_FORMAT, DeferredConnection, PracticeCase
from web import db as web_db
from web.access import ANONYMOUS, Principal, account_principal
from web.services import assignment_quota, lifecycle, practice
from web.services.quiz import quiz_load_questions

VISUAL_PROMPT = "Разгледайте графиката и отговорете?"


class SubmitCase(PracticeCase):
    """Helpers over the start-service fixtures."""

    def open_session(self, who=None, scope=None, count=5, *, topic_id=None, plan=None, seed="stored-seed"):
        """An open stored session of ``who`` over the first ``count`` questions; returns (id, plan)."""
        who = who or self.student
        scope = scope or self.make_scope(8)
        plan = list(plan) if plan is not None else list(scope.all_question_ids[:count])
        return self.insert_session(who, scope, plan, topic_id=topic_id, seed=seed), plan

    def submit(self, who, session_id, choices, conn=None):
        return practice.submit_practice(conn or self.conn, who, session_id, choices)

    def key(self, question_id, *, correct=True):
        """A stored option key of the question: its correct one, or a wrong one ("A" if it has no options)."""
        conn = sqlite3.connect(self.db_path)
        try:
            row = conn.execute(
                "SELECT option_letter FROM multiple_choice_options WHERE question_id = ? AND is_correct = ? "
                "ORDER BY option_letter LIMIT 1", (question_id, 1 if correct else 0)).fetchone()
        finally:
            conn.close()
        return "A" if row is None else row[0]

    def all_correct(self, plan):
        return {question_id: self.key(question_id) for question_id in plan}

    def answers(self, session_id=None):
        with self.raw() as conn:
            sql = "SELECT * FROM practice_answers" + ("" if session_id is None else " WHERE session_id = ?")
            return [dict(row) for row in conn.execute(sql + " ORDER BY session_id, question_id",
                                                       () if session_id is None else (session_id,))]

    def session(self, session_id):
        return [row for row in self.sessions() if row["id"] == session_id][0]

    def assert_closed_and_free(self, conn=None):
        self.assertFalse((conn or self.conn).in_transaction)
        probe = sqlite3.connect(self.db_path, timeout=0)
        self.addCleanup(probe.close)
        probe.execute("BEGIN IMMEDIATE")
        probe.rollback()

    def assert_nothing_written(self, action):
        """``action`` leaves every practice row as it was and no transaction open; returns its result."""
        before = self.practice_digest()
        try:
            return action()
        finally:
            self.assertEqual(before, self.practice_digest())
            self.assertFalse(self.conn.in_transaction)

    def keyed_question(self, topic_id, keys, correct, subject="it"):
        options = {key: f"Option {key} {self.next()}" for key in keys}
        return add_question(self.conn, topic_id, subject, self.next(), source="practice-synthetic",
                            options=options, correct=correct)


# ====================================================================== outcome contract
class SubmitContractTest(SubmitCase):
    def test_a_submission_stores_answers_verdicts_score_and_time_and_nothing_else_of_the_session(self):
        scope = self.make_scope(8)
        session_id, plan = self.open_session(scope=scope, seed="the-seed")
        before = self.session(session_id)
        choices = {
            plan[0]: self.key(plan[0]),                    # correct
            plan[1]: self.key(plan[1], correct=False),     # wrong
            plan[2]: self.key(plan[2]),                    # correct
            plan[3]: self.key(plan[3], correct=False),     # wrong
        }                                                  # plan[4]: unanswered
        with patch.object(lifecycle, "naive_utc_now", return_value=datetime(2026, 11, 20, 12, 0, 0, 999999)):
            result = self.submit(self.student, session_id, choices)
        self.assertEqual(result.outcome, practice.OUTCOME_SUBMITTED)
        self.assertTrue(result.submitted)
        self.assertFalse(result.already_submitted)
        self.assertEqual((result.session_id, result.score_correct, result.score_total, result.submitted_at),
                         (session_id, 2, 5, "2026-11-20 12:00:00"))
        self.assertEqual(result.answers, (
            practice.PracticeAnswer(plan[0], choices[plan[0]], True),
            practice.PracticeAnswer(plan[1], choices[plan[1]], False),
            practice.PracticeAnswer(plan[2], choices[plan[2]], True),
            practice.PracticeAnswer(plan[3], choices[plan[3]], False),
            practice.PracticeAnswer(plan[4], None, False)))
        self.assertEqual(self.answers(), [
            {"session_id": session_id, "question_id": qid, "chosen_letter": letter, "is_correct": verdict}
            for qid, letter, verdict in sorted(
                (a.question_id, a.chosen_letter, int(a.is_correct)) for a in result.answers)])
        after = self.session(session_id)
        # Only the three finalization columns changed: id, owner, scope, plan, seed and start are kept.
        for column in ("id", "user_id", "section_id", "topic_id", "seed", "question_ids_json", "started_at",
                       "abandoned_at"):
            self.assertEqual(after[column], before[column], column)
        self.assertEqual((after["submitted_at"], after["score_correct"], after["score_total"]),
                         ("2026-11-20 12:00:00", 2, 5))
        self.assertFalse(self.conn.in_transaction)

    def test_the_time_is_a_utc_text_in_the_stored_format(self):
        session_id, plan = self.open_session()
        self.submit(self.student, session_id, {plan[0]: self.key(plan[0])})
        stored = self.session(session_id)["submitted_at"]
        self.assertRegex(stored, r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d$")
        datetime.strptime(stored, TIMESTAMP_FORMAT)

    def test_a_single_usable_choice_is_enough_and_the_rest_count_wrong(self):
        session_id, plan = self.open_session()
        result = self.submit(self.student, session_id, {plan[3]: self.key(plan[3], correct=False)})
        self.assertEqual((result.outcome, result.score_correct, result.score_total), (practice.OUTCOME_SUBMITTED, 0, 5))
        rows = self.answers(session_id)
        self.assertEqual(len(rows), 5)
        self.assertEqual([row["chosen_letter"] for row in rows if row["question_id"] != plan[3]], [None] * 4)
        self.assertEqual({row["is_correct"] for row in rows}, {0})

    def test_a_perfect_and_an_all_wrong_submission_have_stable_scores(self):
        for name, correct, expected in (("perfect", True, 5), ("all wrong", False, 0)):
            with self.subTest(name):
                session_id, plan = self.open_session(scope=self.make_scope(8))
                result = self.submit(self.student, session_id,
                                     {qid: self.key(qid, correct=correct) for qid in plan})
                self.assertEqual((result.score_correct, result.score_total), (expected, 5))
                row = self.session(session_id)
                self.assertEqual((row["score_correct"], row["score_total"]), (expected, 5))
                self.assertEqual(sum(r["is_correct"] for r in self.answers(session_id)), expected)

    def test_a_session_of_fewer_than_five_presentable_questions_can_be_submitted(self):
        session_id, plan = self.open_session(count=1)
        result = self.submit(self.student, session_id, {plan[0]: self.key(plan[0])})
        self.assertEqual((result.outcome, result.score_correct, result.score_total), (practice.OUTCOME_SUBMITTED, 1, 1))

    def test_the_session_made_by_the_start_service_round_trips(self):
        scope = self.make_scope(12)
        started = self.start(self.student, scope)
        self.assertEqual(len(started.question_ids), 10)
        result = self.submit(self.student, started.session_id, self.all_correct(started.question_ids))
        self.assertEqual((result.outcome, result.score_correct, result.score_total), (practice.OUTCOME_SUBMITTED, 10, 10))
        self.assertEqual([a.question_id for a in result.answers], list(started.question_ids))
        row = self.session(started.session_id)
        self.assertEqual((row["seed"], row["started_at"]), (started.seed, row["started_at"]))
        self.assertEqual(self.start(self.student, scope).outcome, practice.OUTCOME_CREATED)   # a repeat is a new session

    def test_the_result_properties_and_defaults(self):
        self.assertFalse(practice.PracticeSubmission(practice.OUTCOME_NOT_FOUND).submitted)
        self.assertEqual(practice.PracticeSubmission(practice.OUTCOME_EMPTY).answers, ())


class ValidationTest(SubmitCase):
    def test_session_ids_must_be_positive_integers(self):
        session_id, plan = self.open_session()
        for bad in (0, -1, True, False, "1", 1.0, None, 2 ** 63, b"1", [1]):
            with self.subTest(session_id=bad):
                with self.assertRaises(ValueError):
                    self.assert_nothing_written(lambda: self.submit(self.student, bad, self.all_correct(plan)))

    def test_choices_must_be_a_mapping(self):
        session_id, plan = self.open_session()
        for bad in (None, [], [(plan[0], "A")], "A", 5, {1, 2}):
            with self.subTest(choices=bad):
                with self.assertRaises(ValueError):
                    self.assert_nothing_written(lambda: self.submit(self.student, session_id, bad))

    def test_only_an_account_principal_can_submit(self):
        session_id, plan = self.open_session()
        principals = {
            "anonymous": ANONYMOUS,
            "teacher capability without an id": Principal(is_teacher=True, is_admin=False),
            "unknown role": Principal(is_teacher=False, is_admin=False, user_id=self.student.user_id, role="parent"),
            "no role": Principal(is_teacher=False, is_admin=False, user_id=self.student.user_id),
            "boolean id": Principal(is_teacher=False, is_admin=False, user_id=True, role="student"),
            "zero id": Principal(is_teacher=False, is_admin=False, user_id=0, role="student"),
            "None": None,
            "a form value": {"user_id": self.student.user_id},
        }
        for name, principal in principals.items():
            with self.subTest(name):
                with self.assertRaises(practice.PracticeAccessError):
                    self.assert_nothing_written(lambda: self.submit(principal, session_id, self.all_correct(plan)))
        self.assertEqual(self.answers(), [])

    def test_an_unconfigured_connection_is_refused_without_a_write(self):
        session_id, plan = self.open_session()
        plain = sqlite3.connect(self.db_path)
        self.addCleanup(plain.close)
        with self.assertRaises(RuntimeError):
            self.assert_nothing_written(lambda: self.submit(self.student, session_id, self.all_correct(plan), conn=plain))
        no_foreign_keys = web_db.connect_db(self.db_path)
        self.addCleanup(no_foreign_keys.close)
        no_foreign_keys.execute("PRAGMA foreign_keys=OFF")
        with self.assertRaises(RuntimeError):
            self.assert_nothing_written(
                lambda: self.submit(self.student, session_id, self.all_correct(plan), conn=no_foreign_keys))

    def test_a_caller_transaction_is_never_ended_by_the_service(self):
        session_id, plan = self.open_session()
        self.conn.execute("INSERT INTO curriculum_topics (topic_slug, title_bg) VALUES ('caller-pending', 'x')")
        self.assertTrue(self.conn.in_transaction)
        with self.assertRaises(RuntimeError):
            self.submit(self.student, session_id, self.all_correct(plan))
        self.assertTrue(self.conn.in_transaction)       # still the caller's, not rolled back or committed
        self.conn.commit()
        self.assertEqual(self.raw().execute(
            "SELECT COUNT(*) FROM curriculum_topics WHERE topic_slug = 'caller-pending'").fetchone()[0], 1)
        self.assertEqual(self.answers(), [])
        self.assertIsNone(self.session(session_id)["submitted_at"])


# ====================================================================== accounts and ownership
class AccountAndOwnershipTest(SubmitCase):
    def test_inactive_and_missing_accounts_cannot_submit_and_nothing_is_written(self):
        session_id, plan = self.open_session()
        inactive = self.add_account("student", active=False)
        self.conn.commit()
        for name, who in (("inactive", inactive), ("missing", account_principal(999999, "student", "x"))):
            with self.subTest(name):
                result = self.assert_nothing_written(lambda: self.submit(who, session_id, self.all_correct(plan)))
                self.assertEqual(result, practice.PracticeSubmission(practice.OUTCOME_ACCOUNT_UNAVAILABLE))

    def test_an_account_deactivated_after_the_principal_was_loaded_is_rechecked_under_the_lock(self):
        session_id, plan = self.open_session()
        self.conn.execute("UPDATE users SET is_active = 0 WHERE id = ?", (self.student.user_id,))
        self.conn.commit()
        result = self.assert_nothing_written(lambda: self.submit(self.student, session_id, self.all_correct(plan)))
        self.assertEqual(result.outcome, practice.OUTCOME_ACCOUNT_UNAVAILABLE)
        self.assertIsNone(self.session(session_id)["submitted_at"])
        self.conn.execute("UPDATE users SET is_active = 1 WHERE id = ?", (self.student.user_id,))   # control
        self.conn.commit()
        self.assertTrue(self.submit(self.student, session_id, self.all_correct(plan)).submitted)

    def test_a_foreign_and_a_missing_session_are_one_indistinguishable_outcome_for_every_role(self):
        scope = self.make_scope(8)
        session_id, plan = self.open_session(self.student, scope)
        choices = self.all_correct(plan)
        missing = 987654
        for role, other in (("student", self.other_student), ("teacher", self.teacher), ("admin", self.admin)):
            with self.subTest(role):
                foreign = self.assert_nothing_written(lambda: self.submit(other, session_id, choices))
                absent = self.assert_nothing_written(lambda: self.submit(other, missing, choices))
                self.assertEqual(foreign, practice.PracticeSubmission(practice.OUTCOME_NOT_FOUND))
                self.assertEqual(foreign, absent)
        self.assertIsNone(self.session(session_id)["submitted_at"])
        self.assertEqual(self.answers(), [])

    def test_the_owner_condition_is_part_of_the_lookup_for_submitted_and_abandoned_rows_too(self):
        scope = self.make_scope(8)
        submitted = self.insert_session(self.student, scope, scope.all_question_ids[:3], submitted=STAMP)
        abandoned = self.insert_session(self.student, scope, scope.all_question_ids[3:6], abandoned=STAMP)
        for session_id in (submitted, abandoned):
            result = self.assert_nothing_written(lambda: self.submit(self.other_student, session_id, {}))
            self.assertEqual(result.outcome, practice.OUTCOME_NOT_FOUND)         # not "already submitted"/"abandoned"

    def test_every_role_submits_only_its_own_session_independently(self):
        scope = self.make_scope(12)
        owners = {role: (who, self.open_session(who, scope, count=5, plan=plan)) for role, who, plan in (
            ("student", self.student, scope.all_question_ids[0:5]),
            ("teacher", self.teacher, scope.all_question_ids[2:7]),
            ("admin", self.admin, scope.all_question_ids[4:9]))}
        for role, (who, (session_id, plan)) in owners.items():
            with self.subTest(role):
                result = self.submit(who, session_id, self.all_correct(plan))
                self.assertEqual((result.outcome, result.score_correct, result.score_total),
                                 (practice.OUTCOME_SUBMITTED, 5, 5))
        self.assertEqual({row["user_id"] for row in self.sessions() if row["submitted_at"]},
                         {self.student.user_id, self.teacher.user_id, self.admin.user_id})
        self.assertEqual(len(self.answers()), 15)

    def test_another_accounts_sessions_and_scopes_are_not_touched(self):
        scope = self.make_scope(12)
        mine, my_plan = self.open_session(self.student, scope, plan=scope.all_question_ids[:5])
        theirs, _ = self.open_session(self.other_student, scope, plan=scope.all_question_ids[:5])
        other_scope_session, _ = self.open_session(self.student, self.make_scope(8))
        before = {row["id"]: row for row in self.sessions()}
        self.submit(self.student, mine, self.all_correct(my_plan))
        for session_id in (theirs, other_scope_session):
            self.assertEqual(self.session(session_id), before[session_id])
        self.assertEqual({row["session_id"] for row in self.answers()}, {mine})


# ====================================================================== idempotence and abandonment
class IdempotenceTest(SubmitCase):
    def test_a_retry_is_write_free_and_returns_the_stored_result(self):
        session_id, plan = self.open_session()
        first = self.submit(self.student, session_id, {plan[0]: self.key(plan[0]), plan[1]: self.key(plan[1], correct=False)})
        digest = self.practice_digest()
        for choices in ({}, self.all_correct(plan), {plan[0]: self.key(plan[0], correct=False)}, {99: "A"}):
            with self.subTest(choices=choices):
                again = self.submit(self.student, session_id, choices)
                self.assertEqual(again, practice.PracticeSubmission(
                    practice.OUTCOME_ALREADY_SUBMITTED, session_id, 1, 5, first.submitted_at))
                self.assertTrue(again.already_submitted)
                self.assertEqual(digest, self.practice_digest())
                self.assertFalse(self.conn.in_transaction)

    def test_a_retry_does_not_look_at_changed_content_or_an_unusable_scope(self):
        scope = self.make_scope(8)
        session_id, plan = self.open_session(scope=scope)
        first = self.submit(self.student, session_id, self.all_correct(plan))
        digest = self.practice_digest()
        for qid in plan:
            self.break_question(qid)
        self.conn.execute("UPDATE curriculum_sections SET section_type = 'dzi_training' WHERE id = ?", (scope.section_id,))
        self.conn.commit()
        again = self.submit(self.student, session_id, self.all_correct(plan))
        self.assertEqual(again.outcome, practice.OUTCOME_ALREADY_SUBMITTED)
        self.assertEqual((again.score_correct, again.score_total, again.submitted_at),
                         (5, 5, first.submitted_at))
        self.assertEqual(digest, self.practice_digest())

    def test_a_submitted_row_is_not_parsed_even_with_a_plan_that_would_be_malformed(self):
        scope = self.make_scope(6)
        session_id = self.insert_session(self.student, scope, "not json", submitted=STAMP)
        result = self.assert_nothing_written(lambda: self.submit(self.student, session_id, {1: "A"}))
        self.assertEqual((result.outcome, result.score_correct, result.score_total, result.submitted_at),
                         (practice.OUTCOME_ALREADY_SUBMITTED, 1, 2, STAMP))

    def test_the_second_submission_cannot_alter_answers_time_or_score(self):
        session_id, plan = self.open_session()
        self.submit(self.student, session_id, {plan[0]: self.key(plan[0])})
        answers, row = self.answers(session_id), self.session(session_id)
        with patch.object(lifecycle, "naive_utc_now", return_value=datetime(2030, 1, 1)):
            self.submit(self.student, session_id, self.all_correct(plan))
        self.assertEqual(answers, self.answers(session_id))
        self.assertEqual(row, self.session(session_id))

    def test_a_submitted_session_blocks_nothing_and_a_new_one_follows_by_start(self):
        scope = self.make_scope(12)
        started = self.start(self.student, scope)
        self.submit(self.student, started.session_id, self.all_correct(started.question_ids))
        again = self.start(self.student, scope)
        self.assertEqual(again.outcome, practice.OUTCOME_CREATED)
        self.assertNotEqual(again.session_id, started.session_id)


class AbandonedSessionTest(SubmitCase):
    def test_an_abandoned_session_is_refused_without_a_write(self):
        scope = self.make_scope(8)
        session_id = self.insert_session(self.student, scope, scope.all_question_ids[:5], abandoned=STAMP)
        for choices in ({}, self.all_correct(scope.all_question_ids[:5])):
            with self.subTest(choices=bool(choices)):
                result = self.assert_nothing_written(lambda: self.submit(self.student, session_id, choices))
                self.assertEqual(result, practice.PracticeSubmission(practice.OUTCOME_ABANDONED))
        self.assertIsNone(self.session(session_id)["submitted_at"])
        self.assertEqual(self.answers(), [])

    def test_an_old_tab_cannot_grade_or_touch_the_replacement(self):
        scope = self.make_scope(12)
        old_plan = scope.all_question_ids[:5]
        old = self.insert_session(self.student, scope, old_plan, seed="old-seed")
        for qid in old_plan:                                      # the old plan goes stale...
            self.break_question(qid)
        self.conn.commit()
        replacement = self.start(self.student, scope)             # ...so start abandons and replaces it
        self.assertEqual((replacement.outcome, replacement.abandoned_session_id),
                         (practice.OUTCOME_CREATED, old))
        before = self.practice_digest()
        # The old tab posts the old session id with every id it knows, the replacement's included.
        stale_tab = {**{qid: "A" for qid in old_plan}, **self.all_correct(replacement.question_ids)}
        result = self.submit(self.student, old, stale_tab)
        self.assertEqual(result.outcome, practice.OUTCOME_ABANDONED)
        self.assertEqual(before, self.practice_digest())
        self.assertEqual(self.answers(), [])
        self.assertIsNone(self.session(replacement.session_id)["submitted_at"])
        # The replacement is still open and gradable by its own id alone.
        graded = self.submit(self.student, replacement.session_id, self.all_correct(replacement.question_ids))
        self.assertEqual((graded.outcome, graded.score_correct), (practice.OUTCOME_SUBMITTED, len(replacement.question_ids)))
        self.assertEqual({row["session_id"] for row in self.answers()}, {replacement.session_id})
        self.assertEqual(self.session(old)["submitted_at"], None)

    def test_a_replacement_session_id_cannot_be_graded_through_another_accounts_tab(self):
        scope = self.make_scope(12)
        started = self.start(self.student, scope)
        result = self.assert_nothing_written(
            lambda: self.submit(self.other_student, started.session_id, self.all_correct(started.question_ids)))
        self.assertEqual(result.outcome, practice.OUTCOME_NOT_FOUND)


# ====================================================================== scope, staleness and plans
class StaleAndScopeTest(SubmitCase):
    def assert_stale(self, session_id, plan, outcome=practice.OUTCOME_STALE):
        result = self.assert_nothing_written(lambda: self.submit(self.student, session_id, self.all_correct(plan)))
        self.assertEqual(result, practice.PracticeSubmission(outcome))
        row = self.session(session_id)
        self.assertEqual((row["submitted_at"], row["abandoned_at"]), (None, None))     # still open, not abandoned
        self.assertEqual(self.answers(), [])

    def test_a_plan_of_missing_questions_is_stale_and_writes_nothing(self):
        session_id, _ = self.open_session(plan=[8_000_001, 8_000_002])
        self.assert_stale(session_id, [8_000_001, 8_000_002])

    def test_an_empty_array_plan_is_well_formed_and_stale(self):
        session_id, _ = self.open_session(plan=[])
        self.assert_stale(session_id, [])

    def test_a_plan_none_of_which_is_renderable_or_in_scope_is_stale(self):
        scope = self.make_scope(8)
        session_id, plan = self.open_session(scope=scope)
        for qid in plan[:3]:
            self.break_question(qid)                         # no longer renderable
        elsewhere = self.make_scope(2)
        self.conn.execute("UPDATE questions SET topic_id = ? WHERE id = ?", (elsewhere.topic_ids[0], plan[3]))
        self.make_dzi_legacy(plan[4])                        # renderable, but now official DZI
        self.conn.commit()
        self.assert_stale(session_id, plan)

    def test_the_open_stale_session_is_left_for_start_to_replace(self):
        scope = self.make_scope(12)
        session_id, plan = self.open_session(scope=scope)
        for qid in plan:
            self.break_question(qid)
        self.conn.commit()
        self.assert_stale(session_id, plan)
        replaced = self.start(self.student, scope)
        self.assertEqual((replaced.outcome, replaced.abandoned_session_id), (practice.OUTCOME_CREATED, session_id))

    def test_dzi_training_sections_and_missing_subjects_are_unavailable_and_write_nothing(self):
        scope = self.make_scope(8)
        session_id, plan = self.open_session(scope=scope)
        self.conn.execute("UPDATE curriculum_sections SET section_type = 'dzi_training' WHERE id = ?", (scope.section_id,))
        self.conn.commit()
        self.assert_stale(session_id, plan, practice.OUTCOME_SCOPE_UNAVAILABLE)
        self.conn.execute("UPDATE curriculum_sections SET section_type = 'content' WHERE id = ?", (scope.section_id,))
        self.conn.commit()
        self.assertTrue(self.submit(self.student, session_id, self.all_correct(plan)).submitted)   # control

    def test_a_topic_session_whose_topic_left_the_section_is_unavailable(self):
        scope = self.make_scope(8, topics=2)
        plan = scope.question_ids[0][:5]
        session_id, _ = self.open_session(scope=scope, plan=plan, topic_id=scope.topic_ids[0])
        self.conn.execute("DELETE FROM topic_section_assignments WHERE topic_id = ? AND section_id = ?",
                          (scope.topic_ids[0], scope.section_id))
        self.conn.commit()
        self.assert_stale(session_id, plan, practice.OUTCOME_SCOPE_UNAVAILABLE)

    def test_a_topic_session_grades_only_questions_still_in_its_topic(self):
        scope = self.make_scope(8, topics=2)
        plan = scope.question_ids[0][:5]
        session_id, _ = self.open_session(scope=scope, plan=plan, topic_id=scope.topic_ids[0])
        self.conn.execute("UPDATE questions SET topic_id = ? WHERE id = ?", (scope.topic_ids[1], plan[0]))
        self.conn.commit()   # still in the section, still renderable, but no longer in the topic
        result = self.submit(self.student, session_id, self.all_correct(plan))
        self.assertEqual((result.score_correct, result.score_total), (4, 4))
        self.assertEqual([row["question_id"] for row in self.answers(session_id)], sorted(plan[1:]))

    def test_a_section_session_ignores_the_topic_split(self):
        scope = self.make_scope(8, topics=2)
        plan = scope.question_ids[0][:3] + scope.question_ids[1][:2]
        session_id, _ = self.open_session(scope=scope, plan=plan)
        result = self.submit(self.student, session_id, self.all_correct(plan))
        self.assertEqual((result.score_correct, result.score_total), (5, 5))

    def test_a_section_and_a_topic_scope_are_graded_independently(self):
        scope = self.make_scope(8, topics=2)
        section_id, section_plan = self.open_session(scope=scope, plan=scope.question_ids[0][:5])
        topic_id, topic_plan = self.open_session(scope=scope, plan=scope.question_ids[0][:5],
                                                 topic_id=scope.topic_ids[0])
        self.submit(self.student, topic_id, self.all_correct(topic_plan))
        self.assertEqual(self.answers(section_id), [])
        self.assertIsNone(self.session(section_id)["submitted_at"])
        self.assertEqual(len(self.answers(topic_id)), 5)


class MalformedPlanTest(SubmitCase):
    MALFORMED = {
        "not JSON": "not json", "empty text": "", "an object": '{"question_ids": [1, 2, 3]}', "a string": '"abc"',
        "null": "null", "a number": "7", "a string element": '[1, "2"]', "a boolean element": "[true, 2]",
        "a float element": "[1.0, 2]", "a null element": "[null]", "a nested array": "[[1]]",
        "a zero id": "[0, 3]", "a negative id": "[-1]", "a duplicate": "[7, 7]",
        "too many ids": "[" + ",".join(str(i) for i in range(1, 12)) + "]",
        "an id above SQLite's range": "[9223372036854775808]", "NaN": "[NaN]",
        "deeply nested JSON": "[" * 200_000,
    }

    def test_every_malformed_plan_fails_closed_without_a_write(self):
        scope = self.make_scope(12)
        for name, raw in self.MALFORMED.items():
            with self.subTest(name):
                with self.conn:
                    self.conn.execute("DELETE FROM practice_sessions")
                session_id = self.insert_session(self.student, scope, raw, seed="keep-me")
                with self.assertRaises(practice.PracticePlanError) as raised:
                    self.assert_nothing_written(
                        lambda: self.submit(self.student, session_id, {qid: "A" for qid in scope.all_question_ids}))
                self.assertEqual(raised.exception.session_id, session_id)
                row = self.session(session_id)
                self.assertEqual((row["question_ids_json"], row["seed"], row["abandoned_at"], row["submitted_at"]),
                                 (raw, "keep-me", None, None))
                self.assertEqual(self.answers(), [])
                self.assert_closed_and_free()

    def test_a_malformed_plan_is_never_read_as_stale_empty_or_abandoned(self):
        scope = self.make_scope(12)
        session_id = self.insert_session(self.student, scope, "[8000001,")
        with self.assertRaises(practice.PracticePlanError):
            self.submit(self.student, session_id, {8000001: "A"})
        self.assertEqual(self.start_outcome_after(scope), "error")        # start refuses it too
        # The merely stale and the malformed are different outcomes.
        stale_id = self.insert_session(self.other_student, scope, "[8000001]")
        self.assertEqual(self.submit(self.other_student, stale_id, {8000001: "A"}).outcome, practice.OUTCOME_STALE)

    def start_outcome_after(self, scope):
        try:
            self.start(self.student, scope)
        except practice.PracticePlanError:
            return "error"
        return "ok"

    def test_an_unavailable_scope_is_decided_before_the_plan_is_read(self):
        scope = self.make_scope(8, section_type="dzi_training")
        session_id = self.insert_session(self.student, scope, "garbage")
        result = self.assert_nothing_written(lambda: self.submit(self.student, session_id, {1: "A"}))
        self.assertEqual(result.outcome, practice.OUTCOME_SCOPE_UNAVAILABLE)     # as in start


class InconsistentStateTest(SubmitCase):
    def test_an_open_session_that_already_has_answers_is_refused_and_unchanged(self):
        session_id, plan = self.open_session()
        self.conn.execute("INSERT INTO practice_answers (session_id, question_id, chosen_letter, is_correct) "
                          "VALUES (?, ?, 'A', 1)", (session_id, plan[0]))
        self.conn.commit()
        for choices in ({}, self.all_correct(plan)):
            with self.assertRaises(practice.PracticeStateError):
                self.assert_nothing_written(lambda: self.submit(self.student, session_id, choices))
        self.assertEqual(len(self.answers()), 1)
        self.assertIsNone(self.session(session_id)["submitted_at"])
        # Another question's answer row blocks it just the same (the primary key alone would not).
        self.conn.execute("DELETE FROM practice_answers")
        self.conn.execute("INSERT INTO practice_answers (session_id, question_id, chosen_letter, is_correct) "
                          "VALUES (?, ?, NULL, 0)", (session_id, plan[4]))
        self.conn.commit()
        with self.assertRaises(practice.PracticeStateError):
            self.submit(self.student, session_id, self.all_correct(plan))

    def test_a_presentable_question_without_a_correct_option_fails_closed(self):
        session_id, plan = self.open_session()
        with patch.object(practice, "quiz_correct_mc_letters_by_id", return_value={}):
            with self.assertRaises(practice.PracticeStateError):
                self.assert_nothing_written(lambda: self.submit(self.student, session_id, self.all_correct(plan)))
        self.assertTrue(self.submit(self.student, session_id, self.all_correct(plan)).submitted)      # control


# ====================================================================== grading
class GradingTest(SubmitCase):
    def test_only_the_plan_is_graded_and_ids_outside_it_are_ignored(self):
        scope = self.make_scope(10)
        plan = scope.all_question_ids[:5]
        outside_in_scope = scope.all_question_ids[5:8]
        session_id, _ = self.open_session(scope=scope, plan=plan)
        choices = {
            **self.all_correct(plan[:2]),
            **self.all_correct(outside_in_scope),        # in scope, usable, but not in the plan
            8_000_001: "A",                              # a question that does not exist
            0: "A", -3: "A", True: "A", "7": "A", None: "A", 2.5: "A", (1, 2): "A",
        }
        result = self.submit(self.student, session_id, choices)
        self.assertEqual((result.score_correct, result.score_total), (2, 5))
        self.assertEqual([row["question_id"] for row in self.answers()], sorted(plan))
        self.assertEqual(self.answers(session_id), self.answers())

    def test_choices_only_for_ids_outside_the_plan_are_an_empty_submission(self):
        scope = self.make_scope(10)
        session_id, plan = self.open_session(scope=scope)
        outside = scope.all_question_ids[5:8]
        result = self.assert_nothing_written(
            lambda: self.submit(self.student, session_id, {**self.all_correct(outside), 8_000_001: "A", "1": "A"}))
        self.assertEqual(result, practice.PracticeSubmission(practice.OUTCOME_EMPTY))
        self.assertIsNone(self.session(session_id)["submitted_at"])

    def test_a_boolean_key_never_reaches_grading_as_the_question_with_that_number(self):
        # Canonical question 1 is an official DZI row, so no in-scope question can be id 1: assert the
        # normalization at the grading boundary instead. True == 1 in Python and must not name a question.
        session_id, plan = self.open_session()
        seen, real = [], practice._grade

        def spy(conn, presentable, choices):
            seen.append(dict(choices))
            return real(conn, presentable, choices)

        with patch.object(practice, "_grade", side_effect=spy):
            self.submit(self.student, session_id,
                        {True: "A", False: "A", "7": "A", None: "A", 2.5: "A", plan[0]: self.key(plan[0])})
        self.assertEqual(seen, [{plan[0]: self.key(plan[0])}])

    def test_a_missing_or_unusable_choice_is_unanswered_and_wrong(self):
        session_id, plan = self.open_session()
        good = self.key(plan[0])
        choices = {
            plan[0]: good,                  # usable
            plan[1]: "Z",                   # not a key of the question
            plan[2]: "",                    # empty
            plan[3]: None,                  # no choice
            plan[4]: 1,                     # not text
        }
        result = self.submit(self.student, session_id, choices)
        self.assertEqual((result.score_correct, result.score_total), (1, 5))
        by_question = {row["question_id"]: row for row in self.answers(session_id)}
        self.assertEqual(by_question[plan[0]]["chosen_letter"], good)
        for qid in plan[1:]:
            self.assertEqual((by_question[qid]["chosen_letter"], by_question[qid]["is_correct"]), (None, 0), qid)

    def test_non_text_choice_values_are_unanswered(self):
        for value in (b"A", ["A"], ("A",), {"A"}, 65, 1.0, True, object()):
            with self.subTest(type(value).__name__):
                session_id, plan = self.open_session(scope=self.make_scope(8))
                result = self.assert_nothing_written(
                    lambda: self.submit(self.student, session_id, {plan[0]: value}))
                self.assertEqual(result.outcome, practice.OUTCOME_EMPTY)

    def test_keys_are_matched_exactly(self):
        session_id, plan = self.open_session()
        correct = self.key(plan[0])
        for variant in (correct.lower(), f" {correct}", f"{correct} ", f"{correct}\n", correct + correct):
            with self.subTest(variant=variant):
                if variant == correct:
                    continue
                result = self.assert_nothing_written(lambda: self.submit(self.student, session_id, {plan[0]: variant}))
                self.assertEqual(result.outcome, practice.OUTCOME_EMPTY)
        self.assertTrue(self.submit(self.student, session_id, {plan[0]: correct}).submitted)

    def test_choices_may_be_any_mapping(self):
        session_id, plan = self.open_session()
        mapping = {plan[0]: self.key(plan[0])}

        class ReadOnlyView(dict):
            """A dict subclass, as a request-form conversion may produce."""

        result = self.submit(self.student, session_id, ReadOnlyView(mapping))
        self.assertEqual(result.score_correct, 1)

    def test_option_keys_that_are_not_a_to_d_are_graded_by_key(self):
        scope = self.make_scope(6)
        topic = scope.topic_ids[0]
        kinds = {
            "digits": self.keyed_question(topic, "1234", "3"),
            "cyrillic": self.keyed_question(topic, "АБВГ", "В"),
            "lowercase": self.keyed_question(topic, "abcd", "c"),
            "mixed": self.keyed_question(topic, ["A", "B2", "x", "Я"], "x"),
            "latin, late letter correct": self.keyed_question(topic, "ABCD", "D"),
        }
        self.conn.commit()
        plan = list(kinds.values())
        session_id, _ = self.open_session(scope=scope, plan=plan)
        keys = {"digits": ("3", "1"), "cyrillic": ("В", "А"), "lowercase": ("c", "a"), "mixed": ("x", "A"),
                "latin, late letter correct": ("D", "A")}
        right = {kinds[name]: keys[name][0] for name in kinds}
        result = self.submit(self.student, session_id, right)
        self.assertEqual((result.score_correct, result.score_total), (5, 5))
        self.assertEqual({row["question_id"]: row["chosen_letter"] for row in self.answers(session_id)}, right)
        # A wrong stored key is wrong, and a key of another question's option set is unanswered.
        other_id, _ = self.open_session(self.other_student, scope, plan=plan)
        wrong = {kinds[name]: keys[name][1] for name in kinds}
        wrong[kinds["digits"]] = "C"                                    # not a key of that question
        result = self.submit(self.other_student, other_id, wrong)
        self.assertEqual((result.score_correct, result.score_total), (0, 5))
        stored = {row["question_id"]: row["chosen_letter"] for row in self.answers(other_id)}
        self.assertIsNone(stored[kinds["digits"]])
        self.assertEqual(stored[kinds["cyrillic"]], "А")

    def test_shuffled_display_letters_play_no_part(self):
        scope = self.make_scope(6)
        qid = self.keyed_question(scope.topic_ids[0], "АБВГ", "Б")      # stored keys equal the display alphabet
        self.conn.commit()
        for seed in ("seed-one", "seed-two", "seed-three", "seed-four", "seed-five", "seed-six"):
            with self.subTest(seed):
                who = self.add_account("student")
                self.conn.commit()
                session_id, _ = self.open_session(who, scope, plan=[qid], seed=seed)
                shown = {option["option_letter"]: option["display_letter"]
                         for option in quiz_load_questions(self.conn, [qid], seed, include_correct=True)[0]["options"]}
                moved = [key for key, shown_as in shown.items() if key != shown_as]
                self.assertTrue(moved, "the shuffle must move at least one option for the test to mean something")
                # What the student sees under a moved correct option is not its key.
                result = self.submit(who, session_id, {qid: shown["Б"]})
                if shown["Б"] == "Б":
                    self.assertEqual(result.outcome, practice.OUTCOME_SUBMITTED)
                    self.assertEqual(result.score_correct, 1)
                else:
                    self.assertEqual(result.score_correct, 0)           # the display letter names another key
                    self.assertEqual(self.answers(session_id)[0]["chosen_letter"], shown["Б"])
                other = self.add_account("student")
                self.conn.commit()
                other_id, _ = self.open_session(other, scope, plan=[qid], seed=seed)
                self.assertEqual(self.submit(other, other_id, {qid: "Б"}).score_correct, 1)   # the key always wins

    def test_a_display_letter_that_is_not_a_key_is_unanswered(self):
        scope = self.make_scope(6)
        qid = self.keyed_question(scope.topic_ids[0], "1234", "2")
        self.conn.commit()
        session_id, _ = self.open_session(scope=scope, plan=[qid])
        result = self.assert_nothing_written(lambda: self.submit(self.student, session_id, {qid: "Б"}))
        self.assertEqual(result.outcome, practice.OUTCOME_EMPTY)       # "Б" is only ever a display letter here

    def test_content_changed_since_the_start_is_graded_by_the_current_stored_options(self):
        scope = self.make_scope(8)
        session_id, plan = self.open_session(scope=scope)
        # plan[0]: the key D was renamed E (still four valid options): "D" is no longer a choice.
        self.conn.execute("UPDATE multiple_choice_options SET option_letter = 'E' WHERE question_id = ? "
                          "AND option_letter = 'D'", (plan[0],))
        # plan[1]: the correct answer moved from A to C.
        self.conn.execute("UPDATE multiple_choice_options SET is_correct = (option_letter = 'C') "
                          "WHERE question_id = ?", (plan[1],))
        self.conn.commit()
        result = self.submit(self.student, session_id, {plan[0]: "D", plan[1]: "A", plan[2]: "A", plan[3]: "E",
                                                        plan[4]: "A"})
        by_question = {row["question_id"]: row for row in self.answers(session_id)}
        self.assertEqual((by_question[plan[0]]["chosen_letter"], by_question[plan[0]]["is_correct"]), (None, 0))
        self.assertEqual((by_question[plan[1]]["chosen_letter"], by_question[plan[1]]["is_correct"]), ("A", 0))
        self.assertEqual((by_question[plan[2]]["chosen_letter"], by_question[plan[2]]["is_correct"]), ("A", 1))
        self.assertEqual((by_question[plan[3]]["chosen_letter"], by_question[plan[3]]["is_correct"]), (None, 0))
        self.assertEqual((by_question[plan[4]]["chosen_letter"], by_question[plan[4]]["is_correct"]), ("A", 1))
        self.assertEqual((result.score_correct, result.score_total), (2, 5))

    def test_a_choice_that_became_unusable_for_every_question_is_an_empty_submission(self):
        session_id, plan = self.open_session()
        for qid in plan:
            self.conn.execute("UPDATE multiple_choice_options SET option_letter = option_letter || '2' "
                              "WHERE question_id = ?", (qid,))
        self.conn.commit()
        result = self.assert_nothing_written(lambda: self.submit(self.student, session_id, {q: "A" for q in plan}))
        self.assertEqual(result.outcome, practice.OUTCOME_EMPTY)
        self.assertEqual(self.submit(self.student, session_id, {q: "A2" for q in plan}).score_correct, 5)

    def test_the_stored_order_is_the_answer_order_and_the_unique_questions_are_graded_once(self):
        scope = self.make_scope(8)
        plan = list(reversed(scope.all_question_ids[:5]))
        session_id, _ = self.open_session(scope=scope, plan=plan)
        result = self.submit(self.student, session_id, self.all_correct(plan))
        self.assertEqual([a.question_id for a in result.answers], plan)
        self.assertEqual(len(self.answers(session_id)), 5)


class EmptySubmissionTest(SubmitCase):
    def test_an_empty_submission_writes_nothing_and_the_session_stays_open(self):
        session_id, plan = self.open_session()
        for choices in ({}, {plan[0]: None}, {plan[0]: "Z"}, {plan[0]: ""}):
            with self.subTest(choices=choices):
                result = self.assert_nothing_written(lambda: self.submit(self.student, session_id, choices))
                self.assertEqual(result, practice.PracticeSubmission(practice.OUTCOME_EMPTY))
        row = self.session(session_id)
        self.assertEqual((row["submitted_at"], row["abandoned_at"], row["score_correct"], row["score_total"]),
                         (None, None, None, None))
        self.assertEqual(self.answers(), [])
        # The same session is submittable afterwards, so an empty attempt burned nothing.
        self.assertTrue(self.submit(self.student, session_id, {plan[0]: self.key(plan[0])}).submitted)

    def test_a_partial_submission_stores_one_row_per_presentable_question(self):
        scope = self.make_scope(8)
        session_id, plan = self.open_session(scope=scope)
        self.submit(self.student, session_id, {plan[2]: self.key(plan[2])})
        self.assertEqual({row["question_id"] for row in self.answers(session_id)}, set(plan))
        self.assertEqual(sum(1 for row in self.answers(session_id) if row["chosen_letter"] is None), 4)


# ====================================================================== eligibility exclusions
class EligibilityTest(SubmitCase):
    """Each exclusion is a pair: a plan of five good questions, one of them made bad -> four
    are graded and the bad one has no row; the same plan left good -> five are graded."""

    def mutations(self):
        existing_image = self.directory / "figure.png"
        existing_image.write_bytes(b"not-a-real-png-but-it-exists")

        def sql(statement, *params):
            return lambda qid: self.conn.execute(statement, (*params, qid)) and None

        return {
            "deleted": sql("DELETE FROM questions WHERE id = ?"),
            "options removed": self.break_question,
            "three options": sql("DELETE FROM multiple_choice_options WHERE option_letter = 'D' AND question_id = ?"),
            "two correct options": sql("UPDATE multiple_choice_options SET is_correct = 1 WHERE question_id = ?"),
            "no correct option": sql("UPDATE multiple_choice_options SET is_correct = 0 WHERE question_id = ?"),
            "blank option text": sql("UPDATE multiple_choice_options SET option_text = '-' WHERE option_letter = 'B' "
                                     "AND question_id = ?"),
            "turned into fill-in": sql("UPDATE questions SET question_type = 'fill_in' WHERE id = ?"),
            "unapproved AI": sql("UPDATE questions SET is_ai_generated = 1, quality_score = 0.4 WHERE id = ?"),
            "visual prompt without an image":
                sql("UPDATE questions SET prompt = ?, image_path = NULL WHERE id = ?", VISUAL_PROMPT),
            "visual prompt with a missing image":
                sql("UPDATE questions SET prompt = ?, image_path = ? WHERE id = ?", VISUAL_PROMPT,
                    str(self.directory / "missing.png")),
            "another subject": sql("UPDATE questions SET subject = 'physics' WHERE id = ?"),
            "official DZI (legacy exam)": self.make_dzi_legacy,
            "official DZI (task link)": self.make_dzi_task,
            "moved to another section": lambda qid: self.conn.execute(
                "UPDATE questions SET topic_id = ? WHERE id = ?", (self.make_scope(1).topic_ids[0], qid)),
        }, existing_image

    def test_each_exclusion_is_what_removes_the_question_from_the_grade(self):
        mutations, _ = self.mutations()
        for name, mutate in mutations.items():
            with self.subTest(name):
                scope = self.make_scope(8)
                session_id, plan = self.open_session(scope=scope)
                control_id, _ = self.open_session(self.other_student, scope, plan=plan)
                choices = {qid: "A" for qid in plan}             # "A" is the correct key of every good question
                # Control, before the change: the very same plan and choices count all five.
                control = self.submit(self.other_student, control_id, choices)
                self.assertEqual((control.score_correct, control.score_total), (5, 5))
                with self.conn:          # the control's rows go: a question that has answers cannot be deleted
                    self.conn.execute("DELETE FROM practice_answers WHERE session_id = ?", (control_id,))
                    self.conn.execute("DELETE FROM practice_sessions WHERE id = ?", (control_id,))
                mutate(plan[2])
                self.conn.commit()
                result = self.submit(self.student, session_id, choices)
                self.assertEqual(result.outcome, practice.OUTCOME_SUBMITTED)
                self.assertEqual((result.score_correct, result.score_total), (4, 4))   # not counted, not graded
                self.assertEqual({row["question_id"] for row in self.answers(session_id)}, set(plan) - {plan[2]})

    def test_the_same_plan_left_untouched_counts_all_five(self):
        session_id, plan = self.open_session(scope=self.make_scope(8))
        result = self.submit(self.student, session_id, {qid: "A" for qid in plan})
        self.assertEqual((result.score_correct, result.score_total), (5, 5))

    def test_a_visual_question_with_an_existing_image_stays_presentable(self):
        mutations, image = self.mutations()
        scope = self.make_scope(8)
        session_id, plan = self.open_session(scope=scope)
        self.conn.execute("UPDATE questions SET prompt = ?, image_path = ? WHERE id = ?",
                          (VISUAL_PROMPT, str(image), plan[0]))
        self.conn.commit()
        self.assertEqual(self.submit(self.student, session_id, {q: "A" for q in plan}).score_total, 5)

    def test_unrenderable_and_out_of_scope_questions_do_not_count_and_their_choices_are_ignored(self):
        scope = self.make_scope(8)
        session_id, plan = self.open_session(scope=scope)
        self.break_question(plan[0])
        self.make_dzi_legacy(plan[1])
        self.conn.commit()
        result = self.submit(self.student, session_id, self.all_correct(plan[2:]) | {plan[0]: "A", plan[1]: "A"})
        self.assertEqual((result.score_correct, result.score_total), (3, 3))
        self.assertEqual([row["question_id"] for row in self.answers(session_id)], sorted(plan[2:]))
        # The remaining question is the only one a score can come from: one presentable question left.
        single_id, single = self.open_session(self.other_student, scope, plan=plan[:3])
        result = self.submit(self.other_student, single_id, {plan[2]: self.key(plan[2], correct=False)})
        self.assertEqual((result.score_correct, result.score_total), (0, 1))

    def test_a_submission_may_not_introduce_a_question_into_the_scope(self):
        scope = self.make_scope(8)
        session_id, plan = self.open_session(scope=scope, plan=scope.all_question_ids[:3])
        intruder = scope.all_question_ids[5]
        result = self.submit(self.student, session_id, {**self.all_correct(plan), intruder: "A"})
        self.assertEqual(result.score_total, 3)
        self.assertNotIn(intruder, {row["question_id"] for row in self.answers()})


# ====================================================================== transactions
class TransactionTest(SubmitCase):
    def test_the_timestamp_is_sampled_while_the_writer_lock_is_held(self):
        session_id, plan = self.open_session()
        probe = sqlite3.connect(self.db_path, timeout=0)
        self.addCleanup(probe.close)
        probe.execute("BEGIN IMMEDIATE")                   # control: with no submit running the lock is free
        probe.rollback()
        sampled = []

        def sample():
            with self.assertRaises(sqlite3.OperationalError):
                probe.execute("BEGIN IMMEDIATE")           # the service holds the writer lock right now
            sampled.append(self.conn.in_transaction)
            return datetime(2026, 11, 20, 12, 0, 0, 999999)

        with patch.object(lifecycle, "naive_utc_now", side_effect=sample):
            result = self.submit(self.student, session_id, self.all_correct(plan))
        self.assertEqual(sampled, [True])
        self.assertEqual(self.session(session_id)["submitted_at"], "2026-11-20 12:00:00")
        self.assertTrue(result.submitted)
        self.assertFalse(self.conn.in_transaction)

    def test_a_non_writing_outcome_never_samples_the_clock(self):
        scope = self.make_scope(8)
        session_id, plan = self.open_session(scope=scope)
        submitted = self.insert_session(self.other_student, scope, plan, submitted=STAMP)
        abandoned = self.insert_session(self.teacher, scope, plan, abandoned=STAMP)
        with patch.object(lifecycle, "naive_utc_now", side_effect=AssertionError("the clock was read")):
            for who, target, choices in ((self.student, session_id, {}), (self.other_student, submitted, {}),
                                         (self.teacher, abandoned, {}), (self.admin, session_id, {}),
                                         (self.student, 987654, {})):
                self.submit(who, target, choices)

    def test_a_failing_answer_insert_restores_every_previous_row(self):
        session_id, plan = self.open_session()
        before = self.practice_digest()
        self.conn.execute(f"CREATE TRIGGER fail_answer BEFORE INSERT ON practice_answers WHEN NEW.question_id = {plan[3]} "
                          "BEGIN SELECT RAISE(ABORT, 'answer refused'); END")
        self.conn.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            self.submit(self.student, session_id, self.all_correct(plan))
        self.assertEqual(before, self.practice_digest())            # the answers written first rolled back too
        self.assertEqual(self.answers(), [])
        self.assertIsNone(self.session(session_id)["submitted_at"])
        self.assert_closed_and_free()
        self.conn.execute("DROP TRIGGER fail_answer")               # control: the same call then works
        self.conn.commit()
        self.assertTrue(self.submit(self.student, session_id, self.all_correct(plan)).submitted)

    def test_a_failing_finalization_rolls_back_the_answers(self):
        session_id, plan = self.open_session()
        before = self.practice_digest()
        self.conn.execute("CREATE TRIGGER fail_final BEFORE UPDATE ON practice_sessions "
                          "BEGIN SELECT RAISE(ABORT, 'update refused'); END")
        self.conn.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            self.submit(self.student, session_id, self.all_correct(plan))
        self.assertEqual(before, self.practice_digest())
        self.assertEqual(self.answers(), [])
        self.assert_closed_and_free()
        self.conn.execute("DROP TRIGGER fail_final")
        self.conn.commit()
        self.assertTrue(self.submit(self.student, session_id, self.all_correct(plan)).submitted)

    def test_a_finalization_that_changes_no_row_is_an_error_not_a_silent_success(self):
        session_id, plan = self.open_session()
        before = self.practice_digest()
        self.conn.execute("CREATE TRIGGER ignore_final BEFORE UPDATE ON practice_sessions "
                          "BEGIN SELECT RAISE(IGNORE); END")
        self.conn.commit()
        with self.assertRaises(practice.PracticeStateError):
            self.submit(self.student, session_id, self.all_correct(plan))
        self.assertEqual(before, self.practice_digest())
        self.assertEqual(self.answers(), [])
        self.assert_closed_and_free()

    def test_an_answer_insert_that_changes_no_row_is_an_error_not_a_silent_success(self):
        session_id, plan = self.open_session()
        before = self.practice_digest()
        self.conn.execute("CREATE TRIGGER ignore_answer BEFORE INSERT ON practice_answers "
                          "BEGIN SELECT RAISE(IGNORE); END")
        self.conn.commit()
        with self.assertRaises(practice.PracticeStateError):
            self.submit(self.student, session_id, self.all_correct(plan))
        self.assertEqual(before, self.practice_digest())
        self.assert_closed_and_free()

    def test_unexpected_database_errors_propagate_and_release_the_lock(self):
        session_id, plan = self.open_session()
        self.conn.execute("ALTER TABLE practice_answers RENAME TO practice_answers_moved")
        self.conn.commit()
        with self.assertRaises(sqlite3.OperationalError):
            self.submit(self.student, session_id, self.all_correct(plan))
        self.assert_closed_and_free()
        self.assertIsNone(self.session(session_id)["submitted_at"])

    def test_an_error_raised_by_a_helper_is_not_swallowed(self):
        session_id, plan = self.open_session()
        for name in ("_eligible_question_ids", "_grade", "_finalize"):
            with self.subTest(name):
                with patch.object(practice, name, side_effect=sqlite3.DatabaseError("disk I/O error")):
                    with self.assertRaises(sqlite3.DatabaseError):
                        self.assert_nothing_written(lambda: self.submit(self.student, session_id, self.all_correct(plan)))
                self.assert_closed_and_free()
        self.assertTrue(self.submit(self.student, session_id, self.all_correct(plan)).submitted)

    def test_a_lock_held_by_another_connection_is_not_stolen_and_nothing_is_written(self):
        session_id, plan = self.open_session()
        busy = web_db.connect_db(self.db_path, timeout=0.2)
        self.addCleanup(busy.close)
        holder = sqlite3.connect(self.db_path, timeout=0)
        self.addCleanup(holder.close)
        holder.execute("BEGIN IMMEDIATE")
        try:
            with self.assertRaises(sqlite3.OperationalError):
                self.submit(self.student, session_id, self.all_correct(plan), conn=busy)
            self.assertFalse(busy.in_transaction)
        finally:
            holder.rollback()
        self.assertEqual(self.answers(), [])
        self.assertTrue(self.submit(self.student, session_id, self.all_correct(plan), conn=busy).submitted)

    def test_non_writing_outcomes_leave_no_transaction_open(self):
        session_id, plan = self.open_session()
        for who, target, choices in ((self.student, session_id, {}), (self.student, 987654, {}),
                                     (self.other_student, session_id, {})):
            self.submit(who, target, choices)
            self.assertFalse(self.conn.in_transaction)
        self.assert_closed_and_free()

    def test_the_service_has_no_part_in_the_lifecycle_the_quota_or_the_start(self):
        assignment = runtime_fixtures.add_assignment(self.conn, question_plan=None)
        attempt = runtime_fixtures.add_attempt(self.conn, assignment, user_id=self.student.user_id)
        runtime_fixtures.add_answer(self.conn, attempt, runtime_fixtures.first_multiple_choice_question_ids(self.conn, 1)[0])
        self.conn.commit()
        set_lifecycle_clock_row(self.db_path, high_water=datetime(2026, 11, 20, 9, 0, 0), state="suspect",
                                reason="clock_step", changed_at=datetime(2026, 11, 20, 9, 0, 0))
        scope = self.make_scope(12)
        mine, plan = self.open_session(self.student, scope, plan=scope.all_question_ids[:5])
        theirs, _ = self.open_session(self.other_student, scope, plan=scope.all_question_ids[3:8])
        stale, _ = self.open_session(self.teacher, scope, plan=[8_000_001])
        before = self.table_digests(skip={"practice_sessions", "practice_answers"})
        other_rows = {row["id"]: row for row in self.sessions() if row["id"] != mine}
        forbidden = AssertionError("the teacher lifecycle, the quota or the start was used")
        with patch.object(lifecycle, "write_transaction", side_effect=forbidden), \
                patch.object(lifecycle, "_begin_write", side_effect=forbidden), \
                patch.object(lifecycle, "wall_reading", side_effect=forbidden), \
                patch.object(lifecycle, "read_clock_row", side_effect=forbidden), \
                patch.object(assignment_quota, "enforce", side_effect=forbidden), \
                patch.object(assignment_quota, "record", side_effect=forbidden), \
                patch.object(lifecycle, "count_unclosed_owned", side_effect=forbidden), \
                patch.object(practice, "_new_seed", side_effect=forbidden), \
                patch.object(practice, "_create", side_effect=forbidden), \
                patch.object(practice, "_abandon", side_effect=forbidden):
            result = self.submit(self.student, mine, self.all_correct(plan))
            self.submit(self.teacher, stale, {1: "A"})                     # a stale session is not abandoned here
        self.assertTrue(result.submitted)
        self.assertEqual(before, self.table_digests(skip={"practice_sessions", "practice_answers"}))
        self.assertEqual({row["id"]: row for row in self.sessions() if row["id"] != mine}, other_rows)
        with self.raw() as conn:
            self.assertEqual(conn.execute("SELECT state, high_water_at FROM lifecycle_clock").fetchone()[:],
                             ("suspect", "2026-11-20 09:00:00"))
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM quiz_answers").fetchone()[0], 1)   # legacy state kept
        self.assertEqual({row["session_id"] for row in self.answers()}, {mine})


# ====================================================================== autocommit connections
class AutocommitConnectionTest(SubmitCase):
    """Under ``autocommit=True`` ``Connection.commit()``/``rollback()`` do nothing even after a manual
    ``BEGIN``: the service must end its own transaction with explicit SQL or the lock stays held."""

    def autocommit_connection(self):
        conn = sqlite3.connect(self.db_path, timeout=30, autocommit=True)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        self.addCleanup(conn.close)
        return conn

    def test_every_outcome_ends_the_transaction_and_a_write_is_committed(self):
        conn = self.autocommit_connection()
        scope = self.make_scope(12)
        fresh, plan = self.open_session(self.student, scope, plan=scope.all_question_ids[:5])
        stale, _ = self.open_session(self.other_student, scope, plan=[8_000_001])
        submitted = self.insert_session(self.teacher, scope, plan, submitted=STAMP)
        abandoned = self.insert_session(self.admin, scope, plan, abandoned=STAMP)
        inactive = self.add_account("student", active=False)
        dzi = self.make_scope(8, section_type="dzi_training")
        in_dzi, _ = self.open_session(self.student, dzi)
        self.conn.commit()
        good = self.all_correct(plan)
        cases = {
            "empty": (self.student, fresh, {}, practice.OUTCOME_EMPTY),
            "submitted": (self.student, fresh, good, practice.OUTCOME_SUBMITTED),
            "already submitted": (self.student, fresh, good, practice.OUTCOME_ALREADY_SUBMITTED),
            "stale": (self.other_student, stale, good, practice.OUTCOME_STALE),
            "retried": (self.teacher, submitted, good, practice.OUTCOME_ALREADY_SUBMITTED),
            "abandoned": (self.admin, abandoned, good, practice.OUTCOME_ABANDONED),
            "not found": (self.student, 987654, good, practice.OUTCOME_NOT_FOUND),
            "foreign": (self.other_student, fresh, good, practice.OUTCOME_NOT_FOUND),
            "account unavailable": (inactive, fresh, good, practice.OUTCOME_ACCOUNT_UNAVAILABLE),
            "scope unavailable": (self.student, in_dzi, good, practice.OUTCOME_SCOPE_UNAVAILABLE),
        }
        for name, (who, session_id, choices, expected) in cases.items():
            with self.subTest(name):
                result = self.submit(who, session_id, choices, conn=conn)
                self.assertEqual(result.outcome, expected)
                self.assert_closed_and_free(conn)
        # The write is durable: visible to another connection with nothing left to commit.
        self.assertEqual(len(self.answers(fresh)), 5)
        self.assertIsNotNone(self.session(fresh)["submitted_at"])

    def test_a_failure_rolls_back_and_releases_the_lock(self):
        conn = self.autocommit_connection()
        session_id, plan = self.open_session()
        before = self.practice_digest()
        self.conn.execute(f"CREATE TRIGGER fail_answer BEFORE INSERT ON practice_answers WHEN NEW.question_id = {plan[4]} "
                          "BEGIN SELECT RAISE(ABORT, 'answer refused'); END")
        self.conn.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            self.submit(self.student, session_id, self.all_correct(plan), conn=conn)
        self.assert_closed_and_free(conn)
        self.assertEqual(before, self.practice_digest())

    def test_a_caller_owned_transaction_is_refused_and_left_alone(self):
        conn = self.autocommit_connection()
        session_id, plan = self.open_session()
        conn.execute("BEGIN")
        with self.assertRaises(RuntimeError):
            self.submit(self.student, session_id, self.all_correct(plan), conn=conn)
        self.assertTrue(conn.in_transaction)              # the caller's transaction, untouched
        conn.execute("ROLLBACK")
        self.assertEqual(self.answers(), [])
        self.assertTrue(self.submit(self.student, session_id, self.all_correct(plan), conn=conn).submitted)

    def test_a_caller_transaction_with_pending_writes_is_not_committed_by_a_refused_submit(self):
        conn = self.autocommit_connection()
        session_id, plan = self.open_session()
        conn.execute("BEGIN")
        conn.execute("INSERT INTO curriculum_topics (topic_slug, title_bg) VALUES ('caller-pending', 'x')")
        with self.assertRaises(RuntimeError):
            self.submit(self.student, session_id, self.all_correct(plan), conn=conn)
        conn.execute("ROLLBACK")
        self.assertIsNone(self.raw().execute(
            "SELECT 1 FROM curriculum_topics WHERE topic_slug = 'caller-pending'").fetchone())


# ====================================================================== concurrency
class ConcurrencyTest(SubmitCase):
    def run_threads(self, jobs, *, barrier=True):
        """Run ``jobs`` (callables taking their own connection) at once; returns results or exceptions."""
        gate = threading.Barrier(len(jobs), timeout=30) if barrier else None
        outcomes = [None] * len(jobs)

        def worker(index, job):
            conn = web_db.connect_db(self.db_path, timeout=30)
            try:
                if gate:
                    gate.wait()
                outcomes[index] = job(conn)
            except BaseException as exc:             # reported to the test, which asserts on it
                outcomes[index] = exc
            finally:
                conn.close()

        threads = [threading.Thread(target=worker, args=(index, job), daemon=True) for index, job in enumerate(jobs)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(60)
            self.assertFalse(thread.is_alive(), "a call did not finish")
        return outcomes

    def test_two_simultaneous_identical_submits_store_one_answer_set_and_one_score(self):
        scope = self.make_scope(12)
        for round_number in range(8):
            who = self.add_account("student")
            self.conn.commit()
            session_id, plan = self.open_session(who, scope, plan=scope.all_question_ids[round_number:round_number + 5])
            choices = {plan[0]: self.key(plan[0]), plan[1]: self.key(plan[1], correct=False)}
            outcomes = self.run_threads([lambda c: self.submit(who, session_id, choices, conn=c)] * 2)
            for outcome in outcomes:
                self.assertIsInstance(outcome, practice.PracticeSubmission, outcome)
            self.assertEqual(sorted(o.outcome for o in outcomes),
                             [practice.OUTCOME_ALREADY_SUBMITTED, practice.OUTCOME_SUBMITTED])
            self.assertEqual({(o.score_correct, o.score_total, o.submitted_at) for o in outcomes},
                             {(1, 5, outcomes[0].submitted_at)})
            row = self.session(session_id)
            self.assertEqual((row["score_correct"], row["score_total"], row["submitted_at"]),
                             (1, 5, outcomes[0].submitted_at))
            self.assertEqual(len(self.answers(session_id)), 5)

    def test_the_loser_of_two_different_submits_cannot_change_what_the_winner_stored(self):
        scope = self.make_scope(12)
        for round_number in range(6):
            who = self.add_account("student")
            self.conn.commit()
            session_id, plan = self.open_session(who, scope, plan=scope.all_question_ids[round_number:round_number + 5])
            right, wrong = self.all_correct(plan), {q: self.key(q, correct=False) for q in plan}
            outcomes = self.run_threads([lambda c: self.submit(who, session_id, right, conn=c),
                                         lambda c: self.submit(who, session_id, wrong, conn=c)])
            winner = [o for o in outcomes if isinstance(o, practice.PracticeSubmission) and o.submitted]
            loser = [o for o in outcomes if isinstance(o, practice.PracticeSubmission) and o.already_submitted]
            self.assertEqual((len(winner), len(loser)), (1, 1), outcomes)
            stored = {row["question_id"]: row["chosen_letter"] for row in self.answers(session_id)}
            self.assertEqual(stored, {a.question_id: a.chosen_letter for a in winner[0].answers})
            self.assertEqual(self.session(session_id)["score_correct"], winner[0].score_correct)
            self.assertEqual((loser[0].score_correct, loser[0].submitted_at),
                             (winner[0].score_correct, winner[0].submitted_at))

    def test_the_second_submit_waits_for_the_first_and_then_finds_it_submitted(self):
        session_id, plan = self.open_session()
        choices = self.all_correct(plan)
        inside, release = threading.Event(), threading.Event()
        calls = []

        def hold_the_lock():
            calls.append(1)
            inside.set()
            self.assertTrue(release.wait(30))
            return "2026-11-20 12:00:00"

        results = {}

        def run(name):
            def job():
                conn = web_db.connect_db(self.db_path, timeout=30)
                try:
                    results[name] = self.submit(self.student, session_id, choices, conn=conn)
                except BaseException as exc:
                    results[name] = exc
                finally:
                    conn.close()
            return threading.Thread(target=job, daemon=True)

        with patch.object(practice, "_utc_now_text", side_effect=hold_the_lock):
            first = run("first")
            first.start()
            self.assertTrue(inside.wait(30))
            probe = sqlite3.connect(self.db_path, timeout=0)
            self.addCleanup(probe.close)
            with self.assertRaises(sqlite3.OperationalError):        # the lock is held while deciding
                probe.execute("BEGIN IMMEDIATE")
            second = run("second")
            second.start()
            release.set()
            first.join(60)
            second.join(60)
        self.assertEqual(calls, [1])                                  # only the first ever reached the write
        self.assertEqual(results["first"].outcome, practice.OUTCOME_SUBMITTED)
        self.assertEqual(results["second"].outcome, practice.OUTCOME_ALREADY_SUBMITTED)
        self.assertEqual(results["second"].submitted_at, "2026-11-20 12:00:00")
        self.assertEqual(len(self.answers(session_id)), 5)

    def test_a_deferred_transaction_is_the_negative_control_and_does_not_keep_the_contract(self):
        session_id, plan = self.open_session()
        choices = self.all_correct(plan)
        rendezvous = threading.Barrier(2, timeout=10)
        real_grade = practice._grade

        def both_have_read_an_open_session(*args, **kwargs):
            rendezvous.wait()
            return real_grade(*args, **kwargs)

        def deferred_job(conn_unused):
            conn = sqlite3.connect(self.db_path, timeout=5, factory=DeferredConnection)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys=ON")
            try:
                return practice.submit_practice(conn, self.student, session_id, choices)
            except BaseException as exc:
                return exc
            finally:
                conn.close()

        with patch.object(practice, "_grade", side_effect=both_have_read_an_open_session):
            outcomes = self.run_threads([deferred_job, deferred_job], barrier=False)
        failures = [o for o in outcomes if isinstance(o, BaseException)]
        self.assertTrue(failures, "without BEGIN IMMEDIATE the race must surface as an error")
        self.assertTrue(all(isinstance(f, (sqlite3.OperationalError, sqlite3.IntegrityError)) for f in failures), failures)
        self.assertLessEqual(len(self.answers(session_id)), 5)

    def test_a_stale_session_racing_its_replacement_never_grades_the_replacement(self):
        seen = set()
        for _ in range(8):
            who = self.add_account("student")
            scope = self.make_scope(12)
            old, old_plan = self.open_session(who, scope, plan=scope.all_question_ids[:5])
            for qid in old_plan:
                self.break_question(qid)                      # the old plan is stale; seven good questions remain
            self.conn.commit()
            old_tab = {qid: "A" for qid in scope.all_question_ids}
            outcomes = self.run_threads([lambda c: self.submit(who, old, old_tab, conn=c),
                                         lambda c: self.start(who, scope, conn=c)])
            submit_result, start_result = outcomes
            self.assertIsInstance(submit_result, practice.PracticeSubmission, submit_result)
            self.assertIsInstance(start_result, practice.PracticeStart, start_result)
            seen.add(submit_result.outcome)
            self.assertIn(submit_result.outcome, (practice.OUTCOME_STALE, practice.OUTCOME_ABANDONED))
            self.assertEqual((start_result.outcome, start_result.abandoned_session_id),
                             (practice.OUTCOME_CREATED, old))
            self.assertEqual(self.answers(), [])              # nothing, of the old tab or the replacement, was graded
            self.assertEqual([r for r in self.sessions() if r["user_id"] == who.user_id and r["submitted_at"]], [])
            replacement = self.session(start_result.session_id)
            self.assertEqual((replacement["submitted_at"], replacement["abandoned_at"]), (None, None))
            self.assertIsNotNone(self.session(old)["abandoned_at"])
            # A late retry of the same old tab is refused as abandoned.
            self.assertEqual(self.submit(who, old, old_tab).outcome, practice.OUTCOME_ABANDONED)
            self.assertEqual(self.answers(), [])
        self.assertTrue(seen <= {practice.OUTCOME_STALE, practice.OUTCOME_ABANDONED})

    def test_a_submit_racing_a_start_of_the_same_open_session_leaves_one_consistent_result(self):
        seen = set()
        for _ in range(8):
            who = self.add_account("student")
            scope = self.make_scope(12)
            session_id, plan = self.open_session(who, scope, plan=scope.all_question_ids[:5])
            choices = self.all_correct(plan)
            submit_result, start_result = self.run_threads([lambda c: self.submit(who, session_id, choices, conn=c),
                                                            lambda c: self.start(who, scope, conn=c)])
            self.assertIsInstance(submit_result, practice.PracticeSubmission, submit_result)
            self.assertIsInstance(start_result, practice.PracticeStart, start_result)
            self.assertEqual(submit_result.outcome, practice.OUTCOME_SUBMITTED)      # start never abandons a live one
            seen.add(start_result.outcome)
            self.assertIn(start_result.outcome, (practice.OUTCOME_RESUMED, practice.OUTCOME_CREATED))
            if start_result.resumed:
                self.assertEqual(start_result.session_id, session_id)
            else:
                self.assertNotEqual(start_result.session_id, session_id)
            self.assertEqual(len(self.answers(session_id)), 5)
            self.assertEqual(self.session(session_id)["score_total"], 5)
            self.assertLessEqual(len([r for r in self.sessions()
                                      if r["user_id"] == who.user_id and not r["submitted_at"] and not r["abandoned_at"]]), 1)
        self.assertTrue(seen)

    def test_different_accounts_and_sessions_submit_concurrently_without_interference(self):
        scope = self.make_scope(12)
        accounts = [self.add_account("student") for _ in range(6)]
        self.conn.commit()
        sessions = [(who, *self.open_session(who, scope, plan=scope.all_question_ids[index:index + 5]))
                    for index, who in enumerate(accounts)]
        chosen = [self.all_correct(plan) for _, _, plan in sessions]          # read on this thread, not the workers'
        outcomes = self.run_threads([lambda c, s=s, k=k: self.submit(s[0], s[1], k, conn=c)
                                     for s, k in zip(sessions, chosen)])
        for outcome in outcomes:
            self.assertIsInstance(outcome, practice.PracticeSubmission, outcome)
            self.assertEqual((outcome.outcome, outcome.score_correct, outcome.score_total),
                             (practice.OUTCOME_SUBMITTED, 5, 5))
        self.assertEqual(len(self.answers()), 30)


# ====================================================================== canonical content
class CanonicalContentTest(SubmitCase):
    def section(self, slug):
        return self.conn.execute("SELECT id FROM curriculum_sections WHERE section_slug = ?", (slug,)).fetchone()[0]

    def correct_keys(self, question_ids):
        with self.raw() as conn:
            return {qid: conn.execute("SELECT option_letter FROM multiple_choice_options "
                                      "WHERE question_id = ? AND is_correct = 1", (qid,)).fetchone()[0]
                    for qid in question_ids}

    def test_a_real_it_section_session_is_graded_on_real_options(self):
        section = self.section("grade8-computer-systems")
        started = self.start(self.student, section)
        self.assertTrue(started.created)
        correct = self.correct_keys(started.question_ids)
        half = {qid: key for qid, key in list(correct.items())[:6]}
        result = self.submit(self.student, started.session_id, half)
        self.assertEqual((result.outcome, result.score_correct, result.score_total),
                         (practice.OUTCOME_SUBMITTED, 6, len(started.question_ids)))
        self.assertEqual({row["question_id"]: row["is_correct"] for row in self.answers(started.session_id)},
                         {qid: int(qid in half) for qid in started.question_ids})
        retry = self.submit(self.student, started.session_id, correct)
        self.assertEqual((retry.outcome, retry.score_correct), (practice.OUTCOME_ALREADY_SUBMITTED, 6))

    def test_a_real_topic_session_and_the_real_dzi_training_section(self):
        section = self.section("grade8-computer-systems")
        topic = self.conn.execute(
            "SELECT q.topic_id FROM questions q JOIN topic_section_assignments t ON t.topic_id = q.topic_id "
            "AND t.section_id = ? GROUP BY q.topic_id ORDER BY COUNT(*) DESC LIMIT 1", (section,)).fetchone()[0]
        started = self.start(self.student, section, topic)
        self.assertTrue(started.created)
        result = self.submit(self.student, started.session_id, self.correct_keys(started.question_ids))
        self.assertEqual((result.score_correct, result.score_total), (len(started.question_ids),) * 2)
        dzi = self.insert_session(self.other_student, type("S", (), {"section_id": self.section("grade12-dzi-preparation")}),
                                  [1, 2, 3])
        before = self.practice_digest()
        self.assertEqual(self.submit(self.other_student, dzi, {1: "A"}).outcome, practice.OUTCOME_SCOPE_UNAVAILABLE)
        self.assertEqual(before, self.practice_digest())

    def test_the_database_stays_consistent_after_every_kind_of_submission(self):
        scope = self.make_scope(12)
        a, plan_a = self.open_session(self.student, scope)
        b, _ = self.open_session(self.other_student, scope, plan=[8_000_001])
        c, plan_c = self.open_session(self.teacher, scope, plan=scope.all_question_ids[:5])
        self.submit(self.student, a, self.all_correct(plan_a))
        self.submit(self.student, a, {})
        self.submit(self.other_student, b, {8_000_001: "A"})
        self.submit(self.teacher, c, {})
        self.submit(self.admin, c, {plan_c[0]: "A"})
        self.submit(self.teacher, c, {plan_c[0]: "A"})
        with self.raw() as conn:
            self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(conn.execute("PRAGMA quick_check").fetchone()[0], "ok")
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM practice_sessions s WHERE submitted_at IS NOT NULL AND score_total != "
                "(SELECT COUNT(*) FROM practice_answers a WHERE a.session_id = s.id)").fetchone()[0], 0)
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM practice_sessions s WHERE submitted_at IS NOT NULL AND score_correct != "
                "(SELECT COALESCE(SUM(is_correct), 0) FROM practice_answers a WHERE a.session_id = s.id)").fetchone()[0], 0)
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM practice_answers a JOIN practice_sessions s ON s.id = a.session_id "
                "WHERE s.submitted_at IS NULL").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
