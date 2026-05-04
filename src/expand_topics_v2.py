"""
Втора вълна разширения — добавя 4 Topics за остатъчните patterns.

Употреба:
    python3 expand_topics_v2.py
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


NEW_TOPICS = [
    ("web-standards", "Уеб стандарти (W3C)", "web", "[12]",
     "web, klas12", "module-3-web-moc"),
    ("wireframe-mockup", "Wireframe и mockup на уеб страница", "web", "[12]",
     "web, klas12", "module-3-web-moc"),
    ("audio-effects", "Ефекти при аудио обработка (fade, echo)", "video_audio", "[11]",
     "video_audio, klas11", "module-2-multimedia-moc"),
    ("audio-playback-devices", "Устройства за възпроизвеждане на звук", "video_audio", "[11]",
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

> Stub. Попълни кратко описание тук.

## Кратко описание

> Една-две изречения за концепцията.

## Връзки

- [[{parent_link}]]
"""


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--vault", type=Path, default=Path("vault"))
    args = p.parse_args()
    
    if not args.vault.exists():
        print(f"❌ Vault не съществува: {args.vault}")
        sys.exit(1)
    
    topics_dir = args.vault / "Topics"
    topics_dir.mkdir(exist_ok=True)
    
    print(f"📄 Creating Topic stubs (wave 2)...")
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
    
    print(f"\n📊 Created: {created}, Skipped: {skipped}")
    print(f"\nСледващи стъпки:")
    print(f"   python3 src/sync_vault.py --vault vault --db data/questions.db")
    print(f"   python3 src/topic_classifier.py")
    print(f"   python3 src/report_links.py --db data/questions.db --summary")


if __name__ == "__main__":
    main()
