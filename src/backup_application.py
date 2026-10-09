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

The lock alone does not cover a backup that spans a whole restore, as one
started by hand can: the lock is absent before the restore and after it. So the
database, the uploads and the vault are also taken note of as the objects they
are when the backup starts. The archive is read from those two directories
themselves, wherever their names lead by then, and nothing is published unless
all three are still what is at their paths when the snapshot is complete.
"""

from __future__ import annotations

import argparse
import grp
import math
import os
import pwd
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


def _identity(info: os.stat_result) -> tuple[int, int]:
    return info.st_dev, info.st_ino


class _HeldState:
    """The database, the uploads and the vault a snapshot is read from, as objects.

    A restore replaces all three by renaming others into their places
    (``deploy/learnpilot-restore.sh``), so afterwards the same paths lead to
    other files. Whether that happened is told by device and inode number, which
    a rename does not change and a replacement does.

    The two directories are held open from before anything is read. An open
    directory keeps its number (it cannot be given to a new one while this one
    is held), and the archive is read from the open directory (``_add_directory``),
    not from what its path leads to at that moment: a restore that is taken back
    puts the old directory at its path again after another has been there, and a
    comparison at the end would not see that.

    The database file is held open only where that does not touch its locks
    (``O_PATH``, Linux). Closing any ordinary descriptor of a file releases every
    POSIX lock the process holds on it, SQLite's own included
    (https://www.sqlite.org/howtocorrupt.html, 2.2). Elsewhere it is compared by
    its numbers without being held.
    """

    def __init__(self, given: dict[str, Path], held: dict[str, Path]):
        # ``given`` is each path as it was named, ``held`` the same after its
        # links were followed: the second is opened, the first is asked again at
        # the end, so a link that was pointed elsewhere counts as a replacement.
        self._given = given
        self._seen: dict[str, tuple[int, int]] = {}
        self._descriptors: dict[str, int] = {}
        try:
            for name, path in held.items():
                info = os.stat(path)
                if stat.S_ISDIR(info.st_mode):
                    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                elif hasattr(os, "O_PATH"):
                    flags = os.O_PATH
                else:
                    self._seen[name] = _identity(info)
                    continue
                descriptor = os.open(path, flags | getattr(os, "O_CLOEXEC", 0))
                self._descriptors[name] = descriptor
                self._seen[name] = _identity(os.fstat(descriptor))
        except BaseException:
            self.close()
            raise

    def directory(self, name: str) -> int:
        return self._descriptors[name]

    def replaced(self) -> list[str]:
        """What is no longer at its path, or is no longer what the snapshot was read from."""
        found = []
        for name, path in self._given.items():
            try:
                now = _identity(os.stat(path))
            except OSError:
                found.append(f"the {name} is no longer there ({path})")
                continue
            if now != self._seen[name]:
                found.append(f"the {name} was replaced by another ({path})")
        return found

    def close(self) -> None:
        while self._descriptors:
            os.close(self._descriptors.popitem()[1])


_READ_HERE = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)


class _Descriptor:
    """What ``TarFile.gettarinfo`` asks of an open file, for a bare descriptor."""

    def __init__(self, descriptor: int, name: str):
        self.name = name
        self._descriptor = descriptor

    def fileno(self) -> int:
        return self._descriptor


def _add_directory(tar: tarfile.TarFile, directory: int, arcname: str, *, recursive: bool = True) -> None:
    """``tar.add(path, arcname)`` for a directory that is open, with nothing reached by a path.

    Every entry is opened relative to the descriptor of the directory it is in,
    and no link is followed on the way. What is archived is therefore in the tree
    that was opened, wherever that tree is by now and whatever stands at its old
    path. The members are those ``TarFile.add`` writes for the same tree, in the
    same order (``tests/test_application_backup.py`` compares the two).
    """
    tar.addfile(tar.gettarinfo(arcname=arcname, fileobj=_Descriptor(directory, arcname)))
    if not recursive:
        return
    for name in sorted(os.listdir(directory)):
        _add_entry(tar, directory, name, f"{arcname}/{name}")


def _add_entry(tar: tarfile.TarFile, parent: int, name: str, arcname: str) -> None:
    info = os.stat(name, dir_fd=parent, follow_symlinks=False)
    if stat.S_ISDIR(info.st_mode):
        descriptor = os.open(name, _READ_HERE | getattr(os, "O_DIRECTORY", 0), dir_fd=parent)
        try:
            _add_directory(tar, descriptor, arcname)
        finally:
            os.close(descriptor)
    elif stat.S_ISREG(info.st_mode):
        with os.fdopen(os.open(name, _READ_HERE, dir_fd=parent), "rb") as handle:
            member = tar.gettarinfo(arcname=arcname, fileobj=handle)
            if member is None or not (member.isreg() or member.islnk()):
                raise OSError(f"changed while it was being archived: {arcname}")
            tar.addfile(member, handle if member.isreg() else None)   # a hard link carries no data
    else:
        member = _other_member(tar, parent, name, arcname, info)
        if member is not None:
            tar.addfile(member)


def _other_member(tar: tarfile.TarFile, parent: int, name: str, arcname: str,
                  info: os.stat_result) -> tarfile.TarInfo | None:
    """What ``TarFile.gettarinfo`` makes of an entry that is neither a file nor a directory."""
    for is_kind, kind in ((stat.S_ISLNK, tarfile.SYMTYPE), (stat.S_ISFIFO, tarfile.FIFOTYPE),
                          (stat.S_ISCHR, tarfile.CHRTYPE), (stat.S_ISBLK, tarfile.BLKTYPE)):
        if is_kind(info.st_mode):
            break
    else:
        return None   # a socket: ``TarFile.add`` leaves it out as well
    member = tar.tarinfo(arcname)
    member.type, member.mode, member.mtime = kind, info.st_mode, info.st_mtime
    member.uid, member.gid = info.st_uid, info.st_gid
    if kind == tarfile.SYMTYPE:
        member.linkname = os.readlink(name, dir_fd=parent)
    try:
        member.uname = pwd.getpwuid(info.st_uid)[0]
    except KeyError:
        pass
    try:
        member.gname = grp.getgrgid(info.st_gid)[0]
    except KeyError:
        pass
    if kind in (tarfile.CHRTYPE, tarfile.BLKTYPE):
        member.devmajor, member.devminor = os.major(info.st_rdev), os.minor(info.st_rdev)
    return member


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
    given = {name: Path(path).absolute()
             for name, path in (("database", db), ("uploads directory", uploads), ("vault directory", vault))}
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

    # From here on the three are these objects, whatever is renamed meanwhile.
    if not db.is_file():
        raise BackupError(f"source database not found: {db}")
    try:
        state = _HeldState(given, {"database": db, "uploads directory": uploads, "vault directory": vault})
    except OSError as exc:
        raise BackupError(f"the state to back up cannot be opened ({exc}); no snapshot was created") from exc
    staging = None
    try:
        staging = Path(tempfile.mkdtemp(prefix=".snapshot-", dir=output_dir))
        os.chmod(staging, 0o700)
        backup_database(db, staging / "questions.db")
        archive = staging / "files.tar.gz"
        try:
            with tarfile.open(archive, "w:gz") as tar:
                # Read from the two directories that were opened, not from their paths.
                _add_directory(tar, state.directory("uploads directory"), "uploads/practical")
                vault_directory = state.directory("vault directory")
                _add_directory(tar, vault_directory, "vault", recursive=False)
                try:
                    is_directory = stat.S_ISDIR(os.stat("Generated", dir_fd=vault_directory).st_mode)
                except FileNotFoundError:
                    is_directory = False
                if is_directory:
                    _add_entry(tar, vault_directory, "Generated", "vault/Generated")
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
        # A restore that began and finished in between leaves no lock behind. It
        # leaves other files at these paths: the database that was copied and the
        # files that were archived may then belong to two different states.
        replaced = state.replaced()
        if replaced:
            raise BackupError(
                "the state changed hands while the snapshot was being made: "
                + "; ".join(replaced)
                + ". The copy may hold the database of one state and the files of another, so "
                "nothing was published. If a restore has just been made, run the backup again "
                "(docs/DEPLOYMENT.md, section 17)"
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
        state.close()
        if staging is not None and staging.exists():
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
