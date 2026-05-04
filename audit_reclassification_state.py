from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from collections import defaultdict, Counter

DB = Path("data/questions.db")
SCRIPT = Path("src/reclassify_topics.py")
LOG = Path("data/reclassify_log.jsonl")
VAULT = Path("vault")
BACKUP = Path("data/questions.backup-after-classroom-reclassification-complete.db")
REPORT = Path("audit_reclassification_report.md")

ORIGINAL_67_SLUGS = {
    "algorithm-properties",
    "algorithm-types",
    "dataset",
    "deep-fake",
    "machine-learning",
    "pseudocode",
    "scripting-language",
    "mail-merge",
    "foreign-key",
    "many-to-many",
    "primary-key",
    "relational-model",
    "sql-join",
    "sql-select",
    "color-wheel",
    "file-formats-image",
    "image-filters",
    "lasso-tool",
    "layers",
    "magic-wand",
    "raster-vs-vector",
    "hardware-cpu",
    "hardware-graphics-card",
    "hardware-motherboard",
    "hardware-storage",
    "cloud-saas",
    "gantt-chart",
    "sdlc",
    "system-architecture",
    "copyright-law",
    "plagiarism",
    "software-licenses",
    "standards-organizations",
    "cookies",
    "electronic-signature",
    "gps",
    "local-network",
    "countif",
    "data-validation",
    "dsum",
    "filtering-data",
    "lookup",
    "pivot-table",
    "pmt-ipmt-ppmt",
    "sumif",
    "sumif-renamed",
    "switch-function",
    "audio-discretization",
    "audio-effects",
    "audio-playback-devices",
    "audio-quantization",
    "camera-optics",
    "file-formats-audio",
    "fps",
    "sample-rate",
    "video-codec",
    "cms",
    "css",
    "css-selectors",
    "html",
    "html-forms",
    "seo",
    "web-domain",
    "web-hosting",
    "web-security",
    "web-standards",
    "wireframe-mockup",
}

SCRIPT_IMPACT_MATRIX = [
    ("topic_classifier.py", "b", "Still runs, but should optionally read topic_aliases and write question_topic_assignments instead of only questions.topic_id."),
    ("review_export.py", "a", "No required migration for current workflow; can still read questions.topic_id/curriculum_topics."),
    ("review_import.py", "a", "No required migration for current workflow; quality/approval fields unchanged."),
    ("sync_vault.py", "b", "Needs minor migration if new topics should have vault notes/section metadata. Should not delete DB topics missing from vault."),
    ("build_worksheet.py", "a", "Existing topic/area/class worksheet mode still works. Optional future enhancement: --section/--assessment-event."),
    ("build_exam.py", "b", "Still works, but DZI generation should eventually use assessment_events / DZI-relevant sections rather than broad random selection."),
    ("predict_answers.py", "a", "Answer prediction tables unchanged."),
    ("import_classroom_tests.py", "a", "Insert path unchanged. Optional future enhancement: invoke reclassifier after import."),
]

def table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}

def scalar(conn: sqlite3.Connection, sql: str, params=()):
    return conn.execute(sql, params).fetchone()[0]

def rows(conn: sqlite3.Connection, sql: str, params=()):
    return conn.execute(sql, params).fetchall()

def parse_override_returns(script_text: str):
    m = re.search(
        r"def deterministic_topic_override\(.*?\n(?=def\s|\Z)",
        script_text,
        flags=re.S,
    )
    if not m:
        return []

    block = m.group(0)
    found = []
    for line in block.splitlines():
        m2 = re.search(r'return\s+"([^"]+)",\s+"([^"]+)"', line)
        if m2:
            found.append((m2.group(1), m2.group(2)))
    return found

def load_logs():
    if not LOG.exists():
        return []

    out = []
    with LOG.open("r", encoding="utf-8") as f:
        for i, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            obj["_line"] = i
            out.append(obj)
    return out

def latest_log_by_question(logs):
    latest = {}
    for obj in logs:
        qid = obj.get("question_id")
        if qid is None:
            continue
        latest[qid] = obj
    return latest

def classify_decision(log_obj):
    if not log_obj:
        return "unknown_no_log"

    alias_action = log_obj.get("alias_action")
    alias_note = log_obj.get("alias_note") or ""

    if alias_action in ("keyword_override", "priority_override"):
        if alias_note.startswith("priority_override:"):
            return "keyword_override_priority"
        return "keyword_override_deterministic"

    # This script never uses embedding alone as the final classifier.
    # Embeddings produce candidates; BgGPT chooses from the shortlist.
    return "embedding_shortlist_bgGPT_pick"

def main():
    if not DB.exists():
        raise SystemExit(f"Missing DB: {DB}")

    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row

    report = []
    add = report.append

    add("# Reclassification Audit Report\n")

    # ------------------------------------------------------------------
    # Basic state
    # ------------------------------------------------------------------
    add("## 0. Current DB state\n")
    total_q = scalar(conn, "SELECT COUNT(*) FROM questions")
    tagged_q = scalar(conn, "SELECT COUNT(*) FROM questions WHERE topic_id IS NOT NULL")
    untagged_q = scalar(conn, "SELECT COUNT(*) FROM questions WHERE topic_id IS NULL")
    topics_total = scalar(conn, "SELECT COUNT(*) FROM curriculum_topics")
    add(f"- Total questions: `{total_q}`")
    add(f"- Questions with topic_id: `{tagged_q}`")
    add(f"- Questions with topic_id NULL: `{untagged_q}`")
    add(f"- Total curriculum_topics: `{topics_total}`\n")

    add("Untagged by source:\n")
    add("```text")
    for r in rows(conn, """
        SELECT source_exam, COUNT(*) AS c
        FROM questions
        WHERE topic_id IS NULL
        GROUP BY source_exam
        ORDER BY source_exam
    """):
        add(f"{r['source_exam']} | {r['c']}")
    add("```\n")

    # ------------------------------------------------------------------
    # 1. Override scale + reasoning
    # ------------------------------------------------------------------
    add("## 1. Override scale + reasoning\n")
    script_text = SCRIPT.read_text(encoding="utf-8") if SCRIPT.exists() else ""
    overrides = parse_override_returns(script_text)
    add(f"Total deterministic/priority override return rules found: `{len(overrides)}`\n")

    logs = load_logs()
    latest = latest_log_by_question(logs)

    active_hybrid = rows(conn, """
        SELECT
            qta.question_id,
            qta.topic_id,
            ct.topic_slug,
            q.source_exam,
            q.prompt
        FROM question_topic_assignments qta
        JOIN questions q ON q.id = qta.question_id
        JOIN curriculum_topics ct ON ct.id = qta.topic_id
        WHERE qta.is_active = 1
          AND qta.method = 'hybrid_reclassifier_v1'
        ORDER BY qta.question_id
    """)

    caught_by_note = defaultdict(list)
    caught_by_slug = defaultdict(list)

    for r in active_hybrid:
        qid = r["question_id"]
        log = latest.get(qid)
        if not log:
            continue
        alias_note = log.get("alias_note") or ""
        resolved_slug = log.get("resolved_slug")
        if (log.get("alias_action") in ("keyword_override", "priority_override")):
            caught_by_slug[resolved_slug].append(qid)
            if alias_note:
                caught_by_note[alias_note].append(qid)

    add("| # | slug returned | override reason in code | active questions caught |")
    add("|---:|---|---|---|")
    for i, (slug, reason) in enumerate(overrides, 1):
        qids = caught_by_note.get(reason) or caught_by_slug.get(slug) or []
        qids_txt = ", ".join(map(str, sorted(set(qids)))) if qids else "—"
        add(f"| {i} | `{slug}` | {reason} | {qids_txt} |")
    add("")

    add("Motivation summary:")
    add("- Most overrides were added because embeddings/BgGPT chose a nearby but wrong topic when the whitelist was missing or semantically crowded.")
    add("- Examples observed during dry-runs: social-network questions going to `web-standards`; MS Word table formatting going to `dsum`; router/network questions going to unrelated hardware/history topics; CMS security going to `archive-compression`; Access action queries going to select-query topics.")
    add("- The overrides should be considered high-precision classroom-test rescue rules, not a clean long-term architecture.\n")

    # ------------------------------------------------------------------
    # 2. Decision pathway breakdown
    # ------------------------------------------------------------------
    add("## 2. Decision pathway breakdown for classroom reclassified questions\n")

    classroom_sources = ("classroom_tests_8_12_2026", "classroom_tests_alt_2026")
    active_classroom = rows(conn, """
        SELECT
            q.id AS question_id,
            q.source_exam,
            qta.method,
            ct.topic_slug
        FROM questions q
        JOIN question_topic_assignments qta ON qta.question_id = q.id AND qta.is_active = 1
        LEFT JOIN curriculum_topics ct ON ct.id = qta.topic_id
        WHERE q.source_exam IN ('classroom_tests_8_12_2026', 'classroom_tests_alt_2026')
        ORDER BY q.id
    """)

    # Only count those that are now tagged from the two classroom sources.
    reclassified = [r for r in active_classroom if r["topic_slug"] is not None]
    decision_counts = Counter()
    manual_methods = Counter()

    for r in reclassified:
        method = r["method"]
        qid = r["question_id"]
        if method == "hybrid_reclassifier_v1":
            decision_counts[classify_decision(latest.get(qid))] += 1
        elif method.startswith("manual"):
            decision_counts["manual_correction_or_manual_final"] += 1
            manual_methods[method] += 1
        else:
            decision_counts[f"other_method:{method}"] += 1

    add(f"Total tagged classroom questions counted: `{len(reclassified)}`")
    add("")
    add("| decision pathway | count |")
    add("|---|---:|")
    add(f"| pure embedding final decision | `{decision_counts.get('pure_embedding', 0)}` |")
    add(f"| embedding shortlist + BgGPT pick | `{decision_counts.get('embedding_shortlist_bgGPT_pick', 0)}` |")
    add(f"| keyword override deterministic | `{decision_counts.get('keyword_override_deterministic', 0)}` |")
    add(f"| keyword override priority | `{decision_counts.get('keyword_override_priority', 0)}` |")
    add(f"| manual correction / manual final | `{decision_counts.get('manual_correction_or_manual_final', 0)}` |")
    add(f"| unknown / no matching log | `{decision_counts.get('unknown_no_log', 0)}` |")
    add("")
    if manual_methods:
        add("Manual methods:")
        add("```text")
        for k, v in sorted(manual_methods.items()):
            add(f"{k}: {v}")
        add("```\n")

    add("Note: the script does not use pure embedding as a final assignment method. Embeddings only build/rank the candidate shortlist; BgGPT or deterministic override makes the final choice.\n")

    # ------------------------------------------------------------------
    # 3. DB consistency checks
    # ------------------------------------------------------------------
    add("## 3. DB consistency checks\n")

    fk = rows(conn, "PRAGMA foreign_key_check")
    dup_active = rows(conn, """
        SELECT question_id, COUNT(*) AS c
        FROM question_topic_assignments
        WHERE is_active = 1
        GROUP BY question_id
        HAVING COUNT(*) > 1
    """)
    bad_topic_fk = rows(conn, """
        SELECT q.id, q.topic_id
        FROM questions q
        LEFT JOIN curriculum_topics ct ON ct.id = q.topic_id
        WHERE q.topic_id IS NOT NULL
          AND ct.id IS NULL
    """)

    bad_tsa = rows(conn, """
        SELECT tsa.id, tsa.topic_id, tsa.section_id
        FROM topic_section_assignments tsa
        LEFT JOIN curriculum_topics ct ON ct.id = tsa.topic_id
        LEFT JOIN curriculum_sections cs ON cs.id = tsa.section_id
        WHERE ct.id IS NULL OR cs.id IS NULL
    """)

    alias_dupes = rows(conn, """
        SELECT alias_slug, COUNT(*) AS c
        FROM topic_aliases
        GROUP BY alias_slug
        HAVING COUNT(*) > 1
        ORDER BY c DESC, alias_slug
    """)

    add(f"- PRAGMA foreign_key_check rows: `{len(fk)}`")
    add(f"- Duplicate active question_topic_assignments: `{len(dup_active)}`")
    add(f"- questions.topic_id pointing to non-existent topic: `{len(bad_topic_fk)}`")
    add(f"- topic_section_assignments with invalid topic/section: `{len(bad_tsa)}`")
    add(f"- Duplicate topic_aliases.alias_slug rows: `{len(alias_dupes)}`")
    add("")

    if fk:
        add("Foreign key check failures:")
        add("```text")
        for r in fk:
            add(str(tuple(r)))
        add("```\n")

    if dup_active:
        add("Duplicate active assignment rows:")
        add("```text")
        for r in dup_active:
            add(f"question_id={r['question_id']} count={r['c']}")
        add("```\n")

    if alias_dupes:
        add("Duplicate alias_slug rows:")
        add("```text")
        for r in alias_dupes:
            add(f"{r['alias_slug']} count={r['c']}")
        add("```\n")

    # ------------------------------------------------------------------
    # 4. DZI relevance flags
    # ------------------------------------------------------------------
    add("## 4. ДЗИ-relevance flagging\n")

    sec_cols = table_columns(conn, "curriculum_sections")
    if {"section_slug", "is_dzi_relevant", "source_url"} <= sec_cols:
        dzi_rows = rows(conn, """
            SELECT section_slug, is_dzi_relevant, source_url
            FROM curriculum_sections
            WHERE is_dzi_relevant = 1
            ORDER BY class, display_order, section_slug
        """)
        add("```text")
        for r in dzi_rows:
            add(f"{r['section_slug']} | {r['is_dzi_relevant']} | {r['source_url']}")
        add("```\n")
    else:
        add("Cannot run exact requested query because curriculum_sections does not have all requested columns.")
        add(f"Available curriculum_sections columns: `{', '.join(sorted(sec_cols))}`\n")

    add("Authority used for current flags: `NONE / not verified`.")
    add("These flags were seeded from the project’s working curriculum assumptions and user-provided distribution, not from an official МОН DZI exam-program source. Treat them as provisional until checked against the official current МОН изпитна програма.\n")

    # ------------------------------------------------------------------
    # 5. Per-script impact matrix
    # ------------------------------------------------------------------
    add("## 5. Per-script impact matrix\n")
    add("| script | status | impact / needed change |")
    add("|---|---|---|")
    for script, status, note in SCRIPT_IMPACT_MATRIX:
        status_label = {"a": "a) unaffected", "b": "b) needs minor migration", "c": "c) needs full rewrite"}[status]
        add(f"| `{script}` | {status_label} | {note} |")
    add("")

    # ------------------------------------------------------------------
    # 6. Vault state
    # ------------------------------------------------------------------
    add("## 6. Vault state\n")

    topics = rows(conn, """
        SELECT topic_slug, note_path
        FROM curriculum_topics
        ORDER BY topic_slug
    """)

    new_topics = [r for r in topics if r["topic_slug"] not in ORIGINAL_67_SLUGS]
    with_notes = []
    without_notes = []

    for r in new_topics:
        slug = r["topic_slug"]
        note_path = r["note_path"] or f"Topics/{slug}.md"
        full = VAULT / note_path
        if full.exists():
            with_notes.append(slug)
        else:
            without_notes.append(slug)

    add(f"- New topics expected from expansion: `{len(new_topics)}`")
    add(f"- New topics with vault notes: `{len(with_notes)}`")
    add(f"- New topics without vault notes: `{len(without_notes)}`")
    add("")

    if without_notes:
        add("New topics without vault notes:")
        add("```text")
        for slug in without_notes:
            add(slug)
        add("```\n")

    sync_text = Path("src/sync_vault.py").read_text(encoding="utf-8") if Path("src/sync_vault.py").exists() else ""
    deletes_topics = bool(re.search(r"DELETE\s+FROM\s+curriculum_topics", sync_text, flags=re.I))
    add(f"`sync_vault.py` appears to delete missing curriculum_topics: `{deletes_topics}`")
    add("")
    add("Recommendation: generate stub notes for the new topics rather than leaving them invisible in the vault. Do not make sync_vault delete DB topics that do not have notes. Best next step is a `generate_topic_stubs.py` utility that creates minimal `vault/Topics/<slug>.md` files from curriculum_topics.note_path/title_bg/description/classes/section.\n")

    # ------------------------------------------------------------------
    # 7. Backup confirmation
    # ------------------------------------------------------------------
    add("## 7. Backup confirmation\n")
    add(f"- Backup path: `{BACKUP}`")
    add(f"- Exists: `{BACKUP.exists()}`")

    if BACKUP.exists():
        bconn = sqlite3.connect(BACKUP)
        b_total = scalar(bconn, "SELECT COUNT(*) FROM questions")
        b_tagged = scalar(bconn, "SELECT COUNT(*) FROM questions WHERE topic_id IS NOT NULL")
        b_null = scalar(bconn, "SELECT COUNT(*) FROM questions WHERE topic_id IS NULL")
        bconn.close()
        add(f"- Backup total questions: `{b_total}`")
        add(f"- Backup with topic_id NOT NULL: `{b_tagged}`")
        add(f"- Backup with topic_id NULL: `{b_null}`")
        add("")
        add("Expected backup counts:")
        add("- total questions: `819`")
        add("- topic_id NOT NULL: `794`")
        add("- topic_id NULL: `25`")
    else:
        add("")
        add("Backup missing. Create it with:")
        add("```bash")
        add("cp data/questions.db data/questions.backup-after-classroom-reclassification-complete.db")
        add("```")

    conn.close()
    REPORT.write_text("\n".join(report), encoding="utf-8")
    print(f"Wrote {REPORT}")

if __name__ == "__main__":
    main()
