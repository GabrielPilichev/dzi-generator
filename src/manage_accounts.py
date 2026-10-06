#!/usr/bin/env python3
"""Operator CLI for LearnPilot accounts (Accounts PR 2).

Server-side tooling only: it is the one place an admin can be created or promoted.
The target database must be named explicitly (``--db`` or ``DZI_DB``); it is never
defaulted to ``data/questions.db``, never created, and never migrated.

    python3 -m src.manage_accounts --db "$DZI_DB" create --email a@b.bg --name "Ана" --role teacher
    python3 -m src.manage_accounts --db "$DZI_DB" list

Passwords are read with ``getpass`` (twice) or, for automation, ``--password-stdin``
(exactly one line). They are never accepted from argv or the environment.
"""

from __future__ import annotations

import argparse
import getpass
import os
import sqlite3
import sys
from pathlib import Path

try:
    from web.services import accounts
except ModuleNotFoundError:  # run as `python3 src/manage_accounts.py`
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from web.services import accounts

ADMIN_MIN_PASSWORD_LENGTH = 14
MAX_STDIN_BYTES = 4096
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
    problem = accounts.password_problem(password, email)
    if problem is None and role == "admin" and len(password) < ADMIN_MIN_PASSWORD_LENGTH:
        problem = f"Паролата на администратор трябва да е поне {ADMIN_MIN_PASSWORD_LENGTH} символа."
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
