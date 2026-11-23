"""The bounded submit request of the dormant DZI Part 1 service (C3.3): ``web.services.dzi_part1.read_bounded_form``.

Every request is built in process (a WSGI environ from ``werkzeug.test.EnvironBuilder`` with the stream and
the declared length set by hand, ``wsgi.input_terminated`` true as the reviewer's check had it) and pushed as
a Flask request context of the real application object: no route is registered, no server is started, and
the helper never touches a connection or the clock. The end-to-end class at the bottom hands the bounded
fields to ``submit_part1`` on a disposable database built from migrations + canonical at a frozen lifecycle
time. No connection opens ``data/questions.db`` (building the template runs the migration runner, whose
default-path check resolves that path: filesystem metadata only).

What is proved (design 6.4 and 9):

* the fixed order: 411 and 413 before the body is read, the bounded read and the declared-length equality
  before ``request.form`` is touched, then the form;
* the four required cases (complete 200, chunked or missing length 411, oversize 413, short 400), a stream
  that delivers more under a declared length below 64 KiB (400), the exact 64 KiB edge (200 with only the
  declared prefix consumed and parsed), and a malformed length as Werkzeug 3.1.9 reads it (``0``);
* the per-request cap beats the application's, and the body is read once into Werkzeug's cache;
* end to end: the excess bytes of an over-delivering stream are never graded or stored, a refused request
  changes no table (the clock row included) and a request with no fields takes the normal ``empty`` outcome.

What is NOT claimed: that over-delivery past the cap is always detected. At a declared length of exactly
65,536 the read stops at the declared bytes, so the excess is simply not seen.
"""

from __future__ import annotations

import io
import unittest
from urllib.parse import urlencode

from werkzeug.test import EnvironBuilder

from tests.support import web_app
from tests.test_dzi_part1_start_service import Part1Case
from web.services import dzi_part1 as part1

FORM = "application/x-www-form-urlencoded"
LIMIT = 64 * 1024


def make_environ(delivered: bytes, declared=None, *, terminated=True, chunked=False, content_type=FORM):
    """A POST environ delivering ``delivered`` under a declared length chosen independently of it."""
    environ = EnvironBuilder(method="POST", path="/probe", content_type=content_type).get_environ()
    stream = io.BytesIO(delivered)
    environ["CONTENT_TYPE"] = content_type  # EnvironBuilder would invent its own multipart boundary
    environ["wsgi.input"] = stream
    environ.pop("CONTENT_LENGTH", None)
    environ.pop("HTTP_TRANSFER_ENCODING", None)
    if chunked:
        environ["HTTP_TRANSFER_ENCODING"] = "chunked"
    if declared is not None:
        environ["CONTENT_LENGTH"] = declared if isinstance(declared, str) else str(declared)
    if terminated:
        environ["wsgi.input_terminated"] = True
    else:
        environ.pop("wsgi.input_terminated", None)
    return environ, stream


def padded(total: int, name: str = "a_1_1") -> bytes:
    """A urlencoded body of exactly ``total`` bytes with one long field."""
    return (name + "=" + "x" * (total - len(name) - 1)).encode()


class RequestCase(unittest.TestCase):
    def call(self, delivered: bytes, declared=None, **kwargs):
        """(result, stream position, the request) for one in-process request."""
        environ, stream = make_environ(delivered, declared, **kwargs)
        with web_app.app.request_context(environ):
            from flask import request

            result = part1.read_bounded_form(request)
            self.last_request = request
            form_parsed = "form" in request.__dict__
            cap = request.max_content_length
            cached = request.get_data(cache=True) if result.status in (200, 400) and form_parsed else None
        self.form_parsed, self.cap, self.cached = form_parsed, cap, cached
        return result, stream.tell()


class ConstantsTest(unittest.TestCase):
    def test_the_bounds_are_the_design_values(self):
        self.assertEqual((part1.MAX_BODY_BYTES, part1.MAX_ANSWER_CHARACTERS), (65536, 200))

    def test_the_application_wide_limit_is_far_above_the_per_request_cap(self):
        self.assertGreater(web_app.app.config["MAX_CONTENT_LENGTH"], LIMIT * 100)


class FourRequiredCasesTest(RequestCase):
    def test_a_complete_declared_body_is_accepted_and_parsed(self):
        body = urlencode({"q_5": "А", "a_6_1": "клас"}).encode()
        result, consumed = self.call(body, len(body))
        self.assertEqual((result.status, result.reason, result.ok), (200, "ok", True))
        self.assertEqual(result.fields, {"q_5": ("А",), "a_6_1": ("клас",)})
        self.assertEqual((result.declared_length, result.received_length, consumed), (len(body),) * 3)

    def test_a_chunked_request_is_411_and_nothing_is_read(self):
        for body in (b"q_1=A", padded(70_000)):
            with self.subTest(size=len(body)):
                result, consumed = self.call(body, chunked=True)
                self.assertEqual((result.status, result.reason, result.fields), (411, "length_required", None))
                self.assertEqual(consumed, 0)
                self.assertFalse(self.form_parsed)

    def test_chunked_wins_over_a_content_length_header(self):
        result, consumed = self.call(b"q_1=A", 5, chunked=True)
        self.assertEqual((result.status, consumed, self.form_parsed), (411, 0, False))

    def test_a_missing_length_is_411_and_nothing_is_read(self):
        result, consumed = self.call(b"q_1=A")
        self.assertEqual((result.status, result.reason, consumed, self.form_parsed), (411, "length_required", 0, False))

    def test_an_oversize_declaration_is_413_before_any_read(self):
        for declared in (LIMIT + 1, 10 ** 9, 10 ** 15):
            with self.subTest(declared=declared):
                result, consumed = self.call(padded(70_000), declared)
                self.assertEqual((result.status, result.reason, result.fields), (413, "too_large", None))
                self.assertEqual((consumed, self.form_parsed), (0, False))

    def test_a_short_declared_body_is_400_before_any_field_is_parsed(self):
        body = padded(1000)
        result, consumed = self.call(body[:500], 1000)
        self.assertEqual((result.status, result.reason, result.fields), (400, "length_mismatch", None))
        self.assertEqual((result.declared_length, result.received_length, consumed), (1000, 500, 500))
        self.assertFalse(self.form_parsed)  # request.form was never read: nothing was parsed, so nothing is graded

    def test_a_short_body_on_an_unterminated_stream_is_a_refused_disconnect(self):
        result, _consumed = self.call(padded(1000)[:500], 1000, terminated=False)
        self.assertEqual((result.status, result.reason, result.fields), (400, "unreadable", None))
        self.assertFalse(self.form_parsed)


class LongDeliveryTest(RequestCase):
    def test_a_stream_delivering_more_under_a_declared_length_below_the_cap_is_400_after_a_bounded_read(self):
        result, consumed = self.call(padded(1_000_000), 500)
        self.assertEqual((result.status, result.reason, result.fields), (400, "length_mismatch", None))
        # the read stopped at the 64 KiB cap, never at the million bytes the stream offered
        self.assertEqual((result.declared_length, result.received_length, consumed), (500, LIMIT, LIMIT))
        self.assertFalse(self.form_parsed)

    def test_one_byte_more_than_declared_just_below_the_cap_is_400(self):
        result, consumed = self.call(padded(LIMIT), LIMIT - 1)
        self.assertEqual((result.status, result.received_length, consumed), (400, LIMIT, LIMIT))
        self.assertFalse(self.form_parsed)

    def test_the_exact_cap_is_accepted(self):
        body = padded(LIMIT)
        result, consumed = self.call(body, LIMIT)
        self.assertEqual((result.status, result.declared_length, result.received_length, consumed),
                         (200, LIMIT, LIMIT, LIMIT))
        self.assertEqual(len(result.fields["a_1_1"][0]), LIMIT - len("a_1_1="))

    def test_at_exactly_the_cap_a_stream_delivering_more_reads_only_the_declared_prefix(self):
        """Not a refusal and not claimed to be one: the read stops at the declared length, which equals the cap."""
        delivered = padded(LIMIT) + b"&q_999=EXCESS-BYTES-NEVER-SEEN" + b"z" * 4000
        self.assertGreater(len(delivered), LIMIT)
        result, consumed = self.call(delivered, LIMIT)
        self.assertEqual((result.status, result.declared_length, result.received_length), (200, LIMIT, LIMIT))
        self.assertEqual(consumed, LIMIT)                      # the excess was never read
        self.assertEqual(set(result.fields), {"a_1_1"})        # the excess field was never parsed
        self.assertEqual(result.fields["a_1_1"][0], "x" * (LIMIT - len("a_1_1=")))
        self.assertNotIn("EXCESS", repr(result.fields))

    def test_on_an_unterminated_stream_a_longer_stream_is_limited_to_the_declared_length(self):
        result, consumed = self.call(padded(1_000_000), 500, terminated=False)
        self.assertEqual((result.status, result.received_length, consumed), (200, 500, 500))


class MalformedLengthTest(RequestCase):
    """Werkzeug 3.1.9 reads a non-numeric, negative, signed or underscored length as ``0``, not ``None``."""

    BAD = ("abc", "-5", "+5", "1_000", "1e3", "12abc", "")

    def test_the_declared_value_is_zero_for_each(self):
        for bad in self.BAD:
            with self.subTest(header=bad):
                environ, _stream = make_environ(b"", bad)
                with web_app.app.request_context(environ):
                    from flask import request

                    self.assertEqual(request.content_length, 0)

    def test_with_a_body_delivered_it_is_400_and_nothing_is_parsed(self):
        for bad in self.BAD:
            with self.subTest(header=bad):
                result, consumed = self.call(b"q_1=A&a_2_1=x", bad)
                self.assertEqual((result.status, result.reason, result.declared_length), (400, "length_mismatch", 0))
                self.assertEqual((result.received_length, consumed), (13, 13))
                self.assertFalse(self.form_parsed)

    def test_with_no_body_it_is_the_normal_empty_request(self):
        for bad in self.BAD:
            with self.subTest(header=bad):
                result, _consumed = self.call(b"", bad)
                self.assertEqual((result.status, result.fields, result.declared_length), (200, {}, 0))


class CacheAndCapTest(RequestCase):
    def test_the_per_request_cap_replaces_the_application_limit(self):
        self.call(b"q_1=A", 5)
        self.assertEqual(self.cap, LIMIT)
        self.assertNotEqual(web_app.app.config["MAX_CONTENT_LENGTH"], LIMIT)

    def test_a_small_application_limit_does_not_override_the_cap(self):
        from unittest.mock import patch

        with patch.dict(web_app.app.config, {"MAX_CONTENT_LENGTH": 10}):
            result, _consumed = self.call(padded(100), 100)
        self.assertEqual(result.status, 200)

    def test_the_body_is_read_once_into_the_cache_and_the_form_comes_from_it(self):
        body = urlencode({"q_5": "Б"}).encode()
        result, consumed = self.call(body, len(body))
        self.assertEqual((result.status, self.cached, consumed), (200, body, len(body)))
        self.assertTrue(self.form_parsed)

    def test_a_body_read_before_the_helper_fails_closed(self):
        environ, stream = make_environ(b"q_1=A", 5)
        with web_app.app.request_context(environ):
            from flask import request

            self.assertEqual(request.form.get("q_1"), "A")  # something else already parsed the form
            result = part1.read_bounded_form(request)
        self.assertEqual((result.status, result.reason, result.fields, result.received_length),
                         (400, "length_mismatch", None, 0))


class FormShapeTest(RequestCase):
    def test_duplicate_names_keep_every_value(self):
        body = b"q_1=A&q_1=%D0%91&a_2_1=x"
        result, _consumed = self.call(body, len(body))
        self.assertEqual(result.fields, {"q_1": ("A", "Б"), "a_2_1": ("x",)})

    def test_a_multipart_form_is_accepted_and_a_file_part_is_not_a_field(self):
        body = (b"--B\r\nContent-Disposition: form-data; name=\"q_1\"\r\n\r\nA\r\n"
                b"--B\r\nContent-Disposition: form-data; name=\"upload\"; filename=\"f.txt\"\r\n"
                b"Content-Type: text/plain\r\n\r\nFILE-BYTES\r\n--B--\r\n")
        result, _consumed = self.call(body, len(body), content_type="multipart/form-data; boundary=B")
        self.assertEqual((result.status, result.fields), (200, {"q_1": ("A",)}))

    def test_a_non_form_content_type_is_the_normal_empty_request(self):
        body = b'{"q_1": "A"}'
        result, consumed = self.call(body, len(body), content_type="application/json")
        self.assertEqual((result.status, result.fields, consumed), (200, {}, len(body)))

    def test_too_many_multipart_parts_inside_the_cap_is_413_from_the_form_parser(self):
        part = b"--B\r\nContent-Disposition: form-data; name=\"f\"\r\n\r\n\r\n"
        body = part * 1100 + b"--B--\r\n"
        self.assertLess(len(body), LIMIT)
        result, _consumed = self.call(body, len(body), content_type="multipart/form-data; boundary=B")
        self.assertEqual((result.status, result.reason, result.fields), (413, "too_large", None))


class BoundedRequestToServiceTest(Part1Case):
    """The bounded fields handed to the service, on a disposable database at a frozen clock."""

    def setUp(self):
        super().setUp()
        self.attempt = self.start()
        self.plan = list(self.attempt.question_ids)

    def correct_letter(self, question_id):
        return self.conn.execute("SELECT option_letter FROM multiple_choice_options WHERE question_id = ? "
                                 "AND is_correct = 1", (question_id,)).fetchone()[0]

    def request(self, delivered: bytes, declared=None, **kwargs):
        environ, stream = make_environ(delivered, declared, **kwargs)
        with web_app.app.request_context(environ):
            from flask import request

            return part1.read_bounded_form(request), stream.tell()

    def answers(self):
        return self.sql_rows("SELECT * FROM dzi_part1_answers ORDER BY slot_number, subquestion_number")

    def sql_rows(self, statement, params=()):
        from contextlib import closing

        with closing(self.raw()) as conn:
            return [dict(row) for row in conn.execute(statement, params)]

    def test_an_accepted_request_is_graded_from_the_bounded_fields(self):
        body = urlencode({f"q_{self.plan[0]}": self.correct_letter(self.plan[0]), "seed": "x", "user_id": "9"}).encode()
        bounded, _consumed = self.request(body, len(body))
        outcome = part1.submit_part1(self.conn, self.student, self.attempt.attempt_id, bounded.fields)
        self.assertEqual((outcome.outcome, outcome.score_points, outcome.score_max), ("submitted", 1, 45))

    def test_excess_bytes_beyond_the_cap_are_never_graded_or_stored(self):
        first, second = self.plan[0], self.plan[1]
        prefix = urlencode({f"q_{first}": self.correct_letter(first)})
        filler = "&pad=" + "x" * (LIMIT - len(prefix) - len("&pad="))
        excess = "&" + urlencode({f"q_{second}": self.correct_letter(second)})
        delivered = (prefix + filler).encode() + excess.encode() + b"&q_%d=%s" % (self.plan[2], b"%D0%90")
        self.assertEqual(len((prefix + filler).encode()), LIMIT)
        bounded, consumed = self.request(delivered, LIMIT)
        self.assertEqual((bounded.status, consumed, bounded.received_length), (200, LIMIT, LIMIT))
        self.assertEqual(set(bounded.fields), {f"q_{first}", "pad"})
        outcome = part1.submit_part1(self.conn, self.student, self.attempt.attempt_id, bounded.fields)
        self.assertEqual(outcome.outcome, "submitted")
        rows = self.answers()
        by_slot = {row["slot_number"]: row for row in rows if row["kind"] == "multiple_choice"}
        self.assertEqual(by_slot[1]["chosen_letter"], self.correct_letter(first))
        self.assertIsNone(by_slot[2]["chosen_letter"])  # the excess answer for slot 2 was never seen
        self.assertIsNone(by_slot[3]["chosen_letter"])
        self.assertEqual((outcome.score_points, outcome.score_max), (1, 45))
        self.assertNotIn("pad", repr(rows))

    def test_a_refused_request_never_reaches_the_service_and_changes_no_table(self):
        before = self.state()
        for delivered, declared in ((b"q_1=A", None), (padded(LIMIT + 5), LIMIT + 5), (padded(1000)[:500], 1000),
                                    (padded(1_000_000), 500)):
            with self.subTest(declared=declared):
                bounded, _consumed = self.request(delivered, declared)
                self.assertFalse(bounded.ok)
                self.assertIsNone(bounded.fields)
                self.assertIn(bounded.status, (400, 411, 413))
        self.assertEqual(self.state(), before)  # every table, the clock row included
        self.assertEqual(self.attempts()[0]["submitted_at"], None)

    def test_a_request_with_no_fields_is_the_normal_empty_outcome_and_writes_nothing(self):
        for declared in (0, "abc"):
            with self.subTest(declared=declared):
                bounded, _consumed = self.request(b"", declared)
                self.assertEqual((bounded.status, bounded.fields), (200, {}))
                before = self.state()
                outcome = part1.submit_part1(self.conn, self.student, self.attempt.attempt_id, bounded.fields)
                self.assertEqual(outcome.outcome, "empty")
                self.assertEqual(self.state(), before)


if __name__ == "__main__":
    unittest.main()
