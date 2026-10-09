"""Assignment lifecycle on the student routes: entry, start, the attempt and submission.

Expiry without any job, the exclusive deadline and grace cutoffs, in-flight
eligibility, the three submission outcomes, double submission, the observation
markers, holds and clock faults. Case ids refer to
docs/ASSIGNMENT_LIFECYCLE_DESIGN.md, section 15.1.
"""

import json
import sqlite3
import threading
import time
import unittest
from contextlib import closing
from datetime import datetime, timedelta
from unittest.mock import patch

from tests.lifecycle_support import GRACE, SAME_ORIGIN, LifecycleTestCase, ts
from tests.support import web_app
from web.attempt_access import quiz_attempt_owner_session_key
from web.services import lifecycle
from web.services import quiz as quiz_service

SECOND = timedelta(seconds=1)


class StudentRouteTestCase(LifecycleTestCase):
    def setUp(self):
        super().setUp()
        self.teacher = self.account("teacher")
        self.student = self.account("student", display_name="Ива Ученичка")
        self.browser = self.client(self.student)
        self.E = self.T0 + timedelta(hours=2)  # the deadline most tests use

    def expiring(self, **columns) -> int:
        return self.assignment(self.teacher, deadline_at=self.E, **columns)

    def started_before(self, assignment_id, account=None, **columns) -> int:
        """An attempt started half an hour before T0: before the closure, and never ahead of the clock."""
        return self.attempt(assignment_id, account or self.student, started_at=self.T0 - timedelta(minutes=30),
                            **columns)

    def page(self, client, path):
        response = client.get(path)
        return response.status_code, response.get_data(as_text=True)


class ExpiryWithoutJobsTest(StudentRouteTestCase):
    def test_l38_a_passed_deadline_closes_the_assignment_for_every_request(self):
        assignment_id = self.expiring()
        self.clock.set(self.E + timedelta(days=3))  # nothing ran in between
        for client in (self.client(), self.browser):  # L55: anonymous and signed in
            status, html = self.page(client, self.entry(assignment_id))
            self.assertEqual(status, 200)
            self.assertIn('id="assignment-closed-notice"', html)
            self.assertIn("Тестът е затворен и вече не приема нови опити.", html)
            self.assertNotIn('<form method="post"', html)
            self.assertNotIn("Продължи към теста", html)
        refused = self.start(self.browser, assignment_id)  # L56
        self.assertEqual(refused.status_code, 409)
        self.assertIn('id="assignment-closed-notice"', refused.get_data(as_text=True))
        self.assertEqual(self.attempts(assignment_id), [])

    def test_l39_the_first_request_after_the_deadline_stamps_the_observation_once(self):
        assignment_id = self.expiring()
        self.clock.set(self.E + timedelta(minutes=3))
        self.assertIsNone(self.row(assignment_id)["expiry_observed_at"])
        self.client().get(self.entry(assignment_id))  # a plain GET is an observer
        first = self.lifecycle_columns(assignment_id)
        self.assertEqual(first["expiry_observed_at"], ts(self.E + timedelta(minutes=3)))
        self.assertIsNone(first["submissions_closed_at"])  # the grace window has not ended yet
        self.clock.advance(60)
        self.client().get(self.entry(assignment_id))
        self.start(self.browser, assignment_id)
        self.assertEqual(self.lifecycle_columns(assignment_id), first)
        # The end of the grace window is observed separately, by whoever sees it first.
        self.clock.set(self.E + GRACE + timedelta(minutes=1))
        self.client().get(self.entry(assignment_id))
        after = self.lifecycle_columns(assignment_id)
        self.assertEqual(after, {**first, "submissions_closed_at": ts(self.E + GRACE + timedelta(minutes=1))})
        self.assertIsNone(after["closed_at"])  # an expiry never becomes a manual closure

    def test_l104_a_skipped_stamp_does_not_change_the_response(self):
        assignment_id = self.expiring()
        self.clock.set(self.E + timedelta(minutes=1))
        with closing(sqlite3.connect(str(self.db_path))) as holder:
            holder.execute("BEGIN IMMEDIATE")  # the stamp cannot take the lock
            started = time.monotonic()
            status, html = self.page(self.client(), self.entry(assignment_id))
            self.assertLess(time.monotonic() - started, 2.0)  # and the page did not wait for it
            holder.rollback()
        self.assertEqual(status, 200)
        self.assertIn('id="assignment-closed-notice"', html)
        self.assertIsNone(self.row(assignment_id)["expiry_observed_at"])
        self.client().get(self.entry(assignment_id))  # the next observer stamps it
        self.assertEqual(self.row(assignment_id)["expiry_observed_at"], ts(self.E + timedelta(minutes=1)))

    def test_l10_a_no_deadline_assignment_stays_open(self):
        assignment_id = self.assignment(self.teacher)
        self.clock.set(self.T0 + timedelta(days=400))
        status, html = self.page(self.browser, self.entry(assignment_id))
        self.assertEqual(status, 200)
        self.assertNotIn("assignment-closed-notice", html)
        attempt_id = self.attempt_id_of(self.start(self.browser, assignment_id))
        self.assertEqual(self.submit(self.browser, attempt_id).status_code, 302)
        self.assertEqual(self.attempt_row(attempt_id)["submitted_at"], ts(self.T0 + timedelta(days=400)))
        self.assertEqual(self.lifecycle_columns(assignment_id),
                         {**{c: None for c in lifecycle.LIFECYCLE_COLUMNS}, "display_timezone": "Europe/Sofia"})

    def test_l90_the_token_keeps_resolving_and_a_malformed_one_is_still_404(self):
        assignment_id = self.expiring()
        self.clock.set(self.E + timedelta(days=40))
        self.assertEqual(self.client().get(self.entry(assignment_id)).status_code, 200)
        for token in ("A" * 42, "A" * 44, "nope", "A" * 43):
            self.assertEqual(self.client().get(f"/quiz/s/{token}").status_code, 404)

    def test_the_open_page_shows_the_deadline_in_the_assignments_zone(self):
        assignment_id = self.assignment(self.teacher, deadline_at="2026-03-16 22:00:00")
        _status, html = self.page(self.browser, self.entry(assignment_id))
        self.assertIn("до 23:59 на 16.03.2026", html)
        self.assertIn("(Europe/Sofia)", html)
        self.assertIn("готов за решаване", html)
        # The attempt page shows it too, so a student is warned before the closure.
        attempt_id = self.attempt_id_of(self.start(self.browser, assignment_id))
        _status, attempt_page = self.page(self.browser, f"/quiz/attempt/{attempt_id}")
        self.assertIn('id="attempt-deadline-note"', attempt_page)
        self.assertIn("до 23:59 на 16.03.2026 (Europe/Sofia)", attempt_page)
        self.assertNotIn("attempt-grace-notice", attempt_page)


class StartBoundaryTest(StudentRouteTestCase):
    def test_l41_a_start_in_the_last_second_creates_the_attempt(self):
        assignment_id = self.expiring()
        self.clock.set(self.E - SECOND)
        response = self.start(self.browser, assignment_id)
        self.assertEqual(response.status_code, 302)
        attempt = self.attempt_row(self.attempt_id_of(response))
        self.assertEqual(attempt["started_at"], ts(self.E - SECOND))  # the application's now, not SQLite's
        self.assertEqual(attempt["user_id"], self.student["id"])

    def test_l42_a_start_at_the_deadline_is_refused(self):
        assignment_id = self.expiring()
        self.clock.set(self.E)
        self.assertEqual(self.start(self.browser, assignment_id).status_code, 409)
        self.assertEqual(self.attempts(assignment_id), [])
        self.assertEqual(self.row(assignment_id)["expiry_observed_at"], ts(self.E))

    def test_l99_the_displayed_day_is_usable_through_its_last_second(self):
        teacher_client = self.client(self.teacher)
        created = self.create(teacher_client, **self.deadline_form("2026-03-16"))
        assignment_id = self.created_id(created)
        midnight = datetime(2026, 3, 16, 22, 0, 0)  # 2026-03-17 00:00:00 in Sofia
        self.clock.set(midnight - SECOND)             # 23:59:59 on the displayed day
        self.assertEqual(self.start(self.browser, assignment_id).status_code, 302)
        self.clock.set(midnight)
        self.assertEqual(self.start(self.client(self.account()), assignment_id).status_code, 409)

    def test_l43_a_start_that_gets_the_lock_at_the_deadline_is_refused(self):
        assignment_id = self.expiring()
        self.clock.set(self.E - SECOND)
        result = {}
        with closing(sqlite3.connect(str(self.db_path))) as holder:
            holder.execute("BEGIN IMMEDIATE")
            worker = threading.Thread(
                target=lambda: result.setdefault("response", self.start(self.browser, assignment_id)))
            worker.start()               # issued at E - 1 s
            time.sleep(0.4)
            self.clock.set(self.E)       # the writer lock is released at E
            holder.rollback()
            worker.join(timeout=10)
        self.assertEqual(result["response"].status_code, 409)
        self.assertEqual(self.attempts(assignment_id), [])

    def test_l61_two_starts_of_one_account_make_one_attempt(self):
        assignment_id = self.expiring()
        first = self.start(self.browser, assignment_id)
        second = self.start(self.client(self.student), assignment_id)  # another device, same account
        self.assertEqual(first.headers["Location"], second.headers["Location"])
        self.assertEqual(len(self.attempts(assignment_id)), 1)

        responses = []
        other = self.account()
        workers = [threading.Thread(target=lambda: responses.append(self.start(self.client(other), assignment_id)))
                   for _ in range(4)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=10)
        self.assertEqual({r.status_code for r in responses}, {302})
        self.assertEqual(len({r.headers["Location"] for r in responses}), 1)
        self.assertEqual(len(self.attempts(assignment_id)), 2)

    def test_l62_a_start_and_a_close_are_ordered_by_their_transactions(self):
        owner = self.client(self.teacher)
        close_first = self.assignment(self.teacher)
        owner.post(f"/teacher/assignment/{close_first}/close", headers=SAME_ORIGIN)
        self.assertEqual(self.start(self.browser, close_first).status_code, 409)  # the same second
        self.assertEqual(self.attempts(close_first), [])

        start_first = self.assignment(self.teacher)
        attempt_id = self.attempt_id_of(self.start(self.browser, start_first))
        owner.post(f"/teacher/assignment/{start_first}/close", headers=SAME_ORIGIN)  # the same second
        row, attempt = self.row(start_first), self.attempt_row(attempt_id)
        self.assertEqual(row["closed_at"], attempt["started_at"])   # equal, and no stored time was adjusted
        self.assertEqual(row["closed_at"], ts(self.T0))             # never later than the clock
        self.clock.advance(60)
        self.assertEqual(self.submit(self.browser, attempt_id).status_code, 302)  # in flight: submittable in grace
        self.assertEqual(self.clock_row()["state"], "ok")

    def test_l56_a_stranger_cannot_start_and_an_anonymous_visitor_is_sent_to_login(self):
        assignment_id = self.expiring()
        self.clock.set(self.E + SECOND)
        anonymous = self.start(self.client(), assignment_id)
        self.assertEqual((anonymous.status_code, anonymous.headers["Location"]),
                         (302, f"/login?next={self.entry(assignment_id)}"))
        self.assertEqual(self.attempts(assignment_id), [])


class GraceTest(StudentRouteTestCase):
    def test_l49_l50_l51_the_grace_window_is_half_open(self):
        cases = ((self.E + SECOND, 302), (self.E + GRACE - SECOND, 302), (self.E + GRACE, 409))
        for when, expected in cases:
            with self.subTest(when=when):
                assignment_id = self.expiring()
                attempt_id = self.started_before(assignment_id, self.account())
                browser = self.client({"id": self.attempt_row(attempt_id)["user_id"],
                                       "session_key": self.query(
                                           "SELECT session_key FROM users WHERE id = ?",
                                           (self.attempt_row(attempt_id)["user_id"],))[0][0]})
                self.clock.set(when)
                response = self.submit(browser, attempt_id)
                self.assertEqual(response.status_code, expected)
                attempt = self.attempt_row(attempt_id)
                if expected == 302:
                    self.assertEqual(attempt["submitted_at"], ts(when))
                    self.assertGreater(self.answer_count(attempt_id), 0)
                else:
                    self.assertIsNone(attempt["submitted_at"])
                    self.assertIsNone(attempt["score_correct"])
                    self.assertEqual(self.answer_count(attempt_id), 0)
                    self.assertIn('id="attempt-closed-notice"', response.get_data(as_text=True))
                    # The observer stamps the end of submissions; from now on no clock is consulted.
                    self.assertEqual(self.row(assignment_id)["submissions_closed_at"], ts(when))
                self.clock.set(self.T0)
                self.execute("UPDATE lifecycle_clock SET high_water_at = ?", (ts(self.T0),))

    def test_an_accepted_grace_submission_persists_the_expiry_it_observed(self):
        # Codex finding 1: the observation was stamped only when the submission was refused.
        assignment_id = self.expiring()
        attempt_id = self.started_before(assignment_id)
        other = self.account()
        other_attempt = self.started_before(assignment_id, other)
        when = self.E + timedelta(minutes=5)
        self.clock.set(when)
        self.assertIsNone(self.row(assignment_id)["expiry_observed_at"])

        self.assertEqual(self.submit(self.browser, attempt_id).status_code, 302)
        row = self.row(assignment_id)
        self.assertEqual(row["expiry_observed_at"], ts(when))   # in the same transaction as the submission
        self.assertEqual(self.attempt_row(attempt_id)["submitted_at"], ts(when))
        self.assertEqual((row["closed_at"], row["submissions_closed_at"]), (None, None))  # the window is still open

        # Grace behaviour is unchanged: another in-flight attempt still submits, the marker is written once.
        self.clock.advance(120)
        self.assertEqual(self.submit(self.client(other), other_attempt).status_code, 302)
        self.assertEqual(self.row(assignment_id)["expiry_observed_at"], ts(when))
        self.assertEqual(self.start(self.client(self.account()), assignment_id).status_code, 409)

    def test_the_observation_commits_atomically_with_the_submission(self):
        assignment_id = self.expiring()
        attempt_id = self.started_before(assignment_id)
        self.clock.set(self.E + timedelta(minutes=5))
        with patch.object(quiz_service, "quiz_write_attempt_note") as note:
            with patch.object(quiz_service, "quiz_record_planned_open_text_answers",
                              side_effect=RuntimeError("after the answers were inserted")):
                with self.assertRaises(RuntimeError):
                    self.submit(self.browser, attempt_id)
            # Rolled back as one: no answers, no submission and no observation.
            self.assertEqual((self.answer_count(attempt_id), self.attempt_row(attempt_id)["submitted_at"],
                              self.row(assignment_id)["expiry_observed_at"]), (0, None, None))
            self.assertEqual(note.call_count, 0)
            self.assertEqual(self.submit(self.browser, attempt_id).status_code, 302)
            self.assertEqual(note.call_count, 1)  # graded and noted once
        self.assertEqual(self.row(assignment_id)["expiry_observed_at"], ts(self.E + timedelta(minutes=5)))
        self.assertGreater(self.answer_count(attempt_id), 0)

    def test_an_empty_submission_in_grace_is_refused_and_still_records_the_expiry(self):
        assignment_id = self.expiring()
        attempt_id = self.started_before(assignment_id)
        self.clock.set(self.E + timedelta(minutes=2))
        self.assertEqual(self.submit(self.browser, attempt_id, {}).status_code, 400)
        self.assertEqual(self.row(assignment_id)["expiry_observed_at"], ts(self.E + timedelta(minutes=2)))
        self.assertIsNone(self.attempt_row(attempt_id)["submitted_at"])

    def test_l52_only_attempts_in_flight_at_the_closure_may_use_the_window(self):
        expired = self.expiring()
        closed_at = self.E - timedelta(minutes=10)
        manual = self.assignment(self.teacher, closed_at=closed_at, closed_by_user_id=self.teacher["id"])
        accounts = [self.account() for _ in range(3)]
        late_on_expired = self.attempt(expired, accounts[0], started_at=self.E)                 # >= deadline
        late_on_manual = self.attempt(manual, accounts[1], started_at=closed_at + SECOND)       # > closed_at
        same_second = self.attempt(manual, accounts[2], started_at=closed_at)                   # = closed_at
        self.clock.set(self.E + timedelta(minutes=2))  # inside both windows
        outcomes = [self.submit(self.client(account), attempt_id).status_code
                    for account, attempt_id in zip(accounts, (late_on_expired, late_on_manual, same_second))]
        self.assertEqual(outcomes, [409, 409, 302])
        self.assertEqual([self.answer_count(a) for a in (late_on_expired, late_on_manual)], [0, 0])

    def test_l57_l60_the_attempt_page_in_grace_shows_the_cutoff_in_the_assignments_zone(self):
        assignment_id = self.assignment(self.teacher, zone="America/New_York")
        attempt_id = self.attempt_id_of(self.start(self.browser, assignment_id))
        self.clock.advance(120)
        self.client(self.teacher).post(f"/teacher/assignment/{assignment_id}/close", headers=SAME_ORIGIN)
        self.clock.advance(60)

        entry = self.browser.get(self.entry(assignment_id))  # L57: redirected to the attempt
        self.assertEqual((entry.status_code, entry.headers["Location"]), (302, f"/quiz/attempt/{attempt_id}"))
        status, html = self.page(self.browser, f"/quiz/attempt/{attempt_id}")
        self.assertEqual(status, 200)
        self.assertIn('id="attempt-grace-notice"', html)
        # Closed at 09:02 UTC = 04:02 in New York; the cutoff is closed_at + G.
        self.assertIn("<strong>02.03.2026 04:17</strong> (America/New_York)", html)
        self.assertIn('id="quiz-form"', html)
        self.assertNotIn("attempt-closed-notice", html)

    def test_l58_after_the_cutoff_the_attempt_page_shows_no_questions(self):
        assignment_id = self.expiring()
        attempt_id = self.started_before(assignment_id)
        self.clock.set(self.E + GRACE)
        status, html = self.page(self.browser, f"/quiz/attempt/{attempt_id}")
        self.assertEqual(status, 200)
        self.assertIn('id="attempt-closed-notice"', html)
        self.assertIn("Времето за предаване изтече", html)
        for absent in ('id="quiz-form"', "question-card", 'name="q_', "countdown"):
            self.assertNotIn(absent, html)
        self.assertEqual(self.row(assignment_id)["submissions_closed_at"], ts(self.E + GRACE))
        # The entry page no longer leads anywhere but the same notice.
        entry = self.browser.get(self.entry(assignment_id))
        self.assertEqual((entry.status_code, entry.headers["Location"]), (302, f"/quiz/attempt/{attempt_id}"))

    def test_l59_a_submitted_attempt_always_leads_to_its_result(self):
        assignment_id = self.expiring()
        attempt_id = self.started_before(assignment_id, submitted_at=self.T0 - timedelta(minutes=5))
        for when in (self.E + SECOND, self.E + GRACE + timedelta(days=5)):
            self.clock.set(when)
            for path in (self.entry(assignment_id), f"/quiz/attempt/{attempt_id}"):
                with self.subTest(when=when, path=path):
                    response = self.browser.get(path)
                    self.assertEqual((response.status_code, response.headers["Location"]),
                                     (302, f"/quiz/attempt/{attempt_id}/result"))
            self.assertEqual(self.browser.get(f"/quiz/attempt/{attempt_id}/result").status_code, 200)

    def test_l63_a_submission_and_a_close_both_orders_store_the_submission(self):
        owner = self.client(self.teacher)
        for close_first in (False, True):
            with self.subTest(close_first=close_first):
                assignment_id = self.assignment(self.teacher)
                account = self.account()
                browser = self.client(account)
                attempt_id = self.attempt_id_of(self.start(browser, assignment_id))
                self.clock.advance(30)
                if close_first:
                    owner.post(f"/teacher/assignment/{assignment_id}/close", headers=SAME_ORIGIN)
                    self.assertEqual(self.submit(browser, attempt_id).status_code, 302)  # a grace submission
                else:
                    self.assertEqual(self.submit(browser, attempt_id).status_code, 302)
                    owner.post(f"/teacher/assignment/{assignment_id}/close", headers=SAME_ORIGIN)
                self.assertIsNotNone(self.attempt_row(attempt_id)["submitted_at"])
                self.assertIsNotNone(self.row(assignment_id)["closed_at"])

    def test_l44_a_submission_that_gets_the_lock_at_the_cutoff_is_closed(self):
        assignment_id = self.expiring()
        attempt_id = self.started_before(assignment_id)
        self.clock.set(self.E + GRACE - SECOND)
        result = {}
        with closing(sqlite3.connect(str(self.db_path))) as holder:
            holder.execute("BEGIN IMMEDIATE")
            worker = threading.Thread(
                target=lambda: result.setdefault("response", self.submit(self.browser, attempt_id)))
            worker.start()                        # issued one second before the cutoff
            time.sleep(0.4)
            self.clock.set(self.E + GRACE)        # the lock is obtained at the cutoff
            holder.rollback()
            worker.join(timeout=10)
        self.assertEqual(result["response"].status_code, 409)
        self.assertEqual((self.attempt_row(attempt_id)["submitted_at"], self.answer_count(attempt_id)), (None, 0))

    def test_l91_nothing_in_the_form_decides_the_cutoff(self):
        assignment_id = self.expiring(time_limit=60)
        attempt_id = self.started_before(assignment_id)
        self.clock.set(self.E + GRACE + SECOND)
        forged = {**self.answers(attempt_id), "submission_reason": "timer_expired", "remaining_seconds": "9999",
                  "submitted_at": ts(self.E - SECOND), "started_at": ts(self.E - timedelta(hours=1)),
                  "deadline_at": "2099-01-01 00:00:00", "now": ts(self.E - SECOND)}
        self.assertEqual(self.submit(self.browser, attempt_id, forged).status_code, 409)
        self.assertEqual(self.submit(self.browser, attempt_id, {"submission_reason": "timer_expired"}).status_code, 409)
        self.assertIsNone(self.attempt_row(attempt_id)["submitted_at"])

    def test_in_grace_a_timed_attempt_counts_down_to_the_earlier_cutoff(self):
        assignment_id = self.expiring(time_limit=240)
        attempt_id = self.started_before(assignment_id)  # 90 minutes of the timer are left at E
        self.clock.set(self.E + timedelta(minutes=5))
        _status, html = self.page(self.browser, f"/quiz/attempt/{attempt_id}")
        self.assertIn(f'data-remaining="{10 * 60}"', html)  # E + G is earlier than the timer's end

    def test_l71_a_historical_attempt_follows_the_same_rules(self):
        assignment_id = self.expiring()
        in_time = self.attempt(assignment_id, None, started_at=self.E - timedelta(minutes=30), name="Стар ученик")
        too_late = self.attempt(assignment_id, None, started_at=self.E - timedelta(minutes=20), name="Друг")
        browser = self.client()
        with browser.session_transaction() as session:
            session[quiz_attempt_owner_session_key(in_time)] = True
            session[quiz_attempt_owner_session_key(too_late)] = True
        self.clock.set(self.E + timedelta(minutes=5))
        self.assertIn('id="attempt-grace-notice"', browser.get(f"/quiz/attempt/{in_time}").get_data(as_text=True))
        self.assertEqual(self.submit(browser, in_time).status_code, 302)
        self.clock.set(self.E + GRACE)
        self.assertEqual(self.submit(browser, too_late).status_code, 409)
        self.assertIsNone(self.attempt_row(too_late)["submitted_at"])

    def test_l72_a_mixed_attempt_submitted_in_grace_keeps_its_scores(self):
        with closing(sqlite3.connect(str(self.db_path))) as conn:
            open_id = conn.execute(
                "INSERT INTO questions (source_exam, source_number, question_type, topic, difficulty, points, "
                "prompt, has_image, is_ai_generated, quality_score) VALUES ('lifecycle-mixed', 16, 'fill_in', "
                "'test', 'medium', 1, 'Попълнете липсващите стойности.', 0, 0, NULL)").lastrowid
            for number, answer in ((1, "клиент"), (2, "сървър")):
                conn.execute("INSERT INTO fill_in_subquestions (question_id, subquestion_number, correct_answer, "
                             "answer_alternatives) VALUES (?, ?, ?, NULL)", (open_id, number, answer))
            conn.commit()
        plan = {"mixed_open_enabled": True, "question_ids": [*self.question_ids[:2], open_id],
                "open_question_ids": [open_id], "include_open_answers_in_final_score": True}
        assignment_id = self.expiring(question_plan_json=json.dumps(plan))
        attempt_id = self.started_before(assignment_id)
        self.execute("UPDATE quiz_attempts SET question_ids_json = ? WHERE id = ?", (json.dumps(plan), attempt_id))
        with closing(web_app.quiz_db()) as conn:
            correct = quiz_service.quiz_correct_mc_letters_by_id(conn, self.question_ids[:2])

        self.clock.set(self.E + timedelta(minutes=7))
        response = self.submit(self.browser, attempt_id, {
            f"q_{self.question_ids[0]}": correct[self.question_ids[0]],
            f"open_q_{open_id}_1": "клиент", f"open_q_{open_id}_2": "грешно"})
        self.assertEqual(response.status_code, 302)
        attempt = self.attempt_row(attempt_id)
        self.assertEqual((attempt["score_correct"], attempt["score_total"]), (1, 2))  # the MC score, stored
        open_rows = self.query("SELECT subquestion_number, is_correct FROM quiz_text_answers WHERE attempt_id = ? "
                               "ORDER BY subquestion_number", (attempt_id,))
        self.assertEqual([tuple(r) for r in open_rows], [(1, 1), (2, 0)])
        html = self.browser.get(f"/quiz/attempt/{attempt_id}/result").get_data(as_text=True)
        self.assertEqual(self.browser.get(f"/quiz/attempt/{attempt_id}/result").status_code, 200)
        self.assertIn("клиент", html)


class SubmissionOutcomeTest(StudentRouteTestCase):
    def submit_service(self, attempt_id, form=None, **kwargs):
        with closing(web_app.quiz_db()) as conn:
            plan = json.loads(self.attempt_row(attempt_id)["question_ids_json"])
            return quiz_service.quiz_submit_attempt(
                conn, attempt_id, mc_question_ids=plan, question_ids=plan, open_question_ids=[],
                form=self.answers(attempt_id) if form is None else form, **kwargs)

    def dump(self):
        with closing(sqlite3.connect(str(self.db_path))) as conn:
            tables = [row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")]
            return {table: conn.execute(f'SELECT * FROM "{table}" ORDER BY 1').fetchall() for table in tables}

    def test_l64_l65_the_second_of_two_submissions_writes_nothing(self):
        assignment_id = self.expiring()
        attempt_id = self.started_before(assignment_id)
        with closing(web_app.quiz_db()) as conn:
            correct = quiz_service.quiz_correct_mc_letters_by_id(conn, self.question_ids)
        right = {f"q_{qid}": correct[qid] for qid in self.question_ids}

        first = self.submit_service(attempt_id, right)
        self.assertEqual(first, (quiz_service.SUBMIT_SUBMITTED, 3, 3))
        stored = self.dump()
        self.clock.advance(30)
        second = self.submit_service(attempt_id, {})  # different answers: none at all
        self.assertEqual(second, (quiz_service.SUBMIT_ALREADY_SUBMITTED, None, None))
        self.assertEqual(self.dump(), stored)  # no row in any table, the high-water mark included
        attempt = self.attempt_row(attempt_id)
        self.assertEqual((attempt["submitted_at"], attempt["score_correct"]), (ts(self.T0), 3))

    def test_l65_l118_a_resent_post_is_redirected_and_has_no_side_effects(self):
        assignment_id = self.expiring()
        attempt_id = self.attempt_id_of(self.start(self.browser, assignment_id))
        with patch.object(quiz_service, "quiz_write_attempt_note") as attempt_note, \
                patch.object(quiz_service, "quiz_write_assignment_note") as assignment_note:
            first = self.submit(self.browser, attempt_id)
            stored = self.dump()
            for when in (self.T0 + timedelta(seconds=5), self.E + GRACE + timedelta(hours=1)):  # L66
                self.clock.set(when)
                resent = self.submit(self.browser, attempt_id, {})
                self.assertEqual((resent.status_code, resent.headers["Location"]),
                                 (302, f"/quiz/attempt/{attempt_id}/result"))
        self.assertEqual(first.status_code, 302)
        self.assertEqual((attempt_note.call_count, assignment_note.call_count), (1, 1))
        self.assertEqual(self.dump(), stored)

    def test_an_already_submitted_retry_in_grace_writes_nothing_not_even_the_observation(self):
        # The retry stays write-free: no answer, no second grading, no note, no marker, no high-water mark.
        assignment_id = self.expiring()
        attempt_id = self.started_before(assignment_id, submitted_at=self.T0 - timedelta(minutes=5))
        self.execute("INSERT INTO quiz_answers (attempt_id, question_id, chosen_letter, is_correct) "
                     "VALUES (?, ?, 'А', 1)", (attempt_id, self.question_ids[0]))
        self.clock.set(self.E + timedelta(minutes=5))  # the deadline has passed and nothing observed it yet
        stored = self.dump()
        with patch.object(quiz_service, "quiz_write_attempt_note") as attempt_note, \
                patch.object(quiz_service, "quiz_write_assignment_note") as assignment_note:
            self.assertEqual(self.submit_service(attempt_id, {}),
                             (quiz_service.SUBMIT_ALREADY_SUBMITTED, None, None))
            resent = self.submit(self.browser, attempt_id, {})
        self.assertEqual((resent.status_code, resent.headers["Location"]),
                         (302, f"/quiz/attempt/{attempt_id}/result"))
        self.assertEqual((attempt_note.call_count, assignment_note.call_count), (0, 0))
        self.assertEqual(self.dump(), stored)
        self.assertIsNone(self.row(assignment_id)["expiry_observed_at"])

    def test_l66_already_submitted_wins_over_closed_at_any_time(self):
        assignment_id = self.expiring()
        attempt_id = self.started_before(assignment_id, submitted_at=self.T0 - timedelta(minutes=1))
        self.clock.set(self.E + GRACE + timedelta(hours=1))
        self.assertEqual(self.submit_service(attempt_id).outcome, quiz_service.SUBMIT_ALREADY_SUBMITTED)

    def test_l67_a_failure_before_the_commit_leaves_nothing_and_a_retry_succeeds(self):
        assignment_id = self.expiring()
        attempt_id = self.attempt_id_of(self.start(self.browser, assignment_id))
        with patch.object(quiz_service, "quiz_write_attempt_note") as note:
            with patch.object(quiz_service, "quiz_record_planned_open_text_answers",
                              side_effect=RuntimeError("after the answers were inserted")):
                with self.assertRaises(RuntimeError):
                    self.submit(self.browser, attempt_id)
            self.assertEqual(note.call_count, 0)
        self.assertEqual((self.answer_count(attempt_id), self.attempt_row(attempt_id)["submitted_at"]), (0, None))
        self.assertEqual(self.submit(self.browser, attempt_id).status_code, 302)
        self.assertGreater(self.answer_count(attempt_id), 0)

    def test_l68_an_existing_answer_row_is_never_overwritten(self):
        assignment_id = self.expiring()
        attempt_id = self.started_before(assignment_id)
        self.execute("INSERT INTO quiz_answers (attempt_id, question_id, chosen_letter, is_correct) "
                     "VALUES (?, ?, 'Г', 0)", (attempt_id, self.question_ids[0]))
        with self.assertRaises(sqlite3.IntegrityError):
            self.submit_service(attempt_id)
        self.assertEqual([tuple(r) for r in self.query(
            "SELECT question_id, chosen_letter FROM quiz_answers WHERE attempt_id = ?", (attempt_id,))],
            [(self.question_ids[0], "Г")])
        self.assertIsNone(self.attempt_row(attempt_id)["submitted_at"])

    def test_an_empty_submission_is_refused_only_while_the_attempt_is_submittable(self):
        assignment_id = self.expiring()
        attempt_id = self.started_before(assignment_id)
        empty = self.submit(self.browser, attempt_id, {})
        self.assertEqual(empty.status_code, 400)
        self.assertIn(quiz_service.QUIZ_EMPTY_SUBMISSION_MESSAGE, empty.get_data(as_text=True))
        self.clock.set(self.E + GRACE)
        self.assertEqual(self.submit(self.browser, attempt_id, {}).status_code, 409)  # closed, not "empty"
        self.assertEqual(self.submit_service(attempt_id, {}, empty_submission=True).outcome,
                         quiz_service.SUBMIT_CLOSED)

    def test_l113_the_worked_case_of_a_close_at_noon(self):
        noon = datetime(2026, 3, 2, 12, 0, 0)
        assignment_id = self.assignment(self.teacher, closed_at=noon, closed_by_user_id=self.teacher["id"])
        first_account, second_account = self.account(), self.account()
        first = self.attempt(assignment_id, first_account, started_at=noon - timedelta(minutes=1))
        second = self.attempt(assignment_id, second_account, started_at=noon - timedelta(minutes=1))
        before = self.lifecycle_columns(assignment_id)

        self.clock.set(noon + timedelta(minutes=5))
        # A. No new start, at any time.
        self.assertEqual(self.start(self.browser, assignment_id).status_code, 409)
        # B. A valid in-flight submission inside the exclusive window is accepted.
        self.assertEqual(self.submit(self.client(first_account), first).status_code, 302)
        self.assertEqual(self.attempt_row(first)["submitted_at"], ts(noon + timedelta(minutes=5)))
        self.assertEqual(self.submit_service(first).outcome, quiz_service.SUBMIT_ALREADY_SUBMITTED)

        self.clock.set(noon + GRACE)  # exactly 12:15
        self.assertEqual(self.submit(self.client(second_account), second).status_code, 409)
        self.assertIsNone(self.attempt_row(second)["submitted_at"])
        # A retry of the first is never answered "closed", also after the cutoff.
        self.clock.set(noon + GRACE + timedelta(hours=3))
        self.assertEqual(self.submit_service(first).outcome, quiz_service.SUBMIT_ALREADY_SUBMITTED)
        self.assertEqual(self.start(self.client(self.account()), assignment_id).status_code, 409)
        # No marker was cleared or changed; the end of submissions was stamped by its observer.
        self.assertEqual(self.lifecycle_columns(assignment_id),
                         {**before, "submissions_closed_at": ts(noon + GRACE)})


class TerminalMarkerTest(StudentRouteTestCase):
    def test_l102_l103_submissions_closed_is_terminal_whatever_the_clock_reads(self):
        assignment_id = self.expiring()
        stays_open = self.started_before(assignment_id)
        stamper_account = self.account()
        stamper = self.started_before(assignment_id, stamper_account)
        self.clock.set(self.E + GRACE)
        self.assertEqual(self.submit(self.client(stamper_account), stamper).status_code, 409)
        self.assertEqual(self.row(assignment_id)["submissions_closed_at"], ts(self.E + GRACE))

        # L103 (b): a regression within the tolerance: lifecycle time stays at the mark.
        self.clock.step_wall(-lifecycle.CLOCK_REGRESSION_TOLERANCE_SECONDS)
        self.assertEqual(self.submit(self.browser, stays_open).status_code, 409)
        self.assertEqual(self.clock_row()["state"], "ok")

        # L102 (a), L103 (a): the clock reads E + 1 s, far behind the mark: found before any outcome.
        self.clock.step_wall(-(lifecycle.GRACE_SECONDS - 6))
        before = (self.lifecycle_columns(assignment_id), self.table_counts())
        self.assertEqual(self.submit(self.browser, stays_open).status_code, 503)
        self.assertEqual(self.clock_row()["state"], "suspect")
        self.assertEqual((self.lifecycle_columns(assignment_id), self.table_counts()), before)
        self.assertIsNone(self.attempt_row(stays_open)["submitted_at"])

    def test_l102_l103_the_marker_decides_without_comparing_times(self):
        # Service level: the state function at a lifecycle time before E, with the marker set.
        marker = {"id": 1, **{c: None for c in lifecycle.LIFECYCLE_COLUMNS},
                  "deadline_at": ts(self.E), "submissions_closed_at": ts(self.E + GRACE)}
        attempt = {"started_at": ts(self.E - timedelta(minutes=30)), "submitted_at": None}
        for now in (self.E - timedelta(days=1), self.E + SECOND, self.E + GRACE - SECOND):
            with self.subTest(now=now):
                view = lifecycle.evaluate(marker, now)
                self.assertTrue(view.is_closed)
                self.assertFalse(lifecycle.attempt_submittable(view, attempt))

        # With a consistent clock row at a time before E and the marker not ahead of it:
        # the submission and the start are "closed", and nothing is written.
        assignment_id = self.assignment(self.teacher, deadline_at=self.E,
                                        submissions_closed_at=self.T0 - timedelta(hours=1))
        attempt_id = self.attempt(assignment_id, self.student, started_at=self.T0 - timedelta(hours=2))
        before = (self.lifecycle_columns(assignment_id), self.table_counts())
        self.assertLess(self.clock.wall(), self.E)
        self.assertEqual(self.submit(self.browser, attempt_id).status_code, 409)
        self.assertEqual(self.start(self.client(self.account()), assignment_id).status_code, 409)
        self.assertEqual((self.lifecycle_columns(assignment_id), self.table_counts()), before)
        self.assertEqual(self.clock_row()["state"], "ok")

    def test_a_marker_ahead_of_an_accepted_clock_stops_the_request_as_a_clock_fault(self):
        # The same fixture with the marker later than lifecycle time is not "closed":
        # the clock is detectably wrong, so the request is refused with 503 and nothing is accepted.
        assignment_id = self.assignment(self.teacher, deadline_at=self.E, submissions_closed_at=self.E + GRACE)
        attempt_id = self.started_before(assignment_id)
        self.clock.set(self.E + SECOND)
        self.assertEqual(self.submit(self.browser, attempt_id).status_code, 503)
        self.assertIsNone(self.attempt_row(attempt_id)["submitted_at"])
        self.assertEqual(self.clock_row()["state_reason"], lifecycle.CLOCK_REASON_FUTURE_TIMESTAMP)

    def test_l80_l81_malformed_timestamps_fail_safe(self):
        malformed = self.assignment(self.teacher, deadline_at="9999-99-99 99:99:99")
        attempt_id = self.attempt(malformed, self.student, started_at=self.T0 - timedelta(hours=1))
        _status, html = self.page(self.client(), self.entry(malformed))
        self.assertIn('id="assignment-closed-notice"', html)
        self.assertEqual(self.start(self.client(self.account()), malformed).status_code, 409)
        self.assertEqual(self.submit(self.browser, attempt_id).status_code, 409)
        owner = self.client(self.teacher)
        edit = owner.post(f"/teacher/assignment/{malformed}/edit",
                          data={"title_bg": "Х", **self.no_deadline_form()}, headers=SAME_ORIGIN)
        self.assertEqual(edit.status_code, 409)  # frozen for edits
        self.assertEqual(owner.post(f"/teacher/assignment/{malformed}/close", headers=SAME_ORIGIN).status_code, 302)
        row = self.row(malformed)
        self.assertEqual((row["deadline_at"], row["closed_at"], row["expiry_observed_at"]),
                         ("9999-99-99 99:99:99", None, None))  # reported, never rewritten or stamped

        # L81: a malformed started_at on a closed assignment is not submittable.
        closed = self.expiring()
        broken = self.attempt(closed, self.account(), started_at="2026-03-02T10:00:00")
        account = {"id": self.attempt_row(broken)["user_id"]}
        account["session_key"] = self.query("SELECT session_key FROM users WHERE id = ?", (account["id"],))[0][0]
        self.clock.set(self.E + SECOND)
        self.assertEqual(self.submit(self.client(account), broken).status_code, 409)


class ClockFaultTest(StudentRouteTestCase):
    def test_l75_l76_l78_a_regression_stops_every_student_write(self):
        manual = self.assignment(self.teacher)
        expired = self.expiring()
        in_flight = self.started_before(expired)
        self.clock.set(self.E + timedelta(minutes=2))
        self.client(self.teacher).post(f"/teacher/assignment/{manual}/close", headers=SAME_ORIGIN)
        self.client().get(self.entry(expired))  # observed
        markers = (self.lifecycle_columns(manual), self.lifecycle_columns(expired))
        self.assertIsNotNone(markers[1]["expiry_observed_at"])

        self.clock.step_wall(-7200)  # the clock now reads before both closures
        counts = self.table_counts()
        for assignment_id in (manual, expired):  # read pages keep working and show closed
            status, html = self.page(self.client(), self.entry(assignment_id))
            self.assertEqual(status, 200)
            self.assertIn('id="assignment-closed-notice"', html)
        self.assertEqual(self.start(self.browser, manual).status_code, 503)
        self.assertEqual(self.clock_row()["state"], "suspect")
        # Including a submission for an in-flight attempt: a grace window cannot be measured
        # with a clock known to be unreliable.
        self.assertEqual(self.submit(self.browser, in_flight).status_code, 503)
        self.assertEqual(self.start(self.client(self.account()), expired).status_code, 503)
        self.assertEqual((self.lifecycle_columns(manual), self.lifecycle_columns(expired)), markers)
        self.assertEqual(self.table_counts(), counts)
        self.assertEqual(self.client().get("/healthz").get_data(as_text=True), "degraded\n")

    def test_a_suspect_clock_offers_no_start_and_tells_the_student_to_keep_the_page(self):
        assignment_id = self.assignment(self.teacher)
        attempt_id = self.attempt_id_of(self.start(self.browser, assignment_id))
        self.execute("UPDATE lifecycle_clock SET state = 'suspect', state_reason = 'clock_step'")
        _status, entry = self.page(self.client(self.account()), self.entry(assignment_id))
        self.assertIn('id="assignment-unavailable-notice"', entry)
        self.assertNotIn('<form method="post"', entry)
        status, attempt_page = self.page(self.browser, f"/quiz/attempt/{attempt_id}")
        self.assertEqual(status, 200)
        self.assertIn('id="attempt-unavailable-notice"', attempt_page)
        self.assertIn('id="quiz-form"', attempt_page)


class HoldAndOwnershipTest(StudentRouteTestCase):
    def test_l116_a_held_assignment_serves_no_student_page(self):
        for hold in ("restore_pending", "clock_review,restore_pending", "not a reason"):
            with self.subTest(hold=hold):
                assignment_id = self.assignment(self.teacher)
                owner_account, stranger = self.account(), self.account()
                unsubmitted = self.attempt(assignment_id, owner_account, started_at=self.T0 - timedelta(hours=1))
                submitted = self.attempt(assignment_id, self.account(), started_at=self.T0 - timedelta(hours=2),
                                         submitted_at=self.T0 - timedelta(hours=1))
                submitted_owner = {"id": self.attempt_row(submitted)["user_id"]}
                submitted_owner["session_key"] = self.query(
                    "SELECT session_key FROM users WHERE id = ?", (submitted_owner["id"],))[0][0]
                self.execute("UPDATE quiz_assignments SET lifecycle_hold = ? WHERE id = ?", (hold, assignment_id))
                before = (self.lifecycle_columns(assignment_id), self.table_counts())

                browser = self.client(owner_account)
                self.assertEqual(self.client().get(self.entry(assignment_id)).status_code, 503)
                self.assertEqual(self.start(self.client(stranger), assignment_id).status_code, 503)
                self.assertEqual(browser.get(f"/quiz/attempt/{unsubmitted}").status_code, 503)
                self.assertEqual(self.submit(browser, unsubmitted).status_code, 503)
                finished = self.client(submitted_owner)
                self.assertEqual(finished.get(f"/quiz/attempt/{submitted}").status_code, 503)
                self.assertEqual(finished.get(f"/quiz/attempt/{submitted}/result").status_code, 503)
                # A stranger gets the usual 404: the hold reveals nothing about a foreign attempt.
                for path in (f"/quiz/attempt/{unsubmitted}", f"/quiz/attempt/{submitted}/result"):
                    self.assertEqual(self.client(stranger).get(path).status_code, 404)
                self.assertEqual(self.submit(self.client(stranger), unsubmitted).status_code, 404)
                self.assertEqual((self.lifecycle_columns(assignment_id), self.table_counts()), before)

                listing = browser.get("/account/attempts").get_data(as_text=True)
                self.assertIn("временно недостъпен", listing)
                self.assertNotIn(f"/quiz/attempt/{unsubmitted}", listing)

    def test_l85_another_account_gets_404_and_no_closure_notice_in_grace(self):
        assignment_id = self.expiring()
        attempt_id = self.started_before(assignment_id)
        self.clock.set(self.E + timedelta(minutes=3))
        stranger = self.client(self.account())
        for response in (stranger.get(f"/quiz/attempt/{attempt_id}"), self.submit(stranger, attempt_id),
                         self.client(self.teacher).get(f"/quiz/attempt/{attempt_id}"),
                         self.client().get(f"/quiz/attempt/{attempt_id}")):
            html = response.get_data(as_text=True)
            self.assertEqual(response.status_code, 404)
            for notice in ("attempt-grace-notice", "attempt-closed-notice", "затворен"):
                self.assertNotIn(notice, html)
        self.assertIsNone(self.attempt_row(attempt_id)["submitted_at"])

    def test_the_vault_note_records_the_computed_state(self):
        assignment_id = self.expiring()
        note_path = quiz_service.quiz_assignment_note_path(
            assignment_id, self.query("SELECT section_slug FROM curriculum_sections WHERE id = ?",
                                      (self.section_id,))[0][0], self.row(assignment_id)["created_at"])
        with web_app.app.test_request_context():
            quiz_service.quiz_write_assignment_note(assignment_id, "http://localhost/")
            self.assertIn('status: "open"', note_path.read_text(encoding="utf-8"))
            self.clock.set(self.E)
            quiz_service.quiz_write_assignment_note(assignment_id, "http://localhost/")
            self.assertIn('status: "expired"', note_path.read_text(encoding="utf-8"))
            closed = self.assignment(self.teacher, closed_at=self.T0)
            quiz_service.quiz_write_assignment_note(closed, "http://localhost/")
        closed_note = quiz_service.quiz_assignment_note_path(
            closed, self.query("SELECT section_slug FROM curriculum_sections WHERE id = ?",
                               (self.section_id,))[0][0], self.row(closed)["created_at"])
        self.assertIn('status: "closed_manually"', closed_note.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
