import csv
import sqlite3
from datetime import date
from pathlib import Path

DB_PATH = Path("data/questions.db")
CSV_PATH = Path("data/dzi_relevance_audit.csv")
MD_PATH = Path("vault/Generated/Audits/dzi-relevance-audit.md")

BASE_FIELDS = [
    "class",
    "module",
    "order_index",
    "section_slug",
    "title",
    "is_dzi_relevant",
    "dzi_relevance_verified",
    "source_authority",
    "source_title",
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

    select_parts = [
        expr("class", cols, "NULL"),
        expr("module", cols, "''"),
        expr("order_index", cols, "0"),
        expr("section_slug", cols, "''"),
        expr("title", cols, "''"),
        expr("is_dzi_relevant", cols, "0"),
        expr("dzi_relevance_verified", cols, "0"),
        expr("source_authority", cols, "''"),
        expr("source_title", cols, "''"),
        expr("source_url", cols, "''"),
        expr("dzi_relevance_notes", cols, "''"),
    ]

    order_parts = []
    for col in ["class", "module", "order_index", "section_slug"]:
        if col in cols:
            order_parts.append(col)
    order_sql = ", ".join(order_parts) if order_parts else "title"

    query = f"""
    SELECT
      {", ".join(select_parts)}
    FROM curriculum_sections
    ORDER BY {order_sql};
    """

    rows = con.execute(query).fetchall()
    con.close()

    CSV_PATH.parent.mkdir(parents=True, exist_ok=True)
    MD_PATH.parent.mkdir(parents=True, exist_ok=True)

    with CSV_PATH.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=BASE_FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row[key] for key in BASE_FIELDS})

    verified = sum(1 for row in rows if row["dzi_relevance_verified"])
    old_dzi = sum(1 for row in rows if row["is_dzi_relevant"])

    lines = []
    lines.append("---")
    lines.append("type: dzi_relevance_audit")
    lines.append(f"date: {date.today().isoformat()}")
    lines.append("source: SQLite curriculum_sections")
    lines.append("---")
    lines.append("")
    lines.append("# ДЗИ релевантност — одит")
    lines.append("")
    lines.append(f"- Общо раздели: **{len(rows)}**")
    lines.append(f"- Стари `is_dzi_relevant = 1`: **{old_dzi}**")
    lines.append(f"- Официално проверени: **{verified}**")
    lines.append("")
    lines.append("> Важно: `is_dzi_relevant` не трябва да се използва за ДЗИ генериране, докато `dzi_relevance_verified` не е 1 и няма официален източник.")
    lines.append("")
    lines.append("## Раздели")
    lines.append("")

    current_class = object()
    for row in rows:
        row_class = row["class"] if row["class"] is not None else "без клас"
        if row_class != current_class:
            current_class = row_class
            lines.append(f"## {row_class}. клас")
            lines.append("")

        status = "✅ проверено" if row["dzi_relevance_verified"] else "⚠️ непроверено"
        old_flag = "да" if row["is_dzi_relevant"] else "не"

        lines.append(f"### {row['title']}")
        lines.append("")
        lines.append(f"- Статус: **{status}**")
        lines.append(f"- Стар ДЗИ флаг: `{old_flag}`")
        lines.append(f"- Модул: `{row['module'] or '—'}`")
        lines.append(f"- Slug: `{row['section_slug']}`")
        lines.append(f"- Източник: {row['source_authority'] or '—'}")
        lines.append(f"- Заглавие на източник: {row['source_title'] or '—'}")
        lines.append(f"- URL: {row['source_url'] or '—'}")
        lines.append(f"- Бележка: {row['dzi_relevance_notes'] or '—'}")
        lines.append("")

    MD_PATH.write_text("\n".join(lines), encoding="utf-8")

    print(f"Wrote {CSV_PATH}")
    print(f"Wrote {MD_PATH}")
    print(f"Sections: {len(rows)}")
    print(f"Old is_dzi_relevant=1: {old_dzi}")
    print(f"Verified: {verified}")

if __name__ == "__main__":
    main()
