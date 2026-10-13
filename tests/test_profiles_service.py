"""Profile service (EMAIL_REGISTRATION PR 1): ``web.services.profiles``.

Every database is a temporary clone of the migrated canonical template
(``tests.support.build_test_database``); none is ``data/questions.db``. The service never
commits, so each test commits (or rolls back) itself.
"""

import re
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from src import subject_registry
from tests.support import ROOT, build_test_database
from web.services import accounts, profiles

NOW = datetime(2026, 10, 13, 12, 0, 0, tzinfo=timezone.utc)
LATER = NOW + timedelta(days=400)
STAMP = "2026-10-13 12:00:00"
LATER_STAMP = "2027-11-17 12:00:00"
IT = "it"
IT_LABEL = "Информационни технологии"
SCHOOL = "СУ „Васил Левски“, Пловдив"


def it():
    return {"subject_id": IT, "label": IT_LABEL}


def other(label="Роден език"):
    return {"subject_id": None, "label": label}


class ProfileTestCase(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="profiles-service-")
        self.addCleanup(directory.cleanup)
        self.path = build_test_database(Path(directory.name) / "profiles.db")
        self.conn = sqlite3.connect(self.path)
        self.addCleanup(self.conn.close)
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.student = accounts.create_user(self.conn, display_name="Ана Иванова", email="ana@example.bg",
                                            password="a-long-enough-password", role="student")
        self.teacher = accounts.create_user(self.conn, display_name="Иван Петров", email="ivan@example.bg",
                                            password="a-long-enough-password", role="teacher")
        self.admin = accounts.create_user(self.conn, display_name="Админ Админов", email="admin@example.bg",
                                          password="a-much-longer-admin-password", role="admin")
        self.conn.commit()

    def student_profile(self, user_id=None, **overrides):
        values = {"account_type": "student", "first_name": "Ана", "last_name": "Иванова", "grade": 9, "now": NOW}
        values.update(overrides)
        return profiles.create_profile(self.conn, user_id or self.student, **values)

    def teacher_profile(self, user_id=None, **overrides):
        values = {"account_type": "teacher", "first_name": "Иван", "last_name": "Петров", "school_name": SCHOOL,
                  "subjects": [it()], "now": NOW}
        values.update(overrides)
        return profiles.create_profile(self.conn, user_id or self.teacher, **values)

    def users(self):
        return self.conn.execute("SELECT * FROM users ORDER BY id").fetchall()

    def counts(self):
        return tuple(self.conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                     for t in ("user_profiles", "teacher_subjects"))

    def refuses(self, exception, call, *args, **kwargs):
        before = self.counts()
        with self.assertRaises(exception):
            call(*args, **kwargs)
        self.assertEqual(self.counts(), before)


class CreateTest(ProfileTestCase):
    def test_a_student_profile_is_stored_and_returned(self):
        profile = self.student_profile()
        self.assertEqual(profile, {"user_id": self.student, "account_type": "student", "first_name": "Ана",
                                   "last_name": "Иванова", "grade": 9, "grade_set_at": STAMP, "school_name": None,
                                   "created_at": STAMP, "updated_at": STAMP, "subjects": []})
        self.assertEqual(profiles.get_profile(self.conn, self.student), profile)
        self.assertEqual(self.counts(), (1, 0))

    def test_a_teacher_profile_keeps_its_subjects_in_order(self):
        profile = self.teacher_profile(subjects=[other(), it(), {"subject_id": "physics", "label": "Физика"}])
        self.assertEqual(profile["account_type"], "teacher")
        self.assertEqual((profile["grade"], profile["grade_set_at"], profile["school_name"]), (None, None, SCHOOL))
        self.assertEqual(profile["subjects"], [
            {"position": 1, "subject_id": None, "label": "Роден език"},
            {"position": 2, "subject_id": IT, "label": IT_LABEL},
            {"position": 3, "subject_id": "physics", "label": "Физика"}])

    def test_every_grade_from_8_to_12(self):
        for index, grade in enumerate((8, 9, 10, 11, 12)):
            user = accounts.create_user(self.conn, display_name="Ученик Ученик", email=f"s{index}@example.bg")
            self.assertEqual(self.student_profile(user, grade=grade)["grade"], grade)

    def test_every_registry_subject_is_accepted_with_its_title(self):
        for index, subject in enumerate(subject_registry.all_subjects()):
            user = accounts.create_user(self.conn, display_name="Учител Учител", email=f"t{index}@example.bg")
            profile = self.teacher_profile(user, subjects=[{"subject_id": subject.id, "label": subject.title}])
            self.assertEqual(profile["subjects"], [{"position": 1, "subject_id": subject.id, "label": subject.title}])

    def test_the_moment_dates_the_profile_in_utc(self):
        local = datetime(2026, 10, 13, 15, 30, 0, tzinfo=timezone(timedelta(hours=3)))
        profile = self.student_profile(now=local)
        self.assertEqual((profile["created_at"], profile["updated_at"], profile["grade_set_at"]),
                         ("2026-10-13 12:30:00",) * 3)

    def test_nothing_is_committed_and_a_rollback_undoes_it(self):
        self.student_profile()
        self.assertTrue(self.conn.in_transaction)
        with closing(sqlite3.connect(self.path)) as other_connection:
            self.assertEqual(other_connection.execute("SELECT COUNT(*) FROM user_profiles").fetchone()[0], 0)
        self.conn.rollback()
        self.assertEqual(self.counts(), (0, 0))
        self.teacher_profile(subjects=[it(), other()])
        self.conn.rollback()
        self.assertEqual(self.counts(), (0, 0))

    def test_a_second_profile_is_refused_and_the_first_is_unchanged(self):
        first = self.student_profile()
        self.conn.commit()
        with self.assertRaises(profiles.ProfileExists):
            self.student_profile(first_name="Мария", grade=12, now=LATER)
        self.assertEqual(profiles.get_profile(self.conn, self.student), first)
        with self.assertRaises(profiles.ProfileExists):
            self.teacher_profile(self.student)    # not even another type
        self.assertEqual(profiles.get_profile(self.conn, self.student), first)
        self.assertEqual(self.counts(), (1, 0))

    def test_a_second_profile_from_another_connection_is_refused(self):
        self.student_profile()
        self.conn.commit()
        with closing(sqlite3.connect(self.path)) as second:
            second.execute("PRAGMA foreign_keys = ON")
            with self.assertRaises(profiles.ProfileExists):
                profiles.create_profile(second, self.student, account_type="student", first_name="Ана",
                                        last_name="Иванова", grade=9, now=NOW)
            second.rollback()

    def test_an_unknown_account_is_a_lookup_error_even_without_foreign_keys(self):
        self.conn.execute("PRAGMA foreign_keys = OFF")
        self.refuses(LookupError, self.student_profile, 9999)
        self.refuses(LookupError, self.teacher_profile, 9999)

    def test_a_malformed_account_id_is_refused(self):
        for bad in (0, -1, True, False, None, "1", 1.0, 2 ** 63, [1], b"1"):
            with self.subTest(user_id=bad):
                self.refuses(ValueError, profiles.create_profile, self.conn, bad, account_type="student",
                             first_name="Ана", last_name="Иванова", grade=9, now=NOW)

    def test_the_account_type_is_student_or_teacher_only(self):
        for bad in ("admin", "Student", "", None, 1, ["student"]):
            with self.subTest(account_type=bad):
                self.refuses(ValueError, profiles.create_profile, self.conn, self.student, account_type=bad,
                             first_name="Ана", last_name="Иванова", grade=9, now=NOW)

    def test_cross_field_combinations(self):
        self.refuses(ValueError, self.student_profile, grade=None)
        self.refuses(ValueError, self.student_profile, school_name=SCHOOL)
        self.refuses(ValueError, self.student_profile, subjects=[it()])
        self.refuses(ValueError, self.student_profile, subjects=[])
        self.refuses(ValueError, self.teacher_profile, grade=9)
        self.refuses(ValueError, self.teacher_profile, school_name=None)
        self.refuses(ValueError, self.teacher_profile, subjects=None)
        self.refuses(ValueError, self.teacher_profile, subjects=[])

    def test_names_must_already_be_in_cleaned_form(self):
        for bad in ("", " Ана", "Ана ", "Жан  Пол", "Ана\tБ", "Ана1", "а" * 40, None, 5, ["Ана"], "Ана\x00",
                    "Ана‮", "😀"):
            with self.subTest(name=bad):
                self.refuses(ValueError, self.student_profile, first_name=bad)
                self.refuses(ValueError, self.student_profile, last_name=bad)
        self.assertEqual(self.student_profile(first_name="а" * 39, last_name="Жан Пол")["first_name"], "а" * 39)

    def test_grade_is_an_integer_from_8_to_12(self):
        for bad in (7, 13, 0, -9, "9", 9.0, 9.5, True, None, [9], 2 ** 70):
            with self.subTest(grade=bad):
                self.refuses(ValueError, self.student_profile, grade=bad)

    def test_school_must_already_be_in_cleaned_form(self):
        for bad in ("аб", "а" * 121, " СУ Тест", "СУ  Тест", "СУ Тест ", "123456", "СУ\x00Тест", "СУ‮Тест",
                    "", None, 5):
            with self.subTest(school=bad):
                self.refuses(ValueError, self.teacher_profile, school_name=bad)
        self.assertEqual(self.teacher_profile(school_name="а" * 120)["school_name"], "а" * 120)

    def test_subjects_follow_the_registry(self):
        bad_lists = {
            "four": [it(), {"subject_id": "physics", "label": "Физика"}, {"subject_id": "biology", "label": "Биология"},
                     other()],
            "unknown id": [{"subject_id": "latin", "label": "Латински"}],
            "route slug": [{"subject_id": "information-technologies", "label": IT_LABEL}],
            "alias": [{"subject_id": "IT", "label": IT_LABEL}],
            "wrong label": [{"subject_id": IT, "label": "ИТ"}],
            "lower label": [{"subject_id": IT, "label": IT_LABEL.lower()}],
            "label for other is not a title": [other("Информационни технологии ")],
            "duplicate": [it(), it()],
            "two others": [other("Роден език"), other("Друг език")],
            "other too short": [other("а")],
            "other too long": [other("а" * 61)],
            "other with digit": [other("Език 2")],
            "other uncollapsed": [other("Роден  език")],
            "other None": [other(None)],
            "subject_id not text": [{"subject_id": 1, "label": "Един"}],
            "subject_id list": [{"subject_id": ["it"], "label": IT_LABEL}],
            "label not text": [{"subject_id": IT, "label": None}],
            "missing key": [{"subject_id": IT}],
            "extra key": [{"subject_id": IT, "label": IT_LABEL, "position": 1}],
            "not a dict": [("it", IT_LABEL)],
            "a string": "it",
            "a dict": {"subject_id": IT, "label": IT_LABEL},
            "bytes": b"it",
            "a number": 5,
        }
        for name, subjects in bad_lists.items():
            with self.subTest(case=name):
                self.refuses((ValueError, TypeError), self.teacher_profile, subjects=subjects)
        self.assertEqual(self.counts(), (0, 0))

    def test_subjects_are_checked_against_the_registry_at_the_time_of_the_call(self):
        only_it = tuple(s for s in subject_registry.all_subjects() if s.id == IT)
        with patch.object(subject_registry, "_SUBJECTS_BY_ID", {s.id: s for s in only_it}):
            self.refuses(ValueError, self.teacher_profile, subjects=[{"subject_id": "physics", "label": "Физика"}])
            self.assertEqual(self.teacher_profile(subjects=[it()])["subjects"][0]["subject_id"], IT)

    def test_a_generator_of_subjects_is_accepted(self):
        profile = self.teacher_profile(subjects=(subject for subject in [it(), other()]))
        self.assertEqual(len(profile["subjects"]), 2)

    def test_the_moment_must_be_a_timezone_aware_datetime(self):
        for bad in (datetime(2026, 10, 13, 12, 0, 0), "2026-10-13 12:00:00", 1760356800, None):
            with self.subTest(now=bad):
                self.refuses(ValueError, self.student_profile, now=bad)

    def test_a_refused_call_leaves_no_transaction_open(self):
        self.assertFalse(self.conn.in_transaction)
        with self.assertRaises(ValueError):
            self.teacher_profile(subjects=[it(), it()])
        self.assertFalse(self.conn.in_transaction)       # everything is checked before the first write
        with self.assertRaises(LookupError):
            self.student_profile(9999)
        self.assertFalse(self.conn.in_transaction)

    def test_an_error_says_nothing_about_the_value(self):
        marker = "СЕКРЕТНО-УЧИЛИЩЕ-МАРКЕР"
        for call in (lambda: self.teacher_profile(school_name=marker + "\x00"),
                     lambda: self.teacher_profile(subjects=[other(marker + "1")]),
                     lambda: self.student_profile(first_name=marker + "1"),
                     lambda: self.teacher_profile(subjects=[{"subject_id": marker, "label": marker}])):
            with self.assertRaises(ValueError) as caught:
                call()
            self.assertNotIn(marker, str(caught.exception))
            self.assertNotIn(marker, repr(caught.exception))


class AuthorizationTest(ProfileTestCase):
    def test_a_profile_changes_nothing_about_any_account(self):
        before = self.users()
        self.student_profile()
        self.teacher_profile()
        self.conn.commit()
        self.assertEqual(self.users(), before)

    def test_a_profile_of_the_other_type_grants_and_changes_no_role(self):
        before = self.users()
        self.teacher_profile(self.student, first_name="Ана", last_name="Иванова")      # a student account declared as a teacher
        self.student_profile(self.teacher, first_name="Иван", last_name="Петров")      # a teacher account declared as a student
        self.teacher_profile(self.admin, first_name="Админ", last_name="Админов")      # an admin account
        self.conn.commit()
        self.assertEqual(self.users(), before)
        roles = dict(self.conn.execute("SELECT id, role FROM users"))
        self.assertEqual(roles, {self.student: "student", self.teacher: "teacher", self.admin: "admin"})
        # accounts.set_role is still the only way a role changes, and a profile follows nobody's role.
        accounts.set_role(self.conn, self.student, "student")
        self.assertEqual(profiles.get_profile(self.conn, self.student)["account_type"], "teacher")

    def test_updates_leave_the_account_untouched_too(self):
        self.student_profile()
        self.teacher_profile()
        self.conn.commit()
        before = self.users()
        profiles.update_profile(self.conn, self.student, first_name="Мария", grade=11, now=LATER)
        profiles.update_profile(self.conn, self.teacher, school_name="ПГ Тест", subjects=[other()], now=LATER)
        self.conn.commit()
        self.assertEqual(self.users(), before)

    def test_the_account_still_signs_in_after_a_profile(self):
        self.student_profile()
        self.conn.commit()
        self.assertEqual(accounts.authenticate(self.conn, "ana@example.bg", "a-long-enough-password")["id"], self.student)

    def test_no_function_here_reads_or_writes_a_users_column(self):
        source = (ROOT / "web" / "services" / "profiles.py").read_text(encoding="utf-8")
        statements = re.findall(r"(?:SELECT|INSERT|UPDATE|DELETE)[^\"]*", source)
        self.assertTrue(statements)
        for sql in statements:
            if re.search(r"\busers\b", sql):
                self.assertEqual(sql.strip(), "SELECT 1 FROM users WHERE id = ?")   # only an existence check
        for forbidden in ("role", "is_active", "session_key", "email_verified_at", "password"):
            self.assertNotIn(forbidden, " ".join(statements), forbidden)

    def test_only_the_profile_service_touches_the_profile_tables(self):
        allowed = {"web/services/profiles.py", "web/migrations/021_registration_schema.sql",
                   "src/canonical_tables.py"}
        pattern = re.compile(r"user_profiles|teacher_subjects|services\.profiles|import profiles\b")
        found = []
        for directory in ("web", "src", "deploy"):
            for path in sorted((ROOT / directory).rglob("*")):
                if path.is_file() and path.suffix in {".py", ".sql", ".html", ".sh", ".service", ".timer"} \
                        and "__pycache__" not in path.parts:
                    if pattern.search(path.read_text(encoding="utf-8", errors="replace")):
                        found.append(path.relative_to(ROOT).as_posix())
        self.assertEqual(set(found), allowed)


class GetProfileTest(ProfileTestCase):
    def test_no_profile_and_malformed_ids_find_nothing(self):
        self.assertIsNone(profiles.get_profile(self.conn, self.student))
        for bad in (0, -1, None, "1", True, 1.0, 2 ** 63, 9999):
            with self.subTest(user_id=bad):
                self.assertIsNone(profiles.get_profile(self.conn, bad))

    def test_a_read_writes_nothing(self):
        self.teacher_profile()
        self.conn.commit()
        self.assertFalse(self.conn.in_transaction)
        profiles.get_profile(self.conn, self.teacher)
        self.assertFalse(self.conn.in_transaction)

    def test_profiles_are_per_account(self):
        self.student_profile()
        self.teacher_profile()
        self.assertEqual(profiles.get_profile(self.conn, self.student)["account_type"], "student")
        self.assertEqual(profiles.get_profile(self.conn, self.teacher)["subjects"][0]["subject_id"], IT)
        self.assertIsNone(profiles.get_profile(self.conn, self.admin))


class UpdateTest(ProfileTestCase):
    def test_given_fields_change_and_the_rest_stay(self):
        self.student_profile()
        self.conn.commit()
        updated = profiles.update_profile(self.conn, self.student, first_name="Мария", now=LATER)
        self.assertEqual((updated["first_name"], updated["last_name"], updated["grade"]), ("Мария", "Иванова", 9))
        self.assertEqual((updated["created_at"], updated["updated_at"], updated["grade_set_at"]),
                         (STAMP, LATER_STAMP, STAMP))
        self.assertEqual(updated, profiles.get_profile(self.conn, self.student))

    def test_a_changed_grade_moves_its_date_and_an_unchanged_one_does_not(self):
        self.student_profile()
        same = profiles.update_profile(self.conn, self.student, grade=9, now=LATER)
        self.assertEqual((same["grade"], same["grade_set_at"]), (9, STAMP))
        changed = profiles.update_profile(self.conn, self.student, grade=10, now=LATER)
        self.assertEqual((changed["grade"], changed["grade_set_at"], changed["updated_at"]),
                         (10, LATER_STAMP, LATER_STAMP))
        for bad in (7, 13, "10", 10.0, True, None):
            with self.subTest(grade=bad), self.assertRaises(ValueError):
                profiles.update_profile(self.conn, self.student, grade=bad, now=LATER)
        self.assertEqual(profiles.get_profile(self.conn, self.student)["grade"], 10)

    def test_there_is_no_automatic_advancement(self):
        self.student_profile(grade=12)
        profiles.update_profile(self.conn, self.student, last_name="Петрова", now=LATER + timedelta(days=3650))
        self.assertEqual(profiles.get_profile(self.conn, self.student)["grade"], 12)

    def test_a_teacher_changes_school_and_replaces_subjects_as_a_whole(self):
        self.teacher_profile(subjects=[it(), other()])
        updated = profiles.update_profile(self.conn, self.teacher, school_name="ПГ по ЕТ, Бургас",
                                          subjects=[{"subject_id": "physics", "label": "Физика"}], now=LATER)
        self.assertEqual(updated["school_name"], "ПГ по ЕТ, Бургас")
        self.assertEqual(updated["subjects"], [{"position": 1, "subject_id": "physics", "label": "Физика"}])
        self.assertEqual(self.counts(), (1, 1))
        again = profiles.update_profile(self.conn, self.teacher, subjects=[other("Роден език"), it()], now=LATER)
        self.assertEqual([s["subject_id"] for s in again["subjects"]], [None, IT])
        self.assertEqual(again["school_name"], "ПГ по ЕТ, Бургас")
        # The same subjects again in another order is a replacement, not a conflict.
        reversed_order = profiles.update_profile(self.conn, self.teacher, subjects=[it(), other("Роден език")],
                                                 now=LATER)
        self.assertEqual([s["subject_id"] for s in reversed_order["subjects"]], [IT, None])

    def test_the_type_is_fixed(self):
        self.student_profile()
        self.teacher_profile()
        self.conn.commit()
        for call in (lambda: profiles.update_profile(self.conn, self.student, school_name=SCHOOL, now=LATER),
                     lambda: profiles.update_profile(self.conn, self.student, subjects=[it()], now=LATER),
                     lambda: profiles.update_profile(self.conn, self.teacher, grade=9, now=LATER),
                     lambda: profiles.update_profile(self.conn, self.student, account_type="teacher", now=LATER)):
            with self.assertRaises((ValueError, TypeError)):
                call()
        self.assertEqual(profiles.get_profile(self.conn, self.student)["school_name"], None)
        self.assertEqual(profiles.get_profile(self.conn, self.teacher)["grade"], None)

    def test_a_refused_update_changes_nothing(self):
        self.teacher_profile(subjects=[it(), other()])
        self.conn.commit()
        before = profiles.get_profile(self.conn, self.teacher)
        for kwargs in ({"first_name": "Нов", "school_name": "аб"}, {"first_name": "Нов", "subjects": [it(), it()]},
                       {"school_name": SCHOOL, "subjects": []}, {"first_name": "Ана1"}, {"last_name": " Петров"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                profiles.update_profile(self.conn, self.teacher, now=LATER, **kwargs)
            self.assertEqual(profiles.get_profile(self.conn, self.teacher), before)
            self.assertFalse(self.conn.in_transaction)

    def test_no_profile_or_no_field_is_an_error(self):
        with self.assertRaises(LookupError):
            profiles.update_profile(self.conn, self.student, first_name="Мария", now=LATER)
        self.student_profile()
        with self.assertRaises(ValueError):
            profiles.update_profile(self.conn, self.student, now=LATER)
        for bad in (0, -1, True, None, "1", 2 ** 63):
            with self.subTest(user_id=bad), self.assertRaises(ValueError):
                profiles.update_profile(self.conn, bad, first_name="Мария", now=LATER)
        with self.assertRaises(ValueError):
            profiles.update_profile(self.conn, self.student, first_name="Мария", now=datetime(2027, 1, 1))

    def test_an_update_is_not_committed(self):
        self.student_profile()
        self.conn.commit()
        profiles.update_profile(self.conn, self.student, first_name="Мария", now=LATER)
        self.conn.rollback()
        self.assertEqual(profiles.get_profile(self.conn, self.student)["first_name"], "Ана")


class DisplayNameTest(unittest.TestCase):
    def test_two_names_make_the_snapshot_string(self):
        self.assertEqual(profiles.display_name("Ана", "Иванова"), "Ана Иванова")
        self.assertEqual(profiles.display_name("Жан Пол", "О'Конър-Петрова"), "Жан Пол О'Конър-Петрова")

    def test_the_longest_pair_is_inside_the_account_rule(self):
        name = profiles.display_name("а" * 39, "б" * 39)
        self.assertEqual(len(name), 79)
        self.assertTrue(accounts.MIN_DISPLAY_NAME_LENGTH <= len(name) <= accounts.MAX_DISPLAY_NAME_LENGTH)
        shortest = profiles.display_name("А", "Б")
        self.assertTrue(accounts.MIN_DISPLAY_NAME_LENGTH <= len(shortest))

    def test_uncleaned_names_are_refused(self):
        for first, last in (("", "Б"), ("А", ""), (" А", "Б"), ("А", "Б "), ("А1", "Б"), (None, "Б"), ("а" * 40, "Б")):
            with self.subTest(first=first, last=last), self.assertRaises(ValueError):
                profiles.display_name(first, last)


class ModuleTest(unittest.TestCase):
    def test_it_imports_without_flask_or_the_application(self):
        code = (
            "import sys; before = set(sys.modules); import web.services.profiles; "
            "print(sorted(m for m in set(sys.modules) - before "
            "if m == 'flask' or m.startswith('flask.') or m == 'web.app' or m.startswith('web.blueprints')))"
        )
        out = subprocess.run([sys.executable, "-B", "-c", code], cwd=ROOT, capture_output=True, text=True, check=True)
        self.assertEqual(out.stdout.strip(), "[]")

    def test_it_never_commits_logs_or_prints(self):
        source = (ROOT / "web" / "services" / "profiles.py").read_text(encoding="utf-8")
        code = "\n".join(line for line in source.splitlines() if not line.lstrip().startswith(("#", '"""')))
        for forbidden in (".commit(", ".rollback(", "import logging", "getLogger", "print(", "SAVEPOINT", "BEGIN",
                          "executescript", "CURRENT_TIMESTAMP", "os.environ", "open("):
            self.assertNotIn(forbidden, code, forbidden)


if __name__ == "__main__":
    unittest.main()
