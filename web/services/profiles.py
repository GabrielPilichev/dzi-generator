"""Account profiles: what a person declared at registration
(docs/reviews/EMAIL_REGISTRATION_DESIGN.md, sections 1.4, 6.1, 6.3, 7.3 and 7.4).

Storage and validation of ``user_profiles`` and ``teacher_subjects``, the profile gate
and the one rule that asks a student for a profile (:func:`needs_profile`). Independent
of Flask, ``web.app`` and the blueprints. Every function takes an open ``sqlite3``
connection and never commits or rolls back: the caller owns the transaction. A write
may begin one implicitly, and it stays open when a statement fails or changes no row.
The caller finishes it, and rolls back after any exception: a failure in the middle of
a write can leave the earlier statements of the same call in it.

* **A profile is a declaration, never authorization.** ``users.role`` stays the only
  authority. No function here decides who may do what, and none writes ``role``,
  ``email``, ``email_verified_at``, ``is_active``, ``session_key`` or the mark
  ``profile_required_at``. A mismatch between a profile's ``account_type`` and the
  account's role is possible and means nothing: the profile keeps the type it was
  declared with, whatever the role becomes.
* **Who is asked.** :func:`needs_profile` is the only reader of ``users`` here, through
  ``accounts.profile_requirement``, and the only thing that reads the mark. It can send
  a student without a profile to the profile form before a NEW attempt; it never blocks
  anything else.
* **Nothing is repaired.** Values must already be in the cleaned form the validators of
  ``web.services.registration_forms`` produce; a value that would be changed by
  cleaning is refused. Everything is validated before the first write.
* **Subjects follow the registry.** A subject is an id of ``src.subject_registry``
  with that subject's title as its label, or a free-text „Друг предмет“ with no id.
  Order is kept (positions 1 to 3). A subject, and the free-text entry, cannot repeat.
* ``users.display_name`` is written through ``accounts.set_display_name``, and only by
  :func:`complete_student_profile` and :func:`save_profile`, which do it in the caller's
  one transaction together with the profile. ``display_name`` builds the string
  (design 1.4).
* **The gate.** ``PRODUCTION_ACTIVATION`` is the build's profile gate (design 7.3, D14).
  While it is off a production start keeps ``/account/profile``, its link, the redirect
  after Google onboarding, the new-attempt check and the writing of the mark switched off
  whatever any setting holds. A development run follows ``DEVELOPMENT_ENV`` instead.
  Either way the result is ``app.config[CONFIG_KEY]``, which callers read with
  :func:`enabled_in` and pass to :func:`needs_profile`; nothing here reads the
  environment. A mark written while the gate was on stays stored and is ignored while it
  is off.

Names, schools and subjects are never logged or put into exception messages.
"""

from __future__ import annotations

import sqlite3
import unicodedata
from datetime import datetime, timezone
from typing import Any, Iterable

from src import subject_registry
from web.services import accounts
from web.services import registration_forms as forms

ACCOUNT_TYPES = (forms.STUDENT, forms.TEACHER)

# The build's profile gate (design 7.3, decision D14). While it is False a production
# start keeps the whole profile flow off. No setting and no environment variable
# overrides it: a deploy changes it, and nothing else in the code assigns it.
PRODUCTION_ACTIVATION = False

# Where the effective gate of a running application is kept (``app.config``), and the
# development-only setting that turns it on in a development run. ``web.production``
# never reads the setting: it copies ``PRODUCTION_ACTIVATION`` into the configuration.
CONFIG_KEY = "PROFILES_ENABLED"
DEVELOPMENT_ENV = "DZI_PROFILES_ENABLED"

_UNSET: Any = object()

_PROFILE_COLUMNS = (
    "user_id, account_type, first_name, last_name, grade, grade_set_at, school_name, "
    "created_at, updated_at"
)


class ProfileExists(Exception):
    """The account already has a profile. Nothing was written and the stored row is unchanged."""


def _is_row_id(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 0 < value < 2**63


def _timestamp(moment) -> str:
    """SQLite's CURRENT_TIMESTAMP layout in UTC, so stored values compare as text."""
    if not isinstance(moment, datetime) or moment.tzinfo is None:
        raise ValueError("Невалидно време.")
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def display_name(first_name: str, last_name: str) -> str:
    """``"<first_name> <last_name>"``, the ``users.display_name`` of a registered person.

    Two names of at most 39 characters give 3 to 79, inside the 2 to 80 of ``users``.
    """
    first = _name(first_name, "first_name")
    last = _name(last_name, "last_name")
    return f"{first} {last}"


# ---------------------------------------------------------------------------- validation

def _name(value, field: str) -> str:
    cleaned, _ = forms.clean_name(value)
    if cleaned is None or cleaned != value:
        raise ValueError("Невалидно име." if field == "first_name" else "Невалидна фамилия.")
    return cleaned


def _grade(value) -> int:
    if type(value) is not int or forms.clean_grade(value) is None:
        raise ValueError("Невалиден клас.")
    return value


def _school(value) -> str:
    cleaned = forms.clean_school(value)
    if cleaned is None or cleaned != value:
        raise ValueError("Невалидно училище.")
    return cleaned


def _is_storable_label(text) -> bool:
    """Text the ``teacher_subjects.label`` column takes: cleaned, 2 to 60 characters.

    Cleaned means stripped, with no run of whitespace and no control, format or other
    ``C`` character. Applied to a registry title as well as to „Друг предмет“, so a
    title the table would refuse is found before the first write, not by its CHECK
    halfway through one.
    """
    return (
        isinstance(text, str)
        and forms.MIN_SUBJECT_OTHER_LENGTH <= len(text) <= forms.MAX_SUBJECT_OTHER_LENGTH
        and " ".join(text.split()) == text
        and not any(unicodedata.category(ch).startswith("C") for ch in text)
    )


def _subjects(value) -> list[dict[str, Any]]:
    """Subjects as ``{"subject_id", "label"}`` dicts, checked against the registry."""
    if isinstance(value, (str, bytes, dict)) or not isinstance(value, Iterable):
        raise ValueError("Невалидни предмети.")
    items = list(value)
    if not forms.MIN_SUBJECTS <= len(items) <= forms.MAX_SUBJECTS:
        raise ValueError("Невалиден брой предмети.")
    seen: set[str | None] = set()
    checked: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict) or set(item) != {"subject_id", "label"}:
            raise ValueError("Невалиден предмет.")
        subject_id, label = item["subject_id"], item["label"]
        if (subject_id is not None and not isinstance(subject_id, str)) or not isinstance(label, str):
            raise ValueError("Невалиден предмет.")
        if subject_id in seen:
            raise ValueError("Повтарящ се предмет.")
        seen.add(subject_id)
        if subject_id is None:
            if forms.clean_subject_other(label) != label:
                raise ValueError("Невалиден друг предмет.")
        else:
            subject = subject_registry.get_subject(subject_id)
            if subject is None or label != subject.title:
                raise ValueError("Непознат предмет.")
            if not _is_storable_label(subject.title):
                raise ValueError("Невалиден предмет.")
        checked.append({"subject_id": subject_id, "label": label})
    return checked


def _rows(cursor: sqlite3.Cursor) -> list[dict]:
    columns = [col[0] for col in cursor.description]
    return [dict(zip(columns, tuple(row))) for row in cursor.fetchall()]


def _insert_subjects(conn: sqlite3.Connection, user_id: int, subjects: list[dict[str, Any]]) -> None:
    for position, subject in enumerate(subjects, start=1):
        conn.execute(
            "INSERT INTO teacher_subjects (user_id, position, subject_id, label) VALUES (?, ?, ?, ?)",
            (user_id, position, subject["subject_id"], subject["label"]),
        )


# ---------------------------------------------------------------------------- reads

def get_profile(conn: sqlite3.Connection, user_id) -> dict | None:
    """The profile of one account, or ``None`` for no profile or a malformed id.

    A teacher's ``subjects`` hold ``position``, ``subject_id`` and ``label`` in order; a
    student's list is empty.
    """
    if not _is_row_id(user_id):
        return None
    found = _rows(conn.execute(f"SELECT {_PROFILE_COLUMNS} FROM user_profiles WHERE user_id = ?", (user_id,)))
    if not found:
        return None
    profile = found[0]
    profile["subjects"] = _rows(conn.execute(
        "SELECT position, subject_id, label FROM teacher_subjects WHERE user_id = ? ORDER BY position",
        (user_id,),
    ))
    return profile


def declared_types(conn: sqlite3.Connection) -> dict[int, str]:
    """``{user_id: account_type}`` of every profile, for the operator's account list.

    Empty when the database predates the profile storage (migration 021). The declared
    type is shown, never used for access: ``users.role`` stays the only authority.
    """
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'user_profiles'").fetchone() is None:
        return {}
    return {row[0]: row[1] for row in conn.execute("SELECT user_id, account_type FROM user_profiles")}


# ---------------------------------------------------------------------------- writes

def create_profile(
    conn: sqlite3.Connection,
    user_id: int,
    *,
    account_type: str,
    first_name: str,
    last_name: str,
    grade: int | None = None,
    school_name: str | None = None,
    subjects: Iterable[dict[str, Any]] | None = None,
    now: datetime,
) -> dict:
    """Store the profile of an existing account and return it as ``get_profile`` does.

    A student needs a ``grade`` (8 to 12) and takes no school and no subjects. A teacher
    needs a ``school_name`` and one to three ``subjects`` and takes no grade. ``now`` is
    a timezone-aware moment: it dates the profile and, for a student, the grade.

    ``ValueError`` for malformed input, ``LookupError`` when the account does not exist,
    :class:`ProfileExists` when it already has a profile. Whether this account should
    have a profile, and what its role allows, is the caller's decision, not this
    function's.
    """
    if not _is_row_id(user_id):
        raise ValueError("Невалиден потребител.")
    if account_type not in ACCOUNT_TYPES:
        raise ValueError("Невалиден вид акаунт.")
    first, last = _name(first_name, "first_name"), _name(last_name, "last_name")
    stamp = _timestamp(now)
    if account_type == forms.STUDENT:
        if school_name is not None or subjects is not None:
            raise ValueError("Ученик няма училище и предмети.")
        checked_grade, checked_school, checked_subjects = _grade(grade), None, []
    else:
        if grade is not None:
            raise ValueError("Учител няма клас.")
        checked_grade, checked_school = None, _school(school_name)
        checked_subjects = _subjects(subjects if subjects is not None else ())
    # Checked here, not left to the foreign key: that is enforced only on a connection
    # that turned foreign_keys on.
    if conn.execute("SELECT 1 FROM users WHERE id = ?", (user_id,)).fetchone() is None:
        raise LookupError(f"Няма потребител с id {user_id}.")
    try:
        conn.execute(
            "INSERT INTO user_profiles (user_id, account_type, first_name, last_name, grade, "
            "grade_set_at, school_name, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (user_id, account_type, first, last, checked_grade,
             stamp if checked_grade is not None else None, checked_school, stamp, stamp),
        )
    except sqlite3.IntegrityError as exc:
        if conn.execute("SELECT 1 FROM user_profiles WHERE user_id = ?", (user_id,)).fetchone() is not None:
            raise ProfileExists("Акаунтът вече има профил.") from exc
        raise
    _insert_subjects(conn, user_id, checked_subjects)
    return get_profile(conn, user_id)


def update_profile(
    conn: sqlite3.Connection,
    user_id: int,
    *,
    first_name: str = _UNSET,
    last_name: str = _UNSET,
    grade: int = _UNSET,
    school_name: str = _UNSET,
    subjects: Iterable[dict[str, Any]] = _UNSET,
    now: datetime,
) -> dict:
    """Change the fields given, leaving every other as it is, and return the profile.

    The profile's ``account_type`` is fixed: a student's profile takes ``grade`` and
    refuses ``school_name`` and ``subjects``; a teacher's takes those two and refuses
    ``grade``. ``subjects`` replaces the whole list. ``grade_set_at`` is moved to ``now``
    only when the grade changes; there is no automatic advancement (D2). ``LookupError``
    when the account has no profile, ``ValueError`` for malformed input or no field.
    """
    if not _is_row_id(user_id):
        raise ValueError("Невалиден потребител.")
    changes: dict[str, Any] = {}
    if first_name is not _UNSET:
        changes["first_name"] = _name(first_name, "first_name")
    if last_name is not _UNSET:
        changes["last_name"] = _name(last_name, "last_name")
    if grade is not _UNSET:
        changes["grade"] = _grade(grade)
    if school_name is not _UNSET:
        changes["school_name"] = _school(school_name)
    checked_subjects = _subjects(subjects) if subjects is not _UNSET else None
    if not changes and checked_subjects is None:
        raise ValueError("Няма промени.")
    stamp = _timestamp(now)
    stored = get_profile(conn, user_id)
    if stored is None:
        raise LookupError(f"Няма профил за потребител {user_id}.")
    if stored["account_type"] == forms.STUDENT and ("school_name" in changes or checked_subjects is not None):
        raise ValueError("Ученик няма училище и предмети.")
    if stored["account_type"] == forms.TEACHER and "grade" in changes:
        raise ValueError("Учител няма клас.")
    if "grade" in changes and changes["grade"] != stored["grade"]:
        changes["grade_set_at"] = stamp
    changes["updated_at"] = stamp
    assignments = ", ".join(f"{column} = ?" for column in changes)
    cursor = conn.execute(
        f"UPDATE user_profiles SET {assignments} WHERE user_id = ?", (*changes.values(), user_id)
    )
    if cursor.rowcount != 1:
        raise LookupError(f"Няма профил за потребител {user_id}.")
    if checked_subjects is not None:
        conn.execute("DELETE FROM teacher_subjects WHERE user_id = ?", (user_id,))
        _insert_subjects(conn, user_id, checked_subjects)
    return get_profile(conn, user_id)


# ---------------------------------------------------------------------------- the gate and who is asked

def enabled_in(config) -> bool:
    """Whether the profile gate is on in an application configuration.

    ``True`` only for the value ``True`` under :data:`CONFIG_KEY`: a missing key, ``None``
    and any other value are off, so a configuration that never set it behaves as before
    this feature.
    """
    return config.get(CONFIG_KEY) is True


def needs_profile(conn: sqlite3.Connection, user, *, enabled: bool) -> bool:
    """Whether this account must give a profile before a NEW attempt (design 7.4).

    True only when all four hold: the gate is on (``enabled``); the account's **current**
    role is ``student``; ``profile_required_at`` is set; and no ``user_profiles`` row of
    any type exists. ``user`` identifies the account (its ``id``); the role and the mark
    are read from the row as it is now, under the caller's transaction, never from what a
    request loaded earlier. Read-only, and it writes, deletes and reinterprets nothing: a
    role change, a profile of the other type or a mark left from a time the gate was on
    are all read, never repaired.
    """
    if enabled is not True or not isinstance(user, dict):
        return False
    state = accounts.profile_requirement(conn, user.get("id"))
    if state is None or state["role"] != "student":
        return False
    mark = state["profile_required_at"]
    if not isinstance(mark, str) or not mark:
        return False
    return conn.execute("SELECT 1 FROM user_profiles WHERE user_id = ?", (user["id"],)).fetchone() is None


# ---------------------------------------------------------------------------- profile pages

def complete_student_profile(
    conn: sqlite3.Connection,
    user_id: int,
    *,
    first_name: str,
    last_name: str,
    grade: int,
    now: datetime,
) -> dict:
    """Store a student profile and set ``display_name`` to the two names, in the caller's
    one transaction, and return the profile.

    The two writes belong together: the caller commits both or rolls both back, so an
    account never has a profile whose names differ from what its attempts will copy.
    Nothing else of ``users`` is written; the session key is not rotated. Whether this
    account may have a student profile (its role is ``student`` and it has none) is the
    caller's decision. Errors as :func:`create_profile`, and as
    ``accounts.set_display_name``.
    """
    profile = create_profile(
        conn, user_id, account_type=forms.STUDENT, first_name=first_name, last_name=last_name,
        grade=grade, now=now,
    )
    accounts.set_display_name(conn, user_id, display_name(profile["first_name"], profile["last_name"]))
    return profile


def save_profile(
    conn: sqlite3.Connection,
    user_id: int,
    *,
    first_name: str,
    last_name: str,
    grade: int = _UNSET,
    school_name: str = _UNSET,
    subjects: Iterable[dict[str, Any]] = _UNSET,
    now: datetime,
) -> dict:
    """Edit the profile the account already has, and return it.

    The fields are those of the profile's stored ``account_type`` (``update_profile``
    refuses the other type's), whatever the account's role is. ``display_name`` is set to
    the two names only when they changed, in the same caller transaction; an operator's
    later rename is not overwritten by an edit that left the names alone. Nothing else of
    ``users`` is written. ``LookupError`` when the account has no profile.
    """
    before = get_profile(conn, user_id)
    if before is None:
        raise LookupError(f"Няма профил за потребител {user_id}.")
    fields: dict[str, Any] = {"first_name": first_name, "last_name": last_name}
    for name, value in (("grade", grade), ("school_name", school_name), ("subjects", subjects)):
        if value is not _UNSET:
            fields[name] = value
    profile = update_profile(conn, user_id, now=now, **fields)
    if (profile["first_name"], profile["last_name"]) != (before["first_name"], before["last_name"]):
        accounts.set_display_name(conn, user_id, display_name(profile["first_name"], profile["last_name"]))
    return profile
