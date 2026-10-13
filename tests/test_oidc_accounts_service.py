"""Google OIDC, test plan B3: the accounts service (``web.services.oidc_accounts``).

Sign-in and onboarding in one transaction, the email and name rules, the account a
link is bound to, the link decision and the write that stores an identity once. The
service is called as its callers call it: on a connection that began ``BEGIN
IMMEDIATE``, committed after an outcome and rolled back after an exception. Races use
threads with one connection each. Every database is temporary.
"""

import ast
import hashlib
import hmac
import logging
import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from tests.google_oidc_support import ABSENT, GoogleAppTestCase
from tests.support import ROOT, build_test_database
from web import oauth_state
from web.services import accounts, google_oidc, identities, login_throttle, oidc_accounts, oidc_consumption
from web.services.oidc_accounts import GoogleProfile

SECRET = "synthetic-secret-key-for-the-accounts-service-tests"
ISSUER = "https://accounts.google.com"
IAT = 1_800_000_000  # the ``iat`` of the session that started a link
NOW = 5_000  # the monotonic clock, as the caller read it under the write lock
EXPIRES = NOW + 100


class ServiceTestCase(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="learnpilot-oidc-accounts-")
        self.addCleanup(directory.cleanup)
        self.db_path = build_test_database(Path(directory.name) / "questions.db")
        self.conn = self.connect()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=20, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        self.addCleanup(conn.close)
        return conn

    def add_user(self, role="teacher", *, email=None, has_password=False, active=True, name="Тест Потребител"):
        """A user row, without paying for scrypt: the hash is a placeholder."""
        with closing(sqlite3.connect(self.db_path)) as conn:
            user_id = accounts.create_user(conn, display_name=name, role=role, email=email, is_active=active)
            if has_password:
                conn.execute("UPDATE users SET password_hash = 'placeholder$salt$hash' WHERE id = ?", (user_id,))
            conn.commit()
            key = conn.execute("SELECT session_key FROM users WHERE id = ?", (user_id,)).fetchone()[0]
        return {"id": user_id, "session_key": key}

    def query(self, sql, params=()):
        with closing(sqlite3.connect(self.db_path)) as conn:
            return [tuple(row) for row in conn.execute(sql, params)]

    def execute(self, sql, params=()):
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute(sql, params)
            conn.commit()

    def users(self):
        return self.query("SELECT id, email, display_name, role, is_active, password_hash FROM users ORDER BY id")

    def identity_rows(self):
        return self.query("SELECT user_id, issuer, subject, provider, email_at_link FROM user_identities ORDER BY id")

    def records(self):
        return self.query("SELECT tx_hash, expires_at FROM oidc_consumed_transactions ORDER BY tx_hash")

    def sign_in(self, profile, conn=None) -> oidc_accounts.SignIn:
        """As the callback does it: one write transaction, committed only for a user."""
        conn = conn or self.conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            result = oidc_accounts.sign_in_or_onboard(conn, profile)
            if result.user is None:
                conn.rollback()
            else:
                conn.commit()
        except BaseException:
            conn.rollback()
            raise
        return result

    # -- linking ---------------------------------------------------------------

    def pending(self, account, *, sub="google-subject-1", email="", name="", exp=EXPIRES, link_id="i" * 43, **overrides):
        pending = {
            "v": 1, "id": link_id, "inst": "0" * 32, "exp": exp, "uid": account["id"],
            "skd": oidc_accounts.session_key_digest(SECRET, account["session_key"]), "siat": IAT,
            "sub": sub, "email": email, "name": name,
        }
        pending.update(overrides)
        return pending

    def confirm(self, pending, account, *, conn=None, now=NOW, wall_now=IAT + 60, session_key=None, session_iat=IAT):
        """As the confirm route does it: begin, confirm, commit after every outcome."""
        conn = conn or self.conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            outcome = oidc_accounts.confirm_link(
                conn, pending, secret_key=SECRET, session_key=session_key or account["session_key"],
                session_iat=session_iat, now=now, wall_now=wall_now)
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        return outcome

    def key(self, pending) -> str:
        return oidc_consumption.consumption_key(SECRET, "link-confirm", pending["id"])


# --------------------------------------------------------------------------- email and name


class EmailAndNameRuleTest(unittest.TestCase):
    def test_comparable_is_the_normalized_form_or_nothing(self):
        self.assertEqual(oidc_accounts.comparable_email("Ana@Example.BG"), "ana@example.bg")
        self.assertEqual(oidc_accounts.comparable_email("  ana@example.bg\t"), "ana@example.bg")
        self.assertEqual(oidc_accounts.comparable_email("ａｎａ@example.bg"), "ana@example.bg")  # NFKC
        for value in (None, 5, ["a@b.bg"], "", "no-at-sign", "two@@example.bg", "a@b@c.bg", "@example.bg", "ana@",
                      "an a@example.bg", "x" * 250 + "@example.bg", "ana\0@example.bg"):
            with self.subTest(value=repr(value)[:30]):
                self.assertIsNone(oidc_accounts.comparable_email(value))
        longest = "x" * 243 + "@example.bg"
        self.assertEqual(len(longest), 254)
        self.assertEqual(oidc_accounts.comparable_email(longest), longest)

    def test_storable_is_the_reported_value_when_the_storage_would_take_it(self):
        self.assertEqual(oidc_accounts.storable_email("Ana@Example.BG"), "Ana@Example.BG")
        # Comparable, and not storable: the reported value carries whitespace.
        for value in (" ana@example.bg", "ana@example.bg\n", "\tana@example.bg "):
            with self.subTest(value=repr(value)):
                self.assertIsNotNone(oidc_accounts.comparable_email(value))
                self.assertIsNone(oidc_accounts.storable_email(value))
        for value in (None, 5, "no-at-sign", "x" * 250 + "@example.bg"):
            self.assertIsNone(oidc_accounts.storable_email(value))

    def test_whatever_is_storable_is_accepted_by_the_identity_storage(self):
        for value in ("Ana@Example.BG", "x" * 243 + "@example.bg", "ана@пример.бг", "a+tag@example.bg"):
            with self.subTest(value=value[:30]):
                self.assertTrue(identities._is_email_at_link(oidc_accounts.storable_email(value)))
        for value in (" ana@example.bg", "ana@example.bg\n"):
            self.assertFalse(identities._is_email_at_link(value))

    def test_a_pending_link_carries_the_reported_address_only_when_it_is_comparable(self):
        self.assertEqual(oidc_accounts.reported_email(" Ana@Example.BG "), " Ana@Example.BG ")
        for value in (None, 5, "no-at-sign", "x" * 250 + "@example.bg"):
            self.assertEqual(oidc_accounts.reported_email(value), "")

    def test_names_are_cleaned_and_cut(self):
        cases = {
            "Иван Иванов": "Иван Иванов",
            "  Иван \t\n  Иванов  ": "Иван Иванов",
            "Иван‮вонавИ": "ИванвонавИ",          # a bidirectional override
            "A​B⁦C⁩D﻿": "ABCD",    # zero width space, isolates, a byte order mark
            "tab\there\0and\x1fcontrol": "tabhereandcontrol",
            "=HYPERLINK(\"https://evil.example\")": "=HYPERLINK(\"https://evil.example\")",
            "<script>alert(1)</script>": "<script>alert(1)</script>",
            "x" * 81: "x" * 80,
            "y" * 5000: "y" * 80,
            "a" * 79 + "  b": "a" * 79,   # cut to eighty and stripped again
        }
        for reported, cleaned in cases.items():
            with self.subTest(reported=reported[:20]):
                self.assertEqual(oidc_accounts.cleaned_name(reported), cleaned)
                self.assertLessEqual(len(oidc_accounts.cleaned_name(reported)), 80)
        for value in (None, 5, ["Иван"], {"name": "Иван"}):
            self.assertEqual(oidc_accounts.cleaned_name(value), "")

    def test_a_student_without_a_usable_name_is_called_uchenik(self):
        # Decision P4.
        for value in (ABSENT, None, 5, ["Иван"], "", " ", "​​​", "A", " Я ", "\x00\x01"):
            with self.subTest(value=repr(value)[:20]):
                self.assertEqual(oidc_accounts.student_display_name(None if value is ABSENT else value), "Ученик")
        self.assertEqual(oidc_accounts.student_display_name("Яя"), "Яя")
        self.assertEqual(oidc_accounts.DEFAULT_STUDENT_NAME, "Ученик")

    def test_a_profile_has_no_place_for_anything_but_subject_email_and_name(self):
        self.assertEqual(set(GoogleProfile.__dataclass_fields__), {"subject", "email", "name"})
        self.assertNotIn("person@example.bg", repr(GoogleProfile("subject-value", "person@example.bg", "Име")))
        self.assertNotIn("subject-value", repr(GoogleProfile("subject-value")))


# --------------------------------------------------------------------------- sign-in and onboarding


class SignInTest(ServiceTestCase):
    def test_a_new_identity_creates_exactly_one_student_and_one_identity(self):
        result = self.sign_in(GoogleProfile("subject-1", "Pupil@Example.BG", "Мария Петрова"))
        self.assertEqual(result.outcome, oidc_accounts.CREATED)
        user_id = result.user["id"]
        # No email and no password: users.email is the identifier of the local
        # password credential and stays NULL.
        self.assertEqual(self.users(), [(user_id, None, "Мария Петрова", "student", 1, None)])
        self.assertEqual(self.identity_rows(), [(user_id, ISSUER, "subject-1", "google", "Pupil@Example.BG")])
        self.assertTrue(result.user["session_key"])
        stamps = self.query("SELECT last_login_at FROM users UNION ALL SELECT last_login_at FROM user_identities")
        self.assertTrue(all(row[0] for row in stamps))

    def test_the_second_sign_in_signs_the_same_account_in(self):
        first = self.sign_in(GoogleProfile("subject-1", "a@example.bg", "Първо Име"))
        second = self.sign_in(GoogleProfile("subject-1", "changed@example.bg", "Друго Име"))
        self.assertEqual((second.outcome, second.user["id"]), (oidc_accounts.SIGNED_IN, first.user["id"]))
        # Neither the name nor the reported address is refreshed afterwards.
        self.assertEqual(len(self.users()), 1)
        self.assertEqual(self.users()[0][2], "Първо Име")
        self.assertEqual(self.identity_rows()[0][4], "a@example.bg")

    def test_a_failure_between_the_two_inserts_leaves_neither(self):
        with patch.object(identities, "add_identity", side_effect=sqlite3.OperationalError("disk I/O error")):
            with self.assertRaises(sqlite3.OperationalError):
                self.sign_in(GoogleProfile("subject-1", "a@example.bg", "Име Фамилия"))
        self.assertEqual((self.users(), self.identity_rows()), ([], []))
        self.assertFalse(self.conn.in_transaction)

    def test_eight_connections_on_one_new_identity_make_one_user_and_one_identity(self):
        barrier = threading.Barrier(8)

        def racer(number):
            conn = self.connect()
            barrier.wait(10)
            return self.sign_in(GoogleProfile("contested", f"racer{number}@example.bg", f"Състезател {number}"), conn)

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(racer, range(8)))
        self.assertEqual(sorted(result.outcome for result in results),
                         [oidc_accounts.CREATED] + [oidc_accounts.SIGNED_IN] * 7)
        self.assertEqual(len({result.user["id"] for result in results}), 1)
        self.assertEqual((len(self.users()), len(self.identity_rows())), (1, 1))
        orphans = self.query("SELECT id FROM users WHERE id NOT IN (SELECT user_id FROM user_identities)")
        self.assertEqual(orphans, [])

    def test_the_identity_is_resolved_first_whatever_email_the_token_carries(self):
        other = self.add_user("teacher", email="teacher@example.bg", has_password=True)
        created = self.sign_in(GoogleProfile("subject-1", "pupil@example.bg", "Ученик Първи"))
        # The same identity now reports the address of another account: it still
        # signs its own user in, and the email is not read at all.
        again = self.sign_in(GoogleProfile("subject-1", "Teacher@Example.BG", "Ученик Първи"))
        self.assertEqual((again.outcome, again.user["id"]), (oidc_accounts.SIGNED_IN, created.user["id"]))
        self.assertNotEqual(again.user["id"], other["id"])
        self.assertEqual(len(self.users()), 2)
        with patch.object(oidc_accounts, "comparable_email", side_effect=AssertionError("the email was read")):
            self.assertEqual(self.sign_in(GoogleProfile("subject-1", "x@example.bg")).outcome, oidc_accounts.SIGNED_IN)

    def test_a_disabled_accounts_identity_is_refused_and_creates_no_second_account(self):
        created = self.sign_in(GoogleProfile("subject-1", "pupil@example.bg", "Ученик Първи"))
        self.execute("UPDATE users SET is_active = 0, last_login_at = NULL WHERE id = ?", (created.user["id"],))
        self.execute("UPDATE user_identities SET last_login_at = NULL")
        before = self.users(), self.identity_rows()
        refused = self.sign_in(GoogleProfile("subject-1", "pupil@example.bg", "Ученик Първи"))
        self.assertEqual((refused.outcome, refused.user), (oidc_accounts.NOT_ALLOWED, None))
        self.assertEqual((self.users(), self.identity_rows()), before)
        # A refused sign-in stamps nothing.
        self.assertEqual(self.query("SELECT last_login_at FROM users UNION ALL "
                                    "SELECT last_login_at FROM user_identities"), [(None,), (None,)])

    def test_an_admins_identity_is_refused_including_a_teacher_promoted_after_linking(self):
        teacher = self.add_user("teacher", email="teacher@example.bg", has_password=True)
        with closing(sqlite3.connect(self.db_path)) as conn:
            identities.add_identity(conn, teacher["id"], issuer=ISSUER, subject="subject-t", provider="google")
            conn.commit()
        self.assertEqual(self.sign_in(GoogleProfile("subject-t")).outcome, oidc_accounts.SIGNED_IN)
        # Role and active state are read at the write, on every sign-in.
        self.execute("UPDATE users SET role = 'admin' WHERE id = ?", (teacher["id"],))
        refused = self.sign_in(GoogleProfile("subject-t"))
        self.assertEqual((refused.outcome, refused.user), (oidc_accounts.NOT_ALLOWED, None))
        self.assertEqual(len(self.users()), 1)

    def test_a_comparable_email_of_an_existing_account_creates_and_links_nothing(self):
        existing = self.add_user("teacher", email="teacher@example.bg", has_password=True)
        before = self.users()
        for reported in ("teacher@example.bg", "Teacher@Example.BG", "  teacher@example.bg  ",
                         "ｔｅａｃｈｅｒ@example.bg", "TEACHER@EXAMPLE.BG\n"):
            with self.subTest(reported=repr(reported)):
                refused = self.sign_in(GoogleProfile("subject-new", reported, "Нов Човек"))
                self.assertEqual((refused.outcome, refused.user), (oidc_accounts.EMAIL_EXISTS, None))
                self.assertEqual((self.users(), self.identity_rows()), (before, []))
        # An account without a sign-in email cannot collide with anything.
        self.add_user("student")
        self.assertEqual(self.sign_in(GoogleProfile("subject-a", "other@example.bg")).outcome, oidc_accounts.CREATED)
        self.assertEqual(self.sign_in(GoogleProfile("subject-b", "other@example.bg")).outcome, oidc_accounts.CREATED)
        self.assertNotEqual(existing["id"], self.identity_rows()[0][0])

    def test_the_email_table_for_a_new_identity(self):
        cases = (
            # reported, stored as email_at_link
            ("pupil@example.bg", "pupil@example.bg"),
            ("Pupil@Example.BG", "Pupil@Example.BG"),          # as Google reported it
            (" spaced@example.bg ", None),                      # comparable, not storable
            (None, None),
            (5, None),
            ("no-at-sign", None),
            ("x" * 250 + "@example.bg", None),
        )
        for number, (reported, stored) in enumerate(cases):
            with self.subTest(reported=repr(reported)[:30]):
                result = self.sign_in(GoogleProfile(f"subject-{number}", reported, "Име Фамилия"))
                self.assertEqual(result.outcome, oidc_accounts.CREATED)
                row = self.query("SELECT email_at_link FROM user_identities WHERE subject = ?", (f"subject-{number}",))
                self.assertEqual(row, [(stored,)])
        # Never written to users.email, whatever was reported.
        self.assertEqual({row[1] for row in self.users()}, {None})

    def test_only_a_student_without_a_password_is_ever_created(self):
        self.sign_in(GoogleProfile("subject-1", "a@example.bg", "Име"))
        self.assertEqual([(row[3], row[5]) for row in self.users()], [("student", None)])
        source = (ROOT / "web/services/oidc_accounts.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        creations = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
                     and ast.unparse(node.func) == "accounts.create_user"]
        self.assertEqual(len(creations), 1)
        keywords = {keyword.arg: ast.unparse(keyword.value) for keyword in creations[0].keywords}
        # The role is a literal, and neither an email nor a password is passed.
        self.assertEqual(keywords["role"], "'student'")
        self.assertEqual(set(keywords), {"display_name", "role"})
        for needle in ("set_role", "set_password", "set_active", "UPDATE ", "INSERT ", "DELETE "):
            self.assertFalse(needle in source, needle)

    def test_hostile_and_unusable_names(self):
        cases = {
            "x" * 81: "x" * 80,
            "y" * 5000: "y" * 80,
            "Иван‮\u0000Иванов": "ИванИванов",
            "=cmd|' /C calc'!A0": "=cmd|' /C calc'!A0",
            "<img src=x onerror=alert(1)>": "<img src=x onerror=alert(1)>",
            None: "Ученик", 5: "Ученик", "": "Ученик", "Я": "Ученик", "​ ": "Ученик",
        }
        for number, (reported, stored) in enumerate(cases.items()):
            with self.subTest(reported=repr(reported)[:20]):
                result = self.sign_in(GoogleProfile(f"subject-{number}", None, reported))
                self.assertEqual(result.user["display_name"], stored)

    def test_the_service_never_commits_or_rolls_back(self):
        self.conn.execute("BEGIN IMMEDIATE")
        oidc_accounts.sign_in_or_onboard(self.conn, GoogleProfile("subject-1", None, "Име"))
        self.assertTrue(self.conn.in_transaction)
        self.assertEqual(self.users(), [])  # nothing is visible to another connection yet
        self.conn.rollback()
        self.assertEqual((self.users(), self.identity_rows()), ([], []))


class EmailVerifiedPlaysNoPartTest(GoogleAppTestCase):
    """The email table of design 6.3 through the whole flow, with every form of
    ``email_verified``: true, false, absent and a string."""

    VERIFIED = (True, False, ABSENT, "maybe")

    def test_a_collision_is_a_collision_verified_or_not(self):
        self.make("teacher", email="teacher@example.bg")
        for verified in self.VERIFIED:
            with self.subTest(email_verified=repr(verified)[:10]):
                client = self.app.test_client()
                with self.assertLogs("learnpilot.oidc", logging.WARNING):
                    refused = self.google_sign_in(client, subject=f"s-{verified!r}"[:30].replace(" ", ""),
                                                  email="Teacher@Example.BG", email_verified=verified)
                self.assert_redirect(refused, "/login?google=email_exists")
                self.assertEqual(self.session(client), {})
        self.assertEqual((self.user_count(), self.identity_rows()), (1, []))

    def test_every_row_creates_the_same_account_verified_or_not(self):
        rows = (("pupil@example.bg", "pupil@example.bg"), (" spaced@example.bg", None), (ABSENT, None), (7, None),
                ("no-at-sign", None), ("x" * 250 + "@example.bg", None))
        number = 0
        for reported, stored in rows:
            for verified in self.VERIFIED:
                number += 1
                with self.subTest(reported=repr(reported)[:20], email_verified=repr(verified)[:10]):
                    client = self.app.test_client()
                    done = self.google_sign_in(client, subject=f"subject-{number}", email=reported,
                                               email_verified=verified)
                    self.assert_redirect(done, "/")
                    user_id = self.session(client)["uid"]
                    self.assertEqual(self.identity_rows(user_id),
                                     [(user_id, ISSUER, f"subject-{number}", "google", stored)])
                    self.assertEqual(self.query("SELECT email, role FROM users WHERE id = ?", (user_id,)),
                                     [(None, "student")])


# --------------------------------------------------------------------------- the account a link is bound to


def binding_cases():
    """What can happen to the bound account while a link is in progress."""
    return {
        "nothing": (None, True),
        "the row is missing": ("DELETE FROM users WHERE id = :uid", False),
        "the account is disabled": ("UPDATE users SET is_active = 0 WHERE id = :uid", False),
        "the account is an admin": ("UPDATE users SET role = 'admin' WHERE id = :uid", False),
        "the session key rotated": (
            "UPDATE users SET session_key = 'another-session-key-of-at-least-32-characters' WHERE id = :uid", False),
        "a student instead of a teacher": ("UPDATE users SET role = 'student' WHERE id = :uid", True),
    }


class BoundAccountTest(ServiceTestCase):
    def bound(self, account, *, siat=IAT, wall_now=IAT + 60, skd=None):
        if skd is None:
            skd = oidc_accounts.session_key_digest(SECRET, account["session_key"])
        return oidc_accounts.bound_account(self.conn, account["id"], skd, siat, secret_key=SECRET, wall_now=wall_now)

    def test_the_binding_digest(self):
        expected = hmac.new(SECRET.encode(), b"oidc-link\0" + b"the-session-key", hashlib.sha256).hexdigest()
        self.assertEqual(oidc_accounts.session_key_digest(SECRET, "the-session-key"), expected)
        self.assertEqual(oidc_accounts.session_key_digest(SECRET.encode(), "the-session-key"), expected)
        self.assertNotEqual(oidc_accounts.session_key_digest(SECRET, "another-key"), expected)
        self.assertNotEqual(oidc_accounts.session_key_digest("another secret", "the-session-key"), expected)
        self.assertTrue(oidc_accounts.digests_match(expected, expected))
        for other in (expected[:-1] + "0", "", None, 5, expected.encode()):
            self.assertFalse(oidc_accounts.digests_match(expected, other))

    def test_the_account_read_again(self):
        for name, (change, passes) in binding_cases().items():
            with self.subTest(case=name):
                account = self.add_user("teacher", email=f"t{abs(hash(name))}@example.bg", has_password=True)
                if change:
                    self.execute(change, {"uid": account["id"]})
                self.assertEqual(self.bound(account) is not None, passes)

    def test_a_session_past_its_absolute_lifetime_and_a_malformed_binding_are_refused(self):
        account = self.add_user("teacher", email="t@example.bg", has_password=True)
        month = 30 * 24 * 3600
        self.assertIsNotNone(self.bound(account, wall_now=IAT + month - 1))
        self.assertIsNone(self.bound(account, wall_now=IAT + month))
        for siat in (None, True, "1800000000", 0, IAT + 10_000):
            with self.subTest(siat=repr(siat)):
                self.assertIsNone(self.bound(account, siat=siat))
        for skd in ("0" * 64, "", "not-a-digest"):
            self.assertIsNone(self.bound(account, skd=skd))
        for uid in (None, True, "1", 0, 99999):
            self.assertIsNone(oidc_accounts.bound_account(
                self.conn, uid, "0" * 64, IAT, secret_key=SECRET, wall_now=IAT))

    def test_it_only_reads(self):
        account = self.add_user("teacher", email="t@example.bg", has_password=True)
        statements = []
        self.conn.set_trace_callback(statements.append)
        self.bound(account)
        self.assertTrue(statements)
        self.assertTrue(all(statement.lstrip().upper().startswith("SELECT") for statement in statements))
        self.assertFalse(self.conn.in_transaction)

    def test_confirm_link_gives_the_same_answer_for_every_case(self):
        for name, (change, passes) in binding_cases().items():
            with self.subTest(case=name):
                account = self.add_user("teacher", email=f"t{abs(hash(name))}@example.bg", has_password=True)
                pending = self.pending(account, sub=f"subject-{abs(hash(name))}", link_id=f"{abs(hash(name)):043d}"[:43])
                if change:
                    self.execute(change, {"uid": account["id"]})
                outcome = self.confirm(pending, account)
                self.assertEqual(outcome, oidc_accounts.LINKED if passes else oidc_accounts.FAILED)
                self.assertEqual(self.bound(account) is not None, passes)
                # Refused at steps 2 to 4: no identity is written, and the record is committed.
                self.assertIn((self.key(pending), EXPIRES), self.records())
                linked = [row for row in self.identity_rows() if row[2] == pending["sub"]]
                self.assertEqual(len(linked), 1 if passes else 0)


class BindingAtTheCallbackTest(GoogleAppTestCase):
    """The same cases through the callback, changed while it waits for Google."""

    def test_the_callback_gives_the_same_answer_after_the_exchange(self):
        for name, (change, passes) in binding_cases().items():
            if name == "the row is missing":
                continue  # the callback's own session could not load: covered by the service
            with self.subTest(case=name):
                client = self.app.test_client()
                account = self.signed_in("teacher", client=client)
                started = self.start_link(client)
                if change:
                    self.google.during_exchange = lambda change=change, uid=account["id"]: self.execute(
                        change.replace(":uid", "?"), (uid,))
                rows = len(self.consumption_rows())
                if passes:
                    returned = self.return_from_google(started, client, subject=f"s-{abs(hash(name))}")
                    self.assert_redirect(returned, "/auth/google/link/confirm")
                else:
                    with self.assertLogs("learnpilot.oidc", logging.WARNING) as logs:
                        returned = self.return_from_google(started, client, subject=f"s-{abs(hash(name))}")
                    self.assert_redirect(returned, "/account?google=failed")
                    self.assertIn("reason=account_changed", logs.output[-1])
                    with client.session_transaction() as sess:
                        self.assertNotIn(oauth_state.LINK_SLOT, sess)
                # Nothing but the callback's own record was written.
                self.assertEqual(len(self.consumption_rows()), rows + 1)
                self.assertEqual(self.identity_rows(account["id"]), [])


# --------------------------------------------------------------------------- the link decision and the write


class LinkDecisionTest(ServiceTestCase):
    """The identity comes first, in both places; then the account; then the email (P1)."""

    def setUp(self):
        super().setUp()
        self.linking = self.add_user("teacher", email="linking@example.bg", has_password=True)
        self.other = self.add_user("teacher", email="other@example.bg", has_password=True)
        self.no_password = self.add_user("student")
        self.counter = 0

    def give_identity(self, account, subject):
        with closing(sqlite3.connect(self.db_path)) as conn:
            identities.add_identity(conn, account["id"], issuer=ISSUER, subject=subject, provider="google")
            conn.commit()

    def both_places(self, account, profile):
        """The decision as the callback takes it, and as the write takes it."""
        self.counter += 1
        look = oidc_accounts.link_decision(self.conn, account["id"], profile)
        self.assertFalse(self.conn.in_transaction, "the look is read-only")
        pending = self.pending(account, sub=profile.subject, email=profile.email or "",
                               link_id=f"{self.counter:043d}")
        written = self.confirm(pending, account)
        return look, written

    def test_an_identity_already_on_this_account_is_already_linked_whatever_else_holds(self):
        self.give_identity(self.linking, "mine")
        self.give_identity(self.no_password, "passwordless")
        for account, subject in ((self.linking, "mine"), (self.no_password, "passwordless")):
            for email in (None, "other@example.bg", "linking@example.bg", "free@example.bg"):
                with self.subTest(account=account["id"], email=email):
                    look, written = self.both_places(account, GoogleProfile(subject, email))
                    self.assertEqual((look, written), (oidc_accounts.ALREADY_LINKED, oidc_accounts.ALREADY_LINKED))
        self.assertEqual(len(self.identity_rows()), 2)

    def test_an_identity_on_another_account_is_taken_whatever_else_holds(self):
        self.give_identity(self.other, "theirs")
        for account in (self.linking, self.no_password):
            for email in (None, "other@example.bg", "linking@example.bg"):
                with self.subTest(account=account["id"], email=email):
                    look, written = self.both_places(account, GoogleProfile("theirs", email))
                    self.assertEqual((look, written), (oidc_accounts.IDENTITY_TAKEN, oidc_accounts.IDENTITY_TAKEN))
        # Nothing is reassigned.
        self.assertEqual(self.identity_rows(), [(self.other["id"], ISSUER, "theirs", "google", None)])

    def test_only_then_an_account_that_may_not_link_is_not_allowed(self):
        self.give_identity(self.other, "another-identity-of-the-other")
        cases = (
            ("an account without a password", self.no_password, "fresh-1"),
            ("an account that already has a Google identity", self.other, "fresh-2"),
        )
        for name, account, subject in cases:
            for email in (None, "linking@example.bg"):  # also when the email is another account's
                with self.subTest(case=name, email=email):
                    look, written = self.both_places(account, GoogleProfile(subject, email))
                    self.assertEqual((look, written), (oidc_accounts.NOT_ALLOWED, oidc_accounts.NOT_ALLOWED))
        self.assertEqual(len(self.identity_rows()), 1)

    def test_only_then_the_email_of_another_account_refuses_the_link(self):
        # Decision P1.
        for reported in ("other@example.bg", "Other@Example.BG", " other@example.bg "):
            with self.subTest(reported=repr(reported)):
                look, written = self.both_places(self.linking, GoogleProfile("fresh", reported))
                self.assertEqual((look, written), (oidc_accounts.EMAIL_OTHER_ACCOUNT,) * 2)
        self.assertEqual(self.identity_rows(), [])

    def test_the_linking_accounts_own_email_no_accounts_email_and_an_unusable_one_do_not(self):
        cases = (("own", "Linking@Example.BG", "Linking@Example.BG"), ("free", "free@example.bg", "free@example.bg"),
                 ("absent", None, None), ("unusable", "no-at-sign", None), ("spaced", " free2@example.bg", None))
        for subject, reported, stored in cases:
            with self.subTest(reported=repr(reported)):
                account = self.add_user("teacher", email=f"{subject}-account@example.bg", has_password=True)
                if subject == "own":
                    account = self.linking
                profile = GoogleProfile(f"subject-{subject}", reported)
                self.assertIsNone(oidc_accounts.link_decision(self.conn, account["id"], profile))
                pending = self.pending(account, sub=profile.subject, email=oidc_accounts.reported_email(reported),
                                       link_id=subject.ljust(43, "x"))
                self.assertEqual(self.confirm(pending, account), oidc_accounts.LINKED)
                self.assertIn((account["id"], ISSUER, f"subject-{subject}", "google", stored), self.identity_rows())


class ConfirmLinkTest(ServiceTestCase):
    def setUp(self):
        super().setUp()
        self.account = self.add_user("teacher", email="teacher@example.bg", has_password=True)

    def test_the_happy_path_stores_one_identity_and_its_record_in_the_same_commit(self):
        pending = self.pending(self.account, email="Teacher.Google@Example.BG", name="Учител")
        self.conn.execute("BEGIN IMMEDIATE")
        outcome = oidc_accounts.confirm_link(
            self.conn, pending, secret_key=SECRET, session_key=self.account["session_key"], session_iat=IAT,
            now=NOW, wall_now=IAT + 60)
        self.assertEqual(outcome, oidc_accounts.LINKED)
        # Neither is visible before the caller commits; both are after.
        self.assertTrue(self.conn.in_transaction)
        self.assertEqual((self.identity_rows(), self.records()), ([], []))
        self.conn.commit()
        self.assertEqual(self.identity_rows(),
                         [(self.account["id"], ISSUER, "google-subject-1", "google", "Teacher.Google@Example.BG")])
        self.assertEqual(self.records(), [(self.key(pending), EXPIRES)])
        # A link changes no session key and no name.
        row = self.query("SELECT session_key, display_name FROM users WHERE id = ?", (self.account["id"],))
        self.assertEqual(row, [(self.account["session_key"], "Тест Потребител")])

    def test_an_expired_pending_link_executes_no_statement(self):
        pending = self.pending(self.account)
        login_throttle.record_failure(self.conn, "k" * 64, datetime(2026, 1, 1, tzinfo=timezone.utc))
        self.conn.commit()
        for now in (EXPIRES, EXPIRES + 1, EXPIRES + 10_000):
            with self.subTest(now=now):
                statements = []
                self.conn.execute("BEGIN IMMEDIATE")
                login_throttle.clear(self.conn, "k" * 64)   # the caller's earlier write
                self.conn.set_trace_callback(statements.append)
                outcome = oidc_accounts.confirm_link(
                    self.conn, pending, secret_key=SECRET, session_key=self.account["session_key"],
                    session_iat=IAT, now=now, wall_now=IAT + 60)
                self.conn.set_trace_callback(None)
                self.assertEqual((outcome, statements), (oidc_accounts.EXPIRED, []))
                self.conn.commit()
        # Neither a record nor an identity; what the caller wrote earlier was committed.
        self.assertEqual((self.records(), self.identity_rows()), ([], []))
        self.assertEqual(self.query("SELECT COUNT(*) FROM login_throttle"), [(0,)])
        self.assertEqual(self.confirm(pending, self.account, now=EXPIRES - 1), oidc_accounts.LINKED)

    def test_an_already_used_pending_link_stores_nothing_more(self):
        pending = self.pending(self.account)
        self.assertEqual(self.confirm(pending, self.account), oidc_accounts.LINKED)
        before = self.identity_rows(), self.records()
        login_throttle.record_failure(self.conn, "k" * 64, datetime(2026, 1, 1, tzinfo=timezone.utc))
        self.conn.commit()

        statements = []
        self.conn.execute("BEGIN IMMEDIATE")
        login_throttle.clear(self.conn, "k" * 64)
        self.conn.set_trace_callback(statements.append)
        outcome = oidc_accounts.confirm_link(
            self.conn, pending, secret_key=SECRET, session_key=self.account["session_key"], session_iat=IAT,
            now=NOW + 5, wall_now=IAT + 60)
        self.conn.set_trace_callback(None)
        self.assertEqual(outcome, oidc_accounts.ALREADY_LINKED)
        writes = [s for s in statements if not s.lstrip().upper().startswith("SELECT")]
        # Only the storage's cleanup of expired records and the refused insert.
        self.assertEqual(len(writes), 2)
        self.assertTrue(writes[0].startswith("DELETE FROM oidc_consumed_transactions WHERE expires_at <"))
        self.assertTrue(writes[1].startswith("INSERT INTO oidc_consumed_transactions"))
        self.assertTrue(self.conn.in_transaction)
        self.conn.commit()
        self.assertEqual((self.identity_rows(), self.records()), before)
        self.assertEqual(self.query("SELECT COUNT(*) FROM login_throttle"), [(0,)])

    def test_an_already_used_pending_link_without_the_identity_is_failed(self):
        pending = self.pending(self.account)
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute("INSERT INTO oidc_consumed_transactions (tx_hash, expires_at) VALUES (?, ?)",
                         (self.key(pending), EXPIRES))
            conn.commit()
        self.assertEqual(self.confirm(pending, self.account), oidc_accounts.FAILED)
        # On another account the identity is not "already linked" for this one either.
        other = self.add_user("teacher", email="other@example.bg", has_password=True)
        with closing(sqlite3.connect(self.db_path)) as conn:
            identities.add_identity(conn, other["id"], issuer=ISSUER, subject=pending["sub"], provider="google")
            conn.commit()
        self.assertEqual(self.confirm(pending, self.account), oidc_accounts.FAILED)

    def test_a_session_that_is_not_the_bound_one_is_refused_and_the_link_is_spent(self):
        cases = {
            "another session of the account": dict(session_iat=IAT + 5),
            "a session with another key": dict(session_key="not-the-bound-key"),
            "no session key at all": dict(session_key=5),
        }
        for number, (name, arguments) in enumerate(cases.items()):
            with self.subTest(case=name):
                pending = self.pending(self.account, link_id=f"{number:043d}", sub=f"subject-{number}")
                self.assertEqual(self.confirm(pending, self.account, **arguments), oidc_accounts.FAILED)
                self.assertIn((self.key(pending), EXPIRES), self.records())
        self.assertEqual(self.identity_rows(), [])

    def test_a_bound_session_past_its_absolute_lifetime_is_refused(self):
        pending = self.pending(self.account)
        outcome = self.confirm(pending, self.account, wall_now=IAT + 30 * 24 * 3600)
        self.assertEqual(outcome, oidc_accounts.FAILED)
        self.assertEqual((self.identity_rows(), self.records()), ([], [(self.key(pending), EXPIRES)]))

    def test_an_exception_between_the_record_and_the_identity_leaves_neither(self):
        pending = self.pending(self.account)
        with patch.object(identities, "add_identity", side_effect=sqlite3.OperationalError("disk I/O error")):
            with self.assertRaises(sqlite3.OperationalError):
                self.confirm(pending, self.account)
        self.assertEqual((self.identity_rows(), self.records()), ([], []))
        # A second attempt goes through the same steps.
        self.assertEqual(self.confirm(pending, self.account), oidc_accounts.LINKED)

    def test_the_key_and_the_expiry_are_the_pending_links_own(self):
        pending = self.pending(self.account, exp=NOW + 77)
        with patch.object(oidc_consumption, "consume", wraps=oidc_consumption.consume) as consume:
            self.confirm(pending, self.account)
        (conn, key), keywords = consume.call_args
        self.assertEqual((key, keywords), (self.key(pending), {"now": NOW, "expires_at": NOW + 77}))
        self.assertNotEqual(key, oidc_consumption.consumption_key(SECRET, "callback", pending["id"]))

    def test_an_older_python_reports_a_repeated_key_as_an_error_never_as_an_acceptance(self):
        pending = self.pending(self.account)
        self.confirm(pending, self.account)
        with patch.object(oidc_consumption, "consume", side_effect=sqlite3.IntegrityError("UNIQUE constraint failed")):
            with self.assertRaises(sqlite3.IntegrityError):
                self.confirm(pending, self.account)
        self.assertEqual(len(self.identity_rows()), 1)

    def test_eight_simultaneous_confirmations_link_once(self):
        pending = self.pending(self.account)
        barrier = threading.Barrier(8)

        def racer(_):
            conn = self.connect()
            barrier.wait(10)
            return self.confirm(pending, self.account, conn=conn)

        with ThreadPoolExecutor(max_workers=8) as pool:
            outcomes = sorted(pool.map(racer, range(8)))
        self.assertEqual(outcomes, [oidc_accounts.ALREADY_LINKED] * 7 + [oidc_accounts.LINKED])
        self.assertEqual((len(self.identity_rows()), len(self.records())), (1, 1))

    def test_two_accounts_confirming_one_google_identity_at_once(self):
        other = self.add_user("teacher", email="other@example.bg", has_password=True)
        pendings = [(self.account, self.pending(self.account, sub="contested", link_id="a" * 43)),
                    (other, self.pending(other, sub="contested", link_id="b" * 43))]
        barrier = threading.Barrier(2)

        def racer(pair):
            account, pending = pair
            conn = self.connect()
            barrier.wait(10)
            return account["id"], self.confirm(pending, account, conn=conn)

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = dict(pool.map(racer, pendings))
        self.assertEqual(sorted(results.values()), [oidc_accounts.IDENTITY_TAKEN, oidc_accounts.LINKED])
        winner = next(user_id for user_id, outcome in results.items() if outcome == oidc_accounts.LINKED)
        # The first commit wins and nothing is reassigned.
        self.assertEqual(self.identity_rows(), [(winner, ISSUER, "contested", "google", None)])
        self.assertEqual(len(self.records()), 2)

    def test_a_first_sign_in_against_a_confirmation_of_the_same_new_identity(self):
        for order in ("sign-in first", "confirmation first", "at once"):
            with self.subTest(order=order):
                subject = "contested-" + order.replace(" ", "-")
                account = self.add_user("teacher", email=f"{abs(hash(order))}@example.bg", has_password=True)
                pending = self.pending(account, sub=subject, link_id=f"{abs(hash(order)):043d}"[:43])
                profile = GoogleProfile(subject, None, "Нов Ученик")
                if order == "sign-in first":
                    signed = self.sign_in(profile)
                    confirmed = self.confirm(pending, account)
                elif order == "confirmation first":
                    confirmed = self.confirm(pending, account)
                    signed = self.sign_in(profile)
                else:
                    barrier = threading.Barrier(2)

                    def run(action):
                        conn = self.connect()
                        barrier.wait(10)
                        return action(conn)

                    with ThreadPoolExecutor(max_workers=2) as pool:
                        first = pool.submit(run, lambda conn: self.sign_in(profile, conn))
                        second = pool.submit(run, lambda conn: self.confirm(pending, account, conn=conn))
                        signed, confirmed = first.result(20), second.result(20)
                owners = [row[0] for row in self.identity_rows() if row[2] == subject]
                self.assertEqual(len(owners), 1, "one identity, for good")
                if confirmed == oidc_accounts.LINKED:
                    # The identity belongs to the confirming account, and the sign-in signed that account in.
                    self.assertEqual(owners, [account["id"]])
                    self.assertEqual((signed.outcome, signed.user["id"]), (oidc_accounts.SIGNED_IN, account["id"]))
                else:
                    # It belongs to a new student, and the link was refused.
                    self.assertEqual(confirmed, oidc_accounts.IDENTITY_TAKEN)
                    self.assertEqual(signed.outcome, oidc_accounts.CREATED)
                    self.assertEqual(owners, [signed.user["id"]])


class ServiceBoundaryTest(unittest.TestCase):
    def test_the_service_imports_no_flask_and_no_controller(self):
        tree = ast.parse((ROOT / "web/services/oidc_accounts.py").read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add(node.module or "")
                imported.update(f"{node.module}.{alias.name}" for alias in node.names)
        self.assertFalse({name for name in imported if name.split(".")[0] in ("flask", "werkzeug")})
        self.assertEqual({name for name in imported if name.startswith("web")}, {
            "web.services", "web.services.accounts", "web.services.google_oidc", "web.services.identities",
            "web.services.oidc_consumption"})
        source = (ROOT / "web/services/oidc_accounts.py").read_text(encoding="utf-8")
        # The caller owns the transaction, and nothing here writes a log line.
        for needle in (".commit(", ".rollback(", ".execute(", "import logging", "print("):
            self.assertFalse(needle in source, needle)

    def test_the_issuer_and_the_provider_are_the_pinned_constants(self):
        self.assertEqual((google_oidc.ISSUER, google_oidc.PROVIDER), (ISSUER, "google"))
        self.assertIn("google", identities.PROVIDERS)
        self.assertEqual(oidc_accounts.LINKABLE_ROLES, ("student", "teacher"))


if __name__ == "__main__":
    unittest.main()
