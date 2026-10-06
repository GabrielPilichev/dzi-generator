import sqlite3
import subprocess
import sys
import unittest
from pathlib import Path

from werkzeug.security import check_password_hash

from web.services import accounts

ROOT = Path(__file__).resolve().parents[1]
GOOD = "correct horse"


class AccountsTestCase(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.executescript((ROOT / "web" / "migrations" / "012_users.sql").read_text(encoding="utf-8"))
        self.addCleanup(self.conn.close)

    def stored(self, user_id, column):
        return self.conn.execute(f"SELECT {column} FROM users WHERE id = ?", (user_id,)).fetchone()[0]

    def make(self, **kw):
        kw.setdefault("display_name", "Ана Иванова")
        kw.setdefault("email", "ana@example.bg")
        kw.setdefault("password", GOOD)
        return accounts.create_user(self.conn, **kw)


class NormalizeEmailTest(unittest.TestCase):
    def test_trim_lowercase_nfkc(self):
        self.assertEqual(accounts.normalize_email("  Ana@Example.BG "), "ana@example.bg")
        self.assertEqual(accounts.normalize_email("ａna@example.bg"), "ana@example.bg")  # fullwidth a

    def test_no_provider_folding(self):
        self.assertEqual(accounts.normalize_email("A.Na+tag@Gmail.com"), "a.na+tag@gmail.com")

    def test_rejects_malformed(self):
        for bad in ("", "   ", "plain", "a@@b.bg", "a@b@c.bg", "@b.bg", "a@", "a b@c.bg",
                    "a@b .bg", "a\t@b.bg", "a\x00@b.bg", "a​@b.bg", "x" * 250 + "@b.bg", None):
            with self.subTest(email=bad), self.assertRaises(ValueError):
                accounts.normalize_email(bad)

    def test_length_boundary(self):
        email = "a" * 248 + "@b.bg"
        self.assertEqual(len(email), 253)
        self.assertEqual(accounts.normalize_email(email + "c"), email + "c")
        with self.assertRaises(ValueError):
            accounts.normalize_email(email + "cc")


class PasswordProblemTest(unittest.TestCase):
    def test_boundaries(self):
        self.assertIsNotNone(accounts.password_problem("x" * 9))
        self.assertIsNone(accounts.password_problem("x" * 10))
        self.assertIsNone(accounts.password_problem("x" * 256))
        self.assertIsNotNone(accounts.password_problem("x" * 257))

    def test_equal_to_email_rejected(self):
        self.assertIsNotNone(accounts.password_problem("ana@example.bg", "Ana@Example.bg"))
        self.assertIsNone(accounts.password_problem("ana@example.bg", "other@example.bg"))

    def test_no_composition_rules(self):
        self.assertIsNone(accounts.password_problem("aaaaaaaaaa"))


class CreateUserTest(AccountsTestCase):
    def test_defaults_and_session_key(self):
        uid = self.make()
        user = accounts.get_user_by_id(self.conn, uid)
        self.assertEqual((user["role"], user["is_active"], user["has_password"]), ("student", 1, 1))
        self.assertGreaterEqual(len(user["session_key"]), 32)
        self.assertIsNotNone(user["password_changed_at"])
        self.assertNotIn("password_hash", user)
        self.assertNotEqual(user["session_key"], accounts.get_user_by_id(self.conn, self.make(email="b@example.bg"))["session_key"])

    def test_password_hashed_with_scrypt(self):
        uid = self.make()
        stored = self.stored(uid, "password_hash")
        self.assertNotIn(GOOD, stored)
        self.assertTrue(stored.startswith("scrypt:"))
        self.assertTrue(check_password_hash(stored, GOOD))

    def test_explicit_role_and_inactive(self):
        uid = self.make(role="teacher", is_active=False)
        user = accounts.get_user_by_id(self.conn, uid)
        self.assertEqual((user["role"], user["is_active"]), ("teacher", 0))

    def test_email_normalized_and_duplicate_rejected(self):
        uid = self.make(email=" Ana@Example.BG ")
        self.assertEqual(self.stored(uid, "email"), "ana@example.bg")
        with self.assertRaises(sqlite3.IntegrityError):
            self.make(email="ANA@example.bg")

    def test_invalid_input_rejected(self):
        for kw in ({"role": "owner"}, {"display_name": "а"}, {"display_name": "x" * 81},
                   {"password": "short"}, {"email": "bad"}, {"email": None}):
            with self.subTest(kw=kw), self.assertRaises(ValueError):
                self.make(**kw)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM users").fetchone()[0], 0)

    def test_passwordless_user(self):
        uid = self.make(password=None, email=None)
        user = accounts.get_user_by_id(self.conn, uid)
        self.assertEqual((user["email"], user["has_password"], user["password_changed_at"]), (None, 0, None))
        self.assertFalse(accounts.verify_password(self.conn, uid, GOOD))

    def test_caller_controls_commit(self):
        self.conn.commit()
        self.make()
        self.assertTrue(self.conn.in_transaction)
        self.conn.rollback()
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM users").fetchone()[0], 0)


class LookupTest(AccountsTestCase):
    def test_lookups(self):
        uid = self.make()
        self.assertEqual(accounts.get_user_by_id(self.conn, uid)["email"], "ana@example.bg")
        self.assertEqual(accounts.get_user_by_email(self.conn, " ANA@example.bg")["id"], uid)

    def test_missing(self):
        self.assertIsNone(accounts.get_user_by_id(self.conn, 99))
        self.assertIsNone(accounts.get_user_by_email(self.conn, "none@example.bg"))
        self.assertIsNone(accounts.get_user_by_email(self.conn, "not an email"))


class VerifyPasswordTest(AccountsTestCase):
    def test_verify(self):
        uid = self.make()
        self.assertTrue(accounts.verify_password(self.conn, uid, GOOD))
        self.assertFalse(accounts.verify_password(self.conn, uid, "wrong password"))
        self.assertFalse(accounts.verify_password(self.conn, 999, GOOD))


class MutationTest(AccountsTestCase):
    def setUp(self):
        super().setUp()
        self.uid = self.make()
        self.conn.execute("UPDATE users SET password_changed_at = '2000-01-01 00:00:00', "
                          "updated_at = '2000-01-01 00:00:00'")
        self.old_key = self.stored(self.uid, "session_key")

    def test_set_password(self):
        key = accounts.set_password(self.conn, self.uid, "another long pass")
        self.assertEqual(key, self.stored(self.uid, "session_key"))
        self.assertNotEqual(key, self.old_key)
        self.assertNotEqual(self.stored(self.uid, "password_changed_at"), "2000-01-01 00:00:00")
        self.assertTrue(accounts.verify_password(self.conn, self.uid, "another long pass"))
        self.assertFalse(accounts.verify_password(self.conn, self.uid, GOOD))

    def test_set_password_rejects_bad_input(self):
        with self.assertRaises(ValueError):
            accounts.set_password(self.conn, self.uid, "short")
        with self.assertRaises(ValueError):
            accounts.set_password(self.conn, self.uid, "ana@example.bg")
        with self.assertRaises(LookupError):
            accounts.set_password(self.conn, 999, "another long pass")
        noemail = self.make(email=None, password=None)
        with self.assertRaises(ValueError):
            accounts.set_password(self.conn, noemail, "another long pass")
        self.assertEqual(self.stored(self.uid, "session_key"), self.old_key)

    def test_set_role(self):
        key = accounts.set_role(self.conn, self.uid, "admin")
        self.assertEqual(self.stored(self.uid, "role"), "admin")
        self.assertNotEqual(key, self.old_key)
        with self.assertRaises(ValueError):
            accounts.set_role(self.conn, self.uid, "root")
        self.assertEqual(self.stored(self.uid, "role"), "admin")

    def test_set_active(self):
        key = accounts.set_active(self.conn, self.uid, False)
        self.assertEqual(self.stored(self.uid, "is_active"), 0)
        self.assertNotEqual(key, self.old_key)
        accounts.set_active(self.conn, self.uid, True)
        self.assertEqual(self.stored(self.uid, "is_active"), 1)
        for bad in (2, "yes", None):
            with self.subTest(value=bad), self.assertRaises(ValueError):
                accounts.set_active(self.conn, self.uid, bad)

    def test_rotate_session_key(self):
        key = accounts.rotate_session_key(self.conn, self.uid)
        self.assertNotEqual(key, self.old_key)
        self.assertEqual(self.stored(self.uid, "session_key"), key)
        self.assertNotEqual(self.stored(self.uid, "updated_at"), "2000-01-01 00:00:00")
        with self.assertRaises(LookupError):
            accounts.rotate_session_key(self.conn, 999)


class IndependenceTest(unittest.TestCase):
    def test_imports_neither_app_nor_blueprints(self):
        code = (
            "import sys; import web.services.accounts; "
            "bad = [m for m in sys.modules if m == 'web.app' or m.startswith('web.blueprints') or m == 'flask']; "
            "print(bad)"
        )
        out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, check=True)
        self.assertEqual(out.stdout.strip(), "[]")


if __name__ == "__main__":
    unittest.main()
