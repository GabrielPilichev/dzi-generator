import argparse
import json
import random
import re
import sqlite3
from collections import defaultdict
from datetime import datetime
from pathlib import Path

DB_PATH = Path("data/questions.db")
CONFIG_PATH = Path("data/dzi_training/class_mix.json")
DATA_OUT_DIR = Path("data/dzi_training/sets")
VAULT_OUT_DIR = Path("vault/Generated/DZI-Training/sets")

APPROVED_FILTER = "(q.is_ai_generated = 0 OR q.quality_score >= 1.0)"

PROMPT_COLUMNS = [
    "prompt_bg",
    "question_text_bg",
    "question_text",
    "prompt",
    "text_bg",
    "text",
    "body",
]

QUESTION_TYPE_COLUMNS = [
    "question_type",
    "type",
]


def slugify(value: str) -> str:
    value = value.lower().strip()
    value = re.sub(r"[^a-z0-9а-я]+", "-", value)
    value = re.sub(r"-+", "-", value).strip("-")
    return value or "dzi-training-set"


def table_columns(con, table):
    return {row[1] for row in con.execute(f"PRAGMA table_info({table})").fetchall()}


def pick_column(columns, candidates):
    for col in candidates:
        if col in columns:
            return col
    return None


def load_mix():
    if not CONFIG_PATH.exists():
        raise SystemExit(f"Missing config: {CONFIG_PATH}")

    data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    classes = data.get("classes") or {}

    mix = {}
    for key, value in classes.items():
        cls = int(key)
        pct = int(value)
        if pct > 0:
            mix[cls] = pct

    total = sum(mix.values())
    if total != 100:
        raise SystemExit(f"Class percentages must sum to 100, got {total}: {mix}")

    return data, mix


def allocate_counts(total_count, mix):
    raw = []
    allocated = {}
    used = 0

    for cls, pct in sorted(mix.items()):
        exact = total_count * pct / 100.0
        base = int(exact)
        frac = exact - base
        allocated[cls] = base
        used += base
        raw.append((frac, cls))

    remaining = total_count - used
    for _, cls in sorted(raw, reverse=True):
        if remaining <= 0:
            break
        allocated[cls] += 1
        remaining -= 1

    return allocated


def get_pool(con, classes):
    class_placeholders = ",".join("?" for _ in classes)

    query = f"""
    WITH question_classes AS (
        SELECT DISTINCT
            q.id AS question_id,
            cs.class AS class
        FROM questions q
        JOIN question_topic_assignments qta
          ON qta.question_id = q.id
         AND qta.is_active = 1
        JOIN topic_section_assignments tsa
          ON tsa.topic_id = qta.topic_id
        JOIN curriculum_sections cs
          ON cs.id = tsa.section_id
        WHERE {APPROVED_FILTER}
          AND cs.class IN ({class_placeholders})
    )
    SELECT class, question_id
    FROM question_classes
    ORDER BY class, question_id;
    """

    pools = defaultdict(list)
    for row in con.execute(query, tuple(classes)):
        pools[int(row["class"])].append(int(row["question_id"]))

    return pools


def choose_questions(pools, target_counts, seed):
    rng = random.Random(seed)
    chosen_by_class = {}
    already_used = set()

    for cls, target in sorted(target_counts.items()):
        available = [qid for qid in pools.get(cls, []) if qid not in already_used]
        rng.shuffle(available)

        selected = available[:target]
        chosen_by_class[cls] = selected
        already_used.update(selected)

    # If a class was short, fill from any remaining allowed class.
    missing = sum(target_counts.values()) - sum(len(v) for v in chosen_by_class.values())
    if missing > 0:
        fallback = []
        for cls in sorted(pools):
            for qid in pools[cls]:
                if qid not in already_used:
                    fallback.append((cls, qid))

        rng.shuffle(fallback)

        for cls, qid in fallback[:missing]:
            chosen_by_class.setdefault(cls, []).append(qid)
            already_used.add(qid)

    return chosen_by_class


def fetch_questions(con, question_ids):
    if not question_ids:
        return {}

    question_cols = table_columns(con, "questions")
    prompt_col = pick_column(question_cols, PROMPT_COLUMNS)
    qtype_col = pick_column(question_cols, QUESTION_TYPE_COLUMNS)

    if not prompt_col:
        raise SystemExit(
            "Could not find a known prompt column in questions table. "
            f"Available columns: {sorted(question_cols)}"
        )

    placeholders = ",".join("?" for _ in question_ids)
    rows = con.execute(
        f"SELECT * FROM questions WHERE id IN ({placeholders})",
        tuple(question_ids),
    ).fetchall()

    result = {}
    for row in rows:
        result[int(row["id"])] = {
            "id": int(row["id"]),
            "prompt": row[prompt_col] or "",
            "question_type": row[qtype_col] if qtype_col else "",
            "raw": dict(row),
        }

    return result


def fetch_options(con, question_ids):
    options_cols = table_columns(con, "multiple_choice_options")
    if not {"question_id", "option_letter"}.issubset(options_cols):
        return {}

    text_col = pick_column(options_cols, ["option_text_bg", "option_text", "text_bg", "text"])
    if not text_col:
        return {}

    has_correct = "is_correct" in options_cols

    placeholders = ",".join("?" for _ in question_ids)
    select_correct = ", is_correct" if has_correct else ", 0 AS is_correct"

    rows = con.execute(
        f"""
        SELECT question_id, option_letter, {text_col} AS option_text {select_correct}
        FROM multiple_choice_options
        WHERE question_id IN ({placeholders})
        ORDER BY question_id, option_letter
        """,
        tuple(question_ids),
    ).fetchall()

    grouped = defaultdict(list)
    for row in rows:
        grouped[int(row["question_id"])].append(
            {
                "letter": row["option_letter"],
                "text": row["option_text"] or "",
                "is_correct": bool(row["is_correct"]),
            }
        )

    return grouped


def write_outputs(name, seed, count, config, target_counts, chosen_by_class, questions, options):
    now = datetime.now()
    date_str = now.strftime("%Y-%m-%d")
    ts = now.strftime("%Y%m%d-%H%M%S")
    slug = slugify(name)
    filename = f"{date_str}-{slug}-{ts}.md"

    md_path = VAULT_OUT_DIR / filename
    json_path = DATA_OUT_DIR / filename.replace(".md", ".json")

    flat_ids = []
    for cls in sorted(chosen_by_class):
        flat_ids.extend(chosen_by_class[cls])

    lines = []
    lines.append("---")
    lines.append(f'title: "{name}"')
    lines.append('type: "dzi_training_set"')
    lines.append(f'date: "{date_str}"')
    lines.append(f'total_questions: {len(flat_ids)}')
    lines.append(f'seed: "{seed}"')
    lines.append("tags: [dzi, training, generated]")
    lines.append("---")
    lines.append("")
    lines.append(f"# {name}")
    lines.append("")
    lines.append("> Тренировъчен комплект. Не означава официална ДЗИ релевантност без проверка срещу програма на МОН.")
    lines.append("")
    lines.append("## Разпределение")
    lines.append("")
    lines.append("| Клас | Цел | Избрани |")
    lines.append("|---:|---:|---:|")
    for cls in sorted(target_counts):
        lines.append(f"| {cls} | {target_counts[cls]} | {len(chosen_by_class.get(cls, []))} |")

    lines.append("")
    lines.append("## Въпроси")
    lines.append("")

    answer_lines = []
    answer_lines.append("## Отговори")
    answer_lines.append("")

    number = 1
    for cls in sorted(chosen_by_class):
        lines.append(f"## {cls}. клас")
        lines.append("")
        for qid in chosen_by_class[cls]:
            q = questions[qid]
            qopts = options.get(qid, [])

            lines.append(f"### {number}. Въпрос #{qid}")
            lines.append("")
            if q["question_type"]:
                lines.append(f"- Тип: `{q['question_type']}`")
            lines.append(f"- Клас: `{cls}`")
            lines.append("")
            lines.append(q["prompt"].strip() or "_Няма текст на въпроса._")
            lines.append("")

            if qopts:
                for opt in qopts:
                    lines.append(f"- **{opt['letter']}**. {opt['text']}")
                correct = [opt for opt in qopts if opt["is_correct"]]
                if correct:
                    answer_lines.append(
                        f"{number}. #{qid}: " +
                        ", ".join(f"{opt['letter']}. {opt['text']}" for opt in correct)
                    )
                else:
                    answer_lines.append(f"{number}. #{qid}: няма маркиран верен отговор")
            else:
                answer_lines.append(f"{number}. #{qid}: свободен/практически отговор или няма опции")

            lines.append("")
            number += 1

    lines.append("---")
    lines.append("")
    lines.extend(answer_lines)
    lines.append("")

    md_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.parent.mkdir(parents=True, exist_ok=True)

    md_path.write_text("\n".join(lines), encoding="utf-8")

    payload = {
        "title": name,
        "created_at": now.isoformat(timespec="seconds"),
        "seed": seed,
        "requested_count": count,
        "actual_count": len(flat_ids),
        "config": config,
        "target_counts": {str(k): v for k, v in target_counts.items()},
        "chosen_by_class": {str(k): v for k, v in chosen_by_class.items()},
        "question_ids": flat_ids,
        "markdown_path": str(md_path),
    }
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    return md_path, json_path, payload


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--count", type=int, default=28, help="Total questions in the training set.")
    parser.add_argument("--seed", default=None, help="Stable random seed.")
    parser.add_argument("--name", default=None, help="Training set title.")
    args = parser.parse_args()

    config, mix = load_mix()
    seed = args.seed or datetime.now().strftime("%Y%m%d-%H%M%S")
    name = args.name or f"ДЗИ тренировъчен комплект — {args.count} въпроса"

    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row

    target_counts = allocate_counts(args.count, mix)
    pools = get_pool(con, sorted(mix.keys()))
    chosen_by_class = choose_questions(pools, target_counts, seed)

    selected_ids = []
    for cls in sorted(chosen_by_class):
        selected_ids.extend(chosen_by_class[cls])

    questions = fetch_questions(con, selected_ids)
    options = fetch_options(con, selected_ids)
    con.close()

    md_path, json_path, payload = write_outputs(
        name=name,
        seed=seed,
        count=args.count,
        config=config,
        target_counts=target_counts,
        chosen_by_class=chosen_by_class,
        questions=questions,
        options=options,
    )

    print(f"Wrote {md_path}")
    print(f"Wrote {json_path}")
    print(f"Questions: {payload['actual_count']}")
    print("Distribution:")
    for cls in sorted(payload["chosen_by_class"], key=int):
        print(f"  class {cls}: {len(payload['chosen_by_class'][cls])}")


if __name__ == "__main__":
    main()
