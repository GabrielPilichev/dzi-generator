"""A6: the naive UTC clock (``web.services.lifecycle.naive_utc_now``).

The default application clock, the quiz clock and the unfrozen test clock all read
"now" as a *naive UTC* ``datetime`` with its microseconds. Lifecycle time, the stored
timestamps and the quiz countdown depend on exactly that value. These tests pin it
beneath each changed reader, where the frozen ``TEST_CLOCK`` and ``lifecycle_time``
(which replace the reader) cannot see a regression:

* ``lifecycle.datetime`` is replaced, only while a reader samples, by a subclass whose
  ``now(tz)`` returns a fixed instant converted to ``tz`` and, when no zone is given,
  the Europe/Sofia wall time of that instant. A reader that asked for local time, kept
  a zone, truncated or sampled twice therefore returns a different value;
* a fixed instant is the oracle, never the real clock, except for the cold production
  default, which is bracketed by independent ``time.time()`` readings in fresh
  interpreters under two server time zones.

Nothing reloads a module. Every temporary clock configuration restores the exact
previous reader object.
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
import time
import unittest
import warnings
from contextlib import ExitStack, closing, contextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from zoneinfo import ZoneInfo

from tests.support import ROOT, TEST_CLOCK, isolated_app, reset_lifecycle_clock_row, web_app
from web.services import lifecycle
from web.services import quiz as quiz_service

SOFIA = ZoneInfo("Europe/Sofia")

# name -> (the fixed UTC instant, the whole-second stored timestamp of that instant).
# The stored form is written out, not computed: truncation (never rounding) is part of it.
INSTANTS = {
    "whole second": (datetime(2026, 3, 2, 9, 0, 0), "2026-03-02 09:00:00"),
    "microseconds": (datetime(2026, 3, 2, 9, 0, 0, 123456), "2026-03-02 09:00:00"),
    "last microsecond": (datetime(2026, 3, 2, 9, 0, 0, 999999), "2026-03-02 09:00:00"),
    "UTC midnight": (datetime(2026, 3, 3, 0, 0, 0), "2026-03-03 00:00:00"),
    "before UTC midnight": (datetime(2026, 3, 2, 23, 59, 59, 999999), "2026-03-02 23:59:59"),
    # Europe/Sofia moves 03:00 -> 04:00 at 2026-03-29 01:00 UTC ...
    "before Sofia spring forward": (datetime(2026, 3, 29, 0, 59, 59, 999999), "2026-03-29 00:59:59"),
    "Sofia spring forward": (datetime(2026, 3, 29, 1, 0, 0, 1), "2026-03-29 01:00:00"),
    # ... and 04:00 -> 03:00 (the local hour repeats) at 2026-10-25 01:00 UTC.
    "before Sofia fall back": (datetime(2026, 10, 25, 0, 59, 59, 500000), "2026-10-25 00:59:59"),
    "Sofia fall back": (datetime(2026, 10, 25, 1, 0, 0), "2026-10-25 01:00:00"),
    "inside the repeated Sofia hour": (datetime(2026, 10, 25, 1, 30, 0, 1), "2026-10-25 01:30:00"),
}


def fixed_datetime(instant: datetime):
    """A ``datetime`` stand-in whose ``now`` reads the fixed UTC ``instant``.

    ``now(tz)`` is that instant in ``tz``; without a zone it is the Europe/Sofia wall
    time, which is what a reader that wrongly used server-local time would return.
    The zones it was asked for are recorded in ``calls``: one entry per sample.
    """
    aware = instant.replace(tzinfo=timezone.utc)
    calls: list = []

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            calls.append(tz)
            if tz is None:
                return aware.astimezone(SOFIA).replace(tzinfo=None)
            return aware.astimezone(tz)

    FixedDatetime.calls = calls
    return FixedDatetime


@contextmanager
def clock_at(instant: datetime):
    """The clock beneath the readers reads ``instant``; a deprecated reader would fail loudly."""
    fake = fixed_datetime(instant)
    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        with patch.object(lifecycle, "datetime", fake):
            yield fake


def read_at(instant: datetime, reader):
    """``(value, zones requested)`` of one call of ``reader`` at ``instant``.

    The stand-in is gone again when this returns: ``lifecycle.format_timestamp`` checks
    ``isinstance`` against the module's real ``datetime``.
    """
    with clock_at(instant) as fake:
        value = reader()
    return value, list(fake.calls)


_READER_AT_START = None


def setUpModule():
    global _READER_AT_START
    _READER_AT_START = lifecycle._wall_clock


def tearDownModule():
    # No test of this module may leave a different application clock registered.
    if lifecycle._wall_clock is not _READER_AT_START:
        lifecycle.configure_wall_clock(_READER_AT_START)
        raise AssertionError("tests.test_naive_utc_clock leaked a registered wall clock")


class ClockRestoringTestCase(unittest.TestCase):
    """Every test leaves the exact reader object it found registered."""

    def setUp(self):
        super().setUp()
        self.addCleanup(self._restore_reader, lifecycle._wall_clock)

    @staticmethod
    def _restore_reader(reader):
        lifecycle.configure_wall_clock(reader)


class NaiveUtcNowTest(unittest.TestCase):
    def test_the_helper_is_the_fixed_utc_instant_naive_and_unrounded(self):
        for name, (instant, _stored) in INSTANTS.items():
            with self.subTest(name):
                value, _zones = read_at(instant, lifecycle.naive_utc_now)
                self.assertIsNone(value.tzinfo)
                self.assertEqual(value, instant)  # not Sofia wall time, not truncated, not rounded
                self.assertEqual(value.microsecond, instant.microsecond)

    def test_the_helper_names_utc_and_samples_the_clock_once(self):
        for name, (instant, _stored) in INSTANTS.items():
            with self.subTest(name):
                _value, zones = read_at(instant, lifecycle.naive_utc_now)
                self.assertEqual(len(zones), 1)
                self.assertIsNotNone(zones[0], "the zone must be named, never server-local")
                self.assertEqual(zones[0].utcoffset(None), timedelta(0))

    def test_the_helper_reads_the_real_clock_when_nothing_is_substituted(self):
        # The real clock, bracketed by independent ``time.time()`` readings.
        before = datetime.fromtimestamp(time.time(), timezone.utc).replace(tzinfo=None)
        value = lifecycle.naive_utc_now()
        after = datetime.fromtimestamp(time.time(), timezone.utc).replace(tzinfo=None)
        self.assertIsNone(value.tzinfo)
        slack = timedelta(milliseconds=5)
        self.assertTrue(before - slack <= value <= after + slack, (before, value, after))


class QuizReaderTest(unittest.TestCase):
    def test_the_quiz_clock_is_the_unrounded_naive_utc_instant(self):
        for name, (instant, _stored) in INSTANTS.items():
            with self.subTest(name):
                value, zones = read_at(instant, quiz_service.quiz_current_timestamp)
                self.assertIsNone(value.tzinfo)
                self.assertEqual(value, instant)
                self.assertEqual(value.isoformat(), instant.isoformat())  # no "+00:00", microseconds kept
                self.assertEqual(len(zones), 1)

    def test_the_countdown_compares_a_naive_start_with_the_naive_utc_clock(self):
        # ``quiz_remaining_seconds`` swallows every error and then reports the full limit, so a
        # local-time or aware clock shows up here only as a wrong number of seconds.
        cases = (
            ("ordinary", datetime(2026, 3, 2, 9, 10, 30, 750000), "2026-03-02T09:00:00", 30, 1170),
            ("repeated Sofia hour", datetime(2026, 10, 25, 1, 10, 30, 1), "2026-10-25T00:30:00", 60, 1170),
            ("Sofia spring forward", datetime(2026, 3, 29, 1, 5, 0), "2026-03-29T00:55:00", 20, 600),
        )
        for name, instant, started_at, limit, expected in cases:
            with self.subTest(name):
                assignment = {"time_limit_minutes": limit}
                attempt = {"started_at": started_at}
                value, _zones = read_at(instant, lambda: quiz_service.quiz_remaining_seconds(assignment, attempt))
                self.assertEqual(value, expected)


class UnfrozenTestClockTest(unittest.TestCase):
    def setUp(self):
        saved = dict(TEST_CLOCK.__dict__)
        self.addCleanup(self._restore, saved)

    @staticmethod
    def _restore(saved):
        TEST_CLOCK.__dict__.clear()
        TEST_CLOCK.__dict__.update(saved)

    def test_an_unfrozen_test_clock_reads_naive_utc_with_microseconds(self):
        TEST_CLOCK.frozen_at = None
        for name, (instant, _stored) in INSTANTS.items():
            with self.subTest(name):
                value, zones = read_at(instant, TEST_CLOCK.wall)
                self.assertEqual(value, instant)
                self.assertIsNone(value.tzinfo)
                self.assertEqual(len(zones), 1)

    def test_a_frozen_test_clock_returns_its_instant_without_reading_the_clock(self):
        TEST_CLOCK.freeze(datetime(2026, 3, 2, 9, 0, 0, 999999))
        frozen = TEST_CLOCK.frozen_at
        self.assertEqual(frozen, datetime(2026, 3, 2, 9, 0, 0))  # ``freeze`` drops microseconds
        value, zones = read_at(datetime(2030, 1, 1, 12, 0, 0, 5), TEST_CLOCK.wall)
        self.assertIs(value, frozen)
        self.assertEqual(zones, [])


class InjectionSeamTest(ClockRestoringTestCase):
    def test_the_registered_application_clock_is_read_through_the_quiz_reader(self):
        for name, (instant, _stored) in INSTANTS.items():
            with self.subTest(name):
                value, zones = read_at(instant, lifecycle.wall_reading)
                self.assertIsNone(value.tzinfo)
                self.assertEqual(value, instant.replace(microsecond=0))  # whole seconds only here
                self.assertEqual(len(zones), 1)

    def test_a_later_patch_of_the_quiz_clock_still_governs_lifecycle_reads(self):
        patched = datetime(2031, 5, 6, 7, 8, 9, 654321)
        with patch.object(quiz_service, "quiz_current_timestamp", return_value=patched):
            self.assertEqual(lifecycle.wall_reading(), patched.replace(microsecond=0))

    def test_a_configured_naive_reader_is_truncated_to_whole_seconds(self):
        lifecycle.configure_wall_clock(lambda: datetime(2026, 3, 2, 9, 0, 0, 999999))
        value = lifecycle.wall_reading()
        self.assertEqual(value, datetime(2026, 3, 2, 9, 0, 0))
        self.assertIsNone(value.tzinfo)

    def test_a_configured_aware_reader_is_converted_to_naive_utc(self):
        # The second pass of the repeated Sofia hour: 03:30:00.7 EET is 01:30:00.7 UTC.
        local = datetime(2026, 10, 25, 3, 30, 0, 700000, tzinfo=SOFIA, fold=1)
        self.assertEqual(local.utcoffset(), timedelta(hours=2))
        lifecycle.configure_wall_clock(lambda: local)
        value = lifecycle.wall_reading()
        self.assertEqual(value, datetime(2026, 10, 25, 1, 30, 0))
        self.assertIsNone(value.tzinfo)


class StoredTimestampTest(unittest.TestCase):
    """The lifecycle writer stores the same bytes at the same instant."""

    def setUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        self.root = stack.enter_context(isolated_app())
        self.db_path = web_app.app.config["DB_PATH"]

    def stored_clock_row(self) -> tuple:
        with closing(web_app.quiz_db()) as conn:
            row = conn.execute("SELECT high_water_at, ref_wall_at, state FROM lifecycle_clock").fetchone()
            return tuple(row)

    def test_a_lifecycle_write_stores_the_whole_second_utc_text_of_the_instant(self):
        real_reader = quiz_service.quiz_current_timestamp
        for name, (instant, stored) in INSTANTS.items():
            with self.subTest(name):
                reset_lifecycle_clock_row(self.db_path)

                def at_instant():
                    # The real quiz reader, with the clock beneath it fixed only while it samples.
                    value, _zones = read_at(instant, real_reader)
                    return value

                with patch.object(quiz_service, "quiz_current_timestamp", new=at_instant):
                    with closing(web_app.quiz_db()) as conn:
                        with lifecycle.write_transaction(conn) as transaction:
                            transaction.admit()
                            admitted = transaction.now
                self.assertEqual(admitted, datetime.strptime(stored, "%Y-%m-%d %H:%M:%S"))
                self.assertEqual(self.stored_clock_row(), (stored, stored, "ok"))
                self.assertEqual(lifecycle.format_timestamp(admitted), stored)

    def test_a_read_page_sees_the_whole_second_utc_instant(self):
        real_reader = quiz_service.quiz_current_timestamp
        for name, (instant, stored) in INSTANTS.items():
            with self.subTest(name):
                reset_lifecycle_clock_row(self.db_path)

                def at_instant():
                    value, _zones = read_at(instant, real_reader)
                    return value

                with patch.object(quiz_service, "quiz_current_timestamp", new=at_instant):
                    with closing(web_app.quiz_db()) as conn:
                        reading = lifecycle.read_clock(conn)
                self.assertTrue(reading.healthy)
                self.assertEqual(lifecycle.format_timestamp(reading.now), stored)


COLD_PROBE = r"""
import json, sys, time
from datetime import datetime, timezone

sys.path.insert(0, sys.argv[1])
from web.services import lifecycle
import src.manage_lifecycle  # noqa: F401  (the operator command, as the restore script runs it)


def utc(seconds):
    return datetime.fromtimestamp(seconds, timezone.utc).replace(tzinfo=None).isoformat()


loaded = [name for name in ("web.services.quiz", "web.app") if name in sys.modules]
first = time.time()
raw = lifecycle._wall_clock()
reading = lifecycle.wall_reading()
last = time.time()
print(json.dumps({
    "loaded": loaded,
    "local_offset": time.localtime().tm_gmtoff,
    "first": utc(first), "last": utc(last),
    "raw": raw.isoformat(), "raw_aware": raw.tzinfo is not None,
    "reading": reading.isoformat(), "reading_aware": reading.tzinfo is not None,
    "reading_microsecond": reading.microsecond,
}))
"""


class ColdProductionDefaultTest(unittest.TestCase):
    """The default clock of ``python -m src.manage_lifecycle``, which never imports the quiz."""

    def probe(self, zone: str) -> dict:
        env = {"PATH": os.environ.get("PATH", ""), "TZ": zone, "PYTHONDONTWRITEBYTECODE": "1"}
        done = subprocess.run([sys.executable, "-B", "-c", COLD_PROBE, str(ROOT)], env=env, cwd=str(ROOT),
                              capture_output=True, text=True, timeout=120)
        self.assertEqual(done.returncode, 0, done.stderr)
        return json.loads(done.stdout)

    def test_the_default_clock_is_naive_utc_whatever_the_server_zone(self):
        slack = timedelta(milliseconds=5)
        for zone in ("Europe/Sofia", "Pacific/Kiritimati", "America/Los_Angeles"):
            with self.subTest(zone):
                result = self.probe(zone)
                self.assertEqual(result["loaded"], [], "the default must not depend on the quiz registering a clock")
                self.assertNotEqual(result["local_offset"], 0, "the probe must really run in a non-UTC zone")
                first = datetime.fromisoformat(result["first"])
                last = datetime.fromisoformat(result["last"])
                raw = datetime.fromisoformat(result["raw"])
                reading = datetime.fromisoformat(result["reading"])
                self.assertFalse(result["raw_aware"])
                self.assertFalse(result["reading_aware"])
                self.assertTrue(first - slack <= raw <= last + slack, (first, raw, last))
                self.assertEqual(result["reading_microsecond"], 0)
                self.assertTrue(first.replace(microsecond=0) <= reading <= last, (first, reading, last))


class NoDeprecatedClockReaderTest(unittest.TestCase):
    """A6 is done when no tracked Python reads the clock the deprecated way, as a call or a binding."""

    DEPRECATED = "utc" + "now"

    def tracked_python(self):
        files = list(ROOT.glob("*.py"))
        for directory in ("deploy", "src", "tests", "web"):
            files.extend((ROOT / directory).rglob("*.py"))
        return sorted(path for path in files if "__pycache__" not in path.parts)

    def test_no_tracked_python_references_the_deprecated_reader(self):
        found = []
        for path in self.tracked_python():
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Attribute) and node.attr == self.DEPRECATED:
                    found.append(f"{path.relative_to(ROOT)}:{node.lineno}")
        self.assertEqual(found, [])

    def test_the_scan_sees_a_call_and_a_stored_reference(self):
        # The detector itself: a calls-only search would miss the second form.
        for source in (f"datetime.{self.DEPRECATED}()", f"clock = datetime.{self.DEPRECATED}"):
            attributes = [node.attr for node in ast.walk(ast.parse(source)) if isinstance(node, ast.Attribute)]
            self.assertIn(self.DEPRECATED, attributes)


if __name__ == "__main__":
    unittest.main()
