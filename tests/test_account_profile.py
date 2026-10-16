"""Registration PR 6: ``/account/profile`` and the profile on ``/account`` (test plan I5, I6, J5, J7, J9, F2, F13).

Every test has its own temporary database built from migrations and canonical data; none
is ``data/questions.db``. The profile gate is switched on per test through the application
configuration (the only thing a development run, or a deploy of the build constant, sets);
the gate-off tests leave it at the build's default. No mail is sent and nothing sleeps.
"""

import html
import itertools
import re
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

from tests import runtime_fixtures
from tests.auth_helpers import set_account_session
from tests.support import build_test_database, student_entry_path, web_app
from tests.test_account_login import CROSS_ORIGIN, PASSWORD, SAME_ORIGIN, AccountAppTestCase
from tests.test_registration_routes import FlakyCommits
from web.assignment_tokens import new_public_token
from web.blueprints import account as account_blueprint
from web.registration_settings import CONFIG_KEY as REGISTRATION_KEY
from web.registration_settings import RegistrationSettings
from web.services import accounts, profiles
from web.services import quiz as quiz_service
from web.services.mail import InMemoryMailer
from web.services import registration as registration_service

MARK = "2026-10-15 09:00:00"
MARKER_PREFIX = "quiz_attempt_owner:"
STUDENT_FORM = {"first_name": "Мария", "last_name": "Петрова", "grade": "10"}
TEACHER_FORM = {"first_name": "Иван", "last_name": "Петров", "school_name": "ПГ Тест, София",
                "subject": ["it", "other"], "subject_other": "Роден език"}
START = "/quiz/s/" + "A" * 43
FAILURE = "Нещо се обърка и нищо не беше записано. Опитай отново."
REQUIRED = "Попълни профила си, за да започнеш теста."
USER_COLUMNS = ("id", "email", "display_name", "role", "password_hash", "is_active", "session_key",
                "password_changed_at", "last_login_at", "created_at", "email_verified_at", "profile_required_at")


class ProfileRouteTestCase(AccountAppTestCase):
    """A fresh database per test; the profile gate is on unless ``gate`` says otherwise."""

    gate = True

    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="account-profile-")
        self.addCleanup(directory.cleanup)
        self.db_path = str(build_test_database(Path(directory.name) / "profile.db"))
        self.app.config["DB_PATH"] = self.db_path
        web_app.DB_PATH = web_app.QUIZ_DB_PATH = Path(self.db_path)  # restored by preserved_app_state
        super().setUp()
        if self.gate:
            config = patch.dict(self.app.config, {profiles.CONFIG_KEY: True})
            config.start()
            self.addCleanup(config.stop)

    # ------------------------------------------------------------------ accounts and requests

    def signed_in(self, role="student", *, marked=False, profile=None, **options):
        """An account with a password, signed in on a new client without the login form."""
        account = self.make(role, **options)
        if marked:
            self.execute("UPDATE users SET profile_required_at = ? WHERE id = ?", (MARK, account["id"]))
        if profile:
            self.give_profile(account["id"], profile)
        client = self.app.test_client()
        set_account_session(client, account["id"], account["session_key"])
        return account, client

    def give_profile(self, user_id, kind):
        with closing(sqlite3.connect(self.db_path)) as conn:
            now = self.now
            if kind == "student":
                profiles.create_profile(conn, user_id, account_type="student", first_name="Ана", last_name="Иванова",
                                        grade=9, now=now)
            else:
                profiles.create_profile(
                    conn, user_id, account_type="teacher", first_name="Иван", last_name="Петров",
                    school_name="СУ „Васил Левски“, Пловдив",
                    subjects=[{"subject_id": "it", "label": "Информационни технологии"}], now=now)
            conn.commit()

    def get(self, client, path="/account/profile", **kwargs):
        return client.get(path, **kwargs)

    def post(self, client, data, *, headers=SAME_ORIGIN, path="/account/profile"):
        return client.post(path, data=data, headers=headers)

    def text(self, response):
        return html.unescape(self.body(response))

    def users(self):
        return self.query(f"SELECT {', '.join(USER_COLUMNS)}, updated_at FROM users ORDER BY id")

    def profile_rows(self):
        return (self.query("SELECT * FROM user_profiles ORDER BY user_id"),
                self.query("SELECT * FROM teacher_subjects ORDER BY user_id, position"))

    def snapshot(self):
        return (self.users(), self.profile_rows())

    def assignment(self):
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.row_factory = sqlite3.Row
            assignment_id = runtime_fixtures.add_assignment(
                conn, section_id=self.section_id(conn), title="Тест за профили", question_count=2,
                public_token=new_public_token())
            conn.commit()
        return assignment_id

    @staticmethod
    def section_id(conn):
        for row in conn.execute("SELECT id FROM curriculum_sections ORDER BY id"):
            if len(quiz_service.quiz_section_question_ids(conn, row["id"])) >= 3:
                return int(row["id"])
        raise AssertionError("no section with eligible questions")


# --------------------------------------------------------------------------- the gate is off


class GateOffTest(ProfileRouteTestCase):
    """The build's default: nothing of the profile flow is reachable or visible (J6)."""

    gate = False

    def test_the_default_configuration_has_the_gate_off(self):
        self.assertIs(self.app.config.get(profiles.CONFIG_KEY), False)
        self.assertFalse(profiles.enabled_in(self.app.config))

    def test_the_page_is_the_generic_404_for_every_signed_in_account(self):
        for role in ("student", "teacher", "admin"):
            with self.subTest(role=role):
                account, client = self.signed_in(role, profile="student" if role == "student" else None)
                for path in ("/account/profile", "/account/profile?next=/account/attempts", "/account/profile?saved=1"):
                    self.assertEqual(self.get(client, path).status_code, 404, path)
                before = self.snapshot()
                self.assertEqual(self.post(client, STUDENT_FORM).status_code, 404)
                self.assertEqual(self.snapshot(), before)
                self.assertEqual(client.get("/account").status_code, 200)   # and nobody is signed out

    def database_dump(self):
        """Every row of every table: a disabled gate must write nothing anywhere."""
        with closing(sqlite3.connect(self.db_path)) as conn:
            tables = [row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
            return {table: conn.execute(f'SELECT * FROM "{table}"').fetchall() for table in tables}

    def same_page(self, client, response, path):
        """The body is the one an unknown URL gets for the same visitor (only the echoed path differs)."""
        unknown = "/definitely/not/a/page"
        reference = self.body(self.get(client, unknown)).replace(unknown, "/account/profile")
        return self.body(response).replace(path.split("?")[0], "/account/profile") == reference

    def test_every_visitor_gets_the_generic_404_and_never_a_login_redirect(self):
        """The disabled gate is decided before authentication (approved by Gabriel)."""
        anonymous = self.app.test_client()
        signed = {role: self.signed_in(role)[1] for role in ("student", "teacher", "admin")}
        before = self.database_dump()
        for who, client in {"anonymous": anonymous, **signed}.items():
            for path in ("/account/profile", "/account/profile?next=/account/attempts", "/account/profile?saved=1"):
                with self.subTest(visitor=who, method="GET", path=path):
                    response = self.get(client, path)
                    self.assertEqual(response.status_code, 404)
                    self.assertNotIn("Location", response.headers)
                    self.assertEqual(response.headers.getlist("Set-Cookie"), [])
                    self.assertEqual(self.same_page(client, response, path), True)
            with self.subTest(visitor=who, method="POST"):
                response = self.post(client, {**STUDENT_FORM, "next": START})
                self.assertEqual(response.status_code, 404)
                self.assertNotIn("Location", response.headers)
                self.assertEqual(response.headers.getlist("Set-Cookie"), [])
                self.assertEqual(self.same_page(client, response, "/account/profile"), True)
        self.assertEqual(self.database_dump(), before)     # no profile, account, session-key or throttle write

    def test_an_anonymous_visitor_issues_no_session_while_the_gate_is_off(self):
        anonymous = self.app.test_client()
        self.get(anonymous, "/account/profile")
        self.post(anonymous, STUDENT_FORM)
        with anonymous.session_transaction() as session:
            self.assertEqual(dict(session), {})

    def test_same_origin_protection_still_comes_first_while_the_gate_is_off(self):
        anonymous = self.app.test_client()
        _, signed = self.signed_in("student")
        before = self.database_dump()
        for client in (anonymous, signed):
            for headers in (CROSS_ORIGIN, {"Origin": "null"}, {"Referer": "http://evil.example/form"}):
                with self.subTest(headers=headers):
                    response = self.post(client, STUDENT_FORM, headers=headers)
                    self.assertEqual(response.status_code, 403)
                    self.assertEqual(response.headers.getlist("Set-Cookie"), [])
        with patch.dict(self.app.config, {"TESTING": False}):
            self.assertEqual(anonymous.post("/account/profile", data=STUDENT_FORM).status_code, 403)
        self.assertEqual(self.database_dump(), before)


    def test_the_account_page_has_no_profile_section_and_no_link(self):
        for role, kind in (("student", None), ("student", "student"), ("teacher", "teacher"), ("admin", None)):
            with self.subTest(role=role, profile=kind):
                _, client = self.signed_in(role, marked=True, profile=kind)
                page = self.text(client.get("/account"))
                self.assertNotIn("/account/profile", page)
                self.assertNotIn("Данни за профила", page)
                self.assertNotIn("Попълни профила", page)
                self.assertNotIn("Иван", page.replace("Тест Потребител", ""))  # no declared name is shown

    def test_a_mark_that_exists_is_ignored_and_stays_stored(self):
        account, client = self.signed_in("student", marked=True)
        self.assertEqual(client.get("/account").status_code, 200)
        self.assertEqual(self.user(account["id"], "profile_required_at"), MARK)

    def test_only_a_true_value_turns_the_gate_on(self):
        for value in (None, False, 1, "1", "true", "yes", [True]):
            with self.subTest(value=value), patch.dict(self.app.config, {profiles.CONFIG_KEY: value}):
                self.assertFalse(profiles.enabled_in(self.app.config))
        with patch.dict(self.app.config, {profiles.CONFIG_KEY: True}):
            self.assertTrue(profiles.enabled_in(self.app.config))
        self.assertFalse(profiles.enabled_in({}))


class GateOrderTest(ProfileRouteTestCase):
    """With the gate on, an anonymous visitor keeps the existing sign-in flow."""

    def test_anonymous_get_and_post_are_sent_to_the_login_with_the_path(self):
        anonymous = self.app.test_client()
        before = self.snapshot()
        for response in (self.get(anonymous), self.post(anonymous, STUDENT_FORM)):
            self.assertEqual((response.status_code, response.headers["Location"]), (302, "/login?next=/account/profile"))
            self.assertEqual(response.headers.getlist("Set-Cookie"), [])
        self.assertEqual(self.snapshot(), before)

    def test_the_login_it_points_to_signs_the_account_in_and_returns_to_the_profile(self):
        account = self.make("student", email="gate-order@example.bg")
        client = self.app.test_client()
        response = client.post("/login?next=/account/profile", headers=SAME_ORIGIN,
                               data={"email": "gate-order@example.bg", "password": PASSWORD})
        self.assertEqual((response.status_code, response.headers["Location"]), (302, "/account/profile"))
        self.assertEqual(self.get(client).status_code, 200)

    def test_a_disabled_account_and_a_cross_origin_post_keep_their_answers(self):
        account, client = self.signed_in("student")
        self.assertEqual(self.post(client, STUDENT_FORM, headers=CROSS_ORIGIN).status_code, 403)
        self.execute("UPDATE users SET is_active = 0 WHERE id = ?", (account["id"],))
        response = self.get(client)
        self.assertEqual((response.status_code, response.headers["Location"]), (302, "/login?next=/account/profile"))


# --------------------------------------------------------------------------- the page, GET


class ProfilePageTest(ProfileRouteTestCase):
    def test_a_student_without_a_profile_gets_the_three_field_form_and_no_login_data(self):  # J7
        account, client = self.signed_in("student")
        response = self.get(client)
        self.assertEqual(response.status_code, 200)
        page = self.body(response)
        for name in ("first_name", "last_name", "grade"):
            self.assertIn(f'name="{name}"', page)
        for forbidden in ('name="email"', 'type="password"', 'name="password"', 'name="role"', 'name="account_type"',
                          'name="school_name"', 'name="subject"'):
            self.assertNotIn(forbidden, page)
        self.assertNotIn(REQUIRED, page)       # optional unless the account is marked
        self.assertEqual(self.get(client, "/account/profile?saved=1").status_code, 200)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(self.user(account["id"], "profile_required_at"), None)

    def test_a_marked_student_is_told_why(self):
        _, client = self.signed_in("student", marked=True)
        self.assertIn(REQUIRED, self.text(self.get(client)))

    def test_a_marked_student_with_a_profile_is_no_longer_told(self):
        _, client = self.signed_in("student", marked=True, profile="student")
        page = self.text(self.get(client))
        self.assertNotIn(REQUIRED, page)
        self.assertIn('value="Ана"', page)

    def test_a_teacher_or_admin_without_a_profile_is_told_there_is_none_and_offered_no_form(self):
        for role in ("teacher", "admin"):
            with self.subTest(role=role):
                _, client = self.signed_in(role)
                response = self.get(client)
                self.assertEqual(response.status_code, 200)
                page = self.text(response)
                self.assertIn("Няма попълнен профил", page)
                self.assertNotIn('name="first_name"', page)
                self.assertNotIn('action="/account/profile"', page)

    def test_a_student_type_profile_shows_its_own_fields_whatever_the_role_is(self):  # I5
        for role in ("student", "teacher", "admin"):
            with self.subTest(role=role):
                _, client = self.signed_in(role, profile="student")
                page = self.get(client).get_data(as_text=True)
                self.assertIn('name="grade"', page)
                self.assertIn('<option value="9" selected>', page)
                self.assertNotIn('name="school_name"', page)
                self.assertNotIn('name="subject"', page)

    def test_a_teacher_type_profile_shows_its_own_fields_whatever_the_role_is(self):  # I5
        for role in ("student", "teacher", "admin"):
            with self.subTest(role=role):
                _, client = self.signed_in(role, profile="teacher")
                page = self.get(client).get_data(as_text=True)
                self.assertIn('name="school_name"', page)
                self.assertIn('value="СУ „Васил Левски“, Пловдив"', page)
                self.assertRegex(page, r'value="it" checked')
                self.assertNotIn('name="grade"', page)
                self.assertNotIn('type="password"', page)
                self.assertNotIn('name="email"', page)

    def test_the_form_posts_to_the_page_itself_and_carries_no_authority(self):
        _, client = self.signed_in("student")
        page = self.body(self.get(client))
        self.assertIn('method="post" action="/account/profile"', page)
        self.assertEqual(page.count('action="/account/profile"'), 1)
        self.assertNotIn("is_admin", page)

    def test_next_is_echoed_only_when_it_is_safe_and_an_unsafe_one_is_never_shown(self):  # F13
        _, client = self.signed_in("student")
        self.assertIn(f'name="next" value="{START}"', self.body(self.get(client, f"/account/profile?next={START}")))
        for bad in ("//evil.example/x", "https://evil.example/", "/a\\b", "/a\r\nb", "evil.example", "/" + "a" * 600,
                    "/‮evil", "/a b"):
            with self.subTest(next=bad):
                page = self.body(self.get(client, "/account/profile", query_string={"next": bad}))
                self.assertIn('name="next" value=""', page)
                self.assertNotIn("evil", page)

    def test_saved_is_a_message_only_for_exactly_one(self):
        _, client = self.signed_in("student", profile="student")
        self.assertIn("Профилът е запазен.", self.text(self.get(client, "/account/profile?saved=1")))
        for value in ("0", "2", "true", "<b>"):
            page = self.text(self.get(client, "/account/profile", query_string={"saved": value}))
            self.assertNotIn("Профилът е запазен.", page)
            self.assertNotIn("<b>", page)

    def test_a_get_writes_nothing_and_sets_no_cookie(self):  # F3
        _, client = self.signed_in("student", marked=True)
        before = self.snapshot()
        response = self.get(client, f"/account/profile?next={START}")
        self.assertEqual((self.snapshot(), response.headers.getlist("Set-Cookie")), (before, []))


class AccountPageTest(ProfileRouteTestCase):
    def test_a_student_without_a_profile_is_offered_to_complete_it(self):
        _, client = self.signed_in("student")
        page = self.text(client.get("/account"))
        self.assertIn("Данни за профила", page)
        self.assertIn('href="/account/profile"', self.body(client.get("/account")))
        self.assertIn("Попълни профила", page)
        self.assertNotIn(REQUIRED, page)

    def test_a_marked_student_is_told_on_the_account_page_too(self):
        _, client = self.signed_in("student", marked=True)
        self.assertIn(REQUIRED, self.text(client.get("/account")))

    def test_a_profile_is_shown_with_its_values_and_a_link_to_edit_it(self):
        _, student = self.signed_in("student", profile="student")
        page = self.text(student.get("/account"))
        for needle in ("Ана", "Иванова", "9. клас", "Редактирай данните"):
            self.assertIn(needle, page)
        _, teacher = self.signed_in("teacher", profile="teacher")
        page = self.text(teacher.get("/account"))
        for needle in ("Иван", "Петров", "СУ „Васил Левски“, Пловдив", "Информационни технологии"):
            self.assertIn(needle, page)
        self.assertNotIn("клас", page.split("Данни за профила")[1].split("Начини за вход")[0])

    def test_a_teacher_or_admin_without_a_profile_is_asked_for_nothing(self):
        for role in ("teacher", "admin"):
            with self.subTest(role=role):
                _, client = self.signed_in(role, marked=True)    # even a stray mark asks a teacher for nothing
                page = self.text(client.get("/account"))
                self.assertIn("Този акаунт няма профил", page)
                self.assertNotIn("Попълни профила", page)
                self.assertNotIn(REQUIRED, page)

    def test_the_account_page_still_writes_nothing(self):
        _, client = self.signed_in("student", marked=True)
        before = self.snapshot()
        self.assertEqual(client.get("/account").status_code, 200)
        self.assertEqual(self.snapshot(), before)

    def test_stored_values_are_escaped_on_both_pages(self):
        """Values reach the table only through the validators, so a hostile one is put
        there directly, as an operator's SQL or a bug could."""
        account, client = self.signed_in("student", profile="student")
        self.execute("UPDATE user_profiles SET first_name = ?, last_name = ? WHERE user_id = ?",
                     ('<img src=x onerror=alert(1)>', '"><script>alert(2)</script>', account["id"]))
        for path in ("/account", "/account/profile"):
            raw = client.get(path).get_data(as_text=True)
            self.assertNotIn("<img src=x", raw, path)
            self.assertNotIn("<script>alert(2)", raw, path)
            self.assertIn("&lt;img src=x onerror=alert(1)&gt;", raw, path)


# --------------------------------------------------------------------------- creating and editing, POST


class CreateProfileTest(ProfileRouteTestCase):
    def test_completing_creates_the_profile_and_sets_the_display_name(self):  # J7
        account, client = self.signed_in("student", marked=True, display_name="Google Име")
        response = self.post(client, {**STUDENT_FORM, "next": START})
        self.assertEqual((response.status_code, response.headers["Location"]), (303, START))
        profile = self.query("SELECT user_id, account_type, first_name, last_name, grade FROM user_profiles")
        self.assertEqual(profile, [(account["id"], "student", "Мария", "Петрова", 10)])
        self.assertEqual(self.user(account["id"], "display_name"), "Мария Петрова")
        self.assertEqual(self.query("SELECT COUNT(*) FROM teacher_subjects"), [(0,)])

    def test_nothing_else_of_the_account_changes_and_its_sessions_stay_valid(self):  # J5
        account, client = self.signed_in("student", marked=True, display_name="Google Име")
        other_browser = self.app.test_client()
        set_account_session(other_browser, account["id"], account["session_key"])
        with client.session_transaction() as sess:
            sess[MARKER_PREFIX + "41"] = True
        session_before = self.session(client)
        before = self.users()[-1]
        self.assertEqual(self.post(client, STUDENT_FORM).status_code, 303)
        after = self.users()[-1]
        changed = {USER_COLUMNS[i] for i in range(len(USER_COLUMNS)) if before[i] != after[i]}
        self.assertEqual(changed, {"display_name"})
        for column in ("role", "email", "email_verified_at", "is_active", "session_key", "password_hash",
                       "profile_required_at"):
            self.assertEqual(after[USER_COLUMNS.index(column)], before[USER_COLUMNS.index(column)], column)
        # The session of this browser and of every other is exactly as it was.
        self.assertEqual(self.session(client), session_before)
        self.assertIn(MARKER_PREFIX + "41", self.session(client))
        self.assertEqual(client.get("/account").status_code, 200)
        self.assertEqual(other_browser.get("/account").status_code, 200)

    def test_attempt_ownership_and_historical_names_are_unchanged_and_later_attempts_copy_the_new_name(self):  # J5
        account, client = self.signed_in("student", marked=True, display_name="Google Име")
        assignment_id, other_assignment = self.assignment(), self.assignment()
        with closing(sqlite3.connect(self.db_path)) as conn:
            ids = runtime_fixtures.first_multiple_choice_question_ids(conn)
            earlier = runtime_fixtures.add_attempt(conn, assignment_id, student_name="Исторически Ученик",
                                                   question_ids=ids)
            owned = runtime_fixtures.add_attempt(conn, other_assignment, student_name="Google Име", question_ids=ids,
                                                 user_id=account["id"], submitted=True)
            conn.commit()
        rows_before = self.query("SELECT id, assignment_id, user_id, student_name FROM quiz_attempts ORDER BY id")
        owners_before = self.query("SELECT id, owner_user_id FROM quiz_assignments ORDER BY id")
        self.assertEqual(self.post(client, STUDENT_FORM).status_code, 303)
        self.assertEqual(self.query("SELECT id, assignment_id, user_id, student_name FROM quiz_attempts ORDER BY id"),
                         rows_before)
        self.assertEqual(self.query("SELECT id, owner_user_id FROM quiz_assignments ORDER BY id"), owners_before)
        self.assertEqual(client.get(f"/quiz/attempt/{owned}/result").status_code, 200)   # still its owner
        # The next attempt copies what the account is called now.
        started = client.post(student_entry_path(assignment_id), headers=SAME_ORIGIN)
        self.assertEqual(started.status_code, 302, self.body(started))
        names = self.query("SELECT student_name FROM quiz_attempts WHERE assignment_id = ? AND user_id = ?",
                           (assignment_id, account["id"]))
        self.assertEqual(names, [("Мария Петрова",)])
        self.assertEqual(self.query("SELECT student_name FROM quiz_attempts WHERE id = ?", (earlier,)),
                         [("Исторически Ученик",)])

    def test_without_next_the_saved_profile_is_shown_and_with_an_unsafe_one_the_home_is_(self):  # J9
        _, client = self.signed_in("student")
        response = self.post(client, STUDENT_FORM)
        self.assertEqual((response.status_code, response.headers["Location"]), (303, "/account/profile?saved=1"))
        for bad in ("//evil.example", "https://evil.example/x", "/a\\b", "evil", "/" + "a" * 600):
            with self.subTest(next=bad):
                account, other = self.signed_in("student")
                response = self.post(other, {**STUDENT_FORM, "next": bad})
                self.assertEqual((response.status_code, response.headers["Location"]), (303, "/"))
                self.assertNotIn("evil", response.headers["Location"])
                self.assertNotIn("evil", self.body(response))

    def test_the_target_is_validated_again_when_it_is_used(self):  # F13
        _, client = self.signed_in("student")
        with patch.object(account_blueprint, "safe_redirect_target", wraps=account_blueprint.safe_redirect_target) as spy:
            self.assertEqual(self.post(client, {**STUDENT_FORM, "next": START}).headers["Location"], START)
        spy.assert_called_with(START, "/")
        # A check that refuses at use time wins over the one at acceptance.
        _, other = self.signed_in("student")
        with patch.object(account_blueprint, "safe_redirect_target", lambda value, fallback: fallback):
            self.assertEqual(self.post(other, {**STUDENT_FORM, "next": START}).headers["Location"], "/")

    def test_completing_never_starts_an_attempt(self):  # J9
        _, client = self.signed_in("student", marked=True)
        assignment_id = self.assignment()
        response = self.post(client, {**STUDENT_FORM, "next": student_entry_path(assignment_id)})
        self.assertEqual(response.headers["Location"], student_entry_path(assignment_id))
        self.assertEqual(self.query("SELECT COUNT(*) FROM quiz_attempts"), [(0,)])
        self.assertEqual(client.get(response.headers["Location"]).status_code, 200)   # a GET: the start page
        self.assertEqual(self.query("SELECT COUNT(*) FROM quiz_attempts"), [(0,)])

    def test_login_and_credential_fields_are_never_read_or_stored(self):  # J7, B11
        account, client = self.signed_in("student")
        extra = {"email": "other@example.bg", "password": "another-long-password", "confirm_password": "another-long-password",
                 "role": "admin", "account_type": "teacher", "is_admin": "1", "email_verified_at": MARK,
                 "profile_required_at": MARK, "school_name": "ПГ Тест, София", "subject": ["it"], "is_active": "0"}
        before = self.users()[-1]
        self.assertEqual(self.post(client, {**STUDENT_FORM, **extra}).status_code, 303)
        after = self.users()[-1]
        for column in ("role", "email", "email_verified_at", "is_active", "session_key", "password_hash",
                       "profile_required_at"):
            self.assertEqual(after[USER_COLUMNS.index(column)], before[USER_COLUMNS.index(column)], column)
        self.assertEqual(self.query("SELECT account_type, school_name FROM user_profiles"), [("student", None)])
        self.assertEqual(self.query("SELECT COUNT(*) FROM teacher_subjects"), [(0,)])
        self.assertTrue(accounts.verify_password(sqlite3.connect(self.db_path), account["id"], PASSWORD))

    def test_an_invalid_form_rerenders_with_errors_and_writes_nothing(self):
        _, client = self.signed_in("student", marked=True)
        before = self.snapshot()
        response = self.post(client, {"first_name": "", "last_name": "я" * 40, "grade": "7", "next": "//evil.example"})
        self.assertEqual(response.status_code, 200)
        page = self.text(response)
        for message in ("Въведи име.", "Името трябва да е най-много 39 символа.", "Избери клас."):
            self.assertIn(message, page)
        self.assertIn(REQUIRED, page)
        self.assertNotIn("evil", page)
        self.assertEqual(self.snapshot(), before)

    def test_hostile_input_is_refused_or_escaped_never_echoed_raw(self):
        _, client = self.signed_in("student")
        response = self.post(client, {"first_name": '"><script>alert(1)</script>', "last_name": "<b>x</b>", "grade": "<i>9</i>"})
        self.assertEqual(response.status_code, 200)
        raw = response.get_data(as_text=True)
        self.assertNotIn("<script>alert(1)", raw)
        self.assertNotIn("<b>x</b>", raw)
        self.assertNotIn("<i>9</i>", raw)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", raw)
        self.assertEqual(self.profile_rows(), ([], []))

    def test_a_teacher_or_admin_without_a_profile_creates_nothing(self):  # I5
        for role in ("teacher", "admin"):
            for form in (STUDENT_FORM, TEACHER_FORM, {**STUDENT_FORM, "role": "student", "account_type": "student"}):
                with self.subTest(role=role, form=sorted(form)):
                    _, client = self.signed_in(role, marked=True)
                    before = self.snapshot()
                    response = self.post(client, form)
                    self.assertEqual(response.status_code, 403)
                    self.assertIn("Няма попълнен профил", self.text(response))
                    self.assertEqual(self.snapshot(), before)

    def test_a_second_submission_of_the_same_form_changes_nothing_more(self):
        _, client = self.signed_in("student", marked=True)
        self.assertEqual(self.post(client, STUDENT_FORM).status_code, 303)
        after_first = self.snapshot()
        self.assertEqual(self.post(client, STUDENT_FORM).status_code, 303)
        self.assertEqual(self.profile_rows()[0][0][:5], after_first[1][0][0][:5])
        self.assertEqual(self.query("SELECT COUNT(*) FROM user_profiles"), [(1,)])
        self.assertEqual(self.users()[0][2], "Мария Петрова")

    def test_a_profile_created_between_the_form_and_the_submission_is_edited_not_duplicated(self):
        account, client = self.signed_in("student")
        self.get(client)
        self.give_profile(account["id"], "student")      # another tab completed it meanwhile
        self.assertEqual(self.post(client, STUDENT_FORM).status_code, 303)
        self.assertEqual(self.query("SELECT first_name, grade FROM user_profiles"), [("Мария", 10)])

    def test_a_role_that_changed_after_the_form_was_read_decides_at_the_write(self):
        account, client = self.signed_in("student")
        real = profiles.get_profile
        reads = []

        def stale_then_real(conn, user_id):
            reads.append(user_id)
            found = real(conn, user_id)
            if len(reads) == 1:      # after the form was read, before the write lock: an operator promotes the account
                with closing(sqlite3.connect(self.db_path)) as other:
                    accounts.set_role(other, account["id"], "teacher")
                    other.commit()
            return found

        with patch.object(profiles, "get_profile", stale_then_real):
            response = self.post(client, STUDENT_FORM)
        self.assertEqual(self.profile_rows(), ([], []))
        self.assertEqual(response.status_code, 409)
        self.assertIn(FAILURE, self.text(response))
        self.assertEqual(self.user(account["id"], "role"), "teacher")


HIDDEN_INPUT = re.compile(r'<input type="hidden" name="([^"]+)" value="([^"]*)">')
UNSAFE_TARGETS = ("//evil.example/x", "https://evil.example/", "/a\\evil", "evil.example", "/" + "a" * 600, "/‮evil")


class RefusedNextRoundTripTest(ProfileRouteTestCase):
    """A ``next`` the form refused is remembered as a yes/no through rendering and retries.

    The hidden fields of the rendered page are posted back as a browser would, so the
    GET -> POST and the invalid-POST -> corrected-POST round trips are real. The refused
    target itself is never rendered; it is validated again where it is used."""

    HOMES = {"student": "/", "teacher": "/teacher/new", "admin": "/teacher"}

    def hidden(self, response):
        return {name: html.unescape(value) for name, value in HIDDEN_INPUT.findall(self.body(response))}

    def assert_location(self, response, expected):
        self.assertEqual((response.status_code, response.headers["Location"]), (303, expected))
        location = urlsplit(response.headers["Location"])
        self.assertEqual((location.scheme, location.netloc), ("", ""))             # never an external redirect
        self.assertFalse(response.headers["Location"].startswith("//"))
        self.assertNotIn("evil", response.headers["Location"])

    def test_an_unsafe_next_on_get_then_a_successful_submission_goes_to_the_role_home(self):
        for bad in UNSAFE_TARGETS:
            with self.subTest(next=bad):
                _, client = self.signed_in("student")
                page = self.get(client, "/account/profile", query_string={"next": bad})
                fields = self.hidden(page)
                self.assertEqual(fields, {"next": "", "next_refused": "1"})
                self.assertNotIn("evil", self.body(page))
                self.assert_location(self.post(client, {**fields, **STUDENT_FORM}), "/")

    def test_an_unsafe_next_with_invalid_fields_then_a_corrected_submission_goes_to_the_role_home(self):
        invalid = {"first_name": "", "last_name": "Петрова", "grade": "10"}
        for bad in UNSAFE_TARGETS:
            with self.subTest(next=bad):
                _, client = self.signed_in("student")
                # Straight from the GET page, and with the raw target posted directly (a stale
                # or hand-built form): both retries carry the indicator and never the target.
                from_page = self.hidden(self.get(client, "/account/profile", query_string={"next": bad}))
                for first in ({**from_page, **invalid}, {**invalid, "next": bad}):
                    retry = self.post(client, first)
                    self.assertEqual(retry.status_code, 200)
                    self.assertIn("Въведи име.", self.text(retry))
                    self.assertNotIn("evil", self.body(retry))
                    fields = self.hidden(retry)
                    self.assertEqual(fields, {"next": "", "next_refused": "1"})
                    second = self.post(client, {**fields, **invalid})              # still invalid: again a retry
                    self.assertEqual(self.hidden(second), {"next": "", "next_refused": "1"})
                self.assert_location(self.post(client, {**self.hidden(retry), **STUDENT_FORM}), "/")

    def test_a_genuinely_absent_next_keeps_the_current_behaviour(self):
        for query in ({}, {"next": ""}):
            with self.subTest(query=query):
                _, client = self.signed_in("student")
                fields = self.hidden(self.get(client, "/account/profile", query_string=query))
                self.assertEqual(fields, {"next": ""})                              # no indicator
                retry = self.post(client, {**fields, "first_name": "", "last_name": "Петрова", "grade": "10"})
                self.assertEqual(self.hidden(retry), {"next": ""})
                response = self.post(client, {**self.hidden(retry), **STUDENT_FORM})
                self.assertEqual((response.status_code, response.headers["Location"]), (303, "/account/profile?saved=1"))

    def test_a_valid_next_is_preserved_through_the_page_and_a_retry(self):
        _, client = self.signed_in("student")
        fields = self.hidden(self.get(client, f"/account/profile?next={START}"))
        self.assertEqual(fields, {"next": START})
        retry = self.post(client, {**fields, "first_name": "", "last_name": "Петрова", "grade": "10"})
        self.assertEqual(self.hidden(retry), {"next": START})
        self.assert_location(self.post(client, {**self.hidden(retry), **STUDENT_FORM}), START)

    def test_a_valid_next_wins_over_the_indicator(self):
        _, client = self.signed_in("student")
        response = self.post(client, {**STUDENT_FORM, "next": START, "next_refused": "1"})
        self.assert_location(response, START)

    def test_the_indicator_is_a_yes_no_and_a_forged_one_can_only_reach_the_role_home(self):
        _, client = self.signed_in("student")
        self.assert_location(self.post(client, {**STUDENT_FORM, "next_refused": "1"}), "/")
        for forged in ("0", "", "yes", "true", "//evil.example", "https://evil.example/"):
            with self.subTest(next_refused=forged):
                _, other = self.signed_in("student")
                response = self.post(other, {**STUDENT_FORM, "next_refused": forged})
                self.assertEqual((response.status_code, response.headers["Location"]), (303, "/account/profile?saved=1"))
                self.assertNotIn("evil", self.body(response))

    def test_the_fallback_is_the_role_at_the_time_of_the_submission(self):
        for role, home in (("teacher", "/teacher/new"), ("admin", "/teacher")):
            with self.subTest(role=role):
                account, client = self.signed_in("student", profile="student")
                fields = self.hidden(self.get(client, "/account/profile", query_string={"next": "//evil.example"}))
                self.assertEqual(fields, {"next": "", "next_refused": "1"})       # rendered for a student ...
                self.execute("UPDATE users SET role = ? WHERE id = ?", (role, account["id"]))
                self.assert_location(                                             # ... used for the current role
                    self.post(client, {**fields, "first_name": "Ана", "last_name": "Иванова", "grade": "9"}), home)

    def test_every_role_with_a_stored_profile_falls_back_to_its_own_home(self):
        for role, kind, form in (("student", "student", STUDENT_FORM), ("teacher", "teacher", TEACHER_FORM),
                                 ("admin", "teacher", TEACHER_FORM)):
            with self.subTest(role=role):
                _, client = self.signed_in(role, profile=kind)
                fields = self.hidden(self.get(client, "/account/profile", query_string={"next": "https://evil.example/"}))
                self.assertEqual(fields, {"next": "", "next_refused": "1"})
                self.assert_location(self.post(client, {**fields, **form}), self.HOMES[role])

    def test_the_target_is_validated_again_at_use_after_a_round_trip(self):
        _, client = self.signed_in("student")
        fields = self.hidden(self.get(client, f"/account/profile?next={START}"))
        with patch.object(account_blueprint, "safe_redirect_target", lambda value, fallback: fallback):
            self.assert_location(self.post(client, {**fields, **STUDENT_FORM}), "/")

    def test_the_failure_pages_carry_the_indicator_and_never_the_target(self):
        account, client = self.signed_in("student")
        self.execute("UPDATE users SET role = 'teacher' WHERE id = ?", (account["id"],))   # no form for a teacher
        denied = self.post(client, {**STUDENT_FORM, "next": "//evil.example"})
        self.assertEqual(denied.status_code, 403)
        self.assertNotIn("evil", self.body(denied))
        _, other = self.signed_in("student")
        with patch.object(profiles, "complete_student_profile", side_effect=sqlite3.OperationalError("boom")):
            failed = self.post(other, {**STUDENT_FORM, "next": "//evil.example"})
        self.assertEqual(failed.status_code, 500)
        self.assertNotIn("evil", self.body(failed))
        self.assertEqual(self.hidden(failed), {"next": "", "next_refused": "1"})


class EditProfileTest(ProfileRouteTestCase):
    def test_a_student_edits_the_class_and_the_names_and_the_display_name_follows(self):
        account, client = self.signed_in("student", profile="student")
        self.assertEqual(self.post(client, {"first_name": "Ана", "last_name": "Иванова", "grade": "11"}).status_code, 303)
        profile = self.query("SELECT first_name, grade, grade_set_at FROM user_profiles")[0]
        self.assertEqual(profile[:2], ("Ана", 11))
        self.assertEqual(self.user(account["id"], "display_name"), "Тест Потребител")   # names unchanged: not touched
        self.assertEqual(self.post(client, {"first_name": "Анна", "last_name": "Иванова", "grade": "11"}).status_code, 303)
        self.assertEqual(self.user(account["id"], "display_name"), "Анна Иванова")

    def test_the_stored_type_decides_which_fields_are_stored_whatever_the_role_is(self):  # I5
        for role, kind, form, expected in (
            ("student", "teacher", TEACHER_FORM, ("teacher", None, "ПГ Тест, София")),
            ("admin", "teacher", TEACHER_FORM, ("teacher", None, "ПГ Тест, София")),
            ("teacher", "student", {"first_name": "Иван", "last_name": "Петров", "grade": "12"}, ("student", 12, None)),
            ("admin", "student", {"first_name": "Иван", "last_name": "Петров", "grade": "12"}, ("student", 12, None)),
        ):
            with self.subTest(role=role, profile=kind):
                account, client = self.signed_in(role, profile=kind)
                extra = {"grade": "8", "school_name": "Друго училище", "subject": ["it"]}
                payload = {**{k: v for k, v in extra.items() if k not in form}, **form}
                response = self.post(client, payload)
                self.assertEqual(response.status_code, 303)
                self.assertEqual(self.query("SELECT account_type, grade, school_name FROM user_profiles WHERE user_id = ?",
                                            (account["id"],)), [expected])
                self.assertEqual(self.user(account["id"], "role"), role)
                subjects = self.query("SELECT subject_id, label FROM teacher_subjects WHERE user_id = ?", (account["id"],))
                self.assertEqual(subjects, [("it", "Информационни технологии"), (None, "Роден език")] if kind == "teacher" else [])

    def test_a_teacher_type_form_with_no_subject_is_refused_with_the_registration_message(self):
        account, client = self.signed_in("teacher", profile="teacher")
        before = self.snapshot()
        response = self.post(client, {"first_name": "Иван", "last_name": "Петров", "school_name": "ПГ Тест, София"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("Избери поне един предмет.", self.text(response))
        self.assertEqual(self.snapshot(), before)

    def test_editing_grants_no_role_and_no_privilege(self):  # I5, I6
        for role in ("student", "teacher"):
            with self.subTest(role=role):
                account, client = self.signed_in(role, profile="teacher" if role == "student" else "student")
                form = (TEACHER_FORM if role == "student" else {"first_name": "Иван", "last_name": "Петров", "grade": "9"})
                self.post(client, {**form, "role": "admin", "is_admin": "1", "account_type": "teacher"})
                self.assertEqual(self.user(account["id"], "role"), role)
                self.assertEqual(client.get("/teacher").status_code, 403)
                self.assertEqual(client.get("/dzi").status_code, 403)
                if role == "student":
                    self.assertEqual(client.get("/teacher/new").status_code, 403)

    def test_one_account_cannot_edit_anothers_profile(self):  # I5
        victim, _ = self.signed_in("student", profile="student")
        attacker, client = self.signed_in("student")
        before = self.query("SELECT * FROM user_profiles WHERE user_id = ?", (victim["id"],))
        self.post(client, {**STUDENT_FORM, "user_id": str(victim["id"])})
        self.assertEqual(self.query("SELECT * FROM user_profiles WHERE user_id = ?", (victim["id"],)), before)
        self.assertEqual(self.query("SELECT user_id FROM user_profiles ORDER BY user_id"),
                         [(victim["id"],), (attacker["id"],)])

    def test_a_demoted_marked_student_with_no_profile_is_offered_nothing_and_a_promoted_one_the_form(self):
        account, client = self.signed_in("student", marked=True)
        with closing(sqlite3.connect(self.db_path)) as conn:
            new_key = accounts.set_role(conn, account["id"], "teacher")
            conn.commit()
        set_account_session(client, account["id"], new_key)
        self.assertIn("Няма попълнен профил", self.text(self.get(client)))     # a teacher: nothing to fill
        self.assertEqual(self.user(account["id"], "profile_required_at"), MARK)  # the mark is neither written nor erased


# --------------------------------------------------------------------------- integrity, atomicity


class IntegrityTest(ProfileRouteTestCase):
    def test_a_cross_origin_post_is_refused_and_writes_nothing(self):  # F2
        account, client = self.signed_in("student", marked=True)
        before = self.snapshot()
        for headers in (CROSS_ORIGIN, {"Origin": "http://localhost.evil.example"},
                        {"Origin": "null"}, {"Referer": "http://evil.example/form"}):
            with self.subTest(headers=headers):
                response = self.post(client, STUDENT_FORM, headers=headers)
                self.assertEqual(response.status_code, 403)
                self.assertEqual(response.headers.getlist("Set-Cookie"), [])
        self.assertEqual(self.snapshot(), before)

    def test_a_post_with_no_origin_is_refused_outside_testing(self):
        _, client = self.signed_in("student")
        before = self.snapshot()
        with patch.dict(self.app.config, {"TESTING": False}):
            self.assertEqual(client.post("/account/profile", data=STUDENT_FORM).status_code, 403)
        self.assertEqual(self.snapshot(), before)

    def test_the_account_must_be_signed_in(self):
        anonymous = self.app.test_client()
        before = self.snapshot()
        response = self.post(anonymous, STUDENT_FORM)
        self.assertEqual((response.status_code, response.headers["Location"]), (302, "/login?next=/account/profile"))
        self.assertEqual(self.snapshot(), before)

    def test_a_disabled_account_is_signed_out_and_writes_nothing(self):
        account, client = self.signed_in("student")
        self.execute("UPDATE users SET is_active = 0 WHERE id = ?", (account["id"],))
        before = self.snapshot()
        self.assertEqual(self.post(client, STUDENT_FORM).status_code, 302)
        self.assertEqual(self.snapshot(), before)


class AtomicityTest(ProfileRouteTestCase):
    def assert_nothing_written_and_still_signed_in(self, account, client, before):
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.user(account["id"], "display_name"), "Google Име")
        self.assertEqual(self.get(client).status_code, 200)

    def test_a_failure_after_the_profile_insert_leaves_neither_write(self):
        account, client = self.signed_in("student", marked=True, display_name="Google Име")
        before = self.snapshot()
        with patch.object(accounts, "set_display_name", side_effect=sqlite3.OperationalError("disk I/O error")), \
                self.assertLogs(account_blueprint.logger, "WARNING") as logs:
            response = self.post(client, STUDENT_FORM)
        self.assertEqual(response.status_code, 500)
        self.assertIn(FAILURE, self.text(response))
        self.assertNotIn("Профилът е запазен", self.text(response))
        self.assertEqual(logs.output, [f"WARNING:{account_blueprint.logger.name}:profile_save_failed "
                                       f"user={account['id']} error=OperationalError"])
        self.assert_nothing_written_and_still_signed_in(account, client, before)
        # A retry that does not fail completes it.
        self.assertEqual(self.post(client, STUDENT_FORM).status_code, 303)
        self.assertEqual(self.user(account["id"], "display_name"), "Мария Петрова")

    def test_a_failure_of_the_commit_itself_leaves_neither_write(self):
        account, client = self.signed_in("student", marked=True, display_name="Google Име")
        before = self.snapshot()
        state = {"commits": 0, "fail": {1}}
        real = account_blueprint.get_request_db
        with patch.object(account_blueprint, "get_request_db", lambda: FlakyCommits(real(), state)), \
                self.assertLogs(account_blueprint.logger, "WARNING"):
            response = self.post(client, STUDENT_FORM)
        self.assertEqual(response.status_code, 500)
        self.assertIn(FAILURE, self.text(response))
        self.assert_nothing_written_and_still_signed_in(account, client, before)

    def test_a_failure_while_editing_leaves_the_stored_profile_and_name_as_they_were(self):
        account, client = self.signed_in("student", profile="student", display_name="Google Име")
        before = self.snapshot()
        with patch.object(accounts, "set_display_name", side_effect=sqlite3.OperationalError("boom")), \
                self.assertLogs(account_blueprint.logger, "WARNING"):
            response = self.post(client, {"first_name": "Анна", "last_name": "Иванова", "grade": "12"})
        self.assertEqual(response.status_code, 500)
        self.assert_nothing_written_and_still_signed_in(account, client, before)

    def test_a_failed_rollback_is_logged_by_class_and_still_answers_the_generic_failure(self):
        account, client = self.signed_in("student", display_name="Google Име")
        real = account_blueprint.get_request_db

        class BrokenRollback(FlakyCommits):
            def rollback(self):
                self._conn.rollback()
                raise sqlite3.OperationalError("rollback failed")

        state = {"commits": 0, "fail": {1}}
        with patch.object(account_blueprint, "get_request_db", lambda: BrokenRollback(real(), state)), \
                self.assertLogs(account_blueprint.logger, "WARNING") as logs:
            response = self.post(client, STUDENT_FORM)
        self.assertEqual(response.status_code, 500)
        self.assertTrue(any("profile_rollback_failed error=OperationalError" in line for line in logs.output))
        self.assertEqual(self.profile_rows(), ([], []))

    def test_no_log_line_holds_a_name_a_school_or_a_value(self):
        account, client = self.signed_in("student")
        with patch.object(accounts, "set_display_name", side_effect=sqlite3.OperationalError("Мария Петрова")), \
                self.assertLogs(account_blueprint.logger, "WARNING") as logs:
            self.post(client, STUDENT_FORM)
        joined = "\n".join(logs.output)
        for secret in ("Мария", "Петрова", "10"):
            self.assertNotIn(secret, joined.replace(str(account["id"]), ""))


# --------------------------------------------------------------------------- the three gates are independent


class GateIndependenceTest(ProfileRouteTestCase):
    """J15: registration, teacher and profile gates each decide only their own surface."""

    gate = False

    def test_every_combination_of_the_three_gates_answers_by_its_own_gate(self):
        for registration, teacher, profile in itertools.product((False, True), repeat=3):
            with self.subTest(registration=registration, teacher=teacher, profile=profile):
                account, client = self.signed_in("student", marked=True)
                assignment_id = self.assignment()
                settings = RegistrationSettings(mailer=InMemoryMailer()) if registration else None
                with patch.dict(self.app.config, {REGISTRATION_KEY: settings, profiles.CONFIG_KEY: profile}), \
                        patch.object(registration_service, "TEACHER_ACTIVATION", teacher):
                    anonymous = self.app.test_client()
                    # Registration: by its own gate alone. The teacher form needs registration *and* the
                    # teacher gate (PR 7); the teacher gate alone opens nothing, and the profile gate never matters.
                    self.assertEqual(anonymous.get("/register").status_code, 200 if registration else 404)
                    self.assertEqual(anonymous.get("/register/student").status_code, 200 if registration else 404)
                    self.assertEqual(anonymous.get("/register/teacher").status_code,
                                     200 if registration and teacher else 404)
                    # The profile page, its link and the new-attempt check: by the profile gate alone.
                    self.assertEqual(client.get("/account/profile").status_code, 200 if profile else 404)
                    self.assertEqual("/account/profile" in client.get("/account").get_data(as_text=True), profile)
                    started = client.post(student_entry_path(assignment_id), headers=SAME_ORIGIN)
                    if profile:
                        self.assertEqual((started.status_code, started.headers["Location"]),
                                         (303, f"/account/profile?next={student_entry_path(assignment_id)}"))
                    else:
                        self.assertEqual(started.status_code, 302)
                        self.assertRegex(started.headers["Location"], r"^/quiz/attempt/\d+$")

    def test_a_profile_that_registration_created_exists_whatever_the_profile_gate_says(self):
        """Registration on with profiles off creates accounts with profiles that cannot yet be
        viewed, and blocks no attempt (J15)."""
        account, client = self.signed_in("student", profile="student")
        self.assertEqual(client.get("/account/profile").status_code, 404)
        self.assertEqual(self.query("SELECT COUNT(*) FROM user_profiles"), [(1,)])
        self.assertEqual(client.post(student_entry_path(self.assignment()), headers=SAME_ORIGIN).status_code, 302)


class ProfilePageAccessTest(ProfileRouteTestCase):
    def test_who_reaches_the_page_with_the_gate_on(self):
        anonymous = self.app.test_client().get("/account/profile")
        self.assertEqual((anonymous.status_code, anonymous.headers["Location"]), (302, "/login?next=/account/profile"))
        for role in ("student", "teacher", "admin"):
            with self.subTest(role=role):
                _, client = self.signed_in(role)
                self.assertEqual(client.get("/account/profile").status_code, 200)

    def test_the_page_grants_no_other_route(self):
        for role in ("student", "teacher"):
            with self.subTest(role=role):
                _, client = self.signed_in(role, profile="student")
                self.post(client, {"first_name": "Ана", "last_name": "Иванова", "grade": "9"})
                self.assertEqual(client.get("/dzi").status_code, 403)
                self.assertEqual(client.get("/teacher").status_code, 403)
                self.assertEqual(client.get("/teacher/dzi-training").status_code, 403)


if __name__ == "__main__":
    import unittest
    unittest.main()
