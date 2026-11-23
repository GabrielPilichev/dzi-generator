"""The dormant DZI Part 1 submit and grading service (C3.3): ``web.services.dzi_part1.submit_part1``.

Every database is a temporary clone of the migrations + canonical template
(``tests.support.build_test_database``) at a frozen lifecycle time (``tests.support.lifecycle_time``). No
connection opens ``data/questions.db`` and no test goes through the isolated-app connection guard. Building
the template runs the migration runner, whose default-path check resolves that path (filesystem metadata
only). Every expected value below is computed by an **oracle that does not use the service**: plain SQL over
the stored options and sub-answers, never ``grade_quiz_text_answers`` or the service's own helpers.

What is proved, and how:

* **Scoring**: both kinds, partly answered attempts, the effective maximum, part weights (a 1 + 2 item), the
  persisted rows against the stored plan and slot format (never a recomputation, never the slot's points once
  per part), a seeded random sweep against the oracle, normalization, alternatives and both grading modes;
* **Fields**: only the fixed names against the *stored* plan; unknown ids and non-parts ignored; duplicates
  unanswered; the 200-code-point / NUL bound applied to **every value** of a recognized text field, so a
  duplicate that holds one bad value still refuses the whole submit with no row and no clock write;
* **Outcomes and order**: owner-only lookup, terminal rows answered from the row alone, the stored cutoff
  (exclusive) judged before the plan is parsed, future-start, stale, empty, invalid input, idempotent retry;
* **Drift**: relevant current content (key, accepted answer, readiness, weights, type), the stimulus list and a
  re-seeded blueprint after the start, and a persisted result that nothing later can alter;
* **Transactions**: one admission before the first write, none for a no-write outcome, no ``check_factual``,
  a rollback of rows, terminal state and clock admission together, a real writer lock and separate-connection
  races between submit and start (both orders) and between two submits;
* **Clock and recovery**: refusals and the high-water mark with the real fixture clock, the two reviewed
  forward-fault sequences through the real operator CLI (``manage_lifecycle.main``), and, as characterizations
  that are **not guarantees**, the undetected-reboot late submit and the two-step same-boot case.
"""

from __future__ import annotations

import json
import random
import re
import sqlite3
import threading
import unittest
from contextlib import closing, contextmanager
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from werkzeug.datastructures import MultiDict

from tests.support import TEST_CLOCK, reset_lifecycle_clock_row
from tests.test_dzi_part1_clock_recovery import HOUR, RecoveryCase
from tests.test_dzi_part1_start_service import T0, Part1Case, ts
from web import db as web_db
from web.access import ANONYMOUS, account_principal
from web.services import dzi_part1 as part1
from web.services import lifecycle

CYR_A = "А"   # the stored option keys are Cyrillic: А Б В Г
LATIN_A = "A"
ROOT = Path(__file__).resolve().parents[1]


class DormancyTest(unittest.TestCase):
    def test_nothing_imports_the_service_and_no_simulation_route_exists(self):
        pattern = re.compile(r"(from\s+web\.services\.dzi_part1|from\s+web\.services\s+import[^\n]*\bdzi_part1\b"
                             r"|import\s+web\.services\.dzi_part1|from\s+\.dzi_part1|from\s+\.\s+import[^\n]*dzi_part1)")
        offenders = [str(path.relative_to(ROOT)) for base in ("web", "src") for path in (ROOT / base).rglob("*.py")
                     if path.name != "dzi_part1.py" and pattern.search(path.read_text(encoding="utf-8"))]
        self.assertEqual(offenders, [])
        from tests.support import web_app

        self.assertFalse([rule.rule for rule in web_app.app.url_map.iter_rules() if "simulation" in rule.rule])


class SubmitCase(Part1Case):
    """Fixtures and an independent oracle (plain SQL; never the service's own grading helpers)."""

    # ------------------------------------------------------------------ plumbing
    def rows(self, statement, params=()):
        with closing(self.raw()) as conn:
            return [dict(row) for row in conn.execute(statement, params)]

    def answers(self, attempt_id=None):
        if attempt_id is None:
            return self.rows("SELECT * FROM dzi_part1_answers ORDER BY attempt_id, slot_number, subquestion_number")
        return self.rows("SELECT * FROM dzi_part1_answers WHERE attempt_id = ? ORDER BY slot_number, subquestion_number",
                         (attempt_id,))

    def at(self, seconds):
        self.clock.set(T0 + timedelta(seconds=seconds))

    def begin(self, who=None):
        result = self.start(who)
        self.assertTrue(result.created)
        return result

    def submit(self, fields, attempt_id, who=None, conn=None):
        return part1.submit_part1(conn or self.conn, who or self.student, attempt_id, fields)

    def refused(self, fields, attempt_id, who=None):
        with self.assertRaises(lifecycle.ClockUnavailable) as caught:
            self.submit(fields, attempt_id, who)
        return caught.exception.reason

    def forced_attempt(self, forced, who=None, **kwargs):
        """A fixture open attempt whose plan has ``forced`` ({slot: question id}) in place (same kind swaps)."""
        plan, _blueprint = self.good_plan()
        for slot, question_id in forced.items():
            if question_id in plan:
                plan[plan.index(question_id)] = plan[slot - 1]
            plan[slot - 1] = question_id
        return self.insert_attempt(who, ids=plan, **kwargs)

    # ------------------------------------------------------------------ oracle
    def options(self, question_id):
        return self.rows("SELECT option_letter, is_correct FROM multiple_choice_options WHERE question_id = ? "
                         "ORDER BY option_letter", (question_id,))

    def right_letter(self, question_id):
        return next(row["option_letter"] for row in self.options(question_id) if row["is_correct"])

    def wrong_letter(self, question_id):
        return next(row["option_letter"] for row in self.options(question_id) if not row["is_correct"])

    def parts(self, question_id):
        return self.rows("SELECT subquestion_number, correct_answer, answer_alternatives, points FROM "
                         "fill_in_subquestions WHERE question_id = ? ORDER BY subquestion_number", (question_id,))

    @staticmethod
    def accepted(row):
        values = []
        for column in ("correct_answer", "answer_alternatives"):
            if row[column] is None:
                continue
            try:
                parsed = json.loads(row[column])
            except ValueError:
                parsed = row[column]
            values.extend(parsed if isinstance(parsed, list) else [parsed])
        return [str(value) for value in values]

    def order_independent(self, parts):
        sets = [frozenset(value.casefold() for value in self.accepted(row)) for row in parts]
        return len(parts) > 1 and all(item and item == sets[0] for item in sets)

    def right_texts(self, question_id):
        parts = self.parts(question_id)
        if self.order_independent(parts):
            distinct = list(dict.fromkeys(self.accepted(parts[0])))
            return {row["subquestion_number"]: distinct[index] for index, row in enumerate(parts)}
        return {row["subquestion_number"]: self.accepted(row)[0] for row in parts}

    def right_fields(self, attempt_id):
        fields = {}
        for question_id, kind in zip(self.plan_of(attempt_id), self.slot_kinds(attempt_id)):
            if kind == "multiple_choice":
                fields[f"q_{question_id}"] = self.right_letter(question_id)
            else:
                for number, text in self.right_texts(question_id).items():
                    fields[f"a_{question_id}_{number}"] = text
        return fields

    def short_answer_ids(self, attempt_id):
        return [q for q, kind in zip(self.plan_of(attempt_id), self.slot_kinds(attempt_id)) if kind == "short_answer"]

    def mc_ids(self, attempt_id):
        return [q for q, kind in zip(self.plan_of(attempt_id), self.slot_kinds(attempt_id)) if kind == "multiple_choice"]

    def single_part_item(self):
        return next(q for q in self.pools().short_answer if len(self.parts(q)) == 1)

    def weighted_item(self):
        """A short answer whose two parts weigh 1 and 2."""
        return next(q for q in self.pools().short_answer if sorted(r["points"] for r in self.parts(q)) == [1, 2])

    # ------------------------------------------------------------------ invariants
    def assert_follows_plan(self, attempt_id):
        """Every stored row agrees with the stored plan, the slot format and the item's own parts."""
        attempt = self.attempt(attempt_id)
        plan = json.loads(attempt["question_ids_json"])
        slots = json.loads(attempt["slot_format_json"])
        rows = self.answers(attempt_id)
        by_slot: dict[int, list[dict]] = {}
        for row in rows:
            by_slot.setdefault(row["slot_number"], []).append(row)
        for number, items in by_slot.items():
            slot = slots[number - 1]
            self.assertTrue(all(r["question_id"] == plan[number - 1] and r["kind"] == slot["kind"] for r in items), number)
            if slot["kind"] == "multiple_choice":
                self.assertEqual([(r["subquestion_number"], r["points_possible"]) for r in items], [(0, slot["points"])])
            else:
                weights = {p["subquestion_number"]: p["points"] for p in self.parts(plan[number - 1])}
                self.assertEqual({r["subquestion_number"]: r["points_possible"] for r in items}, weights)
                self.assertEqual(sum(r["points_possible"] for r in items), slot["points"])
        self.assertEqual(len({items[0]["question_id"] for items in by_slot.values()}), len(by_slot))
        for row in rows:
            self.assertEqual(row["points_awarded"], row["points_possible"] if row["is_correct"] else 0)
            self.assertEqual(row["grader_version"], part1.GRADER_VERSION)
        self.assertEqual(attempt["score_max"], sum(r["points_possible"] for r in rows))
        self.assertEqual(attempt["score_points"], sum(r["points_awarded"] for r in rows))
        return rows

    def no_write(self, call):
        """``call`` changes no table at all (the Part 1 tables and the clock row included)."""
        before = self.state()
        result = call()
        self.assertEqual(self.state(), before)
        self.assertFalse(self.conn.in_transaction)
        return result


# =========================================================================== scoring

class ScoringTest(SubmitCase):
    def test_all_correct_is_45_of_45_and_every_row_follows_the_plan(self):
        attempt = self.begin()
        result = self.submit(self.right_fields(attempt.attempt_id), attempt.attempt_id)
        self.assertEqual((result.outcome, result.score_points, result.score_max, result.attempt_id),
                         ("submitted", 45, 45, attempt.attempt_id))
        row = self.attempt(attempt.attempt_id)
        self.assertEqual((row["submitted_at"], row["score_points"], row["score_max"], row["max_points_planned"]),
                         (ts(T0), 45, 45, 45))
        self.assertEqual((row["expired_at"], row["abandoned_at"]), (None, None))
        rows = self.assert_follows_plan(attempt.attempt_id)
        parts = sum(len(self.parts(q)) for q in self.short_answer_ids(attempt.attempt_id))
        self.assertEqual((len(rows), len(result.answers)), (15 + parts, 15 + parts))
        self.assertTrue(all(r["is_correct"] == 1 for r in rows))
        self.assertEqual(result.submitted_at, ts(T0))

    def test_a_mix_of_right_wrong_and_blank_equals_the_oracle(self):
        attempt = self.begin()
        fields, expected = {}, 0
        mc, sa = self.mc_ids(attempt.attempt_id), self.short_answer_ids(attempt.attempt_id)
        for index, question_id in enumerate(mc):
            if index % 3 == 0:
                fields[f"q_{question_id}"] = self.right_letter(question_id)
                expected += 1
            elif index % 3 == 1:
                fields[f"q_{question_id}"] = self.wrong_letter(question_id)
        for index, question_id in enumerate(sa):
            right = self.right_texts(question_id)
            for number, weight in ((r["subquestion_number"], r["points"]) for r in self.parts(question_id)):
                if index % 2 == 0:
                    fields[f"a_{question_id}_{number}"] = right[number]
                    expected += weight
                else:
                    fields[f"a_{question_id}_{number}"] = "няма такъв отговор"
        result = self.submit(fields, attempt.attempt_id)
        self.assertEqual((result.outcome, result.score_points, result.score_max), ("submitted", expected, 45))
        self.assert_follows_plan(attempt.attempt_id)

    def test_a_partly_answered_attempt_keeps_zero_rows_with_their_possible_points(self):
        attempt = self.begin()
        first = self.mc_ids(attempt.attempt_id)[0]
        result = self.submit({f"q_{first}": self.right_letter(first)}, attempt.attempt_id)
        self.assertEqual((result.outcome, result.score_points, result.score_max), ("submitted", 1, 45))
        rows = self.assert_follows_plan(attempt.attempt_id)
        mc_rows = [r for r in rows if r["kind"] == "multiple_choice"]
        self.assertEqual((len(mc_rows), sum(1 for r in mc_rows if r["chosen_letter"] is None)), (15, 14))
        sa_rows = [r for r in rows if r["kind"] == "short_answer"]
        self.assertTrue(all(r["raw_answer"] is None and r["is_correct"] == 0 and r["points_awarded"] == 0 and
                            r["points_possible"] >= 1 for r in sa_rows))
        self.assertEqual(sum(r["points_possible"] for r in sa_rows), 30)

    def test_each_kind_alone_is_enough(self):
        mc_only, sa_only = self.begin(), self.begin(self.other)
        self.submit({f"q_{q}": self.right_letter(q) for q in self.mc_ids(mc_only.attempt_id)}, mc_only.attempt_id)
        self.assertEqual((self.attempt(mc_only.attempt_id)["score_points"], self.attempt(mc_only.attempt_id)["score_max"]), (15, 45))
        fields = {k: v for k, v in self.right_fields(sa_only.attempt_id).items() if k.startswith("a_")}
        self.submit(fields, sa_only.attempt_id, self.other)
        self.assertEqual((self.attempt(sa_only.attempt_id)["score_points"], self.attempt(sa_only.attempt_id)["score_max"]), (30, 45))

    def test_a_one_plus_two_item_carries_its_part_weights_not_the_slot_points(self):
        item = self.weighted_item()
        attempt_id = self.forced_attempt({16: item})
        weights = {r["subquestion_number"]: r["points"] for r in self.parts(item)}
        heavy = next(n for n, w in weights.items() if w == 2)
        result = self.submit({f"a_{item}_{heavy}": self.right_texts(item)[heavy]}, attempt_id)
        self.assertEqual((result.outcome, result.score_points, result.score_max), ("submitted", 2, 45))
        rows = [r for r in self.assert_follows_plan(attempt_id) if r["question_id"] == item]
        self.assertEqual(sorted((r["points_possible"], r["points_awarded"]) for r in rows), [(1, 0), (2, 2)])
        self.assertEqual(sum(r["points_possible"] for r in rows), 3)  # not 3 once per part

    def test_a_plan_total_of_45_is_never_105(self):
        attempt = self.begin()
        self.submit(self.right_fields(attempt.attempt_id), attempt.attempt_id)
        self.assertEqual(sum(r["points_possible"] for r in self.answers(attempt.attempt_id)), 45)

    def test_a_seeded_sweep_over_many_attempts_equals_the_oracle(self):
        rng = random.Random(20261123)
        for index in range(14):
            who = self.add_account("student")
            attempt = self.begin(who)
            fields, expected = {}, 0
            for question_id in self.mc_ids(attempt.attempt_id):
                mode = rng.choice(("right", "wrong", "none"))
                if mode == "right":
                    fields[f"q_{question_id}"] = self.right_letter(question_id)
                    expected += 1
                elif mode == "wrong":
                    fields[f"q_{question_id}"] = self.wrong_letter(question_id)
            for question_id in self.short_answer_ids(attempt.attempt_id):
                right = self.right_texts(question_id)
                for row in self.parts(question_id):
                    number = row["subquestion_number"]
                    mode = rng.choice(("right", "wrong", "none"))
                    if mode == "right":
                        fields[f"a_{question_id}_{number}"] = right[number]
                        expected += row["points"]
                    elif mode == "wrong":
                        fields[f"a_{question_id}_{number}"] = f"грешен {index}"
            first = self.mc_ids(attempt.attempt_id)[0]  # always at least one usable answer
            if f"q_{first}" not in fields:
                fields[f"q_{first}"] = self.right_letter(first)
                expected += 1
            result = self.submit(fields, attempt.attempt_id, who)
            self.assertEqual((result.outcome, result.score_points, result.score_max), ("submitted", expected, 45), index)
            self.assert_follows_plan(attempt.attempt_id)


# =========================================================================== grading rules

class GradingRulesTest(SubmitCase):
    """Normalization, alternatives and both matching modes, through the existing matcher (judged by the oracle)."""

    ORDERED, ALTERNATIVES, UNORDERED = 18, 17, 135

    def verdicts(self, attempt_id, question_id):
        return {r["subquestion_number"]: r["is_correct"] for r in self.answers(attempt_id) if r["question_id"] == question_id}

    def test_the_fixture_items_are_what_these_tests_assume(self):
        pools = self.pools().short_answer
        for question_id in (self.ORDERED, self.ALTERNATIVES, self.UNORDERED):
            self.assertIn(question_id, pools)
        self.assertEqual([self.accepted(r)[0] for r in self.parts(self.ORDERED)], ["Клас", "Извънкласна дейност", "Count"])
        self.assertIn("празен низ", self.accepted(self.parts(self.ALTERNATIVES)[2]))
        self.assertTrue(self.order_independent(self.parts(self.UNORDERED)))
        self.assertFalse(self.order_independent(self.parts(self.ORDERED)))

    def test_case_whitespace_and_nfkc_are_normalized_and_the_raw_text_is_stored_as_typed(self):
        attempt_id = self.forced_attempt({16: self.ORDERED})
        fields = {f"a_{self.ORDERED}_1": "  кЛАС  ", f"a_{self.ORDERED}_2": " извънкласна \t  дейност",
                  f"a_{self.ORDERED}_3": "ＣＯＵＮＴ"}  # fullwidth COUNT
        self.assertEqual(self.submit(fields, attempt_id).outcome, "submitted")
        self.assertEqual(self.verdicts(attempt_id, self.ORDERED), {1: 1, 2: 1, 3: 1})
        stored = {r["subquestion_number"]: r["raw_answer"] for r in self.answers(attempt_id) if r["question_id"] == self.ORDERED}
        self.assertEqual(stored[1], "  кЛАС  ")

    def test_there_is_no_typo_punctuation_or_word_tolerance(self):
        attempt_id = self.forced_attempt({16: self.ORDERED})
        fields = {f"a_{self.ORDERED}_1": "Класс", f"a_{self.ORDERED}_2": "Извънкласна", f"a_{self.ORDERED}_3": "Count."}
        self.assertEqual(self.submit({**fields, f"q_{self.mc_ids(attempt_id)[0]}": CYR_A}, attempt_id).outcome, "submitted")
        self.assertEqual(self.verdicts(attempt_id, self.ORDERED), {1: 0, 2: 0, 3: 0})

    def test_smart_quotes_are_folded(self):
        self.sql("UPDATE fill_in_subquestions SET correct_answer = ? WHERE question_id = ? AND subquestion_number = 1",
                 (json.dumps(["it's \"ok\""]), self.ORDERED))
        attempt_id = self.forced_attempt({16: self.ORDERED})
        self.assertEqual(self.submit({f"a_{self.ORDERED}_1": "it’s “ok”"}, attempt_id).outcome, "submitted")
        self.assertEqual(self.verdicts(attempt_id, self.ORDERED)[1], 1)

    def test_a_stored_alternative_is_accepted_as_well_as_the_main_answer(self):
        for text, expected in (("празен низ", 1), ("празна клетка", 1), ("празно", 0)):
            with self.subTest(text=text):
                self.setUp()
                attempt_id = self.forced_attempt({16: self.ALTERNATIVES})
                self.submit({f"a_{self.ALTERNATIVES}_3": text}, attempt_id)
                self.assertEqual(self.verdicts(attempt_id, self.ALTERNATIVES)[3], expected)

    def test_an_ordered_item_does_not_accept_swapped_parts(self):
        attempt_id = self.forced_attempt({16: self.ALTERNATIVES})
        self.submit({f"a_{self.ALTERNATIVES}_1": "20%", f"a_{self.ALTERNATIVES}_2": "5%"}, attempt_id)
        self.assertEqual(self.verdicts(attempt_id, self.ALTERNATIVES), {1: 0, 2: 0, 3: 0})

    def test_an_order_independent_item_accepts_any_order_but_each_answer_once(self):
        attempt_id = self.forced_attempt({16: self.UNORDERED})
        item = self.UNORDERED
        self.submit({f"a_{item}_1": "мениджър на хотела", f"a_{item}_2": "клиент", f"a_{item}_3": "рецепционист"}, attempt_id)
        self.assertEqual(self.verdicts(attempt_id, item), {1: 1, 2: 1, 3: 1})
        self.assertEqual(self.attempt(attempt_id)["score_points"], 3)

    def test_characterization_the_shared_matcher_lets_one_repeated_answer_match_once_per_part(self):
        """NOT a rule of this service: the existing matcher (``grade_quiz_text_answers``, order-independent mode) builds its
        pool of accepted answers from every part's list, so one accepted value typed in all three parts is accepted three
        times. The service reuses the matcher unchanged (scope), so it scores 3 of 3 here. Pinned so that a change to the
        shared matcher is a deliberate, reviewed edit; reported as a pre-existing finding."""
        item = self.UNORDERED
        attempt_id = self.forced_attempt({16: item})
        self.submit({f"a_{item}_1": "клиент", f"a_{item}_2": "клиент", f"a_{item}_3": "Клиент"}, attempt_id)
        self.assertEqual(self.verdicts(attempt_id, item), {1: 1, 2: 1, 3: 1})
        # the same behavior through the shared matcher itself, with no Part 1 code involved
        from web.services.quiz import grade_quiz_text_answers

        accepted = {n: self.accepted(row) for n, row in ((r["subquestion_number"], r) for r in self.parts(item))}
        graded = grade_quiz_text_answers({1: "клиент", 2: "клиент", 3: "клиент"}, accepted, grading_mode="order_independent")
        self.assertEqual([r["is_correct"] for r in graded], [True, True, True])

    def test_a_multiple_choice_answer_is_the_stored_key_and_nothing_else(self):
        attempt = self.begin()
        question_id = self.mc_ids(attempt.attempt_id)[0]
        right = self.right_letter(question_id)
        others = [r["option_letter"] for r in self.options(question_id) if r["option_letter"] != right]
        self.assertEqual(len(others), 3)
        self.submit({f"q_{question_id}": others[0]}, attempt.attempt_id)
        row = next(r for r in self.answers(attempt.attempt_id) if r["question_id"] == question_id)
        self.assertEqual((row["chosen_letter"], row["is_correct"], row["points_awarded"], row["points_possible"]),
                         (others[0], 0, 0, 1))


# =========================================================================== fields

class FieldRulesTest(SubmitCase):
    def setUp(self):
        super().setUp()
        self.attempt_id = self.begin().attempt_id
        self.plan = self.plan_of(self.attempt_id)
        self.first_mc = self.mc_ids(self.attempt_id)[0]
        self.valid = {f"q_{self.first_mc}": self.right_letter(self.first_mc)}

    def assert_untouched(self, fields):
        outcome = self.no_write(lambda: self.submit(fields, self.attempt_id))
        self.assertEqual(self.attempt(self.attempt_id)["submitted_at"], None)
        self.assertEqual(self.answers(), [])
        return outcome

    def test_unknown_fields_and_ids_outside_the_plan_are_ignored(self):
        outside = next(q for q in self.pools().multiple_choice if q not in self.plan)
        extras = {f"q_{outside}": self.right_letter(outside), "user_id": "9", "owner": "1", "score_points": "45",
                  "score_max": "45", "points": "3", "seed": "x", "submitted_at": "2000-01-01 00:00:00",
                  "attempt_id": "999", "question_ids_json": "[1]", "q_0": CYR_A, "q_-1": CYR_A, "Q_5": CYR_A}
        result = self.submit({**self.valid, **extras}, self.attempt_id)
        self.assertEqual((result.outcome, result.score_points, result.score_max), ("submitted", 1, 45))
        rows = self.assert_follows_plan(self.attempt_id)
        self.assertNotIn(outside, [r["question_id"] for r in rows])
        row = self.attempt(self.attempt_id)
        self.assertEqual((row["user_id"], row["seed"], row["submitted_at"]), (self.student.user_id, row["seed"], ts(T0)))
        self.assertEqual(row["question_ids_json"], json.dumps(self.plan))

    def test_only_unknown_fields_is_the_empty_outcome(self):
        outside = next(q for q in self.pools().multiple_choice if q not in self.plan)
        outcome = self.assert_untouched({f"q_{outside}": self.right_letter(outside), "score_points": "45"})
        self.assertEqual(outcome.outcome, "empty")

    def test_names_of_another_shape_are_not_fields(self):
        sa = self.short_answer_ids(self.attempt_id)[0]
        names = [f"q_0{self.first_mc}", f"q_{self.first_mc} ", f" q_{self.first_mc}", f"Q_{self.first_mc}",
                 f"q_{self.first_mc}_1", f"q_١٢", f"a_{sa}_01", f"a_{sa}_1x", f"a_{sa}", f"a_{sa}_1_2",
                 "q_" + "9" * 5000, f"a_{sa}_" + "1" * 5000, "a_" + "1" * 5000 + "_1"]
        outcome = self.assert_untouched({name: CYR_A for name in names})
        self.assertEqual(outcome.outcome, "empty")

    def test_a_field_of_the_other_kind_is_not_recognized(self):
        sa = self.short_answer_ids(self.attempt_id)[0]
        outcome = self.assert_untouched({f"q_{sa}": CYR_A, f"a_{self.first_mc}_1": "клас", f"a_{self.first_mc}_0": "клас"})
        self.assertEqual(outcome.outcome, "empty")

    def test_a_part_that_is_not_a_stored_part_is_ignored_and_adds_no_row(self):
        item = self.single_part_item()
        attempt_id = self.forced_attempt({16: item}, self.other)
        fields = {f"q_{self.mc_ids(attempt_id)[0]}": self.right_letter(self.mc_ids(attempt_id)[0]),
                  f"a_{item}_1": "x", f"a_{item}_2": "y", f"a_{item}_9": "z", f"a_{item}_0": "w"}
        self.submit(fields, attempt_id, self.other)
        rows = [r for r in self.answers(attempt_id) if r["question_id"] == item]
        self.assertEqual([r["subquestion_number"] for r in rows], [1])
        self.assertEqual(rows[0]["raw_answer"], "x")
        self.assert_follows_plan(attempt_id)

    def test_a_non_part_is_ignored_even_with_an_over_long_value(self):
        item = self.single_part_item()
        attempt_id = self.forced_attempt({16: item}, self.other)
        outside = next(q for q in self.pools().short_answer if q not in self.plan_of(attempt_id))
        first = self.mc_ids(attempt_id)[0]
        fields = {f"q_{first}": self.right_letter(first), f"a_{item}_9": "x" * 500, f"a_{item}_0": "\x00",
                  f"a_{outside}_1": "x" * 500, f"a_{first}_1": "x" * 500}
        self.assertEqual(self.submit(fields, attempt_id, self.other).outcome, "submitted")

    def test_a_duplicate_field_is_unanswered_even_when_both_values_are_right(self):
        sa = self.short_answer_ids(self.attempt_id)[0]
        right = self.right_texts(sa)
        fields = {**self.valid, f"a_{sa}_1": (right[1], right[1]),
                  f"q_{self.mc_ids(self.attempt_id)[1]}": (self.right_letter(self.mc_ids(self.attempt_id)[1]),) * 2}
        result = self.submit(fields, self.attempt_id)
        self.assertEqual((result.outcome, result.score_points), ("submitted", 1))
        rows = self.answers(self.attempt_id)
        self.assertIsNone(next(r for r in rows if r["question_id"] == sa and r["subquestion_number"] == 1)["raw_answer"])
        self.assertIsNone(next(r for r in rows if r["question_id"] == self.mc_ids(self.attempt_id)[1])["chosen_letter"])

    def test_a_multidict_is_read_through_getlist_so_a_duplicate_stays_a_duplicate(self):
        form = MultiDict([(f"q_{self.first_mc}", self.right_letter(self.first_mc)), (f"q_{self.first_mc}", CYR_A)])
        self.assertEqual(self.assert_untouched(form).outcome, "empty")

    # ---------------------------------------------------------------- the text bound
    def text_field(self):
        sa = self.short_answer_ids(self.attempt_id)[0]
        return sa, f"a_{sa}_1"

    def test_the_bound_is_200_code_points_and_no_nul(self):
        _sa, name = self.text_field()
        for label, value, outcome in (("200 ascii", "x" * 200, "submitted"), ("201 ascii", "x" * 201, "invalid_input"),
                                      ("nul alone", "\x00", "invalid_input"), ("nul at start", "\x00ab", "invalid_input"),
                                      ("nul in the middle", "a\x00b", "invalid_input"), ("nul at the end", "ab\x00", "invalid_input"),
                                      ("200 four-byte code points", "\U0001F600" * 200, "submitted"),
                                      ("201 four-byte code points", "\U0001F600" * 201, "invalid_input"),
                                      ("200 cyrillic", "ж" * 200, "submitted")):
            with self.subTest(label):
                self.setUp()
                attempt_id = self.attempt_id
                sa = self.short_answer_ids(attempt_id)[0]
                fields = {f"q_{self.mc_ids(attempt_id)[0]}": CYR_A, f"a_{sa}_1": value}
                before = self.state()
                result = self.submit(fields, attempt_id)
                self.assertEqual(result.outcome, outcome)
                if outcome == "invalid_input":
                    self.assertEqual(self.state(), before)           # no row, no terminal state, no clock write
                    self.assertEqual(self.answers(), [])
                    self.assertEqual(self.attempt(attempt_id)["submitted_at"], None)
                else:
                    self.assertEqual(next(r["raw_answer"] for r in self.answers(attempt_id)
                                          if r["question_id"] == sa and r["subquestion_number"] == 1), value)

    def test_an_invalid_value_refuses_the_whole_submit_even_with_valid_answers_beside_it(self):
        _sa, name = self.text_field()
        self.at(30)
        before = self.state()
        result = self.submit({**self.valid, name: "x" * 201}, self.attempt_id)
        self.assertEqual(result.outcome, "invalid_input")
        self.assertEqual(self.state(), before)
        self.assertEqual(self.clock_row()["high_water_at"], ts(T0))  # not admitted
        self.assertEqual(self.submit(self.valid, self.attempt_id).outcome, "submitted")  # the attempt stayed open

    def test_the_bound_applies_to_every_value_of_a_duplicate_before_it_counts_as_unanswered(self):
        """One bad value among legal duplicates, beside another valid planned answer: refused, nothing written."""
        sa, name = self.text_field()
        right = self.right_texts(sa)[1]
        second_mc = self.mc_ids(self.attempt_id)[1]
        bad_values = {"201 first": ("x" * 201, right), "201 last": (right, "x" * 201), "201 middle": (right, "x" * 201, right),
                      "nul first": ("\x00", right), "nul last": (right, "a\x00"), "both bad": ("x" * 201, "\x00"),
                      "201 in a list value": ["x" * 201, right]}
        self.at(30)
        for label, values in bad_values.items():
            with self.subTest(label):
                before = self.state()
                fields = {**self.valid, f"q_{second_mc}": self.right_letter(second_mc), name: values}
                result = self.submit(fields, self.attempt_id)
                self.assertEqual(result.outcome, "invalid_input")
                self.assertEqual(self.state(), before)                 # no answer row, no state, no clock write
                self.assertEqual(self.answers(), [])
                self.assertEqual(self.clock_row()["high_water_at"], ts(T0))
                self.assertFalse(self.conn.in_transaction)
        # the same request as a MultiDict (duplicates through getlist) is refused the same way
        form = MultiDict([(f"q_{self.first_mc}", self.right_letter(self.first_mc)), (name, right), (name, "x" * 201)])
        self.assertEqual(self.no_write(lambda: self.submit(form, self.attempt_id)).outcome, "invalid_input")

    def test_legal_duplicates_are_unanswered_and_two_valid_values_are_not_refused(self):
        sa, name = self.text_field()
        right = self.right_texts(sa)[1]
        for values in ((right, right), ("x" * 200, right), ("a", "b", "c")):
            with self.subTest(values=values):
                self.setUp()
                attempt_id = self.attempt_id
                item = self.short_answer_ids(attempt_id)[0]
                first = self.mc_ids(attempt_id)[0]
                result = self.submit({f"q_{first}": self.right_letter(first), f"a_{item}_1": values}, attempt_id)
                self.assertEqual(result.outcome, "submitted")
                self.assertIsNone(next(r["raw_answer"] for r in self.answers(attempt_id)
                                       if r["question_id"] == item and r["subquestion_number"] == 1))

    def test_a_bad_value_in_a_duplicate_of_an_unrecognized_name_is_ignored(self):
        outside = next(q for q in self.pools().short_answer if q not in self.plan)
        sa, _name = self.text_field()
        fields = {**self.valid, f"a_{outside}_1": ("x" * 201, "\x00"), f"a_{sa}_9": ("x" * 201, "\x00"),
                  f"a_{self.first_mc}_1": ("x" * 300, "x")}
        self.assertEqual(self.submit(fields, self.attempt_id).outcome, "submitted")

    def test_a_long_multiple_choice_value_is_unanswered_not_refused_and_never_stored(self):
        fields = {f"q_{self.first_mc}": "Ж" * 5000, **{f"q_{self.mc_ids(self.attempt_id)[1]}": self.right_letter(self.mc_ids(self.attempt_id)[1])}}
        self.assertEqual(self.submit(fields, self.attempt_id).outcome, "submitted")
        row = next(r for r in self.answers(self.attempt_id) if r["question_id"] == self.first_mc)
        self.assertIsNone(row["chosen_letter"])
        self.assertNotIn("Ж" * 50, json.dumps(self.answers(self.attempt_id), ensure_ascii=False))

    def test_an_over_long_value_for_a_recognized_but_unpresentable_item_still_refuses(self):
        sa, name = self.text_field()
        self.sql("UPDATE questions SET source_number = 27 WHERE id = ?", (sa,))  # no longer ready: not presentable
        self.assertEqual(self.no_write(lambda: self.submit({**self.valid, name: "x" * 201}, self.attempt_id)).outcome,
                         "invalid_input")

    # ---------------------------------------------------------------- blank, invalid keys, non-text
    def test_blank_text_is_not_usable_and_is_stored_as_no_answer(self):
        sa, name = self.text_field()
        self.assertEqual(self.assert_untouched({name: "   ", f"a_{sa}_2": "\t\n"}).outcome, "empty")
        result = self.submit({**self.valid, name: "   "}, self.attempt_id)
        self.assertEqual(result.outcome, "submitted")
        self.assertIsNone(next(r["raw_answer"] for r in self.answers(self.attempt_id)
                               if r["question_id"] == sa and r["subquestion_number"] == 1))

    def test_an_invalid_option_key_is_unanswered_and_never_stored(self):
        other_mc = self.mc_ids(self.attempt_id)[1]
        for label, value in (("latin A for cyrillic A", LATIN_A), ("empty", ""), ("lowercase", "а"), ("unknown", "Ж"),
                             ("two keys", "АБ"), ("padded", " А"), ("a number", "1")):
            with self.subTest(label):
                self.setUp()
                attempt_id = self.attempt_id
                question_id = self.mc_ids(attempt_id)[0]
                self.assertEqual(self.no_write(lambda: self.submit({f"q_{question_id}": value}, attempt_id)).outcome, "empty")
                right = self.right_letter(self.mc_ids(attempt_id)[1])
                self.submit({f"q_{question_id}": value, f"q_{self.mc_ids(attempt_id)[1]}": right}, attempt_id)
                row = next(r for r in self.answers(attempt_id) if r["question_id"] == question_id)
                self.assertEqual((row["chosen_letter"], row["is_correct"]), (None, 0))

    def test_the_key_of_another_question_is_not_a_key(self):
        a, b = self.mc_ids(self.attempt_id)[:2]
        other_keys = {r["option_letter"] for r in self.options(b)} - {r["option_letter"] for r in self.options(a)}
        if other_keys:
            self.assertEqual(self.no_write(lambda: self.submit({f"q_{a}": sorted(other_keys)[0]}, self.attempt_id)).outcome, "empty")

    def test_values_that_are_not_text_are_unanswered(self):
        sa, name = self.text_field()
        fields = {f"q_{self.first_mc}": 5, name: b"\xd0\x90", f"q_{self.mc_ids(self.attempt_id)[1]}": None}
        self.assertEqual(self.assert_untouched(fields).outcome, "empty")

    def test_no_unrelated_text_or_correct_answer_is_ever_stored(self):
        sentinel = "SENTINEL-ЖЖ-9f3a"
        result = self.submit({**self.valid, "note": sentinel, "comment": (sentinel, sentinel), f"q_{self.plan[20]}": sentinel,
                              "a_999999_1": sentinel}, self.attempt_id)
        self.assertEqual(result.outcome, "submitted")
        with closing(self.raw()) as conn:
            tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' "
                                                 "AND name IN ('dzi_part1_attempts', 'dzi_part1_answers', 'lifecycle_clock')")]
            dump = json.dumps([[tuple(row) for row in conn.execute(f'SELECT * FROM "{t}"')] for t in tables], ensure_ascii=False)
            columns = [r[1] for r in conn.execute("PRAGMA table_info(dzi_part1_answers)")]
        self.assertNotIn("SENTINEL", dump)
        self.assertEqual(columns, ["attempt_id", "slot_number", "subquestion_number", "question_id", "kind", "chosen_letter",
                                   "raw_answer", "is_correct", "points_possible", "points_awarded", "grader_version"])
        for row in self.answers(self.attempt_id):  # an unanswered part keeps no accepted answer behind
            if row["kind"] == "short_answer":
                self.assertIsNone(row["raw_answer"])


# =========================================================================== outcomes and order

class OutcomesTest(SubmitCase):
    def test_a_foreign_and_an_absent_attempt_are_the_same_not_found_and_write_nothing(self):
        attempt = self.begin()
        fields = self.right_fields(attempt.attempt_id)
        teacher, admin = self.add_account("teacher"), self.add_account("admin")
        for who, attempt_id in ((self.other, attempt.attempt_id), (teacher, attempt.attempt_id), (admin, attempt.attempt_id),
                                (self.student, 999999), (self.student, 0), (self.student, -4), (self.student, 2 ** 63),
                                (self.other, 2 ** 70)):
            with self.subTest(who=who.role, attempt_id=attempt_id):
                result = self.no_write(lambda: self.submit(fields, attempt_id, who))
                self.assertEqual((result.outcome, result.attempt_id, result.score_points, result.answers), ("not_found", None, None, ()))
        self.assertEqual(self.attempt(attempt.attempt_id)["submitted_at"], None)

    def test_only_an_account_principal_may_submit(self):
        attempt = self.begin()
        with self.assertRaises(part1.Part1AccessError):
            self.submit({}, attempt.attempt_id, ANONYMOUS)
        with self.assertRaises(part1.Part1AccessError):
            self.submit({}, attempt.attempt_id, "student")

    def test_an_inactive_or_deactivated_account_is_unavailable_under_the_lock(self):
        attempt = self.begin()
        fields = self.right_fields(attempt.attempt_id)
        self.sql("UPDATE users SET is_active = 0 WHERE id = ?", (self.student.user_id,))
        result = self.no_write(lambda: self.submit(fields, attempt.attempt_id))
        self.assertEqual(result.outcome, "account_unavailable")
        inactive = self.add_account("student", active=False)
        self.assertEqual(self.submit(fields, attempt.attempt_id, inactive).outcome, "account_unavailable")
        self.assertEqual(self.answers(), [])

    def test_argument_and_connection_errors_are_exceptions_and_write_nothing(self):
        attempt = self.begin()
        before = self.state()
        for bad_id in (True, "1", None, 1.0):
            with self.subTest(attempt_id=bad_id), self.assertRaises(ValueError):
                self.submit({}, bad_id)
        for bad_fields in (5, None, [("q_1", "A")], "q_1=A"):
            with self.subTest(fields=bad_fields), self.assertRaises(ValueError):
                self.submit(bad_fields, attempt.attempt_id)
        plain = sqlite3.connect(self.db_path)
        self.addCleanup(plain.close)
        with self.assertRaises(RuntimeError):  # no Row factory, foreign keys off
            self.submit({}, attempt.attempt_id, conn=plain)
        auto = sqlite3.connect(self.db_path, autocommit=True)
        auto.row_factory = sqlite3.Row
        self.addCleanup(auto.close)
        with self.assertRaises(RuntimeError):
            self.submit({}, attempt.attempt_id, conn=auto)
        busy = self.connect()
        busy.execute("BEGIN")
        with self.assertRaises(RuntimeError):
            self.submit({}, attempt.attempt_id, conn=busy)
        busy.rollback()
        self.assertEqual(self.state(), before)

    def test_an_empty_submission_leaves_the_attempt_open_and_writes_nothing(self):
        attempt = self.begin()
        self.at(40)
        for fields in ({}, {"x": "y"}, {f"q_{self.mc_ids(attempt.attempt_id)[0]}": ""}):
            with self.subTest(fields=fields):
                result = self.no_write(lambda: self.submit(fields, attempt.attempt_id))
                self.assertEqual((result.outcome, result.attempt_id), ("empty", attempt.attempt_id))
        self.assertEqual(self.clock_row()["high_water_at"], ts(T0))
        self.assertEqual(self.submit(self.right_fields(attempt.attempt_id), attempt.attempt_id).outcome, "submitted")

    def test_a_retry_is_idempotent_and_changes_no_table_not_even_the_clock_row(self):
        attempt = self.begin()
        first = self.submit(self.right_fields(attempt.attempt_id), attempt.attempt_id)
        stored = (self.attempt(attempt.attempt_id), self.answers())
        self.at(500)
        for fields, conn in (({}, None), (self.right_fields(attempt.attempt_id), None), ({"q_1": "x"}, self.connect())):
            retry = self.no_write(lambda: self.submit(fields, attempt.attempt_id, conn=conn))
            self.assertEqual((retry.outcome, retry.score_points, retry.score_max, retry.submitted_at, retry.answers),
                             ("already_submitted", first.score_points, first.score_max, first.submitted_at, ()))
        self.assertEqual((self.attempt(attempt.attempt_id), self.answers()), stored)

    def test_a_submitted_attempt_stays_submitted_long_after_its_cutoff(self):
        attempt = self.begin()
        self.submit(self.right_fields(attempt.attempt_id), attempt.attempt_id)
        self.at(10 * 3600)
        self.assertEqual(self.no_write(lambda: self.submit({}, attempt.attempt_id)).outcome, "already_submitted")

    def test_an_abandoned_attempt_is_refused_whatever_the_time_and_writes_nothing(self):
        attempt = self.begin()
        fields = self.right_fields(attempt.attempt_id)
        # an earlier abandonment (a false stamp in the future) depends on the row only
        self.sql("UPDATE dzi_part1_attempts SET abandoned_at = ? WHERE id = ?", (ts(T0 + timedelta(hours=10)), attempt.attempt_id))
        for seconds in (10, 5399, 5459, 5460, 10 * 3600 - 1, 10 * 3600, 12 * 3600):
            with self.subTest(seconds=seconds):
                self.at(seconds)
                result = self.no_write(lambda: self.submit(fields, attempt.attempt_id))
                self.assertEqual(result.outcome, "abandoned")
        self.assertEqual(self.answers(), [])
        row = self.attempt(attempt.attempt_id)
        self.assertEqual((row["submitted_at"], row["expired_at"], row["score_points"]), (None, None, None))

    def test_an_expired_row_is_refused_and_never_revived_or_rewritten(self):
        attempt = self.begin()
        self.sql("UPDATE dzi_part1_attempts SET expired_at = ? WHERE id = ?", (ts(T0 + timedelta(seconds=5460)), attempt.attempt_id))
        for seconds in (10, 5400, 7000):
            self.at(seconds)
            self.assertEqual(self.no_write(lambda: self.submit(self.right_fields(attempt.attempt_id), attempt.attempt_id)).outcome,
                             "expired")
        self.assertEqual(self.answers(), [])

    def test_the_next_start_follows_a_submission_and_an_old_tab_cannot_touch_the_new_attempt(self):
        first = self.begin()
        self.submit(self.right_fields(first.attempt_id), first.attempt_id)
        second = self.begin()
        self.assertNotEqual(second.attempt_id, first.attempt_id)
        stale_tab = self.no_write(lambda: self.submit(self.right_fields(second.attempt_id), first.attempt_id))
        self.assertEqual(stale_tab.outcome, "already_submitted")
        self.assertEqual(self.attempt(second.attempt_id)["submitted_at"], None)
        self.assertEqual(self.submit(self.right_fields(second.attempt_id), second.attempt_id).outcome, "submitted")

    def test_an_invalid_or_empty_attempt_can_still_be_submitted_afterwards(self):
        attempt = self.begin()
        sa = self.short_answer_ids(attempt.attempt_id)[0]
        self.assertEqual(self.submit({f"a_{sa}_1": "x" * 201}, attempt.attempt_id).outcome, "invalid_input")
        self.assertEqual(self.submit({}, attempt.attempt_id).outcome, "empty")
        self.assertEqual(self.submit(self.right_fields(attempt.attempt_id), attempt.attempt_id).outcome, "submitted")


# =========================================================================== time

class TimeTest(SubmitCase):
    """The stored cutoff, exclusive, with a start at T0: deadline T0 + 5400, cutoff T0 + 5460."""

    def test_before_the_deadline_at_it_and_in_grace_are_accepted_and_the_cutoff_is_exclusive(self):
        cases = ((1, "submitted"), (5399, "submitted"), (5400, "submitted"), (5459, "submitted"),
                 (5460, "expired"), (5461, "expired"), (86400, "expired"))
        for seconds, outcome in cases:
            with self.subTest(seconds=seconds):
                self.setUp()
                attempt = self.begin()
                self.at(seconds)
                result = self.submit(self.right_fields(attempt.attempt_id), attempt.attempt_id)
                self.assertEqual(result.outcome, outcome)
                row = self.attempt(attempt.attempt_id)
                if outcome == "submitted":
                    self.assertEqual((row["submitted_at"], row["expired_at"], row["score_points"]), (ts(T0 + timedelta(seconds=seconds)), None, 45))
                    self.assert_follows_plan(attempt.attempt_id)
                else:
                    self.assertEqual((row["submitted_at"], row["expired_at"], row["score_points"], row["score_max"]),
                                     (None, ts(T0 + timedelta(seconds=seconds)), None, None))
                    self.assertEqual(self.answers(), [])
                    self.assertEqual(self.clock_row()["high_water_at"], ts(T0 + timedelta(seconds=seconds)))  # the marking admitted

    def test_the_expiry_is_marked_once_and_a_retry_writes_nothing(self):
        attempt = self.begin()
        self.at(5460)
        self.assertEqual(self.submit(self.right_fields(attempt.attempt_id), attempt.attempt_id).outcome, "expired")
        marked = self.attempt(attempt.attempt_id)
        self.at(9000)
        for _ in range(2):
            self.assertEqual(self.no_write(lambda: self.submit(self.right_fields(attempt.attempt_id), attempt.attempt_id)).outcome, "expired")
        self.assertEqual(self.attempt(attempt.attempt_id), marked)

    def test_a_lapsed_submit_is_judged_from_time_alone_whatever_the_fields(self):
        attempt = self.begin()
        sa = self.short_answer_ids(attempt.attempt_id)[0]
        self.at(5460)
        result = self.submit({f"a_{sa}_1": "x" * 5000, "q_1": "\x00"}, attempt.attempt_id)  # would be invalid input in time
        self.assertEqual(result.outcome, "expired")
        self.assertEqual(self.answers(), [])

    def test_the_grace_constant_never_changes_a_stored_row(self):
        for grace in (1, 600):
            with self.subTest(grace=grace):
                self.setUp()
                attempt = self.begin()
                with patch.object(part1, "SUBMIT_GRACE_SECONDS", grace):
                    self.at(5459)
                    outcome_in_grace = self.submit({}, attempt.attempt_id).outcome
                    self.at(5460)
                    outcome_at_cutoff = self.submit(self.right_fields(attempt.attempt_id), attempt.attempt_id).outcome
                self.assertEqual((outcome_in_grace, outcome_at_cutoff), ("empty", "expired"))

    def test_a_historical_cutoff_unrelated_to_the_grace_decides(self):
        for extra, seconds, outcome in ((10, 5409, "submitted"), (10, 5410, "expired"), (600, 5460, "submitted"),
                                        (600, 5999, "submitted"), (600, 6000, "expired")):
            with self.subTest(cutoff_extra=extra, seconds=seconds):
                self.setUp()
                attempt_id = self.insert_attempt(self.student, cutoff_extra=extra)
                self.at(seconds)
                self.assertEqual(self.submit(self.right_fields(attempt_id), attempt_id).outcome, outcome)

    def test_a_row_stamped_ahead_of_now_is_refused_with_nothing_graded_or_written(self):
        attempt_id = self.insert_attempt(self.student, started=T0 + timedelta(hours=1))
        fields = self.right_fields(attempt_id)
        result = self.no_write(lambda: self.submit(fields, attempt_id))
        self.assertEqual((result.outcome, result.attempt_id), ("future_start", attempt_id))
        row = self.attempt(attempt_id)
        self.assertEqual((row["abandoned_at"], row["expired_at"], row["submitted_at"]), (None, None, None))
        self.assertEqual(self.answers(), [])
        # the next start marks it abandoned and creates a normal new attempt
        started = self.start()
        self.assertEqual((started.outcome, started.abandoned_attempt_id), ("created", attempt_id))
        self.assertEqual(self.no_write(lambda: self.submit(fields, attempt_id)).outcome, "abandoned")

    def test_a_future_start_is_decided_before_the_plan_is_parsed(self):
        attempt_id = self.insert_attempt(self.student, started=T0 + timedelta(hours=1), ids=[1, 2, 3])
        self.assertEqual(self.no_write(lambda: self.submit({}, attempt_id)).outcome, "future_start")


# =========================================================================== stored snapshot

class MalformedSnapshotTest(SubmitCase):
    def good_slot_format(self):
        return self.blueprint().slot_format_json()

    def malformed_rows(self):
        plan, _blueprint = self.good_plan()
        slots = json.loads(self.good_slot_format())
        return {
            "plan not json": dict(ids="not json"),
            "plan not an array": dict(ids={"a": 1}),
            "24 ids": dict(ids=plan[:24]),
            "duplicate id": dict(ids=plan[:24] + [plan[0]]),
            "non-integer id": dict(ids=plan[:24] + ["x"]),
            "zero id": dict(ids=plan[:24] + [0]),
            "26 slot formats": dict(slot_format=json.dumps(slots + [slots[0]])),
            "slot format with an extra key": dict(slot_format=json.dumps([dict(slots[0], extra=1)] + slots[1:])),
            "slot format with an unsupported kind": dict(slot_format=json.dumps([{"kind": "practical_web", "points": 1}] + slots[1:])),
            "slot format with zero points": dict(slot_format=json.dumps([{"kind": "multiple_choice", "points": 0}] + slots[1:])),
            "slot format not json": dict(slot_format="nope"),
            "saved maximum is not the slot points": dict(planned=44),
        }

    def test_a_malformed_plan_or_slot_format_fails_closed_with_nothing_written(self):
        for label, kwargs in self.malformed_rows().items():
            with self.subTest(label):
                self.setUp()
                ids = kwargs.pop("ids", None)
                slot_format = kwargs.pop("slot_format", None)
                attempt_id = self.insert_attempt(self.student, ids=ids, slot_format=slot_format, **kwargs)
                before = self.state()
                with self.assertRaises(part1.Part1StateError):
                    self.submit({f"q_{self.good_plan()[0][0]}": CYR_A}, attempt_id)
                self.assertEqual(self.state(), before)
                self.assertFalse(self.conn.in_transaction)
                self.assertEqual(self.attempt(attempt_id)["submitted_at"], None)

    def test_an_unreadable_seed_duration_or_instants_fail_closed(self):
        cases = {"empty seed": dict(seed=""), "instants not following the duration": dict(duration=5000)}
        for label, kwargs in cases.items():
            with self.subTest(label):
                self.setUp()
                attempt_id = self.insert_attempt(self.student, **kwargs)
                if "duration" in kwargs:
                    self.sql("UPDATE dzi_part1_attempts SET deadline_at = ?, submit_cutoff_at = ? WHERE id = ?",
                             (ts(T0 + timedelta(seconds=5400)), ts(T0 + timedelta(seconds=5460)), attempt_id))
                before = self.state()
                with self.assertRaises(part1.Part1StateError):
                    self.submit({}, attempt_id)
                self.assertEqual(self.state(), before)

    def test_a_lapsed_row_is_expired_from_its_time_alone_even_with_a_malformed_plan(self):
        attempt_id = self.insert_attempt(self.student, ids="not json")
        self.at(5460)
        self.assertEqual(self.submit({}, attempt_id).outcome, "expired")
        self.assertIsNotNone(self.attempt(attempt_id)["expired_at"])

    def test_ids_swapped_across_kinds_are_unpresentable_not_an_error(self):
        plan, _blueprint = self.good_plan()
        plan[0], plan[15] = plan[15], plan[0]  # a short answer in slot 1, a multiple choice in slot 16
        attempt_id = self.insert_attempt(self.student, ids=plan)
        fields = {f"q_{plan[1]}": self.right_letter(plan[1]), f"q_{plan[15]}": self.right_letter(plan[15])}
        result = self.submit(fields, attempt_id)
        self.assertEqual(result.outcome, "submitted")
        slots = {r["slot_number"] for r in self.answers(attempt_id)}
        self.assertNotIn(1, slots)
        self.assertNotIn(16, slots)
        self.assertEqual(self.attempt(attempt_id)["score_max"], 45 - 1 - 3)

    def test_a_plan_id_with_no_question_row_is_unpresentable_not_an_error(self):
        plan, _blueprint = self.good_plan()
        plan[0] = 99999999  # the plan column holds no foreign key: the row is simply gone
        attempt_id = self.insert_attempt(self.student, ids=plan)
        fields = {f"q_{plan[1]}": self.right_letter(plan[1]), "q_99999999": CYR_A}
        result = self.submit(fields, attempt_id)
        self.assertEqual((result.outcome, result.score_points, result.score_max), ("submitted", 1, 44))
        self.assertNotIn(1, {r["slot_number"] for r in self.answers(attempt_id)})


# =========================================================================== content and blueprint drift

class DriftTest(SubmitCase):
    def setUp(self):
        super().setUp()
        self.attempt_id = self.begin().attempt_id
        self.mc = self.mc_ids(self.attempt_id)
        self.sa = self.short_answer_ids(self.attempt_id)

    def submit_all(self):
        return self.submit(self.right_fields(self.attempt_id), self.attempt_id)

    def test_the_current_correct_key_is_what_is_graded(self):
        question_id = self.mc[0]
        right, wrong = self.right_letter(question_id), self.wrong_letter(question_id)
        self.sql("UPDATE multiple_choice_options SET is_correct = CASE option_letter WHEN ? THEN 1 ELSE 0 END WHERE question_id = ?",
                 (wrong, question_id))
        result = self.submit({f"q_{question_id}": right, f"q_{self.mc[1]}": self.right_letter(self.mc[1])}, self.attempt_id)
        self.assertEqual((result.outcome, result.score_points), ("submitted", 1))  # the old key no longer scores

    def test_a_changed_accepted_answer_is_what_is_matched(self):
        question_id = self.sa[0]
        self.sql("UPDATE fill_in_subquestions SET correct_answer = ?, answer_alternatives = NULL WHERE question_id = ? "
                 "AND subquestion_number = 1", (json.dumps(["ново-значение"]), question_id))
        self.submit({f"a_{question_id}_1": "ново-значение"}, self.attempt_id)
        row = next(r for r in self.answers(self.attempt_id) if r["question_id"] == question_id and r["subquestion_number"] == 1)
        self.assertEqual(row["is_correct"], 1)

    def test_an_item_that_is_no_longer_renderable_is_neither_graded_nor_counted(self):
        question_id = self.mc[2]
        fields = self.right_fields(self.attempt_id)
        self.sql("UPDATE multiple_choice_options SET is_correct = 0 WHERE question_id = ?", (question_id,))
        result = self.submit(fields, self.attempt_id)
        self.assertEqual((result.outcome, result.score_points, result.score_max), ("submitted", 44, 44))
        self.assertNotIn(question_id, [r["question_id"] for r in self.answers(self.attempt_id)])

    def test_a_short_answer_no_longer_ready_is_dropped_with_its_points(self):
        question_id = self.sa[0]
        self.sql("UPDATE questions SET source_number = 27 WHERE id = ?", (question_id,))  # renumbered into part 2
        right = self.right_fields(self.attempt_id)
        result = self.submit(right, self.attempt_id)
        self.assertEqual((result.score_points, result.score_max), (42, 42))
        self.assertNotIn(question_id, [r["question_id"] for r in self.answers(self.attempt_id)])

    def test_part_weights_that_no_longer_sum_to_the_stored_slot_points_drop_the_item(self):
        question_id = next(q for q in self.sa if len(self.parts(q)) == 3)
        self.sql("UPDATE fill_in_subquestions SET points = 2 WHERE question_id = ? AND subquestion_number = 1", (question_id,))
        result = self.submit(self.right_fields(self.attempt_id), self.attempt_id)
        self.assertEqual((result.score_points, result.score_max), (42, 42))
        self.assertNotIn(question_id, [r["question_id"] for r in self.answers(self.attempt_id)])

    def test_a_removed_part_is_no_longer_a_field_and_a_changed_type_is_unpresentable(self):
        question_id = self.mc[3]
        self.sql("UPDATE questions SET question_type = 'fill_in' WHERE id = ?", (question_id,))
        result = self.submit(self.right_fields(self.attempt_id), self.attempt_id)
        self.assertEqual((result.score_points, result.score_max), (44, 44))

    def test_every_item_unpresentable_is_stale_and_writes_nothing(self):
        self.sql("UPDATE multiple_choice_options SET is_correct = 0")
        self.sql("UPDATE fill_in_subquestions SET correct_answer = '[]', answer_alternatives = NULL")  # nothing to match
        result = self.no_write(lambda: self.submit({f"q_{self.mc[0]}": CYR_A}, self.attempt_id))
        self.assertEqual((result.outcome, result.attempt_id), ("stale", self.attempt_id))

    def test_a_later_stimulus_exclusion_does_not_drop_an_item_the_student_saw(self):
        with patch.object(part1, "MISSING_STIMULUS_QUESTION_IDS", frozenset(self.plan_of(self.attempt_id))):
            result = self.submit(self.right_fields(self.attempt_id), self.attempt_id)
        self.assertEqual((result.outcome, result.score_points, result.score_max), ("submitted", 45, 45))

    def test_a_reseeded_blueprint_changes_neither_the_grading_nor_the_maximum(self):
        self.sql("UPDATE dzi_blueprint_slots SET points = 9, task_kind = 'practical_web' WHERE exam_part = 1")
        self.sql("UPDATE dzi_blueprints SET is_active = 0")
        result = self.submit(self.right_fields(self.attempt_id), self.attempt_id)
        self.assertEqual((result.outcome, result.score_points, result.score_max), ("submitted", 45, 45))
        self.assert_follows_plan(self.attempt_id)

    def test_stored_slot_points_are_what_a_multiple_choice_row_is_worth(self):
        slots = json.loads(self.blueprint().slot_format_json())
        slots[0]["points"] = 2
        attempt_id = self.insert_attempt(self.other, slot_format=json.dumps(slots), planned=46)
        result = self.submit(self.right_fields(attempt_id), attempt_id, self.other)
        self.assertEqual((result.score_points, result.score_max), (46, 46))
        self.assertEqual(self.answers(attempt_id)[0]["points_possible"], 2)

    def test_a_stored_short_answer_slot_whose_points_no_longer_match_the_weights_is_dropped(self):
        slots = json.loads(self.blueprint().slot_format_json())
        slots[15]["points"] = 4
        attempt_id = self.insert_attempt(self.other, slot_format=json.dumps(slots), planned=46)
        result = self.submit(self.right_fields(attempt_id), attempt_id, self.other)
        self.assertEqual((result.score_points, result.score_max), (42, 42))

    def test_nothing_changed_later_alters_a_persisted_result(self):
        result = self.submit(self.right_fields(self.attempt_id), self.attempt_id)
        stored = (self.attempt(self.attempt_id), self.answers())
        self.sql("UPDATE multiple_choice_options SET is_correct = 0")
        self.sql("UPDATE dzi_blueprint_slots SET points = 9")
        self.sql("UPDATE fill_in_subquestions SET correct_answer = '[\"zzz\"]', points = 5")
        self.sql("UPDATE dzi_blueprints SET part1_minutes = 1, is_active = 0")
        retry = self.submit({}, self.attempt_id)
        self.assertEqual((retry.outcome, retry.score_points, retry.score_max), ("already_submitted", result.score_points, result.score_max))
        self.assertEqual((self.attempt(self.attempt_id), self.answers()), stored)


# =========================================================================== clock

class ClockTest(SubmitCase):
    def setUp(self):
        super().setUp()
        self.attempt_id = self.begin().attempt_id
        self.fields = self.right_fields(self.attempt_id)

    def test_a_step_is_refused_with_no_answer_row_and_the_attempt_stays_open(self):
        self.clock.step_wall(3600)
        self.assertEqual(self.refused(self.fields, self.attempt_id), "clock_step")
        self.assertEqual(self.refused(self.fields, self.attempt_id), "clock_suspect")
        self.assertEqual(self.refused({}, 999999), "clock_suspect")  # even a read-only outcome checks the clock
        self.assertEqual((self.answers(), self.attempt(self.attempt_id)["submitted_at"]), ([], None))
        self.assertEqual(self.clock_row()["state"], "suspect")
        self.assertFalse(self.conn.in_transaction)

    def test_a_regression_after_a_write_is_refused_because_the_write_admitted(self):
        self.at(100)
        self.assertEqual(self.submit(self.fields, self.attempt_id).outcome, "submitted")
        self.assertEqual(self.clock_row()["high_water_at"], ts(T0 + timedelta(seconds=100)))
        self.clock.step_wall(-3600)
        self.assertEqual(self.refused({}, self.attempt_id), "clock_regression")  # even an already_submitted retry
        self.assertEqual(self.clock_row()["state"], "suspect")

    def test_a_read_only_outcome_does_not_raise_the_high_water_mark(self):
        self.at(100)
        for call in (lambda: self.submit({}, self.attempt_id), lambda: self.submit({}, 999999),
                     lambda: self.submit({f"a_{self.short_answer_ids(self.attempt_id)[0]}_1": "x" * 201}, self.attempt_id)):
            call()
            self.assertEqual(self.clock_row()["high_water_at"], ts(T0))
        self.submit(self.fields, self.attempt_id)
        self.assertEqual(self.clock_row()["high_water_at"], ts(T0 + timedelta(seconds=100)))

    def test_an_unsynchronised_first_write_of_a_boot_and_an_unusable_clock_row_write_no_part1_row(self):
        reset_lifecycle_clock_row(self.db_path)
        TEST_CLOCK.sync = False
        self.assertEqual(self.refused(self.fields, self.attempt_id), "no_sync_evidence")
        TEST_CLOCK.sync = True
        self.sql("DELETE FROM lifecycle_clock")
        self.assertEqual(self.refused(self.fields, self.attempt_id), "clock_row_invalid")
        self.assertEqual((self.answers(), self.attempt(self.attempt_id)["submitted_at"]), ([], None))

    def test_a_suspect_clock_refuses_until_recovery_and_the_deadline_does_not_pause(self):
        self.clock.step_wall(3600)
        self.refused(self.fields, self.attempt_id)
        self.clock.step_wall(-3600)
        self.assertEqual(self.refused(self.fields, self.attempt_id), "clock_suspect")
        self.at(6000)
        self.assertEqual(self.refused(self.fields, self.attempt_id), "clock_suspect")
        self.assertEqual(self.answers(), [])

    def test_check_factual_is_never_called_on_any_path(self):
        def boom(*_args, **_kwargs):
            raise AssertionError("check_factual must not be called on a Part 1 row")

        with patch.object(lifecycle.LifecycleWrite, "check_factual", boom):
            self.assertEqual(self.submit({}, self.attempt_id).outcome, "empty")
            self.assertEqual(self.submit(self.fields, self.attempt_id).outcome, "submitted")
            self.assertEqual(self.submit({}, self.attempt_id).outcome, "already_submitted")
            lapsed = self.begin(self.other).attempt_id
            self.at(9000)
            self.assertEqual(self.submit({}, lapsed, self.other).outcome, "expired")
            future = self.insert_attempt(self.add_account("student"), started=T0 + timedelta(days=1))
            self.assertEqual(self.submit({}, future, account_principal(self.attempt(future)["user_id"], "student", "x")).outcome,
                             "future_start")
        self.assertEqual(self.clock_row()["state"], "ok")  # a row stamped ahead never made the shared clock suspect


# =========================================================================== transactions

class AtomicityTest(SubmitCase):
    @contextmanager
    def watch_admit(self):
        record = {"calls": 0, "changes": [], "answer_rows": [], "terminal": []}
        original = lifecycle.LifecycleWrite.admit

        def spy(transaction):
            record["calls"] += 1
            record["changes"].append(transaction.conn.total_changes)
            record["answer_rows"].append(transaction.conn.execute("SELECT COUNT(*) FROM dzi_part1_answers").fetchone()[0])
            record["terminal"].append(transaction.conn.execute(
                "SELECT COUNT(*) FROM dzi_part1_attempts WHERE submitted_at IS NOT NULL OR expired_at IS NOT NULL").fetchone()[0])
            return original(transaction)

        with patch.object(lifecycle.LifecycleWrite, "admit", spy):
            yield record

    def setUp(self):
        super().setUp()
        self.attempt_id = self.begin().attempt_id
        self.fields = self.right_fields(self.attempt_id)

    def test_a_submission_admits_exactly_once_before_its_first_write(self):
        before = self.conn.total_changes
        with self.watch_admit() as record:
            self.assertEqual(self.submit(self.fields, self.attempt_id).outcome, "submitted")
        self.assertEqual((record["calls"], record["changes"], record["answer_rows"], record["terminal"]), (1, [before], [0], [0]))

    def test_an_expiry_admits_once_before_its_marking(self):
        self.at(5460)
        before = self.conn.total_changes
        with self.watch_admit() as record:
            self.assertEqual(self.submit(self.fields, self.attempt_id).outcome, "expired")
        self.assertEqual((record["calls"], record["changes"], record["terminal"]), (1, [before], [0]))

    def test_no_write_outcomes_never_admit(self):
        sa = self.short_answer_ids(self.attempt_id)[0]
        future = self.insert_attempt(self.other, started=T0 + timedelta(hours=1))
        with self.watch_admit() as record:
            self.submit({}, self.attempt_id)
            self.submit({f"a_{sa}_1": "x" * 201}, self.attempt_id)
            self.submit(self.fields, 999999)
            self.submit(self.fields, self.attempt_id, self.add_account("student", active=False))
            self.submit(self.fields, future, self.other)
            self.sql("UPDATE multiple_choice_options SET is_correct = 0")
            self.sql("UPDATE fill_in_subquestions SET correct_answer = '[]', answer_alternatives = NULL")
            self.submit(self.fields, self.attempt_id)  # stale
            self.sql("UPDATE dzi_part1_attempts SET abandoned_at = ? WHERE id = ?", (ts(T0), self.attempt_id))
            self.submit(self.fields, self.attempt_id)  # abandoned
        self.assertEqual(record["calls"], 0)

    def test_a_decision_that_reports_no_write_is_discarded_even_if_a_statement_ran(self):
        """The wrapper, not the decision, guarantees a read-only exit leaves no trace: the submit equivalent of the start
        service's regression. The decision admits (and, in the second case, writes a row) yet reports no write."""
        self.at(10)  # so that a stray admission would really move the high-water mark
        before = self.state()
        for label, stray in (("admission only", lambda conn, transaction: transaction.admit()),
                             ("admission and a row", lambda conn, transaction: (
                                 transaction.admit(),
                                 conn.execute("UPDATE dzi_part1_attempts SET abandoned_at = ? WHERE id = ?",
                                              (transaction.now_text, self.attempt_id))))):
            with self.subTest(label):
                def decide(conn, transaction, user_id, attempt_id, raw_fields, _stray=stray):
                    _stray(conn, transaction)  # a stray write that the decision does not report
                    return part1.Part1Submission(part1.OUTCOME_EMPTY, attempt_id), False

                with patch.object(part1, "_decide_submission", decide):
                    self.assertEqual(self.submit({}, self.attempt_id).outcome, part1.OUTCOME_EMPTY)
                # judged from a separate connection: the outer writer rolled back, the row and the clock row included
                self.assertEqual(self.state(), before)
                self.assertEqual(self.clock_row()["high_water_at"], ts(T0))
                self.assertEqual(self.attempt(self.attempt_id)["abandoned_at"], None)
                self.assertFalse(self.conn.in_transaction)
                # and no transaction leaked: another connection takes the writer lock at once
                other = self.connect(timeout=0.2)
                other.execute("BEGIN IMMEDIATE")
                other.rollback()

    def test_a_failing_answer_insert_rolls_back_the_rows_the_state_and_the_admission(self):
        self.at(30)
        self.sql("CREATE TRIGGER fail_slot_20 BEFORE INSERT ON dzi_part1_answers WHEN NEW.slot_number = 20 "
                 "BEGIN SELECT RAISE(ABORT, 'boom'); END")
        before = self.state()
        with self.assertRaises(sqlite3.Error):
            self.submit(self.fields, self.attempt_id)
        self.assertEqual(self.state(), before)  # no answer row, no terminal state, the clock row included
        self.assertEqual(self.clock_row()["high_water_at"], ts(T0))
        self.assertFalse(self.conn.in_transaction)
        self.sql("DROP TRIGGER fail_slot_20")
        self.assertEqual(self.submit(self.fields, self.attempt_id).outcome, "submitted")

    def test_a_final_update_that_changes_no_row_rolls_everything_back(self):
        self.at(30)
        self.sql("CREATE TRIGGER skip_submit BEFORE UPDATE ON dzi_part1_attempts WHEN NEW.submitted_at IS NOT NULL "
                 "BEGIN SELECT RAISE(IGNORE); END")
        before = self.state()
        with self.assertRaises(part1.Part1StateError):
            self.submit(self.fields, self.attempt_id)
        self.assertEqual(self.state(), before)
        self.assertEqual(self.clock_row()["high_water_at"], ts(T0))
        self.assertFalse(self.conn.in_transaction)

    def test_a_marking_that_changes_no_row_rolls_back_the_admission(self):
        self.at(5460)
        self.sql("CREATE TRIGGER skip_expiry BEFORE UPDATE ON dzi_part1_attempts WHEN NEW.expired_at IS NOT NULL "
                 "BEGIN SELECT RAISE(IGNORE); END")
        before = self.state()
        with self.assertRaises(part1.Part1StateError):
            self.submit(self.fields, self.attempt_id)
        self.assertEqual(self.state(), before)
        self.assertEqual(self.clock_row()["high_water_at"], ts(T0))

    def test_an_open_attempt_that_already_has_answers_is_refused(self):
        plan = self.plan_of(self.attempt_id)
        self.sql("INSERT INTO dzi_part1_answers (attempt_id, slot_number, subquestion_number, question_id, kind, is_correct, "
                 "points_possible, points_awarded, grader_version) VALUES (?, 1, 0, ?, 'multiple_choice', 0, 1, 0, 'v')",
                 (self.attempt_id, plan[0]))
        before = self.state()
        with self.assertRaises(part1.Part1StateError):
            self.submit(self.fields, self.attempt_id)
        self.assertEqual(self.state(), before)

    def test_the_final_update_is_guarded_on_id_owner_and_open_state(self):
        captured = []
        real = part1._store_submission

        def spy(conn, attempt_id, user_id, now_text, answers):
            captured.append((attempt_id, user_id))
            return real(conn, attempt_id, user_id, now_text, answers)

        with patch.object(part1, "_store_submission", spy):
            self.submit(self.fields, self.attempt_id)
        self.assertEqual(captured, [(self.attempt_id, self.student.user_id)])
        with closing(self.raw()) as conn:  # the statement itself restates id, owner and open state
            cursor = conn.execute("UPDATE dzi_part1_attempts SET submitted_at = ?, score_points = 1, score_max = 2 WHERE id = ? "
                                  "AND user_id = ? AND submitted_at IS NULL AND expired_at IS NULL AND abandoned_at IS NULL",
                                  (ts(T0), self.attempt_id, self.student.user_id))
            self.assertEqual(cursor.rowcount, 0)


# =========================================================================== races

class RaceTest(SubmitCase):
    @contextmanager
    def hold_first(self, target):
        inside, release = threading.Event(), threading.Event()
        real = getattr(part1, target)

        def gated(*args, **kwargs):
            if threading.current_thread().name == "first":
                inside.set()
                if not release.wait(30):
                    raise AssertionError("the gate was never released")
            return real(*args, **kwargs)

        with patch.object(part1, target, gated):
            yield inside, release

    def run_pair(self, gate, first_call, second_call):
        """``first_call`` holds the writer lock inside ``gate``; ``second_call`` starts meanwhile and must wait."""
        results, errors = {}, {}

        def runner(key, call):
            conn = web_db.connect_db(self.db_path, timeout=30)
            try:
                results[key] = call(conn)
            except BaseException as exc:  # noqa: BLE001 - reported below
                errors[key] = exc
            finally:
                conn.close()

        with self.hold_first(gate) as (inside, release):
            first = threading.Thread(target=runner, args=("first", first_call), name="first")
            second = threading.Thread(target=runner, args=("second", second_call), name="second")
            first.start()
            self.assertTrue(inside.wait(30))
            second.start()
            second.join(0.4)
            self.assertTrue(second.is_alive(), "the second writer must be blocked behind the writer lock")
            release.set()
            first.join(30)
            second.join(30)
        self.assertEqual(errors, {})
        return results["first"], results["second"]

    def submit_call(self, attempt_id, fields, who=None):
        return lambda conn: part1.submit_part1(conn, who or self.student, attempt_id, fields)

    def start_call(self, who=None):
        return lambda conn: part1.start_or_resume_part1(conn, who or self.student)

    def one_open_attempt(self):
        rows = self.rows("SELECT COUNT(*) AS n FROM dzi_part1_attempts WHERE user_id = ? AND submitted_at IS NULL "
                         "AND expired_at IS NULL AND abandoned_at IS NULL", (self.student.user_id,))
        self.assertLessEqual(rows[0]["n"], 1)

    def test_a_submit_that_wins_the_lock_is_followed_by_a_start_that_creates_a_new_attempt(self):
        attempt = self.begin()
        submitted, started = self.run_pair("_presentable_slots", self.submit_call(attempt.attempt_id, self.right_fields(attempt.attempt_id)),
                                           self.start_call())
        self.assertEqual((submitted.outcome, submitted.score_points), ("submitted", 45))
        self.assertEqual(started.outcome, "created")
        self.assertNotEqual(started.attempt_id, attempt.attempt_id)
        self.assertIsNotNone(self.attempt(attempt.attempt_id)["submitted_at"])
        self.one_open_attempt()
        self.assert_follows_plan(attempt.attempt_id)

    def test_a_start_that_wins_the_lock_resumes_and_the_submit_never_loses_to_it(self):
        attempt = self.begin()
        started, submitted = self.run_pair("_presentable_slots", self.start_call(),
                                           self.submit_call(attempt.attempt_id, self.right_fields(attempt.attempt_id)))
        self.assertEqual((started.outcome, started.attempt_id), ("resumed", attempt.attempt_id))
        self.assertEqual((submitted.outcome, submitted.score_points), ("submitted", 45))
        self.assertEqual(len(self.attempts()), 1)
        self.assertEqual(self.attempt(attempt.attempt_id)["abandoned_at"], None)

    def test_a_lapsed_attempt_is_expired_first_whichever_wins_and_no_answer_is_stored(self):
        for first_is_submit in (True, False):
            with self.subTest(first_is_submit=first_is_submit):
                self.setUp()
                attempt = self.begin()
                fields = self.right_fields(attempt.attempt_id)
                self.at(5460)
                submit, start = self.submit_call(attempt.attempt_id, fields), self.start_call()
                results = self.run_pair("_mark", submit, start) if first_is_submit else self.run_pair("_mark", start, submit)
                submitted, started = results if first_is_submit else results[::-1]
                self.assertEqual(submitted.outcome, "expired")
                self.assertEqual(started.outcome, "created")
                self.assertNotEqual(started.attempt_id, attempt.attempt_id)
                self.assertEqual(self.answers(), [])
                self.assertEqual(self.attempt(attempt.attempt_id)["expired_at"], ts(T0 + timedelta(seconds=5460)))
                self.one_open_attempt()

    def test_two_submits_of_one_attempt_store_one_answer_set(self):
        attempt = self.begin()
        fields = self.right_fields(attempt.attempt_id)
        first, second = self.run_pair("_presentable_slots", self.submit_call(attempt.attempt_id, fields),
                                      self.submit_call(attempt.attempt_id, fields))
        self.assertEqual((first.outcome, second.outcome), ("submitted", "already_submitted"))
        self.assertEqual(len(self.answers()), len(first.answers))
        self.assert_follows_plan(attempt.attempt_id)

    def test_a_content_change_committed_while_a_submit_waits_is_judged_on_the_committed_state(self):
        attempt = self.begin()
        fields = self.right_fields(attempt.attempt_id)
        mc = self.mc_ids(attempt.attempt_id)[0]
        holder = self.raw()
        holder.execute("BEGIN IMMEDIATE")
        results, errors = {}, {}

        def run():
            conn = web_db.connect_db(self.db_path, timeout=30)
            try:
                results["submit"] = part1.submit_part1(conn, self.student, attempt.attempt_id, fields)
            except BaseException as exc:  # noqa: BLE001
                errors["submit"] = exc
            finally:
                conn.close()

        thread = threading.Thread(target=run)
        thread.start()
        thread.join(0.4)
        self.assertTrue(thread.is_alive())
        holder.execute("UPDATE multiple_choice_options SET is_correct = 0 WHERE question_id = ?", (mc,))
        holder.commit()
        thread.join(30)
        self.assertEqual(errors, {})
        self.assertEqual((results["submit"].outcome, results["submit"].score_max), ("submitted", 44))
        self.assertNotIn(mc, [r["question_id"] for r in self.answers(attempt.attempt_id)])

    def test_a_foreign_writer_blocks_a_submit_and_nothing_is_written(self):
        attempt = self.begin()
        holder = self.raw()
        holder.execute("BEGIN IMMEDIATE")
        try:
            blocked = self.connect(timeout=0.2)
            before = self.state()
            with self.assertRaises(sqlite3.OperationalError):
                part1.submit_part1(blocked, self.student, attempt.attempt_id, self.right_fields(attempt.attempt_id))
            self.assertFalse(blocked.in_transaction)
        finally:
            holder.rollback()
        self.assertEqual(self.state(), before)


# =========================================================================== recovery and limitations

class SubmitRecoveryTest(RecoveryCase):
    """The two reviewed fault-and-recovery sequences through the real operator CLI, with a submit.

    Judged by the persisted rows and the clock row only. The CLI is ``manage_lifecycle.main`` on a disposable
    database at the controlled fixture clock: no host clock, boot identity or database of the machine is read.
    """

    def forward_first_write(self, ahead):
        reset_lifecycle_clock_row(self.db_path)  # no boot anchor yet
        self.clock.freeze(T0 + ahead)

    def fields_for(self, attempt_id):
        fields = {}
        for question_id, kind in zip(self.plan_of(attempt_id), self.slot_kinds(attempt_id)):
            if kind == "multiple_choice":
                right = self.conn.execute("SELECT option_letter FROM multiple_choice_options WHERE question_id = ? AND is_correct = 1",
                                          (question_id,)).fetchone()[0]
                fields[f"q_{question_id}"] = right
        return fields

    def submit(self, attempt_id, who=None):
        return part1.submit_part1(self.conn, who or self.student, attempt_id, self.fields_for(attempt_id))

    def answer_rows(self):
        with closing(self.raw()) as conn:
            return conn.execute("SELECT COUNT(*) FROM dzi_part1_answers").fetchone()[0]

    def assert_refused_abandoned(self, attempt_id, frozen_row):
        before_state = self.state()
        result = self.submit(attempt_id)
        self.assertEqual(result.outcome, "abandoned")
        self.assertEqual(self.answer_rows(), 0)
        self.assertEqual(self.attempt(attempt_id), frozen_row)
        self.assertEqual(self.state(), before_state)

    # ------------------------------------------------------------------ the two reviewed sequences
    def test_plus_6_hours_recovered_before_the_stamp_then_a_submit_after_the_stamp_is_refused(self):
        self.forward_first_write(6 * HOUR)
        first = part1.start_or_resume_part1(self.conn, self.student)
        stamp = T0 + 6 * HOUR
        self.assertEqual(first.started_at, ts(stamp))
        self.clock.advance(600)
        self.clock.step_wall(-6 * 3600)
        # before any recovery: every writing request, a submit included, is refused and stores nothing
        with self.assertRaises(lifecycle.ClockUnavailable) as caught:
            self.submit(first.attempt_id)
        self.assertEqual(caught.exception.reason, "clock_regression")
        with self.assertRaises(lifecycle.ClockUnavailable) as caught:
            self.submit(first.attempt_id)
        self.assertEqual(caught.exception.reason, "clock_suspect")
        self.assertEqual((self.answer_rows(), self.attempt(first.attempt_id)["abandoned_at"]), (0, None))

        verified = T0 + timedelta(seconds=600)
        code, out, err = self.recover(verified)
        self.assertEqual(code, 0, out + err)
        self.assertIn("DZI Part 1 attempts abandoned: 1", out)
        row = self.attempt(first.attempt_id)
        self.assertEqual(row["abandoned_at"], ts(verified))
        self.assertLess(row["abandoned_at"], row["started_at"])
        # real time reaches the false stamp and passes what would have been its deadline and cutoff: still abandoned
        for target in (verified, stamp + timedelta(seconds=1), stamp + timedelta(seconds=5400), stamp + timedelta(seconds=5461)):
            self.clock.set(target)
            self.assert_refused_abandoned(first.attempt_id, row)
        self.assertEqual(self.clock_row()["state"], "ok")
        # the next start creates a new attempt, and only that one can be submitted
        second = part1.start_or_resume_part1(self.conn, self.student)
        self.assertTrue(second.created)
        self.assert_refused_abandoned(first.attempt_id, row)
        self.assertEqual(self.submit(second.attempt_id).outcome, "submitted")
        self.assertEqual(self.attempt(first.attempt_id), row)

    def test_plus_1_hour_recovered_after_the_stamp_then_a_submit_is_refused_with_and_without_an_interval(self):
        for mode in ("none", "covering", "elsewhere"):
            with self.subTest(interval=mode):
                self.setUp()
                self.forward_first_write(HOUR)
                first = part1.start_or_resume_part1(self.conn, self.student)
                self.clock.advance(7200)
                self.clock.step_wall(-3600)
                verified = T0 + 2 * HOUR
                with self.assertRaises(lifecycle.ClockUnavailable) as caught:  # the row was never future-stamped, the write is refused
                    self.submit(first.attempt_id)
                self.assertEqual(caught.exception.reason, "clock_step")
                self.assertEqual((self.answer_rows(), self.attempt(first.attempt_id)["abandoned_at"]), (0, None))
                code, out, err = self.recover(verified, *self.interval(mode, verified))
                self.assertEqual(code, 0, out + err)
                self.assertIn("DZI Part 1 attempts abandoned: 1", out)
                row = self.attempt(first.attempt_id)
                self.assertEqual(row["abandoned_at"], ts(verified))
                self.assertGreater(row["abandoned_at"], row["started_at"])
                self.assert_refused_abandoned(first.attempt_id, row)
                self.clock.advance(5400 + 61)
                self.assert_refused_abandoned(first.attempt_id, row)
                second = part1.start_or_resume_part1(self.conn, self.student)
                self.assertTrue(second.created)
                self.assert_refused_abandoned(first.attempt_id, row)

    # ------------------------------------------------------------------ limitations: characterizations, not guarantees
    def test_limitation_undetected_reboot_leaves_a_late_submit_accepted_with_the_clock_ok(self):
        """The documented case: a forward fault from a boot's first write, corrected with no lifecycle transaction before
        a reboot, then no transaction until the wall clock is within the 5 s tolerance of the mark. No fault is recorded,
        no recovery runs, and a submit is saved 8,400 s after the true start against a 5,400 s limit."""
        self.forward_first_write(HOUR)
        first = part1.start_or_resume_part1(self.conn, self.student)
        stamp = T0 + HOUR
        self.assertEqual((first.started_at, self.clock_row()["high_water_at"]), (ts(stamp), ts(stamp)))
        self.clock.step_wall(-3600)       # corrected, with no lifecycle transaction at all
        self.clock.reboot()
        self.clock.advance(3595)          # five seconds behind the mark
        probe = part1.submit_part1(self.conn, self.student, first.attempt_id, {})
        self.assertEqual(probe.outcome, "empty")  # admitted by the clock check: no fault recorded
        self.assertEqual((self.clock_row()["state"], self.clock_row()["state_reason"]), ("ok", None))
        self.clock.set(T0 + timedelta(seconds=8400))
        result = self.submit(first.attempt_id)
        self.assertEqual((result.outcome, result.score_points), ("submitted", 15))
        self.assertEqual(self.attempt(first.attempt_id)["submitted_at"], ts(T0 + timedelta(seconds=8400)))
        true_elapsed = 8400
        self.assertGreater(true_elapsed, 5400 + part1.SUBMIT_GRACE_SECONDS)
        self.assertEqual((self.clock_row()["state"], self.clock_row()["state_reason"]), ("ok", None))
        self.assertTrue(lifecycle.read_clock(self.conn).healthy)

    def test_control_the_same_correction_without_a_reboot_refuses_a_submit_and_latches(self):
        self.forward_first_write(HOUR)
        first = part1.start_or_resume_part1(self.conn, self.student)
        self.clock.step_wall(-3600)
        with self.assertRaises(lifecycle.ClockUnavailable) as caught:
            self.submit(first.attempt_id)
        self.assertEqual(caught.exception.reason, "clock_regression")
        self.assertEqual(self.clock_row()["state"], "suspect")
        self.clock.advance(3600)  # the wall clock reaches the mark: still latched
        with self.assertRaises(lifecycle.ClockUnavailable) as caught:
            self.submit(first.attempt_id)
        self.assertEqual(caught.exception.reason, "clock_suspect")
        self.assertEqual((self.answer_rows(), self.attempt(first.attempt_id)["abandoned_at"]), (0, None))

    def test_control_a_same_boot_submit_once_the_wall_clock_passes_the_mark_is_a_step_fault(self):
        self.forward_first_write(HOUR)
        first = part1.start_or_resume_part1(self.conn, self.student)
        self.clock.step_wall(-3600)
        self.clock.advance(3600)
        with self.assertRaises(lifecycle.ClockUnavailable) as caught:
            self.submit(first.attempt_id)
        self.assertEqual(caught.exception.reason, "clock_step")
        self.assertEqual(self.answer_rows(), 0)

    def test_control_a_reboot_followed_by_a_submit_before_the_mark_is_a_regression(self):
        self.forward_first_write(HOUR)
        first = part1.start_or_resume_part1(self.conn, self.student)
        self.clock.step_wall(-3600)
        self.clock.reboot()
        self.clock.advance(60)
        with self.assertRaises(lifecycle.ClockUnavailable) as caught:
            self.submit(first.attempt_id)
        self.assertEqual(caught.exception.reason, "clock_regression")
        self.assertEqual((self.answer_rows(), self.clock_row()["state"]), (0, "suspect"))

    def test_limitation_two_same_boot_forward_steps_admit_a_submit_up_to_120_seconds_late(self):
        """Context, a separate limitation: the anchor is 60 s ahead and a later start 120 s ahead, both within the step
        tolerance of one boot; after the correction a request five seconds behind the mark is admitted. The second
        attempt is stamped 120 s ahead of true time, so it is accepted until 5,579 s of true elapsed time against a
        5,400 s limit plus 60 s grace."""
        reset_lifecycle_clock_row(self.db_path)
        self.clock.freeze(T0 + timedelta(seconds=60))
        first_attempt = part1.start_or_resume_part1(self.conn, self.accounts[0])
        self.assertTrue(first_attempt.created)
        self.clock.advance(1000)          # true time T0 + 1000 s
        self.clock.step_wall(60)          # a second forward step: 120 s ahead of true time
        second = part1.start_or_resume_part1(self.conn, self.accounts[1])
        self.assertEqual(second.started_at, ts(T0 + timedelta(seconds=1120)))
        true_start = T0 + timedelta(seconds=1000)
        self.clock.step_wall(-120)        # corrected
        mark = lifecycle.parse_timestamp(self.clock_row()["high_water_at"])
        self.clock.set(mark - timedelta(seconds=5))
        self.assertEqual(part1.submit_part1(self.conn, self.accounts[1], second.attempt_id, {}).outcome, "empty")  # admitted
        self.assertEqual((self.clock_row()["state"], self.clock_row()["state_reason"]), ("ok", None))
        self.clock.set(true_start + timedelta(seconds=5579))  # 119 s beyond the 5,460 s the attempt really had
        result = self.submit(second.attempt_id, self.accounts[1])
        self.assertEqual(result.outcome, "submitted")
        self.assertGreater(5579, 5400 + part1.SUBMIT_GRACE_SECONDS)
        # the false stamp bought exactly 120 s: the stored cutoff is 5,580 s after the true start
        cutoff = lifecycle.parse_timestamp(self.attempt(second.attempt_id)["submit_cutoff_at"])
        self.assertEqual((cutoff - true_start).total_seconds(), 5460 + 120)

    def test_limitation_two_step_case_beyond_the_tolerance_is_refused(self):
        """The control of the case above: a second step of 121 s is a step fault and latches the clock."""
        reset_lifecycle_clock_row(self.db_path)
        self.clock.freeze(T0 + timedelta(seconds=60))
        part1.start_or_resume_part1(self.conn, self.accounts[0])
        self.clock.advance(1000)
        self.clock.step_wall(61)
        with self.assertRaises(lifecycle.ClockUnavailable) as caught:
            part1.start_or_resume_part1(self.conn, self.accounts[1])
        self.assertEqual(caught.exception.reason, "clock_step")
        self.assertEqual(self.clock_row()["state"], "suspect")


if __name__ == "__main__":
    unittest.main()
