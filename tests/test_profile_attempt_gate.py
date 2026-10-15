"""Registration PR 6: the profile check at the locked "would create a new attempt" decision
(test plan J6, J8, J9, J10, J12, J13).

The decision is made inside ``quiz_start_or_resume_attempt``, under its writer lock, in one
order: authentication and access (the route), an existing attempt (resume or result, closed
or not), a closed assignment (the closed notice), and only then ``profiles.needs_profile``.
Every test has its own temporary database and a frozen lifecycle clock
(``tests.lifecycle_support``); the profile gate is switched on per test through the
application configuration and is off in the gate-off tests, which are the build's default.
"""

import inspect
import json
import threading
import unittest
from contextlib import closing
from datetime import timedelta, timezone
from unittest.mock import patch

from tests.lifecycle_support import SAME_ORIGIN, LifecycleTestCase
from tests.support import web_app
from web.services import accounts, lifecycle, profiles
from web.services import quiz as quiz_service

MARK = "2026-03-01 08:00:00"
PROFILE_FORM = {"first_name": "Мария", "last_name": "Петрова", "grade": "10"}
HOUR = timedelta(hours=1)


class AttemptGateTestCase(LifecycleTestCase):
    gate = True

    def setUp(self):
        super().setUp()
        if self.gate:
            config = patch.dict(web_app.app.config, {profiles.CONFIG_KEY: True})
            config.start()
            self.addCleanup(config.stop)
        self.teacher = self.account("teacher")
        self.assignment_id = self.assignment(self.teacher)

    # ------------------------------------------------------------------ accounts

    def student(self, *, marked=True, profile=False, **options):
        account = self.account("student", **options)
        if marked:
            self.execute("UPDATE users SET profile_required_at = ? WHERE id = ?", (MARK, account["id"]))
        if profile:
            self.give_profile(account)
        return account, self.client(account)

    def give_profile(self, account, kind="student"):
        with closing(web_app.quiz_db()) as conn:
            if kind == "student":
                profiles.create_profile(conn, account["id"], account_type="student", first_name="Ана",
                                        last_name="Иванова", grade=9, now=self.clock.wall().replace(tzinfo=timezone.utc))
            else:
                profiles.create_profile(
                    conn, account["id"], account_type="teacher", first_name="Иван", last_name="Петров",
                    school_name="СУ „Васил Левски“, Пловдив",
                    subjects=[{"subject_id": "it", "label": "Информационни технологии"}],
                    now=self.clock.wall().replace(tzinfo=timezone.utc))
            conn.commit()

    # ------------------------------------------------------------------ observations

    def rows(self, sql, params=()):
        """``query`` with plain tuples, so that results compare with literals."""
        return [tuple(row) for row in self.query(sql, params)]

    def attempt_count(self):
        return self.query("SELECT COUNT(*) FROM quiz_attempts")[0][0]

    def assert_sent_to_the_profile(self, response, assignment_id=None):
        path = self.entry(assignment_id or self.assignment_id)
        self.assertEqual((response.status_code, response.headers["Location"]),
                         (303, f"/account/profile?next={path}"))

    def assert_started(self, response):
        self.assertEqual(response.status_code, 302, response.get_data(as_text=True)[:200])
        self.assertRegex(response.headers["Location"], r"^/quiz/attempt/\d+$")


# --------------------------------------------------------------------------- the new attempt


class NewAttemptTest(AttemptGateTestCase):
    def test_a_marked_student_without_a_profile_is_sent_to_the_profile_and_no_attempt_is_created(self):  # J8 (a)
        account, client = self.student()
        before = (self.table_counts(), self.clock_row()[:])
        response = self.start(client, self.assignment_id)
        self.assert_sent_to_the_profile(response)
        self.assertEqual(self.table_counts(), before[0])
        self.assertEqual(self.attempt_count(), 0)
        # Not even the clock's high-water mark moved: the decision wrote nothing.
        self.assertEqual(tuple(self.clock_row()), tuple(before[1]))
        # The same, again and again.
        for _ in range(2):
            self.assert_sent_to_the_profile(self.start(client, self.assignment_id))
        self.assertEqual(self.attempt_count(), 0)

    def test_the_start_page_itself_is_never_blocked(self):
        account, client = self.student()
        response = client.get(self.entry(self.assignment_id))
        self.assertEqual(response.status_code, 200)
        self.assertIn("Продължи към теста", response.get_data(as_text=True))
        self.assertEqual(self.client().get(self.entry(self.assignment_id)).status_code, 200)

    def test_completing_the_profile_returns_by_get_and_the_student_then_starts_it_themselves(self):  # J9
        account, client = self.student(display_name="Google Име")
        redirected = self.start(client, self.assignment_id)
        self.assert_sent_to_the_profile(redirected)
        page = client.get(redirected.headers["Location"])
        self.assertEqual(page.status_code, 200)
        self.assertIn(f'name="next" value="{self.entry(self.assignment_id)}"', page.get_data(as_text=True))
        done = client.post("/account/profile", data={**PROFILE_FORM, "next": self.entry(self.assignment_id)},
                           headers=SAME_ORIGIN)
        self.assertEqual((done.status_code, done.headers["Location"]), (303, self.entry(self.assignment_id)))
        self.assertEqual(self.attempt_count(), 0)                                   # nothing started by itself
        self.assertEqual(client.get(done.headers["Location"]).status_code, 200)    # a GET
        self.assertEqual(self.attempt_count(), 0)
        started = self.start(client, self.assignment_id)                            # the student presses the button
        self.assert_started(started)
        self.assertEqual(self.rows("SELECT student_name, user_id FROM quiz_attempts"),
                         [("Мария Петрова", account["id"])])

    def test_the_new_attempt_is_created_when_nothing_asks_for_a_profile(self):  # J10, J13
        cases = {
            "an unmarked student without a profile (CLI-created)": self.student(marked=False)[1],
            "a marked student with a profile": self.student(marked=True, profile=True)[1],
            "an unmarked student with a profile": self.student(marked=False, profile=True)[1],
            "a teacher": self.client(self.account("teacher")),
            "an admin": self.client(self.account("admin")),
        }
        marked_teacher = self.account("teacher")
        self.execute("UPDATE users SET profile_required_at = ? WHERE id = ?", (MARK, marked_teacher["id"]))
        cases["a teacher with a stray mark"] = self.client(marked_teacher)
        for name, client in cases.items():
            with self.subTest(case=name):
                self.assert_started(self.start(client, self.assignment_id))
        self.assertEqual(self.attempt_count(), len(cases))

    def test_a_teacher_type_profile_on_a_student_account_satisfies_it_and_a_student_type_one_too(self):  # I6
        marked, client = self.student()
        self.give_profile(marked, "teacher")
        self.assert_started(self.start(client, self.assignment_id))

    def test_a_started_attempt_copies_the_display_name_at_that_moment(self):
        account, client = self.student(marked=False, display_name="Първо Име")
        other = self.assignment(self.teacher)
        self.assert_started(self.start(client, self.assignment_id))
        with closing(web_app.quiz_db()) as conn:
            accounts.set_display_name(conn, account["id"], "Второ Име")
            conn.commit()
        self.assert_started(self.start(client, other))
        self.assertEqual(self.rows("SELECT assignment_id, student_name FROM quiz_attempts ORDER BY id"),
                         [(self.assignment_id, "Първо Име"), (other, "Второ Име")])


class GateOffTest(AttemptGateTestCase):
    """The build's default (J6): attempt creation behaves as before whatever is marked."""

    gate = False

    def test_a_marked_profile_less_student_starts_an_attempt_exactly_as_before(self):
        account, client = self.student()
        self.assertEqual(self.user_mark(account), MARK)           # the mark stays stored ...
        self.assert_started(self.start(client, self.assignment_id))   # ... and is ignored
        self.assertEqual(self.attempt_count(), 1)
        self.assertEqual(self.user_mark(account), MARK)

    def user_mark(self, account):
        return self.query("SELECT profile_required_at FROM users WHERE id = ?", (account["id"],))[0][0]

    def test_the_service_default_is_off_and_never_reads_the_mark(self):
        account, _ = self.student()
        with patch.object(profiles, "needs_profile", side_effect=AssertionError("must not be asked")):
            with closing(web_app.quiz_db()) as conn:
                assignment = quiz_service.quiz_fetch_assignment(conn, self.assignment_id)
                result = quiz_service.quiz_start_or_resume_attempt(
                    conn, assignment, user_id=account["id"], display_name="Ученик")
        self.assertTrue(result.created)
        self.assertFalse(result.profile_required)

    def test_every_account_starts_with_the_gate_off(self):
        for client in (self.student()[1], self.student(marked=False)[1], self.client(self.account("teacher")),
                       self.client(self.account("admin"))):
            self.assert_started(self.start(client, self.assignment_id))


# --------------------------------------------------------------------------- precedence


class PrecedenceTest(AttemptGateTestCase):
    def setUp(self):
        super().setUp()
        self.account_, self.browser = self.student()
        self.begin = self.T0 - timedelta(minutes=30)

    def test_an_unfinished_attempt_is_resumed_not_blocked(self):  # J8 (b)
        attempt = self.attempt(self.assignment_id, self.account_, started_at=self.begin)
        response = self.start(self.browser, self.assignment_id)
        self.assertEqual((response.status_code, response.headers["Location"]), (302, f"/quiz/attempt/{attempt}"))
        self.assertEqual(self.attempt_count(), 1)

    def test_a_submitted_attempt_goes_to_its_result(self):  # J8 (c)
        attempt = self.attempt(self.assignment_id, self.account_, started_at=self.begin,
                               submitted_at=self.begin + timedelta(minutes=5))
        response = self.start(self.browser, self.assignment_id)
        self.assertEqual((response.status_code, response.headers["Location"]),
                         (302, f"/quiz/attempt/{attempt}/result"))

    def test_a_closed_assignment_resumes_and_shows_results_too(self):
        closed = self.assignment(self.teacher, closed_at=self.T0 - HOUR, closed_by_user_id=self.teacher["id"])
        started = self.attempt(closed, self.account_, started_at=self.T0 - 2 * HOUR)
        response = self.start(self.browser, closed)
        self.assertEqual((response.status_code, response.headers["Location"]), (302, f"/quiz/attempt/{started}"))
        expired = self.assignment(self.teacher, deadline_at=self.T0 - HOUR)
        done = self.attempt(expired, self.account_, started_at=self.T0 - 3 * HOUR, submitted_at=self.T0 - 2 * HOUR)
        response = self.start(self.browser, expired)
        self.assertEqual((response.status_code, response.headers["Location"]), (302, f"/quiz/attempt/{done}/result"))

    def test_a_closed_assignment_without_an_attempt_shows_its_closed_notice_not_the_profile_form(self):  # J8 (e)
        for column, value in (("closed_at", self.T0 - HOUR), ("deadline_at", self.T0 - HOUR)):
            with self.subTest(closed_by=column):
                columns = {column: value}
                if column == "closed_at":
                    columns["closed_by_user_id"] = self.teacher["id"]
                closed = self.assignment(self.teacher, **columns)
                before = self.attempt_count()
                response = self.start(self.browser, closed)
                self.assertEqual(response.status_code, 409)
                self.assertNotIn("Location", response.headers)
                self.assertIn('id="assignment-closed-notice"', response.get_data(as_text=True))
                self.assertEqual(self.attempt_count(), before)

    def test_needs_profile_is_not_asked_for_a_resume_or_a_closed_assignment(self):
        closed = self.assignment(self.teacher, closed_at=self.T0 - HOUR, closed_by_user_id=self.teacher["id"])
        self.attempt(self.assignment_id, self.account_, started_at=self.begin)
        with patch.object(profiles, "needs_profile", wraps=profiles.needs_profile) as spy:
            self.start(self.browser, self.assignment_id)     # resume
            self.start(self.browser, closed)                 # closed notice
        spy.assert_not_called()

    def test_it_is_asked_once_under_the_writer_lock_inside_the_service_and_only_there(self):  # J8 "under the lock"
        seen = []
        real = profiles.needs_profile

        def spy(conn, user, *, enabled):
            seen.append({"in_transaction": conn.in_transaction, "enabled": enabled,
                         "inside_service": any(f.function == "quiz_start_or_resume_attempt"
                                               for f in inspect.stack())})
            return real(conn, user, enabled=enabled)

        looked_up = []
        real_find = quiz_service.quiz_find_account_attempt

        def find(*args, **kwargs):
            looked_up.append(args)
            return real_find(*args, **kwargs)

        with patch.object(profiles, "needs_profile", spy), patch.object(quiz_service, "quiz_find_account_attempt", find):
            response = self.start(self.browser, self.assignment_id)
        self.assert_sent_to_the_profile(response)
        self.assertEqual(seen, [{"in_transaction": True, "enabled": True, "inside_service": True}])
        self.assertEqual(looked_up, [])    # the route looks up no attempt of its own on a POST

    def test_the_precedence_holds_when_the_service_is_called_directly(self):
        account = self.account_
        with closing(web_app.quiz_db()) as conn:
            assignment = quiz_service.quiz_fetch_assignment(conn, self.assignment_id)
            first = quiz_service.quiz_start_or_resume_attempt(
                conn, assignment, user_id=account["id"], display_name="Ученик", profile_gate=True)
            self.assertEqual(first, quiz_service.QuizAttemptStart(None, False, False, closed=False, profile_required=True))
            self.assertFalse(conn.in_transaction)
            self.assertEqual(self.attempt_count(), 0)
            # With the gate off the very same call creates the attempt, ...
            created = quiz_service.quiz_start_or_resume_attempt(
                conn, assignment, user_id=account["id"], display_name="Ученик", profile_gate=False)
            self.assertTrue(created.created)
            # ... and with it on, the existing attempt is now resumed.
            resumed = quiz_service.quiz_start_or_resume_attempt(
                conn, assignment, user_id=account["id"], display_name="Ученик", profile_gate=True)
            self.assertEqual((resumed.attempt_id, resumed.created, resumed.profile_required),
                             (created.attempt_id, False, False))
            closed = self.assignment(self.teacher, closed_at=self.T0 - HOUR, closed_by_user_id=self.teacher["id"])
            outcome = quiz_service.quiz_start_or_resume_attempt(
                conn, quiz_service.quiz_fetch_assignment(conn, closed), user_id=account["id"],
                display_name="Ученик", profile_gate=True)
            self.assertEqual((outcome.closed, outcome.profile_required, outcome.attempt_id), (True, False, None))


class NeverBlockedTest(AttemptGateTestCase):
    def test_every_other_page_of_a_marked_student_works(self):  # J8 (d)
        account, client = self.student()
        attempt = self.attempt(self.assignment_id, account, started_at=self.T0 - timedelta(minutes=30))
        finished = self.attempt(self.assignment(self.teacher), account, started_at=self.T0 - HOUR,
                                submitted_at=self.T0 - timedelta(minutes=50))
        for path in (f"/quiz/attempt/{attempt}", f"/quiz/attempt/{finished}/result", "/account/attempts", "/account",
                     "/account/password", "/account/profile", "/", "/grade/8", self.entry(self.assignment_id)):
            with self.subTest(path=path):
                response = client.get(path)
                self.assertEqual(response.status_code, 200, path)
        submitted = self.submit(client, attempt)
        self.assertEqual((submitted.status_code, submitted.headers["Location"]), (302, f"/quiz/attempt/{attempt}/result"))
        self.assertEqual(client.get(f"/quiz/attempt/{attempt}/result").status_code, 200)

    def test_saving_and_submitting_an_attempt_started_before_the_mark_works(self):
        account, client = self.student(marked=False)
        started = self.start(client, self.assignment_id)
        attempt = self.attempt_id_of(started)
        self.execute("UPDATE users SET profile_required_at = ? WHERE id = ?", (MARK, account["id"]))
        self.assertEqual(client.get(f"/quiz/attempt/{attempt}").status_code, 200)
        self.assertEqual(self.submit(client, attempt).status_code, 302)
        self.assertEqual(client.get(f"/quiz/attempt/{attempt}/result").status_code, 200)
        self.assertEqual(self.start(client, self.assignment_id).headers["Location"], f"/quiz/attempt/{attempt}/result")


class RoleChangeTest(AttemptGateTestCase):
    def test_a_promoted_marked_student_starts_and_a_demoted_one_is_asked_again(self):  # J12
        account, _ = self.student()
        before = self.rows("SELECT profile_required_at FROM users WHERE id = ?", (account["id"],))
        for role, expected in (("teacher", "start"), ("admin", "start"), ("student", "profile")):
            with self.subTest(role=role):
                with closing(web_app.quiz_db()) as conn:
                    key = accounts.set_role(conn, account["id"], role)
                    conn.commit()
                client = self.client({"id": account["id"], "session_key": key})
                target = self.assignment(self.teacher)    # a new one each time: an attempt would be resumed
                response = self.start(client, target)
                if expected == "start":
                    self.assert_started(response)
                else:
                    self.assert_sent_to_the_profile(response, target)
                self.assertEqual(self.rows("SELECT profile_required_at FROM users WHERE id = ?", (account["id"],)), before)
                self.assertEqual(self.rows("SELECT COUNT(*) FROM user_profiles"), [(0,)])

    def test_a_teacher_with_a_teacher_profile_demoted_to_student_is_not_asked(self):  # J12
        account = self.account("teacher")
        self.give_profile(account, "teacher")
        with closing(web_app.quiz_db()) as conn:
            key = accounts.set_role(conn, account["id"], "student")
            conn.commit()
        self.assert_started(self.start(self.client({"id": account["id"], "session_key": key}), self.assignment_id))
        self.assertEqual(self.rows("SELECT account_type FROM user_profiles WHERE user_id = ?", (account["id"],)),
                         [("teacher",)])


# --------------------------------------------------------------------------- the lock


class LockedDecisionTest(AttemptGateTestCase):
    """The decision is the one made under the writer lock, whatever happened before it."""

    def run_start_while_the_lock_is_held(self, account, *, while_held):
        """Start through the service on its own connection; ``while_held`` runs on a second
        connection that holds the write lock from before the start reaches it."""
        reached, result, errors = threading.Event(), [], []
        real = lifecycle._begin_write

        def reaching(conn):
            reached.set()
            return real(conn)

        def start():
            try:
                with closing(web_app.quiz_db()) as conn:
                    assignment = quiz_service.quiz_fetch_assignment(conn, self.assignment_id)
                    result.append(quiz_service.quiz_start_or_resume_attempt(
                        conn, assignment, user_id=account["id"], display_name="Ученик", profile_gate=True))
            except Exception as exc:  # noqa: BLE001 - reported below
                errors.append(exc)

        holder = web_app.quiz_db()
        try:
            holder.execute("BEGIN IMMEDIATE")
            with patch.object(lifecycle, "_begin_write", reaching):
                thread = threading.Thread(target=start)
                thread.start()
                self.assertTrue(reached.wait(10))
                while_held(holder)
                holder.commit()
                thread.join(30)
        finally:
            holder.close()
        self.assertEqual(errors, [])
        self.assertEqual(len(result), 1)
        return result[0]

    def test_a_profile_given_while_the_start_waits_for_the_lock_is_seen_by_the_decision(self):
        account, _ = self.student()

        def give(holder):
            profiles.create_profile(holder, account["id"], account_type="student", first_name="Ана", last_name="Иванова",
                                    grade=9, now=self.clock.wall().replace(tzinfo=timezone.utc))

        outcome = self.run_start_while_the_lock_is_held(account, while_held=give)
        self.assertTrue(outcome.created)
        self.assertFalse(outcome.profile_required)
        self.assertEqual(self.attempt_count(), 1)

    def test_an_attempt_created_while_the_start_waits_for_the_lock_is_resumed_not_blocked(self):
        account, _ = self.student()
        created = []

        def create(holder):
            created.append(holder.execute(
                "INSERT INTO quiz_attempts (assignment_id, user_id, student_name, seed, question_ids_json, "
                "started_at, score_total) VALUES (?, ?, 'Ученик', 's', ?, ?, 3)",
                (self.assignment_id, account["id"], json.dumps(self.question_ids),
                 lifecycle.format_timestamp(self.T0 - timedelta(minutes=1)))).lastrowid)

        outcome = self.run_start_while_the_lock_is_held(account, while_held=create)
        self.assertEqual((outcome.attempt_id, outcome.created, outcome.profile_required), (created[0], False, False))
        self.assertEqual(self.attempt_count(), 1)

    def test_an_assignment_closed_while_the_start_waits_for_the_lock_is_a_closed_notice(self):
        account, _ = self.student()

        def close(holder):
            holder.execute("UPDATE quiz_assignments SET closed_at = ?, closed_by_user_id = ? WHERE id = ?",
                           (lifecycle.format_timestamp(self.T0 - timedelta(minutes=1)), self.teacher["id"], self.assignment_id))

        outcome = self.run_start_while_the_lock_is_held(account, while_held=close)
        self.assertEqual((outcome.closed, outcome.profile_required, outcome.attempt_id), (True, False, None))
        self.assertEqual(self.attempt_count(), 0)

    def test_an_account_unmarked_while_the_start_waits_is_not_asked(self):
        account, _ = self.student()

        def unmark(holder):
            holder.execute("UPDATE users SET profile_required_at = NULL WHERE id = ?", (account["id"],))

        outcome = self.run_start_while_the_lock_is_held(account, while_held=unmark)
        self.assertTrue(outcome.created)

    def test_a_role_changed_while_the_start_waits_is_read_at_the_lock(self):
        account, _ = self.student()

        def promote(holder):
            accounts.set_role(holder, account["id"], "teacher")

        outcome = self.run_start_while_the_lock_is_held(account, while_held=promote)
        self.assertTrue(outcome.created)


class ConcurrentStartTest(AttemptGateTestCase):
    WORKERS = 6

    def start_together(self, account, *, workers=None):
        workers = workers or self.WORKERS
        barrier, results, errors = threading.Barrier(workers), [], []

        def start():
            try:
                with closing(web_app.quiz_db()) as conn:
                    assignment = quiz_service.quiz_fetch_assignment(conn, self.assignment_id)
                    barrier.wait(timeout=10)
                    results.append(quiz_service.quiz_start_or_resume_attempt(
                        conn, assignment, user_id=account["id"], display_name="Ученик", profile_gate=True))
            except Exception as exc:  # noqa: BLE001 - reported below
                errors.append(exc)

        threads = [threading.Thread(target=start) for _ in range(workers)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        self.assertEqual(errors, [])
        self.assertEqual(len(results), workers)
        return results

    def test_simultaneous_starts_of_a_marked_account_create_nothing_and_all_say_profile_required(self):  # J8
        account, _ = self.student()
        results = self.start_together(account)
        self.assertTrue(all(r.profile_required and r.attempt_id is None and not r.created for r in results))
        self.assertEqual(self.attempt_count(), 0)

    def test_simultaneous_starts_of_an_unmarked_account_still_end_in_one_attempt(self):
        account, _ = self.student(marked=False)
        results = self.start_together(account)
        rows = self.query("SELECT id FROM quiz_attempts")
        self.assertEqual(len(rows), 1)
        self.assertEqual({r.attempt_id for r in results}, {rows[0][0]})
        self.assertEqual(sum(r.created for r in results), 1)
        self.assertFalse(any(r.profile_required for r in results))

    def test_two_http_posts_released_together_both_get_the_profile_redirect(self):
        account, client_a = self.student()
        client_b = self.client(account)
        barrier, responses = threading.Barrier(2), []

        def post(client):
            barrier.wait(timeout=10)
            responses.append(self.start(client, self.assignment_id))

        threads = [threading.Thread(target=post, args=(c,)) for c in (client_a, client_b)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        self.assertEqual(len(responses), 2)
        for response in responses:
            self.assert_sent_to_the_profile(response)
        self.assertEqual(self.attempt_count(), 0)


if __name__ == "__main__":
    unittest.main()
