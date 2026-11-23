"""B4 canonical content updater: plan, identity, apply, preservation, report and CLI.

Every database is a temporary file built by ``tests.update_canonical_fixtures`` (standard library
only). Each test installs the connect spy that refuses ``data/questions.db``. Numbers in the test
names are the case numbers of docs/reviews/canonical-content-updater-test-matrix.md.
"""

import ast
import io
import json
import math
import os
import re
import shutil
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

from src import canonical_export as export
from src import canonical_tables as ct
from src import manage_migrations
from src import update_canonical_content as tool
from tests import update_canonical_fixtures as fx
from tests.runtime_db_guard import RUNTIME_DB, RuntimeDatabaseAccess

ROOT = fx.ROOT
MODULE_SOURCE = (ROOT / "src" / "update_canonical_content.py").read_text(encoding="utf-8")
LATE_MIGRATION = "028_fixture_marker.sql"


def source_lines(directory, table):
    return fx.table_path(directory, table).read_text(encoding="utf-8").splitlines()


def statements(log):
    return [entry.upper() for entry in log if isinstance(entry, str)]


class BaselineAndFirstApplyTest(fx.UpdaterTestCase):
    def new_rows_source(self, **kwargs):
        return self.ws.source(lambda conn: fx.add_new_family(conn), **kwargs)

    def test_01_real_canonical_is_a_no_op_and_an_empty_footprint_takes_no_write_lock(self):
        target = self.ws.target("real.db", template=fx.real_canonical_template())
        before_bytes = fx.file_bytes_digest(target)
        listing = fx.tree_listing(target.parent)
        result = self.dry(target, fx.CANONICAL_DIR, preimage=fx.CANONICAL_DIR)
        self.assertEqual((result.exit, result.report["status"], result.report["run_code"]), (0, "no_change", None))
        self.assertEqual(sum(row["insert"] for row in result.report["tables"].values()), 0)
        self.assertGreater(sum(row["identical"] for row in result.report["tables"].values()), 1000)
        self.assertEqual(result.report["conflicts"]["total"], 0)
        # the historical identical non-id pairs are present and are not refused
        self.assertEqual(fx.scalar(target, "SELECT COUNT(*) FROM questions WHERE id IN (371, 393, 659, 681)"), 4)
        log = []
        connect = fx.FactoryConnect([fx.make_connection_class(log)])
        applied = self.apply(target, fx.CANONICAL_DIR, fx.CANONICAL_DIR, result.report["plan_sha256"], connect=connect)
        self.assertEqual((applied.exit, applied.report["status"]), (0, "no_change"))
        self.assertNotIn("BEGIN IMMEDIATE", statements(log))
        self.assertNotIn("COMMIT", statements(log))
        self.assertEqual(fx.file_bytes_digest(target), before_bytes)
        self.assertEqual(fx.tree_listing(target.parent), listing)

    def test_02_dry_run_lists_actions_prints_a_hash_and_changes_nothing(self):
        target, preimage, source = self.ws.target(), self.ws.preimage(), self.new_rows_source()
        before_bytes, listing = fx.file_bytes_digest(target), fx.tree_listing(target.parent)
        result = self.dry(target, source, preimage)
        report = result.report
        self.assertEqual((result.exit, report["status"]), (0, "planned"))
        self.assertRegex(report["plan_sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(report["tables"]["questions"]["insert"], 1)
        self.assertEqual(report["tables"]["multiple_choice_options"]["insert"], 2)
        self.assertEqual(report["tables"]["exam_task_questions"]["insert"], 1)
        self.assertIn({"table": "questions", "pre": 7, "expected": 100}, report["sequence_items"])
        self.assertEqual(fx.file_bytes_digest(target), before_bytes)
        self.assertEqual(fx.tree_listing(target.parent), listing)

    def test_03_apply_makes_the_after_state_exactly_target_plus_candidates(self):
        target, preimage, source = self.ws.target(runtime=True), self.ws.preimage(), self.new_rows_source()
        token = self.plan_of(target, source, preimage).report["plan_sha256"]
        result = self.apply(target, source, preimage, token, fmt="text")
        self.assertEqual(result.exit, 0, result.out)
        self.assertIn("COMMITTED", result.out)
        manifest = json.loads((source / ct.MANIFEST_NAME).read_text(encoding="utf-8"))
        sequences = fx.sequence_map(target)
        for table in ct.tables_of(ct.CANONICAL):
            self.assertEqual(fx.canonical_lines(target, table), source_lines(source, table), table)
            entry = manifest["tables"][table]
            if entry["autoincrement"]:
                self.assertEqual(sequences[table], entry["sqlite_sequence"], table)
        self.assertNotIn("practical_tasks", sequences)

    def test_04_an_old_token_is_stale_and_a_fresh_dry_run_is_a_no_op(self):
        target, preimage, source = self.ws.target(), self.ws.preimage(), self.new_rows_source()
        token = self.plan_of(target, source, preimage).report["plan_sha256"]
        self.assertEqual(self.apply(target, source, preimage, token).exit, 0)
        before = fx.db_snapshot(target)
        stale = self.apply(target, source, preimage, token)
        self.assert_refused(stale, 2, "stale_plan")
        self.assert_untouched(target, before)
        fresh = self.dry(target, source, preimage)
        self.assertEqual((fresh.exit, fresh.report["status"]), (0, "no_change"))
        self.assertNotEqual(fresh.report["plan_sha256"], token)
        again = self.apply(target, source, preimage, fresh.report["plan_sha256"])
        self.assertEqual((again.exit, again.report["status"]), (0, "no_change"))

    def test_05_incomplete_and_usage_errors_exit_one_not_argparse_two(self):
        target, preimage, source = self.ws.target(), self.ws.preimage(), self.ws.source()
        incomplete = self.dry(target, source)
        self.assertEqual((incomplete.exit, incomplete.report["status"], incomplete.report["run_code"],
                          incomplete.report["plan_sha256"]), (1, "incomplete", "preimage_required", None))
        no_preimage = fx.run_tool(target, source, raw_argv=["--db", str(target), "--source", str(source), "--apply",
                                                           "--expect-plan-sha256", "a" * 64, "--format", "json"])
        self.assert_refused(no_preimage, 1, "preimage_required")
        for label, argv in (
            ("apply without a token", ["--db", str(target), "--source", str(source), "--preimage", str(preimage),
                                       "--apply", "--format", "json"]),
            ("token without apply", ["--db", str(target), "--source", str(source), "--preimage", str(preimage),
                                     "--expect-plan-sha256", "a" * 64, "--format", "json"]),
            ("short token", ["--db", str(target), "--source", str(source), "--preimage", str(preimage), "--apply",
                             "--expect-plan-sha256", "abc", "--format", "json"]),
            ("missing --db", ["--source", str(source), "--format", "json"]),
            ("missing --source", ["--db", str(target), "--format", "json"]),
            ("unknown flag", ["--db", str(target), "--source", str(source), "--force", "--format", "json"]),
            ("abbreviated flag", ["--db", str(target), "--sourc", str(source), "--format", "json"]),
            ("timeout too small", ["--db", str(target), "--source", str(source), "--busy-timeout-ms", "50",
                                   "--format", "json"]),
            ("timeout too large", ["--db", str(target), "--source", str(source), "--busy-timeout-ms", "60001",
                                   "--format", "json"]),
            ("timeout not a number", ["--db", str(target), "--source", str(source), "--busy-timeout-ms", "x",
                                      "--format", "json"]),
            ("bad format", ["--db", str(target), "--source", str(source), "--format", "xml"]),
        ):
            with self.subTest(label):
                result = fx.run_tool(target, source, raw_argv=argv)
                self.assertEqual(result.exit, 1, result.out)
                self.assertIn("usage_error", result.out)
                self.assert_clean(result)
        for flag in ("--create", "--migrate", "--yes", "--ignore-conflicts"):
            with self.subTest(flag):
                result = fx.run_tool(target, source, raw_argv=["--db", str(target), "--source", str(source), flag,
                                                              "--format", "json"])
                self.assertEqual(result.exit, 1)

    def test_06_reports_are_deterministic_and_text_matches_json(self):
        target, preimage, source = self.ws.target(), self.ws.preimage(), self.new_rows_source()
        one, two = self.dry(target, source, preimage), self.dry(target, source, preimage)
        self.assertEqual(one.out, two.out)
        text_one, text_two = self.dry(target, source, preimage, fmt="text"), self.dry(target, source, preimage, fmt="text")
        self.assertEqual(text_one.out, text_two.out)
        for table, counters in one.report["tables"].items():
            self.assertIn(f"table {table}: identical {counters['identical']}, insert {counters['insert']}, "
                          f"target_only {counters['target_only']}, conflict {counters['conflict']}", text_one.out)
        self.assertIn(f"plan sha256: {one.report['plan_sha256']}", text_one.out)
        for item in one.report["sequence_items"]:
            self.assertIn(f"sequence item {item['table']}: {item['pre']} -> {item['expected']}", text_one.out)


class IdentityIdsAndSequencesTest(fx.UpdaterTestCase):
    def setUp(self):
        super().setUp()
        self.preimage = self.ws.preimage()

    def refuse(self, mutate_source, *, target=None, codes, mutate_target=None):
        target = target or self.ws.target(mutate=mutate_target)
        source = self.ws.source(mutate_source)
        before = fx.db_snapshot(target)
        result = self.dry(target, source, self.preimage)
        self.assert_refused(result, 1, "plan_conflicts", row_codes=codes)
        self.assert_untouched(target, before)
        return result

    def refuse_rows(self, table, build, *, codes):
        """Refusal for a source whose extra rows could not exist in one database (a unique-key clash)."""
        source = self.ws.source()
        fx.tamper_table(source, table, build)
        target = self.ws.target()
        before = fx.db_snapshot(target)
        result = self.dry(target, source, self.preimage)
        self.assert_refused(result, 1, "plan_conflicts", row_codes=codes)
        self.assert_untouched(target, before)
        return result

    def test_07_explicit_ids_and_sequences_are_inserted_verbatim(self):
        target = self.ws.target()
        source = self.ws.source(lambda conn: fx.add_new_family(conn, base=250))
        token = self.plan_of(target, source, self.preimage).report["plan_sha256"]
        self.assertEqual(self.apply(target, source, self.preimage, token).exit, 0)
        self.assertEqual(fx.scalar(target, "SELECT COUNT(*) FROM questions WHERE id = 250"), 1)
        self.assertEqual(fx.scalar(target, "SELECT COUNT(*) FROM exams WHERE id = 250"), 1)
        self.assertEqual(fx.sequence_map(target)["questions"], 250)
        self.assertEqual(fx.sequence_map(target)["exam_tasks"], 251)

    def test_08_a_candidate_id_held_by_a_target_only_row_differs(self):
        result = self.refuse(lambda conn: fx.add_question(conn, 50, number=9, prompt="Source text"),
                             codes=["value_differs"],
                             mutate_target=lambda conn: fx.add_question(conn, 50, number=9, prompt="Target text"))
        listed = result.report["conflicts"]["listed"]
        self.assertEqual([(item["table"], item["key"], item["columns"]) for item in listed],
                         [("questions", "[50]", ["prompt"])])

    def test_09_ids_at_or_below_the_target_high_water_are_refused(self):
        with self.subTest("below the target maximum id"):
            self.refuse(lambda conn: fx.add_question(conn, 110, number=9), codes=["id_not_above_high_water"],
                        mutate_target=lambda conn: fx.add_question(conn, 120, number=8))
        with self.subTest("below the target sequence while the row is absent (gap id)"):
            self.refuse(lambda conn: fx.add_question(conn, 200, number=9), codes=["id_not_above_high_water"],
                        mutate_target=lambda conn: fx.raise_sequence(conn, "questions", 500))

    def test_10_a_lowered_sequence_is_refused(self):
        with self.subTest("lowered entry"):
            self.refuse(None, codes=["sequence_below_preimage"],
                        mutate_target=lambda conn: fx.raise_sequence(conn, "questions", 3))
        with self.subTest("missing entry counts as zero"):
            self.refuse(None, codes=["sequence_below_preimage"],
                        mutate_target=lambda conn: conn.execute("DELETE FROM sqlite_sequence WHERE name = 'assets'"))

    def test_11_an_id_inside_the_preimage_range_but_absent_everywhere_is_refused(self):
        self.refuse(lambda conn: fx.add_question(conn, 6, number=9), codes=["id_not_above_high_water"])

    def base_without_sequences(self):
        """A base whose fill_in_subquestions and topic_concepts have no rows and no sequence entry."""
        def strip(conn):
            conn.execute("DELETE FROM fill_in_subquestions")
            conn.execute("DELETE FROM topic_concepts")
            conn.execute("DELETE FROM sqlite_sequence WHERE name IN ('fill_in_subquestions', 'topic_concepts')")
        base = self.ws.target("base.db", mutate=strip)
        return base, fx.export_database_dir(self.ws, base, "base-preimage")

    def test_12_sequence_only_plan_raises_entries_through_the_full_write_path(self):
        base, base_preimage = self.base_without_sequences()
        raised = self.ws.source(lambda conn: (fx.raise_sequence(conn, "questions", 300),
                                              fx.raise_sequence(conn, "fill_in_subquestions", 50)), base=base)
        target = self.ws.target("seq-target.db", runtime=True, template=base)
        non_canonical_before = {name: seq for name, seq in fx.sequence_map(target).items()
                                if name not in ct.tables_of(ct.CANONICAL)}
        rows_before = {table: fx.canonical_lines(target, table) for table in ct.tables_of(ct.CANONICAL)}
        dry = self.plan_of(target, raised, base_preimage)
        self.assertEqual(dry.report["status"], "planned")
        self.assertEqual(sum(row["insert"] for row in dry.report["tables"].values()), 0)
        self.assertEqual(sorted((item["table"], item["pre"], item["expected"]) for item in dry.report["sequence_items"]),
                         [("fill_in_subquestions", None, 50), ("questions", 7, 300)])
        log = []
        connect = fx.FactoryConnect([fx.make_connection_class(log)])
        applied = self.apply(target, raised, base_preimage, dry.report["plan_sha256"], connect=connect)
        self.assertEqual((applied.exit, applied.report["status"]), (0, "applied"), applied.out)
        self.assertIn("BEGIN IMMEDIATE", statements(log))
        self.assertIn("COMMIT", statements(log))
        sequences = fx.sequence_map(target)
        self.assertEqual((sequences["questions"], sequences["fill_in_subquestions"]), (300, 50))   # UPDATE and INSERT
        self.assertNotIn("topic_concepts", sequences)                     # a null manifest sequence is untouched
        self.assertEqual({name: seq for name, seq in sequences.items() if name not in ct.tables_of(ct.CANONICAL)},
                         non_canonical_before)
        self.assertEqual({table: fx.canonical_lines(target, table) for table in ct.tables_of(ct.CANONICAL)}, rows_before)

    def sequence_only_case(self, raised_seq: int = 300):
        source = self.ws.source(lambda conn: fx.raise_sequence(conn, "questions", raised_seq))
        target = self.ws.target(runtime=True)
        return target, source, self.plan_of(target, source, self.preimage).report["plan_sha256"]

    def test_12a_an_empty_footprint_never_takes_the_write_lock(self):
        target = self.ws.target(mutate=lambda conn: fx.raise_sequence(conn, "questions", 500))
        source = self.ws.source()
        dry = self.dry(target, source, self.preimage)
        self.assertEqual((dry.exit, dry.report["status"], dry.report["sequence_items"]), (0, "no_change", []))
        log = []
        applied = self.apply(target, source, self.preimage, dry.report["plan_sha256"],
                             connect=fx.FactoryConnect([fx.make_connection_class(log)]))
        self.assertEqual((applied.exit, applied.report["status"]), (0, "no_change"))
        self.assertNotIn("BEGIN IMMEDIATE", statements(log))
        self.assertNotIn("COMMIT", statements(log))

    def test_12b_failures_after_the_sequence_write_and_the_restore_lock_boundary(self):
        target, source, token = self.sequence_only_case()
        before = fx.db_snapshot(target)
        lock = target.parent / "restore-in-progress"
        with self.subTest("hook raises after apply_sequence"):
            def boom(table):
                raise RuntimeError("injected")
            result = self.apply(target, source, self.preimage, token, hooks={"after_sequence": boom})
            self.assert_refused(result, 3, "write_failed", status="not_committed")
            self.assertEqual(fx.sequence_map(target)["questions"], 7)
            self.assert_untouched(target, before)
        with self.subTest("restore lock found under the write lock before the first canonical write"):
            result = self.apply(target, source, self.preimage, token, hooks={"after_lock": lambda conn: lock.mkdir()})
            lock.rmdir()
            self.assert_refused(result, 1, "restore_lock_present")
            self.assertEqual(fx.sequence_map(target)["questions"], 7)
            self.assert_untouched(target, before)
        with self.subTest("restore lock found after the first canonical write"):
            result = self.apply(target, source, self.preimage, token, hooks={"after_sequence": lambda table: lock.mkdir()})
            lock.rmdir()
            self.assert_refused(result, 3, "restore_lock_present", status="not_committed")
            self.assert_untouched(target, before)

    def commit_run(self, injection, *, connect_kwargs=None, probe_rewrite=None, source_seq=300):
        target, source, token = self.sequence_only_case(source_seq)
        log = []
        classes = [fx.make_connection_class(log, injection)]
        if probe_rewrite is not None:
            classes.append(fx.make_connection_class([], rewrite=probe_rewrite))
        connect = fx.FactoryConnect(classes, **(connect_kwargs or {}))
        result = self.apply(target, source, self.preimage, token, connect=connect, fmt="text")
        return target, result

    def test_12c_a_commit_error_whose_probe_sees_the_raise_is_reported_committed(self):
        target, result = self.commit_run(fx.CommitInjection("after"))
        self.assertEqual(result.exit, 4, result.out)
        self.assertIn("COMMITTED: the planned write footprint is visible to a fresh connection after a COMMIT error",
                      result.out)
        for banned in ("rolled back", "durable", "this run committed"):
            self.assertNotIn(banned, result.out)
        self.assertEqual(fx.sequence_map(target)["questions"], 300)
        self.assertIn("status: applied", result.out)
        self.assertIn("run code: commit_error_committed", result.out)

    def test_12d_a_commit_error_whose_probe_sees_the_pre_state_is_not_committed(self):
        for label, injection, code in (("io", fx.CommitInjection("before"), 3), ("busy", fx.CommitInjection("busy"), 2)):
            with self.subTest(label):
                target, result = self.commit_run(injection)
                self.assertEqual(result.exit, code, result.out)
                self.assertIn("status: not_committed", result.out)
                self.assertIn("run code: commit_error_not_committed", result.out)
                self.assertEqual(fx.sequence_map(target)["questions"], 7)

    def test_12e_an_unconfirmable_commit_error_is_an_unknown_outcome(self):
        with self.subTest("the probe connection cannot open"):
            target, result = self.commit_run(fx.CommitInjection("after"), connect_kwargs={"fail_on": {1}})
            self.assertEqual(result.exit, 5, result.out)
            self.assertIn("status: outcome_unknown", result.out)
            self.assertIn("Do not retry this plan or token", result.out)
        sequence_select = "SELECT name, seq FROM sqlite_sequence"
        for label, replacement in (("an entry at neither value", 12345),):
            with self.subTest(label):
                def rewrite(sql, replacement=replacement):
                    if sql == sequence_select:
                        return f"SELECT name, CASE WHEN name = 'questions' THEN {replacement} ELSE seq END FROM sqlite_sequence"
                target, result = self.commit_run(fx.CommitInjection("after"), probe_rewrite=rewrite)
                self.assertEqual(result.exit, 5, result.out)
                self.assertIn("run code: commit_outcome_unknown", result.out)
        with self.subTest("one entry at the raise and another at the pre-state"):
            source = self.ws.source(lambda conn: (fx.raise_sequence(conn, "questions", 300),
                                                  fx.raise_sequence(conn, "assets", 90)))
            target = self.ws.target()
            token = self.plan_of(target, source, self.preimage).report["plan_sha256"]

            def rewrite(sql):
                if sql == sequence_select:
                    return "SELECT name, CASE WHEN name = 'questions' THEN 7 ELSE seq END FROM sqlite_sequence"
            connect = fx.FactoryConnect([fx.make_connection_class([], fx.CommitInjection("after")),
                                         fx.make_connection_class([], rewrite=rewrite)])
            result = self.apply(target, source, self.preimage, token, connect=connect)
            self.assertEqual((result.exit, result.report["status"]), (5, "outcome_unknown"))
        with self.subTest("a candidate row visible while its sequence entry shows the pre-state"):
            source = self.ws.source(lambda conn: fx.add_question(conn, 100, number=9))
            target = self.ws.target()
            token = self.plan_of(target, source, self.preimage).report["plan_sha256"]

            def rewrite(sql):
                if sql == sequence_select:
                    return "SELECT name, CASE WHEN name = 'questions' THEN 7 ELSE seq END FROM sqlite_sequence"
            connect = fx.FactoryConnect([fx.make_connection_class([], fx.CommitInjection("after")),
                                         fx.make_connection_class([], rewrite=rewrite)])
            result = self.apply(target, source, self.preimage, token, connect=connect)
            self.assertEqual(result.exit, 5, result.out)

    def test_12f_a_transaction_still_open_after_the_single_rollback_is_unknown_and_never_probed(self):
        # implementation map section 9 step 4(b): COMMIT raised, the one ROLLBACK fails and leaves the
        # transaction open, so the outcome is unknown (exit 5) and no fresh probe connection is opened.
        target, source, token = self.sequence_only_case()
        before = fx.db_snapshot(target)
        events = []
        connect = fx.FactoryConnect([fx.stuck_rollback_class(events), sqlite3.Connection])
        result = self.apply(target, source, self.preimage, token, connect=connect, fmt="text")
        self.assertEqual(events, ["commit", "rollback"])                  # one COMMIT, one failed ROLLBACK
        self.assertEqual(result.exit, 5, result.out)
        self.assertIn("status: outcome_unknown", result.out)
        self.assertIn("run code: commit_outcome_unknown", result.out)
        self.assertEqual(len(connect.calls), 1)                           # the probe connection never opened
        self.assertEqual(fx.sequence_map(target)["questions"], 7)         # closing the connection ended the transaction
        self.assertEqual(fx.db_snapshot(target), before)
        self.assert_clean(result)

    def test_13_composite_and_no_id_tables_use_their_full_key(self):
        def additions(conn):
            fx.add_new_family(conn)
            fx.insert(conn, "topic_classes", topic_id=1, **{"class": 12})
            fx.insert(conn, "assessment_event_sections", assessment_event_id=1, section_id=2, weight=0.5)
            fx.insert(conn, "topic_prerequisites", topic_id=1, requires_topic_id=2)
        target, source = self.ws.target(), self.ws.source(additions)
        dry = self.plan_of(target, source, self.preimage)
        for table in ("topic_classes", "assessment_event_sections", "topic_prerequisites", "exam_task_questions",
                      "practical_tasks"):
            self.assertEqual(dry.report["tables"][table]["insert"], 1, table)
        self.assertNotIn("practical_tasks", {item["table"] for item in dry.report["sequence_items"]})
        self.assertEqual(self.apply(target, source, self.preimage, dry.report["plan_sha256"]).exit, 0)
        self.assertEqual(fx.scalar(target, "SELECT COUNT(*) FROM topic_classes WHERE topic_id = 1 AND class = 12"), 1)
        sequences = fx.sequence_map(target)
        for table in ("topic_classes", "assessment_event_sections", "topic_prerequisites", "exam_task_questions",
                      "practical_tasks"):
            self.assertNotIn(table, sequences)
        with self.subTest("a differing non-key column"):
            source = self.ws.source(lambda conn: conn.execute("UPDATE assessment_event_sections SET weight = 0.25"))
            result = self.dry(self.ws.target(), source, self.preimage)
            self.assert_refused(result, 1, "plan_conflicts", row_codes=["value_differs"])
            self.assertEqual(result.report["conflicts"]["listed"][0]["columns"], ["weight"])
        with self.subTest("absent from the target but in the preimage"):
            target2 = self.ws.target(mutate=lambda conn: conn.execute("DELETE FROM topic_classes WHERE topic_id = 2"))
            result = self.dry(target2, self.ws.source(), self.preimage)
            self.assert_refused(result, 1, "plan_conflicts", row_codes=["deleted_in_target"])

    def test_14_null_and_mistyped_keys_get_fixed_codes(self):
        base_source = self.ws.source()
        target = self.ws.target()
        before = fx.db_snapshot(target)
        cases = {
            "null_primary_key": ("topic_classes", lambda rows: rows + [{"topic_id": None, "class": 11}]),
            "float": ("topic_classes", lambda rows: rows + [{"topic_id": 1.5, "class": 11}]),
            "string": ("topic_classes", lambda rows: rows + [{"topic_id": "5", "class": 11}]),
            "boolean": ("topic_classes", lambda rows: rows + [{"topic_id": True, "class": 12}]),
            "text key typed as an integer": ("exam_task_questions", lambda rows: rows + [
                {"task_id": 1, "question_id": 2, "role": 5, "sub_number": None}]),
        }
        for label, (table, transform) in cases.items():
            with self.subTest(label):
                source = self.ws.root / f"typed-{re.sub(r'[^a-z]', '', label)}"
                shutil.copytree(base_source, source)
                fx.tamper_table(source, table, transform)
                result = self.dry(target, source, self.preimage)
                expected = "null_primary_key" if label == "null_primary_key" else "invalid_key_type"
                self.assert_refused(result, 1, "plan_conflicts", row_codes=[expected])
                self.assert_untouched(target, before)
        with self.subTest("id above 2^63-1"):
            source = self.ws.root / "typed-huge"
            shutil.copytree(base_source, source)
            fx.tamper_table(source, "curricula", lambda rows: rows + [dict(rows[0], id=2 ** 63, grade=13)])
            result = self.dry(target, source, self.preimage)
            self.assert_refused(result, 1, "plan_conflicts", row_codes=["invalid_key_type"])

    def test_15_a_natural_key_with_a_null_part_never_collides(self):
        def additions(conn):
            fx.add_question(conn, 120, number=None, prompt="Първи без номер", source_exam="legacy")
            fx.add_question(conn, 121, number=None, prompt="Втори без номер", source_exam="legacy")
        target, source = self.ws.target(), self.ws.source(additions)
        result = self.plan_of(target, source, self.preimage)
        self.assertEqual(result.report["tables"]["questions"]["insert"], 2)
        self.assertEqual(result.report["conflicts"]["total"], 0)

    def test_16_a_natural_key_held_under_another_id_is_refused(self):
        # A source cannot hold the key twice (check_directory), so a target-only row holds it under another id.
        cases = {
            "topic_slug": (lambda conn: fx.add_topic(conn, 50, slug="shared-slug"),
                           lambda conn: fx.add_topic(conn, 100, slug="shared-slug")),
            "source exam and number": (lambda conn: fx.add_question(conn, 50, number=70, source_exam="shared"),
                                       lambda conn: fx.add_question(conn, 100, number=70, source_exam="shared")),
            "option letter": (lambda conn: fx.insert(conn, "multiple_choice_options", id=500, question_id=1,
                                                     option_letter="E", option_text="Само в целта"),
                              lambda conn: fx.insert(conn, "multiple_choice_options", id=600, question_id=1,
                                                     option_letter="E", option_text="В източника")),
        }
        for label, (in_target, in_source) in cases.items():
            with self.subTest(label):
                self.refuse(in_source, codes=["unique_key_exists"], mutate_target=in_target)

    def test_17_declared_unique_keys_beyond_the_natural_keys_are_refused(self):
        cases = {
            "module_number": ("curriculum_modules", lambda rows: rows + [dict(rows[0], id=100, module_slug="other")]),
            "exam sha256": ("exams", lambda rows: rows + [dict(rows[0], id=100, year=2030, session="june")]),
            "lesson title": ("lessons", lambda rows: rows + [dict(rows[0], id=100, lesson_number=9)]),
            "partial module_slug": ("curriculum_modules", lambda rows: rows + [dict(rows[0], id=100, module_number=9)]),
        }
        for label, (table, build) in cases.items():
            with self.subTest(label):
                self.refuse_rows(table, build, codes=["unique_key_exists"])
        with self.subTest("candidate against candidate"):
            self.refuse_rows("curriculum_modules", lambda rows: rows + [
                dict(rows[0], id=100, module_number=9, module_slug="new-100"),
                dict(rows[0], id=101, module_number=9, module_slug="new-101")], codes=["unique_key_in_source_twice"])

    def test_17_unsupported_unique_indexes_refuse_the_plan(self):
        variants = {
            "expression column": "CREATE UNIQUE INDEX uniq_alias_lower ON topic_aliases (lower(alias_slug));\n",
            "predicate other than IS NOT NULL": "CREATE UNIQUE INDEX uniq_alias_merge ON topic_aliases (alias_slug) "
                                                "WHERE action = 'merge';\n",
            "non-BINARY collation": "CREATE UNIQUE INDEX uniq_alias_nocase ON topic_aliases (alias_slug COLLATE NOCASE);\n",
        }
        for label, sql in variants.items():
            with self.subTest(label):
                directory = fx.fixture_migrations(self.ws, {LATE_MIGRATION: sql}, name=f"mig-{len(label)}")
                base = fx.custom_base(self.ws, directory, f"base-{len(label)}.db")
                source = fx.export_database_dir(self.ws, base, f"src-{len(label)}", directory)
                target = self.ws.target(f"t-{len(label)}.db", template=base)
                result = self.dry(target, source, source, migrations_dir=directory)
                self.assert_refused(result, 1, "plan_conflicts", row_codes=["unsupported_unique_index"])

    def test_18_the_non_id_duplicate_guard_applies_to_insert_candidates_only(self):
        def clone_of_five(conn, new_id):
            row = dict(zip(*[[c[1] for c in conn.execute('PRAGMA table_info("questions")')],
                             conn.execute("SELECT * FROM questions WHERE id = 5").fetchone()]))
            row["id"] = new_id
            fx.insert(conn, "questions", **row)
        with self.subTest("a candidate identical to an existing row except its id"):
            self.refuse(lambda conn: clone_of_five(conn, 150), codes=["duplicate_content"])
        with self.subTest("candidate against candidate"):
            def twin(conn):
                clone_of_five(conn, 150)
                clone_of_five(conn, 151)
            result = self.refuse(twin, codes=["duplicate_content"])
        with self.subTest("a difference only in a timestamp is not a duplicate"):
            def later(conn):
                clone_of_five(conn, 150)
                conn.execute("UPDATE questions SET created_at = '2027-01-01 00:00:00' WHERE id = 150")
            self.plan_of(self.ws.target(), self.ws.source(later), self.preimage)
        with self.subTest("both members already exist and are identical: a no-op"):
            result = self.dry(self.ws.target(), self.ws.source(), self.preimage)
            self.assertEqual((result.exit, result.report["status"]), (0, "no_change"))
        with self.subTest("a table whose primary key is not id is never guarded"):
            def two_practical(conn):
                fx.add_new_family(conn)
                fx.insert(conn, "exam_tasks", id=102, exam_id=100, task_number=27, exam_part=2,
                          task_kind="practical_spreadsheet", points=15)
                fx.insert(conn, "practical_tasks", task_id=102, work_environment="spreadsheet")
            result = self.dry(self.ws.target(), self.ws.source(two_practical), self.preimage)
            self.assertEqual((result.exit, result.report["status"]), (0, "planned"), result.out)

    def test_19_differing_existing_values_are_refused_without_a_partial_update(self):
        cases = {
            "text": "UPDATE questions SET prompt = 'Променен' WHERE id = 1",
            "timestamp": "UPDATE exam_tasks SET created_at = '2030-05-05 00:00:00' WHERE id = 1",
            "approval flag": "UPDATE questions SET is_ai_generated = 1 WHERE id = 2",
            "quality score": "UPDATE questions SET quality_score = 0.5 WHERE id = 2",
            "correct flag": "UPDATE multiple_choice_options SET is_correct = 0 WHERE id = 1",
        }
        for label, sql in cases.items():
            with self.subTest(label):
                result = self.refuse(lambda conn, sql=sql: (conn.execute(sql), fx.add_question(conn, 100, number=9)),
                                     codes=["value_differs"])
                self.assertEqual(result.report["tables"]["questions"]["insert"], 1)

    def test_20_one_against_one_point_zero_differs_and_an_affinity_rewrite_stops_the_apply(self):
        target, base_source = self.ws.target(), self.ws.source()
        before = fx.db_snapshot(target)
        with self.subTest("an int against a REAL column value"):
            source = self.ws.root / "real-col"
            shutil.copytree(base_source, source)
            fx.tamper_table(source, "topic_section_assignments",
                            lambda rows: [dict(row, confidence=1) if row["id"] == 1 else row for row in rows])
            self.assertEqual(fx.scalar(target, "SELECT typeof(confidence) FROM topic_section_assignments WHERE id = 1"),
                             "real")
            self.assert_refused(self.dry(target, source, self.preimage), 1, "plan_conflicts", row_codes=["value_differs"])
        with self.subTest("a float against an INTEGER column value"):
            source = self.ws.root / "int-col"
            shutil.copytree(base_source, source)
            fx.tamper_table(source, "questions", lambda rows: [dict(row, points=1.0) if row["id"] == 1 else row
                                                               for row in rows])
            self.assertEqual(fx.scalar(target, "SELECT typeof(points) FROM questions WHERE id = 1"), "integer")
            self.assert_refused(self.dry(target, source, self.preimage), 1, "plan_conflicts", row_codes=["value_differs"])
        with self.subTest("a candidate that column affinity rewrites passes the dry run and fails before COMMIT"):
            source = self.ws.source(lambda conn: fx.insert(conn, "topic_section_assignments", id=100, topic_id=2,
                                                           section_id=1, confidence=0.5))
            self.assertTrue(fx.read_rows(source, "topic_section_assignments")[-1]["id"] == 100)
            fx.tamper_table(source, "topic_section_assignments",
                            lambda rows: [dict(row, confidence=1) if row["id"] == 100 else row for row in rows],
                            keep_sequence=True)
            dry = self.dry(target, source, self.preimage)
            self.assertEqual((dry.exit, dry.report["status"]), (1, "refused"))      # topic 2 is existing
            source2 = self.ws.source(lambda conn: (fx.add_topic(conn, 100, section_id=1),
                                                   fx.insert(conn, "topic_section_assignments", id=100, topic_id=100,
                                                             section_id=1, confidence=0.5)))
            fx.tamper_table(source2, "topic_section_assignments",
                            lambda rows: [dict(row, confidence=1) if row["id"] == 100 else row for row in rows],
                            keep_sequence=True)
            dry = self.dry(target, source2, self.preimage)
            self.assertEqual((dry.exit, dry.report["status"]), (0, "planned"), dry.out)
            result = self.apply(target, source2, self.preimage, dry.report["plan_sha256"])
            self.assert_refused(result, 3, "write_failed", status="not_committed")
            self.assert_untouched(target, before)

    def test_21_deleted_in_target_and_removed_in_source_refuse_the_whole_plan(self):
        with self.subTest("absent in the target but in the preimage"):
            target = self.ws.target(mutate=lambda conn: conn.execute("DELETE FROM topic_aliases"))
            before = fx.db_snapshot(target)
            result = self.dry(target, self.ws.source(lambda conn: fx.add_new_family(conn)), self.preimage)
            self.assert_refused(result, 1, "plan_conflicts", row_codes=["deleted_in_target"])
            self.assert_untouched(target, before)
        with self.subTest("in the preimage but removed from the source"):
            source = self.ws.source(lambda conn: (fx.add_new_family(conn), conn.execute("DELETE FROM topic_aliases")))
            target = self.ws.target()
            before = fx.db_snapshot(target)
            result = self.dry(target, source, self.preimage)
            self.assert_refused(result, 1, "plan_conflicts", row_codes=["removed_in_source"])
            self.assert_untouched(target, before)

    def test_22_target_only_rows_are_kept_and_listed_with_a_bound(self):
        def extras(conn):
            for number in range(30):
                fx.add_question(conn, 300 + number, number=40 + number, prompt=f"Само в целта {number}")
        target = self.ws.target(mutate=extras)
        result = self.dry(target, self.ws.source(), self.preimage)
        self.assertEqual((result.exit, result.report["status"]), (0, "no_change"))
        self.assertEqual(result.report["tables"]["questions"]["target_only"], 30)
        self.assertEqual(len(result.report["target_only_keys"]["questions"]), tool.LIST_LIMIT)
        with self.subTest("a target-only row colliding with a candidate's unique key"):
            target = self.ws.target(mutate=lambda conn: fx.add_question(conn, 300, number=77, source_exam="other"))
            source = self.ws.source(lambda conn: fx.add_question(conn, 400, number=77, source_exam="other"))
            self.assert_refused(self.dry(target, source, self.preimage), 1, "plan_conflicts",
                                row_codes=["unique_key_exists"])


class SourcePreimageSchemaTest(fx.UpdaterTestCase):
    def setUp(self):
        super().setUp()
        self.preimage = self.ws.preimage()
        self.good_source = self.ws.source(lambda conn: fx.add_new_family(conn))
        self.target = self.ws.target()
        self.before = fx.db_snapshot(self.target)

    def variant(self, base, name, edit):
        directory = self.ws.root / f"variant-{name}"
        shutil.copytree(base, directory)
        edit(directory)
        return directory

    def assert_input_refused(self, source=None, preimage=None, code="invalid_source", **kwargs):
        result = self.dry(self.target, source or self.good_source, preimage or self.preimage, **kwargs)
        self.assert_refused(result, 1, code)
        self.assert_untouched(self.target, self.before)
        return result

    def test_23_invalid_sources_are_refused(self):
        def rewrite(table, change):
            def apply(directory):
                path = fx.table_path(directory, table)
                path.write_bytes(change(path.read_bytes()))
            return apply

        def manifest(change):
            return lambda directory: fx.edit_manifest(directory, change)

        def unlink(directory):
            fx.table_path(directory, "topic_classes").unlink()

        def symlink(directory):
            path = fx.table_path(directory, "topic_classes")
            target = directory.parent / "elsewhere-topic-classes.jsonl"
            target.write_bytes(path.read_bytes())
            path.unlink()
            os.symlink(target, path)

        cases = {
            "bad format": manifest(lambda m: m.__setitem__("format", "other")),
            "bad format_version": manifest(lambda m: m.__setitem__("format_version", 2)),
            "bad file sha256": manifest(lambda m: m["tables"]["curricula"].__setitem__("sha256", "0" * 64)),
            "wrong row count": manifest(lambda m: m["tables"]["curricula"].__setitem__("rows", 9)),
            "non-normalized line": rewrite("curricula", lambda b: b.replace(b'"id":1,', b'"id": 1,', 1)),
            "byte order mark": rewrite("curricula", lambda b: b"\xef\xbb\xbf" + b),
            "carriage return": rewrite("curricula", lambda b: b.replace(b"\n", b"\r\n")),
            "missing final newline": rewrite("curricula", lambda b: b[:-1]),
            "duplicate key": rewrite("curricula", lambda b: b + b.splitlines(True)[0]),
            "missing file": unlink,
            "extra file": lambda d: (d / "extra.txt").write_text("x"),
            "extra directory entry": lambda d: (d / "subdir").mkdir(),
            "symlinked file": symlink,
            "classification differs": manifest(lambda m: m["classification"]["canonical"].remove("curricula")),
            "sequence below max id": manifest(lambda m: m["tables"]["questions"].__setitem__("sqlite_sequence", 3)),
            "older migration level": manifest(lambda m: (m["schema"]["migrations"].pop(),
                                                         m["schema"].__setitem__("migration_level",
                                                                                 m["schema"]["migrations"][-1]))),
            "newer migration level": manifest(lambda m: (m["schema"]["migrations"].append("028_future.sql"),
                                                         m["schema"].__setitem__("migration_level", "028_future.sql"))),
            "primary key differs": manifest(lambda m: m["tables"]["curricula"].__setitem__("primary_key", ["grade"])),
            "AUTOINCREMENT declared false": manifest(lambda m: m["tables"]["questions"].__setitem__("autoincrement",
                                                                                                    False)),
        }
        for label, edit in cases.items():
            with self.subTest(label):
                self.assert_input_refused(source=self.variant(self.good_source, re.sub(r"\W", "", label), edit))
        with self.subTest("oversized file"):
            big = self.variant(self.good_source, "big", lambda d: fx.table_path(d, "curricula").write_bytes(b" " * 1_000_001))
            with mock.patch.object(tool, "MAX_FILE_BYTES", 1_000_000):
                self.assert_input_refused(source=big)

    def test_24_invalid_preimages_are_refused_and_an_older_prefix_is_accepted(self):
        def manifest(change):
            return lambda directory: fx.edit_manifest(directory, change)
        cases = {
            "tampered hash": manifest(lambda m: m["tables"]["questions"].__setitem__("sha256", "1" * 64)),
            "different columns": manifest(lambda m: m["tables"]["curricula"]["columns"].reverse()),
            "different table set": manifest(lambda m: (m["canonical_tables"].remove("lessons"),
                                                       m["tables"].pop("lessons"))),
            "newer than the source's list": manifest(lambda m: (m["schema"]["migrations"].append("028_future.sql"),
                                                                m["schema"].__setitem__("migration_level",
                                                                                        "028_future.sql"))),
            "primary key differs": manifest(lambda m: m["tables"]["curricula"].__setitem__("primary_key", ["grade"])),
            "AUTOINCREMENT differs": manifest(lambda m: m["tables"]["questions"].__setitem__("autoincrement", False)),
            "tampered file": lambda d: fx.table_path(d, "curricula").write_bytes(
                fx.table_path(d, "curricula").read_bytes().replace(b"11", b"13", 1)),
        }
        for label, edit in cases.items():
            with self.subTest(label):
                preimage = self.variant(self.preimage, "pre-" + re.sub(r"\W", "", label), edit)
                self.assert_input_refused(preimage=preimage, code="invalid_preimage")
        with self.subTest("an older prefix migration list with an added non-canonical table"):
            def older(manifest_dict):
                manifest_dict["schema"]["migrations"].pop()
                manifest_dict["schema"]["migration_level"] = manifest_dict["schema"]["migrations"][-1]
                manifest_dict["classification"]["runtime"].remove("password_recoveries")
                manifest_dict["runtime_exclusions"].remove("password_recoveries")
            preimage = self.variant(self.preimage, "older", lambda d: fx.edit_manifest(d, older))
            result = self.dry(self.target, self.good_source, preimage)
            self.assertEqual((result.exit, result.report["status"]), (0, "planned"), result.out)
        with self.subTest("a missing preimage directory"):
            self.assert_input_refused(preimage=self.ws.root / "no-such-preimage", code="invalid_preimage")

    def test_25_source_bytes_are_pinned_once_and_the_private_copy_is_rehashed(self):
        reference = self.plan_of(self.target, self.good_source, self.preimage).report
        manifest_sha = fx.file_bytes_digest(self.good_source / ct.MANIFEST_NAME)
        with self.subTest("a mutation after the read changes nothing"):
            source = self.variant(self.good_source, "pinned", lambda d: None)

            def mutate_original():
                fx.table_path(source, "questions").write_bytes(b"garbage\n")
            pinned = self.dry(self.target, source, self.preimage, hooks={"after_pin": mutate_original})
            self.assertEqual((pinned.exit, pinned.report["status"]), (0, "planned"), pinned.out)
            self.assertEqual(pinned.report["source"]["manifest_sha256"], manifest_sha)
            self.assertEqual(pinned.report["plan_sha256"], reference["plan_sha256"])
        created = []
        real = tempfile.mkdtemp

        def recording(*args, **kwargs):
            directory = real(*args, **kwargs)
            created.append(directory)
            return directory

        def tamper_copy():
            copy = next(Path(d) / "source" / "questions.jsonl" for d in created
                        if Path(d).name.startswith("learnpilot-updater-") and (Path(d) / "source").exists())
            copy.write_bytes(copy.read_bytes() + b" ")
        token = reference["plan_sha256"]
        with mock.patch.object(tempfile, "mkdtemp", recording):
            with self.subTest("tampering before the first canonical write"):
                result = self.apply(self.target, self.good_source, self.preimage, token,
                                    hooks={"after_lock": lambda conn: tamper_copy()})
                self.assert_refused(result, 1, "pinned_copy_changed")
                self.assert_untouched(self.target, self.before)
            with self.subTest("tampering before COMMIT"):
                result = self.apply(self.target, self.good_source, self.preimage, token,
                                    hooks={"before_commit": lambda conn: tamper_copy()})
                self.assert_refused(result, 3, "pinned_copy_changed", status="not_committed")
                self.assert_untouched(self.target, self.before)

    REDIRECTS = ("absolute", "relative", "other_table", "missing", "entry_is_string", "entry_is_null",
                 "file_is_number", "file_is_list", "file_absent", "tables_is_list")

    def redirected_dry_run(self, kind: str, how: str, table: str):
        """Dry run with a source (or preimage) manifest edited by ``how``; returns what was opened or called."""
        outside = self.ws.root / f"outside-{kind}-{how}"
        base = self.good_source if kind == "source" else self.preimage
        variant = self.variant(base, f"{kind}-{how}", lambda d: None)
        external = fx.redirect_table_file(variant, table, how, outside)
        spy = fx.OpenSpy()
        self.spy.clear()
        watched = {"source": (export, "check_directory"), "preimage": (ct, "file_digest")}[kind]
        with mock.patch.object(*watched, wraps=getattr(*watched)) as helper:
            kwargs = ({"source": variant, "preimage": self.preimage} if kind == "source"
                      else {"source": self.good_source, "preimage": variant})
            try:
                result = self.dry(self.target, kwargs["source"], kwargs["preimage"], hooks={"after_pin": spy.start})
            finally:
                spy.stop()
        if external is not None:
            self.assertTrue(external.is_file())                              # the outside file stays a valid JSONL file
        self.assertTrue(any("learnpilot-updater-" in path for path in spy.paths), "the open spy saw no pinned read")
        self.assertEqual(spy.opened_under(outside), [], f"{how}: an outside file was read after the pin")
        self.assertEqual([path for path in self.spy.calls if path["path"] == os.path.realpath(self.target)], [],
                         f"{how}: the target was opened")
        return result, helper, outside

    def test_23b_a_source_manifest_may_name_only_its_own_pinned_table_file(self):
        for how in self.REDIRECTS:
            with self.subTest(how):
                result, helper, _outside = self.redirected_dry_run("source", how, "questions")
                self.assert_refused(result, 1, "invalid_source")
                self.assert_untouched(self.target, self.before)
                self.assertEqual(helper.call_count, 0, "canonical_export.check_directory ran before the file gate")

    def test_24b_a_preimage_manifest_may_name_only_its_own_pinned_table_file(self):
        for how in self.REDIRECTS:
            with self.subTest(how):
                result, helper, outside = self.redirected_dry_run("preimage", how, "curricula")
                self.assert_refused(result, 1, "invalid_preimage")
                self.assert_untouched(self.target, self.before)
                digested = [os.path.realpath(call.args[0]) for call in helper.call_args_list]
                self.assertEqual([path for path in digested
                                  if os.path.basename(os.path.dirname(path)) == "preimage"
                                  or path.startswith(os.path.realpath(outside) + os.sep)], [],
                                 "file_digest followed a preimage manifest entry")

    def test_25b_nothing_outside_the_private_copy_is_read_after_the_pin(self):
        with self.subTest("the open spy is live: it sees all the ways of reading a file"):
            probe = self.ws.path("spied.txt")
            probe.write_bytes(b"x")
            spy = fx.OpenSpy()
            spy.start()
            try:
                probe.read_bytes()
                with open(probe, "rb"):
                    pass
                os.close(os.open(probe, os.O_RDONLY))
            finally:
                spy.stop()
            self.assertEqual(len(spy.opened_named("spied.txt")), 3)
        with self.subTest("a normally pinned run reads only the private copy; the originals may change freely"):
            source = self.variant(self.good_source, "spied-source", lambda d: None)
            preimage = self.variant(self.preimage, "spied-preimage", lambda d: None)
            reference = self.plan_of(self.target, self.good_source, self.preimage).report["plan_sha256"]
            spy = fx.OpenSpy()

            def after_pin():
                fx.table_path(source, "questions").write_bytes(b"garbage\n")
                fx.table_path(preimage, "curricula").write_bytes(b"garbage\n")
                spy.start()                                              # only the tool's own later reads count
            try:
                result = self.dry(self.target, source, preimage, hooks={"after_pin": after_pin})
            finally:
                spy.stop()
            self.assertEqual((result.exit, result.report["status"]), (0, "planned"), result.out)
            self.assertEqual(result.report["plan_sha256"], reference)
            self.assertTrue(any("learnpilot-updater-" in path for path in spy.paths))
            self.assertEqual(spy.opened_under(source) + spy.opened_under(preimage), [])

    def test_26_incompatible_or_unclean_targets_are_refused(self):
        statements_by_case = {
            "pending migration": "DELETE FROM schema_migrations WHERE filename = '027_dzi_part1_runtime.sql'",
            "previous migration unrecorded under the latest one": "DELETE FROM schema_migrations "
                                                                  "WHERE filename = '026_practice_runtime.sql'",
            "earlier migration unrecorded under a later one": "DELETE FROM schema_migrations "
                                                              "WHERE filename = '025_password_recoveries.sql'",
            "practice open-scope index missing": "DROP INDEX uniq_practice_sessions_open_scope",
            "practice table missing": "DROP TABLE practice_answers",
            "part 1 open-attempt index missing": "DROP INDEX uniq_dzi_part1_attempts_open_user",
            "part 1 history index missing": "DROP INDEX idx_dzi_part1_attempts_user_started",
            "part 1 table missing": "DROP TABLE dzi_part1_answers",
            "unknown applied filename": "INSERT INTO schema_migrations (filename) VALUES ('999_unknown.sql')",
            "middle migration missing": "DELETE FROM schema_migrations WHERE filename = '010_topic_section_display_order.sql'",
            "extra column": "ALTER TABLE questions ADD COLUMN extra_column TEXT",
            "missing index": "DROP INDEX idx_questions_type",
            "extra unclassified table": "CREATE TABLE extra_unclassified (a)",
            "classified table missing": "DROP TABLE scrape_log",
            "no-op trigger": "CREATE TRIGGER noop_trigger AFTER INSERT ON scrape_log BEGIN SELECT 1; END",
            "view": "CREATE VIEW fixture_view AS SELECT 1 AS one",
        }
        for label, sql in statements_by_case.items():
            with self.subTest(label):
                target = self.ws.target("incompat-" + re.sub(r"\W", "", label) + ".db",
                                        mutate=lambda conn, sql=sql: conn.execute(sql))
                before = fx.db_snapshot(target)
                self.assert_refused(self.dry(target, self.good_source, self.preimage), 1, "incompatible_target")
                self.assert_untouched(target, before)
        with self.subTest("changed CHECK"):
            def recreate(conn):
                conn.execute("DROP TABLE login_throttle")
                conn.execute("CREATE TABLE login_throttle (identifier_hash TEXT PRIMARY KEY CHECK (length(identifier_hash) = 64), "
                             "failure_count INTEGER NOT NULL DEFAULT 0 CHECK (failure_count >= 1), "
                             "window_started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, blocked_until TEXT) WITHOUT ROWID")
            target = self.ws.target("check.db", mutate=recreate)
            self.assert_refused(self.dry(target, self.good_source, self.preimage), 1, "incompatible_target")
        with self.subTest("quick_check failure"):
            def rewrite(sql):
                return "SELECT 'row 1 missing from index fixture'" if sql == "PRAGMA quick_check" else None
            connect = fx.FactoryConnect([fx.make_connection_class([], rewrite=rewrite)])
            self.assert_refused(self.dry(self.target, self.good_source, self.preimage, connect=connect), 1,
                                "incompatible_target")
        with self.subTest("a pre-existing foreign-key violation"):
            def orphan(conn):
                conn.execute("INSERT INTO multiple_choice_options (id, question_id, option_letter, option_text) "
                             "VALUES (900, 999, 'A', 'orphan')")
            target = self.ws.target("orphan.db", mutate=orphan, foreign_keys=False)
            result = self.dry(target, self.good_source, self.preimage)
            self.assert_refused(result, 1, "plan_conflicts", row_codes=["preexisting_fk_violation"])
        with self.subTest("a differing applied_at alone does not change the plan hash"):
            later = self.ws.target("applied-at.db", mutate=lambda conn: conn.execute(
                "UPDATE schema_migrations SET applied_at = '2001-01-01 00:00:00'"))
            one = self.dry(self.target, self.good_source, self.preimage)
            two = self.dry(later, self.good_source, self.preimage)
            self.assertEqual(one.report["plan_sha256"], two.report["plan_sha256"])

    def test_27_migrations_are_pinned_and_never_run_on_the_target(self):
        applied_to = []
        real_apply = manage_migrations.apply_migrations

        def recording(db_path, *args, **kwargs):
            applied_to.append(Path(db_path))
            return real_apply(db_path, *args, **kwargs)
        created = []
        real_mkdtemp = tempfile.mkdtemp

        def mkdtemp(*args, **kwargs):
            directory = real_mkdtemp(*args, **kwargs)
            created.append(directory)
            return directory
        self.spy.clear()
        with mock.patch.object(manage_migrations, "apply_migrations", recording), \
                mock.patch.object(tempfile, "mkdtemp", mkdtemp):
            result = self.apply(self.target, self.good_source, self.preimage,
                                self.plan_of(self.target, self.good_source, self.preimage).report["plan_sha256"])
        self.assertEqual(result.exit, 0)
        resolved = os.path.realpath(self.target)
        self.assertTrue(applied_to)
        for path in applied_to:
            self.assertNotEqual(os.path.realpath(path), resolved)
            self.assertTrue(any(str(path).startswith(directory) for directory in created), path)
        target_connects = [call for call in self.spy.calls if call["path"] == resolved]
        self.assertTrue(target_connects)                                  # positive control: the spy sees the target
        for call in self.spy.calls:
            if call["path"] == resolved:
                self.assertTrue(call["raw"].startswith("file:") and call["raw"].endswith("?mode=rw"), call)
            else:
                self.assertTrue(any(call["path"].startswith(directory) for directory in created), call)
            self.assertNotEqual(call["path"], str(RUNTIME_DB))
        with self.subTest("a migration list that differs from the manifest list is refused"):
            directory = fx.fixture_migrations(self.ws, {LATE_MIGRATION: "SELECT 1;\n"}, name="late-migrations")
            self.assert_refused(self.dry(self.target, self.good_source, self.preimage, migrations_dir=directory), 1,
                                "invalid_source")
        with self.subTest("two runs with different pinned migration sets have independent reference schemas"):
            late = fx.fixture_migrations(self.ws, {LATE_MIGRATION: "UPDATE schema_migrations SET filename = filename;\n"},
                                         name="late-two")
            base = fx.custom_base(self.ws, late, "late-base.db")
            source = fx.export_database_dir(self.ws, base, "late-source", late)
            late_target = self.ws.target("late-target.db", template=base)
            first = self.dry(late_target, source, source, migrations_dir=late)
            self.assertEqual((first.exit, first.report["status"]), (0, "no_change"), first.out)
            second = self.dry(self.target, self.good_source, self.preimage)
            self.assertEqual((second.exit, second.report["status"]), (0, "no_change"))
            mixed = self.dry(late_target, source, source)
            self.assertEqual(mixed.exit, 1)                                   # not served from a stale cache

    def test_27a_migration_file_bytes_are_bound_into_the_plan_hash(self):
        baseline = self.plan_of(self.target, self.good_source, self.preimage).report

        def migrations(name, edit=None):
            directory = fx.fixture_migrations(self.ws, {}, name=name)
            if edit is not None:
                edit(directory)
            return directory
        same_names = sorted(path.name for path in ct.MIGRATIONS_DIR.glob("*.sql"))
        inert = migrations("inert-migrations", lambda d: (d / same_names[4]).write_bytes(
            (d / same_names[4]).read_bytes() + b"\n-- a shape-inert comment\n"))
        copy = migrations("unchanged-migrations")
        with self.subTest("an unchanged copy keeps the plan hash and the identity"):
            result = self.plan_of(self.target, self.good_source, self.preimage, migrations_dir=copy).report
            self.assertEqual(result["plan_sha256"], baseline["plan_sha256"])
            self.assertEqual(result["repository_migrations_sha256"], baseline["repository_migrations_sha256"])
        with self.subTest("one inert byte change with unchanged filenames changes the plan hash"):
            self.assertEqual(sorted(path.name for path in inert.glob("*.sql")), same_names)
            changed = self.plan_of(self.target, self.good_source, self.preimage, migrations_dir=inert).report
            self.assertNotEqual(changed["repository_migrations_sha256"], baseline["repository_migrations_sha256"])
            self.assertNotEqual(changed["plan_sha256"], baseline["plan_sha256"])
            self.assertEqual(changed["tables"], baseline["tables"])           # only the bound identity differs
        with self.subTest("the old token is stale and nothing is written"):
            result = self.apply(self.target, self.good_source, self.preimage, baseline["plan_sha256"],
                                migrations_dir=inert)
            self.assert_refused(result, 2, "stale_plan")
            self.assert_untouched(self.target, self.before)
        with self.subTest("a token of the same pinned bytes applies (both phases bind one identity)"):
            fresh = self.ws.target("inert-target.db")
            token = self.plan_of(fresh, self.good_source, self.preimage, migrations_dir=inert).report["plan_sha256"]
            result = self.apply(fresh, self.good_source, self.preimage, token, migrations_dir=inert)
            self.assertEqual((result.exit, result.report["status"]), (0, "applied"), result.out)

    def test_28_a_missing_or_unsuitable_target_is_refused_before_any_sqlite_call(self):
        directory = self.ws.path("a-directory")
        directory.mkdir()
        dangling = self.ws.path("dangling.db")
        os.symlink(self.ws.path("nowhere.db"), dangling)
        link_to_valid = self.ws.path("link.db")
        os.symlink(self.target, link_to_valid)
        for label, candidate in (("absent", self.ws.path("absent.db")), ("a directory", directory),
                                 ("a dangling symlink", dangling), ("a symlink to a valid database", link_to_valid)):
            with self.subTest(label):
                listing, digest = fx.tree_listing(self.ws.root), fx.file_bytes_digest(self.target)
                self.spy.clear()
                result = self.dry(candidate, self.good_source, self.preimage)
                self.assert_refused(result, 1, "target_not_regular_file")
                self.assertEqual(fx.tree_listing(self.ws.root), listing)
                self.assertEqual(fx.file_bytes_digest(self.target), digest)
                touched = {os.path.realpath(self.target), str(self.target), os.path.realpath(candidate), str(candidate)}
                self.assertEqual([call for call in self.spy.calls if call["path"] in touched], [])
        with self.subTest("a symlinked parent directory is accepted and the resolved path is opened"):
            real_directory = self.ws.path("real-dir")
            real_directory.mkdir()
            moved = real_directory / "t.db"
            shutil.copyfile(self.target, moved)
            os.symlink(real_directory, self.ws.path("linked-dir"))
            self.spy.clear()
            result = self.dry(self.ws.path("linked-dir") / "t.db", self.good_source, self.preimage)
            self.assertEqual(result.exit, 0, result.out)
            opened = [call["path"] for call in self.spy.uri_targets() if call["path"] == os.path.realpath(moved)]
            self.assertEqual(opened, [os.path.realpath(moved)])
            self.assertNotIn(str(self.ws.path("linked-dir") / "t.db"), [call["path"] for call in self.spy.calls])

    def test_29_source_and_preimage_arguments_are_validated_and_never_modified(self):
        def tree_digest(directory):
            return {name: fx.file_bytes_digest(Path(directory) / name) for name in fx.tree_listing(directory)}
        digest = tree_digest(self.good_source)
        a_file = self.ws.path("a-file")
        a_file.write_text("x")
        link = self.ws.path("source-link")
        os.symlink(self.good_source, link)
        for label, value in (("missing", self.ws.path("nope")), ("not a directory", a_file), ("a symlink", link)):
            with self.subTest("source " + label):
                self.assert_refused(self.dry(self.target, value, self.preimage), 1, "invalid_source")
            with self.subTest("preimage " + label):
                self.assert_refused(self.dry(self.target, self.good_source, value), 1, "invalid_preimage")
        self.assertEqual(tree_digest(self.good_source), digest)
        self.assert_untouched(self.target, self.before)


class PreservationTest(fx.UpdaterTestCase):
    def setUp(self):
        super().setUp()
        self.preimage = self.ws.preimage()

    def noncanonical(self, snapshot):
        canonical = set(ct.tables_of(ct.CANONICAL)) | {"sqlite_sequence", "__master__"}
        return {name: rows for name, rows in snapshot.items() if name not in canonical}

    def test_30_a_populated_target_keeps_every_non_canonical_row(self):
        target = self.ws.target(runtime=True)
        source = self.ws.source(lambda conn: fx.add_new_family(conn))
        before = fx.db_snapshot(target)
        token = self.plan_of(target, source, self.preimage).report["plan_sha256"]
        result = self.apply(target, source, self.preimage, token)
        self.assertEqual((result.exit, result.report["preservation"]["equal"]), (0, True), result.out)
        after = fx.db_snapshot(target)
        self.assertEqual(self.noncanonical(after), self.noncanonical(before))
        self.assertEqual(len(self.noncanonical(before)), len(tool.NONCANONICAL_TABLES))
        self.assertEqual(after["__master__"], before["__master__"])
        populated = [name for name in self.noncanonical(before) if self.noncanonical(before)[name]]
        self.assertGreaterEqual(len(populated), 20)

    PRACTICE = ("practice_sessions", "practice_answers")

    def practice_state(self, target):
        """Storage-class-exact rows of both practice tables and the sessions' AUTOINCREMENT mark."""
        snapshot = fx.db_snapshot(target)
        return {table: snapshot[table] for table in self.PRACTICE}, fx.sequence_map(target).get("practice_sessions")

    def test_30a_practice_rows_and_their_sequence_survive_a_genuine_update_and_a_no_op(self):
        self.assertTrue(set(self.PRACTICE) <= set(tool.NONCANONICAL_TABLES))        # digested, not canonical
        target = self.ws.target(runtime=True, mutate=lambda conn: conn.execute(
            "UPDATE sqlite_sequence SET seq = 40 WHERE name = 'practice_sessions'"))
        before = self.practice_state(target)
        self.assertEqual([len(rows) for rows in before[0].values()], [3, 2])
        self.assertEqual(before[1], 40)                                              # above MAX(id): a real high-water mark
        source = self.ws.source(lambda conn: fx.add_new_family(conn))
        token = self.plan_of(target, source, self.preimage).report["plan_sha256"]
        result = self.apply(target, source, self.preimage, token)
        self.assertEqual((result.exit, result.report["status"], result.report["preservation"]["equal"]),
                         (0, "applied", True), result.out)
        self.assertGreater(fx.scalar(target, "SELECT COUNT(*) FROM questions WHERE id >= 100"), 0)   # it did write
        self.assertEqual(self.practice_state(target), before)
        noop = self.dry(target, source, self.preimage)
        self.assertEqual((noop.exit, noop.report["status"]), (0, "no_change"), noop.out)
        self.assertEqual(self.practice_state(target), before)

    PART1 = ("dzi_part1_attempts", "dzi_part1_answers")

    def part1_state(self, target):
        """Storage-class-exact rows of both Part 1 tables and the attempts' AUTOINCREMENT mark."""
        snapshot = fx.db_snapshot(target)
        return {table: snapshot[table] for table in self.PART1}, fx.sequence_map(target).get("dzi_part1_attempts")

    def test_30b_part1_rows_and_their_sequence_survive_a_genuine_update_and_a_no_op(self):
        self.assertTrue(set(self.PART1) <= set(tool.NONCANONICAL_TABLES))            # digested, not canonical
        target = self.ws.target(runtime=True, mutate=lambda conn: conn.execute(
            "UPDATE sqlite_sequence SET seq = 40 WHERE name = 'dzi_part1_attempts'"))
        before = self.part1_state(target)
        self.assertEqual([len(rows) for rows in before[0].values()], [4, 3])
        self.assertEqual(before[1], 40)                                              # above MAX(id): a real high-water mark
        source = self.ws.source(lambda conn: fx.add_new_family(conn))
        token = self.plan_of(target, source, self.preimage).report["plan_sha256"]
        result = self.apply(target, source, self.preimage, token)
        self.assertEqual((result.exit, result.report["status"], result.report["preservation"]["equal"]),
                         (0, "applied", True), result.out)
        self.assertGreater(fx.scalar(target, "SELECT COUNT(*) FROM questions WHERE id >= 100"), 0)   # it did write
        self.assertEqual(self.part1_state(target), before)
        noop = self.dry(target, source, self.preimage)
        self.assertEqual((noop.exit, noop.report["status"]), (0, "no_change"), noop.out)
        self.assertEqual(self.part1_state(target), before)

    def test_31_the_preservation_digest_is_storage_class_exact_and_order_independent(self):
        with closing(sqlite3.connect(":memory:")) as conn:
            def digest(rows, *, without_rowid=False):
                conn.execute("DROP TABLE IF EXISTS s")
                conn.execute("CREATE TABLE s (k, v)" if not without_rowid else "CREATE TABLE s (k PRIMARY KEY, v) WITHOUT ROWID")
                conn.executemany("INSERT INTO s VALUES (?, ?)", [(index, value) for index, value in enumerate(rows)])
                return tool.table_multiset(conn, "s")
            classes = {"int": 1, "real": 1.0, "text": "1", "blob": b"1", "null": None, "empty": "",
                       "invalid utf-8 text": None, "negative zero": -0.0, "zero": 0.0}
            digests = {name: digest([value]) for name, value in classes.items() if name != "invalid utf-8 text"}
            conn.execute("DROP TABLE IF EXISTS s")
            conn.execute("CREATE TABLE s (k, v)")
            conn.execute("INSERT INTO s VALUES (0, CAST(x'ff61' AS TEXT))")
            digests["invalid utf-8 text"] = tool.table_multiset(conn, "s")
            stored_sign = {name: math.copysign(1, conn.execute("SELECT ?", (value,)).fetchone()[0])
                           for name, value in (("negative zero", -0.0), ("zero", 0.0))}
            self.assertEqual(conn.execute("SELECT typeof(?)", (1.0,)).fetchone()[0], "real")
            for left in digests:
                for right in digests:
                    if left == right or {left, right} == {"negative zero", "zero"}:
                        continue
                    self.assertNotEqual(digests[left], digests[right], (left, right))
            self.assertEqual(digests["negative zero"] != digests["zero"],
                             stored_sign["negative zero"] != stored_sign["zero"])
            def plain(rows):
                conn.execute("DROP TABLE IF EXISTS s")
                conn.execute("CREATE TABLE s (v)")
                conn.executemany("INSERT INTO s VALUES (?)", [(value,) for value in rows])
                return tool.table_multiset(conn, "s")
            self.assertEqual(plain([1, "a", None, b"x", 1.5]), plain([1.5, b"x", None, "a", 1]))
            self.assertNotEqual(plain([1, 1]), plain([1]))
            self.assertEqual(digest([1, "a"], without_rowid=True), digest([1, "a"], without_rowid=True))
            self.assertNotEqual(digest([1, 1]), digest([1]))                 # duplicate identical rows count
            self.assertEqual(conn.text_factory, str)

    def test_31_storage_classes_in_a_populated_target_survive_and_mutations_are_caught(self):
        def odd_values(conn):
            conn.execute("UPDATE scrape_log SET notes = x'616263' WHERE id = 1")                  # blob in a TEXT column
            conn.execute("UPDATE obsidian_notes SET body_text = CAST(x'ff61' AS TEXT), title = '' WHERE id = 1")
            conn.execute("UPDATE quiz_text_answers SET points_awarded = -0.0 WHERE id = 1")
            conn.execute("INSERT INTO generated_exams (id, exam_name, question_ids) VALUES (2, NULL, '[1]')")
            conn.execute("INSERT INTO generated_exams (id, exam_name, question_ids) VALUES (3, '', '[1]')")
        source = self.ws.source(lambda conn: fx.add_new_family(conn))
        probe = self.ws.target("odd.db", runtime=True, mutate=odd_values)
        self.assertEqual(fx.scalar(probe, "SELECT typeof(notes) FROM scrape_log WHERE id = 1"), "blob")
        token = self.plan_of(probe, source, self.preimage).report["plan_sha256"]
        seen = {}
        result = self.apply(probe, source, self.preimage, token, hooks={
            "before_commit": lambda conn: seen.__setitem__("factory", conn.text_factory)})
        self.assertEqual(result.exit, 0, result.out)
        self.assertIs(seen["factory"], str)
        self.assertEqual(fx.scalar(probe, "SELECT typeof(notes) FROM scrape_log WHERE id = 1"), "blob")
        mutations = {
            "blob to the same bytes as text": "UPDATE scrape_log SET notes = CAST(notes AS TEXT) WHERE id = 1",
            "invalid utf-8 byte changed": "UPDATE obsidian_notes SET body_text = CAST(x'fe61' AS TEXT) WHERE id = 1",
            "empty string to NULL": "UPDATE obsidian_notes SET title = NULL WHERE id = 1",
            "integer changed in a WITHOUT ROWID table": "UPDATE login_throttle SET failure_count = failure_count + 1",
        }
        for label, sql in mutations.items():
            with self.subTest(label):
                target = self.ws.target(f"mut-{abs(hash(label))}.db", runtime=True, mutate=odd_values)
                token = self.plan_of(target, source, self.preimage).report["plan_sha256"]
                before = fx.db_snapshot(target)

                def corrupt(conn, sql=sql):
                    conn.set_authorizer(None)
                    conn.execute(sql)
                result = self.apply(target, source, self.preimage, token, hooks={"before_commit": corrupt})
                self.assert_refused(result, 3, "write_failed", status="not_committed")
                self.assertIs(result.report["preservation"]["equal"], False)
                self.assert_untouched(target, before)

    def test_32_injected_corruption_is_detected_and_rolled_back(self):
        source = self.ws.source(lambda conn: fx.add_new_family(conn))
        cases = {
            "a runtime row updated": ("UPDATE users SET display_name = 'changed' WHERE id = 2", False),
            "a runtime row deleted": ("DELETE FROM scrape_log", False),
            "a runtime row added": ("INSERT INTO login_throttle (identifier_hash, failure_count) VALUES ('" + "6" * 64 + "', 1)", False),
            "a practice session updated": ("UPDATE practice_sessions SET seed = 'changed' WHERE id = 1", False),
            "a practice answer deleted": ("DELETE FROM practice_answers WHERE question_id = 1", False),
            "a practice session added": ("INSERT INTO practice_sessions (user_id, section_id, seed, question_ids_json, "
                                         "started_at) VALUES (3, 2, 'x', '[2]', '2026-01-01 00:00:00')", None),   # also moves the sequence, checked first
            "a practice sequence raised": ("UPDATE sqlite_sequence SET seq = 99 WHERE name = 'practice_sessions'", None),
            "a part 1 attempt updated": ("UPDATE dzi_part1_attempts SET seed = 'changed' WHERE id = 1", False),
            "a part 1 answer deleted": ("DELETE FROM dzi_part1_answers WHERE slot_number = 1", False),
            "a part 1 answer raw text changed": ("UPDATE dzi_part1_answers SET raw_answer = 'changed' "
                                                 "WHERE slot_number = 2 AND subquestion_number = 1", False),
            "a part 1 attempt added": ("INSERT INTO dzi_part1_attempts (user_id, blueprint_id, seed, question_ids_json, "
                                       "slot_format_json, max_points_planned, duration_seconds, started_at, deadline_at, "
                                       "submit_cutoff_at) VALUES (1, 1, 'x', '[2]', '[]', 1, 60, '2026-01-02 00:00:00', "
                                       "'2026-01-02 00:01:00', '2026-01-02 00:02:00')", None),   # also moves the sequence, checked first
            "a part 1 sequence raised": ("UPDATE sqlite_sequence SET seq = 99 WHERE name = 'dzi_part1_attempts'", None),
            "a runtime sequence entry added": ("INSERT INTO sqlite_sequence (name, seq) VALUES ('ghost', 5)", None),
            "a canonical row changed": ("UPDATE questions SET prompt = 'changed' WHERE id = 1", None),
            "a canonical sequence lowered": ("UPDATE sqlite_sequence SET seq = 1 WHERE name = 'questions'", None),
        }
        for index, (label, (sql, runtime)) in enumerate(cases.items()):
            with self.subTest(label):
                target = self.ws.target(f"corrupt-{index}.db", runtime=True)
                token = self.plan_of(target, source, self.preimage).report["plan_sha256"]
                before = fx.db_snapshot(target)

                def corrupt(conn, sql=sql):
                    conn.set_authorizer(None)
                    conn.execute(sql)
                result = self.apply(target, source, self.preimage, token, hooks={"before_commit": corrupt})
                self.assert_refused(result, 3, "write_failed", status="not_committed")
                if runtime is False:
                    self.assertIs(result.report["preservation"]["equal"], False)
                self.assert_untouched(target, before)

    def test_33_validation_uses_the_union_of_target_and_source(self):
        def extras(conn):
            fx.add_question(conn, 50, number=60, prompt="Само в целта")
            fx.insert(conn, "assets", id=60, asset_type="image", local_path="web/static/img/questions/dzi/only.png")
            fx.raise_sequence(conn, "lessons", 40)
        target = self.ws.target(mutate=extras)
        source = self.ws.source(lambda conn: fx.add_new_family(conn, base=300))
        dry = self.plan_of(target, source, self.preimage)
        self.assertEqual(dry.report["tables"]["questions"]["target_only"], 1)
        result = self.apply(target, source, self.preimage, dry.report["plan_sha256"])
        self.assertEqual(result.exit, 0, result.out)
        self.assertEqual(fx.scalar(target, "SELECT COUNT(*) FROM questions WHERE id IN (50, 300)"), 2)
        self.assertEqual(fx.sequence_map(target)["lessons"], 40)             # higher than the source's: untouched
        self.assertEqual(fx.sequence_map(target)["questions"], 300)

    def test_34_the_authorizer_guards_are_live_in_both_phases(self):
        target = self.ws.target(runtime=True)
        source = self.ws.source(lambda conn: fx.add_new_family(conn))
        token = self.plan_of(target, source, self.preimage).report["plan_sha256"]
        outcomes = {}

        def attempt(label, conn, sql):
            try:
                conn.execute(sql).fetchall()
                outcomes[label] = "allowed"
            except sqlite3.DatabaseError as exc:
                outcomes[label] = "denied" if "not authorized" in str(exc) else f"other: {exc}"

        def read_phase(conn):
            for label, sql in (("insert", "INSERT INTO scrape_log (source, url, status) VALUES ('a', 'b', 'c')"),
                               ("update", "UPDATE users SET display_name = 'x'"),
                               ("delete", "DELETE FROM scrape_log"),
                               ("journal_mode", "PRAGMA journal_mode=WAL"),
                               ("create", "CREATE TABLE sneaky (a)"),
                               ("seq insert", "INSERT INTO sqlite_sequence (name, seq) VALUES ('z', 1)")):
                attempt("read " + label, conn, sql)
            for label, sql in (("select", "SELECT COUNT(*) FROM users"), ("table_info", "PRAGMA table_info(users)"),
                               ("journal read", "PRAGMA journal_mode"), ("fk", "PRAGMA foreign_key_check")):
                attempt("read " + label, conn, sql)

        def write_phase_denied(conn):
            for label, sql in (("update users", "UPDATE users SET display_name = 'x'"),
                               ("insert users", "INSERT INTO users (email, display_name, session_key) "
                                                "VALUES ('z@example.test', 'zz', '" + "k" * 40 + "')"),
                               ("delete", "DELETE FROM scrape_log"), ("drop", "DROP TABLE scrape_log"),
                               ("create", "CREATE TABLE sneaky (a)"), ("attach", "ATTACH ':memory:' AS other"),
                               ("update canonical", "UPDATE questions SET prompt = 'x'")):
                attempt("write " + label, conn, sql)

        def write_phase_allowed(table):
            if "write insert canonical" in outcomes:
                return
            conn = held["conn"]
            for label, sql in (("insert canonical", "INSERT INTO curricula (id, grade, subject, education_type, title_bg) "
                                                    "VALUES (900, 9, 'S', 'E', 'T')"),
                               ("insert sequence", "INSERT INTO sqlite_sequence (name, seq) VALUES ('probe_a', 1)"),
                               ("update sequence", "UPDATE sqlite_sequence SET seq = seq WHERE name = 'probe_a'")):
                attempt("write " + label, conn, sql)
        held = {}

        def after_lock(conn):
            held["conn"] = conn
            write_phase_denied(conn)
        log = []
        connect = fx.FactoryConnect([fx.make_connection_class(log)])
        before = fx.db_snapshot(target)
        result = self.apply(target, source, self.preimage, token, connect=connect,
                            hooks={"after_read_phase": read_phase, "after_lock": after_lock,
                                   "after_insert": write_phase_allowed})
        for label in ("insert", "update", "delete", "journal_mode", "create", "seq insert"):
            self.assertEqual(outcomes["read " + label], "denied", label)
        for label in ("select", "table_info", "journal read", "fk"):
            self.assertEqual(outcomes["read " + label], "allowed", label)
        for label in ("update users", "insert users", "delete", "drop", "create", "attach", "update canonical"):
            self.assertEqual(outcomes["write " + label], "denied", label)
        for label in ("insert canonical", "insert sequence", "update sequence"):
            self.assertEqual(outcomes["write " + label], "allowed", label)
        # the permitted probes changed the database inside the lock, so the validation refuses the apply
        self.assertEqual((result.exit, result.report["run_code"]), (3, "write_failed"), result.out)
        self.assert_untouched(target, before)
        # transition order: read ROLLBACK, authorizer removed, query_only off, write authorizer, BEGIN IMMEDIATE
        marks = [entry for entry in log if entry in ("ROLLBACK", "PRAGMA query_only=OFF", "BEGIN IMMEDIATE")
                 or (isinstance(entry, tuple) and entry[0] == "authorizer")]
        position = marks.index("BEGIN IMMEDIATE")
        self.assertEqual(marks[position - 4:position + 1],
                         ["ROLLBACK", ("authorizer", "_no_authorizer"), "PRAGMA query_only=OFF",
                          ("authorizer", "_write_authorizer"), "BEGIN IMMEDIATE"])

    def test_34a_a_python_3_10_style_set_authorizer_none_cannot_break_the_phase_transition(self):
        # Before CPython 3.11, set_authorizer(None) installs a deny-all callback instead of removing the
        # authorizer. The emulating connection reaches the tool through the unchanged connect seam.
        with self.subTest("the emulation is not vacuous: None denies everything, as on 3.9 and 3.10"):
            path = self.ws.path("legacy-probe.db")
            with closing(sqlite3.connect(str(path), factory=fx.LegacyAuthorizerConnection,
                                         isolation_level=None)) as probe:
                probe.execute("PRAGMA query_only=ON")
                probe.set_authorizer(None)
                with self.assertRaises(sqlite3.DatabaseError) as raised:
                    probe.execute("PRAGMA query_only=OFF")
                self.assertIn("not authorized", str(raised.exception))
        target = self.ws.target(runtime=True)
        source = self.ws.source(lambda conn: fx.add_new_family(conn))
        token = self.plan_of(target, source, self.preimage).report["plan_sha256"]
        before = fx.db_snapshot(target)
        connect = fx.FactoryConnect([fx.LegacyAuthorizerConnection])
        result = self.apply(target, source, self.preimage, token, connect=connect)
        self.assertEqual((result.exit, result.report["status"], result.report["run_code"]), (0, "applied", None),
                         result.out)
        self.assertEqual(len(connect.calls), 1)                         # the legacy class was the working connection
        self.assertEqual(fx.scalar(target, "SELECT COUNT(*) FROM questions WHERE id = 100"), 1)
        self.assertNotEqual(fx.db_snapshot(target), before)


class ReportTest(fx.UpdaterTestCase):
    def setUp(self):
        super().setUp()
        self.preimage = self.ws.preimage()

    def test_49_conflicts_are_bounded_while_totals_stay_exact_and_ordered(self):
        def many(conn, prompt):
            conn.executemany("INSERT INTO questions (id, source_exam, source_number, question_type, points, prompt, "
                             "created_at) VALUES (?, 'bulk', ?, 'multiple_choice', 1, ?, ?)",
                             [(1000 + n, 1000 + n, f"{prompt} {n}", fx.FIXED_TS) for n in range(500)])
        target = self.ws.target(mutate=lambda conn: many(conn, "target"))
        source = self.ws.source(lambda conn: (many(conn, "source"), fx.add_new_family(conn, base=2000)))
        one = self.dry(target, source, self.preimage)
        two = self.dry(target, source, self.preimage)
        self.assertEqual(one.out, two.out)
        conflicts = one.report["conflicts"]
        self.assertEqual((conflicts["total"], conflicts["by_code"]), (500, {"value_differs": 500}))
        self.assertEqual(len(conflicts["listed"]), 20)
        keys = [item["key"] for item in conflicts["listed"]]
        self.assertEqual(keys, sorted(f"[{1000 + n}]" for n in range(500))[:20])

    def test_51_the_report_states_the_unverified_prerequisites_in_every_mode(self):
        target, source = self.ws.target(), self.ws.source(lambda conn: fx.add_new_family(conn))
        runs = {"dry run": self.dry(target, source, self.preimage, fmt="text"),
                "incomplete": self.dry(target, source, fmt="text"),
                "refused": self.dry(self.ws.path("nope.db"), source, self.preimage, fmt="text")}
        token = self.plan_of(target, source, self.preimage).report["plan_sha256"]
        runs["applied"] = self.apply(target, source, self.preimage, token, fmt="text")
        for label, result in runs.items():
            with self.subTest(label):
                self.assertIn("a complete snapshot of the target taken before an apply: external, unverified", result.out)
                self.assertIn("the owner release record that ties the preimage to the target: external, unverified",
                              result.out)
                self.assertIn("this report is not a history witness", result.out)
                self.assertIn("no backup, file availability or approval is claimed", result.out)
                for banned in ("backup verified", "backup completed", "approved by"):
                    self.assertNotIn(banned, result.out)

    def test_52_long_identifiers_are_truncated_and_output_is_ascii_and_json_safe(self):
        long_role = "x" * 300
        source = self.ws.source()
        fx.tamper_table(source, "exam_task_questions", lambda rows: rows + [
            {"task_id": 1, "question_id": 1, "role": long_role, "sub_number": None}])
        result = self.dry(self.ws.target(), source, self.preimage)
        self.assert_refused(result, 1, "plan_conflicts", row_codes=["history_child_of_existing"])
        for item in result.report["conflicts"]["listed"]:
            self.assertLessEqual(len(item["key"]), tool.IDENTIFIER_LIMIT)
        self.assertNotIn(long_role, result.out)
        self.assertTrue(result.out.isascii())
        json.loads(result.out)
        with self.subTest("an ASCII-only stream does not turn a Cyrillic listing into an error"):
            def cyrillic(conn):
                fx.add_new_family(conn)
            target, source2 = self.ws.target("ascii.db"), self.ws.source(cyrillic)
            token = self.plan_of(target, source2, self.preimage).report["plan_sha256"]
            raw = io.BytesIO()
            stream = io.TextIOWrapper(raw, encoding="ascii")
            code = tool.run(["--db", str(target), "--source", str(source2), "--preimage", str(self.preimage)],
                            out=stream, err=io.StringIO())
            stream.flush()
            self.assertEqual(code, 0)
            self.assertEqual(raw.getvalue().decode("ascii").count("\n") > 5, True)
            del token


class StaticGuardsAndCliTest(fx.UpdaterTestCase):
    FORBIDDEN = {
        "DELETE FROM": r"DELETE\s+FROM", "INSERT OR REPLACE": r"INSERT\s+OR\s+REPLACE", "REPLACE INTO": r"REPLACE\s+INTO",
        "ON CONFLICT": r"ON\s+CONFLICT", "executescript": r"executescript", "immutable": r"immutable=1",
        "journal_mode assignment": r"journal_mode\s*=", "file copy": r"shutil\.copy", "environment": r"os\.environ",
        "getenv": r"getenv", "app run": r"app\.run", "runtime database path": r"data/questions\.db",
        "recovery word": r"DZI_RECOVER", "recovery gate": r"\bRECOVERY_(?:ENABLED|ACTIVATION)\b",
    }
    ALLOWED_IMPORTS = {"__future__", "argparse", "hashlib", "io", "json", "os", "re", "shutil", "sqlite3", "stat",
                       "struct", "sys", "tempfile", "unicodedata", "contextlib", "pathlib", "src", "web"}

    def test_71_the_module_source_contains_no_forbidden_forms_and_imports_only_allowed_modules(self):
        for name, pattern in self.FORBIDDEN.items():
            with self.subTest(name):
                self.assertIsNone(re.search(pattern, MODULE_SOURCE, re.IGNORECASE), name)
        for sample in ("DELETE  FROM t", "insert or replace into", "ON\nCONFLICT", "mode=ro&immutable=1",
                       "PRAGMA journal_mode = WAL", "os.environ['x']"):
            self.assertTrue(any(re.search(p, sample, re.IGNORECASE) for p in self.FORBIDDEN.values()), sample)
        self.assertIsNone(re.search("|".join(self.FORBIDDEN.values()), "sqlite3.SQLITE_DELETE and prose", re.IGNORECASE))
        tree = ast.parse(MODULE_SOURCE)
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported |= {alias.name.split(".")[0] for alias in node.names}
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                imported.add(node.module.split(".")[0])
        self.assertLessEqual(imported, self.ALLOWED_IMPORTS)
        for banned in ("subprocess", "socket", "urllib", "http", "requests"):
            self.assertNotIn(banned, imported)
        calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
                 and isinstance(node.func, ast.Attribute) and node.func.attr == "apply_migrations"]
        self.assertEqual(len(calls), 1)
        self.assertIsInstance(calls[0].args[0], ast.Name)
        self.assertEqual(calls[0].args[0].id, "reference_path")

    def test_72_every_connect_is_the_target_uri_or_a_run_temporary_and_the_guard_is_live(self):
        target, preimage, source = self.ws.target(runtime=True), self.ws.preimage(), self.ws.source(
            lambda conn: fx.add_new_family(conn))
        created = []
        real = tempfile.mkdtemp

        def recording(*args, **kwargs):
            directory = real(*args, **kwargs)
            created.append(directory)
            return directory
        self.spy.clear()
        with mock.patch.object(tempfile, "mkdtemp", recording):
            token = self.plan_of(target, source, preimage).report["plan_sha256"]
            self.assertEqual(self.apply(target, source, preimage, token).exit, 0)
        resolved = os.path.realpath(target)
        self.assertIn(resolved, [call["path"] for call in self.spy.uri_targets()])      # not vacuous
        for call in self.spy.calls:
            self.assertNotEqual(call["path"], str(RUNTIME_DB))
            if call["path"] == resolved:
                self.assertTrue(call["raw"].startswith("file:") and call["raw"].endswith("?mode=rw"), call)
            else:
                self.assertTrue(any(call["path"].startswith(directory) for directory in created), call)
        with self.assertRaises(RuntimeDatabaseAccess):
            sqlite3.connect(str(RUNTIME_DB))
        with self.assertRaises(RuntimeDatabaseAccess):
            sqlite3.connect(RUNTIME_DB.as_uri() + "?mode=rw", uri=True)
        self.assertNotIn(str(RUNTIME_DB), [call["path"] for call in self.spy.calls])
        # and the tool itself, asked for the runtime database path, never opens it
        result = fx.run_tool(RUNTIME_DB, self.ws.source(), preimage=preimage)
        self.assertEqual(result.exit, 1)
        self.assertNotIn(str(RUNTIME_DB), [call["path"] for call in self.spy.calls])

    def test_every_code_the_module_emits_belongs_to_the_fixed_vocabulary(self):
        tree = ast.parse(MODULE_SOURCE)
        run_codes, row_codes = set(), set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, "attr", "")
            literals = [arg.value for arg in node.args if isinstance(arg, ast.Constant) and isinstance(arg.value, str)]
            if name == "Stop" and len(node.args) >= 2 and isinstance(node.args[1], ast.Constant):
                run_codes.add(node.args[1].value)
            elif name == "_phase_stop" and len(node.args) == 2 and isinstance(node.args[1], ast.Constant):
                run_codes.add(node.args[1].value)
            elif name == "conflict" and len(node.args) >= 3 and isinstance(node.args[2], ast.Constant):
                row_codes.add(node.args[2].value)
            elif name == "fail" and literals:
                run_codes.add(literals[0])
        run_codes.discard(None)
        self.assertLessEqual(run_codes, set(tool.RUN_CODES))
        self.assertLessEqual(row_codes, set(tool.ROW_CODES))
        self.assertGreaterEqual(len(run_codes), 12)
        self.assertEqual(len(tool.ROW_CODES), 21)
        self.assertEqual(len(tool.RUN_CODES), 18)

    def test_34b_no_code_path_passes_none_to_set_authorizer(self):
        tree = ast.parse(MODULE_SOURCE)
        calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
                 and isinstance(node.func, ast.Attribute) and node.func.attr == "set_authorizer"]
        self.assertGreaterEqual(len(calls), 4)                            # not vacuous: read, probe, transition, write
        for call in calls:
            self.assertEqual(len(call.args), 1)
            self.assertNotIsInstance(call.args[0], ast.Constant, "set_authorizer(None) needs Python 3.11")
            self.assertIsInstance(call.args[0], ast.Name)

    def test_help_and_main_use_the_standard_streams(self):
        out = io.StringIO()
        with mock.patch("sys.stdout", out):
            self.assertEqual(tool.main(["--help"]), 0)
        self.assertIn("--expect-plan-sha256", out.getvalue())
        self.assertNotIn("questions.db", out.getvalue())

    def test_74_the_inventory_has_exactly_one_row_for_the_module(self):
        text = (ROOT / "docs" / "SCRIPT_INVENTORY.md").read_text(encoding="utf-8")
        rows = [line for line in text.splitlines() if line.startswith("| `src/update_canonical_content.py`")]
        self.assertEqual(len(rows), 1)
        active = text.split("## Active / currently used", 1)[1].split("## Review before use", 1)[0]
        self.assertIn(rows[0], active)


class ExpressionIndexTargetTest(fx.UpdaterTestCase):
    """PR #366: the target check sees the *expression* of an index key, not just that it has one.

    ``uniq_practice_sessions_open_scope`` is the one runtime index whose key is an expression.
    Replacing it under the same name with ``topic_id + 0`` or ``NULLIF(topic_id, topic_id)`` used to
    pass the compatible-target gate (both record the same NULL key name) and left two open
    section-wide sessions possible. The fix is in the shared ``table_shape``; nothing here is a
    practice-specific exception.
    """
    NAME = "uniq_practice_sessions_open_scope"
    REAL = (f"CREATE UNIQUE INDEX {NAME} ON practice_sessions(user_id, section_id, COALESCE(topic_id, 0)) "
            "WHERE submitted_at IS NULL AND abandoned_at IS NULL")

    def setUp(self):
        super().setUp()
        self.preimage = self.ws.preimage()
        self.source = self.ws.source(lambda conn: fx.add_new_family(conn))
        self.control = self.ws.target("control.db", runtime=True)
        self.token = self.plan_of(self.control, self.source, self.preimage).report["plan_sha256"]

    def replaced(self, name, sql):
        return self.ws.target(name, runtime=True, mutate=lambda conn: (conn.execute(f"DROP INDEX {self.NAME}"),
                                                                        conn.execute(sql)))

    def two_open_null_topic_sessions_possible(self, target):
        """Whether the index lets one user hold two open section-wide sessions (checked on a copy)."""
        copy = self.ws.path(f"probe-{target.name}")
        shutil.copyfile(target, copy)
        row = "(3, 1, NULL, 's', '[1]', '2026-01-01 00:00:00')"
        insert = ("INSERT INTO practice_sessions (user_id, section_id, topic_id, seed, question_ids_json, started_at) "
                  f"VALUES {row}")
        with closing(fx.REAL_CONNECT(str(copy), isolation_level=None)) as conn:
            conn.execute("PRAGMA foreign_keys = OFF")
            conn.execute("DELETE FROM practice_answers")
            conn.execute("DELETE FROM practice_sessions")
            conn.execute(insert)
            try:
                conn.execute(insert)
            except sqlite3.IntegrityError:
                return False
            return True

    def assert_refused_before_any_write(self, target):
        before, sequences, digest = fx.db_snapshot(target), fx.sequence_map(target), fx.file_bytes_digest(target)
        dry = self.dry(target, self.source, self.preimage)
        self.assert_refused(dry, 1, "incompatible_target")
        self.assertIsNone(dry.report.get("plan_sha256"))
        applied = self.apply(target, self.source, self.preimage, self.token)      # a token that is right for the control
        self.assert_refused(applied, 1, "incompatible_target")
        self.assertNotEqual(applied.report["status"], "applied")
        self.assert_untouched(target, before)
        self.assertEqual(fx.sequence_map(target), sequences)
        self.assertEqual(fx.file_bytes_digest(target), digest)
        self.assertEqual(fx.scalar(target, "SELECT COUNT(*) FROM questions WHERE id >= 100"), 0)   # the new family was not written

    def test_the_control_is_open_unique_and_wrong_expressions_really_are_not(self):
        self.assertFalse(self.two_open_null_topic_sessions_possible(self.control))
        for label, expression in (("plus zero", "topic_id + 0"), ("nullif", "NULLIF(topic_id, topic_id)")):
            with self.subTest(label):
                target = self.replaced(f"behaviour-{label.replace(' ', '-')}.db",
                                       self.REAL.replace("COALESCE(topic_id, 0)", expression))
                self.assertTrue(self.two_open_null_topic_sessions_possible(target))

    def test_a_same_named_index_with_another_key_expression_is_an_incompatible_target(self):
        for label, expression in (("plus zero", "topic_id + 0"), ("nullif", "NULLIF(topic_id, topic_id)"),
                                  ("other sentinel", "COALESCE(topic_id, -1)"),
                                  ("lower case function", "coalesce(topic_id, 0)")):    # conservative: spelling counts
            with self.subTest(label):
                target = self.replaced(f"wrong-{label.replace(' ', '-')}.db",
                                       self.REAL.replace("COALESCE(topic_id, 0)", expression))
                self.assert_refused_before_any_write(target)

    def test_the_other_wrong_indexes_are_still_incompatible_targets(self):
        cases = {
            "plain column key": self.REAL.replace("COALESCE(topic_id, 0)", "topic_id"),
            "predicate only submitted": self.REAL.replace(" AND abandoned_at IS NULL", ""),
            "not unique": self.REAL.replace("CREATE UNIQUE INDEX", "CREATE INDEX"),
            "reordered key": self.REAL.replace("user_id, section_id,", "section_id, user_id,"),
        }
        for label, sql in cases.items():
            with self.subTest(label):
                self.assert_refused_before_any_write(self.replaced(f"other-{label.replace(' ', '-')}.db", sql))

    def test_an_identical_recreated_index_still_plans_and_genuinely_applies(self):
        relaid = ("CREATE UNIQUE INDEX uniq_practice_sessions_open_scope ON practice_sessions ( user_id ,\n"
                  "    section_id , COALESCE( topic_id , 0 ) )  -- recreated\n"
                  "    WHERE submitted_at IS NULL AND abandoned_at IS NULL")
        decoy = self.REAL.replace(")) WHERE", ")) /* WHERE decoy */ WHERE").replace("ON practice_sessions(", "ON practice_sessions /* x */ (")
        for label, sql in (("same text", self.REAL), ("different layout", relaid), ("comments with where", decoy)):
            with self.subTest(label):
                target = self.replaced(f"recreated-{label.replace(' ', '-')}.db", sql)
                self.assertFalse(self.two_open_null_topic_sessions_possible(target))
                before = fx.db_snapshot(target)
                dry = self.plan_of(target, self.source, self.preimage)
                self.assertEqual(dry.report["plan_sha256"], self.token)                # same schema, same plan token
                self.assertEqual(fx.db_snapshot(target), before)
                result = self.apply(target, self.source, self.preimage, dry.report["plan_sha256"])
                self.assertEqual((result.exit, result.report["status"], result.report["preservation"]["equal"]),
                                 (0, "applied", True), result.out)
                self.assertGreater(fx.scalar(target, "SELECT COUNT(*) FROM questions WHERE id >= 100"), 0)
                self.assertFalse(self.two_open_null_topic_sessions_possible(target))

    def test_the_reference_records_the_expression_of_the_open_scope_index(self):
        with closing(fx.REAL_CONNECT(str(self.control))) as conn:
            shape = tool.table_shape(conn, "practice_sessions")["indexes"][self.NAME]
        self.assertEqual(shape["columns"][2][0], None)
        self.assertIn("COALESCE(topic_id,0)", shape["expression_identity"])
        self.assertIn("WHERE submitted_at IS NULL AND abandoned_at IS NULL", shape["expression_identity"])


if __name__ == "__main__":
    unittest.main()
