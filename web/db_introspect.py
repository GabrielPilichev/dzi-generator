"""Small SQLite schema-introspection helpers shared by web modules.

Low-level like ``web.db``: standard library only, never ``web.app`` or a
blueprint, and every function works on a connection the caller owns.
"""

import sqlite3

ALLOWED_INTERNAL_TABLES = {
    "multiple_choice_options",
    "practical_submission_grades",
    "practical_submissions",
    "questions",
    "quiz_text_answers",
}


def sqlite_table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    ).fetchone()
    return row is not None


def sqlite_table_columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
    if table_name not in ALLOWED_INTERNAL_TABLES:
        raise ValueError(f"Unsupported internal table name: {table_name}")
    # SAFE: table name is selected from ALLOWED_INTERNAL_TABLES.
    return {
        row["name"]
        for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    }


def first_existing_column(columns: set[str], candidates: tuple[str, ...]) -> str | None:
    for candidate in candidates:
        if candidate in columns:
            return candidate
    return None
