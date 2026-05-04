"""
Добавя нови curriculum_areas + Topics за orphan въпроси.

Какво прави:
  1. Добавя 2 нови areas в DB (hardware, legal)
  2. Създава 9 нови Topic stub файла в vault/Topics/
  3. Sync-ва (recommend: пусни sync_vault.py след това)

Употреба:
    python3 expand_topics.py [--vault PATH] [--db PATH]
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path


# ============================================================
# Нови areas
# ============================================================

NEW_AREAS = [
    ("hardware", "Хардуер", None,
     "Компютърен хардуер — CPU, RAM, дискове, видеокарти, дънни платки. "
     "Главно 8-9 клас."),
    ("legal", "Авторско право и стандарти", None,
     "Авторско право, лицензи, плагиатство, международни стандарти и организации. "
     "Главно 12 клас Модул 4."),
]


# ============================================================
# Нови Topics
# ============================================================

NEW_TOPICS = [
    # Hardware
    ("hardware-cpu", "Процесор (CPU)", "hardware", "[8, 9]",
     "hardware, klas8, klas9", "klas-8-moc"),
    ("hardware-storage", "Запомнящи устройства (HDD, SSD, флаш)", "hardware", "[8]",
     "hardware, klas8", "klas-8-moc"),
    ("hardware-motherboard", "Дънна платка", "hardware", "[8]",
     "hardware, klas8", "klas-8-moc"),
    ("hardware-graphics-card", "Видеокарта", "hardware", "[8]",
     "hardware, klas8", "klas-8-moc"),
    
    # Legal
    ("copyright-law", "Закон за авторското право", "legal", "[12]",
     "legal, klas12", "module-4-ict-moc"),
    ("software-licenses", "Софтуерни лицензи", "legal", "[8, 12]",
     "legal, klas8, klas12", "module-4-ict-moc"),
    ("plagiarism", "Плагиатство", "legal", "[12]",
     "legal, klas12", "module-4-ict-moc"),
    ("standards-organizations", "Стандартизиращи организации (ISO, W3C, IEEE)", "legal", "[10, 12]",
     "legal, klas10, klas12", "module-4-ict-moc"),
    
    # Video/audio specifics
    ("camera-optics", "Камера и обектив", "video_audio", "[11]",
     "video_audio, klas11", "module-2-multimedia-moc"),
]


TOPIC_STUB = """---
title: {title}
aliases: []
type: topic
parent_topic: {parent}
class: {classes}
tags: [{tags}]
---

# {title}

> Stub. Попълни кратко описание тук, после примери и upgrade-ни.

## Кратко описание

> Една-две изречения за концепцията.

## Кога се учи

> 

## Връзки

- [[{parent_link}]]
"""


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--vault", type=Path,
                   default=Path("vault"))
    p.add_argument("--db", type=Path,
                   default=Path("data/questions.db"))
    args = p.parse_args()
    
    if not args.vault.exists():
        print(f"❌ Vault не съществува: {args.vault}")
        sys.exit(1)
    if not args.db.exists():
        print(f"❌ DB не съществува: {args.db}")
        sys.exit(1)
    
    # 1. Add areas to DB
    conn = sqlite3.connect(str(args.db))
    cur = conn.cursor()
    
    print(f"📂 Adding curriculum_areas...")
    added_areas = 0
    for area_id, title_bg, moc, desc in NEW_AREAS:
        existing = cur.execute(
            "SELECT id FROM curriculum_areas WHERE area_id=?",
            (area_id,)
        ).fetchone()
        if existing:
            print(f"   ⏭️  {area_id}: вече съществува")
            continue
        cur.execute("""
            INSERT INTO curriculum_areas (area_id, title_bg, moc_filename, description)
            VALUES (?, ?, ?, ?)
        """, (area_id, title_bg, moc, desc))
        print(f"   ✓ {area_id}: {title_bg}")
        added_areas += 1
    conn.commit()
    conn.close()
    
    # 2. Create Topic stubs in vault
    topics_dir = args.vault / "Topics"
    topics_dir.mkdir(exist_ok=True)
    
    print(f"\n📄 Creating Topic stubs...")
    created = 0
    skipped = 0
    for slug, title, parent, classes, tags, parent_link in NEW_TOPICS:
        path = topics_dir / f"{slug}.md"
        if path.exists():
            print(f"   ⏭️  {slug}: вече съществува")
            skipped += 1
            continue
        path.write_text(
            TOPIC_STUB.format(
                title=title,
                parent=parent,
                classes=classes,
                tags=tags,
                parent_link=parent_link,
            ),
            encoding="utf-8",
        )
        print(f"   ✓ {slug}")
        created += 1
    
    print(f"\n📊 Summary:")
    print(f"   Areas added: {added_areas}")
    print(f"   Topics created: {created}")
    print(f"   Topics skipped: {skipped}")
    print(f"\nСледващи стъпки:")
    print(f"   1. python3 src/sync_vault.py --vault vault --db data/questions.db")
    print(f"   2. python3 src/topic_classifier.py")
    print(f"   3. python3 src/report_links.py --db data/questions.db --summary")


if __name__ == "__main__":
    main()
