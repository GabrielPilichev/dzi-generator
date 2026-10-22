"""Accounts PR 3 — the principal seam (account-only since PR10).

A request whose session carries an account identity resolves to an
account-backed ``Principal`` loaded from the database; every other request is
anonymous. A stale account identity fails closed to anonymous. The session keys
of the removed shared-password bridge (``tester_authenticated``,
``admin_authenticated``, ``ui_profile``) are injected here only to prove that
nothing reads them: they never create, raise or replace a principal.
"""

import ast
import sqlite3
import time
import unittest
from contextlib import closing, contextmanager
from pathlib import Path
from unittest.mock import patch

from tests.auth_helpers import create_account, login_as, set_account_session
from tests.support import ROOT, IsolatedAppTestCase, web_app
from web import access
from web import db as web_db
from web.access import ANONYMOUS, Principal, account_principal
from web.services import accounts


class AccountPrincipalUnitTest(unittest.TestCase):
    def test_role_capabilities(self):
        self.assertEqual(account_principal(1, "student", "С"), Principal(False, False, 1, "student", "С"))
        self.assertEqual(account_principal(2, "teacher", "У"), Principal(True, False, 2, "teacher", "У"))
        self.assertEqual(account_principal(3, "admin", "А"), Principal(True, True, 3, "admin", "А"))

    def test_unknown_role_has_no_capability(self):
        self.assertEqual(account_principal(1, "superuser", "X"), ANONYMOUS)
        self.assertEqual(account_principal(1, None, "X"), ANONYMOUS)

    def test_anonymous_has_no_capability_and_no_account(self):
        self.assertEqual(ANONYMOUS, Principal(is_teacher=False, is_admin=False))
        self.assertEqual((ANONYMOUS.user_id, ANONYMOUS.role, ANONYMOUS.display_name), (None, None, None))

    def test_the_session_fallback_and_its_switches_are_gone(self):
        for name in ("principal_from_session", "legacy_shared_login_enabled", "parse_legacy_shared_login",
                     "legacy_shared_login_route", "is_legacy_shared_login_view", "denial_login_endpoint",
                     "LEGACY_SHARED_LOGIN_ENV", "LEGACY_SHARED_LOGIN_CONFIG", "LEGACY_AUTH_SESSION_KEYS",
                     "PROFILE_SWITCH"):
            with self.subTest(name=name):
                self.assertFalse(hasattr(access, name))
        self.assertNotIn("profile_switch", access.ACCESS_LEVELS)

    def test_access_module_never_reads_the_session(self):
        tree = ast.parse(Path(ROOT, "web/access.py").read_text(encoding="utf-8"))
        names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
        self.assertNotIn("session", names)
        self.assertNotIn("current_app", names)

    def test_current_principal_is_anonymous_without_a_loaded_account(self):
        # No hook has run: whatever the cookie holds, nothing but g.principal is read.
        from flask import session

        for state in ({}, {"tester_authenticated": True}, {"admin_authenticated": True},
                      {"ui_profile": "admin"}, {"uid": 1, "sk": "x", "iat": 1},
                      {"tester_authenticated": True, "admin_authenticated": True, "ui_profile": "admin"}):
            with self.subTest(state=state), web_app.app.test_request_context("/teacher"):
                session.update(state)
                self.assertEqual(access.current_principal(), ANONYMOUS)
                self.assertEqual(dict(session), state)


class LoadSessionUserServiceTest(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript((ROOT / "web" / "migrations" / "012_users.sql").read_text(encoding="utf-8"))
        self.addCleanup(self.conn.close)
        self.uid = accounts.create_user(self.conn, display_name="Ана Иванова", role="teacher")
        self.key = self.conn.execute("SELECT session_key FROM users WHERE id=?", (self.uid,)).fetchone()[0]

    def test_valid_identity_returns_the_user(self):
        user = accounts.load_session_user(self.conn, self.uid, self.key)
        self.assertEqual((user["id"], user["role"], user["display_name"]), (self.uid, "teacher", "Ана Иванова"))
        self.assertNotIn("password_hash", user)

    def test_rejected_identities(self):
        cases = {
            "wrong key": (self.uid, self.key + "x"),
            "empty key": (self.uid, ""),
            "missing key": (self.uid, None),
            "bytes key": (self.uid, self.key.encode()),
            "non-ascii key": (self.uid, "ключ"),
            "unknown user": (self.uid + 100, self.key),
            "string uid": (str(self.uid), self.key),
            "bool uid": (True, self.key),
            "float uid": (float(self.uid), self.key),
            "none uid": (None, self.key),
            "zero uid": (0, self.key),
            "negative uid": (-1, self.key),
            "overflow uid": (2**70, self.key),
        }
        for name, (uid, key) in cases.items():
            with self.subTest(name):
                self.assertIsNone(accounts.load_session_user(self.conn, uid, key))

    def test_disabled_user_is_rejected_and_rotated_key_invalidates(self):
        new_key = accounts.set_active(self.conn, self.uid, False)
        self.assertIsNone(accounts.load_session_user(self.conn, self.uid, new_key))
        accounts.set_active(self.conn, self.uid, True)
        self.assertIsNone(accounts.load_session_user(self.conn, self.uid, self.key))

    def test_key_is_compared_with_compare_digest(self):
        with patch.object(accounts.hmac, "compare_digest", wraps=accounts.hmac.compare_digest) as compare:
            self.assertIsNotNone(accounts.load_session_user(self.conn, self.uid, self.key))
            self.assertIsNone(accounts.load_session_user(self.conn, self.uid, "other"))
        self.assertEqual(compare.call_count, 2)

    def test_exactly_one_users_statement(self):
        statements = []
        self.conn.set_trace_callback(statements.append)
        accounts.load_session_user(self.conn, self.uid, self.key)
        self.conn.set_trace_callback(None)
        self.assertEqual(len(statements), 1)
        self.assertRegex(statements[0], r"FROM users WHERE id = \d+$")  # primary-key lookup

    def test_no_plain_equality_on_the_key(self):
        source = Path(ROOT, "web/services/accounts.py").read_text(encoding="utf-8")
        function = source.split("def load_session_user", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("compare_digest", function)
        self.assertNotIn("== session_key", function)
        self.assertNotIn("session_key ==", function)


class AccountBridgeTestCase(IsolatedAppTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.app = web_app.app
        cls.app.config.update(TESTING=True)
        cls.db_path = cls.app.config["DB_PATH"]

    def setUp(self):
        self.client = self.app.test_client()

    def principal_for(self, session_state, path="/"):
        """Run the app's before_request chain, then read the request principal."""
        with self.app.test_request_context(path):
            from flask import session

            session.update(session_state)
            response = self.app.preprocess_request()
            self.assertIsNone(response)
            return access.current_principal(), dict(session)

    def account_state(self, account, **extra):
        return {"uid": account["id"], "sk": account["session_key"], "iat": int(time.time()), **extra}

    @contextmanager
    def users_statements(self):
        """Capture SQL run on request connections that touches the users table."""
        statements = []
        connect = web_db.connect_db

        def traced(*args, **kwargs):
            conn = connect(*args, **kwargs)
            conn.set_trace_callback(statements.append)
            return conn

        with patch.object(web_db, "connect_db", traced):
            yield statements

    @staticmethod
    def user_lookups(statements):
        return [sql for sql in statements if "FROM users" in sql]


class AccountPrincipalTest(AccountBridgeTestCase):
    def test_each_role_resolves_to_its_principal_from_the_database(self):
        for role, teacher, admin in (("student", False, False), ("teacher", True, False), ("admin", True, True)):
            with self.subTest(role=role):
                account = create_account(self.db_path, role, display_name=f"Име {role}")
                principal, _ = self.principal_for(self.account_state(account))
                self.assertEqual(
                    principal,
                    Principal(teacher, admin, account["id"], role, f"Име {role}"),
                )

    def test_role_and_name_come_from_the_database_not_the_cookie(self):
        account = create_account(self.db_path, "student")
        state = self.account_state(account, role="admin", display_name="Фалшив", is_admin=True)
        principal, _ = self.principal_for(state)
        self.assertEqual((principal.role, principal.is_admin, principal.display_name),
                         ("student", False, "Тест Потребител"))

    def test_role_change_applies_on_the_next_request(self):
        account = create_account(self.db_path, "student")
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute("UPDATE users SET role='teacher' WHERE id=?", (account["id"],))
            conn.commit()
        principal, _ = self.principal_for(self.account_state(account))
        self.assertEqual((principal.role, principal.is_teacher), ("teacher", True))

    def test_iat_is_required_and_enforced_since_pr_4(self):
        # The lifetime rules themselves are pinned in tests/test_account_login.py.
        account = create_account(self.db_path, "teacher")
        for iat in (None, 0, 1, 10**12):
            with self.subTest(iat=iat):
                state = {"uid": account["id"], "sk": account["session_key"]}
                if iat is not None:
                    state["iat"] = iat
                principal, session_after = self.principal_for(state)
                self.assertEqual(principal, ANONYMOUS)
                self.assertNotIn("uid", session_after)

    def test_valid_account_session_does_not_change_the_cookie(self):
        account = login_as(self.client, self.db_path, "teacher")
        response = self.client.get("/teacher/new")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("Set-Cookie", response.headers)
        with self.client.session_transaction() as sess:
            self.assertEqual(sess["uid"], account["id"])


class AccountSessionValidationTest(AccountBridgeTestCase):
    def assert_dropped(self, state):
        principal, session_after = self.principal_for(state)
        self.assertEqual(principal, ANONYMOUS)
        for key in ("uid", "sk", "iat"):
            self.assertNotIn(key, session_after)
        # Whatever else the cookie holds is not an identity on the next request either.
        self.assertEqual(self.principal_for(session_after)[0], ANONYMOUS)
        return session_after

    def test_nonexistent_user(self):
        account = create_account(self.db_path)
        self.assert_dropped({"uid": account["id"] + 1000, "sk": account["session_key"], "iat": 1})

    def test_inactive_user(self):
        account = create_account(self.db_path, "admin", is_active=False)
        self.assert_dropped(self.account_state(account))

    def test_missing_sk(self):
        account = create_account(self.db_path)
        self.assert_dropped({"uid": account["id"], "iat": 1})

    def test_wrong_sk(self):
        account = create_account(self.db_path)
        self.assert_dropped({"uid": account["id"], "sk": "wrong", "iat": 1})

    def test_rotated_key_signs_the_old_session_out(self):
        account = create_account(self.db_path, "teacher")
        with closing(sqlite3.connect(self.db_path)) as conn:
            accounts.rotate_session_key(conn, account["id"])
            conn.commit()
        self.assert_dropped(self.account_state(account))

    def test_malformed_identities_are_anonymous_and_do_not_crash(self):
        for name, state in {
            "string uid": {"uid": "1", "sk": "x"},
            "list uid": {"uid": [1], "sk": "x"},
            "dict sk": {"uid": 1, "sk": {"a": 1}},
            "int sk": {"uid": 1, "sk": 5},
            "huge uid": {"uid": 2**80, "sk": "x"},
            "none uid": {"uid": None, "sk": "x"},
            "sk only": {"sk": "x"},
            "iat only": {"iat": 1},
        }.items():
            with self.subTest(name):
                self.assert_dropped({**state, "admin_authenticated": True})

    def test_unrelated_session_state_survives_a_drop(self):
        session_after = self.assert_dropped(
            {"uid": 99999, "sk": "x", "iat": 1, "admin_authenticated": True,
             "quiz_attempt_owner:7": "token", "ui_profile": "tester"}
        )
        self.assertEqual(session_after.get("quiz_attempt_owner:7"), "token")


class HistoricalSessionKeysTest(AccountBridgeTestCase):
    LEGACY_KEYS = (
        {"admin_authenticated": True},
        {"tester_authenticated": True},
        {"admin_authenticated": True, "tester_authenticated": True, "ui_profile": "admin"},
    )

    def test_valid_account_identity_is_unaffected_by_every_historical_key(self):
        for role in ("student", "teacher", "admin"):
            account = create_account(self.db_path, role)
            expected, _ = self.principal_for(self.account_state(account))
            for legacy in self.LEGACY_KEYS:
                with self.subTest(role=role, legacy=legacy):
                    principal, session_after = self.principal_for(self.account_state(account, **legacy))
                    self.assertEqual(principal, expected)
                    self.assertEqual(principal.role, role)
                    self.assertEqual(session_after["uid"], account["id"])

    def test_account_student_with_historical_admin_key_is_student(self):
        account = create_account(self.db_path, "student")
        principal, _ = self.principal_for(self.account_state(account, admin_authenticated=True))
        self.assertEqual((principal.is_teacher, principal.is_admin), (False, False))

    def test_account_teacher_with_historical_admin_key_stays_teacher(self):
        account = create_account(self.db_path, "teacher")
        principal, _ = self.principal_for(self.account_state(account, admin_authenticated=True))
        self.assertEqual((principal.is_teacher, principal.is_admin), (True, False))

    def test_account_admin_with_historical_tester_key_is_admin(self):
        account = create_account(self.db_path, "admin")
        principal, _ = self.principal_for(self.account_state(account, tester_authenticated=True))
        self.assertEqual((principal.is_teacher, principal.is_admin), (True, True))

    def test_historical_keys_without_an_account_identity_are_anonymous(self):
        for state in ({}, {"ui_profile": "admin"}, *self.LEGACY_KEYS):
            with self.subTest(state=state):
                principal, session_after = self.principal_for(state)
                self.assertEqual(principal, ANONYMOUS)
                # Not read, not rewritten: the request does not even touch the cookie.
                self.assertEqual(session_after, state)

    def test_historical_keys_open_no_protected_route(self):
        for state in ({"ui_profile": "admin"}, *self.LEGACY_KEYS):
            client = self.app.test_client()
            with client.session_transaction() as sess:
                sess.update(state)
            for path in ("/teacher", "/teacher/new", "/teacher/assignments", "/dzi",
                         "/admin/open-question-candidates", "/teacher/practical-submissions",
                         "/account/password"):
                with self.subTest(state=state, path=path):
                    response = client.get(path)
                    self.assertEqual(response.status_code, 302)
                    self.assertEqual(response.headers["Location"], f"/login?next={path}")
                    self.assertNotIn("Set-Cookie", response.headers)
            with self.subTest(state=state, request="POST"):
                response = client.post("/teacher/dzi-training", headers={"Origin": "http://localhost"})
                self.assertEqual(response.headers["Location"], "/login?next=/teacher/dzi-training")


class FailClosedTest(AccountBridgeTestCase):
    def test_stale_account_identity_never_falls_through_to_historical_keys(self):
        live = create_account(self.db_path, "student")
        disabled = create_account(self.db_path, "teacher", is_active=False)
        stale = {
            "deleted uid": {"uid": live["id"] + 5000, "sk": live["session_key"]},
            "wrong sk": {"uid": live["id"], "sk": "wrong"},
            "missing sk": {"uid": live["id"]},
            "disabled uid": {"uid": disabled["id"], "sk": disabled["session_key"]},
        }
        for name, identity in stale.items():
            for legacy in ({"admin_authenticated": True}, {"tester_authenticated": True},
                           {"admin_authenticated": True, "tester_authenticated": True}):
                with self.subTest(identity=name, legacy=legacy):
                    principal, _ = self.principal_for({**identity, "iat": 1, **legacy})
                    self.assertEqual(principal, ANONYMOUS)

    def test_dropped_cookie_does_not_become_an_admin_on_the_next_request(self):
        with self.client.session_transaction() as sess:
            sess.update({"uid": 987654, "sk": "stale", "iat": 1, "admin_authenticated": True,
                         "ui_profile": "admin"})
        for attempt in (1, 2):
            with self.subTest(attempt=attempt):
                response = self.client.get("/teacher")
                self.assertEqual(response.status_code, 302)
                self.assertEqual(response.headers["Location"], "/login?next=/teacher")
        with self.client.session_transaction() as sess:
            self.assertNotIn("uid", sess)

    def test_disabling_an_account_ends_its_admin_access(self):
        account = login_as(self.client, self.db_path, "admin")
        self.assertEqual(self.client.get("/teacher").status_code, 200)
        with closing(sqlite3.connect(self.db_path)) as conn:
            accounts.set_active(conn, account["id"], False)
            conn.commit()
        self.assertEqual(self.client.get("/teacher").status_code, 302)


class AccountRouteMatrixTest(AccountBridgeTestCase):
    def status(self, role, path):
        client = self.app.test_client()
        if role:
            login_as(client, self.db_path, role)
        return client.get(path).status_code

    def test_existing_policies_apply_to_account_principals(self):
        # (path, student, teacher, admin); anonymous is always a login redirect and a
        # signed-in account without the role is refused.
        for path, student, teacher, admin in (
            ("/", 200, 200, 200),
            ("/teacher/new", 403, 200, 200),
            ("/teacher", 403, 403, 200),
            ("/dzi", 403, 403, 200),
        ):
            for role, expected in (("student", student), ("teacher", teacher), ("admin", admin)):
                with self.subTest(path=path, role=role):
                    self.assertEqual(self.status(role, path), expected)
            with self.subTest(path=path, role="anonymous"):
                self.assertEqual(self.status(None, path), 200 if path == "/" else 302)

    def test_denied_account_principal_is_refused_not_redirected(self):
        client = self.app.test_client()
        login_as(client, self.db_path, "student")
        for path in ("/teacher/new", "/teacher"):
            with self.subTest(path=path):
                response = client.get(path)
                self.assertEqual(response.status_code, 403)
                self.assertNotIn("Location", response.headers)
                self.assertIn("Нямаш достъп до тази страница с този профил.",
                              response.get_data(as_text=True))

    def test_the_profile_switch_route_does_not_exist_for_any_role(self):
        for role in (None, "student", "teacher", "admin"):
            for profile in ("admin", "tester"):
                with self.subTest(role=role, profile=profile):
                    self.assertEqual(self.status(role, f"/profile/{profile}"), 404)


class AnonymousAndQueryCountTest(AccountBridgeTestCase):
    def test_anonymous_get_has_no_cookie_and_no_users_query(self):
        with self.users_statements() as statements:
            response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("Set-Cookie", response.headers)
        self.assertEqual(self.user_lookups(statements), [])

    def test_historical_session_keys_run_no_users_query(self):
        with self.client.session_transaction() as sess:
            sess.update({"tester_authenticated": True, "admin_authenticated": True, "ui_profile": "admin"})
        with self.users_statements() as statements:
            self.assertEqual(self.client.get("/teacher/new").status_code, 302)
            self.assertEqual(self.client.get("/").status_code, 200)
        self.assertEqual(self.user_lookups(statements), [])

    def test_account_session_runs_exactly_one_users_lookup(self):
        for role, path in (("student", "/"), ("teacher", "/teacher/new"), ("admin", "/teacher")):
            with self.subTest(role=role):
                client = self.app.test_client()
                login_as(client, self.db_path, role)
                with self.users_statements() as statements:
                    self.assertEqual(client.get(path).status_code, 200)
                lookups = self.user_lookups(statements)
                self.assertEqual(len(lookups), 1, lookups)
                self.assertRegex(lookups[0], r"FROM users WHERE id = \d+$")

    def test_stale_session_runs_one_lookup_and_malformed_uid_none(self):
        set_account_session(self.client, 424242, "stale")
        with self.users_statements() as statements:
            self.client.get("/")
        self.assertEqual(len(self.user_lookups(statements)), 1)

        client = self.app.test_client()
        with client.session_transaction() as sess:
            sess.update({"uid": "not-an-int", "sk": "x"})
        with self.users_statements() as statements:
            client.get("/")
        self.assertEqual(self.user_lookups(statements), [])  # a malformed uid never reaches SQL

    def test_health_and_static_skip_account_loading(self):
        account = create_account(self.db_path, "admin")
        set_account_session(self.client, account["id"], account["session_key"])
        static_file = next(Path(ROOT, "web", "static").rglob("*.css")).relative_to(Path(ROOT, "web", "static"))
        with self.users_statements() as statements:
            health = self.client.get("/healthz")
            static = self.client.get(f"/static/{static_file.as_posix()}")
        self.assertEqual((health.status_code, static.status_code), (200, 200))
        self.assertEqual(self.user_lookups(statements), [])

    def test_stale_session_on_healthz_gets_no_cookie_and_no_query(self):
        set_account_session(self.client, 424242, "stale")
        with self.users_statements() as statements:
            response = self.client.get("/healthz")
        self.assertNotIn("Set-Cookie", response.headers)
        self.assertEqual(self.user_lookups(statements), [])

    def test_account_loading_does_not_scale_with_page_content(self):
        account = login_as(self.client, self.db_path, "admin")
        with self.users_statements() as statements:
            for _ in range(3):
                self.client.get("/teacher/assignments")
        self.assertEqual(len(self.user_lookups(statements)), 3)  # one per request, none per row/template


class BridgeBoundariesTest(unittest.TestCase):
    @staticmethod
    def imports(relative):
        tree = ast.parse(Path(ROOT, relative).read_text(encoding="utf-8"))
        found = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                found.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                found.add(node.module or "")
        return found

    def test_access_module_stays_free_of_services_and_web_app(self):
        imported = self.imports("web/access.py")
        self.assertNotIn("web.app", imported)
        self.assertFalse({name for name in imported if name.startswith("web.services")})
        self.assertFalse({name for name in imported if name.startswith("web.blueprints")})

    def test_accounts_service_stays_independent_of_flask_and_the_app(self):
        imported = self.imports("web/services/accounts.py")
        self.assertFalse({name for name in imported if name.split(".")[0] == "flask" or name.startswith("web.app")
                          or name.startswith("web.blueprints")})

    def test_request_integrity_does_not_know_accounts(self):
        relative = "web/request_integrity.py"
        source = Path(ROOT, relative).read_text(encoding="utf-8")
        self.assertFalse({name for name in self.imports(relative) if "accounts" in name})
        self.assertNotIn('"uid"', source)

    def test_hook_order_loads_identity_between_integrity_and_authorization(self):
        hooks = [func.__name__ for func in web_app.app.before_request_funcs[None]]
        self.assertEqual(hooks.count("load_current_user"), 1)
        self.assertLess(hooks.index("protect_same_origin_unsafe_methods"), hooks.index("load_current_user"))
        self.assertLess(hooks.index("load_current_user"), hooks.index("enforce_access_policy"))

    def test_account_routes_exist_and_no_registration_route_does(self):
        paths = {rule.rule for rule in web_app.app.url_map.iter_rules()}
        for expected in ("/login", "/logout", "/account/password"):
            self.assertIn(expected, paths)
        for forbidden in ("/signup", "/account/reset", "/oauth"):
            self.assertNotIn(forbidden, paths)
        # Registration (PR 4) is its own blueprint; none of its routes signs anyone in.
        self.assertEqual({rule.endpoint for rule in web_app.app.url_map.iter_rules()
                          if rule.rule.startswith("/register")},
                         {"registration.choose", "registration.student", "registration.teacher",
                          "registration.verify", "registration.resend", "registration.cancel"})
        # Password recovery (scope 8.3) is its own blueprint too; none of its routes signs anyone in.
        self.assertEqual({rule.endpoint for rule in web_app.app.url_map.iter_rules()
                          if rule.rule.startswith("/recover")},
                         {"recovery.start", "recovery.verify", "recovery.resend", "recovery.cancel"})


if __name__ == "__main__":
    unittest.main()
