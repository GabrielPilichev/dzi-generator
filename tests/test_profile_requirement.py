"""Registration PR 6, services: ``profiles.needs_profile``, the mark, ``display_name`` and the profile form.

Test plan I5, I6, J6, J10, J11, J12 at service level. Every database is a temporary clone
of the migrated canonical template (``tests.support.build_test_database``); none is
``data/questions.db``. The services never commit, so each test commits itself.
"""

import itertools
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from tests.support import build_test_database
from web.services import accounts, profiles
from web.services import registration_forms as forms

NOW = datetime(2026, 10, 15, 12, 0, 0, tzinfo=timezone.utc)
LATER = NOW + timedelta(days=30)
MARK = "2026-10-15 09:00:00"
SCHOOL = "СУ „Васил Левски“, Пловдив"
IT = {"subject_id": "it", "label": "Информационни технологии"}
USER_COLUMNS = ("id", "email", "display_name", "role", "password_hash", "is_active", "session_key",
                "password_changed_at", "last_login_at", "created_at", "updated_at", "email_verified_at",
                "profile_required_at")
_emails = itertools.count(1)


class ServiceTestCase(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="profile-requirement-")
        self.addCleanup(directory.cleanup)
        self.path = build_test_database(Path(directory.name) / "profiles.db")
        self.conn = sqlite3.connect(self.path)
        self.addCleanup(self.conn.close)
        self.conn.execute("PRAGMA foreign_keys = ON")

    def account(self, role="student", *, marked=False, name="Ученик Тестов"):
        user_id = accounts.create_user(self.conn, display_name=name, role=role,
                                       email=f"user{next(_emails)}@example.bg", password="a-long-enough-password")
        if marked:
            self.conn.execute("UPDATE users SET profile_required_at = ? WHERE id = ?", (MARK, user_id))
        self.conn.commit()
        return user_id

    def give_profile(self, user_id, kind):
        if kind == "student":
            profiles.create_profile(self.conn, user_id, account_type="student", first_name="Ана",
                                    last_name="Иванова", grade=9, now=NOW)
        else:
            profiles.create_profile(self.conn, user_id, account_type="teacher", first_name="Иван",
                                    last_name="Петров", school_name=SCHOOL, subjects=[IT], now=NOW)
        self.conn.commit()

    def row(self, user_id):
        return self.conn.execute(f"SELECT {', '.join(USER_COLUMNS)} FROM users WHERE id = ?", (user_id,)).fetchone()

    def needs(self, user_id, *, enabled=True):
        return profiles.needs_profile(self.conn, {"id": user_id}, enabled=enabled)


class NeedsProfileTest(ServiceTestCase):
    def test_every_combination_is_true_only_for_the_one_that_qualifies(self):
        """Gate x role x mark x profile (none, student type, teacher type): 36 cases."""
        for enabled, role, marked, kind in itertools.product(
                (False, True), ("student", "teacher", "admin"), (False, True), (None, "student", "teacher")):
            with self.subTest(gate=enabled, role=role, marked=marked, profile=kind):
                user_id = self.account(role, marked=marked)
                if kind:
                    self.give_profile(user_id, kind)
                expected = enabled and role == "student" and marked and kind is None
                self.assertIs(self.needs(user_id, enabled=enabled), expected)

    def test_the_gate_must_be_exactly_true(self):
        user_id = self.account(marked=True)
        self.assertTrue(self.needs(user_id))
        for value in (False, None, 1, "yes", "True", [True]):
            with self.subTest(enabled=value):
                self.assertFalse(profiles.needs_profile(self.conn, {"id": user_id}, enabled=value))

    def test_a_malformed_or_unknown_account_never_needs_one(self):
        self.account(marked=True)
        for user in (None, {}, {"id": None}, {"id": "1"}, {"id": True}, {"id": 0}, {"id": -1}, {"id": 1.0},
                     {"id": 9999}, {"id": 2 ** 63}, "1", 1, [1]):
            with self.subTest(user=user):
                self.assertFalse(profiles.needs_profile(self.conn, user, enabled=True))

    def test_the_row_decides_not_the_dict_that_was_passed(self):
        marked_student = self.account(marked=True)
        unmarked_teacher = self.account("teacher")
        stale_unmarked = {"id": marked_student, "role": "teacher", "profile_required_at": None}
        self.assertTrue(profiles.needs_profile(self.conn, stale_unmarked, enabled=True))
        stale_marked = {"id": unmarked_teacher, "role": "student", "profile_required_at": MARK}
        self.assertFalse(profiles.needs_profile(self.conn, stale_marked, enabled=True))

    def test_an_empty_mark_is_no_mark(self):
        user_id = self.account()
        for value in ("", None):
            self.conn.execute("UPDATE users SET profile_required_at = ? WHERE id = ?", (value, user_id))
            self.assertFalse(self.needs(user_id))

    def test_it_reads_and_writes_nothing(self):
        user_id = self.account(marked=True)
        before, changes = self.row(user_id), self.conn.total_changes
        for _ in range(3):
            self.assertTrue(self.needs(user_id))
            self.assertFalse(self.needs(user_id, enabled=False))
        self.assertFalse(self.conn.in_transaction)
        self.assertEqual((self.row(user_id), self.conn.total_changes), (before, changes))

    def test_a_mark_written_while_the_gate_was_on_stays_and_is_ignored_while_it_is_off(self):
        user_id = self.account(marked=True)
        self.assertFalse(self.needs(user_id, enabled=False))
        self.assertEqual(self.row(user_id)[-1], MARK)       # stored, untouched
        self.assertTrue(self.needs(user_id, enabled=True))  # and read again when the gate is back on

    def test_a_profile_of_either_type_satisfies_it_whatever_the_role(self):  # I6
        for role, kind in (("student", "teacher"), ("student", "student"), ("teacher", "student")):
            with self.subTest(role=role, profile=kind):
                user_id = self.account(role, marked=True)
                self.assertEqual(self.needs(user_id), role == "student")
                self.give_profile(user_id, kind)
                self.assertFalse(self.needs(user_id))


class RoleChangeTest(ServiceTestCase):
    def snapshot(self, user_id):
        return (self.conn.execute("SELECT * FROM user_profiles WHERE user_id = ?", (user_id,)).fetchall(),
                self.conn.execute("SELECT * FROM teacher_subjects WHERE user_id = ?", (user_id,)).fetchall(),
                self.conn.execute("SELECT profile_required_at FROM users WHERE id = ?", (user_id,)).fetchone())

    def test_promoting_a_marked_student_stops_the_question_and_demoting_it_restores_it(self):  # J12
        user_id = self.account(marked=True)
        before = self.snapshot(user_id)
        self.assertTrue(self.needs(user_id))
        for role in ("teacher", "admin"):
            accounts.set_role(self.conn, user_id, role)
            self.conn.commit()
            self.assertFalse(self.needs(user_id), role)
            self.assertEqual(self.snapshot(user_id), before)   # nothing written, deleted or reinterpreted
        accounts.set_role(self.conn, user_id, "student")
        self.conn.commit()
        self.assertTrue(self.needs(user_id))
        self.assertEqual(self.snapshot(user_id), before)

    def test_demoting_a_teacher_with_a_teacher_profile_is_not_asked_and_keeps_the_profile(self):  # J12
        user_id = self.account("teacher")
        self.give_profile(user_id, "teacher")
        before = self.snapshot(user_id)
        accounts.set_role(self.conn, user_id, "student")
        self.conn.commit()
        self.assertFalse(self.needs(user_id))
        self.assertEqual(self.snapshot(user_id), before)
        self.assertEqual(profiles.get_profile(self.conn, user_id)["account_type"], "teacher")

    def test_a_demoted_cli_teacher_without_mark_or_profile_is_not_asked(self):
        user_id = self.account("teacher")
        accounts.set_role(self.conn, user_id, "student")
        self.conn.commit()
        self.assertFalse(self.needs(user_id))
        self.assertEqual(self.snapshot(user_id), ([], [], (None,)))

    def test_a_profile_follows_nobodys_role(self):  # I6
        user_id = self.account("student")
        self.give_profile(user_id, "teacher")
        accounts.set_role(self.conn, user_id, "admin")
        self.conn.commit()
        self.assertEqual(profiles.get_profile(self.conn, user_id)["account_type"], "teacher")
        self.assertEqual(self.row(user_id)[3], "admin")


class MarkProfileRequiredTest(ServiceTestCase):
    def test_it_marks_a_student_once_and_writes_only_the_mark(self):
        user_id = self.account()
        before = self.row(user_id)
        accounts.mark_profile_required(self.conn, user_id)
        self.conn.commit()
        after = self.row(user_id)
        self.assertRegex(after[-1], r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d$")
        self.assertEqual(after[:-1], before[:-1])   # role, email, key, updated_at... all as they were
        mark = after[-1]
        with self.assertRaises(LookupError):
            accounts.mark_profile_required(self.conn, user_id)
        self.assertEqual(self.row(user_id)[-1], mark)

    def test_it_refuses_everything_that_is_not_an_unmarked_student(self):
        teacher, admin, marked = self.account("teacher"), self.account("admin"), self.account(marked=True)
        for user_id in (teacher, admin, marked, 9999, 0, -1):
            with self.subTest(user_id=user_id), self.assertRaises(LookupError):
                accounts.mark_profile_required(self.conn, user_id)
        self.assertEqual([self.row(u)[-1] for u in (teacher, admin, marked)], [None, None, MARK])

    def test_it_does_not_rotate_the_session_key(self):
        user_id = self.account()
        key = self.row(user_id)[6]
        accounts.mark_profile_required(self.conn, user_id)
        self.assertEqual(self.row(user_id)[6], key)

    def test_profile_requirement_reads_the_role_and_the_mark(self):
        unmarked, marked = self.account(), self.account("teacher", marked=True)
        self.assertEqual(accounts.profile_requirement(self.conn, unmarked),
                         {"role": "student", "profile_required_at": None})
        self.assertEqual(accounts.profile_requirement(self.conn, marked),
                         {"role": "teacher", "profile_required_at": MARK})
        for bad in (9999, 0, -1, None, "1", True, 1.0, 2 ** 63):
            self.assertIsNone(accounts.profile_requirement(self.conn, bad), bad)

    def test_the_loaded_account_does_not_carry_the_mark(self):
        user_id = self.account(marked=True)
        self.assertNotIn("profile_required_at", accounts.get_user_by_id(self.conn, user_id))


class SetDisplayNameTest(ServiceTestCase):
    def test_it_writes_the_name_and_the_time_and_nothing_else(self):
        user_id = self.account(marked=True)
        self.conn.execute("UPDATE users SET email_verified_at = ?, last_login_at = ? WHERE id = ?", (MARK, MARK, user_id))
        self.conn.commit()
        before = self.row(user_id)
        self.assertEqual(accounts.set_display_name(self.conn, user_id, "  Мария Петрова  "), "Мария Петрова")
        self.conn.commit()
        after = self.row(user_id)
        changed = {USER_COLUMNS[i] for i in range(len(USER_COLUMNS)) if before[i] != after[i]}
        self.assertLessEqual(changed, {"display_name", "updated_at"})
        self.assertEqual(after[2], "Мария Петрова")
        for column in ("role", "email", "email_verified_at", "is_active", "session_key", "password_hash",
                       "profile_required_at"):
            self.assertEqual(after[USER_COLUMNS.index(column)], before[USER_COLUMNS.index(column)], column)

    def test_a_bad_name_or_account_writes_nothing(self):
        user_id = self.account()
        before = self.row(user_id)
        for bad in ("", " ", "А", "я" * 81, None, 5, b"name"):
            with self.subTest(name=bad), self.assertRaises(ValueError):
                accounts.set_display_name(self.conn, user_id, bad)
        with self.assertRaises(LookupError):
            accounts.set_display_name(self.conn, 9999, "Мария Петрова")
        self.assertEqual(self.row(user_id), before)


class CompleteStudentProfileTest(ServiceTestCase):
    def test_it_creates_the_profile_and_sets_the_display_name_in_one_transaction(self):  # J7
        user_id = self.account(marked=True, name="Google Име")
        before = self.row(user_id)
        profile = profiles.complete_student_profile(
            self.conn, user_id, first_name="Мария", last_name="Петрова", grade=10, now=NOW)
        self.assertTrue(self.conn.in_transaction)   # nothing is committed by the service
        self.conn.commit()
        self.assertEqual((profile["account_type"], profile["grade"]), ("student", 10))
        after = self.row(user_id)
        self.assertEqual(after[2], "Мария Петрова")
        for column in ("role", "email", "email_verified_at", "is_active", "session_key", "password_hash",
                       "profile_required_at", "id"):
            self.assertEqual(after[USER_COLUMNS.index(column)], before[USER_COLUMNS.index(column)], column)
        self.assertFalse(self.needs(user_id))   # the mark stays, the profile satisfies it
        self.assertEqual(after[-1], MARK)

    def test_a_failed_display_name_leaves_neither_write_after_the_callers_rollback(self):
        user_id = self.account(marked=True, name="Google Име")
        self.conn.commit()
        before = self.row(user_id)
        with patch.object(accounts, "set_display_name", side_effect=sqlite3.OperationalError("disk I/O error")):
            with self.assertRaises(sqlite3.OperationalError):
                profiles.complete_student_profile(
                    self.conn, user_id, first_name="Мария", last_name="Петрова", grade=10, now=NOW)
        self.assertTrue(self.conn.in_transaction)   # the profile insert is still pending: the caller rolls back
        self.conn.rollback()
        self.assertIsNone(profiles.get_profile(self.conn, user_id))
        self.assertEqual(self.row(user_id), before)
        self.assertTrue(self.needs(user_id))

    def test_a_second_profile_is_refused_and_changes_neither_the_profile_nor_the_name(self):
        user_id = self.account(marked=True)
        profiles.complete_student_profile(self.conn, user_id, first_name="Мария", last_name="Петрова", grade=10, now=NOW)
        self.conn.commit()
        before = (self.row(user_id), profiles.get_profile(self.conn, user_id))
        with self.assertRaises(profiles.ProfileExists):
            profiles.complete_student_profile(self.conn, user_id, first_name="Друга", last_name="Фамилия", grade=12, now=LATER)
        self.conn.rollback()
        self.assertEqual((self.row(user_id), profiles.get_profile(self.conn, user_id)), before)


class SaveProfileTest(ServiceTestCase):
    def test_changed_names_update_the_display_name_and_unchanged_names_do_not(self):
        user_id = self.account()
        profiles.complete_student_profile(self.conn, user_id, first_name="Мария", last_name="Петрова", grade=10, now=NOW)
        self.conn.commit()
        accounts.set_display_name(self.conn, user_id, "Преименуван оператор")    # an operator's rename
        self.conn.commit()
        profiles.save_profile(self.conn, user_id, first_name="Мария", last_name="Петрова", grade=11, now=LATER)
        self.conn.commit()
        self.assertEqual(self.row(user_id)[2], "Преименуван оператор")
        profile = profiles.get_profile(self.conn, user_id)
        self.assertEqual((profile["grade"], profile["grade_set_at"]), (11, "2026-11-14 12:00:00"))
        profiles.save_profile(self.conn, user_id, first_name="Мариана", last_name="Петрова", grade=11, now=LATER)
        self.conn.commit()
        self.assertEqual(self.row(user_id)[2], "Мариана Петрова")

    def test_a_teacher_type_profile_on_a_student_account_edits_school_and_subjects_and_grants_nothing(self):  # I5
        user_id = self.account("student")
        self.give_profile(user_id, "teacher")
        before = self.row(user_id)
        profile = profiles.save_profile(
            self.conn, user_id, first_name="Иван", last_name="Петров", school_name="ПГ Тест, София",
            subjects=[{"subject_id": None, "label": "Роден език"}], now=LATER)
        self.conn.commit()
        self.assertEqual((profile["school_name"], [s["label"] for s in profile["subjects"]]),
                         ("ПГ Тест, София", ["Роден език"]))
        after = self.row(user_id)
        self.assertEqual(after[3], "student")
        for column in ("email", "email_verified_at", "is_active", "session_key", "password_hash",
                       "profile_required_at"):
            self.assertEqual(after[USER_COLUMNS.index(column)], before[USER_COLUMNS.index(column)], column)

    def test_a_field_of_the_other_type_is_refused_and_writes_nothing(self):  # I5
        student, teacher = self.account(), self.account("teacher")
        self.give_profile(student, "student")
        self.give_profile(teacher, "teacher")
        before = (profiles.get_profile(self.conn, student), profiles.get_profile(self.conn, teacher),
                  self.row(student), self.row(teacher))
        for user_id, fields in ((student, {"school_name": SCHOOL}), (student, {"subjects": [IT]}),
                                (teacher, {"grade": 9})):
            with self.subTest(user_id=user_id, fields=sorted(fields)):
                with self.assertRaises(ValueError):
                    profiles.save_profile(self.conn, user_id, first_name="Нова", last_name="Фамилия", now=LATER, **fields)
                self.conn.rollback()
        self.assertEqual((profiles.get_profile(self.conn, student), profiles.get_profile(self.conn, teacher),
                          self.row(student), self.row(teacher)), before)

    def test_an_account_without_a_profile_has_nothing_to_edit(self):
        user_id = self.account()
        with self.assertRaises(LookupError):
            profiles.save_profile(self.conn, user_id, first_name="Мария", last_name="Петрова", grade=9, now=NOW)
        self.assertIsNone(profiles.get_profile(self.conn, user_id))

    def test_a_failed_display_name_rolls_the_edit_back_with_the_caller(self):
        user_id = self.account()
        profiles.complete_student_profile(self.conn, user_id, first_name="Мария", last_name="Петрова", grade=10, now=NOW)
        self.conn.commit()
        before = (profiles.get_profile(self.conn, user_id), self.row(user_id))
        with patch.object(accounts, "set_display_name", side_effect=sqlite3.OperationalError("boom")):
            with self.assertRaises(sqlite3.OperationalError):
                profiles.save_profile(self.conn, user_id, first_name="Мариана", last_name="Петрова", grade=12, now=LATER)
        self.conn.rollback()
        self.assertEqual((profiles.get_profile(self.conn, user_id), self.row(user_id)), before)


class ValidateProfileTest(unittest.TestCase):
    STUDENT = {"first_name": "  Мария ", "last_name": "Петрова", "grade": "10"}
    TEACHER = {"first_name": "Иван", "last_name": "Петров", "school_name": " СУ „Васил Левски“,  Пловдив ",
               "subject": ["it", "other"], "subject_other": "Роден език"}

    def test_a_student_form_has_three_fields_and_no_login_data(self):  # J7
        result = forms.validate_profile(self.STUDENT, "student")
        self.assertTrue(result.ok)
        self.assertEqual(result.values, {"account_type": "student", "first_name": "Мария", "last_name": "Петрова",
                                         "grade": 10, "next": None})
        self.assertIsNone(result.password)
        self.assertNotIn("email", result.values)
        self.assertNotIn("email", result.echo)

    def test_a_teacher_type_form_has_school_and_subjects(self):
        result = forms.validate_profile(self.TEACHER, "teacher")
        self.assertTrue(result.ok, result.errors)
        self.assertEqual(result.values["school_name"], "СУ „Васил Левски“, Пловдив")
        self.assertEqual(result.values["subjects"], [{"subject_id": "it", "label": "Информационни технологии"},
                                                     {"subject_id": None, "label": "Роден език"}])
        self.assertNotIn("grade", result.values)

    def test_the_type_is_the_callers_and_no_submitted_field_changes_it(self):  # I5, B11
        extra = {"role": "admin", "account_type": "teacher", "is_admin": "1", "email_verified_at": MARK,
                 "profile_required_at": MARK, "email": "x@example.bg", "password": "a-long-enough-password",
                 "confirm_password": "a-long-enough-password", "school_name": SCHOOL, "subject": ["it"]}
        result = forms.validate_profile({**self.STUDENT, **extra}, "student")
        self.assertTrue(result.ok)
        self.assertEqual(set(result.values), {"account_type", "first_name", "last_name", "grade", "next"})
        self.assertEqual(result.values["account_type"], "student")
        self.assertEqual(set(result.echo), {"first_name", "last_name", "grade", "next"})
        with self.assertRaises(ValueError):
            forms.validate_profile(self.STUDENT, "admin")
        with self.assertRaises(ValueError):
            forms.validate_profile(self.STUDENT, "")

    def test_errors_are_the_registration_messages_and_the_echo_is_bounded(self):
        result = forms.validate_profile({"first_name": "", "last_name": "я" * 200, "grade": "7", "next": "//evil.example"},
                                        "student")
        self.assertEqual(result.errors, {"first_name": "Въведи име.", "last_name": "Името трябва да е най-много 39 символа.",
                                         "grade": "Избери клас."})
        self.assertIsNone(result.values)
        self.assertEqual(len(result.echo["last_name"]), 39)
        self.assertEqual(result.echo["next"], "")

    def test_next_is_kept_only_when_the_form_rules_keep_it(self):  # F13
        kept = "/quiz/s/" + "A" * 43
        self.assertEqual(forms.validate_profile({**self.STUDENT, "next": kept}, "student").values["next"], kept)
        for bad in ("//evil.example", "https://evil.example/", "/a\\b", "/a\nb", "/a b", "/\u202eevil", "x" * 600,
                    "relative", "", None, ["/ok"]):
            with self.subTest(next=bad):
                result = forms.validate_profile({**self.STUDENT, "next": bad}, "student")
                self.assertTrue(result.ok)
                self.assertIsNone(result.values["next"])
                self.assertEqual(result.echo["next"], "")


if __name__ == "__main__":
    unittest.main()
