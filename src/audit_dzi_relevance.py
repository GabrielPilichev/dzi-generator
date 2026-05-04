import sqlite3
from pathlib import Path

DB_PATH = Path("data/questions.db")

FIELDS = [
    "class",
    "module",
    "section_slug",
    "title",
    "title_bg",
    "is_dzi_relevant",
    "dzi_relevance_verified",
    "source_authority",
    "source_url",
    "dzi_relevance_notes",
]

def table_columns(con, table):
    return {row[1] for row in con.execute(f"PRAGMA table_info({table})").fetchall()}

def expr(col, existing, default="''"):
    if col in existing:
        return f"COALESCE({col}, {default}) AS {col}"
    return f"{default} AS {col}"

def main():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row

    cols = table_columns(con, "curriculum_sections")

    title_expr = "COALESCE(title, title_bg, '') AS title" if "title" in cols else "COALESCE(title_bg, '') AS title"

    select_parts = [
        expr("class", cols, "NULL"),
        expr("module", cols, "''"),
        expr("section_slug", cols, "''"),
        title_expr,
        expr("is_dzi_relevant", cols, "0"),
        expr("dzi_relevance_verified", cols, "0"),
        expr("source_authority", cols, "''"),
        expr("source_url", cols, "''"),
        expr("dzi_relevance_notes", cols, "''"),
    ]

    order_parts = [c for c in ["class", "module", "display_order", "section_slug"] if c in cols]
    order_sql = ", ".join(order_parts) if order_parts else "title"

    query = f"""
    SELECT
      {", ".join(select_parts)}
    FROM curriculum_sections
    ORDER BY {order_sql};
    """

    rows = con.execute(query).fetchall()

    print(f"Sections: {len(rows)}")
    print()

    for row in rows:
        marker = "✅ VERIFIED" if row["dzi_relevance_verified"] else "⚠️  UNVERIFIED"
        old_flag = "old_is_dzi=1" if row["is_dzi_relevant"] else "old_is_dzi=0"

        print(f"[{marker}] class={row['class']} module={row['module'] or '—'} {old_flag}")
        print(f"  slug:  {row['section_slug']}")
        print(f"  title: {row['title']}")
        print(f"  auth:  {row['source_authority'] or '—'}")
        print(f"  url:   {row['source_url'] or '—'}")
        if row["dzi_relevance_notes"]:
            print(f"  note:  {row['dzi_relevance_notes']}")
        print()

    con.close()

if __name__ == "__main__":
    main()
