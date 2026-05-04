"""
Import на review решения от Obsidian markdown → DB.

Чете всички файлове в vault/Generated/Review/*.md и ъпдейтва:
  - quality_score = 1.0 за approve
  - quality_score = 0.0 за reject
  - is_correct корекции (ако correct: X е попълнено)

След ъпдейт, файлът се преместя в vault/Generated/Review/done/

Workflow:
  - approve без correct: → quality_score=1.0, опциите остават
  - approve с correct: А → quality_score=1.0 + reset is_correct, А става правилен
  - reject → quality_score=0.0
  - нито edno → въпросът остава pending

Употреба:
    python3 review_import.py [--dry-run]
"""

from __future__ import annotations

import argparse
import re
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path


# Patterns
QUESTION_HEADER = re.compile(r"^##\s+Q#(\d+)\s+\[(\w+)\]", re.MULTILINE)
APPROVE_LINE = re.compile(r"^\s*-\s+\[([ xX])\]\s+approve\s*$", re.MULTILINE)
REJECT_LINE = re.compile(r"^\s*-\s+\[([ xX])\]\s+reject\s*$", re.MULTILINE)
CORRECT_LINE = re.compile(r"^\s*-?\s*correct:\s*([АБВГ]?)\s*$", re.MULTILINE)


def parse_review_file(text: str) -> list:
    """
    Парсва един review markdown файл.
    Връща list of dicts: {qid, decision, correct_override}.
    """
    decisions = []
    
    # Find all question headers and their positions
    headers = list(QUESTION_HEADER.finditer(text))
    
    for i, header in enumerate(headers):
        qid = int(header.group(1))
        
        # Range of this question's section: from this header to next (or end)
        start = header.end()
        end = headers[i + 1].start() if i + 1 < len(headers) else len(text)
        section = text[start:end]
        
        # Find approve/reject markers
        approve_m = APPROVE_LINE.search(section)
        reject_m = REJECT_LINE.search(section)
        correct_m = CORRECT_LINE.search(section)
        
        approved = approve_m and approve_m.group(1).lower() == "x"
        rejected = reject_m and reject_m.group(1).lower() == "x"
        
        if approved and rejected:
            decisions.append({
                "qid": qid,
                "decision": "conflict",
                "correct_override": None,
            })
            continue
        
        if approved:
            decision = "approve"
        elif rejected:
            decision = "reject"
        else:
            decision = "pending"
        
        correct_override = None
        if correct_m:
            ch = correct_m.group(1).strip()
            if ch in ("А", "Б", "В", "Г"):
                correct_override = ch
        
        # Warn: correct override без approve/reject
        if correct_override and decision == "pending":
            decision = "correct_without_approve"
        
        decisions.append({
            "qid": qid,
            "decision": decision,
            "correct_override": correct_override,
        })
    
    return decisions


def apply_decisions(conn: sqlite3.Connection,
                    decisions: list,
                    dry_run: bool = False) -> dict:
    cur = conn.cursor()
    stats = {
        "approved": 0,
        "rejected": 0,
        "pending": 0,
        "conflicts": 0,
        "corrections": 0,
        "not_found": 0,
    }
    
    for d in decisions:
        qid = d["qid"]
        decision = d["decision"]
        correct = d["correct_override"]
        
        # Check question exists & is AI-generated pending
        row = cur.execute(
            "SELECT id, is_ai_generated, quality_score "
            "FROM questions WHERE id=?",
            (qid,)
        ).fetchone()
        if not row:
            stats["not_found"] += 1
            print(f"   ⚠️  Q#{qid} не съществува в DB")
            continue
        
        if decision == "conflict":
            stats["conflicts"] += 1
            print(f"   ⚠️  Q#{qid}: маркирано И approve И reject — пропускам")
            continue
        
        if decision == "correct_without_approve":
            stats["pending"] += 1
            print(f"   ⚠️  Q#{qid}: correct={correct} зададен, но няма approve. Маркирай approve!")
            continue
        
        if decision == "pending":
            stats["pending"] += 1
            continue
        
        if decision == "approve":
            stats["approved"] += 1
            if not dry_run:
                cur.execute(
                    "UPDATE questions SET quality_score=1.0 WHERE id=?",
                    (qid,)
                )
            
            # Apply is_correct override
            if correct:
                stats["corrections"] += 1
                if not dry_run:
                    # Reset all is_correct to 0, then set the chosen one to 1
                    cur.execute(
                        "UPDATE multiple_choice_options "
                        "SET is_correct = 0 WHERE question_id = ?",
                        (qid,)
                    )
                    cur.execute(
                        "UPDATE multiple_choice_options "
                        "SET is_correct = 1 "
                        "WHERE question_id = ? AND option_letter = ?",
                        (qid, correct)
                    )
                print(f"   ✓ Q#{qid}: approved (correct → {correct})")
            else:
                print(f"   ✓ Q#{qid}: approved")
        
        elif decision == "reject":
            stats["rejected"] += 1
            if not dry_run:
                cur.execute(
                    "UPDATE questions SET quality_score=0.0 WHERE id=?",
                    (qid,)
                )
            print(f"   ✗ Q#{qid}: rejected")
    
    if not dry_run:
        conn.commit()
    
    return stats


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--db", type=Path, default=Path("data/questions.db"))
    p.add_argument("--vault", type=Path, default=Path("vault"))
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--keep-files", action="store_true",
                   help="Не премествай processed файлове в done/")
    args = p.parse_args()
    
    if not args.db.exists():
        print(f"❌ DB не съществува: {args.db}")
        sys.exit(1)
    
    review_dir = args.vault / "Generated" / "Review"
    if not review_dir.exists():
        print(f"❌ Няма {review_dir}")
        sys.exit(1)
    
    md_files = sorted(review_dir.glob("*.md"))
    if not md_files:
        print(f"⏭️  Няма review файлове в {review_dir}")
        return
    
    print(f"📂 Намерени {len(md_files)} review файла")
    if args.dry_run:
        print(f"   ⚠️  DRY RUN")
    print()
    
    conn = sqlite3.connect(str(args.db))
    
    grand_stats = {
        "approved": 0, "rejected": 0, "pending": 0,
        "conflicts": 0, "corrections": 0, "not_found": 0,
        "files_processed": 0, "files_with_actions": 0,
    }
    
    for md_path in md_files:
        text = md_path.read_text(encoding="utf-8")
        decisions = parse_review_file(text)
        if not decisions:
            print(f"⏭️  {md_path.name}: няма въпроси")
            continue
        
        actions = sum(1 for d in decisions if d["decision"] in ("approve", "reject"))
        
        print(f"📄 {md_path.name}: {len(decisions)} въпроса, {actions} с решения")
        stats = apply_decisions(conn, decisions, dry_run=args.dry_run)
        for k, v in stats.items():
            grand_stats[k] = grand_stats.get(k, 0) + v
        grand_stats["files_processed"] += 1
        if actions > 0:
            grand_stats["files_with_actions"] += 1
        print()
        
        # Move file to done/ if everything resolved (no pending)
        if not args.dry_run and not args.keep_files:
            has_pending = any(d["decision"] == "pending" for d in decisions)
            if not has_pending and actions > 0:
                done_dir = review_dir / "done"
                done_dir.mkdir(exist_ok=True)
                target = done_dir / f"{md_path.stem}.{datetime.now().strftime('%Y%m%d-%H%M%S')}.md"
                shutil.move(str(md_path), str(target))
                print(f"   📦 Преместен в done/{target.name}")
    
    conn.close()
    
    print(f"\n{'=' * 60}")
    print(f"📊 SUMMARY")
    print(f"{'=' * 60}")
    print(f"   Files processed:  {grand_stats['files_processed']}")
    print(f"   ✓ Approved:       {grand_stats['approved']}")
    print(f"   ✗ Rejected:       {grand_stats['rejected']}")
    print(f"   ✏️  Corrections:   {grand_stats['corrections']}")
    print(f"   ⏳ Pending:        {grand_stats['pending']}")
    if grand_stats["conflicts"]:
        print(f"   ⚠️  Conflicts:    {grand_stats['conflicts']}")
    if grand_stats["not_found"]:
        print(f"   ❌ Not in DB:    {grand_stats['not_found']}")


if __name__ == "__main__":
    main()
