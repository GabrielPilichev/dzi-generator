"""External identity storage (``web.services.identities``).

Exact ``(issuer, subject)`` keys, one owner per identity, email as metadata only and
transactions owned by the caller. Every test works on its own temporary database cloned
by ``tests.support.build_test_database`` (migrations + canonical content, no runtime
rows) and opened under the application's connection policy (``web.db.connect_db``:
foreign keys on, ``sqlite3.Row`` rows). Nothing reads ``data/questions.db``.
"""

import inspect
import re
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.support import ROOT, build_test_database
from web.db import connect_db
from web.services import accounts, identities

GOOGLE = "https://accounts.google.com"
MICROSOFT = "https://login.microsoftonline.com/9188040d-6c67-4c5b-b112-36a304b66dad/v2.0"
SUBJECT = "110169484474386276334"
ANA_HASH = "scrypt:32768:8:1$fixturesalt$" + "c3" * 64
TIMESTAMP = r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d$"
MODULE = ROOT / "web" / "services" / "identities.py"

MALFORMED_KEYS = (
    "", " ", "   ", None, 123, 12.5, True, b"https://accounts.google.com", ["x"],
    " padded", "padded ", "\tpadded", "padded\n", "in ner", "in\tner", "in\nner",
    "nul\x00byte", "del\x7fchar", "escape\x1b", "ñandú", "ｓｕｂ", "zero\u200bwidth", "nbsp\u00a0x",
    "\ud800", "x" * 256,
)


class IdentitiesTestCase(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="identities-service-")
        self.addCleanup(directory.cleanup)
        self.path = build_test_database(Path(directory.name) / "identities.db")
        self.conn = self.connect()
        self.ana = accounts.create_user(self.conn, display_name="Ана Иванова", email="ana@example.bg",
                                        role="teacher")
        self.boris = accounts.create_user(self.conn, display_name="Борис Петров", email="boris@example.bg")
        # A credential that must never change here (a fixed value: no scrypt work per test).
        self.conn.execute("UPDATE users SET password_hash = ?, password_changed_at = '2026-01-01 00:00:00' "
                          "WHERE id = ?", (ANA_HASH, self.ana))
        self.conn.commit()

    def connect(self):
        conn = connect_db(self.path)
        self.addCleanup(conn.close)
        return conn

    def add(self, user_id=None, *, issuer=GOOGLE, subject=SUBJECT, provider="google", email_at_link=None):
        return identities.add_identity(
            self.conn, self.ana if user_id is None else user_id,
            issuer=issuer, subject=subject, provider=provider, email_at_link=email_at_link)

    def rows(self, conn=None):
        return [tuple(row) for row in (conn or self.conn).execute("SELECT * FROM user_identities ORDER BY id")]

    def users(self, conn=None):
        return [tuple(row) for row in (conn or self.conn).execute("SELECT * FROM users ORDER BY id")]

    def traced(self):
        """SQL statements the connection runs from now on."""
        statements = []
        self.conn.set_trace_callback(statements.append)
        self.addCleanup(self.conn.set_trace_callback, None)
        return statements


class ExactLookupTest(IdentitiesTestCase):
    def test_an_added_identity_is_found_by_its_exact_pair(self):
        identity_id = self.add(email_at_link="ana.private@example.org")
        found = identities.find_identity(self.conn, GOOGLE, SUBJECT)
        self.assertEqual(list(found), ["id", "user_id", "issuer", "subject", "provider", "email_at_link",
                                       "created_at", "last_login_at"])
        self.assertEqual((found["id"], found["user_id"], found["issuer"], found["subject"], found["provider"],
                          found["email_at_link"], found["last_login_at"]),
                         (identity_id, self.ana, GOOGLE, SUBJECT, "google", "ana.private@example.org", None))
        self.assertRegex(found["created_at"], TIMESTAMP)

    def test_an_unknown_pair_finds_nothing(self):
        self.add()
        self.assertIsNone(identities.find_identity(self.conn, GOOGLE, "another-subject"))
        self.assertIsNone(identities.find_identity(self.conn, MICROSOFT, SUBJECT))
        self.assertIsNone(identities.find_identity(self.conn, SUBJECT, GOOGLE))  # the pair is ordered

    def test_lookup_is_case_sensitive(self):
        self.add(issuer=GOOGLE, subject="AbCdEf")
        for issuer, subject in ((GOOGLE, "abcdef"), (GOOGLE, "ABCDEF"), (GOOGLE, "abCdEf"),
                                (GOOGLE.upper(), "AbCdEf"), ("https://Accounts.Google.com", "AbCdEf")):
            with self.subTest(issuer=issuer, subject=subject):
                self.assertIsNone(identities.find_identity(self.conn, issuer, subject))
        self.assertEqual(identities.find_identity(self.conn, GOOGLE, "AbCdEf")["user_id"], self.ana)

    def test_lookup_neither_trims_nor_normalizes(self):
        self.add()
        near_misses = (
            (GOOGLE + "/", SUBJECT),                    # a trailing slash is another issuer
            (GOOGLE.replace("https://", "http://"), SUBJECT),
            (GOOGLE.replace("https://", ""), SUBJECT),
            (" " + GOOGLE, SUBJECT), (GOOGLE + " ", SUBJECT),
            (GOOGLE, " " + SUBJECT), (GOOGLE, SUBJECT + " "), (GOOGLE, SUBJECT + "\n"),
            (GOOGLE, "0" + SUBJECT),                    # a number would be equal; text is not
            (GOOGLE, SUBJECT.translate({ord("1"): "１"})),  # compatibility forms are not folded
            (GOOGLE, int(SUBJECT)),                     # not a string: never coerced to one
        )
        for issuer, subject in near_misses:
            with self.subTest(issuer=issuer, subject=subject):
                self.assertIsNone(identities.find_identity(self.conn, issuer, subject))

    def test_a_malformed_key_finds_nothing_and_runs_no_query(self):
        self.add()
        statements = self.traced()
        for bad in MALFORMED_KEYS:
            with self.subTest(bad=bad):
                self.assertIsNone(identities.find_identity(self.conn, bad, SUBJECT))
                self.assertIsNone(identities.find_identity(self.conn, GOOGLE, bad))
        self.assertEqual(statements, [])

    def test_case_variants_are_different_identities(self):
        upper = self.add(self.ana, subject="AbCdEf")
        lower = self.add(self.boris, subject="abcdef")
        self.assertNotEqual(upper, lower)
        self.assertEqual(identities.find_identity(self.conn, GOOGLE, "AbCdEf")["user_id"], self.ana)
        self.assertEqual(identities.find_identity(self.conn, GOOGLE, "abcdef")["user_id"], self.boris)
        self.assertEqual(len(self.rows()), 2)

    def test_the_same_subject_under_another_issuer_is_another_identity(self):
        google = self.add(self.ana, issuer=GOOGLE, subject=SUBJECT, provider="google")
        microsoft = self.add(self.boris, issuer=MICROSOFT, subject=SUBJECT, provider="microsoft")
        self.assertNotEqual(google, microsoft)
        self.assertEqual(identities.find_identity(self.conn, GOOGLE, SUBJECT)["user_id"], self.ana)
        self.assertEqual(identities.find_identity(self.conn, MICROSOFT, SUBJECT)["user_id"], self.boris)
        # Two tenants of one provider are two issuers as well.
        other_tenant = MICROSOFT.replace("9188040d", "00000000")
        self.add(self.ana, issuer=other_tenant, subject=SUBJECT, provider="microsoft")
        self.assertEqual(identities.find_identity(self.conn, MICROSOFT, SUBJECT)["user_id"], self.boris)
        self.assertEqual(identities.find_identity(self.conn, other_tenant, SUBJECT)["user_id"], self.ana)
        self.assertEqual(len(self.rows()), 3)

    def test_an_issuer_that_differs_by_a_trailing_slash_is_another_identity(self):
        self.add(self.ana, issuer=GOOGLE)
        self.add(self.boris, issuer=GOOGLE + "/")
        self.assertEqual(identities.find_identity(self.conn, GOOGLE, SUBJECT)["user_id"], self.ana)
        self.assertEqual(identities.find_identity(self.conn, GOOGLE + "/", SUBJECT)["user_id"], self.boris)

    def test_values_are_stored_exactly_as_given(self):
        issuer, subject, email = "HTTPS://Issuer.Example/Tenant-A/v2.0", "000123-AbC_xyz~", "Ana.Link+Tag@Example.BG"
        self.add(issuer=issuer, subject=subject, provider="microsoft", email_at_link=email)
        stored = self.conn.execute(
            "SELECT issuer, subject, provider, email_at_link, typeof(issuer), typeof(subject) FROM user_identities"
        ).fetchone()
        self.assertEqual(tuple(stored), (issuer, subject, "microsoft", email, "text", "text"))

    def test_length_boundaries(self):
        self.add(issuer="i" * identities.MAX_ISSUER_LENGTH, subject="s" * identities.MAX_SUBJECT_LENGTH)
        self.assertIsNotNone(identities.find_identity(self.conn, "i" * 255, "s" * 255))
        self.assertIsNone(identities.find_identity(self.conn, "i" * 254, "s" * 255))
        with self.assertRaises(ValueError):
            self.add(issuer="i" * 256, subject="other")
        with self.assertRaises(ValueError):
            self.add(issuer="other", subject="s" * 256)
        self.assertEqual(len(self.rows()), 1)


class ConflictTest(IdentitiesTestCase):
    def setUp(self):
        super().setUp()
        self.identity = self.add(self.ana, email_at_link="ana.private@example.org")
        self.conn.commit()
        self.stored = self.rows()

    def test_a_duplicate_for_another_user_is_refused_and_the_owner_is_unchanged(self):
        with self.assertRaises(identities.IdentityConflict):
            self.add(self.boris)
        self.assertEqual(self.rows(), self.stored)
        self.assertEqual(identities.find_identity(self.conn, GOOGLE, SUBJECT)["user_id"], self.ana)
        self.assertEqual(identities.list_identities(self.conn, self.boris), [])

    def test_a_duplicate_for_the_same_user_is_refused_and_nothing_changes(self):
        with self.assertRaises(identities.IdentityConflict):
            self.add(self.ana)
        self.assertEqual(self.rows(), self.stored)

    def test_a_conflict_replaces_no_column_of_the_stored_identity(self):
        identities.record_identity_login(self.conn, self.identity)
        self.conn.commit()
        stored = self.rows()
        for provider, email in (("microsoft", "boris@example.bg"), ("google", None), ("google", "new@example.org")):
            with self.subTest(provider=provider, email=email), self.assertRaises(identities.IdentityConflict):
                self.add(self.boris, provider=provider, email_at_link=email)
        # Owner, provider, email, creation and login time: all as they were.
        self.assertEqual(self.rows(), stored)
        self.assertEqual(len(stored), 1)

    def test_a_conflict_is_not_an_integrity_error_and_names_no_identity_metadata(self):
        with self.assertRaises(identities.IdentityConflict) as raised:
            self.add(self.boris, email_at_link="boris.private@example.org")
        self.assertNotIsInstance(raised.exception, sqlite3.Error)
        self.assertIsInstance(raised.exception.__cause__, sqlite3.IntegrityError)
        for text in (str(raised.exception), repr(raised.exception), str(raised.exception.__cause__)):
            for secret in (GOOGLE, "accounts.google", SUBJECT, "example.org", "example.bg"):
                self.assertNotIn(secret, text)

    def test_a_conflict_leaves_the_callers_transaction_open_and_its_earlier_writes_intact(self):
        other = self.add(self.boris, subject="boris-subject")  # not committed
        with self.assertRaises(identities.IdentityConflict):
            self.add(self.boris)
        self.assertTrue(self.conn.in_transaction)  # the helper did not roll back
        self.assertEqual(identities.find_identity(self.conn, GOOGLE, "boris-subject")["id"], other)
        self.assertEqual(self.rows(self.connect()), self.stored)  # nor did it commit
        self.conn.commit()  # the caller may keep what it wrote before the conflict
        self.assertEqual([row[:4] for row in self.rows(self.connect())],
                         [(self.identity, self.ana, GOOGLE, SUBJECT), (other, self.boris, GOOGLE, "boris-subject")])

    def test_after_a_conflict_the_caller_can_roll_everything_back(self):
        self.add(self.boris, subject="boris-subject")
        with self.assertRaises(identities.IdentityConflict):
            self.add(self.boris)
        self.conn.rollback()
        self.assertEqual(self.rows(), self.stored)
        self.assertEqual(self.rows(self.connect()), self.stored)

    def test_a_conflict_is_decided_by_the_database_even_between_two_connections(self):
        other = self.connect()
        with self.assertRaises(identities.IdentityConflict):
            identities.add_identity(other, self.boris, issuer=GOOGLE, subject=SUBJECT, provider="google")
        other.rollback()
        self.assertEqual(self.rows(), self.stored)

    def test_another_integrity_failure_is_not_reported_as_a_conflict(self):
        # The schema's provider CHECK is the last word, whatever the service allows.
        with patch.object(identities, "PROVIDERS", ("google", "microsoft", "apple")):
            with self.assertRaises(sqlite3.IntegrityError):
                self.add(self.boris, subject="apple-subject", provider="apple")
        self.assertEqual(self.rows(), self.stored)


class MultipleIdentitiesTest(IdentitiesTestCase):
    def test_one_user_can_have_several_identities(self):
        first = self.add(self.ana, issuer=GOOGLE, subject="google-work", provider="google")
        second = self.add(self.ana, issuer=MICROSOFT, subject="microsoft-school", provider="microsoft")
        third = self.add(self.ana, issuer=GOOGLE, subject="google-private", provider="google")
        listed = identities.list_identities(self.conn, self.ana)
        self.assertEqual([(row["id"], row["issuer"], row["subject"], row["provider"]) for row in listed],
                         [(first, GOOGLE, "google-work", "google"),
                          (second, MICROSOFT, "microsoft-school", "microsoft"),
                          (third, GOOGLE, "google-private", "google")])
        self.assertEqual({row["user_id"] for row in listed}, {self.ana})
        for row in listed:
            self.assertEqual(identities.find_identity(self.conn, row["issuer"], row["subject"]), row)

    def test_listing_returns_only_that_users_identities(self):
        self.add(self.ana, subject="ana-1")
        mine = self.add(self.boris, subject="boris-1")
        self.add(self.ana, subject="ana-2")
        self.assertEqual([row["id"] for row in identities.list_identities(self.conn, self.boris)], [mine])
        self.assertEqual([row["subject"] for row in identities.list_identities(self.conn, self.ana)],
                         ["ana-1", "ana-2"])

    def test_listing_an_unknown_or_malformed_user_is_empty(self):
        self.add(self.ana)
        self.assertEqual(self.ana, 1)
        for user_id in (9999, 0, -1, 2**63, None, "1", str(self.ana), 1.0, True, [1]):
            with self.subTest(user_id=user_id):
                self.assertEqual(identities.list_identities(self.conn, user_id), [])


class EmailMetadataTest(IdentitiesTestCase):
    def test_equal_email_metadata_does_not_merge_identities_or_accounts(self):
        users_before = self.users()
        first = self.add(self.ana, subject="ana-google", email_at_link="shared@example.org")
        second = self.add(self.boris, subject="boris-google", email_at_link="shared@example.org")
        third = self.add(self.boris, issuer=MICROSOFT, subject="boris-microsoft", provider="microsoft",
                         email_at_link="shared@example.org")
        self.assertEqual(len({first, second, third}), 3)
        self.assertEqual([(row[1], row[3], row[5]) for row in self.rows()],
                         [(self.ana, "ana-google", "shared@example.org"),
                          (self.boris, "boris-google", "shared@example.org"),
                          (self.boris, "boris-microsoft", "shared@example.org")])
        self.assertEqual(self.users(), users_before)  # no account created, merged or changed

    def test_an_email_equal_to_another_accounts_email_links_nothing_to_that_account(self):
        users_before = self.users()
        # Boris's provider reports Ana's account email (in any spelling): it stays Boris's identity.
        self.add(self.boris, subject="boris-1", email_at_link="ana@example.bg")
        self.add(self.boris, subject="boris-2", email_at_link="Ana@Example.BG")
        self.assertEqual(identities.list_identities(self.conn, self.ana), [])
        self.assertEqual([row["user_id"] for row in identities.list_identities(self.conn, self.boris)],
                         [self.boris, self.boris])
        self.assertEqual(self.users(), users_before)
        self.assertEqual(accounts.get_user_by_email(self.conn, "ana@example.bg")["id"], self.ana)

    def test_the_account_email_is_not_a_key_either(self):
        # An identity whose own account has that very email is still found only by its pair.
        self.add(self.ana, email_at_link="ana@example.bg")
        self.assertIsNone(identities.find_identity(self.conn, GOOGLE, "ana@example.bg"))
        self.assertIsNone(identities.find_identity(self.conn, "ana@example.bg", SUBJECT))
        self.assertEqual(identities.find_identity(self.conn, GOOGLE, SUBJECT)["user_id"], self.ana)

    def test_email_is_optional_and_stored_verbatim(self):
        self.add(self.ana, subject="none")
        self.add(self.ana, subject="mixed", email_at_link="Mixed.Case+Tag@Example.ORG")
        self.add(self.ana, subject="unicode", email_at_link="иван@пример.бг")
        self.add(self.ana, subject="longest", email_at_link="x" * identities.MAX_EMAIL_AT_LINK_LENGTH)
        self.assertEqual([row["email_at_link"] for row in identities.list_identities(self.conn, self.ana)],
                         [None, "Mixed.Case+Tag@Example.ORG", "иван@пример.бг", "x" * 254])

    def test_no_function_takes_or_filters_by_an_email(self):
        self.assertEqual(list(inspect.signature(identities.find_identity).parameters), ["conn", "issuer", "subject"])
        self.assertEqual(list(inspect.signature(identities.list_identities).parameters), ["conn", "user_id"])
        self.assertEqual(list(inspect.signature(identities.record_identity_login).parameters),
                         ["conn", "identity_id"])
        statements = self.traced()
        identity = self.add(self.ana, email_at_link="ana@example.bg")
        with self.assertRaises(identities.IdentityConflict):
            self.add(self.boris, email_at_link="ana@example.bg")
        identities.find_identity(self.conn, GOOGLE, SUBJECT)
        identities.list_identities(self.conn, self.ana)
        identities.record_identity_login(self.conn, identity)
        conditions = [statement.split(" WHERE ", 1)[1] for statement in statements if " WHERE " in statement]
        self.assertGreaterEqual(len(conditions), 5)
        for condition in conditions:
            self.assertNotIn("email", condition.lower())


class TransactionTest(IdentitiesTestCase):
    def test_add_does_not_commit_and_a_rollback_undoes_it(self):
        self.add(self.ana)
        self.assertTrue(self.conn.in_transaction)
        self.assertEqual(self.rows(self.connect()), [])  # invisible to another connection
        self.conn.rollback()
        self.assertEqual(self.rows(), [])
        self.assertIsNone(identities.find_identity(self.conn, GOOGLE, SUBJECT))
        self.assertEqual(identities.list_identities(self.conn, self.ana), [])

    def test_a_committed_identity_is_visible_to_another_connection(self):
        identity = self.add(self.ana)
        self.conn.commit()
        self.assertEqual(identities.find_identity(self.connect(), GOOGLE, SUBJECT)["id"], identity)

    def test_record_login_does_not_commit_and_a_rollback_undoes_it(self):
        identity = self.add(self.ana)
        self.conn.commit()
        identities.record_identity_login(self.conn, identity)
        self.assertTrue(self.conn.in_transaction)
        self.assertIsNone(identities.find_identity(self.connect(), GOOGLE, SUBJECT)["last_login_at"])
        self.conn.rollback()
        self.assertIsNone(identities.find_identity(self.conn, GOOGLE, SUBJECT)["last_login_at"])

    def test_several_writes_form_one_transaction_of_the_caller(self):
        first = self.add(self.ana, subject="one")
        self.add(self.boris, subject="two")
        identities.record_identity_login(self.conn, first)
        self.assertEqual(self.rows(self.connect()), [])
        self.conn.rollback()
        self.assertEqual(self.rows(), [])
        # The connection is usable as before: the next write starts the caller's next transaction.
        again = self.add(self.boris, subject="three")
        self.conn.commit()
        self.assertEqual(identities.find_identity(self.connect(), GOOGLE, "three")["id"], again)

    def test_reads_and_writes_refused_before_their_write_statement_open_no_transaction(self):
        # Only what is refused before an INSERT or UPDATE is attempted: malformed input and an
        # unknown user. A refusal by the write statement itself is the next two tests.
        identity = self.add(self.ana)
        self.conn.commit()
        identities.find_identity(self.conn, GOOGLE, SUBJECT)
        identities.list_identities(self.conn, self.ana)
        with self.assertRaises(ValueError):
            self.add(self.ana, subject="")
        with self.assertRaises(LookupError):
            self.add(9999, subject="nobody")
        with self.assertRaises(ValueError):
            identities.record_identity_login(self.conn, str(identity))
        self.assertFalse(self.conn.in_transaction)

    def test_a_duplicate_on_a_transaction_free_connection_begins_one_the_caller_must_finish(self):
        self.add(self.ana)
        self.conn.commit()
        stored = self.rows()
        self.assertFalse(self.conn.in_transaction)
        with self.assertRaises(identities.IdentityConflict):
            self.add(self.boris)
        # sqlite3 began a transaction for the INSERT, and its refusal does not end it: the helper
        # neither commits nor rolls back. Nothing was written; ending it is the caller's.
        self.assertTrue(self.conn.in_transaction)
        self.assertEqual(self.rows(), stored)
        self.assertEqual(self.rows(self.connect()), stored)
        self.conn.rollback()
        self.assertFalse(self.conn.in_transaction)
        self.assertEqual(self.rows(), stored)

    def test_a_login_stamp_for_an_unknown_id_begins_a_transaction_the_caller_must_finish(self):
        identity = self.add(self.ana)
        self.conn.commit()
        stored = self.rows()
        self.assertFalse(self.conn.in_transaction)
        with self.assertRaises(LookupError):
            identities.record_identity_login(self.conn, identity + 1)
        # The UPDATE matched no row, and sqlite3 had begun a transaction for it all the same.
        self.assertTrue(self.conn.in_transaction)
        self.assertEqual(self.rows(), stored)
        self.conn.commit()  # nothing was written, so a commit ends it as harmlessly as a rollback
        self.assertFalse(self.conn.in_transaction)
        self.assertEqual(self.rows(self.connect()), stored)
        self.assertIsNone(identities.find_identity(self.conn, GOOGLE, SUBJECT)["last_login_at"])

    def test_no_helper_ever_commits_or_rolls_back(self):
        calls = []

        class Recording(sqlite3.Connection):
            def commit(self):
                calls.append("commit")
                super().commit()

            def rollback(self):
                calls.append("rollback")
                super().rollback()

        conn = sqlite3.connect(self.path, factory=Recording)
        self.addCleanup(conn.close)
        conn.execute("PRAGMA foreign_keys = ON")
        statements = []
        conn.set_trace_callback(statements.append)
        identity = identities.add_identity(conn, self.ana, issuer=GOOGLE, subject=SUBJECT, provider="google")
        with self.assertRaises(identities.IdentityConflict):
            identities.add_identity(conn, self.boris, issuer=GOOGLE, subject=SUBJECT, provider="google")
        with self.assertRaises(ValueError):
            identities.add_identity(conn, self.boris, issuer="", subject=SUBJECT, provider="google")
        with self.assertRaises(LookupError):
            identities.add_identity(conn, 9999, issuer=GOOGLE, subject="other", provider="google")
        identities.record_identity_login(conn, identity)
        with self.assertRaises(LookupError):
            identities.record_identity_login(conn, 9999)
        identities.find_identity(conn, GOOGLE, SUBJECT)
        identities.list_identities(conn, self.ana)
        conn.set_trace_callback(None)
        self.assertEqual(calls, [])
        self.assertTrue(conn.in_transaction)
        for statement in statements:
            self.assertNotRegex(statement.upper(), r"^\s*(COMMIT|END|ROLLBACK|SAVEPOINT|RELEASE)\b")
        conn.rollback()
        self.assertEqual(calls, ["rollback"])
        self.assertEqual(self.rows(), [])


class ValidationTest(IdentitiesTestCase):
    def assert_nothing_written(self, statements):
        self.assertEqual(self.rows(), [])
        self.assertFalse(self.conn.in_transaction)
        self.assertEqual([s for s in statements if re.match(r"\s*(INSERT|UPDATE|DELETE|REPLACE)\b", s, re.I)], [])

    def test_malformed_issuers_and_subjects_are_rejected_before_any_statement(self):
        statements = self.traced()
        for bad in MALFORMED_KEYS:
            with self.subTest(issuer=bad), self.assertRaises(ValueError):
                self.add(issuer=bad)
            with self.subTest(subject=bad), self.assertRaises(ValueError):
                self.add(subject=bad)
        self.assertEqual(statements, [])  # refused before the database is asked anything
        self.assert_nothing_written(statements)

    def test_a_padded_key_is_refused_not_trimmed(self):
        for issuer, subject in ((" " + GOOGLE, SUBJECT), (GOOGLE + " ", SUBJECT), (GOOGLE, " " + SUBJECT),
                                (GOOGLE, SUBJECT + " "), (GOOGLE, SUBJECT + "\n")):
            with self.subTest(issuer=issuer, subject=subject), self.assertRaises(ValueError):
                self.add(issuer=issuer, subject=subject)
        self.assertIsNone(identities.find_identity(self.conn, GOOGLE, SUBJECT))
        self.assertEqual(self.rows(), [])

    def test_the_provider_must_be_one_the_schema_allows(self):
        statements = self.traced()
        for bad in ("apple", "Google", "GOOGLE", "Microsoft", " google", "google ", "", None, 1, b"google",
                    ["google"], "facebook"):
            with self.subTest(provider=bad), self.assertRaises(ValueError):
                self.add(provider=bad)
        self.assert_nothing_written(statements)

    def test_the_providers_are_exactly_those_of_the_migration(self):
        migration = (ROOT / "web" / "migrations" / "019_user_identities.sql").read_text(encoding="utf-8")
        allowed = re.search(r"CHECK \(provider IN \(([^)]*)\)\)", migration).group(1)
        self.assertEqual(identities.PROVIDERS, tuple(re.findall(r"'([^']*)'", allowed)))
        self.assertEqual(identities.PROVIDERS, ("google", "microsoft"))

    def test_malformed_email_metadata_is_rejected(self):
        statements = self.traced()
        for bad in ("", " ", " ana@example.org", "ana@example.org ", "ana smith@example.org", "ana\n@example.org",
                    "ana\x00@example.org", "ana\u200b@example.org", "\ud800@example.org", "x" * 255, 5,
                    b"ana@example.org", ["ana@example.org"], False):
            with self.subTest(email=bad), self.assertRaises(ValueError):
                self.add(email_at_link=bad)
        self.assert_nothing_written(statements)

    def test_a_malformed_user_id_is_rejected(self):
        statements = self.traced()
        self.assertEqual(self.ana, 1)
        for bad in ("1", str(self.ana), 1.0, True, False, None, 0, -1, 2**63, [1], b"1"):
            with self.subTest(user_id=bad), self.assertRaises(ValueError):
                identities.add_identity(self.conn, bad, issuer=GOOGLE, subject=SUBJECT, provider="google")
        self.assertEqual(statements, [])
        self.assert_nothing_written(statements)

    def test_an_unknown_user_is_refused(self):
        statements = self.traced()
        for missing in (9999, 2**63 - 1):
            with self.subTest(user_id=missing), self.assertRaises(LookupError):
                self.add(missing)
        self.assert_nothing_written(statements)

    def test_an_unknown_user_is_refused_without_foreign_key_enforcement_too(self):
        plain = sqlite3.connect(self.path)  # foreign keys off, tuple rows
        self.addCleanup(plain.close)
        self.assertEqual(plain.execute("PRAGMA foreign_keys").fetchone()[0], 0)
        with self.assertRaises(LookupError):
            identities.add_identity(plain, 9999, issuer=GOOGLE, subject=SUBJECT, provider="google")
        self.assertEqual(self.rows(plain), [])
        # The helpers do not depend on the row factory either.
        identity = identities.add_identity(plain, self.boris, issuer=GOOGLE, subject=SUBJECT, provider="google")
        self.assertEqual(identities.find_identity(plain, GOOGLE, SUBJECT)["user_id"], self.boris)
        self.assertEqual([row["id"] for row in identities.list_identities(plain, self.boris)], [identity])
        plain.rollback()

    def test_error_messages_do_not_echo_the_refused_values(self):
        cases = (
            {"issuer": "bad issuer value"}, {"subject": "bad subject value"}, {"provider": "bad-provider-value"},
            {"email_at_link": "bad email@example.org"},
        )
        for case in cases:
            with self.subTest(case=case), self.assertRaises(ValueError) as raised:
                self.add(**case)
            self.assertNotIn("bad", str(raised.exception))
            self.assertNotIn("example", str(raised.exception))

    def test_a_disabled_account_is_storage_wise_an_existing_user(self):
        # Whether a disabled account may link or sign in is the later flow's decision.
        accounts.set_active(self.conn, self.boris, False)
        self.add(self.boris)
        self.assertEqual(identities.find_identity(self.conn, GOOGLE, SUBJECT)["user_id"], self.boris)
        self.assertEqual(accounts.get_user_by_id(self.conn, self.boris)["is_active"], 0)


class RecordLoginTest(IdentitiesTestCase):
    def test_it_stamps_only_that_identity(self):
        first = self.add(self.ana, subject="one")
        second = self.add(self.ana, subject="two")
        self.conn.commit()
        before, users_before = self.rows(), self.users()
        self.assertIsNone(identities.record_identity_login(self.conn, first))
        after = self.rows()
        self.assertRegex(after[0][7], TIMESTAMP)
        self.assertEqual(after[0][:7], before[0][:7])  # every other column of that identity
        self.assertEqual(after[1], before[1])          # the other identity, last_login_at still NULL
        self.assertIsNone(identities.find_identity(self.conn, GOOGLE, "two")["last_login_at"])
        self.assertEqual(identities.find_identity(self.conn, GOOGLE, "two")["id"], second)
        # The account's own last_login_at is the sign-in flow's to record, not this helper's.
        self.assertEqual(self.users(), users_before)

    def test_unknown_and_malformed_identity_ids(self):
        identity = self.add(self.ana)
        self.conn.commit()
        with self.assertRaises(LookupError):
            identities.record_identity_login(self.conn, identity + 1)
        for bad in ("1", str(identity), 1.0, True, None, 0, -1, 2**63):
            with self.subTest(identity_id=bad), self.assertRaises(ValueError):
                identities.record_identity_login(self.conn, bad)
        self.conn.rollback()
        self.assertIsNone(identities.find_identity(self.conn, GOOGLE, SUBJECT)["last_login_at"])


class AccountsUntouchedTest(IdentitiesTestCase):
    def test_no_helper_creates_or_changes_an_account(self):
        before = self.users()
        self.assertEqual([(row[1], row[3], row[4]) for row in before],
                         [("ana@example.bg", "teacher", ANA_HASH), ("boris@example.bg", "student", None)])
        identity = self.add(self.ana, email_at_link="someone.else@example.org")
        self.add(self.boris, issuer=MICROSOFT, subject="boris", provider="microsoft", email_at_link="ana@example.bg")
        with self.assertRaises(identities.IdentityConflict):
            self.add(self.boris)
        with self.assertRaises(ValueError):
            self.add(self.boris, subject="")
        with self.assertRaises(LookupError):
            self.add(9999, subject="nobody")
        identities.record_identity_login(self.conn, identity)
        identities.find_identity(self.conn, GOOGLE, SUBJECT)
        identities.list_identities(self.conn, self.ana)
        self.conn.commit()
        # Emails, names, roles, active flags, password hashes, session keys and every
        # timestamp (last_login_at and updated_at included), and no new account.
        self.assertEqual(self.users(), before)
        for table in ("totp_credentials", "totp_recovery_codes", "login_throttle", "quiz_assignments", "quiz_attempts"):
            self.assertEqual(self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0, table)
        # An identity is not a credential: the account's sign-in state is what it was.
        user = accounts.load_session_user(self.conn, self.ana, before[0][6])
        self.assertEqual((user["id"], user["role"], user["has_password"]), (self.ana, "teacher", 1))


class StorageOnlyTest(unittest.TestCase):
    """The scope of this PR: a small storage module that nothing calls yet."""

    def test_the_public_api_is_the_four_helpers_and_the_conflict(self):
        public = {name for name, value in vars(identities).items()
                  if not name.startswith("_") and (inspect.isfunction(value) or inspect.isclass(value))}
        self.assertEqual(public, {"IdentityConflict", "add_identity", "find_identity", "list_identities",
                                  "record_identity_login"})

    def test_the_module_cannot_unlink_reassign_replace_or_log(self):
        source = MODULE.read_text(encoding="utf-8")
        sql = " ".join(re.findall(r'"([^"\n]*)"', source)).upper()
        self.assertEqual(len(re.findall(r"\bINSERT INTO USER_IDENTITIES\b", sql)), 1)
        self.assertEqual(re.findall(r"\bUPDATE (\w+) SET (\w+)", sql), [("USER_IDENTITIES", "LAST_LOGIN_AT")])
        for forbidden in ("DELETE", "REPLACE", "INSERT OR", "ON CONFLICT", "UPSERT", "DROP", "ALTER",
                          "COMMIT", "ROLLBACK", "SAVEPOINT", "UPDATE USERS", "INSERT INTO USERS"):
            self.assertNotIn(forbidden, sql, forbidden)
        for forbidden in ("import logging", "getLogger", "print(", ".commit(", ".rollback(", "lower(", "strip(",
                          "casefold(", "normalize(", "COLLATE"):
            self.assertNotIn(forbidden, source, forbidden)

    def test_it_imports_only_the_standard_library(self):
        code = (
            "import sys; before = set(sys.modules); import web.services.identities; "
            "print(sorted(m for m in set(sys.modules) - before "
            "if m.split('.')[0] in ('web', 'src', 'flask', 'werkzeug', 'authlib', 'requests', 'jwt')))"
        )
        out = subprocess.run([sys.executable, "-B", "-c", code], cwd=ROOT, capture_output=True, text=True, check=True)
        self.assertEqual(out.stdout.strip(), "['web', 'web.services', 'web.services.identities']")

    def test_nothing_in_the_application_uses_the_storage_yet(self):
        # Provider sign-in is a later PR: until then no route, blueprint, service, template
        # or tool may read or write identities. That PR replaces this test.
        allowed = {Path("web/services/identities.py"), Path("web/migrations/019_user_identities.sql"),
                   Path("src/canonical_tables.py")}
        pattern = re.compile(r"user_identities|services\.identities|import identities|\bidentities\.")
        users = []
        for directory in ("web", "src", "deploy"):
            for path in sorted((ROOT / directory).rglob("*")):
                relative = path.relative_to(ROOT)
                if (not path.is_file() or relative in allowed or "__pycache__" in path.parts
                        or path.suffix not in {".py", ".html", ".sql", ".sh", ".txt", ".conf", ".service", ".js"}):
                    continue
                if pattern.search(path.read_text(encoding="utf-8", errors="replace")):
                    users.append(str(relative))
        self.assertEqual(users, [])
        requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8").lower()
        self.assertNotIn("authlib", requirements)


if __name__ == "__main__":
    unittest.main()
