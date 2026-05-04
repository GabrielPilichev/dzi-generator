"""
Linking на старите 140 въпроса към новите curriculum_topics.

Старите въпроси имат `legacy_topic` като свободен текст ("spreadsheets", "web", "general").
Това не е прецизно мапване към топика (SUMIF, COUNTIF), а към area-та.

Стратегия: 
  1. Първо ниво — мапни legacy_topic → area_id (стария 'topic' string е валиден area_id)
  2. След това опитваме keyword-based matching от prompt-а към конкретен topic_slug:
     - "SUMIF" в prompt → topic_id на 'sumif'
     - "COUNTIF" → 'countif'
     - "VLOOKUP" → 'lookup'
     - и т.н.
  3. Ако няма точен match, оставяме само area-то (топик-а остава None)

Това е one-shot скрипт. Пуска се след sync_vault.py.

Употреба:
    python3 link_questions_to_topics.py [--db PATH] [--dry-run]
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path


# Keyword → topic_slug mapping
# Order matters: по-конкретните първо
KEYWORD_TO_SLUG = [
    # Spreadsheets — конкретни функции
    (["sumif"], "sumif"),
    (["countif"], "countif"),
    (["vlookup", "hlookup", "lookup"], "lookup"),
    (["pivot", "обобщаваща таблица", "обобщаваща диаграма"], "pivot-table"),
    (["pmt", "ipmt", "ppmt"], "pmt-ipmt-ppmt"),
    (["dsum", "dcount", "daverage"], "dsum"),
    (["валидиране", "validation"], "data-validation"),
    (["филтри", "филтър", "filter"], "filtering-data"),
    (["switch", "choose"], "switch-function"),
    
    # Databases
    (["първичен ключ", "primary key"], "primary-key"),
    (["външен ключ", "foreign key", "чужд ключ"], "foreign-key"),
    (["релационен модел", "relational model"], "relational-model"),
    (["select"], "sql-select"),
    (["join"], "sql-join"),
    (["many to many", "many-to-many", "m:m"], "many-to-many"),
    
    # Web
    (["css селектор", "id селектор", "class селектор"], "css-selectors"),
    (["формуляр", "<form", "<input", "<textarea"], "html-forms"),
    (["хостинг", "hosting"], "web-hosting"),
    (["домейн", "dns", "domain"], "web-domain"),
    (["seo"], "seo"),
    (["cms", "система за управление на съдържание"], "cms"),
    (["ddos", "xss", "защитна стена", "firewall"], "web-security"),
    (["html"], "html"),
    (["css"], "css"),
    
    # Graphics
    (["растер", "вектор"], "raster-vs-vector"),
    (["слоев", "layers"], "layers"),
    (["цветов кръг", "color wheel"], "color-wheel"),
    (["ласо", "lasso"], "lasso-tool"),
    (["магическа пръчка", "magic wand"], "magic-wand"),
    (["филтри"], "image-filters"),
    
    # Video/Audio
    (["квантуване"], "audio-quantization"),
    (["дискретизация"], "audio-discretization"),
    (["кодек", "codec"], "video-codec"),
    (["fps", "кадър"], "fps"),
    
    # Info Systems
    (["sdlc", "етапи на разработка", "проектиране на ис"], "sdlc"),
    (["гант", "gantt"], "gantt-chart"),
    (["облак", "cloud", "saas"], "cloud-saas"),
    
    # AI / Programming
    (["псевдокод"], "pseudocode"),
    (["dataset", "набор от данни"], "dataset"),
    (["машинно", "machine learning", "ml"], "machine-learning"),
    (["deep-fake", "deep fake"], "deep-fake"),
    (["алгоритъм"], "algorithm-properties"),
    
    # Networks (9 клас)
    (["локална мрежа", "lan"], "local-network"),
    (["бисквитки", "cookies"], "cookies"),
    (["електронен подпис", "цифров подпис"], "electronic-signature"),
    (["gps"], "gps"),
    (["циркулярни писма", "mail merge"], "mail-merge"),
]


def link_questions(db_path: Path, dry_run: bool = False) -> dict:
    if not db_path.exists():
        print(f"❌ DB не намерен: {db_path}")
        sys.exit(1)
    
    conn = sqlite3.connect(str(db_path))
    cur = conn.cursor()
    
    # Build slug → topic_id lookup
    slug_to_id = {
        row[0]: row[1]
        for row in cur.execute("SELECT topic_slug, id FROM curriculum_topics")
    }
    print(f"📋 Намерени {len(slug_to_id)} curriculum_topics в DB")
    
    if not slug_to_id:
        print(f"❌ DB-то няма curriculum_topics. Пусни първо sync_vault.py.")
        sys.exit(1)
    
    # Build area_id (string) → DB id lookup
    area_to_id = {
        row[1]: row[0]
        for row in cur.execute("SELECT id, area_id FROM curriculum_areas")
    }
    
    # Add area_id (int) directly to questions as fallback signal
    # We'll store NULL for topic_id when only area is known, but track the area separately.
    # Simpler approach: keep questions.topic_id NULL when no specific topic; rely on
    # questions.subject + legacy_topic for area matching at query time.
    # Actually — better: add a question.area_id column? No, let's keep it simple:
    # if no specific topic, leave topic_id NULL but log the area fallback.
    
    questions = cur.execute("""
        SELECT id, prompt, legacy_topic, topic_id
        FROM questions
    """).fetchall()
    
    stats = {
        "total": len(questions),
        "already_linked": 0,
        "linked_specific_topic": 0,
        "area_only_fallback": 0,
        "no_match": 0,
        "by_slug": {},
        "by_area_fallback": {},
    }
    
    print(f"\n🔍 Търся mapping за {len(questions)} въпроса...")
    
    for q_id, prompt, legacy, current_topic_id in questions:
        if current_topic_id is not None:
            stats["already_linked"] += 1
            continue
        
        prompt_lower = (prompt or "").lower()
        legacy_lower = (legacy or "").lower()
        haystack = prompt_lower + " " + legacy_lower
        
        # First: try keyword matching
        matched_slug = None
        for keywords, slug in KEYWORD_TO_SLUG:
            for kw in keywords:
                if kw in haystack:
                    matched_slug = slug
                    break
            if matched_slug:
                break
        
        if matched_slug and matched_slug in slug_to_id:
            topic_id = slug_to_id[matched_slug]
            if not dry_run:
                cur.execute("UPDATE questions SET topic_id=? WHERE id=?",
                            (topic_id, q_id))
            stats["linked_specific_topic"] += 1
            stats["by_slug"].setdefault(matched_slug, 0)
            stats["by_slug"][matched_slug] += 1
        elif legacy in area_to_id:
            # Fallback: legacy_topic match-ва area_id директно
            stats["area_only_fallback"] += 1
            stats["by_area_fallback"].setdefault(legacy, 0)
            stats["by_area_fallback"][legacy] += 1
        else:
            stats["no_match"] += 1
    
    if not dry_run:
        conn.commit()
    
    conn.close()
    return stats


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--db", type=Path, default=Path("data/questions.db"))
    p.add_argument("--dry-run", action="store_true",
                   help="Покажи какво би се случило, без да записваш")
    args = p.parse_args()
    
    if args.dry_run:
        print(f"🧪 DRY RUN — нищо няма да се запише\n")
    
    stats = link_questions(args.db, dry_run=args.dry_run)
    
    print(f"\n📊 Резултати:")
    print(f"   Общо въпроси: {stats['total']}")
    print(f"   Вече свързани: {stats['already_linked']}")
    print(f"   ✓ Свързани с конкретен topic: {stats['linked_specific_topic']}")
    print(f"   → Само area fallback (без specific topic): {stats['area_only_fallback']}")
    print(f"   ⚠️  Без match: {stats['no_match']}")
    
    if stats["by_slug"]:
        print(f"\n📈 Distribution по конкретен topic:")
        for slug, count in sorted(stats["by_slug"].items(), key=lambda x: -x[1]):
            print(f"   {slug}: {count}")
    
    if stats["by_area_fallback"]:
        print(f"\n📊 Distribution по area (без specific topic):")
        for area, count in sorted(stats["by_area_fallback"].items(), key=lambda x: -x[1]):
            print(f"   {area}: {count}")
    
    if stats["area_only_fallback"]:
        print(f"\n💡 {stats['area_only_fallback']} въпроса имат само area, без specific topic.")
        print(f"   Те си запазват legacy_topic. За да им сложиш topic_id ръчно,")
        print(f"   разшири KEYWORD_TO_SLUG в скрипта или го прави един по един.")
    
    if stats["no_match"]:
        print(f"\n⚠️  {stats['no_match']} въпроса нямат match нито за topic, нито за area.")
        print(f"   Това са въпроси с legacy_topic като 'general', 'hardware', 'security', 'encoding'.")
    
    if args.dry_run:
        print(f"\n(dry-run: нищо не е записано)")
    else:
        print(f"\n✅ Готово.")


if __name__ == "__main__":
    main()
