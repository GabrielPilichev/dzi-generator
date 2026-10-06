"""Logical SQLite schema snapshot and comparison, independent of file bytes.

``snapshot(conn)`` captures what the application can observe about a schema:
tables with ordered columns (declared type, NOT NULL, default, primary-key
position), foreign keys, indexes (uniqueness, key columns, sort order, partial
predicates), CHECK constraints, AUTOINCREMENT, triggers and views. Only
irrelevant SQL whitespace is normalised. ``compare(a, b)`` returns every
difference as a readable line; an empty list means the schemas are identical.

Run ``python3 -m tests.schema_snapshot --left A.db --right B.db`` for a compact
PASS/FAIL report. Both databases are opened read-only.
"""

import argparse
import sqlite3
import sys
from pathlib import Path

from src.schema_introspection import normalize_sql, table_shape, user_table_names


def snapshot(conn):
    tables = {name: table_shape(conn, name) for name in user_table_names(conn)}
    other = {
        (kind, name): normalize_sql(sql)
        for kind, name, sql in conn.execute(
            "SELECT type, name, sql FROM sqlite_master WHERE type IN ('trigger', 'view')"
        )
    }
    return {"tables": tables, "other": other}


def compare(left, right, left_name="left", right_name="right"):
    """Every logical difference between two snapshots; [] when identical."""
    problems = []
    for name in sorted(set(left["tables"]) | set(right["tables"])):
        a, b = left["tables"].get(name), right["tables"].get(name)
        if a is None or b is None:
            problems.append(f"table {name}: only in {right_name if a is None else left_name}")
            continue
        for key in ("columns", "foreign_keys", "auto_indexes", "checks", "autoincrement", "without_rowid"):
            if a[key] != b[key]:
                problems.append(f"table {name}: {key} differ\n    {left_name}: {a[key]}\n    {right_name}: {b[key]}")
        for index in sorted(set(a["indexes"]) | set(b["indexes"])):
            if a["indexes"].get(index) != b["indexes"].get(index):
                problems.append(
                    f"index {index}: {left_name}={a['indexes'].get(index)} {right_name}={b['indexes'].get(index)}"
                )
    for key in sorted(set(left["other"]) | set(right["other"])):
        if left["other"].get(key) != right["other"].get(key):
            problems.append(f"{key[0]} {key[1]} differs")
    return problems


def open_read_only(path):
    return sqlite3.connect(f"{Path(path).resolve().as_uri()}?mode=ro", uri=True)


def report(left_conn, right_conn):
    """Compact per-category PASS/FAIL lines; a list of difference strings on failure."""
    a, b = snapshot(left_conn), snapshot(right_conn)
    names = sorted(set(a["tables"]) | set(b["tables"]))
    both = [n for n in names if n in a["tables"] and n in b["tables"]]

    def same(selector):
        return all(selector(a["tables"][n]) == selector(b["tables"][n]) for n in both)

    categories = [
        ("tables", set(a["tables"]) == set(b["tables"]), f"{len(a['tables'])} vs {len(b['tables'])}"),
        ("columns", same(lambda t: t["columns"]), f"{sum(len(a['tables'][n]['columns']) for n in both)} columns"),
        ("primary keys", same(lambda t: [c[4] for c in t["columns"]]), ""),
        ("foreign keys", same(lambda t: t["foreign_keys"]), ""),
        ("indexes", same(lambda t: t["indexes"]), f"{sum(len(a['tables'][n]['indexes']) for n in both)} explicit"),
        ("unique/partial", same(lambda t: (t["auto_indexes"], t["indexes"])), ""),
        ("CHECK", same(lambda t: t["checks"]), ""),
        ("AUTOINCREMENT", same(lambda t: t["autoincrement"]), ""),
        ("triggers/views", a["other"] == b["other"], ""),
    ]
    lines = [f"{'PASS' if ok else 'FAIL'}  {label:<15}{detail}" for label, ok, detail in categories]
    return lines, compare(a, b, "left", "right")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--left", required=True)
    parser.add_argument("--right", required=True)
    args = parser.parse_args(argv)
    left, right = open_read_only(args.left), open_read_only(args.right)
    try:
        lines, problems = report(left, right)
    finally:
        left.close()
        right.close()
    print("\n".join(lines))
    for problem in problems:
        print(problem)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
