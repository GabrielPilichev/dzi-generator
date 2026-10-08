"""Public assignment tokens: creation, the /quiz/s/<public_token> entry, student links.

The token names an assignment; it is not a credential. Starting an attempt still
needs an account and attempts stay account-owned. The numeric /quiz/<id> entry is
retired (tests/test_numeric_quiz_entry_retired.py). Everything runs on the class's
temporary database and vault.
"""

import re
import sqlite3
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from tests import runtime_fixtures
from tests.auth_helpers import create_account, set_account_session
from tests.support import ROOT, IsolatedAppTestCase, web_app
from web import assignment_tokens
from web.access import account_principal
from web.services import quiz

SAME_ORIGIN = {"Origin": "http://localhost"}
CROSS_ORIGIN = {"Origin": "http://evil.example"}
UNKNOWN_TOKEN = "Z" * 43
TOKEN_SHAPE = r"[A-Za-z0-9_-]{43}"
NO_LINK_WARNING = "Този тест още няма защитен линк за ученици."


class TokenFormatTest(unittest.TestCase):
    def test_new_token_is_256_bits_of_urlsafe_text_from_secrets(self):
        with patch.object(assignment_tokens.secrets, "token_urlsafe", wraps=assignment_tokens.secrets.token_urlsafe) as spy:
            token = assignment_tokens.new_public_token()
        spy.assert_called_once_with(32)
        self.assertEqual(assignment_tokens.PUBLIC_TOKEN_BYTES * 8, 256)
        self.assertRegex(token, f"^{TOKEN_SHAPE}$")
        self.assertTrue(assignment_tokens.is_valid_public_token(token))
        self.assertEqual(len({assignment_tokens.new_public_token() for _ in range(200)}), 200)

    def test_only_the_exact_shape_is_valid(self):
        for value in (None, "", 5, b"A" * 43, "A" * 42, "A" * 44, "A" * 42 + "=", "A" * 42 + "/",
                      "A" * 42 + "+", "A" * 42 + " ", "A" * 42 + "\n", "A" * 43 + "\n", "А" * 43, "None", "null"):
            with self.subTest(value=value):
                self.assertFalse(assignment_tokens.is_valid_public_token(value))
        self.assertTrue(assignment_tokens.is_valid_public_token("aZ0_-" + "A" * 38))


class PublicTokenAppTestCase(IsolatedAppTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        web_app.app.config.update(TESTING=True)
        with closing(web_app.quiz_db()) as conn:
            for row in conn.execute("SELECT id FROM curriculum_sections ORDER BY id"):
                if len(quiz.quiz_section_question_ids(conn, row["id"])) >= 3:
                    cls.section_id = int(row["id"])
                    break
            else:
                raise AssertionError("No canonical section with eligible MC questions")

    @property
    def db_path(self):
        return web_app.app.config["DB_PATH"]

    def query(self, sql, params=()):
        with closing(web_app.quiz_db()) as conn:
            return conn.execute(sql, params).fetchall()

    def account(self, role="student", **options):
        return create_account(self.db_path, role, **options)

    def client(self, account=None):
        client = web_app.app.test_client()
        if account:
            set_account_session(client, account["id"], account["session_key"])
        return client

    def create(self, owner, *, title="Тест с токен", question_plan_json=None, time_limit_minutes=None):
        """An assignment made by the application's creation service."""
        with closing(web_app.quiz_db()) as conn:
            assignment_id = quiz.quiz_create_assignment(
                conn, principal=account_principal(owner["id"], "teacher", "Учител"),
                section_id=self.section_id, title_bg=title, question_count=2,
                time_limit_minutes=time_limit_minutes, question_plan_json=question_plan_json)
            conn.commit()
        return assignment_id

    def legacy(self, owner=None):
        """An assignment from before migration 017: no public token."""
        with closing(web_app.quiz_db()) as conn:
            assignment_id = runtime_fixtures.add_assignment(
                conn, section_id=self.section_id, title="Стар тест", question_count=2)
            if owner is not None:
                conn.execute("UPDATE quiz_assignments SET owner_user_id = ? WHERE id = ?",
                             (owner["id"], assignment_id))
            conn.commit()
        return assignment_id

    def row(self, assignment_id):
        return self.query("SELECT * FROM quiz_assignments WHERE id = ?", (assignment_id,))[0]

    def token(self, assignment_id):
        return self.row(assignment_id)["public_token"]

    def attempts_of(self, assignment_id):
        return self.query("SELECT * FROM quiz_attempts WHERE assignment_id = ? ORDER BY id", (assignment_id,))

    def attempt_count(self):
        return self.query("SELECT COUNT(*) FROM quiz_attempts")[0][0]

    def start(self, client, path):
        response = client.post(path, headers=SAME_ORIGIN)
        self.assertEqual(response.status_code, 302, response.get_data(as_text=True)[:300])
        self.assertRegex(response.headers["Location"], r"^/quiz/attempt/\d+(/result)?$")
        return int(response.headers["Location"].split("/")[3])

    def submit(self, client, attempt_id):
        plan = quiz.quiz_parse_attempt_question_plan(
            self.query("SELECT question_ids_json FROM quiz_attempts WHERE id = ?", (attempt_id,))[0][0])
        return client.post(f"/quiz/attempt/{attempt_id}", data={f"q_{plan['question_ids'][0]}": "A"},
                           headers=SAME_ORIGIN)


class TokenCreationTest(PublicTokenAppTestCase):
    def setUp(self):
        self.teacher = self.account("teacher", display_name="Учител")

    def test_new_assignment_gets_a_secure_unique_token(self):
        with patch.object(assignment_tokens.secrets, "token_urlsafe",
                          wraps=assignment_tokens.secrets.token_urlsafe) as spy:
            ids = [self.create(self.teacher, title=f"Тест {n}") for n in range(5)]
        self.assertEqual([call.args for call in spy.call_args_list], [(32,)] * 5)
        tokens = [self.token(assignment_id) for assignment_id in ids]
        for token in tokens:
            self.assertRegex(token, f"^{TOKEN_SHAPE}$")
        self.assertEqual(len(set(tokens)), 5)
        self.assertEqual(ids, sorted(ids))  # ids stay sequential and are still the management key
        self.assertEqual({self.row(i)["owner_user_id"] for i in ids}, {self.teacher["id"]})

    def test_creation_stays_inside_the_callers_transaction(self):
        before = self.query("SELECT COUNT(*) FROM quiz_assignments")[0][0]
        with closing(web_app.quiz_db()) as conn:
            quiz.quiz_create_assignment(
                conn, principal=account_principal(self.teacher["id"], "teacher", "Учител"),
                section_id=self.section_id, title_bg="Отказан", question_count=1)
            self.assertTrue(conn.in_transaction)
            conn.rollback()
        self.assertEqual(self.query("SELECT COUNT(*) FROM quiz_assignments")[0][0], before)

    def test_token_collision_is_retried_with_a_new_token(self):
        taken = self.token(self.create(self.teacher))
        fresh = "N" * 43
        with patch.object(assignment_tokens, "new_public_token", side_effect=[taken, taken, fresh]) as generator:
            assignment_id = self.create(self.teacher, title="След сблъсък")
        self.assertEqual(generator.call_count, 3)
        self.assertEqual(self.token(assignment_id), fresh)
        self.assertEqual(self.query("SELECT COUNT(*) FROM quiz_assignments WHERE public_token = ?", (taken,))[0][0], 1)

    def test_collision_retry_is_bounded_and_inserts_nothing(self):
        taken = self.token(self.create(self.teacher))
        before = self.query("SELECT COUNT(*) FROM quiz_assignments")[0][0]
        with patch.object(assignment_tokens, "new_public_token", return_value=taken) as generator:
            with self.assertRaises(assignment_tokens.PublicTokenError):
                self.create(self.teacher, title="Никога")
        self.assertEqual(generator.call_count, assignment_tokens.PUBLIC_TOKEN_MAX_ATTEMPTS)
        self.assertEqual(self.query("SELECT COUNT(*) FROM quiz_assignments")[0][0], before)

    def test_unrelated_integrity_error_propagates_without_a_retry(self):
        before = self.query("SELECT COUNT(*) FROM quiz_assignments")[0][0]
        with patch.object(assignment_tokens, "new_public_token",
                          wraps=assignment_tokens.new_public_token) as generator:
            with self.assertRaises(sqlite3.IntegrityError) as raised:
                self.create(self.teacher, title=None)  # title_bg is NOT NULL
        self.assertIn("title_bg", str(raised.exception))
        self.assertEqual(generator.call_count, 1)
        self.assertEqual(self.query("SELECT COUNT(*) FROM quiz_assignments")[0][0], before)

    def test_no_insert_or_replace_anywhere_near_assignments(self):
        for relative in ("web/services/quiz.py", "web/assignment_tokens.py", "src/manage_assignment_tokens.py"):
            source = (ROOT / relative).read_text(encoding="utf-8").upper()
            self.assertNotIn("INSERT OR REPLACE", source, relative)
            self.assertNotRegex(source, r"REPLACE\s+INTO", relative)

    def test_ordinary_edit_keeps_the_token(self):
        assignment_id = self.create(self.teacher, question_plan_json=None)
        before = dict(self.row(assignment_id))
        response = self.client(self.teacher).post(
            f"/teacher/assignment/{assignment_id}/edit",
            data={"title_bg": "Ново заглавие", "time_limit_minutes": "25", "public_token": UNKNOWN_TOKEN,
                  "owner_user_id": "1"},
            headers=SAME_ORIGIN)
        self.assertEqual(response.status_code, 302)
        after = dict(self.row(assignment_id))
        self.assertEqual((after["title_bg"], after["time_limit_minutes"]), ("Ново заглавие", 25))
        for column in ("id", "public_token", "owner_user_id", "question_plan_json", "section_id",
                       "question_count", "created_at"):
            self.assertEqual(after[column], before[column], column)

    def test_duplicate_gets_its_own_token_and_changes_nothing_on_the_source(self):
        source_id = self.create(self.teacher)
        source_before = tuple(self.row(source_id))
        source_token = self.token(source_id)
        admin = self.account("admin", display_name="Админ")
        response = self.client(admin).post(f"/teacher/assignment/{source_id}/duplicate", headers=SAME_ORIGIN)
        self.assertEqual(response.status_code, 302)
        copy_id = int(response.headers["Location"].rsplit("/", 1)[1])
        self.assertNotEqual(copy_id, source_id)
        copy = self.row(copy_id)
        self.assertRegex(copy["public_token"], f"^{TOKEN_SHAPE}$")
        self.assertNotEqual(copy["public_token"], source_token)
        self.assertEqual(copy["owner_user_id"], admin["id"])
        self.assertEqual(copy["question_plan_json"], self.row(source_id)["question_plan_json"])
        self.assertEqual(tuple(self.row(source_id)), source_before)

    def test_duplicate_of_a_legacy_assignment_gets_a_token_and_the_source_stays_null(self):
        source_id = self.legacy(owner=self.teacher)
        response = self.client(self.teacher).post(
            f"/teacher/assignment/{source_id}/duplicate", headers=SAME_ORIGIN)
        copy_id = int(response.headers["Location"].rsplit("/", 1)[1])
        self.assertRegex(self.token(copy_id), f"^{TOKEN_SHAPE}$")
        self.assertIsNone(self.token(source_id))


class TokenRouteTest(PublicTokenAppTestCase):
    def setUp(self):
        self.teacher = self.account("teacher", display_name="Учител Собственик")
        self.admin = self.account("admin", display_name="Админ Профил")
        self.student = self.account(display_name="Ученик Първи")
        self.other_student = self.account(display_name="Ученик Втори")
        self.assignment_id = self.create(self.teacher, title="Тест по токен")
        self.public_token = self.token(self.assignment_id)
        self.path = f"/quiz/s/{self.public_token}"

    def test_get_shows_the_landing_page_to_everyone(self):
        anonymous = self.client().get(self.path)
        self.assertEqual(anonymous.status_code, 200)
        html = anonymous.get_data(as_text=True)
        self.assertIn("Тест по токен", html)
        self.assertIn(f'href="/login?next={self.path}"', html)
        self.assertNotIn('type="submit">Продължи към теста', html)
        self.assertEqual(self.attempts_of(self.assignment_id), [])

        signed_in = self.client(self.student).get(self.path)
        self.assertEqual(signed_in.status_code, 200)
        page = signed_in.get_data(as_text=True)
        self.assertIn("Решаваш като <strong>Ученик Първи</strong>", page)
        self.assertIn('type="submit">Продължи към теста', page)
        # The form posts back to the token URL, never to the numeric one.
        self.assertIn('<form method="post" class="card form-card', page)
        self.assertNotIn(f"/quiz/{self.assignment_id}", page)
        self.assertEqual(self.attempts_of(self.assignment_id), [])

    def test_anonymous_post_creates_nothing_and_goes_to_the_sign_in(self):
        before = self.attempt_count()
        response = self.client().post(self.path, data={"student_name": "Натрапник", "user_id": self.student["id"]},
                                      headers=SAME_ORIGIN)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], f"/login?next={self.path}")
        self.assertEqual(self.attempt_count(), before)

    def test_each_role_starts_and_resumes_its_own_attempt(self):
        attempts = {}
        for label, account in (("student", self.student), ("teacher", self.teacher), ("admin", self.admin)):
            with self.subTest(role=label):
                client = self.client(account)
                attempt_id = self.start(client, self.path)
                row = self.query("SELECT * FROM quiz_attempts WHERE id = ?", (attempt_id,))[0]
                self.assertEqual((row["assignment_id"], row["user_id"]), (self.assignment_id, account["id"]))
                # Same account, another browser: the same attempt.
                self.assertEqual(self.start(self.client(account), self.path), attempt_id)
                self.assertEqual(client.get(f"/quiz/attempt/{attempt_id}").status_code, 200)
                attempts[label] = attempt_id
        self.assertEqual(len(set(attempts.values())), 3)
        self.assertEqual(len(self.attempts_of(self.assignment_id)), 3)

    def test_submitted_attempt_resumes_to_its_result(self):
        client = self.client(self.student)
        attempt_id = self.start(client, self.path)
        self.assertEqual(self.submit(client, attempt_id).status_code, 302)
        response = client.post(self.path, headers=SAME_ORIGIN)
        self.assertEqual(response.headers["Location"], f"/quiz/attempt/{attempt_id}/result")

    def test_time_limit_applies_as_on_the_numeric_entry(self):
        assignment_id = self.create(self.teacher, time_limit_minutes=7)
        path = f"/quiz/s/{self.token(assignment_id)}"
        client = self.client(self.student)
        self.assertIn("7 мин", client.get(path).get_data(as_text=True))
        attempt_id = self.start(client, path)
        attempt = self.query("SELECT * FROM quiz_attempts WHERE id = ?", (attempt_id,))[0]
        with closing(web_app.quiz_db()) as conn:
            remaining = quiz.quiz_remaining_seconds(quiz.quiz_fetch_assignment(conn, assignment_id), attempt)
        self.assertTrue(0 < remaining <= 7 * 60)
        self.assertEqual(client.get(f"/quiz/attempt/{attempt_id}").status_code, 200)

    def test_unreadable_plan_is_a_400_as_on_the_numeric_entry(self):
        assignment_id = self.create(self.teacher, question_plan_json="{not json")
        before = self.attempt_count()
        response = self.client(self.student).post(f"/quiz/s/{self.token(assignment_id)}", headers=SAME_ORIGIN)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.attempt_count(), before)

    def test_unknown_malformed_and_null_tokens_are_the_same_404(self):
        legacy_id = self.legacy(owner=self.teacher)
        self.assertIsNone(self.token(legacy_id))
        candidates = (
            UNKNOWN_TOKEN,                          # well-formed, names nothing
            self.public_token[:-1],                 # one character short
            self.public_token + "A",                # one too long
            self.public_token[:-1] + "!",           # outside the alphabet
            self.public_token.swapcase() if self.public_token.swapcase() != self.public_token else UNKNOWN_TOKEN,
            str(self.assignment_id), str(legacy_id),  # a numeric id is not a token
            "None", "null", "NULL", "%00", "%20" * 43, "' OR 1=1 --", "%25" * 43,
        )
        reference = self.client().get(f"/quiz/s/{UNKNOWN_TOKEN}")
        self.assertEqual(reference.status_code, 404)
        before = self.attempt_count()
        for candidate in candidates:
            for label, account in (("anonymous", None), ("student", self.student), ("admin", self.admin)):
                with self.subTest(candidate=candidate, who=label):
                    client = self.client(account)
                    response = client.get(f"/quiz/s/{candidate}")
                    self.assertEqual(response.status_code, 404)
                    body = response.get_data(as_text=True)
                    if self.public_token not in candidate:  # the 404 page shows the requested path
                        self.assertNotIn(self.public_token, body)
                    self.assertNotIn("Стар тест", body)
                    self.assertNotIn("Тест по токен", body)
                    self.assertEqual(client.post(f"/quiz/s/{candidate}", headers=SAME_ORIGIN).status_code, 404)
        for path in ("/quiz/s/", "/quiz/s", f"/quiz/s/{self.public_token}/", f"/quiz/s/{self.public_token}/x"):
            with self.subTest(path=path):
                self.assertEqual(self.client(self.student).get(path).status_code, 404)
        self.assertEqual(self.attempt_count(), before)

    def test_lookup_is_exact_and_parameterized(self):
        with closing(web_app.quiz_db()) as conn:
            statements = []
            conn.set_trace_callback(statements.append)
            row = quiz.quiz_fetch_assignment_by_public_token(conn, self.public_token)
            self.assertEqual(row["id"], self.assignment_id)
            self.assertEqual(len(statements), 1)
            statements.clear()
            for value in (None, "", UNKNOWN_TOKEN[:-1], self.public_token + " ", 5):
                self.assertIsNone(quiz.quiz_fetch_assignment_by_public_token(conn, value))
            self.assertEqual(statements, [])  # refused before the database is asked
            self.assertIsNone(quiz.quiz_fetch_assignment_by_public_token(conn, UNKNOWN_TOKEN))
        source = (ROOT / "web" / "services" / "quiz.py").read_text(encoding="utf-8")
        self.assertIn('" WHERE qa.public_token = ?", (public_token,)', source)

    def test_cross_origin_post_is_refused_before_anything_happens(self):
        before = self.attempt_count()
        response = self.client(self.student).post(self.path, headers=CROSS_ORIGIN)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.attempt_count(), before)

    def test_the_token_never_opens_another_accounts_attempt(self):
        attempt_id = self.start(self.client(self.student), self.path)
        for label, account in (("anonymous", None), ("other student", self.other_student),
                               ("owning teacher", self.teacher), ("admin", self.admin)):
            with self.subTest(who=label):
                client = self.client(account)
                self.assertEqual(client.get(self.path).status_code, 200)  # knows the token
                self.assertEqual(client.get(f"/quiz/attempt/{attempt_id}").status_code, 404)
                self.assertEqual(client.get(f"/quiz/attempt/{attempt_id}/result").status_code, 404)
                self.assertEqual(self.submit(client, attempt_id).status_code, 404)
                if account:
                    own = self.start(client, self.path)
                    self.assertNotEqual(own, attempt_id)
                    self.assertEqual(
                        self.query("SELECT user_id FROM quiz_attempts WHERE id = ?", (own,))[0][0], account["id"])
        row = self.query("SELECT user_id, submitted_at FROM quiz_attempts WHERE id = ?", (attempt_id,))[0]
        self.assertEqual(tuple(row), (self.student["id"], None))

    def test_historical_attempts_are_never_matched_through_the_token(self):
        with closing(web_app.quiz_db()) as conn:
            historical = runtime_fixtures.add_attempt(
                conn, self.assignment_id, student_name="Ученик Първи",
                question_ids=[int(q) for q in quiz.quiz_section_question_ids(conn, self.section_id)[:2]])
            conn.commit()
        own = self.start(self.client(self.student), self.path)
        self.assertNotEqual(own, historical)
        self.assertIsNone(self.query("SELECT user_id FROM quiz_attempts WHERE id = ?", (historical,))[0][0])
        self.assertEqual(self.client(self.student).get(f"/quiz/attempt/{historical}").status_code, 404)


class LoginReturnTest(PublicTokenAppTestCase):
    def setUp(self):
        self.teacher = self.account("teacher", display_name="Учител")
        self.student = self.account(display_name="Ученик")
        self.admin = self.account("admin", display_name="Админ")
        self.path = f"/quiz/s/{self.token(self.create(self.teacher))}"

    def test_signed_in_account_is_sent_on_to_the_safe_next(self):
        for label, account in (("student", self.student), ("teacher", self.teacher), ("admin", self.admin)):
            with self.subTest(role=label):
                response = self.client(account).get(f"/login?next={self.path}")
                self.assertEqual(response.status_code, 302)
                self.assertEqual(response.headers["Location"], self.path)

    def test_signed_in_without_a_usable_next_goes_to_the_role_home(self):
        homes = {"student": "/", "teacher": "/teacher/new", "admin": "/teacher"}
        accounts = {"student": self.student, "teacher": self.teacher, "admin": self.admin}
        unsafe = ("", "//evil.example/quiz", "https://evil.example/", "/\\evil.example", "quiz/s/x",
                  "javascript:alert(1)", "/ok\r\nSet-Cookie: x=1")
        for role, account in accounts.items():
            for candidate in unsafe:
                with self.subTest(role=role, next=candidate):
                    response = self.client(account).get("/login", query_string={"next": candidate})
                    self.assertEqual(response.status_code, 302)
                    self.assertEqual(response.headers["Location"], homes[role])

    def test_anonymous_still_gets_the_form_with_the_next_kept(self):
        response = self.client().get(f"/login?next={self.path}")
        self.assertEqual(response.status_code, 200)
        self.assertIn(f'name="next" value="{self.path}"', response.get_data(as_text=True))

    def test_sign_in_then_returns_to_the_token_page(self):
        account = self.account(display_name="С парола", email="token-return@example.bg",
                               password="correct horse battery")
        client = self.client()
        response = client.post("/login", data={"email": "token-return@example.bg",
                                               "password": "correct horse battery", "next": self.path},
                               headers=SAME_ORIGIN)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], self.path)
        attempt_id = self.start(client, self.path)
        self.assertEqual(self.query("SELECT user_id FROM quiz_attempts WHERE id = ?", (attempt_id,))[0][0],
                         account["id"])

    def test_pending_second_factor_is_not_signed_in(self):
        client = self.client()
        with client.session_transaction() as session:
            session["mfa_uid"] = self.admin["id"]
            session["mfa_sk"] = self.admin["session_key"]
            session["mfa_iat"] = int(time.time())
        before = self.attempt_count()
        login = client.get(f"/login?next={self.path}")
        self.assertEqual(login.status_code, 200)  # the form, not a redirect past the second factor
        self.assertNotIn("Location", login.headers)
        self.assertEqual(client.get("/login?next=/teacher").status_code, 200)
        self.assertEqual(client.get("/teacher").status_code, 302)
        post = client.post(self.path, headers=SAME_ORIGIN)
        self.assertEqual(post.headers["Location"], f"/login?next={self.path}")
        self.assertEqual(self.attempt_count(), before)

    def test_stale_and_inactive_sessions_get_the_form(self):
        stale = web_app.app.test_client()
        set_account_session(stale, self.student["id"], "x" * 43)
        self.assertEqual(stale.get(f"/login?next={self.path}").status_code, 200)

        disabled = self.account(display_name="Изключен")
        client = self.client(disabled)
        self.assertEqual(client.get(f"/login?next={self.path}").status_code, 302)
        with closing(web_app.quiz_db()) as conn:
            conn.execute("UPDATE users SET is_active = 0 WHERE id = ?", (disabled["id"],))
            conn.commit()
        response = client.get(f"/login?next={self.path}")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("Location", response.headers)
        self.assertEqual(client.post(self.path, headers=SAME_ORIGIN).headers["Location"],
                         f"/login?next={self.path}")


class StudentLinkTest(PublicTokenAppTestCase):
    def setUp(self):
        self.teacher = self.account("teacher", display_name="Учител")
        self.student = self.account(display_name="Ученик")
        self.assignment_id = self.create(self.teacher, title="Тест с линк")
        self.public_token = self.token(self.assignment_id)
        self.legacy_id = self.legacy(owner=self.teacher)

    def assert_no_numeric_student_link(self, html, assignment_id):
        self.assertNotRegex(html, rf'["\'/]quiz/{assignment_id}["\'?#]')
        self.assertNotIn(f"localhost/quiz/{assignment_id}", html)

    def test_teacher_detail_shares_the_token_link(self):
        html = self.client(self.teacher).get(f"/teacher/assignment/{self.assignment_id}").get_data(as_text=True)
        self.assertIn(f'value="http://localhost/quiz/s/{self.public_token}"', html)
        self.assertIn(f'<a class="button" href="/quiz/s/{self.public_token}">Отвори теста</a>', html)
        self.assertNotIn(NO_LINK_WARNING, html)
        self.assert_no_numeric_student_link(html, self.assignment_id)
        # Management links keep the numeric id.
        self.assertIn(f'href="/teacher/assignment/{self.assignment_id}/results"', html)

    def test_teacher_detail_of_a_legacy_assignment_warns_and_offers_no_link(self):
        response = self.client(self.teacher).get(f"/teacher/assignment/{self.legacy_id}")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn(NO_LINK_WARNING, html)
        self.assertIn("src.manage_assignment_tokens", html)
        self.assertNotIn('id="share-link"', html)
        self.assertNotIn("Отвори теста", html)
        self.assertNotIn("/quiz/s/", html)
        self.assertNotIn("None", html)
        self.assert_no_numeric_student_link(html, self.legacy_id)

    def test_teacher_listing_links_by_token_and_marks_legacy_rows(self):
        html = self.client(self.teacher).get("/teacher/assignments").get_data(as_text=True)
        self.assertIn(f'<a href="/quiz/s/{self.public_token}">Линк за ученици</a>', html)
        self.assertEqual(html.count("Няма линк за ученици"), 1)
        self.assertEqual(len(re.findall(r'href="/quiz/s/', html)), 1)
        for assignment_id in (self.assignment_id, self.legacy_id):
            self.assert_no_numeric_student_link(html, assignment_id)

    def test_edit_error_rendering_keeps_the_token_link(self):
        client = self.client(self.teacher)
        response = client.post(f"/teacher/assignment/{self.assignment_id}/edit",
                               data={"title_bg": "", "time_limit_minutes": ""}, headers=SAME_ORIGIN)
        self.assertEqual(response.status_code, 400)
        html = response.get_data(as_text=True)
        self.assertIn("Заглавието е задължително.", html)
        self.assertIn(f'value="http://localhost/quiz/s/{self.public_token}"', html)
        self.assert_no_numeric_student_link(html, self.assignment_id)

        legacy = client.post(f"/teacher/assignment/{self.legacy_id}/edit",
                             data={"title_bg": "", "time_limit_minutes": ""}, headers=SAME_ORIGIN)
        self.assertEqual(legacy.status_code, 400)
        self.assertIn(NO_LINK_WARNING, legacy.get_data(as_text=True))
        self.assert_no_numeric_student_link(legacy.get_data(as_text=True), self.legacy_id)

    def test_quiz_result_links_back_by_token(self):
        client = self.client(self.student)
        attempt_id = self.start(client, f"/quiz/s/{self.public_token}")
        self.assertEqual(self.submit(client, attempt_id).status_code, 302)
        html = client.get(f"/quiz/attempt/{attempt_id}/result").get_data(as_text=True)
        self.assertIn(f'<a class="button" href="/quiz/s/{self.public_token}">Нов опит</a>', html)
        self.assert_no_numeric_student_link(html, self.assignment_id)

    def test_quiz_result_of_a_legacy_assignment_has_no_entry_link(self):
        client = self.client(self.student)
        # A legacy assignment has no entry URL; its attempt predates the retirement.
        with closing(web_app.quiz_db()) as conn:
            attempt_id = quiz.quiz_start_or_resume_attempt(
                conn, quiz.quiz_fetch_assignment(conn, self.legacy_id),
                user_id=self.student["id"], display_name="Ученик").attempt_id
        self.assertEqual(self.submit(client, attempt_id).status_code, 302)
        response = client.get(f"/quiz/attempt/{attempt_id}/result")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertNotIn("Нов опит", html)
        self.assertNotIn("/quiz/s/", html)
        self.assert_no_numeric_student_link(html, self.legacy_id)

    def note(self, assignment_id):
        with web_app.app.test_request_context("/"):
            quiz.quiz_write_assignment_note(assignment_id, "http://localhost/")
        notes = list((Path(web_app.QUIZ_VAULT_PATH) / "Generated" / "Quizzes").glob(
            f"*-assignment-{assignment_id}-*.md"))
        self.assertEqual(len(notes), 1)
        self.assertTrue(notes[0].resolve().is_relative_to(Path(self.isolation_root).resolve()))
        return notes[0].read_text(encoding="utf-8")

    def test_generated_assignment_note_uses_the_token_link(self):
        note = self.note(self.assignment_id)
        self.assertIn(f"**Споделяем линк:** http://localhost/quiz/s/{self.public_token}\n", note)
        self.assertNotIn(f"/quiz/{self.assignment_id}", note)

    def test_generated_note_of_a_legacy_assignment_has_no_link(self):
        note = self.note(self.legacy_id)
        self.assertIn(f"**Споделяем линк:** {quiz.QUIZ_NO_PUBLIC_LINK_NOTE}\n", note)
        self.assertNotIn("http", note)
        self.assertNotIn("/quiz/", note)

    def test_entry_path_is_none_for_missing_or_invalid_tokens(self):
        with web_app.app.test_request_context("/"):
            self.assertEqual(quiz.quiz_student_entry_path({"public_token": self.public_token}),
                             f"/quiz/s/{self.public_token}")
            for value in (None, "", "short", "A" * 42 + "/", 5):
                with self.subTest(value=value):
                    self.assertIsNone(quiz.quiz_student_entry_path({"public_token": value}))

    def test_shared_link_uses_the_trusted_request_host_only(self):
        path = f"/teacher/assignment/{self.assignment_id}"
        with patch.dict(web_app.app.config, {"TRUSTED_HOSTS": ["learnpilot.test"]}):
            client = web_app.app.test_client()
            with client.session_transaction(base_url="https://learnpilot.test") as session:
                session.update(uid=self.teacher["id"], sk=self.teacher["session_key"], iat=int(time.time()))
            trusted = client.get(path, base_url="https://learnpilot.test")
            self.assertEqual(trusted.status_code, 200)
            self.assertIn(f'value="https://learnpilot.test/quiz/s/{self.public_token}"',
                          trusted.get_data(as_text=True))
            untrusted = client.get(path, base_url="https://evil.example")
            self.assertEqual(untrusted.status_code, 400)
            self.assertNotIn(self.public_token, untrusted.get_data(as_text=True))

    def test_no_template_or_view_builds_the_numeric_student_url(self):
        for path in sorted((ROOT / "web" / "templates").glob("*.html")):
            self.assertNotIn("quiz.quiz_start'", path.read_text(encoding="utf-8"), path.name)
        for relative in ("web/blueprints/teacher.py", "web/services/quiz.py"):
            self.assertNotIn('"quiz.quiz_start"', (ROOT / relative).read_text(encoding="utf-8"), relative)


class NginxQuizWriteLimitTest(unittest.TestCase):
    def setUp(self):
        conf = (ROOT / "deploy" / "nginx" / "learnpilot.conf").read_text(encoding="utf-8")
        self.limited = re.findall(
            r"location ~ (\S+) \{\s+limit_req zone=lp_quiz_write burst=90 nodelay;\s+proxy_pass \S+;\s+\}", conf)

    def matches(self, path):
        return [pattern for pattern in self.limited if re.search(pattern, path)]

    def test_token_and_numeric_entries_are_both_write_limited(self):
        self.assertEqual(len(self.limited), 3)
        token = assignment_tokens.new_public_token()
        for path in (f"/quiz/s/{token}", "/quiz/s/" + "-_aZ09" * 7 + "x", "/quiz/5", "/quiz/123456",
                     "/quiz/attempt/5"):
            with self.subTest(path=path):
                self.assertEqual(len(self.matches(path)), 1)

    def test_limits_stay_narrow(self):
        for path in ("/quiz/s/", "/quiz/s/a/b", "/quiz/s/abc/result", "/quiz/attempt/5/result",
                     "/teacher/assignment/5", "/login", "/quiz/sx"):
            with self.subTest(path=path):
                self.assertEqual(self.matches(path), [])


if __name__ == "__main__":
    unittest.main()
