import sqlite3
from pathlib import Path

DB_PATH = Path("data/questions.db")

COLUMNS = [
    ("source_url", "TEXT"),
    ("source_title", "TEXT"),
    ("source_authority", "TEXT"),
    ("dzi_relevance_verified", "INTEGER NOT NULL DEFAULT 0"),
    ("dzi_relevance_notes", "TEXT"),
]

def main():
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()

    cur.execute("PRAGMA table_info(curriculum_sections)")
    existing = {row[1] for row in cur.fetchall()}

    for name, ddl in COLUMNS:
        if name in existing:
            print(f"Already exists: curriculum_sections.{name}")
        else:
            print(f"Adding curriculum_sections.{name}")
            cur.execute(f"ALTER TABLE curriculum_sections ADD COLUMN {name} {ddl}")

    cur.execute("""
        UPDATE curriculum_sections
        SET
          dzi_relevance_verified = 0,
          dzi_relevance_notes = COALESCE(
            dzi_relevance_notes,
            'НЕПРОВЕРЕНО: is_dzi_relevant не трябва да се използва за ДЗИ генериране без официална проверка по програма на МОН.'
          )
    """)

    con.commit()
    con.close()
    print("Done.")

if __name__ == "__main__":
    main()
