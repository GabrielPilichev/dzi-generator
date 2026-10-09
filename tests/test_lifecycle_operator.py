"""Assignment lifecycle operator tooling: ``python3 -m src.manage_lifecycle``.

Clock incident recovery (3.7), holds as a set of reasons (3.8), restore and
reconciliation (13.4), verification before traffic is reopened (13.5) and the
exposure audit (13.3). What the commands refuse matters as much as what they do:
no closure is manufactured, no marker is cleared or changed, and no held
assignment is released without the evidence its reason requires. After a restore
that evidence is the replaced database and nothing else: application log lines
and an operator's statement release nothing. Case ids refer to
docs/ASSIGNMENT_LIFECYCLE_DESIGN.md, section 15.1.
"""

import io
import shutil
import sqlite3
import unittest
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path

from src import manage_lifecycle
from tests.lifecycle_support import GRACE, SAME_ORIGIN, LifecycleTestCase, ts
from tests.support import set_lifecycle_clock_row, web_app
from web.services import lifecycle

MARKERS = lifecycle.TERMINAL_MARKER_COLUMNS
HOUR = timedelta(hours=1)


class OperatorTestCase(LifecycleTestCase):
    def setUp(self):
        super().setUp()
        self.teacher = self.account("teacher")
        self.student = self.account("student")
        self.owner = self.client(self.teacher)

    def hold(self, assignment_id):
        return self.row(assignment_id)["lifecycle_hold"]

    def markers(self, assignment_id) -> dict:
        row = self.row(assignment_id)
        return {column: row[column] for column in (*MARKERS, "deadline_at")}

    def incident(self, *, verified: datetime, old_high_water: datetime, reason="clock_step", local=None):
        """The clock ran to ``old_high_water`` and now reads ``local`` (default: the verified time)."""
        self.clock.freeze(local or verified)
        set_lifecycle_clock_row(self.db_path, high_water=old_high_water, state="suspect", reason=reason,
                                changed_at=old_high_water)

    def recover(self, verified: datetime, *extra):
        return self.cli("clock-recover", "--verified-utc", ts(verified), *extra, writer=True)

    def snapshot(self, name="snapshot.db") -> Path:
        """The database as a backup taken an hour ago holds it: the same rows, and a lifecycle
        clock that had not yet run as far as the live one has by now."""
        target = self.root / name
        shutil.copyfile(self.db_path, target)
        set_lifecycle_clock_row(target, high_water=self.T0 - HOUR, anchored=False)
        return target

    def copy_of(self, source: Path, name: str) -> Path:
        """A byte copy, with nothing adjusted."""
        target = self.root / name
        shutil.copyfile(source, target)
        return target

    def reconcile(self, pre, snapshot, note="проба"):
        return self.cli("reconcile", "--pre-restore-db", pre, "--snapshot-db", snapshot,
                        "--evidence-note", note, writer=True)

    @staticmethod
    def sql(path, statement, params=()):
        with closing(sqlite3.connect(str(path))) as conn:
            rows = conn.execute(statement, params).fetchall()
            conn.commit()
            return rows

    def restore(self, snapshot: Path) -> Path:
        """Replace the live database by ``snapshot``; the old one is moved aside and returned."""
        moved_aside = self.root / "pre-restore.db"
        shutil.move(str(self.db_path), str(moved_aside))
        shutil.copyfile(snapshot, self.db_path)
        return moved_aside

    # The lines the application writes to its log, as an operator would copy them from the journal.
    # They are diagnostic: the tests below hand them to the tool to show that nothing is released.

    @staticmethod
    def deadline_event(kind, assignment_id, at, deadline_at, zone="Europe/Sofia", actor=1) -> str:
        deadline = None if deadline_at is None else ts(deadline_at)
        return (f"Oct 08 12:00:01 host gunicorn[77]: learnpilot.lifecycle INFO lifecycle event={kind} "
                f"assignment_id={assignment_id} at={ts(at)!r} deadline_at={deadline!r} "
                f"display_timezone={zone!r} actor_user_id={actor}")

    @staticmethod
    def close_event(assignment_id, closed_at, actor) -> str:
        return (f"Oct 08 12:00:02 host gunicorn[77]: learnpilot.lifecycle INFO lifecycle event=close "
                f"assignment_id={assignment_id} closed_at={ts(closed_at)!r} actor_user_id={actor}")

    def record_file(self, *lines, name="record.txt") -> Path:
        path = self.root / name
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    def reconcile_record(self, assignment_id, record, *state, audit=None):
        argv = ["reconcile-record", "--assignment", assignment_id, "--record-file", record, *state]
        if audit is None:
            return self.cli(*argv, writer=True)
        return self.cli(*argv, "--operator", "тест оператор", "--audit-log", audit)


class CommandLineTest(OperatorTestCase):
    def test_the_database_is_never_defaulted_and_must_exist(self):
        import os
        from unittest.mock import patch
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("DZI_DB", None)
            out, err = __import__("io").StringIO(), __import__("io").StringIO()
            self.assertEqual(manage_lifecycle.main(["clock-status"], out=out, err=err), 2)
            self.assertIn("no database given", err.getvalue())
        code, _out, err = self.cli("clock-status", db=self.root / "missing.db")
        self.assertEqual(code, 2)
        self.assertIn("database not found", err)
        self.assertFalse((self.root / "missing.db").exists())

    def test_a_tool_refuses_a_schema_its_build_does_not_match(self):
        self.execute("INSERT INTO schema_migrations (filename) VALUES ('019_from_a_newer_build.sql')")
        code, _out, err = self.cli("clock-status")
        self.assertEqual(code, 2)
        self.assertIn("applied migrations this build does not ship: 019_from_a_newer_build.sql", err)
        self.execute("DELETE FROM schema_migrations WHERE filename IN ('019_from_a_newer_build.sql', "
                     "'018_assignment_lifecycle.sql')")
        code, _out, err = self.cli("verify")
        self.assertEqual(code, 2)
        self.assertIn("pending migrations: 018_assignment_lifecycle.sql", err)

    def test_a_writing_command_needs_an_operator_and_an_audit_log(self):
        with self.assertRaises(SystemExit):  # argparse: the options are required
            self.cli("restore-hold")
        code, _out, err = self.cli("verify", "--hold-exceptions")
        self.assertEqual(code, 2)
        self.assertIn("--operator and --audit-log", err)

    def test_the_commands_are_exactly_the_documented_ones(self):
        # L121: there is no command that ends, closes or releases without evidence.
        self.assertEqual(sorted(manage_lifecycle.READ_ONLY_COMMANDS),
                         ["clock-inspect", "clock-status", "exposure-audit", "hold-list", "restore-status"])
        self.assertEqual(sorted(manage_lifecycle.WRITING_COMMANDS),
                         ["clock-recover", "hold-resolve", "reconcile", "restore-hold"])
        # The two that rested on application logs are names that explain themselves, not commands.
        self.assertEqual(sorted(manage_lifecycle.UNSUPPORTED_COMMANDS), ["reconcile-partial", "reconcile-record"])
        self.assertFalse(set(manage_lifecycle.UNSUPPORTED_COMMANDS)
                         & {*manage_lifecycle.READ_ONLY_COMMANDS, *manage_lifecycle.WRITING_COMMANDS})

    def test_the_log_based_commands_open_nothing_and_say_why(self):
        missing_db, never_written = self.root / "no-such.db", self.root / "never-written.log"
        record = self.record_file(self.deadline_event("edit", 1, self.T0, None))
        for command, arguments in (
                ("reconcile-record", ()),
                ("reconcile-record", ("--assignment", "1", "--record-file", record, "--no-deadline",
                                      "--display-timezone", "Europe/Sofia", "--not-closed")),
                ("reconcile-record", ("--assignment", "1", "--force", "--i-know-what-i-am-doing")),
                ("reconcile-partial", ()),
                ("reconcile-partial", ("--affected-ids", "1,2", "--record-file", record))):
            with self.subTest(command=command, arguments=arguments[:2]):
                out, err = io.StringIO(), io.StringIO()
                code = manage_lifecycle.main(
                    ["--db", str(missing_db), command, *map(str, arguments), "--operator", "тест оператор",
                     "--audit-log", str(never_written)], out=out, err=err)
                self.assertEqual((code, out.getvalue()), (manage_lifecycle.EXIT_UNSUPPORTED, ""))
                message = err.getvalue()
                self.assertTrue(message.startswith(f"unsupported: {command} "))
                for stated in ("Nothing was read and nothing was written", "diagnostic, not evidence",
                               "does not establish that the assignment was not closed",
                               "does not mention is not thereby unaffected", "reconcile --pre-restore-db",
                               "stays held", "do not clear a hold by hand", "I5", "deferred"):
                    self.assertIn(stated, message)
        # The database was not even looked for, and nothing was appended anywhere.
        self.assertFalse(missing_db.exists())
        self.assertFalse(never_written.exists())
        self.assertEqual(self.audit_entries(), [])
        self.assertEqual((manage_lifecycle.EXIT_REFUSED, manage_lifecycle.EXIT_ERROR, manage_lifecycle.EXIT_INCOMPLETE,
                          manage_lifecycle.EXIT_UNSUPPORTED), (1, 2, 3, 4))

    def test_clock_status_reports_the_row_and_the_hosts_evidence(self):
        code, out, _err = self.cli("clock-status")
        self.assertEqual(code, 0)
        self.assertIn("state: ok", out)
        self.assertIn(f"high-water mark: {ts(self.T0)}", out)
        self.assertIn("clock synchronised: yes", out)
        self.execute("DELETE FROM lifecycle_clock")
        code, out, _err = self.cli("clock-status")
        self.assertEqual(code, 1)
        self.assertIn("clock row: UNUSABLE", out)


class HoldSetTest(OperatorTestCase):
    def test_l139_reasons_are_added_and_resolved_one_at_a_time(self):
        assignment_id = self.assignment(self.teacher)
        with closing(sqlite3.connect(str(self.db_path))) as conn:
            add, resolve = manage_lifecycle.add_hold_reason, manage_lifecycle.resolve_hold_reason
            self.assertTrue(add(conn, assignment_id, "restore_pending"))
            self.assertFalse(add(conn, assignment_id, "restore_pending"))  # idempotent
            self.assertTrue(add(conn, assignment_id, "clock_review"))
            conn.commit()
            self.assertEqual(self.hold(assignment_id), "clock_review,restore_pending")  # the fixed order
            self.assertTrue(add(conn, assignment_id, "restore_unreconciled"))
            self.assertTrue(manage_lifecycle.hold_is_readable(self.hold(assignment_id)))
            resolve(conn, assignment_id, "restore_pending")
            conn.commit()
            self.assertEqual(self.hold(assignment_id), "clock_review,restore_unreconciled")
            with self.assertRaises(manage_lifecycle.Refused):
                resolve(conn, assignment_id, "restore_pending")  # not there: nothing else is touched
            resolve(conn, assignment_id, "clock_review")
            resolve(conn, assignment_id, "restore_unreconciled")
            conn.commit()
            self.assertIsNone(self.hold(assignment_id))
            with self.assertRaises(manage_lifecycle.OperatorError):
                add(conn, assignment_id, "restore_reclean")  # PR #328: unknown to this build

    MALFORMED_HOLDS = ("clock_review,", ",clock_review", "clock_review,,restore_pending",
                       "clock_review,clock_review", "restore_pending,clock_review", "clock_review,mystery",
                       "mystery", "", " clock_review", "clock_review ")

    def test_a_malformed_hold_is_never_resolved_and_never_released(self):
        # Codex finding 6: 'clock_review,' must not become NULL by resolving clock_review.
        for stored in self.MALFORMED_HOLDS:
            with self.subTest(stored=stored):
                assignment_id = self.assignment(self.teacher, lifecycle_hold=stored)
                self.assertFalse(manage_lifecycle.hold_is_readable(stored))
                with closing(sqlite3.connect(str(self.db_path))) as conn:
                    for reason in lifecycle.HOLD_REASONS:
                        with self.assertRaises(manage_lifecycle.Refused) as caught:
                            manage_lifecycle.resolve_hold_reason(conn, assignment_id, reason)
                        self.assertIn("unreadable", str(caught.exception))
                    conn.commit()
                code, _out, err = self.cli("hold-resolve", "--assignment", assignment_id, "--reason", "clock_review",
                                           "--evidence", "преглед", writer=True)
                self.assertEqual(code, 1)
                self.assertIn("repairs the stored value explicitly", err)
                self.assertEqual(self.hold(assignment_id), stored)  # byte for byte what it was
                self.assertEqual(self.owner.get(f"/teacher/assignment/{assignment_id}").status_code, 503)
        self.assertEqual(self.audit_entries(), [])
        code, out, _err = self.cli("hold-list")
        self.assertEqual(code, 1)
        self.assertEqual(out.count("UNREADABLE: it stays held"), len(self.MALFORMED_HOLDS))

    def test_adding_to_a_malformed_hold_keeps_every_byte_of_it(self):
        expected = {"clock_review,": "clock_review,,restore_pending", "mystery": "mystery,restore_pending",
                    "": ",restore_pending", "clock_review,clock_review": "clock_review,clock_review,restore_pending"}
        for stored, after in expected.items():
            with self.subTest(stored=stored):
                assignment_id = self.assignment(self.teacher, lifecycle_hold=stored)
                with closing(sqlite3.connect(str(self.db_path))) as conn:
                    self.assertTrue(manage_lifecycle.add_hold_reason(conn, assignment_id, "restore_pending"))
                    self.assertFalse(manage_lifecycle.add_hold_reason(conn, assignment_id, "restore_pending"))
                    conn.commit()
                self.assertEqual(self.hold(assignment_id), after)  # nothing dropped, nothing tidied
                self.assertFalse(manage_lifecycle.hold_is_readable(after))

    def test_no_reconciliation_command_releases_a_malformed_hold(self):
        malformed = self.assignment(self.teacher)
        readable = self.assignment(self.teacher)
        snapshot = self.snapshot()
        pre = self.restore(snapshot)
        self.assertEqual(self.cli("restore-hold", "--pre-restore-db", pre, writer=True)[0], 0)
        self.execute("UPDATE quiz_assignments SET lifecycle_hold = 'restore_pending,' WHERE id = ?", (malformed,))
        record = self.record_file(self.deadline_event("edit", readable, self.T0, None))

        code, out, err = self.reconcile(pre, snapshot)
        self.assertEqual(code, manage_lifecycle.EXIT_INCOMPLETE, out + err)  # committed, and one stays held
        self.assertIn(f"unreadable hold, left untouched and still held: assignment(s) {malformed}", out)
        self.assertIn("RECOVERY INCOMPLETE: 1 assignment(s) could not be reconciled and stay held", out)
        self.assertEqual((self.hold(malformed), self.hold(readable)), ("restore_pending,", None))

        self.execute("UPDATE quiz_assignments SET lifecycle_hold = 'restore_pending' WHERE id = ?", (readable,))
        code, _out, _err = self.cli("reconcile-partial", "--affected-ids", readable, "--record-file", record,
                                    writer=True)
        self.assertEqual(code, manage_lifecycle.EXIT_UNSUPPORTED)
        self.assertEqual((self.hold(malformed), self.hold(readable)), ("restore_pending,", "restore_pending"))

        own_record = self.record_file(self.deadline_event("edit", malformed, self.T0, None), name="own.txt")
        code, _out, _err = self.reconcile_record(malformed, own_record, "--no-deadline", "--display-timezone",
                                                 "Europe/Sofia", "--not-closed")
        self.assertEqual(code, manage_lifecycle.EXIT_UNSUPPORTED)
        self.assertEqual(self.hold(malformed), "restore_pending,")
        self.assertEqual(self.client().get(self.entry(malformed)).status_code, 503)

    def test_a_restore_carries_an_unreadable_hold_over_instead_of_dropping_it(self):
        assignment_id = self.assignment(self.teacher)
        snapshot = self.snapshot()
        self.execute("UPDATE quiz_assignments SET lifecycle_hold = 'clock_review,' WHERE id = ?", (assignment_id,))
        pre = self.restore(snapshot)
        self.assertEqual(self.cli("restore-hold", "--pre-restore-db", pre, writer=True)[0], 0)
        self.assertEqual(self.hold(assignment_id), "restore_pending,clock_review,")
        self.assertFalse(manage_lifecycle.hold_is_readable(self.hold(assignment_id)))
        self.assertEqual(self.cli("restore-hold", "--pre-restore-db", pre, writer=True)[0], 0)  # idempotent
        self.assertEqual(self.hold(assignment_id), "restore_pending,clock_review,")

    # --- A hold arriving from another database (second Codex review, blocker 3) ---

    MALFORMED_INCOMING = {
        "duplicate tokens": "clock_review,clock_review",
        "a trailing comma": "clock_review,",
        "a leading comma": ",clock_review",
        "an empty token in the middle": "clock_review,,restore_pending",
        "nothing but an empty token": "",
        "an unknown reason": "clock_review,mystery",
        "nothing but an unknown reason": "mystery",
        "a reason of a later phase": "restore_reclean",
        "leading whitespace": " clock_review",
        "trailing whitespace": "clock_review ",
        "whitespace after the comma": "clock_review, restore_pending",
        "a trailing newline": "clock_review\n",
        "the wrong order": "restore_pending,clock_review",
    }

    def test_an_incoming_hold_is_judged_as_it_is_stored_before_anything_is_merged(self):
        for label, incoming in self.MALFORMED_INCOMING.items():
            self.assertFalse(manage_lifecycle.hold_is_readable(incoming), label)
            for stored in (None, "clock_review", "restore_pending", "clock_review,restore_pending", "legacy,legacy"):
                with self.subTest(incoming=label, stored=stored):
                    assignment_id = self.assignment(self.teacher, lifecycle_hold=stored)
                    with closing(sqlite3.connect(str(self.db_path))) as conn:
                        self.assertTrue(manage_lifecycle.carry_hold(conn, assignment_id, incoming))
                        conn.commit()
                        after = self.hold(assignment_id)
                        # Carrying the same text again, any number of times, changes nothing.
                        self.assertFalse(manage_lifecycle.carry_hold(conn, assignment_id, incoming))
                        self.assertFalse(manage_lifecycle.carry_hold(conn, assignment_id, incoming))
                        conn.commit()
                    self.assertEqual(self.hold(assignment_id), after)
                    # The incoming text is there exactly, as one stretch, after what was stored exactly.
                    self.assertEqual(after, incoming if stored is None else f"{stored},{incoming}")
                    # And the value is unreadable from now on, whatever it was: nothing is resolved from it.
                    self.assertFalse(manage_lifecycle.hold_is_readable(after))
                    with closing(sqlite3.connect(str(self.db_path))) as conn:
                        for reason in lifecycle.HOLD_REASONS:
                            with self.assertRaises(manage_lifecycle.Refused):
                                manage_lifecycle.resolve_hold_reason(conn, assignment_id, reason)
                    self.assertEqual(self.owner.get(f"/teacher/assignment/{assignment_id}").status_code, 503)

    def test_a_readable_incoming_hold_is_still_merged_as_a_set(self):
        for stored, incoming, after in (
                (None, "clock_review", "clock_review"),
                ("restore_pending", "clock_review", "clock_review,restore_pending"),
                ("restore_pending", "clock_review,restore_unreconciled",
                 "clock_review,restore_pending,restore_unreconciled"),
                ("clock_review,restore_pending", "restore_pending", "clock_review,restore_pending"),
                ("mystery,", "clock_review", "mystery,,clock_review")):   # added to; the stored bytes are kept
            with self.subTest(stored=stored, incoming=incoming):
                assignment_id = self.assignment(self.teacher, lifecycle_hold=stored)
                with closing(sqlite3.connect(str(self.db_path))) as conn:
                    self.assertEqual(manage_lifecycle.carry_hold(conn, assignment_id, incoming), stored != after)
                    self.assertFalse(manage_lifecycle.carry_hold(conn, assignment_id, incoming))
                    self.assertFalse(manage_lifecycle.carry_hold(conn, assignment_id, None))
                    conn.commit()
                self.assertEqual(self.hold(assignment_id), after)

    def test_a_restore_keeps_every_malformed_incoming_hold_exactly_and_blocking(self):
        ids = {label: self.assignment(self.teacher) for label in self.MALFORMED_INCOMING}
        readable = self.assignment(self.teacher)
        with_its_own = self.assignment(self.teacher, lifecycle_hold="clock_review")
        snapshot = self.snapshot()
        # After the snapshot the live database came to hold these values (it is the incoming source).
        for label, incoming in self.MALFORMED_INCOMING.items():
            self.execute("UPDATE quiz_assignments SET lifecycle_hold = ? WHERE id = ?", (incoming, ids[label]))
        self.execute("UPDATE quiz_assignments SET lifecycle_hold = 'clock_review' WHERE id = ?", (readable,))
        self.execute("UPDATE quiz_assignments SET lifecycle_hold = 'clock_review,clock_review' WHERE id = ?",
                     (with_its_own,))
        pre = self.restore(snapshot)

        expected = {ids[label]: "restore_pending," + incoming for label, incoming in self.MALFORMED_INCOMING.items()}
        expected[readable] = "clock_review,restore_pending"            # a readable one is merged as a set
        expected[with_its_own] = "clock_review,restore_pending,clock_review,clock_review"
        for attempt in range(3):                                       # repeated restore attempts
            with self.subTest(restore_hold_run=attempt + 1):
                code, out, err = self.cli("restore-hold", "--pre-restore-db", pre, writer=True)
                self.assertEqual(code, 0, out + err)
                self.assertEqual({a: self.hold(a) for a in expected}, expected)
        # The reproduction of the review, to the letter: not normalised into something resolvable.
        duplicate = ids["duplicate tokens"]
        self.assertEqual(self.hold(duplicate), "restore_pending,clock_review,clock_review")
        self.assertNotEqual(self.hold(duplicate), "clock_review,restore_pending")
        first = [entry for entry in self.audit_entries() if entry["action"] == "restore_hold"][0]
        self.assertEqual(first["reasons_carried_over"][str(duplicate)], "clock_review,clock_review")
        self.assertEqual(first["reasons_carried_over"][str(ids["a trailing newline"])], "clock_review\n")

        # Blocking, and beyond every ordinary command.
        malformed = sorted(expected.keys() - {readable})
        for assignment_id in malformed:
            for reason in lifecycle.HOLD_REASONS:
                code, _out, err = self.cli("hold-resolve", "--assignment", assignment_id, "--reason", reason,
                                           "--evidence", "преглед", writer=True)
                self.assertEqual(code, 1, err)
        code, out, err = self.reconcile(pre, snapshot)
        self.assertEqual(code, manage_lifecycle.EXIT_INCOMPLETE, out + err)
        self.assertEqual(self.recover(self.T0)[0], 0)
        self.assertIn("unreadable hold, left untouched and still held: assignment(s) "
                      + ", ".join(map(str, malformed)), out)
        self.assertEqual({a: self.hold(a) for a in malformed}, {a: expected[a] for a in malformed})
        self.assertEqual(self.hold(readable), "clock_review")          # reconciled; its own reason stays
        for assignment_id in malformed:
            self.assertEqual(self.client().get(self.entry(assignment_id)).status_code, 503)
        self.assertEqual(self.cli("verify")[0], 1)
        code, out, _err = self.cli("restore-status")
        self.assertEqual(code, manage_lifecycle.EXIT_INCOMPLETE)
        self.assertIn(f"held for the restore: {len(malformed)}; held for another reason only: 1", out)
        self.assertEqual(out.count("UNREADABLE"), len(malformed))

    def test_reconcile_carries_a_malformed_evidence_hold_instead_of_dropping_it(self):
        # The hold step was run without the replaced database, so the text first arrives here.
        assignment_id = self.assignment(self.teacher)
        snapshot = self.snapshot()
        self.execute("UPDATE quiz_assignments SET lifecycle_hold = 'clock_review,clock_review', closed_at = ? "
                     "WHERE id = ?", (ts(self.T0 - HOUR), assignment_id))
        pre = self.restore(snapshot)
        self.assertEqual(self.cli("restore-hold", writer=True)[0], 0)
        for _ in range(2):
            code, out, err = self.reconcile(pre, snapshot)
            self.assertEqual(code, manage_lifecycle.EXIT_INCOMPLETE, out + err)
            self.assertEqual(self.hold(assignment_id), "restore_pending,clock_review,clock_review,restore_unreconciled")
        self.assertIsNone(self.row(assignment_id)["closed_at"])   # nothing else of the row was taken over
        self.assertEqual(self.start(self.client(self.student), assignment_id).status_code, 503)

    def test_a_hold_that_is_not_text_is_neither_rewritten_nor_carried(self):
        stored = self.assignment(self.teacher)
        other = self.assignment(self.teacher)
        self.execute("UPDATE quiz_assignments SET lifecycle_hold = CAST('clock_review' AS BLOB) WHERE id = ?", (stored,))
        kind = lambda a: self.query("SELECT typeof(lifecycle_hold), lifecycle_hold FROM quiz_assignments "  # noqa: E731
                                    "WHERE id = ?", (a,))[0]
        self.assertEqual(tuple(kind(stored)), ("blob", b"clock_review"))
        with closing(sqlite3.connect(str(self.db_path))) as conn:
            self.assertFalse(manage_lifecycle.hold_is_readable(b"clock_review"))
            self.assertFalse(manage_lifecycle.add_hold_reason(conn, stored, "restore_pending"))
            self.assertFalse(manage_lifecycle.carry_hold(conn, stored, "clock_review,"))
            with self.assertRaises(manage_lifecycle.Refused):
                manage_lifecycle.resolve_hold_reason(conn, stored, "clock_review")
            with self.assertRaises(manage_lifecycle.OperatorError):
                manage_lifecycle.carry_hold(conn, other, b"clock_review")   # cannot arrive as exact text
            conn.commit()
        self.assertEqual((tuple(kind(stored)), self.hold(other)), (("blob", b"clock_review"), None))
        self.assertEqual(self.owner.get(f"/teacher/assignment/{stored}").status_code, 503)   # it holds as it is
        self.assertEqual(self.cli("verify")[0], 1)
        self.assertEqual(self.cli("restore-status")[0], manage_lifecycle.EXIT_INCOMPLETE)

        # As the source of a restore, a database with such a value does not qualify at all.
        pre = self.snapshot("with-a-blob.db")
        self.execute("UPDATE quiz_assignments SET lifecycle_hold = NULL")
        code, _out, err = self.cli("restore-hold", "--pre-restore-db", pre, writer=True)
        self.assertEqual(code, 2)
        self.assertIn("is not text", err)
        self.assertEqual((self.hold(stored), self.hold(other), self.clock_row()["state"]), (None, None, "ok"))
        origin = self.snapshot("origin.db")
        self.assertEqual(self.cli("restore-hold", writer=True)[0], 0)
        code, _out, err = self.reconcile(pre, origin)
        self.assertEqual(code, 2)
        self.assertIn("do not qualify as evidence", err)
        self.assertEqual((self.hold(stored), self.hold(other)), ("restore_pending", "restore_pending"))

    def test_l121_hold_resolve_never_releases_a_restore_reason(self):
        assignment_id = self.assignment(self.teacher, lifecycle_hold="restore_pending,restore_unreconciled")
        before = self.lifecycle_columns(assignment_id)
        for reason in ("restore_pending", "restore_unreconciled"):
            code, _out, err = self.cli("hold-resolve", "--assignment", assignment_id, "--reason", reason,
                                       "--evidence", "учителят си спомня", writer=True)
            self.assertEqual(code, 1)
            self.assertIn("resolved only by `reconcile`, from the database the restore replaced", err)
        # With a record, without one, with an empty one: there is no command that takes its word for it.
        state = ("--no-deadline", "--display-timezone", "Europe/Sofia", "--not-closed")
        empty = self.root / "empty.txt"
        empty.write_text("  \n", encoding="utf-8")
        own = self.record_file(self.deadline_event("edit", assignment_id, self.T0, None))
        for record in (own, self.root / "no-such-record.txt", empty):
            self.assertEqual(self.reconcile_record(assignment_id, record, *state)[0], manage_lifecycle.EXIT_UNSUPPORTED)
        self.assertEqual(self.cli("reconcile-record", "--assignment", assignment_id, *state, writer=True)[0],
                         manage_lifecycle.EXIT_UNSUPPORTED)
        self.assertEqual(self.lifecycle_columns(assignment_id), before)  # held, nothing written
        self.assertEqual(self.audit_entries(), [])


class ClockRecoveryTest(OperatorTestCase):
    def test_l112_l114_recovery_holds_what_is_uncertain_and_manufactures_no_closure(self):
        verified, old_mark = self.T0 + HOUR, self.T0 + 10 * HOUR
        passed = self.assignment(self.teacher, deadline_at=self.T0 + timedelta(minutes=30))   # before verified
        uncertain = self.assignment(self.teacher, deadline_at=self.T0 + 5 * HOUR)             # (verified, old mark]
        future = self.assignment(self.teacher, deadline_at=self.T0 + 20 * HOUR)               # only a deadline ahead
        no_deadline = self.assignment(self.teacher)
        closed_before = self.assignment(self.teacher, closed_at=self.T0 - HOUR)
        grace_uncertain = self.assignment(self.teacher, closed_at=verified - timedelta(minutes=5))
        stamped_ahead = self.assignment(self.teacher, closed_at=self.T0 + 3 * HOUR, lifecycle_hold="restore_pending")
        started_ahead = self.assignment(self.teacher)
        self.attempt(started_ahead, self.student, started_at=self.T0 + 2 * HOUR)
        everything = (passed, uncertain, future, no_deadline, closed_before, grace_uncertain, stamped_ahead,
                      started_ahead)
        before = {a: self.lifecycle_columns(a) for a in everything}
        self.incident(verified=verified, old_high_water=old_mark)

        code, out, _err = self.cli("clock-inspect", "--verified-utc", ts(verified))  # read-only preview
        self.assertEqual(code, 0)
        self.assertIn("nothing was changed", out)
        self.assertEqual({a: self.lifecycle_columns(a) for a in everything}, before)

        code, out, err = self.recover(verified)
        self.assertEqual(code, 0, out + err)
        after = {a: self.lifecycle_columns(a) for a in everything}

        # No closure marker of any kind was written, for any assignment.
        for assignment_id in everything:
            for column in (*MARKERS, "closed_by_user_id", "deadline_at", "display_timezone"):
                self.assertEqual(after[assignment_id][column], before[assignment_id][column], (assignment_id, column))
        # Held: the two in doubt by list (b), the two with a factual timestamp ahead by list (a).
        self.assertEqual({a: after[a]["lifecycle_hold"] for a in everything}, {
            passed: None, future: None, no_deadline: None, closed_before: None,
            uncertain: "clock_review", grace_uncertain: "clock_review",
            stamped_ahead: "clock_review,restore_pending",   # added to the reason it already carried
            started_ahead: "clock_review",
        })
        # Only the closed assignment of list (a) gets a retention floor: the verified time.
        self.assertEqual({a: after[a]["retention_base_at"] for a in everything},
                         {**{a: None for a in everything}, stamped_ahead: ts(verified)})
        clock = self.clock_row()
        self.assertEqual((clock["state"], clock["high_water_at"]), ("ok", ts(verified)))  # the mark was lowered
        self.assertEqual(clock["ref_boot_id"], self.clock.boot_id())                        # a new boot anchor

        entry = self.audit_entries()[-1]
        self.assertEqual((entry["action"], entry["operator"], entry["new_high_water"]),
                         ("clock_recover", "тест оператор", ts(verified)))
        self.assertEqual(sorted(entry["held"]), sorted(map(str, (uncertain, grace_uncertain, stamped_ahead,
                                                                 started_ahead))))

        # A deadline that really passed is recorded by ordinary observation, with that instant.
        self.clock.advance(120)
        self.client().get(self.entry(passed))
        self.assertEqual(self.row(passed)["expiry_observed_at"], ts(verified + timedelta(seconds=120)))
        self.assertNotEqual(self.row(passed)["expiry_observed_at"], ts(old_mark))
        # The rest serve again; the held ones do not.
        self.assertEqual(self.client().get(self.entry(future)).status_code, 200)
        self.assertEqual(self.client().get(self.entry(uncertain)).status_code, 503)

    def test_l115_a_verified_time_that_disagrees_with_the_clock_is_rejected(self):
        assignment_id = self.assignment(self.teacher, deadline_at=self.T0 + 5 * HOUR)
        local = self.T0 + HOUR
        self.incident(verified=local, old_high_water=self.T0 + 10 * HOUR)
        before = (tuple(self.clock_row()), self.lifecycle_columns(assignment_id))
        # The local clock may be at most 5 s behind the verified time and at most 60 s ahead of it.
        refused = {local + timedelta(seconds=6): "6 s behind", local + timedelta(minutes=10): "600 s behind",
                   local - timedelta(seconds=61): "61 s ahead", local - timedelta(hours=2): "7200 s ahead"}
        for wrong, described in refused.items():
            with self.subTest(verified=wrong):
                code, _out, err = self.recover(wrong)
                self.assertEqual(code, 1)
                self.assertIn(f"the local clock is {described}", err)
                self.assertIn("correct the clock first; the clock state is unchanged", err)
                inspect = self.cli("clock-inspect", "--verified-utc", ts(wrong))[1]
                self.assertIn("clock-recover would be REFUSED", inspect)
        self.assertEqual((tuple(self.clock_row()), self.lifecycle_columns(assignment_id)), before)
        self.assertEqual(self.audit_entries(), [])
        self.assertEqual(self.start(self.client(self.student), assignment_id).status_code, 503)  # still suspect

    def test_recovery_with_the_local_clock_a_minute_behind_is_refused_codex_counterexample(self):
        """Local 09:00:00, verified 09:01:00, deadline 09:00:30, old high-water 08:59:30.

        Recovery used to succeed without holding the assignment, and a start was then
        admitted although the verified deadline had passed. Now nothing leaves the
        suspect state until the local clock agrees with the verified time, and from
        then on the verified time is the floor of lifecycle time.
        """
        nine = datetime(2026, 3, 2, 9, 0, 0)
        verified, deadline = nine + timedelta(minutes=1), nine + timedelta(seconds=30)
        assignment_id = self.assignment(self.teacher, deadline_at=deadline)
        self.incident(verified=verified, old_high_water=nine - timedelta(seconds=30), local=nine)
        before = (tuple(self.clock_row()), self.lifecycle_columns(assignment_id))

        code, _out, err = self.recover(verified)
        self.assertEqual(code, 1)
        self.assertIn("the local clock is 60 s behind the verified time", err)
        self.assertEqual((tuple(self.clock_row()), self.lifecycle_columns(assignment_id)), before)
        refused = self.start(self.client(self.student), assignment_id)
        self.assertEqual(refused.status_code, 503)  # lifecycle writes are still refused
        self.assertEqual(self.attempts(assignment_id), [])

        self.clock.freeze(verified + timedelta(seconds=2))  # the operator corrected the clock
        code, out, err = self.recover(verified)
        self.assertEqual(code, 0, out + err)
        clock = self.clock_row()
        self.assertEqual((clock["state"], clock["high_water_at"]), ("ok", ts(verified)))
        # The verified deadline has passed: no start, whichever second the request arrives in.
        self.assertEqual(self.start(self.client(self.student), assignment_id).status_code, 409)
        self.assertEqual(self.attempts(assignment_id), [])
        row = self.row(assignment_id)
        self.assertEqual((row["closed_at"], row["expiry_observed_at"]), (None, ts(verified + timedelta(seconds=2))))

    def test_with_the_local_clock_slightly_behind_the_verified_time_is_the_floor(self):
        nine = datetime(2026, 3, 2, 9, 0, 0)
        verified, local = nine + timedelta(minutes=1), nine + timedelta(seconds=57)
        passed = self.assignment(self.teacher, deadline_at=nine + timedelta(seconds=59))  # between local and verified
        later = self.assignment(self.teacher, deadline_at=nine + timedelta(minutes=30))
        self.incident(verified=verified, old_high_water=nine - timedelta(seconds=30), local=local)

        code, out, err = self.recover(verified)
        self.assertEqual(code, 0, out + err)
        clock = self.clock_row()
        self.assertEqual((clock["state"], clock["high_water_at"]), ("ok", ts(verified)))  # verified, not local
        self.assertEqual(clock["ref_wall_at"], ts(local))  # the anchor ties the local reading to boot time
        self.assertEqual((self.hold(passed), self.hold(later)), (None, None))

        self.clock.advance(1)  # the local clock reads 09:00:58, before that deadline
        self.assertLess(self.clock.wall(), nine + timedelta(seconds=59))
        self.assertEqual(self.start(self.client(self.student), passed).status_code, 409)
        self.assertEqual(self.attempts(passed), [])
        self.assertEqual(self.row(passed)["expiry_observed_at"], ts(verified))  # lifecycle time is the floor
        started = self.start(self.client(self.student), later)
        self.assertEqual(started.status_code, 302)
        self.assertEqual(self.attempt_row(self.attempt_id_of(started))["started_at"], ts(verified))
        self.assertEqual(self.clock_row()["state"], "ok")

    def test_with_the_local_clock_ahead_what_lies_between_the_two_readings_is_held(self):
        nine = datetime(2026, 3, 2, 9, 0, 0)
        verified, local = nine + timedelta(minutes=1), nine + timedelta(minutes=1, seconds=30)
        between = self.assignment(self.teacher, deadline_at=verified + timedelta(seconds=10))
        grace_between = self.assignment(self.teacher, closed_at=verified + timedelta(seconds=20)
                                        - timedelta(seconds=lifecycle.GRACE_SECONDS))
        before_verified = self.assignment(self.teacher, deadline_at=verified - timedelta(seconds=10))
        after_local = self.assignment(self.teacher, deadline_at=local + timedelta(minutes=5))
        everything = (between, grace_between, before_verified, after_local)
        markers = {a: self.markers(a) for a in everything}
        self.incident(verified=verified, old_high_water=nine - timedelta(minutes=5), local=local)

        code, out, err = self.recover(verified)
        self.assertEqual(code, 0, out + err)
        # By the verified time these two have not yet passed; by the local clock they have. Uncertain: held.
        self.assertEqual([self.hold(a) for a in everything], ["clock_review", "clock_review", None, None])
        self.assertEqual({a: self.markers(a) for a in everything}, markers)  # no closure was manufactured
        self.assertEqual(self.clock_row()["high_water_at"], ts(verified))
        self.assertEqual(self.start(self.client(self.student), between).status_code, 503)
        self.assertEqual(self.start(self.client(self.account()), before_verified).status_code, 409)
        self.assertEqual(self.start(self.client(self.account()), after_local).status_code, 302)
        entry = self.audit_entries()[-1]
        self.assertEqual((entry["verified_utc"], entry["local_clock"], entry["new_high_water"]),
                         (ts(verified), ts(local), ts(verified)))

    def test_existing_holds_and_floors_survive_a_recovery(self):
        verified = self.T0 + HOUR
        reviewed = self.assignment(self.teacher, lifecycle_hold="clock_review",
                                   deadline_at=self.T0 + 5 * HOUR)                       # list (b) again
        both = self.assignment(self.teacher, lifecycle_hold="clock_review,restore_pending")  # not listed at all
        restored = self.assignment(self.teacher, lifecycle_hold="restore_unreconciled",
                                   closed_at=self.T0 + 2 * HOUR, retention_base_at=self.T0 + 9 * HOUR)  # list (a)
        self.incident(verified=verified, old_high_water=self.T0 + 10 * HOUR)
        for _ in range(2):  # a second run changes nothing either
            self.assertEqual(self.recover(verified)[0], 0)
            self.assertEqual([self.hold(a) for a in (reviewed, both, restored)],
                             ["clock_review", "clock_review,restore_pending", "clock_review,restore_unreconciled"])
            # A floor is only ever raised: the later one that was already there stays.
            self.assertEqual(self.row(restored)["retention_base_at"], ts(self.T0 + 9 * HOUR))
            self.assertEqual(self.row(restored)["closed_at"], ts(self.T0 + 2 * HOUR))
        first, second = self.audit_entries()[-2:]
        self.assertTrue(first["held"][str(reviewed)]["added"] is False)  # it was already held for it
        self.assertEqual(first["held"][str(restored)]["added"], True)
        self.assertEqual(second["held"][str(restored)]["added"], False)

    def test_a_deadline_crossed_during_the_suspect_interval_stays_closed_after_recovery(self):
        nine = datetime(2026, 3, 2, 9, 0, 0)
        deadline = nine + timedelta(minutes=20)
        crossed = self.assignment(self.teacher, deadline_at=deadline)
        admitted_late = self.assignment(self.teacher, deadline_at=deadline)
        # A start that was recorded while the clock was wrong.
        late_attempt = self.attempt(admitted_late, self.account(), started_at=deadline - timedelta(seconds=5))
        verified = nine + timedelta(minutes=40)
        self.incident(verified=verified, old_high_water=nine + timedelta(minutes=10))
        code, out, err = self.recover(verified, "--suspect-from", ts(nine + timedelta(minutes=15)),
                                      "--suspect-to", ts(verified))
        self.assertEqual(code, 0, out + err)
        # The deadline lies at or before the verified time: it has really passed. Nothing is
        # stamped by the recovery; the next request observes it, and no start is admitted.
        self.assertEqual((self.hold(crossed), self.row(crossed)["expiry_observed_at"]), (None, None))
        self.clock.advance(30)
        self.assertEqual(self.start(self.client(self.student), crossed).status_code, 409)
        self.assertEqual(self.attempts(crossed), [])
        self.assertEqual(self.row(crossed)["expiry_observed_at"], ts(verified + timedelta(seconds=30)))
        # The attempt that the wrong clock let in carries a start inside the suspect interval: held.
        self.assertEqual(self.hold(admitted_late), "clock_review")
        self.assertIsNone(self.attempt_row(late_attempt)["submitted_at"])
        self.assertEqual(self.start(self.client(self.account()), admitted_late).status_code, 503)

    def test_l75_l76_after_recovery_a_closed_assignment_is_startable_in_neither_case(self):
        closed_at = self.T0 + 2 * HOUR
        for verified, expect_hold in ((closed_at + timedelta(minutes=30), None), (closed_at - HOUR, "clock_review")):
            with self.subTest(verified=verified):
                manual = self.assignment(self.teacher, closed_at=closed_at, closed_by_user_id=self.teacher["id"],
                                         submissions_closed_at=closed_at + GRACE)
                expired = self.assignment(self.teacher, deadline_at=closed_at - HOUR, expiry_observed_at=closed_at,
                                          submissions_closed_at=closed_at + GRACE)
                markers = (self.markers(manual), self.markers(expired))
                self.incident(verified=verified, old_high_water=closed_at + 5 * HOUR, reason="clock_regression")
                self.assertEqual(self.recover(verified)[0], 0)
                self.assertEqual((self.hold(manual), self.hold(expired)), (expect_hold, expect_hold))
                self.assertEqual((self.markers(manual), self.markers(expired)), markers)
                expected = 503 if expect_hold else 409  # held, or refused as closed by the marker
                for assignment_id in (manual, expired):
                    self.assertEqual(self.start(self.client(self.account()), assignment_id).status_code, expected)
                    self.assertEqual(self.attempts(assignment_id), [])

    def test_l113_markers_survive_recovery_and_a_valid_grace_submission_is_accepted(self):
        noon = datetime(2026, 3, 2, 12, 0, 0)
        assignment_id = self.assignment(self.teacher, closed_at=noon, closed_by_user_id=self.teacher["id"])
        inside = self.assignment(self.teacher, closed_at=noon + timedelta(minutes=3))   # inside the suspect interval
        first_account, second_account = self.account(), self.account()
        first = self.attempt(assignment_id, first_account, started_at=noon - timedelta(minutes=1))
        second = self.attempt(assignment_id, second_account, started_at=noon - timedelta(minutes=1))
        markers = (self.markers(assignment_id), self.markers(inside))

        verified = noon + timedelta(minutes=5)
        self.incident(verified=verified, old_high_water=noon + timedelta(minutes=6))
        code, out, err = self.recover(verified, "--suspect-from", ts(noon + timedelta(minutes=2)),
                                      "--suspect-to", ts(noon + timedelta(minutes=6)))
        self.assertEqual(code, 0, out + err)
        self.assertEqual((self.markers(assignment_id), self.markers(inside)), markers)
        self.assertEqual((self.hold(assignment_id), self.hold(inside)), (None, "clock_review"))

        # A. No new start. B. The in-flight attempt submits at 12:05; another at exactly 12:15 is closed.
        self.assertEqual(self.start(self.client(self.account()), assignment_id).status_code, 409)
        self.assertEqual(self.submit(self.client(first_account), first).status_code, 302)
        self.assertEqual(self.attempt_row(first)["submitted_at"], ts(verified))
        self.clock.set(noon + GRACE)
        self.assertEqual(self.submit(self.client(second_account), second).status_code, 409)
        resent = self.submit(self.client(first_account), first)
        self.assertEqual((resent.status_code, resent.headers["Location"]), (302, f"/quiz/attempt/{first}/result"))
        # C. A hold refuses everything of the assignment whose marker lies inside the suspect interval.
        self.assertEqual(self.client().get(self.entry(inside)).status_code, 503)

    def test_an_expiry_observed_by_an_accepted_grace_write_survives_recovery_and_hold_resolution(self):
        """Codex finding 1, end to end. A submission accepted in the grace window used to leave
        ``expiry_observed_at`` NULL. After a clock recovery that verified a time before the deadline
        and the resolution of the hold, the assignment computed as open and accepted a new start."""
        deadline = self.T0 + HOUR
        observed = deadline + timedelta(minutes=5)
        for kind in ("submission", "upload"):
            with self.subTest(accepted=kind):
                assignment_id = self.assignment(self.teacher, deadline_at=deadline)
                account = self.account()
                attempt_id = self.attempt(assignment_id, account, started_at=self.T0 - HOUR)
                self.clock.freeze(observed)
                set_lifecycle_clock_row(self.db_path, high_water=observed)
                if kind == "submission":
                    self.assertEqual(self.submit(self.client(account), attempt_id).status_code, 302)
                else:
                    task_id = self.query(
                        "SELECT t.id FROM exam_tasks t JOIN exams e ON e.id = t.exam_id WHERE e.year = 2025 "
                        "AND e.session = 'may' AND e.variant = 2 AND t.task_number = 26")[0][0]
                    uploaded = self.client(account).post(
                        f"/quiz/attempt/{attempt_id}/practical/may_2025_v2/task/{task_id}/upload",
                        data={"files": (io.BytesIO(b"xlsx"), "answer.xlsx")},
                        content_type="multipart/form-data", headers=SAME_ORIGIN)
                    self.assertIn("uploaded=1", uploaded.headers["Location"])
                self.assertEqual(self.row(assignment_id)["expiry_observed_at"], ts(observed))

                # The clock had run ahead: the verified time is before the deadline.
                verified = deadline - timedelta(minutes=30)
                self.incident(verified=verified, old_high_water=observed)
                self.assertEqual(self.recover(verified)[0], 0)
                self.assertEqual(self.hold(assignment_id), "clock_review")  # a marker after the verified time
                self.assertEqual(self.row(assignment_id)["retention_base_at"], ts(verified))
                newcomer = self.client(self.account())
                self.assertEqual(self.start(newcomer, assignment_id).status_code, 503)

                resolve = ("hold-resolve", "--assignment", assignment_id, "--reason", "clock_review",
                           "--evidence", "прегледано")
                self.assertEqual(self.cli(*resolve, writer=True)[0], 1)  # not before time has passed the marker
                self.clock.set(observed + timedelta(minutes=1))
                self.assertEqual(self.cli(*resolve, writer=True)[0], 0)
                self.assertIsNone(self.hold(assignment_id))
                # Released, and closed for good: the persisted observation refuses every new start.
                self.assertEqual(self.start(newcomer, assignment_id).status_code, 409)
                self.assertEqual(len(self.attempts(assignment_id)), 1)
                self.assertEqual(self.row(assignment_id)["expiry_observed_at"], ts(observed))

    def test_l132_an_unobserved_expiry_is_held_then_open_again_after_review(self):
        deadline = self.T0 + 5 * HOUR
        assignment_id = self.assignment(self.teacher, deadline_at=deadline)
        before = self.lifecycle_columns(assignment_id)
        self.incident(verified=self.T0 + HOUR, old_high_water=self.T0 + 10 * HOUR)
        self.recover(self.T0 + HOUR)
        self.assertEqual(self.lifecycle_columns(assignment_id), {**before, "lifecycle_hold": "clock_review"})
        self.assertEqual(self.start(self.client(self.student), assignment_id).status_code, 503)

        code, out, err = self.cli("hold-resolve", "--assignment", assignment_id, "--reason", "clock_review",
                                  "--evidence", "срокът е проверен спрямо независим часовник", writer=True)
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.lifecycle_columns(assignment_id), before)  # resolving changes no other column
        # Not a reopening: no closure of it was ever persisted. It is open until its stored deadline.
        self.assertEqual(self.start(self.client(self.student), assignment_id).status_code, 302)
        self.clock.set(deadline)
        self.assertEqual(self.start(self.client(self.account()), assignment_id).status_code, 409)
        entry = self.audit_entries()[-1]
        self.assertEqual((entry["action"], entry["reason"], entry["remaining"]), ("hold_resolve", "clock_review", None))
        self.assertIn("независим часовник", entry["evidence"])

    def test_l133_a_marker_stamped_ahead_stays_held_until_time_has_passed_it(self):
        verified = self.T0 + HOUR
        closed_at = verified + HOUR  # stamped an hour ahead during the incident
        assignment_id = self.assignment(self.teacher, closed_at=closed_at, closed_by_user_id=self.teacher["id"])
        account = self.account()
        attempt_id = self.attempt(assignment_id, account, started_at=self.T0 - HOUR)
        self.incident(verified=verified, old_high_water=closed_at + HOUR)
        self.recover(verified)
        self.assertEqual(self.hold(assignment_id), "clock_review")
        self.assertEqual(self.row(assignment_id)["retention_base_at"], ts(verified))

        resolve = ("hold-resolve", "--assignment", assignment_id, "--reason", "clock_review", "--evidence", "преглед")
        code, _out, err = self.cli(*resolve, writer=True)
        self.assertEqual(code, 1)
        self.assertIn("stays held", err)
        self.assertEqual(self.submit(self.client(account), attempt_id).status_code, 503)  # no grace until then
        self.assertEqual(self.hold(assignment_id), "clock_review")

        self.clock.set(closed_at + timedelta(minutes=1))
        self.assertEqual(self.cli(*resolve, writer=True)[0], 0)
        self.assertIsNone(self.hold(assignment_id))
        self.assertEqual(self.row(assignment_id)["closed_at"], ts(closed_at))  # never changed
        # The ordinary window from closed_at, at most G: not one that lasted until that future arrived.
        self.clock.set(closed_at + GRACE)
        self.assertEqual(self.submit(self.client(account), attempt_id).status_code, 409)

    def test_recovery_repairs_a_missing_clock_row_and_assumes_nothing_about_the_old_mark(self):
        ahead = self.assignment(self.teacher, deadline_at=self.T0 + 30 * 24 * HOUR)
        passed = self.assignment(self.teacher, deadline_at=self.T0 - HOUR)
        self.execute("DELETE FROM lifecycle_clock")
        self.assertEqual(self.start(self.client(self.student), ahead).status_code, 503)
        code, out, err = self.recover(self.T0)
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.clock_row()["state"], "ok")
        # With an unknown old mark every deadline after the verified time is in doubt.
        self.assertEqual((self.hold(ahead), self.hold(passed)), ("clock_review", None))
        self.assertEqual(self.row(ahead)["expiry_observed_at"], None)


class RestoreTest(OperatorTestCase):
    def test_l119_after_restore_hold_nothing_is_served(self):
        plain = self.assignment(self.teacher)
        reviewed = self.assignment(self.teacher, lifecycle_hold="clock_review")
        attempt_id = self.attempt(plain, self.student, started_at=self.T0 - HOUR)
        pre = self.restore(self.snapshot())
        code, out, err = self.cli("restore-hold", "--pre-restore-db", pre, writer=True)
        self.assertEqual(code, 0, out + err)

        self.assertEqual((self.hold(plain), self.hold(reviewed)), ("restore_pending", "clock_review,restore_pending"))
        clock = self.clock_row()
        self.assertEqual((clock["state"], clock["state_reason"]), ("suspect", lifecycle.CLOCK_REASON_RESTORE))
        student = self.client(self.student)
        for response in (self.client().get(self.entry(plain)), student.get(f"/quiz/attempt/{attempt_id}"),
                         self.submit(student, attempt_id), self.owner.get(f"/teacher/assignment/{plain}"),
                         self.owner.get(f"/teacher/assignment/{plain}/results"),
                         self.owner.post(f"/teacher/assignment/{plain}/close", headers=SAME_ORIGIN),
                         self.owner.post(f"/teacher/assignment/{plain}/duplicate", data=self.no_deadline_form(),
                                         headers=SAME_ORIGIN)):
            self.assertEqual(response.status_code, 503)
        self.assertEqual(self.client().get("/healthz").status_code, 503)
        # Running it again changes nothing.
        before = (self.hold(plain), self.hold(reviewed), tuple(self.clock_row()))
        self.assertEqual(self.cli("restore-hold", "--pre-restore-db", pre, writer=True)[0], 0)
        self.assertEqual((self.hold(plain), self.hold(reviewed), tuple(self.clock_row())), before)

    def test_l135_l136_holds_and_a_clock_incident_from_after_the_snapshot_are_carried_over(self):
        first = self.assignment(self.teacher)
        second = self.assignment(self.teacher)
        snapshot = self.snapshot()
        # After the snapshot: two holds were added and the clock became suspect.
        self.execute("UPDATE quiz_assignments SET lifecycle_hold = 'clock_review' WHERE id = ?", (first,))
        self.execute("UPDATE quiz_assignments SET lifecycle_hold = 'restore_unreconciled' WHERE id = ?", (second,))
        set_lifecycle_clock_row(self.db_path, high_water=self.T0 + 9 * HOUR, state="suspect",
                                reason="clock_regression", changed_at=self.T0 + 3 * HOUR)
        pre = self.restore(snapshot)
        self.assertEqual((self.hold(first), self.clock_row()["state"]), (None, "ok"))  # the restored copy is older

        self.assertEqual(self.cli("restore-hold", "--pre-restore-db", pre, writer=True)[0], 0)
        self.assertEqual((self.hold(first), self.hold(second)),
                         ("clock_review,restore_pending", "restore_pending,restore_unreconciled"))
        clock = self.clock_row()
        self.assertEqual((clock["state"], clock["state_reason"], clock["state_changed_at"], clock["high_water_at"]),
                         ("suspect", "clock_regression", ts(self.T0 + 3 * HOUR), ts(self.T0 + 9 * HOUR)))
        self.assertEqual(self.start(self.client(self.student), first).status_code, 503)

    def test_l138_restore_without_a_readable_pre_restore_database_needs_nothing_from_later_phases(self):
        assignment_id = self.assignment(self.teacher)
        tables = {row[0] for row in self.query("SELECT name FROM sqlite_master WHERE type = 'table'")}
        self.assertFalse({"database_identity"} & tables)
        columns = {row[1] for row in self.query("PRAGMA table_info(quiz_assignments)")}
        self.assertFalse({"details_cleaned_at", "closure_upper_bound_at"} & columns)

        self.assertEqual(self.cli("restore-hold", writer=True)[0], 0)  # no --pre-restore-db
        self.assertEqual(self.hold(assignment_id), "restore_pending")
        code, _out, err = self.cli("restore-hold", "--pre-restore-db", self.root / "gone.db", writer=True)
        self.assertEqual(code, 2)
        self.assertEqual(self.recover(self.T0)[0], 0)
        self.assertEqual(self.cli("verify")[0], 0)
        # Without evidence every assignment stays held (L120, second case).
        self.assertEqual(self.hold(assignment_id), "restore_pending")
        self.assertEqual(self.client().get(self.entry(assignment_id)).status_code, 503)

    def test_l98_l134_evidence_a_restores_the_later_state_in_both_directions(self):
        jan = lambda day: datetime(2026, 1, day, 22, 0, 0)  # noqa: E731
        extended = self.assignment(self.teacher, deadline_at=jan(5))
        reviewed = self.assignment(self.teacher, deadline_at=jan(5), lifecycle_hold="clock_review")
        shortened = self.assignment(self.teacher, deadline_at=jan(30), retention_base_at=jan(2))
        untouched = self.assignment(self.teacher)
        snapshot = self.snapshot()
        # After the snapshot: extended to 30 January, then closed manually on 10 January.
        closed = datetime(2026, 1, 10, 12, 0, 0)
        for assignment_id in (extended, reviewed):
            self.execute("UPDATE quiz_assignments SET deadline_at = ?, closed_at = ?, closed_by_user_id = ? "
                         "WHERE id = ?", (ts(jan(30)), ts(closed), self.teacher["id"], assignment_id))
        # A reduction is lost by a restore just the same; and a floor was raised.
        self.execute("UPDATE quiz_assignments SET deadline_at = ?, display_timezone = 'America/New_York', "
                     "retention_base_at = ? WHERE id = ?", (ts(jan(8)), ts(jan(20)), shortened))
        pre = self.restore(snapshot)
        self.assertEqual(self.row(extended)["deadline_at"], ts(jan(5)))  # what the restored row alone says

        self.cli("restore-hold", "--pre-restore-db", pre, writer=True)
        code, out, err = self.reconcile(pre, snapshot, "възстановяване заради грешен ъпдейт на друга таблица")
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.clock_row()["state"], "suspect")   # nothing is served yet:
        self.assertEqual(self.owner.get(f"/teacher/assignment/{untouched}").status_code, 200)   # (pages are)
        self.assertEqual(self.start(self.client(self.account()), untouched).status_code, 503)   # the clock is next
        self.assertEqual(self.recover(self.T0)[0], 0)

        row = self.row(extended)
        self.assertEqual((row["deadline_at"], row["closed_at"], row["closed_by_user_id"], row["lifecycle_hold"]),
                         (ts(jan(30)), ts(closed), self.teacher["id"], None))
        with closing(web_app.quiz_db()) as conn:
            view = lifecycle.evaluate(lifecycle.fetch_lifecycle_row(conn, extended), self.T0)
        self.assertEqual(view.effective_closure, closed)  # 10 January, not 5 January
        # L134: only restore_pending is resolved; the reason it already carried is still there.
        self.assertEqual(self.hold(reviewed), "clock_review")
        self.assertEqual(self.row(reviewed)["closed_at"], ts(closed))
        self.assertEqual(self.owner.get(f"/teacher/assignment/{reviewed}").status_code, 503)
        reduced = self.row(shortened)
        self.assertEqual((reduced["deadline_at"], reduced["display_timezone"], reduced["retention_base_at"],
                          reduced["lifecycle_hold"]), (ts(jan(8)), "America/New_York", ts(jan(20)), None))
        self.assertIsNone(self.hold(untouched))
        self.assertEqual(self.owner.get(f"/teacher/assignment/{extended}").status_code, 200)

        (entry,) = [entry for entry in self.audit_entries() if entry["action"] == "reconcile"]
        self.assertEqual(entry["evidence"], "pre_restore_database")
        self.assertEqual(sorted(entry["reconciled"]), sorted([extended, reviewed, shortened, untouched]))
        self.assertIn("грешен ъпдейт", entry["evidence_note"])
        self.assertEqual(entry["snapshot_db"], str(snapshot))
        self.assertIn("its lifecycle clock ran to", entry["evidence_is_later_because"])
        self.assertEqual([entry["action"] for entry in self.audit_entries()],
                         ["restore_hold", "reconcile", "clock_recover"])

    def test_l122_l137_contradictory_evidence_leaves_the_assignment_held_and_untouched(self):
        marker_only_here = self.assignment(self.teacher, deadline_at=self.T0 - 5 * HOUR,
                                           expiry_observed_at=self.T0 - 4 * HOUR)
        different_value = self.assignment(self.teacher, closed_at=self.T0 - 3 * HOUR)
        deadline_moved = self.assignment(self.teacher, deadline_at=self.T0 - 5 * HOUR,
                                         expiry_observed_at=self.T0 - 4 * HOUR)
        floor = self.assignment(self.teacher, retention_base_at=self.T0)
        snapshot = self.snapshot()
        # "Evidence" that would clear a marker, change one, or move the deadline of a marked row.
        self.execute("UPDATE quiz_assignments SET expiry_observed_at = NULL WHERE id = ?", (marker_only_here,))
        self.execute("UPDATE quiz_assignments SET closed_at = ? WHERE id = ?", (ts(self.T0 - 2 * HOUR), different_value))
        self.execute("UPDATE quiz_assignments SET deadline_at = ? WHERE id = ?", (ts(self.T0 + 50 * HOUR), deadline_moved))
        self.execute("UPDATE quiz_assignments SET retention_base_at = ? WHERE id = ?", (ts(self.T0 - 9 * HOUR), floor))
        pre = self.restore(snapshot)
        restored = {a: self.lifecycle_columns(a) for a in (marker_only_here, different_value, deadline_moved, floor)}

        self.cli("restore-hold", writer=True)
        code, out, err = self.reconcile(pre, snapshot)
        # Committed, and not a success: three assignments stay held.
        self.assertEqual(code, manage_lifecycle.EXIT_INCOMPLETE, out + err)
        self.assertIn("held with restore_unreconciled: 3", out)
        self.assertIn("RECOVERY INCOMPLETE: 3 assignment(s) could not be reconciled and stay held", out)
        for assignment_id in (marker_only_here, different_value, deadline_moved):
            self.assertEqual(self.lifecycle_columns(assignment_id),
                             {**restored[assignment_id], "lifecycle_hold": "restore_pending,restore_unreconciled"})
        # The retention floor is never lowered; that assignment reconciles.
        self.assertEqual(self.lifecycle_columns(floor), restored[floor])

    def test_evidence_must_qualify_before_anything_is_reconciled(self):
        assignment_id = self.assignment(self.teacher)
        snapshot = self.snapshot()
        self.execute("UPDATE quiz_assignments SET closed_at = '2026-01-10T12:00:00' WHERE id = ?", (assignment_id,))
        pre = self.restore(snapshot)
        self.cli("restore-hold", writer=True)
        code, _out, err = self.reconcile(pre, snapshot, "x")
        self.assertEqual(code, 2)
        self.assertIn("do not qualify as evidence", err)
        self.assertEqual(self.hold(assignment_id), "restore_pending")
        garbage = self.root / "garbage.db"
        garbage.write_bytes(b"not a database" * 100)
        self.assertEqual(self.reconcile(garbage, snapshot, "x")[0], 2)
        self.assertEqual(self.reconcile(pre, garbage, "x")[0], 2)
        self.assertEqual(self.reconcile(pre, snapshot, " ")[0], 2)
        with self.assertRaises(SystemExit):   # the snapshot is not optional
            self.cli("reconcile", "--pre-restore-db", pre, "--evidence-note", "x", writer=True)
        self.assertEqual(self.hold(assignment_id), "restore_pending")

    # --- Without the replaced database nothing is released (second Codex review, blockers 1 and 2).
    # Application logs are diagnostic. The two commands that read them are refused whatever they are
    # given (exit 4), and what each of them used to release stays held.

    STATE_NONE = ("--no-deadline", "--display-timezone", "Europe/Sofia", "--not-closed")

    def held_without_evidence(self):
        """restore-hold without the replaced database, then a confirmed clock: every assignment is
        held with restore_pending and nothing else stands between it and being served."""
        self.assertEqual(self.cli("restore-hold", writer=True)[0], 0)
        self.assertEqual(self.recover(self.clock.wall())[0], 0)

    def test_l120_with_incomplete_evidence_nothing_is_released(self):
        ids = [self.assignment(self.teacher) for _ in range(4)]
        self.held_without_evidence()
        before = {a: self.lifecycle_columns(a) for a in ids}
        entries = len(self.audit_entries())
        # "The application log since the snapshot", complete as far as anybody can tell.
        record = self.record_file(
            self.deadline_event("edit", ids[1], self.T0, self.T0 + 50 * HOUR),
            self.close_event(ids[2], self.T0, self.teacher["id"]),
            self.deadline_event("create", 9001, self.T0, None))
        for affected in (ids[1:3], ids[1:2], ids, [999]):
            with self.subTest(affected=affected):
                code, out, err = self.cli("reconcile-partial", "--affected-ids", ",".join(map(str, affected)),
                                          "--record-file", record, writer=True)
                self.assertEqual((code, out), (manage_lifecycle.EXIT_UNSUPPORTED, ""))
                self.assertIn("does not mention is not thereby unaffected", err)
        # Neither released nor marked: every assignment is exactly as restore-hold left it.
        self.assertEqual({a: self.lifecycle_columns(a) for a in ids}, before)
        self.assertEqual({self.hold(a) for a in ids}, {"restore_pending"})
        self.assertEqual(len(self.audit_entries()), entries)
        self.assertEqual([self.client().get(self.entry(a)).status_code for a in ids], [503] * 4)

    def test_only_the_edit_is_in_the_supplied_log_and_the_lost_closure_stays_held(self):
        """The first reproduction of the review. A deadline is edited, the assignment is then
        closed, an earlier snapshot in which it is open is restored, and only the log line of the
        edit is supplied. `reconcile-record --not-closed` returned success, cleared the hold, and
        the student's start was answered with 302."""
        assignment_id = self.assignment(self.teacher, deadline_at=self.T0 + 24 * HOUR)
        snapshot = self.snapshot()
        with self.assertLogs("learnpilot.lifecycle", level="INFO") as logs:
            self.clock.advance(60)
            edited = self.owner.post(f"/teacher/assignment/{assignment_id}/edit", data={
                "title_bg": "Удължен", **self.deadline_form("2026-03-20")}, headers=SAME_ORIGIN)
            self.clock.advance(60)
            closed = self.owner.post(f"/teacher/assignment/{assignment_id}/close", headers=SAME_ORIGIN)
        self.assertEqual((edited.status_code, closed.status_code), (302, 302))
        truth = self.lifecycle_columns(assignment_id)
        self.assertIsNotNone(truth["closed_at"])
        edit_lines = [line for line in logs.output if "lifecycle event=edit" in line]
        self.assertEqual((len(edit_lines), len(logs.output)), (1, 2))

        self.restore(snapshot)
        self.held_without_evidence()
        held = self.lifecycle_columns(assignment_id)
        self.assertEqual((held["closed_at"], held["lifecycle_hold"]), (None, "restore_pending"))
        entries = len(self.audit_entries())

        stated = ("--deadline-at", truth["deadline_at"], "--display-timezone", truth["display_timezone"],
                  "--not-closed")
        for label, lines in (("only the edit", edit_lines), ("the whole log", logs.output)):
            with self.subTest(record=label):
                code, out, err = self.reconcile_record(assignment_id, self.record_file(*lines), *stated)
                self.assertEqual((code, out), (manage_lifecycle.EXIT_UNSUPPORTED, ""))
                self.assertIn("does not establish that the assignment was not closed", err)
                self.assertEqual(self.lifecycle_columns(assignment_id), held)
        self.assertEqual(len(self.audit_entries()), entries)
        # The unsafe outcome was a 302 here: a new attempt on an assignment that had been closed.
        self.assertEqual(self.start(self.client(self.student), assignment_id).status_code, 503)
        self.assertEqual(self.attempts(assignment_id), [])
        self.assertEqual(self.cli("restore-status")[0], manage_lifecycle.EXIT_INCOMPLETE)

    def test_an_assignment_the_supplied_log_leaves_out_is_not_thereby_unaffected(self):
        """The second reproduction. The closure of another assignment is left out of the log
        given to `reconcile-partial`; that assignment was classified as unaffected and released."""
        edited = self.assignment(self.teacher, deadline_at=self.T0 + 24 * HOUR)
        closed = self.assignment(self.teacher)
        untouched = self.assignment(self.teacher)
        snapshot = self.snapshot()
        with self.assertLogs("learnpilot.lifecycle", level="INFO") as logs:
            self.clock.advance(60)
            first = self.owner.post(f"/teacher/assignment/{edited}/edit", data={
                "title_bg": "Удължен", **self.deadline_form("2026-03-20")}, headers=SAME_ORIGIN)
            second = self.owner.post(f"/teacher/assignment/{closed}/close", headers=SAME_ORIGIN)
        self.assertEqual((first.status_code, second.status_code), (302, 302))
        without_the_closure = [line for line in logs.output if f"assignment_id={closed} " not in line]
        self.assertEqual((len(without_the_closure), len(logs.output)), (1, 2))

        self.restore(snapshot)
        self.held_without_evidence()
        self.assertIsNone(self.row(closed)["closed_at"])   # the restored row has lost the closure
        before = {a: self.lifecycle_columns(a) for a in (edited, closed, untouched)}

        code, out, _err = self.cli("reconcile-partial", "--affected-ids", edited, "--record-file",
                                   self.record_file(*without_the_closure), writer=True)
        self.assertEqual((code, out), (manage_lifecycle.EXIT_UNSUPPORTED, ""))
        self.assertEqual({a: self.lifecycle_columns(a) for a in (edited, closed, untouched)}, before)
        student = self.client(self.student)
        # The unsafe outcome was a 302 on `closed`. Nothing is released, the untouched one included:
        # that it changed nothing after the snapshot is not something a log can show.
        self.assertEqual([self.start(student, a).status_code for a in (closed, edited, untouched)], [503] * 3)
        self.assertEqual(self.attempts(closed), [])

    def test_a_fabricated_record_that_matches_what_is_stated_releases_nothing(self):
        closer = self.teacher["id"]
        assignment_id = self.assignment(self.teacher)
        other = self.assignment(self.teacher)
        snapshot = self.snapshot()
        self.assertEqual(self.owner.post(f"/teacher/assignment/{assignment_id}/close",
                                         headers=SAME_ORIGIN).status_code, 302)
        self.restore(snapshot)
        self.held_without_evidence()
        before = {a: self.lifecycle_columns(a) for a in (assignment_id, other)}
        entries = len(self.audit_entries())

        far = self.T0 + 100 * 24 * HOUR
        forgeries = {
            # Lines nobody can tell from real ones: each agrees perfectly with what is stated.
            "never closed, no deadline": (
                [self.deadline_event("create", assignment_id, self.T0 - 24 * HOUR, None, actor=closer)],
                self.STATE_NONE),
            "a deadline far ahead": (
                [self.deadline_event("create", assignment_id, self.T0 - 24 * HOUR, None, actor=closer),
                 self.deadline_event("edit", assignment_id, self.T0 - HOUR, far, actor=closer)],
                ("--deadline-at", ts(far), "--display-timezone", "Europe/Sofia", "--not-closed")),
            "a closure that happens to suit": (
                [self.close_event(assignment_id, self.T0 - 2 * HOUR, closer)],
                ("--no-deadline", "--display-timezone", "Europe/Sofia", "--closed-at", ts(self.T0 - 2 * HOUR),
                 "--closed-by-user-id", closer)),
        }
        for label, (lines, stated) in forgeries.items():
            with self.subTest(forgery=label):
                record = self.record_file(*lines)
                code, out, _err = self.reconcile_record(assignment_id, record, *stated)
                self.assertEqual((code, out), (manage_lifecycle.EXIT_UNSUPPORTED, ""))
                # A forged "log since the snapshot" that names only the other assignment as changed.
                code, out, _err = self.cli("reconcile-partial", "--affected-ids", other, "--record-file",
                                           self.record_file(self.deadline_event("edit", other, self.T0, None),
                                                            name="forged-partial.txt"), writer=True)
                self.assertEqual((code, out), (manage_lifecycle.EXIT_UNSUPPORTED, ""))
                self.assertEqual({a: self.lifecycle_columns(a) for a in (assignment_id, other)}, before)
        self.assertEqual(len(self.audit_entries()), entries)
        self.assertEqual(self.start(self.client(self.student), assignment_id).status_code, 503)

    def test_two_edits_of_the_same_second_are_not_ordered_by_a_log(self):
        """Blocker 2 of the review, as reported. Two deadline edits commit in this order: 11:00,
        then 09:30. Both carry the event time 09:00:00. The line of the first is delivered late, so
        the log shows them the other way round. Reconciliation from the log took 11:00, the obsolete
        one, and at 10:00 a new attempt was admitted although the deadline had passed at 09:30."""
        eleven, half_past_nine = datetime(2026, 3, 2, 11, 0, 0), datetime(2026, 3, 2, 9, 30, 0)
        self.assertEqual(self.T0, datetime(2026, 3, 2, 9, 0, 0))
        assignment_id = self.assignment(self.teacher)
        snapshot = self.snapshot()
        with self.assertLogs("learnpilot.lifecycle", level="INFO") as logs:
            for shown in ("12:59", "11:29"):     # Europe/Sofia is UTC+2: through 12:59 is a cutoff of 11:00 UTC
                response = self.owner.post(f"/teacher/assignment/{assignment_id}/edit", data={
                    "title_bg": "Срок", **self.deadline_form("2026-03-02", shown)}, headers=SAME_ORIGIN)
                self.assertEqual(response.status_code, 302)
        first, second = logs.output
        self.assertIn(f"at={ts(self.T0)!r} deadline_at={ts(eleven)!r}", first)
        self.assertIn(f"at={ts(self.T0)!r} deadline_at={ts(half_past_nine)!r}", second)
        self.assertEqual(self.row(assignment_id)["deadline_at"], ts(half_past_nine))   # what was committed last
        pre = self.restore(snapshot)

        self.assertEqual(self.cli("restore-hold", "--pre-restore-db", pre, writer=True)[0], 0)
        restored = self.lifecycle_columns(assignment_id)
        self.assertEqual((restored["deadline_at"], restored["lifecycle_hold"]), (None, "restore_pending"))
        deliveries = {"in commit order": (first, second), "the first line delivered late": (second, first)}
        for label, lines in deliveries.items():
            for stated in (eleven, half_past_nine):
                with self.subTest(delivery=label, stated=ts(stated)):
                    code, out, _err = self.reconcile_record(
                        assignment_id, self.record_file(*lines), "--deadline-at", ts(stated),
                        "--display-timezone", "Europe/Sofia", "--not-closed")
                    self.assertEqual((code, out), (manage_lifecycle.EXIT_UNSUPPORTED, ""))
                    self.assertEqual(self.lifecycle_columns(assignment_id), restored)   # no deadline was chosen

        self.clock.advance(3600)   # 10:00
        student = self.client(self.student)
        self.assertEqual(self.start(student, assignment_id).status_code, 503)   # it was 302
        self.assertEqual(self.attempts(assignment_id), [])

        # The replaced database does not have the question: it holds what was committed last.
        code, out, err = self.reconcile(pre, snapshot)
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.recover(self.clock.wall())[0], 0)
        row = self.row(assignment_id)
        self.assertEqual((row["deadline_at"], row["lifecycle_hold"]), (ts(half_past_nine), None))
        self.assertEqual(self.start(student, assignment_id).status_code, 409)   # served again, and closed
        self.assertEqual(self.attempts(assignment_id), [])

    def test_nothing_but_reconcile_resolves_a_restore_reason(self):
        ids = [self.assignment(self.teacher) for _ in range(3)]
        self.held_without_evidence()
        self.execute("UPDATE quiz_assignments SET lifecycle_hold = 'restore_pending,restore_unreconciled' "
                     "WHERE id = ?", (ids[1],))
        before = {a: self.lifecycle_columns(a) for a in ids}
        record = self.record_file(*(self.deadline_event("edit", a, self.T0, None) for a in ids))

        attempts = [
            *(("hold-resolve", "--assignment", a, "--reason", reason, "--evidence", "учителят потвърди по телефона")
              for a in ids for reason in ("restore_pending", "restore_unreconciled")),
            *(("reconcile-record", "--assignment", a, "--record-file", record, *self.STATE_NONE) for a in ids),
            ("reconcile-partial", "--affected-ids", ids[1], "--record-file", record),
            ("clock-recover", "--verified-utc", ts(self.T0)),
            ("verify", "--hold-exceptions"),
        ]
        for attempt in attempts:
            with self.subTest(command=attempt[:4]):
                self.cli(*attempt, writer=True)
                self.assertEqual({a: self.lifecycle_columns(a) for a in ids}, before)
        # Acknowledging the number that is held is not a release either.
        for arguments, expected in ((("restore-status",), manage_lifecycle.EXIT_INCOMPLETE),
                                    (("restore-status", "--expect-held", "2"), manage_lifecycle.EXIT_INCOMPLETE),
                                    (("restore-status", "--expect-held", "3"), 0),
                                    (("restore-status", "--expect-held", "all"), 0)):
            code, out, _err = self.cli(*arguments)
            self.assertEqual(code, expected, out)
        self.assertIn("ACKNOWLEDGED, NOT RECOVERED: 3 assignment(s) stay held",
                      self.cli("restore-status", "--expect-held", "3")[1])
        self.assertEqual({a: self.lifecycle_columns(a) for a in ids}, before)
        self.assertEqual([self.client().get(self.entry(a)).status_code for a in ids], [503] * 3)

    def test_restore_status_counts_what_a_restore_left_held_and_writes_nothing(self):
        plain = self.assignment(self.teacher)
        reviewed = self.assignment(self.teacher, lifecycle_hold="clock_review")
        unreadable = self.assignment(self.teacher, lifecycle_hold="clock_review,")
        before = {a: self.lifecycle_columns(a) for a in (plain, reviewed, unreadable)}

        # Before the hold step: not every assignment is held, so the database must not be served.
        code, out, _err = self.cli("restore-status", "--expect-held", "all")
        self.assertEqual(code, 1)
        self.assertIn(f"NOT HELD for the restore: assignment(s) {plain}, {reviewed}.", out)
        # A hold that cannot be read counts as held for the restore: nothing shows that it is not.
        code, out, _err = self.cli("restore-status")
        self.assertEqual(code, manage_lifecycle.EXIT_INCOMPLETE)
        self.assertIn("assignments: 3; held for the restore: 1; held for another reason only: 1", out)
        self.assertIn(f"assignment {unreadable}: 'clock_review,'  UNREADABLE", out)
        self.assertEqual(self.cli("restore-status", "--expect-held", "1")[0], 0)
        self.assertEqual({a: self.lifecycle_columns(a) for a in (plain, reviewed, unreadable)}, before)

        self.assertEqual(self.cli("restore-hold", writer=True)[0], 0)
        code, out, _err = self.cli("restore-status", "--expect-held", "all")
        self.assertEqual(code, 0, out)
        self.assertIn("every assignment is held for the restore", out)
        # Nothing held, and a number expected: the acknowledgement does not describe this database.
        self.execute("UPDATE quiz_assignments SET lifecycle_hold = NULL")
        code, out, _err = self.cli("restore-status", "--expect-held", "2")
        self.assertEqual(code, 1)
        self.assertIn("no assignment is held for the restore, but 2 were expected", out)
        self.assertEqual(self.cli("restore-status")[0], 0)
        self.assertEqual([entry["action"] for entry in self.audit_entries()], ["restore_hold"])

    def test_the_restored_database_is_not_evidence_about_itself(self):
        assignment_id = self.assignment(self.teacher)
        snapshot = self.snapshot()
        self.restore(snapshot)
        self.assertEqual(self.cli("restore-hold", writer=True)[0], 0)
        for arguments in (("restore-hold", "--pre-restore-db", self.db_path),
                          ("reconcile", "--pre-restore-db", self.db_path, "--snapshot-db", snapshot,
                           "--evidence-note", "проба")):
            code, _out, err = self.cli(*arguments, writer=True)
            self.assertEqual(code, 2)
            self.assertIn("not evidence about its own past", err)
        self.assertEqual(self.hold(assignment_id), "restore_pending")
        # Nor is a copy of it: it says nothing that this database does not already say.
        copy = self.copy_of(self.db_path, "copy-of-the-restored.db")
        code, _out, err = self.reconcile(copy, snapshot)
        self.assertEqual(code, 2)
        self.assertIn("it shows no lifecycle activity after the snapshot", err)
        self.assertEqual(self.hold(assignment_id), "restore_pending")
        self.assertEqual(self.client().get(self.entry(assignment_id)).status_code, 503)


class EvidenceProvenanceTest(OperatorTestCase):
    """Claude review of PR #326, M1. ``reconcile`` released restore holds from a byte copy of the
    snapshot, and from an unrelated database whose assignments had the same integer ids. Evidence
    now has to qualify as a whole: the same rows, and a lifecycle that went on after the snapshot.

    Claude review, N3: "went on" was also read off any difference in any table, so a copy of the
    snapshot with one account added qualified. Only the lifecycle clock shows it now."""

    def closed_after_the_snapshot(self):
        """Two assignments and an attempt; one assignment is closed after the snapshot; the
        snapshot is restored and everything is held. Returns ``(closed, other, snapshot, pre)``."""
        closed = self.assignment(self.teacher)
        other = self.assignment(self.teacher, deadline_at=self.T0 + 24 * HOUR)
        self.attempt(closed, self.student, started_at=self.T0 - 2 * HOUR, submitted_at=self.T0 - HOUR - timedelta(minutes=30))
        snapshot = self.snapshot()
        self.execute("UPDATE quiz_assignments SET closed_at = ?, closed_by_user_id = ? WHERE id = ?",
                     (ts(self.T0 - HOUR), self.teacher["id"], closed))
        pre = self.restore(snapshot)
        self.assertIsNone(self.row(closed)["closed_at"])        # the restored copy has lost the closure
        self.assertEqual(self.cli("restore-hold", "--pre-restore-db", pre, writer=True)[0], 0)
        return closed, other, snapshot, pre

    def assert_refused(self, evidence, snapshot, ids, *because):
        before = {a: self.lifecycle_columns(a) for a in ids}
        entries = len(self.audit_entries())
        code, out, err = self.reconcile(evidence, snapshot)
        self.assertEqual((code, out), (2, ""), err)
        for text in because:
            self.assertIn(text, err)
        self.assertEqual({a: self.lifecycle_columns(a) for a in ids}, before)   # nothing released, nothing marked
        self.assertEqual(len(self.audit_entries()), entries)
        return err

    def test_a_byte_copy_of_the_snapshot_is_not_evidence(self):
        closed, other, snapshot, pre = self.closed_after_the_snapshot()
        self.assert_refused(snapshot, snapshot, (closed, other),
                            "--pre-restore-db is the snapshot itself", "says nothing about what")
        copy = self.copy_of(snapshot, "copy-of-the-snapshot.db")
        stood = ts(self.T0 - HOUR)
        self.assert_refused(copy, snapshot, (closed, other),
                            "does not qualify as evidence: it shows no lifecycle activity after the snapshot",
                            f"its lifecycle clock stands at {stood}, exactly where the snapshot's stood at {stood}",
                            "A copy of the snapshot looks like this",
                            "Nothing was reconciled and every assignment stays held")
        # Opened, written to by the restore tooling, renamed: still nothing but the snapshot.
        touched = self.copy_of(snapshot, "the-snapshot-with-holds.db")
        self.sql(touched, "UPDATE quiz_assignments SET lifecycle_hold = 'clock_review'")
        self.assert_refused(touched, snapshot, (closed, other), "it shows no lifecycle activity after the snapshot")

        # The reopening this was: with the clock confirmed, a new attempt on the closed assignment.
        self.assertEqual(self.recover(self.T0)[0], 0)
        self.assertEqual(self.start(self.client(self.account()), closed).status_code, 503)   # it was 302
        self.assertEqual(len(self.attempts(closed)), 1)
        # The database that was live qualifies, and what it knows comes back: closed.
        self.assertEqual(self.cli("restore-hold", "--pre-restore-db", pre, writer=True)[0], 0)
        code, out, err = self.reconcile(pre, snapshot)
        self.assertEqual(code, 0, out + err)
        self.assertIn("the pre-restore database qualifies as the later state: its lifecycle clock ran to", out)
        self.assertEqual(self.recover(self.T0)[0], 0)
        self.assertEqual((self.row(closed)["closed_at"], self.hold(closed)), (ts(self.T0 - HOUR), None))
        self.assertEqual(self.start(self.client(self.account()), closed).status_code, 409)
        self.assertEqual(self.start(self.client(self.account()), other).status_code, 302)

    def test_another_database_with_the_same_assignment_ids_is_not_evidence(self):
        closed, other, snapshot, _pre = self.closed_after_the_snapshot()
        # Another school's database. Its assignments happen to have the same numbers, and none is closed.
        unrelated = self.copy_of(snapshot, "another-school.db")
        self.sql(unrelated, "UPDATE quiz_assignments SET public_token = 'другo-' || id, "
                            "created_at = '2025-09-15 08:00:00', title_bg = 'Чужд тест'")
        self.sql(unrelated, "UPDATE quiz_assignments SET lifecycle_hold = 'clock_review' WHERE id = ?", (other,))
        set_lifecycle_clock_row(unrelated, high_water=self.T0, anchored=False)
        self.assertEqual([row[0] for row in self.sql(unrelated, "SELECT id FROM quiz_assignments ORDER BY id")],
                         [closed, other])

        err = self.assert_refused(unrelated, snapshot, (closed, other),
                                  "it is not a later state of this database",
                                  f"assignment {closed} is a different assignment in the pre-restore database",
                                  "The same ids are not the same rows")
        self.assertRegex(err, r"\((created_at|public_token) differs\)")
        # Its holds are not this database's either: nothing is carried from a stranger.
        code, out, _err = self.cli("restore-hold", "--pre-restore-db", unrelated, writer=True)
        self.assertEqual(code, 0)
        self.assertIn(f"held there under the same id, but a different assignment (nothing carried): {other}", out)
        self.assertEqual(self.hold(other), "restore_pending")
        self.assertEqual(self.audit_entries()[-1]["not_the_same_assignment"], [other])
        self.assertEqual(self.recover(self.T0)[0], 0)
        self.assertEqual(self.start(self.client(self.account()), closed).status_code, 503)

    def test_every_identifying_column_has_to_agree_and_matching_ids_are_not_enough(self):
        closed, other, snapshot, pre = self.closed_after_the_snapshot()
        another_section = self.sql(pre, "SELECT id FROM curriculum_sections WHERE id != ? ORDER BY id LIMIT 1",
                                   (self.section_id,))[0][0]
        someone_else = self.account("teacher")["id"]
        differing = {
            "public_token": "a-token-that-is-not-this-assignments",
            "created_at": "2025-09-15 08:00:00",
            "owner_user_id": someone_else,
            "section_id": another_section,
            "question_count": 99,
            "question_plan_json": '{"question_ids": [1]}',
        }
        for column, value in differing.items():
            with self.subTest(assignment_column=column):
                evidence = self.copy_of(pre, f"evidence-{column}.db")
                self.sql(evidence, f"UPDATE quiz_assignments SET {column} = ? WHERE id = ?", (value, other))
                self.assert_refused(evidence, snapshot, (closed, other),
                                    f"assignment {other} is a different assignment in the pre-restore database "
                                    f"({column} differs)")
        (attempt,) = [row["id"] for row in self.attempts(closed)]
        attempts = {
            "is not in the pre-restore database": "DELETE FROM quiz_attempts WHERE id = ?",
            "(started_at differs)": "UPDATE quiz_attempts SET started_at = '2026-01-01 00:00:00' WHERE id = ?",
            "(student_name differs)": "UPDATE quiz_attempts SET student_name = 'Друг ученик' WHERE id = ?",
            "(assignment_id differs)": f"UPDATE quiz_attempts SET assignment_id = {other} WHERE id = ?",
            # Submitted in the snapshot, not submitted in the "later" state: time does not run backwards.
            "(submitted_at differs)": "UPDATE quiz_attempts SET submitted_at = NULL WHERE id = ?",
        }
        for expected, statement in attempts.items():
            with self.subTest(attempt=expected):
                evidence = self.copy_of(pre, "evidence-attempt.db")
                self.sql(evidence, statement, (attempt,))
                self.assert_refused(evidence, snapshot, (closed, other), f"attempt {attempt}", expected)

    def test_what_is_written_once_later_does_not_make_it_another_assignment(self):
        # Ownership and the public token were added by later releases: an old row has neither, and
        # the live database may have been given them since. An attempt may have been submitted since.
        legacy = self.assignment(None)
        self.execute("UPDATE quiz_assignments SET public_token = NULL WHERE id = ?", (legacy,))
        attempt = self.attempt(legacy, self.student, started_at=self.T0 - 2 * HOUR)
        snapshot = self.snapshot()
        self.execute("UPDATE quiz_assignments SET public_token = 'given-later', owner_user_id = ?, closed_at = ? "
                     "WHERE id = ?", (self.teacher["id"], ts(self.T0 - HOUR), legacy))
        self.execute("UPDATE quiz_attempts SET submitted_at = ?, score_correct = 1 WHERE id = ?",
                     (ts(self.T0 - HOUR - timedelta(minutes=5)), attempt))
        pre = self.restore(snapshot)
        self.cli("restore-hold", "--pre-restore-db", pre, writer=True)
        code, out, err = self.reconcile(pre, snapshot)
        self.assertEqual(code, 0, out + err)
        self.assertEqual((self.row(legacy)["closed_at"], self.hold(legacy)), (ts(self.T0 - HOUR), None))

    def test_evidence_older_than_the_snapshot_is_refused(self):
        closed, other, snapshot, pre = self.closed_after_the_snapshot()
        # Last week's backup, with every row the snapshot has: only its clock shows its age.
        older = self.copy_of(pre, "last-weeks-backup.db")
        self.sql(older, "UPDATE quiz_assignments SET deadline_at = ? WHERE id = ?", (ts(self.T0 + 99 * HOUR), other))
        set_lifecycle_clock_row(older, high_water=self.T0 - 3 * HOUR, anchored=False)
        self.assert_refused(older, snapshot, (closed, other), "it is older than the snapshot",
                            f"its lifecycle clock stopped at {ts(self.T0 - 3 * HOUR)}, where the snapshot's "
                            f"stood at {ts(self.T0 - HOUR)}")
        self.assertEqual(self.row(other)["deadline_at"], ts(self.T0 + 24 * HOUR))   # not moved to the older value
        # A database at another schema than this build is not the one this build was serving.
        elsewhere = self.copy_of(pre, "another-build.db")
        self.sql(elsewhere, "INSERT INTO schema_migrations (filename) VALUES ('019_from_a_newer_build.sql')")
        self.assert_refused(elsewhere, snapshot, (closed, other), "it is not at the schema of this build",
                            "019_from_a_newer_build.sql")
        # Its clock row has to be readable, or nothing says how far it had run.
        broken = self.copy_of(pre, "no-clock.db")
        self.sql(broken, "DELETE FROM lifecycle_clock")
        self.assert_refused(broken, snapshot, (closed, other), "its lifecycle clock row cannot be read")

    def test_the_snapshot_named_has_to_be_the_one_this_database_was_restored_from(self):
        closed, other, snapshot, pre = self.closed_after_the_snapshot()
        # Another snapshot: taken before the second assignment existed.
        earlier = self.copy_of(snapshot, "an-earlier-snapshot.db")
        self.sql(earlier, "DELETE FROM quiz_assignments WHERE id = ?", (other,))
        err = self.assert_refused(pre, earlier, (closed, other),
                                  "--snapshot-db is not the snapshot this database was restored from")
        self.assertIn(f"assignment {other} is not in the snapshot", err)
        later = self.copy_of(snapshot, "a-later-snapshot.db")
        self.sql(later, "INSERT INTO quiz_assignments (section_id, title_bg, question_count, created_at) "
                        "VALUES (?, 'По-късен', 1, '2026-03-02 08:30:00')", (self.section_id,))
        self.assert_refused(pre, later, (closed, other), "--snapshot-db is not the snapshot this database was restored from",
                            "is not in the restored database")

    def test_a_database_that_lived_on_is_evidence_even_if_only_its_clock_shows_it(self):
        # After the snapshot nothing changed but the clock: a refused start, a page looked at.
        assignment_id = self.assignment(self.teacher, closed_at=self.T0 - 5 * HOUR)
        snapshot = self.snapshot()
        pre = self.restore(snapshot)
        self.cli("restore-hold", "--pre-restore-db", pre, writer=True)
        code, out, err = self.reconcile(pre, snapshot)
        self.assertEqual(code, 0, out + err)
        self.assertIn(f"its lifecycle clock ran to {ts(self.T0)}; the snapshot's stood at {ts(self.T0 - HOUR)}", out)
        self.assertIsNone(self.hold(assignment_id))

    # What is not lifecycle history, and was counted as some (Claude review, N3).
    UNRELATED_CHANGES = {
        "an account added": "INSERT INTO users (display_name, role, session_key, created_at, updated_at) "
                            f"VALUES ('Нов учител', 'teacher', '{'k' * 32}', '2026-03-02 08:59:00', '2026-03-02 08:59:00')",
        "a sign-in": "UPDATE users SET last_login_at = '2026-03-02 08:59:00' WHERE id = (SELECT MIN(id) FROM users)",
        "a failed sign-in": "INSERT INTO login_throttle (identifier_hash, failure_count, window_started_at) "
                            f"VALUES ('{'a' * 64}', 1, '2026-03-02 08:00:00')",
        "a title edited": "UPDATE quiz_assignments SET title_bg = 'Преименуван след копието'",
        "the day of the migrations": "UPDATE schema_migrations SET applied_at = '2030-01-01 00:00:00'",
    }

    def test_a_copy_of_the_snapshot_with_something_else_changed_in_it_is_not_evidence(self):
        closed, other, snapshot, pre = self.closed_after_the_snapshot()
        for label, statement in self.UNRELATED_CHANGES.items():
            with self.subTest(changed=label):
                copy = self.copy_of(snapshot, "a-copy-with-a-change.db")
                self.sql(copy, statement)
                self.assert_refused(copy, snapshot, (closed, other),
                                    "it shows no lifecycle activity after the snapshot",
                                    "an account, a sign-in or an edited title says nothing about deadlines and closures",
                                    "Nothing was reconciled and every assignment stays held")
        # All of them at once are no more than each.
        copy = self.copy_of(snapshot, "a-copy-with-every-change.db")
        for statement in self.UNRELATED_CHANGES.values():
            self.sql(copy, statement)
        self.assert_refused(copy, snapshot, (closed, other), "it shows no lifecycle activity after the snapshot")
        self.assertEqual({self.hold(closed), self.hold(other)}, {"restore_pending"})
        self.assertIsNone(self.row(closed)["closed_at"])
        # The database that was live has what none of those has, and its closure comes back.
        code, out, err = self.reconcile(pre, snapshot)
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.row(closed)["closed_at"], ts(self.T0 - HOUR))

    def test_an_older_copy_with_other_things_changed_in_it_is_still_older(self):
        closed, other, snapshot, pre = self.closed_after_the_snapshot()
        for source, name in ((snapshot, "an-older-copy-of-the-snapshot.db"), (pre, "an-older-copy-of-the-live-one.db")):
            with self.subTest(copy_of=name):
                older = self.copy_of(source, name)
                for statement in self.UNRELATED_CHANGES.values():
                    self.sql(older, statement)
                set_lifecycle_clock_row(older, high_water=self.T0 - 2 * HOUR, anchored=False)
                self.assert_refused(older, snapshot, (closed, other), "it is older than the snapshot",
                                    f"its lifecycle clock stopped at {ts(self.T0 - 2 * HOUR)}")
        self.assertEqual({self.hold(closed), self.hold(other)}, {"restore_pending"})

    def test_the_same_rows_without_lifecycle_activity_after_the_snapshot_are_not_evidence(self):
        # Every assignment and attempt is there, as the same row, and lifecycle columns even differ.
        # Nothing but the clock says when they were written, and it says: not after the snapshot.
        closed, other, snapshot, pre = self.closed_after_the_snapshot()
        stood = self.T0 - HOUR
        by_hand = {
            "a closure": ("UPDATE quiz_assignments SET closed_at = ? WHERE id = ?", (ts(self.T0 - HOUR), other)),
            "a closure taken out": ("UPDATE quiz_assignments SET closed_at = NULL WHERE id = ?", (closed,)),
            "a deadline moved": ("UPDATE quiz_assignments SET deadline_at = ? WHERE id = ?",
                                 (ts(self.T0 + 99 * HOUR), other)),
            "an attempt added": ("INSERT INTO quiz_attempts (assignment_id, student_name, seed, question_ids_json) "
                                 "VALUES (?, 'Ученик', 's', '[]')", (other,)),
        }
        for label, (statement, params) in by_hand.items():
            with self.subTest(written=label):
                copy = self.copy_of(snapshot, "a-copy-written-to.db")
                self.sql(copy, statement, params)
                self.assert_refused(copy, snapshot, (closed, other),
                                    "it shows no lifecycle activity after the snapshot",
                                    f"its lifecycle clock stands at {ts(stood)}, exactly where the snapshot's stood at "
                                    f"{ts(stood)}")
        # The database that was live itself, had its clock not moved since the snapshot: the same.
        quiet = self.copy_of(pre, "the-live-one-with-a-clock-that-stood.db")
        set_lifecycle_clock_row(quiet, high_water=stood, anchored=False)
        self.assert_refused(quiet, snapshot, (closed, other), "it shows no lifecycle activity after the snapshot",
                            "that cannot be told from a copy")
        self.assertEqual(self.row(other)["deadline_at"], ts(self.T0 + 24 * HOUR))
        self.assertIsNone(self.row(closed)["closed_at"])
        # One second of lifecycle after the snapshot is what makes it the later state.
        moved_on = self.copy_of(pre, "the-live-one-a-second-later.db")
        set_lifecycle_clock_row(moved_on, high_water=stood + timedelta(seconds=1), anchored=False)
        code, out, err = self.reconcile(moved_on, snapshot)
        self.assertEqual(code, 0, out + err)
        self.assertIn(f"its lifecycle clock ran to {ts(stood + timedelta(seconds=1))}; the snapshot's stood at "
                      f"{ts(stood)}", out)
        self.assertEqual(self.row(closed)["closed_at"], ts(self.T0 - HOUR))

    def test_nothing_but_the_lifecycle_clock_is_asked_whether_the_evidence_is_later(self):
        # There is no comparison of table contents left to be satisfied by an unrelated row.
        self.assertFalse(hasattr(manage_lifecycle, "content_differs"))
        source = Path(manage_lifecycle.__file__).read_text(encoding="utf-8")
        qualify = source[source.index("def require_later_state("):source.index("def _carry_clock(")]
        self.assertIn("if evidence_mark < reference:", qualify)
        self.assertIn("if evidence_mark == reference:", qualify)
        for never in ("sqlite_master WHERE type = 'table' AND name NOT", "EXCEPT", "_table_rows", "content_differs"):
            self.assertNotIn(never, qualify)


class ClockAfterReconciliationTest(OperatorTestCase):
    """Claude review of PR #326, M2. ``clock-recover`` ran before ``reconcile``: a deadline that
    reconciliation brought in afterwards was never classified, and came back open and unheld."""

    def edited_and_expired_by_a_clock_that_ran_ahead(self, **after_the_snapshot):
        """The replaced database's clock has a high-water mark two hours ahead; after the snapshot
        the deadline was set to an hour ahead, so by that clock the assignment had expired."""
        assignment_id = self.assignment(self.teacher)
        snapshot = self.snapshot()
        deadline = self.T0 + HOUR
        columns = {"deadline_at": ts(deadline), **after_the_snapshot}
        self.execute("UPDATE quiz_assignments SET " + ", ".join(f"{name} = ?" for name in columns) + " WHERE id = ?",
                     (*columns.values(), assignment_id))
        set_lifecycle_clock_row(self.db_path, high_water=self.T0 + 2 * HOUR)
        pre = self.restore(snapshot)
        self.assertIsNone(self.row(assignment_id)["deadline_at"])    # the restored copy has the older state
        return assignment_id, snapshot, pre, deadline

    def test_the_order_that_reopened_it_is_refused_and_the_right_order_holds_it(self):
        assignment_id, snapshot, pre, deadline = self.edited_and_expired_by_a_clock_that_ran_ahead()
        self.assertEqual(self.cli("restore-hold", "--pre-restore-db", pre, writer=True)[0], 0)
        self.assertEqual(self.clock_row()["high_water_at"], ts(self.T0 + 2 * HOUR))   # the replaced clock's mark

        # The order of the reproduction: the clock is confirmed first, and nothing is held for it,
        # because the copy still has no deadline. Reconciliation is then refused.
        self.assertEqual(self.recover(self.T0)[0], 0)
        self.assertEqual(self.hold(assignment_id), "restore_pending")
        code, out, err = self.reconcile(pre, snapshot)
        self.assertEqual((code, out), (1, ""))
        self.assertIn("reconcile runs after restore-hold and before clock-recover", err)
        self.assertIn("Run restore-hold again, then reconcile, then clock-recover", err)
        self.assertEqual((self.row(assignment_id)["deadline_at"], self.hold(assignment_id)), (None, "restore_pending"))

        # hold, reconcile, clock.
        self.assertEqual(self.cli("restore-hold", "--pre-restore-db", pre, writer=True)[0], 0)
        code, out, err = self.reconcile(pre, snapshot)
        self.assertEqual(code, 0, out + err)
        clock = self.clock_row()
        self.assertEqual((clock["state"], clock["high_water_at"]), ("suspect", ts(self.T0 + 2 * HOUR)))
        self.assertIn("complete clock-recover next: it classifies what was reconciled", out)
        code, out, err = self.recover(self.T0)
        self.assertEqual(code, 0, out + err)
        row = self.row(assignment_id)
        self.assertEqual((row["deadline_at"], row["lifecycle_hold"]), (ts(deadline), "clock_review"))
        self.assertIsNone(row["expiry_observed_at"])                 # held, and no closure manufactured
        self.assertIn(f"deadline {ts(deadline)} lies after the verified time and was never recorded as passed", out)
        # It was open and unheld: a student could start an assignment that had already expired.
        self.assertEqual(self.start(self.client(self.account()), assignment_id).status_code, 503)
        self.assertEqual(self.attempts(assignment_id), [])
        self.assertEqual(self.cli("verify")[0], 0)

    def test_reconcile_hands_the_replaced_clock_on_even_when_the_hold_step_did_not_see_it(self):
        assignment_id, snapshot, pre, deadline = self.edited_and_expired_by_a_clock_that_ran_ahead()
        self.assertEqual(self.cli("restore-hold", writer=True)[0], 0)        # without the replaced database
        self.assertLess(self.clock_row()["high_water_at"], ts(self.T0 + 2 * HOUR))
        code, out, err = self.reconcile(pre, snapshot)
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.clock_row()["high_water_at"], ts(self.T0 + 2 * HOUR))
        self.assertEqual(self.audit_entries()[-1]["clock"]["high_water"], ts(self.T0 + 2 * HOUR))
        self.assertEqual(self.recover(self.T0)[0], 0)
        self.assertEqual(self.hold(assignment_id), "clock_review")
        self.assertEqual(self.start(self.client(self.account()), assignment_id).status_code, 503)

    def test_an_expiry_the_replaced_database_had_observed_stays_observed(self):
        observed = self.T0 + HOUR + timedelta(minutes=30)             # stamped by the clock that ran ahead
        assignment_id, snapshot, pre, deadline = self.edited_and_expired_by_a_clock_that_ran_ahead(
            expiry_observed_at=ts(observed))
        self.cli("restore-hold", "--pre-restore-db", pre, writer=True)
        self.assertEqual(self.reconcile(pre, snapshot)[0], 0)
        self.assertEqual(self.recover(self.T0)[0], 0)
        row = self.row(assignment_id)
        self.assertEqual((row["deadline_at"], row["expiry_observed_at"], row["lifecycle_hold"]),
                         (ts(deadline), ts(observed), "clock_review"))
        self.assertEqual(self.start(self.client(self.account()), assignment_id).status_code, 503)
        # A marker stamped ahead keeps it held until time has passed it, and closed after that.
        code, _out, err = self.cli("hold-resolve", "--assignment", assignment_id, "--reason", "clock_review",
                                   "--evidence", "преглед", writer=True)
        self.assertEqual(code, 1)
        self.assertIn("is still after lifecycle time", err)
        self.clock.advance(2 * 3600)
        self.assertEqual(self.cli("hold-resolve", "--assignment", assignment_id, "--reason", "clock_review",
                                  "--evidence", "преглед", writer=True)[0], 0)
        self.assertEqual(self.start(self.client(self.account()), assignment_id).status_code, 409)

    def test_an_incident_that_was_open_in_the_replaced_database_is_still_open_after_reconciliation(self):
        assignment_id = self.assignment(self.teacher)
        snapshot = self.snapshot()
        self.execute("UPDATE quiz_assignments SET closed_at = ? WHERE id = ?", (ts(self.T0 - HOUR), assignment_id))
        set_lifecycle_clock_row(self.db_path, high_water=self.T0 + 5 * HOUR, state="suspect",
                                reason="clock_regression", changed_at=self.T0 - timedelta(minutes=10))
        pre = self.restore(snapshot)
        self.cli("restore-hold", writer=True)                         # the hold step did not see the incident
        self.assertEqual(self.clock_row()["state_reason"], lifecycle.CLOCK_REASON_RESTORE)
        self.assertEqual(self.reconcile(pre, snapshot)[0], 0)
        clock = self.clock_row()
        self.assertEqual((clock["state"], clock["state_reason"], clock["state_changed_at"], clock["high_water_at"]),
                         ("suspect", "clock_regression", ts(self.T0 - timedelta(minutes=10)), ts(self.T0 + 5 * HOUR)))
        self.assertEqual(self.client().get("/healthz").status_code, 503)
        # The declared interval reaches the classification, which runs on what was reconciled.
        code, out, err = self.recover(self.T0, "--suspect-from", ts(self.T0 - 2 * HOUR), "--suspect-to", ts(self.T0))
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.hold(assignment_id), "clock_review")   # closed inside the suspect interval
        self.assertEqual(self.row(assignment_id)["closed_at"], ts(self.T0 - HOUR))


class VerificationTest(OperatorTestCase):
    def test_l128_timestamp_classes_in_verification(self):
        near = self.assignment(self.teacher, deadline_at=self.T0 + 3 * HOUR)
        far = self.assignment(self.teacher, deadline_at=self.T0 + timedelta(days=179))
        anomaly = self.assignment(self.teacher, deadline_at=self.T0 + timedelta(days=400))
        started_ahead = self.assignment(self.teacher)
        self.attempt(started_ahead, self.student, started_at=self.T0 + HOUR)
        closed_ahead = self.assignment(self.teacher, closed_at=self.T0 + HOUR)
        clock_before = tuple(self.clock_row())

        code, out, _err = self.cli("verify")
        self.assertEqual(code, 1)
        self.assertIn(f"exception (NOT HELD): assignment {started_ahead}: attempt 1 started_at", out)
        self.assertIn(f"exception (NOT HELD): assignment {closed_ahead}: closed_at", out)
        self.assertIn(f"data anomaly: assignment {anomaly} has a deadline", out)
        for fine in (near, far):
            self.assertNotIn(f"assignment {fine}", out)  # a scheduled deadline in the future is expected
        self.assertEqual([self.hold(a) for a in (near, far, anomaly, started_ahead, closed_ahead)], [None] * 5)

        code, out, err = self.cli("verify", "--hold-exceptions", "--operator", "оператор", "--audit-log",
                                  self.audit_log)
        self.assertEqual(code, 0, out + err)
        self.assertEqual([self.hold(a) for a in (near, far, anomaly, started_ahead, closed_ahead)],
                         [None, None, None, "clock_review", "clock_review"])
        self.assertEqual(tuple(self.clock_row()), clock_before)  # the anomaly never changes the clock state
        self.assertIn("OK: lifecycle state verifies", out)
        self.assertEqual(self.audit_entries()[-1]["action"], "verify_hold_exceptions")

    def test_verification_reports_what_must_not_reach_traffic(self):
        malformed = self.assignment(self.teacher, deadline_at="2026-13-45 00:00:00")
        orphan_closer = self.assignment(self.teacher, closed_by_user_id=self.teacher["id"])
        unreadable = self.assignment(self.teacher, lifecycle_hold="who knows")
        self.attempt(malformed, self.student, started_at="yesterday")
        self.execute("UPDATE lifecycle_clock SET state = 'suspect', state_reason = 'clock_step'")
        code, out, _err = self.cli("verify")
        self.assertEqual(code, 1)
        for expected in (f"FAIL: assignment {malformed}: deadline_at does not parse",
                         f"FAIL: assignment {malformed}: attempt 1 started_at does not parse",
                         f"FAIL: assignment {orphan_closer}: closed_by_user_id without closed_at",
                         f"FAIL: assignment {unreadable}: lifecycle_hold is unreadable (it stays held)",
                         "FAIL: lifecycle clock state is suspect (clock_step)", "held assignments: 1"):
            self.assertIn(expected, out)
        self.execute("DELETE FROM lifecycle_clock")
        self.assertIn("FAIL: lifecycle clock row is unusable", self.cli("verify")[1])

    def test_a_clean_database_verifies(self):
        self.assignment(self.teacher, deadline_at=self.T0 + 3 * HOUR)
        self.assignment(self.teacher, closed_at=self.T0 - HOUR, closed_by_user_id=self.teacher["id"])
        code, out, _err = self.cli("verify")
        self.assertEqual(code, 0, out)
        self.assertIn("held assignments: 0", out)


class ExposureAuditTest(OperatorTestCase):
    def test_l97_the_audit_lists_what_non_enforcing_code_let_through_and_changes_nothing(self):
        E = self.T0 - 10 * HOUR
        expired = self.assignment(self.teacher, deadline_at=E)
        closed = self.assignment(self.teacher, closed_at=E)
        accounts = [self.account() for _ in range(6)]
        fine = self.attempt(expired, accounts[0], started_at=E - HOUR, submitted_at=E - timedelta(minutes=30))
        late_start = self.attempt(expired, accounts[1], started_at=E)                     # >= deadline
        late_submit = self.attempt(expired, accounts[2], started_at=E - HOUR, submitted_at=E + GRACE)
        in_grace = self.attempt(expired, accounts[3], started_at=E - HOUR, submitted_at=E + timedelta(minutes=5))
        after_close = self.attempt(closed, accounts[4], started_at=E + timedelta(seconds=1))
        same_second = self.attempt(closed, accounts[5], started_at=E)
        before = {table: self.query(f"SELECT * FROM {table} ORDER BY 1")
                  for table in ("quiz_assignments", "quiz_attempts")}

        code, out, _err = self.cli("exposure-audit", "--from", ts(E), "--to", ts(E + 2 * HOUR))
        self.assertEqual(code, 1)
        self.assertIn(f"attempt {late_start} (assignment {expired}): started {ts(E)}, after the closure", out)
        self.assertIn(f"attempt {after_close} (assignment {closed}): started", out)
        self.assertIn(f"AMBIGUOUS attempt {same_second} (assignment {closed}): started in the same second", out)
        self.assertIn(f"attempt {late_submit} (assignment {expired}): submitted {ts(E + GRACE)}, at or after the "
                      "cutoff", out)
        self.assertIn(f"attempt {in_grace} (assignment {expired}): submitted", out)
        self.assertIn("listed for completeness", out)
        self.assertNotIn(f"attempt {fine} ", out)
        self.assertIn("nothing was modified", out)
        self.assertEqual({table: self.query(f"SELECT * FROM {table} ORDER BY 1") for table in before},
                         before)

    def test_edits_and_overwritten_submissions_are_found_against_a_snapshot(self):
        E = self.T0 - 10 * HOUR
        closed = self.assignment(self.teacher, closed_at=E, title="Преди")
        attempt_id = self.attempt(closed, self.student, started_at=E - HOUR, submitted_at=E - timedelta(minutes=30))
        snapshot = self.snapshot("before-exposure.db")
        self.execute("UPDATE quiz_assignments SET title_bg = 'След' WHERE id = ?", (closed,))
        self.execute("UPDATE quiz_attempts SET submitted_at = ?, score_correct = 3 WHERE id = ?",
                     (ts(E + timedelta(minutes=3)), attempt_id))
        code, out, _err = self.cli("exposure-audit", "--from", ts(E), "--to", ts(E + HOUR), "--snapshot-db", snapshot)
        self.assertEqual(code, 1)
        self.assertIn(f"assignment {closed}: title or time limit differs from the pre-exposure snapshot", out)
        self.assertIn(f"attempt {attempt_id}: its submission differs from the pre-exposure snapshot", out)
        clean = self.cli("exposure-audit", "--from", ts(self.T0), "--to", ts(self.T0 + HOUR))
        self.assertIn("cannot be detected from the live data alone", clean[1])


class NoReopeningTest(OperatorTestCase):
    def test_l37_no_writer_and_no_command_clears_or_changes_a_terminal_marker(self):
        past = self.T0 - 3 * HOUR
        states = {
            "closed manually, in grace": dict(closed_at=self.T0 - timedelta(minutes=5),
                                              closed_by_user_id=self.teacher["id"],
                                              deadline_at=self.T0 + 5 * 24 * HOUR),
            "closed manually, ended": dict(closed_at=past, submissions_closed_at=past + GRACE),
            "expired, observed": dict(deadline_at=self.T0 - timedelta(minutes=5),
                                      expiry_observed_at=self.T0 - timedelta(minutes=4)),
            "expired, ended": dict(deadline_at=past, expiry_observed_at=past + HOUR,
                                   submissions_closed_at=past + HOUR),
            "expired, never observed": dict(deadline_at=past),
        }
        ids = {label: self.assignment(self.teacher, **columns) for label, columns in states.items()}
        attempts = {}
        for label, assignment_id in ids.items():
            account = self.account()
            attempts[label] = (account, self.attempt(assignment_id, account, started_at=self.T0 - 4 * HOUR))
        seen = {label: self.markers(assignment_id) for label, assignment_id in ids.items()}

        def check(step):
            for label, assignment_id in ids.items():
                now = self.markers(assignment_id)
                for column, old in seen[label].items():
                    if old is not None:
                        self.assertEqual(now[column], old, f"{step}: {label}: {column} changed")
                self.assertEqual(now["deadline_at"], seen[label]["deadline_at"], f"{step}: {label}: deadline")
                seen[label] = now  # a marker stamped by an observer must then stay as well

        snapshot = self.snapshot("for-reconcile.db")
        closer = self.teacher["id"]
        zone = ("--display-timezone", "Europe/Sofia")

        def record_that(*lines):
            return self.record_file(*lines, name="l37-record.txt")

        steps = {
            "start": lambda a, label: self.start(self.client(self.account()), a),
            "submit": lambda a, label: self.submit(self.client(attempts[label][0]), attempts[label][1]),
            "close": lambda a, label: self.owner.post(f"/teacher/assignment/{a}/close", headers=SAME_ORIGIN),
            "edit later": lambda a, label: self.owner.post(
                f"/teacher/assignment/{a}/edit", data={"title_bg": "Отново", **self.deadline_form("2026-06-01")},
                headers=SAME_ORIGIN),
            "edit none": lambda a, label: self.owner.post(
                f"/teacher/assignment/{a}/edit", data={"title_bg": "Отново", **self.no_deadline_form()},
                headers=SAME_ORIGIN),
            "duplicate": lambda a, label: self.owner.post(
                f"/teacher/assignment/{a}/duplicate", data=self.no_deadline_form(), headers=SAME_ORIGIN),
            "pages": lambda a, label: [self.owner.get(f"/teacher/assignment/{a}"), self.client().get(self.entry(a)),
                                       self.owner.get(f"/teacher/assignment/{a}/results")],
            "observe": lambda a, label: lifecycle.observe(sqlite3.connect(str(self.db_path)), a),
            # Records an operator might be handed that would clear or change what is terminal.
            # (The command they are handed to refuses every record; the steps stay as a guard.)
            "record: change closed_at": lambda a, label: self.reconcile_record(
                a, record_that(self.close_event(a, self.T0 - HOUR, closer)),
                "--no-deadline", *zone, "--closed-at", ts(self.T0 - HOUR), "--closed-by-user-id", closer),
            "record: move deadline": lambda a, label: self.reconcile_record(
                a, record_that(self.deadline_event("edit", a, self.T0 - 9 * HOUR, self.T0 + 99 * HOUR)),
                "--deadline-at", ts(self.T0 + 99 * HOUR), *zone, "--not-closed"),
            "record: no deadline": lambda a, label: self.reconcile_record(
                a, record_that(self.deadline_event("edit", a, self.T0 - 9 * HOUR, None)),
                "--no-deadline", *zone, "--not-closed"),
        }
        database_wide = {
            "verify": lambda: self.cli("verify", "--hold-exceptions", "--operator", "о", "--audit-log", self.audit_log),
            "clock-recover": lambda: self.recover(self.clock.wall()),
            "restore-hold": lambda: self.cli("restore-hold", "--pre-restore-db", snapshot, writer=True),
            "reconcile-partial": lambda: self.cli(
                "reconcile-partial", "--affected-ids", str(ids["expired, ended"]), "--record-file",
                record_that(self.deadline_event("edit", ids["expired, ended"], self.T0 - 9 * HOUR, None)),
                writer=True),
            "reconcile": lambda: self.reconcile(snapshot, self.copy_of(snapshot, "l37-origin.db")),
            "clock-recover again": lambda: self.recover(self.clock.wall()),
        }
        for name, step in steps.items():
            for label, assignment_id in ids.items():
                step(assignment_id, label)
            check(name)
        self.clock.advance(20 * 60)  # every grace window has ended: observers stamp, nothing else moves
        for name, step in list(steps.items())[:8]:
            for label, assignment_id in ids.items():
                step(assignment_id, label)
            check(name + " (later)")
        for name, step in database_wide.items():
            step()
            check(name)
            for label, assignment_id in ids.items():  # and again per assignment, now held or released
                for per_assignment in ("record: change closed_at", "record: move deadline", "record: no deadline"):
                    steps[per_assignment](assignment_id, label)
            check(name + " + records")
        # In the end every one of them is still closed: nothing became startable.
        self.execute("UPDATE quiz_assignments SET lifecycle_hold = NULL")
        self.recover(self.clock.wall())
        self.execute("UPDATE quiz_assignments SET lifecycle_hold = NULL")
        for label, assignment_id in ids.items():
            self.assertEqual(self.start(self.client(self.account()), assignment_id).status_code, 409, label)
            self.assertEqual(len(self.attempts(assignment_id)), 1, label)


if __name__ == "__main__":
    unittest.main()
