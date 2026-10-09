"""Assignment lifecycle: timestamps, timezones, deadlines and the state function.

Pure tests of ``web.services.lifecycle``: no application, no database, no clock.
Case ids (``L3``) refer to docs/ASSIGNMENT_LIFECYCLE_DESIGN.md, section 15.1.
Timezone cases use Europe/Sofia (DST on the last Sundays of March and October)
and America/New_York, with the process timezone set to a third zone so that any
use of the server's zone would change a result.
"""

import os
import time as time_module
import unittest
from datetime import date, datetime, time, timedelta

from web.services import lifecycle

SOFIA = "Europe/Sofia"
NEW_YORK = "America/New_York"
ts = lifecycle.format_timestamp
G = timedelta(seconds=lifecycle.GRACE_SECONDS)
R = timedelta(seconds=lifecycle.RETENTION_SECONDS)


def row(**columns):
    """An assignment row as the state function reads it; everything NULL by default."""
    base = {"id": 1, **{column: None for column in lifecycle.LIFECYCLE_COLUMNS}}
    for name, value in columns.items():
        base[name] = ts(value) if isinstance(value, datetime) else value
    return base


class ThirdZoneProcessMixin:
    """Run with the process timezone set to a zone no test selects (L6)."""

    PROCESS_ZONE = "Pacific/Auckland"

    def setUp(self):
        super().setUp()
        previous = os.environ.get("TZ")
        os.environ["TZ"] = self.PROCESS_ZONE
        time_module.tzset()

        def restore():
            if previous is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = previous
            time_module.tzset()

        self.addCleanup(restore)


class TimestampTest(unittest.TestCase):
    def test_serialiser_writes_exactly_one_format(self):
        self.assertEqual(ts(datetime(2026, 4, 3, 21, 0, 0)), "2026-04-03 21:00:00")
        self.assertEqual(ts(datetime(2026, 1, 2, 3, 4, 5, 999999)), "2026-01-02 03:04:05")  # whole seconds
        self.assertEqual(ts(datetime(987, 1, 2, 3, 4, 5)), "0987-01-02 03:04:05")           # fixed width

    def test_parser_round_trips_and_is_strict(self):
        self.assertEqual(lifecycle.parse_timestamp("2026-04-03 21:00:00"), datetime(2026, 4, 3, 21, 0, 0))
        for text in ("2026-04-03T21:00:00", "2026-04-03 21:00:00Z", "2026-04-03 21:00:00+00:00",
                     "2026-04-03 21:00:00.5", "2026-04-03 21:00", "2026-4-3 21:00:00", "2026-04-03  21:00:00",
                     "2026-02-30 10:00:00", "2026-04-03 24:00:00", "2026-13-01 00:00:00", "9999-99-99 99:99:99",
                     "2026-04-03 21:00:00\n", " 2026-04-03 21:00:00", "２０２６-04-03 21:00:00", "", None, 5,
                     datetime(2026, 4, 3)):
            with self.subTest(text=text):
                with self.assertRaises(lifecycle.TimestampError):
                    lifecycle.parse_timestamp(text)

    def test_stored_strings_order_like_the_instants(self):
        instants = [datetime(2026, 1, 1) + timedelta(hours=37 * n) for n in range(40)]
        self.assertEqual(sorted(ts(value) for value in instants), [ts(value) for value in sorted(instants)])


class DefaultDeadlineTest(ThirdZoneProcessMixin, unittest.TestCase):
    def test_l3_default_across_dst_uses_the_offset_of_the_target_date(self):
        # Created 2026-03-20 (UTC+2); DST starts on 2026-03-29, so the cutoff is at UTC+3.
        shown = lifecycle.default_displayed_deadline(datetime(2026, 3, 20, 10, 0, 0), SOFIA)
        self.assertEqual(shown, (date(2026, 4, 3), time(23, 59)))
        self.assertEqual(ts(lifecycle.deadline_cutoff(*shown, SOFIA, typed=False)), "2026-04-03 21:00:00")
        # Created 2026-10-15 (UTC+3); DST ends on 2026-10-25, so the cutoff is at UTC+2.
        shown = lifecycle.default_displayed_deadline(datetime(2026, 10, 15, 10, 0, 0), SOFIA)
        self.assertEqual(shown, (date(2026, 10, 29), time(23, 59)))
        self.assertEqual(ts(lifecycle.deadline_cutoff(*shown, SOFIA, typed=False)), "2026-10-29 22:00:00")

    def test_l3_the_default_is_calendar_days_not_336_hours(self):
        now = datetime(2026, 3, 20, 10, 0, 0)
        cutoff = lifecycle.deadline_cutoff(*lifecycle.default_displayed_deadline(now, SOFIA), SOFIA, typed=False)
        self.assertNotEqual(cutoff - now, timedelta(hours=336))
        self.assertNotEqual((cutoff - now).total_seconds() % 3600, 1)

    def test_l4_the_default_does_not_depend_on_the_time_of_day(self):
        # 00:05 and 23:55 local time on 2026-05-10 in Sofia (UTC+3).
        early, late = datetime(2026, 5, 9, 21, 5, 0), datetime(2026, 5, 10, 20, 55, 0)
        self.assertEqual(lifecycle.local_today(early, SOFIA), date(2026, 5, 10))
        self.assertEqual(lifecycle.local_today(late, SOFIA), date(2026, 5, 10))
        self.assertEqual(lifecycle.default_displayed_deadline(early, SOFIA),
                         lifecycle.default_displayed_deadline(late, SOFIA))
        self.assertEqual(lifecycle.default_displayed_deadline(early, SOFIA), (date(2026, 5, 24), time(23, 59)))

    def test_l5_the_same_local_deadline_in_two_zones_is_two_utc_instants(self):
        sofia = lifecycle.deadline_cutoff(date(2026, 6, 10), time(23, 59), SOFIA, typed=True)
        new_york = lifecycle.deadline_cutoff(date(2026, 6, 10), time(23, 59), NEW_YORK, typed=True)
        self.assertEqual(ts(sofia), "2026-06-10 21:00:00")      # UTC+3
        self.assertEqual(ts(new_york), "2026-06-11 04:00:00")   # UTC-4
        self.assertNotEqual(sofia, new_york)

    def test_l6_the_process_timezone_changes_nothing(self):
        self.assertEqual(time_module.tzname[0][:2], "NZ")
        now = datetime(2026, 3, 20, 10, 0, 0)
        self.assertEqual(lifecycle.local_today(now, SOFIA), date(2026, 3, 20))
        self.assertEqual(lifecycle.local_today(datetime(2026, 3, 20, 23, 30, 0), SOFIA), date(2026, 3, 21))
        self.assertEqual(lifecycle.local_today(datetime(2026, 3, 20, 3, 30, 0), NEW_YORK), date(2026, 3, 19))
        self.assertEqual(ts(lifecycle.deadline_cutoff(date(2026, 4, 3), time(23, 59), SOFIA, typed=True)),
                         "2026-04-03 21:00:00")
        self.assertEqual(lifecycle.displayed_deadline(datetime(2026, 4, 3, 21, 0, 0), SOFIA),
                         datetime(2026, 4, 3, 23, 59))

    def test_the_cutoff_is_one_minute_after_any_displayed_time(self):
        cutoff = lifecycle.deadline_cutoff(date(2026, 6, 10), time(14, 30), SOFIA, typed=True)
        self.assertEqual(ts(cutoff), "2026-06-10 11:31:00")
        self.assertEqual(lifecycle.displayed_deadline(cutoff, SOFIA), datetime(2026, 6, 10, 14, 30))

    def test_the_cutoff_is_the_next_local_midnight_where_the_clock_is_set_back_at_midnight(self):
        # Codex finding 5. Africa/Cairo leaves DST at the end of Thursday 2026-10-29: local
        # 23:00-23:59 occurs twice, first at UTC+3 and then at UTC+2. "Through 23:59" on that day
        # ends at local midnight starting 2026-10-30, which is 22:00 UTC. A minute after the first
        # 23:59 would be 21:00 UTC: an hour of the displayed day cut off.
        cairo = "Africa/Cairo"
        self.assertEqual(lifecycle._local_instants(datetime(2026, 10, 29, 23, 59), cairo),
                         [datetime(2026, 10, 29, 20, 59), datetime(2026, 10, 29, 21, 59)])
        self.assertEqual(lifecycle._local_instants(datetime(2026, 10, 30, 0, 0), cairo),
                         [datetime(2026, 10, 29, 22, 0)])

        shown = lifecycle.default_displayed_deadline(datetime(2026, 10, 15, 10, 0, 0), cairo)
        self.assertEqual(shown, (date(2026, 10, 29), time(23, 59)))
        for typed in (False, True):
            cutoff = lifecycle.deadline_cutoff(*shown, cairo, typed=typed)
            self.assertEqual(ts(cutoff), "2026-10-29 22:00:00")
            self.assertNotEqual(ts(cutoff), "2026-10-29 21:00:00")
            # Shown again, it is the same day and minute; and the whole of both 23:xx hours is usable.
            self.assertEqual(lifecycle.displayed_deadline(cutoff, cairo), datetime(2026, 10, 29, 23, 59))
            for usable in (datetime(2026, 10, 29, 20, 59, 59), datetime(2026, 10, 29, 21, 30), cutoff - timedelta(seconds=1)):
                self.assertFalse(lifecycle.evaluate(row(deadline_at=cutoff), usable).is_closed)
            self.assertTrue(lifecycle.evaluate(row(deadline_at=cutoff), cutoff).is_closed)
        # The form round trip is stable: resolving the displayed value again gives the stored cutoff.
        request = lifecycle.parse_deadline_request({"deadline_choice": "deadline", "deadline_date": "2026-10-29",
                                                    "deadline_time": "23:59", "display_timezone": cairo})
        self.assertEqual(lifecycle.resolve_deadline(request, datetime(2026, 10, 15, 10, 0, 0)), "2026-10-29 22:00:00")
        # The 180-day boundary is still decided by the displayed local date.
        self.assertEqual(lifecycle.resolve_deadline(request, datetime(2026, 5, 2, 10, 0, 0)), "2026-10-29 22:00:00")
        with self.assertRaises(lifecycle.DeadlineError) as caught:
            lifecycle.resolve_deadline(request, datetime(2026, 5, 1, 10, 0, 0))  # 181 days before
        self.assertEqual(caught.exception.code, "deadline_too_far")

    def test_the_cutoff_is_constructed_in_local_time_and_only_then_converted(self):
        # An overlap is the earlier instant at the cutoff itself, not at the displayed minute.
        # Sofia, 2026-10-25: 03:00-03:59 occurs twice (UTC+3, then UTC+2).
        cases = {
            (date(2026, 10, 25), time(2, 59)): "2026-10-25 00:00:00",   # cutoff 03:00, first occurrence
            (date(2026, 10, 25), time(3, 30)): "2026-10-25 00:31:00",   # cutoff 03:31, first occurrence
            (date(2026, 10, 25), time(3, 59)): "2026-10-25 02:00:00",   # cutoff 04:00, which occurs once
            (date(2026, 10, 24), time(23, 59)): "2026-10-24 21:00:00",  # the ordinary midnight before
            (date(2026, 10, 25), time(23, 59)): "2026-10-25 22:00:00",  # and the first one after, at UTC+2
        }
        for (day, clock), expected in cases.items():
            with self.subTest(day=day, clock=clock):
                cutoff = lifecycle.deadline_cutoff(day, clock, SOFIA, typed=True)
                self.assertEqual(ts(cutoff), expected)
                self.assertEqual(lifecycle.displayed_deadline(cutoff, SOFIA), datetime.combine(day, clock))
        # A gap at the cutoff moves it forward; the displayed minute before the gap is valid.
        self.assertEqual(ts(lifecycle.deadline_cutoff(date(2026, 3, 29), time(2, 59), SOFIA, typed=True)),
                         "2026-03-29 01:00:00")
        # Every day of a year in Sofia, Cairo and New York: the cutoff of "through 23:59" is the
        # first instant of the next local date, and is shown again as 23:59 on the same day.
        gaps_at_midnight = []
        for zone in (SOFIA, "Africa/Cairo", NEW_YORK):
            day = date(2026, 1, 1)
            while day < date(2027, 1, 1):
                cutoff = lifecycle.deadline_cutoff(day, time(23, 59), zone, typed=True)
                first = lifecycle._to_local(cutoff, zone)
                self.assertEqual(first.date(), day + timedelta(days=1), (zone, day))
                self.assertEqual(lifecycle._to_local(cutoff - timedelta(seconds=1), zone).date(), day, (zone, day))
                self.assertEqual(lifecycle.displayed_deadline(cutoff, zone), datetime.combine(day, time(23, 59)))
                if first.time() != time(0, 0):
                    gaps_at_midnight.append((zone, day, first.time()))
                day += timedelta(days=1)
        # The one day local midnight does not exist: Cairo enters DST at 00:00 on 2026-04-24, so
        # the day before ends at the first valid instant, 01:00.
        self.assertEqual(gaps_at_midnight, [("Africa/Cairo", date(2026, 4, 23), time(1, 0))])

    def test_a_computed_cutoff_in_a_midnight_gap_moves_to_the_first_valid_instant(self):
        # America/Havana starts DST at 00:00 on 2026-03-08: local midnight does not exist.
        self.assertEqual(lifecycle._local_instants(datetime(2026, 3, 8, 0, 0), "America/Havana"), [])
        cutoff = lifecycle.deadline_cutoff(date(2026, 3, 7), time(23, 59), "America/Havana", typed=False)
        self.assertEqual(ts(cutoff), "2026-03-08 05:00:00")  # 01:00 local, the first instant of the day
        # A computed displayed minute that itself falls in a gap also moves forward.
        self.assertEqual(ts(lifecycle.deadline_cutoff(date(2026, 3, 8), time(0, 30), "America/Havana", typed=False)),
                         "2026-03-08 05:00:00")


class DeadlineInputTest(ThirdZoneProcessMixin, unittest.TestCase):
    NOW = datetime(2026, 3, 2, 9, 0, 0)  # 2026-03-02 11:00 in Sofia

    def request(self, **overrides):
        form = {"deadline_choice": "deadline", "deadline_date": "2026-03-16", "deadline_time": "23:59",
                "display_timezone": SOFIA, **overrides}
        return lifecycle.parse_deadline_request({k: v for k, v in form.items() if v is not None})

    def assert_refused(self, code, **overrides):
        with self.assertRaises(lifecycle.DeadlineError) as caught:
            lifecycle.resolve_deadline(self.request(**overrides), self.NOW)
        self.assertEqual(caught.exception.code, code)

    def test_a_valid_request_resolves_to_the_stored_cutoff(self):
        self.assertEqual(lifecycle.resolve_deadline(self.request(), self.NOW), "2026-03-16 22:00:00")

    def test_l7_an_unknown_or_missing_zone_is_refused(self):
        for zone in ("Europe/Atlantis", "", None, "europe/sofia", "UTC+2", "localtime", "../etc/passwd"):
            with self.subTest(zone=zone):
                with self.assertRaises(lifecycle.DeadlineError) as caught:
                    self.request(display_timezone=zone)
                self.assertEqual(caught.exception.code, "timezone_invalid")
        # The zone is required for "no deadline" too.
        with self.assertRaises(lifecycle.DeadlineError):
            lifecycle.parse_deadline_request({"deadline_choice": "none"})

    def test_l8_the_choice_must_be_stated(self):
        for choice in (None, "", "maybe", "DEADLINE", "0"):
            with self.subTest(choice=choice):
                with self.assertRaises(lifecycle.DeadlineError) as caught:
                    self.request(deadline_choice=choice)
                self.assertEqual(caught.exception.code, "deadline_choice_required")

    def test_l9_l16_no_deadline_is_always_acceptable_and_keeps_the_zone(self):
        request = lifecycle.parse_deadline_request({"deadline_choice": "none", "display_timezone": NEW_YORK})
        self.assertEqual(request, (NEW_YORK, False, None, None))
        self.assertIsNone(lifecycle.resolve_deadline(request, self.NOW))
        # Date and time fields are not read at all for "no deadline".
        request = lifecycle.parse_deadline_request({"deadline_choice": "none", "display_timezone": SOFIA,
                                                    "deadline_date": "garbage", "deadline_time": "garbage"})
        self.assertIsNone(lifecycle.resolve_deadline(request, self.NOW))

    def test_l11_a_deadline_at_or_before_now_is_refused(self):
        # 11:00 local now: displayed 10:59 gives a cutoff of exactly now.
        self.assert_refused("deadline_in_past", deadline_date="2026-03-02", deadline_time="10:59")
        self.assert_refused("deadline_in_past", deadline_date="2026-03-01")
        self.assert_refused("deadline_in_past", deadline_date="2020-01-01")
        # One minute later is strictly in the future.
        self.assertEqual(
            lifecycle.resolve_deadline(self.request(deadline_date="2026-03-02", deadline_time="11:00"), self.NOW),
            "2026-03-02 09:01:00")

    def test_l12_the_180th_day_is_accepted_and_stored_as_the_next_local_midnight(self):
        boundary = date(2026, 3, 2) + timedelta(days=180)
        self.assertEqual(boundary, date(2026, 8, 29))
        self.assertEqual(lifecycle.resolve_deadline(self.request(deadline_date="2026-08-29"), self.NOW),
                         "2026-08-29 21:00:00")  # 2026-08-30 00:00 at UTC+3

    def test_l13_the_181st_day_and_beyond_are_refused(self):
        self.assert_refused("deadline_too_far", deadline_date="2026-08-30", deadline_time="00:00")
        self.assert_refused("deadline_too_far", deadline_date="2027-03-02")
        self.assert_refused("deadline_too_far", deadline_date="9999-12-31")

    def test_l14_the_maximum_is_decided_by_local_dates_across_dst_changes(self):
        # One DST change between now and the boundary (above), and two here:
        # from 2026-10-01 the range crosses the autumn change and the next spring change.
        now = datetime(2026, 10, 1, 9, 0, 0)
        boundary = date(2026, 10, 1) + timedelta(days=180)
        self.assertEqual(boundary, date(2027, 3, 30))
        ok = lifecycle.parse_deadline_request({"deadline_choice": "deadline", "deadline_date": "2027-03-30",
                                               "deadline_time": "23:59", "display_timezone": SOFIA})
        self.assertEqual(lifecycle.resolve_deadline(ok, now), "2027-03-30 21:00:00")
        late = ok._replace(displayed_date=date(2027, 3, 31), displayed_time=time(0, 0))
        with self.assertRaises(lifecycle.DeadlineError) as caught:
            lifecycle.resolve_deadline(late, now)
        self.assertEqual(caught.exception.code, "deadline_too_far")
        # Near local midnight the boundary follows the local date, not the UTC date:
        # 2026-10-01 21:30 UTC is already 2026-10-02 in Sofia.
        self.assertEqual(lifecycle.resolve_deadline(late, datetime(2026, 10, 1, 21, 30, 0)), "2027-03-30 21:01:00")

    def test_l15_the_maximum_counts_from_the_day_the_deadline_is_set(self):
        thirty_days_later = self.NOW + timedelta(days=30)
        # Refused when the assignment was created, accepted when edited 30 days later.
        self.assert_refused("deadline_too_far", deadline_date="2026-09-28")
        self.assertEqual(lifecycle.resolve_deadline(self.request(deadline_date="2026-09-28"), thirty_days_later),
                         "2026-09-28 21:00:00")
        with self.assertRaises(lifecycle.DeadlineError):
            lifecycle.resolve_deadline(self.request(deadline_date="2026-09-29"), thirty_days_later)

    def test_l17_a_typed_time_in_the_spring_gap_is_refused(self):
        # 2026-03-29 03:00..03:59 does not exist in Sofia.
        self.assert_refused("deadline_nonexistent", deadline_date="2026-03-29", deadline_time="03:30")
        self.assert_refused("deadline_nonexistent", deadline_date="2026-03-29", deadline_time="03:00")
        self.assertEqual(
            lifecycle.resolve_deadline(self.request(deadline_date="2026-03-29", deadline_time="02:59"), self.NOW),
            "2026-03-29 01:00:00")  # the minute before the gap; its cutoff is the instant the gap ends

    def test_l18_a_typed_time_in_the_autumn_overlap_is_the_earlier_instant(self):
        now = datetime(2026, 9, 1, 9, 0, 0)
        request = self.request(deadline_date="2026-10-25", deadline_time="03:30")
        # 03:30 occurs at 00:30 UTC (UTC+3) and again at 01:30 UTC (UTC+2).
        self.assertEqual(lifecycle.resolve_deadline(request, now), "2026-10-25 00:31:00")

    def test_l19_date_and_time_parse_strictly(self):
        for fields in ({"deadline_date": "2026-03-16T23:59"}, {"deadline_date": "2026-03-16 23:59"},
                       {"deadline_date": "2026-02-30"}, {"deadline_date": "2026-3-16"},
                       {"deadline_date": "16.03.2026"}, {"deadline_date": ""}, {"deadline_date": None},
                       {"deadline_time": "23:59:00"}, {"deadline_time": "23:59+02:00"}, {"deadline_time": "23:59Z"},
                       {"deadline_time": "23:59.5"}, {"deadline_time": "24:00"}, {"deadline_time": "9:05"},
                       {"deadline_time": ""}, {"deadline_time": None}, {"deadline_date": "2026-03-16\n"}):
            with self.subTest(fields=fields):
                with self.assertRaises(lifecycle.DeadlineError) as caught:
                    self.request(**fields)
                self.assertEqual(caught.exception.code, "deadline_invalid")

    def test_every_refusal_has_a_bulgarian_message(self):
        for code in ("deadline_choice_required", "timezone_invalid", "deadline_invalid", "deadline_nonexistent",
                     "deadline_in_past", "deadline_too_far"):
            self.assertRegex(lifecycle.DEADLINE_ERROR_MESSAGES_BG[code], "[а-я]")
        self.assertIn(str(lifecycle.MAX_DEADLINE_DAYS), lifecycle.DEADLINE_ERROR_MESSAGES_BG["deadline_too_far"])

    def test_the_selectable_zones_exclude_the_servers_own(self):
        names = lifecycle.timezone_names()
        self.assertIn(SOFIA, names)
        self.assertEqual(list(names), sorted(names))
        self.assertFalse({"localtime", "posixrules", "Factory"} & set(names))


class StateTest(unittest.TestCase):
    D = datetime(2026, 4, 3, 21, 0, 0)  # a stored deadline (exclusive cutoff)

    def test_no_deadline_is_open_forever(self):
        for now in (datetime(2026, 1, 1), datetime(2027, 6, 1), datetime(2099, 1, 1)):
            view = lifecycle.evaluate(row(), now)
            self.assertEqual((view.state, view.is_closed), (lifecycle.STATE_OPEN, False))
            self.assertIsNone(view.effective_closure)
            self.assertIsNone(view.submit_cutoff)
            self.assertIsNone(view.retention_eligible_at)  # never eligible for cleanup

    def test_l99_the_cutoff_is_exclusive(self):
        before = lifecycle.evaluate(row(deadline_at=self.D), self.D - timedelta(seconds=1))
        at = lifecycle.evaluate(row(deadline_at=self.D), self.D)
        self.assertEqual((before.state, before.is_closed), (lifecycle.STATE_OPEN, False))
        self.assertEqual((at.state, at.is_closed), (lifecycle.STATE_EXPIRED, True))
        self.assertEqual(at.effective_closure, self.D)

    def test_grace_is_a_half_open_window_from_the_closure(self):
        assignment = row(deadline_at=self.D)
        self.assertTrue(lifecycle.evaluate(assignment, self.D).in_grace)
        self.assertTrue(lifecycle.evaluate(assignment, self.D + G - timedelta(seconds=1)).in_grace)
        at_cutoff = lifecycle.evaluate(assignment, self.D + G)
        self.assertFalse(at_cutoff.in_grace)
        self.assertTrue(at_cutoff.submissions_ended)
        self.assertEqual(at_cutoff.submit_cutoff, self.D + G)

    def test_l30_a_manual_close_before_the_deadline_is_the_effective_closure(self):
        closed = self.D - timedelta(days=3)
        view = lifecycle.evaluate(row(deadline_at=self.D, closed_at=closed), closed + timedelta(minutes=1))
        self.assertEqual(view.state, lifecycle.STATE_CLOSED_MANUALLY)
        self.assertEqual(view.effective_closure, closed)
        self.assertEqual(view.submit_cutoff, closed + G)
        self.assertEqual(view.deadline_at, self.D)  # stays stored, still in the future

    def test_l40_l54_markers_and_submissions_never_move_the_retention_clock(self):
        late = self.D + timedelta(days=30)
        observed = lifecycle.evaluate(row(deadline_at=self.D, expiry_observed_at=late,
                                          submissions_closed_at=late), late)
        self.assertEqual(observed.effective_closure, self.D)
        self.assertEqual(observed.retention_eligible_at, self.D + R)
        # Retention is counted from E, never from E + G and never from a submission.
        self.assertEqual(lifecycle.evaluate(row(deadline_at=self.D), self.D + timedelta(minutes=14))
                         .retention_eligible_at, self.D + R)

    def test_a_retention_floor_only_delays(self):
        floor = self.D + timedelta(days=10)
        view = lifecycle.evaluate(row(deadline_at=self.D, retention_base_at=floor), floor)
        self.assertEqual(view.effective_closure, self.D)  # the floor never changes E
        self.assertEqual(view.retention_eligible_at, floor + R)
        earlier = lifecycle.evaluate(row(deadline_at=self.D, retention_base_at=self.D - timedelta(days=10)), floor)
        self.assertEqual(earlier.retention_eligible_at, self.D + R)

    def test_terminal_markers_close_whatever_the_clock_reads(self):
        long_before = self.D - timedelta(days=100)
        for marker in ("closed_at", "expiry_observed_at", "submissions_closed_at"):
            with self.subTest(marker=marker):
                view = lifecycle.evaluate(row(deadline_at=self.D, **{marker: self.D}), long_before)
                self.assertTrue(view.is_closed)
        ended = lifecycle.evaluate(row(deadline_at=self.D, expiry_observed_at=self.D,
                                       submissions_closed_at=self.D + G), self.D + timedelta(seconds=1))
        self.assertFalse(ended.in_grace)        # decided by the marker, not by comparing times
        self.assertTrue(ended.submissions_ended)

    def test_l80_a_malformed_timestamp_fails_safe_as_closed(self):
        for column, value in (("deadline_at", "9999-99-99 99:99:99"), ("deadline_at", "2026-04-03T21:00:00"),
                              ("deadline_at", "soon"), ("closed_at", "2026-02-30 10:00:00"),
                              ("retention_base_at", "x"), ("expiry_observed_at", "2026-04-03 21:00")):
            with self.subTest(column=column, value=value):
                view = lifecycle.evaluate(row(**{column: value}), datetime(2026, 1, 1))
                self.assertEqual(view.malformed, (column,))
                self.assertTrue(view.is_closed)
                self.assertFalse(view.in_grace)
                self.assertTrue(view.submissions_ended)
                self.assertIsNone(view.retention_eligible_at)  # never eligible for cleanup
                self.assertFalse(lifecycle.needs_observation(view))

    def test_l52_in_flight_is_strict_for_a_deadline_and_inclusive_for_a_manual_close(self):
        expired = lifecycle.evaluate(row(deadline_at=self.D), self.D + timedelta(minutes=1))
        self.assertTrue(lifecycle.attempt_in_flight(expired, ts(self.D - timedelta(seconds=1))))
        self.assertFalse(lifecycle.attempt_in_flight(expired, ts(self.D)))
        self.assertFalse(lifecycle.attempt_in_flight(expired, ts(self.D + timedelta(seconds=1))))

        closed_at = self.D - timedelta(days=1)
        closed = lifecycle.evaluate(row(deadline_at=self.D, closed_at=closed_at), closed_at + timedelta(minutes=1))
        self.assertTrue(lifecycle.attempt_in_flight(closed, ts(closed_at)))  # an equal second: started first
        self.assertFalse(lifecycle.attempt_in_flight(closed, ts(closed_at + timedelta(seconds=1))))
        # Closed manually without any deadline.
        manual = lifecycle.evaluate(row(closed_at=closed_at), closed_at + timedelta(minutes=1))
        self.assertTrue(lifecycle.attempt_in_flight(manual, ts(closed_at)))
        # Both set and equal (the close predicate rules it out): the stricter comparison.
        both = lifecycle.evaluate(row(deadline_at=self.D, closed_at=self.D), self.D + timedelta(minutes=1))
        self.assertFalse(lifecycle.attempt_in_flight(both, ts(self.D)))

    def test_l81_a_malformed_start_is_never_in_flight_or_submittable(self):
        expired = lifecycle.evaluate(row(deadline_at=self.D), self.D + timedelta(minutes=1))
        for started in ("", None, "2026-04-03T20:00:00", "2026-02-30 10:00:00", "yesterday"):
            with self.subTest(started=started):
                self.assertFalse(lifecycle.attempt_in_flight(expired, started))
                self.assertFalse(lifecycle.attempt_submittable(
                    expired, {"started_at": started, "submitted_at": None}))

    def test_submittable_follows_open_grace_and_in_flight(self):
        started = ts(self.D - timedelta(hours=1))
        attempt = {"started_at": started, "submitted_at": None}
        cases = (
            (row(deadline_at=self.D), self.D - timedelta(seconds=1), True),            # open
            (row(deadline_at=self.D), self.D, True),                                   # first second of grace
            (row(deadline_at=self.D), self.D + G - timedelta(seconds=1), True),        # last second of grace
            (row(deadline_at=self.D), self.D + G, False),                              # the cutoff is exclusive
            (row(deadline_at=self.D, submissions_closed_at=self.D + G), self.D + timedelta(seconds=1), False),
            (row(), datetime(2030, 1, 1), True),                                       # no deadline, never closed
        )
        for assignment, now, expected in cases:
            with self.subTest(now=now, assignment=assignment):
                self.assertEqual(lifecycle.attempt_submittable(lifecycle.evaluate(assignment, now), attempt),
                                 expected)
        submitted = {"started_at": started, "submitted_at": ts(self.D)}
        self.assertFalse(lifecycle.attempt_submittable(lifecycle.evaluate(row(), self.D), submitted))

    def test_observation_is_needed_only_for_an_unstamped_fact(self):
        need = lifecycle.needs_observation
        self.assertFalse(need(lifecycle.evaluate(row(deadline_at=self.D), self.D - timedelta(seconds=1))))
        self.assertTrue(need(lifecycle.evaluate(row(deadline_at=self.D), self.D)))
        self.assertTrue(need(lifecycle.evaluate(row(deadline_at=self.D, expiry_observed_at=self.D), self.D + G)))
        self.assertFalse(need(lifecycle.evaluate(
            row(deadline_at=self.D, expiry_observed_at=self.D, submissions_closed_at=self.D + G), self.D + G)))
        # A manual close needs no expiry stamp; only the end of its grace window.
        closed = row(closed_at=self.D)
        self.assertFalse(need(lifecycle.evaluate(closed, self.D + timedelta(minutes=1))))
        self.assertTrue(need(lifecycle.evaluate(closed, self.D + G)))
        # Nothing is stamped for a held assignment.
        self.assertFalse(need(lifecycle.evaluate(row(deadline_at=self.D, lifecycle_hold="clock_review"), self.D)))


class HoldParsingTest(unittest.TestCase):
    def test_a_set_of_known_reasons_in_the_fixed_order_is_well_formed(self):
        self.assertEqual(lifecycle.parse_hold(None), ((), True))
        self.assertEqual(lifecycle.parse_hold("clock_review"), (("clock_review",), True))
        self.assertEqual(lifecycle.parse_hold("clock_review,restore_pending"),
                         (("clock_review", "restore_pending"), True))
        self.assertEqual(lifecycle.HOLD_REASONS, ("clock_review", "restore_pending", "restore_unreconciled"))

    def test_anything_else_is_unreadable_and_still_holds(self):
        for raw in ("", "restore_pending,clock_review", "clock_review,clock_review", "clock_review,",
                    "restore_reclean", "CLOCK_REVIEW", "clock_review restore_pending", 1):
            with self.subTest(raw=raw):
                self.assertFalse(lifecycle.parse_hold(raw)[1])
                self.assertTrue(lifecycle.is_held({"lifecycle_hold": raw}))
                with self.assertRaises(lifecycle.AssignmentHeld):
                    lifecycle.require_served({"id": 7, "lifecycle_hold": raw})
        lifecycle.require_served({"id": 7, "lifecycle_hold": None})  # no hold: served


class PresentationTest(ThirdZoneProcessMixin, unittest.TestCase):
    def test_l100_times_are_shown_in_the_stored_zone_which_is_named(self):
        closed = datetime(2026, 7, 1, 9, 0, 30)
        shown = lifecycle.describe(lifecycle.evaluate(
            row(closed_at=closed, display_timezone=NEW_YORK), closed + timedelta(minutes=1)))
        self.assertEqual((shown["zone"], shown["zone_is_stored"]), (NEW_YORK, True))
        self.assertEqual(shown["closed_at"], "01.07.2026 05:00")       # UTC-4
        self.assertEqual(shown["submit_cutoff"], "01.07.2026 05:15")
        self.assertIsNone(shown["deadline"])
        self.assertEqual(shown["grace_minutes"], lifecycle.GRACE_SECONDS // 60)

    def test_l101_a_row_without_a_zone_is_shown_in_the_named_application_default(self):
        shown = lifecycle.describe(lifecycle.evaluate(
            row(deadline_at=datetime(2026, 4, 3, 21, 0, 0)), datetime(2026, 4, 1)))
        self.assertEqual((shown["zone"], shown["zone_is_stored"]), (lifecycle.DEFAULT_TIMEZONE, False))
        self.assertEqual(shown["deadline"], {"text": "до 23:59 на 03.04.2026",
                                             "form_date": "2026-04-03", "form_time": "23:59"})
        # A zone the database no longer knows is treated the same way.
        unknown = lifecycle.describe(lifecycle.evaluate(row(display_timezone="Europe/Atlantis"), datetime(2026, 4, 1)))
        self.assertEqual((unknown["zone"], unknown["zone_is_stored"]), (lifecycle.DEFAULT_TIMEZONE, False))

    def test_the_default_form_offers_fourteen_days_at_2359(self):
        form = lifecycle.default_deadline_form(datetime(2026, 3, 20, 10, 0, 0), SOFIA)
        self.assertEqual(form, {"choice": "deadline", "form_date": "2026-04-03", "form_time": "23:59",
                                "zone": SOFIA, "max_days": 180})


if __name__ == "__main__":
    unittest.main()
