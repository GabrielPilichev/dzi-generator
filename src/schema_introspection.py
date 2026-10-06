"""Read-only SQLite schema introspection shared by the migration runner and tests.

``table_shape`` describes one table the way the application observes it: ordered
columns (name, declared type, NOT NULL, default, primary-key position), foreign
keys, indexes, CHECK constraints and AUTOINCREMENT. Only whitespace and SQL
comments are normalised away; nothing here writes to the database.
"""

from __future__ import annotations

import re
import sqlite3

INTERNAL_TABLES = frozenset({"sqlite_sequence", "sqlite_stat1", "sqlite_stat4"})


def normalize_sql(sql: str | None) -> str:
    """Drop SQL comments, collapse whitespace and trim it next to punctuation."""
    sql = re.sub(r"--[^\n]*", "", sql or "")
    sql = re.sub(r"\s+", " ", sql).strip()
    return re.sub(r"\s*([(),])\s*", r"\1", sql)


def check_constraints(table_sql: str | None) -> list[str]:
    """Normalised bodies of every CHECK (...) clause, in declaration order."""
    checks = []
    text = re.sub(r"--[^\n]*", "", table_sql or "")
    for match in re.finditer(r"\bCHECK\s*\(", text, re.IGNORECASE):
        depth, quote, index = 1, None, match.end()
        while index < len(text) and depth:
            char = text[index]
            if quote:
                quote = None if char == quote else quote
            elif char in "'\"":
                quote = char
            elif char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
            index += 1
        checks.append(normalize_sql(text[match.end():index - 1]))
    return checks


def _index_shape(conn: sqlite3.Connection, name: str, unique: int, origin: str, partial: int) -> dict:
    rows = conn.execute(f'PRAGMA index_xinfo("{name}")').fetchall()
    key_columns = tuple((row[2], row[3], row[4]) for row in rows if row[5])  # name, DESC, collation
    predicate = None
    if partial:
        sql = conn.execute("SELECT sql FROM sqlite_master WHERE name = ?", (name,)).fetchone()[0]
        predicate = normalize_sql(re.split(r"\bWHERE\b", sql, maxsplit=1, flags=re.IGNORECASE)[1])
    return {"unique": bool(unique), "origin": origin, "columns": key_columns, "where": predicate}


def user_table_names(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name").fetchall()
    return [row[0] for row in rows if row[0] not in INTERNAL_TABLES]


def table_shape(conn: sqlite3.Connection, name: str) -> dict:
    sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone()[0]
    named, automatic = {}, []
    for _seq, index_name, unique, origin, partial in conn.execute(f'PRAGMA index_list("{name}")').fetchall():
        shape = _index_shape(conn, index_name, unique, origin, partial)
        if origin == "c":
            named[index_name] = shape
        else:  # table-level UNIQUE / PRIMARY KEY: SQLite chooses the index name
            automatic.append(shape)
    return {
        "columns": [tuple(row[1:]) for row in conn.execute(f'PRAGMA table_info("{name}")')],
        "foreign_keys": sorted(tuple(row[2:]) for row in conn.execute(f'PRAGMA foreign_key_list("{name}")')),
        "indexes": named,
        "auto_indexes": sorted(automatic, key=repr),
        "checks": check_constraints(sql),
        "autoincrement": bool(re.search(r"\bAUTOINCREMENT\b", sql or "", re.IGNORECASE)),
        "without_rowid": bool(re.search(r"\bWITHOUT\s+ROWID\b", sql or "", re.IGNORECASE)),
    }


def shape_requirement_problems(expected: dict, actual: dict) -> list[str]:
    """What ``actual`` lacks compared with ``expected``; extra objects are allowed.

    Extras are legitimate for a baseline check: a migrated database has columns
    and indexes that the pre-001 baseline does not.
    """
    problems = []
    actual_columns = {column[0]: column for column in actual["columns"]}
    for column in expected["columns"]:
        found = actual_columns.get(column[0])
        if found is None:
            problems.append(f"column {column[0]} is missing")
        elif found != column:
            problems.append(f"column {column[0]} differs (expected {column[1:]}, found {found[1:]})")
    for foreign_key in expected["foreign_keys"]:
        if foreign_key not in actual["foreign_keys"]:
            problems.append(f"foreign key {foreign_key[:3]} is missing or differs")
    for index_name, index in expected["indexes"].items():
        if actual["indexes"].get(index_name) != index:
            problems.append(f"index {index_name} is missing or differs")
    for index in expected["auto_indexes"]:
        if index not in actual["auto_indexes"]:
            problems.append(f"unique/primary-key constraint on {[c[0] for c in index['columns']]} is missing")
    for check in expected["checks"]:
        if check not in actual["checks"]:
            problems.append(f"CHECK ({check[:60]}) is missing or differs")
    if expected["autoincrement"] and not actual["autoincrement"]:
        problems.append("AUTOINCREMENT is missing")
    return problems
