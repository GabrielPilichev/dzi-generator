#!/usr/bin/env python3
"""Operator CLI for LearnPilot accounts (Accounts PR 2).

Server-side tooling only: it is the one place an admin can be created or promoted.
The target database must be named explicitly (``--db`` or ``DZI_DB``); it is never
defaulted to ``data/questions.db``, never created, and never migrated.

    python3 -m src.manage_accounts --db "$DZI_DB" create --email a@b.bg --name "Ана" --role teacher
    python3 -m src.manage_accounts --db "$DZI_DB" list

Passwords are read with ``getpass`` (twice) or, for automation, ``--password-stdin``
(exactly one line). They are never accepted from argv or the environment.

Admin TOTP (migration 016) is enrolled only here: ``totp-enroll`` prints the
``otpauth://`` provisioning URI once, ``totp-confirm`` activates the credential with a
current code and prints the recovery codes once, ``totp-recovery-codes`` replaces
them, and ``totp-reset`` removes the credential. Enrolment and confirmation need
``DZI_TOTP_KEY``; no other command prints a secret.
"""

from __future__ import annotations

import argparse
import getpass
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    from web.services import accounts, login_throttle, totp
except ModuleNotFoundError:  # run as `python3 src/manage_accounts.py`
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from web.services import accounts, login_throttle, totp

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


class OperatorError(Exception):
    """An expected operator/input/database problem: reported without a traceback."""


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

    set_role = sub.add_parser("set-role", help="Change a role (the only way to grant admin).")
    set_role.add_argument("--email", required=True)
    set_role.add_argument("--role", required=True, choices=accounts.ROLES)

    for name, text in (
        ("disable", "Disable an account."),
        ("enable", "Enable an account."),
        ("sign-out", "Invalidate all existing sessions of an account."),
    ):
        sub.add_parser(name, help=text).add_argument("--email", required=True)

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

    sub.add_parser("list", help="List accounts (no secrets).")
    return parser


def resolve_db_path(arg_db: str | None) -> Path:
    raw = arg_db or os.environ.get("DZI_DB", "").strip()
    if not raw:
        raise OperatorError("no database given: pass --db <path> or set DZI_DB")
    path = Path(raw).expanduser()
    if not path.is_file():
        raise OperatorError(f"database not found: {path}")
    return path


def open_database(path: Path) -> sqlite3.Connection:
    """Open an existing database read-write (never creating it) and check for ``users``."""
    try:
        conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=rw", uri=True)
        conn.row_factory = sqlite3.Row
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
    return f"password changed for {user['email']}; existing sessions invalidated"


def cmd_set_role(conn, args) -> str:
    user = find_user(conn, args.email)
    accounts.set_role(conn, user["id"], args.role)
    message = f"role of {user['email']} is now {args.role}; existing sessions invalidated"
    if args.role == "admin":
        message += "\nnote: run set-password if the current password is shorter than 14 characters"
    return message


def cmd_set_active(active: bool):
    def run(conn, args) -> str:
        user = find_user(conn, args.email)
        accounts.set_active(conn, user["id"], active)
        state = "enabled" if active else "disabled"
        return f"{state} {user['email']}; existing sessions invalidated"

    return run


def cmd_sign_out(conn, args) -> str:
    user = find_user(conn, args.email)
    accounts.rotate_session_key(conn, user["id"])
    return f"sessions of {user['email']} invalidated"


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
    if user["role"] != "admin":
        raise OperatorError("TOTP is the admin second factor; this account is not an admin")
    key = require_totp_key()
    try:
        uri = totp.enroll(conn, user["id"], key, user["email"])
    except ValueError as exc:
        raise OperatorError(str(exc)) from exc
    return (
        f"TOTP enrolment started for {user['email']}; it is NOT active until totp-confirm.\n"
        "Existing sessions invalidated.\n"
        "Add this provisioning URI to the authenticator app. It contains the secret and\n"
        "is shown only now; do not store or share it:\n"
        f"{uri}\n"
        f"Then run: totp-confirm --email {user['email']}"
    )


def cmd_totp_confirm(conn, args) -> str:
    require_totp_tables(conn)
    user = find_user(conn, args.email)
    key = require_totp_key()
    code = read_code(args.code_stdin)
    try:
        codes = totp.confirm(conn, user["id"], key, code, datetime.now(timezone.utc))
    except totp.TotpSecretError as exc:
        raise OperatorError(f"{exc}; run totp-reset and enrol again") from exc
    except (LookupError, ValueError) as exc:
        raise OperatorError(str(exc)) from exc
    return (
        f"TOTP confirmed for {user['email']}; admin sign-in now requires a code.\n"
        "Existing sessions invalidated.\n"
        "Recovery codes (each works once; shown only now, store them offline):\n"
        + "\n".join(codes)
    )


def cmd_totp_recovery_codes(conn, args) -> str:
    require_totp_tables(conn)
    user = find_user(conn, args.email)
    try:
        codes = totp.regenerate_recovery_codes(conn, user["id"])
    except LookupError as exc:
        raise OperatorError(str(exc)) from exc
    return (
        f"recovery codes replaced for {user['email']}; every earlier code is void.\n"
        "New recovery codes (each works once; shown only now, store them offline):\n"
        + "\n".join(codes)
    )


def cmd_totp_reset(conn, args) -> str:
    require_totp_tables(conn)
    user = find_user(conn, args.email)
    existed = totp.reset(conn, user["id"])
    state = "TOTP and recovery codes removed" if existed else "no TOTP was enrolled"
    return (
        f"{state} for {user['email']}; existing sessions invalidated.\n"
        "An admin cannot sign in until totp-enroll and totp-confirm are run again."
    )


def cmd_list(conn, args) -> str:
    rows = conn.execute(
        "SELECT id, email, display_name, role, is_active, created_at, last_login_at "
        "FROM users ORDER BY id"
    ).fetchall()
    lines = ["id\temail\tname\trole\tactive\tcreated_at\tlast_login_at"]
    for r in rows:
        lines.append(
            "\t".join(
                str(v) if v is not None else "-"
                for v in (r["id"], r["email"], r["display_name"], r["role"],
                          "yes" if r["is_active"] else "no", r["created_at"], r["last_login_at"])
            )
        )
    return "\n".join(lines)


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
    "list": cmd_list,
}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        conn = open_database(resolve_db_path(args.db))
    except OperatorError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    try:
        try:
            message = COMMANDS[args.command](conn, args)
            conn.commit()
        except (OperatorError, LookupError, ValueError, sqlite3.Error) as exc:
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
