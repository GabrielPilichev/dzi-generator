"""
Query утилита за curriculum graph.

Показва за избран topic, area, или клас:
  - Колко въпроси са свързани (specific и via area-fallback)
  - Кои Topics/ бележки покриват темата
  - Кои генерирани изпити я включват
  - Връзки към други теми (prerequisites)

Употреба:
    # По topic slug
    python3 report_links.py --topic sumif
    
    # По area
    python3 report_links.py --area spreadsheets
    
    # По клас
    python3 report_links.py --class 9
    
    # Цялостен отчет (executive summary)
    python3 report_links.py --summary

    # Кои въпроси нямат topic_id?
    python3 report_links.py --orphans
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path


def report_topic(conn: sqlite3.Connection, slug: str) -> None:
    """Detail за един topic."""
    row = conn.execute("""
        SELECT t.id, t.title_bg, t.note_path, t.description,
               a.area_id, a.title_bg
        FROM curriculum_topics t
        LEFT JOIN curriculum_areas a ON a.id = t.area_id
        WHERE t.topic_slug = ?
    """, (slug,)).fetchone()
    
    if not row:
        print(f"❌ Не намирам topic '{slug}'")
        return
    
    topic_id, title, note_path, desc, area_slug, area_title = row
    
    print(f"\n{'=' * 60}")
    print(f"📌 {title}")
    print(f"{'=' * 60}")
    print(f"   slug:        {slug}")
    print(f"   id:          {topic_id}")
    print(f"   note:        {note_path or '—'}")
    print(f"   area:        {area_title} ({area_slug})" if area_slug else "   area:        — (не е свързан)")
    
    # Classes
    classes = [r[0] for r in conn.execute(
        "SELECT class FROM topic_classes WHERE topic_id=? ORDER BY class",
        (topic_id,)
    )]
    print(f"   classes:     {', '.join(str(c) for c in classes) if classes else '—'}")
    
    if desc:
        print(f"\n   📝 {desc[:200]}")
    
    # Свързани въпроси
    questions = conn.execute("""
        SELECT id, source_exam, source_number, question_type, points,
               substr(prompt, 1, 80)
        FROM questions WHERE topic_id = ?
        ORDER BY source_exam, source_number
    """, (topic_id,)).fetchall()
    
    print(f"\n   📚 Въпроси свързани с този topic: {len(questions)}")
    for q in questions[:10]:
        qid, src, num, qtype, pts, prev = q
        print(f"      [{qid}] {src} #{num} ({qtype}, {pts}т) — {(prev or '').strip()}...")
    if len(questions) > 10:
        print(f"      ... и още {len(questions) - 10}")
    
    # Prerequisites
    prereqs = conn.execute("""
        SELECT t2.title_bg, t2.topic_slug
        FROM topic_prerequisites tp
        JOIN curriculum_topics t2 ON t2.id = tp.requires_topic_id
        WHERE tp.topic_id = ?
    """, (topic_id,)).fetchall()
    
    if prereqs:
        print(f"\n   🔗 Изисква (prerequisites):")
        for pt in prereqs:
            print(f"      - {pt[0]} ({pt[1]})")


def report_area(conn: sqlite3.Connection, area_slug: str) -> None:
    """Detail за една тематична област."""
    row = conn.execute("""
        SELECT id, title_bg, moc_filename, description
        FROM curriculum_areas WHERE area_id = ?
    """, (area_slug,)).fetchone()
    
    if not row:
        print(f"❌ Не намирам area '{area_slug}'")
        # Show available
        print(f"\nДостъпни areas:")
        for r in conn.execute("SELECT area_id, title_bg FROM curriculum_areas ORDER BY area_id"):
            print(f"   {r[0]}: {r[1]}")
        return
    
    area_id, title, moc, desc = row
    
    print(f"\n{'=' * 60}")
    print(f"📂 {title} ({area_slug})")
    print(f"{'=' * 60}")
    print(f"   MOC:  {moc or '—'}")
    if desc:
        print(f"   {desc}")
    
    # Topics в тази area
    topics = conn.execute("""
        SELECT t.topic_slug, t.title_bg, COUNT(q.id) as q_count
        FROM curriculum_topics t
        LEFT JOIN questions q ON q.topic_id = t.id
        WHERE t.area_id = ?
        GROUP BY t.id
        ORDER BY q_count DESC, t.topic_slug
    """, (area_id,)).fetchall()
    
    print(f"\n   📌 Topics ({len(topics)}):")
    for slug, t_title, qc in topics:
        marker = "✓" if qc > 0 else " "
        print(f"      {marker} [{qc:3}] {slug:30} {t_title}")
    
    # Questions: specific topic vs area-fallback
    specific_q = conn.execute("""
        SELECT COUNT(*) FROM questions q
        JOIN curriculum_topics t ON t.id = q.topic_id
        WHERE t.area_id = ?
    """, (area_id,)).fetchone()[0]
    
    fallback_q = conn.execute("""
        SELECT COUNT(*) FROM questions
        WHERE topic_id IS NULL AND legacy_topic = ?
    """, (area_slug,)).fetchone()[0]
    
    print(f"\n   📚 Въпроси:")
    print(f"      ✓ С конкретен topic: {specific_q}")
    print(f"      → Само area fallback (legacy_topic='{area_slug}'): {fallback_q}")
    print(f"      = ОБЩО: {specific_q + fallback_q}")


def report_class(conn: sqlite3.Connection, class_num: int) -> None:
    """Detail за един клас."""
    print(f"\n{'=' * 60}")
    print(f"🎓 Клас {class_num}")
    print(f"{'=' * 60}")
    
    # Topics в този клас
    topics = conn.execute("""
        SELECT t.topic_slug, t.title_bg, a.area_id
        FROM curriculum_topics t
        JOIN topic_classes tc ON tc.topic_id = t.id
        LEFT JOIN curriculum_areas a ON a.id = t.area_id
        WHERE tc.class = ?
        ORDER BY a.area_id, t.topic_slug
    """, (class_num,)).fetchall()
    
    by_area: dict = {}
    for slug, title, area in topics:
        by_area.setdefault(area or "—", []).append((slug, title))
    
    print(f"\n   📌 Topics в учебната програма ({len(topics)} общо):")
    for area, ts in sorted(by_area.items()):
        print(f"\n   [{area}]")
        for slug, title in ts:
            print(f"      - {title} ({slug})")
    
    # Questions в този клас (от subject/level/year може да филтрираме)
    # Задължителна подготовка приключва в 10, ДЗИ след 12
    if class_num <= 10:
        print(f"\n   📚 НВО релевантност: НВО {class_num} (ако е изпитен клас)")
    elif class_num >= 11:
        print(f"\n   📚 ДЗИ релевантност: матурата след 12 клас")


def report_summary(conn: sqlite3.Connection) -> None:
    """Executive summary."""
    print(f"\n{'=' * 60}")
    print(f"📊 SUMMARY на curriculum graph")
    print(f"{'=' * 60}")
    
    stats = {
        "questions": conn.execute("SELECT COUNT(*) FROM questions").fetchone()[0],
        "questions_with_topic": conn.execute(
            "SELECT COUNT(*) FROM questions WHERE topic_id IS NOT NULL"
        ).fetchone()[0],
        "areas": conn.execute("SELECT COUNT(*) FROM curriculum_areas").fetchone()[0],
        "modules": conn.execute("SELECT COUNT(*) FROM curriculum_modules").fetchone()[0],
        "topics": conn.execute("SELECT COUNT(*) FROM curriculum_topics").fetchone()[0],
        "topic_classes": conn.execute("SELECT COUNT(*) FROM topic_classes").fetchone()[0],
        "obsidian_notes": conn.execute("SELECT COUNT(*) FROM obsidian_notes").fetchone()[0],
        "links": conn.execute("SELECT COUNT(*) FROM note_question_links").fetchone()[0],
    }
    
    print(f"\n📚 Questions:       {stats['questions']}")
    print(f"   с topic_id:      {stats['questions_with_topic']} "
          f"({stats['questions_with_topic'] * 100 // max(stats['questions'], 1)}%)")
    print(f"\n📂 Curriculum:")
    print(f"   areas:           {stats['areas']}")
    print(f"   modules:         {stats['modules']}")
    print(f"   topics:          {stats['topics']}")
    print(f"   topic↔class:     {stats['topic_classes']}")
    print(f"\n📝 Obsidian:")
    print(f"   notes:           {stats['obsidian_notes']}")
    print(f"   note↔question:   {stats['links']}")
    
    # Topics with most questions
    print(f"\n🔥 Топ 10 topics по брой въпроси:")
    rows = conn.execute("""
        SELECT t.topic_slug, t.title_bg, COUNT(q.id) as cnt
        FROM curriculum_topics t
        JOIN questions q ON q.topic_id = t.id
        GROUP BY t.id
        ORDER BY cnt DESC
        LIMIT 10
    """).fetchall()
    for slug, title, cnt in rows:
        print(f"   [{cnt:3}] {slug:25} {title}")
    
    # Topics with no questions
    empty_topics = conn.execute("""
        SELECT COUNT(*) FROM curriculum_topics t
        LEFT JOIN questions q ON q.topic_id = t.id
        WHERE q.id IS NULL
    """).fetchone()[0]
    print(f"\n⚠️  Topics без въпроси: {empty_topics}/{stats['topics']}")


def report_orphans(conn: sqlite3.Connection) -> None:
    """Списък на orphan въпроси (без topic_id)."""
    rows = conn.execute("""
        SELECT id, source_exam, source_number, legacy_topic,
               substr(prompt, 1, 70)
        FROM questions
        WHERE topic_id IS NULL
        ORDER BY legacy_topic, id
    """).fetchall()
    
    print(f"\n{'=' * 60}")
    print(f"🔍 Orphan questions (без topic_id): {len(rows)}")
    print(f"{'=' * 60}")
    
    by_legacy: dict = {}
    for q in rows:
        by_legacy.setdefault(q[3] or "—", []).append(q)
    
    for legacy, qs in sorted(by_legacy.items()):
        print(f"\n   [legacy_topic = '{legacy}'] {len(qs)} въпроса:")
        for qid, src, num, _leg, prev in qs[:5]:
            print(f"      [{qid}] {src} #{num}: {(prev or '').strip()}...")
        if len(qs) > 5:
            print(f"      ... и още {len(qs) - 5}")


# ============================================================
# Main
# ============================================================

def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--db", type=Path, default=Path("data/questions.db"))
    
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--topic", help="Topic slug (e.g. 'sumif')")
    g.add_argument("--area", help="Area id (e.g. 'spreadsheets')")
    g.add_argument("--class", dest="class_num", type=int, help="Class number 8-12")
    g.add_argument("--summary", action="store_true")
    g.add_argument("--orphans", action="store_true")
    
    args = p.parse_args()
    
    if not args.db.exists():
        print(f"❌ DB не съществува: {args.db}")
        sys.exit(1)
    
    conn = sqlite3.connect(str(args.db))
    
    if args.topic:
        report_topic(conn, args.topic)
    elif args.area:
        report_area(conn, args.area)
    elif args.class_num:
        report_class(conn, args.class_num)
    elif args.summary:
        report_summary(conn)
    elif args.orphans:
        report_orphans(conn)
    
    conn.close()
    print()


if __name__ == "__main__":
    main()
