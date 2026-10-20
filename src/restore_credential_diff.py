"""Read-only offline restore evidence, PASSWORD_RECOVERY_DESIGN section 6.6.

Only manage_accounts' separate dispatch calls run. No live connection, migration,
repair, application import or credential output. Equal identity/hash brackets
detect lasting changes; operator preparation must exclude concurrent writers
(including changes made and undone between checks).
"""

import hashlib
import os
import re
import shlex
import sqlite3
import stat
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

REQUIRED = {
    "users": ("012_users.sql", ("id", "email", "role", "password_hash", "is_active",
                                "session_key", "password_changed_at")),
    "totp_credentials": ("016_admin_totp.sql", ("user_id", "secret_ciphertext", "confirmed_at")),
    "totp_recovery_codes": ("016_admin_totp.sql", ("id", "user_id", "code_hash", "used_at")),
}
CLASSES = {
    1: "uncertain (no evidence)", 2: "uncertain (identity)",
    3: "created after the snapshot", 4: "uncertain (password evidence)",
    5: "uncertain (password evidence)", 6: "password reverted",
    7: "sessions revived", 8: "unchanged (as far as compared)",
}


class Unavailable(Exception):
    """Sanitized reason evidence cannot be used."""


@dataclass(repr=False)
class Evidence:
    users: dict = field(default_factory=dict)
    factors: dict = field(default_factory=dict)
    source: tuple = ()  # (st_dev, st_ino) of the bracketed input file


def _identity(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _bracket(path, live_identity, inspect, inspection_error, expected=None):
    """No-follow file read, complete header, shared sidecar policy, identity/hash."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as handle:
            before = os.fstat(handle.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise Unavailable("not a regular file")
            if (before.st_dev, before.st_ino) == live_identity:
                raise Unavailable("input is the live --db file (including hard links)")
            header = handle.read(100)
            if len(header) != 100 or header[:16] != b"SQLite format 3\0":
                raise Unavailable("missing complete 100-byte SQLite header")
            if header[18:20] != b"\1\1":
                raise Unavailable("WAL-mode or unsupported journal header; prepare a rollback-mode copy")
            try:
                inspect(path)
            except inspection_error as exc:
                # The existing helper mentions cutover-check and may include OS text.
                raise Unavailable("sidecars or uninspectable input; prepare a self-contained rollback-mode copy") from exc
            digest = hashlib.sha256(header)
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
            after = os.fstat(handle.fileno())
        named = os.stat(path, follow_symlinks=False)
        if _identity(before) != _identity(after) or _identity(after) != _identity(named):
            raise Unavailable("file identity or metadata changed during inspection")
        result = (*_identity(after), digest.hexdigest())
        if expected is not None:
            if not re.fullmatch(r"[0-9a-fA-F]{64}", expected):
                raise Unavailable("expected SHA-256 must be 64 hexadecimal characters")
            if result[-1] != expected.lower():
                raise Unavailable("expected SHA-256 does not match")
        return result
    except OSError as exc:
        raise Unavailable("missing, unreadable, symbolic-link or non-regular input") from exc


def _read_rows(conn):
    if [r[0] for r in conn.execute("PRAGMA quick_check")] != ["ok"]:
        raise Unavailable("quick_check failed")
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    migrations = None
    if "schema_migrations" in tables:
        migrations = {r[0] for r in conn.execute("SELECT filename FROM schema_migrations")}
    rows = {}
    for table, (migration, columns) in REQUIRED.items():
        if table not in tables:
            if migrations is None or migration in migrations:
                raise Unavailable(f"unsupported {table}: missing required storage")
            rows[table] = []
            continue
        present = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if not set(columns) <= present:
            raise Unavailable(f"unsupported {table}: missing required columns")
        cursor = conn.execute(f"SELECT {', '.join(columns)} FROM {table}")
        rows[table] = [dict(zip(columns, row)) for row in cursor]
    if "users" not in tables and tables & {"totp_credentials", "totp_recovery_codes"}:
        raise Unavailable("unsupported TOTP storage without users")
    users = {r["id"]: r for r in rows["users"]}
    if len(users) != len(rows["users"]):
        raise Unavailable("unsupported users: duplicate identity")
    factors = {uid: ([], []) for uid in users}
    for table, slot, cols in (("totp_credentials", 0, ("secret_ciphertext", "confirmed_at")),
                              ("totp_recovery_codes", 1, ("id", "code_hash", "used_at"))):
        for row in rows[table]:
            if row["user_id"] not in users:
                raise Unavailable("unsupported TOTP storage without owning account")
            factors[row["user_id"]][slot].append(tuple(row[c] for c in cols))
    # Sets include recovery-code identity and used_at, not unrelated audit times.
    return Evidence(users, {uid: tuple(frozenset(part) for part in value)
                            for uid, value in factors.items()})


def read_input(path, live_identity, inspect, inspection_error, expected=None):
    path = Path(os.path.abspath(path.expanduser()))  # do not resolve away a symlink
    before = _bracket(path, live_identity, inspect, inspection_error, expected)
    try:
        conn = sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)
        try:
            evidence = _read_rows(conn)
        finally:
            conn.close()
    except sqlite3.Error as exc:
        # SQLite errors can contain schema text. Never echo credential values.
        raise Unavailable("unsupported or unreadable SQLite credential storage") from exc
    after = _bracket(path, live_identity, inspect, inspection_error, expected)
    if before != after:
        raise Unavailable("input changed during the read; evidence unavailable")
    evidence.source = after[:2]
    return evidence


def _timestamp(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2}", value):
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def classify(snapshot, replaced, *, unavailable=False):
    """Return the first matching row of the ordered design table, and P/N/K/None."""
    if unavailable:
        order = 1
    elif snapshot is not None and (replaced is None or snapshot["email"] != replaced["email"]):
        order = 2
    elif snapshot is None:
        order = 3
    else:
        same_hash = snapshot["password_hash"] == replaced["password_hash"]
        same_time = snapshot["password_changed_at"] == replaced["password_changed_at"]
        a, b = _timestamp(snapshot["password_changed_at"]), _timestamp(replaced["password_changed_at"])
        if same_hash and not same_time:
            order = 4
        elif not same_hash:
            order = (6 if snapshot["password_hash"] is not None and replaced["password_hash"] is not None
                     and a is not None and b is not None and b > a else 5)
        elif snapshot["session_key"] != replaced["session_key"]:
            order = 7
        else:
            order = 8
    if order in (3, 8):
        category = None
    elif order == 7:
        category = "K"
    else:
        category = "P" if snapshot["password_hash"] is not None else "N"
    return order, category


def report(snapshot, replaced):
    """Only labels, ids, addresses, findings and feasible operator guidance escape."""
    lines = []
    needs_repair = replaced is None
    ids = set(snapshot.users) | (set(replaced.users) if replaced is not None else set())
    for uid in sorted(ids):
        a = snapshot.users.get(uid)
        b = replaced.users.get(uid) if replaced is not None else None
        order, category = classify(a, b, unavailable=replaced is None)
        address = (a if a is not None else b)["email"]
        lines.append(f"account {uid} ({address!r}): {CLASSES[order]}; category {category or 'none'}")
        selector = f"--id {uid}"
        email_selector = "--email " + shlex.quote(address) if address is not None else None
        if category == "P":
            if email_selector:
                lines.append(f"  P: set-password {email_selector} (new reviewed password) OR disable {selector}; never sign-out alone")
            else:
                lines.append(f"  P: disable {selector}; missing address prevents set-password")
            if b is not None and b["password_hash"] is None:
                lines.append(f"  disable {selector} is closest to the replaced passwordless state")
        elif category == "N":
            lines.append(f"  N: sign-out {selector} OR disable {selector}; never grant a password")
        elif category == "K":
            lines.append(f"  K: sign-out {selector}")
        needs_repair |= category is not None
        if a is not None and b is not None:
            if (a["role"], a["is_active"]) != (b["role"], b["is_active"]):
                needs_repair = True
                lines.append("  state differs")
                if b["role"] == "admin" and a["password_hash"] is None:
                    lines.append(f"  disable {selector}; restored account has no password and cannot be promoted to admin")
                else:
                    lines.append(f"  {'enable' if b['is_active'] else 'disable'} {selector}; "
                                 f"set-role {selector} --role {shlex.quote(b['role'])}")
        empty = (frozenset(), frozenset())
        a_factor = snapshot.factors.get(uid, empty)
        factor_diff = (a is not None and b is not None
                       and a_factor != replaced.factors.get(uid, empty))
        if factor_diff or (order in (1, 2) and any(a_factor)):
            needs_repair = True
            lines.append("  second factor differs" if factor_diff else "  second factor uncertain")
            if email_selector:
                lines.append(f"  totp-reset {email_selector}; new enrolment before the next admin sign-in "
                             "(totp-enroll and totp-confirm require an admin)")
            else:
                lines.append(f"  disable {selector}; no address for totp-reset: operator review required")
    return lines, int(needs_repair)


def run(args, inspect, inspection_error):
    try:
        info = os.stat(args.database_path)  # the only examination of the LIVE path
    except OSError:
        print("restore-credential-diff: cannot stat live --db; no inputs read")
        return 1
    live_identity = (info.st_dev, info.st_ino)
    try:
        snapshot = read_input(args.snapshot, live_identity, inspect, inspection_error, args.snapshot_sha256)
    except Unavailable as exc:
        print(f"snapshot {args.snapshot}: evidence unavailable: {exc}")
        print("Treat every account of the live database as uncertain; use the pre-repair snapshot for P/N repair.")
        return 1
    try:
        replaced = read_input(args.replaced, live_identity, inspect, inspection_error, args.replaced_sha256)
        # A file compared with itself would report every account unchanged.
        if replaced.source == snapshot.source:
            raise Unavailable("replaced is the same file as the snapshot (same device and inode, "
                              "including hard links)")
    except Unavailable as exc:
        print(f"replaced {args.replaced}: evidence unavailable: {exc}")
        print("No comparison made; every snapshot account is uncertain.")
        replaced = None
    lines, status = report(snapshot, replaced)
    print("restore-credential-diff: read-only; no repairs performed")
    print("\n".join(lines))
    return status
