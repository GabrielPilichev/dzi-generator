"""The read contract of account-owned practice progress (C2, roadmap PR #373): ``practice.read_practice_progress``.

Every database is a temporary clone of the migrations + canonical template with synthetic curriculum rows on
top (``tests.test_practice_start_service.PracticeCase``). Nothing here reads, copies or hashes
``data/questions.db``. Sessions are stored the way a submission leaves them, with explicit saved scores and
timestamps, so every number asserted is a stored number: the service never grades, never reads an answer and
never looks at a question.

What is proved, and how:

* the counting rules: zero, open, abandoned and submitted sessions; every submitted repeat; the latest by the
  stored UTC submission time with the larger id for the same second, whatever order the rows were inserted in;
* the exact scope: a section is not its topics, a topic is not its section, NULL topic is the section-wide
  practice only, another section or topic is another scope;
* the owner: the id comes from the principal only; another account's rows never move a number, roles included;
  an inactive, missing or unknown-role account has no progress;
* the scope decision shared with a start: invalid, DZI, subject-less and non-member scopes are ``None``;
* saved scores survive content changes (no content is read at all);
* it only reads: a ``query_only`` connection works, no row changes, a caller's transaction is reused and left
  open, one snapshot holds the scope, the account, the count and the latest score together under a concurrent
  commit (with a negative control that does diverge), and the lookup is the scoped index.
"""

import inspect
import sqlite3
import unittest
from contextlib import closing

from tests.test_practice_start_service import STAMP, PracticeCase, Scope  # noqa: F401  (fixtures only)
from web import db as web_db
from web.access import ANONYMOUS, Principal, account_principal
from web.services import practice

EARLIER = "2026-01-01 09:59:59"
LATER = "2026-01-01 10:00:01"


class ProgressCase(PracticeCase):
    def add_submitted(self, who, scope, score=(1, 2), *, topic_index=None, submitted=STAMP, commit=True):
        """A submitted session as a submission leaves it: its stored score and UTC time, answers elsewhere."""
        topic_id = None if topic_index is None else scope.topic_ids[topic_index]
        return self.add_session(who, scope, topic_id, submitted=submitted, score=score, commit=commit)

    def add_session(self, who, scope, topic_id=None, *, submitted=None, abandoned=None, score=None, commit=True):
        correct, total = score if score else (None, None)
        cursor = self.conn.execute(
            "INSERT INTO practice_sessions (user_id, section_id, topic_id, seed, question_ids_json, started_at, "
            "submitted_at, abandoned_at, score_correct, score_total) VALUES (?, ?, ?, 's', '[]', ?, ?, ?, ?, ?)",
            (who.user_id, scope.section_id, topic_id, STAMP, submitted, abandoned, correct, total))
        if commit:
            self.conn.commit()
        return int(cursor.lastrowid)

    def progress(self, who, scope, topic_index=None, conn=None):
        topic_id = None if topic_index is None else scope.topic_ids[topic_index]
        return practice.read_practice_progress(conn or self.conn, who, scope.section_id, topic_id)

    def expect(self, scope, count, correct=None, total=None, topic_index=None):
        topic_id = None if topic_index is None else scope.topic_ids[topic_index]
        return practice.PracticeProgress(scope.section_id, topic_id, count, correct, total)


class CountingTest(ProgressCase):
    def test_a_scope_nobody_practised_has_zero_progress(self):
        scope = self.make_scope(6, topics=2)
        for index in (None, 0, 1):
            with self.subTest(topic=index):
                self.assertEqual(self.progress(self.student, scope, index), self.expect(scope, 0, topic_index=index))

    def test_open_and_abandoned_sessions_are_never_counted_or_scored(self):
        scope = self.make_scope(6)
        self.add_session(self.student, scope)                              # open
        self.add_session(self.student, scope, abandoned="2026-01-01 11:00:00")
        self.assertEqual(self.progress(self.student, scope), self.expect(scope, 0))
        self.add_submitted(self.student, scope, (4, 5), submitted=EARLIER)
        self.assertEqual(self.progress(self.student, scope), self.expect(scope, 1, 4, 5))
        # An open or abandoned session newer than the submitted one still moves nothing.
        self.add_session(self.student, scope, abandoned="2030-01-01 00:00:00")
        self.assertEqual(self.progress(self.student, scope), self.expect(scope, 1, 4, 5))

    def test_every_submitted_repeat_counts_and_none_is_best_or_averaged(self):
        scope = self.make_scope(6)
        self.add_submitted(self.student, scope, (9, 10), submitted="2026-01-01 10:00:00")
        self.add_submitted(self.student, scope, (2, 10), submitted="2026-01-02 10:00:00")
        self.add_submitted(self.student, scope, (5, 8), submitted="2026-01-03 10:00:00")
        self.assertEqual(self.progress(self.student, scope), self.expect(scope, 3, 5, 8))

    def test_the_latest_is_the_greatest_stored_time_not_the_greatest_id_or_the_best_score(self):
        scope = self.make_scope(6)
        self.add_submitted(self.student, scope, (3, 4), submitted="2026-03-01 12:00:00")   # id 1: the latest
        self.add_submitted(self.student, scope, (9, 10), submitted="2026-01-01 12:00:00")  # id 2: older, better
        self.add_submitted(self.student, scope, (1, 10), submitted="2025-12-31 23:59:59")  # id 3: oldest
        self.assertEqual(self.progress(self.student, scope), self.expect(scope, 3, 3, 4))

    def test_text_order_is_time_order_across_every_field_of_the_stored_timestamp(self):
        scope = self.make_scope(6)
        times = ["2025-12-31 23:59:59", "2026-01-01 00:00:00", "2026-01-01 09:59:59", "2026-01-01 10:00:00",
                 "2026-01-01 10:00:01", "2026-01-01 10:01:00", "2026-02-01 00:00:00", "2026-10-01 00:00:00"]
        for position, time in enumerate(reversed(times), start=1):       # inserted newest first
            self.add_submitted(self.student, scope, (position, 10), submitted=time)
        self.assertEqual(self.progress(self.student, scope), self.expect(scope, len(times), 1, 10))

    def test_a_timestamp_tie_takes_the_larger_id_whatever_the_insertion_order_of_scores(self):
        scope = self.make_scope(6)
        self.add_submitted(self.student, scope, (7, 10), submitted=STAMP)   # smaller id
        self.add_submitted(self.student, scope, (2, 10), submitted=STAMP)   # larger id wins the tie
        self.assertEqual(self.progress(self.student, scope), self.expect(scope, 2, 2, 10))
        self.add_submitted(self.student, scope, (5, 10), submitted=EARLIER)  # a later id but an earlier time
        self.assertEqual(self.progress(self.student, scope), self.expect(scope, 3, 2, 10))
        self.add_submitted(self.student, scope, (6, 10), submitted=STAMP)   # the largest id of the tied second
        self.assertEqual(self.progress(self.student, scope), self.expect(scope, 4, 6, 10))
        self.assertEqual(self.progress(self.student, scope), self.progress(self.student, scope))

    def test_the_saved_score_is_whatever_was_stored(self):
        scope = self.make_scope(6)
        for stored in ((0, 1), (0, 10), (10, 10), (3, 7)):
            with self.subTest(score=stored):
                self.conn.execute("DELETE FROM practice_sessions")
                self.add_submitted(self.student, scope, stored)
                self.assertEqual(self.progress(self.student, scope), self.expect(scope, 1, *stored))


class ScopeSeparationTest(ProgressCase):
    def test_a_topic_is_not_its_section_and_the_null_topic_is_the_section_wide_practice_alone(self):
        scope = self.make_scope(6, topics=2)
        self.add_submitted(self.student, scope, (1, 5), submitted="2026-01-01 10:00:00")                  # section-wide
        self.add_submitted(self.student, scope, (2, 6), topic_index=0, submitted="2026-01-02 10:00:00")
        self.add_submitted(self.student, scope, (3, 7), topic_index=0, submitted="2026-01-03 10:00:00")
        self.add_submitted(self.student, scope, (4, 8), topic_index=1, submitted="2026-01-04 10:00:00")
        self.assertEqual(self.progress(self.student, scope), self.expect(scope, 1, 1, 5))
        self.assertEqual(self.progress(self.student, scope, 0), self.expect(scope, 2, 3, 7, topic_index=0))
        self.assertEqual(self.progress(self.student, scope, 1), self.expect(scope, 1, 4, 8, topic_index=1))

    def test_topic_sessions_alone_leave_the_section_at_zero_and_there_is_no_roll_up(self):
        scope = self.make_scope(6, topics=2)
        self.add_submitted(self.student, scope, (5, 6), topic_index=0)
        self.add_submitted(self.student, scope, (6, 6), topic_index=1)
        self.assertEqual(self.progress(self.student, scope), self.expect(scope, 0))

    def test_a_section_session_never_counts_in_a_topic(self):
        scope = self.make_scope(6, topics=2)
        self.add_submitted(self.student, scope, (5, 6))
        self.assertEqual(self.progress(self.student, scope, 0), self.expect(scope, 0, topic_index=0))

    def test_another_section_is_another_scope(self):
        first, second = self.make_scope(6), self.make_scope(6)
        self.add_submitted(self.student, first, (3, 3))
        self.assertEqual(self.progress(self.student, first), self.expect(first, 1, 3, 3))
        self.assertEqual(self.progress(self.student, second), self.expect(second, 0))


class OwnerTest(ProgressCase):
    def test_another_accounts_sessions_never_move_a_number_and_each_role_reads_only_its_own(self):
        scope = self.make_scope(6, topics=2)
        self.add_submitted(self.student, scope, (3, 5), submitted="2026-01-01 10:00:00")
        self.add_submitted(self.student, scope, (4, 5), submitted="2026-01-02 10:00:00")
        self.add_submitted(self.other_student, scope, (9, 10), submitted="2026-05-01 10:00:00")
        self.add_submitted(self.teacher, scope, (1, 3), submitted="2026-06-01 10:00:00")
        self.add_submitted(self.teacher, scope, (2, 3), submitted="2026-06-02 10:00:00")
        self.add_submitted(self.teacher, scope, (3, 3), submitted="2026-06-03 10:00:00")
        self.add_submitted(self.admin, scope, (7, 8), topic_index=1, submitted="2026-07-01 10:00:00")
        self.assertEqual(self.progress(self.student, scope), self.expect(scope, 2, 4, 5))
        self.assertEqual(self.progress(self.other_student, scope), self.expect(scope, 1, 9, 10))
        self.assertEqual(self.progress(self.teacher, scope), self.expect(scope, 3, 3, 3))
        self.assertEqual(self.progress(self.admin, scope), self.expect(scope, 0))
        self.assertEqual(self.progress(self.admin, scope, 1), self.expect(scope, 1, 7, 8, topic_index=1))
        self.assertEqual(self.progress(self.student, scope, 1), self.expect(scope, 0, topic_index=1))

    def test_an_account_with_nothing_submitted_sees_zero_beside_a_busy_account(self):
        scope = self.make_scope(6)
        for index in range(5):
            self.add_submitted(self.other_student, scope, (index, 5), submitted=f"2026-01-0{index + 1} 10:00:00")
        self.assertEqual(self.progress(self.student, scope), self.expect(scope, 0))

    def test_the_account_id_comes_from_the_principal_only(self):
        scope = self.make_scope(6)
        self.add_submitted(self.other_student, scope, (5, 5))
        # A principal that claims a role but another account's display name still reads its own id.
        forged = account_principal(self.student.user_id, "student", self.other_student.role)
        self.assertEqual(self.progress(forged, scope), self.expect(scope, 0))
        parameters = inspect.signature(practice.read_practice_progress).parameters
        self.assertEqual(list(parameters), ["conn", "principal", "section_id", "topic_id"])

    def test_an_inactive_account_has_no_progress_but_the_scope_is_still_available(self):
        scope = self.make_scope(6)
        inactive = self.add_account("student", active=False)
        self.conn.commit()
        self.add_submitted(inactive, scope, (4, 5))
        self.assertEqual(self.progress(inactive, scope), self.expect(scope, 0))
        self.conn.execute("UPDATE users SET is_active = 0 WHERE id = ?", (self.student.user_id,))
        self.add_submitted(self.student, scope, (3, 3))
        self.assertEqual(self.progress(self.student, scope), self.expect(scope, 0))
        self.conn.execute("UPDATE users SET is_active = 1 WHERE id = ?", (self.student.user_id,))
        self.conn.commit()
        self.assertEqual(self.progress(self.student, scope), self.expect(scope, 1, 3, 3))

    def test_a_missing_account_and_an_unknown_stored_role_have_no_progress(self):
        scope = self.make_scope(6)
        self.add_submitted(self.student, scope, (3, 3))
        ghost = account_principal(987654, "student", "x")
        self.assertEqual(self.progress(ghost, scope), self.expect(scope, 0))
        self.conn.execute("PRAGMA ignore_check_constraints = ON")
        self.conn.execute("UPDATE users SET role = 'visitor' WHERE id = ?", (self.student.user_id,))
        self.conn.execute("PRAGMA ignore_check_constraints = OFF")
        self.conn.commit()
        self.assertEqual(self.progress(self.student, scope), self.expect(scope, 0))

    def test_a_principal_that_is_not_an_account_is_refused(self):
        scope = self.make_scope(6)
        self.add_submitted(self.student, scope, (3, 3))
        pending = Principal(is_teacher=False, is_admin=True, user_id=None, role="admin", display_name="x")
        for who in (ANONYMOUS, pending, None, "student", 7, self.student.user_id):
            with self.subTest(principal=who):
                with self.assertRaises(practice.PracticeAccessError):
                    practice.read_practice_progress(self.conn, who, scope.section_id)
        for bad in (Principal(False, False, True, "student", "x"), Principal(False, False, 0, "student", "x"),
                    Principal(False, False, -3, "student", "x"), Principal(False, False, 2 ** 63, "student", "x"),
                    Principal(False, False, 5, "visitor", "x")):
            with self.subTest(principal=bad):
                with self.assertRaises(practice.PracticeAccessError):
                    practice.read_practice_progress(self.conn, bad, scope.section_id)
        self.assertFalse(self.conn.in_transaction)


class AvailabilityTest(ProgressCase):
    def test_a_scope_a_start_would_refuse_is_none_whatever_was_stored(self):
        scope, other = self.make_scope(6), self.make_scope(6)
        dzi = self.make_scope(6, section_type="dzi_training")
        orphan = self.make_scope(6)
        self.add_submitted(self.student, dzi, (3, 3))
        self.add_submitted(self.student, orphan, (3, 3))
        self.conn.execute("UPDATE curriculum_sections SET curriculum_id = NULL WHERE id = ?", (orphan.section_id,))
        self.conn.commit()
        for section_id, topic_id in ((dzi.section_id, None), (dzi.section_id, dzi.topic_ids[0]),
                                     (orphan.section_id, None), (987654, None),
                                     (scope.section_id, other.topic_ids[0]), (scope.section_id, 987654)):
            with self.subTest(section=section_id, topic=topic_id):
                self.assertIsNone(practice.read_practice_progress(self.conn, self.student, section_id, topic_id))
                self.assertFalse(practice.practice_scope_available(self.conn, section_id, topic_id))

    def test_an_id_that_is_not_a_positive_integer_is_none(self):
        scope = self.make_scope(6)
        for section_id, topic_id in ((0, None), (-1, None), (True, None), ("1", None), (None, None), (1.0, None),
                                     (2 ** 63, None), (scope.section_id, 0), (scope.section_id, -1),
                                     (scope.section_id, True), (scope.section_id, "1"), (scope.section_id, 2 ** 63)):
            with self.subTest(section=section_id, topic=topic_id):
                self.assertIsNone(practice.read_practice_progress(self.conn, self.student, section_id, topic_id))

    def test_the_decision_is_the_scope_of_a_start_and_never_the_pool(self):
        thin, empty = self.make_scope(2, topics=2), self.make_scope(0)
        self.add_submitted(self.student, thin, (2, 2), topic_index=1)
        self.assertEqual(self.progress(self.student, thin, 1), self.expect(thin, 1, 2, 2, topic_index=1))
        self.assertEqual(self.progress(self.student, thin), self.expect(thin, 0))
        self.assertEqual(self.progress(self.student, empty), self.expect(empty, 0))

    def test_it_agrees_with_the_scope_reads_for_every_scope_shape(self):
        scope, other = self.make_scope(6, topics=2), self.make_scope(6)
        dzi = self.make_scope(6, section_type="dzi_training")
        for section_id, topic_id in ((scope.section_id, None), (scope.section_id, scope.topic_ids[1]),
                                     (other.section_id, other.topic_ids[0]), (dzi.section_id, None),
                                     (scope.section_id, other.topic_ids[0]), (999999, None), (0, None)):
            with self.subTest(section=section_id, topic=topic_id):
                available = practice.practice_scope_available(self.conn, section_id, topic_id)
                read = practice.read_practice_progress(self.conn, self.student, section_id, topic_id)
                self.assertEqual(read is not None, available)
                self.assertEqual(read is not None, practice.read_practice_scope(self.conn, section_id, topic_id) is not None)


class SavedScoreTest(ProgressCase):
    def test_content_changes_never_move_the_count_or_a_saved_score(self):
        scope = self.make_scope(8, topics=2)
        self.add_submitted(self.student, scope, (6, 8), submitted="2026-01-01 10:00:00")
        self.add_submitted(self.student, scope, (7, 8), topic_index=1, submitted="2026-01-02 10:00:00")
        before = (self.progress(self.student, scope), self.progress(self.student, scope, 1))
        for question_id in scope.question_ids[0] + scope.question_ids[1][:3]:
            self.break_question(question_id)
        self.conn.execute("UPDATE questions SET prompt = 'changed' WHERE id = ?", (scope.question_ids[1][5],))
        self.make_dzi_legacy(scope.question_ids[1][6])
        self.conn.commit()
        self.assertEqual((self.progress(self.student, scope), self.progress(self.student, scope, 1)), before)
        # Even when no question is left at all: the score is stored, not re-derived.
        self.conn.execute("DELETE FROM multiple_choice_options")
        self.conn.execute("UPDATE questions SET is_ai_generated = 1, quality_score = 0")  # no longer approved
        self.conn.commit()
        self.assertEqual((self.progress(self.student, scope), self.progress(self.student, scope, 1)), before)
        self.assertEqual(before[0], self.expect(scope, 1, 6, 8))

    def test_it_reads_no_question_answer_or_assignment_table(self):
        scope = self.make_scope(6, topics=2)
        self.add_submitted(self.student, scope, (3, 3))
        statements = []
        with closing(web_db.connect_db(self.db_path)) as conn:
            conn.set_trace_callback(statements.append)
            self.assertEqual(self.progress(self.student, scope, conn=conn), self.expect(scope, 1, 3, 3))
        text = "\n".join(statements).lower()
        self.assertIn("practice_sessions", text)
        for table in ("practice_answers", "questions", "multiple_choice_options", "quiz_answers", "quiz_attempts",
                      "quiz_assignments", "exam_tasks", "assignment"):
            self.assertNotIn(table, text.replace("curriculum_questions", ""))


class ReadOnlyTest(ProgressCase):
    def seeded(self):
        scope = self.make_scope(6, topics=2)
        self.add_submitted(self.student, scope, (3, 4))
        self.add_session(self.student, scope, scope.topic_ids[0])
        return scope

    def test_it_works_on_a_query_only_connection_and_writes_nothing(self):
        scope = self.seeded()
        before = self.table_digests()
        with closing(web_db.connect_db(self.db_path, query_only=True)) as conn:
            self.assertEqual(self.progress(self.student, scope, conn=conn), self.expect(scope, 1, 3, 4))
            self.assertEqual(self.progress(self.student, scope, 0, conn=conn), self.expect(scope, 0, topic_index=0))
            self.assertFalse(conn.in_transaction)
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute("UPDATE practice_sessions SET seed = 'x'")
        self.assertEqual(self.table_digests(), before)

    def test_no_row_changes_and_no_change_is_counted_on_a_normal_connection(self):
        scope = self.seeded()
        before = self.table_digests()
        with closing(web_db.connect_db(self.db_path)) as conn:
            changes = conn.total_changes
            self.progress(self.student, scope, conn=conn)
            self.progress(self.student, scope, 0, conn=conn)
            self.progress(self.student, scope, 1, conn=conn)
            self.assertEqual(conn.total_changes, changes)
            self.assertFalse(conn.in_transaction)
        self.assertEqual(self.table_digests(), before)

    def test_it_is_one_snapshot_from_a_begin_to_an_explicit_rollback(self):
        scope = self.seeded()
        statements = []
        with closing(web_db.connect_db(self.db_path)) as conn:
            conn.set_trace_callback(statements.append)
            self.progress(self.student, scope, conn=conn)
        keywords = [s.split()[0].upper() for s in statements if not s.lstrip().upper().startswith("PRAGMA")]
        self.assertEqual(keywords[0], "BEGIN")
        self.assertEqual(keywords[-1], "ROLLBACK")
        self.assertEqual(keywords.count("BEGIN"), 1)
        self.assertEqual(keywords.count("ROLLBACK"), 1)
        self.assertNotIn("COMMIT", keywords)
        self.assertEqual({k for k in keywords[1:-1]}, {"SELECT"})

    def test_a_caller_transaction_is_reused_and_left_open_exactly_as_it_was(self):
        scope = self.seeded()
        self.conn.execute("BEGIN")
        self.assertEqual(self.progress(self.student, scope), self.expect(scope, 1, 3, 4))
        self.assertTrue(self.conn.in_transaction)
        # The caller's own uncommitted work is part of its snapshot and survives the read.
        self.add_submitted(self.student, scope, (4, 4), submitted=LATER, commit=False)
        self.assertEqual(self.progress(self.student, scope), self.expect(scope, 2, 4, 4))
        self.assertTrue(self.conn.in_transaction)
        self.conn.execute("ROLLBACK")
        self.assertEqual(self.progress(self.student, scope), self.expect(scope, 1, 3, 4))
        self.assertFalse(self.conn.in_transaction)

    def test_a_caller_transaction_stays_open_when_the_read_fails(self):
        scope = self.seeded()
        self.conn.execute("BEGIN")
        with self.assertRaises(practice.PracticeAccessError):
            practice.read_practice_progress(self.conn, ANONYMOUS, scope.section_id)
        self.assertTrue(self.conn.in_transaction)
        self.conn.execute("ROLLBACK")

    def test_an_error_inside_our_own_snapshot_releases_the_connection(self):
        scope = self.seeded()
        self.conn.execute("PRAGMA ignore_check_constraints = ON")
        self.conn.execute("UPDATE practice_sessions SET score_total = 0 WHERE submitted_at IS NOT NULL")
        self.conn.execute("PRAGMA ignore_check_constraints = OFF")
        self.conn.commit()
        with self.assertRaises(practice.PracticeStateError):
            self.progress(self.student, scope)
        self.assertFalse(self.conn.in_transaction)
        self.assertEqual(self.progress(self.student, scope, 0), self.expect(scope, 0, topic_index=0))

    def test_stored_scores_that_are_not_whole_and_bounded_are_refused_never_repaired(self):
        scope = self.make_scope(6)
        for correct, total in ((5, 4), (-1, 4), (0, 0), (1.5, 4), (2, 4.5)):
            with self.subTest(score=(correct, total)):
                self.conn.execute("DELETE FROM practice_sessions")
                self.conn.execute("PRAGMA ignore_check_constraints = ON")
                self.conn.execute(
                    "INSERT INTO practice_sessions (user_id, section_id, topic_id, seed, question_ids_json, "
                    "started_at, submitted_at, score_correct, score_total) VALUES (?, ?, NULL, 's', '[]', ?, ?, ?, ?)",
                    (self.student.user_id, scope.section_id, STAMP, STAMP, correct, total))
                self.conn.execute("PRAGMA ignore_check_constraints = OFF")
                self.conn.commit()
                with self.assertRaises(practice.PracticeStateError):
                    self.progress(self.student, scope)
                self.assertFalse(self.conn.in_transaction)

    def test_an_unconfigured_connection_is_refused(self):
        scope = self.seeded()
        plain = sqlite3.connect(self.db_path)
        self.addCleanup(plain.close)
        with self.assertRaises(RuntimeError):
            practice.read_practice_progress(plain, self.student, scope.section_id)
        no_foreign_keys = web_db.connect_db(self.db_path)
        self.addCleanup(no_foreign_keys.close)
        no_foreign_keys.execute("PRAGMA foreign_keys = OFF")
        with self.assertRaises(RuntimeError):
            practice.read_practice_progress(no_foreign_keys, self.student, scope.section_id)

    def test_a_connection_in_autocommit_mode_is_released_too(self):
        scope = self.seeded()
        conn = sqlite3.connect(self.db_path, autocommit=True)
        self.addCleanup(conn.close)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        self.assertEqual(self.progress(self.student, scope, conn=conn), self.expect(scope, 1, 3, 4))
        self.assertFalse(conn.in_transaction)
        self.conn.execute("DELETE FROM practice_sessions")
        self.conn.commit()
        self.assertEqual(self.progress(self.student, scope, conn=conn), self.expect(scope, 0))


class SnapshotTest(ProgressCase):
    """The count and the saved score are one snapshot, even while another connection commits between them."""

    def setUp(self):
        super().setUp()
        self.conn.execute("PRAGMA journal_mode = WAL")  # a disposable fixture; the live database is prepared once
        self.scope = self.make_scope(6)
        self.add_submitted(self.student, self.scope, (3, 4), submitted="2026-01-01 10:00:00")
        self.add_submitted(self.student, self.scope, (2, 5), submitted="2026-01-02 10:00:00")
        self.conn.commit()

    def commit_newer_session_from_another_connection(self):
        with closing(sqlite3.connect(self.db_path, timeout=30)) as writer:
            writer.execute("PRAGMA foreign_keys = ON")
            writer.execute(
                "INSERT INTO practice_sessions (user_id, section_id, topic_id, seed, question_ids_json, started_at, "
                "submitted_at, score_correct, score_total) VALUES (?, ?, NULL, 's', '[]', ?, ?, 9, 9)",
                (self.student.user_id, self.scope.section_id, STAMP, "2026-06-01 10:00:00"))
            writer.commit()

    def trace_until(self, conn, fired, prefix):
        """Commit a newer session from another connection right before the statement that starts with ``prefix``."""
        def trace(statement):
            if statement.lstrip().upper().startswith(prefix) and not fired:
                fired.append(statement)
                self.commit_newer_session_from_another_connection()
        conn.set_trace_callback(trace)

    def test_a_commit_before_the_count_or_between_the_count_and_the_score_changes_neither_number(self):
        for prefix in ("SELECT COUNT(*)", "SELECT SCORE_CORRECT"):
            with self.subTest(commit_before=prefix):
                fired = []
                with closing(web_db.connect_db(self.db_path)) as conn:
                    self.trace_until(conn, fired, prefix)
                    seen = self.progress(self.student, self.scope, conn=conn)
                self.assertEqual(len(fired), 1, "the concurrent commit must really happen mid-read")
                self.assertEqual(seen, self.expect(self.scope, 2, 2, 5))
                # The next read is a new snapshot and sees the committed session, as one coherent pair.
                self.assertEqual(self.progress(self.student, self.scope), self.expect(self.scope, 3, 9, 9))
                self.conn.execute("DELETE FROM practice_sessions WHERE score_correct = 9")
                self.conn.commit()

    def test_negative_control_the_same_commit_does_split_two_unsnapshotted_reads(self):
        fired = []
        with closing(web_db.connect_db(self.db_path)) as conn:
            self.trace_until(conn, fired, "SELECT SCORE_CORRECT")
            parameters = {"user_id": self.student.user_id, "section_id": self.scope.section_id, "topic_id": None}
            count = conn.execute(f"SELECT COUNT(*) {practice._SUBMITTED_SCOPE_SQL}", parameters).fetchone()[0]
            latest = conn.execute(
                f"SELECT score_correct, score_total {practice._SUBMITTED_SCOPE_SQL} "
                "ORDER BY submitted_at DESC, id DESC LIMIT 1", parameters).fetchone()
        self.assertEqual(len(fired), 1)
        self.assertEqual((count, tuple(latest)), (2, (9, 9)))  # a count of two beside a score of the third

    def test_a_caller_snapshot_is_kept_across_a_concurrent_commit(self):
        self.conn.execute("BEGIN")
        self.assertEqual(self.progress(self.student, self.scope), self.expect(self.scope, 2, 2, 5))
        self.commit_newer_session_from_another_connection()
        self.assertEqual(self.progress(self.student, self.scope), self.expect(self.scope, 2, 2, 5))
        self.assertTrue(self.conn.in_transaction)
        self.conn.execute("ROLLBACK")
        self.assertEqual(self.progress(self.student, self.scope), self.expect(self.scope, 3, 9, 9))

    def test_the_account_is_part_of_the_snapshot(self):
        fired = []

        def deactivate_after_the_account_row(statement):
            if statement.lstrip().upper().startswith("SELECT SCORE_CORRECT") and not fired:
                fired.append(statement)
                with closing(sqlite3.connect(self.db_path, timeout=30)) as writer:
                    writer.execute("UPDATE users SET is_active = 0 WHERE id = ?", (self.student.user_id,))
                    writer.commit()
        with closing(web_db.connect_db(self.db_path)) as conn:
            conn.set_trace_callback(deactivate_after_the_account_row)
            seen = self.progress(self.student, self.scope, conn=conn)
        self.assertEqual(len(fired), 1)
        self.assertEqual(seen, self.expect(self.scope, 2, 2, 5))  # active when the snapshot began
        self.assertEqual(self.progress(self.student, self.scope), self.expect(self.scope, 0))


class QueryPlanTest(ProgressCase):
    def plans(self, topic_id):
        parameters = {"user_id": self.student.user_id, "section_id": 1, "topic_id": topic_id}
        count = f"SELECT COUNT(*) {practice._SUBMITTED_SCOPE_SQL}"
        latest = (f"SELECT score_correct, score_total {practice._SUBMITTED_SCOPE_SQL} "
                  "ORDER BY submitted_at DESC, id DESC LIMIT 1")
        return {name: [row[3] for row in self.conn.execute("EXPLAIN QUERY PLAN " + sql, parameters)]
                for name, sql in (("count", count), ("latest", latest))}

    def test_both_reads_search_the_scoped_index_on_all_three_scope_columns(self):
        index = self.conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'index' AND name = 'idx_practice_sessions_user_scope'"
        ).fetchone()
        self.assertIsNotNone(index)
        self.assertIn("practice_sessions(user_id, section_id, topic_id)", index[0])
        for topic_id in (None, 7):
            for name, plan in self.plans(topic_id).items():
                with self.subTest(read=name, topic=topic_id):
                    searches = [line for line in plan if line.startswith("SEARCH practice_sessions")]
                    self.assertEqual(len(searches), 1, plan)
                    self.assertIn("USING INDEX idx_practice_sessions_user_scope "
                                  "(user_id=? AND section_id=? AND topic_id=?)", searches[0])
                    self.assertFalse([line for line in plan if line.startswith("SCAN")], plan)

    def test_no_new_index_or_schema_object_is_introduced(self):
        names = {row[0] for row in self.conn.execute(
            "SELECT name FROM sqlite_master WHERE tbl_name = 'practice_sessions' AND type = 'index' "
            "AND name NOT LIKE 'sqlite_autoindex%'")}
        self.assertEqual(names, {"uniq_practice_sessions_open_scope", "idx_practice_sessions_user_scope"})


if __name__ == "__main__":
    unittest.main()
