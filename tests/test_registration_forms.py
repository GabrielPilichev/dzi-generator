"""Registration form validators (test plan B1-B11, EMAIL_REGISTRATION PR 1).

Pure functions: no database is opened except where B3 and B8 check what the stored
account does with a validated value, and that one is an in-memory ``users`` table.
"""

import sqlite3
import subprocess
import sys
import unittest

from werkzeug.datastructures import MultiDict
from werkzeug.security import check_password_hash, generate_password_hash

from src import subject_registry
from tests.support import ROOT
from web.redirects import safe_redirect_target
from web.services import accounts
from web.services import registration_forms as forms

PASSWORD = "correct horse battery"
M = forms.MESSAGES


def student(**overrides):
    values = {"first_name": "Ана", "last_name": "Иванова", "email": "ana@example.bg", "grade": "9",
              "password": PASSWORD, "confirm_password": PASSWORD}
    values.update(overrides)
    return {key: value for key, value in values.items() if value is not None}


def teacher(**overrides):
    values = {"first_name": "Иван", "last_name": "Петров", "email": "ivan@school.bg",
              "school_name": "СУ „Васил Левски“, Пловдив", "subject": ["informatics"],
              "password": PASSWORD, "confirm_password": PASSWORD}
    values.update(overrides)
    return {key: value for key, value in values.items() if value is not None}


def user_table():
    conn = sqlite3.connect(":memory:")
    conn.executescript((ROOT / "web" / "migrations" / "012_users.sql").read_text(encoding="utf-8"))
    return conn


class FormTestCase(unittest.TestCase):
    def errors(self, form, validate=forms.validate_student):
        return validate(form).errors

    def assertValid(self, form, validate=forms.validate_student):
        result = validate(form)
        self.assertEqual(result.errors, {})
        self.assertTrue(result.ok)
        return result


class FieldBoundaryTest(FormTestCase):
    def test_names_empty_minimum_maximum_and_over(self):  # B1
        for field, empty in (("first_name", M["first_name_empty"]), ("last_name", M["last_name_empty"])):
            with self.subTest(field=field):
                self.assertEqual(self.errors(student(**{field: ""})), {field: empty})
                self.assertEqual(self.errors(student(**{field: "   "})), {field: empty})
                self.assertEqual(self.errors(student(**{field: None})), {field: empty})
                self.assertValid(student(**{field: "А"}))
                self.assertValid(student(**{field: "б" * 39}))
                self.assertEqual(self.errors(student(**{field: "б" * 40})), {field: M["name_too_long"]})
        self.assertEqual(M["name_too_long"], "Името трябва да е най-много 39 символа.")
        self.assertEqual(M["name_characters"], "Името може да съдържа само букви, интервал, тире и апостроф.")

    def test_the_length_is_measured_after_stripping_and_collapsing(self):  # B1, B2
        padded = "  " + "б" * 39 + "  "
        self.assertEqual(self.assertValid(student(first_name=padded)).values["first_name"], "б" * 39)
        spaced = "б" * 19 + " " * 8 + "в" * 19            # 46 characters, 39 once collapsed
        self.assertEqual(self.assertValid(student(first_name=spaced)).values["first_name"], "б" * 19 + " " + "в" * 19)

    def test_school_minimum_maximum_and_over(self):  # B1
        message = "Въведи име на училището (от 3 до 120 символа)."
        self.assertEqual(M["school"], message)
        for value in (None, "", "   ", "аб", "а" * 121):
            with self.subTest(school=value):
                self.assertEqual(self.errors(teacher(school_name=value), forms.validate_teacher),
                                 {"school_name": message})
        for value in ("абв", "а" * 120):
            self.assertEqual(self.assertValid(teacher(school_name=value), forms.validate_teacher)
                             .values["school_name"], value)

    def test_school_is_collapsed_and_needs_a_letter_and_no_control_or_format_character(self):  # B1
        collapsed = self.assertValid(teacher(school_name="  СУ   „Васил Левски“,\tПловдив  "), forms.validate_teacher)
        self.assertEqual(collapsed.values["school_name"], "СУ „Васил Левски“, Пловдив")
        # Collapsing happens before measuring: padding does not make two letters three.
        self.assertIn("school_name", self.errors(teacher(school_name="   аб   "), forms.validate_teacher))
        self.assertEqual(self.assertValid(teacher(school_name="а      б"), forms.validate_teacher)
                         .values["school_name"], "а б")
        for value in ("123456", "---", "„“ ,.", "СУ\x00Тест", "СУ\x07Тест", "СУ‮Тест", "СУ​Тест",
                      "СУ﻿Тест", "СУТест", "СУ\ud800Тест"):
            with self.subTest(school=value):
                self.assertIn("school_name", self.errors(teacher(school_name=value), forms.validate_teacher))
        for value in ("СУ „Васил Левски“, Пловдив", "School No. 5, Sofia", "ПГ по ЕТ 'Дончо Узунов', Бургас"):
            self.assertValid(teacher(school_name=value), forms.validate_teacher)
        self.assertIn("СУ „Васил Левски“, Пловдив", forms.SCHOOL_HINT)

    def test_subject_other_minimum_maximum_and_over(self):  # B1, B6
        form = teacher(subject=["other"])
        for value in (None, "", "   ", "а", "а" * 61):
            with self.subTest(other=value):
                self.assertEqual(self.errors(dict(form, subject_other=value) if value is not None else form,
                                             forms.validate_teacher), {"subject_other": M["subject_other"]})
        self.assertEqual(M["subject_other"], "Въведи името на предмета.")
        for value in ("аб", "а" * 60):
            self.assertValid(dict(form, subject_other=value), forms.validate_teacher)

    def test_email_length_boundaries(self):  # B1
        local = "a" * 64
        domain_budget = 254 - len(local) - 1
        domain = "b" * (domain_budget - 3) + ".bg"
        longest = f"{local}@{domain}"
        self.assertEqual(len(longest), 254)
        self.assertEqual(self.assertValid(student(email=longest)).values["email"], longest)
        self.assertEqual(self.errors(student(email="x" + longest)), {"email": M["email"]})
        self.assertEqual(self.errors(student(email=f"{'a' * 65}@example.bg")), {"email": M["email"]})
        self.assertValid(student(email=f"{'a' * 64}@example.bg"))
        self.assertEqual(self.errors(student(email="")), {"email": M["email"]})
        self.assertEqual(M["email"], "Невалиден имейл.")

    def test_password_boundaries(self):  # B1, B7
        for length, ok in ((0, False), (9, False), (10, True), (256, True), (257, False)):
            with self.subTest(length=length):
                value = "x" * length
                errors = self.errors(student(password=value, confirm_password=value))
                self.assertEqual(errors == {}, ok, errors)
        self.assertEqual(self.errors(student(password="x" * 9, confirm_password="x" * 9)),
                         {"password": accounts.password_problem("x" * 9)})
        self.assertEqual(self.errors(student(password="x" * 257, confirm_password="x" * 257)),
                         {"password": accounts.password_problem("x" * 257)})
        self.assertEqual(accounts.MIN_PASSWORD_LENGTH, 10)  # D12
        # Not the email, however it is cased.
        self.assertEqual(self.errors(student(email="Pupil.Name@example.bg", password="pupil.name@example.bg",
                                             confirm_password="pupil.name@example.bg")),
                         {"password": "Паролата не може да съвпада с имейла."})


class NameTest(FormTestCase):
    def test_accepted_names(self):  # B2
        for name in ("Ана", "Ivan", "Мария-Луиза", "О'Конър", "О’Конър", "Д.Х.", "Жан Пол", "Åsa", "Zoë",
                     "Nguyễn", "李", "José María", "а́", "Ал-Хасан", "Ян. Ян"):
            with self.subTest(name=name):
                self.assertEqual(self.assertValid(student(first_name=name)).values["first_name"], name)

    def test_refused_names(self):  # B2
        for name in ("Ана1", "123", "Ана_", "Ана@", "Ана/Ива", "Ана😀", "😀", "-", "...", "'", "’ ’",
                     "Ана\x00", "Ана\x07", "Ана\x7f", "Ана‮иВ", "Ана‪", "Ана⁦", "Ана​", "Ана﻿",
                     "Ана­", "́", "Ана<b>", "Ана;DROP", "Ана", "Ана\ud800", "Ана Б\x1b"):
            with self.subTest(name=name):
                self.assertEqual(self.errors(student(first_name=name)), {"first_name": M["name_characters"]})
                self.assertEqual(self.errors(student(last_name=name)), {"last_name": M["name_characters"]})

    def test_whitespace_is_stripped_and_collapsed_nothing_else_changes(self):  # B2
        cases = {"  Ана  ": "Ана", "Жан   Пол": "Жан Пол", "Жан\tПол": "Жан Пол", "Жан Пол": "Жан Пол",
                 "\t Ана \n": "Ана", "Ана　Б": "Ана Б", "ЖАН пол": "ЖАН пол", "é": "é"}
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(self.assertValid(student(first_name=raw)).values["first_name"], expected)

    def test_names_are_not_unicode_normalised(self):  # B2
        decomposed, composed = "é", "é"
        self.assertEqual(self.assertValid(student(first_name=decomposed)).values["first_name"], decomposed)
        self.assertEqual(self.assertValid(student(first_name=composed)).values["first_name"], composed)
        self.assertEqual(self.assertValid(student(first_name="ＡＢＣ")).values["first_name"], "ＡＢＣ")

    def test_two_longest_names_make_a_display_name_the_account_table_accepts(self):  # B3
        first, last = "а" * 39, "б" * 39
        result = self.assertValid(student(first_name=first, last_name=last))
        display = f"{result.values['first_name']} {result.values['last_name']}"
        self.assertEqual(len(display), 79)
        conn = user_table()
        self.addCleanup(conn.close)
        uid = accounts.create_user(conn, display_name=display, email=result.values["email"], password=PASSWORD)
        self.assertEqual(accounts.get_user_by_id(conn, uid)["display_name"], display)
        # The shortest pair, one character each, is inside the 2-80 rule too.
        short = self.assertValid(student(first_name="А", last_name="Б")).values
        accounts.create_user(conn, display_name=f"{short['first_name']} {short['last_name']}")

    def test_non_text_names_are_refused_not_raised(self):  # B2
        for value in (1, 1.5, True, ["Ана"], ("Ана",), {"a": 1}, b"Ana", object()):
            with self.subTest(value=type(value).__name__):
                self.assertEqual(self.errors(student(first_name=value)), {"first_name": M["name_characters"]})


class EmailTest(FormTestCase):
    def email(self, value, **kwargs):
        return forms.public_email(value, **kwargs)

    def test_accepted_shapes_are_normalised_by_the_account_rule(self):  # B4
        cases = {"ana@example.bg": "ana@example.bg", "Ana.Ivanova@Example.BG": "ana.ivanova@example.bg",
                 "  ana@example.bg  ": "ana@example.bg", "a+tag@example.bg": "a+tag@example.bg",
                 "a_b%c-d@mail.example.co.uk": "a_b%c-d@mail.example.co.uk", "a.b@x-y.bg": "a.b@x-y.bg",
                 "ＡＮＡ＠ＥＸＡＭＰＬＥ．ＢＧ": "ana@example.bg", "1@2.bg": "1@2.bg"}
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(self.email(raw), expected)
                self.assertEqual(self.email(raw), accounts.normalize_email(raw))

    def test_everything_normalize_email_accepts_but_the_public_rule_refuses(self):  # B4
        cases = ["ана@example.bg", "ana@пример.бг", "ana@example.бг", "añ@example.bg", "ana@exämple.bg",
                 "ana@localhost", "ana@example", "ana@example.b", "ana@example.b1", "ana@example.1g",
                 "ana@example..bg", "ana@.example.bg", "ana@example.bg.", "ana..b@example.bg", ".ana@example.bg",
                 "ana.@example.bg", "a" * 65 + "@example.bg", "ana@exa_mple.bg", "ana@exa mple.bg",
                 "ana@example.bg\x00", "a'na@example.bg", "a(na)@example.bg", "a,b@example.bg", "a;b@example.bg",
                 "<ana>@example.bg", "ana@[127.0.0.1]", "ana@example.bg>", "\"ana\"@example.bg", "a!b@example.bg",
                 "a=b@example.bg", "a/b@example.bg", "a?b@example.bg", "a#b@example.bg", "ana@exam$ple.bg"]
        for raw in cases:
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError):
                    self.email(raw)
        # Each of them is a shape sign-in would still take (that rule is unchanged).
        for raw in ("ана@example.bg", "ana@localhost", "ana@example", "ana@пример.бг", "ana..b@example.bg"):
            self.assertEqual(accounts.normalize_email(raw), raw.lower())

    def test_inner_whitespace_is_refused_never_collapsed(self):  # B4
        for raw in ("an a@example.bg", "ana@exam ple.bg", "ana@example.bg x", "ana\t@example.bg",
                    "ana@example.bg x", "ana　@example.bg", "ana @ example.bg"):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                self.email(raw)

    def test_a_carriage_return_or_line_feed_anywhere_is_refused(self):  # B4
        for raw in ("ana@example.bg\n", "\nana@example.bg", "ana\n@example.bg", "ana@example.bg\r",
                    "ana@example.bg\r\n", "\rana@example.bg", "ana@exam\nple.bg", "ana@example.bg\r\nBcc: x@y.bg"):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                self.email(raw)
            self.assertIn("email", self.errors(student(email=raw)))

    def test_non_text_emails_are_refused_not_raised(self):  # B4
        for value in (None, 1, ["a@b.bg"], b"a@b.bg", {"a": 1}):
            with self.subTest(value=type(value).__name__), self.assertRaises(ValueError):
                self.email(value)
        self.assertEqual(self.errors(student(email=None)), {"email": M["email"]})
        self.assertEqual(self.errors(student(email=["a@b.bg"])), {"email": M["email"]})

    def test_the_error_names_no_address(self):  # B4
        with self.assertRaises(ValueError) as caught:
            self.email("secret.person@example")
        self.assertEqual(str(caught.exception), M["email"])
        self.assertNotIn("secret", repr(caught.exception))

    def test_a_valid_form_carries_the_normalised_address(self):  # B4
        result = self.assertValid(student(email="  Ana.Ivanova@Example.BG "))
        self.assertEqual(result.values["email"], "ana.ivanova@example.bg")
        self.assertEqual(result.echo["email"], "  Ana.Ivanova@Example.BG ")


class GradeTest(FormTestCase):
    def test_only_8_to_12_as_integers(self):  # B5
        for grade in ("8", "9", "10", "11", "12", 8, 12):
            with self.subTest(grade=grade):
                self.assertEqual(self.assertValid(student(grade=grade)).values["grade"], int(grade))
        for grade in ("8.0", "08", "08a", "7", "13", "0", "-9", "", " 9", "9 ", "9\n", "٩", "９", "nine", "1e1",
                      "0x9", "9.5", 7, 13, 8.0, 9.5, True, False, None, ["9"], ("9",), {"9": 1}, b"9"):
            with self.subTest(grade=grade):
                self.assertEqual(self.errors(student(grade=grade)), {"grade": "Избери клас."})

    def test_a_missing_grade_is_refused(self):  # B5
        form = student()
        del form["grade"]
        self.assertEqual(self.errors(form), {"grade": "Избери клас."})
        self.assertEqual(self.errors(student(grade=["9", "10"])), {"grade": "Избери клас."})

    def test_a_student_form_takes_no_school_or_subjects(self):
        result = self.assertValid(student(school_name="СУ Тест", subject=["it"], subject_other="Друго"))
        self.assertEqual(set(result.values), {"first_name", "last_name", "email", "next", "account_type", "grade"})


class SubjectTest(FormTestCase):
    def validate(self, **overrides):
        return forms.validate_teacher(teacher(**overrides))

    def test_the_accepted_ids_are_the_registry_ids_at_call_time(self):  # B6
        ids = [subject.id for subject in subject_registry.all_subjects()]
        self.assertGreaterEqual(len(ids), 3)
        for subject in subject_registry.all_subjects():
            with self.subTest(subject=subject.id):
                result = self.validate(subject=[subject.id])
                self.assertEqual(result.errors, {})
                self.assertEqual(result.values["subjects"], [{"subject_id": subject.id, "label": subject.title}])
        self.assertEqual(self.validate(subject=ids[:3]).errors, {})

    def test_the_form_follows_a_changed_registry(self):  # B6
        from unittest.mock import patch
        with patch.object(subject_registry, "all_subjects", return_value=(subject_registry.all_subjects()[0],)):
            first = subject_registry.all_subjects()[0].id
            self.assertEqual(self.validate(subject=[first]).errors, {})
            self.assertEqual(self.validate(subject=["mathematics"]).errors, {"subject": M["subject_unknown"]})

    def test_none_four_unknown_duplicate_and_malformed(self):  # B6
        self.assertEqual(self.validate(subject=[]).errors, {"subject": "Избери поне един предмет."})
        self.assertEqual(self.validate(subject=None).errors, {"subject": "Избери поне един предмет."})
        self.assertEqual(self.validate(subject=["it", "mathematics", "physics", "biology"]).errors,
                         {"subject": "Избери най-много 3 предмета."})
        self.assertEqual(self.validate(subject=["it", "mathematics", "physics", "other"], subject_other="Роден")
                         .errors, {"subject": "Избери най-много 3 предмета."})  # other counts toward the three
        for bad in (["nope"], ["IT"], ["information-technologies"], ["informatika_it"], ["Информационни технологии"],
                    ["it", "it"], ["other", "other"], [""], [" it"], [None], [1], [["it"]], [b"it"], [{"a": 1}],
                    ["it", "nope"], ["it\x00"]):
            with self.subTest(bad=bad):
                self.assertEqual(self.validate(subject=bad, subject_other="Роден").errors,
                                 {"subject": M["subject_unknown"]})

    def test_a_bare_string_is_one_subject(self):  # B6
        self.assertEqual(self.validate(subject="it").values["subjects"],
                         [{"subject_id": "it", "label": "Информационни технологии"}])

    def test_other_needs_its_text(self):  # B6
        ok = self.validate(subject=["it", "other"], subject_other="  Роден   език ")
        self.assertEqual(ok.values["subjects"], [{"subject_id": "it", "label": "Информационни технологии"},
                                                 {"subject_id": None, "label": "Роден език"}])
        for text in (None, "", "а", "а" * 61, "Роден език 2", "Роден_език", "Роден\x00език", "---", "Рoден😀", 5, ["a"]):
            with self.subTest(text=text):
                self.assertEqual(self.validate(subject=["other"], subject_other=text).errors,
                                 {"subject_other": M["subject_other"]})
        for text in ("Ал-Хасан", "Тяло и движение", "Ab", "a" * 60):
            self.assertEqual(self.validate(subject=["other"], subject_other=text).errors, {})

    def test_other_text_without_other_is_ignored(self):  # B6
        result = self.validate(subject=["it"], subject_other="Роден език")
        self.assertEqual(result.errors, {})
        self.assertEqual(result.values["subjects"], [{"subject_id": "it", "label": "Информационни технологии"}])
        self.assertEqual(self.validate(subject=["it"], subject_other="а\x00").errors, {})

    def test_order_is_kept(self):  # B6
        result = self.validate(subject=["physics", "other", "it"], subject_other="Роден")
        self.assertEqual([s["subject_id"] for s in result.values["subjects"]], ["physics", None, "it"])

    def test_a_multidict_form_works_like_a_dict(self):  # B6
        form = MultiDict(teacher(subject=None))
        form.setlist("subject", ["it", "physics"])
        result = forms.validate_teacher(form)
        self.assertEqual(result.errors, {})
        self.assertEqual([s["subject_id"] for s in result.values["subjects"]], ["it", "physics"])
        self.assertEqual(result.password, PASSWORD)

    def test_a_teacher_form_takes_no_grade(self):
        result = self.validate(grade="9")
        self.assertEqual(result.errors, {})
        self.assertNotIn("grade", result.values)


class PasswordTest(FormTestCase):
    EXACT = ["  pupil  passphrase  ", "  leading-space-pass", "trailing-space-pass  ", "doubled  inner  space",
             "tab\tinside-password", "ｆｕｌｌｗｉｄｔｈ-pass", "x" * 256, "ＡＢＣé́pass-word", " " * 10, " " * 10,
             "a​b-hidden-pass", "pass\nword-multiline"]

    def test_the_validator_returns_the_identical_string(self):  # B8
        for password in self.EXACT:
            with self.subTest(password=password):
                result = self.assertValid(student(password=password, confirm_password=password))
                self.assertIs(type(result.password), str)
                self.assertEqual(result.password, password)
                self.assertEqual(len(result.password), len(password))
                self.assertEqual(result.values.get("password"), None)  # not part of the cleaned values

    def test_the_stored_hash_verifies_the_original_and_login_refuses_every_variant(self):  # B8
        import unicodedata
        conn = user_table()
        self.addCleanup(conn.close)
        for index, password in enumerate(self.EXACT):
            with self.subTest(password=password):
                email = f"user{index}@example.bg"
                result = self.assertValid(student(email=email, password=password, confirm_password=password))
                digest = generate_password_hash(result.password, method=accounts.PASSWORD_METHOD)
                self.assertTrue(check_password_hash(digest, password))
                accounts.create_user(conn, display_name="Проба Проба", email=email, password=result.password)
                self.assertIsNotNone(accounts.authenticate(conn, email, password))
                variants = {password.strip(), " ".join(password.split()), unicodedata.normalize("NFKC", password),
                            unicodedata.normalize("NFC", password), password[:-1], password + " "} - {password}
                for variant in variants:
                    self.assertIsNone(accounts.authenticate(conn, email, variant), repr(variant))

    def test_a_257_character_password_is_refused_not_cut(self):  # B8
        value = "x" * 257
        result = forms.validate_student(student(password=value, confirm_password=value))
        self.assertEqual(result.errors, {"password": accounts.password_problem(value)})
        self.assertIsNone(result.password)
        self.assertIsNone(result.values)

    def test_the_confirmation_is_compared_exactly(self):  # B9
        for password, confirmation in (("abc  defghij", "abc defghij"), ("abcdefghij", "abcdefghij "),
                                       (" abcdefghij", "abcdefghij"), ("abcdefghij", "ABCDEFGHIJ"),
                                       ("ａｂｃｄｅｆｇｈｉｊ", "abcdefghij"), ("abcdefghij", ""), ("abcdefghij", None),
                                       ("abcdefghij", 5), ("abcdefghij", ["abcdefghij"])):
            with self.subTest(confirmation=confirmation):
                form = student(password=password, confirm_password=confirmation)
                self.assertEqual(self.errors(form), {"confirm_password": "Паролите не съвпадат."})
        self.assertEqual(self.assertValid(student(password="abc  defghij", confirm_password="abc  defghij")).password,
                         "abc  defghij")

    def test_a_missing_or_malformed_password_is_an_error_not_an_exception(self):  # B7
        form = student()
        del form["password"]
        del form["confirm_password"]
        self.assertEqual(self.errors(form), {"password": accounts.password_problem("")})
        for value in (5, ["x" * 12], b"x" * 12, None):
            with self.subTest(value=value):
                errors = self.errors(student(password=value, confirm_password=value))
                self.assertIn("password", errors)

    def test_a_password_is_never_in_an_error_the_echo_or_a_repr(self):  # B7
        secret = "uniquely-secret-pass-phrase"
        for form, validate in ((student(password=secret, confirm_password="other " + secret, email="bad"),
                                forms.validate_student),
                               (teacher(password=secret, confirm_password=secret + "x", first_name="1"),
                                forms.validate_teacher),
                               (student(password=secret, confirm_password=secret), forms.validate_student)):
            result = validate(form)
            self.assertNotIn(secret, repr(result))
            self.assertNotIn(secret, str(result.errors))
            self.assertNotIn(secret, str(result.echo))
            self.assertNotIn(secret, str(result.values))
            self.assertNotIn("password", result.echo)
            self.assertNotIn("confirm_password", result.echo)
        self.assertEqual(validate(form).password, secret)

    def test_the_password_equal_to_the_email_is_judged_on_the_normalised_address(self):  # B7
        result = forms.validate_student(student(email="Ana@Example.BG", password="ana@example.bg",
                                                confirm_password="ana@example.bg"))
        self.assertEqual(result.errors, {"password": "Паролата не може да съвпада с имейла."})


class NextTest(FormTestCase):
    def kept(self, value):
        return self.assertValid(student(next=value)).values["next"]

    def test_next_is_stored_unaltered_when_safe_redirect_target_accepts_it(self):  # B10
        for value in ("/", "/quiz/s/abcDEF123", "/login?next=/x", "/a/b?c=d&e=f#frag", "/account/profile?next=/quiz/s/t"):
            with self.subTest(value=value):
                self.assertEqual(safe_redirect_target(value, "/"), value)
                self.assertEqual(self.kept(value), value)

    def test_unsafe_next_is_dropped_never_repaired(self):  # B10
        for value in ("https://evil.example/", "//evil.example", "/\\evil.example", "http:evil", "evil", "",
                      "/ok\nLocation: x", "/ok\r", "/ok\x00", "/ok\x7f", "javascript:alert(1)", "\\\\evil",
                      "/" + "a" * 512, None, 5, ["/ok"], b"/ok"):
            with self.subTest(value=value if not isinstance(value, str) else value[:40]):
                self.assertIsNone(self.kept(value))
        self.assertEqual(self.kept("/" + "a" * 511), "/" + "a" * 511)   # 512 characters is allowed
        self.assertIsNone(self.kept("/" + "a" * 512))                  # 513 is not

    def test_next_absent_is_none(self):  # B10
        self.assertIsNone(self.assertValid(student()).values["next"])

    def test_a_dropped_next_is_not_an_error(self):  # B10
        self.assertEqual(self.errors(student(next="//evil.example")), {})

    def test_a_rejected_next_is_never_echoed(self):  # B10
        for value in ("//" + "a" * 600, "//evil.example", "https://evil.example/", "javascript:alert(1)",
                      "/ok\nLocation: x", "/ok\u202eevil", "relative", "я" * 1000, None, 5, ["/ok"]):
            with self.subTest(value=value if not isinstance(value, str) else value[:30]):
                result = forms.validate_student(student(next=value, email="bad"))   # an invalid form re-renders
                self.assertFalse(result.ok)
                self.assertEqual(result.echo["next"], "")
                self.assertNotIn("evil", repr(result.echo))

    def test_an_accepted_next_is_echoed_exactly(self):  # B10
        for value in ("/", "/quiz/s/abcDEF123", "/login?next=/x", "/a/b?c=d&e=f#frag"):
            with self.subTest(value=value):
                result = forms.validate_student(student(next=value, email="bad"))
                self.assertEqual(result.echo["next"], value)
                self.assertEqual(self.kept(value), value)

    def test_registration_also_refuses_control_format_and_separator_characters_in_next(self):  # B10
        # Each is something the shared rule lets through (it looks only below U+0020 and at DEL),
        # so these show what the registration-specific rule adds.
        added = {"NEL": "/ok\x85x", "right-to-left override": "/ok\u202eevil", "ideographic space": "/ok\u3000x",
                 "space": "/ok x", "no-break space": "/ok\u00a0x", "line separator": "/ok\u2028x",
                 "paragraph separator": "/ok\u2029x", "zero-width space": "/ok\u200bx", "byte order mark": "/ok\ufeffx",
                 "soft hyphen": "/ok\u00adx", "isolate": "/ok\u2066x", "private use": "/ok\ue000x",
                 "tab": "/ok\tx", "trailing space": "/ok "}
        for name, value in added.items():
            with self.subTest(name):
                if name not in ("tab",):
                    self.assertEqual(safe_redirect_target(value, "/"), value)   # shared rule: unchanged, accepts
                self.assertIsNone(self.kept(value))
                self.assertEqual(self.errors(student(next=value)), {})          # dropped, not an error
        # Ordinary internal targets are kept exactly, including letters and percent escapes.
        for value in ("/quiz/s/token_-123", "/учител/тест", "/search?q=a%20b&x=%D0%B0", "/path/with.dots/and-dashes",
                      "/a?b=c&d=e#frag", "/~user/"):
            with self.subTest(value=value):
                self.assertEqual(self.kept(value), value)

    def test_the_shared_redirect_rule_is_not_changed(self):  # B10
        self.assertEqual(safe_redirect_target("/ok\u3000x", "/home"), "/ok\u3000x")
        self.assertEqual(safe_redirect_target("/ok\u202eevil", "/home"), "/ok\u202eevil")
        self.assertEqual(safe_redirect_target("//evil", "/home"), "/home")


class NoPrivilegeFieldsTest(FormTestCase):
    SMUGGLED = {"role": "admin", "account_type": "admin", "is_admin": "1", "is_active": "0", "id": "1",
                "email_verified_at": "2020-01-01 00:00:00", "profile_required_at": "2020-01-01 00:00:00",
                "user_id": "1", "session_key": "x" * 32, "password_hash": "scrypt:x", "state": "activated"}

    def test_no_submitted_field_changes_the_type_or_adds_a_value(self):  # B11
        plain_student = forms.validate_student(student())
        smuggled_student = forms.validate_student(student(**self.SMUGGLED))
        self.assertEqual(smuggled_student.values, plain_student.values)
        self.assertEqual(smuggled_student.values["account_type"], "student")
        plain_teacher = forms.validate_teacher(teacher())
        smuggled_teacher = forms.validate_teacher(teacher(**self.SMUGGLED))
        self.assertEqual(smuggled_teacher.values, plain_teacher.values)
        self.assertEqual(smuggled_teacher.values["account_type"], "teacher")
        for result in (smuggled_student, smuggled_teacher):
            # account_type is set by the function called and is not a copy of the submitted field.
            self.assertFalse((set(self.SMUGGLED) - {"account_type"}) & set(result.values))
            self.assertFalse(set(self.SMUGGLED) & set(result.echo))

    def test_the_account_type_is_the_function_called(self):
        self.assertEqual(forms.validate_student(student(account_type="teacher")).values["account_type"], "student")
        self.assertEqual(forms.validate_teacher(teacher(account_type="student")).values["account_type"], "teacher")

    def test_the_keys_of_a_valid_result_are_exactly_these(self):
        self.assertEqual(set(forms.validate_student(student()).values),
                         {"account_type", "first_name", "last_name", "email", "grade", "next"})
        self.assertEqual(set(forms.validate_teacher(teacher()).values),
                         {"account_type", "first_name", "last_name", "email", "school_name", "subjects", "next"})


class ResultTest(FormTestCase):
    def test_the_password_takes_no_part_in_equality_or_repr(self):
        """Not a serialization test: ``compare=False`` and ``repr=False`` do nothing about that."""
        first = forms.ValidationResult(values={"a": 1}, errors={}, echo={}, password="test-only-one")
        second = forms.ValidationResult(values={"a": 1}, errors={}, echo={}, password="test-only-two")
        self.assertEqual(first, second)
        self.assertNotEqual(first, forms.ValidationResult(values={"a": 2}, errors={}, echo={}, password="test-only-one"))
        for result in (first, second):
            self.assertNotIn("test-only", repr(result))
        doc = forms.ValidationResult.__doc__
        for warning in ("plaintext password", "asdict", "pickle", "log"):
            self.assertIn(warning, doc)
        self.assertIn("do not stop a result from being serialized", " ".join(doc.split()))

    def test_an_invalid_form_has_no_values_and_no_password(self):
        result = forms.validate_student(student(first_name=""))
        self.assertFalse(result.ok)
        self.assertIsNone(result.values)
        self.assertIsNone(result.password)

    def test_every_error_is_a_known_bulgarian_message(self):
        allowed = set(M.values()) | {accounts.password_problem(p) for p in ("", "x" * 257)} \
            | {"Паролата не може да съвпада с имейла."}
        result = forms.validate_teacher({"first_name": "1", "last_name": "", "email": "x", "school_name": "a",
                                         "subject": ["zz"], "password": "x", "confirm_password": "y"})
        self.assertEqual(set(result.errors), {"first_name", "last_name", "email", "school_name", "subject",
                                              "password", "confirm_password"})
        self.assertLessEqual(set(result.errors.values()), allowed)
        for message in result.errors.values():
            self.assertTrue(any("а" <= ch.lower() <= "я" for ch in message), message)  # Bulgarian text

    def test_a_result_is_immutable(self):
        result = forms.validate_student(student())
        with self.assertRaises(Exception):
            result.errors = {}

    def test_the_echo_holds_every_submitted_value_cut_to_its_maximum(self):
        long = "я" * 1000
        result = forms.validate_teacher(teacher(first_name=long, last_name=long, email=long, school_name=long,
                                                 subject=[long, "it", "it", "x", "y"], subject_other=long, next=long))
        self.assertEqual(result.echo["first_name"], "я" * 39)
        self.assertEqual(result.echo["last_name"], "я" * 39)
        self.assertEqual(result.echo["email"], "я" * 254)
        self.assertEqual(result.echo["school_name"], "я" * 120)
        self.assertEqual(result.echo["subject_other"], "я" * 60)
        self.assertEqual(result.echo["next"], "")     # not a path: rejected, so not echoed
        self.assertEqual(len(result.echo["subject"]), 4)
        self.assertTrue(all(len(item) <= 64 for item in result.echo["subject"]))
        self.assertEqual(forms.validate_student(student(grade="12")).echo["grade"], "12")
        self.assertEqual(forms.validate_student(student(grade="9" * 50)).echo["grade"], "99")

    def test_nothing_malformed_raises(self):
        junk = [None, 0, 1.5, True, b"x", [], ["x"], {}, {"a": 1}, object(), "", "\x00" * 5, "\ud800", "я" * 10_000]
        for value in junk:
            for field in ("first_name", "last_name", "email", "grade", "password", "confirm_password", "next",
                          "school_name", "subject", "subject_other"):
                with self.subTest(field=field, value=type(value).__name__):
                    form = dict(teacher(), grade="9")
                    form[field] = value
                    forms.validate_student(form)
                    forms.validate_teacher(form)
        forms.validate_student({})
        forms.validate_teacher({})
        # Nothing was typed, so there is nothing to confirm: one error for the password.
        self.assertEqual(set(forms.validate_student({}).errors),
                         {"first_name", "last_name", "email", "password", "grade"})

    def test_the_hint_text_is_the_approved_one(self):
        self.assertEqual(forms.SCHOOL_HINT, "Име и населено място, напр. СУ „Васил Левски“, Пловдив")


class ModuleTest(unittest.TestCase):
    def test_it_imports_without_flask_or_the_application(self):
        code = (
            "import sys; before = set(sys.modules); import web.services.registration_forms; "
            "print(sorted(m for m in set(sys.modules) - before "
            "if m == 'flask' or m.startswith('flask.') or m == 'web.app' or m.startswith('web.blueprints')))"
        )
        out = subprocess.run([sys.executable, "-B", "-c", code], cwd=ROOT, capture_output=True, text=True, check=True)
        self.assertEqual(out.stdout.strip(), "[]")

    def test_it_opens_no_database_and_reads_no_clock(self):
        source = (ROOT / "web" / "services" / "registration_forms.py").read_text(encoding="utf-8")
        for forbidden in ("sqlite3", "datetime", "import time", "import logging", "getLogger", "print(", "open(",
                          "os.environ", "secrets", "random"):
            self.assertNotIn(forbidden, source, forbidden)


if __name__ == "__main__":
    unittest.main()
