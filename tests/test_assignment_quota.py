"""Per-account assignment creation limits (EMAIL_REGISTRATION PR 7; scope I1 to I11).

At most 20 creations in a rolling 24 hours and at most 10 open assignments, decided inside
``quiz_create_assignment`` under the lifecycle writer lock. Every database is a temporary
clone of the migrated canonical template and every vault is a temporary directory; time is
frozen (``tests.support.lifecycle_time``); nothing reads ``data/questions.db``.

The service tests call ``quiz_create_assignment`` the way the routes do. The route tests go
through the real application, so the refusals are seen as a teacher sees them.
"""

import re
import sqlite3
import threading
import time
from contextlib import ExitStack, closing
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from tests.auth_helpers import create_account, set_account_session
from tests.support import NO_DEADLINE_FORM, ROOT, IsolatedAppTestCase, lifecycle_time, web_app
from web.access import ANONYMOUS, Principal, account_principal
from web.services import assignment_quota, lifecycle, quiz

T0 = datetime(2026, 3, 2, 9, 0, 0)
HOUR = timedelta(hours=1)
SAME_ORIGIN = {"Origin": "http://localhost"}
TABLES = ("quiz_assignments", "assignment_creations", "lifecycle_clock")
Q = quiz.quiz_create_assignment


class QuotaTestCase(IsolatedAppTestCase):
    """A frozen lifecycle clock and fresh accounts for every test."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        with closing(web_app.quiz_db()) as conn:
            for row in conn.execute("SELECT id FROM curriculum_sections ORDER BY id"):
                if not quiz.quiz_section_question_ids(conn, row["id"]):
                    continue
                candidates = web_app.fetch_open_question_candidates(conn, section_id=row["id"])
                if candidates:
                    cls.section_id = row["id"]
                    cls.source_slug = candidates[0]["source_slug"]
                    break
            else:
                raise AssertionError("No canonical mixed section")

    def setUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        self.clock = stack.enter_context(lifecycle_time(T0))
        self.teacher = create_account(web_app.QUIZ_DB_PATH, "teacher")
        self.other = create_account(web_app.QUIZ_DB_PATH, "teacher")
        self.admin = create_account(web_app.QUIZ_DB_PATH, "admin")
        self.student = create_account(web_app.QUIZ_DB_PATH, "student")

    # ------------------------------------------------------------------ plumbing

    def connect(self):
        return web_app.quiz_db()

    def principal(self, account, role="teacher"):
        return account_principal(account["id"], role, "Учител")

    def create(self, account, role="teacher", **kwargs):
        """One creation in a transaction of its own, committed; the service's refusal propagates."""
        kwargs.setdefault("title_bg", "Тест")
        kwargs.setdefault("question_count", 1)
        with closing(self.connect()) as conn:
            assignment_id = Q(conn, principal=self.principal(account, role), section_id=self.section_id, **kwargs)
            conn.commit()
        return assignment_id

    def close(self, account, assignment_id):
        with closing(self.connect()) as conn:
            return lifecycle.close_assignment(conn, assignment_id, principal_user_id=account["id"])

    def create_closed(self, account, role="teacher", **kwargs):
        assignment_id = self.create(account, role, **kwargs)
        self.close(account, assignment_id)
        return assignment_id

    def at(self, moment):
        self.clock.set(moment)

    def sql(self, statement, *params):
        with closing(self.connect()) as conn:
            rows = [tuple(row) for row in conn.execute(statement, params).fetchall()]
            conn.commit()
        return rows

    def ledger(self, account):
        return self.sql(
            "SELECT assignment_id, created_at FROM assignment_creations WHERE owner_user_id = ? ORDER BY id",
            account["id"])

    def assignments(self, account):
        return [row[0] for row in self.sql(
            "SELECT id FROM quiz_assignments WHERE owner_user_id = ? ORDER BY id", account["id"])]

    def open_count(self, account):
        with closing(self.connect()) as conn:
            return lifecycle.count_unclosed_owned(conn, account["id"], lifecycle.format_timestamp(self.clock.wall()))

    def digest(self):
        with closing(self.connect()) as conn:
            return {table: [tuple(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY 1")]
                    for table in TABLES}

    def assertRefused(self, reason, account=None, role="teacher", **kwargs):
        """The creation is refused with ``reason`` and the database is exactly as it was."""
        before = self.digest()
        with self.assertRaises(assignment_quota.QuotaExceeded) as caught:
            self.create(account or self.teacher, role, **kwargs)
        self.assertEqual(caught.exception.reason, reason)
        self.assertEqual(self.digest(), before)

    def fill(self, account, creations, *, closed=True):
        """``creations`` creations now, closed again unless told otherwise (they still count)."""
        make = self.create_closed if closed else self.create
        return [make(account) for _ in range(creations)]


class ConstantsTest(QuotaTestCase):
    def test_the_approved_values(self):
        self.assertEqual(assignment_quota.DAILY_CREATION_LIMIT, 20)
        self.assertEqual(assignment_quota.OPEN_ASSIGNMENT_LIMIT, 10)
        self.assertEqual(assignment_quota.CREATION_WINDOW, timedelta(hours=24))
        self.assertEqual(assignment_quota.EXEMPT_ROLES, frozenset({"admin"}))

    def test_every_refusal_has_a_bulgarian_message_and_a_status(self):
        for reason in (assignment_quota.REASON_DAILY, assignment_quota.REASON_OPEN):
            self.assertRegex(assignment_quota.MESSAGES_BG[reason], r"[А-Яа-я]")
            self.assertNotRegex(assignment_quota.MESSAGES_BG[reason], r"[A-Za-z]")
        self.assertRegex(assignment_quota.MESSAGES_BG[assignment_quota.REASON_ACCOUNTING], r"[А-Яа-я]")
        self.assertNotRegex(assignment_quota.MESSAGES_BG[assignment_quota.REASON_ACCOUNTING], r"[A-Za-z]")
        self.assertEqual(assignment_quota.STATUS_CODES,
                         {assignment_quota.REASON_DAILY: 429, assignment_quota.REASON_OPEN: 409,
                          assignment_quota.REASON_ACCOUNTING: 503})
        self.assertIn("20", assignment_quota.MESSAGES_BG[assignment_quota.REASON_DAILY])
        self.assertIn("10", assignment_quota.MESSAGES_BG[assignment_quota.REASON_OPEN])


class CreationWindowTest(QuotaTestCase):
    def test_the_twentieth_is_allowed_and_the_twenty_first_refused(self):
        self.fill(self.teacher, 19)
        self.fill(self.teacher, 1)  # the 20th
        self.assertEqual(len(self.ledger(self.teacher)), 20)
        self.assertRefused(assignment_quota.REASON_DAILY)
        self.assertEqual(len(self.ledger(self.teacher)), 20)
        self.assertEqual(len(self.assignments(self.teacher)), 20)

    def test_the_window_is_exactly_twenty_four_hours_and_half_open(self):
        first = self.create_closed(self.teacher)                    # T0
        self.at(T0 + HOUR)
        self.fill(self.teacher, 19)                                 # T0 + 1 h: 20 in the window
        self.assertRefused(assignment_quota.REASON_DAILY)
        self.at(T0 + timedelta(hours=24) - timedelta(seconds=1))    # the first is 23:59:59 old
        self.assertRefused(assignment_quota.REASON_DAILY)
        self.at(T0 + timedelta(hours=24))                           # exactly 24 h old: no longer counted
        again = self.create_closed(self.teacher)
        self.assertNotEqual(again, first)
        self.assertEqual(len(self.ledger(self.teacher)), 21)        # history is kept...
        self.assertRefused(assignment_quota.REASON_DAILY)           # ...and the window is full again (20)
        self.at(T0 + timedelta(hours=25))                           # the 19 older creations age out together
        self.fill(self.teacher, 19)
        self.assertRefused(assignment_quota.REASON_DAILY)

    def test_creations_are_stamped_with_the_writer_clock(self):
        self.at(T0 + timedelta(minutes=5, seconds=7))
        assignment_id = self.create(self.teacher)
        self.assertEqual(self.ledger(self.teacher), [(assignment_id, "2026-03-02 09:05:07")])

    def test_closing_does_not_refund_the_creation_allowance(self):
        self.fill(self.teacher, 20, closed=True)
        self.assertEqual(self.open_count(self.teacher), 0)          # nothing is open...
        self.assertRefused(assignment_quota.REASON_DAILY)            # ...and nothing is given back

    def test_a_refusal_consumes_nothing(self):
        self.fill(self.teacher, 20)
        for _ in range(3):
            self.assertRefused(assignment_quota.REASON_DAILY)
        self.at(T0 + timedelta(hours=24, seconds=1))
        self.fill(self.teacher, 20)                                  # all 20 slots return together, no more
        self.assertRefused(assignment_quota.REASON_DAILY)

    def test_accounts_are_independent(self):
        self.fill(self.teacher, 20)
        self.assertRefused(assignment_quota.REASON_DAILY)
        created = self.fill(self.other, 1)
        self.assertEqual(self.assignments(self.other), created)
        self.assertEqual(len(self.ledger(self.other)), 1)

    def test_a_ledger_row_dated_in_the_future_still_counts(self):
        self.fill(self.teacher, 19)
        self.sql("INSERT INTO assignment_creations (owner_user_id, assignment_id, created_at) VALUES (?, ?, ?)",
                 self.teacher["id"], 9_000_001, "2026-03-09 09:00:00")
        self.assertRefused(assignment_quota.REASON_DAILY)


class OpenLimitTest(QuotaTestCase):
    def spaced(self, account, count, **kwargs):
        """``count`` open assignments, an hour apart, so that the daily limit is never near."""
        ids = []
        for _ in range(count):
            ids.append(self.create(account, **kwargs))
            self.at(self.clock.wall() + HOUR)
        return ids

    def test_the_tenth_is_allowed_and_the_eleventh_refused(self):
        ids = self.spaced(self.teacher, 10)
        self.assertEqual(self.open_count(self.teacher), 10)
        self.assertRefused(assignment_quota.REASON_OPEN)
        self.assertEqual(self.assignments(self.teacher), ids)

    def test_closing_frees_a_slot_and_only_one(self):
        ids = self.spaced(self.teacher, 10)
        self.close(self.teacher, ids[3])
        self.assertEqual(self.open_count(self.teacher), 9)
        self.create(self.teacher)
        self.assertRefused(assignment_quota.REASON_OPEN)

    def test_a_deadline_that_passes_frees_a_slot(self):
        soon = lifecycle.format_timestamp(T0 + timedelta(hours=3))
        self.create(self.teacher, deadline_at=soon)
        self.spaced(self.teacher, 0)
        for _ in range(9):
            self.create(self.teacher)
        self.assertRefused(assignment_quota.REASON_OPEN)             # T0: ten open
        self.at(T0 + timedelta(hours=3) - timedelta(seconds=1))
        self.assertRefused(assignment_quota.REASON_OPEN)             # one second before the cutoff: still open
        self.at(T0 + timedelta(hours=3))
        self.create(self.teacher)                                    # the cutoff instant: expired, a slot is free
        self.assertRefused(assignment_quota.REASON_OPEN)

    def test_a_held_assignment_that_is_otherwise_open_counts_as_open(self):
        ids = self.spaced(self.teacher, 10)
        self.sql("UPDATE quiz_assignments SET lifecycle_hold = ? WHERE id = ?", lifecycle.HOLD_CLOCK_REVIEW, ids[0])
        self.assertRefused(assignment_quota.REASON_OPEN)
        # A held assignment that is also closed is not open.
        self.sql("UPDATE quiz_assignments SET closed_at = ?, closed_by_user_id = ? WHERE id = ?",
                 "2026-03-02 09:30:00", self.teacher["id"], ids[0])
        self.create(self.teacher)

    def test_the_daily_limit_is_reported_first_when_both_are_used_up(self):
        for _ in range(10):
            self.create_closed(self.teacher)
        self.spaced(self.teacher, 10)
        self.assertEqual(self.open_count(self.teacher), 10)
        self.assertEqual(len(self.ledger(self.teacher)), 20)
        self.assertRefused(assignment_quota.REASON_DAILY)

    def test_only_the_accounts_own_assignments_count(self):
        self.spaced(self.other, 10)
        self.create(self.teacher)
        self.assertRefused(assignment_quota.REASON_OPEN, self.other)

    def test_unowned_assignments_belong_to_nobody(self):
        for _ in range(12):
            self.sql("INSERT INTO quiz_assignments (section_id, title_bg, question_count) VALUES (?, 'Без собственик', 1)",
                     self.section_id)
        self.assertEqual(self.open_count(self.teacher), 0)
        self.create(self.teacher)
        self.assertEqual(self.sql("SELECT COUNT(*) FROM assignment_creations WHERE owner_user_id IS NULL"), [(0,)])


class ExistingAssignmentsTest(QuotaTestCase):
    """Scope I8: nothing is closed, hidden or refused retroactively; only new creations are limited."""

    def seed_owned(self, account, count, *, created_at="2026-03-02 08:00:00", closed=False):
        """Rows as code that does not know the ledger leaves them (explicitly dated: the clock is frozen)."""
        for index in range(count):
            self.sql("INSERT INTO quiz_assignments (section_id, title_bg, question_count, owner_user_id, public_token, "
                     "created_at, closed_at, closed_by_user_id) VALUES (?, ?, 1, ?, ?, ?, ?, ?)",
                     self.section_id, f"Съществуващ {index}", account["id"], f"{account['id']:03d}{index:040d}",
                     created_at, created_at if closed else None, account["id"] if closed else None)

    def test_existing_open_assignments_count_toward_the_open_limit(self):
        self.seed_owned(self.teacher, 12)
        self.assertEqual(self.open_count(self.teacher), 12)
        before = self.sql("SELECT * FROM quiz_assignments ORDER BY id")
        self.assertRefused(assignment_quota.REASON_OPEN)
        self.assertEqual(self.sql("SELECT * FROM quiz_assignments ORDER BY id"), before)  # none closed, none hidden

    def test_the_teacher_can_still_close_them_and_then_create_again(self):
        self.seed_owned(self.teacher, 12)
        ids = self.assignments(self.teacher)
        for assignment_id in ids[:2]:
            self.close(self.teacher, assignment_id)
        self.assertRefused(assignment_quota.REASON_OPEN)             # 10 open
        self.close(self.teacher, ids[2])
        new = self.create(self.teacher)                              # 9 open
        self.assertIn(new, self.assignments(self.teacher))

    def test_existing_recent_assignments_are_recorded_at_the_next_admission_and_count(self):
        """No row was written by the migration; the admission records them, by their age, then counts."""
        self.seed_owned(self.teacher, 5, closed=True)
        self.assertEqual(self.ledger(self.teacher), [])
        self.fill(self.teacher, 15)                                   # 5 recorded on the way + 15 = 20
        self.assertEqual(len(self.ledger(self.teacher)), 20)
        self.assertRefused(assignment_quota.REASON_DAILY)

    def test_a_pre_migration_creation_inside_the_window_counts(self):
        """Ledger rows with the old created_at values, as reconciliation writes them."""
        for index in range(20):
            self.sql("INSERT INTO assignment_creations (owner_user_id, assignment_id, created_at) VALUES (?, ?, ?)",
                     self.teacher["id"], 7_000_000 + index, "2026-03-01 10:00:00")
        self.assertRefused(assignment_quota.REASON_DAILY)            # 23 h old at T0
        self.at(T0 + timedelta(hours=1))
        self.create(self.teacher)                                    # exactly 24 h old now: out of the window


class RolesTest(QuotaTestCase):
    def test_a_student_cannot_create_and_nothing_is_recorded(self):
        before = self.digest()
        with closing(self.connect()) as conn, self.assertRaises(ValueError):
            Q(conn, principal=self.principal(self.student, "student"), section_id=self.section_id,
              title_bg="Тест", question_count=1)
        with closing(self.connect()) as conn, self.assertRaises(ValueError):
            Q(conn, principal=ANONYMOUS, section_id=self.section_id, title_bg="Тест", question_count=1)
        self.assertEqual(self.digest(), before)

    def test_an_admin_is_recorded_and_never_refused(self):
        for _ in range(25):
            self.create_closed(self.admin, "admin")
        still_open = [self.create(self.admin, "admin") for _ in range(12)]
        self.assertEqual(len(self.ledger(self.admin)), 37)           # recorded, so a later decision has the numbers
        self.assertEqual(self.open_count(self.admin), 12)
        self.assertEqual(self.assignments(self.admin)[-12:], still_open)   # owned by the admin, as always

    def test_the_role_is_read_under_the_lock_not_from_the_principal(self):
        # The principal was loaded as a teacher; the row says admin now: not refused.
        self.fill(self.teacher, 20)
        self.assertRefused(assignment_quota.REASON_DAILY)
        self.sql("UPDATE users SET role = 'admin' WHERE id = ?", self.teacher["id"])
        self.create(self.teacher, "teacher")
        # The principal says admin, the row says teacher: refused.
        self.sql("UPDATE users SET role = 'teacher' WHERE id = ?", self.teacher["id"])
        self.assertRefused(assignment_quota.REASON_DAILY, self.teacher, "admin")

    def test_history_belongs_to_the_account_whatever_its_role_was(self):
        for _ in range(20):
            self.create_closed(self.admin, "admin")
        self.assertEqual(len(self.ledger(self.admin)), 20)
        self.sql("UPDATE users SET role = 'teacher' WHERE id = ?", self.admin["id"])
        self.assertRefused(assignment_quota.REASON_DAILY, self.admin, "teacher")  # demoted: the history counts
        self.at(T0 + timedelta(hours=24))
        self.create(self.admin, "teacher")

    def test_a_promoted_teacher_is_not_refused_and_a_student_made_teacher_starts_at_zero(self):
        self.sql("UPDATE users SET role = 'teacher' WHERE id = ?", self.student["id"])
        self.create(self.student, "teacher")
        self.assertEqual(len(self.ledger(self.student)), 1)

    def test_the_exemption_is_one_constant(self):
        for _ in range(20):
            self.create_closed(self.admin, "admin")
        with patch.object(assignment_quota, "EXEMPT_ROLES", frozenset()):
            self.assertRefused(assignment_quota.REASON_DAILY, self.admin, "admin")
        self.create(self.admin, "admin")

    def test_an_account_less_principal_creates_an_unowned_unaccounted_row(self):
        """Only tests and the retired shared sign-in produced one; no route can (scope I9)."""
        idless = Principal(is_teacher=True, is_admin=False)
        for _ in range(12):
            with closing(self.connect()) as conn:
                Q(conn, principal=idless, section_id=self.section_id, title_bg="Без акаунт", question_count=1)
                conn.commit()
        self.assertEqual(self.sql("SELECT COUNT(*) FROM quiz_assignments WHERE owner_user_id IS NULL"), [(12,)])
        self.assertEqual(self.sql("SELECT COUNT(*) FROM assignment_creations WHERE owner_user_id IS NULL"), [(0,)])


class AtomicityTest(QuotaTestCase):
    def test_a_refusal_writes_nothing_not_even_the_clock_mark(self):
        self.fill(self.teacher, 20)
        before = self.digest()
        self.at(T0 + timedelta(minutes=10))
        with closing(self.connect()) as conn:
            with self.assertRaises(assignment_quota.QuotaExceeded):
                Q(conn, principal=self.principal(self.teacher), section_id=self.section_id,
                  title_bg="Тест", question_count=1)
            self.assertFalse(conn.in_transaction)                    # the lock was released
        self.assertEqual(self.digest(), before)                      # lifecycle_clock included

    def test_a_refusal_inside_a_callers_transaction_leaves_it_rolled_back(self):
        self.fill(self.teacher, 20)
        before = self.digest()
        with closing(self.connect()) as conn:
            with self.assertRaises(assignment_quota.QuotaExceeded):
                with lifecycle.write_transaction(conn) as transaction:
                    Q(conn, principal=self.principal(self.teacher), section_id=self.section_id,
                      title_bg="Тест", question_count=1, transaction=transaction)
            self.assertFalse(conn.in_transaction)
        self.assertEqual(self.digest(), before)

    def test_the_ledger_row_commits_and_rolls_back_with_the_assignment(self):
        with closing(self.connect()) as conn:
            Q(conn, principal=self.principal(self.teacher), section_id=self.section_id, title_bg="Тест", question_count=1)
            self.assertTrue(conn.in_transaction)                     # the caller owns the commit
            conn.rollback()
        self.assertEqual((self.assignments(self.teacher), self.ledger(self.teacher)), ([], []))
        with closing(self.connect()) as conn:
            assignment_id = Q(conn, principal=self.principal(self.teacher), section_id=self.section_id,
                              title_bg="Тест", question_count=1)
            conn.commit()
        self.assertEqual(self.assignments(self.teacher), [assignment_id])
        self.assertEqual([row[0] for row in self.ledger(self.teacher)], [assignment_id])

    def test_a_failure_after_the_insert_consumes_no_allowance(self):
        self.fill(self.teacher, 19)
        self.sql("CREATE TRIGGER test_refuse_ledger BEFORE INSERT ON assignment_creations "
                 "BEGIN SELECT RAISE(ABORT, 'ledger unavailable'); END")
        before = self.digest()
        with self.assertRaises(sqlite3.IntegrityError):
            self.create(self.teacher)
        self.assertEqual(self.digest(), before)                      # no assignment without its ledger row
        self.sql("DROP TRIGGER test_refuse_ledger")
        self.create_closed(self.teacher)                             # the 20th is still available
        self.assertRefused(assignment_quota.REASON_DAILY)

    def test_a_failure_in_the_deadline_write_consumes_no_allowance(self):
        self.fill(self.teacher, 19)
        before = self.digest()
        with self.assertRaises(ValueError):
            self.create(self.teacher, display_timezone="Not/AZone")
        self.assertEqual(self.digest(), before)
        self.create_closed(self.teacher)
        self.assertRefused(assignment_quota.REASON_DAILY)

class ConcurrencyTest(QuotaTestCase):
    """The decision is made under the writer lock: the last slot goes to exactly one request."""

    def race(self, account, workers):
        barrier = threading.Barrier(workers)
        outcomes = []
        lock = threading.Lock()

        def attempt():
            conn = web_app.quiz_db()
            try:
                barrier.wait(timeout=10)
                try:
                    assignment_id = Q(conn, principal=self.principal(account), section_id=self.section_id,
                                      title_bg="Състезание", question_count=1)
                    conn.commit()
                    result = ("created", assignment_id)
                except assignment_quota.QuotaExceeded as exc:
                    result = ("refused", exc.reason)
                except BaseException as exc:  # a lock timeout would be a failure of the test
                    result = ("error", repr(exc))
            finally:
                conn.close()
            with lock:
                outcomes.append(result)

        threads = [threading.Thread(target=attempt) for _ in range(workers)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
            self.assertFalse(thread.is_alive())
        return outcomes

    def test_the_last_daily_slot_goes_to_exactly_one_request(self):
        for round_ in range(3):
            account = create_account(web_app.QUIZ_DB_PATH, "teacher")
            self.fill(account, 19)
            outcomes = self.race(account, 4)
            self.assertEqual(sorted(kind for kind, _ in outcomes), ["created"] + ["refused"] * 3, outcomes)
            self.assertEqual({detail for kind, detail in outcomes if kind == "refused"}, {assignment_quota.REASON_DAILY})
            self.assertEqual(len(self.ledger(account)), 20)
            self.assertEqual(len(self.assignments(account)), 20)

    def test_the_last_open_slot_goes_to_exactly_one_request(self):
        for round_ in range(3):
            account = create_account(web_app.QUIZ_DB_PATH, "teacher")
            for _ in range(9):
                self.create(account)
            outcomes = self.race(account, 4)
            self.assertEqual(sorted(kind for kind, _ in outcomes), ["created"] + ["refused"] * 3, outcomes)
            self.assertEqual({detail for kind, detail in outcomes if kind == "refused"}, {assignment_quota.REASON_OPEN})
            self.assertEqual(self.open_count(account), 10)

    def test_two_accounts_do_not_take_each_others_slots(self):
        a, b = create_account(web_app.QUIZ_DB_PATH, "teacher"), create_account(web_app.QUIZ_DB_PATH, "teacher")
        for account in (a, b):
            self.fill(account, 19)
        results = {}

        def run(name, account):
            results[name] = self.race_one(account)

        threads = [threading.Thread(target=run, args=(n, acc)) for n, acc in (("a", a), ("b", b))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        self.assertEqual(results, {"a": "created", "b": "created"})

    def race_one(self, account):
        conn = web_app.quiz_db()
        try:
            Q(conn, principal=self.principal(account), section_id=self.section_id, title_bg="Т", question_count=1)
            conn.commit()
            return "created"
        except assignment_quota.QuotaExceeded as exc:
            return exc.reason
        finally:
            conn.close()

    def test_time_is_read_after_the_lock_was_acquired(self):
        """A request that waits for the lock is judged at the time it gets it, not at the time it arrived."""
        self.fill(self.teacher, 20)                                  # all at T0
        self.assertRefused(assignment_quota.REASON_DAILY)
        holder = sqlite3.connect(str(web_app.QUIZ_DB_PATH), timeout=5)
        holder.execute("BEGIN IMMEDIATE")                            # someone else holds the writer lock
        result = {}

        def waiting_request():
            conn = web_app.quiz_db()
            try:
                result["id"] = Q(conn, principal=self.principal(self.teacher), section_id=self.section_id,
                                 title_bg="Чакащ", question_count=1)
                conn.commit()
            except BaseException as exc:
                result["error"] = repr(exc)
            finally:
                conn.close()

        thread = threading.Thread(target=waiting_request)
        thread.start()
        time.sleep(0.4)                                              # it is waiting for BEGIN IMMEDIATE
        self.assertNotIn("id", result)
        self.clock.advance(24 * 3600 + 5)                            # time passes while it waits
        holder.rollback()
        holder.close()
        thread.join(30)
        self.assertFalse(thread.is_alive())
        self.assertNotIn("error", result)
        self.assertEqual(self.ledger(self.teacher)[-1][1], lifecycle.format_timestamp(self.clock.wall()))


class RouteQuotaTest(QuotaTestCase):
    """The same limits through the application: `/teacher/new` (both kinds) and the copy form."""

    def setUp(self):
        super().setUp()
        self.vault = Path(web_app.QUIZ_VAULT_PATH)
        self.notes_at_start = set(self.notes())

    def client(self, account=None):
        client = web_app.app.test_client()
        if account:
            set_account_session(client, account["id"], account["session_key"])
        return client

    def notes(self):
        """Every file in this class's temporary vault (the vault is shared by the class's tests)."""
        return sorted(str(path.relative_to(self.vault)) for path in self.vault.rglob("*") if path.is_file())

    def new_notes(self):
        return sorted(set(self.notes()) - self.notes_at_start)

    def post_new(self, client, *, mixed=False, **extra):
        data = {**NO_DEADLINE_FORM, "section_id": self.section_id, "question_count": 2, "subject": "it", **extra}
        if mixed:
            data.update(open_count=1, source_slug=self.source_slug)
        return client.post("/teacher/new", data=data, headers=SAME_ORIGIN)

    def post_duplicate(self, client, assignment_id):
        return client.post(f"/teacher/assignment/{assignment_id}/duplicate", data=dict(NO_DEADLINE_FORM),
                           headers=SAME_ORIGIN)

    def created_id(self, response):
        self.assertEqual(response.status_code, 302, response.get_data(as_text=True)[:300])
        return int(response.headers["Location"].rsplit("/", 1)[1])

    def assertRouteUnchanged(self, before, notes):
        self.assertEqual(self.digest(), before)
        self.assertEqual(self.notes(), notes)

    # ------------------------------------------------------------------ creation

    def test_each_creation_path_records_the_acting_account(self):
        client = self.client(self.teacher)
        plain = self.created_id(self.post_new(client))
        mixed = self.created_id(self.post_new(client, mixed=True))
        copy = self.created_id(self.post_duplicate(client, plain))
        self.assertEqual([row[0] for row in self.ledger(self.teacher)], [plain, mixed, copy])
        self.assertEqual(self.assignments(self.teacher), [plain, mixed, copy])
        self.assertEqual(len(self.new_notes()), 3)                  # one vault note per success, in the temporary vault

    def test_the_open_limit_refuses_both_kinds_and_the_copy_without_side_effects(self):
        client = self.client(self.teacher)
        ids = [self.created_id(self.post_new(client)) for _ in range(10)]
        before, notes = self.digest(), self.notes()
        for mixed in (False, True):
            response = self.post_new(client, mixed=mixed)
            body = response.get_data(as_text=True)
            self.assertEqual(response.status_code, 409)
            self.assertIn(assignment_quota.MESSAGES_BG[assignment_quota.REASON_OPEN], body)
            self.assertNotIn("Location", response.headers)
            self.assertRouteUnchanged(before, notes)
        response = self.post_duplicate(client, ids[0])
        self.assertEqual(response.status_code, 409)
        self.assertIn(assignment_quota.MESSAGES_BG[assignment_quota.REASON_OPEN], response.get_data(as_text=True))
        self.assertRouteUnchanged(before, notes)

    def test_the_daily_limit_refuses_both_kinds_and_the_copy_without_side_effects(self):
        client = self.client(self.teacher)
        source = self.created_id(self.post_new(client))
        self.close(self.teacher, source)
        for _ in range(19):
            self.created_id(self.post_new(client))
            self.close(self.teacher, self.assignments(self.teacher)[-1])
        self.assertEqual(len(self.ledger(self.teacher)), 20)
        before, notes = self.digest(), self.notes()
        for mixed in (False, True):
            response = self.post_new(client, mixed=mixed)
            self.assertEqual(response.status_code, 429)
            self.assertIn(assignment_quota.MESSAGES_BG[assignment_quota.REASON_DAILY], response.get_data(as_text=True))
            self.assertRouteUnchanged(before, notes)
        response = self.post_duplicate(client, source)            # duplicating a closed test is allowed, not exempt
        self.assertEqual(response.status_code, 429)
        self.assertIn(assignment_quota.MESSAGES_BG[assignment_quota.REASON_DAILY], response.get_data(as_text=True))
        self.assertRouteUnchanged(before, notes)

    def test_a_refusal_is_logged_by_account_id_and_reason_only(self):
        client = self.client(self.teacher)
        for _ in range(10):
            self.created_id(self.post_new(client))
        with self.assertLogs(lifecycle.logger, level="INFO") as logs:
            self.assertEqual(self.post_new(client).status_code, 409)
        refused = [line for line in logs.output if "create_refused" in line]
        self.assertEqual(len(refused), 1)
        self.assertIn(f"actor_user_id={self.teacher['id']} reason=quota_open", refused[0])
        self.assertNotIn("event=create ", "\n".join(logs.output))     # no success line for a refusal

    def test_the_refused_form_is_shown_again_with_what_was_typed(self):
        client = self.client(self.teacher)
        for _ in range(10):
            self.created_id(self.post_new(client))
        response = self.post_new(client, question_count=7, time_limit_minutes=25)
        body = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 409)
        self.assertRegex(body, r'name="question_count"[^>]*value="7"')
        self.assertRegex(body, r'name="time_limit_minutes"[^>]*value="25"')
        self.assertIn('role="alert"', body)

    def test_closing_frees_the_slot_through_the_application(self):
        client = self.client(self.teacher)
        ids = [self.created_id(self.post_new(client)) for _ in range(10)]
        self.assertEqual(self.post_new(client).status_code, 409)
        self.assertEqual(client.post(f"/teacher/assignment/{ids[0]}/close", headers=SAME_ORIGIN).status_code, 302)
        self.created_id(self.post_new(client))
        self.assertEqual(self.post_new(client).status_code, 409)

    def test_a_teacher_at_the_limit_keeps_every_management_page(self):
        client = self.client(self.teacher)
        ids = [self.created_id(self.post_new(client)) for _ in range(10)]
        self.assertEqual(self.post_new(client).status_code, 409)
        for path in ("/teacher/assignments", f"/teacher/assignment/{ids[0]}", f"/teacher/assignment/{ids[0]}/results",
                     f"/teacher/assignment/{ids[0]}/results.csv", "/teacher/new"):
            self.assertEqual(client.get(path).status_code, 200, path)

    def test_a_copy_counts_for_the_account_that_makes_it(self):
        teacher_client, admin_client = self.client(self.teacher), self.client(self.admin)
        source = self.created_id(self.post_new(teacher_client))
        copy = self.created_id(self.post_duplicate(admin_client, source))     # an admin may copy a teacher's test
        self.assertEqual(self.sql("SELECT owner_user_id FROM quiz_assignments WHERE id = ?", copy), [(self.admin["id"],)])
        self.assertEqual([row[0] for row in self.ledger(self.admin)], [copy])
        self.assertEqual([row[0] for row in self.ledger(self.teacher)], [source])

    def test_an_admin_is_not_refused_through_the_application(self):
        client = self.client(self.admin)
        ids = [self.created_id(self.post_new(client)) for _ in range(12)]
        self.assertEqual(len(self.ledger(self.admin)), 12)
        self.assertEqual(self.assignments(self.admin), ids)

    # ------------------------------------------------------------------ authorization

    def test_authorization_comes_before_the_quota(self):
        teacher_client, other_client = self.client(self.teacher), self.client(self.other)
        source = self.created_id(self.post_new(teacher_client))
        for _ in range(9):
            self.created_id(self.post_new(other_client))
        self.created_id(self.post_new(other_client))                           # other is now at 10 open
        before, notes = self.digest(), self.notes()
        # Another teacher's test is a 404 whether or not the caller is over a limit.
        self.assertEqual(self.post_duplicate(other_client, source).status_code, 404)
        self.assertRouteUnchanged(before, notes)

    def test_students_and_visitors_create_nothing_and_are_not_counted(self):
        before, notes = self.digest(), self.notes()
        student = self.client(self.student)
        self.assertEqual(self.post_new(student).status_code, 403)
        self.assertEqual(self.post_duplicate(student, 1).status_code, 403)
        visitor = self.client()
        self.assertEqual(self.post_new(visitor).status_code, 302)
        self.assertIn("/login", self.post_new(visitor).headers["Location"])
        self.assertRouteUnchanged(before, notes)

    def test_a_forged_owner_is_ignored_and_a_limit_is_not_changed_by_the_form(self):
        client = self.client(self.teacher)
        for _ in range(10):
            self.created_id(self.post_new(client, owner_user_id=self.other["id"], role="admin",
                                          quota="off", daily_limit="1000", open_limit="1000"))
        self.assertEqual(self.post_new(client, owner_user_id=self.other["id"], role="admin").status_code, 409)
        self.assertEqual(self.assignments(self.other), [])
        self.assertEqual(len(self.ledger(self.teacher)), 10)

    def test_a_cross_origin_post_is_refused_before_anything_else(self):
        client = self.client(self.teacher)
        response = client.post("/teacher/new", data={**NO_DEADLINE_FORM, "section_id": self.section_id, "question_count": 2},
                               headers={"Origin": "http://evil.example"})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.ledger(self.teacher), [])

    def test_an_invalid_input_is_reported_as_before_and_not_as_a_quota_matter(self):
        client = self.client(self.teacher)
        for _ in range(10):
            self.created_id(self.post_new(client))
        response = self.post_new(client, question_count="abc")
        self.assertEqual(response.status_code, 400)
        self.assertNotIn(assignment_quota.MESSAGES_BG[assignment_quota.REASON_OPEN], response.get_data(as_text=True))
        response = client.post("/teacher/new", data={"section_id": self.section_id, "question_count": 2, "subject": "it"},
                               headers=SAME_ORIGIN)                              # no deadline choice
        self.assertEqual(response.status_code, 400)

    # ------------------------------------------------------------------ failures

    def test_a_failure_after_the_insert_through_the_application_consumes_nothing(self):
        client = self.client(self.teacher)
        for _ in range(19):
            self.created_id(self.post_new(client))
            self.close(self.teacher, self.assignments(self.teacher)[-1])
        self.sql("CREATE TRIGGER test_refuse_ledger BEFORE INSERT ON assignment_creations "
                 "BEGIN SELECT RAISE(ABORT, 'ledger unavailable'); END")
        before, notes = self.digest(), self.notes()
        with patch.dict(web_app.app.config, {"PROPAGATE_EXCEPTIONS": False}), \
                self.assertLogs(web_app.app.logger, level="ERROR"):
            self.assertEqual(self.post_new(client).status_code, 500)
        self.assertRouteUnchanged(before, notes)
        self.sql("DROP TRIGGER test_refuse_ledger")
        self.created_id(self.post_new(client))                                    # the 20th is still there
        self.assertEqual(self.post_new(client).status_code, 429)


class StaticPinsTest(QuotaTestCase):
    """The shape that keeps every creation path inside the one decision."""

    def sources(self, *roots):
        for root in roots:
            for path in sorted((ROOT / root).rglob("*.py")):
                yield path.relative_to(ROOT).as_posix(), path.read_text(encoding="utf-8")

    def test_one_insert_into_assignments_in_the_application_and_its_tools(self):
        found = [name for name, text in self.sources("web", "src")
                 if re.search(r"INSERT\s+(OR\s+\w+\s+)?INTO\s+quiz_assignments", text, re.IGNORECASE)]
        self.assertEqual(found, ["web/services/quiz.py"])

    def test_every_caller_of_the_creation_function_passes_its_transaction(self):
        calls = []
        for name, text in self.sources("web", "src"):
            if name == "web/services/quiz.py":
                continue
            for match in re.finditer(r"quiz_create_assignment\(", text):
                calls.append((name, text[match.start():match.start() + 700].split("\n\n")[0]))
        self.assertEqual(len(calls), 2)
        for name, call in calls:
            self.assertEqual(name, "web/blueprints/teacher.py")
            self.assertIn("transaction=transaction", call)

    def test_only_the_quota_module_writes_the_ledger(self):
        for name, text in self.sources("web", "src"):
            if name in ("web/services/assignment_quota.py",):
                continue
            self.assertNotRegex(text, r"(INSERT|UPDATE|DELETE|REPLACE)[^;\"']{0,60}assignment_creations", name)
        text = (ROOT / "web/services/assignment_quota.py").read_text(encoding="utf-8")
        writes = re.findall(r"(INSERT|UPDATE|DELETE|REPLACE)\b[^\"']{0,40}assignment_creations", text, re.IGNORECASE)
        self.assertEqual({w.upper() for w in writes}, {"INSERT"})               # never an update or a delete
        self.assertEqual(len(writes), 2)                                         # the admission's record and reconcile

    def test_the_decision_precedes_the_insert_in_the_creation_function(self):
        source = (ROOT / "web/services/quiz.py").read_text(encoding="utf-8")
        body = source[source.index("def quiz_create_assignment("):source.index("def quiz_can_access_assignment(")]
        self.assertLess(body.index("assignment_quota.enforce("), body.index("write_with_new_public_token(conn, insert)"))
        self.assertLess(body.index("write_with_new_public_token(conn, insert)"), body.index("assignment_quota.record("))
        self.assertIn("transaction.now", body)

    def test_the_open_predicate_text_is_unchanged(self):
        self.assertEqual(
            lifecycle.OPEN_PREDICATE_SQL,
            "lifecycle_hold IS NULL AND closed_at IS NULL AND expiry_observed_at IS NULL"
            " AND submissions_closed_at IS NULL AND (deadline_at IS NULL OR deadline_at > :now)")
        self.assertEqual(lifecycle.OPEN_PREDICATE_SQL, "lifecycle_hold IS NULL AND " + lifecycle.UNCLOSED_STATE_SQL)

    def test_no_route_or_template_names_a_limit_input(self):
        for name, text in self.sources("web"):
            if name.startswith("web/blueprints/"):
                self.assertNotRegex(text, r"""form\.get\(\s*["'](quota|daily_limit|open_limit|owner_user_id)["']""", name)
