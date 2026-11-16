"""Assignment lifecycle on the teacher routes: create, duplicate, edit, manual close.

Deadline choice and validation, the 180-day maximum, the closure transaction and
its zero-row outcomes, the title freeze, the field whitelist, ownership and
request integrity, holds, and snapshot reads. Case ids refer to
docs/ASSIGNMENT_LIFECYCLE_DESIGN.md, section 15.1.
"""

import csv
import io
import re
import sqlite3
import unittest
from contextlib import closing
from datetime import datetime, timedelta
from unittest.mock import patch

from src import manage_lifecycle
from tests.lifecycle_support import GRACE, SAME_ORIGIN, LifecycleTestCase, ts
from tests.support import web_app
from web import access, request_integrity
from web.blueprints import teacher as teacher_module
from web.services import lifecycle
from web.services import quiz as quiz_service

FOREIGN_ORIGIN = {"Origin": "http://evil.example"}
FORGED_FIELDS = {
    "closed_at": "2020-01-01 00:00:00",
    "closed_by_user_id": "1",
    "expiry_observed_at": "2020-01-01 00:00:00",
    "submissions_closed_at": "2020-01-01 00:00:00",
    "retention_base_at": "2020-01-01 00:00:00",
    "lifecycle_hold": "clock_review",
    "owner_user_id": "999",
    "public_token": "A" * 43,
    "deadline_at": "2020-01-01 00:00:00",
    "state": "open",
    "reopen": "1",
}


class TeacherRouteTestCase(LifecycleTestCase):
    def setUp(self):
        super().setUp()
        self.teacher = self.account("teacher")
        self.other_teacher = self.account("teacher")
        self.admin = self.account("admin")
        self.student = self.account("student")
        self.owner = self.client(self.teacher)

    def assignment_count(self) -> int:
        return self.query("SELECT COUNT(*) FROM quiz_assignments")[0][0]

    def post(self, client, assignment_id, action, data=None, headers=SAME_ORIGIN):
        return client.post(f"/teacher/assignment/{assignment_id}/{action}", data=data or {}, headers=headers)

    def close(self, client, assignment_id, **kwargs):
        return self.post(client, assignment_id, "close", **kwargs)

    def edit(self, client, assignment_id, title="Ново заглавие", time_limit="", **deadline):
        form = deadline or self.no_deadline_form()
        return self.post(client, assignment_id, "edit",
                         {"title_bg": title, "time_limit_minutes": time_limit, **form})

    def detail(self, client, assignment_id) -> str:
        response = client.get(f"/teacher/assignment/{assignment_id}")
        self.assertEqual(response.status_code, 200)
        return response.get_data(as_text=True)


class CreateTest(TeacherRouteTestCase):
    def test_l1_the_form_offers_2359_fourteen_days_ahead_in_an_editable_zone(self):
        html = self.owner.get("/teacher/new").get_data(as_text=True)
        self.assertRegex(html, r'id="new-deadline-choice-deadline"[^>]*checked')
        self.assertNotRegex(html, r'id="new-deadline-choice-none"[^>]*checked')
        self.assertRegex(html, r'name="deadline_date" value="2026-03-16"')
        self.assertRegex(html, r'name="deadline_time" value="23:59"')
        self.assertRegex(html, r'<select id="new-timezone" name="display_timezone" required>')
        self.assertIn('<option value="Europe/Sofia" selected>Europe/Sofia</option>', html)
        self.assertEqual(html.count(' selected>'), 1 + html.count('<option value="" selected>'))
        self.assertIn('<option value="America/New_York" >America/New_York</option>', html)

    def test_l1_every_subjects_form_carries_the_deadline_choice(self):
        # Also where the DZI-only blocks of the form are not rendered at all.
        for subject in ("it", "physics", "biology"):
            with self.subTest(subject=subject):
                response = self.owner.get(f"/teacher/new?subject={subject}")
                self.assertEqual(response.status_code, 200)
                html = response.get_data(as_text=True)
                self.assertEqual(html.count('id="deadline-section"'), 1)
                self.assertEqual(html.count('name="deadline_choice"'), 2)
                self.assertEqual(html.count('name="display_timezone"'), 1)
                self.assertRegex(html, r'name="deadline_date" value="2026-03-16"')
                # Inside the form that is posted, before its submit button.
                form = html.split('id="new-assignment-form"', 1)[1].split("</form>", 1)[0]
                self.assertIn('id="deadline-section"', form)
                self.assertLess(form.index('id="deadline-section"'), form.index("Създай тест и линк"))

    def test_l1_the_preselection_follows_the_teachers_most_recent_zone(self):
        self.assignment(self.teacher, zone="Asia/Tokyo")
        self.assignment(self.other_teacher, zone="America/New_York")
        html = self.owner.get("/teacher/new").get_data(as_text=True)
        self.assertIn('<option value="Asia/Tokyo" selected>Asia/Tokyo</option>', html)
        # 2026-03-02 09:00 UTC is 18:00 in Tokyo: the same local date, 14 days ahead.
        self.assertRegex(html, r'name="deadline_date" value="2026-03-16"')

    def test_l2_posting_the_offer_stores_the_next_local_midnight_in_utc(self):
        response = self.create(self.owner, **self.deadline_form("2026-03-16"))
        self.assertEqual(response.status_code, 302)
        row = self.row(self.created_id(response))
        self.assertEqual(row["deadline_at"], "2026-03-16 22:00:00")  # 2026-03-17 00:00 at UTC+2
        self.assertEqual(row["display_timezone"], "Europe/Sofia")
        self.assertEqual(row["owner_user_id"], self.teacher["id"])
        for column in ("closed_at", "closed_by_user_id", "expiry_observed_at", "submissions_closed_at",
                       "retention_base_at", "lifecycle_hold"):
            self.assertIsNone(row[column], column)

    def test_l3_l5_the_stored_instant_depends_on_the_zone_and_the_target_dates_offset(self):
        across_dst = self.created_id(self.create(self.owner, **self.deadline_form("2026-04-03")))
        self.assertEqual(self.row(across_dst)["deadline_at"], "2026-04-03 21:00:00")
        new_york = self.created_id(self.create(
            self.owner, **self.deadline_form("2026-04-03", zone="America/New_York")))
        self.assertEqual(self.row(new_york)["deadline_at"], "2026-04-04 04:00:00")
        self.assertEqual(self.row(new_york)["display_timezone"], "America/New_York")

    def test_the_offer_in_cairo_ends_at_the_next_local_midnight_when_the_clock_is_set_back(self):
        # Codex finding 5, through the form: offered on 2026-10-15 in Africa/Cairo.
        self.clock.set(datetime(2026, 10, 15, 9, 0, 0))
        self.assignment(self.teacher, zone="Africa/Cairo")  # the teacher's most recent zone
        html = self.owner.get("/teacher/new").get_data(as_text=True)
        self.assertIn('<option value="Africa/Cairo" selected>Africa/Cairo</option>', html)
        self.assertRegex(html, r'name="deadline_date" value="2026-10-29"')
        self.assertRegex(html, r'name="deadline_time" value="23:59"')

        created = self.create(self.owner, **self.deadline_form("2026-10-29", zone="Africa/Cairo"))
        assignment_id = self.created_id(created)
        self.assertEqual(self.row(assignment_id)["deadline_at"], "2026-10-29 22:00:00")  # not 21:00:00
        page = self.detail(self.owner, assignment_id)
        self.assertRegex(page, r'id="lifecycle-deadline">\s*до 23:59 на 29\.10\.2026')
        self.assertRegex(page, r'id="edit-deadline-date" name="deadline_date" value="2026-10-29"')
        # Saving the edit form unchanged keeps the cutoff where it is.
        self.assertEqual(self.edit(self.owner, assignment_id, **self.deadline_form("2026-10-29", zone="Africa/Cairo"))
                         .status_code, 302)
        self.assertEqual(self.row(assignment_id)["deadline_at"], "2026-10-29 22:00:00")

        # 21:30 UTC is 23:30 local for the second time: still the displayed day.
        self.clock.set(datetime(2026, 10, 29, 21, 30, 0))
        self.assertEqual(self.start(self.client(self.student), assignment_id).status_code, 302)
        self.clock.set(datetime(2026, 10, 29, 22, 0, 0))  # local midnight
        self.assertEqual(self.start(self.client(self.account()), assignment_id).status_code, 409)

    def test_l7_l8_l19_a_request_without_a_valid_choice_or_zone_creates_nothing(self):
        valid = self.deadline_form("2026-03-16")
        refused = (
            {},                                                      # L8: neither a deadline nor "none"
            {"display_timezone": "Europe/Sofia"},
            {**valid, "deadline_choice": "later"},
            {**valid, "display_timezone": "Europe/Atlantis"},        # L7
            {k: v for k, v in valid.items() if k != "display_timezone"},
            {"deadline_choice": "none"},                             # a zone is always required
            {**valid, "deadline_date": "2026-03-16T23:59"},          # L19
            {**valid, "deadline_time": "23:59:00"},
            {**valid, "deadline_time": "23:59+02:00"},
            {**valid, "deadline_date": "2026-02-30"},
            {**valid, "deadline_date": ""},
        )
        for fields in refused:
            with self.subTest(fields=fields):
                response = self.create(self.owner, **fields)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(self.assignment_count(), 0)
        # The refused form comes back with what was typed and a Bulgarian message.
        html = self.create(self.owner, **{**valid, "display_timezone": "Europe/Atlantis"}).get_data(as_text=True)
        self.assertIn(lifecycle.DEADLINE_ERROR_MESSAGES_BG["timezone_invalid"], html)
        self.assertRegex(html, r'name="deadline_date" value="2026-03-16"')

    def test_l9_no_deadline_stores_null_and_the_zone(self):
        response = self.create(self.owner, **self.no_deadline_form("America/New_York"))
        self.assertEqual(response.status_code, 302)
        row = self.row(self.created_id(response))
        self.assertIsNone(row["deadline_at"])
        self.assertEqual(row["display_timezone"], "America/New_York")
        html = self.detail(self.owner, row["id"])
        self.assertRegex(html, r'id="lifecycle-state-label">отворен<')
        self.assertRegex(html, r'id="lifecycle-deadline">\s*без краен срок')

    def test_l11_a_deadline_at_or_before_now_is_refused(self):
        for day, clock in (("2026-03-02", "10:59"), ("2026-03-01", "23:59"), ("2025-12-31", "23:59")):
            with self.subTest(day=day, clock=clock):
                response = self.create(self.owner, **self.deadline_form(day, clock))
                self.assertEqual(response.status_code, 400)
                self.assertIn(lifecycle.DEADLINE_ERROR_MESSAGES_BG["deadline_in_past"],
                              response.get_data(as_text=True))
        self.assertEqual(self.assignment_count(), 0)

    def test_l12_l13_l16_the_180_day_maximum(self):
        accepted = self.create(self.owner, **self.deadline_form("2026-08-29"))  # today + 180
        self.assertEqual(accepted.status_code, 302)
        self.assertEqual(self.row(self.created_id(accepted))["deadline_at"], "2026-08-29 21:00:00")
        for day, clock in (("2026-08-30", "00:00"), ("2027-03-02", "23:59")):
            with self.subTest(day=day):
                response = self.create(self.owner, **self.deadline_form(day, clock))
                self.assertEqual(response.status_code, 400)
                self.assertIn(lifecycle.DEADLINE_ERROR_MESSAGES_BG["deadline_too_far"],
                              response.get_data(as_text=True))
        self.assertEqual(self.assignment_count(), 1)
        # "No deadline" is not subject to the maximum.
        self.assertEqual(self.create(self.owner, **self.no_deadline_form()).status_code, 302)

    def test_l17_l18_dst_input(self):
        gap = self.create(self.owner, **self.deadline_form("2026-03-29", "03:30"))
        self.assertEqual(gap.status_code, 400)
        self.assertIn(lifecycle.DEADLINE_ERROR_MESSAGES_BG["deadline_nonexistent"], gap.get_data(as_text=True))
        self.assertEqual(self.assignment_count(), 0)
        with self.subTest("autumn overlap"):
            self.clock.set(self.T0 + timedelta(days=120))  # 2026-06-30: the overlap is within 180 days
            overlap = self.create(self.owner, **self.deadline_form("2026-10-25", "03:30"))
            self.assertEqual(overlap.status_code, 302)
            self.assertEqual(self.row(self.created_id(overlap))["deadline_at"], "2026-10-25 00:31:00")

    def test_l117_forged_lifecycle_fields_are_ignored_on_create(self):
        response = self.create(self.owner, **self.deadline_form("2026-03-16"), **{
            k: v for k, v in FORGED_FIELDS.items() if k != "deadline_at"})
        row = self.row(self.created_id(response))
        self.assertEqual(row["deadline_at"], "2026-03-16 22:00:00")
        self.assertEqual(row["owner_user_id"], self.teacher["id"])
        self.assertNotEqual(row["public_token"], "A" * 43)
        for column in ("closed_at", "closed_by_user_id", "expiry_observed_at", "submissions_closed_at",
                       "retention_base_at", "lifecycle_hold"):
            self.assertIsNone(row[column], column)

    def test_a_suspect_clock_refuses_creation(self):
        self.clock.step_wall(-3600)
        response = self.create(self.owner, **self.no_deadline_form())
        self.assertEqual(response.status_code, 503)
        self.assertEqual(self.assignment_count(), 0)
        self.assertEqual(self.clock_row()["state"], "suspect")


class DuplicateTest(TeacherRouteTestCase):
    def duplicate(self, client, assignment_id, **form):
        return self.post(client, assignment_id, "duplicate", form)

    def test_l20_the_duplicate_form_is_the_creation_choice_prefilled_with_the_default(self):
        source = self.assignment(self.teacher, deadline_at=self.T0 + timedelta(days=3), zone="America/New_York")
        html = self.detail(self.owner, source)
        self.assertIn(f'action="/teacher/assignment/{source}/duplicate"', html)
        self.assertRegex(html, r'id="duplicate-deadline-choice-deadline"[^>]*checked')
        self.assertRegex(html, r'id="duplicate-deadline-date" name="deadline_date" value="2026-03-16"')
        self.assertRegex(html, r'id="duplicate-deadline-time" name="deadline_time" value="23:59"')
        self.assertRegex(html, r'<select id="duplicate-timezone" name="display_timezone" required>')
        self.assertIn('id="duplicate-deadline-choice-none"', html)

    def test_l21_l25_a_copy_gets_its_own_deadline_owner_token_and_no_attempts(self):
        source = self.assignment(self.other_teacher, title="Изходен")
        self.attempt(source, self.student, started_at=self.T0 - timedelta(hours=1))
        response = self.duplicate(self.client(self.admin), source, **self.deadline_form("2026-03-16"))
        self.assertEqual(response.status_code, 302)
        copy = self.row(self.created_id(response))
        self.assertNotEqual(copy["id"], source)
        self.assertEqual(copy["deadline_at"], "2026-03-16 22:00:00")
        self.assertEqual(copy["display_timezone"], "Europe/Sofia")
        self.assertEqual(copy["owner_user_id"], self.admin["id"])  # the acting account, not the source's owner
        self.assertNotEqual(copy["public_token"], self.row(source)["public_token"])
        self.assertEqual(self.attempts(copy["id"]), [])
        self.assertRegex(self.detail(self.client(self.admin), copy["id"]), r'id="lifecycle-state-label">отворен<')

    def test_l22_l23_nothing_of_the_sources_lifecycle_is_ever_copied(self):
        closer = self.teacher["id"]
        sources = {
            "open with a deadline": dict(deadline_at=self.T0 + timedelta(days=3), zone="America/New_York"),
            "expired": dict(deadline_at=self.T0 - timedelta(days=3), zone="America/New_York",
                            expiry_observed_at=self.T0 - timedelta(days=2),
                            submissions_closed_at=self.T0 - timedelta(days=2)),
            "closed manually": dict(deadline_at=self.T0 + timedelta(days=30), zone="America/New_York",
                                    closed_at=self.T0 - timedelta(days=1), closed_by_user_id=closer,
                                    retention_base_at=self.T0),
        }
        for label, columns in sources.items():
            with self.subTest(source=label):
                source = self.assignment(self.teacher, **columns)
                before = self.lifecycle_columns(source)
                response = self.duplicate(self.owner, source, **self.no_deadline_form("Europe/Sofia"))
                self.assertEqual(response.status_code, 302)
                copy = self.lifecycle_columns(self.created_id(response))
                self.assertEqual(copy, {**{c: None for c in lifecycle.LIFECYCLE_COLUMNS},
                                        "display_timezone": "Europe/Sofia"})
                # The source keeps everything it had. The only thing a duplicate may add to it
                # is an observation its own transaction made: the end of its grace window.
                after = self.lifecycle_columns(source)
                stamped = {c for c in after if after[c] != before[c]}
                self.assertLessEqual(stamped, {"expiry_observed_at", "submissions_closed_at"})
                self.assertTrue(all(before[c] is None and after[c] == ts(self.T0) for c in stamped))

    def test_a_hold_added_between_the_first_look_and_the_copy_refuses_the_duplicate(self):
        # Codex finding 4: the hold was checked before the writer transaction only.
        source = self.assignment(self.teacher, title="Изходен")
        first_look = lifecycle.parse_deadline_request  # runs after the request's first look at the source

        def hold_then_parse(form):
            # Another connection commits an operator's hold on the source in the meantime.
            with closing(sqlite3.connect(str(self.db_path))) as operator:
                manage_lifecycle.add_hold_reason(operator, source, lifecycle.HOLD_CLOCK_REVIEW)
                operator.commit()
            return first_look(form)

        with patch.object(lifecycle, "parse_deadline_request", side_effect=hold_then_parse):
            response = self.duplicate(self.owner, source, **self.deadline_form("2026-03-16"))
        self.assertEqual(response.status_code, 503)
        self.assertEqual(self.assignment_count(), 1)  # no copy of a held source
        self.assertEqual(self.row(source)["lifecycle_hold"], "clock_review")
        self.assertEqual(self.clock_row()["state"], "ok")
        # Once the hold is resolved the same request succeeds.
        self.execute("UPDATE quiz_assignments SET lifecycle_hold = NULL WHERE id = ?", (source,))
        self.assertEqual(self.duplicate(self.owner, source, **self.deadline_form("2026-03-16")).status_code, 302)

    def test_authorization_is_decided_again_under_the_writer_lock(self):
        source = self.assignment(self.teacher)
        first_look = lifecycle.parse_deadline_request

        def transfer_then_parse(form):
            with closing(sqlite3.connect(str(self.db_path))) as other:
                other.execute("UPDATE quiz_assignments SET owner_user_id = ? WHERE id = ?",
                              (self.other_teacher["id"], source))
                other.commit()
            return first_look(form)

        with patch.object(lifecycle, "parse_deadline_request", side_effect=transfer_then_parse):
            response = self.duplicate(self.owner, source, **self.no_deadline_form())
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.assignment_count(), 1)

    def test_the_sources_factual_timestamps_are_checked_before_it_is_copied(self):
        # A closure stamped ahead of the clock, on a source nobody has held yet: a clock fault,
        # found under the writer lock before anything is copied.
        source = self.assignment(self.teacher, closed_at=self.T0 + timedelta(hours=1))
        response = self.duplicate(self.owner, source, **self.no_deadline_form())
        self.assertEqual(response.status_code, 503)
        self.assertEqual(self.assignment_count(), 1)
        self.assertEqual(self.clock_row()["state_reason"], lifecycle.CLOCK_REASON_FUTURE_TIMESTAMP)

    def test_the_copy_is_made_from_the_source_as_it_is_under_the_lock(self):
        # What is copied is read in the same transaction that creates the copy, and the
        # source's unobserved expiry is recorded by that transaction too.
        source = self.assignment(self.teacher, title="Преди", time_limit=10,
                                 deadline_at=self.T0 - timedelta(minutes=5))
        first_look = lifecycle.parse_deadline_request

        def rename_then_parse(form):
            with closing(sqlite3.connect(str(self.db_path))) as other:
                other.execute("UPDATE quiz_assignments SET title_bg = 'След', time_limit_minutes = 45 "
                              "WHERE id = ?", (source,))
                other.commit()
            return first_look(form)

        with patch.object(lifecycle, "parse_deadline_request", side_effect=rename_then_parse):
            response = self.duplicate(self.owner, source, **self.deadline_form("2026-03-16", zone="Asia/Tokyo"))
        self.assertEqual(response.status_code, 302)
        copy = self.row(self.created_id(response))
        self.assertEqual((copy["title_bg"], copy["time_limit_minutes"]), ("След (копие)", 45))
        # The fresh deadline choice and zone of this request; nothing of the expired source.
        self.assertEqual((copy["deadline_at"], copy["display_timezone"], copy["expiry_observed_at"]),
                         ("2026-03-16 15:00:00", "Asia/Tokyo", None))
        self.assertEqual(self.row(source)["expiry_observed_at"], ts(self.T0))

    def test_l24_l26_a_copy_without_a_valid_choice_or_beyond_the_maximum_is_not_created(self):
        source = self.assignment(self.teacher, deadline_at=self.T0 + timedelta(days=3))
        for form in ({}, {"display_timezone": "Europe/Sofia"}, {"deadline_choice": "none"},
                     self.deadline_form("2026-08-30", "00:00"), self.deadline_form("2026-03-01"),
                     self.deadline_form("2026-03-16", zone="Nowhere/City")):
            with self.subTest(form=form):
                response = self.duplicate(self.owner, source, **form)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(self.assignment_count(), 1)
        html = self.duplicate(self.owner, source, **self.deadline_form("2026-08-30", "00:00")).get_data(as_text=True)
        self.assertRegex(html, r'id="duplicate-error"')
        self.assertIn(lifecycle.DEADLINE_ERROR_MESSAGES_BG["deadline_too_far"], html)

    def test_l86_only_whoever_manages_the_source_may_duplicate_it(self):
        source = self.assignment(self.teacher)
        form = self.no_deadline_form()
        self.assertEqual(self.duplicate(self.client(self.other_teacher), source, **form).status_code, 404)
        self.assertEqual(self.duplicate(self.client(self.student), source, **form).status_code, 403)
        anonymous = self.duplicate(self.client(), source, **form)
        self.assertEqual((anonymous.status_code, anonymous.headers["Location"]),
                         (302, f"/login?next=/teacher/assignment/{source}/duplicate"))
        self.assertEqual(self.assignment_count(), 1)


class CloseTest(TeacherRouteTestCase):
    def test_l27_the_owner_closes_an_open_assignment_at_the_sampled_now(self):
        assignment_id = self.assignment(self.teacher)
        self.clock.advance(77)
        response = self.close(self.owner, assignment_id)
        self.assertEqual((response.status_code, response.headers["Location"]),
                         (302, f"/teacher/assignment/{assignment_id}"))
        row = self.row(assignment_id)
        self.assertEqual(row["closed_at"], ts(self.T0 + timedelta(seconds=77)))
        self.assertEqual(row["closed_by_user_id"], self.teacher["id"])
        self.assertIsNone(row["expiry_observed_at"])
        html = self.detail(self.owner, assignment_id)
        self.assertRegex(html, r'id="lifecycle-state-label">затворен от учителя<')
        self.assertNotIn('id="close-assignment-form"', html)
        self.assertIn('id="edit-frozen-note"', html)

    def test_each_lifecycle_event_leaves_a_record_in_the_application_log(self):
        with self.assertLogs("learnpilot.lifecycle", level="INFO") as logs:
            created = self.created_id(self.create(self.owner, **self.deadline_form("2026-03-16")))
            self.clock.advance(5)
            self.edit(self.owner, created, **self.deadline_form("2026-03-18"))
            self.clock.advance(5)
            self.close(self.client(self.admin), created)
            self.close(self.owner, created)  # already closed: no second event
        events = [line.split("lifecycle ", 1)[1] for line in logs.output if "lifecycle event=" in line]
        self.assertEqual(events, [
            f"event=create assignment_id={created} at='2026-03-02 09:00:00' deadline_at='2026-03-16 22:00:00' "
            f"display_timezone='Europe/Sofia' actor_user_id={self.teacher['id']}",
            f"event=edit assignment_id={created} at='2026-03-02 09:00:05' deadline_at='2026-03-18 22:00:00' "
            f"display_timezone='Europe/Sofia' actor_user_id={self.teacher['id']}",
            f"event=close assignment_id={created} closed_at='2026-03-02 09:00:10' "
            f"actor_user_id={self.admin['id']}",
        ])

    def test_l28_closing_again_changes_nothing_and_answers_the_same(self):
        assignment_id = self.assignment(self.teacher)
        self.close(self.owner, assignment_id)
        before = self.lifecycle_columns(assignment_id)
        self.clock.advance(3600)
        for client in (self.owner, self.client(self.admin)):
            response = self.close(client, assignment_id)
            self.assertEqual((response.status_code, response.headers["Location"]),
                             (302, f"/teacher/assignment/{assignment_id}"))
        self.assertEqual(self.lifecycle_columns(assignment_id), before)

    def test_l29_closing_after_the_deadline_only_records_the_expiry(self):
        deadline = self.T0 + timedelta(hours=1)
        assignment_id = self.assignment(self.teacher, deadline_at=deadline)
        self.clock.set(deadline + timedelta(seconds=5))
        self.assertEqual(self.close(self.owner, assignment_id).status_code, 302)
        row = self.row(assignment_id)
        self.assertIsNone(row["closed_at"])
        self.assertIsNone(row["closed_by_user_id"])
        self.assertEqual(row["expiry_observed_at"], ts(deadline + timedelta(seconds=5)))
        self.assertEqual(row["deadline_at"], ts(deadline))  # E stays the deadline

    def test_l30_closing_before_a_later_deadline_keeps_the_deadline_stored(self):
        deadline = self.T0 + timedelta(days=5)
        assignment_id = self.assignment(self.teacher, deadline_at=deadline)
        self.close(self.owner, assignment_id)
        row = self.row(assignment_id)
        self.assertEqual((row["closed_at"], row["deadline_at"]), (ts(self.T0), ts(deadline)))
        with closing(web_app.quiz_db()) as conn:
            view = lifecycle.evaluate(lifecycle.fetch_lifecycle_row(conn, assignment_id), self.T0)
        self.assertEqual(view.effective_closure, self.T0)

    def test_l31_the_request_body_is_never_read(self):
        assignment_id = self.assignment(self.teacher)
        self.clock.advance(10)
        self.close(self.client(self.admin), assignment_id, data=FORGED_FIELDS)
        row = self.lifecycle_columns(assignment_id)
        self.assertEqual(row["closed_at"], ts(self.T0 + timedelta(seconds=10)))
        self.assertEqual(row["closed_by_user_id"], self.admin["id"])
        for column in ("expiry_observed_at", "submissions_closed_at", "retention_base_at", "lifecycle_hold"):
            self.assertIsNone(row[column], column)

    def test_l123_l125_a_close_in_the_last_second_before_the_deadline_and_of_a_start(self):
        deadline = self.T0 + timedelta(hours=1)
        last_second = deadline - timedelta(seconds=1)
        assignment_id = self.assignment(self.teacher, deadline_at=deadline)
        self.clock.set(last_second)
        student = self.client(self.student)
        attempt_id = self.attempt_id_of(self.start(student, assignment_id))
        self.assertEqual(self.attempt_row(attempt_id)["started_at"], ts(last_second))

        self.assertEqual(self.close(self.owner, assignment_id).status_code, 302)  # the same second
        row = self.row(assignment_id)
        self.assertEqual(row["closed_at"], ts(last_second))   # the sampled time, not moved to the deadline
        self.assertIsNone(row["expiry_observed_at"])
        # Another request in that second raises no clock fault: nothing is later than the clock.
        self.assertEqual(student.get(f"/quiz/attempt/{attempt_id}").status_code, 200)
        self.assertEqual(self.clock_row()["state"], "ok")

        # In flight, with the whole grace window counted from closed_at.
        self.clock.set(last_second + GRACE)
        refused = self.submit(student, attempt_id)
        self.assertEqual(refused.status_code, 409)             # exactly E + G is closed
        self.assertIsNone(self.attempt_row(attempt_id)["submitted_at"])

    def test_l123_the_in_flight_attempt_may_submit_inside_the_window(self):
        deadline = self.T0 + timedelta(hours=1)
        last_second = deadline - timedelta(seconds=1)
        assignment_id = self.assignment(self.teacher, deadline_at=deadline)
        self.clock.set(last_second)
        student = self.client(self.student)
        attempt_id = self.attempt_id_of(self.start(student, assignment_id))
        self.close(self.owner, assignment_id)
        self.clock.set(last_second + GRACE - timedelta(seconds=1))
        self.assertEqual(self.submit(student, attempt_id).status_code, 302)
        self.assertEqual(self.attempt_row(attempt_id)["submitted_at"], ts(last_second + GRACE - timedelta(seconds=1)))

    def test_l124_a_zero_row_close_is_classified_and_never_reported_as_a_closure(self):
        held = self.assignment(self.teacher, lifecycle_hold="clock_review")
        manual = self.assignment(self.teacher, closed_at=self.T0 - timedelta(hours=1),
                                 closed_by_user_id=self.teacher["id"])
        observed = self.assignment(self.teacher, deadline_at=self.T0 - timedelta(hours=2),
                                   expiry_observed_at=self.T0 - timedelta(hours=1))
        unobserved = self.assignment(self.teacher, deadline_at=self.T0 - timedelta(hours=2))
        snapshots = {name: self.lifecycle_columns(name) for name in (held, manual, observed, unobserved)}
        self.clock.advance(1)
        high_water = self.clock_row()["high_water_at"]

        self.assertEqual(self.close(self.owner, held).status_code, 503)
        self.assertEqual(self.lifecycle_columns(held), snapshots[held])
        for assignment_id in (manual, observed):
            with self.subTest(assignment=assignment_id):
                self.assertEqual(self.close(self.owner, assignment_id).status_code, 302)
                self.assertEqual(self.lifecycle_columns(assignment_id), snapshots[assignment_id])
        # Nothing written by any of them: not even the high-water mark.
        self.assertEqual(self.clock_row()["high_water_at"], high_water)

        self.assertEqual(self.close(self.owner, unobserved).status_code, 302)
        after = self.lifecycle_columns(unobserved)
        self.assertEqual(after, {**snapshots[unobserved], "expiry_observed_at": ts(self.T0 + timedelta(seconds=1)),
                                 "submissions_closed_at": ts(self.T0 + timedelta(seconds=1))})
        self.assertIsNone(after["closed_at"])

    def test_l124_an_update_that_matches_nothing_on_an_open_row_fails_and_rolls_back(self):
        assignment_id = self.assignment(self.teacher)
        before = (self.lifecycle_columns(assignment_id), tuple(self.clock_row()))
        self.clock.advance(5)
        with patch.object(lifecycle, "_close_update", return_value=0):
            with self.assertRaises(lifecycle.LifecycleInvariantError):
                self.close(self.owner, assignment_id)
        self.assertEqual((self.lifecycle_columns(assignment_id), tuple(self.clock_row())), before)

    def test_l82_l83_l88_l89_authorization_and_the_declared_policies(self):
        assignment_id = self.assignment(self.teacher)
        before = self.lifecycle_columns(assignment_id)
        self.assertEqual(self.close(self.client(self.other_teacher), assignment_id).status_code, 404)
        self.assertEqual(self.close(self.client(self.student), assignment_id).status_code, 403)
        anonymous = self.close(self.client(), assignment_id)
        self.assertEqual((anonymous.status_code, anonymous.headers["Location"]),
                         (302, f"/login?next=/teacher/assignment/{assignment_id}/close"))
        self.assertEqual(self.close(self.owner, 987654).status_code, 404)
        self.assertEqual(self.owner.get(f"/teacher/assignment/{assignment_id}/close").status_code, 405)  # L88
        self.assertEqual(self.lifecycle_columns(assignment_id), before)

        view = web_app.app.view_functions["teacher.teacher_assignment_close"]  # L89
        self.assertEqual(access.policy_for_view(view), access.ASSIGNMENT_OWNER)
        self.assertEqual(request_integrity.integrity_policy_for_view(view), request_integrity.SAME_ORIGIN)
        rules = [rule for rule in web_app.app.url_map.iter_rules() if rule.endpoint == "teacher.teacher_assignment_close"]
        self.assertEqual([sorted(rule.methods - {"OPTIONS"}) for rule in rules], [["POST"]])

        self.assertEqual(self.close(self.client(self.admin), assignment_id).status_code, 302)  # L83
        self.assertEqual(self.row(assignment_id)["closed_by_user_id"], self.admin["id"])

    def test_l84_a_pending_second_factor_is_anonymous_for_every_lifecycle_route(self):
        assignment_id = self.assignment(self.teacher)
        before = self.lifecycle_columns(assignment_id)
        pending = self.client()
        with pending.session_transaction() as session:
            session.update(mfa_uid=self.admin["id"], mfa_sk=self.admin["session_key"], mfa_iat=1)
        for action in ("close", "edit", "duplicate"):
            with self.subTest(action=action):
                response = self.post(pending, assignment_id, action,
                                     {"title_bg": "Х", **self.no_deadline_form()})
                self.assertEqual(response.status_code, 302)
                self.assertTrue(response.headers["Location"].startswith("/login?next="))
        self.assertEqual(pending.get(f"/teacher/assignment/{assignment_id}/results").status_code, 302)
        self.assertEqual((self.lifecycle_columns(assignment_id), self.assignment_count()), (before, 1))

    def test_l87_cross_origin_requests_are_refused_before_anything_else(self):
        assignment_id = self.assignment(self.teacher)
        before = (dict(self.row(assignment_id)), self.assignment_count())
        form = {"title_bg": "Подправено", **self.no_deadline_form()}
        for headers in (FOREIGN_ORIGIN, {"Referer": "http://evil.example/page"}):
            for action in ("close", "edit", "duplicate"):
                with self.subTest(headers=headers, action=action):
                    response = self.post(self.owner, assignment_id, action, form, headers=headers)
                    self.assertEqual(response.status_code, 403)
        self.assertEqual((dict(self.row(assignment_id)), self.assignment_count()), before)


class EditTest(TeacherRouteTestCase):
    def test_l32_only_the_whitelisted_fields_change(self):
        assignment_id = self.assignment(self.teacher, zone="Europe/Sofia")
        token = self.row(assignment_id)["public_token"]
        response = self.post(self.owner, assignment_id, "edit", {
            "title_bg": "Нов", "time_limit_minutes": "25",
            **self.deadline_form("2026-03-20", "18:30", "America/New_York"),
            **{k: v for k, v in FORGED_FIELDS.items() if k != "deadline_at"}, "deadline_at": "2020-01-01 00:00:00",
        })
        self.assertEqual(response.status_code, 302)
        row = self.row(assignment_id)
        self.assertEqual((row["title_bg"], row["time_limit_minutes"]), ("Нов", 25))
        self.assertEqual((row["deadline_at"], row["display_timezone"]),
                         ("2026-03-20 22:31:00", "America/New_York"))  # 18:30 at UTC-4, plus one minute
        self.assertEqual((row["owner_user_id"], row["public_token"]), (self.teacher["id"], token))
        for column in ("closed_at", "closed_by_user_id", "expiry_observed_at", "submissions_closed_at",
                       "retention_base_at", "lifecycle_hold"):
            self.assertIsNone(row[column], column)

    def test_an_open_assignments_deadline_can_be_moved_earlier_or_removed(self):
        assignment_id = self.assignment(self.teacher, deadline_at=self.T0 + timedelta(days=10))
        self.assertEqual(self.edit(self.owner, assignment_id, **self.deadline_form("2026-03-03")).status_code, 302)
        self.assertEqual(self.row(assignment_id)["deadline_at"], "2026-03-03 22:00:00")
        self.assertEqual(self.edit(self.owner, assignment_id).status_code, 302)  # "no deadline"
        self.assertIsNone(self.row(assignment_id)["deadline_at"])
        html = self.detail(self.owner, assignment_id)
        self.assertRegex(html, r'id="edit-deadline-choice-none"[^>]*checked')

    def test_changing_the_zone_recomputes_and_revalidates_the_cutoff(self):
        # "Through 23:59 on 2 March" in Sofia. The zone can only be changed together with
        # the displayed date and time, and the cutoff is computed again in the new zone.
        assignment_id = self.assignment(self.teacher, deadline_at="2026-03-02 22:00:00")
        moved = self.edit(self.owner, assignment_id, **self.deadline_form("2026-03-02", "23:59", "Asia/Tokyo"))
        self.assertEqual(moved.status_code, 302)
        row = self.row(assignment_id)
        self.assertEqual((row["deadline_at"], row["display_timezone"]), ("2026-03-02 15:00:00", "Asia/Tokyo"))
        html = self.detail(self.owner, assignment_id)
        self.assertRegex(html, r'id="lifecycle-deadline">\s*до 23:59 на 02\.03\.2026')
        self.assertRegex(html, r'id="lifecycle-zone">\s*Asia/Tokyo')
        # The same local time in a zone where it has already passed is refused: nothing changes.
        before = dict(self.row(assignment_id))
        past = self.edit(self.owner, assignment_id, **self.deadline_form("2026-03-02", "22:00", "Pacific/Kiritimati"))
        self.assertEqual(past.status_code, 400)
        self.assertIn(lifecycle.DEADLINE_ERROR_MESSAGES_BG["deadline_in_past"], past.get_data(as_text=True))
        self.assertEqual(dict(self.row(assignment_id)), before)
        # The 180-day boundary follows the new zone's local date as well: in Kiritimati
        # (UTC+14) it is still 2 March, so 29 August is the last acceptable day there too.
        self.assertEqual(self.edit(self.owner, assignment_id,
                                   **self.deadline_form("2026-08-30", "00:00", "Pacific/Kiritimati")).status_code, 400)
        self.assertEqual(self.edit(self.owner, assignment_id,
                                   **self.deadline_form("2026-08-29", "23:59", "Pacific/Kiritimati")).status_code, 302)
        self.assertEqual(self.row(assignment_id)["deadline_at"], "2026-08-29 10:00:00")

    def test_the_edit_form_shows_the_stored_deadline_in_its_zone(self):
        assignment_id = self.assignment(self.teacher, deadline_at="2026-03-20 22:31:00", zone="America/New_York")
        html = self.detail(self.owner, assignment_id)
        self.assertRegex(html, r'id="edit-deadline-choice-deadline"[^>]*checked')
        self.assertRegex(html, r'id="edit-deadline-date" name="deadline_date" value="2026-03-20"')
        self.assertRegex(html, r'id="edit-deadline-time" name="deadline_time" value="18:30"')
        self.assertRegex(html, r'id="lifecycle-deadline">\s*до 18:30 на 20\.03\.2026')
        self.assertRegex(html, r'id="lifecycle-zone">\s*America/New_York')

    def test_an_invalid_deadline_on_an_open_assignment_is_a_400_and_changes_nothing(self):
        assignment_id = self.assignment(self.teacher, deadline_at=self.T0 + timedelta(days=10))
        before = dict(self.row(assignment_id))
        for form in ({"display_timezone": "Europe/Sofia"}, self.deadline_form("2026-03-01"),
                     self.deadline_form("2026-08-30", "00:00"), self.deadline_form("2026-03-10", zone="X/Y"),
                     self.deadline_form("2026-03-29", "03:30")):
            with self.subTest(form=form):
                response = self.post(self.owner, assignment_id, "edit", {"title_bg": "Друго", **form})
                self.assertEqual(response.status_code, 400)
                self.assertEqual(dict(self.row(assignment_id)), before)

    def test_l15_the_maximum_counts_from_the_day_of_the_edit(self):
        assignment_id = self.assignment(self.teacher)
        self.assertEqual(self.edit(self.owner, assignment_id, **self.deadline_form("2026-09-28")).status_code, 400)
        self.clock.set(self.T0 + timedelta(days=30))
        self.assertEqual(self.edit(self.owner, assignment_id, **self.deadline_form("2026-09-28")).status_code, 302)
        self.assertEqual(self.row(assignment_id)["deadline_at"], "2026-09-28 21:00:00")
        self.assertEqual(self.edit(self.owner, assignment_id, **self.deadline_form("2026-09-29")).status_code, 400)

    def test_l33_l36_everything_is_frozen_after_a_manual_close(self):
        assignment_id = self.assignment(self.teacher, title="Първо", time_limit=10,
                                        deadline_at=self.T0 + timedelta(days=5))
        self.close(self.owner, assignment_id)
        before = dict(self.row(assignment_id))
        attempts = (self.no_deadline_form(), self.deadline_form("2026-03-20"), self.deadline_form("2026-08-20"),
                    {}, {"deadline_choice": "deadline", "deadline_date": "garbage"})
        for client in (self.owner, self.client(self.admin)):
            for form in attempts:
                with self.subTest(form=form):
                    response = self.post(client, assignment_id, "edit",
                                         {"title_bg": "Променено", "time_limit_minutes": "99", **form})
                    self.assertEqual(response.status_code, 409)
                    self.assertIn(teacher_module.ASSIGNMENT_CLOSED_MESSAGE, response.get_data(as_text=True))
                    self.assertEqual(dict(self.row(assignment_id)), before)
        # Closure is decided before input validation: an empty title is still a 409.
        self.assertEqual(self.edit(self.owner, assignment_id, title="").status_code, 409)

    def test_l34_l46_frozen_after_expiry_observed_or_not(self):
        deadline = self.T0 + timedelta(hours=1)
        observed = self.assignment(self.teacher, deadline_at=deadline)
        unobserved = self.assignment(self.teacher, deadline_at=deadline)
        self.clock.set(deadline)  # L46: an edit at exactly E is refused
        self.owner.get(f"/teacher/assignment/{observed}")  # a GET observes and stamps
        self.assertEqual(self.row(observed)["expiry_observed_at"], ts(deadline))
        self.assertIsNone(self.row(unobserved)["expiry_observed_at"])
        for assignment_id in (observed, unobserved):
            with self.subTest(assignment=assignment_id):
                before = dict(self.row(assignment_id))
                response = self.edit(self.owner, assignment_id, **self.deadline_form("2026-03-20"))
                self.assertEqual(response.status_code, 409)
                after = dict(self.row(assignment_id))
                self.assertEqual(after.pop("expiry_observed_at"), ts(deadline))  # the observation is stamped
                before.pop("expiry_observed_at")
                self.assertEqual(after, before)

    def test_l45_an_edit_in_the_last_second_before_the_deadline_is_accepted(self):
        deadline = self.T0 + timedelta(hours=1)
        assignment_id = self.assignment(self.teacher, deadline_at=deadline)
        self.clock.set(deadline - timedelta(seconds=1))
        self.assertEqual(self.edit(self.owner, assignment_id, **self.deadline_form("2026-03-20")).status_code, 302)
        self.assertEqual(self.row(assignment_id)["deadline_at"], "2026-03-20 22:00:00")

    def test_l35_a_closed_assignment_answers_strangers_exactly_like_an_open_one(self):
        open_id = self.assignment(self.teacher)
        closed_id = self.assignment(self.teacher, closed_at=self.T0 - timedelta(hours=1))
        for assignment_id in (open_id, closed_id):
            with self.subTest(assignment=assignment_id):
                stranger = self.edit(self.client(self.other_teacher), assignment_id)
                self.assertEqual(stranger.status_code, 404)
                self.assertNotIn(teacher_module.ASSIGNMENT_CLOSED_MESSAGE, stranger.get_data(as_text=True))
                self.assertEqual(self.edit(self.client(self.student), assignment_id).status_code, 403)
                anonymous = self.edit(self.client(), assignment_id)
                self.assertEqual((anonymous.status_code, anonymous.headers["Location"]),
                                 (302, f"/login?next=/teacher/assignment/{assignment_id}/edit"))

    def test_l47_l48_concurrent_edits_and_closes_are_ordered_by_their_commits(self):
        first = self.assignment(self.teacher)
        self.edit(self.owner, first, **self.deadline_form("2026-03-10"))
        self.edit(self.client(self.admin), first, **self.deadline_form("2026-03-12"))
        self.assertEqual(self.row(first)["deadline_at"], "2026-03-12 22:00:00")  # the later commit's value

        close_first = self.assignment(self.teacher)
        self.close(self.owner, close_first)
        before = dict(self.row(close_first))
        self.assertEqual(self.edit(self.owner, close_first, **self.deadline_form("2026-03-20")).status_code, 409)
        self.assertEqual(dict(self.row(close_first)), before)

        edit_first = self.assignment(self.teacher)
        self.assertEqual(self.edit(self.owner, edit_first, title="Преди", **self.deadline_form("2026-03-20"))
                         .status_code, 302)
        self.clock.advance(5)
        self.assertEqual(self.close(self.owner, edit_first).status_code, 302)
        row = self.row(edit_first)
        self.assertEqual((row["title_bg"], row["deadline_at"], row["closed_at"]),
                         ("Преди", "2026-03-20 22:00:00", ts(self.T0 + timedelta(seconds=5))))

    def test_l48_an_edit_that_lost_the_race_to_a_close_changes_nothing(self):
        # The close commits between the edit's request and its writer transaction.
        assignment_id = self.assignment(self.teacher)
        real_edit = lifecycle.edit_assignment

        def close_then_edit(conn, target, **kwargs):
            with closing(sqlite3.connect(str(self.db_path))) as other:
                lifecycle.close_assignment(other, target, principal_user_id=self.admin["id"])
            return real_edit(conn, target, **kwargs)

        with patch.object(lifecycle, "edit_assignment", side_effect=close_then_edit):
            response = self.edit(self.owner, assignment_id, title="Късно", **self.deadline_form("2026-08-20"))
        self.assertEqual(response.status_code, 409)
        row = self.row(assignment_id)
        self.assertEqual((row["title_bg"], row["deadline_at"], row["closed_by_user_id"]),
                         ("Тест за срокове", None, self.admin["id"]))


class DisplayTest(TeacherRouteTestCase):
    def test_l100_closure_times_of_a_no_deadline_assignment_are_shown_in_its_zone(self):
        assignment_id = self.assignment(self.teacher, zone="America/New_York")
        self.close(self.owner, assignment_id)  # 2026-03-02 09:00 UTC = 04:00 in New York (UTC-5)
        html = self.detail(self.owner, assignment_id)
        self.assertRegex(html, r'id="lifecycle-zone">\s*America/New_York')
        self.assertRegex(html, r'id="lifecycle-closed-at">\s*02\.03\.2026 04:00')
        self.assertRegex(html, r'id="lifecycle-submit-cutoff">\s*до 02\.03\.2026 04:15')
        self.assertIn('id="share-link-closed"', html)
        self.clock.advance(lifecycle.GRACE_SECONDS)
        self.assertRegex(self.detail(self.owner, assignment_id),
                         r'id="lifecycle-submit-cutoff">\s*приключи на 02\.03\.2026 04:15')

    def test_l101_an_assignment_from_before_the_migration_asks_for_a_zone(self):
        assignment_id = self.assignment(self.teacher, zone=None)
        self.assertEqual(self.lifecycle_columns(assignment_id), {c: None for c in lifecycle.LIFECYCLE_COLUMNS})
        html = self.detail(self.owner, assignment_id)
        self.assertRegex(html, r'id="lifecycle-state-label">отворен<')
        self.assertRegex(html, r'id="lifecycle-zone">\s*Europe/Sofia')
        self.assertIn("Зона по подразбиране", html)
        # Editing needs a zone: without one nothing changes; with one it is stored.
        missing = self.post(self.owner, assignment_id, "edit", {"title_bg": "Старо", "deadline_choice": "none"})
        self.assertEqual(missing.status_code, 400)
        self.assertIsNone(self.row(assignment_id)["display_timezone"])
        self.assertEqual(self.edit(self.owner, assignment_id, **self.no_deadline_form("America/New_York"))
                         .status_code, 302)
        self.assertEqual(self.row(assignment_id)["display_timezone"], "America/New_York")
        # It can still be closed and duplicated like any other.
        self.assertEqual(self.close(self.owner, assignment_id).status_code, 302)
        self.assertEqual(self.post(self.owner, assignment_id, "duplicate", self.no_deadline_form()).status_code, 302)

    def test_the_list_shows_each_assignments_state(self):
        self.assignment(self.teacher, title="Отворен със срок", deadline_at=self.T0 + timedelta(days=3))
        self.assignment(self.teacher, title="Без срок")
        self.assignment(self.teacher, title="Изтекъл", deadline_at=self.T0 - timedelta(days=3))
        self.assignment(self.teacher, title="Затворен", closed_at=self.T0 - timedelta(hours=3))
        self.assignment(self.teacher, title="Задържан", lifecycle_hold="restore_pending")
        html = self.owner.get("/teacher/assignments").get_data(as_text=True)
        compact = re.sub(r"\s+", " ", html)
        # T0 + 3 days is 11:00 local: the last usable minute shown is 10:59.
        for expected in ("Състояние: отворен · срок до 10:59 на 05.03.2026 (Europe/Sofia)",
                         "Състояние: отворен · без краен срок", "Състояние: изтекъл срок",
                         "Състояние: затворен от учителя", "Състояние: временно недостъпен"):
            self.assertIn(expected, compact)
        self.assertNotIn("/duplicate", html)  # a copy needs its own deadline choice

    def test_l126_l127_a_future_deadline_is_never_an_anomaly(self):
        open_far = self.assignment(self.teacher, deadline_at=self.T0 + timedelta(days=179))
        self.assertEqual(self.edit(self.owner, open_far, **self.deadline_form("2026-08-28")).status_code, 302)
        closed = self.assignment(self.teacher, deadline_at=self.T0 + timedelta(days=100))
        self.assertEqual(self.close(self.owner, closed).status_code, 302)
        self.clock.advance(60)
        for path in (f"/teacher/assignment/{closed}", f"/teacher/assignment/{closed}/results",
                     f"/teacher/assignment/{closed}/results.csv", "/teacher/assignments"):
            with self.subTest(path=path):
                self.assertEqual(self.owner.get(path).status_code, 200)
        self.assertEqual(self.close(self.owner, open_far).status_code, 302)
        self.assertEqual((self.clock_row()["state"], self.row(closed)["lifecycle_hold"]), ("ok", None))
        self.assertRegex(self.detail(self.owner, closed), r'id="lifecycle-state-label">затворен от учителя<')


class ResultsTest(TeacherRouteTestCase):
    def reviewed_fixture(self, **assignment_columns):
        assignment_id = self.assignment(self.teacher, **assignment_columns)
        attempt_id = self.attempt(assignment_id, self.student, started_at=self.T0 - timedelta(hours=3),
                                  submitted_at=self.T0 - timedelta(hours=2))
        answer_id = self.execute(
            "INSERT INTO quiz_text_answers (attempt_id, question_id, subquestion_number, raw_answer, "
            "normalized_answer, grading_mode, accepted_answers_json, is_correct, points_awarded, points_possible) "
            "VALUES (?, ?, 1, 'отговор', 'отговор', 'ordered', '[]', 0, 0, 2)", (attempt_id, self.question_ids[0]))
        return assignment_id, attempt_id, answer_id

    def test_l69_review_stays_allowed_after_closure(self):
        assignment_id, _attempt_id, answer_id = self.reviewed_fixture(
            closed_at=self.T0 - timedelta(hours=1), submissions_closed_at=self.T0 - timedelta(minutes=30))
        response = self.post(self.owner, assignment_id, "results", {
            "text_answer_id": answer_id, "teacher_override": "partially_correct", "points_awarded": "1",
            "teacher_note": "половината"})
        self.assertEqual(response.status_code, 302)
        answer = self.query("SELECT * FROM quiz_text_answers WHERE id = ?", (answer_id,))[0]
        self.assertEqual((answer["teacher_override"], answer["teacher_override_status"], answer["points_awarded"],
                          answer["teacher_note"]), (1, "partially_correct", 1.0, "половината"))

    def test_review_is_not_a_lifecycle_write_and_works_with_a_suspect_clock(self):
        assignment_id, _attempt_id, answer_id = self.reviewed_fixture()
        self.execute("UPDATE lifecycle_clock SET state = 'suspect', state_reason = 'clock_step'")
        response = self.post(self.owner, assignment_id, "results",
                             {"text_answer_id": answer_id, "teacher_override": "correct"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.query("SELECT teacher_override FROM quiz_text_answers")[0][0], 1)

    def test_the_results_page_and_the_csv_carry_the_lifecycle_state(self):
        deadline = self.T0 + timedelta(days=5)
        assignment_id, attempt_id, _answer_id = self.reviewed_fixture(deadline_at=deadline)
        self.close(self.owner, assignment_id)
        html = self.owner.get(f"/teacher/assignment/{assignment_id}/results").get_data(as_text=True)
        self.assertRegex(re.sub(r"\s+", " ", html),
                         r'id="results-lifecycle-state"> Състояние: <strong>затворен от учителя</strong>')

        export = self.owner.get(f"/teacher/assignment/{assignment_id}/results.csv")
        rows = list(csv.reader(io.StringIO(export.get_data(as_text=True))))
        self.assertEqual(rows[0], teacher_module.QUIZ_RESULTS_EXPORT_COLUMNS)
        # Appended, never inserted: the first 27 columns are where they always were.
        self.assertEqual(rows[0][-3:], ["assignment_deadline_at", "assignment_closed_at",
                                        "assignment_lifecycle_state"])
        self.assertEqual((rows[0][0], rows[0][26], len(rows[0])), ("row_type", "is_correct", 30))
        for row in rows[1:]:
            self.assertEqual(row[-3:], [ts(deadline), ts(self.T0), "closed_manually"])
            self.assertEqual(len(row), 30)
        self.assertEqual([row[0] for row in rows[1:]], ["attempt", "open_answer"])
        self.assertEqual(rows[1][3], str(attempt_id))

    def test_l92_results_csv_and_the_student_result_read_inside_one_transaction(self):
        assignment_id, attempt_id, _answer_id = self.reviewed_fixture()

        def traced(module, request):
            statements = []
            real = web_app.quiz_db

            def connect():
                conn = real()
                conn.set_trace_callback(statements.append)
                return conn

            with patch.object(module, "quiz_db", side_effect=connect):
                response = request()
            self.assertEqual(response.status_code, 200)
            return [" ".join(s.split()).upper() for s in statements]

        requests = {
            "teacher results": (quiz_service, lambda: self.owner.get(f"/teacher/assignment/{assignment_id}/results")),
            "csv": (quiz_service, lambda: self.owner.get(f"/teacher/assignment/{assignment_id}/results.csv")),
            "filtered csv": (quiz_service,
                             lambda: self.owner.get(f"/teacher/assignment/{assignment_id}/results.csv?filtered=1")),
            "student result": (quiz_service,
                               lambda: self.client(self.student).get(f"/quiz/attempt/{attempt_id}/result")),
        }
        for label, (module, request) in requests.items():
            with self.subTest(view=label):
                statements = traced(module, request)
                inside, selects, transactions = False, 0, 0
                for statement in statements:
                    if statement.startswith("BEGIN"):
                        inside, transactions = True, transactions + 1
                    elif statement.startswith(("ROLLBACK", "COMMIT")):
                        inside = False
                    elif statement.startswith("SELECT"):
                        selects += 1
                        self.assertTrue(inside, f"read outside the transaction: {statement}")
                self.assertGreater(selects, 3)
                self.assertEqual(transactions, 1)  # one snapshot for authorization and data


class HoldTest(TeacherRouteTestCase):
    def requests(self, assignment_id, answer_id):
        base = f"/teacher/assignment/{assignment_id}"
        form = {"title_bg": "Променено", **self.no_deadline_form()}
        return {
            "detail": lambda c: c.get(base),
            "qr": lambda c: c.get(f"{base}/qr.svg"),
            "results": lambda c: c.get(f"{base}/results"),
            "csv": lambda c: c.get(f"{base}/results.csv"),
            "edit": lambda c: c.post(f"{base}/edit", data=form, headers=SAME_ORIGIN),
            "close": lambda c: c.post(f"{base}/close", headers=SAME_ORIGIN),
            "duplicate": lambda c: c.post(f"{base}/duplicate", data=self.no_deadline_form(), headers=SAME_ORIGIN),
            "review": lambda c: c.post(f"{base}/results", data={"text_answer_id": answer_id,
                                                                "teacher_override": "correct"}, headers=SAME_ORIGIN),
        }

    def test_l116_a_held_assignment_is_not_served_and_a_stranger_learns_nothing(self):
        for hold in ("clock_review", "clock_review,restore_pending", "something unreadable"):
            with self.subTest(hold=hold):
                assignment_id = self.assignment(self.teacher, lifecycle_hold=hold)
                attempt_id = self.attempt(assignment_id, self.student, started_at=self.T0 - timedelta(hours=2),
                                          submitted_at=self.T0 - timedelta(hours=1))
                answer_id = self.execute(
                    "INSERT INTO quiz_text_answers (attempt_id, question_id, subquestion_number, raw_answer, "
                    "normalized_answer, grading_mode, accepted_answers_json, is_correct, points_awarded, "
                    "points_possible) VALUES (?, ?, 1, 'а', 'а', 'ordered', '[]', 0, 0, 1)",
                    (attempt_id, self.question_ids[0]))
                before = (dict(self.row(assignment_id)), self.table_counts(),
                          tuple(self.query("SELECT * FROM quiz_text_answers")[-1]))
                for label, request in self.requests(assignment_id, answer_id).items():
                    with self.subTest(route=label):
                        for client in (self.owner, self.client(self.admin)):
                            response = request(client)
                            self.assertEqual(response.status_code, 503, label)
                            self.assertNotIn("Променено", response.get_data(as_text=True))
                        self.assertEqual(request(self.client(self.other_teacher)).status_code, 404)
                self.assertEqual((dict(self.row(assignment_id)), self.table_counts(),
                                  tuple(self.query("SELECT * FROM quiz_text_answers")[-1])), before)

    def test_the_unavailable_page_is_bulgarian_and_names_no_reason(self):
        assignment_id = self.assignment(self.teacher, lifecycle_hold="restore_unreconciled")
        response = self.owner.get(f"/teacher/assignment/{assignment_id}")
        html = response.get_data(as_text=True)
        self.assertEqual((response.status_code, response.headers["Cache-Control"]), (503, "no-store"))
        self.assertIn("временно недостъпна", html)
        for leak in ("restore_unreconciled", "restore_pending", "clock_review", "lifecycle_hold",
                     "assignment_held", "Тест за срокове"):
            self.assertNotIn(leak, html)

    def test_l117_no_request_can_set_a_hold_or_a_retention_floor(self):
        assignment_id = self.assignment(self.teacher)
        forged = {"lifecycle_hold": "clock_review", "retention_base_at": "2030-01-01 00:00:00"}
        self.post(self.owner, assignment_id, "edit", {"title_bg": "А", **self.no_deadline_form(), **forged})
        copy = self.created_id(self.post(self.owner, assignment_id, "duplicate",
                                         {**self.no_deadline_form(), **forged}))
        created = self.created_id(self.create(self.owner, **self.no_deadline_form(), **forged))
        self.post(self.owner, assignment_id, "close", forged)
        for target in (assignment_id, copy, created):
            row = self.row(target)
            self.assertEqual((row["lifecycle_hold"], row["retention_base_at"]), (None, None))


if __name__ == "__main__":
    unittest.main()
