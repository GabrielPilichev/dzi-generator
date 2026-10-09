"""Create one recoverable SQLite, uploads and generated-vault snapshot.

One contract: ``snapshot-<UTC stamp>/`` (``questions.db`` + ``files.tar.gz``)
exists only if every part succeeded. Any failure -- including too little free
space, checked before any work starts -- exits non-zero, publishes nothing and
removes all staging files.

No snapshot is taken while a restore of the database is in progress or was left
unfinished (``deploy/learnpilot-restore.sh`` leaves a lock next to it): the live
state may then be a snapshot's files next to a database they do not belong to,
and it would be published as one complete, consistent snapshot. The lock is
looked for before anything is read and again before the snapshot is published,
so a restore that begins in between publishes nothing either.
"""

from __future__ import annotations

import argparse
import math
import os
import shutil
import stat
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from src.backup_sqlite import BackupError, _inside_checkout, backup_database
from web.services.lifecycle import restore_lock_path, restore_lock_present


# Space needed in the backup filesystem = (database + archived files + tar
# metadata) x 1.10, plus headroom, because compression is not assumed.
TAR_ENTRY_OVERHEAD_BYTES = 2048        # 512-byte header + padding, with slack
TAR_FIXED_OVERHEAD_BYTES = 1024 * 1024
SIZE_SAFETY_FACTOR = 1.10
FREE_SPACE_HEADROOM_BYTES = 256 * 1024 * 1024


def _tree_usage(root: Path) -> tuple[int, int]:
    """(bytes of regular files, entries) tar would store; symlinks are not followed."""
    total = entries = 0
    for directory, subdirs, files in os.walk(root, followlinks=False):
        entries += 1 + len(subdirs) + len(files)
        for name in files:
            try:
                info = os.lstat(os.path.join(directory, name))
            except OSError:
                continue  # vanished while scanning
            if stat.S_ISREG(info.st_mode):
                total += info.st_size
    return total, entries


def estimate_snapshot_bytes(db: Path, uploads: Path, vault: Path) -> int:
    """Conservative size of one snapshot, from exactly what will be archived.

    Counted: the database (main file plus its WAL, which the online backup folds
    into the copy), the uploads tree and vault/Generated. Not counted: SHM, env
    files, anything else in the vault or outside the configured paths.
    """
    db_bytes = sum(path.stat().st_size for path in (db, Path(f"{db}-wal")) if path.is_file())
    file_bytes, entries = _tree_usage(uploads)
    generated = vault / "Generated"
    if generated.is_dir():
        more_bytes, more_entries = _tree_usage(generated)
        file_bytes += more_bytes
        entries += more_entries
    entries += 2  # vault root + uploads root headers
    raw = db_bytes + file_bytes + entries * TAR_ENTRY_OVERHEAD_BYTES + TAR_FIXED_OVERHEAD_BYTES
    return math.ceil(raw * SIZE_SAFETY_FACTOR) + FREE_SPACE_HEADROOM_BYTES


def _overlaps(first: Path, second: Path) -> bool:
    return first == second or first in second.parents or second in first.parents


def _reject_unsafe_layout(db: Path, uploads: Path, vault: Path, output_dir: Path) -> None:
    named = (("uploads", uploads), ("vault", vault), ("backup output", output_dir))
    for index, (first_name, first) in enumerate(named):
        for second_name, second in named[index + 1:]:
            if _overlaps(first, second):
                raise BackupError(
                    f"{first_name} and {second_name} directories must not overlap or nest"
                )
    for name, tree in (("uploads", uploads), ("vault", vault)):
        if db == tree or tree in db.parents:
            raise BackupError(f"the database must not be inside the {name} directory")


def backup_application(db: Path, uploads: Path, vault: Path, output_dir: Path,
                       *, stamp: str | None = None) -> Path:
    db, uploads, vault, output_dir = (
        Path(path).resolve() for path in (db, uploads, vault, output_dir)
    )
    if any(_inside_checkout(path) for path in (db, uploads, vault, output_dir)):
        raise BackupError("application state and backups must be outside the checkout")
    # Before anything else is looked at: during a restore a directory may be missing.
    if restore_lock_present(db):
        raise BackupError(
            "a restore of this database is in progress or was left unfinished "
            f"({restore_lock_path(db)} exists); no snapshot is taken of a partly replaced state. "
            "Finish the restore first (docs/DEPLOYMENT.md, section 17)"
        )
    if not all(path.is_dir() for path in (uploads, vault, output_dir)):
        raise BackupError("upload, vault and backup directories must exist")
    _reject_unsafe_layout(db, uploads, vault, output_dir)
    stamp = stamp or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    if not stamp.isascii() or not stamp.isalnum():
        raise BackupError("invalid backup timestamp")
    destination = output_dir / f"snapshot-{stamp}"
    if destination.exists():
        raise BackupError("backup snapshot already exists")

    required = estimate_snapshot_bytes(db, uploads, vault)
    free = shutil.disk_usage(output_dir).free
    if free < required:
        raise BackupError(
            f"not enough free space for a complete snapshot in {output_dir}: "
            f"need about {required // 2**20} MiB, have {free // 2**20} MiB. "
            "No snapshot was created; free space or move older snapshots off the server."
        )

    staging = Path(tempfile.mkdtemp(prefix=".snapshot-", dir=output_dir))
    try:
        os.chmod(staging, 0o700)
        backup_database(db, staging / "questions.db")
        archive = staging / "files.tar.gz"
        try:
            with tarfile.open(archive, "w:gz") as tar:
                tar.add(uploads, arcname="uploads/practical")
                tar.add(vault, arcname="vault", recursive=False)
                generated = vault / "Generated"
                if generated.is_dir():
                    tar.add(generated, arcname="vault/Generated")
        except (OSError, tarfile.TarError) as exc:
            raise BackupError(
                f"file archive failed ({exc}); no snapshot was published and the "
                "partial database copy was removed"
            ) from exc
        os.chmod(archive, 0o600)
        with archive.open("rb") as handle:
            os.fsync(handle.fileno())
        # A restore that began after the check above, while this was being read:
        # what was read may be half of one state and half of another.
        if restore_lock_present(db):
            raise BackupError(
                "a restore of this database began while the snapshot was being made "
                f"({restore_lock_path(db)} exists); nothing was published. Run the backup again "
                "when the restore has finished (docs/DEPLOYMENT.md, section 17)"
            )
        # A completed snapshot becomes visible in one rename. No live DB tar.
        staging.rename(destination)
        directory_fd = os.open(output_dir, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        return destination
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--uploads", required=True, type=Path)
    parser.add_argument("--vault", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        destination = backup_application(args.db, args.uploads, args.vault, args.output_dir)
    except (BackupError, OSError, tarfile.TarError) as exc:
        parser.exit(1, f"LearnPilot backup failed: {exc}\n")
    print(f"Complete backup: {destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
