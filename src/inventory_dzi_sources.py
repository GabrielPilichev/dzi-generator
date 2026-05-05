#!/usr/bin/env python3
"""Inventory official DZI source files without parsing their contents."""

from __future__ import annotations

import argparse
import hashlib
import mimetypes
import sqlite3
from dataclasses import dataclass
from pathlib import Path


DEFAULT_DB_PATH = Path("data/questions.db")
DEFAULT_FOLDER = Path("data/reference/dzi/official_pdfs")
FORMAT_VERSION = "dzi_it_pp_2025_format"
SUBJECT = "informatika_it"
LEVEL = "DZI"

KIND_TO_SOURCE_KIND = {
    "exam": "exam_pdf",
    "answers": "answer_key_pdf",
    "practical": "practical_archive",
    "rubric": "rubric",
    "other": "other",
}
KIND_TO_ROLE = {
    "exam": "source_pdf",
    "answers": "answer_key",
    "practical": "practical_archive",
    "rubric": "rubric",
    "other": "other",
}
SESSION_BY_PREFIX = {
    "may": "may",
    "aug": "august",
}
ARCHIVE_SUFFIXES = {".zip", ".rar", ".7z"}


@dataclass(frozen=True)
class SourceFile:
    path: Path
    local_path: str
    original_filename: str
    source_slug: str
    kind: str
    source_kind: str
    role: str
    year: int
    session: str
    variant: int
    sha256: str
    file_size: int
    mime_type: str | None
    asset_type: str


@dataclass
class Counters:
    scanned_files: int = 0
    matched_files: int = 0
    skipped_no_exam: int = 0
    official_sources_inserted: int = 0
    official_sources_updated: int = 0
    assets_inserted: int = 0
    assets_updated: int = 0
    asset_links_inserted: int = 0
    asset_links_updated: int = 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Inventory official DZI source files and link them to exams."
    )
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH))
    parser.add_argument("--folder", default=str(DEFAULT_FOLDER))
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def asset_type_for_path(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return "pdf"
    if suffix in ARCHIVE_SUFFIXES:
        return "archive"
    return "other"


def parse_source_filename(path: Path) -> tuple[str, str, int, str, int] | None:
    parts = path.stem.split("_")
    if len(parts) < 4:
        return None

    kind = parts[-1]
    if kind not in KIND_TO_SOURCE_KIND:
        kind = "other"
        source_slug = path.stem
    else:
        source_slug = "_".join(parts[:-1])

    slug_parts = source_slug.split("_")
    if len(slug_parts) != 3:
        return None

    session = SESSION_BY_PREFIX.get(slug_parts[0])
    if session is None:
        return None

    try:
        year = int(slug_parts[1])
        variant_part = slug_parts[2]
        if not variant_part.startswith("v"):
            return None
        variant = int(variant_part[1:])
    except ValueError:
        return None

    return source_slug, kind, year, session, variant


def make_source_file(path: Path) -> SourceFile | None:
    parsed = parse_source_filename(path)
    if parsed is None:
        return None

    source_slug, kind, year, session, variant = parsed
    mime_type, _ = mimetypes.guess_type(path.name)
    return SourceFile(
        path=path,
        local_path=path.as_posix(),
        original_filename=path.name,
        source_slug=source_slug,
        kind=kind,
        source_kind=KIND_TO_SOURCE_KIND[kind],
        role=KIND_TO_ROLE[kind],
        year=year,
        session=session,
        variant=variant,
        sha256=sha256_file(path),
        file_size=path.stat().st_size,
        mime_type=mime_type,
        asset_type=asset_type_for_path(path),
    )


def iter_source_files(folder: Path) -> list[SourceFile]:
    if not folder.exists():
        raise RuntimeError(f"Source folder does not exist: {folder}")
    files: list[SourceFile] = []
    for path in sorted(item for item in folder.iterdir() if item.is_file()):
        source_file = make_source_file(path)
        if source_file is not None:
            files.append(source_file)
    return files


def find_exam_id(conn: sqlite3.Connection, source: SourceFile) -> int | None:
    params = (SUBJECT, LEVEL, source.year, source.session, source.variant)
    row = conn.execute(
        """
        SELECT id
        FROM exams
        WHERE subject = ?
          AND level = ?
          AND year = ?
          AND session = ?
          AND variant = ?
          AND format_version = ?
        ORDER BY id
        LIMIT 1
        """,
        (*params, FORMAT_VERSION),
    ).fetchone()
    if row is not None:
        return int(row["id"])

    row = conn.execute(
        """
        SELECT id
        FROM exams
        WHERE subject = ?
          AND level = ?
          AND year = ?
          AND session = ?
          AND variant = ?
        ORDER BY id
        LIMIT 1
        """,
        params,
    ).fetchone()
    return int(row["id"]) if row else None


def find_official_source_id(
    conn: sqlite3.Connection,
    exam_id: int,
    source_kind: str,
    local_path: str,
) -> int | None:
    row = conn.execute(
        """
        SELECT id
        FROM official_exam_sources
        WHERE exam_id = ?
          AND source_kind = ?
          AND local_path = ?
        ORDER BY id
        LIMIT 1
        """,
        (exam_id, source_kind, local_path),
    ).fetchone()
    return int(row["id"]) if row else None


def upsert_official_source(
    conn: sqlite3.Connection,
    exam_id: int,
    source: SourceFile,
    dry_run: bool,
) -> str:
    existing_id = find_official_source_id(conn, exam_id, source.source_kind, source.local_path)
    notes = "May contain both questions and answer key" if source.source_kind == "exam_pdf" else None
    if dry_run:
        return "updated" if existing_id is not None else "inserted"

    if existing_id is None:
        conn.execute(
            """
            INSERT INTO official_exam_sources (
                exam_id, authority, source_kind, local_path, sha256, notes
            )
            VALUES (?, 'MON', ?, ?, ?, ?)
            """,
            (exam_id, source.source_kind, source.local_path, source.sha256, notes),
        )
        return "inserted"

    conn.execute(
        """
        UPDATE official_exam_sources
        SET authority = 'MON',
            local_path = ?,
            sha256 = ?,
            notes = ?
        WHERE id = ?
        """,
        (source.local_path, source.sha256, notes, existing_id),
    )
    return "updated"


def find_asset_id(conn: sqlite3.Connection, local_path: str) -> int | None:
    row = conn.execute(
        """
        SELECT id
        FROM assets
        WHERE local_path = ?
        """,
        (local_path,),
    ).fetchone()
    return int(row["id"]) if row else None


def upsert_asset(conn: sqlite3.Connection, source: SourceFile, dry_run: bool) -> tuple[str, int | None]:
    existing_id = find_asset_id(conn, source.local_path)
    if dry_run:
        return ("updated" if existing_id is not None else "inserted"), existing_id

    if existing_id is None:
        conn.execute(
            """
            INSERT INTO assets (
                asset_type, original_filename, local_path, sha256, mime_type, file_size
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                source.asset_type,
                source.original_filename,
                source.local_path,
                source.sha256,
                source.mime_type,
                source.file_size,
            ),
        )
        return "inserted", int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])

    conn.execute(
        """
        UPDATE assets
        SET asset_type = ?,
            original_filename = ?,
            sha256 = ?,
            mime_type = ?,
            file_size = ?
        WHERE id = ?
        """,
        (
            source.asset_type,
            source.original_filename,
            source.sha256,
            source.mime_type,
            source.file_size,
            existing_id,
        ),
    )
    return "updated", existing_id


def find_asset_link_id(
    conn: sqlite3.Connection,
    asset_id: int,
    exam_id: int,
    role: str,
) -> int | None:
    row = conn.execute(
        """
        SELECT id
        FROM asset_links
        WHERE asset_id = ?
          AND owner_type = 'exam'
          AND owner_id = ?
          AND role = ?
          AND display_order = 0
        ORDER BY id
        LIMIT 1
        """,
        (asset_id, exam_id, role),
    ).fetchone()
    return int(row["id"]) if row else None


def planned_asset_link_action(
    conn: sqlite3.Connection,
    asset_id: int | None,
    exam_id: int,
    role: str,
) -> str:
    if asset_id is None:
        return "inserted"
    return "updated" if find_asset_link_id(conn, asset_id, exam_id, role) is not None else "inserted"


def upsert_asset_link(
    conn: sqlite3.Connection,
    asset_id: int,
    exam_id: int,
    role: str,
    dry_run: bool,
) -> str:
    existing_id = find_asset_link_id(conn, asset_id, exam_id, role)
    if dry_run:
        return "updated" if existing_id is not None else "inserted"

    if existing_id is None:
        conn.execute(
            """
            INSERT INTO asset_links (asset_id, owner_type, owner_id, role, display_order)
            VALUES (?, 'exam', ?, ?, 0)
            """,
            (asset_id, exam_id, role),
        )
        return "inserted"

    conn.execute(
        """
        UPDATE asset_links
        SET owner_type = 'exam',
            owner_id = ?,
            role = ?,
            display_order = 0
        WHERE id = ?
        """,
        (exam_id, role, existing_id),
    )
    return "updated"


def add_action(counter: Counters, prefix: str, action: str) -> None:
    setattr(counter, f"{prefix}_{action}", getattr(counter, f"{prefix}_{action}") + 1)


def inventory_sources(conn: sqlite3.Connection, sources: list[SourceFile], dry_run: bool) -> Counters:
    counters = Counters(scanned_files=len(sources))

    for source in sources:
        exam_id = find_exam_id(conn, source)
        if exam_id is None:
            counters.skipped_no_exam += 1
            print(f"skipped_no_exam: {source.local_path} ({source.source_slug})")
            continue

        counters.matched_files += 1
        official_action = upsert_official_source(conn, exam_id, source, dry_run)
        asset_action, asset_id = upsert_asset(conn, source, dry_run)
        if dry_run:
            link_action = planned_asset_link_action(conn, asset_id, exam_id, source.role)
        else:
            if asset_id is None:
                raise RuntimeError(f"Asset id missing after upsert for {source.local_path}")
            link_action = upsert_asset_link(conn, asset_id, exam_id, source.role, dry_run)

        add_action(counters, "official_sources", official_action)
        add_action(counters, "assets", asset_action)
        add_action(counters, "asset_links", link_action)

        mode = "plan" if dry_run else "registered"
        print(
            f"{mode}: exam_id={exam_id} source_slug={source.source_slug} "
            f"kind={source.kind} path={source.local_path} "
            f"official_source={official_action} asset={asset_action} asset_link={link_action}"
        )

    return counters


def print_summary(counters: Counters) -> None:
    print(f"scanned files: {counters.scanned_files}")
    print(f"matched files: {counters.matched_files}")
    print(f"skipped_no_exam: {counters.skipped_no_exam}")
    print(f"official_exam_sources inserted: {counters.official_sources_inserted}")
    print(f"official_exam_sources updated: {counters.official_sources_updated}")
    print(f"assets inserted: {counters.assets_inserted}")
    print(f"assets updated: {counters.assets_updated}")
    print(f"asset_links inserted: {counters.asset_links_inserted}")
    print(f"asset_links updated: {counters.asset_links_updated}")


def main() -> None:
    args = parse_args()
    sources = iter_source_files(Path(args.folder))

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        if args.dry_run:
            counters = inventory_sources(conn, sources, dry_run=True)
        else:
            with conn:
                counters = inventory_sources(conn, sources, dry_run=False)
        print_summary(counters)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
