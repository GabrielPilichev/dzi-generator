"""The sequential student entry /quiz/<assignment_id> is retired.

Every such URL is the same generic 404: no lookup, no redirect, no token, no
attempt, whoever asks and whether or not the assignment exists. Students enter only
through /quiz/s/<public_token>. Numeric teacher-management URLs and numeric
account-owned attempt URLs are a different thing and keep working.
"""

import re
import unittest
from contextlib import closing

from werkzeug.exceptions import NotFound

from tests import runtime_fixtures
from tests.support import NO_DEADLINE_FORM, ROOT, student_entry_path
from tests.test_assignment_public_tokens import CROSS_ORIGIN, SAME_ORIGIN, PublicTokenAppTestCase, web_app
from web.services import quiz

MISSING_ID = 987654321


class NumericEntryRetiredTest(PublicTokenAppTestCase):
    def setUp(self):
        self.teacher = self.account("teacher", display_name="Учител Собственик")
        self.admin = self.account("admin", display_name="Админ")
        self.student = self.account(display_name="Ученик")
        self.assignment_id = self.create(self.teacher, title="Тест със скрит номер")
        self.public_token = self.token(self.assignment_id)
        self.legacy_id = self.legacy(owner=self.teacher)
        self.principals = (("anonymous", None), ("student", self.student),
                           ("owning teacher", self.teacher), ("admin", self.admin))

    def request(self, client, method, path):
        headers = SAME_ORIGIN if method not in ("get", "head") else {}
        return getattr(client, method)(path, headers=headers)

    def test_no_url_rule_or_endpoint_serves_a_numeric_entry(self):
        rules = [rule.rule for rule in web_app.app.url_map.iter_rules()]
        self.assertNotIn("/quiz/<int:assignment_id>", rules)
        self.assertEqual(sorted(rule for rule in rules if rule.startswith("/quiz/") and "attempt" not in rule),
                         ["/quiz/s/<public_token>"])
        self.assertNotIn("quiz.quiz_start", web_app.app.view_functions)
        adapter = web_app.app.url_map.bind("localhost")
        for path in ("/quiz/1", f"/quiz/{self.assignment_id}", "/quiz/0", f"/quiz/{self.assignment_id}/",
                     "/quiz/01", "/quiz/-1", "/quiz/"):
            for method in ("GET", "POST", "HEAD", "PUT", "DELETE"):
                with self.subTest(path=path, method=method):
                    with self.assertRaises(NotFound):
                        adapter.match(path, method=method)

    def test_every_numeric_request_is_the_same_404_and_does_nothing(self):
        before = self.attempt_count()
        for label, account in self.principals:
            bodies = set()
            for kind, assignment_id in (("existing", self.assignment_id), ("without token", self.legacy_id),
                                        ("missing", MISSING_ID)):
                path = f"/quiz/{assignment_id}"
                for method in ("get", "post"):
                    with self.subTest(who=label, assignment=kind, method=method):
                        response = self.request(self.client(account), method, path)
                        self.assertEqual(response.status_code, 404)
                        self.assertNotIn("Location", response.headers)
                        body = response.get_data(as_text=True)
                        self.assertNotIn(self.public_token, body)
                        self.assertNotIn("/quiz/s/", body)
                        self.assertNotIn("Тест със скрит номер", body)
                        self.assertNotIn("Стар тест", body)
                        self.assertNotIn("/login", response.headers.get("Location", ""))
                        bodies.add(body.replace(path, "/quiz/N"))
            # Existing, token-less and missing assignments are indistinguishable.
            with self.subTest(who=label):
                self.assertEqual(len(bodies), 1)
        self.assertEqual(self.attempt_count(), before)

    def test_numeric_404_is_the_404_of_any_unknown_url(self):
        client = self.client(self.student)
        unknown = client.get("/no/such/page")
        numeric = client.get(f"/quiz/{self.assignment_id}")
        self.assertEqual((unknown.status_code, numeric.status_code), (404, 404))
        self.assertEqual(numeric.get_data(as_text=True).replace(f"/quiz/{self.assignment_id}", "PATH"),
                         unknown.get_data(as_text=True).replace("/no/such/page", "PATH"))

    def test_other_methods_and_url_variants_are_404_too(self):
        client = self.client(self.student)
        for path in (f"/quiz/{self.assignment_id}/", f"/quiz/0{self.assignment_id}",
                     f"/quiz/{self.assignment_id}?next=/quiz/s/{self.public_token}",
                     f"/quiz/{self.assignment_id}?public_token={self.public_token}"):
            for method in ("get", "post", "head", "put", "delete"):
                with self.subTest(path=path, method=method):
                    response = self.request(client, method, path)
                    self.assertEqual(response.status_code, 404)
                    self.assertNotIn("Location", response.headers)

    def test_post_starts_or_resumes_nothing_even_with_an_existing_attempt(self):
        client = self.client(self.student)
        attempt_id = self.start(client, f"/quiz/s/{self.public_token}")
        before = self.attempt_count()
        response = client.post(f"/quiz/{self.assignment_id}", headers=SAME_ORIGIN)
        self.assertEqual(response.status_code, 404)
        self.assertNotIn(str(attempt_id), response.headers.get("Location", ""))
        self.assertEqual(self.attempt_count(), before)
        # Anonymous: no sign-in redirect either.
        anonymous = self.client().post(f"/quiz/{self.assignment_id}", headers=SAME_ORIGIN)
        self.assertEqual(anonymous.status_code, 404)
        self.assertNotIn("Location", anonymous.headers)

    def test_cross_origin_post_is_refused_by_request_integrity_first(self):
        for assignment_id in (self.assignment_id, MISSING_ID):
            with self.subTest(assignment_id=assignment_id):
                response = self.client(self.student).post(f"/quiz/{assignment_id}", headers=CROSS_ORIGIN)
                self.assertEqual(response.status_code, 403)
                self.assertNotIn("Location", response.headers)

    def test_login_never_sends_anyone_to_a_working_numeric_entry(self):
        numeric = f"/quiz/{self.assignment_id}"
        response = self.client(self.student).get(f"/login?next={numeric}")
        # A same-site path is still a valid return target; it just leads to the 404.
        self.assertEqual(response.headers["Location"], numeric)
        self.assertEqual(self.client(self.student).get(numeric).status_code, 404)


class TokenEntryAndNumericManagementStillWorkTest(PublicTokenAppTestCase):
    def setUp(self):
        self.teacher = self.account("teacher", display_name="Учител Собственик")
        self.other_teacher = self.account("teacher", display_name="Друг Учител")
        self.admin = self.account("admin", display_name="Админ")
        self.student = self.account(display_name="Ученик Първи")
        self.other_student = self.account(display_name="Ученик Втори")
        self.assignment_id = self.create(self.teacher, title="Тест по токен")
        self.path = f"/quiz/s/{self.token(self.assignment_id)}"

    def test_token_entry_serves_starts_and_requires_an_account(self):
        page = self.client().get(self.path)
        self.assertEqual(page.status_code, 200)
        self.assertIn("Тест по токен", page.get_data(as_text=True))
        self.assertIn(f'href="/login?next={self.path}"', page.get_data(as_text=True))

        before = self.attempt_count()
        anonymous = self.client().post(self.path, headers=SAME_ORIGIN)
        self.assertEqual(anonymous.headers["Location"], f"/login?next={self.path}")
        self.assertEqual(self.attempt_count(), before)

        attempts = {}
        for label, account in (("student", self.student), ("teacher", self.teacher), ("admin", self.admin)):
            with self.subTest(role=label):
                attempt_id = self.start(self.client(account), self.path)
                self.assertEqual(self.start(self.client(account), self.path), attempt_id)  # resumed
                self.assertEqual(
                    self.query("SELECT user_id FROM quiz_attempts WHERE id = ?", (attempt_id,))[0][0], account["id"])
                attempts[label] = attempt_id
        self.assertEqual(len(set(attempts.values())), 3)
        for label, account in (("student", self.student), ("teacher", self.teacher), ("admin", self.admin)):
            for owner, attempt_id in attempts.items():
                with self.subTest(who=label, attempt_of=owner):
                    expected = 200 if owner == label else 404
                    self.assertEqual(self.client(account).get(f"/quiz/attempt/{attempt_id}").status_code, expected)

    def test_unknown_malformed_and_unbackfilled_tokens_are_404(self):
        legacy_id = self.legacy(owner=self.teacher)
        self.assertIsNone(self.token(legacy_id))
        for candidate in ("Z" * 43, "short", str(self.assignment_id), str(legacy_id), "None", "null"):
            for account in (None, self.student, self.admin):
                with self.subTest(candidate=candidate, signed_in=bool(account)):
                    client = self.client(account)
                    self.assertEqual(client.get(f"/quiz/s/{candidate}").status_code, 404)
                    self.assertEqual(client.post(f"/quiz/s/{candidate}", headers=SAME_ORIGIN).status_code, 404)
        self.assertEqual(self.attempts_of(legacy_id), [])

    def test_numeric_attempt_result_and_my_attempts_routes_work_for_the_owner_only(self):
        client = self.client(self.student)
        attempt_id = self.start(client, self.path)
        self.assertEqual(client.get(f"/quiz/attempt/{attempt_id}").status_code, 200)
        self.assertEqual(self.submit(client, attempt_id).status_code, 302)
        result = client.get(f"/quiz/attempt/{attempt_id}/result")
        self.assertEqual(result.status_code, 200)
        self.assertIn(f'href="{self.path}">Нов опит</a>', result.get_data(as_text=True))
        mine = client.get("/account/attempts")
        self.assertEqual(mine.status_code, 200)
        self.assertIn(f'href="/quiz/attempt/{attempt_id}/result"', mine.get_data(as_text=True))
        for label, account in (("anonymous", None), ("other student", self.other_student),
                               ("owning teacher", self.teacher), ("admin", self.admin)):
            with self.subTest(who=label):
                other = self.client(account)
                self.assertEqual(other.get(f"/quiz/attempt/{attempt_id}").status_code, 404)
                self.assertEqual(other.get(f"/quiz/attempt/{attempt_id}/result").status_code, 404)

    def test_historical_attempt_stays_bound_to_its_browser_marker(self):
        with closing(web_app.quiz_db()) as conn:
            question_ids = [int(q) for q in quiz.quiz_section_question_ids(conn, self.section_id)[:2]]
            historical = runtime_fixtures.add_attempt(
                conn, self.assignment_id, student_name="Исторически", question_ids=question_ids)
            conn.commit()
        holder = self.client()
        with holder.session_transaction() as session:
            session[web_app.quiz_attempt_owner_session_key(historical)] = True
        self.assertEqual(holder.get(f"/quiz/attempt/{historical}").status_code, 200)
        for label, account in (("anonymous", None), ("student", self.student), ("admin", self.admin)):
            with self.subTest(who=label):
                self.assertEqual(self.client(account).get(f"/quiz/attempt/{historical}").status_code, 404)
        # Entering by token never claims it: the account gets an attempt of its own.
        own = self.start(self.client(self.student), self.path)
        self.assertNotEqual(own, historical)
        self.assertIsNone(self.query("SELECT user_id FROM quiz_attempts WHERE id = ?", (historical,))[0][0])

    def test_numeric_teacher_management_routes_keep_their_owner_and_admin_policy(self):
        base = f"/teacher/assignment/{self.assignment_id}"
        self.start(self.client(self.student), self.path)
        for label, account, allowed in (("owning teacher", self.teacher, True), ("admin", self.admin, True),
                                        ("other teacher", self.other_teacher, False)):
            client = self.client(account)
            for suffix in ("", "/results", "/results.csv"):
                with self.subTest(who=label, route=suffix or "detail"):
                    self.assertEqual(client.get(base + suffix).status_code, 200 if allowed else 404)
            with self.subTest(who=label, route="edit"):
                edit = client.post(base + "/edit", data={**NO_DEADLINE_FORM, "title_bg": "Тест по токен",
                                                         "time_limit_minutes": ""},
                                   headers=SAME_ORIGIN)
                self.assertEqual(edit.status_code, 302 if allowed else 404)
            with self.subTest(who=label, route="duplicate"):
                duplicate = client.post(base + "/duplicate", data=NO_DEADLINE_FORM, headers=SAME_ORIGIN)
                self.assertEqual(duplicate.status_code, 302 if allowed else 404)
                if allowed:
                    self.assertRegex(duplicate.headers["Location"], r"^/teacher/assignment/\d+$")
        self.assertEqual(self.client(self.student).get(base).status_code, 403)
        self.assertEqual(self.client().get(base).headers["Location"], f"/login?next={base}")
        self.assertEqual(self.client(self.teacher).get("/teacher/assignments").status_code, 200)
        self.assertEqual(self.token(self.assignment_id), self.path.rsplit("/", 1)[1])  # edit kept the token


class StudentEntryPathHelperTest(PublicTokenAppTestCase):
    """tests.support.student_entry_path only reads: it never gives an assignment a token."""

    def test_it_returns_the_stored_token_and_writes_nothing(self):
        assignment_id = self.create(self.account("teacher"))
        before = self.query("SELECT * FROM quiz_assignments ORDER BY id")
        self.assertEqual(student_entry_path(assignment_id), f"/quiz/s/{self.token(assignment_id)}")
        self.assertEqual([tuple(r) for r in self.query("SELECT * FROM quiz_assignments ORDER BY id")],
                         [tuple(r) for r in before])

    def test_it_fails_for_a_missing_or_invalid_token_instead_of_generating_one(self):
        legacy_id = self.legacy()
        with self.assertRaisesRegex(AssertionError, "no valid public token"):
            student_entry_path(legacy_id)
        self.assertIsNone(self.token(legacy_id))
        with closing(web_app.quiz_db()) as conn:
            conn.execute("UPDATE quiz_assignments SET public_token = 'not-a-token' WHERE id = ?", (legacy_id,))
            conn.commit()
        with self.assertRaisesRegex(AssertionError, "no valid public token"):
            student_entry_path(legacy_id)
        self.assertEqual(self.token(legacy_id), "not-a-token")
        with self.assertRaisesRegex(AssertionError, "no assignment"):
            student_entry_path(MISSING_ID)


class NoNumericStudentEntryProducerTest(unittest.TestCase):
    """Nothing in the application still builds or routes a numeric student link."""

    def sources(self, *patterns):
        for pattern in patterns:
            for path in sorted((ROOT / "web").glob(pattern)):
                yield path.relative_to(ROOT).as_posix(), path.read_text(encoding="utf-8")

    def test_no_code_template_or_script_builds_a_numeric_entry_url(self):
        forbidden = (
            r"quiz\.quiz_start['\"]",                 # url_for of the removed endpoint
            r"/quiz/<int:assignment_id>",             # the removed rule
            r"/quiz/\{\{",                            # Jinja: /quiz/{{ id }}
            r"/quiz/\{(?!\{)",                        # f-string or JS template: /quiz/{id}
            r"[\"']/quiz/[\"']\s*\+",                 # concatenation: "/quiz/" + id
            r"/quiz/%[sd]",                           # %-formatting
        )
        for name, text in self.sources("**/*.py", "templates/**/*.html", "static/**/*.js"):
            code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
            for pattern in forbidden:
                with self.subTest(source=name, pattern=pattern):
                    self.assertIsNone(re.search(pattern, code))

    def test_student_links_have_one_producer_and_it_uses_the_token(self):
        producers = [name for name, text in self.sources("**/*.py") if '"quiz.quiz_start_public"' in text]
        self.assertEqual(producers, ["web/services/quiz.py"])
        service = (ROOT / "web" / "services" / "quiz.py").read_text(encoding="utf-8")
        self.assertEqual(service.count('"quiz.quiz_start_public"'), 1)
        self.assertIn('_quiz_url_for("quiz.quiz_start_public", public_token=public_token)', service)
        users = [name for name, text in self.sources("templates/**/*.html") if "student_quiz_path(" in text]
        self.assertEqual(users, ["web/templates/quiz_result.html", "web/templates/teacher_assignment.html",
                                 "web/templates/teacher_assignments.html"])

    def test_token_generation_and_backfill_are_untouched(self):
        from web import assignment_tokens

        self.assertEqual((assignment_tokens.PUBLIC_TOKEN_BYTES, assignment_tokens.PUBLIC_TOKEN_LENGTH), (32, 43))
        self.assertRegex(assignment_tokens.new_public_token(), r"^[A-Za-z0-9_-]{43}$")
        migrations = sorted(path.name for path in (ROOT / "web" / "migrations").glob("*.sql"))
        self.assertIn("017_assignment_public_token.sql", migrations)
        self.assertTrue((ROOT / "src" / "manage_assignment_tokens.py").is_file())

    def test_nginx_still_limits_the_token_entry(self):
        conf = (ROOT / "deploy" / "nginx" / "learnpilot.conf").read_text(encoding="utf-8")
        self.assertRegex(
            conf,
            r"location ~ \^/quiz/s/\[A-Za-z0-9_-\]\+\$ \{\s+limit_req zone=lp_quiz_write burst=90 nodelay;")


if __name__ == "__main__":
    unittest.main()
