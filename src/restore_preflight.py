#!/usr/bin/env python3
"""Read-only checks before a restore touches anything (``deploy/learnpilot-restore.sh``).

    python3 -m src.restore_preflight --snapshot /var/backups/learnpilot/snapshot-<stamp> \\
        --state-root /var/lib/learnpilot

Run as the service user, while the service is still running. It reads the
snapshot and looks at the state directory; it writes nothing and changes
nothing. Everything it finds wrong is something that would otherwise stop the
restore half-way:

* the snapshot is complete and readable: ``questions.db`` passes
  ``PRAGMA quick_check``, and ``files.tar.gz`` can be read to its end and holds
  only regular files and directories under ``uploads/`` and ``vault/``;
* the state directory and its ``data`` directory are writable by this user;
* the state directory is itself not a symbolic link. One that is, is refused
  as it stands: the kernel lists mount points under their real paths, and a
  filesystem mounted inside the directory the link leads to would not be found
  under the name that was given;
* the state directory, ``data``, ``uploads`` and ``vault`` are on one filesystem,
  **all the way down**: no filesystem is mounted on or anywhere inside
  ``uploads`` or ``vault`` (``uploads/practical`` on a volume of its own, for
  one), and none of them, nor the database, is a symbolic link. The restore
  replaces the live state with renames and one hard link. They are atomic only
  inside a filesystem, and a directory that is renamed takes a filesystem
  mounted inside it along: the restored files would then be written to the
  wrong disk, without any error;
* there is room for a second copy of the snapshot next to the live state.

Exit status: 0 nothing found, 1 something found, 2 wrong arguments.

A second use, by the same script, during one run:

    python3 -m src.restore_preflight --fingerprint PATH [PATH ...] [--expect DIGEST]

prints one SHA-256 over the bytes of the files named and over every file of the
directory trees named (path and bytes). With ``--expect`` it compares instead,
and exits 1 when the digest differs. The script keeps the digest in a shell
variable between examining something and replacing something, so that what it
replaces is byte for byte what it examined. The digest is never written to
disk and nothing reads one from disk: it says nothing to a later run.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import sqlite3
import sys
import tarfile
import zlib
from pathlib import Path, PurePosixPath
from typing import TextIO

ARCHIVE_ROOTS = ("uploads", "vault")
# The staged database is written twice at most (the copy, then its write-ahead
# log while it is migrated and held), and the rest is headroom for SQLite's
# sidecar files and for directories.
DATABASE_FACTOR = 2
HEADROOM_BYTES = 64 * 1024 * 1024


def snapshot_database_problem(path: Path) -> str | None:
    if not path.is_file():
        return f"the snapshot has no questions.db ({path})"
    try:
        conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
        try:
            if conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                return f"the snapshot database does not pass quick_check ({path})"
            conn.execute("SELECT COUNT(*) FROM quiz_assignments").fetchone()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return f"the snapshot database cannot be read ({path}: {exc})"
    return None


def archive_usage(path: Path) -> tuple[int, list[str]]:
    """``(bytes it extracts to, problems)``. The archive is read to its end."""
    problems: list[str] = []
    total = 0
    if not path.is_file():
        return 0, [f"the snapshot has no files.tar.gz ({path})"]
    try:
        with tarfile.open(path, "r:gz") as archive:
            for member in archive:
                name = PurePosixPath(member.name)
                if name.is_absolute() or ".." in name.parts or not name.parts or name.parts[0] not in ARCHIVE_ROOTS:
                    problems.append(f"the archive has an entry outside uploads/ and vault/: {member.name}")
                elif not (member.isreg() or member.isdir()):
                    problems.append(f"the archive has an entry that is neither a file nor a directory: {member.name}")
                else:
                    total += member.size
                if len(problems) >= 5:
                    break
    except (OSError, EOFError, tarfile.TarError, zlib.error) as exc:
        problems.append(f"the archive cannot be read to its end ({path}: {exc})")
    return total, problems


def _device(path: Path) -> int:
    return path.lstat().st_dev


def _is_mount(path: Path) -> bool:
    return os.path.ismount(path)


def _mount_points(listing: str = "/proc/self/mountinfo") -> list[Path]:
    """Every mount point the kernel lists (Linux); nothing where it keeps no such list.

    It is asked as well as ``_is_mount`` because a bind mount of a directory of
    the same filesystem has the same device number as what is around it.
    """
    try:
        with open(listing, encoding="utf-8", errors="surrogateescape") as handle:
            lines = handle.readlines()
    except OSError:
        return []
    points = []
    for line in lines:
        fields = line.split(" ")
        if len(fields) > 4:   # the fifth field, with a space written as \040
            points.append(Path(re.sub(r"\\([0-7]{3})", lambda match: chr(int(match.group(1), 8)), fields[4])))
    return points


MAX_REPORTED = 5


def state_root_link_problem(state_root: Path) -> str | None:
    """Why a state directory that is a symbolic link is not looked into at all."""
    if not state_root.is_symlink():
        return None
    return (f"the state directory {state_root} is a symbolic link (to {os.path.realpath(state_root)}): what is "
            "mounted inside it cannot be told under that name. Give the directory itself as the state directory, "
            "in the service's settings and to the restore, or make it a real directory")


def nested_filesystem_problems(state_root: Path) -> list[str]:
    """Why ``uploads``, ``vault`` and the database cannot be replaced by renaming them.

    ``uploads`` and ``vault`` are each moved aside and replaced as a whole. Every
    directory in them therefore has to be a plain directory of the state
    directory's own filesystem. ``data`` is not moved, only files in it are, so
    only ``data`` itself matters there (``data/reference`` may be a mount).

    Every path below is built on the state directory's real path, the one the
    kernel uses for it. A directory above it may be a symbolic link
    (``/var/lib`` on another disk); compared under the name that goes through
    that link, a mount point from the kernel's list would never match, and a
    filesystem mounted inside ``vault`` would pass. The state directory itself
    may not be a link, so that its real path differs from its name only above it.
    """
    link = state_root_link_problem(state_root)
    if link:
        return [link]
    state_root = Path(os.path.realpath(state_root))
    device = _device(state_root)
    data, database = state_root / "data", state_root / "data" / "questions.db"
    moved = [tree for tree in (state_root / "uploads", state_root / "vault") if os.path.lexists(tree)]
    problems = []

    def why_not(path: Path, tree: Path) -> str | None:
        whole = f"the restore replaces {tree.name} as a whole, by renaming it"
        if path.is_symlink():
            return (f"{path} is a symbolic link: {whole}, which would replace the link and leave what it "
                    "points to where it is")
        if _is_mount(path) or _device(path) != device:
            return (f"a filesystem is mounted at {path}: {whole}, which would set that filesystem aside with it "
                    f"and write the restored files to the filesystem of {state_root} instead")
        return None

    for tree in moved:
        found = why_not(tree, tree)
        if found:
            problems.append(found)
            continue   # not walked: what is in it is on the other side of that
        for directory, subdirectories, _files in os.walk(tree, followlinks=False):
            for name in subdirectories:
                found = why_not(Path(directory) / name, tree)
                if found:
                    problems.append(found)
            if len(problems) >= MAX_REPORTED:
                return problems
    if data.is_symlink() or _is_mount(data) or _device(data) != device:
        problems.append(f"{data} is a symbolic link or on another filesystem than {state_root}: the database is "
                        "replaced by a rename and kept by a hard link, which work only inside one filesystem")
    if database.is_symlink():
        problems.append(f"{database} is a symbolic link: the rename would replace the link, not the database")
    # The kernel's list catches what a device number does not: a bind mount inside the same filesystem.
    for point in _mount_points():
        inside = [tree for tree in moved if point == tree or tree in point.parents]
        if (inside or point == data) and not any(str(point) in problem for problem in problems):
            problems.append(f"a filesystem is mounted at {point}, inside what the restore replaces by renaming")
    return problems[:MAX_REPORTED]


def layout_problems(state_root: Path) -> list[str]:
    link = state_root_link_problem(state_root)
    if link:
        return [link]   # before anything else is asked of what it points to
    problems = []
    data = state_root / "data"
    for directory in (state_root, data):
        if not directory.is_dir():
            problems.append(f"not a directory: {directory}")
        elif not os.access(directory, os.W_OK | os.X_OK):
            problems.append(f"not writable by this user: {directory}")
    if problems:
        return problems
    return nested_filesystem_problems(state_root)


def preflight(snapshot: Path, state_root: Path, out: TextIO) -> int:
    database = snapshot / "questions.db"
    problems = []
    problem = snapshot_database_problem(database)
    if problem:
        problems.append(problem)
    extracted, archive_problems = archive_usage(snapshot / "files.tar.gz")
    problems += archive_problems
    problems += layout_problems(state_root)

    if not problems:
        needed = DATABASE_FACTOR * database.stat().st_size + extracted + HEADROOM_BYTES
        free = shutil.disk_usage(state_root).free
        print(f"space for the copy: about {needed // 2**20} MiB needed, {free // 2**20} MiB free in {state_root}",
              file=out)
        if free < needed:
            problems.append(f"not enough free space in {state_root} for a copy of the snapshot: about "
                            f"{needed // 2**20} MiB needed, {free // 2**20} MiB free")
    for found in problems:
        print(f"FAIL: {found}", file=out)
    if problems:
        return 1
    print("OK: the snapshot is complete and readable, and the state directory can take the copy", file=out)
    return 0


def fingerprint(paths: list[Path]) -> str:
    """One digest over the bytes of each file and over every file (path and bytes) of each tree."""
    digest = hashlib.sha256()

    def add_file(path: Path) -> None:
        with open(path, "rb") as handle:
            for block in iter(lambda: handle.read(1 << 20), b""):
                digest.update(block)

    for path in paths:
        if path.is_symlink() or not path.exists():
            raise OSError(f"not a file or a directory: {path}")
        if path.is_dir():
            digest.update(b"<tree>")
            for entry in sorted(path.rglob("*")):
                relative = entry.relative_to(path).as_posix().encode("utf-8", "surrogateescape")
                if entry.is_symlink():
                    digest.update(b"<link>" + relative + b"\0" + os.readlink(entry).encode("utf-8", "surrogateescape"))
                elif entry.is_dir():
                    digest.update(b"<dir>" + relative + b"\0")
                else:
                    digest.update(b"<file>" + relative + b"\0")
                    add_file(entry)
                    digest.update(b"\0")
        else:
            digest.update(b"<file>")
            add_file(path)
        digest.update(b"<end>")
    return digest.hexdigest()


def main(argv: list[str] | None = None, *, out: TextIO | None = None) -> int:
    out = sys.stdout if out is None else out
    parser = argparse.ArgumentParser(prog="restore_preflight", description=__doc__.splitlines()[0])
    parser.add_argument("--snapshot", type=Path, help="A snapshot-<stamp> directory.")
    parser.add_argument("--state-root", type=Path,
                        help="The state directory: its data, uploads and vault are what a restore replaces.")
    parser.add_argument("--fingerprint", nargs="+", type=Path, metavar="PATH",
                        help="Print one digest of these files and trees instead of checking a snapshot.")
    parser.add_argument("--expect", metavar="DIGEST", help="With --fingerprint: compare, exit 1 if it differs.")
    args = parser.parse_args(argv)
    if args.fingerprint:
        if args.snapshot or args.state_root:
            parser.error("--fingerprint is used alone")
        try:
            found = fingerprint(args.fingerprint)
        except OSError as exc:
            print(f"FAIL: cannot be read ({exc})", file=out)
            return 1
        if args.expect is None:
            print(found, file=out)
            return 0
        if found != args.expect:
            print("FAIL: changed since it was examined: " + ", ".join(map(str, args.fingerprint)), file=out)
            return 1
        return 0
    if args.expect is not None or not (args.snapshot and args.state_root):
        parser.error("--snapshot and --state-root are both required")
    return preflight(args.snapshot, args.state_root, out)


if __name__ == "__main__":
    raise SystemExit(main())
