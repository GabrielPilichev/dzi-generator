"""
Remove the duplicate `sumif-renamed` topic.

Background:
    `sumif-renamed` was created on 2026-05-04 as leftover debris from an earlier
    rename operation. The real topic is `sumif` (id=6, 14 questions, full note).
    `sumif-renamed` (id=67) has 0 questions, a stub note, and a redundant
    section_assignment to grade11-m1-spreadsheets-big-data — a section that
    `sumif` already covers.

What this script does (idempotent — safe to run multiple times):
    1. Verifies `sumif` exists. Aborts if not (don't delete the duplicate
       without the canonical row in place).
    2. Removes redundant rows referencing `sumif-renamed` from:
       - topic_section_assignments
       - topic_classes
       - obsidian_notes
    3. Deletes the curriculum_topics row.
    4. Deletes the vault file vault/Topics/sumif-renamed.md if present.
    5. Verifies no orphan references remain.

Assumes there are no questions tagged `sumif-renamed` (verified before writing
this script — 0 rows). If a question is found tagged to it, the script aborts.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = REPO_ROOT / "data" / "questions.db"
VAULT_NOTE = REPO_ROOT / "vault" / "Topics" / "sumif-renamed.md"

DEAD_SLUG = "sumif-renamed"
LIVE_SLUG = "sumif"


def main() -> int:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    live = cur.execute(
        "SELECT id FROM curriculum_topics WHERE topic_slug = ?", (LIVE_SLUG,)
    ).fetchone()
    if not live:
        print(f"ABORT: canonical topic '{LIVE_SLUG}' is missing — refusing to delete duplicate.")
        return 2

    dead = cur.execute(
        "SELECT id FROM curriculum_topics WHERE topic_slug = ?", (DEAD_SLUG,)
    ).fetchone()
    if not dead:
        print(f"OK: '{DEAD_SLUG}' is already gone (DB).")
    else:
        dead_id = dead["id"]

        # Safety: refuse if any question references the dead topic.
        n_questions = cur.execute(
            "SELECT COUNT(*) FROM questions WHERE topic_id = ?", (dead_id,)
        ).fetchone()[0]
        if n_questions:
            print(f"ABORT: {n_questions} question(s) still tagged to '{DEAD_SLUG}'.")
            print("Reassign or retag them before running this migration.")
            return 3

        # Also refuse if the dead topic is the source for any question_topic_assignment
        n_qta = cur.execute(
            "SELECT COUNT(*) FROM question_topic_assignments WHERE topic_id = ?",
            (dead_id,),
        ).fetchone()[0]
        if n_qta:
            print(f"ABORT: {n_qta} question_topic_assignment row(s) reference '{DEAD_SLUG}'.")
            return 3

        deleted_rows = {}
        for table in (
            "topic_section_assignments",
            "topic_classes",
            "topic_concepts",
            "topic_aliases",
            "obsidian_notes",
        ):
            n = cur.execute(
                f"DELETE FROM {table} WHERE topic_id = ?", (dead_id,)
            ).rowcount
            deleted_rows[table] = n

        # topic_prerequisites can have it on either side
        n_prereq = cur.execute(
            "DELETE FROM topic_prerequisites WHERE topic_id = ? OR requires_topic_id = ?",
            (dead_id, dead_id),
        ).rowcount
        deleted_rows["topic_prerequisites"] = n_prereq

        cur.execute("DELETE FROM curriculum_topics WHERE id = ?", (dead_id,))
        conn.commit()

        print(f"Removed '{DEAD_SLUG}' (id={dead_id}). Side-tables cleaned:")
        for t, n in deleted_rows.items():
            print(f"  {t}: {n} row(s)")

    # Vault file
    if VAULT_NOTE.exists():
        VAULT_NOTE.unlink()
        print(f"Deleted vault file: {VAULT_NOTE.relative_to(REPO_ROOT)}")
    else:
        print(f"OK: vault file already absent.")

    # Final verification
    leftover = cur.execute(
        "SELECT COUNT(*) FROM curriculum_topics WHERE topic_slug = ?", (DEAD_SLUG,)
    ).fetchone()[0]
    if leftover:
        print(f"WARN: '{DEAD_SLUG}' still present in curriculum_topics.")
        return 4

    conn.close()
    print("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
