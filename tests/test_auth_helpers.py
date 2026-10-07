"""Accounts PR 5 — the account sign-in helpers produce real, production-shaped sessions."""

import sqlite3
import time
from contextlib import closing
from pathlib import Path

from flask import g, session

from tests.auth_helpers import sign_in_admin, sign_in_student, sign_in_teacher
from tests.support import ROOT, IsolatedAppTestCase, web_app

ROLES = (
    (sign_in_student, "student", False, False),
    (sign_in_teacher, "teacher", True, False),
    (sign_in_admin, "admin", True, True),
)
AUTHORITY_KEYS = ("role", "display_name", "is_teacher", "is_admin",
                  "tester_authenticated", "admin_authenticated")


class AccountSignInHelpersTest(IsolatedAppTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.db_path = web_app.app.config["DB_PATH"]

    def setUp(self):
        self.client = web_app.app.test_client()

    def test_helpers_write_to_the_temporary_database_not_the_runtime_one(self):
        runtime = (ROOT / "data" / "questions.db").resolve()
        self.assertNotEqual(Path(self.db_path).resolve(), runtime)
        account = sign_in_teacher(self.client, self.db_path)
        with closing(sqlite3.connect(self.db_path)) as conn:
            self.assertEqual(
                conn.execute("SELECT role FROM users WHERE id = ?", (account["id"],)).fetchone()[0],
                "teacher")

    def test_session_has_only_the_production_identity_keys(self):
        for sign_in, *_ in ROLES:
            with self.subTest(helper=sign_in.__name__):
                client = web_app.app.test_client()
                account = sign_in(client, self.db_path)
                with client.session_transaction() as sess:
                    self.assertEqual(set(sess), {"uid", "sk", "iat"})
                    self.assertEqual(sess["uid"], account["id"])
                    self.assertEqual(sess["sk"], account["session_key"])
                    self.assertIsInstance(sess["iat"], int)
                    self.assertLess(abs(sess["iat"] - time.time()), 60)

    def test_role_comes_from_the_database_through_the_request_principal(self):
        for sign_in, role, is_teacher, is_admin in ROLES:
            with self.subTest(role=role):
                client = web_app.app.test_client()
                account = sign_in(client, self.db_path)
                with client:
                    client.get("/")
                    principal = g.principal
                    self.assertEqual(principal.user_id, account["id"])
                    self.assertEqual(
                        (principal.role, principal.is_teacher, principal.is_admin),
                        (role, is_teacher, is_admin))
                    self.assertFalse(any(key in session for key in AUTHORITY_KEYS))

    def test_inactive_and_rotated_accounts_are_respected_by_request_loading(self):
        for change in ("UPDATE users SET is_active = 0 WHERE id = ?",
                       "UPDATE users SET session_key = 'rrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrr' WHERE id = ?"):
            with self.subTest(change=change):
                client = web_app.app.test_client()
                account = sign_in_admin(client, self.db_path)
                with closing(sqlite3.connect(self.db_path)) as conn:
                    conn.execute(change, (account["id"],))
                    conn.commit()
                with client:
                    self.assertEqual(client.get("/teacher").status_code, 302)
                    self.assertIsNone(g.principal.user_id)
