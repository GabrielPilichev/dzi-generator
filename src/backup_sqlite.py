#!/usr/bin/env python3
"""Online backup of a LearnPilot SQLite database (safe with WAL).

Never copies the live database file: the SQLite backup API reads one consistent
snapshot, including committed pages still in the WAL, while the app keeps
running. The result is verified with PRAGMA quick_check, stored as a single
self-contained rollback-journal file with mode 0600, and published atomically
without overwriting anything.

The isolated copy (never the live database) is logically scrubbed of unverified
registrations before it is checked and published (docs/reviews/EMAIL_REGISTRATION_DESIGN.md,
6.5, D18 (b)): ``scrub_pending_registrations``. Logical only: free pages, unused space
inside pages, earlier backups and storage below the application may keep old bytes.

    python3 -m src.backup_sqlite --db "$DZI_DB" --output-dir /var/backups/learnpilot
    python3 -m src.backup_sqlite --db "$DZI_DB" --output /var/backups/learnpilot/pre-deploy.db
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    from web.services import registration_scrub, recovery_scrub
except ModuleNotFoundError:  # run as `python3 src/backup_sqlite.py`
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from web.services import registration_scrub, recovery_scrub

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SIDECAR_SUFFIXES = ("-journal", "-wal", "-shm")


class BackupError(RuntimeError):
    pass


def _inside_checkout(path: Path) -> bool:
    resolved = path.resolve()
    return resolved == PROJECT_ROOT or PROJECT_ROOT in resolved.parents


def timestamped_destination(output_dir: Path, now: datetime | None = None) -> Path:
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
    return output_dir / f"questions-{stamp}.db"


def _remove_with_sidecars(path: Path) -> None:
    for candidate in (path, *(Path(f"{path}{suffix}") for suffix in SIDECAR_SUFFIXES)):
        try:
            candidate.unlink()
        except FileNotFoundError:
            pass


def _fsync(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def scrub_pending_registrations(copy: sqlite3.Connection) -> int:
    """D18 (b): void and scrub every pending registration of the isolated snapshot copy.

    Only the copy: the connection is the one on the partial file, never on the live
    database. Pending rows become ``void`` with every personal and credential column
    NULL (the restore gate's step 1 without its hold). Accounts, profiles, subjects,
    canonical content, the send ledger, the control row and already final rows are left
    as they are. A database from before the registration storage (no table) has nothing to
    scrub; a table that exists without the required columns is refused.
    Returns the number of rows scrubbed. Raises on any error; the caller publishes nothing.
    """
    try:
        if not registration_scrub.has_pending_storage(copy):
            return 0     # the table is absent: a database from before migration 021
    except registration_scrub.UnsupportedRegistrationSchema as exc:
        raise BackupError(f"unsupported registration schema in the snapshot copy: {exc}") from exc
    copy.execute("BEGIN IMMEDIATE")
    try:
        scrubbed = registration_scrub.void_pending(copy, datetime.now(timezone.utc))
        copy.execute("COMMIT")
    except BaseException:
        copy.execute("ROLLBACK")
        raise
    return scrubbed


def scrub_pending_recoveries(copy: sqlite3.Connection) -> int:
    """Logical scrub on the isolated copy; unsupported storage publishes nothing."""
    try:
        if not recovery_scrub.has_recovery_storage(copy):
            return 0
        copy.execute("BEGIN IMMEDIATE")
        try:
            scrubbed = recovery_scrub.void_pending(copy, datetime.now(timezone.utc))
            copy.commit()
        except BaseException:
            copy.rollback()
            raise
        return scrubbed
    except recovery_scrub.UnsupportedRecoverySchema as exc:
        raise BackupError(f"unsupported recovery schema in the snapshot copy: {exc}") from exc


def backup_database(source: Path, destination: Path) -> dict:
    source = Path(source).expanduser()
    destination = Path(destination).expanduser()
    if not source.is_file():
        raise BackupError(f"source database not found: {source}")
    if destination.exists():
        raise BackupError(f"destination already exists (refusing to overwrite): {destination}")
    if not destination.parent.is_dir():
        raise BackupError(f"destination directory does not exist: {destination.parent}")
    if _inside_checkout(destination):
        raise BackupError("destination must be outside the repository checkout")
    if destination.resolve() == source.resolve():
        raise BackupError("destination must differ from the source database")

    partial = destination.parent / f".{destination.name}.partial-{os.getpid()}"
    _remove_with_sidecars(partial)
    try:
        # mode=rw never creates a missing database but lets SQLite open WAL/SHM
        # sidecars; query_only forbids writes through this connection.
        src = sqlite3.connect(source.resolve().as_uri() + "?mode=rw", uri=True, timeout=30)
        try:
            src.execute("PRAGMA query_only=ON")
            dst = sqlite3.connect(partial)
            try:
                src.backup(dst)  # pages=-1: one step, one consistent snapshot
                dst.execute("PRAGMA journal_mode=DELETE").fetchone()
                # After the copy and before every check, the fsync, the link and the hash: what
                # is verified, published and hashed is the scrubbed file.
                registrations_scrubbed = scrub_pending_registrations(dst)
                recoveries_scrubbed = scrub_pending_recoveries(dst)
                result = [row[0] for row in dst.execute("PRAGMA quick_check")]
                if result != ["ok"]:
                    raise BackupError(f"quick_check failed: {result[:5]}")
            finally:
                dst.close()
        finally:
            src.close()

        os.chmod(partial, 0o600)
        _fsync(partial)
        try:
            os.link(partial, destination)  # fails instead of replacing an existing file
        except FileExistsError as exc:
            raise BackupError(f"destination appeared during backup: {destination}") from exc
        _fsync(destination.parent)
    finally:
        # The partial name (and any SHM/WAL left by the journal-mode switch) is
        # never part of the result, on success or failure.
        _remove_with_sidecars(partial)

    return {
        "source": str(source),
        "destination": str(destination),
        "bytes": destination.stat().st_size,
        "sha256": _sha256(destination),
        "quick_check": "ok",
        "registrations_scrubbed": registrations_scrubbed,
        "recoveries_scrubbed": recoveries_scrubbed,
    }


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", required=True, type=Path, help="source SQLite database (e.g. \"$DZI_DB\")")
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--output", type=Path, help="exact backup file to create")
    target.add_argument("--output-dir", type=Path,
                        help="existing directory; creates questions-<UTC timestamp>.db")
    return parser.parse_args(argv)


def main(argv=None, out=sys.stdout, err=sys.stderr) -> int:
    args = parse_args(argv)
    destination = args.output or timestamped_destination(args.output_dir)
    try:
        summary = backup_database(args.db, destination)
    except (BackupError, OSError, sqlite3.Error) as exc:
        print(f"backup failed: {exc}", file=err)
        return 1
    for key, value in summary.items():
        print(f"{key}: {value}", file=out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
