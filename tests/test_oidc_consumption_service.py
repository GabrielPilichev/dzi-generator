"""Single-use consumption storage (``web.services.oidc_consumption``).

A key is recorded at most once, a repeated key is refused, expiry is decided before
any statement runs, cleanup is by expiry only and transactions belong to the caller.
Every test works on its own temporary database cloned by
``tests.support.build_test_database`` (migrations + canonical content) and opened
under the application's connection policy (``web.db.connect_db``). Nothing reads
``data/questions.db``, and no test sleeps: the clock is an argument.
"""

import ast
import collections
import hashlib
import hmac
import inspect
import random
import re
import secrets
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import closing
from decimal import Decimal
from pathlib import Path

from tests.support import ROOT, build_test_database
from web.db import connect_db
from web.services import oidc_consumption as consumption

TABLE = "oidc_consumed_transactions"
MODULE = ROOT / "web" / "services" / "oidc_consumption.py"
SECRET = "fixture-secret-key"
VALUE = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"  # the shape of secrets.token_urlsafe(32)
KEY = "0123456789abcdef" * 4
ALLOWANCE = 60

MALFORMED_KEYS = {
    "63 characters": KEY[:63],
    "65 characters": KEY + "0",
    "empty": "",
    "upper-case hexadecimal": KEY.upper(),
    "one upper-case digit": KEY[:63] + "A",
    "one character outside 0-9a-f": KEY[:63] + "g",
    "a raw URL-safe value": VALUE,
    "trailing NUL after 64 digits": KEY + "\x00",
    "embedded NUL in 64 characters": KEY[:10] + "\x00" + KEY[:53],
    "NUL as the 64th character": KEY[:63] + "\x00",
    "trailing newline after 64 digits": KEY + "\n",
    "newline as the 64th character": KEY[:63] + "\n",
    "space padding": " " + KEY[:62] + " ",
    "a non-ASCII character": KEY[:63] + "é",
    "a full-width digit": KEY[:63] + "０",
    "bytes": KEY.encode("ascii"),
    "a bytearray": bytearray(KEY.encode("ascii")),
    "an integer": int(KEY, 16),
    "None": None,
    "a list": [KEY],
}


class Seconds(int):
    """An int subclass: not the plain ``int`` a time has to be."""


MALFORMED_TIMES = {
    "True": True, "False": False, "a float": 600.0, "a float with a fraction": 600.5, "text": "600",
    "bytes": b"600", "None": None, "a negative integer": -1, "2**62": 2**62, "2**63": 2**63,
    "a Decimal": Decimal(600), "an int subclass": Seconds(600), "a list": [600],
}


def key_for(label: str, purpose: str = consumption.PURPOSE_CALLBACK) -> str:
    """The key of one transaction of a test, derived as a caller derives it."""
    return consumption.consumption_key(SECRET, purpose, label)


class ConsumptionTestCase(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="oidc-consumption-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.path = build_test_database(self.root / "consumption.db")
        self.conn = self.connect()

    def connect(self, path=None, **options):
        conn = connect_db(self.path if path is None else path, **options)
        self.addCleanup(conn.close)
        return conn

    def begin(self, conn=None):
        """What a caller does first: take the write lock."""
        conn = self.conn if conn is None else conn
        conn.execute("BEGIN IMMEDIATE")
        return conn

    def consume(self, key=KEY, *, now=100, expires_at=700, conn=None):
        return consumption.consume(self.conn if conn is None else conn, key, now=now, expires_at=expires_at)

    def accept(self, key=KEY, *, now=100, expires_at=700, conn=None):
        """A complete acceptance: lock, consume, commit."""
        conn = self.begin(conn)
        self.consume(key, now=now, expires_at=expires_at, conn=conn)
        conn.commit()

    def rows(self, conn=None):
        conn = self.conn if conn is None else conn
        return [tuple(row) for row in conn.execute(f"SELECT tx_hash, expires_at FROM {TABLE} ORDER BY tx_hash")]

    def committed(self):
        """What is committed, read through a connection of its own."""
        with closing(connect_db(self.path)) as conn:
            return self.rows(conn)

    def traced(self, conn=None):
        """SQL statements the connection runs from now on."""
        conn = self.conn if conn is None else conn
        statements = []
        conn.set_trace_callback(statements.append)
        self.addCleanup(conn.set_trace_callback, None)
        return statements

    def note(self, text, conn=None):
        """A write of the caller's own, in a table the service never touches."""
        (self.conn if conn is None else conn).execute(
            "INSERT INTO generated_exams (question_ids) VALUES (?)", (text,))

    def notes(self, conn=None):
        conn = self.conn if conn is None else conn
        return [row[0] for row in conn.execute("SELECT question_ids FROM generated_exams ORDER BY id")]


class ConsumptionKeyTest(unittest.TestCase):
    def key(self, secret=SECRET, purpose=consumption.PURPOSE_CALLBACK, value=VALUE):
        return consumption.consumption_key(secret, purpose, value)

    def assert_plain_refusal(self, **arguments):
        """Refused with a ValueError of its own: not a subclass that carries the input
        (a UnicodeError keeps the whole string), and with no such error chained to it."""
        with self.assertRaises(ValueError) as refused:
            self.key(**arguments)
        error = refused.exception
        self.assertIs(type(error), ValueError)
        self.assertIsNone(error.__cause__)
        self.assertIsNone(error.__context__)
        return error

    def test_it_is_hmac_sha256_over_the_purpose_and_the_value_in_lower_case_hex(self):
        key = self.key()
        self.assertRegex(key, r"\A[0-9a-f]{64}\Z")
        # Reference values from an independent implementation (openssl dgst -sha256 -hmac).
        self.assertEqual(key, "380b5cd6faa20c07980134b8bdc550a6b436fc1aa68394569fde943214fc8a07")
        self.assertEqual(consumption.consumption_key("k", "callback", "v"),
                         "ead8b729f99b924cc15aebb5b4b48baff2a52865109f454c4e1337d6a0842827")
        self.assertEqual(consumption.consumption_key("k", "link-confirm", "v"),
                         "8918a869aaa4f77a06ffd97d279f90de1e970d10156fbb1eb36e7daff591ff70")
        self.assertEqual(key, hmac.new(SECRET.encode(), b"oidc-consume\0callback\0" + VALUE.encode(),
                                       hashlib.sha256).hexdigest())

    def test_it_is_deterministic(self):
        self.assertEqual(self.key(), self.key())
        self.assertEqual({self.key() for _ in range(20)}, {self.key()})

    def test_it_changes_with_the_secret_the_purpose_and_the_value(self):
        key = self.key()
        self.assertNotEqual(key, self.key(secret=SECRET + "x"))
        self.assertNotEqual(key, self.key(purpose=consumption.PURPOSE_LINK_CONFIRM))
        self.assertNotEqual(key, self.key(value=VALUE[:-1] + "l"))
        self.assertNotEqual(key, self.key(value=VALUE.lower()))  # nothing is folded
        self.assertNotEqual(key, self.key(value=VALUE[:-1]))

    def test_a_bytes_secret_and_the_same_str_secret_give_one_key(self):
        for secret in (SECRET, "ключ-за-тест-ñ"):
            with self.subTest(secret=secret):
                self.assertEqual(self.key(secret=secret), self.key(secret=secret.encode("utf-8")))

    def test_it_is_keyed_and_not_a_bare_hash_of_the_value(self):
        self.assertNotEqual(self.key(), hashlib.sha256(VALUE.encode()).hexdigest())
        self.assertNotEqual(self.key(), hmac.new(SECRET.encode(), VALUE.encode(), hashlib.sha256).hexdigest())

    def test_the_two_purposes_never_share_a_key(self):
        values = [VALUE, "callback", "link-confirm", "confirm", "link", "a", "-", "_", "0" * 128]
        keys = {(purpose, value): consumption.consumption_key(SECRET, purpose, value)
                for purpose in sorted(consumption.PURPOSES) for value in values}
        self.assertEqual(len(set(keys.values())), len(values) * 2)
        for value in values:
            with self.subTest(value=value):
                self.assertNotEqual(keys["callback", value], keys["link-confirm", value])

    def test_the_purposes_are_exactly_callback_and_link_confirm(self):
        self.assertEqual(consumption.PURPOSES, {"callback", "link-confirm"})
        self.assertIsInstance(consumption.PURPOSES, frozenset)
        self.assertEqual((consumption.PURPOSE_CALLBACK, consumption.PURPOSE_LINK_CONFIRM),
                         ("callback", "link-confirm"))
        for purpose in consumption.PURPOSES:
            with self.subTest(purpose=purpose):
                self.assertRegex(self.key(purpose=purpose), r"\A[0-9a-f]{64}\Z")

    def test_any_other_purpose_is_refused(self):
        for purpose in ("", "login", "CALLBACK", "Callback", "callback ", " callback", "callback\n", "callback\x00",
                        "link_confirm", "linkconfirm", "callback,link-confirm", None, b"callback", 1, True,
                        ["callback"], ("callback",), frozenset({"callback"}), consumption.PURPOSES):
            with self.subTest(purpose=purpose):
                self.assert_plain_refusal(purpose=purpose)

    def test_a_value_is_1_to_128_url_safe_characters(self):
        alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
        accepted = ["a", "-", "_", "0", alphabet, "A" * 128, alphabet * 2, secrets.token_urlsafe(32),
                    secrets.token_urlsafe(96)]
        self.assertEqual(len(accepted[-1]), 128)
        for value in accepted:
            with self.subTest(value=value):
                self.assertRegex(self.key(value=value), r"\A[0-9a-f]{64}\Z")
        self.assertEqual(len({self.key(value=character) for character in alphabet}), 64)

    def test_any_other_value_is_refused(self):
        for value in ("", "a" * 129, "a b", " a", "a ", "a\n", "a\r\n", "\ta", "a\x00", "a\x00b", "a+b", "a/b", "a=",
                      "a.b", "a~b", "a%2D", "a,b", "é", "я", "１", "a​b", "\ud800", b"abc", bytearray(b"abc"),
                      None, 123, 1.5, True, ["a"], ("a",)):
            with self.subTest(value=value):
                self.assert_plain_refusal(value=value)

    def test_the_secret_is_non_empty_bytes_or_str(self):
        for secret in ("", b"", None, 123, 1.5, True, bytearray(b"secret"), memoryview(b"secret"), ["secret"],
                       ("secret",), "\ud800"):
            with self.subTest(secret=secret):
                self.assert_plain_refusal(secret=secret)

    def test_no_error_message_repeats_what_was_passed(self):
        marker = "Marker-0123456789"
        attempts = [
            dict(secret_key=marker, purpose=marker, value=VALUE),
            dict(secret_key=marker, purpose=consumption.PURPOSE_CALLBACK, value=marker + "!"),
            dict(secret_key=marker + "\ud800", purpose=consumption.PURPOSE_CALLBACK, value=marker),
            dict(secret_key=[marker], purpose=consumption.PURPOSE_CALLBACK, value=marker),
        ]
        for attempt in attempts:
            with self.subTest(attempt=attempt):
                with self.assertRaises(ValueError) as refused:
                    consumption.consumption_key(**attempt)
                self.assertNotIn(marker, str(refused.exception))
                self.assertNotIn(marker, repr(refused.exception))


class AcceptanceTest(ConsumptionTestCase):
    def test_a_fresh_key_is_recorded_with_the_expiry_that_was_passed(self):
        self.begin()
        self.assertIsNone(self.consume(KEY, now=100, expires_at=700))
        self.assertEqual(self.rows(), [(KEY, 700)])
        self.assertEqual(tuple(self.conn.execute(f"SELECT typeof(tx_hash), typeof(expires_at) FROM {TABLE}").fetchone()),
                         ("text", "integer"))

    def test_returning_is_not_acceptance_until_the_caller_commits(self):
        self.begin()
        self.consume()
        self.assertTrue(self.conn.in_transaction)
        self.assertEqual(self.committed(), [])  # nobody else sees it yet
        self.conn.commit()
        self.assertEqual(self.committed(), [(KEY, 700)])

    def test_a_rolled_back_consumption_is_not_an_acceptance_and_the_key_is_unused_again(self):
        self.begin()
        self.consume()
        self.conn.rollback()
        self.assertEqual(self.committed(), [])
        self.accept()  # the same key, accepted now
        self.assertEqual(self.committed(), [(KEY, 700)])

    def test_a_committed_consumption_refuses_the_key_on_every_connection(self):
        self.accept()
        for conn in (self.conn, self.connect(), self.connect()):
            with self.subTest(conn=conn):
                self.begin(conn)
                with self.assertRaises(consumption.AlreadyConsumed):
                    self.consume(conn=conn)
                conn.rollback()
        self.assertEqual(self.committed(), [(KEY, 700)])

    def test_keys_are_independent_of_each_other(self):
        callback, link = key_for(VALUE), key_for(VALUE, consumption.PURPOSE_LINK_CONFIRM)
        self.begin()
        self.consume(callback)
        self.consume(link)  # the same value under the other purpose is another key
        self.consume(key_for("another-value"), expires_at=701)
        self.conn.commit()
        self.assertEqual(sorted(self.committed()),
                         sorted([(callback, 700), (link, 700), (key_for("another-value"), 701)]))

    def test_the_first_and_the_last_second_of_the_lifetime_are_accepted(self):
        self.accept(key_for("first"), now=0, expires_at=1)
        self.accept(key_for("last"), now=699, expires_at=700)
        self.accept(key_for("far"), now=2**62 - 2, expires_at=2**62 - 1)
        self.assertEqual(len(self.committed()), 1)  # the far call's cleanup removed the other two


class DuplicateTest(ConsumptionTestCase):
    def test_a_second_consumption_is_refused_with_the_same_and_with_a_later_time(self):
        self.accept(now=100, expires_at=700)
        for now in (100, 101, 400, 699):
            with self.subTest(now=now):
                self.begin()
                with self.assertRaises(consumption.AlreadyConsumed):
                    self.consume(now=now, expires_at=700)
                self.conn.commit()
        self.assertEqual(self.committed(), [(KEY, 700)])

    def test_it_is_refused_inside_the_transaction_that_recorded_it_too(self):
        self.begin()
        self.consume()
        with self.assertRaises(consumption.AlreadyConsumed):
            self.consume()
        self.conn.commit()
        self.assertEqual(self.committed(), [(KEY, 700)])

    def test_a_duplicate_never_replaces_the_stored_row(self):
        self.accept(now=100, expires_at=700)
        self.begin()
        for expires_at in (701, 5000, 650):  # whatever the second caller claims
            with self.subTest(expires_at=expires_at), self.assertRaises(consumption.AlreadyConsumed):
                self.consume(now=101, expires_at=expires_at)
        self.conn.commit()
        self.assertEqual(self.committed(), [(KEY, 700)])

    def test_the_refusal_is_not_a_database_error_and_names_no_key(self):
        self.accept()
        self.begin()
        with self.assertRaises(consumption.ConsumptionRefused) as refused:
            self.consume()
        self.assertIsInstance(refused.exception, consumption.AlreadyConsumed)
        self.assertNotIsInstance(refused.exception, sqlite3.Error)
        self.assertNotIsInstance(refused.exception, consumption.Expired)
        self.assertNotIn(KEY, str(refused.exception))
        self.assertNotIn(KEY, repr(refused.exception))

    def test_the_callers_earlier_writes_survive_and_the_transaction_stays_usable(self):
        self.accept()
        self.begin()
        self.note("before the refusal")
        with self.assertRaises(consumption.AlreadyConsumed):
            self.consume()
        self.assertTrue(self.conn.in_transaction)
        self.assertEqual(self.notes(), ["before the refusal"])  # still pending, not rolled back
        self.assertEqual(self.notes(self.connect()), [])        # and not committed either
        self.note("after the refusal")
        self.consume(key_for("another"))  # the transaction still records another key
        self.conn.commit()
        self.assertEqual(self.notes(self.connect()), ["before the refusal", "after the refusal"])
        self.assertEqual(sorted(self.committed()), sorted([(KEY, 700), (key_for("another"), 700)]))

    def test_after_a_refusal_the_caller_can_roll_everything_back(self):
        self.accept()
        self.begin()
        self.note("before the refusal")
        with self.assertRaises(consumption.AlreadyConsumed):
            self.consume()
        self.conn.rollback()
        self.assertEqual(self.notes(self.connect()), [])
        self.assertEqual(self.committed(), [(KEY, 700)])


class CallerOwnedTransactionTest(ConsumptionTestCase):
    def test_outside_a_transaction_it_refuses_to_work_and_begins_none(self):
        statements = self.traced()
        with self.assertRaises(RuntimeError):
            self.consume()
        self.assertFalse(self.conn.in_transaction)
        self.assertEqual(statements, [])
        self.assertEqual(self.committed(), [])

    def test_a_malformed_argument_is_reported_before_the_missing_transaction(self):
        with self.assertRaises(ValueError):
            self.consume(KEY.upper())
        with self.assertRaises(ValueError):
            self.consume(now=True)
        # A well-formed but expired call: the transaction is asked for before the expiry.
        with self.assertRaises(RuntimeError):
            self.consume(now=700, expires_at=700)

    def test_it_neither_commits_nor_rolls_back_whatever_the_outcome(self):
        self.accept()
        outcomes = {
            "success": (dict(key=key_for("fresh")), None),
            "already consumed": (dict(key=KEY), consumption.AlreadyConsumed),
            "expired": (dict(key=key_for("late"), now=700, expires_at=700), consumption.Expired),
            "malformed key": (dict(key=KEY.upper()), ValueError),
            "malformed time": (dict(key=key_for("fresh-2"), now=1.5), ValueError),
        }
        observer = self.connect()
        for label, (arguments, error) in outcomes.items():
            with self.subTest(label):
                self.begin()
                self.note(label)
                if error is None:
                    self.consume(**arguments)
                else:
                    with self.assertRaises(error):
                        self.consume(**arguments)
                # Not rolled back: the caller's own write is still there. Not committed:
                # no other connection sees it.
                self.assertTrue(self.conn.in_transaction)
                self.assertEqual(self.notes(), [label])
                self.assertEqual(self.notes(observer), [])
                self.conn.rollback()
        self.assertEqual(self.committed(), [(KEY, 700)])

    def test_several_consumptions_and_other_writes_form_one_transaction_of_the_caller(self):
        self.begin()
        self.note("one")
        self.consume(key_for("one"))
        self.consume(key_for("two"))
        self.conn.rollback()
        self.assertEqual((self.committed(), self.notes()), ([], []))
        self.begin()
        self.note("one")
        self.consume(key_for("one"))
        self.consume(key_for("two"))
        self.conn.commit()
        self.assertEqual(len(self.committed()), 2)
        self.assertEqual(self.notes(self.connect()), ["one"])

    def test_it_writes_no_log_record(self):
        self.accept()
        with self.assertNoLogs(level="DEBUG"):
            self.begin()
            self.consume(key_for("fresh"))
            for arguments, error in ((dict(key=KEY), consumption.AlreadyConsumed),
                                     (dict(key=KEY, now=700), consumption.Expired),
                                     (dict(key="not a key"), ValueError)):
                with self.assertRaises(error):
                    self.consume(**arguments)
            self.conn.commit()
            with self.assertRaises(RuntimeError):
                self.consume(key_for("no-transaction"))
            with self.assertRaises(ValueError):
                consumption.consumption_key(SECRET, "unknown", VALUE)


class ValidationTest(ConsumptionTestCase):
    def setUp(self):
        super().setUp()
        self.begin()
        self.statements = self.traced()

    def assert_refused_untouched(self, **arguments):
        with self.assertRaises(ValueError) as refused:
            self.consume(**arguments)
        self.assertIs(type(refused.exception), ValueError)
        self.assertEqual(self.statements, [])  # refused before any statement
        self.assertTrue(self.conn.in_transaction)
        return refused.exception

    def test_a_malformed_key_is_refused_before_any_statement(self):
        for label, key in MALFORMED_KEYS.items():
            with self.subTest(label):
                self.assert_refused_untouched(key=key)
        self.conn.commit()
        self.assertEqual(self.committed(), [])

    def test_a_time_must_be_a_plain_integer_from_zero_to_below_2_to_the_62(self):
        for label, value in MALFORMED_TIMES.items():
            with self.subTest(label, argument="now"):
                self.assert_refused_untouched(now=value, expires_at=2**62 - 1)
            with self.subTest(label, argument="expires_at"):
                self.assert_refused_untouched(now=0, expires_at=value)
        self.conn.commit()
        self.assertEqual(self.committed(), [])

    def test_a_boolean_is_never_a_time_although_the_column_would_store_it(self):
        # True is an int to Python and SQLite would store it as 1: refused here.
        self.assert_refused_untouched(now=False, expires_at=True)
        self.assert_refused_untouched(now=0, expires_at=True)
        self.assert_refused_untouched(now=True, expires_at=700)
        self.conn.rollback()

    def test_the_largest_and_the_smallest_times_are_accepted(self):
        self.consume(key_for("smallest"), now=0, expires_at=1)
        self.consume(now=2**62 - 2, expires_at=2**62 - 1)
        self.conn.commit()
        self.assertEqual(self.committed(), [(KEY, 2**62 - 1)])  # the second call's cleanup removed the first row

    def test_no_error_message_repeats_the_key(self):
        marker = "abcdef0123456789" * 4
        for key in (marker.upper(), marker + "0", marker[:63]):
            with self.subTest(key=key):
                error = self.assert_refused_untouched(key=key)
                self.assertNotIn(marker[:16], str(error).lower())
        error = self.assert_refused_untouched(key=marker, now=-12345678)
        self.assertNotIn("12345678", str(error))
        self.assertNotIn(marker, str(error))
        self.conn.rollback()


class ExpiryBeforeAnythingElseTest(ConsumptionTestCase):
    def test_at_and_after_the_expiry_the_key_is_refused_and_one_second_before_it_is_accepted(self):
        for now in (700, 701, 5000):
            with self.subTest(now=now):
                self.begin()
                with self.assertRaises(consumption.Expired):
                    self.consume(now=now, expires_at=700)
                self.conn.commit()
        self.assertEqual(self.committed(), [])
        self.accept(now=699, expires_at=700)
        self.assertEqual(self.committed(), [(KEY, 700)])

    def test_an_expired_call_executes_no_statement(self):
        self.begin()
        statements = self.traced()
        for now, expires_at in ((700, 700), (701, 700), (0, 0), (2**62 - 1, 1)):
            with self.subTest(now=now, expires_at=expires_at), self.assertRaises(consumption.Expired):
                self.consume(now=now, expires_at=expires_at)
        self.assertEqual(statements, [])
        self.assertTrue(self.conn.in_transaction)

    def test_it_is_decided_without_the_table(self):
        # No statement can have run: on a database without the table an expired call
        # is still "expired", while a live one fails on the missing table.
        self.conn.execute(f"DROP TABLE {TABLE}")
        self.conn.commit()
        self.begin()
        with self.assertRaises(consumption.Expired):
            self.consume(now=700, expires_at=700)
        with self.assertRaises(sqlite3.OperationalError):
            self.consume(now=699, expires_at=700)

    def test_an_expired_call_cleans_nothing_up(self):
        self.accept(key_for("old"), now=100, expires_at=110)
        self.begin()
        with self.assertRaises(consumption.Expired):
            self.consume(key_for("late"), now=5000, expires_at=4000)  # cleanup at 5000 would remove "old"
        self.conn.commit()
        self.assertEqual(self.committed(), [(key_for("old"), 110)])

    def test_an_expired_key_stays_expired_and_is_not_a_database_error(self):
        self.accept(now=100, expires_at=110)
        self.begin()
        with self.assertRaises(consumption.ConsumptionRefused) as refused:
            self.consume(now=110, expires_at=110)  # recorded and expired: expiry answers first
        self.assertIsInstance(refused.exception, consumption.Expired)
        self.assertNotIsInstance(refused.exception, (consumption.AlreadyConsumed, sqlite3.Error))
        self.assertNotIn(KEY, str(refused.exception))
        self.conn.rollback()

    def test_the_callers_earlier_writes_survive_an_expired_call(self):
        self.begin()
        self.note("before")
        with self.assertRaises(consumption.Expired):
            self.consume(now=700, expires_at=700)
        self.consume(key_for("live"))
        self.conn.commit()
        self.assertEqual(self.notes(self.connect()), ["before"])
        self.assertEqual(self.committed(), [(key_for("live"), 700)])


class CleanupTest(ConsumptionTestCase):
    NOW = 1000

    def record(self, expiries, conn=None, recorded_at=900):
        keys = {}
        for expires_at in expiries:
            keys[expires_at] = key_for(f"expires-{expires_at}-{len(keys)}-{id(conn)}")
            self.accept(keys[expires_at], now=recorded_at, expires_at=expires_at, conn=conn)
        return keys

    def test_a_row_goes_61_seconds_after_its_expiry_and_not_at_60(self):
        keys = self.record([self.NOW - 62, self.NOW - 61, self.NOW - 60, self.NOW - 59, self.NOW, self.NOW + 500])
        self.assertEqual(len(self.committed()), 6)
        self.accept(key_for("fresh"), now=self.NOW, expires_at=self.NOW + 600)
        self.assertEqual({expires_at for _key, expires_at in self.committed()},
                         {self.NOW - 60, self.NOW - 59, self.NOW, self.NOW + 500, self.NOW + 600})
        self.assertNotIn((keys[self.NOW - 61], self.NOW - 61), self.committed())
        self.assertIn((keys[self.NOW - 60], self.NOW - 60), self.committed())

    def test_expiry_is_the_only_thing_that_decides_whichever_connection_wrote_the_row(self):
        writers = [self.conn, self.connect(), self.connect()]
        for writer in writers:
            self.record([self.NOW - 61, self.NOW - 60], conn=writer)
        for cleaner in writers:  # every "instance" cleans up, and each leaves the same rows
            self.accept(key_for(f"fresh-{id(cleaner)}"), now=self.NOW, expires_at=self.NOW + 600, conn=cleaner)
            self.assertEqual(collections.Counter(expires_at for _key, expires_at in self.committed()
                                                 if expires_at < self.NOW),
                             {self.NOW - 60: 3})

    def test_nothing_is_cleaned_up_during_the_first_minute_of_the_clock(self):
        self.accept(key_for("early"), now=0, expires_at=1)
        for now in (1, 30, 60, 61):
            self.accept(key_for(f"at-{now}"), now=now, expires_at=now + 600)
            self.assertIn((key_for("early"), 1), self.committed(), now)
        self.accept(key_for("at-62"), now=62, expires_at=662)
        self.assertNotIn((key_for("early"), 1), self.committed())

    def test_cleanup_belongs_to_the_callers_transaction(self):
        keys = self.record([self.NOW - 61])
        self.begin()
        self.consume(key_for("fresh"), now=self.NOW, expires_at=self.NOW + 600)
        self.assertEqual(self.rows(), [(key_for("fresh"), self.NOW + 600)])
        self.assertEqual(self.committed(), [(keys[self.NOW - 61], self.NOW - 61)])  # not committed by the service
        self.conn.rollback()
        self.assertEqual(self.committed(), [(keys[self.NOW - 61], self.NOW - 61)])

    def test_the_cleanup_of_a_refused_duplicate_stays_pending_for_the_caller(self):
        keys = self.record([self.NOW - 61, self.NOW + 500])
        self.begin()
        with self.assertRaises(consumption.AlreadyConsumed):
            self.consume(keys[self.NOW + 500], now=self.NOW, expires_at=self.NOW + 500)
        self.assertEqual(self.rows(), [(keys[self.NOW + 500], self.NOW + 500)])
        self.conn.commit()
        self.assertEqual(self.committed(), [(keys[self.NOW + 500], self.NOW + 500)])

    def test_rows_with_expiries_of_an_earlier_boot_are_inert_and_go_by_the_same_rule(self):
        # After a reboot the monotonic clock starts near zero; rows of the earlier boot
        # carry expiries far above it.
        earlier = self.record([5_000_000, 5_000_300], recorded_at=4_999_900)
        self.accept(key_for("after-reboot"), now=40, expires_at=640)
        self.assertEqual(len(self.committed()), 3)
        self.accept(key_for("days-later"), now=600_000, expires_at=600_600)
        self.assertLessEqual(set(earlier.values()), {key for key, _expires_at in self.committed()})
        self.accept(key_for("at-the-boundary"), now=5_000_060, expires_at=5_000_660)
        self.assertIn((earlier[5_000_000], 5_000_000), self.committed())
        self.accept(key_for("past-the-first"), now=5_000_061, expires_at=5_000_661)
        self.assertNotIn((earlier[5_000_000], 5_000_000), self.committed())
        self.assertIn((earlier[5_000_300], 5_000_300), self.committed())


class ReplayAcrossConnectionsTest(ConsumptionTestCase):
    """Two connections stand for two instances of the service on one database."""

    def test_another_instances_consumption_does_not_free_a_key(self):
        a, b = self.conn, self.connect()
        first, second = key_for("first"), key_for("second")
        self.accept(first, now=100, expires_at=700, conn=a)
        self.accept(second, now=150, expires_at=750, conn=b)
        self.begin(a)
        with self.assertRaises(consumption.AlreadyConsumed):
            self.consume(first, now=151, expires_at=700, conn=a)
        a.rollback()
        self.assertIn((first, 700), self.committed())

    def test_a_recorded_key_is_never_accepted_again_whenever_it_is_replayed(self):
        # Recorded at 100 with expiry 120. From then on another instance consumes a
        # fresh key every second (so its cleanup runs every second) and the key is
        # replayed, with the one expiry it has, by both instances.
        a, b = self.conn, self.connect()
        key, recorded_at, expires_at = key_for("replayed"), 100, 120
        self.accept(key, now=recorded_at, expires_at=expires_at, conn=a)
        gone_at = None
        for now in range(recorded_at, expires_at + ALLOWANCE + 10):
            self.accept(key_for(f"other-{now}"), now=now, expires_at=now + 600, conn=b)
            present = (key, expires_at) in self.committed()
            if not present and gone_at is None:
                gone_at = now
            expected = consumption.AlreadyConsumed if now < expires_at else consumption.Expired
            for replayer in (a, b):
                with self.subTest(now=now, replayer="a" if replayer is a else "b"):
                    self.begin(replayer)
                    with self.assertRaises(expected):
                        self.consume(key, now=now, expires_at=expires_at, conn=replayer)
                    replayer.commit()
            # While the key could still pass the expiry check, its row is there.
            if now < expires_at:
                self.assertTrue(present, now)
        # The row outlived the key's whole lifetime by the allowance, and no longer.
        self.assertEqual(gone_at, expires_at + ALLOWANCE + 1)
        self.assertNotIn(key, {stored for stored, _expires_at in self.committed()})

    def test_the_row_of_a_key_is_present_at_every_time_at_which_the_key_is_not_expired(self):
        # Whatever the other instance's clock reading, up to the key's expiry plus the allowance.
        a, b = self.conn, self.connect()
        key = key_for("kept")
        self.accept(key, now=100, expires_at=130, conn=a)
        for now in (100, 129, 130, 131, 130 + ALLOWANCE):
            with self.subTest(now=now):
                self.accept(key_for(f"other-{now}"), now=now, expires_at=now + 600, conn=b)
                self.assertIn((key, 130), self.committed())
        self.accept(key_for("other-last"), now=130 + ALLOWANCE + 1, expires_at=5000, conn=b)
        self.assertNotIn((key, 130), self.committed())


class RandomisedModelTest(ConsumptionTestCase):
    """Several connections take turns under ``BEGIN IMMEDIATE`` with one clock that never
    goes back, each key with the one expiry it ever has. Seeded: every run is the same."""

    SEEDS = (20261012, 1, 7, 42)
    STEPS = 250

    def run_model(self, seed):
        rng = random.Random(seed)
        path = build_test_database(self.root / f"model-{seed}.db")  # every run starts from an empty table
        connections = [self.connect(path) for _ in range(3)]
        expiry_of = {}                        # key -> its one, fixed expiry
        table = {}                            # the model of the committed table
        accepted = collections.Counter()      # committed acceptances per key
        seen = collections.Counter()
        now = rng.randrange(0, 40)
        for step in range(self.STEPS):
            now += rng.choice((0, 0, 0, 1, 1, 2, 5, 20, 45, 90))
            conn = rng.choice(connections)
            if expiry_of and rng.random() < 0.6:
                # A replay, or a retry of a rolled-back key: a recent key as often as any key.
                known = list(expiry_of)
                key = rng.choice(known[-6:] if rng.random() < 0.5 else known)
            else:
                key = key_for(f"model-{seed}-{step}")
                expiry_of[key] = now + rng.choice((1, 3, 30, 120, 600))
            expires_at = expiry_of[key]
            commit = rng.random() < 0.8

            if now >= expires_at:
                expected, after = consumption.Expired, table
            else:
                after = {k: e for k, e in table.items() if not e < now - ALLOWANCE}
                if key in after:
                    expected = consumption.AlreadyConsumed
                else:
                    expected, after = None, {**after, key: expires_at}
            if accepted[key] and key not in table:
                seen["replay after its row was cleaned up"] += 1
                self.assertIs(expected, consumption.Expired)

            conn.execute("BEGIN IMMEDIATE")   # the lock first; `now` is the reading taken under it
            try:
                consumption.consume(conn, key, now=now, expires_at=expires_at)
                outcome = None
            except consumption.ConsumptionRefused as refused:
                outcome = type(refused)
            self.assertIs(outcome, expected, f"seed {seed}, step {step}")
            seen[outcome] += 1
            if commit:
                conn.commit()
                table = after
                if outcome is None:
                    accepted[key] += 1
                    self.assertEqual(accepted[key], 1, f"seed {seed}, step {step}: accepted twice")
            else:
                conn.rollback()
            if step % 25 == 0 or step == self.STEPS - 1:
                self.assertEqual(self.rows(rng.choice(connections)), sorted(table.items()))
        return accepted, seen

    def test_no_key_is_ever_accepted_twice_and_every_outcome_matches_the_model(self):
        totals = collections.Counter()
        for seed in self.SEEDS:
            with self.subTest(seed=seed):
                accepted, seen = self.run_model(seed)
                self.assertEqual(set(accepted.values()), {1})
                totals.update(seen)
                totals["accepted keys"] += len(accepted)
        # The runs really went through every path, the one where cleanup had removed
        # the row of an accepted key included.
        for outcome in (None, consumption.AlreadyConsumed, consumption.Expired,
                        "replay after its row was cleaned up", "accepted keys"):
            self.assertGreater(totals[outcome], 100, outcome)


class ConcurrentConsumptionTest(ConsumptionTestCase):
    """Real threads, one connection each, on one database file."""

    THREADS = 12
    ROUNDS = 6

    def setUp(self):
        super().setUp()
        self._tick = 100
        self._clock_lock = threading.Lock()

    def clock(self):
        """A shared clock that never goes back, as time.monotonic() is for the workers."""
        with self._clock_lock:
            self._tick += 1
            return self._tick

    def race(self, keys, expires_at):
        """One caller per key, all released together; returns what each of them got.

        A worker does what a caller does: lock, then clock, ``consume``, commit. Anything
        but an acceptance or ``AlreadyConsumed`` is handed back as the exception it was.
        """
        barrier = threading.Barrier(len(keys))
        results = [None] * len(keys)

        def worker(index, key):
            try:
                conn = connect_db(self.path, timeout=60)
                try:
                    barrier.wait(timeout=60)
                    conn.execute("BEGIN IMMEDIATE")
                    now = self.clock()
                    try:
                        consumption.consume(conn, key, now=now, expires_at=expires_at)
                    except consumption.AlreadyConsumed:
                        conn.rollback()
                        results[index] = "already consumed"
                    else:
                        conn.commit()
                        results[index] = "accepted"
                finally:
                    conn.close()
            except BaseException as error:  # noqa: BLE001 - reported by the test, never swallowed
                results[index] = error

        threads = [threading.Thread(target=worker, args=(index, key), daemon=True) for index, key in enumerate(keys)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=120)
        self.assertFalse([thread for thread in threads if thread.is_alive()], "a worker did not finish")
        for result in results:
            if isinstance(result, BaseException):
                raise result  # an unexpected error is the test's error, with its own traceback
        return results

    def assert_one_winner_in_every_round(self):
        for round_number in range(self.ROUNDS):
            with self.subTest(round=round_number):
                key = key_for(f"raced-{round_number}")
                results = self.race([key] * self.THREADS, expires_at=100_000)
                self.assertEqual(sorted(results), ["accepted"] + ["already consumed"] * (self.THREADS - 1))
                self.assertEqual([row for row in self.committed() if row[0] == key], [(key, 100_000)])
        self.assertEqual(len(self.committed()), self.ROUNDS)

    def test_of_many_simultaneous_callers_of_one_key_exactly_one_is_accepted(self):
        self.assertEqual(self.conn.execute("PRAGMA journal_mode").fetchone()[0], "delete")
        self.assert_one_winner_in_every_round()

    def test_the_same_holds_in_wal_mode_which_a_live_database_uses(self):
        self.assertEqual(self.conn.execute("PRAGMA journal_mode = WAL").fetchone()[0], "wal")
        self.assert_one_winner_in_every_round()

    def test_simultaneous_callers_of_different_keys_are_all_accepted(self):
        keys = [key_for(f"distinct-{index}") for index in range(self.THREADS)]
        self.assertEqual(self.race(keys, expires_at=100_000), ["accepted"] * self.THREADS)
        self.assertEqual(self.committed(), sorted((key, 100_000) for key in keys))

    def test_an_unexpected_error_of_a_worker_is_raised_not_counted_as_a_refusal(self):
        self.conn.execute(f"DROP TABLE {TABLE}")
        self.conn.commit()
        with self.assertRaises(sqlite3.OperationalError):
            self.race([KEY] * 4, expires_at=100_000)


class StorageFailureTest(ConsumptionTestCase):
    """A storage failure is an error of its own: never a refusal, never an acceptance."""

    def assert_storage_error(self, conn, expected=sqlite3.OperationalError):
        with self.assertRaises(expected) as failed:
            self.consume(conn=conn)
        self.assertIsInstance(failed.exception, sqlite3.Error)
        self.assertNotIsInstance(failed.exception, consumption.ConsumptionRefused)
        return failed.exception

    def test_a_missing_table(self):
        self.conn.execute(f"DROP TABLE {TABLE}")
        self.conn.commit()
        self.begin()
        self.note("before")
        error = self.assert_storage_error(self.conn)
        self.assertIn("no such table", str(error))
        self.assertEqual(self.notes(), ["before"])  # the caller decides what happens to its transaction
        self.conn.rollback()

    def test_a_query_only_connection(self):
        reader = self.connect(query_only=True)
        with self.assertRaises(sqlite3.OperationalError):
            reader.execute("BEGIN IMMEDIATE")  # a caller that takes the lock first never gets this far
        reader.execute("BEGIN")
        error = self.assert_storage_error(reader)
        self.assertIn("readonly", str(error))
        reader.rollback()
        self.assertEqual(self.committed(), [])

    def test_a_write_lock_held_past_the_busy_timeout(self):
        holder = self.begin()
        waiting = self.connect(timeout=0.05)
        with self.assertRaises(sqlite3.OperationalError):
            waiting.execute("BEGIN IMMEDIATE")  # what a caller sees: it never reaches consume
        waiting.execute("BEGIN")  # a transaction without the write lock
        error = self.assert_storage_error(waiting)
        self.assertIn("locked", str(error))
        self.assertTrue(waiting.in_transaction)  # left for the caller to finish
        waiting.rollback()
        holder.rollback()
        self.assertEqual(self.committed(), [])
        self.accept(conn=waiting)  # nothing was recorded: the key is still unused
        self.assertEqual(self.committed(), [(KEY, 700)])

    def test_a_commit_that_fails_is_not_an_acceptance(self):
        # Returning is half of an acceptance. A deferred foreign-key violation of the
        # caller's own makes the COMMIT fail, and then nothing was recorded.
        self.begin()
        self.conn.execute("PRAGMA defer_foreign_keys = ON")
        self.conn.execute("INSERT INTO user_identities (user_id, issuer, subject, provider) "
                          "VALUES (999999, 'https://issuer.example', 'subject', 'google')")
        self.consume()
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.commit()
        self.conn.rollback()
        self.assertEqual(self.committed(), [])


class OnlyTheDuplicateIsAlreadyConsumedTest(ConsumptionTestCase):
    """Other integrity failures are storage faults and propagate as what they are."""

    DUPLICATE_TEXT = f"UNIQUE constraint failed: {TABLE}.tx_hash"

    def assert_integrity_error(self, key, **arguments):
        with self.assertRaises(sqlite3.IntegrityError) as failed:
            self.consume(key, **arguments)
        self.assertNotIsInstance(failed.exception, consumption.ConsumptionRefused)
        return failed.exception

    def test_the_real_duplicate_carries_the_text_the_other_tests_imitate(self):
        self.accept()
        self.begin()
        with self.assertRaises(consumption.AlreadyConsumed) as refused:
            self.consume()
        self.assertEqual(str(refused.exception.__cause__), self.DUPLICATE_TEXT)
        self.conn.rollback()

    def test_another_unique_constraint_failing_is_not_a_replay(self):
        self.conn.execute(f"CREATE UNIQUE INDEX one_row_per_expiry ON {TABLE}(expires_at)")
        self.conn.commit()
        self.accept(key_for("first"), expires_at=700)
        self.begin()
        self.assert_integrity_error(key_for("second"), expires_at=700)  # a fresh key
        self.conn.commit()
        self.assertEqual(self.committed(), [(key_for("first"), 700)])

    def test_a_trigger_that_fails_is_not_a_replay_even_with_the_duplicates_own_words(self):
        self.accept()
        self.conn.execute(f"CREATE TRIGGER refuse_every_insert BEFORE INSERT ON {TABLE} "
                          f"BEGIN SELECT RAISE(ABORT, '{self.DUPLICATE_TEXT}'); END")
        self.conn.commit()
        self.begin()
        error = self.assert_integrity_error(key_for("fresh"))        # nothing is stored for it
        self.assertEqual(str(error), self.DUPLICATE_TEXT)
        self.assert_integrity_error(KEY)                             # and a row for this one exists
        self.conn.commit()
        self.assertEqual(self.committed(), [(KEY, 700)])

    def test_an_integrity_error_without_a_result_code_is_not_a_replay(self):
        # The class of the error and its text decide nothing: without SQLite's own
        # result code for "duplicate primary key" the error propagates.
        class Connection:
            in_transaction = True
            statements = []

            def execute(self, sql, parameters=()):
                self.statements.append(sql.split()[0])
                if sql.startswith("INSERT"):
                    raise sqlite3.IntegrityError(OnlyTheDuplicateIsAlreadyConsumedTest.DUPLICATE_TEXT)

        connection = Connection()
        self.assert_integrity_error(KEY, conn=connection)
        self.assertEqual(connection.statements, ["DELETE", "INSERT"])


class WhatTheStorageDoesNotEnforceTest(ConsumptionTestCase):
    """Not requirements: the measured limits that the caller obligations exist for.

    Each test breaks one obligation and shows that the storage alone then accepts a key
    a second time. They keep the documentation honest; none of them is a behaviour to
    rely on, and a stricter storage would change them.
    """

    def test_a_clock_reading_taken_before_the_lock_is_covered_by_the_allowance_only(self):
        a, b = self.conn, self.connect()
        key = key_for("stale")
        self.accept(key, now=100, expires_at=110, conn=a)
        # Another caller's clock is 60 s past the expiry: the row is kept, so a reading
        # that is 61 s stale is still refused.
        self.accept(key_for("other-1"), now=110 + ALLOWANCE, expires_at=5000, conn=b)
        self.begin(a)
        with self.assertRaises(consumption.AlreadyConsumed):
            self.consume(key, now=109, expires_at=110, conn=a)
        a.rollback()
        # One second later the row is cleaned up, and a reading that is 62 s stale gets in.
        self.accept(key_for("other-2"), now=110 + ALLOWANCE + 1, expires_at=5000, conn=b)
        self.accept(key, now=109, expires_at=110, conn=a)
        # Read under the lock, as obligation 1 demands, the same replay is refused.
        self.begin(b)
        with self.assertRaises(consumption.Expired):
            self.consume(key, now=110 + ALLOWANCE + 1, expires_at=110, conn=b)
        b.rollback()

    def test_an_expiry_that_changes_between_calls_lets_the_key_in_again(self):
        self.accept(now=100, expires_at=110)
        # The call's own cleanup removes the row before its INSERT (obligation 3).
        self.accept(now=110 + ALLOWANCE + 1, expires_at=5000)
        self.assertEqual(self.committed(), [(KEY, 5000)])

    def test_an_unused_object_of_an_earlier_boot_is_accepted_by_the_storage_alone(self):
        # Issued at 4_999_900 of the earlier boot's clock and never used; the new boot's
        # clock reads 40. Only the caller's check of the issuing run refuses it (obligation 4).
        self.accept(key_for("issued-before-the-reboot"), now=40, expires_at=5_000_500)
        self.assertEqual(len(self.committed()), 1)

    def test_a_row_deleted_by_anything_else_frees_its_key(self):
        self.accept()
        self.conn.execute(f"DELETE FROM {TABLE}")  # what obligation 8 forbids
        self.conn.commit()
        self.accept()
        self.assertEqual(self.committed(), [(KEY, 700)])

    def test_a_transaction_that_does_not_hold_the_write_lock_yet_is_not_told_apart(self):
        self.conn.execute("BEGIN")  # deferred: no write lock until the first write (obligation 1)
        self.consume()
        self.conn.commit()
        self.assertEqual(self.committed(), [(KEY, 700)])


class StorageOnlyTest(unittest.TestCase):
    """The scope of this PR: a small storage module that nothing calls yet."""

    def test_the_public_api_is_two_functions_three_refusals_and_the_constants(self):
        public = {name for name, value in vars(consumption).items()
                  if not name.startswith("_") and (inspect.isfunction(value) or inspect.isclass(value))}
        self.assertEqual(public, {"ConsumptionRefused", "AlreadyConsumed", "Expired", "consumption_key", "consume"})
        constants = {name for name in vars(consumption) if name.isupper() and not name.startswith("_")}
        self.assertEqual(constants, {"PURPOSE_CALLBACK", "PURPOSE_LINK_CONFIRM", "PURPOSES", "RETENTION_ALLOWANCE"})
        self.assertEqual(consumption.RETENTION_ALLOWANCE, ALLOWANCE)
        self.assertTrue(issubclass(consumption.AlreadyConsumed, consumption.ConsumptionRefused))
        self.assertTrue(issubclass(consumption.Expired, consumption.ConsumptionRefused))
        self.assertFalse(issubclass(consumption.ConsumptionRefused, (sqlite3.Error, ValueError, RuntimeError)))
        self.assertEqual(list(inspect.signature(consumption.consume).parameters), ["conn", "key", "now", "expires_at"])
        self.assertEqual(
            [parameter.kind for parameter in inspect.signature(consumption.consume).parameters.values()][2:],
            [inspect.Parameter.KEYWORD_ONLY] * 2)

    def test_it_imports_the_standard_library_only_and_no_clock_or_logger(self):
        tree = ast.parse(MODULE.read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                self.assertEqual(node.level, 0)  # no relative import of the application
                imported.add((node.module or "").split(".")[0])
        self.assertLessEqual(imported, set(sys.stdlib_module_names))
        self.assertEqual(imported & {"time", "datetime", "logging", "flask", "web", "src"}, set())

    def test_importing_it_loads_nothing_of_the_application(self):
        code = (
            "import sys; before = set(sys.modules); import web.services.oidc_consumption; "
            "print(sorted(m for m in set(sys.modules) - before "
            "if m.split('.')[0] in ('web', 'src', 'flask', 'werkzeug', 'authlib', 'requests', 'jwt')))"
        )
        out = subprocess.run([sys.executable, "-B", "-c", code], cwd=ROOT, capture_output=True, text=True, check=True)
        self.assertEqual(out.stdout.strip(), "['web', 'web.services', 'web.services.oidc_consumption']")

    def test_only_these_modules_use_the_storage(self):
        # Google sign-in (PR #331) records consumptions at three call sites, in two
        # modules: the callback hook and the retirement (web/oauth_state.py), and the
        # confirmation of a link (web/services/oidc_accounts.py). No route, blueprint,
        # template or tool names the storage or its table.
        allowed = {Path("web/services/oidc_consumption.py"),
                   Path("web/migrations/020_oidc_consumed_transactions.sql"),
                   Path("src/canonical_tables.py")}
        importers = ["web/oauth_state.py", "web/services/oidc_accounts.py"]
        pattern = re.compile(r"oidc_consum")
        users = []
        for directory in ("web", "src", "deploy"):
            for path in sorted((ROOT / directory).rglob("*")):
                relative = path.relative_to(ROOT)
                if (not path.is_file() or relative in allowed or "__pycache__" in path.parts
                        or path.suffix not in {".py", ".html", ".sql", ".sh", ".txt", ".conf", ".service", ".js"}):
                    continue
                if pattern.search(path.read_text(encoding="utf-8", errors="replace")):
                    users.append(str(relative))
        self.assertEqual(users, importers)
        for relative in importers:
            source = (ROOT / relative).read_text(encoding="utf-8")
            # They call the service. Neither builds a key itself, names the table or
            # deletes a row: rows leave by the cleanup inside consume() and nothing else.
            self.assertIn("oidc_consumption.consumption_key(", source)
            self.assertIn("oidc_consumption.consume(", source)
            for forbidden in ("oidc_consumed_transactions", "tx_hash", "hmac.new(key, b\"oidc-consume"):
                self.assertNotIn(forbidden, source, relative)


if __name__ == "__main__":
    unittest.main()
