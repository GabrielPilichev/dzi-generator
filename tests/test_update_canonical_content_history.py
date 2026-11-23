"""B4 canonical content updater: history rules, references, file references and redaction.

Standard library only (the render-time task query of case 41a is a fixture-local copy, pinned to
web/services/practical.py by a text test, because that module imports Flask).
"""

import builtins
import io
import json
import os
import pathlib
import re
import shutil
import sqlite3
import unicodedata
import unittest
from contextlib import closing
from unittest import mock

from src import canonical_parity as parity
from src import canonical_tables as ct
from src import practical_resource_resolution as resolver
from src import update_canonical_content as tool
from tests import update_canonical_fixtures as fx
from web.services import question_visuals

ROOT = fx.ROOT
ATTEMPT_QUESTIONS = [1, 2, 3]


class HistoryCase(fx.UpdaterTestCase):
    def setUp(self):
        super().setUp()
        self.preimage = self.ws.preimage()

    def refuse(self, mutate, *, codes, target=None, mutate_target=None, run_code="plan_conflicts"):
        target = target or self.ws.target(mutate=mutate_target)
        source = self.ws.source(mutate)
        before = fx.db_snapshot(target)
        result = self.dry(target, source, self.preimage)
        self.assert_refused(result, 1, run_code, row_codes=codes)
        self.assert_untouched(target, before)
        return result

    def applies(self, mutate, *, target=None, runtime=False):
        target = target or self.ws.target(runtime=runtime)
        source = self.ws.source(mutate)
        dry = self.plan_of(target, source, self.preimage)
        applied = self.apply(target, source, self.preimage, dry.report["plan_sha256"])
        self.assertEqual((applied.exit, applied.report["status"]), (0, "applied"), applied.out)
        return dry, target


def new_section(conn, section_id=100):
    fx.insert(conn, "curriculum_sections", id=section_id, curriculum_id=1, module_id=1, section_slug=f"sec-{section_id}",
              title_bg="Нов раздел", **{"class": 11}, section_type="topic_group")


class ChildrenOfExistingRowsTest(HistoryCase):
    def test_35_children_of_existing_questions_and_tasks_are_refused(self):
        cases = {
            "a plain added option letter": lambda conn: fx.insert(conn, "multiple_choice_options", id=100, question_id=1,
                                                                  option_letter="E", option_text="Още един"),
            "a fill-in subquestion": lambda conn: fx.insert(conn, "fill_in_subquestions", id=100, question_id=3,
                                                            subquestion_number=2, correct_answer="x"),
            "a question topic assignment": lambda conn: fx.insert(conn, "question_topic_assignments", id=100,
                                                                  question_id=1, topic_id=2, assignment_type="secondary"),
            "a practical task for an existing task": lambda conn: fx.insert(conn, "practical_tasks", task_id=1,
                                                                            work_environment="spreadsheet"),
            "a practical resource for an existing task": lambda conn: fx.insert(
                conn, "practical_task_resources", id=100, exam_task_id=3, resource_path="data/reference/more.zip::a/b.xlsx"),
        }
        for label, mutate in cases.items():
            with self.subTest(label):
                result = self.refuse(mutate, codes=["history_child_of_existing"])
                self.assertEqual(result.report["conflicts"]["by_code"], {"history_child_of_existing": 1})

    def test_36_a_new_question_with_children_task_resources_and_links_applies(self):
        dry, target = self.applies(lambda conn: fx.add_new_family(conn))
        self.assertEqual(dry.report["conflicts"]["total"], 0)
        self.assertEqual(fx.scalar(target, "SELECT COUNT(*) FROM multiple_choice_options WHERE question_id = 100"), 2)

    def test_37_task_question_links_need_both_ends_new(self):
        def existing_task_new_question(conn):
            fx.add_question(conn, 100, number=50, topic_id=1)
            fx.insert(conn, "exam_task_questions", task_id=2, question_id=100, role="primary")

        def new_task_existing_question(conn):
            fx.insert(conn, "exams", id=100, subject="informatika_it", level="DZI", year=2031, source_file="reference/x/")
            fx.insert(conn, "exam_tasks", id=100, exam_id=100, task_number=1, exam_part=1, task_kind="multiple_choice",
                      points=1)
            fx.insert(conn, "exam_task_questions", task_id=100, question_id=1, role="primary")

        def both_existing_absent(conn):
            fx.insert(conn, "exam_task_questions", task_id=2, question_id=1, role="extra")
        for label, mutate in (("existing task and new question", existing_task_new_question),
                              ("new task and existing question", new_task_existing_question),
                              ("both existing and the link absent", both_existing_absent)):
            with self.subTest(label):
                self.refuse(mutate, codes=["history_child_of_existing"])
        with self.subTest("both new"):
            self.applies(lambda conn: fx.add_new_family(conn))

    def test_38_asset_links_must_own_through_new_rows_only(self):
        def link(conn, owner_type, owner_id, link_id=100, role="extra"):
            fx.insert(conn, "asset_links", id=link_id, asset_id=2, owner_type=owner_type, owner_id=owner_id, role=role,
                      display_order=5)

        def new_task(conn, question_ids):
            fx.insert(conn, "exams", id=100, subject="informatika_it", level="DZI", year=2031, source_file="reference/x/")
            fx.insert(conn, "exam_tasks", id=100, exam_id=100, task_number=1, exam_part=1, task_kind="multiple_choice",
                      points=1)
            for question_id in question_ids:
                fx.insert(conn, "exam_task_questions", task_id=100, question_id=question_id, role="primary")

        def indirect(conn):
            fx.add_question(conn, 100, number=50)
            new_task(conn, [100, 2])
            link(conn, "exam_task", 100)
        negatives = {
            "an existing question": (lambda conn: link(conn, "question", 1), "asset_link_reaches_existing"),
            "an existing task": (lambda conn: link(conn, "exam_task", 1), "asset_link_reaches_existing"),
            "a new task linking an existing question": (lambda conn: (new_task(conn, [1]), link(conn, "exam_task", 100)),
                                                        "asset_link_reaches_existing"),
            "a new task reaching an existing question through a link of this plan": (indirect,
                                                                                       "asset_link_reaches_existing"),
            "an unknown owner type": (lambda conn: link(conn, "weird", 1), "asset_link_unknown_owner_type"),
            "the legacy owner type for a new question": (lambda conn: (fx.add_question(conn, 100, number=50),
                                                                       link(conn, "questions", 100)),
                                                         "asset_link_legacy_owner_type"),
        }
        for label, (mutate, code) in negatives.items():
            with self.subTest(label):
                self.refuse(mutate, codes=[code])
        with self.subTest("owned by a new question"):
            self.applies(lambda conn: (fx.add_question(conn, 100, number=50), link(conn, "question", 100)))
        with self.subTest("owned by a new task linked only to new questions"):
            self.applies(lambda conn: (fx.add_question(conn, 100, number=50), new_task(conn, [100]),
                                       link(conn, "exam_task", 100)))
        with self.subTest("owned by an existing exam: permitted and counted"):
            dry, _target = self.applies(lambda conn: link(conn, "exam", 1))
            self.assertEqual(dry.report["under_existing_parent"], {"asset_links": 1})

    def reachable(self, database, question_ids=ATTEMPT_QUESTIONS):
        """What an existing attempt can reach: visuals, options, sub-questions, links and its stored rows."""
        marks = ",".join("?" for _ in question_ids)
        with closing(fx.REAL_CONNECT(str(database))) as conn:
            conn.row_factory = sqlite3.Row

            def rows(sql, params=()):
                return [tuple(row) for row in conn.execute(sql, params).fetchall()]
            tasks = [row[0] for row in conn.execute(
                f"SELECT task_id FROM exam_task_questions WHERE question_id IN ({marks})", question_ids)]
            task_marks = ",".join("?" for _ in tasks) or "NULL"
            return {
                "visuals": question_visuals.fetch_question_visual_assets(conn, list(question_ids)),
                "options": rows(f"SELECT * FROM multiple_choice_options WHERE question_id IN ({marks}) ORDER BY id",
                                question_ids),
                "fill_in": rows(f"SELECT * FROM fill_in_subquestions WHERE question_id IN ({marks}) ORDER BY id", question_ids),
                "links": rows(f"SELECT * FROM exam_task_questions WHERE question_id IN ({marks}) ORDER BY 1, 2, 3", question_ids),
                "asset_links": rows(
                    f"SELECT * FROM asset_links WHERE (owner_type = 'question' AND owner_id IN ({marks})) "
                    f"OR (owner_type = 'exam_task' AND owner_id IN ({task_marks})) ORDER BY id", [*question_ids, *tasks]),
                "attempts": rows("SELECT * FROM quiz_attempts ORDER BY id"),
                "answers": rows("SELECT * FROM quiz_answers ORDER BY id"),
            }

    def test_39_permitted_additions_leave_what_an_existing_attempt_reaches_unchanged(self):
        target = self.ws.target(runtime=True)
        before = self.reachable(target)
        self.assertTrue(before["visuals"][1] and before["visuals"][2])        # the fixture really has visuals
        self.assertTrue(before["options"] and before["asset_links"] and before["attempts"])
        self.applies(lambda conn: (fx.add_new_family(conn), fx.add_topic(conn, 100), fx.add_question(conn, 101, number=51)),
                     target=target)
        self.assertEqual(self.reachable(target), before)
        # negative controls: a refused row inserted directly (outside the tool) changes what is reachable
        for label, sql, key in (
            ("a new option", "INSERT INTO multiple_choice_options (id, question_id, option_letter, option_text) "
                             "VALUES (900, 1, 'E', 'x')", "options"),
            ("a link to a task that has an image asset", "INSERT INTO exam_task_questions (task_id, question_id, role) "
                                                        "VALUES (2, 3, 'primary')", "visuals"),
        ):
            with self.subTest(label):
                control = self.ws.target(f"control-{key}.db", runtime=True)
                reference = self.reachable(control)
                fx.execute(control, sql)
                self.assertNotEqual(self.reachable(control)[key], reference[key])

    def test_40_topic_section_assignments_need_a_new_topic(self):
        with self.subTest("an existing topic with an existing section"):
            self.refuse(lambda conn: fx.insert(conn, "topic_section_assignments", id=100, topic_id=1, section_id=2),
                        codes=["history_child_of_existing"])
        with self.subTest("an existing topic with a new section"):
            def attach(conn):
                new_section(conn)
                fx.insert(conn, "topic_section_assignments", id=100, topic_id=1, section_id=100)
            self.refuse(attach, codes=["history_child_of_existing"])
        with self.subTest("a new topic with an existing section is counted"):
            dry, _target = self.applies(lambda conn: (fx.add_topic(conn, 100, section_id=1),
                                                      fx.insert(conn, "topic_section_assignments", id=100, topic_id=100,
                                                                section_id=1, is_primary=1)))
            self.assertEqual(dry.report["under_existing_parent"].get("topic_section_assignments"), 1)
        with self.subTest("a new topic with a new section"):
            def both(conn):
                new_section(conn)
                fx.add_topic(conn, 100, section_id=100)
                fx.insert(conn, "topic_section_assignments", id=100, topic_id=100, section_id=100, is_primary=1)
            dry, _target = self.applies(both)
            self.assertNotIn("topic_section_assignments", dry.report["under_existing_parent"])
        with self.subTest("a new question under an existing topic, and started attempts keep their ids"):
            target = self.ws.target(runtime=True)
            attempts = fx.db_snapshot(target)["quiz_attempts"]
            dry, _target = self.applies(lambda conn: fx.add_question(conn, 100, number=50, topic_id=1), target=target)
            self.assertEqual(dry.report["questions_added_to_existing_topics"], 1)
            self.assertEqual(fx.db_snapshot(target)["quiz_attempts"], attempts)

    def test_41_additions_under_existing_parents_are_permitted_and_counted(self):
        def additions(conn):
            fx.add_topic(conn, 100, section_id=1)
            fx.insert(conn, "official_exam_sources", id=100, exam_id=1, source_kind="answers",
                      local_path="data/reference/dzi/official_pdfs/answers.pdf")
            fx.insert(conn, "asset_links", id=100, asset_id=2, owner_type="exam", owner_id=1, role="extra", display_order=3)
            fx.insert(conn, "dzi_blueprint_slots", id=100, blueprint_id=1, slot_number=2, exam_part=1,
                      task_kind="multiple_choice", points=1)
            fx.insert(conn, "curriculum_sections", id=100, curriculum_id=1, module_id=1, section_slug="sec-extra",
                      title_bg="Допълнителен", **{"class": 11}, section_type="topic_group")
        dry, _target = self.applies(additions)
        self.assertEqual(dry.report["under_existing_parent"],
                         {"asset_links": 1, "curriculum_sections": 1, "curriculum_topics": 1,
                          "dzi_blueprint_slots": 1, "official_exam_sources": 1})

    RENDER_TASKS = ("SELECT et.id, et.task_number, et.task_kind FROM exam_tasks et WHERE et.exam_id = ? "
                    "AND et.task_number BETWEEN 26 AND 28 AND et.task_kind LIKE 'practical_%' ORDER BY et.task_number")

    def test_41a_new_exam_tasks_under_an_existing_exam_are_refused(self):
        def task(conn, number, kind, exam_id=1, task_id=100):
            fx.insert(conn, "exam_tasks", id=task_id, exam_id=exam_id, task_number=number, exam_part=2, task_kind=kind,
                      points=20)
        for label, number, kind in (("practical", 27, "practical_graphics"), ("not practical", 3, "multiple_choice")):
            with self.subTest(label):
                self.refuse(lambda conn, n=number, k=kind: task(conn, n, k), codes=["history_child_of_existing"])
        with self.subTest("the same task under a new exam applies"):
            def under_new(conn):
                fx.insert(conn, "exams", id=100, subject="informatika_it", level="DZI", year=2032, source_file="reference/y/")
                task(conn, 27, "practical_graphics", exam_id=100)
            self.applies(under_new)
        with self.subTest("what an existing practical attempt lists at render time"):
            target = self.ws.target(runtime=True)

            def listing(database):
                with closing(fx.REAL_CONNECT(str(database))) as conn:
                    return conn.execute(self.RENDER_TASKS, (1,)).fetchall()
            before = listing(target)
            self.assertEqual([row[1] for row in before], [26])
            self.applies(lambda conn: fx.add_new_family(conn), target=target)
            self.assertEqual(listing(target), before)
            control = self.ws.target("control.db")
            fx.execute(control, "INSERT INTO exam_tasks (id, exam_id, task_number, exam_part, task_kind, points) "
                                "VALUES (900, 1, 27, 2, 'practical_graphics', 20)")
            self.assertNotEqual(listing(control), before)

    def test_41a_the_fixture_query_is_pinned_to_the_application_source(self):
        text = (ROOT / "web" / "services" / "practical.py").read_text(encoding="utf-8")
        for clause in ("WHERE et.exam_id = ?", "AND et.task_number BETWEEN 26 AND 28", "AND et.task_kind LIKE 'practical_%'"):
            self.assertIn(clause, text)
        self.assertIn("et.exam_id = ?", self.RENDER_TASKS)


class PreexistingReferencesTest(HistoryCase):
    def refuse_target(self, mutate, code, column, *, foreign_keys=True, source=None, run_code="plan_conflicts"):
        target = self.ws.target(runtime=True, mutate=mutate, foreign_keys=foreign_keys)
        source = source or self.ws.source(lambda conn: (fx.add_new_family(conn), fx.add_topic(conn, 100)))
        before = fx.db_snapshot(target)
        result = self.dry(target, source, self.preimage)
        self.assert_refused(result, 1, run_code, row_codes=[code])
        if column:
            self.assertIn(code, result.report["unresolved_references"][column])
            self.assertIsInstance(result.report["unresolved_references"][column][code], int)
        self.assert_untouched(target, before)
        return result

    def test_42_unresolved_or_malformed_references_in_the_pre_state_are_refused(self):
        unresolved, malformed, noninteger = ("preexisting_unresolved_reference", "malformed_reference_json",
                                              "noninteger_reference_value")
        attempt = "UPDATE quiz_attempts SET question_ids_json = ? WHERE id = 1"
        practice = "UPDATE practice_sessions SET question_ids_json = ? WHERE id = 1"
        part1 = "UPDATE dzi_part1_attempts SET question_ids_json = ? WHERE id = 1"
        cases = [
            ("questions.topic_id dangling to a would-be-new id", "UPDATE questions SET topic_id = 100 WHERE id = 3",
             unresolved, "questions.topic_id"),
            ("asset_links.owner_id dangling", "INSERT INTO asset_links (id, asset_id, owner_type, owner_id, role) "
                                              "VALUES (50, 1, 'question', 999, 'x')", unresolved, "asset_links.owner_id"),
            ("quiz_answers.question_id", "UPDATE quiz_answers SET question_id = 100 WHERE id = 1", unresolved,
             "quiz_answers.question_id"),
            ("an id in a list", (attempt, "[1, 100]"), unresolved, "quiz_attempts.question_ids_json"),
            ("an id in a dict", (attempt, '{"question_ids": [1, 100]}'), unresolved, "quiz_attempts.question_ids_json"),
            ("a question plan", ("UPDATE quiz_assignments SET question_plan_json = ? WHERE id = 1", "[100]"), unresolved,
             "quiz_assignments.question_plan_json"),
            ("a generated exam", ("UPDATE generated_exams SET question_ids = ? WHERE id = 1", "[100]"), unresolved,
             "generated_exams.question_ids"),
            ("malformed JSON", (attempt, "{bad"), malformed, "quiz_attempts.question_ids_json"),
            ("an unknown owner type", "INSERT INTO asset_links (id, asset_id, owner_type, owner_id, role) "
                                      "VALUES (50, 1, 'weird', 1, 'x')", unresolved, "asset_links.owner_id"),
            ("the legacy owner type already in the target", "INSERT INTO asset_links (id, asset_id, owner_type, owner_id, "
                                                            "role) VALUES (50, 1, 'questions', 1, 'x')", unresolved,
             "asset_links.owner_id"),
            ("a dangling reference matching no new id", (attempt, "[1, 5000]"), unresolved, "quiz_attempts.question_ids_json"),
            ("a numeric string id", (attempt, '["100"]'), noninteger, "quiz_attempts.question_ids_json"),
            ("a float id", (attempt, "[100.0]"), noninteger, "quiz_attempts.question_ids_json"),
            ("a non-text JSON value", ("UPDATE quiz_attempts SET question_ids_json = x'5b315d' WHERE id = 1",), malformed,
             "quiz_attempts.question_ids_json"),
            ("quiz_answers.question_id stored as non-numeric text", "UPDATE quiz_answers SET question_id = 'abc' WHERE id = 1",
             noninteger, "quiz_answers.question_id"),
            ("a dict with an extra numeric member", (attempt, '{"question_ids": [1], "extra": 999}'), unresolved,
             "quiz_attempts.question_ids_json"),
            ("an empty question plan", ("UPDATE quiz_assignments SET question_plan_json = ? WHERE id = 1", ""), malformed,
             "quiz_assignments.question_plan_json"),
            ("a whitespace question plan", ("UPDATE quiz_assignments SET question_plan_json = ? WHERE id = 1", "   "),
             malformed, "quiz_assignments.question_plan_json"),
            ("empty stored attempt ids", (attempt, ""), malformed, "quiz_attempts.question_ids_json"),
            ("empty generated exam ids", ("UPDATE generated_exams SET question_ids = ? WHERE id = 1", ""), malformed,
             "generated_exams.question_ids"),
            ("a top-level integer that does not resolve", (attempt, "5000"), unresolved, "quiz_attempts.question_ids_json"),
            # The two practice columns (C1 review N5/N7). Storage classes other than text and a non-integer
            # practice_answers.question_id cannot be written at all (the migration's CHECKs refuse them).
            ("a practice session plan list", (practice, "[1, 100]"), unresolved, "practice_sessions.question_ids_json"),
            ("a practice session plan dict", (practice, '{"question_ids": [1, 100]}'), unresolved,
             "practice_sessions.question_ids_json"),
            ("a practice plan id matching no new id", (practice, "[1, 5000]"), unresolved,
             "practice_sessions.question_ids_json"),
            ("malformed practice plan JSON", (practice, "{bad"), malformed, "practice_sessions.question_ids_json"),
            ("an empty practice plan", (practice, ""), malformed, "practice_sessions.question_ids_json"),
            ("a numeric string practice id", (practice, '["100"]'), noninteger, "practice_sessions.question_ids_json"),
            ("a float practice id", (practice, "[100.0]"), noninteger, "practice_sessions.question_ids_json"),
            # The two DZI Part 1 columns (migration 027). Only the flat plan column is a reference column; the
            # separate slot_format_json is not (see the subtest below), and non-text plans and non-integer
            # dzi_part1_answers.question_id values cannot be written (the migration's CHECKs refuse them).
            ("a part 1 plan list", (part1, "[1, 100]"), unresolved, "dzi_part1_attempts.question_ids_json"),
            ("a part 1 plan dict", (part1, '{"question_ids": [1, 100]}'), unresolved,
             "dzi_part1_attempts.question_ids_json"),
            ("a part 1 plan id matching no new id", (part1, "[1, 5000]"), unresolved,
             "dzi_part1_attempts.question_ids_json"),
            ("malformed part 1 plan JSON", (part1, "{bad"), malformed, "dzi_part1_attempts.question_ids_json"),
            ("an empty part 1 plan", (part1, ""), malformed, "dzi_part1_attempts.question_ids_json"),
            ("a numeric string part 1 id", (part1, '["100"]'), noninteger, "dzi_part1_attempts.question_ids_json"),
            ("a float part 1 id", (part1, "[100.0]"), noninteger, "dzi_part1_attempts.question_ids_json"),
        ]
        for label, change, code, column in cases:
            with self.subTest(label):
                def mutate(conn, change=change):
                    if isinstance(change, str):
                        conn.execute(change)
                    else:
                        conn.execute(change[0], change[1:])
                result = self.refuse_target(mutate, code, column)
                self.assertNotIn("Traceback", result.out)
        with self.subTest("a practice answer to a question that is not there (foreign keys off on this disposable copy)"):
            result = self.refuse_target(
                lambda conn: conn.execute("UPDATE practice_answers SET question_id = 100 WHERE question_id = 1"),
                unresolved, "practice_answers.question_id", foreign_keys=False)
            self.assertIn("preexisting_fk_violation", result.report["conflicts"]["by_code"])
        with self.subTest("a practice answer to a would-be-new id must not be adopted by the update"):
            # Same corruption, but the update would CREATE question 100: the gate still refuses (the answer was
            # written before the question existed, so it is not the update's to resolve).
            source = self.ws.source(lambda conn: fx.add_question(conn, 100, number=50))
            self.refuse_target(
                lambda conn: conn.execute("UPDATE practice_answers SET question_id = 100 WHERE question_id = 1"),
                unresolved, "practice_answers.question_id", foreign_keys=False, source=source)
        with self.subTest("a part 1 answer to a question that is not there (foreign keys off on this disposable copy)"):
            result = self.refuse_target(
                lambda conn: conn.execute("UPDATE dzi_part1_answers SET question_id = 100 WHERE question_id = 1"),
                unresolved, "dzi_part1_answers.question_id", foreign_keys=False)
            self.assertIn("preexisting_fk_violation", result.report["conflicts"]["by_code"])
        with self.subTest("a part 1 answer to a would-be-new id must not be adopted by the update"):
            source = self.ws.source(lambda conn: fx.add_question(conn, 100, number=50))
            self.refuse_target(
                lambda conn: conn.execute("UPDATE dzi_part1_answers SET question_id = 100 WHERE question_id = 1"),
                unresolved, "dzi_part1_answers.question_id", foreign_keys=False, source=source)
        with self.subTest("a lowered sequence combined with a runtime reference to a would-be-new id"):
            def both(conn):
                fx.raise_sequence(conn, "questions", 3)
                conn.execute(attempt, ("[100]",))
            result = self.refuse_target(both, unresolved, "quiz_attempts.question_ids_json")
            self.assertIn("sequence_below_preimage", result.report["conflicts"]["by_code"])
        with self.subTest("a practice plan reference is refused beside a valid one and never half-applied"):
            target = self.ws.target(runtime=True, mutate=lambda conn: conn.execute(practice, ("[1, 100]",)))
            before = fx.db_snapshot(target)
            result = self.dry(target, self.ws.source(lambda conn: fx.add_new_family(conn)), self.preimage)
            self.assertEqual(result.exit, 1)
            self.assertEqual(fx.db_snapshot(target), before)
        with self.subTest("a part 1 plan reference is refused beside a valid one and never half-applied"):
            target = self.ws.target(runtime=True, mutate=lambda conn: conn.execute(part1, ("[1, 100]",)))
            before = fx.db_snapshot(target)
            result = self.dry(target, self.ws.source(lambda conn: fx.add_new_family(conn)), self.preimage)
            self.assertEqual(result.exit, 1)
            self.assertEqual(fx.db_snapshot(target), before)
        with self.subTest("slot-format integers are points, not question ids, and never enter the reference check"):
            # 9999 resolves to no question and 100 is an id the update would create: if either validator read
            # slot_format_json, the first would be an unresolved reference and the second an adopted one.
            slots = '[{"kind": "multiple_choice", "points": 9999}, {"kind": "short_answer", "points": 100}]'
            target = self.ws.target(runtime=True, mutate=lambda conn: conn.execute(
                "UPDATE dzi_part1_attempts SET slot_format_json = ? WHERE id = 1", (slots,)))
            with closing(fx.REAL_CONNECT(str(target))) as conn:
                self.assertEqual(tool.reference_gate(conn), {})
                self.assertEqual(parity.embedded_reference_problems(conn), [])
            result = self.dry(target, self.ws.source(), self.preimage)
            self.assertEqual((result.exit, result.report["status"]), (0, "no_change"), result.out)
            planned = self.dry(target, self.ws.source(lambda conn: fx.add_question(conn, 100, number=50)), self.preimage)
            self.assertEqual((planned.exit, planned.report["status"]), (0, "planned"), planned.out)
            self.assertEqual(fx.scalar(target, "SELECT slot_format_json FROM dzi_part1_attempts WHERE id = 1"), slots)
        with self.subTest("a boolean element is ignored"):
            target = self.ws.target(runtime=True, mutate=lambda conn: conn.execute(attempt, ("[1, true]",)))
            result = self.dry(target, self.ws.source(), self.preimage)
            self.assertEqual((result.exit, result.report["status"]), (0, "no_change"), result.out)
        with self.subTest("a boolean practice element is ignored"):
            target = self.ws.target(runtime=True, mutate=lambda conn: conn.execute(practice, ("[1, true]",)))
            result = self.dry(target, self.ws.source(), self.preimage)
            self.assertEqual((result.exit, result.report["status"]), (0, "no_change"), result.out)

    def test_43_clean_references_resolve_before_and_after(self):
        target = self.ws.target(runtime=True)
        with closing(fx.REAL_CONNECT(str(target))) as conn:
            self.assertEqual(parity.embedded_reference_problems(conn), [])
        _dry, _target = self.applies(lambda conn: fx.add_new_family(conn), target=target)
        with closing(fx.REAL_CONNECT(str(target))) as conn:
            self.assertEqual(parity.embedded_reference_problems(conn), [])
            self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])


class FileReferenceTest(HistoryCase):
    COLUMNS = {
        "assets.local_path": ("assets", lambda conn, value: fx.insert(conn, "assets", id=100, asset_type="pdf", local_path=value)),
        "exams.source_file": ("exams", lambda conn, value: fx.insert(conn, "exams", id=100, subject="informatika_it",
                                                                     level="DZI", year=2033, source_file=value)),
        "questions.image_path": ("questions", lambda conn, value: (fx.add_question(conn, 100, number=50),
                                                                   conn.execute("UPDATE questions SET image_path = ? "
                                                                                "WHERE id = 100", (value,)))),
        "curriculum_topics.note_path": ("curriculum_topics", lambda conn, value: (fx.add_topic(conn, 100),
                                                                                  conn.execute("UPDATE curriculum_topics SET "
                                                                                               "note_path = ? WHERE id = 100",
                                                                                               (value,)))),
        "curriculum_areas.moc_filename": ("curriculum_areas", lambda conn, value: fx.insert(
            conn, "curriculum_areas", id=100, area_id="area-100", title_bg="Нова", moc_filename=value)),
        "curriculum_modules.moc_filename": ("curriculum_modules", lambda conn, value: fx.insert(
            conn, "curriculum_modules", id=100, module_number=9, title_bg="Нов", **{"class": 11}, moc_filename=value)),
        "official_exam_sources.local_path": ("official_exam_sources", lambda conn, value: fx.insert(
            conn, "official_exam_sources", id=100, exam_id=1, source_kind="extra", local_path=value)),
        "practical_task_resources.resource_path": ("practical_task_resources", lambda conn, value: (
            fx.add_new_family(conn), conn.execute("UPDATE practical_task_resources SET resource_path = ? WHERE id = 100",
                                                  (value,)))),
    }

    def outcome(self, column, value):
        _table, build = self.COLUMNS[column]
        target = self.ws.target()
        source = self.ws.source(lambda conn: build(conn, value))
        return self.dry(target, source, self.preimage)

    def test_44_safe_references_are_accepted(self):
        good = {
            "assets.local_path": ["data/reference/dzi/official_pdfs/x.pdf", "web/static/img/questions/dzi/x.png",
                                  "data/reference/папка/файл 1.pdf"],
            "exams.source_file": ["reference/aug_2026/", "data/reference/dzi/official_pdfs/x.pdf"],
            "curriculum_topics.note_path": ["Topics/x.md"],
            "curriculum_areas.moc_filename": ["x-moc.md"],
            "curriculum_modules.moc_filename": ["x-moc.md"],
            "official_exam_sources.local_path": ["data/reference/dzi/official_pdfs/y.pdf"],
            "questions.image_path": ["web/static/img/questions/dzi/q.png"],
            "practical_task_resources.resource_path": ["data/reference/a.zip::dir/f.xlsx",
                                                       "data/reference/a.zip::папка/файл 2.xlsx"],
        }
        for column, values in good.items():
            for value in values:
                with self.subTest(f"{column} {value}"):
                    result = self.outcome(column, value)
                    self.assertEqual((result.exit, result.report["status"]), (0, "planned"), result.out)
                    self.assertGreaterEqual(result.report["file_references"]["inserted"][column]["total"], 1)
        with self.subTest("all nullable reference columns NULL"):
            dry = self.dry(self.ws.target(), self.ws.source(lambda conn: fx.add_question(conn, 100, number=50)),
                           self.preimage, fmt="text")
            self.assertEqual(dry.exit, 0)
            self.assertIn("file availability: not checked", dry.out)

    def test_45_unsafe_references_are_refused_without_being_echoed(self):
        nfd = unicodedata.normalize("NFD", "папка-й/x.pdf")
        self.assertNotEqual(nfd, unicodedata.normalize("NFC", nfd))
        generic = {"backslash": "data\\x.pdf", "parent segment": "data/../x.pdf", "dot segment": "data/./x.pdf",
                   "empty segment": "data//x.pdf", "leading slash": "/data/x.pdf", "tilde": "~/x.pdf",
                   "drive letter": "C:\\x.pdf", "file URI": "file://x", "https URI": "https://x", "NFD form": nfd,
                   "control character": "data/x\x01.pdf", "NUL": "data/x\x00.pdf", "leading space": " data/x.pdf",
                   "trailing space": "data/x.pdf ", "empty string": "", "over 512 characters": "d/" + "a" * 520}
        for column in self.COLUMNS:
            values = dict(generic)
            if column == "exams.source_file":
                values["two trailing slashes"] = "reference/x//"
            elif column.endswith("moc_filename"):
                values["a slash"] = "dir/x-moc.md"
            elif column == "practical_task_resources.resource_path":
                values.update({"two separators": "a.zip::b::c.xlsx", "non-zip archive": "data/a.txt::f.xlsx",
                               "empty member": "data/a.zip::", "directory member": "data/a.zip::dir/"})
            else:
                values["trailing slash outside source_file"] = "data/dir/"
            if column == "practical_task_resources.resource_path":
                values.pop("empty string")                      # the schema's own CHECK refuses it first
            for label, value in values.items():
                with self.subTest(f"{column}: {label}"):
                    result = self.outcome(column, value)
                    self.assert_refused(result, 1, "plan_conflicts", row_codes=["unsafe_file_reference"])
                    listed = [item for item in result.report["conflicts"]["listed"] if item["code"] == "unsafe_file_reference"]
                    self.assertTrue(listed)
                    self.assertEqual(listed[0]["columns"], [column.split(".")[1]])
                    shown = value.strip()[:12]
                    if shown and shown.isascii() and shown.isprintable():
                        self.assertNotIn(shown, result.out)
        with self.subTest("a non-string value"):
            source = self.ws.source(lambda conn: fx.insert(conn, "assets", id=100, asset_type="pdf", local_path="data/ok.pdf"))
            fx.tamper_table(source, "assets", lambda rows: [dict(row, local_path=5) if row["id"] == 100 else row
                                                            for row in rows])
            result = self.dry(self.ws.target(), source, self.preimage)
            self.assert_refused(result, 1, "plan_conflicts", row_codes=["unsafe_file_reference"])

    def test_46_the_local_validator_agrees_with_the_application_resolver(self):
        samples = ["data/reference/x.pdf", "web/static/img/q.png", "папка/файл 1.xlsx", "Topics/x.md", "a b/c",
                   "", " a", "a ", "/a", "~a", "a\\b", "a:b", "C:/x", "a/../b", "a/./b", "a//b", "a/b/", "./a", "../a",
                   "a\x00b", "a\x1fb", unicodedata.normalize("NFD", "й"), "a/b//c", "x" * 100]
        for sample in samples:
            with self.subTest(sample=repr(sample)):
                try:
                    resolver._relative_parts(sample)
                    expected = True
                except resolver.ResourceResolutionError:
                    expected = False
                self.assertEqual(tool._relative_ok(sample), expected)
        for archive, member in (("data/reference/a.zip", "dir/f.xlsx"), ("data/reference/a.zip", "/f"),
                                ("data/../a.zip", "f"), ("data/reference/a.zip", "d//f"), ("a b.zip", "папка/ф.xlsx")):
            reference = f"{archive}::{member}"
            parts_ok = tool._relative_ok(archive) and tool._relative_ok(member)
            self.assertEqual(not tool.file_reference_problem("practical_task_resources", "resource_path", reference),
                             parts_ok and archive.lower().endswith(".zip"), reference)

    def test_47_referenced_files_are_never_touched(self):
        marker = "never-created-marker"
        seen = []
        real = {"open": builtins.open, "io.open": io.open, "os.open": os.open, "os.stat": os.stat, "os.lstat": os.lstat,
                "exists": os.path.exists, "isfile": os.path.isfile, "Path.stat": pathlib.Path.stat}

        def spy(name):
            def wrapper(target, *args, **kwargs):
                if marker in str(target):
                    seen.append((name, str(target)))
                return real[name](target, *args, **kwargs)
            return wrapper

        def path_stat(self_, *args, **kwargs):
            if marker in str(self_):
                seen.append(("Path.stat", str(self_)))
            return real["Path.stat"](self_, *args, **kwargs)

        def build(conn):
            fx.add_new_family(conn)
            fx.insert(conn, "assets", id=101, asset_type="pdf", local_path=f"data/reference/{marker}.pdf")
            fx.insert(conn, "official_exam_sources", id=100, exam_id=1, source_kind="x",
                      local_path=f"data/reference/{marker}-2.pdf")
        target, source = self.ws.target(), self.ws.source(build)
        with mock.patch.object(builtins, "open", spy("open")), mock.patch.object(io, "open", spy("io.open")), \
                mock.patch.object(os, "open", spy("os.open")), mock.patch.object(os, "stat", spy("os.stat")), \
                mock.patch.object(os, "lstat", spy("os.lstat")), mock.patch.object(os.path, "exists", spy("exists")), \
                mock.patch.object(os.path, "isfile", spy("isfile")), mock.patch.object(pathlib.Path, "stat", path_stat):
            dry = self.dry(target, source, self.preimage, fmt="text")
            token = dry.report["plan_sha256"] if dry.out.startswith("{") else re.search(r"plan sha256: (\w+)", dry.out).group(1)
            applied = self.apply(target, source, self.preimage, token)
        self.assertEqual(applied.exit, 0, applied.out)
        self.assertEqual(seen, [])
        self.assertIn("file availability: not checked", dry.out)
        self.assertIn(marker, dry.out)                                     # listed, so the spies were not vacuous
        self.assertEqual(fx.scalar(target, "SELECT COUNT(*) FROM assets WHERE local_path LIKE ?", (f"%{marker}%",)), 1)

    def test_48_reports_list_at_most_twenty_references_per_column(self):
        def many(conn):
            for number in range(200):
                fx.insert(conn, "assets", id=1000 + number, asset_type="image", local_path=f"data/reference/bulk/{number}.png",
                          sha256="d" * 64)
        result = self.dry(self.ws.target(), self.ws.source(many), self.preimage)
        entry = result.report["file_references"]["inserted"]["assets.local_path"]
        self.assertEqual((entry["total"], len(entry["listed"])), (200, 20))
        self.assertEqual(entry["listed"][0], {"path": "data/reference/bulk/0.png", "sha256": "d" * 64,
                                              "availability": "not checked"})
        with self.subTest("control characters are escaped"):
            self.assertNotIn("\x1b", tool._escape("a\x1b[31mb"))
            self.assertEqual(tool._escape("a\x07b"), "a\\x07b")
            source = self.ws.source()
            fx.tamper_table(source, "exam_task_questions", lambda rows: rows + [
                {"task_id": 1, "question_id": 1, "role": "r\x07\x1b[31m", "sub_number": None}])
            refused = self.dry(self.ws.target(), source, self.preimage, fmt="text")
            self.assertNotIn("\x07", refused.out)
            self.assertNotIn("\x1b", refused.out)
            self.assertIn("\\u0007", refused.out)


class RedactionTest(HistoryCase):
    def test_50_no_marker_reaches_stdout_or_stderr_in_any_outcome(self):
        base = self.ws.path("PATHMARK-dir")
        base.mkdir()
        target = self.ws.target("PATHMARK-dir/PATHMARK-target.db", runtime=True, marker="ROWMARK")
        source = self.ws.source(lambda conn: fx.add_new_family(conn), name="PATHMARK-source")
        preimage = self.ws.preimage("PATHMARK-preimage")
        differs = self.ws.source(lambda conn: conn.execute("UPDATE questions SET prompt = 'VALMARK differs' WHERE id = 1"),
                                 name="PATHMARK-differs")
        collected = {}

        def record(label, result, expected_exit):
            self.assertEqual(result.exit, expected_exit, (label, result.out))
            collected[label] = result.out + result.err

        plan = self.dry(target, source, preimage)
        record("planned", plan, 0)
        token = plan.report["plan_sha256"]
        record("incomplete", self.dry(target, source), 1)
        record("conflicts", self.dry(target, differs, preimage), 1)
        record("target missing", self.dry(self.ws.path("PATHMARK-dir/missing.db"), source, preimage), 1)
        record("usage", fx.run_tool(target, source, raw_argv=["--db", str(target), "--bogus", "PATHMARK"]), 1)
        record("stale", self.apply(target, source, preimage, "0" * 64), 2)

        def boom(table):
            raise RuntimeError("EXCMARK exception text")
        record("write failed", self.apply(target, source, preimage, token, hooks={"after_insert": boom}), 3)
        io_error = fx.CommitInjection("before", "EXCMARK disk I/O")
        record("commit not committed", self.apply(target, source, preimage, token, connect=fx.FactoryConnect(
            [fx.make_connection_class([], io_error)])), 3)
        record("committed after an error", self.apply(self.ws.target("PATHMARK-dir/second.db", runtime=True, marker="ROWMARK"),
                                                      source, preimage, token, connect=fx.FactoryConnect(
            [fx.make_connection_class([], fx.CommitInjection("after", "EXCMARK disk I/O"))])), 4)
        unknown_target = self.ws.target("PATHMARK-dir/third.db", runtime=True, marker="ROWMARK")
        record("unknown", self.apply(unknown_target, source, preimage, token, connect=fx.FactoryConnect(
            [fx.make_connection_class([], fx.CommitInjection("after", "EXCMARK disk I/O"))], fail_on={1})), 5)

        class Broken(io.StringIO):
            def write(self, text):
                raise OSError("EXCMARK closed stream")
        err = io.StringIO()
        fourth = self.ws.target("PATHMARK-dir/fourth.db", runtime=True, marker="ROWMARK")
        result = fx.run_tool(fourth, source, preimage=preimage, apply_hash=token, out=Broken(), err=err, fmt="json")
        self.assertEqual(result.exit, 4)
        collected["receipt not written"] = err.getvalue()
        self.assertIn("receipt_not_written", err.getvalue())
        record("applied", self.apply(self.ws.target("PATHMARK-dir/fifth.db", runtime=True, marker="ROWMARK"), source,
                                     preimage, token), 0)
        for label, text in collected.items():
            for marker in ("PATHMARK", "ROWMARK", "VALMARK", "EXCMARK", "rowmark"):
                self.assertNotIn(marker, text, (label, marker))
            self.assertNotIn(str(self.ws.root), text)
            self.assertNotIn("Traceback", text)


if __name__ == "__main__":
    unittest.main()
