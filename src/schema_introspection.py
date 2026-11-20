"""Read-only SQLite schema introspection shared by the migration runner and tests.

``table_shape`` describes one table the way the application observes it: ordered
columns (name, declared type, NOT NULL, default, primary-key position), foreign
keys, indexes, CHECK constraints and AUTOINCREMENT. Only whitespace and SQL
comments are normalised away; nothing here writes to the database.

An *expression* index key (``PRAGMA index_xinfo`` column ``cid = -2``) has no
column name, so its (name, DESC, collation) triple cannot tell ``COALESCE(a, 0)``
from ``a + 0``. Such an index additionally records ``expression_identity``: its
own ``CREATE INDEX`` text from ``sqlite_master.sql``, with whitespace and comments
normalised only *outside* quoted text (see :func:`statement_identity`); its
partial predicate comes from the same quote- and comment-aware scan
(:func:`partial_predicate`). The identity is syntactic and deliberately conservative: it never claims that two
different spellings are equivalent, so an equivalent but differently written
expression compares unequal. Ordinary and automatic index shapes are unchanged.
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


class ExpressionIdentityError(ValueError):
    """A safe identity for an expression index could not be obtained (fail closed)."""


_QUOTES = {"'": "'", '"': '"', "`": "`", "[": "]"}
_LAYOUT = " \t\n\f\r"            # SQLite's own whitespace; Python's \s also matches identifier characters
_LAYOUT_RUN = re.compile(r"[ \t\n\f\r]+")
_IDENTIFIER_CHARACTER = "A-Za-z0-9_$\u0080-\U0010ffff"     # SQLite treats every byte >= 0x80 as part of a name
_WHERE_KEYWORD = re.compile(rf"(?<![{_IDENTIFIER_CHARACTER}])WHERE(?![{_IDENTIFIER_CHARACTER}])", re.IGNORECASE)


def _scan_statement(sql: str | None) -> list[tuple[bool, str]]:
    """Split a statement into ``(quoted, text)`` pieces; a comment becomes one space of plain text.

    Quoted text ('..', "..", `..` with a doubled quote as the escape, [..] without
    one) is returned whole and untouched. Raises :class:`ExpressionIdentityError`
    for a missing statement or an unterminated quote or block comment.
    """
    if sql is None or not sql.strip(_LAYOUT):
        raise ExpressionIdentityError("no CREATE INDEX statement to take an identity from")
    pieces, plain, index, length = [], [], 0, len(sql)

    def flush() -> None:
        if plain:
            pieces.append((False, "".join(plain)))
            plain.clear()

    while index < length:
        char = sql[index]
        if char in _QUOTES:
            closing, end = _QUOTES[char], index + 1
            while True:
                end = sql.find(closing, end)
                if end < 0:
                    raise ExpressionIdentityError("unterminated quoted text in the index statement")
                if closing != "]" and sql[end + 1:end + 2] == closing:  # doubled quote: escaped, keep going
                    end += 2
                    continue
                break
            flush()
            pieces.append((True, sql[index:end + 1]))
            index = end + 1
        elif sql.startswith("--", index):
            end = sql.find("\n", index)
            plain.append(" ")
            index = length if end < 0 else end
        elif sql.startswith("/*", index):
            end = sql.find("*/", index + 2)
            if end < 0:
                raise ExpressionIdentityError("unterminated comment in the index statement")
            plain.append(" ")
            index = end + 2
        else:
            plain.append(char)
            index += 1
    flush()
    return pieces


def _layout_normalised(pieces: list[tuple[bool, str]]) -> str:
    out = []
    for quoted, text in pieces:
        if not quoted:
            text = _LAYOUT_RUN.sub(" ", text)
            text = re.sub(r" ?([(),]) ?", r"\1", text)
        out.append(text)
    return "".join(out).strip(_LAYOUT)


def statement_identity(sql: str | None) -> str:
    """Normalise one statement so only layout, never content, is ignored.

    Quoted text ('...', "...", `...` with a doubled quote as the escape, and
    [...] without one) is kept byte for byte, so spaces, ``--``, ``/*`` and
    ``WHERE`` inside a literal or quoted identifier are never touched and two
    different literals never compare equal. Outside quotes, ``--`` and
    ``/* */`` comments count as one space, runs of SQLite whitespace (space, tab,
    newline, form feed, carriage return) collapse to one space and whitespace
    next to ``(``, ``)`` and ``,`` is dropped (as ``normalize_sql`` does). Other
    characters, such as U+00A0, are identifier characters in SQLite and are never
    treated as whitespace, also not at the edges. Case is significant and no
    expression is parsed or reordered. Raises :class:`ExpressionIdentityError`
    for a missing statement or an unterminated quote or block comment instead of
    guessing.
    """
    return _layout_normalised(_scan_statement(sql))


def partial_predicate(sql: str | None) -> str:
    """The normalised WHERE predicate of a ``CREATE INDEX`` statement, found by the same scan.

    It is the text after the first ``WHERE`` keyword that is outside quotes and
    comments and outside parentheses, normalised like :func:`statement_identity`
    (quoted text untouched). A ``WHERE`` inside a comment, a literal or a quoted
    name is not the predicate. Raises :class:`ExpressionIdentityError` when there
    is no such keyword.
    """
    pieces, depth = _scan_statement(sql), 0
    for position, (quoted, text) in enumerate(pieces):
        if quoted:
            continue
        for token in re.finditer(r"[()]|" + _WHERE_KEYWORD.pattern, text, re.IGNORECASE):
            if token.group() == "(":
                depth += 1
            elif token.group() == ")":
                depth -= 1
            elif depth == 0:
                return _layout_normalised([(False, text[token.end():])] + pieces[position + 1:])
    raise ExpressionIdentityError("partial index statement has no WHERE keyword outside quotes and comments")


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
    if any(row[1] == -2 for row in rows if row[5]):  # cid -2 marks an expression key
        found = conn.execute("SELECT sql FROM sqlite_master WHERE type = 'index' AND name = ?", (name,)).fetchone()
        sql = found[0] if found else None
        return {"unique": bool(unique), "origin": origin, "columns": key_columns,
                "where": partial_predicate(sql) if partial else None, "expression_identity": statement_identity(sql)}
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
