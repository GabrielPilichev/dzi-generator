"""Clock recovery and inspection with DZI Part 1 attempts (C3.2): ``src.manage_lifecycle``.

The real operator entry (``manage_lifecycle.main``) runs in-process on disposable databases built from
migrations + canonical, at the controlled fixture clock (``tests.support.lifecycle_time`` and
``TEST_CLOCK``): no host clock, boot identity or database of the machine is read or changed, and no
connection opens ``data/questions.db`` (these tests do not use the isolated-app connection guard; building
the template runs the migration runner, whose default-path check resolves that path, metadata only).

What is proved:

* ``clock-inspect`` writes nothing and adds only the count of open Part 1 attempts; that count is what
  ``clock-recover`` then abandons;
* ``clock-recover`` abandons every still-open attempt at the *verified* time, with and without a declared
  suspect interval and whether or not that time precedes the attempt's start, inside its one transaction:
  every refusal, failed write and audit failure restores every row;
* terminal attempts and answers, assignments, holds, retention floors, the confirmed clock row, the audit
  entry (but for one field) and the output (but for one line) are as for the same recovery on a database
  with no Part 1 row; a recovery with nothing open writes no Part 1 row; 027 pending refuses;
* the two reviewed forward-fault sequences (+6 h recovered before the stamp, +1 h recovered after it): the
  abandonment persists as time moves past the stamp and the next start creates a new row;
* characterization, not a guarantee: the cases the existing clock layer does **not** detect (a reboot after a
  forward fault, two forward steps of 60 s on one boot, a missing boot-time reading). Only the start, the
  stored state and the persisted clock row are judged here; submit refusal is C3.3's.
"""

from __future__ import annotations

import hashlib
import io
import json
import sqlite3
import unittest
from contextlib import closing
from datetime import datetime, timedelta
from unittest.mock import patch

from src import manage_lifecycle
from tests.support import TEST_CLOCK, reset_lifecycle_clock_row, set_lifecycle_clock_row
from tests.test_dzi_part1_start_service import T0, Part1Case, digest, ts
from web.assignment_tokens import new_public_token
from web.services import dzi_part1 as part1
from web.services import lifecycle

HOUR = timedelta(hours=1)
MIGRATION_027 = "027_dzi_part1_runtime.sql"


def file_digest(path) -> str:
    with open(path, "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()


class RecoveryCase(Part1Case):
    """Part 1 fixtures plus the real operator CLI."""

    def setUp(self):
        super().setUp()
        self.audit_log = self.directory / "lifecycle-operator.log"
        self.teacher = self.add_account("teacher")
        self.accounts = [self.add_account("student") for _ in range(6)]

    # ------------------------------------------------------------------ operator CLI
    def cli(self, *args, writer=False, db=None, audit=None):
        argv = ["--db", str(db or self.db_path), *map(str, args)]
        if writer:
            argv += ["--operator", "тест оператор", "--audit-log", str(audit or self.audit_log)]
        out, err = io.StringIO(), io.StringIO()
        code = manage_lifecycle.main(argv, out=out, err=err)
        return code, out.getvalue(), err.getvalue()

    def inspect(self, verified, *extra, db=None):
        return self.cli("clock-inspect", "--verified-utc", ts(verified), *extra, db=db)

    def recover(self, verified, *extra, db=None, audit=None):
        return self.cli("clock-recover", "--verified-utc", ts(verified), *extra, writer=True, db=db, audit=audit)

    def audit_entries(self):
        if not self.audit_log.exists():
            return []
        return [json.loads(line) for line in self.audit_log.read_text(encoding="utf-8").splitlines()]

    def incident(self, *, verified, old_high_water, db=None, reason="clock_step"):
        """The clock ran to ``old_high_water`` and now reads ``verified``."""
        self.clock.freeze(verified)
        set_lifecycle_clock_row(db or self.db_path, high_water=old_high_water, state="suspect", reason=reason,
                                changed_at=old_high_water)

    # ------------------------------------------------------------------ fixtures
    @staticmethod
    def interval(mode, verified):
        return {"none": (), "covering": ("--suspect-from", ts(verified - 4 * HOUR), "--suspect-to", ts(verified + 8 * HOUR)),
                "elsewhere": ("--suspect-from", ts(verified - 40 * HOUR), "--suspect-to", ts(verified - 39 * HOUR))}[mode]

    def terminal_rows(self, base=T0 - timedelta(days=3)):
        """A submitted (with answers), an expired and an abandoned attempt, on the same account as an open one."""
        who = self.accounts[5]
        started = base
        submitted = self.insert_attempt(who, started=started, submitted_at=ts(started + timedelta(seconds=600)),
                                        score_points=20, score_max=45)
        plan = self.plan_of(submitted)
        self.sql("INSERT INTO dzi_part1_answers (attempt_id, slot_number, subquestion_number, question_id, kind, chosen_letter, "
                 "is_correct, points_possible, points_awarded, grader_version) VALUES (?, 1, 0, ?, 'multiple_choice', 'A', 1, 1, 1, 'v1')",
                 (submitted, plan[0]))
        self.sql("INSERT INTO dzi_part1_answers (attempt_id, slot_number, subquestion_number, question_id, kind, raw_answer, "
                 "is_correct, points_possible, points_awarded, grader_version) VALUES (?, 16, 1, ?, 'short_answer', 'x', 0, 1, 0, 'v1')",
                 (submitted, plan[15]))
        expired = self.insert_attempt(who, started=started + HOUR, expired_at=ts(started + HOUR + timedelta(seconds=9000)))
        abandoned = self.insert_attempt(who, started=started + 2 * HOUR, abandoned_at=ts(started + 2 * HOUR + timedelta(seconds=5)))
        return [submitted, expired, abandoned]

    def part1_digest(self):
        with closing(self.raw()) as conn:
            return (digest(conn.execute("SELECT * FROM dzi_part1_attempts ORDER BY id").fetchall()),
                    digest(conn.execute("SELECT * FROM dzi_part1_answers").fetchall()))

    def assignment(self, *, deadline_at=None, closed_at=None, hold=None) -> int:
        section_id = self.conn.execute("SELECT id FROM curriculum_sections ORDER BY id").fetchone()[0]
        return self.sql(
            "INSERT INTO quiz_assignments (section_id, title_bg, question_count, created_at, owner_user_id, public_token, "
            "deadline_at, display_timezone, closed_at, lifecycle_hold) VALUES (?, 'Тест', 3, ?, ?, ?, ?, 'Europe/Sofia', ?, ?)",
            (section_id, ts(T0 - timedelta(days=1)), self.teacher.user_id, new_public_token(),
             None if deadline_at is None else ts(deadline_at), None if closed_at is None else ts(closed_at), hold))

    def assignment_attempt(self, assignment_id, who, started):
        return self.sql(
            "INSERT INTO quiz_attempts (assignment_id, user_id, student_name, seed, question_ids_json, started_at) "
            "VALUES (?, ?, 'Ученик', 's', '[1]', ?)", (assignment_id, who.user_id, ts(started)))

    def outcome_of(self, who):
        """The start's outcome, or the clock refusal reason, without raising."""
        with closing(self.connect()) as conn:
            try:
                return part1.start_or_resume_part1(conn, who).outcome
            except lifecycle.ClockUnavailable as exc:
                return exc.reason


# =========================================================================== inspect

class InspectTest(RecoveryCase):
    def setUp(self):
        super().setUp()
        self.verified = T0 + HOUR
        self.incident(verified=self.verified, old_high_water=T0 + 10 * HOUR)

    def three_open(self):
        return [self.insert_attempt(self.accounts[0], started=self.verified - 2 * HOUR),
                self.insert_attempt(self.accounts[1], started=self.verified),
                self.insert_attempt(self.accounts[2], started=self.verified + 6 * HOUR)]  # stamped ahead

    def test_the_count_is_the_open_rows_only_and_inspection_writes_nothing(self):
        self.three_open()
        self.terminal_rows()
        for mode in ("none", "covering", "elsewhere"):
            with self.subTest(interval=mode):
                before_file, before_state = file_digest(self.db_path), self.state()
                code, out, err = self.inspect(self.verified, *self.interval(mode, self.verified))
                self.assertEqual((code, err), (0, ""), out)
                self.assertIn("open DZI Part 1 attempts: 3 (clock-recover would mark every one of them abandoned)", out)
                self.assertIn("nothing was changed", out)
                self.assertEqual(out.count("DZI Part 1"), 1)  # only the count, no ids, owners or stamps
                self.assertEqual((file_digest(self.db_path), self.state()), (before_file, before_state))
                self.assertFalse(self.audit_log.exists())

    def test_the_count_is_zero_without_open_rows(self):
        self.terminal_rows()
        code, out, _err = self.inspect(self.verified)
        self.assertEqual(code, 0)
        self.assertIn("open DZI Part 1 attempts: 0 ", out)

    def test_the_inspected_count_is_what_recovery_then_abandons(self):
        self.three_open()
        self.terminal_rows()
        counted = int(self.inspect(self.verified)[1].split("open DZI Part 1 attempts: ")[1].split(" ")[0])
        code, out, err = self.recover(self.verified)
        self.assertEqual(code, 0, err)
        self.assertIn(f"DZI Part 1 attempts abandoned: {counted}", out)
        self.assertEqual(self.audit_entries()[-1]["dzi_part1_abandoned"], counted)
        with closing(self.raw()) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM dzi_part1_attempts WHERE abandoned_at = ? ",
                                          (ts(self.verified),)).fetchone()[0], counted)


# =========================================================================== recover

class RecoverTest(RecoveryCase):
    def setUp(self):
        super().setUp()
        self.verified = T0 + HOUR
        self.incident(verified=self.verified, old_high_water=T0 + 10 * HOUR)

    def open_rows(self):
        v = self.verified
        return {
            "stamped before the verified time": self.insert_attempt(self.accounts[0], started=v - 3 * HOUR),
            "stamped at the verified time": self.insert_attempt(self.accounts[1], started=v),
            "stamped after the verified time (forward fault)": self.insert_attempt(self.accounts[2], started=v + 6 * HOUR),
            "stamped one hour ahead": self.insert_attempt(self.accounts[3], started=v + HOUR),
            "far in the past, long lapsed": self.insert_attempt(self.accounts[4], started=v - 30 * 24 * HOUR),
        }

    def test_every_open_row_is_abandoned_at_the_verified_time_with_and_without_an_interval(self):
        for mode in ("none", "covering", "elsewhere"):
            with self.subTest(interval=mode):
                self.setUp()
                rows = self.open_rows()
                terminal = self.terminal_rows()
                before = {i: self.attempt(i) for i in [*rows.values(), *terminal]}
                answers_before = self.part1_digest()[1]
                code, out, err = self.recover(self.verified, *self.interval(mode, self.verified))
                self.assertEqual(code, 0, out + err)
                for name, attempt_id in rows.items():
                    after = self.attempt(attempt_id)
                    self.assertEqual(after["abandoned_at"], ts(self.verified), name)
                    self.assertEqual({k: v for k, v in after.items() if k != "abandoned_at"},
                                     {k: v for k, v in before[attempt_id].items() if k != "abandoned_at"}, name)
                for attempt_id in terminal:  # submitted, expired and abandoned rows are byte-for-byte as they were
                    self.assertEqual(self.attempt(attempt_id), before[attempt_id])
                self.assertEqual(self.part1_digest()[1], answers_before)
                self.assertIn(f"DZI Part 1 attempts abandoned: {len(rows)}", out)
                self.assertEqual(self.audit_entries()[-1]["dzi_part1_abandoned"], len(rows))
                self.assertLess(self.attempt(rows["stamped after the verified time (forward fault)"])["abandoned_at"],
                                self.attempt(rows["stamped after the verified time (forward fault)"])["started_at"])

    def test_the_clock_row_and_the_audit_entry_keep_their_existing_contract(self):
        self.open_rows()
        self.assertEqual(self.recover(self.verified)[0], 0)
        clock = self.clock_row()
        self.assertEqual((clock["state"], clock["high_water_at"], clock["ref_boot_id"]),
                         ("ok", ts(self.verified), TEST_CLOCK.boot_id()))
        entry = self.audit_entries()[-1]
        self.assertEqual((entry["action"], entry["operator"], entry["new_high_water"]), ("clock_recover", "тест оператор", ts(self.verified)))
        self.assertEqual(sorted(entry), sorted(["at", "action", "operator", "database", "verified_utc", "local_clock", "new_high_water",
                                                "previous", "suspect_interval", "held", "retention_floor_raised",
                                                "boot_anchor_written", "dzi_part1_abandoned"]))  # exactly one new field

    def test_a_recovery_with_nothing_open_writes_no_part1_row(self):
        terminal = self.terminal_rows()
        before = self.part1_digest()
        code, out, _err = self.recover(self.verified)
        self.assertEqual(code, 0)
        self.assertIn("DZI Part 1 attempts abandoned: 0", out)
        self.assertEqual(self.part1_digest(), before)
        self.assertEqual(self.audit_entries()[-1]["dzi_part1_abandoned"], 0)
        # and with no Part 1 row at all
        self.sql("DELETE FROM dzi_part1_answers")
        self.sql("DELETE FROM dzi_part1_attempts")
        self.incident(verified=self.verified, old_high_water=T0 + 10 * HOUR)
        self.assertEqual(self.recover(self.verified)[0], 0)
        self.assertEqual(self.attempts(), [])
        self.assertTrue(terminal)

    def test_a_recovery_run_twice_abandons_nothing_the_second_time(self):
        rows = self.open_rows()
        self.recover(self.verified)
        stamped = {i: self.attempt(i) for i in rows.values()}
        self.incident(verified=self.verified + timedelta(minutes=5), old_high_water=T0 + 10 * HOUR)
        code, out, _err = self.recover(self.verified + timedelta(minutes=5))
        self.assertEqual(code, 0)
        self.assertIn("DZI Part 1 attempts abandoned: 0", out)
        self.assertEqual({i: self.attempt(i) for i in rows.values()}, stamped)  # the first stamp is kept

    def test_recovery_with_an_unusable_clock_row_still_abandons(self):
        rows = self.open_rows()
        self.sql("DELETE FROM lifecycle_clock")
        self.clock.freeze(self.verified)
        code, out, err = self.recover(self.verified)
        self.assertEqual(code, 0, out + err)
        self.assertTrue(all(self.attempt(i)["abandoned_at"] == ts(self.verified) for i in rows.values()))
        self.assertEqual(self.clock_row()["state"], "ok")


class BenignRecoveryUnchangedTest(RecoveryCase):
    """The same recovery on a database with and without Part 1 rows: everything else is identical."""

    def build_assignments(self):
        verified = T0 + HOUR
        passed = self.assignment(deadline_at=T0 + timedelta(minutes=30))
        uncertain = self.assignment(deadline_at=T0 + 5 * HOUR)
        future = self.assignment(deadline_at=T0 + 20 * HOUR)
        closed = self.assignment(closed_at=T0 - HOUR)
        stamped_ahead = self.assignment(closed_at=T0 + 3 * HOUR, hold="restore_pending")
        started_ahead = self.assignment()
        self.assignment_attempt(started_ahead, self.accounts[0], T0 + 2 * HOUR)
        return verified, (passed, uncertain, future, closed, stamped_ahead, started_ahead)

    def test_assignment_state_clock_audit_and_output_match_a_recovery_with_no_part1_rows(self):
        verified, assignments = self.build_assignments()
        plain = self.directory / "no-part1-rows.db"
        with closing(sqlite3.connect(self.db_path)) as src, closing(sqlite3.connect(plain)) as dst:
            src.backup(dst)
        # only the first database gets Part 1 rows: open ones of every kind and terminal ones with answers
        self.insert_attempt(self.accounts[1], started=verified - 2 * HOUR)
        self.insert_attempt(self.accounts[2], started=verified + 6 * HOUR)
        terminal = self.terminal_rows()
        terminal_before = {i: self.attempt(i) for i in terminal}
        answers_before = self.part1_digest()[1]
        results = {}
        for label, db in (("with", self.db_path), ("without", plain)):
            self.audit_log = self.directory / f"{label}.log"
            self.incident(verified=verified, old_high_water=T0 + 10 * HOUR, db=db)
            results[label] = (self.recover(verified, db=db), self.audit_entries()[-1])
        (code_w, out_w, err_w), entry_w = results["with"]
        (code_n, out_n, err_n), entry_n = results["without"]
        self.assertEqual((code_w, err_w, code_n, err_n), (0, "", 0, ""))
        strip = lambda text: [line for line in text.splitlines() if "DZI Part 1" not in line]
        self.assertEqual(strip(out_w), strip(out_n))
        self.assertIn("DZI Part 1 attempts abandoned: 2", out_w)
        self.assertIn("DZI Part 1 attempts abandoned: 0", out_n)
        ignore = {"database", "dzi_part1_abandoned"}
        self.assertEqual({k: v for k, v in entry_w.items() if k not in ignore}, {k: v for k, v in entry_n.items() if k not in ignore})
        self.assertEqual((entry_w["dzi_part1_abandoned"], entry_n["dzi_part1_abandoned"]), (2, 0))

        def tables(path):
            with closing(sqlite3.connect(path)) as conn:
                names = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' "
                                                    "AND name NOT LIKE 'dzi_part1_%' ORDER BY name")]
                return {n: digest(conn.execute(f'SELECT * FROM "{n}"').fetchall()) for n in names}

        self.assertEqual(tables(self.db_path), tables(plain))  # assignments, holds, floors, attempts, answers, the clock row
        for assignment_id in assignments:
            with closing(sqlite3.connect(self.db_path)) as a, closing(sqlite3.connect(plain)) as b:
                query = "SELECT * FROM quiz_assignments WHERE id = ?"
                self.assertEqual(a.execute(query, (assignment_id,)).fetchall(), b.execute(query, (assignment_id,)).fetchall())
        with closing(sqlite3.connect(self.db_path)) as conn:
            clock = conn.execute("SELECT state, high_water_at FROM lifecycle_clock").fetchone()
        self.assertEqual(tuple(clock), ("ok", ts(verified)))
        for attempt_id in terminal:
            self.assertEqual(self.attempt(attempt_id), terminal_before[attempt_id])
        self.assertEqual(self.part1_digest()[1], answers_before)
        # the assignments really were exercised: some are held by this recovery
        with closing(sqlite3.connect(plain)) as conn:
            held = conn.execute("SELECT COUNT(*) FROM quiz_assignments WHERE lifecycle_hold LIKE '%clock_review%'").fetchone()[0]
        self.assertGreaterEqual(held, 3)

    def test_a_later_assignment_write_is_not_blocked_and_the_next_start_creates_a_new_attempt(self):
        verified = T0 + HOUR
        old = self.insert_attempt(self.accounts[0], started=verified + 6 * HOUR)
        self.incident(verified=verified, old_high_water=T0 + 10 * HOUR)
        self.assertEqual(self.recover(verified)[0], 0)
        with self.connect() as conn:  # every assignment writer opens its transaction like this
            with lifecycle.write_transaction(conn) as transaction:
                transaction.admit()
        self.assertEqual(self.clock_row()["state"], "ok")
        result = part1.start_or_resume_part1(self.connect(), self.accounts[0])
        self.assertEqual(result.outcome, part1.OUTCOME_CREATED)
        self.assertNotEqual(result.attempt_id, old)
        self.assertEqual(self.attempt(old)["abandoned_at"], ts(verified))


class RecoveryRollbackTest(RecoveryCase):
    """The count and the marks are in the one established transaction: any failure restores every row."""

    def setUp(self):
        super().setUp()
        self.verified = T0 + HOUR
        self.incident(verified=self.verified, old_high_water=T0 + 10 * HOUR)
        self.held = self.assignment(deadline_at=T0 + 5 * HOUR)  # in doubt: the recovery would hold it
        self.rows = [self.insert_attempt(self.accounts[i], started=self.verified + (i - 1) * HOUR) for i in range(3)]

    def assert_untouched(self, before_tables):
        self.assertEqual(self.state(), before_tables)
        self.assertEqual(self.clock_row()["state"], "suspect")
        self.assertTrue(all(self.attempt(i)["abandoned_at"] is None for i in self.rows))
        with closing(self.raw()) as conn:
            self.assertIsNone(conn.execute("SELECT lifecycle_hold FROM quiz_assignments WHERE id = ?", (self.held,)).fetchone()[0])

    def test_each_failure_restores_every_row_with_and_without_a_suspect_interval(self):
        failures = {
            "audit log cannot be written": lambda: ("audit", str(self.directory / "missing-directory" / "audit.log")),
            "a Part 1 update is refused": lambda: ("sql", "CREATE TRIGGER stop_mark BEFORE UPDATE ON dzi_part1_attempts "
                                                    f"WHEN NEW.id = {self.rows[1]} BEGIN SELECT RAISE(ABORT, 'refused'); END"),
            "the clock row update after the marks is refused": lambda: ("sql", "CREATE TRIGGER stop_clock BEFORE UPDATE ON lifecycle_clock "
                                                                        "BEGIN SELECT RAISE(ABORT, 'refused'); END"),
        }
        for mode in ("none", "covering"):
            for name, make in failures.items():
                with self.subTest(interval=mode, failure=name):
                    self.setUp()
                    kind, value = make()
                    audit = None
                    if kind == "sql":
                        self.sql(value)
                    else:
                        audit = value
                    before = self.state()
                    code, out, err = self.recover(self.verified, *self.interval(mode, self.verified), audit=audit)
                    self.assertEqual(code, 2, out + err)
                    self.assertFalse(self.audit_log.exists())
                    self.assert_untouched(before)

    def test_a_local_clock_that_disagrees_refuses_before_any_mark(self):
        for mode in ("none", "covering"):
            with self.subTest(interval=mode):
                self.setUp()
                self.clock.advance(600)  # the local clock reads ten minutes after the verified time
                before = self.state()
                code, out, err = self.recover(self.verified, *self.interval(mode, self.verified))
                self.assertEqual(code, 1, out + err)
                self.assertIn("correct the clock first", err)
                self.assert_untouched(before)

    def test_with_027_pending_both_commands_refuse_and_write_nothing(self):
        self.sql("DELETE FROM schema_migrations WHERE filename = ?", (MIGRATION_027,))
        before_file = file_digest(self.db_path)
        for name, run in (("clock-inspect", lambda: self.inspect(self.verified)), ("clock-recover", lambda: self.recover(self.verified))):
            with self.subTest(command=name):
                code, out, err = run()
                self.assertEqual(code, 2)
                self.assertIn(f"pending migrations: {MIGRATION_027}", err)
                self.assertNotIn("DZI Part 1", out)
                self.assertEqual(file_digest(self.db_path), before_file)
                self.assertFalse(self.audit_log.exists())

    def test_a_database_with_an_unshipped_migration_applied_is_refused_too(self):
        self.sql("INSERT INTO schema_migrations (filename, applied_at) VALUES ('028_future.sql', '2026-01-01 00:00:00')")
        code, _out, err = self.recover(self.verified)
        self.assertEqual(code, 2)
        self.assertIn("this build does not ship: 028_future.sql", err)
        self.assertEqual(self.clock_row()["state"], "suspect")


# =========================================================================== reviewed forward-fault sequences

class ForwardFaultSequenceTest(RecoveryCase):
    """The two reviewed sequences of the #375 review (judged by start, stored state and persisted clock)."""

    def forward_first_write(self, ahead: timedelta):
        """The first lifecycle write of a boot, on a clock that reads ``ahead`` too far."""
        reset_lifecycle_clock_row(self.db_path)  # no boot anchor yet, the seeded high-water mark
        self.clock.freeze(T0 + ahead)

    def read_state(self, attempt_id):
        with closing(self.connect()) as conn:
            row = conn.execute("SELECT * FROM dzi_part1_attempts WHERE id = ?", (attempt_id,)).fetchone()
            stored = part1.parse_stored_attempt(row)
            read = lifecycle.read_clock(conn)
        return part1.attempt_state(stored.started_at, stored.deadline_at, stored.submit_cutoff_at, read.now), read

    def test_plus_6_hours_recovered_ten_minutes_in_before_the_stamp(self):
        self.forward_first_write(6 * HOUR)
        first = part1.start_or_resume_part1(self.conn, self.student)
        self.assertTrue(first.created)
        stamp = T0 + 6 * HOUR
        self.assertEqual(first.started_at, ts(stamp))
        self.assertEqual(self.clock_row()["high_water_at"], ts(stamp))
        self.clock.advance(600)           # ten real minutes pass on the faulty clock...
        self.clock.step_wall(-6 * 3600)   # ...and the clock is then corrected: it reads T0 + 10 min

        # before any recovery: the read state is not future-start (admit() pinned the mark at the stamp) and every
        # writing request is refused, the first as a regression, and nothing is stored but the clock fault
        state, read = self.read_state(first.attempt_id)
        self.assertEqual(state, "open")
        self.assertEqual(read.now, stamp)
        self.assertFalse(read.healthy)
        rows_before = self.attempts()
        self.assertEqual(self.outcome_of(self.student), "clock_regression")
        self.assertEqual(self.outcome_of(self.other), "clock_suspect")
        self.assertEqual(self.attempts(), rows_before)
        self.assertEqual(self.clock_row()["state"], "suspect")

        verified = T0 + timedelta(seconds=600)
        code, out, _err = self.inspect(verified)
        self.assertIn("open DZI Part 1 attempts: 1 ", out)
        self.assertEqual(self.attempts(), rows_before)
        code, out, err = self.recover(verified)
        self.assertEqual(code, 0, out + err)
        self.assertIn("DZI Part 1 attempts abandoned: 1", out)
        row = self.attempt(first.attempt_id)
        self.assertEqual(row["abandoned_at"], ts(verified))
        self.assertLess(row["abandoned_at"], row["started_at"])
        self.assertEqual((self.clock_row()["state"], self.clock_row()["high_water_at"]), ("ok", ts(verified)))

        # time moves past the stamp and past what would have been the deadline and cutoff: nothing revives it
        for target in (stamp + timedelta(seconds=1), stamp + timedelta(seconds=5400), stamp + timedelta(seconds=5461)):
            self.clock.set(target)
            state, read = self.read_state(first.attempt_id)
            self.assertEqual(self.attempt(first.attempt_id), row)  # abandoned stays abandoned; never expired, never open
            self.assertTrue(read.healthy)
        self.assertEqual(self.attempt(first.attempt_id)["expired_at"], None)
        # the next start creates a new attempt; the abandoned row is not touched
        second = part1.start_or_resume_part1(self.conn, self.student)
        self.assertEqual(second.outcome, part1.OUTCOME_CREATED)
        self.assertNotEqual(second.attempt_id, first.attempt_id)
        self.assertEqual(second.started_at, ts(stamp + timedelta(seconds=5461)))
        self.assertEqual(self.attempt(first.attempt_id), row)
        self.assertEqual(self.clock_row()["state"], "ok")
        self.assertEqual(self.clock_row()["high_water_at"], ts(stamp + timedelta(seconds=5461)))

    def run_plus_one_hour(self, interval_mode):
        self.setUp()
        self.forward_first_write(HOUR)
        first = part1.start_or_resume_part1(self.conn, self.student)
        stamp = T0 + HOUR
        self.assertEqual(first.started_at, ts(stamp))
        self.clock.advance(7200)          # two real hours pass
        self.clock.step_wall(-3600)       # the clock is corrected: it reads T0 + 2 h
        verified = T0 + 2 * HOUR
        # the row was never future-stamped: it is open with 30 minutes shown, and a writing request is refused
        state, read = self.read_state(first.attempt_id)
        self.assertEqual((state, read.now), ("open", verified))
        self.assertEqual(self.outcome_of(self.student), "clock_step")
        self.assertEqual(self.clock_row()["state"], "suspect")
        self.assertEqual(self.attempt(first.attempt_id)["abandoned_at"], None)
        code, out, err = self.recover(verified, *self.interval(interval_mode, verified))
        self.assertEqual(code, 0, out + err)
        self.assertIn("DZI Part 1 attempts abandoned: 1", out)
        row = self.attempt(first.attempt_id)
        self.assertEqual(row["abandoned_at"], ts(verified))
        self.assertGreater(row["abandoned_at"], row["started_at"])  # stamped later than its false start
        self.assertEqual((self.clock_row()["state"], self.clock_row()["high_water_at"]), ("ok", ts(verified)))
        # real time moves on beyond the old deadline and cutoff: still abandoned, then a new attempt
        self.clock.advance(5400 + 61)
        self.assertEqual(self.attempt(first.attempt_id), row)
        second = part1.start_or_resume_part1(self.conn, self.student)
        self.assertEqual(second.outcome, part1.OUTCOME_CREATED)
        self.assertEqual(self.attempt(first.attempt_id), row)
        self.assertEqual(self.attempt(second.attempt_id)["started_at"], ts(verified + timedelta(seconds=5461)))

    def test_plus_1_hour_recovered_two_hours_in_after_the_stamp_with_and_without_an_interval(self):
        for mode in ("none", "covering", "elsewhere"):
            with self.subTest(interval=mode):
                self.run_plus_one_hour(mode)

    def test_a_clock_fault_blocks_neither_other_assignment_work_after_recovery(self):
        self.run_plus_one_hour("none")
        with self.connect() as conn, lifecycle.write_transaction(conn) as transaction:
            transaction.admit()


# =========================================================================== not detected / characterization

class UndetectedCasesTest(RecoveryCase):
    """What the existing clock layer does not (or only partly) detect. A characterization for the owner's
    activation decision, not a guarantee: boot identity and boot-time input on the deployed host, and the
    owner's acceptance of these cases, remain activation prerequisites."""

    def forward_first_write(self, ahead):
        reset_lifecycle_clock_row(self.db_path)
        self.clock.freeze(T0 + ahead)

    def remaining(self, result, now):
        return int((lifecycle.parse_timestamp(result.deadline_at) - now).total_seconds())

    def test_reboot_after_a_forward_fault_leaves_the_attempt_open_with_the_clock_ok(self):
        self.forward_first_write(HOUR)
        first = part1.start_or_resume_part1(self.conn, self.student)
        stamp = T0 + HOUR
        self.assertEqual((first.started_at, self.clock_row()["high_water_at"]), (ts(stamp), ts(stamp)))
        self.clock.step_wall(-3600)       # corrected, with no lifecycle transaction at all
        self.clock.reboot()               # a new boot identity and boot time
        self.clock.advance(3595)          # the wall clock comes back to five seconds behind the mark
        self.assertEqual(self.clock.wall(), stamp - timedelta(seconds=5))
        self.assertEqual(self.outcome_of(self.student), "resumed")  # admitted: no fault is recorded
        self.assertEqual((self.clock_row()["state"], self.clock_row()["state_reason"]), ("ok", None))
        # 8,400 real seconds after the true start, against a 5,400 s limit
        self.clock.set(T0 + timedelta(seconds=8400))
        result = part1.start_or_resume_part1(self.conn, self.student)
        self.assertEqual((result.outcome, result.state, result.attempt_id), ("resumed", "open", first.attempt_id))
        self.assertEqual(self.remaining(result, self.clock.wall()), 600)  # stored time left, though 8,400 s have really passed
        self.assertGreater(8400, 5400)
        self.assertEqual((self.clock_row()["state"], self.clock_row()["state_reason"]), ("ok", None))
        self.assertTrue(lifecycle.read_clock(self.conn).healthy)
        # no recovery is prompted and none ran: inspection merely counts the open row
        self.assertEqual(self.attempt(first.attempt_id)["abandoned_at"], None)

    def test_several_attempts_started_during_one_fault_all_keep_the_extra_time(self):
        self.forward_first_write(HOUR)
        people = [self.accounts[0], self.accounts[1], self.accounts[2]]
        results = [part1.start_or_resume_part1(self.conn, people[0])]
        for who in people[1:]:
            self.clock.advance(600)
            results.append(part1.start_or_resume_part1(self.conn, who))
        self.assertEqual([r.started_at for r in results], [ts(T0 + HOUR + timedelta(seconds=n)) for n in (0, 600, 1200)])
        self.clock.step_wall(-3600)
        self.clock.reboot()
        mark = T0 + HOUR + timedelta(seconds=1200)
        self.clock.set(mark - timedelta(seconds=5))
        self.assertEqual(self.outcome_of(people[0]), "resumed")
        self.clock.set(T0 + timedelta(seconds=8400))
        for index, (who, result) in enumerate(zip(people, results)):
            again = part1.start_or_resume_part1(self.conn, who)
            self.assertEqual((again.outcome, again.state), ("resumed", "open"))
            self.assertEqual(self.remaining(again, self.clock.wall()), 600 * (index + 1))
        self.assertEqual(self.clock_row()["state"], "ok")

    def test_the_same_correction_without_a_reboot_is_refused_and_latches(self):
        self.forward_first_write(HOUR)
        part1.start_or_resume_part1(self.conn, self.student)
        self.clock.step_wall(-3600)
        self.assertEqual(self.outcome_of(self.other), "clock_regression")  # more than 5 s behind the mark
        self.assertEqual(self.clock_row()["state"], "suspect")
        self.clock.advance(3600)
        self.assertEqual(self.outcome_of(self.other), "clock_suspect")     # latched until clock-recover
        self.assertEqual(self.attempts()[0]["abandoned_at"], None)

    def test_a_same_boot_request_once_the_wall_clock_passes_the_mark_is_a_step_fault(self):
        self.forward_first_write(HOUR)
        part1.start_or_resume_part1(self.conn, self.student)
        self.clock.step_wall(-3600)
        self.clock.advance(3600)  # no request in between: the wall clock reaches the mark
        self.assertEqual(self.outcome_of(self.other), "clock_step")
        self.assertEqual(self.outcome_of(self.other), "clock_suspect")

    def test_a_reboot_followed_by_a_request_before_the_mark_is_a_regression(self):
        self.forward_first_write(HOUR)
        part1.start_or_resume_part1(self.conn, self.student)
        self.clock.step_wall(-3600)
        self.clock.reboot()
        self.clock.advance(60)
        self.assertEqual(self.outcome_of(self.other), "clock_regression")
        self.assertEqual(self.clock_row()["state"], "suspect")

    def two_forward_steps(self, anchor_ahead, second_ahead):
        """A boot anchors ``anchor_ahead`` s ahead, a later start is ``second_ahead`` s ahead, then the clock is
        corrected and a request comes five seconds behind the mark. Returns the three outcomes."""
        reset_lifecycle_clock_row(self.db_path)
        self.clock.freeze(T0 + timedelta(seconds=anchor_ahead))
        who = [self.add_account("student") for _ in range(3)]
        first = self.outcome_of(who[0])
        self.clock.advance(1000)
        self.clock.step_wall(second_ahead - anchor_ahead)
        second = self.outcome_of(who[1])
        self.clock.step_wall(-second_ahead)
        mark = lifecycle.parse_timestamp(self.clock_row()["high_water_at"])
        self.clock.set(mark - timedelta(seconds=5))
        corrected = self.outcome_of(who[2])
        return first, second, corrected

    def test_two_forward_steps_on_one_boot_can_keep_up_to_120_seconds(self):
        """The #375 revision-6 case: anchor 60 s ahead, a later start 120 s ahead, correction, then a request at
        five seconds behind the mark is admitted with the clock ok."""
        cases = {(60, 120): ("created", "created", "created"), (40, 90): ("created", "created", "created"),
                 (60, 121): ("created", "clock_step", "clock_suspect"), (61, 120): ("created", "created", "clock_step")}
        for (anchor, second), expected in cases.items():
            with self.subTest(anchor_ahead=anchor, second_step_ahead=second):
                self.setUp()
                self.assertEqual(self.two_forward_steps(anchor, second), expected)
        self.setUp()
        self.two_forward_steps(60, 120)
        self.assertEqual(self.clock_row()["state"], "ok")
        rows = self.attempts()
        self.assertEqual(len(rows), 3)
        # the second attempt was stamped 120 s ahead of true time (true time T0 + 1000 s)
        self.assertEqual(rows[1]["started_at"], ts(T0 + timedelta(seconds=1120)))

    def test_a_fault_of_30_seconds_is_admitted_unless_a_request_arrives_in_the_refused_window(self):
        part1.start_or_resume_part1(self.conn, self.student)
        self.clock.step_wall(30)
        second = part1.start_or_resume_part1(self.conn, self.other)  # within the 60 s step tolerance
        self.assertEqual(second.started_at, ts(T0 + timedelta(seconds=30)))
        self.clock.step_wall(-30)  # corrected: 30 s behind the mark
        self.assertEqual(self.outcome_of(self.accounts[0]), "clock_regression")  # one request more than 5 s behind
        self.clock.advance(30)  # the wall clock reaches the mark
        self.assertEqual(self.outcome_of(self.accounts[0]), "clock_suspect")  # still latched after it reaches the mark
        self.assertEqual(self.clock_row()["state"], "suspect")

    def test_a_fault_of_30_seconds_with_no_request_in_the_window_is_never_noticed(self):
        part1.start_or_resume_part1(self.conn, self.student)
        self.clock.step_wall(30)
        part1.start_or_resume_part1(self.conn, self.other)
        self.clock.step_wall(-30)
        self.clock.advance(25)  # five seconds behind the mark
        self.assertEqual(self.outcome_of(self.accounts[0]), "created")
        self.assertEqual(self.clock_row()["state"], "ok")

    def test_a_boot_identity_without_a_boot_time_reading_has_no_step_check(self):
        part1.start_or_resume_part1(self.conn, self.student)
        with patch.object(TEST_CLOCK, "boottime_ms", return_value=None):
            self.clock.step_wall(3600)
            result = part1.start_or_resume_part1(self.conn, self.other)  # admitted: nothing to compare against
        self.assertEqual(result.started_at, ts(T0 + HOUR))
        self.assertEqual(self.clock_row()["state"], "ok")
        # the same step with both values present is refused
        self.setUp()
        part1.start_or_resume_part1(self.conn, self.student)
        self.clock.step_wall(3600)
        self.assertEqual(self.outcome_of(self.other), "clock_step")

    def test_no_boot_identity_at_all_has_no_step_check_either(self):
        part1.start_or_resume_part1(self.conn, self.student)
        with patch.object(TEST_CLOCK, "boot_id", return_value=None):
            self.clock.step_wall(3600)
            result = part1.start_or_resume_part1(self.conn, self.other)
        self.assertEqual(result.started_at, ts(T0 + HOUR))
        self.assertEqual(self.clock_row()["state"], "ok")

    def test_an_unsynchronised_clock_is_refused_at_the_first_write_of_a_boot(self):
        reset_lifecycle_clock_row(self.db_path)
        TEST_CLOCK.sync = False
        self.assertEqual(self.outcome_of(self.student), "no_sync_evidence")
        self.assertEqual(self.attempts(), [])


if __name__ == "__main__":
    unittest.main()
