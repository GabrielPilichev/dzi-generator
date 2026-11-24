"""The dormant DZI Part 1 result loader (C3.4): ``web.services.dzi_part1.load_part1_result``.

Every database is a temporary clone of the migrations + canonical template
(``tests.support.build_test_database``) at a frozen lifecycle time (``tests.support.lifecycle_time``), the
fixtures of the start and submit service tests. No connection opens ``data/questions.db`` and no test goes
through the isolated-app connection guard. Every attempt below is a real start, then a real submit, then a
real read; expected values come from plain SQL over the stored rows, not from the loader.

What is proved, and how:

* **Contract and owner**: the persisted attempt comes back as saved; a missing, foreign, open, grace, expired
  and abandoned attempt (teacher and admin principals included) are one ``not_found`` that is decided from the
  row before anything else is parsed; an inactive account is its own outcome; bad ids and principals raise.
* **No clock, blueprint or write**: the active blueprint, the grace constant, the wall clock and the lifecycle
  layer play no part; every table (the clock row included) is unchanged; the statements are reads only; a
  ``query_only``, an ``autocommit=True`` and an ``isolation_level=None`` connection work; a caller transaction
  is reused and never ended, on success and on error; the loader's own is ended with explicit SQL.
* **One snapshot**: a real writer on a separate connection commits between the loader's reads (WAL) and the
  result is still wholly the earlier state, with a pass-through negative control that shows the probe works.
* **Saved scoring**: the canonical 45-point result, distinct saved subtotals, a one-plus-two item, an
  effective score below the planned maximum with a gap, and nothing regraded, repaired or invented.
* **Content drift**: a changed key, accepted answer, prompt and option text, removed or added parts, changed
  weights, a changed type, a re-seeded blueprint and the stimulus list; an empty review that still returns the
  saved score; every case keeps the saved verdicts and points.
* **Content the per-item rule cannot interpret**: a non-numeric flag, a prompt that is not text or an infinite
  number on one item makes that item unavailable and nothing else: the saved result, the saved verdicts and the
  other items (with their original slot numbers) come back, the stored rows are untouched, a database error and
  any other error still propagate, and the normal path judges the saved slots in one batch.
* **Corrupt stored state**: every tamper of the attempt row or the answer rows raises, writes nothing and
  leaves no transaction (a correct verdict on a blank short answer included, whatever blank it is stored as); the
  same tamper on an open row is still ``not_found``.
* **Saved slot numbers**: the loader's own rows carry ``saved_slot_number`` and render the original slot numbers,
  gaps included; the same rows without it are numbered by position.
* **Dormancy**: nothing imports the service and no simulation route exists.
"""

from __future__ import annotations

import json
import re
import sqlite3
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from tests.support import web_app
from tests.test_dzi_part1_result_partial import NUMBER, render_partial
from tests.test_dzi_part1_start_service import T0, ts
from tests.test_dzi_part1_submit_service import SubmitCase
from web import db as web_db
from web.access import ANONYMOUS, account_principal
from web.services import dzi_part1 as part1
from web.services import lifecycle
from web.services.quiz import QUIZ_NO_EXPLANATION_MESSAGE, quiz_load_questions

ROOT = Path(__file__).resolve().parents[1]
CYR_B = "Б"


class ResultCase(SubmitCase):
    """A real start -> submit -> read round trip, and tools to damage the stored state in between."""

    def finish(self, fields=None, who=None, *, before_submit=None):
        """A started attempt of ``who`` that is really submitted (every right answer unless ``fields`` is given)."""
        who = who or self.student
        attempt_id = self.begin(who).attempt_id
        answers = self.right_fields(attempt_id) if fields is None else fields(attempt_id)  # before any damage below
        if before_submit is not None:
            before_submit(attempt_id)
        result = self.submit(answers, attempt_id, who)
        self.assertEqual(result.outcome, "submitted")
        return attempt_id

    def load(self, attempt_id, who=None, conn=None):
        return part1.load_part1_result(conn or self.conn, who or self.student, attempt_id)

    def change(self, statement, params=(), *, check_constraints=True):
        """A committed write on its own connection, as an operator, an old build or corruption might do it."""
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("PRAGMA foreign_keys=OFF")
            if not check_constraints:
                conn.execute("PRAGMA ignore_check_constraints=ON")
            conn.execute(statement, params)
            conn.commit()
        finally:
            conn.close()

    def assert_released(self, conn=None):
        """No transaction on ``conn`` and no lock a writer on another connection would wait for."""
        self.assertFalse((conn or self.conn).in_transaction)
        probe = sqlite3.connect(self.db_path, timeout=0)
        self.addCleanup(probe.close)
        probe.execute("UPDATE users SET display_name = display_name WHERE id = ?", (self.student.user_id,))
        probe.commit()

    def slot_of(self, attempt_id, question_id):
        return self.plan_of(attempt_id).index(question_id) + 1

    def autocommit_connection(self, *, query_only=False):
        conn = sqlite3.connect(self.db_path, timeout=30, autocommit=True)
        self.addCleanup(conn.close)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        if query_only:
            conn.execute("PRAGMA query_only=ON")
        return conn

    def shown(self, result):
        return [row["id"] for row in result.questions]

    def make_wal(self):
        with closing(self.raw()) as conn:
            self.assertEqual(conn.execute("PRAGMA journal_mode=WAL").fetchone()[0], "wal")

    def drop_mc_slot(self, attempt_id, index=2):
        """Make one multiple-choice item unpresentable *before* the submit, so it is never graded."""
        question_id = self.mc_ids(attempt_id)[index]
        self.sql("UPDATE multiple_choice_options SET is_correct = 0 WHERE question_id = ?", (question_id,))
        return question_id

    def restore_mc_key(self, question_id, letter):
        self.sql("UPDATE multiple_choice_options SET is_correct = CASE option_letter WHEN ? THEN 1 ELSE 0 END "
                 "WHERE question_id = ?", (letter, question_id))


# =========================================================================== contract and owner

class ContractTest(ResultCase):
    def test_a_loaded_result_is_the_persisted_attempt_and_nothing_else(self):
        attempt_id = self.finish()
        result = self.load(attempt_id)
        row, answers = self.attempt(attempt_id), self.answers(attempt_id)
        self.assertEqual(result.outcome, part1.OUTCOME_LOADED)
        self.assertTrue(result.loaded)
        self.assertEqual((result.attempt_id, result.submitted_at, result.started_at, result.duration_seconds),
                         (attempt_id, row["submitted_at"], row["started_at"], 5400))
        self.assertEqual((result.score_points, result.score_max, result.max_points_planned), (45, 45, 45))
        self.assertEqual((result.multiple_choice_points, result.multiple_choice_max), (15, 15))
        self.assertEqual((result.short_answer_points, result.short_answer_max), (30, 30))
        self.assertEqual(result.grader_version, part1.GRADER_VERSION)
        self.assertEqual(result.planned_slot_count, 25)
        every_slot = tuple(range(1, 26))
        self.assertEqual((result.saved_slot_numbers, result.reviewed_slot_numbers), (every_slot, every_slot))
        self.assertEqual((result.omitted_slot_numbers, result.unavailable_slot_numbers), ((), ()))
        self.assertFalse(result.only_saved_score)
        self.assertFalse(result.below_planned_maximum)
        self.assertEqual([q["saved_slot_number"] for q in result.questions], list(range(1, 16)))
        self.assertEqual([q["saved_slot_number"] for q in result.short_answers], list(range(16, 26)))
        self.assertEqual(self.shown(result), self.plan_of(attempt_id)[:15])
        self.assertEqual(sum(len(q["parts"]) for q in result.short_answers),
                         sum(1 for r in answers if r["kind"] == "short_answer"))

    def test_the_review_context_is_what_the_shared_partial_reads(self):
        result = self.load(self.finish())
        context = result.review_context()
        self.assertEqual(set(context), {"questions", "stale_attempt_message"})
        self.assertEqual((context["questions"], context["stale_attempt_message"]), (list(result.questions), None))
        self.assertEqual([q["saved_slot_number"] for q in context["questions"]], list(range(1, 16)))

    def test_the_multiple_choice_rows_have_the_shape_of_the_legacy_review_rows(self):
        attempt_id = self.finish()
        row = self.load(attempt_id).questions[0]
        for key in ("id", "prompt", "difficulty", "visual_assets", "is_correct", "chosen_letter", "chosen_text",
                    "correct_text", "feedback_explanation", "options", "saved_slot_number", "kind", "points_possible",
                    "points_awarded"):
            self.assertIn(key, row)
        self.assertEqual((row["kind"], row["points_possible"], row["points_awarded"], row["is_correct"]),
                         ("multiple_choice", 1, 1, True))
        right = self.right_letter(row["id"])
        self.assertEqual(row["chosen_letter"], right)
        self.assertEqual(row["chosen_text"], row["correct_text"])
        self.assertEqual(row["feedback_explanation"], QUIZ_NO_EXPLANATION_MESSAGE)
        self.assertEqual(sorted(o["option_letter"] for o in row["options"]), [o["option_letter"] for o in self.options(row["id"])])

    def test_the_options_keep_the_attempts_seeded_order_and_the_stored_keys(self):
        attempt_id = self.finish()
        stored_seed = self.attempt(attempt_id)["seed"]
        result = self.load(attempt_id)
        expected = quiz_load_questions(self.conn, self.mc_ids(attempt_id), stored_seed, include_correct=True)
        self.assertEqual([[o["option_letter"] for o in q["options"]] for q in result.questions],
                         [[o["option_letter"] for o in q["options"]] for q in expected])

    def test_a_short_answer_row_shows_saved_answers_and_current_accepted_answers(self):
        attempt_id = self.finish()
        item = self.load(attempt_id).short_answers[0]
        question_id = item["id"]
        rows = self.answers(attempt_id)
        saved = {r["subquestion_number"]: r for r in rows if r["question_id"] == question_id}
        self.assertEqual({p["subquestion_number"] for p in item["parts"]}, set(saved))
        for part in item["parts"]:
            row = saved[part["subquestion_number"]]
            self.assertEqual((part["raw_answer"], part["is_correct"], part["points_possible"], part["points_awarded"]),
                             (row["raw_answer"], bool(row["is_correct"]), row["points_possible"], row["points_awarded"]))
            current = next(p for p in self.parts(question_id) if p["subquestion_number"] == part["subquestion_number"])
            self.assertEqual(part["accepted_answers"], list(dict.fromkeys(self.accepted(current))))
        self.assertEqual((item["kind"], item["points_possible"], item["points_awarded"]), ("short_answer", 3, 3))
        self.assertIn(item["grading_mode"], ("ordered", "order_independent"))

    def test_a_teacher_and_an_admin_load_their_own_attempt_only(self):
        for role in ("teacher", "admin"):
            with self.subTest(role=role):
                who = self.add_account(role)
                attempt_id = self.finish(who=who)
                self.assertTrue(self.load(attempt_id, who).loaded)
                self.assertEqual(self.load(attempt_id, self.student).outcome, part1.OUTCOME_NOT_FOUND)

    def test_a_foreign_and_a_missing_attempt_are_one_indistinguishable_not_found(self):
        attempt_id = self.finish()
        teacher, admin = self.add_account("teacher"), self.add_account("admin")
        absent = self.load(attempt_id + 1000)
        self.assertEqual(absent, part1.Part1Result(part1.OUTCOME_NOT_FOUND))
        for who in (self.other, teacher, admin):
            with self.subTest(role=who.role):
                self.assertEqual(self.load(attempt_id, who), absent)

    def test_an_open_grace_expired_and_abandoned_attempt_has_no_result(self):
        open_id = self.begin().attempt_id
        self.assertEqual(self.load(open_id), part1.Part1Result(part1.OUTCOME_NOT_FOUND))
        self.at(5400)  # grace: the form is gone, the attempt is still open
        self.assertEqual(self.load(open_id), part1.Part1Result(part1.OUTCOME_NOT_FOUND))
        self.at(5460)  # the stored cutoff, unmarked
        self.assertEqual(self.load(open_id), part1.Part1Result(part1.OUTCOME_NOT_FOUND))
        self.assertEqual(self.start().outcome, part1.OUTCOME_CREATED)  # marks the old one expired
        self.assertIsNotNone(self.attempt(open_id)["expired_at"])
        self.assertEqual(self.load(open_id), part1.Part1Result(part1.OUTCOME_NOT_FOUND))
        abandoned = self.insert_attempt(self.other, started=T0, abandoned_at=ts(T0))
        self.assertEqual(self.load(abandoned, self.other), part1.Part1Result(part1.OUTCOME_NOT_FOUND))
        owner = self.add_account()
        expired = self.insert_attempt(owner, started=T0, expired_at=ts(T0.replace(hour=12)))
        self.assertEqual(self.load(expired, owner), part1.Part1Result(part1.OUTCOME_NOT_FOUND))

    def test_a_non_submitted_row_is_not_found_whatever_else_it_stores(self):
        """The state is decided from the row first: a corrupt open, expired or abandoned row is not parsed."""
        for label, overrides in (("open", {}), ("abandoned", {"abandoned_at": ts(T0)}),
                                 ("expired", {"expired_at": ts(T0.replace(hour=12))})):
            with self.subTest(state=label):
                who = self.add_account()
                attempt_id = self.insert_attempt(who, ids="not json", slot_format="[]", planned=7, seed="", **overrides)
                self.assertEqual(self.load(attempt_id, who), part1.Part1Result(part1.OUTCOME_NOT_FOUND))

    def test_an_inactive_account_is_its_own_outcome(self):
        attempt_id = self.finish()
        self.sql("UPDATE users SET is_active = 0 WHERE id = ?", (self.student.user_id,))
        self.assertEqual(self.load(attempt_id), part1.Part1Result(part1.OUTCOME_ACCOUNT_UNAVAILABLE))
        self.assertEqual(self.load(attempt_id + 99), part1.Part1Result(part1.OUTCOME_ACCOUNT_UNAVAILABLE))
        self.assert_released()

    def test_ids_outside_the_integer_range_are_not_found_and_other_types_raise(self):
        self.finish()
        for value in (0, -1, 2 ** 63, 10 ** 30):
            with self.subTest(value=value):
                self.assertEqual(self.load(value).outcome, part1.OUTCOME_NOT_FOUND)
        for value in ("1", 1.0, True, None, b"1", [1]):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.load(value)
        self.assert_released()

    def test_only_an_account_principal_may_load(self):
        attempt_id = self.finish()
        for who in (ANONYMOUS, None, "student", self.student.user_id):
            with self.subTest(who=who), self.assertRaises(part1.Part1AccessError):
                part1.load_part1_result(self.conn, who, attempt_id)
        self.assertEqual(part1.load_part1_result(self.conn, account_principal(self.student.user_id, "student", "x"),
                                                 attempt_id).outcome, part1.OUTCOME_LOADED)

    def test_an_unconfigured_connection_is_refused_before_any_read(self):
        attempt_id = self.finish()
        plain = sqlite3.connect(self.db_path)
        self.addCleanup(plain.close)
        with self.assertRaises(RuntimeError):
            self.load(attempt_id, conn=plain)  # sqlite3 tuples, foreign keys off
        plain.row_factory = sqlite3.Row
        with self.assertRaises(RuntimeError):
            self.load(attempt_id, conn=plain)  # foreign keys still off
        self.assertFalse(plain.in_transaction)


# =========================================================================== no clock, blueprint or write

class NoWriteTest(ResultCase):
    def test_the_active_blueprint_the_grace_constant_and_the_clock_play_no_part(self):
        attempt_id = self.finish()
        baseline = self.load(attempt_id)
        self.sql("UPDATE dzi_blueprint_slots SET points = 9, task_kind = 'practical_web' WHERE exam_part = 1")
        self.sql("UPDATE dzi_blueprints SET is_active = 0")
        self.clock.set(T0.replace(year=2031))  # years past the stored cutoff
        clock_before = self.clock_row()
        with patch.object(part1, "SUBMIT_GRACE_SECONDS", 0), \
                patch.object(part1, "read_active_blueprint", side_effect=AssertionError("blueprint read")), \
                patch.object(part1, "compose_pools", side_effect=AssertionError("pool composed")), \
                patch.object(lifecycle, "write_transaction", side_effect=AssertionError("writer")), \
                patch.object(lifecycle, "read_clock", side_effect=AssertionError("clock read")), \
                patch.object(lifecycle, "read_snapshot", side_effect=AssertionError("lifecycle snapshot")), \
                patch.object(lifecycle, "wall_reading", side_effect=AssertionError("wall clock")):
            result = self.load(attempt_id)
        self.assertEqual(result, baseline)
        self.assertEqual(self.clock_row(), clock_before)

    def test_nothing_is_regraded_repaired_or_recomputed_from_a_current_key(self):
        attempt_id = self.finish()
        baseline = self.load(attempt_id)
        with patch.object(part1, "grade_quiz_text_answers", side_effect=AssertionError("regraded")), \
                patch.object(part1, "quiz_correct_mc_letters_by_id", side_effect=AssertionError("key read")), \
                patch.object(part1, "_grade_submission", side_effect=AssertionError("regraded")), \
                patch.object(part1, "_store_submission", side_effect=AssertionError("written")), \
                patch.object(part1, "draw_plan", side_effect=AssertionError("redrawn")):
            self.assertEqual(self.load(attempt_id), baseline)

    def test_no_table_changes_on_any_outcome(self):
        attempt_id = self.finish()
        open_id = self.begin(self.other).attempt_id
        before = self.state()
        for who, target in ((self.student, attempt_id), (self.student, attempt_id + 50), (self.other, attempt_id),
                            (self.other, open_id)):
            self.load(target, who)
        self.assertEqual(self.state(), before)
        self.sql("UPDATE users SET is_active = 0 WHERE id = ?", (self.other.user_id,))
        before = self.state()
        self.assertEqual(self.load(open_id, self.other).outcome, part1.OUTCOME_ACCOUNT_UNAVAILABLE)
        self.assertEqual(self.state(), before)

    def test_every_statement_is_a_read(self):
        attempt_id = self.finish()
        statements = []
        self.conn.set_trace_callback(statements.append)
        before_changes = self.conn.total_changes
        try:
            result = self.load(attempt_id)
        finally:
            self.conn.set_trace_callback(None)
        self.assertTrue(result.loaded)
        self.assertEqual(self.conn.total_changes, before_changes)
        kinds = {statement.split(None, 1)[0].upper() for statement in statements}
        self.assertLessEqual(kinds, {"BEGIN", "SELECT", "ROLLBACK", "PRAGMA"})
        self.assertIn("BEGIN", kinds)
        self.assertEqual(statements[-1].upper(), "ROLLBACK")
        self.assertFalse([s for s in statements if re.match(r"\s*(INSERT|UPDATE|DELETE|REPLACE|CREATE|DROP|ALTER)", s, re.I)])

    def test_a_query_only_connection_loads(self):
        attempt_id = self.finish()
        reader = web_db.connect_db(self.db_path, query_only=True)
        self.addCleanup(reader.close)
        before = self.state()
        self.assertEqual(self.load(attempt_id, conn=reader), self.load(attempt_id))
        self.assertFalse(reader.in_transaction)
        self.assertEqual(self.state(), before)

    def test_autocommit_and_isolation_level_none_connections_are_released(self):
        attempt_id = self.finish()
        for label, conn in (("autocommit", self.autocommit_connection()),
                            ("autocommit query_only", self.autocommit_connection(query_only=True))):
            with self.subTest(connection=label):
                self.assertTrue(self.load(attempt_id, conn=conn).loaded)
                self.assert_released(conn)
        legacy = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
        self.addCleanup(legacy.close)
        legacy.row_factory = sqlite3.Row
        legacy.execute("PRAGMA foreign_keys=ON")
        self.assertTrue(self.load(attempt_id, conn=legacy).loaded)
        self.assert_released(legacy)

    def test_every_outcome_leaves_no_transaction(self):
        attempt_id = self.finish()
        for target in (attempt_id, attempt_id + 9, 0):
            self.load(target)
            self.assert_released()
        self.sql("UPDATE users SET is_active = 0 WHERE id = ?", (self.student.user_id,))
        self.load(attempt_id)
        self.assert_released()

    def test_a_caller_transaction_is_reused_and_never_ended(self):
        attempt_id = self.finish()
        self.conn.execute("BEGIN")
        result = self.load(attempt_id)
        self.assertTrue(result.loaded)
        self.assertTrue(self.conn.in_transaction)
        with patch.object(part1, "_review_rows", side_effect=RuntimeError("boom")), self.assertRaises(RuntimeError):
            self.load(attempt_id)
        self.assertTrue(self.conn.in_transaction)  # still the caller's, on the error path too
        self.conn.rollback()
        self.assert_released()

    def test_a_caller_write_transaction_is_not_committed_or_rolled_back_by_the_read(self):
        attempt_id = self.finish()
        self.conn.execute("BEGIN IMMEDIATE")
        self.conn.execute("UPDATE users SET display_name = 'caller-pending' WHERE id = ?", (self.other.user_id,))
        self.assertTrue(self.load(attempt_id).loaded)
        self.assertTrue(self.conn.in_transaction)
        self.conn.rollback()
        with closing(self.raw()) as other:
            self.assertNotEqual(other.execute("SELECT display_name FROM users WHERE id = ?",
                                              (self.other.user_id,)).fetchone()[0], "caller-pending")

    def test_the_loaders_own_transaction_is_ended_with_explicit_sql_on_error(self):
        attempt_id = self.finish()
        for connection in (self.conn, self.autocommit_connection()):
            with patch.object(part1, "_review_slots", side_effect=sqlite3.OperationalError("injected")):
                with self.assertRaises(sqlite3.OperationalError):
                    self.load(attempt_id, conn=connection)
            self.assert_released(connection)
        with patch.object(part1, "_review_slots", side_effect=KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
            self.load(attempt_id)
        self.assert_released()


# =========================================================================== one snapshot

class SnapshotTest(ResultCase):
    """A real writer on its own connection commits between the loader's reads; the result stays one state."""

    def setUp(self):
        super().setUp()
        self.make_wal()
        self.attempt_id = self.finish()
        self.mc = self.mc_ids(self.attempt_id)
        self.sa = self.short_answer_ids(self.attempt_id)
        self.baseline = self.load(self.attempt_id)

    def load_with_midflight_change(self, statements):
        """Load; right after the loader's first content read, commit ``statements`` from another connection."""
        real = part1._review_slots
        committed = []

        def hook(conn, stored, saved):
            out = real(conn, stored, saved)
            for statement, params in statements:
                self.sql(statement, params)  # commits on a separate connection; raises if the loader held a writer lock
            committed.append(True)
            return out

        with patch.object(part1, "_review_slots", hook):
            result = self.load(self.attempt_id)
        self.assertTrue(committed)
        return result

    def mutations(self):
        first = self.mc[0]
        right, wrong = self.right_letter(first), self.wrong_letter(first)
        sa_item = self.sa[0]
        return [
            ("UPDATE questions SET prompt = 'ПРОМЕНЕН ПРОМПТ' WHERE id = ?", (first,)),
            ("UPDATE multiple_choice_options SET is_correct = CASE option_letter WHEN ? THEN 1 ELSE 0 END "
             "WHERE question_id = ?", (wrong, first)),
            ("UPDATE multiple_choice_options SET option_text = 'ПРОМЕНЕН ВАРИАНТ' WHERE question_id = ? AND option_letter = ?",
             (right, first)),
            ("UPDATE fill_in_subquestions SET correct_answer = ?, answer_alternatives = NULL WHERE question_id = ? "
             "AND subquestion_number = 1", (json.dumps(["друго-значение"]), sa_item)),
            ("DELETE FROM multiple_choice_options WHERE question_id = ?", (self.mc[1],)),
        ]

    def assert_snapshot_holds(self, statement, params=()):
        after = self.load_with_midflight_change([(statement, params)])
        self.assertEqual(after, self.baseline)
        self.assert_released()
        self.assertNotEqual(self.load(self.attempt_id), self.baseline)  # the change did commit: the next read sees it

    def test_a_changed_prompt_is_not_seen_mid_read(self):
        self.assert_snapshot_holds("UPDATE questions SET prompt = 'ПРОМЕНЕН ПРОМПТ' WHERE id = ?", (self.mc[0],))

    def test_a_changed_correct_key_is_not_seen_mid_read(self):
        first = self.mc[0]
        self.assert_snapshot_holds(
            "UPDATE multiple_choice_options SET is_correct = CASE option_letter WHEN ? THEN 1 ELSE 0 END WHERE question_id = ?",
            (self.wrong_letter(first), first))

    def test_a_changed_option_text_is_not_seen_mid_read(self):
        first = self.mc[0]
        self.assert_snapshot_holds("UPDATE multiple_choice_options SET option_text = 'ПРОМЕНЕН ВАРИАНТ' "
                                   "WHERE question_id = ? AND option_letter = ?", (first, self.right_letter(first)))

    def test_a_changed_accepted_answer_is_not_seen_mid_read(self):
        self.assert_snapshot_holds("UPDATE fill_in_subquestions SET correct_answer = ?, answer_alternatives = NULL "
                                   "WHERE question_id = ? AND subquestion_number = 1",
                                   (json.dumps(["друго-значение"]), self.sa[0]))

    def test_removed_options_are_not_seen_mid_read(self):
        self.assert_snapshot_holds("DELETE FROM multiple_choice_options WHERE question_id = ?", (self.mc[1],))

    def test_a_removed_part_is_not_seen_mid_read(self):
        item = next(q for q in self.sa if len(self.parts(q)) == 3)
        self.assert_snapshot_holds("DELETE FROM fill_in_subquestions WHERE question_id = ? AND subquestion_number = 3", (item,))

    def test_the_probe_sees_the_change_without_the_snapshot_so_the_test_is_sensitive(self):
        """Negative control: with the snapshot replaced by a pass-through, the same change shows up mid-load."""
        first = self.mc[0]
        passthrough = lambda conn, read: read()
        with patch.object(part1, "_in_read_snapshot", passthrough):
            after = self.load_with_midflight_change(
                [("UPDATE questions SET prompt = 'ПРОМЕНЕН ПРОМПТ' WHERE id = ?", (first,))])
        self.assertNotEqual(after, self.baseline)
        self.assertEqual(after.questions[0]["prompt"], "ПРОМЕНЕН ПРОМПТ")
        self.assertNotEqual(self.baseline.questions[0]["prompt"], "ПРОМЕНЕН ПРОМПТ")

    def test_the_owner_check_belongs_to_the_snapshot(self):
        """An account deactivated after the snapshot began does not change that read; the next read refuses."""
        after = self.load_with_midflight_change(
            [("UPDATE users SET is_active = 0 WHERE id = ?", (self.student.user_id,))])
        self.assertEqual(after, self.baseline)
        self.assertEqual(self.load(self.attempt_id).outcome, part1.OUTCOME_ACCOUNT_UNAVAILABLE)

    def test_a_writer_commit_is_not_blocked_by_the_read_and_the_next_read_sees_it(self):
        first = self.mc[0]
        after = self.load_with_midflight_change([("UPDATE questions SET prompt = 'НОВ ПРОМПТ' WHERE id = ?", (first,))])
        self.assertEqual(after, self.baseline)
        self.assertEqual(self.load(self.attempt_id).questions[0]["prompt"], "НОВ ПРОМПТ")


class RollbackJournalSnapshotTest(ResultCase):
    """The default (rollback-journal) mode: the one snapshot is a shared lock that is held for the read only."""

    def test_a_rollback_journal_database_holds_a_shared_lock_for_the_read_and_releases_it(self):
        attempt_id = self.finish()
        with closing(self.raw()) as conn:
            self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0], "delete")
        seen = []
        real = part1._review_slots

        def hook(conn, stored, saved):
            out = real(conn, stored, saved)
            probe = sqlite3.connect(self.db_path, timeout=0)
            try:
                try:
                    probe.execute("UPDATE users SET display_name = display_name WHERE id = ?", (self.student.user_id,))
                    probe.commit()
                    seen.append("writer-ok")
                except sqlite3.OperationalError as error:
                    seen.append(str(error))
            finally:
                probe.close()
            return out

        with patch.object(part1, "_review_slots", hook):
            self.assertTrue(self.load(attempt_id).loaded)
        self.assertEqual(seen, ["database is locked"])  # the reader's own shared lock, held for its one snapshot
        self.assert_released()




# =========================================================================== saved scoring

class ScoringTest(ResultCase):
    def oracle_totals(self, attempt_id):
        rows = self.answers(attempt_id)
        kind = lambda name, column: sum(r[column] for r in rows if r["kind"] == name)
        return (kind("multiple_choice", "points_awarded"), kind("multiple_choice", "points_possible"),
                kind("short_answer", "points_awarded"), kind("short_answer", "points_possible"))

    def test_the_canonical_45_point_result_and_a_zero_result(self):
        full = self.load(self.finish())
        self.assertEqual((full.score_points, full.score_max, full.max_points_planned), (45, 45, 45))
        wrong = self.add_account()
        attempt_id = self.finish(lambda a: {f"q_{q}": self.wrong_letter(q) for q in self.mc_ids(a)}, wrong)
        zero = self.load(attempt_id, wrong)
        self.assertEqual((zero.score_points, zero.score_max, zero.multiple_choice_points, zero.short_answer_points),
                         (0, 45, 0, 0))
        self.assertTrue(all(q["is_correct"] is False and q["points_awarded"] == 0 for q in zero.questions))

    def test_distinct_saved_subtotals_equal_the_saved_rows(self):
        def fields(attempt_id):
            out = {}
            for index, question_id in enumerate(self.mc_ids(attempt_id)):
                if index < 6:
                    out[f"q_{question_id}"] = self.right_letter(question_id)
                elif index < 9:
                    out[f"q_{question_id}"] = self.wrong_letter(question_id)
            for index, question_id in enumerate(self.short_answer_ids(attempt_id)):
                right = self.right_texts(question_id)
                for part in self.parts(question_id):
                    number = part["subquestion_number"]
                    out[f"a_{question_id}_{number}"] = right[number] if index < 4 else "не знам"
            return out

        attempt_id = self.finish(fields)
        result = self.load(attempt_id)
        mc_points, mc_max, sa_points, sa_max = self.oracle_totals(attempt_id)
        self.assertEqual((result.multiple_choice_points, result.multiple_choice_max, result.short_answer_points,
                          result.short_answer_max), (mc_points, mc_max, sa_points, sa_max))
        self.assertEqual((mc_points, mc_max), (6, 15))
        self.assertGreater(sa_points, 0)
        self.assertNotEqual(mc_points, sa_points)
        row = self.attempt(attempt_id)
        self.assertEqual((result.score_points, result.score_max), (row["score_points"], row["score_max"]))
        self.assertEqual(result.score_points, mc_points + sa_points)
        self.assertEqual(sum(q["points_awarded"] for q in result.questions + result.short_answers), result.score_points)

    def test_a_skipped_answer_is_a_saved_zero_row_with_its_possible_points(self):
        first = lambda a: {f"q_{self.mc_ids(a)[0]}": self.right_letter(self.mc_ids(a)[0])}
        result = self.load(self.finish(first))
        self.assertEqual((result.score_points, result.score_max), (1, 45))
        unanswered = [q for q in result.questions if q["chosen_letter"] is None]
        self.assertEqual(len(unanswered), 14)
        self.assertTrue(all(q["is_correct"] is False and q["points_possible"] == 1 and q["points_awarded"] == 0
                            for q in unanswered))
        self.assertTrue(all(p["raw_answer"] is None and p["points_awarded"] == 0
                            for item in result.short_answers for p in item["parts"]))
        self.assertEqual(sum(p["points_possible"] for item in result.short_answers for p in item["parts"]), 30)

    def test_a_one_plus_two_item_keeps_its_saved_part_weights(self):
        item = self.weighted_item()
        attempt_id = self.forced_attempt({16: item})
        heavy = next(r["subquestion_number"] for r in self.parts(item) if r["points"] == 2)
        self.assertEqual(self.submit({f"a_{item}_{heavy}": self.right_texts(item)[heavy]}, attempt_id).outcome, "submitted")
        result = self.load(attempt_id)
        row = next(q for q in result.short_answers if q["id"] == item)
        self.assertEqual(sorted((p["points_possible"], p["points_awarded"]) for p in row["parts"]), [(1, 0), (2, 2)])
        self.assertEqual((row["points_possible"], row["points_awarded"]), (3, 2))
        self.assertEqual((result.score_points, result.score_max), (2, 45))

    def test_an_effective_score_below_the_plan_keeps_the_saved_maximum_and_the_gap(self):
        dropped = {}
        attempt_id = self.finish(before_submit=lambda a: dropped.setdefault("id", self.drop_mc_slot(a, 2)))
        slot = self.slot_of(attempt_id, dropped["id"])
        self.assertEqual(slot, 3)
        result = self.load(attempt_id)
        self.assertEqual((result.score_points, result.score_max, result.max_points_planned), (44, 44, 45))
        self.assertTrue(result.below_planned_maximum)
        self.assertEqual((result.omitted_slot_numbers, result.unavailable_slot_numbers), ((3,), ()))
        self.assertNotIn(3, result.saved_slot_numbers)
        self.assertEqual([q["saved_slot_number"] for q in result.questions], [1, 2] + list(range(4, 16)))
        # even if the item is repaired afterwards, no answer row is invented for the skipped slot and the maximum stays
        rows_before = self.answers(attempt_id)
        self.restore_mc_key(dropped["id"], self.options(dropped["id"])[0]["option_letter"])
        again = self.load(attempt_id)
        self.assertEqual((again.score_max, again.omitted_slot_numbers), (44, (3,)))
        self.assertNotIn(dropped["id"], self.shown(again))
        self.assertEqual(self.answers(attempt_id), rows_before)

    def test_a_short_answer_dropped_before_the_submit_leaves_a_gap_in_the_short_answer_slots(self):
        def before(attempt_id):
            self.sql("UPDATE questions SET source_number = 27 WHERE id = ?", (self.short_answer_ids(attempt_id)[0],))

        attempt_id = self.finish(before_submit=before)
        result = self.load(attempt_id)
        self.assertEqual((result.score_points, result.score_max, result.omitted_slot_numbers), (42, 42, (16,)))
        self.assertEqual([q["saved_slot_number"] for q in result.short_answers], list(range(17, 26)))
        self.assertEqual(result.short_answer_max, 27)


# =========================================================================== content drift

class DriftTest(ResultCase):
    def setUp(self):
        super().setUp()
        self.attempt_id = self.finish()
        self.mc = self.mc_ids(self.attempt_id)
        self.sa = self.short_answer_ids(self.attempt_id)
        self.baseline = self.load(self.attempt_id)

    def saved_numbers(self, result):
        return (result.score_points, result.score_max, result.multiple_choice_points, result.short_answer_points,
                result.saved_slot_numbers)

    def test_a_changed_correct_key_changes_the_review_and_never_the_saved_verdict(self):
        question_id = self.mc[0]
        right, wrong = self.right_letter(question_id), self.wrong_letter(question_id)
        self.restore_mc_key(question_id, wrong)
        result = self.load(self.attempt_id)
        row = result.questions[0]
        wrong_text = next(r["option_text"] for r in self.rows(
            "SELECT option_letter, option_text FROM multiple_choice_options WHERE question_id = ?", (question_id,))
            if r["option_letter"] == wrong)
        self.assertEqual((row["chosen_letter"], row["is_correct"], row["points_awarded"]), (right, True, 1))
        self.assertEqual(row["correct_text"], wrong_text)  # the current key, shown beside a saved "correct" verdict
        self.assertNotEqual(row["chosen_text"], row["correct_text"])
        self.assertEqual(self.saved_numbers(result), self.saved_numbers(self.baseline))

    def test_a_changed_accepted_answer_changes_the_review_and_never_the_saved_verdict(self):
        question_id = self.sa[0]
        self.sql("UPDATE fill_in_subquestions SET correct_answer = ?, answer_alternatives = NULL WHERE question_id = ? "
                 "AND subquestion_number = 1", (json.dumps(["съвсем-ново"]), question_id))
        result = self.load(self.attempt_id)
        part = next(q for q in result.short_answers if q["id"] == question_id)["parts"][0]
        was = next(q for q in self.baseline.short_answers if q["id"] == question_id)["parts"][0]
        self.assertEqual(part["accepted_answers"], ["съвсем-ново"])
        self.assertEqual((part["raw_answer"], part["is_correct"], part["points_awarded"]),
                         (was["raw_answer"], True, was["points_awarded"]))
        self.assertNotEqual(part["raw_answer"], "съвсем-ново")
        self.assertEqual(self.saved_numbers(result), self.saved_numbers(self.baseline))

    def test_a_changed_prompt_and_option_text_are_current(self):
        question_id = self.mc[1]
        self.sql("UPDATE questions SET prompt = 'Нов текст на въпроса' WHERE id = ?", (question_id,))
        self.sql("UPDATE multiple_choice_options SET option_text = 'Нов текст на отговор' WHERE question_id = ? "
                 "AND option_letter = ?", (question_id, self.right_letter(question_id)))
        row = self.load(self.attempt_id).questions[1]
        self.assertEqual((row["prompt"], row["chosen_text"], row["correct_text"]),
                         ("Нов текст на въпроса", "Нов текст на отговор", "Нов текст на отговор"))

    def test_a_removed_chosen_key_keeps_the_saved_key_and_shows_no_text_for_it(self):
        question_id = self.mc[1]
        chosen = self.right_letter(question_id)
        self.sql("DELETE FROM multiple_choice_options WHERE question_id = ? AND option_letter = ?", (question_id, chosen))
        result = self.load(self.attempt_id)
        self.assertEqual(self.saved_numbers(result), self.saved_numbers(self.baseline))
        # the correct option is gone too, so the item is no longer presentable: counted, never regraded
        self.assertIn(2, result.unavailable_slot_numbers)

    def assert_unavailable(self, question_id, statement, params=()):
        """One change makes exactly its item unavailable, keeps every saved number and invents nothing."""
        self.sql(statement, params)
        result = self.load(self.attempt_id)
        slot = self.slot_of(self.attempt_id, question_id)
        self.assertEqual(result.unavailable_slot_numbers, (slot,))
        self.assertNotIn(slot, result.reviewed_slot_numbers)
        self.assertIn(slot, result.saved_slot_numbers)
        self.assertEqual(self.saved_numbers(result), self.saved_numbers(self.baseline))
        self.assertEqual(result.omitted_slot_numbers, ())
        self.assertNotIn(question_id, [q["id"] for q in result.questions + result.short_answers])
        self.assertEqual(len(result.reviewed_slot_numbers), 24)
        self.assertEqual(len(result.questions) + len(result.short_answers), 24)
        self.assertFalse(result.only_saved_score)

    def three_part_item(self):
        return next(q for q in self.sa if len(self.parts(q)) == 3)

    def test_removed_options_make_the_item_unavailable(self):
        self.assert_unavailable(self.mc[0], "DELETE FROM multiple_choice_options WHERE question_id = ?", (self.mc[0],))

    def test_no_correct_option_makes_the_item_unavailable(self):
        self.assert_unavailable(self.mc[1], "UPDATE multiple_choice_options SET is_correct = 0 WHERE question_id = ?",
                                (self.mc[1],))

    def test_a_changed_type_makes_the_item_unavailable(self):
        self.assert_unavailable(self.mc[2], "UPDATE questions SET question_type = 'fill_in' WHERE id = ?", (self.mc[2],))

    def test_a_removed_part_makes_the_item_unavailable(self):
        item = self.three_part_item()
        self.assert_unavailable(item, "DELETE FROM fill_in_subquestions WHERE question_id = ? AND subquestion_number = 3", (item,))

    def test_weights_that_no_longer_sum_make_the_item_unavailable(self):
        item = self.three_part_item()
        self.assert_unavailable(item, "UPDATE fill_in_subquestions SET points = 2 WHERE question_id = ? "
                                      "AND subquestion_number = 1", (item,))

    def test_a_renumbered_part_with_valid_weights_is_not_the_part_that_was_graded(self):
        """Weights still sum to 3 and the item is presentable, but part 3 is now part 4: the saved part has no match."""
        item = self.three_part_item()
        self.assert_unavailable(item, "UPDATE fill_in_subquestions SET subquestion_number = 4 WHERE question_id = ? "
                                      "AND subquestion_number = 3", (item,))

    def test_an_item_renumbered_into_part_two_is_unavailable(self):
        self.assert_unavailable(self.sa[0], "UPDATE questions SET source_number = 27 WHERE id = ?", (self.sa[0],))

    def test_changed_weights_that_still_sum_keep_the_item_and_show_the_saved_weights(self):
        item = self.weighted_item()
        attempt_id = self.forced_attempt({16: item}, who=self.add_account())
        owner = account_principal(self.attempt(attempt_id)["user_id"], "student", "x")
        light, heavy = (r["subquestion_number"] for r in sorted(self.parts(item), key=lambda r: r["points"]))
        self.assertEqual(self.submit({f"a_{item}_{heavy}": self.right_texts(item)[heavy]}, attempt_id, owner).outcome,
                         "submitted")
        self.sql("UPDATE fill_in_subquestions SET points = CASE subquestion_number WHEN ? THEN 2 ELSE 1 END "
                 "WHERE question_id = ?", (light, item))  # swapped: the sum is still 3
        result = self.load(attempt_id, owner)
        row = next(q for q in result.short_answers if q["id"] == item)
        self.assertEqual(sorted((p["subquestion_number"] == heavy, p["points_possible"], p["points_awarded"])
                                for p in row["parts"]), [(False, 1, 0), (True, 2, 2)])

    def test_the_blueprint_and_the_stimulus_list_play_no_part(self):
        self.sql("UPDATE dzi_blueprint_slots SET points = 9, task_kind = 'practical_web' WHERE exam_part = 1")
        self.sql("UPDATE dzi_blueprints SET is_active = 0, part1_minutes = 1")
        with patch.object(part1, "MISSING_STIMULUS_QUESTION_IDS", frozenset(self.plan_of(self.attempt_id))):
            result = self.load(self.attempt_id)
        self.assertEqual(result, self.baseline)

    def test_an_empty_review_still_returns_the_whole_saved_result(self):
        self.sql("UPDATE multiple_choice_options SET is_correct = 0")
        self.sql("UPDATE fill_in_subquestions SET correct_answer = '[]', answer_alternatives = NULL")
        result = self.load(self.attempt_id)
        self.assertTrue(result.loaded)
        self.assertTrue(result.only_saved_score)
        self.assertEqual((result.questions, result.short_answers, result.reviewed_slot_numbers), ((), (), ()))
        self.assertEqual(result.unavailable_slot_numbers, tuple(range(1, 26)))
        self.assertEqual(self.saved_numbers(result), self.saved_numbers(self.baseline))
        self.assertEqual((result.score_points, result.score_max), (45, 45))
        self.assertEqual(result.review_context()["questions"], [])
        self.assertEqual(self.render(result).strip(), "")

    def test_a_partly_removed_review_renders_only_what_remains(self):
        self.sql("UPDATE multiple_choice_options SET is_correct = 0 WHERE question_id IN (%s)" % ",".join(
            str(q) for q in self.mc[:5]))
        result = self.load(self.attempt_id)
        self.assertEqual(result.unavailable_slot_numbers, (1, 2, 3, 4, 5))
        self.assertEqual([q["saved_slot_number"] for q in result.questions], list(range(6, 16)))
        self.assertEqual(NUMBER.findall(self.render(result)), [str(n) for n in range(6, 16)])

    def render(self, result):
        return render_partial(**result.review_context())


# =========================================================================== content the per-item rule cannot interpret

class UninterpretableContentTest(ResultCase):
    """Current content a shared helper cannot read makes that one item unavailable; the saved result stays whole.

    The shared per-item rule converts flags with ``int(...)`` and reads the prompt as text, and raises on content
    that is not what the schema never prevented (no CHECK on those columns). That must cost one item, never the
    saved score, the saved verdicts or the other items.
    """

    VISUAL_PROMPT = "Разгледайте графиката по-долу"

    def setUp(self):
        super().setUp()
        self.attempt_id = self.finish()
        self.mc = self.mc_ids(self.attempt_id)
        self.sa = self.short_answer_ids(self.attempt_id)
        self.baseline = self.load(self.attempt_id)

    # ------------------------------------------------------------------ plumbing
    def verdicts(self, result):
        """The saved verdicts by original slot number, for every reviewed item."""
        rows = [(q["saved_slot_number"], q["chosen_letter"], q["is_correct"], q["points_possible"], q["points_awarded"])
                for q in result.questions]
        rows += [(q["saved_slot_number"], tuple((p["subquestion_number"], p["raw_answer"], p["is_correct"],
                                                 p["points_possible"], p["points_awarded"]) for p in q["parts"]))
                 for q in result.short_answers]
        return sorted(rows, key=lambda row: row[0])

    def saved_numbers(self, result):
        return (result.score_points, result.score_max, result.multiple_choice_points, result.multiple_choice_max,
                result.short_answer_points, result.short_answer_max, result.saved_slot_numbers,
                result.omitted_slot_numbers, result.grader_version)

    def hostile_option(self, question_id, value, *, right=False):
        self.sql("UPDATE multiple_choice_options SET is_correct = ? WHERE id = (SELECT id FROM multiple_choice_options "
                 "WHERE question_id = ? AND is_correct = ? ORDER BY option_letter LIMIT 1)",
                 (value, question_id, 1 if right else 0))

    def hostile_visual(self, question_id, value):
        """A prompt that needs a visual, no path and no asset: the flag is the only thing left to read."""
        self.sql("UPDATE questions SET prompt = ?, image_path = NULL, has_image = ? WHERE id = ?",
                 (self.VISUAL_PROMPT, value, question_id))
        self.sql("DELETE FROM asset_links WHERE owner_type IN ('question', 'questions') AND owner_id = ?", (question_id,))
        self.sql("DELETE FROM asset_links WHERE owner_type = 'exam_task' AND owner_id IN "
                 "(SELECT task_id FROM exam_task_questions WHERE question_id = ?)", (question_id,))

    def assert_only_these_unavailable(self, slots):
        """The saved result is whole, exactly ``slots`` are counted unavailable and every other item is reviewed as
        saved, with its original slot number, in the data and in the shared partial; no stored row is touched."""
        state, rows, attempt = self.state(), self.answers(self.attempt_id), self.attempt(self.attempt_id)
        result = self.load(self.attempt_id)
        slots = tuple(sorted(slots))
        self.assertTrue(result.loaded)
        self.assertEqual(self.saved_numbers(result), self.saved_numbers(self.baseline))
        self.assertEqual((result.score_points, result.score_max, result.multiple_choice_points, result.short_answer_points),
                         (45, 45, 15, 30))
        self.assertEqual(result.unavailable_slot_numbers, slots)
        self.assertEqual(result.reviewed_slot_numbers, tuple(n for n in range(1, 26) if n not in slots))
        self.assertEqual(self.verdicts(result), [row for row in self.verdicts(self.baseline) if row[0] not in slots])
        self.assertEqual([q["saved_slot_number"] for q in result.questions], [n for n in range(1, 16) if n not in slots])
        self.assertEqual([q["saved_slot_number"] for q in result.short_answers], [n for n in range(16, 26) if n not in slots])
        self.assertEqual(NUMBER.findall(render_partial(**result.review_context())),
                         [str(n) for n in range(1, 16) if n not in slots])
        self.assertFalse(result.only_saved_score)
        self.assertEqual((self.state(), self.answers(self.attempt_id), self.attempt(self.attempt_id)), (state, rows, attempt))
        self.assert_released()
        return result

    # ------------------------------------------------------------------ the reproduced findings
    def test_a_non_numeric_option_flag_makes_only_its_item_unavailable(self):
        question_id = self.mc[3]  # slot 4
        key = self.right_letter(question_id)
        for label, value, on_the_key in (("text", "true", False), ("spaces", "  ", False), ("other text", "не", False),
                                         ("infinity", 9e999, False), ("the key itself", "true", True)):
            with self.subTest(value=label):
                self.hostile_option(question_id, value, right=on_the_key)
                self.assert_only_these_unavailable((4,))
                self.restore_mc_key(question_id, key)
                self.assert_only_these_unavailable(())

    def test_a_non_numeric_image_flag_on_an_item_that_needs_a_visual_makes_only_it_unavailable(self):
        question_id = self.mc[6]  # slot 7
        self.hostile_visual(question_id, 1)  # control: the same item with a plain flag is unavailable without any raise
        self.assert_only_these_unavailable((7,))
        for label, value in (("text", "true"), ("spaces", "  "), ("infinity", 9e999)):
            with self.subTest(value=label):
                self.sql("UPDATE questions SET has_image = ? WHERE id = ?", (value, question_id))
                self.assert_only_these_unavailable((7,))

    def test_a_prompt_that_is_not_text_makes_only_its_item_unavailable(self):
        for label, question_id, slot in (("multiple choice", self.mc[8], 9), ("short answer", self.sa[3], 19)):
            with self.subTest(kind=label):
                prompt = self.rows("SELECT prompt FROM questions WHERE id = ?", (question_id,))[0]["prompt"]
                self.sql("UPDATE questions SET prompt = x'ff00fe' WHERE id = ?", (question_id,))
                self.assert_only_these_unavailable((slot,))
                self.sql("UPDATE questions SET prompt = ? WHERE id = ?", (prompt, question_id))
                self.assert_only_these_unavailable(())

    def test_several_uninterpretable_items_are_each_unavailable_and_the_rest_reviewed(self):
        self.hostile_option(self.mc[1], "true")  # slot 2
        self.hostile_visual(self.mc[10], "true")  # slot 11
        self.sql("UPDATE questions SET prompt = x'00' WHERE id = ?", (self.sa[0],))  # slot 16
        self.sql("UPDATE questions SET prompt = x'ff' WHERE id = ?", (self.sa[9],))  # slot 25
        self.assert_only_these_unavailable((2, 11, 16, 25))

    def test_an_item_that_cannot_be_interpreted_beside_one_that_is_merely_unavailable(self):
        self.sql("DELETE FROM multiple_choice_options WHERE question_id = ?", (self.mc[0],))  # slot 1: removed
        self.hostile_option(self.mc[4], "true")  # slot 5: cannot be read
        self.assert_only_these_unavailable((1, 5))

    # ------------------------------------------------------------------ bounded behaviour
    def test_the_normal_path_judges_the_saved_slots_in_one_batch_and_only_a_failure_judges_them_singly(self):
        with patch.object(part1, "_presentable_slots", wraps=part1._presentable_slots) as judged:
            self.assertTrue(self.load(self.attempt_id).loaded)
        self.assertEqual(judged.call_count, 1)
        self.assertEqual(len(judged.call_args.args[1].question_ids), 25)
        self.hostile_option(self.mc[3], "true")
        with patch.object(part1, "_presentable_slots", wraps=part1._presentable_slots) as judged:
            self.assert_only_these_unavailable((4,))
        self.assertEqual(judged.call_count, 1 + 25)  # the failed batch, then each saved slot alone

    def test_an_unsaved_planned_item_is_not_read_at_all(self):
        """A planned item that was never graded has no saved row; hostile content on it cannot matter."""
        dropped, owner = {}, self.add_account()
        attempt_id = self.finish(who=owner, before_submit=lambda a: dropped.setdefault("id", self.drop_mc_slot(a, 2)))
        self.hostile_option(dropped["id"], "true")
        with patch.object(part1, "_presentable_slots", wraps=part1._presentable_slots) as judged:
            result = self.load(attempt_id, owner)
        self.assertEqual(judged.call_count, 1)
        self.assertEqual(len(judged.call_args.args[1].question_ids), 24)
        self.assertTrue(result.loaded)
        self.assertEqual((result.score_points, result.score_max, result.omitted_slot_numbers, result.unavailable_slot_numbers),
                         (44, 44, (3,), ()))
        self.assertEqual(result.reviewed_slot_numbers, tuple(n for n in range(1, 26) if n != 3))

    def test_a_database_error_and_any_other_error_still_propagate(self):
        for error in (sqlite3.OperationalError("database is locked"), sqlite3.DatabaseError("malformed"),
                      RuntimeError("unexpected"), KeyError("slot"), AttributeError("x"), ZeroDivisionError()):
            with self.subTest(error=type(error).__name__):
                with patch.object(part1, "_presentable_slots", side_effect=error), self.assertRaises(type(error)):
                    self.load(self.attempt_id)
                self.assert_released()

    def test_a_database_error_while_judging_one_item_is_not_absorbed_with_the_value_errors(self):
        calls = []

        def judge(conn, stored):
            calls.append(len(stored.question_ids))
            if stored.question_ids == (self.mc[2],):  # slot 3, judged alone after the batch failed
                raise sqlite3.OperationalError("database is locked")
            raise ValueError("not a number")  # the batch, and the first two single items

        with patch.object(part1, "_presentable_slots", side_effect=judge), self.assertRaises(sqlite3.OperationalError):
            self.load(self.attempt_id)
        self.assertEqual(calls, [25, 1, 1, 1])
        self.assert_released()

    def test_an_uninterpretable_item_does_not_hide_a_corrupt_saved_result(self):
        self.hostile_option(self.mc[3], "true")
        self.change("UPDATE dzi_part1_attempts SET score_points = 44 WHERE id = ?", (self.attempt_id,))
        with self.assertRaises(part1.Part1StateError):
            self.load(self.attempt_id)
        self.assert_released()


# =========================================================================== corrupt stored state

class CorruptStateTest(ResultCase):
    """Every damaged saved row raises, writes nothing, repairs nothing and leaves no transaction."""

    def tampers(self):
        sa_part_one = "SELECT MIN(rowid) FROM dzi_part1_answers WHERE attempt_id = {a} AND kind = 'short_answer'"
        return [
            # ---- the attempt row
            ("plan is not JSON", "UPDATE dzi_part1_attempts SET question_ids_json = '[1,2' WHERE id = {a}", ()),
            ("plan has 24 ids", "UPDATE dzi_part1_attempts SET question_ids_json = (SELECT json_remove(question_ids_json, '$[24]')"
             " FROM dzi_part1_attempts WHERE id = {a}) WHERE id = {a}", ()),
            ("plan repeats an id", "UPDATE dzi_part1_attempts SET question_ids_json = (SELECT json_set(question_ids_json, '$[1]', "
             "json_extract(question_ids_json, '$[0]')) FROM dzi_part1_attempts WHERE id = {a}) WHERE id = {a}", ()),
            ("slot format is not an array", "UPDATE dzi_part1_attempts SET slot_format_json = '{{}}' WHERE id = {a}", ()),
            ("planned maximum is not the slot points", "UPDATE dzi_part1_attempts SET max_points_planned = 44 WHERE id = {a}", ()),
            ("seed is empty", "UPDATE dzi_part1_attempts SET seed = '' WHERE id = {a}", ()),
            ("instants out of order", "UPDATE dzi_part1_attempts SET deadline_at = started_at WHERE id = {a}", ()),
            ("duration differs from the instants", "UPDATE dzi_part1_attempts SET duration_seconds = 5399 WHERE id = {a}", ()),
            ("score below the rows", "UPDATE dzi_part1_attempts SET score_points = 44 WHERE id = {a}", ()),
            ("maximum below the rows", "UPDATE dzi_part1_attempts SET score_max = 44 WHERE id = {a}", ()),
            ("maximum above the plan", "UPDATE dzi_part1_attempts SET score_max = 46 WHERE id = {a}", ()),
            ("negative score", "UPDATE dzi_part1_attempts SET score_points = -1 WHERE id = {a}", ()),
            ("score above the maximum", "UPDATE dzi_part1_attempts SET score_points = 46 WHERE id = {a}", ()),
            ("score is NULL", "UPDATE dzi_part1_attempts SET score_points = NULL WHERE id = {a}", ()),
            ("score is real", "UPDATE dzi_part1_attempts SET score_points = 44.5 WHERE id = {a}", ()),
            ("score is a blob", "UPDATE dzi_part1_attempts SET score_max = x'3435' WHERE id = {a}", ()),
            ("submission time unreadable", "UPDATE dzi_part1_attempts SET submitted_at = 'garbage' WHERE id = {a}", ()),
            ("submitted before the start", "UPDATE dzi_part1_attempts SET submitted_at = '2000-01-01 00:00:00' WHERE id = {a}", ()),
            ("submitted at the cutoff", "UPDATE dzi_part1_attempts SET submitted_at = submit_cutoff_at WHERE id = {a}", ()),
            ("also expired", "UPDATE dzi_part1_attempts SET expired_at = '2030-01-01 00:00:00' WHERE id = {a}", ()),
            ("also abandoned", "UPDATE dzi_part1_attempts SET abandoned_at = '2030-01-01 00:00:00' WHERE id = {a}", ()),
            # ---- the answer rows
            ("no answer rows", "DELETE FROM dzi_part1_answers WHERE attempt_id = {a}", ()),
            ("a slot outside the plan", "UPDATE dzi_part1_answers SET slot_number = 26 WHERE attempt_id = {a} AND slot_number = 1", ()),
            ("slot zero", "UPDATE dzi_part1_answers SET slot_number = 0 WHERE attempt_id = {a} AND slot_number = 1", ()),
            ("a question that is not in the plan", "UPDATE dzi_part1_answers SET question_id = 99999999 "
             "WHERE attempt_id = {a} AND slot_number = 1", ()),
            ("a kind other than the slot's", "UPDATE dzi_part1_answers SET kind = 'short_answer' WHERE attempt_id = {a} AND slot_number = 1", ()),
            ("a multiple-choice part number", "UPDATE dzi_part1_answers SET subquestion_number = 1 WHERE attempt_id = {a} AND slot_number = 1", ()),
            ("a short-answer part zero", "UPDATE dzi_part1_answers SET subquestion_number = 0 WHERE rowid = (" + sa_part_one + ")", ()),
            ("a short-answer part with a key", "UPDATE dzi_part1_answers SET chosen_letter = 'А' WHERE rowid = (" + sa_part_one + ")", ()),
            ("a multiple-choice row with text", "UPDATE dzi_part1_answers SET raw_answer = 'x' WHERE attempt_id = {a} AND slot_number = 1", ()),
            ("a verdict of 2", "UPDATE dzi_part1_answers SET is_correct = 2 WHERE attempt_id = {a} AND slot_number = 1", ()),
            ("a verdict that is real", "UPDATE dzi_part1_answers SET is_correct = 1.5 WHERE attempt_id = {a} AND slot_number = 1", ()),
            ("an award that is not all or nothing", "UPDATE dzi_part1_answers SET points_awarded = 0 WHERE attempt_id = {a} AND slot_number = 1", ()),
            ("correct without a chosen key", "UPDATE dzi_part1_answers SET chosen_letter = NULL WHERE attempt_id = {a} AND slot_number = 1", ()),
            ("a chosen key that is a blob", "UPDATE dzi_part1_answers SET chosen_letter = x'41' WHERE attempt_id = {a} AND slot_number = 1", ()),
            ("a short-answer text that is a blob", "UPDATE dzi_part1_answers SET raw_answer = x'41' WHERE rowid = (" + sa_part_one + ")", ()),
            ("correct for an unanswered part", "UPDATE dzi_part1_answers SET raw_answer = NULL WHERE rowid = (" + sa_part_one + ")", ()),
            ("correct for a part stored as an empty text", "UPDATE dzi_part1_answers SET raw_answer = ? WHERE rowid = (" + sa_part_one + ")", ('',)),
            ("correct for a part stored as spaces", "UPDATE dzi_part1_answers SET raw_answer = ? WHERE rowid = (" + sa_part_one + ")", ('   ',)),
            ("correct for a part stored as a space and a tab", "UPDATE dzi_part1_answers SET raw_answer = ? WHERE rowid = (" + sa_part_one + ")", (' \t',)),
            ("correct for a part stored as a no-break space", "UPDATE dzi_part1_answers SET raw_answer = ? WHERE rowid = (" + sa_part_one + ")", ('\xa0',)),
            ("a multiple-choice row not worth the slot", "UPDATE dzi_part1_answers SET points_possible = 2, points_awarded = 2 "
             "WHERE attempt_id = {a} AND slot_number = 1", ()),
            ("short-answer weights that do not sum", "UPDATE dzi_part1_answers SET points_possible = points_possible + 1, "
             "points_awarded = points_awarded + 1 WHERE rowid = (" + sa_part_one + ")", ()),
            ("zero possible points", "UPDATE dzi_part1_answers SET points_possible = 0, points_awarded = 0 "
             "WHERE attempt_id = {a} AND slot_number = 1", ()),
            ("an empty grader version", "UPDATE dzi_part1_answers SET grader_version = '' WHERE attempt_id = {a} AND slot_number = 1", ()),
            ("two grader versions", "UPDATE dzi_part1_answers SET grader_version = 'dzi_part1_v2' WHERE attempt_id = {a} AND slot_number = 1", ()),
            ("a second multiple-choice row", "INSERT INTO dzi_part1_answers (attempt_id, slot_number, subquestion_number, "
             "question_id, kind, chosen_letter, raw_answer, is_correct, points_possible, points_awarded, grader_version) "
             "SELECT attempt_id, slot_number, 1, question_id, kind, chosen_letter, raw_answer, is_correct, points_possible, "
             "points_awarded, grader_version FROM dzi_part1_answers WHERE attempt_id = {a} AND slot_number = 1", ()),
            ("a missing short-answer part", "DELETE FROM dzi_part1_answers WHERE rowid = (" + sa_part_one + ")", ()),
        ]

    def test_every_tamper_raises_without_a_write_or_a_repair_or_a_transaction(self):
        for label, statement, params in self.tampers():
            with self.subTest(tamper=label):
                who = self.add_account()
                attempt_id = self.finish(who=who)
                self.change(statement.format(a=attempt_id), params, check_constraints=False)
                before = self.state()
                with self.assertRaises(part1.Part1StateError) as caught:
                    self.load(attempt_id, who)
                self.assertIn(str(attempt_id), str(caught.exception))
                self.assertEqual(self.state(), before)
                self.assert_released()

    def test_a_maximum_below_the_saved_rows_fails_even_when_the_score_still_fits_under_it(self):
        """A low score keeps ``points <= max`` true, so only the comparison with the saved possible points sees it."""
        who = self.add_account()
        first = lambda a: {f"q_{self.mc_ids(a)[0]}": self.right_letter(self.mc_ids(a)[0])}
        attempt_id = self.finish(first, who)
        self.assertEqual((self.attempt(attempt_id)["score_points"], self.attempt(attempt_id)["score_max"]), (1, 45))
        self.change("UPDATE dzi_part1_attempts SET score_max = 44 WHERE id = ?", (attempt_id,))
        with self.assertRaisesRegex(part1.Part1StateError, "possible points differ from the stored maximum"):
            self.load(attempt_id, who)

    def test_weights_moved_between_two_slots_keep_every_total_and_still_fail(self):
        """One saved part gains a point and another loses one: the score, the maximum and the planned maximum are
        all unchanged, so only the per-slot sum against the saved slot points can see it."""
        who = self.add_account()
        attempt_id = self.finish(who=who)
        parts = [r for r in self.answers(attempt_id) if r["kind"] == "short_answer"]
        heavy = next(r for r in parts if r["points_possible"] >= 2)
        light = next(r for r in parts if r["slot_number"] != heavy["slot_number"])
        for row, delta in ((heavy, -1), (light, 1)):
            self.change("UPDATE dzi_part1_answers SET points_possible = points_possible + ?, points_awarded = points_awarded + ? "
                        "WHERE attempt_id = ? AND slot_number = ? AND subquestion_number = ?",
                        (delta, delta, attempt_id, row["slot_number"], row["subquestion_number"]), check_constraints=False)
        attempt = self.attempt(attempt_id)
        self.assertEqual((attempt["score_points"], attempt["score_max"]), (45, 45))
        self.assertEqual(sum(r["points_possible"] for r in self.answers(attempt_id)), 45)
        with self.assertRaisesRegex(part1.Part1StateError, "weights that do not sum to the slot's saved points"):
            self.load(attempt_id, who)

    def test_a_plan_defect_is_the_plan_error_and_a_result_defect_is_the_state_error(self):
        who = self.add_account()
        attempt_id = self.finish(who=who)
        self.change("UPDATE dzi_part1_attempts SET question_ids_json = '[1,2' WHERE id = ?", (attempt_id,))
        with self.assertRaises(part1.Part1PlanError):
            self.load(attempt_id, who)
        other = self.add_account()
        second = self.finish(who=other)
        self.change("UPDATE dzi_part1_attempts SET score_points = 44 WHERE id = ?", (second,))
        with self.assertRaises(part1.Part1StateError) as caught:
            self.load(second, other)
        self.assertNotIsInstance(caught.exception, part1.Part1PlanError)
        self.assertIn("inconsistent result", str(caught.exception))

    def test_the_tamper_of_one_attempt_never_affects_another_accounts_result(self):
        broken_owner, fine_owner = self.add_account(), self.add_account()
        broken, fine = self.finish(who=broken_owner), self.finish(who=fine_owner)
        self.change("DELETE FROM dzi_part1_answers WHERE attempt_id = ?", (broken,))
        self.assertTrue(self.load(fine, fine_owner).loaded)
        with self.assertRaises(part1.Part1StateError):
            self.load(broken, broken_owner)

    def test_a_corrupt_result_is_not_the_stale_message_and_is_not_hidden_behind_an_empty_review(self):
        attempt_id = self.finish()
        self.change("UPDATE dzi_part1_attempts SET score_points = 44 WHERE id = ?", (attempt_id,), check_constraints=False)
        self.sql("UPDATE multiple_choice_options SET is_correct = 0")  # nothing could be reviewed either
        with self.assertRaises(part1.Part1StateError):
            self.load(attempt_id)


    def test_a_correct_verdict_on_a_blank_short_answer_is_refused_with_its_own_reason(self):
        """The submit stores a blank part as NULL and always wrong, so a correct row whose text is empty, spaces, a
        space and a tab, a no-break space (or NULL) is not something it wrote."""
        for blank in ("", "   ", " \t", "\xa0", None):
            with self.subTest(blank=blank):
                who = self.add_account()
                attempt_id = self.finish(who=who)
                self.change("UPDATE dzi_part1_answers SET raw_answer = ? WHERE attempt_id = ? AND slot_number = 16 "
                            "AND subquestion_number = 1", (blank, attempt_id), check_constraints=False)
                before = self.state()
                with self.assertRaisesRegex(part1.Part1StateError, "a correct verdict for an unanswered part") as caught:
                    self.load(attempt_id, who)
                self.assertIn(str(attempt_id), str(caught.exception))
                self.assertEqual(self.state(), before)
                self.assert_released()

    def test_a_wrong_verdict_on_an_unanswered_short_answer_still_loads_as_saved(self):
        def skip_slot_16(attempt_id):
            skipped = f"a_{self.plan_of(attempt_id)[15]}_"
            return {key: value for key, value in self.right_fields(attempt_id).items() if not key.startswith(skipped)}

        attempt_id = self.finish(skip_slot_16)
        saved = [row for row in self.answers(attempt_id) if row["slot_number"] == 16]
        self.assertTrue(saved and all(row["raw_answer"] is None and row["is_correct"] == 0 for row in saved))
        before = self.state()
        result = self.load(attempt_id)
        self.assertTrue(result.loaded)
        self.assertEqual((result.score_points, result.score_max, result.short_answer_points, result.short_answer_max),
                         (42, 45, 27, 30))
        self.assertEqual(result.reviewed_slot_numbers, tuple(range(1, 26)))
        item = next(q for q in result.short_answers if q["saved_slot_number"] == 16)
        self.assertEqual([(p["raw_answer"], p["is_correct"], p["points_awarded"]) for p in item["parts"]],
                         [(None, False, 0)] * len(saved))
        self.assertEqual((item["points_possible"], item["points_awarded"]), (3, 0))
        self.assertEqual(self.state(), before)
        self.assert_released()


# =========================================================================== saved slot numbers

class SlotNumberTest(ResultCase):
    def test_a_result_with_a_gap_renders_the_original_saved_slot_numbers(self):
        dropped = {}
        attempt_id = self.finish(before_submit=lambda a: dropped.setdefault("id", self.drop_mc_slot(a, 2)))
        result = self.load(attempt_id)
        html = render_partial(**result.review_context())
        self.assertEqual(NUMBER.findall(html), [str(n) for n in [1, 2] + list(range(4, 16))])
        self.assertEqual(html.count("<article"), 14)

    def test_the_same_rows_without_the_saved_number_are_numbered_one_to_n(self):
        attempt_id = self.finish(before_submit=lambda a: self.drop_mc_slot(a, 2))
        context = self.load(attempt_id).review_context()
        context["questions"] = [{k: v for k, v in row.items() if k != "saved_slot_number"} for row in context["questions"]]
        self.assertEqual(NUMBER.findall(render_partial(**context)), [str(n) for n in range(1, 15)])

    def test_a_first_slot_gap_and_a_two_digit_slot_keep_their_numbers(self):
        attempt_id = self.finish(before_submit=lambda a: (self.drop_mc_slot(a, 0), self.drop_mc_slot(a, 14)))
        numbers = NUMBER.findall(render_partial(**self.load(attempt_id).review_context()))
        self.assertEqual(numbers, [str(n) for n in range(2, 15)])  # slots 1 and 15 were never graded

    def test_the_saved_verdict_and_chosen_text_render_in_the_numbered_card(self):
        def fields(attempt_id):
            first, second = self.mc_ids(attempt_id)[:2]
            return {f"q_{first}": self.right_letter(first), f"q_{second}": self.wrong_letter(second)}

        attempt_id = self.finish(fields)
        html = render_partial(**self.load(attempt_id).review_context())
        cards = re.findall(r'<article class="card question-card (correct|incorrect)">', html)
        self.assertEqual(cards[:3], ["correct", "incorrect", "incorrect"])  # saved verdicts: right, wrong, unanswered
        self.assertIn("Няма избран отговор.", html)
        self.assertIn("твоят отговор", html)


# =========================================================================== dormancy

class DormancyTest(unittest.TestCase):
    def test_nothing_imports_the_service_and_no_simulation_route_exists(self):
        pattern = re.compile(r"(from\s+web\.services\.dzi_part1|from\s+web\.services\s+import[^\n]*\bdzi_part1\b"
                             r"|import\s+web\.services\.dzi_part1|from\s+\.dzi_part1|from\s+\.\s+import[^\n]*dzi_part1)")
        offenders = [str(path.relative_to(ROOT)) for base in ("web", "src") for path in (ROOT / base).rglob("*.py")
                     if path.name != "dzi_part1.py" and pattern.search(path.read_text(encoding="utf-8"))]
        self.assertEqual(offenders, [])
        self.assertFalse([rule.rule for rule in web_app.app.url_map.iter_rules() if "simulation" in rule.rule])

    def test_nothing_but_the_loader_sets_the_saved_slot_number_and_the_partial_alone_reads_it(self):
        users = [str(path.relative_to(ROOT)) for path in (ROOT / "web").rglob("*")
                 if path.is_file() and path.suffix in (".py", ".html") and "saved_slot_number" in path.read_text(encoding="utf-8")]
        self.assertEqual(sorted(users), ["web/services/dzi_part1.py", "web/templates/_multiple_choice_result_review.html"])


if __name__ == "__main__":
    unittest.main()
