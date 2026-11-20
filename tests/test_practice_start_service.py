"""The practice start service (C2, roadmap PR #367): ``web.services.practice``.

Every database is a temporary clone of the migrations + canonical template
(``tests.support.build_test_database``) with synthetic curriculum rows added on top, so each
pool has an exactly known size. Nothing here reads, copies or hashes ``data/questions.db``.

What is proved, and how:

* the outcome contract, validation, account rules and ownership;
* scope and topic membership, and every eligibility exclusion. Each exclusion is a pair: a
  pool of four good questions plus one *bad* candidate stays below the minimum, while the
  same pool with the candidate made *good* reaches it, so the exclusion is what mattered;
* resume, stale abandonment, replacement and repeat, with the stored plan never rewritten;
* malformed vs merely stale stored plans;
* transactions: the lock is held while deciding and stamping, a failed write restores the
  previous rows (a real SQLite trigger fails the write; the same call succeeds without it),
  and nothing outside ``practice_sessions`` ever changes;
* concurrency over separate real connections, synchronized with barriers and events rather
  than sleeps, with a deferred-transaction negative control that does fail.
"""

import hashlib
import json
import sqlite3
import tempfile
import threading
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from src.subject_assessment import AssessmentScope
from tests import runtime_fixtures
from tests.subject_fixtures import add_curriculum, add_membership, add_question, add_section, add_topic, four_options
from tests.support import build_test_database, set_lifecycle_clock_row
from web import db as web_db
from web.access import ANONYMOUS, Principal, account_principal
from web.services import accounts as accounts_service
from web.services import assignment_quota, lifecycle, practice
from web.services.dzi import dzi_question_sql, is_dzi_question
from web.services.quiz import filter_renderable_attempt_question_ids, quiz_section_question_ids

STAMP = "2026-01-01 10:00:00"
TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S"


def digest(rows) -> str:
    """Order-independent digest, so tables without a rowid need no special case."""
    return hashlib.sha256(repr(sorted(repr(tuple(row)) for row in rows)).encode()).hexdigest()


class DeferredConnection(sqlite3.Connection):
    """A connection whose ``BEGIN IMMEDIATE`` is silently a deferred ``BEGIN``: the negative
    control that proves the writer lock, not luck, orders two concurrent starts."""

    def execute(self, sql, *args):
        return super().execute("BEGIN" if sql == "BEGIN IMMEDIATE" else sql, *args)


class PracticeCase(unittest.TestCase):
    """A fresh temporary database, accounts and a synthetic-content builder per test."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="learnpilot-practice-")
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.db_path = build_test_database(self.directory / "practice.db")
        self.conn = self.connect()
        self.counter = 1000
        self.student = self.add_account("student")
        self.other_student = self.add_account("student")
        self.teacher = self.add_account("teacher")
        self.admin = self.add_account("admin")
        self.conn.commit()

    # ------------------------------------------------------------------ plumbing
    def connect(self, timeout=30.0):
        conn = web_db.connect_db(self.db_path, timeout=timeout)
        self.addCleanup(conn.close)
        return conn

    def next(self) -> int:
        self.counter += 1
        return self.counter

    def add_account(self, role="student", *, active=True) -> Principal:
        user_id = accounts_service.create_user(
            self.conn, display_name=f"Practice {role} {self.next()}", role=role, is_active=active)
        return account_principal(user_id, role, "x")

    def start(self, who, scope, topic_id=None, conn=None):
        section_id = scope if isinstance(scope, int) else scope.section_id
        return practice.start_or_resume_practice(conn or self.conn, who, section_id, topic_id)

    def sessions(self):
        with self.raw() as conn:
            return [dict(row) for row in conn.execute("SELECT * FROM practice_sessions ORDER BY id")]

    def raw(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        self.addCleanup(conn.close)
        return conn

    def practice_digest(self):
        with self.raw() as conn:
            return digest(conn.execute("SELECT * FROM practice_sessions ORDER BY id").fetchall()
                          + conn.execute("SELECT * FROM practice_answers ORDER BY session_id").fetchall())

    def table_digests(self, skip=()):
        with self.raw() as conn:
            names = [row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
            return {name: digest(conn.execute(f'SELECT * FROM "{name}"').fetchall())
                    for name in names if name not in skip}

    def assert_untouched(self, action):
        """``action`` raised or returned without changing any practice row or the transaction state."""
        before = self.practice_digest()
        try:
            return action()
        finally:
            self.assertEqual(before, self.practice_digest())
            self.assertFalse(self.conn.in_transaction)

    # ------------------------------------------------------------------ content
    def make_scope(self, good=6, *, subject="it", topics=1, section_type="content"):
        """A synthetic section with ``topics`` topics; ``good`` eligible questions in each."""
        n = self.next()
        curriculum = add_curriculum(self.conn, subject, 8, f"practice-{n}")
        section = add_section(self.conn, curriculum, f"practice-section-{n}", grade=8, section_type=section_type)
        topic_ids, question_ids = [], []
        for index in range(topics):
            topic = add_topic(self.conn, section, f"practice-topic-{n}-{index}")
            topic_ids.append(topic)
            question_ids.append([self.good_question(topic, subject) for _ in range(good)])
        self.conn.commit()
        return Scope(section, topic_ids, question_ids, subject, curriculum)

    def good_question(self, topic_id, subject="it", **kwargs):
        number = self.next()
        return add_question(self.conn, topic_id, subject, number, source="practice-synthetic",
                            options=four_options(), **kwargs)

    def add_exam(self, level="DZI"):
        return int(self.conn.execute(
            "INSERT INTO exams (subject, level, year, session, variant, source_file) VALUES ('it', ?, ?, 'may', 1, 'x.pdf')",
            (level, 3000 + self.next())).lastrowid)

    def make_dzi_legacy(self, question_id, level="DZI"):
        self.conn.execute("UPDATE questions SET exam_id = ? WHERE id = ?", (self.add_exam(level), question_id))

    def make_dzi_task(self, question_id):
        exam = self.add_exam()
        task = int(self.conn.execute(
            "INSERT INTO exam_tasks (exam_id, task_number, exam_part, task_kind, points) VALUES (?, 1, 1, 'multiple_choice', 1)",
            (exam,)).lastrowid)
        self.conn.execute("INSERT INTO exam_task_questions (task_id, question_id) VALUES (?, ?)", (task, question_id))

    def break_question(self, question_id):
        self.conn.execute("DELETE FROM multiple_choice_options WHERE question_id = ?", (question_id,))

    def insert_session(self, who, scope, plan, *, topic_id=None, seed="stored-seed", started=STAMP,
                       submitted=None, abandoned=None):
        """A stored session as an earlier start (or damage) would have left it; ``plan`` may be raw text."""
        raw = plan if isinstance(plan, str) else json.dumps(plan)
        score = (1, 2) if submitted else (None, None)
        cursor = self.conn.execute(
            "INSERT INTO practice_sessions (user_id, section_id, topic_id, seed, question_ids_json, started_at, "
            "submitted_at, abandoned_at, score_correct, score_total) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (who.user_id, scope.section_id, topic_id, seed, raw, started, submitted, abandoned, *score))
        self.conn.commit()
        return int(cursor.lastrowid)

    def seeds(self, *values):
        iterator = iter(values)
        return patch.object(practice, "_new_seed", side_effect=lambda: next(iterator))


class Scope:
    def __init__(self, section_id, topic_ids, question_ids, subject, curriculum_id):
        self.section_id = section_id
        self.topic_ids = topic_ids
        self.question_ids = question_ids          # one list per topic
        self.subject = subject
        self.curriculum_id = curriculum_id

    @property
    def all_question_ids(self):
        return [qid for ids in self.question_ids for qid in ids]


# ====================================================================== outcome contract
class CreateTest(PracticeCase):
    def test_a_new_session_is_stored_exactly_as_returned(self):
        scope = self.make_scope(12)
        result = self.start(self.student, scope)
        self.assertEqual(result.outcome, practice.OUTCOME_CREATED)
        self.assertTrue(result.created)
        self.assertFalse(result.resumed)
        self.assertIsNone(result.abandoned_session_id)
        rows = self.sessions()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["id"], result.session_id)
        self.assertEqual((row["user_id"], row["section_id"], row["topic_id"]),
                         (self.student.user_id, scope.section_id, None))
        self.assertEqual(row["seed"], result.seed)
        self.assertEqual(json.loads(row["question_ids_json"]), list(result.question_ids))
        self.assertRegex(row["started_at"], r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d$")
        self.assertEqual((row["submitted_at"], row["abandoned_at"], row["score_correct"], row["score_total"]),
                         (None, None, None, None))
        self.assertEqual(len(result.question_ids), 10)
        self.assertEqual(len(set(result.question_ids)), 10)
        self.assertLessEqual(set(result.question_ids), set(scope.all_question_ids))
        with self.raw() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM practice_answers").fetchone()[0], 0)
        self.assertFalse(self.conn.in_transaction)

    def test_pool_sizes_around_the_minimum_and_the_maximum(self):
        for pool, expected in ((0, None), (4, None), (5, 5), (6, 6), (10, 10), (11, 10), (25, 10)):
            with self.subTest(pool=pool):
                scope = self.make_scope(pool)
                before = len(self.sessions())
                result = self.start(self.student, scope)
                if expected is None:
                    self.assertEqual(result, practice.PracticeStart(practice.OUTCOME_EMPTY_POOL))
                    self.assertEqual(len(self.sessions()), before)
                else:
                    self.assertTrue(result.created)
                    self.assertEqual(len(result.question_ids), expected)
                    self.assertEqual(len(set(result.question_ids)), expected)
                    self.assertEqual(len(self.sessions()), before + 1)

    def test_the_sample_is_without_replacement_and_follows_the_seed(self):
        scope = self.make_scope(25)
        with self.seeds("seed-one"):
            first = self.start(self.student, scope)
        with self.seeds("seed-one"):
            same_seed = self.start(self.other_student, scope)
        self.assertEqual(first.seed, "seed-one")
        self.assertEqual(first.question_ids, same_seed.question_ids)
        with self.seeds("seed-two"):
            other = self.start(self.teacher, scope)
        self.assertNotEqual(first.question_ids, other.question_ids)

    def test_real_seeds_are_fresh_per_session_including_a_repeat_after_submission(self):
        scope = self.make_scope(25)
        first = self.start(self.student, scope)
        self.conn.execute("UPDATE practice_sessions SET submitted_at = ?, score_correct = 1, score_total = 10", (STAMP,))
        self.conn.commit()
        repeat = self.start(self.student, scope)
        others = [self.start(who, scope) for who in (self.other_student, self.teacher, self.admin)]
        seeds = [first.seed, repeat.seed] + [o.seed for o in others]
        self.assertEqual(len(set(seeds)), len(seeds))
        for seed in seeds:
            self.assertRegex(seed, r"^[0-9a-f]{32}$")

    def test_the_stored_row_is_the_whole_effect(self):
        scope = self.make_scope(6)
        before = self.table_digests(skip={"practice_sessions"})
        schema = self.raw().execute("SELECT type, name, sql FROM sqlite_master ORDER BY name").fetchall()
        self.start(self.student, scope)
        self.assertEqual(before, self.table_digests(skip={"practice_sessions"}))
        self.assertEqual(digest(schema), digest(
            self.raw().execute("SELECT type, name, sql FROM sqlite_master ORDER BY name").fetchall()))


class ValidationTest(PracticeCase):
    def test_ids_must_be_positive_integers(self):
        scope = self.make_scope(6)
        for bad in (0, -1, True, False, "1", 1.0, None, 2 ** 63, b"1", [1]):
            with self.subTest(section=bad):
                with self.assertRaises(ValueError):
                    self.assert_untouched(lambda: practice.start_or_resume_practice(self.conn, self.student, bad))
        for bad in (0, -1, True, "1", 1.5, 2 ** 63, [1]):
            with self.subTest(topic=bad):
                with self.assertRaises(ValueError):
                    self.assert_untouched(
                        lambda: practice.start_or_resume_practice(self.conn, self.student, scope.section_id, bad))
        self.assertEqual(self.sessions(), [])

    def test_only_an_account_principal_can_practice(self):
        scope = self.make_scope(6)
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
                    self.assert_untouched(lambda: self.start(principal, scope))

    def test_an_unconfigured_connection_is_refused_without_a_write(self):
        scope = self.make_scope(6)
        plain = sqlite3.connect(self.db_path)
        self.addCleanup(plain.close)
        with self.assertRaises(RuntimeError):
            self.assert_untouched(lambda: self.start(self.student, scope, conn=plain))
        no_foreign_keys = web_db.connect_db(self.db_path)
        self.addCleanup(no_foreign_keys.close)
        no_foreign_keys.execute("PRAGMA foreign_keys=OFF")
        with self.assertRaises(RuntimeError):
            self.assert_untouched(lambda: self.start(self.student, scope, conn=no_foreign_keys))

    def test_a_caller_transaction_is_never_ended_by_the_service(self):
        scope = self.make_scope(6)
        self.conn.execute("INSERT INTO curriculum_topics (topic_slug, title_bg) VALUES ('caller-pending', 'x')")
        self.assertTrue(self.conn.in_transaction)
        with self.assertRaises(RuntimeError):
            self.start(self.student, scope)
        self.assertTrue(self.conn.in_transaction)       # still the caller's, not rolled back
        self.conn.commit()
        self.assertEqual(self.raw().execute(
            "SELECT COUNT(*) FROM curriculum_topics WHERE topic_slug = 'caller-pending'").fetchone()[0], 1)
        self.assertEqual(self.sessions(), [])


class AccountTest(PracticeCase):
    def test_inactive_and_missing_accounts_get_no_session_and_no_write(self):
        scope = self.make_scope(6)
        inactive = self.add_account("student", active=False)
        self.conn.commit()
        missing = account_principal(999999, "student", "x")
        for name, who in (("inactive", inactive), ("missing", missing)):
            with self.subTest(name):
                result = self.assert_untouched(lambda: self.start(who, scope))
                self.assertEqual(result, practice.PracticeStart(practice.OUTCOME_ACCOUNT_UNAVAILABLE))
        self.assertEqual(self.sessions(), [])

    def test_an_account_deactivated_after_the_principal_was_loaded_is_rechecked_under_the_lock(self):
        scope = self.make_scope(6)
        session_id = self.start(self.student, scope).session_id
        self.conn.execute("UPDATE users SET is_active = 0 WHERE id = ?", (self.student.user_id,))
        self.conn.commit()
        before = self.practice_digest()
        result = self.start(self.student, scope)       # the stale principal still says "student"
        self.assertEqual(result.outcome, practice.OUTCOME_ACCOUNT_UNAVAILABLE)
        self.assertEqual(before, self.practice_digest())
        self.assertEqual(self.sessions()[0]["id"], session_id)

    def test_every_role_owns_only_its_own_sessions_independently(self):
        scope = self.make_scope(6)
        results = {who.role + str(who.user_id): self.start(who, scope)
                   for who in (self.student, self.other_student, self.teacher, self.admin)}
        self.assertTrue(all(result.created for result in results.values()))
        rows = self.sessions()
        self.assertEqual(sorted(row["user_id"] for row in rows),
                         sorted(who.user_id for who in (self.student, self.other_student, self.teacher, self.admin)))
        self.assertEqual(len({row["id"] for row in rows}), 4)
        # Each caller resumes its own row, never another account's.
        for who in (self.student, self.other_student, self.teacher, self.admin):
            again = self.start(who, scope)
            self.assertTrue(again.resumed)
            owner = [row for row in rows if row["id"] == again.session_id][0]["user_id"]
            self.assertEqual(owner, who.user_id)

    def test_another_accounts_open_session_is_not_adopted_or_touched(self):
        scope = self.make_scope(6)
        theirs = self.insert_session(self.other_student, scope, scope.all_question_ids[:3])
        before = [row for row in self.sessions() if row["id"] == theirs][0]
        result = self.start(self.student, scope)
        self.assertTrue(result.created)
        self.assertNotEqual(result.session_id, theirs)
        self.assertEqual(before, [row for row in self.sessions() if row["id"] == theirs][0])

    def test_the_abandon_statement_itself_never_matches_another_owner_or_a_closed_session(self):
        scope = self.make_scope(6)
        theirs = self.insert_session(self.other_student, scope, scope.all_question_ids[:3])
        before = self.practice_digest()
        with self.assertRaises(practice.PracticeStateError):
            practice._abandon(self.conn, theirs, self.student.user_id, STAMP)
        self.assertEqual(before, self.practice_digest())
        submitted = self.insert_session(self.admin, scope, scope.all_question_ids[:3], submitted=STAMP)
        before = self.practice_digest()
        with self.assertRaises(practice.PracticeStateError):
            practice._abandon(self.conn, submitted, self.admin.user_id, STAMP)
        self.assertEqual(before, self.practice_digest())
        # Negative control: for its real owner and an open row the same statement does abandon.
        practice._abandon(self.conn, theirs, self.other_student.user_id, STAMP)
        self.conn.commit()
        self.assertEqual([r for r in self.sessions() if r["id"] == theirs][0]["abandoned_at"], STAMP)


# ====================================================================== scope
class ScopeTest(PracticeCase):
    def assert_scope_unavailable(self, scope_or_id, topic_id=None):
        result = self.assert_untouched(lambda: self.start(self.student, scope_or_id, topic_id))
        self.assertEqual(result, practice.PracticeStart(practice.OUTCOME_SCOPE_UNAVAILABLE))

    def test_a_missing_section_is_unavailable(self):
        self.assert_scope_unavailable(987654)

    def test_a_section_without_a_known_subject_is_unavailable_however_rich_its_pool(self):
        scope = self.make_scope(8, subject="astrology")
        self.assert_scope_unavailable(scope)
        orphan = add_section(self.conn, None, f"orphan-{self.next()}", grade=8)
        add_membership(self.conn, scope.topic_ids[0], orphan)
        self.conn.commit()
        self.assert_scope_unavailable(orphan)
        # Negative control: the same content under a known subject is a usable scope.
        self.assertTrue(self.start(self.student, self.make_scope(8)).created)

    def test_dzi_training_sections_are_unavailable_of_any_subject(self):
        for subject in ("it", "physics"):
            with self.subTest(subject):
                scope = self.make_scope(8, subject=subject, section_type="dzi_training")
                self.assert_scope_unavailable(scope)
                # Negative control: the same content in an ordinary section is practicable.
                self.assertTrue(self.start(self.student, self.make_scope(8, subject=subject)).created)

    def test_a_dzi_training_section_does_not_resume_or_abandon_an_open_session(self):
        scope = self.make_scope(8, section_type="dzi_training")
        self.insert_session(self.student, scope, scope.all_question_ids[:3])
        self.assert_scope_unavailable(scope)

    def test_a_topic_must_be_a_member_of_the_section(self):
        scope = self.make_scope(6, topics=2)
        other = self.make_scope(6)
        self.assert_scope_unavailable(scope, other.topic_ids[0])      # another section's topic
        self.assert_scope_unavailable(scope, 987654)                   # no such topic
        # Negative controls: its own topics, and a second membership, are members.
        self.assertTrue(self.start(self.student, scope, scope.topic_ids[0]).created)
        add_membership(self.conn, other.topic_ids[0], scope.section_id)
        self.conn.commit()
        self.assertTrue(self.start(self.other_student, scope, other.topic_ids[0]).created)

    def test_a_topic_with_a_conflicting_subject_is_unavailable(self):
        scope = self.make_scope(6)
        physics = self.make_scope(6, subject="physics")
        add_membership(self.conn, physics.topic_ids[0], scope.section_id)   # cross-subject membership
        self.conn.commit()
        self.assert_scope_unavailable(scope, physics.topic_ids[0])

    def test_a_topic_session_draws_only_from_its_topic(self):
        scope = self.make_scope(6, topics=2)
        first = self.start(self.student, scope, scope.topic_ids[0])
        second = self.start(self.student, scope, scope.topic_ids[1])
        whole = self.start(self.student, scope)
        self.assertTrue(first.created and second.created and whole.created)
        self.assertEqual(set(first.question_ids), set(scope.question_ids[0]))
        self.assertEqual(set(second.question_ids), set(scope.question_ids[1]))
        self.assertLessEqual(set(whole.question_ids), set(scope.all_question_ids))
        self.assertEqual(len({first.session_id, second.session_id, whole.session_id}), 3)

    def test_a_thin_topic_borrows_nothing_from_its_sibling_or_section(self):
        n = self.next()
        scope = self.make_scope(0, topics=0)
        thin = add_topic(self.conn, scope.section_id, f"thin-{n}")
        rich = add_topic(self.conn, scope.section_id, f"rich-{n}")
        for _ in range(4):
            self.good_question(thin)
        for _ in range(20):
            self.good_question(rich)
        self.conn.commit()
        self.assertEqual(self.start(self.student, scope.section_id, thin).outcome, practice.OUTCOME_EMPTY_POOL)
        self.assertEqual(self.sessions(), [])
        self.assertTrue(self.start(self.student, scope.section_id, rich).created)    # control
        self.assertTrue(self.start(self.student, scope.section_id).created)          # section-wide: 24

    def test_no_borrowing_across_sections_or_subjects(self):
        scope = self.make_scope(4)
        self.make_scope(30)                                    # a rich neighbour section
        self.make_scope(30, subject="physics")                 # and another subject
        self.assertEqual(self.start(self.student, scope).outcome, practice.OUTCOME_EMPTY_POOL)
        # A physics-owned question placed under an IT topic is not the IT section's.
        foreign = self.good_question(scope.topic_ids[0], "physics")
        self.conn.commit()
        self.assertEqual(self.start(self.student, scope).outcome, practice.OUTCOME_EMPTY_POOL)
        self.conn.execute("UPDATE questions SET subject = 'it' WHERE id = ?", (foreign,))   # control
        self.conn.commit()
        self.assertTrue(self.start(self.student, scope).created)


# ====================================================================== eligibility
class EligibilityTest(PracticeCase):
    """Each case: four good questions plus one candidate. Bad -> below the minimum; good -> created."""

    def candidates(self):
        existing_image = self.directory / "figure.png"
        existing_image.write_bytes(b"not-a-real-png-but-it-exists")

        def ai(quality):
            def build(topic, subject="it"):
                qid = self.good_question(topic, subject)
                self.conn.execute("UPDATE questions SET is_ai_generated = 1, quality_score = ? WHERE id = ?",
                                  (quality, qid))
                return qid
            return build

        def options(mutator):
            def build(topic, subject="it"):
                qid = self.good_question(topic, subject)
                mutator(qid)
                return qid
            return build

        def delete_option(qid):
            self.conn.execute("DELETE FROM multiple_choice_options WHERE question_id = ? AND option_letter = 'D'", (qid,))

        def second_correct(qid):
            self.conn.execute("UPDATE multiple_choice_options SET is_correct = 1 WHERE question_id = ?", (qid,))

        def blank_option(qid):
            self.conn.execute("UPDATE multiple_choice_options SET option_text = '-' WHERE question_id = ? "
                              "AND option_letter = 'B'", (qid,))

        def no_correct(qid):
            self.conn.execute("UPDATE multiple_choice_options SET is_correct = 0 WHERE question_id = ?", (qid,))

        def visual(image_path):
            def build(topic, subject="it"):
                qid = self.good_question(topic, subject, prompt="Разгледайте графиката и отговорете?")
                self.conn.execute("UPDATE questions SET image_path = ? WHERE id = ?", (image_path, qid))
                return qid
            return build

        def kind(question_type):
            def build(topic, subject="it"):
                if question_type == "multiple_choice":
                    return self.good_question(topic, subject)
                return add_question(self.conn, topic, subject, self.next(), source="practice-synthetic",
                                    kind=question_type, accepted_answer="42")
            return build

        def dzi(maker, level="DZI"):
            def build(topic, subject="it"):
                qid = self.good_question(topic, subject)
                maker(qid) if maker != self.make_dzi_legacy else maker(qid, level)
                return qid
            return build

        def foreign_subject(subject_value):
            def build(topic, subject="it"):
                qid = self.good_question(topic, subject)
                self.conn.execute("UPDATE questions SET subject = ? WHERE id = ?", (subject_value, qid))
                return qid
            return build

        return {
            "unapproved AI question": (ai(0.4), ai(1.0)),
            "fill-in instead of multiple choice": (kind("fill_in"), kind("multiple_choice")),
            "three options": (options(delete_option), options(lambda qid: None)),
            "two correct options": (options(second_correct), options(lambda qid: None)),
            "no correct option": (options(no_correct), options(lambda qid: None)),
            "blank option text": (options(blank_option), options(lambda qid: None)),
            "visual prompt without an image": (visual(None), visual(str(existing_image))),
            "visual prompt with a missing image file": (visual(str(self.directory / "missing.png")),
                                                         visual(str(existing_image))),
            "question owned by another subject": (foreign_subject("physics"), foreign_subject("it")),
            "official DZI question (legacy exam)": (dzi(self.make_dzi_legacy), dzi(self.make_dzi_legacy, "NVO_7")),
            "official DZI question (task link)": (dzi(self.make_dzi_task), options(lambda qid: None)),
        }

    def test_each_exclusion_is_what_keeps_the_pool_below_the_minimum(self):
        for name, (bad, good) in self.candidates().items():
            with self.subTest(name):
                scope = self.make_scope(4)
                bad(scope.topic_ids[0])
                self.conn.commit()
                self.assertEqual(self.start(self.student, scope).outcome, practice.OUTCOME_EMPTY_POOL, name)
                control = self.make_scope(4)
                good(control.topic_ids[0])
                self.conn.commit()
                created = self.start(self.student, control)
                self.assertTrue(created.created, name)
                self.assertEqual(len(created.question_ids), 5, name)

    def test_the_dzi_branches_agree_with_the_existing_predicate_and_ai_rows_are_not_dzi(self):
        scope = self.make_scope(4)
        legacy, task = self.good_question(scope.topic_ids[0]), self.good_question(scope.topic_ids[0])
        ai_in_dzi_exam = self.good_question(scope.topic_ids[0])
        self.make_dzi_legacy(legacy)
        self.make_dzi_task(task)
        self.make_dzi_legacy(ai_in_dzi_exam)
        self.conn.execute("UPDATE questions SET is_ai_generated = 1, quality_score = 1.0 WHERE id = ?", (ai_in_dzi_exam,))
        self.conn.commit()
        self.assertTrue(is_dzi_question(self.conn, legacy))
        self.assertTrue(is_dzi_question(self.conn, task))
        self.assertFalse(is_dzi_question(self.conn, ai_in_dzi_exam))
        created = self.start(self.student, scope)
        self.assertTrue(created.created)
        self.assertEqual(set(created.question_ids), set(scope.all_question_ids) | {ai_in_dzi_exam})

    def test_the_pool_is_the_existing_selection_minus_dzi_with_no_other_rule(self):
        """On every canonical section: batched helper ids, minus the DZI predicate (and topic filter)."""
        conn = self.conn
        compared = 0
        for scope in AssessmentScope.for_all_sections(conn).values():
            ids = quiz_section_question_ids(conn, scope.section_id, scope=scope)
            dzi_ids = {row[0] for row in conn.execute(
                f"SELECT q.id FROM questions q WHERE q.id IN ({','.join(str(i) for i in ids) or 'NULL'}) "
                f"AND {dzi_question_sql('q')}")}
            self.assertEqual(practice._eligible_question_ids(conn, scope, None),
                             [qid for qid in ids if qid not in dzi_ids], scope.section_id)
            compared += 1
        self.assertGreater(compared, 60)


# ====================================================================== resume, stale, repeat
class ResumeTest(PracticeCase):
    def test_resume_returns_the_same_session_and_writes_nothing(self):
        scope = self.make_scope(12)
        first = self.start(self.student, scope)
        before = self.practice_digest()
        again = self.assert_untouched(lambda: self.start(self.student, scope))
        self.assertTrue(again.resumed)
        self.assertEqual((again.session_id, again.question_ids, again.seed),
                         (first.session_id, first.question_ids, first.seed))
        self.assertEqual(before, self.practice_digest())

    def test_a_valid_session_resumes_even_when_the_pool_for_a_new_one_is_thin(self):
        scope = self.make_scope(6)
        first = self.start(self.student, scope)
        for qid in first.question_ids[:3]:                 # three of six remain: below the minimum
            self.break_question(qid)
        self.conn.commit()
        self.assertEqual(self.start(self.other_student, scope).outcome, practice.OUTCOME_EMPTY_POOL)
        before = self.practice_digest()
        again = self.start(self.student, scope)
        self.assertTrue(again.resumed)
        self.assertEqual(again.session_id, first.session_id)
        self.assertEqual(again.question_ids, first.question_ids[3:])    # stored order, renderable only
        self.assertEqual(before, self.practice_digest())

    def test_a_session_with_fewer_than_five_stored_items_resumes(self):
        scope = self.make_scope(8)
        session_id = self.insert_session(self.student, scope, scope.all_question_ids[:2], seed="two-items")
        again = self.start(self.student, scope)
        self.assertEqual((again.outcome, again.session_id, again.question_ids, again.seed),
                         (practice.OUTCOME_RESUMED, session_id, tuple(scope.all_question_ids[:2]), "two-items"))

    def test_the_stored_plan_is_never_rewritten_by_a_resume(self):
        scope = self.make_scope(8)
        plan = scope.all_question_ids[:6]
        session_id = self.insert_session(self.student, scope, plan)
        self.break_question(plan[1])
        self.conn.commit()
        result = self.start(self.student, scope)
        self.assertTrue(result.resumed)
        self.assertEqual(result.question_ids, tuple(plan[:1] + plan[2:]))
        self.assertEqual(json.loads(self.sessions()[0]["question_ids_json"]), plan)
        self.assertEqual(self.sessions()[0]["id"], session_id)

    def test_content_moved_out_of_scope_or_into_dzi_is_not_presented_though_still_renderable(self):
        scope = self.make_scope(12, topics=2)
        plan = scope.question_ids[0][:3] + scope.question_ids[1][:1]
        old = self.insert_session(self.student, scope, plan, topic_id=scope.topic_ids[0])
        elsewhere = self.make_scope(1)
        self.conn.execute("UPDATE questions SET topic_id = ? WHERE id = ?", (elsewhere.topic_ids[0], plan[0]))
        self.make_dzi_legacy(plan[1])
        self.conn.execute("UPDATE questions SET is_ai_generated = 1, quality_score = 0.2 WHERE id = ?", (plan[2],))
        self.conn.commit()
        renderable, _ = filter_renderable_attempt_question_ids(self.conn, plan)
        self.assertEqual(renderable, plan)       # the display check alone keeps all four: it is not scope
        result = self.start(self.student, scope, scope.topic_ids[0])
        # plan[3] belongs to the other topic: not in this topic's pool. Nothing of the plan is in scope any
        # more, so the session is stale and replaced from the topic's remaining nine.
        self.assertEqual((result.outcome, result.abandoned_session_id), (practice.OUTCOME_CREATED, old))
        self.assertTrue(set(result.question_ids) <= set(scope.question_ids[0][3:]))

    def test_the_intersection_keeps_the_items_still_in_scope(self):
        scope = self.make_scope(8)
        plan = scope.all_question_ids[:5]
        session_id = self.insert_session(self.student, scope, plan)
        elsewhere = self.make_scope(1)
        self.conn.execute("UPDATE questions SET topic_id = ? WHERE id = ?", (elsewhere.topic_ids[0], plan[0]))
        self.make_dzi_task(plan[2])
        self.conn.commit()
        result = self.start(self.student, scope)
        self.assertEqual((result.outcome, result.session_id), (practice.OUTCOME_RESUMED, session_id))
        self.assertEqual(result.question_ids, (plan[1], plan[3], plan[4]))

    def test_a_stale_session_is_abandoned_and_replaced_with_a_sufficient_pool(self):
        scope = self.make_scope(8)
        old_plan = scope.all_question_ids[:3]
        old = self.insert_session(self.student, scope, old_plan, seed="old-seed")
        for qid in old_plan:
            self.break_question(qid)
        self.conn.commit()
        before = [row for row in self.sessions() if row["id"] == old][0]
        result = self.start(self.student, scope)
        self.assertEqual(result.outcome, practice.OUTCOME_CREATED)
        self.assertEqual(result.abandoned_session_id, old)
        self.assertNotEqual(result.session_id, old)
        rows = {row["id"]: row for row in self.sessions()}
        self.assertRegex(rows[old]["abandoned_at"], r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d$")
        self.assertEqual({k: v for k, v in rows[old].items() if k != "abandoned_at"},
                         {k: v for k, v in before.items() if k != "abandoned_at"})   # nothing else changed
        self.assertIsNone(rows[old]["submitted_at"])
        self.assertIsNone(rows[old]["score_total"])
        self.assertEqual(rows[result.session_id]["started_at"], rows[old]["abandoned_at"])   # one stamp
        self.assertEqual(len([r for r in rows.values() if r["abandoned_at"] is None and r["submitted_at"] is None]), 1)
        self.assertEqual(set(result.question_ids), set(scope.all_question_ids[3:]))
        self.assertTrue(set(old_plan).isdisjoint(result.question_ids))

    def test_an_insufficient_replacement_commits_the_abandonment_alone(self):
        scope = self.make_scope(6)
        plan = scope.all_question_ids[:3]
        old = self.insert_session(self.student, scope, plan)
        for qid in plan:                                  # every stored item is unrenderable; 3 of 6 remain
            self.break_question(qid)
        self.conn.commit()
        result = self.start(self.student, scope)
        self.assertEqual(result.outcome, practice.OUTCOME_EMPTY_POOL)
        self.assertEqual(result.abandoned_session_id, old)
        self.assertIsNone(result.session_id)
        rows = self.sessions()
        self.assertEqual(len(rows), 1)
        self.assertIsNotNone(rows[0]["abandoned_at"])
        self.assertFalse(self.conn.in_transaction)
        # The abandonment is committed and durable: a second call finds nothing to abandon, writes nothing.
        before = self.practice_digest()
        again = self.start(self.student, scope)
        self.assertEqual(again, practice.PracticeStart(practice.OUTCOME_EMPTY_POOL))
        self.assertEqual(before, self.practice_digest())

    def test_an_empty_array_plan_is_well_formed_and_stale(self):
        scope = self.make_scope(6)
        old = self.insert_session(self.student, scope, "[]")
        result = self.start(self.student, scope)
        self.assertEqual((result.outcome, result.abandoned_session_id), (practice.OUTCOME_CREATED, old))

    def test_a_plan_of_deleted_questions_is_stale_not_malformed(self):
        scope = self.make_scope(6)
        old = self.insert_session(self.student, scope, [8_000_001, 8_000_002])
        result = self.start(self.student, scope)
        self.assertEqual((result.outcome, result.abandoned_session_id), (practice.OUTCOME_CREATED, old))

    def test_a_repeat_after_submission_is_a_fresh_session_and_sample(self):
        scope = self.make_scope(25)
        with self.seeds("first-seed", "second-seed"):
            first = self.start(self.student, scope)
            self.conn.execute(
                "UPDATE practice_sessions SET submitted_at = ?, score_correct = 3, score_total = 10 WHERE id = ?",
                (STAMP, first.session_id))
            self.conn.commit()
            submitted = [row for row in self.sessions() if row["id"] == first.session_id][0]
            second = self.start(self.student, scope)
        self.assertTrue(second.created)
        self.assertIsNone(second.abandoned_session_id)
        self.assertNotEqual(second.session_id, first.session_id)
        self.assertEqual((first.seed, second.seed), ("first-seed", "second-seed"))
        self.assertNotEqual(first.question_ids, second.question_ids)
        self.assertEqual(submitted, [row for row in self.sessions() if row["id"] == first.session_id][0])
        self.assertTrue(self.start(self.student, scope).resumed)

    def test_an_abandoned_session_is_followed_by_a_fresh_one_and_a_submitted_one_never_blocks_a_thin_pool(self):
        scope = self.make_scope(6)
        self.insert_session(self.student, scope, scope.all_question_ids[:3], abandoned=STAMP)
        self.assertTrue(self.start(self.student, scope).created)
        thin = self.make_scope(4)
        self.insert_session(self.student, thin, thin.all_question_ids[:3], submitted=STAMP)
        before = self.practice_digest()
        self.assertEqual(self.start(self.student, thin), practice.PracticeStart(practice.OUTCOME_EMPTY_POOL))
        self.assertEqual(before, self.practice_digest())

    def test_topic_and_section_sessions_are_independent_scopes(self):
        scope = self.make_scope(6)
        whole = self.start(self.student, scope)
        by_topic = self.start(self.student, scope, scope.topic_ids[0])
        self.assertNotEqual(whole.session_id, by_topic.session_id)
        self.assertTrue(self.start(self.student, scope).resumed)
        self.assertTrue(self.start(self.student, scope, scope.topic_ids[0]).resumed)
        self.assertEqual(self.start(self.student, scope).session_id, whole.session_id)


# ====================================================================== malformed plans
class MalformedPlanTest(PracticeCase):
    MALFORMED = {
        "not JSON": "not json",
        "empty text": "",
        "an object (the quiz plan shape)": '{"question_ids": [1, 2, 3]}',
        "a string": '"abc"',
        "null": "null",
        "a number": "7",
        "a string element": '[1, "2"]',
        "a boolean element": "[true, 2]",
        "a float element": "[1.0, 2]",
        "a null element": "[null]",
        "a nested array": "[[1]]",
        "a zero id": "[0, 3]",
        "a negative id": "[-1]",
        "a duplicate": "[7, 7]",
        "too many ids": json.dumps(list(range(1, 12))),
        "an id above SQLite's range": "[9223372036854775808]",
        "NaN": "[NaN]",
        "deeply nested JSON": "[" * 200_000,
    }

    def test_every_malformed_plan_fails_closed_without_a_write(self):
        scope = self.make_scope(12)                       # a rich pool: it must not be read as "empty"
        for name, raw in self.MALFORMED.items():
            with self.subTest(name):
                with self.conn:
                    self.conn.execute("DELETE FROM practice_sessions")
                session_id = self.insert_session(self.student, scope, raw, seed="keep-me")
                with self.assertRaises(practice.PracticePlanError) as raised:
                    self.assert_untouched(lambda: self.start(self.student, scope))
                self.assertEqual(raised.exception.session_id, session_id)
                row = self.sessions()[0]
                self.assertEqual((row["question_ids_json"], row["seed"], row["abandoned_at"], row["submitted_at"]),
                                 (raw, "keep-me", None, None))
                self.assertEqual(len(self.sessions()), 1)
                # The writer lock is released: another connection can take it at once.
                probe = sqlite3.connect(self.db_path, timeout=0)
                probe.execute("BEGIN IMMEDIATE")
                probe.rollback()
                probe.close()

    def test_a_malformed_plan_of_one_account_does_not_affect_another_account_or_scope(self):
        scope = self.make_scope(12)
        self.insert_session(self.student, scope, "garbage")
        self.assertTrue(self.start(self.other_student, scope).created)
        other_scope = self.make_scope(12)
        self.assertTrue(self.start(self.student, other_scope).created)
        self.assertTrue(self.start(self.student, scope, scope.topic_ids[0]).created)   # another scope key
        with self.assertRaises(practice.PracticePlanError):
            self.start(self.student, scope)

    def test_the_malformed_and_the_merely_stale_are_different_outcomes(self):
        for raw, expect_error in (("[8000001]", False), ("[]", False), ("[8000001,", True), ("[8000001, 8000001]", True)):
            with self.subTest(raw):
                scope = self.make_scope(6)
                self.insert_session(self.student, scope, raw)
                if expect_error:
                    with self.assertRaises(practice.PracticePlanError):
                        self.start(self.student, scope)
                else:
                    self.assertTrue(self.start(self.student, scope).created)

    def test_two_open_rows_for_one_scope_are_refused_when_the_index_is_absent(self):
        scope = self.make_scope(6)
        self.conn.execute("DROP INDEX uniq_practice_sessions_open_scope")
        self.insert_session(self.student, scope, scope.all_question_ids[:3])
        self.insert_session(self.student, scope, scope.all_question_ids[3:6])
        with self.assertRaises(practice.PracticeStateError):
            self.assert_untouched(lambda: self.start(self.student, scope))


# ====================================================================== transactions
class TransactionTest(PracticeCase):
    def test_the_timestamp_is_sampled_while_the_writer_lock_is_held(self):
        scope = self.make_scope(6)
        probe = sqlite3.connect(self.db_path, timeout=0)
        self.addCleanup(probe.close)
        probe.execute("BEGIN IMMEDIATE")                   # control: with no start running the lock is free
        probe.rollback()
        sampled = []

        def sample():
            with self.assertRaises(sqlite3.OperationalError):
                probe.execute("BEGIN IMMEDIATE")           # the service holds the writer lock right now
            sampled.append(self.conn.in_transaction)
            return datetime(2026, 11, 20, 12, 0, 0, 999999)

        with patch.object(lifecycle, "naive_utc_now", side_effect=sample):
            result = self.start(self.student, scope)
        self.assertEqual(sampled, [True])
        self.assertEqual(self.sessions()[0]["started_at"], "2026-11-20 12:00:00")
        self.assertTrue(result.created)
        self.assertFalse(self.conn.in_transaction)

    def test_a_failed_insert_rolls_back_the_abandonment_and_leaves_the_rows(self):
        scope = self.make_scope(8)
        old = self.insert_session(self.student, scope, [8_000_001])
        before = self.practice_digest()
        self.conn.execute("CREATE TRIGGER fail_insert BEFORE INSERT ON practice_sessions "
                          "BEGIN SELECT RAISE(ABORT, 'insert refused'); END")
        self.conn.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            self.start(self.student, scope)
        self.assertEqual(before, self.practice_digest())
        self.assertIsNone(self.sessions()[0]["abandoned_at"])         # the abandonment did not survive
        self.assertFalse(self.conn.in_transaction)
        # Negative control: without the trigger the very same call abandons and replaces.
        self.conn.execute("DROP TRIGGER fail_insert")
        self.conn.commit()
        result = self.start(self.student, scope)
        self.assertEqual((result.outcome, result.abandoned_session_id), (practice.OUTCOME_CREATED, old))

    def test_an_abandon_that_changes_no_row_is_an_error_not_a_silent_success(self):
        scope = self.make_scope(8)
        self.insert_session(self.student, scope, [8_000_001])
        self.conn.execute("CREATE TRIGGER ignore_update BEFORE UPDATE ON practice_sessions "
                          "BEGIN SELECT RAISE(IGNORE); END")
        self.conn.commit()
        before = self.practice_digest()
        with self.assertRaises(practice.PracticeStateError):
            self.assert_untouched(lambda: self.start(self.student, scope))
        self.assertEqual(before, self.practice_digest())
        self.conn.execute("DROP TRIGGER ignore_update")                # control: the same call works
        self.conn.commit()
        self.assertTrue(self.start(self.student, scope).created)

    def test_unexpected_database_errors_propagate_and_release_the_lock(self):
        scope = self.make_scope(6)
        self.conn.execute("ALTER TABLE practice_sessions RENAME TO practice_sessions_moved")
        self.conn.commit()
        with self.assertRaises(sqlite3.OperationalError):
            self.start(self.student, scope)
        self.assertFalse(self.conn.in_transaction)
        probe = sqlite3.connect(self.db_path, timeout=0)
        self.addCleanup(probe.close)
        probe.execute("BEGIN IMMEDIATE")
        probe.rollback()

    def test_an_error_raised_by_a_helper_is_not_swallowed(self):
        scope = self.make_scope(6)
        with patch.object(practice, "_eligible_question_ids", side_effect=sqlite3.DatabaseError("disk I/O error")):
            with self.assertRaises(sqlite3.DatabaseError):
                self.assert_untouched(lambda: self.start(self.student, scope))

    def test_a_non_success_outcome_leaves_no_transaction_open(self):
        scope = self.make_scope(2)
        for who, target in ((self.student, scope), (self.student, 987654)):
            self.assertFalse(self.start(who, target).created)
            self.assertFalse(self.conn.in_transaction)


class IsolationFromTheTeacherLifecycleTest(PracticeCase):
    def test_nothing_outside_practice_sessions_changes_and_no_lifecycle_or_quota_code_runs(self):
        assignment = runtime_fixtures.add_assignment(self.conn, question_plan=None)
        attempt = runtime_fixtures.add_attempt(self.conn, assignment, user_id=self.student.user_id)
        runtime_fixtures.add_answer(self.conn, attempt, runtime_fixtures.first_multiple_choice_question_ids(self.conn, 1)[0])
        self.conn.commit()
        set_lifecycle_clock_row(self.db_path, high_water=datetime(2026, 11, 20, 9, 0, 0), state="suspect",
                                reason="clock_step", changed_at=datetime(2026, 11, 20, 9, 0, 0))
        scope = self.make_scope(12)
        stale = self.insert_session(self.other_student, scope, [8_000_001])
        before = self.table_digests(skip={"practice_sessions"})
        forbidden = AssertionError("the teacher lifecycle or quota was used")
        with patch.object(lifecycle, "write_transaction", side_effect=forbidden), \
                patch.object(lifecycle, "_begin_write", side_effect=forbidden), \
                patch.object(lifecycle, "wall_reading", side_effect=forbidden), \
                patch.object(lifecycle, "read_clock_row", side_effect=forbidden), \
                patch.object(assignment_quota, "enforce", side_effect=forbidden), \
                patch.object(assignment_quota, "record", side_effect=forbidden), \
                patch.object(lifecycle, "count_unclosed_owned", side_effect=forbidden):
            created = self.start(self.student, scope)                    # new
            resumed = self.start(self.student, scope)                    # resume
            replaced = self.start(self.other_student, scope)             # stale replacement
        self.assertEqual((created.outcome, resumed.outcome, replaced.outcome),
                         (practice.OUTCOME_CREATED, practice.OUTCOME_RESUMED, practice.OUTCOME_CREATED))
        self.assertEqual(replaced.abandoned_session_id, stale)
        self.assertEqual(before, self.table_digests(skip={"practice_sessions"}))   # incl. a suspect clock row, untouched
        with self.raw() as conn:
            self.assertEqual(conn.execute("SELECT state, high_water_at FROM lifecycle_clock").fetchone()[:],
                             ("suspect", "2026-11-20 09:00:00"))


# ====================================================================== autocommit connections
@unittest.skipUnless(hasattr(sqlite3.Connection, "autocommit"), "needs Connection.autocommit (Python 3.12+)")
class AutocommitConnectionTest(PracticeCase):
    """Under ``autocommit=True`` ``Connection.commit()``/``rollback()`` do nothing, even after a manual
    ``BEGIN``: the service must end its own transaction with explicit SQL or the lock stays held."""

    def autocommit_connection(self):
        conn = sqlite3.connect(self.db_path, timeout=30, autocommit=True)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        self.addCleanup(conn.close)
        return conn

    def assert_lock_free(self):
        probe = sqlite3.connect(self.db_path, timeout=0)
        self.addCleanup(probe.close)
        probe.execute("BEGIN IMMEDIATE")
        probe.rollback()

    def test_the_premise_commit_and_rollback_are_no_ops_in_this_mode(self):
        conn = self.autocommit_connection()
        conn.execute("BEGIN IMMEDIATE")
        conn.commit()
        conn.rollback()
        self.assertTrue(conn.in_transaction)              # what the service must not rely on
        conn.execute("ROLLBACK")
        self.assertFalse(conn.in_transaction)

    def test_every_outcome_ends_the_transaction_and_a_write_is_committed(self):
        conn = self.autocommit_connection()
        rich, thin = self.make_scope(12), self.make_scope(3)
        stale = self.make_scope(8)
        old = self.insert_session(self.other_student, stale, [8_000_001])
        inactive = self.add_account("student", active=False)
        self.conn.commit()
        cases = {
            "created": (self.student, rich, practice.OUTCOME_CREATED),
            "resumed": (self.student, rich, practice.OUTCOME_RESUMED),
            "empty pool": (self.student, thin, practice.OUTCOME_EMPTY_POOL),
            "scope unavailable": (self.student, 987654, practice.OUTCOME_SCOPE_UNAVAILABLE),
            "account unavailable": (inactive, rich, practice.OUTCOME_ACCOUNT_UNAVAILABLE),
            "stale replaced": (self.other_student, stale, practice.OUTCOME_CREATED),
        }
        for name, (who, scope, expected) in cases.items():
            with self.subTest(name):
                result = self.start(who, scope, conn=conn)
                self.assertEqual(result.outcome, expected)
                self.assertFalse(conn.in_transaction)
                self.assert_lock_free()
        # The writes are durable: visible to another connection with nothing left to commit.
        rows = {row["id"]: row for row in self.sessions()}
        self.assertIn(self.start(self.student, rich).session_id, rows)
        self.assertIsNotNone(rows[old]["abandoned_at"])
        self.assertEqual(len(self.sessions()), 3)         # student/rich, other/stale (old), its replacement

    def test_a_failure_rolls_back_and_releases_the_lock(self):
        conn = self.autocommit_connection()
        scope = self.make_scope(8)
        old = self.insert_session(self.student, scope, [8_000_001])
        self.conn.execute("CREATE TRIGGER fail_insert BEFORE INSERT ON practice_sessions "
                          "BEGIN SELECT RAISE(ABORT, 'insert refused'); END")
        self.conn.commit()
        before = self.practice_digest()
        with self.assertRaises(sqlite3.IntegrityError):
            self.start(self.student, scope, conn=conn)
        self.assertFalse(conn.in_transaction)
        self.assert_lock_free()
        self.assertEqual(before, self.practice_digest())
        self.assertIsNone([r for r in self.sessions() if r["id"] == old][0]["abandoned_at"])

    def test_a_caller_owned_transaction_is_still_refused_and_left_alone(self):
        conn = self.autocommit_connection()
        scope = self.make_scope(6)
        conn.execute("BEGIN")
        with self.assertRaises(RuntimeError):
            self.start(self.student, scope, conn=conn)
        self.assertTrue(conn.in_transaction)              # the caller's transaction, untouched
        conn.execute("ROLLBACK")
        self.assertEqual(self.sessions(), [])


# ====================================================================== concurrency
class ConcurrencyTest(PracticeCase):
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
            self.assertFalse(thread.is_alive(), "a start did not finish")
        return outcomes

    def open_rows(self, who=None):
        return [row for row in self.sessions()
                if row["submitted_at"] is None and row["abandoned_at"] is None
                and (who is None or row["user_id"] == who.user_id)]

    def assert_one_session(self, outcomes, who, expected_rows=1):
        for outcome in outcomes:
            self.assertIsInstance(outcome, practice.PracticeStart, outcome)
        self.assertEqual(sorted(o.outcome for o in outcomes), [practice.OUTCOME_CREATED, practice.OUTCOME_RESUMED])
        self.assertEqual(len({o.session_id for o in outcomes}), 1)
        self.assertEqual(outcomes[0].question_ids, outcomes[1].question_ids)
        self.assertEqual(len(self.open_rows(who)), expected_rows)

    def test_two_simultaneous_starts_of_a_section_scope_leave_one_open_session(self):
        scope = self.make_scope(12)
        for _ in range(8):
            who = self.add_account("student")
            self.conn.commit()
            outcomes = self.run_threads([lambda c, w=who: self.start(w, scope, conn=c)] * 2)
            self.assert_one_session(outcomes, who)

    def test_two_simultaneous_starts_of_a_concrete_topic_scope_leave_one_open_session(self):
        scope = self.make_scope(12, topics=2)
        for _ in range(8):
            who = self.add_account("student")
            self.conn.commit()
            outcomes = self.run_threads([lambda c, w=who: self.start(w, scope, scope.topic_ids[1], conn=c)] * 2)
            self.assert_one_session(outcomes, who)
            self.assertEqual(self.open_rows(who)[0]["topic_id"], scope.topic_ids[1])

    def test_the_second_start_waits_for_the_first_and_then_resumes_it(self):
        scope = self.make_scope(12)
        inside, release = threading.Event(), threading.Event()
        calls = []

        def hold_the_lock():
            calls.append(1)
            inside.set()
            self.assertTrue(release.wait(30))
            return "held-seed"

        results = {}

        def run(name, who):
            def job():
                conn = web_db.connect_db(self.db_path, timeout=30)
                try:
                    results[name] = self.start(who, scope, conn=conn)
                except BaseException as exc:
                    results[name] = exc
                finally:
                    conn.close()
            return threading.Thread(target=job, daemon=True)

        with patch.object(practice, "_new_seed", side_effect=hold_the_lock):
            first = run("first", self.student)
            first.start()
            self.assertTrue(inside.wait(30))
            probe = sqlite3.connect(self.db_path, timeout=0)
            self.addCleanup(probe.close)
            with self.assertRaises(sqlite3.OperationalError):        # the lock is held while deciding
                probe.execute("BEGIN IMMEDIATE")
            second = run("second", self.student)
            second.start()
            release.set()
            first.join(60)
            second.join(60)
        self.assertEqual(calls, [1])                                  # only the creator ever drew a sample
        self.assertEqual(results["first"].outcome, practice.OUTCOME_CREATED)
        self.assertEqual(results["second"].outcome, practice.OUTCOME_RESUMED)
        self.assertEqual(results["first"].session_id, results["second"].session_id)
        self.assertEqual(results["first"].seed, "held-seed")
        self.assertEqual(len(self.sessions()), 1)

    def test_a_deferred_transaction_is_the_negative_control_and_does_not_keep_the_contract(self):
        scope = self.make_scope(12)
        rendezvous = threading.Barrier(2, timeout=10)

        def both_have_read_no_open_session():
            rendezvous.wait()
            return "racing-seed"

        def deferred_job(conn_unused):
            conn = sqlite3.connect(self.db_path, timeout=5, factory=DeferredConnection)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys=ON")
            try:
                return practice.start_or_resume_practice(conn, self.student, scope.section_id)
            except BaseException as exc:
                return exc
            finally:
                conn.close()

        with patch.object(practice, "_new_seed", side_effect=both_have_read_no_open_session):
            outcomes = self.run_threads([deferred_job, deferred_job], barrier=False)
        failures = [o for o in outcomes if isinstance(o, BaseException)]
        self.assertTrue(failures, "without BEGIN IMMEDIATE the race must surface as an error")
        self.assertTrue(all(isinstance(f, (sqlite3.OperationalError, sqlite3.IntegrityError)) for f in failures), failures)
        self.assertLessEqual(len(self.open_rows(self.student)), 1)

    def test_other_accounts_and_scopes_stay_independent_under_concurrent_starts(self):
        scope = self.make_scope(12, topics=2)
        other_scope = self.make_scope(12)
        jobs = [
            lambda c: self.start(self.student, scope, conn=c),
            lambda c: self.start(self.student, scope, scope.topic_ids[0], conn=c),
            lambda c: self.start(self.student, other_scope, conn=c),
            lambda c: self.start(self.other_student, scope, conn=c),
            lambda c: self.start(self.teacher, scope, scope.topic_ids[0], conn=c),
            lambda c: self.start(self.admin, scope, conn=c),
        ]
        outcomes = self.run_threads(jobs)
        for outcome in outcomes:
            self.assertIsInstance(outcome, practice.PracticeStart, outcome)
            self.assertTrue(outcome.created)
        self.assertEqual(len({o.session_id for o in outcomes}), 6)
        self.assertEqual(len(self.open_rows()), 6)

    def test_two_concurrent_starts_over_a_stale_session_replace_it_exactly_once(self):
        scope = self.make_scope(12)
        old = self.insert_session(self.student, scope, [8_000_001])
        outcomes = self.run_threads([lambda c: self.start(self.student, scope, conn=c)] * 2)
        self.assert_one_session(outcomes, self.student)
        rows = self.sessions()
        self.assertEqual(len(rows), 2)
        self.assertIsNotNone([row for row in rows if row["id"] == old][0]["abandoned_at"])
        created = [o for o in outcomes if o.created][0]
        self.assertEqual(created.abandoned_session_id, old)
        self.assertIsNone([o for o in outcomes if o.resumed][0].abandoned_session_id)

    def test_two_concurrent_starts_with_a_thin_pool_create_nothing(self):
        scope = self.make_scope(4)
        outcomes = self.run_threads([lambda c: self.start(self.student, scope, conn=c)] * 2)
        self.assertEqual([o.outcome for o in outcomes], [practice.OUTCOME_EMPTY_POOL] * 2)
        self.assertEqual(self.sessions(), [])


# ====================================================================== canonical content
class CanonicalContentTest(PracticeCase):
    def section(self, slug):
        return self.conn.execute("SELECT id FROM curriculum_sections WHERE section_slug = ?", (slug,)).fetchone()[0]

    def test_a_real_it_section_yields_a_non_dzi_in_scope_session(self):
        section = self.section("grade8-computer-systems")
        scope = AssessmentScope(self.conn, section)
        pool = quiz_section_question_ids(self.conn, section, scope=scope)
        dzi_in_pool = [qid for qid in pool if is_dzi_question(self.conn, qid)]
        self.assertTrue(dzi_in_pool, "precondition: this canonical section's quiz pool holds official DZI rows")
        result = self.start(self.student, section)
        self.assertTrue(result.created)
        self.assertEqual(len(result.question_ids), 10)
        self.assertTrue(set(result.question_ids) <= set(pool) - set(dzi_in_pool))
        for qid in result.question_ids:
            self.assertFalse(is_dzi_question(self.conn, qid))
            self.assertTrue(scope.accepts(qid))

    def test_a_real_topic_session_and_the_real_dzi_training_section(self):
        section = self.section("grade8-computer-systems")
        scope = AssessmentScope(self.conn, section)
        pool = [qid for qid in quiz_section_question_ids(self.conn, section, scope=scope)
                if not is_dzi_question(self.conn, qid)]
        by_topic = {}
        for qid in pool:
            by_topic.setdefault(self.conn.execute("SELECT topic_id FROM questions WHERE id = ?", (qid,)).fetchone()[0],
                                []).append(qid)
        topic, expected = max(by_topic.items(), key=lambda item: len(item[1]))
        self.assertGreaterEqual(len(expected), practice.MIN_QUESTIONS)
        result = self.start(self.student, section, topic)
        self.assertTrue(result.created)
        self.assertTrue(set(result.question_ids) <= set(expected))
        dzi_section = self.section("grade12-dzi-preparation")
        before = self.practice_digest()
        self.assertEqual(self.start(self.student, dzi_section).outcome, practice.OUTCOME_SCOPE_UNAVAILABLE)
        self.assertEqual(before, self.practice_digest())

    def test_a_real_physics_section_is_its_own_subject(self):
        section = self.conn.execute(
            "SELECT cs.id FROM curriculum_sections cs JOIN curricula c ON c.id = cs.curriculum_id "
            "WHERE c.subject = 'physics' ORDER BY cs.id LIMIT 1").fetchone()[0]
        result = self.start(self.student, section)
        self.assertTrue(result.created)
        subjects = {self.conn.execute("SELECT subject FROM questions WHERE id = ?", (qid,)).fetchone()[0]
                    for qid in result.question_ids}
        self.assertEqual(subjects, {"physics"})

    def test_the_database_stays_consistent_after_every_kind_of_start(self):
        scope = self.make_scope(8)
        self.start(self.student, scope)
        self.start(self.student, scope)
        self.insert_session(self.other_student, scope, [8_000_001])
        self.start(self.other_student, scope)
        with self.raw() as conn:
            self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(conn.execute("PRAGMA quick_check").fetchone()[0], "ok")


if __name__ == "__main__":
    unittest.main()
