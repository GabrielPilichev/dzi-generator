"""B4 canonical content updater: locks, WAL and rollback-journal targets, restore lock, stale plans,
injected failures and COMMIT outcomes.

Timing is bounded (busy timeouts of 100-500 ms). Behaviors that the specification marks as
expectations (a rollback-journal writer keeps a ``-journal`` file once it modified a page, EXCLUSIVE
without a journal, and so on) are settled here by asserting their preconditions.
"""

import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

from src import canonical_tables as ct
from tests import update_canonical_fixtures as fx

ROOT = fx.ROOT


def writer(path, *, mode="IMMEDIATE"):
    conn = fx.REAL_CONNECT(str(path), isolation_level=None, timeout=0.1)
    conn.execute(f"BEGIN {mode}")
    return conn


def release(conn):
    try:
        conn.execute("ROLLBACK")
    finally:
        conn.close()


class LockCase(fx.UpdaterTestCase):
    def setUp(self):
        super().setUp()
        self.preimage = self.ws.preimage()
        self.source = self.ws.source(lambda conn: fx.add_new_family(conn))

    def token(self, target):
        return self.plan_of(target, self.source, self.preimage).report["plan_sha256"]

    def journal(self, target):
        return Path(str(target) + "-journal")


class WalAndBusyTest(LockCase):
    def test_53_a_dry_run_beside_an_open_wal_writer_sees_the_committed_state_only(self):
        target = self.ws.target(wal=True)
        before = fx.db_snapshot(target)
        other = writer(target)
        other.execute("INSERT INTO questions (id, source_exam, question_type, points, prompt) "
                      "VALUES (50, 'x', 'multiple_choice', 1, 'uncommitted')")
        try:
            result = self.dry(target, self.source, self.preimage, busy_ms=300)
            self.assertEqual((result.exit, result.report["status"]), (0, "planned"), result.out)
            self.assertEqual(result.report["tables"]["questions"]["target_only"], 0)
            self.assertEqual(result.report["target"]["journal_mode"], "wal")
        finally:
            release(other)
        self.assertEqual(fx.db_snapshot(target), before)                 # logical state only; sidecars may exist

    def test_54_one_read_transaction_gives_every_table_the_same_snapshot(self):
        target = self.ws.target(wal=True)
        committed = []

        def writer_commits(table):
            if table == "assets" and not committed:
                committed.append(table)
                fx.execute(target, "INSERT INTO topic_aliases (id, alias_slug, action) VALUES (50, 'late', 'merge')")
        result = self.dry(target, self.source, self.preimage, hooks={"after_table_read": writer_commits})
        self.assertEqual(committed, ["assets"])
        self.assertEqual(result.report["tables"]["topic_aliases"]["target_only"], 0)       # the pre-write snapshot
        again = self.dry(target, self.source, self.preimage)
        self.assertEqual(again.report["tables"]["topic_aliases"]["target_only"], 1)         # control: now it is there

    def test_55_a_wal_writer_holding_the_lock_makes_apply_wait_then_exit_two(self):
        target = self.ws.target(wal=True)
        token = self.token(target)
        for label, strip in (("with the error code", False), ("message fallback without the error code", True)):
            with self.subTest(label):
                other = writer(target)
                try:
                    classes = [fx.make_connection_class([], strip_errors=strip)]
                    started = time.monotonic()
                    result = self.apply(target, self.source, self.preimage, token, busy_ms=300,
                                        connect=fx.FactoryConnect(classes))
                    elapsed = time.monotonic() - started
                finally:
                    release(other)
                self.assert_refused(result, 2, "busy")
                self.assertGreaterEqual(elapsed, 0.28)
                self.assertLess(elapsed, 3.0)
                self.assertEqual(fx.scalar(target, "SELECT COUNT(*) FROM questions WHERE id = 100"), 0)
        probe = sqlite_error_without_code()
        self.assertIsNone(getattr(probe, "sqlite_errorcode", None))                 # the precondition of the fallback

    def test_56_rollback_journal_targets_with_a_writer_next_to_the_tool(self):
        target = self.ws.target()
        journal = self.journal(target)
        with self.subTest("a writer that modified no page leaves no journal and does not block a dry run"):
            other = writer(target)
            try:
                self.assertFalse(journal.exists())
                result = self.dry(target, self.source, self.preimage, busy_ms=300)
                self.assertEqual((result.exit, result.report["status"]), (0, "planned"), result.out)
            finally:
                release(other)
        with self.subTest("a writer that executed an UPDATE keeps a journal: refused, never opened"):
            other = writer(target)
            try:
                other.execute("UPDATE scrape_log SET status = status")
                other.execute("INSERT INTO scrape_log (source, url, status) VALUES ('a', 'b', 'c')")
                self.assertTrue(os.path.lexists(journal), "expectation: a modifying writer keeps a -journal file")
                self.spy.clear()
                result = self.dry(target, self.source, self.preimage, busy_ms=300)
                self.assert_refused(result, 1, "journal_present")
                self.assertEqual([c for c in self.spy.calls if c["path"] == os.path.realpath(target)], [])
            finally:
                release(other)
        with self.subTest("BEGIN EXCLUSIVE held and no journal: busy within the timeout"):
            other = writer(target, mode="EXCLUSIVE")
            try:
                self.assertFalse(journal.exists())
                started = time.monotonic()
                result = self.dry(target, self.source, self.preimage, busy_ms=300)
                elapsed = time.monotonic() - started
            finally:
                release(other)
            self.assert_refused(result, 2, "busy")
            self.assertLess(elapsed, 3.0)

    def test_57_no_read_lock_remains_after_a_dry_run(self):
        target = self.ws.target()
        self.assertEqual(self.dry(target, self.source, self.preimage).exit, 0)
        other = fx.REAL_CONNECT(str(target), isolation_level=None, timeout=0)
        try:
            other.execute("BEGIN IMMEDIATE")
            other.execute("INSERT INTO scrape_log (source, url, status) VALUES ('a', 'b', 'c')")
            other.execute("COMMIT")
        finally:
            other.close()
        self.assertEqual(fx.scalar(target, "SELECT COUNT(*) FROM scrape_log"), 1)


def sqlite_error_without_code():
    import sqlite3
    return sqlite3.OperationalError("database is locked")


class StaleAndRaceTest(LockCase):
    def test_58_a_canonical_change_between_the_phases_is_stale_a_runtime_change_is_not(self):
        target = self.ws.target(runtime=True)
        token = self.token(target)
        with self.subTest("a canonical row inserted by another connection"):
            snapshot = {}

            def other_writer(conn):
                fx.execute(target, "INSERT INTO topic_aliases (id, alias_slug, action) VALUES (50, 'late', 'merge')")
                snapshot.update(fx.db_snapshot(target))
            result = self.apply(target, self.source, self.preimage, token, hooks={"after_read_phase": other_writer})
            self.assert_refused(result, 2, "stale_plan")
            self.assertEqual(fx.db_snapshot(target), snapshot)
            self.assertEqual(fx.scalar(target, "SELECT COUNT(*) FROM questions WHERE id = 100"), 0)
            fx.execute(target, "DELETE FROM topic_aliases WHERE id = 50")
            fx.execute(target, "DELETE FROM sqlite_sequence WHERE name = 'topic_aliases'")
            fx.execute(target, "INSERT INTO sqlite_sequence (name, seq) VALUES ('topic_aliases', 1)")
        with self.subTest("a runtime row inserted in that window is preserved and digested under the lock"):
            target = self.ws.target("runtime.db", runtime=True)
            token = self.token(target)

            def runtime_writer(conn):
                fx.execute(target, "INSERT INTO login_throttle (identifier_hash, failure_count) VALUES ('" + "7" * 64 + "', 1)")
            result = self.apply(target, self.source, self.preimage, token, hooks={"after_read_phase": runtime_writer})
            self.assertEqual((result.exit, result.report["status"]), (0, "applied"), result.out)
            self.assertEqual(fx.scalar(target, "SELECT COUNT(*) FROM login_throttle"), 2)
            self.assertIs(result.report["preservation"]["equal"], True)

    def test_59_a_target_changed_after_the_dry_run_is_stale_before_any_write_lock(self):
        target = self.ws.target()
        token = self.token(target)
        fx.execute(target, "INSERT INTO topic_aliases (id, alias_slug, action) VALUES (50, 'late', 'merge')")
        log = []
        result = self.apply(target, self.source, self.preimage, token,
                            connect=fx.FactoryConnect([fx.make_connection_class(log)]))
        self.assert_refused(result, 2, "stale_plan")
        self.assertNotIn("BEGIN IMMEDIATE", [entry.upper() for entry in log if isinstance(entry, str)])
        other = fx.REAL_CONNECT(str(target), isolation_level=None, timeout=0)
        try:
            other.execute("BEGIN IMMEDIATE")                                  # the write lock was never taken
            other.execute("ROLLBACK")
        finally:
            other.close()

    def test_60_the_restore_lock_is_checked_for_the_given_and_the_resolved_path(self):
        target = self.ws.target()
        token = self.token(target)
        lock = target.parent / "restore-in-progress"
        with self.subTest("present before the run, for a dry run and an apply"):
            lock.mkdir()
            for label, result in (("dry run", self.dry(target, self.source, self.preimage)),
                                  ("apply", self.apply(target, self.source, self.preimage, token))):
                self.spy.clear()
                self.assert_refused(result, 1, "restore_lock_present")
            self.assertEqual([c for c in self.spy.calls if c["path"] == os.path.realpath(target)], [])
            lock.rmdir()
            self.spy.clear()
            self.assertEqual(self.dry(target, self.source, self.preimage).exit, 0)
            self.assertIn(os.path.realpath(target), [c["path"] for c in self.spy.uri_targets()])   # not vacuous
        with self.subTest("a file named restore-in-progress counts"):
            lock.write_text("")
            self.assert_refused(self.dry(target, self.source, self.preimage), 1, "restore_lock_present")
            lock.unlink()
        with self.subTest("only next to the given path or only next to the resolved path"):
            outer = self.ws.path("lexical")
            real = self.ws.path("physical")
            inner = real / "inner"
            outer.mkdir()
            inner.mkdir(parents=True)
            os.symlink(inner, outer / "link")
            moved = real / "t.db"
            import shutil
            shutil.copyfile(target, moved)
            given = f"{outer}/link/../t.db"                  # lexically {outer}/t.db, physically {real}/t.db
            self.assertEqual(os.path.abspath(given), str(outer / "t.db"))
            self.assertEqual(os.path.realpath(given), os.path.realpath(moved))
            self.assertEqual(self.dry(given, self.source, self.preimage).exit, 0)
            for label, directory in (("given path", outer), ("resolved path", real)):
                (directory / "restore-in-progress").mkdir()
                try:
                    self.assert_refused(self.dry(given, self.source, self.preimage), 1, "restore_lock_present")
                finally:
                    (directory / "restore-in-progress").rmdir()
        with self.subTest("created while apply waits for BEGIN IMMEDIATE"):
            before = fx.db_snapshot(target)
            other = writer(target)
            outcome = {}
            thread = threading.Thread(target=lambda: outcome.update(
                result=self.apply(target, self.source, self.preimage, token, busy_ms=5000)))
            thread.start()
            time.sleep(0.4)
            lock.mkdir()
            release(other)
            thread.join(10)
            self.assertFalse(thread.is_alive())
            lock.rmdir()
            self.assert_refused(outcome["result"], 1, "restore_lock_present")
            self.assertEqual(fx.db_snapshot(target), before)
        with self.subTest("created between the inserts and COMMIT"):
            result = self.apply(target, self.source, self.preimage, token,
                                hooks={"before_commit": lambda conn: lock.mkdir()})
            lock.rmdir()
            self.assert_refused(result, 3, "restore_lock_present", status="not_committed")
            self.assertEqual(fx.db_snapshot(target), before)

    def test_60_every_recheck_names_both_the_given_and_the_resolved_path(self):
        import shutil
        outer, real = self.ws.path("lexical-r"), self.ws.path("physical-r")
        (real / "inner").mkdir(parents=True)
        outer.mkdir()
        os.symlink(real / "inner", outer / "link")
        moved = real / "t.db"
        shutil.copyfile(self.ws.target(), moved)
        given = f"{outer}/link/../t.db"
        token = self.plan_of(given, self.source, self.preimage).report["plan_sha256"]
        before = fx.db_snapshot(moved)
        for label, directory in (("given", outer), ("resolved", real)):
            lock = directory / "restore-in-progress"
            for hook_name, exit_code, status in (("after_lock", 1, "refused"), ("before_commit", 3, "not_committed")):
                with self.subTest(f"{label} path, {hook_name}"):
                    result = self.apply(given, self.source, self.preimage, token,
                                        hooks={hook_name: lambda conn, lock=lock: lock.mkdir()})
                    lock.rmdir()
                    self.assert_refused(result, exit_code, "restore_lock_present", status=status)
                    self.assertEqual(fx.db_snapshot(moved), before)

    def test_61_two_appliers_with_the_same_token_commit_exactly_once(self):
        target = self.ws.target(runtime=True)
        token = self.token(target)
        command = [sys.executable, "-B", "-m", "src.update_canonical_content", "--db", str(target), "--source",
                   str(self.source), "--preimage", str(self.preimage), "--apply", "--expect-plan-sha256", token,
                   "--format", "json", "--busy-timeout-ms", "20000"]
        env = {key: value for key, value in os.environ.items() if not key.startswith("DZI_")}
        processes = [subprocess.Popen(command, cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                     for _ in range(2)]
        codes = sorted(process.wait(timeout=120) for process in processes)
        for process in processes:
            process.stdout.close()
            process.stderr.close()
        self.assertEqual(codes, [0, 2])
        self.assertEqual(fx.scalar(target, "SELECT COUNT(*) FROM questions WHERE id = 100"), 1)

    def test_62_any_journal_file_is_refused_before_the_target_is_opened(self):
        target = self.ws.target()
        journal = self.journal(target)
        real_journal = {}
        other = writer(target)
        other.execute("UPDATE scrape_log SET status = status")
        other.execute("INSERT INTO scrape_log (source, url, status) VALUES ('a', 'b', 'c')")
        real_journal["bytes"] = journal.read_bytes() if journal.exists() else b""
        release(other)
        for label, content in (("empty file", b""), ("persisted header", b"\x00" * 512),
                               ("copy of a real journal", real_journal["bytes"] or b"\xd9\xd5\x05\xf9\x20\xa1\x63\xd7")):
            with self.subTest(label):
                journal.write_bytes(content)
                before = fx.file_bytes_digest(target)
                self.spy.clear()
                try:
                    result = self.dry(target, self.source, self.preimage)
                    applied = self.apply(target, self.source, self.preimage, "0" * 64)
                finally:
                    journal.unlink()
                self.assert_refused(result, 1, "journal_present")
                self.assert_refused(applied, 1, "journal_present")
                self.assertEqual([c for c in self.spy.calls if c["path"] == os.path.realpath(target)], [])
                self.assertEqual(fx.file_bytes_digest(target), before)


class InjectedFailureTest(LockCase):
    def test_63_a_failure_at_any_point_before_commit_leaves_the_pre_state(self):
        target = self.ws.target(runtime=True)
        token = self.token(target)
        dry = self.dry(target, self.source, self.preimage)
        order = [table for table in ct.tables_of(ct.CANONICAL) if dry.report["tables"][table]["insert"]]
        with closing(fx.REAL_CONNECT(str(target))) as conn:
            loaded = [table for table in ct.canonical_load_order(conn) if table in order]
        before, listing = fx.db_snapshot(target), fx.tree_listing(target.parent)

        def raiser(only=None):
            def hook(*args):
                if only is None or args[0] == only:
                    raise RuntimeError("injected failure")
            return hook
        cases = {
            "after the first table": {"after_insert": raiser(loaded[0])},
            "after the last insert": {"after_insert": raiser(loaded[-1])},
            "after apply_sequence": {"after_sequence": raiser()},
            "before COMMIT": {"before_commit": raiser()},
        }
        for label, hooks in cases.items():
            with self.subTest(label):
                result = self.apply(target, self.source, self.preimage, token, hooks=hooks)
                self.assert_refused(result, 3, "write_failed", status="not_committed")
                self.assertEqual(fx.db_snapshot(target), before)
                self.assertEqual(fx.tree_listing(target.parent), listing)           # no -journal left behind
                other = fx.REAL_CONNECT(str(target), isolation_level=None, timeout=0)
                try:
                    other.execute("BEGIN IMMEDIATE")                                # the connection was closed
                    other.execute("ROLLBACK")
                finally:
                    other.close()

    def refused_apply(self, mutate_source):
        source = self.ws.source()
        fx.tamper_table(source, *mutate_source)
        target = self.ws.target()
        dry = self.dry(target, source, self.preimage)
        self.assertEqual((dry.exit, dry.report["status"]), (0, "planned"), dry.out)
        before = fx.db_snapshot(target)
        result = self.apply(target, source, self.preimage, dry.report["plan_sha256"])
        self.assert_refused(result, 3, "write_failed", status="not_committed")
        self.assertEqual(fx.db_snapshot(target), before)

    def test_64_an_error_the_plan_cannot_see_is_rolled_back_with_exit_three(self):
        extra = {"id": 100, "exam_id": None, "source_exam": "extra", "source_number": 80, "subject": None, "level": None,
                 "year": None, "question_type": "multiple_choice", "topic": None, "difficulty": "medium", "points": 1,
                 "prompt": "Нов", "has_image": 0, "image_path": None, "created_at": fx.FIXED_TS, "is_ai_generated": 0,
                 "quality_score": None, "topic_id": None, "legacy_topic": None}
        for label, change in (("a foreign key to nothing", {"exam_id": 999}), ("a CHECK edge", {"question_type": "bogus"}),
                              ("a dangling topic reference found by the post-check", {"topic_id": 999})):
            with self.subTest(label):
                self.refused_apply(("questions", lambda rows, change=change: rows + [dict(extra, **change)]))

    def commit_run(self, injection, *, classes=None, target=None, **connect_kwargs):
        target = target or self.ws.target(runtime=True)
        token = self.token(target)
        log = []
        connect = fx.FactoryConnect([fx.make_connection_class(log, injection)] + list(classes or []), **connect_kwargs)
        result = self.apply(target, self.source, self.preimage, token, connect=connect, fmt="text")
        return target, token, result, connect

    def test_65_a_commit_that_fails_before_applying_is_not_committed(self):
        cases = (("busy by code", fx.CommitInjection("before", "x", code=5), 2),
                 ("busy by message without a code", fx.CommitInjection("busy"), 2),
                 ("locked by code", fx.CommitInjection("before", "x", code=6), 2),
                 ("I/O error by code", fx.CommitInjection("before", code=10), 3),
                 ("I/O error without a code", fx.CommitInjection("before"), 3))
        for label, injection, expected in cases:
            with self.subTest(label):
                before_target = self.ws.target(f"c-{abs(hash(label))}.db", runtime=True)
                before = fx.db_snapshot(before_target)
                target, _token, result, _connect = self.commit_run(injection, target=before_target)
                self.assertEqual(result.exit, expected, result.out)
                self.assertIn("status: not_committed", result.out)
                self.assertIn("NOT COMMITTED", result.out)
                self.assertEqual(fx.db_snapshot(target), before)

    def test_66_a_commit_that_executes_and_then_raises_is_reported_committed(self):
        target, token, result, _connect = self.commit_run(fx.CommitInjection("after"))
        self.assertEqual(result.exit, 4, result.out)
        self.assertIn("COMMITTED: the planned write footprint is visible to a fresh connection after a COMMIT error",
                      result.out)
        for banned in ("rolled back", "durable", "this run committed"):
            self.assertNotIn(banned, result.out)
        self.assertEqual(fx.scalar(target, "SELECT COUNT(*) FROM questions WHERE id = 100"), 1)
        with self.subTest("a second applier that commits after this run's failed COMMIT changes nothing in its report"):
            target = self.ws.target("second.db", runtime=True)
            token = self.token(target)
            before = fx.db_snapshot(target)
            outcome = {}

            def second_applier():
                outcome["second"] = self.apply(target, self.source, self.preimage, token)
            connect = fx.FactoryConnect([fx.make_connection_class([], fx.CommitInjection("before"))],
                                        before_open={1: second_applier})
            result = self.apply(target, self.source, self.preimage, token, connect=connect, fmt="text")
            self.assertEqual(outcome["second"].exit, 0)
            self.assertNotEqual(fx.db_snapshot(target), before)
            self.assertEqual(result.exit, 4, result.out)
            self.assertIn("COMMITTED: the planned write footprint is visible to a fresh connection", result.out)
            self.assertNotIn("this run committed", result.out)

    def test_67_an_unreadable_probe_is_an_unknown_outcome(self):
        target, _token, result, connect = self.commit_run(fx.CommitInjection("after"), fail_on={1})
        self.assertEqual(result.exit, 5, result.out)
        self.assertIn("status: outcome_unknown", result.out)
        self.assertIn("Do not retry this plan or token", result.out)
        self.assertEqual(len(connect.calls), 2)

    def test_67a_a_journal_after_the_commit_error_means_the_probe_never_opens(self):
        target = self.ws.target(runtime=True)
        injection = fx.CommitInjection("after_journal", journal=Path(str(target) + "-journal"))
        _target, _token, result, connect = self.commit_run(injection, target=target)
        self.assertEqual(result.exit, 5, result.out)
        self.assertEqual(len(connect.calls), 1)                                  # the probe connection was never made

    def test_67b_a_rollback_that_finds_no_transaction_and_no_post_state_is_not_committed(self):
        before_target = self.ws.target(runtime=True)
        before = fx.db_snapshot(before_target)
        target, _token, result, _connect = self.commit_run(fx.CommitInjection("ended"), target=before_target)
        self.assertEqual(result.exit, 3, result.out)
        self.assertIn("NOT COMMITTED", result.out)
        self.assertEqual(fx.db_snapshot(target), before)

    def test_67c_a_partial_probe_view_is_an_unknown_outcome(self):
        probe = fx.make_connection_class([], hide_table='"questions"')
        _target, _token, result, _connect = self.commit_run(fx.CommitInjection("after"), classes=[probe])
        self.assertEqual(result.exit, 5, result.out)
        self.assertIn("run code: commit_outcome_unknown", result.out)

    def test_68_a_receipt_failure_after_a_real_commit_is_exit_four(self):
        import io

        class Closed(io.StringIO):
            def write(self, text):
                raise OSError("closed")
        target = self.ws.target(runtime=True)
        token = self.token(target)
        err = io.StringIO()
        result = fx.run_tool(target, self.source, preimage=self.preimage, apply_hash=token, out=Closed(), err=err,
                             fmt="text")
        self.assertEqual(result.exit, 4)
        self.assertEqual(fx.scalar(target, "SELECT COUNT(*) FROM questions WHERE id = 100"), 1)
        self.assertIn("COMMITTED: the transaction was committed and the receipt could not be written", err.getvalue())
        self.assertNotIn("rolled back", err.getvalue())

    def test_69_failures_before_the_first_canonical_write_never_use_three_four_or_five(self):
        target = self.ws.target(runtime=True)
        token = self.token(target)
        lock = target.parent / "restore-in-progress"
        outcomes = {
            "unreadable source": (self.apply(target, self.ws.path("absent-source"), self.preimage, token), 1),
            "restore lock under the write lock": (self.apply(target, self.source, self.preimage, token,
                                                              hooks={"after_lock": lambda conn: lock.mkdir()}), 1),
        }
        lock.rmdir()
        other = writer(target)
        try:
            outcomes["busy"] = (self.apply(target, self.source, self.preimage, token, busy_ms=150), 2)
        finally:
            release(other)
        outcomes["stale"] = (self.apply(target, self.source, self.preimage, "f" * 64), 2)
        for label, (result, expected) in outcomes.items():
            with self.subTest(label):
                self.assertEqual(result.exit, expected, result.out)
                self.assertEqual(result.report["status"], "refused")
                self.assertNotIn(result.exit, (3, 4, 5))

    def test_70_no_temporary_directory_remains(self):
        created = []
        real = tempfile.mkdtemp

        def recording(*args, **kwargs):
            directory = real(*args, **kwargs)
            created.append(directory)
            return directory
        target = self.ws.target(runtime=True)
        token = self.token(target)

        def boom(table):
            raise RuntimeError("injected")
        with mock.patch.object(tempfile, "mkdtemp", recording):
            runs = {
                "success": self.apply(target, self.source, self.preimage, token),
                "refusal": self.dry(self.ws.path("absent.db"), self.source, self.preimage),
                "failure": self.apply(self.ws.target("f.db"), self.source, self.preimage, token,
                                      hooks={"after_insert": boom}),
                "invalid source": self.dry(target, self.ws.path("nowhere"), self.preimage),
            }
        self.assertEqual([runs[name].exit for name in ("success", "refusal", "failure", "invalid source")], [0, 1, 3, 1])
        updater_dirs = [d for d in created if Path(d).name.startswith("learnpilot-updater-")]
        self.assertGreaterEqual(len(updater_dirs), 4)
        for directory in created:
            self.assertFalse(os.path.exists(directory), directory)


if __name__ == "__main__":
    unittest.main()
