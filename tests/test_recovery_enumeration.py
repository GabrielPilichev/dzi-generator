"""Password recovery 8.3: what a browser can learn, route by route.

Test plan RG1 to RG7, RPoll (HTTP level), RE1 to RE8 and RK at the routes, RLog2/RLog5 and
the 'correct code first' rule of design 1.4 and 2.4: for every well-formed address the answers a
browser sees are the same until it presents a correct code, and nothing another browser does
short of a correct code changes what this browser sees (RD17). No test measures or asserts a
duration, equal delivery or equal provider behaviour (design 1.4 claims none).
Everything runs against temporary databases with an in-memory mailer.
"""

import logging
import re
import sqlite3
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from tests.recovery_route_support import EMAIL, NEW_PASSWORD, OLD_PASSWORD, RecoveryRouteTestCase
from tests.recovery_service_support import normalized
from web.blueprints import recovery as recovery_blueprint
from web.services import accounts, recovery as recovery_service, recovery_scrub
from web.services.mail import SendOutcome

WRONG = "00000000"
INVALIDATED = "Не успяхме да зададем нова парола. Започни отново."
NO_RECOVERY = "Заявката за нова парола е изтекла или е започната от друг браузър. Започни отново."


class FixtureMixin:
    """One account of each kind of the eligibility table of design 1.3, built through the real functions."""

    ELIGIBLE = ("student.verified@example.bg", "teacher.verified@example.bg")

    def fixtures(self):
        self.verified(self.ELIGIBLE[0], "student")
        self.verified(self.ELIGIBLE[1], "teacher")
        self.cli_account("student.cli@example.bg", "student")
        self.cli_account("teacher.cli@example.bg", "teacher")
        self.admin = self.make("admin", email="admin.person@example.bg")
        disabled = self.verified("disabled@example.bg", "student")
        self.cli("disable", "--id", str(disabled["id"]))
        from tests.auth_helpers import create_account
        create_account(self.db_path, "student", email="no.password@example.bg", password=None)
        promoted = self.verified("promoted@example.bg", "student")
        self.cli("set-role", "--id", str(promoted["id"]), "--role", "admin")
        return {
            "verified student": self.ELIGIBLE[0],
            "verified teacher": self.ELIGIBLE[1],
            "cli student": "student.cli@example.bg",
            "cli teacher": "teacher.cli@example.bg",
            "admin": "admin.person@example.bg",
            "disabled": "disabled@example.bg",
            "no password": "no.password@example.bg",
            "verified, promoted to admin": "promoted@example.bg",
            "no account": "nobody@example.bg",
        }


class SameAnswerTest(FixtureMixin, RecoveryRouteTestCase):
    """RG1, RG2, RG7: the same visible treatment for every well-formed address."""

    def journey(self, address):
        """One browser's journey for ``address``: the observations of every step, and the row it left."""
        client = self.app.test_client()
        observed = []

        def step(response):
            observed.append(self.observe(response, address))
            return response

        step(self.begin(address, client=client))
        step(client.get("/recover/verify"))
        step(self.redeem(WRONG, client=client))
        step(self.resend(client=client))                 # inside the wait
        self.advance(61)
        step(self.resend(client=client))
        step(client.get("/recover/verify?resent=1"))
        step(self.redeem(WRONG, client=client))
        shape = self.session_shape(client)
        row = self.rows("SELECT state, code_attempts, send_count FROM password_recoveries ORDER BY id DESC")[0]
        step(self.cancel(client=client))
        self.advance(1)
        return observed, shape, tuple(row.values())

    def test_every_address_gets_the_same_status_location_page_headers_and_cookie_shape(self):
        addresses = self.fixtures()
        baseline_name = "verified student"
        results = {}
        with patch.object(accounts, "generate_password_hash", side_effect=AssertionError("scrypt")), \
                patch.object(accounts, "check_password_hash", side_effect=AssertionError("scrypt")):
            for name, address in addresses.items():
                results[name] = self.journey(address)
        baseline, shape, row = results[baseline_name]
        self.assertEqual([entry[0] for entry in baseline], [303, 200, 200, 200, 303, 200, 200, 303])
        for name, (observed, shape_, row_) in results.items():
            with self.subTest(address=name):
                self.assertEqual(shape_, shape)
                self.assertEqual(row_, row)
                for position, (got, expected) in enumerate(zip(observed, baseline)):
                    self.assertEqual(got, expected, f"step {position}")

    def test_the_same_accounting_for_every_address(self):
        """RG2: one pending row, two ledger rows (request and resend), one counted attempt, two messages
        and no password hashing, whatever the address; nothing is measured about durations."""
        addresses = self.fixtures()
        counts = {}
        for name, address in addresses.items():
            sent = len(self.mailer.messages)
            before_rows = {table: len(self.rows(f"SELECT * FROM {table}")) for table in
                           ("password_recoveries", "registration_sends")}
            self.journey(address)
            counts[name] = (
                len(self.mailer.messages) - sent,
                len(self.rows("SELECT * FROM password_recoveries")) - before_rows["password_recoveries"],
                len(self.rows("SELECT * FROM registration_sends")) - before_rows["registration_sends"],
                len(self.rows("SELECT * FROM registration_sends WHERE email_hash = ?",
                              registration_email_hash(self, address))),
            )
        self.assertEqual(set(counts.values()), {(2, 1, 2, 2)}, counts)

    def test_the_verify_page_sentence_and_the_login_link_are_the_same_for_every_address(self):
        """RG7: conditional, so true for every row; /login itself answers the standard message."""
        addresses = self.fixtures()
        pages = set()
        for name, address in addresses.items():
            client = self.app.test_client()
            self.recover(address, client=client)
            self.advance(1)
            pages.add(self.observe(client.get("/recover/verify"), address)[2])
        self.assertEqual(len(pages), 1)
        for name, address in addresses.items():
            response = self.login(address, "not the password")
            with self.subTest(address=name):
                self.assertIn("Грешен имейл или парола.", self.body(response))

    def test_the_statements_the_request_runs_do_not_depend_on_the_account(self):
        """RG5, at the route: the same sequence of statements for a real and a shadow request and resend
        (no account-dependent step of the application's own between receiving the request and the
        send, and after it). Not a timing measurement."""
        addresses = self.fixtures()
        sequences = {}
        for kind, address in (("real", self.ELIGIBLE[0]), ("shadow", "nobody@example.bg"),
                              ("cli", "student.cli@example.bg"), ("admin", "admin.person@example.bg")):
            client = self.app.test_client()
            seen = []
            real = recovery_blueprint.get_request_db
            traced = {"conn": None}

            def spying(real=real, seen=seen, traced=traced):
                conn = real()
                if traced["conn"] is not conn:
                    traced["conn"] = conn
                    conn.set_trace_callback(seen.append)
                return conn

            with patch.object(recovery_blueprint, "get_request_db", spying):
                self.recover(address, client=client)
                self.advance(61)
                self.assertEqual(self.resend(client=client).status_code, 303)
            sequences[kind] = normalized(seen)
            self.advance(1)
        self.assertGreater(len(sequences["real"]), 10)
        for kind in ("shadow", "cli", "admin"):
            self.assertEqual(sequences[kind], sequences["real"], kind)
        self.assertEqual(len(addresses), 9)


def registration_email_hash(test, address):
    from web.services import registration as registration_service

    return registration_service.email_hash(test.app.config["SECRET_KEY"], address)


class SendOutcomeTest(FixtureMixin, RecoveryRouteTestCase):
    """RG3, RG4, RG6: how the relay answers changes nothing a browser can see (RD7)."""

    def sequence(self, address, mailer_outcome):
        """A request, three wrong codes and a resend; the observations of each step."""
        self.mailer.outcome = mailer_outcome
        client = self.app.test_client()
        steps = [self.begin(address, client=client)]
        steps += [self.redeem(WRONG, client=client) for _ in range(3)]
        self.advance(61)
        steps.append(self.resend(client=client))
        steps.append(client.get("/recover/verify?resent=1"))
        shape = self.session_shape(client)
        row = self.rows("SELECT state, code_attempts, send_count, resend_after FROM password_recoveries "
                        "ORDER BY id DESC")[0]
        # Relative: the wait and the expiry as seconds from now, so two runs compare.
        wait = (datetime.strptime(row["resend_after"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                - self.now).total_seconds()
        self.advance(1)
        return [self.observe(response, address) for response in steps], shape, (
            row["state"], row["code_attempts"], row["send_count"], wait)

    def fresh(self, kind):
        """A new address (the per-address cap is five sends an hour), real or shadow."""
        self.counter = getattr(self, "counter", 0) + 1
        address = f"{kind}{self.counter}@example.bg"
        if kind == "real":
            self.verified(address)
        return address

    def reset_circuit(self):
        """An unrelated accepted send, which closes the shared circuit after a run of failures."""
        self.mailer.outcome = SendOutcome.ACCEPTED
        self.recover(self.fresh("shadow"), client=self.app.test_client())
        self.advance(1)

    def test_not_sent_refused_and_unknown_look_like_accepted_for_a_real_and_a_shadow_row(self):
        baseline = {kind: self.sequence(self.fresh(kind), SendOutcome.ACCEPTED) for kind in ("real", "shadow")}
        self.assertEqual(baseline["real"][1:], baseline["shadow"][1:])
        for outcome in (SendOutcome.NOT_SENT, SendOutcome.REFUSED, SendOutcome.UNKNOWN):
            for kind in ("real", "shadow"):
                self.reset_circuit()
                with self.subTest(outcome=outcome.value, row=kind):
                    observed, shape, row = self.sequence(self.fresh(kind), outcome)
                    self.assertEqual(shape, baseline[kind][1])
                    self.assertEqual(row, baseline[kind][2])
                    # The pages: the same sentence and the same step answers (the redirects after a
                    # resend and the notice that follows it included).
                    for position, (got, wanted) in enumerate(zip(observed, baseline[kind][0])):
                        self.assertEqual(got[:3], wanted[:3], f"step {position}")

    def test_a_not_sent_real_code_still_counts_attempts_up_to_too_many(self):
        self.verified()
        self.mailer.outcome = SendOutcome.NOT_SENT
        self.recover()
        code = self.last_code()
        for _ in range(5):
            self.assertIn("Грешен или изтекъл код.", self.body(self.redeem(WRONG)))
        self.assertIn("Твърде много грешни опити. Поискай нов код.", self.body(self.redeem(code)))
        self.assertEqual(self.sends()[0]["outcome"], "not_sent")
        self.assertEqual(self.recoveries()[0]["state"], "pending")      # not retired (RD7)

    def test_a_backend_that_refuses_notices_and_accepts_codes_and_the_reverse_changes_no_response(self):
        notice_subject = recovery_service.NOTICE_SUBJECT
        for label, outcome in (
                ("notices refused", lambda message: SendOutcome.REFUSED if message.subject == notice_subject
                 else SendOutcome.ACCEPTED),
                ("codes refused", lambda message: SendOutcome.ACCEPTED if message.subject == notice_subject
                 else SendOutcome.REFUSED)):
            observed = {}
            for kind in ("real", "shadow"):
                self.reset_circuit()
                observed[kind] = self.sequence(self.fresh(kind), outcome)
            with self.subTest(backend=label):
                self.assertEqual(observed["real"][1:], observed["shadow"][1:])
                for got, wanted in zip(observed["real"][0], observed["shadow"][0]):
                    self.assertEqual(got[:3], wanted[:3])

    def test_a_one_sided_stall_closes_recovery_for_every_address_alike(self):
        """RG6, the documented residual: three failures in a row close the shared circuit for everyone."""
        self.fixtures()
        self.mailer.outcome = SendOutcome.NOT_SENT
        for index in range(3):
            self.recover(f"stall{index}@example.bg", client=self.app.test_client())
            self.advance(1)
        seen = set()
        for address in ("student.verified@example.bg", "nobody@example.bg", "student.cli@example.bg",
                        "admin.person@example.bg"):
            response = self.begin(address, client=self.app.test_client())
            seen.add((response.status_code, self.observe(response, address)[2]))
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen.pop()[0], 503)
        # An accepted settlement of any kind reopens it (nothing here sends one while it is closed).
        self.assertEqual(len(self.mailer.messages), 3)


class PollingTest(FixtureMixin, RecoveryRouteTestCase):
    """RPoll1 to RPoll8 at the HTTP level (RD17): two browsers, one asks and watches, one resets.

    Browser A requests recovery for an address and polls; browser O requests for the same address
    and redeems. A's observations are compared with those of an identical A at an address nobody
    resets (the control world), step by step, so the claim is exactly 'the reset changes nothing A
    sees before a correct code'. A's mailbox is open, so A's codes can be read where the test says so.
    """

    CONTROL = "control@example.bg"
    RESET = "reset@example.bg"

    def row_state(self, address_user_row):
        """A's row as a relative description: nothing that differs between two requests by construction."""
        row = self.rows("SELECT state, is_shadow, code_attempts, send_count, resend_after, code_expires_at, "
                        "expires_at FROM password_recoveries WHERE id = ?", address_user_row)[0]
        def relative(stamp):
            if stamp is None:
                return None
            return (datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                    - self.now).total_seconds()
        return (row["state"], row["is_shadow"], row["code_attempts"], row["send_count"],
                relative(row["resend_after"]), relative(row["code_expires_at"]), relative(row["expires_at"]))

    def worlds(self, a_resets_between):
        """Run A's polling in two worlds and return the paired observations.

        ``a_resets_between`` is a list of (label, step) pairs; each step takes (client, address).
        O resets the RESET address after the first ``split`` steps, never the CONTROL address.
        """
        raise NotImplementedError

    def make_a(self, address):
        client = self.app.test_client()
        self.recover(address, client=client)
        row = max(row["id"] for row in self.recoveries())
        return client, row

    def reset_as_owner(self, address):
        owner = self.app.test_client()
        self.advance(1)
        self.recover(address, client=owner)
        response = self.redeem(self.last_code(), NEW_PASSWORD, client=owner)
        self.assertEqual(response.status_code, 303, self.body(response))

    def test_a_reset_changes_nothing_a_browser_sees_before_a_correct_code(self):
        self.verified(self.CONTROL)
        self.verified(self.RESET)
        a_control, row_control = self.make_a(self.CONTROL)
        a_reset, row_reset = self.make_a(self.RESET)
        steps = [
            ("poll", lambda client, address: client.get("/recover/verify")),
            ("wrong", lambda client, address: self.redeem(WRONG, client=client)),
            ("poll again", lambda client, address: client.get("/recover/verify")),
            ("wrong", lambda client, address: self.redeem(WRONG, client=client)),
            ("resend", lambda client, address: self.resend(client=client)),
            ("resent notice", lambda client, address: client.get("/recover/verify?resent=1")),
            ("wrong", lambda client, address: self.redeem(WRONG, client=client)),
            ("wrong", lambda client, address: self.redeem(WRONG, client=client)),
            ("wrong", lambda client, address: self.redeem(WRONG, client=client)),
            ("wrong", lambda client, address: self.redeem(WRONG, client=client)),
            ("wrong (the sixth since the resend)", lambda client, address: self.redeem(WRONG, client=client)),
        ]
        shape_before = self.session_shape(a_reset)
        for position, (label, step) in enumerate(steps):
            if position == 2:
                # O's reset: committed between A's second and third observation. Never A's address
                # in the control world.
                self.reset_as_owner(self.RESET)
            if label == "resend":
                self.advance(61)
            mails = len(self.mailer.messages)
            control = step(a_control, self.CONTROL)
            mails_control = len(self.mailer.messages) - mails
            mails = len(self.mailer.messages)
            reset = step(a_reset, self.RESET)
            mails_reset = len(self.mailer.messages) - mails
            with self.subTest(step=position, label=label):
                self.assertEqual(self.observe(reset, self.RESET), self.observe(control, self.CONTROL))
                self.assertEqual(mails_reset, mails_control)
                self.assertEqual(self.row_state(row_reset), self.row_state(row_control))
                self.assertEqual(self.session_shape(a_reset)[0], self.session_shape(a_control)[0])
        self.assertEqual(shape_before, self.session_shape(a_reset))
        # The sixth wrong code was refused as 'too many' in both worlds (the pages were equal above).
        self.assertEqual(self.row_state(row_reset)[2], 5)

    def test_a_cancel_after_the_reset_is_the_same_303_and_voids_the_row(self):
        self.verified(self.CONTROL)
        self.verified(self.RESET)
        a_control, row_control = self.make_a(self.CONTROL)
        a_reset, row_reset = self.make_a(self.RESET)
        self.reset_as_owner(self.RESET)
        control = self.cancel(client=a_control)
        reset = self.cancel(client=a_reset)
        self.assertEqual(self.observe(reset, self.RESET), self.observe(control, self.CONTROL))
        self.assertEqual((reset.status_code, reset.headers["Location"]), (303, "/recover"))
        for row in (row_control, row_reset):
            self.assertEqual(self.rows("SELECT state, email FROM password_recoveries WHERE id = ?", row),
                             [{"state": "void", "email": None}])

    def test_the_correct_code_of_another_row_after_the_reset_is_invalidated_not_redeemed(self):
        """RPoll5, RV3: A holds the mailbox; its row was issued under the old key."""
        self.verified(self.RESET)
        a, row = self.make_a(self.RESET)
        code = self.last_code()
        digest = self.account(self.rows("SELECT user_id FROM password_recoveries WHERE id = ?", row)[0]["user_id"])
        self.advance(1)
        self.reset_as_owner(self.RESET)
        owner_row = self.recoveries()[-1]
        before_other = self.rows("SELECT * FROM password_recoveries WHERE id = ?", row)[0]
        self.assertEqual(before_other["state"], "pending")        # not written by the reset (RD17)
        users_before = self.rows("SELECT * FROM users WHERE email = ?", self.RESET)[0]
        response = self.redeem(code, "another new password!", client=a)
        self.assertEqual(response.status_code, 200)
        self.assertIn(INVALIDATED, self.body(response))
        self.assertIsNone(self.slot(a))
        self.assertNoAccountSession(a)
        self.assertEqual(self.rows("SELECT * FROM users WHERE email = ?", self.RESET)[0], users_before)
        self.assertTrue(self.authenticates(self.RESET, NEW_PASSWORD))
        self.assertFalse(self.authenticates(self.RESET, "another new password!"))
        stored = self.rows("SELECT state, email, code_hash FROM password_recoveries WHERE id = ?", row)[0]
        self.assertEqual(stored, {"state": "void", "email": None, "code_hash": None})
        self.assertEqual(owner_row["state"], "redeemed")
        self.assertEqual(digest["email"], self.RESET)

    def test_a_resend_after_the_reset_binds_to_the_rotated_key_and_its_code_redeems(self):
        """RPoll3: the new code is the mailbox's; one issued before the reset ends INVALIDATED."""
        self.verified(self.RESET)
        a, row = self.make_a(self.RESET)
        old_code = self.last_code()
        self.reset_as_owner(self.RESET)
        self.advance(61)
        self.assertEqual(self.resend(client=a).status_code, 303)
        new_code = self.last_code()
        self.assertIn(INVALIDATED, self.body(self.redeem(old_code, "yet another passphrase", client=a))) \
            if old_code == new_code else None
        response = self.redeem(new_code, "yet another passphrase", client=a)
        self.assertEqual(response.status_code, 303, self.body(response))
        self.assertTrue(self.authenticates(self.RESET, "yet another passphrase"))

    def test_a_row_expires_on_its_own_schedule_and_is_scrubbed_by_the_cleanup_not_by_a_reset(self):
        """RPoll6: 30 minutes after its creation whether or not another browser reset; no earlier scrub."""
        self.verified(self.CONTROL)
        self.verified(self.RESET)
        a_control, row_control = self.make_a(self.CONTROL)
        a_reset, row_reset = self.make_a(self.RESET)
        self.reset_as_owner(self.RESET)
        self.advance(25 * 60)
        for row in (row_control, row_reset):                      # before its time: pending, with its data
            self.assertEqual(self.rows("SELECT state FROM password_recoveries WHERE id = ?", row)[0]["state"],
                             "pending")
        self.assertEqual(self.row_state(row_reset)[0], self.row_state(row_control)[0])
        self.advance(6 * 60)                                       # 31 minutes after the request
        control = self.redeem(WRONG, client=a_control)
        reset = self.redeem(WRONG, client=a_reset)
        self.assertEqual(self.observe(reset, self.RESET), self.observe(control, self.CONTROL))
        self.assertIn(NO_RECOVERY, self.body(reset))
        # Read-only POSTs: the rows are still pending and keep their data until the cleanup runs.
        for row in (row_control, row_reset):
            stored = self.rows("SELECT state, email FROM password_recoveries WHERE id = ?", row)[0]
            self.assertEqual(stored["state"], "pending")
            self.assertIsNotNone(stored["email"])
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute("BEGIN IMMEDIATE")
            recovery_scrub.cleanup(conn, self.now)
            conn.commit()
        for row in (row_control, row_reset):
            self.assertEqual(self.rows("SELECT state, email, code_hash FROM password_recoveries WHERE id = ?", row),
                             [{"state": "expired", "email": None, "code_hash": None}])

    def test_a_shadow_row_is_polled_the_same_whatever_happens_to_an_unrelated_account(self):
        """RPoll7: an address without an account cannot be told from one that is being reset."""
        self.verified(self.RESET)
        a_shadow, row_shadow = self.make_a("nobody@example.bg")
        a_control, row_control = self.make_a("other.nobody@example.bg")
        self.reset_as_owner(self.RESET)
        for label, step in (("poll", lambda c: c.get("/recover/verify")),
                            ("wrong", lambda c: self.redeem(WRONG, client=c)),
                            ("resend wait", lambda c: self.resend(client=c))):
            with self.subTest(step=label):
                shadow, control = step(a_shadow), step(a_control)
                self.assertEqual(self.observe(shadow, "nobody@example.bg"),
                                 self.observe(control, "other.nobody@example.bg"))
        self.assertEqual(self.row_state(row_shadow), self.row_state(row_control))

    def test_the_reset_does_not_touch_the_sign_in_throttle_and_the_documented_residuals_stand(self):
        """RPoll8, RV4, RT4 at /login: the throttle is neither read nor written by recovery."""
        self.verified(self.RESET)
        self.fail_login(self.RESET, 10)
        locked = self.throttle_rows()
        self.assertEqual(self.login(self.RESET, "wrong password").status_code, 429)
        owner = self.app.test_client()
        self.recover(self.RESET, client=owner)
        for _ in range(3):
            self.redeem(WRONG, client=owner)                       # wrong codes: not counted at /login
        self.assertEqual(self.throttle_rows(), locked)
        self.assertEqual(self.redeem(self.last_code(), NEW_PASSWORD, client=owner).status_code, 303)
        self.assertEqual(self.throttle_rows(), locked)              # a reset clears nothing (RD8 (a))
        self.assertEqual(self.login(self.RESET, NEW_PASSWORD).status_code, 429)
        self.assertEqual(self.login(self.RESET, "wrong password").status_code, 429)

    def test_recovery_reads_and_writes_no_throttle_row(self):
        """RV4/RT4: no ``login_throttle`` statement on any recovery route, for any outcome."""
        self.verified(self.RESET)
        seen = []
        real = recovery_blueprint.get_request_db
        traced = {"conn": None}

        def spying():
            conn = real()
            if traced["conn"] is not conn:
                traced["conn"] = conn
                conn.set_trace_callback(seen.append)
            return conn

        with patch.object(recovery_blueprint, "get_request_db", spying):
            client = self.app.test_client()
            self.recover(self.RESET, client=client)
            for _ in range(6):
                self.redeem(WRONG, client=client)
            self.advance(61)
            self.resend(client=client)
            self.redeem(self.last_code(), NEW_PASSWORD, client=client)
        self.assertGreater(len(seen), 20)
        self.assertFalse([sql for sql in seen if "login_throttle" in sql])

    def test_after_the_reset_the_owners_next_sign_in_clears_the_throttle_as_every_sign_in_does(self):
        """The residual RD8 (a) documents: an observer part-way through a count notices the later,
        non-reset-specific clearing, and sustained relocking denies sign-in under any option."""
        self.verified(self.RESET)
        self.fail_login(self.RESET, 4)
        owner = self.app.test_client()
        self.recover(self.RESET, client=owner)
        self.redeem(self.last_code(), NEW_PASSWORD, client=owner)
        self.assertEqual(self.throttle_rows()[0][1], 4)             # untouched by the reset
        self.assertEqual(self.login(self.RESET, NEW_PASSWORD).status_code, 302)   # the owner signs in
        self.assertEqual(self.throttle_rows(), [])                  # cleared by the sign-in itself
        self.client = self.app.test_client()
        statuses = []
        with patch.object(accounts, "check_password_hash", return_value=False):
            for _ in range(11):
                statuses.append(self.login(self.RESET, "wrong password").status_code)
        self.assertEqual(statuses, [200] * 10 + [429])             # ten further failures, not six
        self.assertEqual(self.login(self.RESET, NEW_PASSWORD).status_code, 429)   # the lockout trade-off


class CorrectCodeFirstTest(FixtureMixin, RecoveryRouteTestCase):
    """Design 1.4 and 2.4: until a correct code arrives, nothing depends on the account; afterwards
    a change to the account is told to the mailbox's holder and to no one else."""

    def test_a_shadow_row_never_redeems_even_with_a_forced_matching_hash(self):
        """RE8: nothing in the notice and no forced match redeems anything."""
        addresses = self.fixtures()
        for name in ("cli student", "admin", "disabled", "no password", "no account"):
            client = self.app.test_client()
            self.recover(addresses[name], client=client)
            row = self.recoveries()[-1]
            forced = "12345678"
            self.execute("UPDATE password_recoveries SET code_hash = ? WHERE id = ?",
                         (recovery_service._code_hash(self.app.config["SECRET_KEY"], row["id"], forced.encode()),
                          row["id"]))
            users_before = self.rows("SELECT * FROM users ORDER BY id")
            response = self.redeem(forced, NEW_PASSWORD, client=client)
            with self.subTest(address=name):
                self.assertEqual(response.status_code, 200)
                self.assertIn(INVALIDATED, self.body(response))
                self.assertEqual(self.rows("SELECT * FROM users ORDER BY id"), users_before)
                self.assertNoAccountSession(client)
                self.assertIsNone(self.slot(client))
            self.advance(1)

    def changes(self):
        def sql(statement):
            return lambda user, email: self.execute(statement, (user["id"],))
        return {
            "promotion to admin": lambda u, e: self.cli("set-role", "--id", str(u["id"]), "--role", "admin"),
            "role change student to teacher": lambda u, e: self.cli("set-role", "--id", str(u["id"]), "--role", "teacher"),
            "disable": lambda u, e: self.cli("disable", "--id", str(u["id"])),
            "disable then enable": lambda u, e: (self.cli("disable", "--id", str(u["id"])),
                                                 self.cli("enable", "--id", str(u["id"]))),
            "operator set-password": lambda u, e: self.cli("set-password", "--email", e, "--password-stdin",
                                                           stdin="operator chosen password\n"),
            "operator sign-out": lambda u, e: self.cli("sign-out", "--id", str(u["id"])),
            "email changed by SQL": lambda u, e: self.execute("UPDATE users SET email = ? WHERE id = ?",
                                                              ("moved@example.bg", u["id"])),
            "password removed by SQL": sql("UPDATE users SET password_hash = NULL WHERE id = ?"),
            "verification removed by SQL": sql("UPDATE users SET email_verified_at = NULL WHERE id = ?"),
        }

    def test_each_change_between_issuance_and_redemption_is_told_only_after_the_correct_code(self):
        """RE1 to RE7, RK1 and design 1.4 at the routes: a code issued while eligible, a change, then (a) wrong
        codes that look exactly as in the control, then (b) the correct code: INVALIDATED, the account
        as the change left it, no session, no new password."""
        control_email = "control.person@example.bg"
        self.verified(control_email)
        control = self.app.test_client()
        self.recover(control_email, client=control)
        wrong_control = self.observe(self.redeem(WRONG, client=control), control_email)
        for index, (name, change) in enumerate(self.changes().items()):
            email = f"person{index}@example.bg"
            user = self.verified(email)
            client = self.app.test_client()
            self.advance(1)
            self.recover(email, client=client)
            code = self.last_code()
            change(user, email)
            changed = self.rows("SELECT * FROM users WHERE id = ?", user["id"])[0]
            with self.subTest(change=name):
                wrong = self.observe(self.redeem(WRONG, client=client), email)
                self.assertEqual(wrong[:3], wrong_control[:3])          # no account-dependent answer
                self.assertEqual(self.observe(client.get("/recover/verify"), email)[0], 200)
                response = self.redeem(code, NEW_PASSWORD, client=client)
                self.assertEqual(response.status_code, 200, self.body(response))
                self.assertIn(INVALIDATED, self.body(response))
                self.assertEqual(self.rows("SELECT * FROM users WHERE id = ?", user["id"])[0], changed)
                self.assertNoAccountSession(client)
                self.assertIsNone(self.slot(client))
                self.assertFalse(self.authenticates(changed["email"], NEW_PASSWORD))
                state = self.rows("SELECT state, email, code_hash FROM password_recoveries ORDER BY id DESC")[0]
                self.assertEqual(state, {"state": "void", "email": None, "code_hash": None})

    def test_a_rotation_that_leaves_the_account_eligible_still_invalidates_the_code(self):
        """RK1, RE2: the digest of the old key is the reason, not a role or an active flag."""
        self.verified()
        self.recover()
        code = self.last_code()
        self.cli("sign-out", "--email", EMAIL)
        response = self.redeem(code)
        self.assertIn(INVALIDATED, self.body(response))
        self.assertTrue(self.authenticates(EMAIL, OLD_PASSWORD))

    def test_changes_that_do_not_rotate_the_key_do_not_invalidate(self):
        """RK2: a successful sign-in and a display-name change leave the code redeemable."""
        self.verified()
        self.recover()
        code = self.last_code()
        self.assertEqual(self.login(EMAIL, OLD_PASSWORD, client=self.app.test_client()).status_code, 302)
        self.execute("UPDATE users SET display_name = ? WHERE email = ?", ("Нова Име", EMAIL))
        self.assertEqual(self.redeem(code).status_code, 303)
        self.assertTrue(self.authenticates(EMAIL, NEW_PASSWORD))

    def test_a_demoted_admin_with_totp_rows_recovers_and_keeps_every_second_factor_row(self):
        """RE11 (a) at the routes."""
        from tests.test_account_login import TOTP_KEY
        user = self.verified("ex.admin@example.bg", "student")
        self.cli("set-role", "--id", str(user["id"]), "--role", "admin")
        from tests.auth_helpers import enroll_totp
        enroll_totp(self.db_path, user["id"], TOTP_KEY, self.now - timedelta(minutes=5))
        self.cli("set-role", "--id", str(user["id"]), "--role", "teacher")
        before = (self.rows("SELECT * FROM totp_credentials"), self.rows("SELECT * FROM totp_recovery_codes"))
        client = self.app.test_client()
        self.recover("ex.admin@example.bg", client=client)
        self.assertEqual(self.recoveries()[-1]["is_shadow"], 0)
        self.assertEqual(self.redeem(self.last_code(), NEW_PASSWORD, client=client).status_code, 303)
        self.assertEqual((self.rows("SELECT * FROM totp_credentials"), self.rows("SELECT * FROM totp_recovery_codes")),
                         before)
        self.assertEqual(self.login("ex.admin@example.bg", NEW_PASSWORD, client=client).status_code, 302)

    def test_a_registered_pending_account_for_the_same_address_changes_nothing(self):
        """An address with a real pending registration begun before the account existed is not read
        by recovery (design 4.4): the typed address of a recovery gets the shadow treatment."""
        self.recover("fresh@example.bg", client=self.app.test_client())
        self.assertEqual(self.recoveries()[-1]["is_shadow"], 1)


class LoggingTest(FixtureMixin, RecoveryRouteTestCase):
    """RLog1, RLog2, RLog4, RLog5: what the log lines of a whole journey hold and do not hold."""

    class Collect(logging.Handler):
        def __init__(self):
            super().__init__(logging.DEBUG)
            self.lines = []

        def emit(self, record):
            self.lines.append(record.getMessage())

    def collecting(self):
        collector = self.Collect()
        loggers = [logging.getLogger(name) for name in ("learnpilot.recovery", "learnpilot.registration",
                                                        "web.services.mail", "learnpilot.mail")]
        for logger in loggers:
            logger.addHandler(collector)
            logger.setLevel(logging.DEBUG)
            self.addCleanup(logger.removeHandler, collector)
        return collector

    def journey(self, address, password):
        client = self.app.test_client()
        self.begin(address, client=client)
        self.redeem(WRONG, password, client=client)
        self.redeem("12345678", "short", client=client)
        self.advance(61)
        self.resend(client=client)
        if address == EMAIL:
            self.redeem(self.last_code(), password, client=client)
        else:
            self.cancel(client=client)

    def test_no_line_holds_an_address_a_code_a_password_a_hash_a_key_or_a_message(self):
        self.verified()
        collector = self.collecting()
        self.journey(EMAIL, NEW_PASSWORD)
        self.journey("nobody@example.bg", NEW_PASSWORD)
        key = self.app.config["SECRET_KEY"]
        text = "\n".join(collector.lines)
        self.assertGreater(len(collector.lines), 10)
        codes = [message.text for message in self.mailer.messages]
        secrets_ = [EMAIL, "nobody@example.bg", NEW_PASSWORD.strip(), NEW_PASSWORD, key, "Кодът ти за нова парола",
                    "Някой поиска"] + re.findall(r"\d{8}", " ".join(codes))
        for row in self.rows("SELECT * FROM users"):
            secrets_ += [row["password_hash"] or "-", row["session_key"] or "-"]
        for row in self.rows("SELECT * FROM registration_sends"):
            secrets_ += [row["issuance_id"] or "-", row["email_hash"]]
        for secret in secrets_:
            self.assertNotIn(secret, text)
        for token in re.findall(r"[A-Za-z0-9_-]{32,}", text):
            self.fail(f"a long token-like value in a log line: {token[:6]}…")

    def test_the_event_names_are_the_same_for_a_real_and_a_shadow_request_and_user_appears_on_redeemed_only(self):
        self.verified()
        names = {}
        for kind, address in (("real", EMAIL), ("shadow", "nobody@example.bg")):
            collector = self.collecting()
            client = self.app.test_client()
            self.begin(address, client=client)
            self.redeem(WRONG, client=client)
            self.advance(61)
            self.resend(client=client)
            self.cancel(client=client)
            names[kind] = [re.match(r"[a-z_]+", line).group(0) for line in collector.lines]
            self.assertFalse([line for line in collector.lines if "shadow" in line.lower()])
            self.assertFalse([line for line in collector.lines if "user=" in line])
            self.advance(1)
        self.assertEqual(names["real"], names["shadow"])
        collector = self.collecting()
        client = self.app.test_client()
        self.begin(EMAIL, client=client)
        self.redeem(self.last_code(), NEW_PASSWORD, client=client)
        with_user = [line for line in collector.lines if "user=" in line]
        # The service's decision line and the route's durable line; both follow a correct code.
        self.assertEqual(len(with_user), 2)
        self.assertTrue(all(line.startswith("redeemed") for line in with_user), with_user)

    def test_nothing_recovery_specific_is_in_a_url_or_a_set_cookie_value(self):
        """RLog4: the slot holds only v, t and next; no value of a row is in a Location or a cookie."""
        self.verified()
        client = self.app.test_client()
        responses = [self.begin(EMAIL, client=client), client.get("/recover/verify"), self.redeem(WRONG, client=client)]
        code = self.last_code()
        responses.append(self.redeem(code, NEW_PASSWORD, client=client))
        responses.append(client.get("/login?recovery=done"))
        for response in responses:
            text = response.headers.get("Location", "") + " ".join(response.headers.getlist("Set-Cookie"))
            for secret in (EMAIL, code, "ana.ivanova", NEW_PASSWORD.strip()):
                self.assertNotIn(secret, text)
        self.assertEqual(responses[-2].headers["Location"], "/login?recovery=done")


class PollingLogTest(RecoveryRouteTestCase):
    def test_polling_writes_no_line_that_differs_before_and_after_another_browsers_reset(self):
        """RLog5: GET verify logs nothing, before or after the reset."""
        self.verified()
        a = self.app.test_client()
        self.recover(client=a)
        collector = LoggingTest.Collect()
        for name in ("learnpilot.recovery", "learnpilot.registration"):
            logger = logging.getLogger(name)
            logger.addHandler(collector)
            logger.setLevel(logging.DEBUG)
            self.addCleanup(logger.removeHandler, collector)
        a.get("/recover/verify")
        before = list(collector.lines)
        owner = self.app.test_client()
        self.recover(client=owner)
        self.redeem(self.last_code(), NEW_PASSWORD, client=owner)
        collector.lines.clear()
        a.get("/recover/verify")
        self.assertEqual(before, [])
        self.assertEqual(collector.lines, [])


if __name__ == "__main__":
    unittest.main()
