"""
Migration v2 -> v3 за questions.db.

Какво прави:
  1. Backup на DB (questions.db.v2.bak)
  2. ALTER questions: добавя topic_id (nullable) и legacy_topic
  3. Копира questions.topic → questions.legacy_topic
  4. Изпълнява schema_v3.sql (нови таблици + seed данни)
  5. Sanity check

Не пипаме съществуващи редове в други таблици. Backward-compatible:
  - стария parser (parse_v2.py) продължава да работи (topic полето остава)
  - стари queries по topic си работят
  - топорят се само добавят нови колони и таблици

Употреба:
    python3 migrate_v2_to_v3.py [--db data/questions.db] [--schema src/schema_v3.sql]
"""

from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
from pathlib import Path


def has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    cur = conn.execute(f"PRAGMA table_info({table})")
    return any(row[1] == column for row in cur.fetchall())


def has_table(conn: sqlite3.Connection, table: str) -> bool:
    cur = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (table,)
    )
    return cur.fetchone() is not None


def migrate(db_path: Path, schema_path: Path) -> None:
    if not db_path.exists():
        print(f"❌ DB не намерен: {db_path}")
        sys.exit(1)
    if not schema_path.exists():
        print(f"❌ Schema не намерен: {schema_path}")
        sys.exit(1)
    
    # 1. Backup
    backup_path = db_path.with_suffix(db_path.suffix + ".v2.bak")
    shutil.copy2(db_path, backup_path)
    print(f"📦 Backup: {backup_path}")
    
    conn = sqlite3.connect(str(db_path))
    cur = conn.cursor()
    
    # 2. ALTER questions table
    print(f"\n🔧 ALTER questions...")
    if not has_column(conn, "questions", "topic_id"):
        cur.execute("ALTER TABLE questions ADD COLUMN topic_id INTEGER")
        print(f"   ✓ Added column: topic_id")
    else:
        print(f"   ⏭️  topic_id вече съществува")
    
    if not has_column(conn, "questions", "legacy_topic"):
        cur.execute("ALTER TABLE questions ADD COLUMN legacy_topic TEXT")
        print(f"   ✓ Added column: legacy_topic")
        # Копираме съществуващия topic в legacy_topic
        cur.execute("UPDATE questions SET legacy_topic = topic")
        rows_updated = cur.rowcount
        print(f"   ✓ Copied {rows_updated} rows: topic → legacy_topic")
    else:
        print(f"   ⏭️  legacy_topic вече съществува")
    
    conn.commit()
    
    # 3. Apply schema_v3 (creates all new tables + seeds)
    print(f"\n📋 Applying schema_v3.sql...")
    schema_sql = schema_path.read_text(encoding="utf-8")
    conn.executescript(schema_sql)
    conn.commit()
    
    # Count what was created
    new_tables = ["curriculum_areas", "curriculum_modules", "curriculum_topics",
                  "topic_classes", "topic_concepts", "topic_prerequisites",
                  "obsidian_notes", "note_question_links"]
    print(f"\n📊 Нови таблици:")
    for t in new_tables:
        if has_table(conn, t):
            count = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            print(f"   ✓ {t}: {count} rows")
        else:
            print(f"   ❌ {t}: НЕ СЪЩЕСТВУВА")
    
    # 4. Sanity checks
    print(f"\n🧪 Sanity checks...")
    
    q_count = conn.execute("SELECT COUNT(*) FROM questions").fetchone()[0]
    print(f"   questions: {q_count} (трябва да е 140)")
    
    legacy_count = conn.execute(
        "SELECT COUNT(*) FROM questions WHERE legacy_topic IS NOT NULL"
    ).fetchone()[0]
    print(f"   questions с legacy_topic: {legacy_count}")
    
    areas_count = conn.execute("SELECT COUNT(*) FROM curriculum_areas").fetchone()[0]
    print(f"   curriculum_areas: {areas_count} (трябва да е 9)")
    
    modules_count = conn.execute("SELECT COUNT(*) FROM curriculum_modules").fetchone()[0]
    print(f"   curriculum_modules: {modules_count} (трябва да е 4)")
    
    if q_count != 140:
        print(f"   ⚠️  Очаквах 140 въпроса, имам {q_count}. Backup: {backup_path}")
    if areas_count != 9:
        print(f"   ⚠️  Очаквах 9 areas, имам {areas_count}.")
    if modules_count != 4:
        print(f"   ⚠️  Очаквах 4 modules, имам {modules_count}.")
    
    conn.close()
    
    print(f"\n✅ Готово.")
    print(f"   Backup: {backup_path}")
    print(f"   За да се върнеш назад: cp {backup_path} {db_path}")
    print(f"\nСледващи стъпки:")
    print(f"   1. python3 src/sync_vault.py — попълва obsidian_notes и curriculum_topics от vault-а")
    print(f"   2. python3 src/link_questions_to_topics.py — мапва старите 140 въпроса към новите topic_id")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--db", default="data/questions.db", type=Path)
    p.add_argument("--schema", default="src/schema_v3.sql", type=Path)
    args = p.parse_args()
    migrate(args.db, args.schema)


if __name__ == "__main__":
    main()
