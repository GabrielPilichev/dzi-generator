"""Creation accounting survives code that does not know the ledger (PR 7 review findings 1 and 2).

* Finding 1: a build that kept migration 024 but not the quota admission (``tests.legacy_creation``,
  the exact pre-PR 7 function) inserts owned assignments without a ledger row. Returning to the
  quota code must not grant the allowance again: the admission records what the surviving rows
  show, under the writer lock, or refuses. Run in rollback-journal and WAL mode.
* Finding 2: legacy ``created_at`` values keep their age under explicit rules; NULL, malformed,
  ambiguous and implausible values are never treated as old.

Every database is a temporary clone of the migrated template; time is frozen; nothing reads
``data/questions.db``.
"""

import itertools
import subprocess
import threading
import unittest
from contextlib import closing
from datetime import datetime, timedelta

from tests import legacy_creation
from tests.auth_helpers import create_account
from tests.support import ROOT, web_app
from tests.test_assignment_quota import HOUR, Q, T0, QuotaTestCase
from web import db as web_db
from web import production
from web.services import assignment_quota, lifecycle

NOW = datetime(2026, 3, 2, 9, 0, 0)
_TOKENS = itertools.count(1_000_000)
DAILY = assignment_quota.REASON_DAILY
ACCOUNTING = assignment_quota.REASON_ACCOUNTING


class NormalizeTimestampTest(unittest.TestCase):
    def norm(self, value, now=NOW):
        return assignment_quota.normalize_legacy_timestamp(value, now)

    def test_supported_shapes_keep_their_age(self):
        cases = {
            "2026-03-01 09:00:01": "2026-03-01 09:00:01",           # what CURRENT_TIMESTAMP writes
            "2026-03-01T09:00:01": "2026-03-01 09:00:01",           # ISO with T: naive means UTC
            "2026-03-01T09:00:01.123456": "2026-03-01 09:00:01",    # fraction cut to the second
            "2026-03-01 09:00:01.5": "2026-03-01 09:00:01",
            "2026-03-01T09:00:01Z": "2026-03-01 09:00:01",
            "2026-03-01T11:00:01+02:00": "2026-03-01 09:00:01",     # offsets are converted to UTC
            "2026-03-01T11:00:01+0200": "2026-03-01 09:00:01",
            "2026-03-01T04:00:01-05:00": "2026-03-01 09:00:01",
            "2026-03-01T23:00:01+14:00": "2026-03-01 09:00:01",
            "2026-03-01": "2026-03-01 23:59:59",                    # a date alone is the end of that UTC day
            "2026-03-02": "2026-03-02 23:59:59",                    # today: still not in the future by more than a day
        }
        for value, expected in cases.items():
            with self.subTest(value=value):
                self.assertEqual(self.norm(value), expected)

    def test_the_same_instant_in_every_shape_gives_the_same_text(self):
        instant = "2026-03-01 09:00:00"
        for value in ("2026-03-01 09:00:00", "2026-03-01T09:00:00", "2026-03-01T09:00:00.000Z",
                      "2026-03-01T12:00:00+03:00", "2026-03-01T01:00:00-08:00"):
            self.assertEqual(self.norm(value), instant, value)

    def test_unusable_values_are_none_never_old(self):
        bad = [None, "", " ", " 2026-03-01 09:00:00", "2026-03-01 09:00:00 ", "2026-03-01\t09:00:00",
               20260301, 1772355600, 1772355600.5, b"2026-03-01 09:00:00", True,
               "2026/03/01", "01.03.2026", "1 March 2026", "yesterday", "2026-03-01 09:00", "2026-3-1",
               "2026-02-30", "2026-13-01", "2026-03-01 25:00:00", "2026-03-01 09:61:00", "2026-03-01 09:00:61",
               "2026-03-01T09:00:00+15:00", "2026-03-01T09:00:00+14:30", "2026-03-01T09:00:00+02:60",
               "2026-03-01T09:00:00 UTC", "2026-03-01T09:00:00+2", "2026-03-01T09:00:00.1234567890",
               "0000-00-00 00:00:00", "1999-12-31 23:59:59", "1970-01-01 00:00:00", "0001-01-01",
               "2026-03-01 09:00:00\x00", "2026-03-01 09:00:00; DROP TABLE users"]
        for value in bad:
            with self.subTest(value=value):
                self.assertIsNone(self.norm(value))

    def test_the_upper_bound_is_one_day_after_now(self):
        self.assertEqual(self.norm("2026-03-03 09:00:00"), "2026-03-03 09:00:00")   # exactly a day ahead: kept
        self.assertIsNone(self.norm("2026-03-03 09:00:01"))                          # a second more: ambiguous
        self.assertIsNone(self.norm("2099-01-01 00:00:00"))
        self.assertIsNone(self.norm("2026-03-04"))
        # The bound is the caller's now, not the machine's.
        self.assertEqual(self.norm("2026-03-03 09:00:01", NOW + timedelta(seconds=1)), "2026-03-03 09:00:01")

    def test_the_result_is_always_the_ledger_layout(self):
        for value in ("2026-03-01", "2026-03-01T09:00:01.9+01:00", "2000-01-01 00:00:00"):
            result = self.norm(value)
            self.assertRegex(result, r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")
            self.assertEqual(lifecycle.format_timestamp(lifecycle.parse_timestamp(result)), result)

    def test_the_earliest_accepted_value_is_2000_01_01(self):
        self.assertEqual(self.norm("2000-01-01 00:00:00"), "2000-01-01 00:00:00")
        self.assertIsNone(self.norm("1999-12-31 23:59:59"))
        self.assertEqual(self.norm("2000-01-01T02:00:00+02:00"), "2000-01-01 00:00:00")


class AccountingTestCase(QuotaTestCase):
    def seed(self, account, values, *, closed=True):
        """Owned rows written the way code without the ledger writes them: explicit ``created_at``, no ledger row."""
        ids = []
        for index, created_at in enumerate(values):
            with closing(self.connect()) as conn:
                cursor = conn.execute(
                    "INSERT INTO quiz_assignments (section_id, title_bg, question_count, owner_user_id, public_token, "
                    "created_at, closed_at, closed_by_user_id) VALUES (?, 'Стар', 1, ?, ?, ?, ?, ?)",
                    (self.section_id, account["id"], f"{next(_TOKENS):043d}",
                     created_at, created_at if closed else None, account["id"] if closed else None))
                ids.append(cursor.lastrowid)
                conn.commit()
        return ids

    def set_created_at(self, assignment_id, value):
        self.sql("UPDATE quiz_assignments SET created_at = ? WHERE id = ?", value, assignment_id)

    def refused_for_accounting(self, account=None, role="teacher"):
        self.assertRefused(ACCOUNTING, account, role)


class LegacyFormatAllowanceTest(AccountingTestCase):
    """Finding 2: twenty recent legacy creations count as twenty, whatever shape their time has."""

    RECENT = {
        "canonical": "2026-03-02 08:00:00",
        "iso T": "2026-03-02T08:00:00",
        "iso T fraction": "2026-03-02T08:00:00.250000",
        "iso Z": "2026-03-02T08:00:00Z",
        "iso offset": "2026-03-02T10:00:00+02:00",
        "iso negative offset": "2026-03-02T03:00:00-05:00",
        "date only, today": "2026-03-02",
        "date only, yesterday": "2026-03-01",
    }

    def test_twenty_recent_creations_in_any_supported_shape_consume_the_daily_allowance(self):
        for name, value in self.RECENT.items():
            with self.subTest(shape=name):
                account = create_account(web_app.QUIZ_DB_PATH, "teacher")
                self.seed(account, [value] * 20)
                self.assertRefused(DAILY, account)
                self.assertEqual(len(self.ledger(account)), 0)         # a refusal records nothing
                self.assertEqual(len(self.assignments(account)), 20)

    def test_the_records_are_written_with_the_normalized_age_when_the_account_is_admitted(self):
        account = create_account(web_app.QUIZ_DB_PATH, "teacher")
        ids = self.seed(account, ["2026-03-02T10:00:00+02:00", "2026-03-01", "2026-03-02T08:00:00.9"])
        new = self.create(account)
        self.assertEqual(self.ledger(account), [(ids[0], "2026-03-02 08:00:00"), (ids[1], "2026-03-01 23:59:59"),
                                                (ids[2], "2026-03-02 08:00:00"), (new, "2026-03-02 09:00:00")])

    def test_age_is_preserved_at_the_window_edge_in_every_shape(self):
        # The same two instants in four shapes: exactly 24 h old at T0 (not counted), one second younger (counted).
        exactly = {"canonical": "2026-03-01 09:00:00", "iso T": "2026-03-01T09:00:00", "iso Z": "2026-03-01T09:00:00Z",
                   "iso offset": "2026-03-01T11:00:00+02:00"}
        younger = {"canonical": "2026-03-01 09:00:01", "iso T": "2026-03-01T09:00:01", "iso Z": "2026-03-01T09:00:01Z",
                   "iso offset": "2026-03-01T11:00:01+02:00"}
        for name in exactly:
            with self.subTest(shape=name, age="24 h"):
                account = create_account(web_app.QUIZ_DB_PATH, "teacher")
                self.seed(account, [exactly[name]] * 20)
                self.create(account)
            with self.subTest(shape=name, age="24 h minus a second"):
                account = create_account(web_app.QUIZ_DB_PATH, "teacher")
                self.seed(account, [younger[name]] * 20)
                self.assertRefused(DAILY, account)

    def test_a_date_older_than_the_window_is_not_counted(self):
        account = create_account(web_app.QUIZ_DB_PATH, "teacher")
        self.seed(account, ["2026-02-28"] * 20)                        # ends 2026-02-28 23:59:59: 33 h old
        self.create(account)
        self.assertEqual(len(self.ledger(account)), 21)

    def test_a_value_up_to_a_day_ahead_counts_as_dated_and_a_later_one_is_refused(self):
        account = create_account(web_app.QUIZ_DB_PATH, "teacher")
        self.seed(account, ["2026-03-03 08:59:59"] * 20)               # ahead of now but inside the tolerance
        self.assertRefused(DAILY, account)                             # conservative: it stays in the window
        other = create_account(web_app.QUIZ_DB_PATH, "teacher")
        self.seed(other, ["2026-03-03 09:00:01"])
        self.refused_for_accounting(other)

    def test_ordinary_lifecycle_timestamps_are_unchanged(self):
        account = create_account(web_app.QUIZ_DB_PATH, "teacher")
        first = self.create(account)
        self.at(T0 + HOUR)
        self.seed(account, ["2026-03-02 09:30:00"])
        second = self.create(account)
        self.assertEqual([row[1] for row in self.ledger(account)][0], "2026-03-02 09:00:00")
        self.assertEqual(self.ledger(account)[0][0], first)
        self.assertIn("2026-03-02 09:30:00", [row[1] for row in self.ledger(account)])
        self.assertEqual(self.ledger(account)[-1], (second, "2026-03-02 10:00:00"))


class UnplaceableHistoryTest(AccountingTestCase):
    """NULL, malformed and ambiguous values quarantine the account's creation; they are never ancient."""

    UNPLACEABLE = {
        "NULL": None, "empty": "", "text": "yesterday", "wrong layout": "01.03.2026", "no seconds": "2026-03-01 09:00",
        "impossible date": "2026-02-30 10:00:00", "offset too big": "2026-03-01T09:00:00+15:00",
        "before 2000": "1999-12-31 23:59:59", "epoch": "1970-01-01 00:00:00", "far future": "2099-01-01 00:00:00",
        "a day and a second ahead": "2026-03-03 09:00:01", "number": 1772355600, "real": 1772355600.5,
    }

    def seed_value(self, account, value):
        ids = self.seed(account, ["2026-03-02 08:00:00"])
        self.sql("UPDATE quiz_assignments SET created_at = ? WHERE id = ?", value, ids[0])
        return ids[0]

    def test_each_unplaceable_value_refuses_the_creation_and_writes_nothing(self):
        for name, value in self.UNPLACEABLE.items():
            with self.subTest(value=name):
                account = create_account(web_app.QUIZ_DB_PATH, "teacher")
                self.seed(account, ["2026-03-02 08:00:00"] * 19)
                bad = self.seed_value(account, value)
                before = self.digest()
                self.refused_for_accounting(account)
                self.assertEqual(self.digest(), before)
                self.assertEqual(self.ledger(account), [])             # not even the 19 that could be placed
                self.assertEqual(len(self.assignments(account)), 20)
                # An operator corrects the value; the account is admitted on its whole history.
                self.set_created_at(bad, "2026-03-02 08:00:00")
                self.assertRefused(DAILY, account)

    def test_a_quarantined_account_does_not_hold_back_another(self):
        stuck, fine = create_account(web_app.QUIZ_DB_PATH, "teacher"), create_account(web_app.QUIZ_DB_PATH, "teacher")
        self.seed_value(stuck, None)
        self.refused_for_accounting(stuck)
        self.create(fine)

    def test_a_row_dated_as_ancient_is_not_quietly_forgiven_by_the_epoch(self):
        account = create_account(web_app.QUIZ_DB_PATH, "teacher")
        self.seed(account, ["2026-03-02 08:00:00"] * 20)
        self.seed_value(account, "1970-01-01 00:00:00")
        self.refused_for_accounting(account)

    def test_the_refusal_is_shown_as_an_unavailable_service_not_a_limit(self):
        account = create_account(web_app.QUIZ_DB_PATH, "teacher")
        self.seed_value(account, None)
        before = self.digest()
        from tests.auth_helpers import set_account_session
        from tests.support import NO_DEADLINE_FORM

        client = web_app.app.test_client()
        set_account_session(client, account["id"], account["session_key"])
        response = client.post("/teacher/new", data={**NO_DEADLINE_FORM, "section_id": self.section_id,
                                                     "question_count": 2, "subject": "it"},
                               headers={"Origin": "http://localhost"})
        self.assertEqual(response.status_code, 503)
        self.assertIn(assignment_quota.MESSAGES_BG[ACCOUNTING], response.get_data(as_text=True))
        self.assertEqual(self.digest(), before)

    def test_a_ledger_row_that_disagrees_with_the_assignment_refuses_both_owners(self):
        a, b = create_account(web_app.QUIZ_DB_PATH, "teacher"), create_account(web_app.QUIZ_DB_PATH, "teacher")
        first = self.create(a)
        self.sql("UPDATE assignment_creations SET owner_user_id = ? WHERE assignment_id = ?", b["id"], first)
        self.refused_for_accounting(a)
        self.refused_for_accounting(b)
        self.sql("UPDATE assignment_creations SET owner_user_id = ? WHERE assignment_id = ?", a["id"], first)
        self.create(a)
        self.create(b)

    def test_a_deleted_ledger_row_is_healed_from_its_surviving_assignment(self):
        account = create_account(web_app.QUIZ_DB_PATH, "teacher")
        for _ in range(20):
            self.create_closed(account)
        # The service stamps assignments with SQLite's own clock; this test runs on a frozen one.
        self.sql("UPDATE quiz_assignments SET created_at = '2026-03-02 09:00:00' WHERE owner_user_id = ?", account["id"])
        self.sql("DELETE FROM assignment_creations WHERE owner_user_id = ?", account["id"])
        self.assertRefused(DAILY, account)                             # re-recorded, so not refunded
        self.assertEqual(len(self.ledger(account)), 0)                 # (a refusal writes nothing; the next admission redoes it)

    def test_what_cannot_be_recovered_is_the_assignment_row_deleted_before_it_was_recorded(self):
        """Stated limit: surviving rows are recovered; a row deleted before any admission is gone."""
        account = create_account(web_app.QUIZ_DB_PATH, "teacher")
        ids = self.seed(account, ["2026-03-02 08:00:00"] * 20)
        self.sql("DELETE FROM quiz_assignments WHERE id IN (%s)" % ",".join(map(str, ids[:10])))
        for _ in range(10):
            self.create_closed(account)                                # only the 10 survivors were found
        self.assertRefused(DAILY, account)


class AdminHistoryTest(AccountingTestCase):
    def test_an_admins_unrecorded_history_is_recorded_and_belongs_to_the_account_after_a_role_change(self):
        admin = create_account(web_app.QUIZ_DB_PATH, "admin")
        ids = self.seed(admin, ["2026-03-02 08:00:00"] * 20)
        new = self.create(admin, "admin")                              # exempt: allowed, and the 20 are recorded
        self.assertEqual([row[0] for row in self.ledger(admin)], ids + [new])
        self.sql("UPDATE users SET role = 'teacher' WHERE id = ?", admin["id"])
        self.assertRefused(DAILY, admin, "teacher")

    def test_an_admin_with_unplaceable_history_is_refused_too(self):
        admin = create_account(web_app.QUIZ_DB_PATH, "admin")
        ids = self.seed(admin, ["2026-03-02 08:00:00"])
        self.sql("UPDATE quiz_assignments SET created_at = NULL WHERE id = ?", ids[0])
        self.assertRefused(ACCOUNTING, admin, "admin")


class RollbackScenarios:
    """Finding 1, reproduced with the exact pre-PR 7 function (run once per journal mode below).

    The old function leaves ``created_at`` to SQLite's own clock, as it did in production, so this
    class moves the frozen lifecycle clock to the real present instead of the usual 2026-03-02.
    """

    JOURNAL_MODE = "DELETE"

    def setUp(self):
        super().setUp()
        self.base = datetime.utcnow().replace(microsecond=0)
        self.clock.set(self.base)
        with closing(self.connect()) as conn:
            self.assertEqual(conn.execute(f"PRAGMA journal_mode={self.JOURNAL_MODE}").fetchone()[0].upper(),
                             self.JOURNAL_MODE)
        self.addCleanup(self.reset_journal_mode)

    def old_create(self, account, role="teacher", *, close=True):
        with closing(self.connect()) as conn:
            assignment_id = legacy_creation.quiz_create_assignment(
                conn, principal=self.principal(account, role), section_id=self.section_id,
                title_bg="Със старата версия", question_count=1)
            conn.commit()
        if close:
            self.close(account, assignment_id)
        return assignment_id

    def reset_journal_mode(self):
        with closing(self.connect()) as conn:
            conn.execute("PRAGMA journal_mode=DELETE")

    def each_mode(self, scenario):
        scenario()

    def test_the_vendored_function_is_the_one_in_git(self):
        try:
            text = subprocess.run(["git", "show", "ede0c0b:web/services/quiz.py"], cwd=ROOT, capture_output=True,
                                  text=True, check=True, timeout=30).stdout
        except (OSError, subprocess.SubprocessError):
            self.skipTest("the base commit is not available in this checkout")
        start = text.index("def quiz_create_assignment(")
        original = text[start:text.index("def quiz_can_access_assignment(")].rstrip()
        mine = open(ROOT / "tests" / "legacy_creation.py", encoding="utf-8").read()
        self.assertTrue(mine.rstrip().endswith(original), "tests/legacy_creation.py differs from the base commit")

    def test_the_old_function_leaves_no_ledger_row(self):
        account = create_account(web_app.QUIZ_DB_PATH, "teacher")
        self.old_create(account)
        self.assertEqual(self.ledger(account), [])
        self.assertEqual(len(self.assignments(account)), 1)

    def test_twenty_old_creations_cannot_be_followed_by_twenty_more(self):
        def scenario():
            account = create_account(web_app.QUIZ_DB_PATH, "teacher")
            for _ in range(20):
                self.old_create(account)
            self.assertEqual(self.ledger(account), [])                 # the reviewer's starting point
            self.assertRefused(DAILY, account)                          # the original allowance is not granted again
            self.assertEqual(len(self.assignments(account)), 20)

        self.each_mode(scenario)

    def test_a_mixture_of_old_and_new_creations_is_one_history(self):
        def scenario():
            account = create_account(web_app.QUIZ_DB_PATH, "teacher")
            for _ in range(10):
                self.create_closed(account)
            for _ in range(10):
                self.old_create(account)
            self.assertRefused(DAILY, account)
            self.assertEqual(len(self.assignments(account)), 20)

        self.each_mode(scenario)

    def test_old_creations_after_new_ones_are_recorded_at_the_next_admission(self):
        def scenario():
            account = create_account(web_app.QUIZ_DB_PATH, "teacher")
            for _ in range(5):
                self.create_closed(account)
            for _ in range(25):
                self.old_create(account)                                # the old build had no limit
            self.assertRefused(DAILY, account)
            self.at(self.base + timedelta(hours=24, seconds=2))            # all thirty age out together
            self.create_closed(account)
            self.assertEqual(len(self.ledger(account)), 31)

        self.each_mode(scenario)

    def test_the_open_limit_also_sees_what_the_old_build_left_open(self):
        def scenario():
            account = create_account(web_app.QUIZ_DB_PATH, "teacher")
            for _ in range(10):
                self.old_create(account, close=False)
            self.assertRefused(assignment_quota.REASON_OPEN, account)

        self.each_mode(scenario)

    def test_an_admin_who_created_with_the_old_build_keeps_that_history_after_a_demotion(self):
        def scenario():
            admin = create_account(web_app.QUIZ_DB_PATH, "admin")
            for _ in range(20):
                self.old_create(admin, "admin")
            self.create(admin, "admin")                                 # exempt while an admin; recorded
            self.sql("UPDATE users SET role = 'teacher' WHERE id = ?", admin["id"])
            self.assertRefused(DAILY, admin, "teacher")

        self.each_mode(scenario)

    def test_old_creations_with_a_bad_time_stop_the_account_until_corrected(self):
        def scenario():
            account = create_account(web_app.QUIZ_DB_PATH, "teacher")
            ids = [self.old_create(account) for _ in range(3)]
            self.sql("UPDATE quiz_assignments SET created_at = 'not a time' WHERE id = ?", ids[1])
            self.assertRefused(ACCOUNTING, account)
            self.set_created_at(ids[1], lifecycle.format_timestamp(self.base))
            self.create(account)

        self.each_mode(scenario)

    def test_the_first_admission_after_a_rollback_is_safe_when_requests_race(self):
        def scenario():
            account = create_account(web_app.QUIZ_DB_PATH, "teacher")
            for _ in range(19):
                self.old_create(account)
            barrier = threading.Barrier(4)
            outcomes, lock = [], threading.Lock()

            def attempt():
                conn = web_app.quiz_db()
                try:
                    barrier.wait(timeout=10)
                    try:
                        Q(conn, principal=self.principal(account), section_id=self.section_id, title_bg="Състезание",
                          question_count=1)
                        conn.commit()
                        result = "created"
                    except assignment_quota.QuotaExceeded as exc:
                        result = exc.reason
                    except BaseException as exc:
                        result = repr(exc)
                finally:
                    conn.close()
                with lock:
                    outcomes.append(result)

            threads = [threading.Thread(target=attempt) for _ in range(4)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(30)
            self.assertEqual(sorted(outcomes), sorted(["created"] + [DAILY] * 3), outcomes)
            rows = self.ledger(account)
            self.assertEqual(len(rows), 20)
            self.assertEqual(len({row[0] for row in rows}), 20)           # no assignment recorded twice

        self.each_mode(scenario)


class RollbackRegressionJournalTest(RollbackScenarios, AccountingTestCase):
    JOURNAL_MODE = "DELETE"


class RollbackRegressionWalTest(RollbackScenarios, AccountingTestCase):
    JOURNAL_MODE = "WAL"


class StartupNoticeTest(unittest.TestCase):
    """On databases of their own: the notice counts the whole database, which the shared class database is not."""

    def setUp(self):
        import tempfile
        from pathlib import Path

        from tests.support import build_test_database

        directory = tempfile.TemporaryDirectory(prefix="accounting-notice-")
        self.addCleanup(directory.cleanup)
        self.path = build_test_database(Path(directory.name) / "notice.db")
        with closing(web_db.connect_db(self.path)) as conn:
            conn.execute("INSERT INTO users (id, display_name, role, session_key) VALUES (7, 'Учител', 'teacher', "
                         "'12345678901234567890123456789012')")
            conn.commit()
        self.section_id = None

    def rows(self, *created_at):
        with closing(web_db.connect_db(self.path)) as conn:
            section = conn.execute("SELECT id FROM curriculum_sections ORDER BY id LIMIT 1").fetchone()[0]
            for index, value in enumerate(created_at):
                conn.execute("INSERT INTO quiz_assignments (section_id, title_bg, question_count, owner_user_id, "
                             "public_token, created_at) VALUES (?, 'Стар', 1, 7, ?, ?)",
                             (section, f"{index:043d}", value))
            conn.commit()

    def digest(self):
        with closing(web_db.connect_db(self.path)) as conn:
            return [[tuple(r) for r in conn.execute(f"SELECT * FROM {t} ORDER BY 1")]
                    for t in ("quiz_assignments", "assignment_creations", "users")]

    def warning(self):
        return production.assignment_accounting_warning(self.path, web_app.open_existing_database_read_only)

    def test_no_notice_for_a_clean_or_merely_unrecorded_database(self):
        self.assertIsNone(self.warning())
        self.rows("2026-03-02 08:00:00", "2026-03-02T08:00:00Z", "2026-03-02")
        self.assertIsNone(self.warning())                              # unrecorded but placeable: the admission handles it

    def test_a_notice_names_counts_and_never_an_account_or_a_value(self):
        self.rows("secret-looking-value", "2026-03-02 08:00:00", None)
        message = self.warning()
        self.assertIn("2 unrecorded assignment(s) with an unusable created_at", message)
        self.assertIn("0 inconsistent", message)
        self.assertNotIn("secret-looking-value", message)
        self.assertNotRegex(message, r"\b7\b")

    def test_the_check_reads_only(self):
        self.rows(None)
        before = self.digest()
        self.assertIsNotNone(self.warning())
        self.assertEqual(self.digest(), before)

    def test_a_database_without_the_ledger_is_silent(self):
        self.rows(None)
        with closing(web_db.connect_db(self.path)) as conn:
            conn.execute("DROP TABLE assignment_creations")
            conn.commit()
        self.assertIsNone(self.warning())

    def test_it_is_only_a_notice_the_admission_is_what_keeps_the_limits(self):
        source = (ROOT / "web" / "production.py").read_text(encoding="utf-8")
        body = source[source.index("def create_production_app("):]
        index = body.index("assignment_accounting_warning(")
        self.assertIn("lifecycle.logger.warning", body[index:index + 300])
        self.assertNotIn("ProductionConfigError", body[index:index + 300])   # it never refuses to start


if __name__ == "__main__":
    unittest.main()
