from pathlib import Path
import re
import sqlite3

DB = Path("data/questions.db")
VAULT = Path("vault")
FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)

def parse_simple_frontmatter(text: str) -> dict:
    m = FRONTMATTER_RE.match(text)
    if not m:
        return {}
    fm = {}
    for line in m.group(1).splitlines():
        line = line.rstrip()
        if not line or line.lstrip().startswith("#"):
            continue
        if line.startswith(" ") or line.startswith("\t"):
            continue
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        fm[key.strip()] = value.strip().strip('"').strip("'")
    return fm

conn = sqlite3.connect(DB)
cur = conn.cursor()

area_lookup = {
    area_slug: area_id
    for area_id, area_slug in cur.execute("SELECT id, area_id FROM curriculum_areas")
}

rows = cur.execute("""
    SELECT id, topic_slug, note_path, area_id
    FROM curriculum_topics
    ORDER BY topic_slug
""").fetchall()

updated = 0
missing_note = 0
missing_area = 0
already_ok = 0

for topic_id, slug, note_path, current_area_id in rows:
    if current_area_id is not None:
        already_ok += 1
        continue

    rel = note_path or f"Topics/{slug}.md"
    path = VAULT / rel

    if not path.exists():
        missing_note += 1
        print(f"MISSING NOTE: {slug} -> {rel}")
        continue

    fm = parse_simple_frontmatter(path.read_text(encoding="utf-8"))
    area_slug = fm.get("area") or fm.get("parent_topic")

    if not area_slug or area_slug not in area_lookup:
        missing_area += 1
        print(f"NO AREA MATCH: {slug} area={area_slug!r}")
        continue

    cur.execute(
        "UPDATE curriculum_topics SET area_id=? WHERE id=?",
        (area_lookup[area_slug], topic_id),
    )
    updated += 1
    print(f"UPDATED: {slug} -> {area_slug}")

conn.commit()
conn.close()

print()
print(f"Already had area: {already_ok}")
print(f"Updated: {updated}")
print(f"Missing note: {missing_note}")
print(f"No area match: {missing_area}")
