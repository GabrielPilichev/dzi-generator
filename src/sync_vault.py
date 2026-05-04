"""
Sync на Obsidian vault → SQLite DB.

Парсва всички .md файлове в vault-а:
  - Извлича frontmatter (YAML) и body
  - Записва в obsidian_notes
  - За файлове в Topics/ — създава/обновява запис в curriculum_topics
  - Свързва topic към area (от parent_topic в frontmatter)
  - Маркира topic_classes (от class в frontmatter)

Idempotent: пуска се много пъти безопасно. Хешира body-то и обновява само
променените файлове.

GC pass (added): След синхронизацията изтрива obsidian_notes редове, чиито
файлове вече не съществуват на диск. Topic нотите получават обнулен note_path
вместо изтриване на curriculum_topics запис (за да не осиротеят въпросите,
вързани към топика).

Без external dependencies (използва само stdlib + json за frontmatter parsing).
Frontmatter parser е минимален — само YAML списъци, скаляри, null.

Употреба:
    python3 sync_vault.py [--vault PATH] [--db PATH] [--verbose] [--no-gc]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional


# ============================================================
# Minimal YAML frontmatter parser (no PyYAML dependency)
# ============================================================

FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """
    Minimal YAML frontmatter parser.

    Supports:
      - key: value
      - key: [a, b, c]
      - key:
          - item
          - item
      - quoted strings
      - ints
    """
    match = FRONTMATTER_RE.match(text)
    if not match:
        return {}, text

    fm_text = match.group(1)
    body = text[match.end():]

    def convert_scalar(value: str):
        value = value.strip().strip('"').strip("'")
        try:
            return int(value)
        except ValueError:
            return value

    fm: dict = {}
    current_list_key: str | None = None

    for raw_line in fm_text.split("\n"):
        line = raw_line.rstrip()
        if not line or line.lstrip().startswith("#"):
            continue

        stripped = line.strip()

        # Multiline list item:
        # key:
        #   - item
        if current_list_key and stripped.startswith("- "):
            item = stripped[2:].strip()
            fm.setdefault(current_list_key, [])
            if not isinstance(fm[current_list_key], list):
                fm[current_list_key] = []
            fm[current_list_key].append(convert_scalar(item))
            continue

        # Any non-list non-indented line ends previous list context.
        if not line.startswith(" ") and not line.startswith("\t"):
            current_list_key = None

        if ":" not in line:
            continue

        # Skip unsupported nested structures except simple list items handled above.
        if line.startswith(" ") or line.startswith("\t"):
            continue

        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()

        if not value:
            fm[key] = []
            current_list_key = key
        elif value.startswith("[") and value.endswith("]"):
            inner = value[1:-1].strip()
            if not inner:
                fm[key] = []
            else:
                fm[key] = [convert_scalar(x.strip()) for x in inner.split(",")]
        else:
            fm[key] = convert_scalar(value)

    return fm, body

def file_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


# ============================================================
# Note type detection
# ============================================================

def detect_note_type(rel_path: str, fm: dict) -> str:
    """От frontmatter type или от папката."""
    # _Templates/ винаги е template, независимо от frontmatter
    if rel_path.startswith("_Templates/"):
        return "template"
    
    fm_type = fm.get("type")
    if fm_type:
        return str(fm_type)
    
    if rel_path.startswith("Topics/"):
        return "topic"
    if rel_path.startswith("MOCs/"):
        return "moc"
    if rel_path.startswith("Daily/"):
        return "daily"
    if rel_path.startswith("Lessons/"):
        return "lesson"
    if rel_path == "Home.md":
        return "home"
    if rel_path.startswith("_Templates/"):
        return "template"
    return "other"


# ============================================================
# Sync logic
# ============================================================

def sync_vault(vault: Path, db_path: Path, verbose: bool = False,
               do_gc: bool = True) -> dict:
    if not vault.exists():
        print(f"❌ Vault не съществува: {vault}")
        sys.exit(1)
    if not db_path.exists():
        print(f"❌ DB не намерен: {db_path}")
        sys.exit(1)
    
    conn = sqlite3.connect(str(db_path))
    cur = conn.cursor()
    
    # Build area lookup: area_id (string) → id (int)
    area_lookup = {
        row[1]: row[0]
        for row in cur.execute("SELECT id, area_id FROM curriculum_areas")
    }
    
    stats = {
        "scanned": 0,
        "skipped_template": 0,
        "skipped_unchanged": 0,
        "notes_created": 0,
        "notes_updated": 0,
        "topics_created": 0,
        "topics_updated": 0,
        "topics_with_area": 0,
        "topics_without_area": 0,
        "topic_classes_added": 0,
        "gc_notes_deleted": 0,
        "gc_topics_unlinked": 0,
    }
    
    # Track which paths we saw on disk (for GC pass)
    seen_paths: set[str] = set()
    
    # Iterate all .md files
    for md_file in vault.rglob("*.md"):
        rel_path = str(md_file.relative_to(vault))
        
        # Skip backup folder
        if rel_path.startswith("_backup_initial_mocs/"):
            continue
        
        stats["scanned"] += 1
        seen_paths.add(rel_path)
        content = md_file.read_text(encoding="utf-8")
        fm, body = parse_frontmatter(content)
        body_h = file_hash(body)
        
        note_type = detect_note_type(rel_path, fm)
        
        # Skip templates (те имат placeholders, не са истински бележки)
        if note_type == "template":
            stats["skipped_template"] += 1
            continue
        
        # Check if note exists & unchanged
        existing = cur.execute(
            "SELECT id, body_hash FROM obsidian_notes WHERE file_path=?",
            (rel_path,)
        ).fetchone()
        
        if existing and existing[1] == body_h:
            stats["skipped_unchanged"] += 1
            if verbose:
                print(f"   ⏭️  {rel_path}")
            continue
        
        # Build classes string from frontmatter
        classes_value = fm.get("class") or fm.get("classes")
        if isinstance(classes_value, list):
            classes_str = ",".join(str(c) for c in classes_value)
        elif classes_value is not None:
            classes_str = str(classes_value)
        else:
            classes_str = ""
        
        tags_value = fm.get("tags")
        if isinstance(tags_value, list):
            tags_str = ",".join(str(t) for t in tags_value)
        else:
            tags_str = str(tags_value) if tags_value else ""
        
        title = fm.get("title") or md_file.stem
        
        file_stat = md_file.stat()
        mtime = datetime.fromtimestamp(file_stat.st_mtime).isoformat(timespec="seconds")
        
        # Insert / update obsidian_notes
        if existing:
            cur.execute("""
                UPDATE obsidian_notes
                SET note_type=?, title=?, frontmatter_json=?, tags=?, classes=?,
                    body_text=?, body_hash=?, file_size=?, file_mtime=?,
                    last_synced=?
                WHERE id=?
            """, (
                note_type, title, json.dumps(fm, ensure_ascii=False),
                tags_str, classes_str, body, body_h,
                file_stat.st_size, mtime,
                datetime.now().isoformat(timespec="seconds"),
                existing[0],
            ))
            note_id = existing[0]
            stats["notes_updated"] += 1
            if verbose:
                print(f"   📝 Updated: {rel_path}")
        else:
            cur.execute("""
                INSERT INTO obsidian_notes
                    (file_path, note_type, title, frontmatter_json, tags, classes,
                     body_text, body_hash, file_size, file_mtime, last_synced)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                rel_path, note_type, title, json.dumps(fm, ensure_ascii=False),
                tags_str, classes_str, body, body_h,
                file_stat.st_size, mtime,
                datetime.now().isoformat(timespec="seconds"),
            ))
            note_id = cur.lastrowid
            stats["notes_created"] += 1
            if verbose:
                print(f"   ✨ Created: {rel_path}")
        
        # If this is a Topic note, sync curriculum_topics too
        if note_type == "topic":
            slug = md_file.stem  # 'sumif'
            parent_topic = fm.get("parent_topic") or ""
            area_key = fm.get("area") or parent_topic or ""
            area_id = area_lookup.get(str(area_key))
            
            if area_id:
                stats["topics_with_area"] += 1
            else:
                stats["topics_without_area"] += 1
            
            # Извличаме кратко описание от body-то (първи non-empty абзац след първия heading)
            description = extract_description(body)
            
            # Insert / update curriculum_topics
            existing_topic = cur.execute(
                "SELECT id, area_id, description FROM curriculum_topics WHERE topic_slug=?",
                (slug,)
            ).fetchone()
            
            note_h = file_hash(content)  # full content (frontmatter + body)
            
            if existing_topic:
                final_area_id = area_id if area_id else existing_topic[1]
                final_description = description or existing_topic[2]
                cur.execute("""
                    UPDATE curriculum_topics
                    SET title_bg=?, area_id=?, note_path=?, note_hash=?,
                        last_synced=?, description=?
                    WHERE id=?
                """, (
                    title, final_area_id, rel_path, note_h,
                    datetime.now().isoformat(timespec="seconds"),
                    final_description, existing_topic[0],
                ))
                topic_id = existing_topic[0]
                stats["topics_updated"] += 1
            else:
                cur.execute("""
                    INSERT INTO curriculum_topics
                        (topic_slug, title_bg, area_id, note_path, note_hash,
                         last_synced, description)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (
                    slug, title, area_id, rel_path, note_h,
                    datetime.now().isoformat(timespec="seconds"),
                    description,
                ))
                topic_id = cur.lastrowid
                stats["topics_created"] += 1
            
            # Update note's topic_id
            cur.execute("UPDATE obsidian_notes SET topic_id=? WHERE id=?",
                        (topic_id, note_id))
            
            # Sync topic_classes only when frontmatter provides classes.
            # This prevents stub/parser mistakes from wiping existing class mappings.
            classes_value = fm.get("class") or fm.get("classes")
            if isinstance(classes_value, list) and classes_value:
                cur.execute("DELETE FROM topic_classes WHERE topic_id=?", (topic_id,))
                for c in classes_value:
                    try:
                        cur.execute("""
                            INSERT OR IGNORE INTO topic_classes (topic_id, class) VALUES (?, ?)
                        """, (topic_id, int(c)))
                        stats["topic_classes_added"] += 1
                    except (ValueError, TypeError):
                        continue
            elif classes_value not in (None, [], ""):
                try:
                    cur.execute("DELETE FROM topic_classes WHERE topic_id=?", (topic_id,))
                    cur.execute("""
                        INSERT OR IGNORE INTO topic_classes (topic_id, class) VALUES (?, ?)
                    """, (topic_id, int(classes_value)))
                    stats["topic_classes_added"] += 1
                except (ValueError, TypeError):
                    pass
    
    # ============================================================
    # GC pass — find DB rows whose files no longer exist on disk
    # ============================================================
    if do_gc:
        all_db_paths = cur.execute(
            "SELECT id, file_path, note_type FROM obsidian_notes"
        ).fetchall()
        
        for note_id, rel_path, note_type in all_db_paths:
            if rel_path in seen_paths:
                continue
            # Belt-and-suspenders — also confirm absent from disk
            if (vault / rel_path).exists():
                continue
            
            # If it's a topic note, NULL out note_path/note_hash on the topic
            # rather than deleting the curriculum_topics row (questions may
            # reference it via topic_id).
            if note_type == "topic":
                slug = Path(rel_path).stem
                cur.execute("""
                    UPDATE curriculum_topics
                    SET note_path=NULL, note_hash=NULL
                    WHERE topic_slug=? AND note_path=?
                """, (slug, rel_path))
                if cur.rowcount > 0:
                    stats["gc_topics_unlinked"] += 1
                    if verbose:
                        print(f"   🔗 Unlinked topic: {slug}")
            
            cur.execute("DELETE FROM obsidian_notes WHERE id=?", (note_id,))
            stats["gc_notes_deleted"] += 1
            if verbose:
                print(f"   🗑️  GC: {rel_path}")
    
    conn.commit()
    conn.close()
    
    return stats


# ============================================================
# Description extraction
# ============================================================

def extract_description(body: str) -> str:
    """
    Извлича кратко описание от body — първи non-empty абзац след първия heading
    или след "## Кратко описание" секция.
    """
    lines = body.split("\n")
    in_short_section = False
    desc_lines: list = []
    
    for line in lines:
        stripped = line.strip()
        
        if "Кратко описание" in stripped or "## Описание" in stripped:
            in_short_section = True
            continue
        
        if in_short_section:
            if stripped.startswith("#") or stripped.startswith("---"):
                break
            if stripped and not stripped.startswith(">"):
                desc_lines.append(stripped)
            elif desc_lines:
                # Empty line след съдържание — край на абзаца
                break
    
    if desc_lines:
        return " ".join(desc_lines)[:500]  # cap at 500 chars
    
    # Fallback: първи non-empty, non-heading, non-blockquote ред
    for line in lines:
        stripped = line.strip()
        if (stripped and not stripped.startswith("#")
                and not stripped.startswith(">")
                and not stripped.startswith("---")):
            return stripped[:500]
    
    return ""


# ============================================================
# Main
# ============================================================

def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--vault", type=Path,
                   default=Path.home() / "dzi-generator" / "vault")
    p.add_argument("--db", type=Path,
                   default=Path("data/questions.db"))
    p.add_argument("--verbose", action="store_true")
    p.add_argument("--no-gc", action="store_true",
                   help="Skip GC pass (don't delete dead obsidian_notes rows)")
    args = p.parse_args()
    
    print(f"📂 Vault: {args.vault}")
    print(f"🗃️  DB:    {args.db}")
    print(f"\n🔄 Syncing...")
    
    stats = sync_vault(args.vault, args.db,
                       verbose=args.verbose, do_gc=not args.no_gc)
    
    print(f"\n📊 Stats:")
    print(f"   Сканирани: {stats['scanned']}")
    print(f"   Templates пропуснати: {stats['skipped_template']}")
    print(f"   Непроменени (skip): {stats['skipped_unchanged']}")
    print(f"   Notes:")
    print(f"      ✨ Created: {stats['notes_created']}")
    print(f"      📝 Updated: {stats['notes_updated']}")
    print(f"   Topics:")
    print(f"      ✨ Created: {stats['topics_created']}")
    print(f"      📝 Updated: {stats['topics_updated']}")
    print(f"      → with area: {stats['topics_with_area']}")
    print(f"      ⚠️  без area: {stats['topics_without_area']}")
    print(f"   Topic-Class links: {stats['topic_classes_added']}")
    if not args.no_gc:
        print(f"   GC:")
        print(f"      🗑️  Notes deleted: {stats['gc_notes_deleted']}")
        print(f"      🔗 Topics unlinked: {stats['gc_topics_unlinked']}")
    
    print(f"\n✅ Готово.")


if __name__ == "__main__":
    main()
