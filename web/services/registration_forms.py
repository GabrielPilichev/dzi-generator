"""Registration form validation (docs/reviews/EMAIL_REGISTRATION_DESIGN.md, section 1).

Pure functions over a submitted form: no database, no request, no clock and no mail.
Independent of Flask, ``web.app`` and the blueprints. ``validate_student`` and
``validate_teacher`` take the submitted mapping (a ``dict`` or a Werkzeug ``MultiDict``)
and return a :class:`ValidationResult`; ``public_email`` is the email rule on its own.

* **Passwords are never altered.** ``password`` and ``confirm_password`` are measured,
  compared and returned exactly as received: not stripped, not collapsed, not
  Unicode-normalised, not truncated. They are never put in an error, in the echo or in
  the ``repr`` of a result.
* **Names, school and „Друг предмет“** are stripped and have their inner whitespace
  collapsed to single spaces before they are measured. Nothing else is normalised.
* **The email** goes through ``accounts.normalize_email`` (the form sign-in uses) and
  then through the narrower public rule of section 1.3. Existing accounts and sign-in
  are not affected by that rule.
* **The account type is the function called**, never a submitted value. A field named
  ``role``, ``account_type``, ``is_admin``, ``email_verified_at`` or
  ``profile_required_at`` is not read.
* **Subjects** are ids of ``src.subject_registry`` or ``other``; the registry is read
  at call time.
* **``next``** is kept only if ``web.redirects.safe_redirect_target`` accepts it as it
  is, and is dropped otherwise, never repaired.

Errors are Bulgarian and never contain a submitted value.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Mapping

from src import subject_registry
from web.services import accounts

STUDENT = "student"
TEACHER = "teacher"

# Subject form value for „Друг предмет“.
OTHER_SUBJECT = "other"

GRADES = (8, 9, 10, 11, 12)

MAX_NAME_LENGTH = 39
MIN_SCHOOL_LENGTH = 3
MAX_SCHOOL_LENGTH = 120
MIN_SUBJECTS = 1
MAX_SUBJECTS = 3
MIN_SUBJECT_OTHER_LENGTH = 2
MAX_SUBJECT_OTHER_LENGTH = 60
MAX_NEXT_LENGTH = 512
MAX_SUBJECT_ID_LENGTH = 64

# Longest address the public rule accepts, as ``accounts`` bounds it.
MAX_EMAIL_LENGTH = accounts.MAX_EMAIL_LENGTH
MAX_LOCAL_PART_LENGTH = 64

SCHOOL_HINT = "Име и населено място, напр. СУ „Васил Левски“, Пловдив"

MESSAGES = {
    "first_name_empty": "Въведи име.",
    "last_name_empty": "Въведи фамилия.",
    "name_characters": "Името може да съдържа само букви, интервал, тире и апостроф.",
    "name_too_long": f"Името трябва да е най-много {MAX_NAME_LENGTH} символа.",
    "email": "Невалиден имейл.",
    "grade": "Избери клас.",
    "school": f"Въведи име на училището (от {MIN_SCHOOL_LENGTH} до {MAX_SCHOOL_LENGTH} символа).",
    "subjects_none": "Избери поне един предмет.",
    "subjects_many": f"Избери най-много {MAX_SUBJECTS} предмета.",
    "subject_other": "Въведи името на предмета.",
    # Not in the table of design 1.2: an id outside the list, a repeated one, or a
    # value that is not text.
    "subject_unknown": "Избери предмети от списъка.",
    "password_mismatch": "Паролите не съвпадат.",
}

_NAME_PUNCTUATION = frozenset(" -'’.")
_SUBJECT_OTHER_PUNCTUATION = frozenset(" -")

_LOCAL_PART = re.compile(r"[a-z0-9._%+-]+")
_DOMAIN_LABEL = re.compile(r"[a-z0-9-]+")
_TLD = re.compile(r"[a-z]{2,}")


@dataclass(frozen=True)
class ValidationResult:
    """The outcome of one form.

    ``values`` holds the cleaned fields when ``ok`` and is ``None`` otherwise; it never
    holds a password. ``password`` is the exact string received, kept out of ``repr``
    and ``None`` unless the form is valid. ``errors`` maps a field name to one Bulgarian
    message. ``echo`` is what to put back into a re-rendered form: every submitted
    value except the passwords, each cut to its maximum length.
    """

    values: dict[str, Any] | None
    errors: dict[str, str]
    echo: dict[str, Any]
    password: str | None = field(default=None, repr=False)

    @property
    def ok(self) -> bool:
        return not self.errors


# ---------------------------------------------------------------------------- helpers

def _multi(form: Mapping, name: str) -> list:
    getter = getattr(form, "getlist", None)
    if getter is not None:
        return list(getter(name))
    value = form.get(name)
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


def _collapse(value: str) -> str:
    """Strip and collapse inner whitespace to single spaces; nothing else changes."""
    return " ".join(value.split())


def _has_forbidden_character(value: str) -> bool:
    """Control, format, surrogate, private-use or unassigned characters (category C*)."""
    return any(unicodedata.category(ch).startswith("C") for ch in value)


def _has_letter(value: str) -> bool:
    return any(unicodedata.category(ch).startswith("L") for ch in value)


def _echo_text(value, limit: int) -> str:
    return value[:limit] if isinstance(value, str) else ""


# ---------------------------------------------------------------------------- fields

def public_email(value) -> str:
    """The stored form of an email under the public rule, or ``ValueError``.

    ``accounts.normalize_email`` first (NFKC, trim, lower-case), then, on its result:
    ASCII only; a local part of ``a-z 0-9 . _ % + -``, 1 to 64 characters, with no
    leading, trailing or doubled dot; a domain of at least two labels of ``a-z 0-9 -``
    whose last label is at least two letters. A carriage return or line feed anywhere
    in the input is refused, even where ``normalize_email`` would trim it, and inner
    whitespace is refused, never collapsed.
    """
    problem = ValueError(MESSAGES["email"])
    if not isinstance(value, str) or "\r" in value or "\n" in value:
        raise problem
    try:
        email = accounts.normalize_email(value)
    except ValueError:
        raise problem from None
    if not email.isascii():
        raise problem
    local, domain = email.split("@")
    if (
        not 1 <= len(local) <= MAX_LOCAL_PART_LENGTH
        or not _LOCAL_PART.fullmatch(local)
        or local.startswith(".")
        or local.endswith(".")
        or ".." in local
    ):
        raise problem
    labels = domain.split(".")
    if (
        len(labels) < 2
        or not all(_DOMAIN_LABEL.fullmatch(label) for label in labels)
        or not _TLD.fullmatch(labels[-1])
    ):
        raise problem
    return email


def clean_name(value, empty_message: str = MESSAGES["first_name_empty"]) -> tuple[str | None, str | None]:
    """``(name, None)`` or ``(None, message)``: stripped, collapsed, 1 to 39 allowed characters."""
    if value is None:
        return None, empty_message
    if not isinstance(value, str):
        return None, MESSAGES["name_characters"]
    name = _collapse(value)
    if not name:
        return None, empty_message
    if (
        not _has_letter(name)
        or any(
            not (unicodedata.category(ch)[0] in "LM" or ch in _NAME_PUNCTUATION)
            for ch in name
        )
    ):
        return None, MESSAGES["name_characters"]
    if len(name) > MAX_NAME_LENGTH:
        return None, MESSAGES["name_too_long"]
    return name, None


def clean_grade(value) -> int | None:
    """8 to 12 as an ``int`` or as exactly ``"8"`` to ``"12"``; anything else is ``None``."""
    if type(value) is int:
        return value if value in GRADES else None
    if isinstance(value, str):
        for grade in GRADES:
            if value == str(grade):
                return grade
    return None


def clean_school(value) -> str | None:
    """The collapsed school name, or ``None`` when it breaks the rule of design 1.1."""
    if not isinstance(value, str):
        return None
    school = _collapse(value)
    if (
        not MIN_SCHOOL_LENGTH <= len(school) <= MAX_SCHOOL_LENGTH
        or _has_forbidden_character(school)
        or not _has_letter(school)
    ):
        return None
    return school


def clean_subject_other(value) -> str | None:
    """The collapsed „Друг предмет“ text (2 to 60 letters, spaces, hyphens), or ``None``."""
    if not isinstance(value, str):
        return None
    text = _collapse(value)
    if (
        not MIN_SUBJECT_OTHER_LENGTH <= len(text) <= MAX_SUBJECT_OTHER_LENGTH
        or not _has_letter(text)
        or any(not (unicodedata.category(ch).startswith("L") or ch in _SUBJECT_OTHER_PUNCTUATION)
               for ch in text)
    ):
        return None
    return text


def _clean_subjects(form: Mapping, errors: dict[str, str]) -> list[dict[str, Any]] | None:
    """Subjects as ``{"subject_id": id | None, "label": text}`` in submitted order."""
    submitted = _multi(form, "subject")
    if not submitted:
        errors["subject"] = MESSAGES["subjects_none"]
        return None
    if len(submitted) > MAX_SUBJECTS:
        errors["subject"] = MESSAGES["subjects_many"]
        return None
    registry = {subject.id: subject for subject in subject_registry.all_subjects()}
    seen: set[str] = set()
    subjects: list[dict[str, Any]] = []
    other_text = form.get("subject_other")
    for value in submitted:
        if not isinstance(value, str) or value in seen or (value != OTHER_SUBJECT and value not in registry):
            errors["subject"] = MESSAGES["subject_unknown"]
            return None
        seen.add(value)
        if value == OTHER_SUBJECT:
            label = clean_subject_other(other_text)
            if label is None:
                errors["subject_other"] = MESSAGES["subject_other"]
                return None
            subjects.append({"subject_id": None, "label": label})
        else:
            subjects.append({"subject_id": value, "label": registry[value].title})
    return subjects


def _accepted_next(value) -> str | None:
    """``value`` itself if ``safe_redirect_target`` accepts it as it is, else ``None``."""
    if not isinstance(value, str) or not value or len(value) > MAX_NEXT_LENGTH:
        return None
    # Imported here so this module imports without Flask; the rule has one definition.
    # A fallback that starts with "/" needs no application context.
    from web.redirects import safe_redirect_target

    return value if safe_redirect_target(value, "/") == value else None


# ---------------------------------------------------------------------------- forms

def _common(form: Mapping, errors: dict[str, str], echo: dict[str, Any]) -> dict[str, Any]:
    """Fields both forms share. Fills ``errors`` and ``echo``; returns cleaned values."""
    values: dict[str, Any] = {}
    first_name, first_error = clean_name(form.get("first_name"), MESSAGES["first_name_empty"])
    last_name, last_error = clean_name(form.get("last_name"), MESSAGES["last_name_empty"])
    if first_error:
        errors["first_name"] = first_error
    if last_error:
        errors["last_name"] = last_error
    values["first_name"], values["last_name"] = first_name, last_name
    for name in ("first_name", "last_name"):
        echo[name] = _echo_text(form.get(name), MAX_NAME_LENGTH)

    submitted_email = form.get("email")
    echo["email"] = _echo_text(submitted_email, MAX_EMAIL_LENGTH)
    email = None
    try:
        email = public_email(submitted_email)
    except ValueError as exc:
        errors["email"] = str(exc)
    values["email"] = email

    password = form.get("password")
    confirmation = form.get("confirm_password")
    problem = accounts.password_problem(password if password is not None else "", email)
    if problem:
        errors["password"] = problem
    # A password that is not text has its own error; the confirmation is judged only against text.
    if isinstance(password, str) and password != confirmation:
        errors["confirm_password"] = MESSAGES["password_mismatch"]

    echo["next"] = _echo_text(form.get("next"), MAX_NEXT_LENGTH)
    values["next"] = _accepted_next(form.get("next"))
    return values


def _result(values: dict[str, Any], errors: dict[str, str], echo: dict[str, Any], form: Mapping) -> ValidationResult:
    if errors:
        return ValidationResult(values=None, errors=errors, echo=echo)
    return ValidationResult(values=values, errors={}, echo=echo, password=form.get("password"))


def validate_student(form: Mapping) -> ValidationResult:
    """Validate the student registration form (design 1.1)."""
    errors: dict[str, str] = {}
    echo: dict[str, Any] = {}
    values = _common(form, errors, echo)
    values["account_type"] = STUDENT

    submitted_grade = form.get("grade")
    echo["grade"] = _echo_text(str(submitted_grade) if type(submitted_grade) is int else submitted_grade, 2)
    grade = clean_grade(submitted_grade)
    if grade is None:
        errors["grade"] = MESSAGES["grade"]
    values["grade"] = grade
    return _result(values, errors, echo, form)


def validate_teacher(form: Mapping) -> ValidationResult:
    """Validate the teacher registration form (design 1.1).

    ``subject_other`` is read only when ``other`` is among the subjects; a text sent
    without it is ignored (still echoed).
    """
    errors: dict[str, str] = {}
    echo: dict[str, Any] = {}
    values = _common(form, errors, echo)
    values["account_type"] = TEACHER

    submitted_school = form.get("school_name")
    echo["school_name"] = _echo_text(submitted_school, MAX_SCHOOL_LENGTH)
    school = clean_school(submitted_school)
    if school is None:
        errors["school_name"] = MESSAGES["school"]
    values["school_name"] = school

    echo["subject"] = [_echo_text(item, MAX_SUBJECT_ID_LENGTH) for item in _multi(form, "subject")[:MAX_SUBJECTS + 1]]
    echo["subject_other"] = _echo_text(form.get("subject_other"), MAX_SUBJECT_OTHER_LENGTH)
    values["subjects"] = _clean_subjects(form, errors)
    return _result(values, errors, echo, form)
