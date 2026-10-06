"""Stable-ID allocation and explicit-ID claims for content importers (G2).

Canonical ids never change and are never reused (``docs/CANONICAL_DATA.md``). An importer
that creates a NEW identity-bearing row calls ``new_row_id``:

* no requested id  -> ``allocate_id``: one above the canonical maximum/sequence **and** the
  target database's maximum/sequence (``canonical_tables.next_free_id``), so it can collide
  with neither a canonical row the target lacks nor a historical id the target once used;
* a requested id   -> ``claim_id``: it must be a positive integer that is free in the target.
  An id at or below a recorded high-water mark is a historical id; it is accepted only to
  restore exactly the canonical row that owns it (same natural key), never to recycle it
  for different content. Anything else raises ``IdConflict`` before any insertion.

Existing rows are never touched here: importers match their natural key first and keep the
existing id. This module only decides ids for rows that genuinely do not exist yet, and never
writes to the database.
"""

from __future__ import annotations

import sqlite3
from functools import lru_cache
from pathlib import Path
from typing import Mapping

try:
    from src import canonical_export as export
    from src import canonical_tables as ct
except ModuleNotFoundError:  # pragma: no cover - script-style import
    import canonical_export as export
    import canonical_tables as ct


class IdConflict(ValueError):
    """A requested explicit id would collide with, or recycle, an existing identity."""


@lru_cache(maxsize=8)
def _canonical_manifest(directory: str) -> dict:
    try:
        return export.load_manifest(Path(directory))
    except ct.CanonicalError:
        return {"tables": {}}


@lru_cache(maxsize=64)
def _canonical_rows_by_id(directory: str, table: str) -> dict[int, dict]:
    entry = _canonical_manifest(directory)["tables"].get(table)
    if not entry or entry["primary_key"] != ["id"]:
        return {}
    return {row["id"]: row for row in ct.read_table_rows(Path(directory) / entry["file"])}


def canonical_entry(table: str, canonical_dir: Path = ct.CANONICAL_DIR) -> dict:
    """The manifest entry of ``table`` (``{}`` when the directory or table is unavailable)."""
    return _canonical_manifest(str(canonical_dir))["tables"].get(table, {})


def allocate_id(conn: sqlite3.Connection, table: str, canonical_dir: Path = ct.CANONICAL_DIR) -> int:
    """The first id a NEW ``table`` row may take (see the module docstring)."""
    return ct.next_free_id(canonical_entry(table, canonical_dir), conn, table)


def claim_id(conn: sqlite3.Connection, table: str, requested: int,
             natural_key: Mapping[str, object] | None = None,
             canonical_dir: Path = ct.CANONICAL_DIR) -> int:
    """Validate an explicit id for a new row; return it or raise ``IdConflict``."""
    if type(requested) is not int or requested < 1:
        raise IdConflict(f"{table}: explicit id must be a positive integer, got {requested!r}")
    if conn.execute(f'SELECT 1 FROM "{table}" WHERE id = ?', (requested,)).fetchone():
        raise IdConflict(f"{table}: id {requested} already exists in the target database")
    entry = canonical_entry(table, canonical_dir)
    high_water = max(entry.get("max_id") or 0, entry.get("sqlite_sequence") or 0,
                     ct.stored_sequence(conn, table) or 0)
    if requested <= high_water:
        owner = _canonical_rows_by_id(str(canonical_dir), table).get(requested)
        if owner is None:
            raise IdConflict(f"{table}: id {requested} was used before; ids are never recycled")
        if natural_key is None or any(owner.get(column) != value for column, value in natural_key.items()):
            raise IdConflict(f"{table}: id {requested} belongs to different canonical content")
    return requested


def new_row_id(conn: sqlite3.Connection, table: str, requested: int | None = None,
               natural_key: Mapping[str, object] | None = None,
               canonical_dir: Path = ct.CANONICAL_DIR) -> int:
    """The id for a new row: the validated ``requested`` one, else a freshly allocated safe id."""
    if requested is None:
        return allocate_id(conn, table, canonical_dir)
    return claim_id(conn, table, requested, natural_key, canonical_dir)
