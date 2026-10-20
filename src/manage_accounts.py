#!/usr/bin/env python3
"""Operator CLI for LearnPilot accounts (Accounts PR 2).

Server-side tooling only: it is the one place an admin can be created or promoted.
The target database must be named explicitly (``--db`` or ``DZI_DB``); it is never
defaulted to ``data/questions.db``, never created, and never migrated.

    python3 -m src.manage_accounts --db "$DZI_DB" create --email a@b.bg --name "Ана" --role teacher
    python3 -m src.manage_accounts --db "$DZI_DB" list

Passwords are read with ``getpass`` (twice) or, for automation, ``--password-stdin``
(exactly one line). They are never accepted from argv or the environment.

``disable``, ``enable``, ``sign-out`` and ``set-role`` select the account by exactly
one of ``--email`` and ``--id``: an account created through Google sign-in has no
email, and ``list`` shows its id. The other commands act on a password or on an
admin's second factor, which such an account does not have, and keep ``--email``.
Every message names the account by its id.

Admin TOTP (migration 016) is enrolled only here: ``totp-enroll`` prints the
``otpauth://`` provisioning URI once, ``totp-confirm`` activates the credential with a
current code and prints the recovery codes once, ``totp-recovery-codes`` replaces
them, and ``totp-reset`` removes the credential. Enrolment and confirmation need
``DZI_TOTP_KEY``; no other command prints a secret.

``cutover-check`` is the read-only deployment gate of account-only sign-in
(introduced with the Accounts PR9 cutover): it reports PASS only if at least one
admin account can complete a password + second-factor sign-in with the configured
``DZI_TOTP_KEY``. It changes nothing and prints no secret, code or hash. It
inspects only a self-contained
rollback-journal database without sidecar files, i.e. a snapshot made by
``src.backup_sqlite``: SQLite cannot read a WAL-mode database without creating or
changing its ``-wal``/``-shm`` files, so the live database is refused before it is
opened.

    python3 -m src.backup_sqlite --db "$DZI_DB" --output /var/backups/learnpilot/cutover-check.db
    python3 -m src.manage_accounts --db /var/backups/learnpilot/cutover-check.db cutover-check
"""

from __future__ import annotations

import argparse
import getpass
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    from web.services import accounts, identities, login_throttle, profiles, registration, recovery_scrub, totp
except ModuleNotFoundError:  # run as `python3 src/manage_accounts.py`
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from web.services import accounts, identities, login_throttle, profiles, registration, recovery_scrub, totp

ADMIN_MIN_PASSWORD_LENGTH = accounts.ADMIN_MIN_PASSWORD_LENGTH
MAX_STDIN_BYTES = 4096
# The application's secret key: login-throttle rows are keyed with it.
SECRET_KEY_ENV = "DZI_SECRET_KEY"
# Encrypts admin TOTP secrets; must be the key the running application uses.
TOTP_KEY_ENV = totp.TOTP_KEY_ENV
MAX_CODE_INPUT_BYTES = 256
REQUIRED_USER_COLUMNS = {
    "id", "email", "display_name", "role", "password_hash", "is_active", "session_key",
    "password_changed_at", "last_login_at", "created_at", "updated_at",
}


# Tables account sign-in needs (migrations 012, 013 and 016).
CUTOVER_REQUIRED_TABLES = ("users", "login_throttle", "totp_credentials", "totp_recovery_codes")
# Commands that open the database read-only.
READ_ONLY_COMMANDS = frozenset({"cutover-check"})
SQLITE_HEADER_MAGIC = b"SQLite format 3\x00"
# Header bytes 18/19 (file format write/read version): 1 is a rollback journal, 2 is WAL.
SQLITE_ROLLBACK_JOURNAL_FORMAT = 1
SQLITE_WAL_FORMAT = 2
SQLITE_SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")


class OperatorError(Exception):
    """An expected operator/input/database problem: reported without a traceback."""


class CheckFailed(Exception):
    """A preflight check did not pass; the message is its complete report."""


def account_id(text: str) -> int:
    """argparse type of ``--id``: a positive decimal integer."""
    if not (text.isascii() and text.isdigit()) or not 0 < int(text) < 2**63:
        raise argparse.ArgumentTypeError("expected a positive account id (see the list command)")
    return int(text)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage LearnPilot accounts (operator tooling).")
    parser.add_argument("--db", help="SQLite database path (or set DZI_DB). Never defaulted.")
    sub = parser.add_subparsers(dest="command", required=True)

    def with_password_flag(p):
        p.add_argument(
            "--password-stdin",
            action="store_true",
            help="Read the password as exactly one line from stdin instead of prompting.",
        )

    create = sub.add_parser("create", help="Create an account with a local password.")
    create.add_argument("--email", required=True)
    create.add_argument("--name", required=True, help="Display name.")
    create.add_argument("--role", choices=accounts.ROLES, default="student")
    with_password_flag(create)

    set_password = sub.add_parser("set-password", help="Set a password and sign the account out everywhere.")
    set_password.add_argument("--email", required=True)
    with_password_flag(set_password)

    def with_account_selector(p):
        # Exactly one of the two: an account created through Google has no email.
        selector = p.add_mutually_exclusive_group(required=True)
        selector.add_argument("--email", help="The account's sign-in email.")
        selector.add_argument("--id", type=account_id, metavar="N",
                              help="The account's id, as the list command shows it.")

    set_role = sub.add_parser("set-role", help="Change a role (the only way to grant admin).")
    with_account_selector(set_role)
    set_role.add_argument("--role", required=True, choices=accounts.ROLES)

    for name, text in (
        ("disable", "Disable an account."),
        ("enable", "Enable an account."),
        ("sign-out", "Invalidate all existing sessions of an account."),
    ):
        with_account_selector(sub.add_parser(name, help=text))

    sub.add_parser(
        "clear-throttle",
        help=f"Lift the login lockout of an email (needs the application's {SECRET_KEY_ENV}).",
    ).add_argument("--email", required=True)

    sub.add_parser(
        "totp-enroll",
        help=f"Start admin TOTP enrolment and print the provisioning URI once (needs {TOTP_KEY_ENV}).",
    ).add_argument("--email", required=True)

    totp_confirm = sub.add_parser(
        "totp-confirm",
        help=f"Activate TOTP with a current code and print recovery codes once (needs {TOTP_KEY_ENV}).",
    )
    totp_confirm.add_argument("--email", required=True)
    totp_confirm.add_argument(
        "--code-stdin",
        action="store_true",
        help="Read the code as exactly one line from stdin instead of prompting.",
    )

    sub.add_parser(
        "totp-recovery-codes",
        help="Replace all recovery codes of a confirmed TOTP and print the new ones once.",
    ).add_argument("--email", required=True)

    sub.add_parser(
        "totp-reset",
        help="Remove TOTP and its recovery codes and sign the account out everywhere.",
    ).add_argument("--email", required=True)

    sub.add_parser(
        "cutover-check",
        help=(
            "Read-only preflight for account-only sign-in: PASS only if an "
            f"admin can sign in with password + TOTP/recovery code (needs {TOTP_KEY_ENV}). "
            "Run it on a src.backup_sqlite snapshot, never on the live WAL database."
        ),
    )

    sub.add_parser("list", help="List accounts (no secrets).")

    diff = sub.add_parser(
        "restore-credential-diff",
        help="Compare prepared offline credential snapshots read-only; --db is stat'ed only. "
             "Needs no secret. See DEPLOYMENT section 37 for preparation and mandatory isolation.")
    diff.add_argument("--snapshot", required=True, type=Path)
    diff.add_argument("--replaced", required=True, type=Path,
                      help="Prepared replaced evidence; an unavailable path makes all accounts uncertain.")
    diff.add_argument("--snapshot-sha256")
    diff.add_argument("--replaced-sha256")

    # Registration operations (docs/reviews/EMAIL_REGISTRATION_DESIGN.md, 3.8, 6.5, 6.6).
    cleanup = sub.add_parser(
        "registrations-cleanup",
        help="Expire and scrub overdue registrations and recoveries; delete old final and ledger rows. "
             "Needs no secret; refuses while a restore of the database is in progress.",
    )
    cleanup.add_argument(
        "--check", action="store_true",
        help="Change nothing; fail when a pending registration or recovery is older than an hour.")
    sub.add_parser(
        "registrations-clock-accept",
        help="After checking the server's time, set the registration high-water mark to the present "
             "(ends a 'clock behind' refusal).",
    )
    sub.add_parser(
        "registrations-restore-gate",
        help="Used by deploy/learnpilot-restore.sh on the STAGED copy of a snapshot: void and scrub every "
             "pending registration and recovery; hold both (an existing hold is kept). "
             "One transaction; repeatable.",
    )
    status = sub.add_parser(
        "registrations-restore-status",
        help="Read-only: show the registration hold and pending count; with --expect, exit non-zero unless "
             "no registration or recovery is pending, the control row exists and the hold is expected.",
    )
    status.add_argument("--expect", choices=("held", "open"), help="the hold state the database must be in")
    sub.add_parser(
        "registrations-release-hold",
        help="End the hold a restore set: the only way it ends. Requires --acknowledge-lost-history.",
    ).add_argument(
        "--acknowledge-lost-history", action="store_true",
        help="Accept that the registration send accounting since the snapshot is gone.")
    sub.add_parser(
        "clear-registration-limit",
        help=f"Delete the registration send history of one email: it may receive 8 more codes the same day "
             f"(needs the application's {SECRET_KEY_ENV}).",
    ).add_argument("--email", required=True)
    return parser


def resolve_db_path(arg_db: str | None) -> Path:
    raw = arg_db or os.environ.get("DZI_DB", "").strip()
    if not raw:
        raise OperatorError("no database given: pass --db <path> or set DZI_DB")
    path = Path(raw).expanduser()
    if not path.is_file():
        raise OperatorError(f"database not found: {path}")
    return path


def require_inspectable_snapshot(path: Path) -> None:
    """Refuse, before SQLite opens it, a database that cannot be read without side effects.

    Even a read-only connection to a WAL-mode database creates or rewrites its
    ``-wal``/``-shm`` files, and skipping them (``immutable=1``) would hide commits
    that are still only in the WAL. A read-only preflight therefore accepts only a
    rollback-journal database with no sidecar file, which is what ``src.backup_sqlite``
    writes; that tool folds the WAL's committed pages into the snapshot. The journal
    mode is read from the file header with plain file I/O. Raising here says nothing
    about the health of the database.
    """
    guidance = (
        "cutover-check reads only a self-contained snapshot and did not open this file. "
        "Take a fresh snapshot of the current committed state and check that:\n"
        f'  python3 -m src.backup_sqlite --db "{path}" --output <snapshot.db>\n'
        "  python3 -m src.manage_accounts --db <snapshot.db> cutover-check"
    )
    present = [suffix for suffix in SQLITE_SIDECAR_SUFFIXES if os.path.lexists(f"{path}{suffix}")]
    if present:
        raise OperatorError(
            f"{path} has SQLite sidecar files ({', '.join(present)}): it is in use or was not "
            f"closed cleanly, and reading it could change them. {guidance}"
        )
    try:
        with path.open("rb") as handle:
            header = handle.read(20)
    except OSError as exc:
        raise OperatorError(f"cannot read database {path}: {exc}") from exc
    if not header:
        return  # an empty file is an empty rollback-journal database
    if len(header) < 20 or not header.startswith(SQLITE_HEADER_MAGIC):
        raise OperatorError(f"{path} is not an SQLite database")
    formats = {header[18], header[19]}
    if SQLITE_WAL_FORMAT in formats:
        raise OperatorError(
            f"{path} is a WAL-mode database (as the live database is): SQLite cannot read it "
            f"without creating or changing its -wal/-shm files. {guidance}"
        )
    if formats != {SQLITE_ROLLBACK_JOURNAL_FORMAT}:
        raise OperatorError(f"{path} has an unsupported SQLite file format. {guidance}")


def open_database(path: Path, *, read_only: bool = False) -> sqlite3.Connection:
    """Open an existing database (never creating it) and check for ``users``.

    Read-write by default; ``read_only`` accepts only a self-contained snapshot (see
    :func:`require_inspectable_snapshot`) and opens it so that no statement can write.
    """
    if read_only:
        require_inspectable_snapshot(path)
    try:
        mode = "ro" if read_only else "rw"
        conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode={mode}", uri=True)
        conn.row_factory = sqlite3.Row
        if read_only:
            conn.execute("PRAGMA query_only = ON")
        columns = {row[1] for row in conn.execute("PRAGMA table_info(users)")}
    except sqlite3.Error as exc:
        raise OperatorError(f"cannot open database {path}: {exc}") from exc
    if not REQUIRED_USER_COLUMNS <= columns:
        conn.close()
        raise OperatorError(
            "database has no usable users table (migration 012 required); "
            "apply migrations with src.manage_migrations first"
        )
    return conn


def read_password(use_stdin: bool, prompt_name: str = "Password") -> str:
    if use_stdin:
        data = sys.stdin.read(MAX_STDIN_BYTES + 1)
        if len(data) > MAX_STDIN_BYTES:
            raise OperatorError("password input too long")
        if data.endswith("\r\n"):
            data = data[:-2]
        elif data.endswith("\n"):
            data = data[:-1]
        if "\n" in data or "\r" in data:
            raise OperatorError("--password-stdin expects exactly one line")
        password = data
    else:
        password = getpass.getpass(f"{prompt_name}: ")
        if password != getpass.getpass(f"{prompt_name} (again): "):
            raise OperatorError("passwords do not match")
    if not password:
        raise OperatorError("empty password")
    return password


def check_password(password: str, email: str | None, role: str) -> None:
    problem = accounts.password_problem(password, email, role)
    if problem:
        raise OperatorError(problem)


def find_user(conn: sqlite3.Connection, email: str) -> dict:
    try:
        normalized = accounts.normalize_email(email)
    except ValueError as exc:
        raise OperatorError(f"invalid email: {email!r}") from exc
    user = accounts.get_user_by_email(conn, normalized)
    if user is None:
        raise OperatorError(f"no account with email {normalized}")
    return user


def find_account(conn: sqlite3.Connection, args) -> dict:
    """The account a command with ``--email`` / ``--id`` selectors names."""
    if args.id is None:
        return find_user(conn, args.email)
    user = accounts.get_user_by_id(conn, args.id)
    if user is None:
        raise OperatorError(f"no account with id {args.id}")
    return user


def account_label(user: dict, *, with_role: bool = False) -> str:
    """How a message names an account: always by id, with its email if it has one."""
    details = user["email"] or "no email"
    if with_role:
        details += f", {user['role']}"
    return f"account {user['id']} ({details})"


def cmd_create(conn, args) -> str:
    try:
        email = accounts.normalize_email(args.email)
    except ValueError as exc:
        raise OperatorError(f"invalid email: {args.email!r}") from exc
    password = read_password(args.password_stdin)
    check_password(password, email, args.role)
    try:
        user_id = accounts.create_user(
            conn, display_name=args.name, email=email, password=password, role=args.role
        )
    except sqlite3.IntegrityError as exc:
        raise OperatorError(f"an account with email {email} already exists") from exc
    except ValueError as exc:
        raise OperatorError(str(exc)) from exc
    return f"created account {user_id} ({email}, {args.role})"


def cmd_set_password(conn, args) -> str:
    user = find_user(conn, args.email)
    password = read_password(args.password_stdin)
    check_password(password, user["email"], user["role"])
    try:
        accounts.set_password(conn, user["id"], password)
    except ValueError as exc:
        raise OperatorError(str(exc)) from exc
    return f"password changed for {account_label(user)}; existing sessions invalidated"


def cmd_set_role(conn, args) -> str:
    user = find_account(conn, args)
    if args.role == "admin" and not user["has_password"]:
        # An admin signs in with a password and a TOTP code only, so the account
        # would have no way in.
        raise OperatorError(
            f"{account_label(user, with_role=True)} has no password and cannot be made an admin: "
            "an admin signs in with a password and a second factor only"
        )
    accounts.set_role(conn, user["id"], args.role)
    message = f"role of {account_label(user)} is now {args.role}; existing sessions invalidated"
    if args.role == "admin":
        message += "\nnote: run set-password if the current password is shorter than 14 characters"
    return message


def cmd_set_active(active: bool):
    def run(conn, args) -> str:
        user = find_account(conn, args)
        accounts.set_active(conn, user["id"], active)
        state = "enabled" if active else "disabled"
        return f"{state} {account_label(user, with_role=True)}; existing sessions invalidated"

    return run


def cmd_sign_out(conn, args) -> str:
    user = find_account(conn, args)
    accounts.rotate_session_key(conn, user["id"])
    return f"sessions of {account_label(user, with_role=True)} invalidated"


def cmd_clear_throttle(conn, args) -> str:
    """Forget the failed sign-in attempts counted for one email.

    The email need not belong to an account: unknown identifiers are throttled too.
    The row is keyed with the application's secret, which must therefore be the one
    the running application uses; there is no fallback key.
    """
    try:
        email = accounts.normalize_email(args.email)
    except ValueError as exc:
        raise OperatorError("invalid email") from exc
    secret_key = os.environ.get(SECRET_KEY_ENV)
    if not secret_key:
        raise OperatorError(
            f"{SECRET_KEY_ENV} is not set: the throttle is keyed with the application's secret"
        )
    if conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'login_throttle'"
    ).fetchone() is None:
        raise OperatorError(
            "database has no login_throttle table (migration 013 required); "
            "apply migrations with src.manage_migrations first"
        )
    login_throttle.clear(conn, login_throttle.identifier_hash(secret_key, email))
    return f"login throttle cleared for {email}"


def require_totp_tables(conn: sqlite3.Connection) -> None:
    if conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'totp_credentials'"
    ).fetchone() is None:
        raise OperatorError(
            "database has no totp_credentials table (migration 016 required); "
            "apply migrations with src.manage_migrations first"
        )


def require_admin(user: dict) -> None:
    """TOTP credentials are established and regenerated only for a current admin.

    ``totp-reset`` deliberately skips this check: it removes authentication material, so
    a demoted account's leftover credential can still be cleaned up.
    """
    if user["role"] != "admin":
        raise OperatorError(
            "TOTP is the admin second factor; this account is not an admin "
            "(use totp-reset to remove leftover TOTP state)"
        )


def require_totp_key() -> str:
    """The configured TOTP key; there is no fallback and its value is never printed."""
    key = os.environ.get(TOTP_KEY_ENV)
    problem = totp.key_problem(key)
    if problem:
        raise OperatorError(f"{problem}; it must be the key the running application uses")
    return key


def read_code(use_stdin: bool) -> str:
    if use_stdin:
        data = sys.stdin.read(MAX_CODE_INPUT_BYTES + 1)
        if len(data) > MAX_CODE_INPUT_BYTES:
            raise OperatorError("code input too long")
        data = data.rstrip("\r\n")
        if "\n" in data or "\r" in data:
            raise OperatorError("--code-stdin expects exactly one line")
        return data
    return getpass.getpass("Code from the authenticator: ")


def cmd_totp_enroll(conn, args) -> str:
    require_totp_tables(conn)
    user = find_user(conn, args.email)
    require_admin(user)
    key = require_totp_key()
    try:
        uri = totp.enroll(conn, user["id"], key, user["email"])
    except ValueError as exc:
        raise OperatorError(str(exc)) from exc
    return (
        f"TOTP enrolment started for {account_label(user)}; it is NOT active until totp-confirm.\n"
        "Existing sessions invalidated.\n"
        "Add this provisioning URI to the authenticator app. It contains the secret and\n"
        "is shown only now; do not store or share it:\n"
        f"{uri}\n"
        f"Then run: totp-confirm --email {user['email']}"
    )


def cmd_totp_confirm(conn, args) -> str:
    require_totp_tables(conn)
    user = find_user(conn, args.email)
    require_admin(user)
    key = require_totp_key()
    code = read_code(args.code_stdin)
    try:
        codes = totp.confirm(conn, user["id"], key, code, datetime.now(timezone.utc))
    except totp.TotpSecretError as exc:
        raise OperatorError(f"{exc}; run totp-reset and enrol again") from exc
    except (LookupError, ValueError) as exc:
        raise OperatorError(str(exc)) from exc
    return (
        f"TOTP confirmed for {account_label(user)}; admin sign-in now requires a code.\n"
        "Existing sessions invalidated.\n"
        "Recovery codes (each works once; shown only now, store them offline):\n"
        + "\n".join(codes)
    )


def cmd_totp_recovery_codes(conn, args) -> str:
    require_totp_tables(conn)
    user = find_user(conn, args.email)
    require_admin(user)
    try:
        codes = totp.regenerate_recovery_codes(conn, user["id"])
    except LookupError as exc:
        raise OperatorError(str(exc)) from exc
    return (
        f"recovery codes replaced for {account_label(user)}; every earlier code is void.\n"
        "New recovery codes (each works once; shown only now, store them offline):\n"
        + "\n".join(codes)
    )


def cmd_totp_reset(conn, args) -> str:
    require_totp_tables(conn)
    user = find_user(conn, args.email)
    existed = totp.reset(conn, user["id"])
    state = "TOTP and recovery codes removed" if existed else "no TOTP was enrolled"
    return (
        f"{state} for {account_label(user)}; existing sessions invalidated.\n"
        "An admin cannot sign in until totp-enroll and totp-confirm are run again."
    )


def _admin_sign_in_problems(conn: sqlite3.Connection, admin, key_usable: bool, key) -> list[str]:
    """Why this active admin could not complete an account sign-in (empty: it can)."""
    problems = []
    if not admin["email"]:
        problems.append("has no email to sign in with")
    # Structural only (web.services.accounts): no password is tried, nothing is hashed
    # or upgraded, and the stored value never reaches a message.
    if admin["password_hash"] is None or admin["password_hash"] == "":
        problems.append("has no password (run set-password)")
    elif not accounts.password_hash_is_verifiable(admin["password_hash"]):
        problems.append("has a password hash that account sign-in cannot verify (run set-password)")
    state = totp.credential_state(conn, admin["id"])
    if state is None:
        problems.append("has no TOTP credential (run totp-enroll, then totp-confirm)")
    elif state != "confirmed":
        problems.append("has an unconfirmed TOTP enrolment (run totp-confirm)")
    else:
        if key_usable and not totp.secret_is_readable(conn, admin["id"], key):
            problems.append(
                f"has a TOTP secret that {TOTP_KEY_ENV} cannot decrypt "
                "(restore the correct key, or run totp-reset and enrol again)"
            )
        if totp.unused_recovery_code_count(conn, admin["id"]) == 0:
            problems.append("has no unused recovery code (run totp-recovery-codes)")
    return problems


def cmd_cutover_check(conn, args) -> str:
    """Can at least one admin sign in? Account sign-in is the only one there is.

    One viable admin is enough: active, with an email and a structurally valid
    password hash, a confirmed TOTP credential that the configured key decrypts, and
    an unused recovery code. Other admins may be unhealthy. Nothing is verified
    against a real password or code, so no hash is upgraded and no TOTP step or
    recovery code is consumed; the connection is read-only and the input is a
    snapshot without sidecar files (``open_database``).
    """
    existing = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    missing = [name for name in CUTOVER_REQUIRED_TABLES if name not in existing]
    if missing:
        raise CheckFailed(
            "cutover-check: FAIL\n"
            f"  - database is missing account tables: {', '.join(missing)} "
            "(apply migrations with src.manage_migrations first)"
        )

    failures = []
    key = os.environ.get(TOTP_KEY_ENV)
    key_problem = totp.key_problem(key)
    if key_problem:
        failures.append(f"{key_problem}; it must be the key the running application uses")

    admins = conn.execute(
        # password_hash is only inspected structurally and never printed.
        "SELECT id, email, is_active, password_hash FROM users WHERE role = 'admin' ORDER BY id"
    ).fetchall()
    active = [admin for admin in admins if admin["is_active"]]
    if not active:
        disabled = len(admins)
        failures.append(
            "no active admin account"
            + (f" ({disabled} disabled; run enable)" if disabled else " (run create --role admin)")
        )

    viable, unhealthy = [], []
    for admin in active:
        label = admin["email"] or f"account {admin['id']}"
        problems = _admin_sign_in_problems(conn, admin, not key_problem, key)
        if problems:
            unhealthy.extend(f"admin {label} {problem}" for problem in problems)
        else:
            viable.append(label)

    if not failures and viable:
        return (
            "cutover-check: PASS\n"
            f"  viable admin account(s): {', '.join(viable)}\n"
            "  each has a usable password hash, a confirmed TOTP credential this key decrypts "
            "and an unused recovery code.\n"
            "  This describes the snapshot that was checked; take a new one after any account "
            "change.\n"
            "  Nothing was changed. Still verify a real sign-in before relying on it."
        )
    if active and not viable and not key_problem:
        failures.append("no admin account can complete an account sign-in")
    # With a viable admin the other admins' problems are not what failed the check.
    details = [] if viable else unhealthy
    raise CheckFailed("\n".join(["cutover-check: FAIL", *(f"  - {line}" for line in failures + details)]))


def _google_column(conn: sqlite3.Connection, user_id: int, has_identity_table: bool) -> str:
    """What ``list`` shows for an account's Google sign-in: the address Google reported
    when the identity was added, ``yes`` when it reported none, ``-`` without one.
    Never the subject."""
    if not has_identity_table:
        return "-"
    linked = [row for row in identities.list_identities(conn, user_id) if row["provider"] == "google"]
    if not linked:
        return "-"
    return ", ".join(row["email_at_link"] or "yes" for row in linked)


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone() is not None


def cmd_list(conn, args) -> str:
    # A database from before the registration storage (migration 021) has neither the
    # verification date nor profiles: both columns then show "-", as they do for every
    # account created by this CLI or through Google ("-" in "verified" means the address
    # was never verified by registration, not that the account is unusable).
    user_columns = {row[1] for row in conn.execute("PRAGMA table_info(users)")}
    verified = "email_verified_at" if "email_verified_at" in user_columns else "NULL"
    rows = conn.execute(
        "SELECT id, email, display_name, role, is_active, password_hash IS NOT NULL AS has_password, "
        f"created_at, last_login_at, {verified} AS email_verified_at FROM users ORDER BY id"
    ).fetchall()
    # A database from before the identity storage (migration 019) simply has none.
    has_identity_table = _has_table(conn, "user_identities")
    declared = profiles.declared_types(conn)
    lines = ["id\temail\tname\trole\tactive\tpassword\tgoogle\tcreated_at\tlast_login_at\tverified\ttype"]
    for r in rows:
        lines.append(
            "\t".join(
                str(v) if v is not None else "-"
                for v in (r["id"], r["email"], r["display_name"], r["role"],
                          "yes" if r["is_active"] else "no", "yes" if r["has_password"] else "no",
                          _google_column(conn, r["id"], has_identity_table),
                          r["created_at"], r["last_login_at"], r["email_verified_at"], declared.get(r["id"]))
            )
        )
    return "\n".join(lines)


# --------------------------------------------------------------------------- registration operations

REGISTRATION_TABLES = ("pending_registrations", "registration_sends", "registration_control")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def require_registration_tables(conn: sqlite3.Connection) -> None:
    """The registration storage of migrations 021 and 022, or a clear error."""
    sends = {row[1] for row in conn.execute("PRAGMA table_info(registration_sends)")}
    if not all(_has_table(conn, name) for name in REGISTRATION_TABLES) or "issuance_id" not in sends:
        raise OperatorError(
            "database has no registration storage (migrations 021 and 022 required); "
            "apply migrations with src.manage_migrations first"
        )
    try:
        has_recovery = recovery_scrub.has_recovery_storage(conn)
        if has_recovery and "recovery_id" not in sends:
            raise recovery_scrub.UnsupportedRecoverySchema("registration_sends lacks recovery_id")
        if not has_recovery and "recovery_id" in sends:
            raise recovery_scrub.UnsupportedRecoverySchema("password_recoveries missing beside recovery ledger")
    except recovery_scrub.UnsupportedRecoverySchema as exc:
        raise OperatorError(f"{exc}; apply migrations with src.manage_migrations first") from exc


def cmd_registrations_cleanup(conn, args) -> str:
    """Logical retention of unverified registrations (design 6.5).

    Needs no secret. Refuses while a restore of this database is in progress, and
    changes nothing while the clock is behind the high-water mark. ``--check`` only
    reads: it fails when a pending registration is older than an hour, which means the
    cleanup has not run or cannot.
    """
    from web.services import lifecycle

    require_registration_tables(conn)
    if lifecycle.restore_lock_present(args.database_path):
        raise OperatorError(
            "a restore of this database is in progress or was left unfinished "
            f"({lifecycle.restore_lock_path(args.database_path)} exists); nothing was changed"
        )
    if args.check:
        now = _utcnow()
        stale = registration.stale_pending_count(conn, now)
        recoveries = recovery_scrub.pending_count(conn, older_than=now - timedelta(hours=1))
        if stale or recoveries:
            raise CheckFailed(
                "registrations-cleanup --check: FAIL\n"
                f"  - {stale} pending registration(s) older than an hour: the cleanup has not run or cannot"
                f"\n  - {recoveries} pending recovery(s) older than an hour"
            )
        return "registrations-cleanup --check: OK (no pending registration or recovery is older than an hour)"
    conn.execute("BEGIN IMMEDIATE")
    # Looked for again with the write lock held: a restore that took its lock while this
    # run waited for the database must not find a cleanup writing after it. A lock made
    # between this look and the commit is caught by the restore's fingerprint of the live
    # database (deploy/learnpilot-restore.sh), as a failed restore.
    if lifecycle.restore_lock_present(args.database_path):
        raise OperatorError(
            "a restore of this database began while the cleanup waited for the database "
            f"({lifecycle.restore_lock_path(args.database_path)} exists); nothing was changed"
        )
    now = _utcnow()  # BEGIN IMMEDIATE already acquired the writer lock
    result = registration.cleanup(conn, clock=lambda: now)
    if result.clock_behind:
        raise OperatorError(
            "the clock is behind the registration high-water mark by more than 300 seconds; nothing was "
            "changed. Check the server's time; registrations-clock-accept ends the refusal"
        )
    recoveries = recovery_scrub.cleanup(conn, now)
    return (
        f"registrations-cleanup: {result.scrubbed} pending registration(s) expired and scrubbed, "
        f"{result.deleted} final row(s) and {result.deleted_sends} send record(s) deleted"
        f"; {recoveries.scrubbed} pending recovery(s) expired and scrubbed, "
        f"{recoveries.deleted} final recovery row(s) deleted"
    )


def cmd_registrations_clock_accept(conn, args) -> str:
    """Design 3.8: the operator, having checked the time, accepts the current clock."""
    require_registration_tables(conn)
    conn.execute("BEGIN IMMEDIATE")
    accepted = registration.accept_clock(conn, clock=_utcnow)
    if accepted.previous is None:
        return "no registration high-water mark is recorded; nothing to accept"
    return (
        f"registration high-water mark was {accepted.previous} UTC; it is now the current time, "
        f"{accepted.current} UTC. Registration refuses again if the clock goes back further."
    )


def cmd_registrations_restore_gate(conn, args) -> str:
    """Design 3.9: the gate, on the staged copy named by ``--db``. One transaction: the
    caller commits after this returns and rolls back after any exception."""
    require_registration_tables(conn)
    conn.execute("BEGIN IMMEDIATE")
    now = _utcnow()
    result = registration.restore_gate(conn, clock=lambda: now)
    recoveries = recovery_scrub.void_pending(conn, now)
    kept = " (the copy already carried a hold, kept as it was)" if result.already_held else ""
    return (
        f"registrations-restore-gate: {result.voided} pending registration(s) voided and scrubbed; "
        f"registration held since {result.hold_set_at} UTC{kept}"
        f"; {recoveries} pending recovery(s) voided and scrubbed"
    )


def cmd_registrations_restore_status(conn, args) -> str:
    """Design 3.9, 6.6. Never writes: the connection is made read-only for the statement."""
    require_registration_tables(conn)
    conn.execute("PRAGMA query_only = ON")
    state = registration.restore_status(conn)
    recoveries = recovery_scrub.pending_count(conn)
    held = "held" if state.held else "open"
    summary = (
        f"registrations-restore-status: {state.pending} pending registration(s); "
        f"control row {'present' if state.control_present else 'MISSING'}; registration {held}"
        + (f" since {state.hold_set_at} UTC" if state.held else "")
        + f"; {recoveries} pending recovery(s)"
    )
    if args.expect is None:
        return summary
    problems = []
    if recoveries:
        problems.append(f"{recoveries} pending recovery(s) exist")
    if state.pending:
        problems.append(f"{state.pending} pending registration(s) exist")
    if not state.control_present:
        problems.append("the registration control row is missing")
    elif held != args.expect:
        problems.append(f"registration is {held}, expected {args.expect}")
    if problems:
        raise CheckFailed(f"{summary}\nregistrations-restore-status --expect {args.expect}: FAIL\n"
                          + "\n".join(f"  - {problem}" for problem in problems))
    return f"{summary}\nregistrations-restore-status --expect {args.expect}: OK"


RELEASE_CONSEQUENCE = (
    "Releasing the registration hold accepts that the send accounting from before the restore is lost:\n"
    "  - the ledger starts from the snapshot's state, so an address that already received 8 codes in a day\n"
    "    may receive up to 8 further codes in that same day, registration or recovery "
    "(80 guesses against its codes, once);\n"
    "  - the total hourly budget and the circuit start from the snapshot's state too;\n"
    "  - nothing else changes: the clock high-water mark is NOT lowered (use registrations-clock-accept,\n"
    "    after checking the time, if the clock is behind it), and no limit is cleared.\n"
    "  - this repairs no account: restored passwords and session keys need the separate credential repair."
)


def cmd_registrations_release_hold(conn, args) -> str:
    """Design 3.9: the only way a restore hold ends. The consequence is printed first."""
    from web.services import lifecycle

    print(RELEASE_CONSEQUENCE)
    if not args.acknowledge_lost_history:
        raise OperatorError(
            "refused: this command releases the hold only with --acknowledge-lost-history, which states "
            "that the lost issuance history above is accepted. Nothing was changed"
        )
    require_registration_tables(conn)
    if lifecycle.restore_lock_present(args.database_path):
        raise OperatorError(
            "a restore of this database is in progress or was left unfinished "
            f"({lifecycle.restore_lock_path(args.database_path)} exists); nothing was changed"
        )
    conn.execute("BEGIN IMMEDIATE")
    # Looked for again with the write lock held: a restore that took its lock while this
    # command waited for the database must not find its hold gone. The dispatcher rolls
    # back, so neither the hold nor the high-water mark changes.
    if lifecycle.restore_lock_present(args.database_path):
        raise OperatorError(
            "a restore of this database began while the release waited for the database "
            f"({lifecycle.restore_lock_path(args.database_path)} exists); nothing was changed"
        )
    result = registration.release_hold(conn, clock=_utcnow)
    if not result.released:
        return "registration is not held; nothing was changed"
    message = f"registration hold (set {result.hold_set_at} UTC) released; the high-water mark is unchanged."
    if result.clock_behind:
        message += (
            f" The clock is behind the high-water mark ({result.high_water_at} UTC) by more than 300 seconds, "
            "so registration still answers 'unavailable'. Check the server's time; "
            "registrations-clock-accept ends that refusal."
        )
    return message


def cmd_clear_registration_limit(conn, args) -> str:
    """Design 6.6 and test DL12: forget the send history of one address.

    The rows are keyed with the application's secret, which must be the one the running
    application uses. The consequence is printed before anything is deleted.
    """
    try:
        email = accounts.normalize_email(args.email)
    except ValueError as exc:
        raise OperatorError("invalid email") from exc
    secret_key = os.environ.get(SECRET_KEY_ENV)
    if not secret_key:
        raise OperatorError(
            f"{SECRET_KEY_ENV} is not set: the registration send history is keyed with the application's secret"
        )
    require_registration_tables(conn)
    print(
        "note: this releases a further daily budget for this address: it may receive up to 8 more codes "
        "within the same day, which weakens the guessing bound for it (16 codes in one day). "
        "A code that address has now stops working; a new one can be requested. "
        "This also releases its recovery budget and stops its current recovery code."
    )
    conn.execute("BEGIN IMMEDIATE")
    deleted = registration.clear_address_limit(conn, registration.email_hash(secret_key, email))
    return f"registration send history cleared for {email}: {deleted} record(s) deleted"


COMMANDS = {
    "create": cmd_create,
    "set-password": cmd_set_password,
    "set-role": cmd_set_role,
    "disable": cmd_set_active(False),
    "enable": cmd_set_active(True),
    "sign-out": cmd_sign_out,
    "clear-throttle": cmd_clear_throttle,
    "totp-enroll": cmd_totp_enroll,
    "totp-confirm": cmd_totp_confirm,
    "totp-recovery-codes": cmd_totp_recovery_codes,
    "totp-reset": cmd_totp_reset,
    "cutover-check": cmd_cutover_check,
    "list": cmd_list,
    "registrations-cleanup": cmd_registrations_cleanup,
    "registrations-clock-accept": cmd_registrations_clock_accept,
    "registrations-restore-gate": cmd_registrations_restore_gate,
    "registrations-restore-status": cmd_registrations_restore_status,
    "registrations-release-hold": cmd_registrations_release_hold,
    "clear-registration-limit": cmd_clear_registration_limit,
}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        args.database_path = resolve_db_path(args.db)
        if args.command == "restore-credential-diff":
            from src.restore_credential_diff import run

            return run(args, require_inspectable_snapshot, OperatorError)
        conn = open_database(args.database_path, read_only=args.command in READ_ONLY_COMMANDS)
    except OperatorError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    try:
        try:
            message = COMMANDS[args.command](conn, args)
            conn.commit()
        except CheckFailed as exc:
            conn.rollback()
            print(exc)
            return 1
        except (OperatorError, LookupError, ValueError, sqlite3.Error,
                recovery_scrub.UnsupportedRecoverySchema) as exc:
            conn.rollback()
            print(f"error: {exc}", file=sys.stderr)
            return 1
        except BaseException:
            conn.rollback()
            raise
    finally:
        conn.close()
    print(message)
    return 0


if __name__ == "__main__":
    sys.exit(main())
