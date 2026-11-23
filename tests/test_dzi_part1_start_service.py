"""The dormant DZI Part 1 pools and start/resume service (C3.2): ``web.services.dzi_part1``.

Every database is a temporary clone of the migrations + canonical template
(``tests.support.build_test_database``) at a frozen lifecycle time (``tests.support.lifecycle_time``).
No connection opens ``data/questions.db`` and no test goes through the isolated-app connection guard.
Building the template runs the migration runner, whose default-path check resolves that path
(filesystem metadata only).

What is proved, and how:

* **Current catalog evidence** (``CatalogEvidenceTest``): today's canonical pools are 105 multiple
  choice and 53 short answer, per exam 6, 7, 9, 7, 9, 7, 8 for the latter. These are observations about
  the catalog, not conditions of the service: ``ThresholdTest`` shows the thresholds are the validated
  blueprint's slot counts (15 and 10) and nothing else.
* every pool rule as a pair: the same candidate in the pool, then out of it for exactly one reason
  (the mutation is made inside a transaction that is rolled back, so each case starts from the template);
* the blueprint validated for creation only, with the duration bound and unit pinned;
* creation: the persisted row, the persisted deterministic draw (recomputed independently from the
  stored seed), one cryptographic seed, no other runtime row;
* the state machine with the stored cutoff at every edge, grace before any stale-item decision,
  future-start abandonment, stale and malformed stored rows, and persistence after a changed blueprint,
  grace constant or exclusion list;
* transactions: read-only exits discard (high-water mark unchanged), every write admits exactly once
  before its first write, a real writer lock, rollback of a mark when the insert fails, a mark that
  survives a refused creation, distinct owners, a real concurrent race;
* the clock contract: no ``check_factual`` call, an unusable clock refuses and writes no Part 1 row.
"""

from __future__ import annotations

import hashlib
import json
import random
import sqlite3
import tempfile
import threading
import unittest
from contextlib import ExitStack, closing, contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from tests.support import build_test_database, lifecycle_time
from web import db as web_db
from web.access import ANONYMOUS, Principal, account_principal
from web.services import accounts as accounts_service
from web.services import dzi_exams, lifecycle
from web.services import dzi_part1 as part1
from web.services.quiz import quiz_prompt_needs_visual

T0 = datetime(2026, 3, 2, 9, 0, 0)
ts = lifecycle.format_timestamp
EXISTING_IMAGE = "web/static/img/questions/dzi/aug_2022_v2_q12.png"  # a tracked file under the project root

CATALOG_MC = 105
CATALOG_SA = 53
CATALOG_SA_PER_EXAM = {"may_2022_v1": 6, "may_2024_v1": 7, "may_2025_v2": 9, "aug_2022_v2": 7,
                       "may_2023_v2": 9, "aug_2023_v2": 7, "aug_2024_v2": 8}


def digest(rows) -> str:
    return hashlib.sha256(repr(sorted(repr(tuple(row)) for row in rows)).encode()).hexdigest()


class Part1Case(unittest.TestCase):
    """A fresh database, a frozen clock and accounts for every test."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="learnpilot-part1-")
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.db_path = build_test_database(self.directory / "part1.db")
        stack = ExitStack()
        self.addCleanup(stack.close)
        self.clock = stack.enter_context(lifecycle_time(T0, self.db_path))
        self.conn = self.connect()
        self.student = self.add_account("student")
        self.other = self.add_account("student")

    # ------------------------------------------------------------------ plumbing
    def connect(self, timeout=30.0):
        conn = web_db.connect_db(self.db_path, timeout=timeout)
        self.addCleanup(conn.close)
        return conn

    def raw(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        self.addCleanup(conn.close)
        return conn

    def add_account(self, role="student", *, active=True) -> Principal:
        with closing(self.raw()) as conn:
            user_id = accounts_service.create_user(conn, display_name=f"Part1 {role}", role=role, is_active=active)
            conn.commit()
        return account_principal(user_id, role, "x")

    def start(self, who=None, conn=None):
        return part1.start_or_resume_part1(conn or self.conn, who or self.student)

    def attempts(self):
        with closing(self.raw()) as conn:
            return [dict(row) for row in conn.execute("SELECT * FROM dzi_part1_attempts ORDER BY id")]

    def attempt(self, attempt_id):
        return next(row for row in self.attempts() if row["id"] == attempt_id)

    def clock_row(self) -> dict:
        with closing(self.raw()) as conn:
            return dict(conn.execute("SELECT * FROM lifecycle_clock").fetchone())

    def state(self):
        """Every table (the Part 1 table and the clock row included), for exact before/after proofs."""
        with closing(self.raw()) as conn:
            names = [row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
            return {name: digest(conn.execute(f'SELECT * FROM "{name}"').fetchall()) for name in names}

    def sql(self, statement, params=()):
        with closing(self.raw()) as conn:
            cursor = conn.execute(statement, params)
            conn.commit()
            return cursor.lastrowid

    def blueprint(self):
        blueprint = part1.read_active_blueprint(self.conn)
        self.assertIsNotNone(blueprint)
        return blueprint

    def pools(self, conn=None) -> part1.Pools:
        conn = conn or self.conn
        blueprint = part1.read_active_blueprint(conn)
        self.assertIsNotNone(blueprint)
        return part1.compose_pools(conn, blueprint)

    @contextmanager
    def mutated(self, statement, params=()):
        """Run the block with one uncommitted change visible to ``self.conn``, then roll it back."""
        self.conn.execute("BEGIN")
        try:
            self.conn.execute(statement, params)
            yield
        finally:
            self.conn.rollback()

    def good_plan(self, seed="fixture-seed"):
        blueprint = self.blueprint()
        return part1.draw_plan(seed, blueprint.slots, part1.compose_pools(self.conn, blueprint)), blueprint

    def insert_attempt(self, who=None, *, started=T0, duration=5400, cutoff_extra=60, ids=None,
                       slot_format=None, planned=None, **overrides) -> int:
        """A fixture row with explicit values (a malformed one included)."""
        plan, blueprint = self.good_plan()
        row = {
            "user_id": (who or self.student).user_id, "blueprint_id": blueprint.blueprint_id, "seed": "fixture-seed",
            "question_ids_json": json.dumps(plan if ids is None else ids),
            "slot_format_json": blueprint.slot_format_json() if slot_format is None else slot_format,
            "max_points_planned": blueprint.max_points if planned is None else planned,
            "duration_seconds": duration, "started_at": ts(started),
            "deadline_at": ts(started + timedelta(seconds=min(duration, 10 ** 6))),
            "submit_cutoff_at": ts(started + timedelta(seconds=min(duration, 10 ** 6) + cutoff_extra)),
        }
        row.update(overrides)
        names = ", ".join(row)
        return self.sql(f"INSERT INTO dzi_part1_attempts ({names}) VALUES ({', '.join('?' for _ in row)})",
                        tuple(row.values()))

    def plan_of(self, attempt_id):
        return json.loads(self.attempt(attempt_id)["question_ids_json"])

    def slot_kinds(self, attempt_id):
        return [slot["kind"] for slot in json.loads(self.attempt(attempt_id)["slot_format_json"])]


# =========================================================================== catalog evidence

class CatalogEvidenceTest(Part1Case):
    """Observations about the canonical catalog at this base (a content PR updates them on purpose)."""

    def source_slug_counts(self, ids):
        rows = self.conn.execute(
            "SELECT e.id, e.year, e.session, e.variant, COUNT(*) AS n FROM questions q JOIN exams e ON e.id = q.exam_id "
            f"WHERE q.id IN ({','.join(str(i) for i in ids)}) GROUP BY e.id ORDER BY e.id").fetchall()
        return {dzi_exams.dzi_source_slug(row): row["n"] for row in rows}

    def test_canonical_pools_are_105_multiple_choice_and_53_short_answer(self):
        pools = self.pools()
        self.assertEqual((len(pools.multiple_choice), len(pools.short_answer)), (CATALOG_MC, CATALOG_SA))
        self.assertEqual(len(set(pools.multiple_choice)), CATALOG_MC)
        self.assertEqual(len(set(pools.short_answer)), CATALOG_SA)
        self.assertFalse(set(pools.multiple_choice) & set(pools.short_answer))
        self.assertEqual(list(pools.multiple_choice), sorted(pools.multiple_choice))

    def test_short_answer_per_exam_and_multiple_choice_per_exam(self):
        pools = self.pools()
        self.assertEqual(self.source_slug_counts(pools.short_answer), CATALOG_SA_PER_EXAM)
        self.assertEqual(list(self.source_slug_counts(pools.short_answer).values()), [6, 7, 9, 7, 9, 7, 8])
        self.assertEqual(self.source_slug_counts(pools.multiple_choice), {slug: 15 for slug in CATALOG_SA_PER_EXAM})

    def test_visual_evidence_of_the_final_pools(self):
        pools = self.pools()
        with_visual = lambda ids: [i for i in ids if part1._resolved_visual_exists(self.conn, i)]
        self.assertEqual((len(with_visual(pools.multiple_choice)), len(with_visual(pools.short_answer))), (7, 18))

    def test_task_number_does_not_decide_the_kind(self):
        pools = self.pools()
        rows = self.conn.execute(
            "SELECT etq.question_id, et.task_number FROM exam_task_questions etq JOIN exam_tasks et ON et.id = etq.task_id "
            "WHERE etq.role = 'primary'").fetchall()
        task = {row["question_id"]: row["task_number"] for row in rows}
        self.assertEqual(sum(1 for i in pools.multiple_choice if task[i] >= 16), 12)
        self.assertEqual(sum(1 for i in pools.multiple_choice if task[i] <= 15), 93)
        self.assertGreater(sum(1 for i in pools.short_answer if task[i] <= 15), 0)

    def test_every_pool_member_is_an_official_part1_linked_it_dzi_question(self):
        pools = self.pools()
        for kind, ids in (("mc", pools.multiple_choice), ("sa", pools.short_answer)):
            for question_id in ids:
                row = self.conn.execute(
                    "SELECT q.question_type, q.level, q.subject, q.year, q.exam_id, q.is_ai_generated, e.level AS el, "
                    "e.format_version, e.year AS ey, e.subject AS es FROM questions q JOIN exams e ON e.id = q.exam_id "
                    "WHERE q.id = ?", (question_id,)).fetchone()
                self.assertEqual((row["level"], row["el"], row["format_version"], row["year"] == row["ey"],
                                  row["is_ai_generated"]), ("DZI", "DZI", dzi_exams.DZI_FORMAT_VERSION, True, 0))
                self.assertEqual(row["question_type"] == "multiple_choice", kind == "mc")
                links = self.conn.execute(
                    "SELECT et.task_number, et.exam_id, etq.role FROM exam_task_questions etq "
                    "JOIN exam_tasks et ON et.id = etq.task_id WHERE etq.question_id = ?", (question_id,)).fetchall()
                self.assertTrue(any(l["role"] == "primary" and 1 <= l["task_number"] <= 25 and l["exam_id"] == row["exam_id"]
                                    for l in links), question_id)
                self.assertFalse(any(l["task_number"] > 25 for l in links))

    def test_the_three_missing_stimulus_questions_exist_where_the_design_says_and_are_never_in_a_pool(self):
        expected = {131: ("aug_2024_v2", 16), 67: ("may_2022_v1", 24), 202: ("aug_2022_v2", 25)}
        self.assertEqual(part1.MISSING_STIMULUS_QUESTION_IDS, frozenset(expected))
        pools = self.pools()
        for question_id, (source, task) in expected.items():
            row = self.conn.execute("SELECT source_exam, source_number FROM questions WHERE id = ?",
                                    (question_id,)).fetchone()
            self.assertEqual((row["source_exam"], row["source_number"]), (source, task))
            self.assertNotIn(question_id, pools.multiple_choice + pools.short_answer)

    def test_the_stimulus_list_is_what_removes_them(self):
        """56 short answers pass every other rule; the list removes exactly 67, 131 and 202 (design 4.2)."""
        with patch.object(part1, "MISSING_STIMULUS_QUESTION_IDS", frozenset()):
            pools = self.pools()
        self.assertEqual((len(pools.multiple_choice), len(pools.short_answer)), (CATALOG_MC, 56))
        self.assertEqual(set(pools.short_answer) - set(self.pools().short_answer), {67, 131, 202})

    def test_the_stimulus_ids_are_never_drawn_over_many_seeds_in_a_full_and_in_a_thin_pool(self):
        blueprint = self.blueprint()
        full = self.pools()
        for seed in range(200):
            plan = part1.draw_plan(f"seed-{seed}", blueprint.slots, full)
            self.assertFalse({67, 131, 202} & set(plan))
        # thin: exactly ten short answers remain besides the three excluded ids, which are otherwise eligible
        with patch.object(part1, "MISSING_STIMULUS_QUESTION_IDS", frozenset()):
            eligible = set(self.pools().short_answer)
        keep = sorted(eligible - {67, 131, 202})[:10]
        drop = sorted(eligible - set(keep) - {67, 131, 202})
        self.sql(f"UPDATE questions SET is_ai_generated = 1, quality_score = 0 WHERE id IN ({','.join(map(str, drop))})")
        self.assertEqual(self.pools().short_answer, tuple(keep))
        seen = set()
        for _ in range(5):
            self.clock.advance(7000)  # each attempt lapses before the next start
            result = self.start()
            self.assertTrue(result.created, result.outcome)
            plan = self.plan_of(result.attempt_id)
            self.assertFalse({67, 131, 202} & set(plan))
            seen.update(plan[15:])
        self.assertEqual(seen, set(keep))


# =========================================================================== pool rules, as pairs

class PoolRuleTest(Part1Case):
    """Each rule: the candidate is in the pool, then out of it for that one reason, then back."""

    def first(self, kind, *, needs_visual=None, parts=None):
        pools = self.pools()
        for question_id in (pools.multiple_choice if kind == "mc" else pools.short_answer):
            prompt = self.conn.execute("SELECT prompt FROM questions WHERE id = ?", (question_id,)).fetchone()[0]
            if needs_visual is not None and quiz_prompt_needs_visual(prompt) != needs_visual:
                continue
            if needs_visual is False and part1._resolved_visual_exists(self.conn, question_id):
                continue
            if parts is not None:
                n = self.conn.execute("SELECT COUNT(*) FROM fill_in_subquestions WHERE question_id = ?",
                                      (question_id,)).fetchone()[0]
                if n != parts:
                    continue
            return question_id
        self.fail("no candidate")

    def assert_removed(self, statement, params, removed, *, kind=None):
        before = self.pools()
        with self.mutated(statement, params):
            after = self.pools()
        self.assertEqual(set(before.multiple_choice) - set(after.multiple_choice), removed & set(before.multiple_choice))
        self.assertEqual(set(before.short_answer) - set(after.short_answer), removed & set(before.short_answer))
        self.assertFalse(set(after.multiple_choice) - set(before.multiple_choice))
        self.assertFalse(set(after.short_answer) - set(before.short_answer))
        self.assertTrue(removed)
        self.assertEqual(self.pools(), before)  # rolled back: the candidate is in again

    def exam_members(self, question_id):
        exam_id = self.conn.execute("SELECT exam_id FROM questions WHERE id = ?", (question_id,)).fetchone()[0]
        pools = self.pools()
        ids = {row[0] for row in self.conn.execute("SELECT id FROM questions WHERE exam_id = ?", (exam_id,))}
        return exam_id, ids & set(pools.multiple_choice + pools.short_answer)

    def test_unapproved_and_ai_generated_candidates_are_excluded(self):
        for kind in ("mc", "sa"):
            x = self.first(kind)
            with self.subTest(kind=kind, rule="unreviewed AI"):
                self.assert_removed("UPDATE questions SET is_ai_generated = 1, quality_score = 0 WHERE id = ?", (x,), {x})
            with self.subTest(kind=kind, rule="AI even with a passing quality score"):
                self.assert_removed("UPDATE questions SET is_ai_generated = 1, quality_score = 1.0 WHERE id = ?", (x,), {x})

    def test_ownership_level_year_and_exam_identity(self):
        for kind in ("mc", "sa"):
            x = self.first(kind)
            exam_id, _members = self.exam_members(x)
            other = self.conn.execute("SELECT id FROM exams WHERE level = 'DZI' AND id <> ? ORDER BY id", (exam_id,)).fetchone()[0]
            cases = {
                "question subject not IT": ("UPDATE questions SET subject = 'physics' WHERE id = ?", (x,)),
                "question level not DZI": ("UPDATE questions SET level = 'NVO' WHERE id = ?", (x,)),
                "question year differs": ("UPDATE questions SET year = year + 1 WHERE id = ?", (x,)),
                "question belongs to another exam": ("UPDATE questions SET exam_id = ? WHERE id = ?", (other, x)),
                "not a candidate type": ("UPDATE questions SET question_type = 'true_false' WHERE id = ?", (x,)),
            }
            for name, (statement, params) in cases.items():
                with self.subTest(kind=kind, rule=name):
                    self.assert_removed(statement, params, {x})

    def test_exam_level_format_and_subject_remove_the_whole_exam(self):
        x = self.first("mc")
        exam_id, members = self.exam_members(x)
        self.assertGreater(len(members), 15)
        for name, statement in {
            "exam is not DZI": "UPDATE exams SET level = 'NVO_7' WHERE id = ?",
            "exam is another format": "UPDATE exams SET format_version = 'modern_2023' WHERE id = ?",
            "exam subject is not IT": "UPDATE exams SET subject = 'matematika' WHERE id = ?",
        }.items():
            with self.subTest(rule=name):
                self.assert_removed(statement, (exam_id,), members)

    def test_only_primary_links_to_tasks_1_to_25_make_a_candidate(self):
        for kind in ("mc", "sa"):
            x = self.first(kind)
            task26 = self.conn.execute(
                "SELECT et.id FROM exam_tasks et WHERE et.task_number = 26 AND et.exam_id = "
                "(SELECT exam_id FROM questions WHERE id = ?)", (x,)).fetchone()[0]
            cases = {
                "secondary link only": ("UPDATE exam_task_questions SET role = 'secondary' WHERE question_id = ?", (x,)),
                "legacy, no link at all": ("DELETE FROM exam_task_questions WHERE question_id = ?", (x,)),
                "linked to Part 2 instead": ("UPDATE exam_task_questions SET task_id = ? WHERE question_id = ?", (task26, x)),
                "also linked to Part 2": ("INSERT INTO exam_task_questions (task_id, question_id, role) VALUES (?, ?, 'secondary')",
                                          (task26, x)),
            }
            for name, (statement, params) in cases.items():
                with self.subTest(kind=kind, rule=name):
                    self.assert_removed(statement, params, {x})

    def test_a_task_numbered_1_to_25_but_stored_as_part_2_is_not_part_1(self):
        """exam_tasks also stores exam_part: the number alone does not make a Part 1 task."""
        for kind in ("mc", "sa"):
            x = self.first(kind)
            task = self.conn.execute(
                "SELECT task_id FROM exam_task_questions WHERE question_id = ? AND role = 'primary'", (x,)).fetchone()[0]
            self.assertLessEqual(self.conn.execute("SELECT task_number FROM exam_tasks WHERE id = ?", (task,)).fetchone()[0], 25)
            pools = self.pools()
            removed = {row[0] for row in self.conn.execute(
                "SELECT question_id FROM exam_task_questions WHERE task_id = ? AND role = 'primary'", (task,))}
            removed &= set(pools.multiple_choice + pools.short_answer)
            with self.subTest(kind=kind):
                self.assert_removed("UPDATE exam_tasks SET exam_part = 2 WHERE id = ?", (task,), removed)

    def test_a_contradictory_cross_link_to_a_part_2_stored_task_numbered_1_to_25_excludes_the_question(self):
        x = self.first("mc")
        exam_id = self.conn.execute("SELECT exam_id FROM questions WHERE id = ?", (x,)).fetchone()[0]
        current = self.conn.execute("SELECT task_id FROM exam_task_questions WHERE question_id = ?", (x,)).fetchone()[0]
        other_task, other_question = self.conn.execute(
            "SELECT et.id, etq.question_id FROM exam_tasks et JOIN exam_task_questions etq ON etq.task_id = et.id "
            "WHERE et.exam_id = ? AND et.task_number <= 25 AND et.id <> ? AND etq.role = 'primary' ORDER BY et.id",
            (exam_id, current)).fetchone()
        before = self.pools()
        self.conn.execute("BEGIN")
        try:
            # x keeps its good Part 1 primary link and gains a link to a task numbered below 26 that is part 2
            self.conn.execute("UPDATE exam_tasks SET exam_part = 2 WHERE id = ?", (other_task,))
            self.conn.execute("INSERT INTO exam_task_questions (task_id, question_id, role) VALUES (?, ?, 'secondary')",
                              (other_task, x))
            after = self.pools()
        finally:
            self.conn.rollback()
        self.assertNotIn(x, after.multiple_choice + after.short_answer)
        self.assertNotIn(other_question, after.multiple_choice + after.short_answer)
        self.assertEqual(self.pools(), before)

    def test_a_question_linked_twice_is_one_candidate(self):
        x = self.first("mc")
        exam_id = self.conn.execute("SELECT exam_id FROM questions WHERE id = ?", (x,)).fetchone()[0]
        current = self.conn.execute("SELECT task_id FROM exam_task_questions WHERE question_id = ?", (x,)).fetchone()[0]
        other_task = self.conn.execute(
            "SELECT id FROM exam_tasks WHERE exam_id = ? AND task_number <= 25 AND id <> ? ORDER BY id", (exam_id, current)).fetchone()[0]
        before = self.pools()
        with self.mutated("INSERT INTO exam_task_questions (task_id, question_id, role) VALUES (?, ?, 'primary')",
                          (other_task, x)):
            after = self.pools()
        self.assertEqual(after, before)
        self.assertEqual(after.multiple_choice.count(x), 1)

    def test_multiple_choice_renderability(self):
        x = self.first("mc", needs_visual=False)
        wrong = self.conn.execute("SELECT id FROM multiple_choice_options WHERE question_id = ? AND is_correct = 0 "
                                  "ORDER BY id", (x,)).fetchone()[0]
        cases = {
            "only three options": ("DELETE FROM multiple_choice_options WHERE id = ?", (wrong,)),
            "no correct option": ("UPDATE multiple_choice_options SET is_correct = 0 WHERE question_id = ?", (x,)),
            "two correct options": ("UPDATE multiple_choice_options SET is_correct = 1 WHERE question_id = ?", (x,)),
            "placeholder option text": ("UPDATE multiple_choice_options SET option_text = '-' WHERE id = ?", (wrong,)),
        }
        for name, (statement, params) in cases.items():
            with self.subTest(rule=name):
                self.assert_removed(statement, params, {x})

    def test_a_prompt_that_needs_a_visual_needs_one_the_existing_loader_resolves(self):
        x = self.first("mc", needs_visual=False)
        prompt = "UPDATE questions SET prompt = prompt || ' (на изображението)' WHERE id = ?"
        with self.subTest(rule="needs a visual, none stored"):
            self.assert_removed(prompt, (x,), {x})
        with self.subTest(rule="a path the shared helper accepts but the loader never attaches"):
            with self.mutated("UPDATE questions SET prompt = prompt || ' (на изображението)', image_path = ? WHERE id = ?",
                              (EXISTING_IMAGE, x)):
                question = self.conn.execute("SELECT * FROM questions WHERE id = ?", (x,)).fetchone()
                from web.services.quiz import question_has_usable_visual
                self.assertTrue(question_has_usable_visual(self.conn, question))
                self.assertNotIn(x, self.pools().multiple_choice)
        with self.subTest(rule="control: the seven items that have a resolved visual stay"):
            with_visual = [i for i in self.pools().multiple_choice if part1._resolved_visual_exists(self.conn, i)]
            self.assertEqual(len(with_visual), 7)

    def test_short_answer_readiness(self):
        x = self.first("sa")
        cases = {
            "no accepted answer": ("UPDATE fill_in_subquestions SET correct_answer = '-', answer_alternatives = NULL "
                                   "WHERE question_id = ?", (x,)),
            "a visual-dependent phrase": ("UPDATE questions SET prompt = prompt || ' на изображението' WHERE id = ?", (x,)),
            "a Part 2 source number": ("UPDATE questions SET source_number = 27 WHERE id = ?", (x,)),
            "no sub-answers": ("DELETE FROM fill_in_subquestions WHERE question_id = ?", (x,)),
        }
        for name, (statement, params) in cases.items():
            with self.subTest(rule=name):
                self.assert_removed(statement, params, {x})

    def test_short_answer_weights(self):
        x = self.first("sa", parts=3)
        ids = [row[0] for row in self.conn.execute(
            "SELECT id FROM fill_in_subquestions WHERE question_id = ? ORDER BY subquestion_number", (x,))]
        cases = {
            "sums to 4": ("UPDATE fill_in_subquestions SET points = 2 WHERE id = ?", (ids[0],)),
            "sums to 2": ("UPDATE fill_in_subquestions SET points = 0 WHERE id = ?", (ids[0],)),
            "a negative weight that still sums to 3": ("UPDATE fill_in_subquestions SET points = CASE id WHEN ? THEN -1 ELSE 2 END "
                                                       "WHERE question_id = ?", (ids[0], x)),
            "a missing weight": ("UPDATE fill_in_subquestions SET points = NULL WHERE id = ?", (ids[0],)),
            "a non-integer weight": ("UPDATE fill_in_subquestions SET points = 1.0 + 0.5 WHERE id = ?", (ids[0],)),
        }
        for name, (statement, params) in cases.items():
            with self.subTest(rule=name):
                self.assert_removed(statement, params, {x})

    def test_equal_weights_are_required_in_order_independent_mode(self):
        x = self.first("sa", parts=2)
        ids = [row[0] for row in self.conn.execute(
            "SELECT id FROM fill_in_subquestions WHERE question_id = ? ORDER BY subquestion_number", (x,))]
        # distinct accepted answers: ordered mode, so weights 2 and 1 are fine
        self.conn.execute("BEGIN")
        self.conn.execute("UPDATE fill_in_subquestions SET correct_answer = 'a', answer_alternatives = NULL, points = 2 WHERE id = ?", (ids[0],))
        self.conn.execute("UPDATE fill_in_subquestions SET correct_answer = 'b', answer_alternatives = NULL, points = 1 WHERE id = ?", (ids[1],))
        try:
            self.assertIn(x, self.pools().short_answer)
            # the same accepted answers on both parts: order independent, so unequal weights are refused
            self.conn.execute("UPDATE fill_in_subquestions SET correct_answer = 'a' WHERE id = ?", (ids[1],))
            self.assertNotIn(x, self.pools().short_answer)
        finally:
            self.conn.rollback()

    def test_part_numbers_must_be_positive_integers(self):
        x = self.first("sa", parts=3)
        before = self.pools()
        first = self.conn.execute("SELECT id FROM fill_in_subquestions WHERE question_id = ? ORDER BY subquestion_number",
                                  (x,)).fetchone()[0]
        for name, value in {"zero": "0", "negative": "-1", "fractional": "1.5", "text": "'one'"}.items():
            with self.subTest(part_number=name):
                self.assert_removed(f"UPDATE fill_in_subquestions SET subquestion_number = {value} WHERE id = ?", (first,), {x})
        self.assertEqual(self.pools().short_answer, before.short_answer)  # weights and counts as before
        self.assertEqual(len(before.short_answer), CATALOG_SA)

    def test_pool_composition_writes_nothing(self):
        before = self.state()
        self.pools()
        self.assertEqual(self.state(), before)
        self.assertFalse(self.conn.in_transaction)


class ThresholdTest(Part1Case):
    """The service asks for as many distinct candidates per kind as the validated blueprint has slots."""

    def drop_to(self, kind, keep):
        pools = self.pools()
        ids = list(pools.multiple_choice if kind == "mc" else pools.short_answer)
        drop = ids[keep:]
        self.sql(f"UPDATE questions SET is_ai_generated = 1, quality_score = 0 WHERE id IN ({','.join(map(str, drop))})")

    def test_the_thresholds_are_the_slot_counts_of_the_blueprint_not_the_catalog_sizes(self):
        blueprint = self.blueprint()
        self.assertEqual((blueprint.slot_count("multiple_choice"), blueprint.slot_count("short_answer")), (15, 10))
        self.drop_to("mc", 15)
        self.drop_to("sa", 10)
        pools = self.pools()
        self.assertEqual((len(pools.multiple_choice), len(pools.short_answer)), (15, 10))  # not 105 and 53
        result = self.start()
        self.assertTrue(result.created)
        self.assertEqual(set(self.plan_of(result.attempt_id)[:15]), set(pools.multiple_choice))
        self.assertEqual(set(self.plan_of(result.attempt_id)[15:]), set(pools.short_answer))

    def test_one_short_in_either_kind_refuses_without_borrowing(self):
        for kind, keep_mc, keep_sa in (("mc", 14, 10), ("sa", 15, 9)):
            with self.subTest(thin=kind):
                self.setUp()  # a fresh database per case
                self.drop_to("mc", keep_mc)
                self.drop_to("sa", keep_sa)
                before = self.state()
                result = self.start()
                self.assertEqual(result.outcome, part1.OUTCOME_POOL_INSUFFICIENT)
                self.assertEqual(self.attempts(), [])
                self.assertEqual(self.state(), before)  # nothing written, the clock row included
                self.assertEqual(result[1:7], (None, (), None, None, None, None))  # no counts, no ids, no names

    def test_the_fifteenth_and_tenth_candidate_make_it_start(self):
        self.drop_to("mc", 15)
        self.drop_to("sa", 10)
        self.assertTrue(self.start().created)


# =========================================================================== blueprint

class BlueprintTest(Part1Case):
    def tamper(self, statement, params=()):
        self.sql(statement, params)

    def expect_unavailable(self):
        before = self.state()
        result = self.start()
        self.assertEqual(result.outcome, part1.OUTCOME_BLUEPRINT_UNAVAILABLE)
        self.assertEqual(self.attempts(), [])
        self.assertEqual(self.state(), before)
        self.assertIsNone(part1.read_active_blueprint(self.conn))

    def test_the_untampered_blueprint_is_derived_from_its_rows(self):
        blueprint = self.blueprint()
        self.assertEqual((blueprint.slug, blueprint.duration_minutes, blueprint.duration_seconds, blueprint.max_points),
                         (dzi_exams.DZI_FORMAT_VERSION, 90, 5400, 45))
        self.assertEqual(blueprint.slots, tuple([part1.SlotFormat("multiple_choice", 1)] * 15
                                                + [part1.SlotFormat("short_answer", 3)] * 10))

    def test_each_tampering_fails_closed_for_creation(self):
        cases = {
            "slot kind swapped": ("UPDATE dzi_blueprint_slots SET task_kind = 'short_answer' WHERE slot_number = 3", ()),
            "unsupported kind": ("UPDATE dzi_blueprint_slots SET task_kind = 'practical_web' WHERE slot_number = 3", ()),
            "points changed": ("UPDATE dzi_blueprint_slots SET points = 2 WHERE slot_number = 20", ()),
            "points not an integer": ("UPDATE dzi_blueprint_slots SET points = 1.5 WHERE slot_number = 2", ()),
            "a slot missing (a gap)": ("DELETE FROM dzi_blueprint_slots WHERE slot_number = 7", ()),
            "a slot renumbered": ("UPDATE dzi_blueprint_slots SET slot_number = 99 WHERE slot_number = 7", ()),
            "an extra Part 1 slot": ("INSERT INTO dzi_blueprint_slots (blueprint_id, slot_number, exam_part, task_kind, points) "
                                     "VALUES (1, 29, 1, 'multiple_choice', 1)", ()),
            "a Part 1 slot moved to Part 2": ("UPDATE dzi_blueprint_slots SET exam_part = 2 WHERE slot_number = 25", ()),
            "a topic area": ("UPDATE dzi_blueprint_slots SET topic_area = 'x' WHERE slot_number = 4", ()),
            "a topic": ("UPDATE dzi_blueprint_slots SET topic_id = (SELECT id FROM curriculum_topics LIMIT 1) WHERE slot_number = 4", ()),
            "a section": ("UPDATE dzi_blueprint_slots SET section_id = (SELECT id FROM curriculum_sections LIMIT 1) WHERE slot_number = 4", ()),
            "a required asset": ("UPDATE dzi_blueprint_slots SET required_asset_type = 'image' WHERE slot_number = 4", ()),
            "inactive": ("UPDATE dzi_blueprints SET is_active = 0", ()),
            "another slug": ("UPDATE dzi_blueprints SET blueprint_slug = 'other'", ()),
        }
        for name, (statement, params) in cases.items():
            with self.subTest(tamper=name):
                self.setUp()
                self.tamper(statement, params)
                self.expect_unavailable()

    def test_a_non_positive_active_blueprint_id_is_unavailable_not_a_late_insert_failure(self):
        for legacy_id in (0, -3):
            with self.subTest(blueprint_id=legacy_id):
                self.setUp()
                self.sql("UPDATE dzi_blueprint_slots SET blueprint_id = ?", (legacy_id,))
                self.sql("UPDATE dzi_blueprints SET id = ?", (legacy_id,))
                self.expect_unavailable()
        self.setUp()  # control: the shipped positive id creates
        self.assertGreater(self.blueprint().blueprint_id, 0)
        self.assertTrue(self.start().created)

    def test_the_duration_bound_is_240_minutes(self):
        self.assertEqual(part1.MAX_PART1_MINUTES, 240)
        for minutes, ok in ((1, True), (240, True), (241, False), (0, False), (-5, False), (90.5, False), ("ninety", False),
                            (10 ** 12, False)):
            with self.subTest(minutes=minutes):
                self.setUp()
                self.tamper("UPDATE dzi_blueprints SET part1_minutes = ?", (minutes,))
                if ok:
                    result = self.start()
                    self.assertTrue(result.created)
                    self.assertEqual(self.attempt(result.attempt_id)["duration_seconds"], minutes * 60)
                else:
                    self.expect_unavailable()

    def test_part_2_configuration_and_the_whole_paper_total_play_no_part(self):
        self.tamper("UPDATE dzi_blueprints SET total_points = 7, part2_minutes = 0")
        self.tamper("DELETE FROM dzi_blueprint_slots WHERE exam_part = 2")
        result = self.start()
        self.assertTrue(result.created)
        row = self.attempt(result.attempt_id)
        self.assertEqual((row["max_points_planned"], row["duration_seconds"]), (45, 5400))

    def test_a_valid_other_duration_is_snapshotted(self):
        self.tamper("UPDATE dzi_blueprints SET part1_minutes = 120")
        result = self.start()
        row = self.attempt(result.attempt_id)
        self.assertEqual(row["duration_seconds"], 7200)
        self.assertEqual(row["deadline_at"], ts(T0 + timedelta(seconds=7200)))


# =========================================================================== creation

class CreationTest(Part1Case):
    def test_the_persisted_row(self):
        result = self.start()
        self.assertEqual(result.outcome, part1.OUTCOME_CREATED)
        row = self.attempt(result.attempt_id)
        blueprint = self.blueprint()
        self.assertEqual((row["user_id"], row["blueprint_id"]), (self.student.user_id, blueprint.blueprint_id))
        self.assertEqual((row["started_at"], row["deadline_at"], row["submit_cutoff_at"]),
                         (ts(T0), ts(T0 + timedelta(seconds=5400)), ts(T0 + timedelta(seconds=5460))))
        self.assertEqual((row["duration_seconds"], row["max_points_planned"]), (5400, 45))
        self.assertEqual((row["submitted_at"], row["expired_at"], row["abandoned_at"]), (None, None, None))
        self.assertEqual((row["score_points"], row["score_max"]), (None, None))
        plan = json.loads(row["question_ids_json"])
        self.assertEqual(len(plan), 25)
        self.assertEqual(len(set(plan)), 25)
        self.assertTrue(all(type(item) is int for item in plan))  # flat: nothing but ids
        self.assertEqual(json.loads(row["slot_format_json"]),
                         [{"kind": "multiple_choice", "points": 1}] * 15 + [{"kind": "short_answer", "points": 3}] * 10)
        self.assertEqual(result[:7], (part1.OUTCOME_CREATED, result.attempt_id, tuple(plan), row["seed"],
                                      row["started_at"], row["deadline_at"], row["submit_cutoff_at"]))
        types = {r["id"]: r["question_type"] for r in self.conn.execute(
            f"SELECT id, question_type FROM questions WHERE id IN ({','.join(map(str, plan))})")}
        self.assertEqual([types[i] for i in plan[:15]], ["multiple_choice"] * 15)
        self.assertTrue(all(types[i] in ("fill_in", "short_answer") for i in plan[15:]))
        pools = self.pools()
        self.assertTrue(set(plan[:15]) <= set(pools.multiple_choice) and set(plan[15:]) <= set(pools.short_answer))

    def test_the_persisted_draw_is_the_deterministic_seeded_draw(self):
        result = self.start()
        row = self.attempt(result.attempt_id)
        pools = self.pools()
        self.assertRegex(row["seed"], r"^[0-9a-f]{32}$")
        rng = random.Random(row["seed"])  # written independently of ``draw_plan``
        expected = rng.sample(sorted(pools.multiple_choice), 15) + rng.sample(sorted(pools.short_answer), 10)
        self.assertEqual(json.loads(row["question_ids_json"]), expected)
        self.assertEqual(part1.draw_plan(row["seed"], self.blueprint().slots, pools), expected)

    def test_one_cryptographic_seed_per_attempt_and_it_is_the_stored_one(self):
        with patch.object(part1.secrets, "token_hex", return_value="ab" * 16) as token:
            result = self.start()
        token.assert_called_once_with(16)
        self.assertEqual(self.attempt(result.attempt_id)["seed"], "ab" * 16)
        second = self.start(self.other)
        self.assertNotEqual(self.attempt(second.attempt_id)["seed"], "ab" * 16)  # a real, different seed
        self.assertRegex(self.attempt(second.attempt_id)["seed"], r"^[0-9a-f]{32}$")

    def test_the_same_seed_and_pools_give_the_same_plan_for_every_account(self):
        with patch.object(part1.secrets, "token_hex", return_value="cd" * 16):
            first, second = self.start(), self.start(self.other)
        self.assertEqual(first.question_ids, second.question_ids)

    def test_no_other_runtime_row_is_created_and_the_clock_is_the_only_other_change(self):
        self.clock.advance(10)
        before = self.state()
        self.start()
        after = self.state()
        changed = {name for name in before if before[name] != after[name]}
        self.assertEqual(changed, {"dzi_part1_attempts", "lifecycle_clock"})
        clock = self.clock_row()
        self.assertEqual((clock["high_water_at"], clock["state"]), (ts(T0 + timedelta(seconds=10)), "ok"))

    def test_admins_and_teachers_may_take_an_attempt_of_their_own(self):
        for role in ("teacher", "admin"):
            who = self.add_account(role)
            self.assertTrue(self.start(who).created)

    def test_a_second_start_returns_the_same_attempt_and_the_plan_is_never_rewritten(self):
        first = self.start()
        before = self.attempt(first.attempt_id)
        self.clock.advance(120)
        second = self.start()
        self.assertEqual((second.outcome, second.attempt_id, second.state), (part1.OUTCOME_RESUMED, first.attempt_id, "open"))
        self.assertEqual(second.question_ids, first.question_ids)
        self.assertEqual(self.attempt(first.attempt_id), before)
        self.assertEqual(len(self.attempts()), 1)


class OwnerTest(Part1Case):
    def test_only_a_signed_in_account_may_start(self):
        for who in (ANONYMOUS, Principal(is_teacher=True, is_admin=True), "not a principal", None):
            with self.subTest(who=who):
                with self.assertRaises(part1.Part1AccessError):
                    part1.start_or_resume_part1(self.conn, who)
        self.assertEqual(self.attempts(), [])

    def test_an_account_that_is_inactive_or_missing_when_read_under_the_lock_is_unavailable_and_nothing_is_written(self):
        sleeper = self.add_account("student", active=False)
        before = self.state()
        self.assertEqual(self.start(sleeper).outcome, part1.OUTCOME_ACCOUNT_UNAVAILABLE)
        self.assertEqual(self.start(account_principal(999999, "student", "x")).outcome, part1.OUTCOME_ACCOUNT_UNAVAILABLE)
        self.assertEqual(self.state(), before)
        # deactivated after an attempt exists: the open row is neither resumed nor marked
        started = self.start()
        self.sql("UPDATE users SET is_active = 0 WHERE id = ?", (self.student.user_id,))
        self.clock.advance(7000)
        before = self.state()
        self.assertEqual(self.start().outcome, part1.OUTCOME_ACCOUNT_UNAVAILABLE)
        self.assertEqual(self.state(), before)
        self.assertIsNone(self.attempt(started.attempt_id)["expired_at"])

    def test_decisions_and_writes_are_limited_to_the_calling_account(self):
        mine, theirs = self.start(), self.start(self.other)
        self.assertNotEqual(mine.attempt_id, theirs.attempt_id)
        self.clock.advance(7000)  # both lapsed
        before_other = self.attempt(theirs.attempt_id)
        result = self.start()
        self.assertEqual((result.outcome, result.expired_attempt_id), (part1.OUTCOME_CREATED, mine.attempt_id))
        self.assertEqual(self.attempt(theirs.attempt_id), before_other)  # the other account's lapsed row was not touched
        self.assertEqual(self.attempt(mine.attempt_id)["user_id"], self.student.user_id)
        self.assertEqual(sorted(r["user_id"] for r in self.attempts()),
                         sorted([self.student.user_id, self.other.user_id, self.student.user_id]))

    def test_connection_checks(self):
        with self.subTest("inside a transaction"):
            self.conn.execute("BEGIN")
            with self.assertRaises(RuntimeError):
                self.start()
            self.conn.rollback()
        with self.subTest("plain rows"):
            plain = sqlite3.connect(self.db_path)
            self.addCleanup(plain.close)
            with self.assertRaises(RuntimeError):
                self.start(conn=plain)
        with self.subTest("foreign keys off"):
            off = sqlite3.connect(self.db_path)
            off.row_factory = sqlite3.Row
            self.addCleanup(off.close)
            with self.assertRaises(RuntimeError):
                self.start(conn=off)
        self.assertEqual(self.attempts(), [])

    @unittest.skipUnless(hasattr(sqlite3.Connection, "autocommit"), "needs Python 3.12 connection autocommit")
    def test_a_true_autocommit_connection_is_rejected_and_the_normal_one_works(self):
        auto = sqlite3.connect(self.db_path, autocommit=True)
        auto.row_factory = sqlite3.Row
        auto.execute("PRAGMA foreign_keys = ON")
        self.addCleanup(auto.close)
        before = self.state()
        with self.assertRaisesRegex(RuntimeError, "autocommit"):
            self.start(conn=auto)
        self.assertFalse(auto.in_transaction)
        self.assertEqual(self.state(), before)
        self.assertTrue(self.start().created)  # the standard connection of web.db.connect_db


# =========================================================================== state and resume

class StateTest(Part1Case):
    """The stored cutoff at every edge (started T0, deadline T0 + 5400, cutoff T0 + 5460)."""

    def test_the_pure_state_function_at_every_edge(self):
        s, d, c = T0, T0 + timedelta(seconds=5400), T0 + timedelta(seconds=5460)
        one = timedelta(seconds=1)
        cases = [(s - one, "future_start"), (s, "open"), (d - one, "open"), (d, "grace"),
                 (c - one, "grace"), (c, "lapsed"), (c + one, "lapsed")]
        for now, expected in cases:
            with self.subTest(offset=(now - T0).total_seconds()):
                self.assertEqual(part1.attempt_state(s, d, c, now), expected)

    def test_the_service_at_every_edge(self):
        first = self.start()
        for offset, outcome, state in ((0, "resumed", "open"), (5399, "resumed", "open"), (5400, "resumed", "grace"),
                                       (5459, "resumed", "grace")):
            with self.subTest(offset=offset):
                self.clock.set(T0 + timedelta(seconds=offset))
                before = self.state()
                result = self.start()
                self.assertEqual((result.outcome, result.attempt_id, result.state), (outcome, first.attempt_id, state))
                self.assertEqual(self.state(), before)  # a resume writes nothing, the clock row included
        self.clock.set(T0 + timedelta(seconds=5460))  # exactly the stored cutoff: exclusive
        result = self.start()
        self.assertEqual((result.outcome, result.expired_attempt_id), ("created", first.attempt_id))
        self.assertEqual(self.attempt(first.attempt_id)["expired_at"], ts(T0 + timedelta(seconds=5460)))
        self.assertNotEqual(result.attempt_id, first.attempt_id)
        self.assertEqual(self.attempt(result.attempt_id)["started_at"], ts(T0 + timedelta(seconds=5460)))
        self.assertEqual([r["expired_at"] is None for r in self.attempts()], [False, True])

    def test_grace_returns_the_same_attempt_without_judging_any_item(self):
        first = self.start()
        for question_id in self.plan_of(first.attempt_id):  # every item is stale now
            self.sql("UPDATE questions SET is_ai_generated = 1, quality_score = 0, question_type = 'true_false' WHERE id = ?",
                     (question_id,))
        self.sql("DELETE FROM multiple_choice_options")
        with patch.object(part1, "_presentable_slots", side_effect=AssertionError("no stale-item decision in grace")):
            self.clock.set(T0 + timedelta(seconds=5400))
            self.assertEqual(self.start().state, "grace")
            self.clock.set(T0 + timedelta(seconds=5459))
            result = self.start()
        self.assertEqual((result.outcome, result.attempt_id, result.state, result.presentable_slots),
                         ("resumed", first.attempt_id, "grace", None))
        self.assertEqual(self.attempt(first.attempt_id)["abandoned_at"], None)

    def test_the_same_all_stale_row_is_abandoned_before_the_deadline_and_expired_after_the_cutoff(self):
        for offset, column in ((100, "abandoned_at"), (5460, "expired_at")):
            with self.subTest(offset=offset):
                self.setUp()
                first = self.start()
                self.sql("DELETE FROM multiple_choice_options")
                self.sql("UPDATE fill_in_subquestions SET correct_answer = '-', answer_alternatives = NULL")
                self.clock.set(T0 + timedelta(seconds=offset))
                result = self.start()
                self.assertEqual(self.attempt(first.attempt_id)[column], ts(self.clock.wall()))
                self.assertEqual(result.outcome, part1.OUTCOME_POOL_INSUFFICIENT)  # no pool is left, the mark stays
                self.assertEqual(getattr(result, "abandoned_attempt_id" if column == "abandoned_at" else "expired_attempt_id"),
                                 first.attempt_id)

    def test_resume_judges_stored_items_individually_and_only_the_current_item(self):
        first = self.start()
        plan = self.plan_of(first.attempt_id)
        self.sql("DELETE FROM multiple_choice_options WHERE question_id = ?", (plan[0],))  # slot 1
        self.sql("UPDATE fill_in_subquestions SET correct_answer = '-', answer_alternatives = NULL WHERE question_id = ?",
                 (plan[15],))  # slot 16
        self.clock.advance(60)
        result = self.start()
        self.assertEqual(result.outcome, part1.OUTCOME_RESUMED)
        self.assertEqual(result.presentable_slots, tuple(n for n in range(1, 26) if n not in (1, 16)))
        self.assertEqual(result.question_ids, tuple(plan))  # the stored plan, never reordered or trimmed

    def test_a_stored_short_answer_renumbered_to_part_2_is_not_presentable(self):
        first = self.start()
        plan = self.plan_of(first.attempt_id)
        self.sql("UPDATE questions SET source_number = 27 WHERE id = ?", (plan[15],))
        self.assertNotIn(16, self.start().presentable_slots)
        for question_id in plan[:15]:
            self.sql("DELETE FROM multiple_choice_options WHERE question_id = ?", (question_id,))
        for question_id in plan[16:]:
            self.sql("UPDATE fill_in_subquestions SET correct_answer = '-', answer_alternatives = NULL "
                     "WHERE question_id = ?", (question_id,))
        self.assertEqual(self.start().abandoned_attempt_id, first.attempt_id)

    def test_an_item_whose_kind_changed_is_not_presentable_under_its_stored_kind(self):
        first = self.start()
        plan = self.plan_of(first.attempt_id)
        self.sql("UPDATE questions SET question_type = 'fill_in' WHERE id = ?", (plan[2],))
        self.assertNotIn(3, self.start().presentable_slots)
        self.sql("UPDATE questions SET question_type = 'multiple_choice' WHERE id = ?", (plan[17],))
        self.assertNotIn(18, self.start().presentable_slots)

    def test_resume_uses_the_stored_points_for_the_part_weights(self):
        first = self.start()
        plan = self.plan_of(first.attempt_id)
        sa = plan[15]
        parts = self.conn.execute("SELECT id FROM fill_in_subquestions WHERE question_id = ? ORDER BY subquestion_number", (sa,)).fetchall()
        self.sql("UPDATE fill_in_subquestions SET points = points + 1 WHERE id = ?", (parts[0][0],))
        self.assertNotIn(16, self.start().presentable_slots)  # no longer sums to the stored 3
        # the active blueprint changing its points does not move the stored 3 the weights are compared with
        self.sql("UPDATE fill_in_subquestions SET points = points - 1 WHERE id = ?", (parts[0][0],))
        self.sql("UPDATE dzi_blueprint_slots SET points = 9 WHERE slot_number = 16")
        self.assertIn(16, self.start().presentable_slots)

    def test_resume_rejects_unpersistable_part_numbers_of_a_stored_item(self):
        first = self.start()
        sa = self.plan_of(first.attempt_id)[15]
        part = self.conn.execute("SELECT id FROM fill_in_subquestions WHERE question_id = ? ORDER BY subquestion_number",
                                 (sa,)).fetchone()[0]
        for value in ("0", "-2", "1.5", "'x'"):
            with self.subTest(part_number=value):
                self.sql(f"UPDATE fill_in_subquestions SET subquestion_number = {value} WHERE id = ?", (part,))
                self.assertNotIn(16, self.start().presentable_slots)
                self.sql("UPDATE fill_in_subquestions SET subquestion_number = 99 WHERE id = ?", (part,))
                self.sql("UPDATE fill_in_subquestions SET subquestion_number = 1 WHERE id = ?", (part,))

    def test_resume_applies_the_loader_based_visual_check_to_a_stored_multiple_choice_item(self):
        # A fixed plan, not a random draw: slot 1 needs no visual, slot 13 holds an item whose visual the loader resolves.
        pools = self.pools()
        resolved_ids = [i for i in pools.multiple_choice if part1._resolved_visual_exists(self.conn, i)]
        plain_ids = [i for i in pools.multiple_choice
                     if i not in resolved_ids
                     and not quiz_prompt_needs_visual(self.conn.execute("SELECT prompt FROM questions WHERE id = ?", (i,)).fetchone()[0])]
        short = list(pools.short_answer[:10])
        plan = plain_ids[:12] + [resolved_ids[0]] + plain_ids[12:14] + short
        attempt_id = self.insert_attempt(ids=plan)
        slot1 = plan[0]
        # a path the shared helper counts, which the loader never attaches: the item is not presentable
        self.sql("UPDATE questions SET prompt = prompt || ' (на изображението)', image_path = ? WHERE id = ?",
                 (EXISTING_IMAGE, slot1))
        result = self.start()
        self.assertEqual((result.outcome, result.attempt_id, result.presentable_slots), ("resumed", attempt_id, tuple(range(2, 26))))
        # every other multiple-choice item stale for that reason alone: the resolved-visual item (slot 13) stays,
        # and so do the short answers
        for question_id in plan[:15]:
            self.sql("UPDATE questions SET prompt = prompt || ' (на изображението)', image_path = ? WHERE id = ?",
                     (EXISTING_IMAGE, question_id))
        self.assertEqual(self.start().presentable_slots, (13,) + tuple(range(16, 26)))
        # and when the short answers and the resolved-visual item are stale too, nothing is left: abandoned
        self.sql("UPDATE fill_in_subquestions SET correct_answer = '-', answer_alternatives = NULL")
        self.sql("DELETE FROM multiple_choice_options WHERE question_id = ?", (plan[12],))
        self.assertEqual(self.start().abandoned_attempt_id, attempt_id)

    def test_resume_neither_composes_pools_nor_reads_the_blueprint_nor_the_stimulus_list(self):
        first = self.start()
        plan = self.plan_of(first.attempt_id)
        self.sql("UPDATE dzi_blueprints SET is_active = 0")  # tampered blueprint: an open attempt still resumes
        with patch.object(part1, "compose_pools", side_effect=AssertionError("pools rebuilt")), \
                patch.object(part1, "read_active_blueprint", side_effect=AssertionError("blueprint read")), \
                patch.object(part1, "MISSING_STIMULUS_QUESTION_IDS", frozenset(plan)):  # a later list change
            result = self.start()
        self.assertEqual((result.outcome, result.presentable_slots), ("resumed", tuple(range(1, 26))))

    def test_a_reseeded_blueprint_changes_neither_the_stored_kinds_nor_the_points(self):
        first = self.start()
        stored = self.attempt(first.attempt_id)
        self.sql("UPDATE dzi_blueprint_slots SET points = 5, task_kind = 'short_answer' WHERE exam_part = 1")
        self.sql("UPDATE dzi_blueprints SET part1_minutes = 10")
        self.clock.advance(30)
        result = self.start()
        self.assertEqual((result.outcome, result.attempt_id), ("resumed", first.attempt_id))
        self.assertEqual(self.attempt(first.attempt_id), stored)

    def test_a_changed_grace_constant_changes_neither_state_nor_row(self):
        first = self.start()
        stored = self.attempt(first.attempt_id)
        for grace in (10, 60, 120, 3600):
            with self.subTest(grace=grace), patch.object(part1, "SUBMIT_GRACE_SECONDS", grace):
                # the same stored row at the same instant: grace at 5459 whatever the constant is
                self.clock.set(T0 + timedelta(seconds=5459))
                self.assertEqual(self.start().state, "grace")
                self.assertEqual(self.attempt(first.attempt_id), stored)
        with patch.object(part1, "SUBMIT_GRACE_SECONDS", 3600):
            self.clock.set(T0 + timedelta(seconds=5460))  # a longer constant does not revive the row
            self.assertEqual(self.start().expired_attempt_id, first.attempt_id)

    def test_the_grace_constant_only_shapes_attempts_started_afterwards(self):
        with patch.object(part1, "SUBMIT_GRACE_SECONDS", 120):
            result = self.start()
        row = self.attempt(result.attempt_id)
        self.assertEqual(row["submit_cutoff_at"], ts(T0 + timedelta(seconds=5520)))
        self.clock.set(T0 + timedelta(seconds=5500))
        self.assertEqual(self.start().state, "grace")  # judged by the stored cutoff, not the constant of 60

    def test_the_state_decision_at_the_stored_cutoff_with_a_historical_cutoff_unrelated_to_the_grace(self):
        for extra in (1, 60, 3600):
            with self.subTest(extra=extra):
                self.setUp()
                attempt_id = self.insert_attempt(started=T0 - timedelta(seconds=5400), cutoff_extra=extra)  # deadline = T0
                self.assertEqual(self.start().state, "grace" if extra > 0 else None)
                self.clock.advance(extra - 1)
                self.assertEqual(self.start().state, "grace")
                self.clock.advance(1)
                self.assertEqual(self.start().expired_attempt_id, attempt_id)


class FutureStartTest(Part1Case):
    def test_a_row_stamped_after_lifecycle_time_is_abandoned_and_a_new_attempt_follows(self):
        future = T0 + timedelta(seconds=90)
        attempt_id = self.insert_attempt(started=future)
        result = self.start()
        self.assertEqual((result.outcome, result.abandoned_attempt_id), ("created", attempt_id))
        old = self.attempt(attempt_id)
        self.assertEqual(old["abandoned_at"], ts(T0))
        self.assertLess(old["abandoned_at"], old["started_at"])  # stamped earlier than its own start: the table allows it
        self.assertEqual(self.attempt(result.attempt_id)["started_at"], ts(T0))
        self.assertEqual(self.clock_row()["high_water_at"], ts(T0))  # the marking admitted

    def test_a_future_row_is_not_resumable_and_its_marking_survives_a_refused_creation(self):
        attempt_id = self.insert_attempt(started=T0 + timedelta(seconds=90))
        self.sql("UPDATE dzi_blueprints SET is_active = 0")
        result = self.start()
        self.assertEqual((result.outcome, result.abandoned_attempt_id), ("blueprint_unavailable", attempt_id))
        self.assertEqual(self.attempt(attempt_id)["abandoned_at"], ts(T0))
        self.assertEqual(len(self.attempts()), 1)

    def test_an_unrelated_row_timestamp_never_invokes_check_factual_or_records_a_fault(self):
        other_future = self.insert_attempt(self.other, started=T0 + timedelta(days=2))
        own_future = self.insert_attempt(started=T0 + timedelta(days=1))
        with patch.object(lifecycle.LifecycleWrite, "check_factual", side_effect=AssertionError("check_factual")):
            result = self.start()
            self.assertEqual(result.abandoned_attempt_id, own_future)
            self.start(self.other)
        clock = self.clock_row()
        self.assertEqual((clock["state"], clock["state_reason"]), ("ok", None))  # no future_timestamp fault
        self.assertEqual(self.attempt(other_future)["abandoned_at"], ts(T0))

    def test_no_path_of_the_service_calls_check_factual(self):
        with patch.object(lifecycle.LifecycleWrite, "check_factual", side_effect=AssertionError("check_factual")):
            first = self.start()                       # create
            self.start()                               # resume
            self.clock.set(T0 + timedelta(seconds=5400))
            self.start()                               # grace
            self.clock.set(T0 + timedelta(seconds=5460))
            self.assertEqual(self.start().expired_attempt_id, first.attempt_id)  # expire + create


class MalformedRowTest(Part1Case):
    """A malformed or inconsistent stored pair fails closed: nothing written, the row stays open."""

    def assert_fails_closed(self, **fields):
        attempt_id = self.insert_attempt(**fields)
        before = self.state()
        clock = self.clock_row()
        with self.assertRaises(part1.Part1StateError):
            self.start()
        self.assertEqual(self.state(), before)
        self.assertEqual(self.clock_row(), clock)
        self.assertFalse(self.conn.in_transaction)
        self.assertIsNone(self.attempt(attempt_id)["abandoned_at"])
        self.sql("DELETE FROM dzi_part1_attempts WHERE id = ?", (attempt_id,))

    def test_malformed_plans(self):
        plan, _ = self.good_plan()
        cases = {
            "not JSON": {"question_ids_json": "[1,"}, "not an array": {"question_ids_json": '{"a": 1}'},
            "24 ids": {"question_ids_json": json.dumps(plan[:24])}, "26 ids": {"question_ids_json": json.dumps(plan + [999999])},
            "a duplicate": {"question_ids_json": json.dumps(plan[:24] + [plan[0]])},
            "a boolean": {"question_ids_json": json.dumps([True] + plan[1:])},
            "a float": {"question_ids_json": json.dumps([1.0] + plan[1:])},
            "a string": {"question_ids_json": json.dumps(["5"] + plan[1:])},
            "zero": {"question_ids_json": json.dumps([0] + plan[1:])}, "negative": {"question_ids_json": json.dumps([-4] + plan[1:])},
            "nested": {"question_ids_json": json.dumps([[plan[0]]] + plan[1:])},
            "oversized": {"question_ids_json": json.dumps([2 ** 70] + plan[1:])},
        }
        for name, fields in cases.items():
            with self.subTest(plan=name):
                self.assert_fails_closed(**fields)

    def test_malformed_slot_formats_and_inconsistent_pairs(self):
        _plan, blueprint = self.good_plan()
        good = [{"kind": s.kind, "points": s.points} for s in blueprint.slots]

        def with_entry(entry):
            return json.dumps([entry] + good[1:])

        cases = {
            "not JSON": {"slot_format_json": "nope"}, "not an array": {"slot_format_json": "{}"},
            "24 entries": {"slot_format_json": json.dumps(good[:24]), "planned": 44},
            "an extra key": {"slot_format_json": with_entry({"kind": "multiple_choice", "points": 1, "id": 5})},
            "a missing key": {"slot_format_json": with_entry({"kind": "multiple_choice"})},
            "an unknown kind": {"slot_format_json": with_entry({"kind": "practical_web", "points": 1})},
            "zero points": {"slot_format_json": with_entry({"kind": "multiple_choice", "points": 0}), "planned": 44},
            "boolean points": {"slot_format_json": with_entry({"kind": "multiple_choice", "points": True})},
            "float points": {"slot_format_json": with_entry({"kind": "multiple_choice", "points": 1.0})},
            "string points": {"slot_format_json": with_entry({"kind": "multiple_choice", "points": "1"})},
            "an entry that is not an object": {"slot_format_json": json.dumps([1] + good[1:])},
            "a saved maximum that is not the sum": {"planned": 46},
        }
        for name, fields in cases.items():
            with self.subTest(format=name):
                self.assert_fails_closed(**fields)

    def test_inconsistent_time_invariants(self):
        started = T0 - timedelta(seconds=100)
        cases = {
            "deadline is not started plus the duration": {"started": started, "deadline_at": ts(started + timedelta(seconds=5401))},
            "an unreadable calendar instant": {"started_at": "2026-13-45 00:00:00", "deadline_at": "2026-13-45 01:30:00",
                                               "submit_cutoff_at": "2026-13-45 01:31:00"},
            "a maximum integer duration (no overflow)": {"duration": 2 ** 63 - 1},
        }
        for name, fields in cases.items():
            with self.subTest(time=name):
                self.assert_fails_closed(**fields)

    def test_a_maximum_integer_duration_is_the_defined_bad_state_not_an_overflow(self):
        attempt_id = self.insert_attempt(started=T0 - timedelta(seconds=1), duration=2 ** 63 - 1,
                                         deadline_at=ts(T0 + timedelta(seconds=5)), submit_cutoff_at=ts(T0 + timedelta(seconds=9)))
        try:
            self.start()
        except part1.Part1StateError:
            pass
        except OverflowError:  # pragma: no cover
            self.fail("OverflowError")
        else:
            self.fail("expected Part1StateError")
        self.assertIsNone(self.attempt(attempt_id)["abandoned_at"])

    def test_a_well_formed_row_of_the_same_shape_is_accepted(self):
        attempt_id = self.insert_attempt(started=T0 - timedelta(seconds=100))
        self.assertEqual(self.start().attempt_id, attempt_id)

    def test_the_stored_pair_is_not_held_to_todays_blueprint_pattern(self):
        _plan, blueprint = self.good_plan()
        swapped = [{"kind": "short_answer" if i % 2 else "multiple_choice", "points": 3 if i % 2 else 1} for i in range(25)]
        attempt_id = self.insert_attempt(slot_format=json.dumps(swapped), planned=sum(s["points"] for s in swapped))
        result = self.start()
        self.assertEqual((result.outcome, result.attempt_id), ("resumed", attempt_id))  # parsed; items judged by stored kind


# =========================================================================== transactions

class AdmissionTest(Part1Case):
    """Read-only exits discard; every write admits exactly once, before its first write."""

    @contextmanager
    def watch_admit(self):
        record = {"calls": 0, "changes_at_admit": []}
        original = lifecycle.LifecycleWrite.admit

        def spy(transaction):
            record["calls"] += 1
            record["changes_at_admit"].append(transaction.conn.total_changes)
            return original(transaction)

        with patch.object(lifecycle.LifecycleWrite, "admit", spy):
            yield record

    def test_creation_admits_once_before_its_first_write(self):
        before = self.conn.total_changes
        with self.watch_admit() as record:
            self.assertTrue(self.start().created)
        self.assertEqual(record["calls"], 1)
        self.assertEqual(record["changes_at_admit"], [before])

    def test_mark_plus_insert_admits_once_not_twice(self):
        for case in ("expired", "future-start"):
            with self.subTest(case=case):
                self.setUp()
                if case == "expired":
                    self.start()
                    self.clock.advance(5460)
                else:
                    self.insert_attempt(started=T0 + timedelta(seconds=30))
                before = self.conn.total_changes
                with self.watch_admit() as record:
                    result = self.start()
                self.assertEqual(result.outcome, part1.OUTCOME_CREATED)
                self.assertIsNotNone(result.expired_attempt_id or result.abandoned_attempt_id)
                self.assertEqual(record["calls"], 1)
                self.assertEqual(record["changes_at_admit"], [before])

    def test_an_abandoned_stale_row_plus_insert_admits_once(self):
        first = self.start()
        plan = self.plan_of(first.attempt_id)
        # make only this attempt's items unpresentable; the rest of the pool stays far above the thresholds
        marks = ",".join(map(str, plan))
        self.sql(f"DELETE FROM multiple_choice_options WHERE question_id IN ({marks})")
        self.sql(f"UPDATE fill_in_subquestions SET correct_answer = '-', answer_alternatives = NULL WHERE question_id IN ({marks})")
        self.clock.advance(30)
        before = self.conn.total_changes
        with self.watch_admit() as record:
            result = self.start()
        self.assertEqual((result.outcome, result.abandoned_attempt_id), ("created", first.attempt_id))
        self.assertEqual((record["calls"], record["changes_at_admit"]), (1, [before]))

    def test_a_mark_alone_admits_once(self):
        self.start()
        self.clock.advance(5460)
        self.sql("UPDATE dzi_blueprints SET is_active = 0")
        before = self.conn.total_changes
        with self.watch_admit() as record:
            self.assertEqual(self.start().outcome, part1.OUTCOME_BLUEPRINT_UNAVAILABLE)
        self.assertEqual((record["calls"], record["changes_at_admit"]), (1, [before]))

    def test_read_only_outcomes_never_admit_and_leave_the_high_water_mark_alone(self):
        sleeper = self.add_account("student", active=False)
        first = self.start()
        mark = self.clock_row()
        cases = [
            ("resume in working time", lambda: (self.clock.advance(100), self.start())[1], "resumed"),
            ("resume in grace", lambda: (self.clock.set(T0 + timedelta(seconds=5430)), self.start())[1], "resumed"),
            ("inactive account", lambda: self.start(sleeper), "account_unavailable"),
        ]
        for name, action, outcome in cases:
            with self.subTest(case=name), self.watch_admit() as record:
                before = self.state()
                self.assertEqual(action().outcome, outcome)
                self.assertEqual(record["calls"], 0)
                self.assertEqual(self.clock_row()["high_water_at"], mark["high_water_at"])
                if name != "resume in working time" and name != "resume in grace":
                    self.assertEqual(self.state(), before)
        self.assertEqual(self.clock_row(), mark)
        # refused creation with nothing marked: blueprint and thin pool
        self.sql("UPDATE dzi_part1_attempts SET abandoned_at = ? WHERE id = ?", (ts(T0), first.attempt_id))
        for name, tamper in (("blueprint", "UPDATE dzi_blueprints SET is_active = 0"),
                             ("pool", "UPDATE questions SET is_ai_generated = 1, quality_score = 0 WHERE question_type = 'multiple_choice'")):
            with self.subTest(refused=name), self.watch_admit() as record:
                self.sql(tamper)
                before = self.state()
                self.assertIn(self.start().outcome, ("blueprint_unavailable", "pool_insufficient"))
                self.assertEqual((record["calls"], self.state()), (0, before))

    def test_a_read_only_outcome_does_not_raise_the_mark_even_when_the_wall_clock_moved_on(self):
        self.start()
        self.clock.advance(3000)
        self.start()
        self.assertEqual(self.clock_row()["high_water_at"], ts(T0))
        self.clock.advance(100)
        self.start()
        self.assertEqual(self.clock_row()["high_water_at"], ts(T0))

    def test_a_write_raises_the_mark_and_a_clock_moved_back_afterwards_is_refused(self):
        self.start()
        self.clock.advance(100)
        self.start(self.other)
        self.assertEqual(self.clock_row()["high_water_at"], ts(T0 + timedelta(seconds=100)))
        self.clock.step_wall(-3600)
        with self.assertRaises(lifecycle.ClockUnavailable) as caught:
            self.start(self.add_account("student"))
        self.assertEqual(caught.exception.reason, "clock_regression")


class WriteGuardTest(Part1Case):
    def test_a_marking_is_guarded_on_id_owner_and_open_state(self):
        mine = self.start()
        theirs = self.start(self.other)
        before = self.state()
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            with self.assertRaises(part1.Part1StateError):  # another owner's id
                part1._mark(self.conn, theirs.attempt_id, self.student.user_id, "abandoned_at", ts(T0))
            with self.assertRaises(part1.Part1StateError):  # no such id
                part1._mark(self.conn, 9999, self.student.user_id, "expired_at", ts(T0))
            part1._mark(self.conn, mine.attempt_id, self.student.user_id, "abandoned_at", ts(T0))
            with self.assertRaises(part1.Part1StateError):  # no longer open
                part1._mark(self.conn, mine.attempt_id, self.student.user_id, "abandoned_at", ts(T0 + timedelta(seconds=9)))
            with self.assertRaises(ValueError):  # only the two terminal columns the service stamps
                part1._mark(self.conn, mine.attempt_id, self.student.user_id, "submitted_at", ts(T0))
        finally:
            self.conn.rollback()
        self.assertEqual(self.state(), before)

    def test_a_decision_that_reports_no_write_is_discarded_even_if_a_statement_ran(self):
        """The wrapper, not the decision, guarantees a read-only exit leaves no trace (the mark included)."""
        self.clock.advance(10)  # so that a stray admission would really move the high-water mark
        before = self.state()

        def decide(conn, transaction, user_id):
            transaction.admit()  # a stray write that the decision does not report
            return part1.Part1Start(part1.OUTCOME_RESUMED), False

        with patch.object(part1, "_decide", decide):
            self.assertEqual(self.start().outcome, part1.OUTCOME_RESUMED)
        self.assertEqual(self.state(), before)
        self.assertEqual(self.clock_row()["high_water_at"], ts(T0))
        self.assertFalse(self.conn.in_transaction)


class RollbackAndLockTest(Part1Case):
    def make_lapsed(self):
        first = self.start()
        self.clock.advance(5460)
        return first

    def fail_inserts(self):
        self.sql("CREATE TRIGGER fail_part1_insert BEFORE INSERT ON dzi_part1_attempts "
                 "BEGIN SELECT RAISE(ABORT, 'insert refused'); END")

    def test_a_failed_second_write_rolls_back_the_mark_and_the_admission(self):
        first = self.make_lapsed()
        self.fail_inserts()
        before = self.state()
        with self.assertRaises(sqlite3.DatabaseError):
            self.start()
        self.assertEqual(self.state(), before)  # the expiry mark and the high-water admission are gone
        self.assertIsNone(self.attempt(first.attempt_id)["expired_at"])
        self.assertEqual(self.clock_row()["high_water_at"], ts(T0))
        self.assertFalse(self.conn.in_transaction)
        self.sql("DROP TRIGGER fail_part1_insert")  # the same call succeeds without the fault
        result = self.start()
        self.assertEqual((result.outcome, result.expired_attempt_id), ("created", first.attempt_id))

    def test_a_failed_mark_changes_nothing(self):
        first = self.make_lapsed()
        self.sql("CREATE TRIGGER fail_part1_update BEFORE UPDATE ON dzi_part1_attempts "
                 "BEGIN SELECT RAISE(ABORT, 'update refused'); END")
        before = self.state()
        with self.assertRaises(sqlite3.DatabaseError):
            self.start()
        self.assertEqual(self.state(), before)
        self.assertFalse(self.conn.in_transaction)
        self.assertIsNone(self.attempt(first.attempt_id)["expired_at"])

    def test_an_unexpected_failure_after_the_mark_rolls_everything_back(self):
        first = self.make_lapsed()
        before = self.state()
        with patch.object(part1, "compose_pools", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                self.start()
        self.assertEqual(self.state(), before)
        self.assertFalse(self.conn.in_transaction)
        self.assertIsNone(self.attempt(first.attempt_id)["expired_at"])

    def test_a_mark_is_kept_when_creation_is_refused(self):
        for name, tamper, outcome in (
                ("blueprint", "UPDATE dzi_blueprints SET is_active = 0", "blueprint_unavailable"),
                ("pool", "UPDATE questions SET is_ai_generated = 1, quality_score = 0 WHERE question_type <> 'multiple_choice'",
                 "pool_insufficient")):
            with self.subTest(refused=name):
                self.setUp()
                first = self.start()
                self.sql(tamper)
                self.clock.advance(5460)
                result = self.start()
                self.assertEqual((result.outcome, result.expired_attempt_id), (outcome, first.attempt_id))
                self.assertEqual(self.attempt(first.attempt_id)["expired_at"], ts(T0 + timedelta(seconds=5460)))
                self.assertEqual(self.clock_row()["high_water_at"], ts(T0 + timedelta(seconds=5460)))  # admitted, committed
                self.assertEqual(len(self.attempts()), 1)
                self.assertFalse(self.conn.in_transaction)

    def test_the_writer_lock_is_held_while_deciding(self):
        inside, release = threading.Event(), threading.Event()
        real_compose = part1.compose_pools
        results = {}

        def slow_compose(conn, blueprint):
            inside.set()
            self.assertTrue(release.wait(30))
            return real_compose(conn, blueprint)

        def run_first():
            conn = web_db.connect_db(self.db_path, timeout=30)
            try:
                results["first"] = part1.start_or_resume_part1(conn, self.student)
            finally:
                conn.close()

        with patch.object(part1, "compose_pools", slow_compose):
            thread = threading.Thread(target=run_first)
            thread.start()
            self.assertTrue(inside.wait(30))
            blocked = self.connect(timeout=0.2)
            before = self.attempts()
            with self.assertRaises(sqlite3.OperationalError):  # locked: the decision holds the writer lock
                part1.start_or_resume_part1(blocked, self.student)
            self.assertFalse(blocked.in_transaction)
            self.assertEqual(before, [])
            release.set()
            thread.join(30)
        self.assertEqual(results["first"].outcome, "created")
        again = part1.start_or_resume_part1(blocked, self.student)
        self.assertEqual((again.outcome, again.attempt_id), ("resumed", results["first"].attempt_id))

    def test_a_foreign_writer_blocks_the_start_and_nothing_is_written(self):
        holder = self.raw()
        holder.execute("BEGIN IMMEDIATE")
        try:
            blocked = self.connect(timeout=0.2)
            with self.assertRaises(sqlite3.OperationalError):
                part1.start_or_resume_part1(blocked, self.student)
            self.assertFalse(blocked.in_transaction)
        finally:
            holder.rollback()
        self.assertEqual(self.attempts(), [])

    def test_concurrent_starts_of_one_account_leave_one_open_attempt(self):
        barrier = threading.Barrier(4)
        results, errors = [], []

        def run():
            conn = web_db.connect_db(self.db_path, timeout=60)
            try:
                barrier.wait(30)
                results.append(part1.start_or_resume_part1(conn, self.student))
            except BaseException as exc:  # pragma: no cover
                errors.append(exc)
            finally:
                conn.close()

        threads = [threading.Thread(target=run) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(120)
        self.assertEqual(errors, [])
        self.assertEqual(sorted(r.outcome for r in results), ["created", "resumed", "resumed", "resumed"])
        self.assertEqual(len({r.attempt_id for r in results}), 1)
        self.assertEqual(len(self.attempts()), 1)

    def test_concurrent_starts_of_different_accounts_each_get_their_own_attempt(self):
        accounts = [self.student, self.other, self.add_account("teacher")]
        barrier = threading.Barrier(len(accounts))
        results = {}

        def run(who):
            conn = web_db.connect_db(self.db_path, timeout=60)
            try:
                barrier.wait(30)
                results[who.user_id] = part1.start_or_resume_part1(conn, who)
            finally:
                conn.close()

        threads = [threading.Thread(target=run, args=(who,)) for who in accounts]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(120)
        self.assertEqual({r.outcome for r in results.values()}, {"created"})
        self.assertEqual(len({r.attempt_id for r in results.values()}), 3)
        self.assertEqual(len(self.attempts()), 3)


class ClockContractTest(Part1Case):
    """An unusable clock refuses (the caller answers 503) and writes no Part 1 row."""

    def assert_refused(self, reason, *, who=None):
        before = {k: v for k, v in self.state().items() if k != "lifecycle_clock"}
        with self.assertRaises(lifecycle.ClockUnavailable) as caught:
            self.start(who)
        self.assertEqual(caught.exception.reason, reason)
        self.assertEqual({k: v for k, v in self.state().items() if k != "lifecycle_clock"}, before)
        self.assertFalse(self.conn.in_transaction)

    def test_regression_step_suspect_unsynchronised_and_unreadable_clock_rows(self):
        self.start()
        self.clock.step_wall(-60)
        self.assert_refused("clock_regression", who=self.other)
        self.assertEqual(self.clock_row()["state"], "suspect")
        self.clock.step_wall(60)
        self.assert_refused("clock_suspect", who=self.other)  # latched until an operator recovers
        self.assert_refused("clock_suspect")  # a read-only outcome still runs the clock check

    def test_a_forward_step_over_the_tolerance_on_one_boot_is_a_step_fault(self):
        self.start()
        self.clock.step_wall(61)
        self.assert_refused("clock_step", who=self.other)

    def test_unsynchronised_clock_and_reboot_input(self):
        self.sql("UPDATE lifecycle_clock SET ref_wall_at = NULL, ref_boot_id = NULL, ref_boottime_ms = NULL")
        self.clock.sync = False
        self.assert_refused("no_sync_evidence")
        self.assertEqual(self.clock_row()["state"], "suspect")  # more than ten minutes after boot
        self.assertEqual(self.attempts(), [])

    def test_a_new_boot_needs_a_synchronised_clock(self):
        self.start()
        self.clock.reboot()
        self.clock.sync = False
        self.assert_refused("no_sync_evidence", who=self.other)
        self.assertEqual(len(self.attempts()), 1)

    def test_an_unusable_clock_row_refuses_every_write(self):
        self.sql("DELETE FROM lifecycle_clock")
        self.assert_refused("clock_row_invalid")
        self.assertEqual(self.attempts(), [])

    def test_a_clock_fault_blocks_nothing_it_did_not_cause_when_the_clock_is_fine(self):
        self.start()
        self.assertEqual(self.clock_row()["state"], "ok")


if __name__ == "__main__":
    unittest.main()
