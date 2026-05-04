"""
Hybrid topic reclassifier.

Purpose:
  - Reclassify approved questions, especially unclassified/orphan questions.
  - Uses local Ollama only.
  - Embeddings narrow the candidate topics.
  - BgGPT chooses among the top candidates.
  - Updates questions.topic_id only when confidence and embedding margin pass.
  - Writes audit rows to question_topic_assignments.
  - Writes JSONL audit log to data/reclassify_log.jsonl.

Default mode:
  - only approved questions
  - only questions currently unclassified / topic_id IS NULL
  - dry-run recommended first

Usage:
  python3 src/reclassify_topics.py --limit 10 --dry-run
  python3 src/reclassify_topics.py --limit 50 --threshold 0.70 --margin 0.03 --dry-run
  python3 src/reclassify_topics.py --source classroom_tests_8_12_2026 --dry-run
  python3 src/reclassify_topics.py --include-classified --limit 20 --dry-run
"""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agents.ollama_client import (
    OllamaClient,
    OllamaError,
    DEFAULT_CHAT_MODEL,
    DEFAULT_EMBED_MODEL,
    DEFAULT_HOST,
)


DEFAULT_DB = Path("data/questions.db")
DEFAULT_LOG = Path("data/reclassify_log.jsonl")
DEFAULT_THRESHOLD = 0.70
DEFAULT_MARGIN = 0.03
DEFAULT_TOP_K = 5


SYSTEM_PROMPT = """Ти си експерт по българската учебна програма по Информационни технологии.

Твоята задача е да класифицираш въпрос към НАЙ-ПОДХОДЯЩИЯ topic_slug.

Ще получиш:
1. Въпрос.
2. Малък списък от кандидат-теми.

ПРАВИЛА:
- Избери само от кандидатите.
- Ако нито един кандидат не е добър, върни slug "none".
- Не измисляй нови slug-ове.
- Отговаряй само с JSON, без markdown.
- Формат:
{"slug": "<topic_slug или none>", "confidence": <0.0 до 1.0>, "reason": "<кратко обяснение на български>"}

Скала:
0.90-1.00 = директно съвпадение
0.75-0.89 = силно съвпадение
0.60-0.74 = вероятно, но не напълно сигурно
0.00-0.59 = несигурно / по-добре не променяй
"""


def cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def parse_json_response(raw: str) -> dict[str, Any] | None:
    if not raw:
        return None
    start = raw.find("{")
    end = raw.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        parsed = json.loads(raw[start:end + 1])
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    return parsed


def write_log(path: Path, entry: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def fetch_aliases(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    rows = conn.execute("""
        SELECT
            ta.alias_slug,
            ta.action,
            ta.topic_id,
            ct.topic_slug
        FROM topic_aliases ta
        LEFT JOIN curriculum_topics ct ON ct.id = ta.topic_id
    """).fetchall()
    return {
        alias: {
            "action": action,
            "topic_id": topic_id,
            "topic_slug": topic_slug,
        }
        for alias, action, topic_id, topic_slug in rows
    }


def fetch_topics(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute("""
        SELECT
            ct.id,
            ct.topic_slug,
            ct.title_bg,
            COALESCE(ct.description, '') AS description,
            COALESCE(ca.area_id, '') AS area_slug,
            COALESCE(ca.title_bg, '') AS area_title,
            COALESCE(cs.section_slug, '') AS section_slug,
            COALESCE(cs.title_bg, '') AS section_title
        FROM curriculum_topics ct
        LEFT JOIN curriculum_areas ca ON ca.id = ct.area_id
        LEFT JOIN curriculum_sections cs ON cs.id = ct.section_id
        ORDER BY ca.area_id, ct.topic_slug
    """).fetchall()

    topics = []
    for row in rows:
        (
            topic_id,
            slug,
            title,
            description,
            area_slug,
            area_title,
            section_slug,
            section_title,
        ) = row
        text = "\n".join([
            f"slug: {slug}",
            f"title_bg: {title}",
            f"area: {area_slug} {area_title}",
            f"section: {section_slug} {section_title}",
            f"description: {description}",
        ]).strip()

        topics.append({
            "id": topic_id,
            "slug": slug,
            "title": title,
            "description": description,
            "area_slug": area_slug,
            "area_title": area_title,
            "section_slug": section_slug,
            "section_title": section_title,
            "text": text,
            "embedding": None,
        })
    return topics


def question_text(conn: sqlite3.Connection, q: sqlite3.Row) -> str:
    parts = [
        f"question_id: {q['id']}",
        f"source_exam: {q['source_exam'] or ''}",
        f"source_number: {q['source_number'] or ''}",
        f"subject: {q['subject'] or ''}",
        f"level: {q['level'] or ''}",
        f"year: {q['year'] or ''}",
        f"source_topic: {q['topic'] or ''}",
        f"legacy_topic: {q['legacy_topic'] or ''}",
        "",
        "Въпрос:",
        q["prompt"] or "",
    ]

    options = conn.execute("""
        SELECT option_letter, option_text, is_correct
        FROM multiple_choice_options
        WHERE question_id = ?
        ORDER BY option_letter
    """, (q["id"],)).fetchall()

    if options:
        parts.append("")
        parts.append("Отговори:")
        for letter, text, _is_correct in options:
            parts.append(f"{letter}) {text}")

    subs = conn.execute("""
        SELECT subquestion_number, subquestion_text, correct_answer, answer_alternatives
        FROM fill_in_subquestions
        WHERE question_id = ?
        ORDER BY subquestion_number
    """, (q["id"],)).fetchall()

    if subs:
        parts.append("")
        parts.append("Подвъпроси / отговори:")
        for num, subtext, answer, alternatives in subs:
            parts.append(f"{num}. {subtext or ''} | answer: {answer or ''} | alternatives: {alternatives or ''}")

    return "\n".join(parts).strip()


def fetch_targets(
    conn: sqlite3.Connection,
    *,
    include_classified: bool,
    source: str | None,
    limit: int | None,
) -> list[sqlite3.Row]:
    conn.row_factory = sqlite3.Row

    where = [
        "(q.is_ai_generated = 0 OR q.quality_score >= 1.0)"
    ]
    params: list[Any] = []

    if not include_classified:
        where.append("q.topic_id IS NULL")

    if source:
        where.append("q.source_exam LIKE ?")
        params.append(f"%{source}%")

    sql = f"""
        SELECT
            q.id,
            q.topic_id,
            q.prompt,
            q.source_exam,
            q.source_number,
            q.subject,
            q.level,
            q.year,
            q.topic,
            q.legacy_topic,
            q.question_type,
            ct.topic_slug AS current_slug
        FROM questions q
        LEFT JOIN curriculum_topics ct ON ct.id = q.topic_id
        WHERE {" AND ".join(where)}
        ORDER BY q.id
    """

    if limit:
        sql += " LIMIT ?"
        params.append(limit)

    return conn.execute(sql, params).fetchall()


def build_llm_prompt(q_text: str, candidates: list[dict[str, Any]]) -> str:
    candidate_lines = []
    for i, c in enumerate(candidates, 1):
        candidate_lines.append(
            f"{i}. slug: {c['slug']}\n"
            f"   title_bg: {c['title']}\n"
            f"   area: {c['area_slug']} / {c['area_title']}\n"
            f"   section: {c['section_slug']} / {c['section_title']}\n"
            f"   embedding_similarity: {c['similarity']:.4f}"
        )

    return f"""Въпрос:
\"\"\"
{q_text}
\"\"\"

Кандидат-теми:
{chr(10).join(candidate_lines)}

Избери най-подходящия slug само от кандидатите.
Ако няма добър match, върни slug "none".

Отговор само JSON:"""



def keyword_topic_boosts(q_text: str) -> dict[str, float]:
    """
    Rule-based boosts for obvious Bulgarian classroom-test cues.

    This does not directly classify the question.
    It only makes sure the correct topic appears among LLM candidates.
    """
    t = (q_text or "").lower()
    boosts: dict[str, float] = {}

    def add(slug: str, boost: float = 0.08) -> None:
        boosts[slug] = max(boosts.get(slug, 0.0), boost)

    # Social networks / online communication
    if any(x in t for x in [
        "instagram", "инстаграм", "linkedin", "линктин", "facebook", "фейсбук",
        "youtube", "ютюб", "tiktok", "тикток", "социална мрежа", "социални мрежи",
        "x (екс)", "twitter", "туитър",
    ]):
        add("social-networks", 0.12)

    # E-learning / shared learning environments
    if any(x in t for x in [
        "електронно обучение", "електронно обуч", "дистанционно обучение",
        "среди за електронно", "учебни ресурси", "споделена съвместна работа",
    ]):
        add("e-learning-platforms", 0.12)

    # Search operators / effective search
    if any(x in t for x in [
        "intext:", "intitle:", "filetype:", "site:", "not america", " south not ",
        "търсене в интернет", "при търсене", "file explorer", "ключови думи",
        "оператор", "оператори за търсене",
    ]):
        add("effective-information-search", 0.12)

    # VoIP / internet voice/video communication
    if any(x in t for x in [
        "пренасянето на глас", "пренасяне на глас", "глас (видео)",
        "видео) между два компютъра", "voice over ip", "voip",
        "интернет телефония", "гласова комуникация",
    ]):
        add("voip-internet-communication", 0.14)

    # Raster images
    if any(x in t for x in [
        "растерно изображение", "растерни изображения", "пиксел", "пиксели",
        "разделителна способност", "растерна графика",
    ]):
        add("raster-image-properties", 0.14)
        add("raster-vs-vector", 0.06)

    # Web page properties
    if any(x in t for x in [
        "характеристики", "страница", "уеб страница", "динамични сайтове",
        "статичен сайт", "навигация", "хипервръзка",
    ]) and any(x in t for x in ["сайт", "страница", "уеб"]):
        add("web-page-properties", 0.12)

    # Word / office table formatting
    if any(x in t for x in [
        "ms word", "word", "текстообработваща", "форматиране на таблица",
        "таблица в текстов", "меню", "control panel",
    ]) and any(x in t for x in ["word", "тексто", "таблица", "control panel"]):
        add("word-table-formatting", 0.10)

    # Spreadsheet generic functions
    if any(x in t for x in [
        "функция в електронна таблица", "електронна таблица", "excel",
        "формула", "аргумент", "функция се обозначава",
    ]):
        add("spreadsheet-function-basics", 0.10)

    # Computer history
    if any(x in t for x in [
        "изобретател", "abc", "електронноизчислителната машина",
        "история на компютърната техника", "поколения компютри",
    ]):
        add("computer-history", 0.14)

    # Operating systems
    if any(x in t for x in [
        "операционната система", "операционна система", "операционни системи",
        "функции на операционната", "не е характерна за операционната",
    ]):
        add("operating-systems", 0.12)

    # Mobile OS
    if any(x in t for x in [
        "мобилна", "мобилни операционни", "android", "ios",
        "не е мобилна",
    ]):
        add("mobile-operating-systems", 0.12)

    # Peripherals
    if any(x in t for x in [
        "периферно устройство", "периферни устройства", "комуникационно устройство",
        "комуникационно", "входно устройство", "изходно устройство",
    ]):
        add("peripheral-devices", 0.16)

    # Drivers
    if any(x in t for x in [
        "драйвер", "driver", "драйвери",
    ]):
        add("device-drivers", 0.14)

    # Install / uninstall software
    if any(x in t for x in [
        "инсталирането на софтуер", "инсталиране на софтуер",
        "инсталиране на програми", "деинсталиране", "деинсталиране на програми",
        "setup", ".exe", "изпълним файл", "изпълним", "разширение",
        "списък на инсталираните програми", "инсталираните програми",
        "control panel",
    ]):
        add("install-uninstall-software", 0.18)

    # Archives / compression
    if any(x in t for x in [
        "архив", "архивиране", "архивираща", "архивирани", "компресиране",
        "компресирани", "декомпресиране", "zip", "rar", "7z", "саморазархивиращ",
    ]):
        add("archive-compression", 0.14)

    # Software licenses/types
    if any(x in t for x in [
        "freeware", "свободен софтуер", "не заплаща", "безплатен софтуер",
        "лиценз", "лицензи", "shareware", "open source",
    ]):
        add("software-types-freeware", 0.12)
        add("software-licenses", 0.06)

    # Network topology/devices/protocols
    if any(x in t for x in ["топология", "звезда", "шина", "пръстен"]):
        add("network-topology", 0.16)

    if any(x in t for x in [
        "маршрутизатор", "рутер", "точка за достъп", "безжичните устройства",
        "окабелена част", "суич", "комутатор",
    ]):
        add("network-devices", 0.16)

    if any(x in t for x in [
        "протоколът tcp", "tcp", "протоколът pop", " pop", "ftp",
        "пренос на файлове", "трансфер на файлове", "име на ip адрес",
        "ip адрес", "домейн",
    ]):
        add("network-protocols", 0.16)

    if any(x in t for x in [
        "сигурността на информацията", "целостта на данните",
        "целостта", "поверителност", "достъпност",
    ]):
        add("information-security", 0.16)

    if any(x in t for x in ["облачните технологии", "облачни технологии", "облак"]):
        add("cloud-technologies", 0.14)

    if any(x in t for x in ["биометрич", "биометрична идентификация", "поведенческите"]):
        add("biometric-identification", 0.16)

    if any(x in t for x in ["device manager", "диспечера за управление на хардуера"]):
        add("device-manager", 0.16)

    # Access / information systems
    if any(x in t for x in ["анализ на реализуемостта", "реализуемостта на ис"]):
        add("information-system-feasibility", 0.16)

    if any(x in t for x in ["таблица в програмата ms access", "панела tables"]):
        add("access-tables", 0.16)

    if any(x in t for x in ["формуляр", "формуляри", "улесняват въвеждането на данните"]):
        add("access-forms", 0.16)

    if any(x in t for x in ["макросите в бд", "макроси в бд", "макросите"]):
        add("access-macros", 0.16)

    if any(x in t for x in ["заявката за избиране", "заявка за избиране"]):
        add("access-select-queries", 0.16)

    if any(x in t for x in [
        "създаване на нови таблици", "добавяне на данни", "актуализиране",
        "изтриване на данни", "заявка, която се използва",
    ]):
        add("access-action-queries", 0.16)

    # Web planning / architecture / responsive / RSS
    if any(x in t for x in [
        "целта на един сайт", "целевата група", "планиране на уеб сайт",
        "дейност, извършвана при планиране",
    ]):
        add("website-planning", 0.16)

    if any(x in t for x in [
        "информационната архитектура", "основните в един уеб сайт",
        "структурата на уеб сайта", "навигация",
    ]):
        add("website-information-architecture", 0.16)

    if any(x in t for x in ["различен размер на екрана", "responsive", "адаптивен дизайн"]):
        add("responsive-design", 0.16)

    if any(x in t for x in ["обмен на новини", "rss"]):
        add("rss-feeds", 0.16)

    return boosts


def build_candidates_with_boosts(
    scored: list[dict[str, Any]],
    slug_to_topic: dict[str, dict[str, Any]],
    q_text: str,
    top_k: int,
) -> list[dict[str, Any]]:
    """
    Combine embedding candidates with rule-boosted candidates.

    Boosted candidates are inserted even when embeddings rank them low.
    Final list is sorted by adjusted similarity.
    """
    boosts = keyword_topic_boosts(q_text)
    by_slug: dict[str, dict[str, Any]] = {}

    # Keep a wider embedding pool than top_k before final sort.
    for c in scored[: max(top_k * 3, top_k)]:
        item = dict(c)
        item["keyword_boost"] = boosts.get(item["slug"], 0.0)
        item["adjusted_similarity"] = item["similarity"] + item["keyword_boost"]
        by_slug[item["slug"]] = item

    # Force boosted topics into the pool.
    for slug, boost in boosts.items():
        topic = slug_to_topic.get(slug)
        if not topic:
            continue
        if slug in by_slug:
            by_slug[slug]["keyword_boost"] = max(by_slug[slug].get("keyword_boost", 0.0), boost)
            by_slug[slug]["adjusted_similarity"] = by_slug[slug]["similarity"] + by_slug[slug]["keyword_boost"]
        else:
            item = dict(topic)
            item["similarity"] = 0.0
            item["keyword_boost"] = boost
            item["adjusted_similarity"] = boost
            by_slug[slug] = item

    candidates = list(by_slug.values())
    candidates.sort(
        key=lambda x: (
            x.get("adjusted_similarity", x.get("similarity", 0.0)),
            x.get("similarity", 0.0),
        ),
        reverse=True,
    )
    return candidates[: max(1, top_k)]




def deterministic_topic_override(q_text: str) -> tuple[str | None, str | None]:
    """
    Very high-precision classroom-test overrides.

    Returns:
      (slug, reason) or (None, None)

    Used only for obvious cues where the LLM has repeatedly picked
    a wrong nearby topic from the candidate list.
    """
    t = (q_text or "").lower()

    # Highest-priority exact classroom-test overrides.
    # These must run before broader archive/search/database rules.

    if any(x in t for x in [
        "заявка, която се използва за създаване на нови таблици",
        "създаване на нови таблици",
        "добавяне на данни в съществуващи таблици",
        "актуализиране и изтриване на данни",
        "изтриване на данни",
    ]):
        return "access-action-queries", "priority_override: clear Access action query cue"

    if any(x in t for x in [
        "защита на системите за управление на уеб съдържание",
        "защита на системите за управление",
        "защита на cms",
    ]):
        return "cms-security", "priority_override: clear CMS security cue"

    if any(x in t for x in [
        "заявката за търсене се нарича",
        "заявката за търсене",
    ]):
        return "effective-information-search", "priority_override: clear search query cue"

    if any(x in t for x in [
        "не е система за управление на уеб съдържание",
        "система за управление на уеб съдържание",
        "характеристики не се отнася за система за управление",
    ]):
        return "cms-systems", "priority_override: clear CMS systems cue"

    # VoIP / internet voice/video communication
    if any(x in t for x in [
        "пренасянето на глас", "пренасяне на глас", "глас (видео)",
        "глас и видео", "voice over ip", "voip", "интернет телефония",
    ]):
        return "voip-internet-communication", "keyword_override: clear VoIP / internet voice-video communication cue"

    # Raster image properties
    if any(x in t for x in [
        "растерно изображение", "растерни изображения", "растерна графика",
    ]):
        return "raster-image-properties", "keyword_override: clear raster image cue"

    # Peripheral devices
    if any(x in t for x in [
        "периферно устройство", "периферни устройства", "комуникационно",
        "входно устройство", "изходно устройство",
    ]):
        return "peripheral-devices", "keyword_override: clear peripheral device cue"

    # Device drivers
    if any(x in t for x in ["драйвер", "драйвери", "driver"]):
        return "device-drivers", "keyword_override: clear device driver cue"

    # Install / uninstall software
    if any(x in t for x in [
        "инсталирането на софтуер", "инсталиране на софтуер",
        "инсталиране на програми", "деинсталиране", "деинсталиране на програми",
        "setup", ".exe", "изпълним файл", "изпълним", "разширение",
        "списък на инсталираните програми", "инсталираните програми",
        "control panel",
    ]):
        return "install-uninstall-software", "keyword_override: clear software install/uninstall cue"

    # Archive / compression
    if any(x in t for x in [
        "архив", "архивиране", "архивираща", "архивирани", "компресиране",
        "компресирани", "декомпресиране", "zip", "rar", "7z", "саморазархивиращ",
    ]):
        return "archive-compression", "keyword_override: clear archive/compression cue"

    # Social networks
    if any(x in t for x in [
        "instagram", "инстаграм", "linkedin", "линктин", "facebook", "фейсбук",
        "youtube", "ютюб", "tiktok", "тикток", "социална мрежа", "социални мрежи",
    ]):
        return "social-networks", "keyword_override: clear social network cue"

    # Effective search
    if any(x in t for x in [
        "intext:", "intitle:", "filetype:", "site:", "not america",
        "търсене в интернет", "при търсене", "file explorer",
        "оператори за търсене", "заявката за търсене",
    ]):
        return "effective-information-search", "keyword_override: clear search/operator cue"

    # Network topology/devices/protocols/security/cloud
    if any(x in t for x in ["топология", "звезда", "шина", "пръстен"]):
        return "network-topology", "keyword_override: clear network topology cue"

    if any(x in t for x in [
        "маршрутизатор", "рутер", "точка за достъп", "безжичните устройства",
        "окабелена част", "суич", "комутатор",
    ]):
        return "network-devices", "keyword_override: clear network device cue"

    if any(x in t for x in [
        "протоколът tcp", "tcp", "протоколът pop", " pop", "ftp",
        "пренос на файлове", "трансфер на файлове", "име на ip адрес",
        "ip адрес", "домейн",
    ]):
        return "network-protocols", "keyword_override: clear network protocol cue"

    if any(x in t for x in [
        "сигурността на информацията", "целостта на данните",
        "целостта", "поверителност", "достъпност",
    ]):
        return "information-security", "keyword_override: clear information security cue"

    if any(x in t for x in ["облачните технологии", "облачни технологии", "облак"]):
        return "cloud-technologies", "keyword_override: clear cloud technologies cue"

    if any(x in t for x in ["биометрич", "биометрична идентификация", "поведенческите"]):
        return "biometric-identification", "keyword_override: clear biometric identification cue"

    if any(x in t for x in [
        "device manager",
        "диспечера за управление на хардуера",
        "диспечер за управление на хардуера",
        "управление на хардуера",
    ]):
        return "device-manager", "keyword_override: clear Device Manager cue"

    # Access / information systems
    if any(x in t for x in ["анализ на реализуемостта", "реализуемостта на ис"]):
        return "information-system-feasibility", "keyword_override: clear IS feasibility cue"

    if any(x in t for x in ["таблица в програмата ms access", "панела tables"]):
        return "access-tables", "keyword_override: clear Access tables cue"

    if any(x in t for x in ["формуляр", "формуляри", "улесняват въвеждането на данните"]):
        return "access-forms", "keyword_override: clear Access forms cue"

    if any(x in t for x in ["макросите в бд", "макроси в бд", "макросите"]):
        return "access-macros", "keyword_override: clear Access macros cue"

    if any(x in t for x in ["заявката за избиране", "заявка за избиране"]):
        return "access-select-queries", "keyword_override: clear select query cue"

    if any(x in t for x in [
        "създаване на нови таблици", "създаване на нови", "добавяне на данни",
        "актуализиране", "изтриване на данни", "заявка, която се използва",
        "заявки за модифициране", "модифициране на бд",
    ]):
        return "access-action-queries", "keyword_override: clear action query cue"

    # Web planning / architecture / responsive / RSS
    if any(x in t for x in [
        "целта на един сайт", "целевата група", "планиране на уеб сайт",
        "дейност, извършвана при планиране",
    ]):
        return "website-planning", "keyword_override: clear website planning cue"

    if any(x in t for x in [
        "информационната архитектура", "основните в един уеб сайт",
        "структурата на уеб сайта", "навигация",
    ]):
        return "website-information-architecture", "keyword_override: clear information architecture cue"

    if any(x in t for x in ["различен размер на екрана", "responsive", "адаптивен дизайн"]):
        return "responsive-design", "keyword_override: clear responsive design cue"

    if any(x in t for x in ["обмен на новини", "rss"]):
        return "rss-feeds", "keyword_override: clear RSS cue"

    # Final classroom-test topics
    if any(x in t for x in ["потребителят общува с компютърната система", "средствата и правилата"]):
        return "user-interface", "keyword_override: clear user interface cue"

    if any(x in t for x in ["проектиране на уеб сайт", "не се отнася към проектиране"]):
        return "website-design-process", "keyword_override: clear website design cue"

    if any(x in t for x in ["не е система за управление на уеб съдържание", "характеристики не се отнася за система за управление"]):
        return "cms-systems", "keyword_override: clear CMS systems cue"

    if any(x in t for x in ["wordpress", "инсталиране на системата за управление"]):
        return "cms-wordpress-installation", "keyword_override: clear WordPress installation cue"

    if any(x in t for x in ["captcha", "публичен ключ за сигурност"]):
        return "captcha", "keyword_override: clear CAPTCHA cue"

    if any(x in t for x in ["улавяне на мрежовия трафик", "прихващане на данни", "sniff"]):
        return "network-sniffing", "keyword_override: clear sniffing cue"

    if any(x in t for x in [
        "защита на системите", "защита на система", "защита на cms",
        "сигурност на cms", "системите за управление на уеб съдържание"
    ]) and any(x in t for x in ["защита", "сигурност"]):
        return "cms-security", "keyword_override: clear CMS security cue"

    if any(x in t for x in ["количество данни", "от сайта към крайните потребители", "bandwidth"]):
        return "web-hosting-bandwidth", "keyword_override: clear hosting bandwidth cue"

    if any(x in t for x in ["части от текст", "изображение, звук", "връзка към друга информация", "хипертекст"]):
        return "hypertext-hyperlinks", "keyword_override: clear hypertext cue"

    if any(x in t for x in ["белбин", "работа в екип"]):
        return "team-roles", "keyword_override: clear team roles cue"

    if any(x in t for x in ["brainstorming", "мозъчна атака"]):
        return "brainstorming", "keyword_override: clear brainstorming cue"

    if any(x in t for x in [
        "ram паметта",
        "ram памет",
        "ram",
        "оперативна памет",
        "настолен компютър",
        "лаптоп",
    ]):
        return "ram-memory", "keyword_override: clear RAM cue"

    if any(x in t for x in [
        "колко вида акаунти",
        "вида акаунти",
        "акаунти се предлагат",
        "акаунти",
        "windows акаунти",
        "ос windows",
    ]):
        return "windows-user-accounts", "keyword_override: clear Windows accounts cue"

    if any(x in t for x in ["windows firewall", "защитна стена"]):
        return "windows-firewall", "keyword_override: clear firewall cue"

    if any(x in t for x in ["safe mode", "безопасен режим"]):
        return "safe-mode", "keyword_override: clear safe mode cue"

    # Alt classroom-test extra topics
    if any(x in t for x in ["грид инфраструктура", "grid инфраструктура"]):
        return "grid-infrastructure", "keyword_override: clear grid infrastructure cue"

    if any(x in t for x in ["услуги не се предлагат в интернет", "услуги в интернет"]):
        return "internet-services", "keyword_override: clear internet services cue"

    if any(x in t for x in ["коаксиалният кабел", "коаксиален кабел"]):
        return "network-transmission-media", "keyword_override: clear transmission media cue"

    if any(x in t for x in ["менюто share", "file explorer"]):
        return "file-explorer-sharing", "keyword_override: clear File Explorer sharing cue"

    if any(x in t for x in ["текстов документ, който се използва като образец", "документ, който се използва като образец"]):
        return "document-templates", "keyword_override: clear document template cue"

    if any(x in t for x in ["диспечера на задачите", "task manager"]):
        return "task-manager", "keyword_override: clear Task Manager cue"

    if any(x in t for x in ["ако не може да се стартира компютърна програма", "не може да се стартира компютърна програма"]):
        return "software-troubleshooting", "keyword_override: clear software troubleshooting cue"

    if any(x in t for x in ["analysis toolpak", "модула analysis toolpak"]):
        return "analysis-toolpak", "keyword_override: clear Analysis ToolPak cue"

    if any(x in t for x in ["етапа на стартиране на една ис", "стартиране на една ис"]):
        return "information-system-development-stages", "keyword_override: clear IS development stage cue"

    if any(x in t for x in ["обекти от бд", "не могат да се въвеждат данни"]):
        return "access-data-entry-objects", "keyword_override: clear Access data-entry object cue"

    if any(x in t for x in ["командни бутони", "панела controls"]):
        return "access-controls-buttons", "keyword_override: clear Access controls/buttons cue"

    if any(x in t for x in ["начална форма", "стартиране на бд"]):
        return "access-startup-form", "keyword_override: clear Access startup form cue"

    # E-learning
    if any(x in t for x in [
        "електронно обучение", "средите за електронно обучение",
        "дистанционно обучение",
    ]):
        return "e-learning-platforms", "keyword_override: clear e-learning cue"

    return None, None



def resolve_alias_or_slug(
    slug: str | None,
    valid_slugs: set[str],
    aliases: dict[str, dict[str, Any]],
) -> tuple[str | None, str | None, str | None]:
    """
    Returns: resolved_slug, action, note
    action:
      - valid
      - alias_map
      - alias_reject
      - alias_skip
      - invalid
      - none
    """
    if not slug or slug == "none":
        return None, "none", None

    if slug in valid_slugs:
        return slug, "valid", None

    alias = aliases.get(slug)
    if not alias:
        return None, "invalid", f"Invalid slug and no alias: {slug!r}"

    action = alias["action"]
    if action == "map" and alias["topic_slug"]:
        return alias["topic_slug"], "alias_map", f"{slug} -> {alias['topic_slug']}"
    if action == "reject":
        return None, "alias_reject", f"Rejected alias: {slug}"
    if action == "skip":
        return None, "alias_skip", f"Skipped broad alias: {slug}"

    return None, "invalid", f"Alias has unsupported action or no target: {slug}"


def classify_with_llm(
    client: OllamaClient,
    *,
    model: str,
    q_text: str,
    candidates: list[dict[str, Any]],
) -> dict[str, Any]:
    prompt = build_llm_prompt(q_text, candidates)
    try:
        result = client.chat(
            messages=[{"role": "user", "content": prompt}],
            model=model,
            system=SYSTEM_PROMPT,
            options={"temperature": 0.0},
        )
    except OllamaError as e:
        return {
            "slug": None,
            "confidence": 0.0,
            "reason": "",
            "error": str(e),
            "raw_response": "",
            "elapsed": 0.0,
        }

    raw = result["content"]
    parsed = parse_json_response(raw)
    if not parsed:
        return {
            "slug": None,
            "confidence": 0.0,
            "reason": "",
            "error": "JSON parse failed",
            "raw_response": raw,
            "elapsed": result["elapsed_seconds"],
        }

    try:
        confidence = float(parsed.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0

    return {
        "slug": parsed.get("slug"),
        "confidence": confidence,
        "reason": parsed.get("reason", ""),
        "error": None,
        "raw_response": raw,
        "elapsed": result["elapsed_seconds"],
    }


def update_assignment(
    conn: sqlite3.Connection,
    *,
    question_id: int,
    topic_id: int,
    section_id: int | None,
    confidence: float,
    margin: float,
    method: str,
    model: str,
    notes: str,
) -> None:
    conn.execute(
        "UPDATE question_topic_assignments SET is_active = 0 WHERE question_id = ? AND is_active = 1",
        (question_id,),
    )
    conn.execute("""
        INSERT INTO question_topic_assignments
        (
            question_id,
            topic_id,
            section_id,
            assignment_type,
            confidence,
            margin,
            method,
            model,
            is_active,
            notes
        )
        VALUES (?, ?, ?, 'primary', ?, ?, ?, ?, 1, ?)
    """, (
        question_id,
        topic_id,
        section_id,
        confidence,
        margin,
        method,
        model,
        notes,
    ))
    conn.execute(
        "UPDATE questions SET topic_id = ? WHERE id = ?",
        (topic_id, question_id),
    )


def main() -> None:
    p = argparse.ArgumentParser(description="Hybrid reclassifier for Bulgarian IT question topics.")
    p.add_argument("--db", type=Path, default=DEFAULT_DB)
    p.add_argument("--host", default=DEFAULT_HOST)
    p.add_argument("--model", default=DEFAULT_CHAT_MODEL)
    p.add_argument("--embedding-model", default=DEFAULT_EMBED_MODEL)
    p.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD)
    p.add_argument("--margin", type=float, default=DEFAULT_MARGIN)
    p.add_argument("--top-k", type=int, default=DEFAULT_TOP_K)
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--source", default=None, help="Filter by source_exam LIKE pattern.")
    p.add_argument("--include-classified", action="store_true",
                   help="Also inspect questions that already have topic_id. Default is only topic_id IS NULL.")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--log-path", type=Path, default=DEFAULT_LOG)
    args = p.parse_args()

    if not args.db.exists():
        print(f"❌ DB не съществува: {args.db}")
        sys.exit(1)

    print("🔁 Topic Reclassifier")
    print(f"   DB:              {args.db}")
    print(f"   Chat model:      {args.model}")
    print(f"   Embedding model: {args.embedding_model}")
    print(f"   Threshold:       {args.threshold}")
    print(f"   Margin:          {args.margin}")
    print(f"   Top-K:           {args.top_k}")
    print(f"   Source filter:   {args.source or '—'}")
    print(f"   Mode:            {'classified + unclassified' if args.include_classified else 'unclassified only'}")
    print(f"   Log:             {args.log_path}")
    if args.dry_run:
        print("   ⚠️  DRY RUN — нищо няма да се запише в DB")

    client = OllamaClient(host=args.host)
    if not client.is_alive():
        print("\n❌ Ollama не работи. Стартирай: ollama serve")
        sys.exit(1)

    conn = sqlite3.connect(str(args.db))
    conn.row_factory = sqlite3.Row

    aliases = fetch_aliases(conn)
    topics = fetch_topics(conn)
    slug_to_topic = {t["slug"]: t for t in topics}
    valid_slugs = set(slug_to_topic)

    print(f"\n📋 Topics: {len(topics)}")
    print("🧠 Embedding topics...")

    t0 = time.monotonic()
    for i, topic in enumerate(topics, 1):
        topic["embedding"] = client.embed(topic["text"], model=args.embedding_model)
        if i % 20 == 0 or i == len(topics):
            print(f"   embedded {i}/{len(topics)}")
    print(f"   done in {time.monotonic() - t0:.1f}s")

    targets = fetch_targets(
        conn,
        include_classified=args.include_classified,
        source=args.source,
        limit=args.limit,
    )
    print(f"\n🔍 Target questions: {len(targets)}")

    stats = {
        "total": len(targets),
        "updated": 0,
        "dry_run_would_update": 0,
        "same_topic": 0,
        "below_threshold": 0,
        "below_margin": 0,
        "no_match": 0,
        "errors": 0,
        "by_slug": {},
    }

    start = time.monotonic()

    for idx, q in enumerate(targets, 1):
        q_text = question_text(conn, q)
        q_emb = client.embed(q_text, model=args.embedding_model)

        scored = []
        for topic in topics:
            sim = cosine(q_emb, topic["embedding"])
            c = dict(topic)
            c["similarity"] = sim
            scored.append(c)

        scored.sort(key=lambda x: x["similarity"], reverse=True)
        candidates = build_candidates_with_boosts(
            scored=scored,
            slug_to_topic=slug_to_topic,
            q_text=q_text,
            top_k=args.top_k,
        )

        top1 = candidates[0].get("adjusted_similarity", candidates[0]["similarity"]) if candidates else 0.0
        top2 = candidates[1].get("adjusted_similarity", candidates[1]["similarity"]) if len(candidates) > 1 else 0.0
        emb_margin = top1 - top2

        llm = classify_with_llm(
            client,
            model=args.model,
            q_text=q_text,
            candidates=candidates,
        )

        resolved_slug, alias_action, alias_note = resolve_alias_or_slug(
            llm["slug"],
            valid_slugs,
            aliases,
        )

        override_slug, override_note = deterministic_topic_override(q_text)
        if override_slug and override_slug in valid_slugs:
            # Even if the LLM picked the same slug, mark it as a deterministic
            # override so clear keyword matches can bypass fragile embedding margins.
            alias_action = "keyword_override"
            alias_note = override_note
            resolved_slug = override_slug
            llm["confidence"] = max(float(llm.get("confidence", 0.0)), 0.90)

        error = llm["error"]
        action = "skip"
        status = ""
        new_topic = slug_to_topic.get(resolved_slug) if resolved_slug else None

        if error:
            stats["errors"] += 1
            action = "error"
            status = f"❌ ERROR: {error}"
        elif not resolved_slug or not new_topic:
            stats["no_match"] += 1
            action = alias_action or "no_match"
            status = f"⏭️  no match ({alias_action})"
        elif llm["confidence"] < args.threshold:
            stats["below_threshold"] += 1
            action = "below_threshold"
            status = f"⚠️  low conf {llm['confidence']:.2f}: {resolved_slug}"
        elif emb_margin < args.margin and alias_action not in ("keyword_override", "priority_override"):
            stats["below_margin"] += 1
            action = "below_margin"
            status = f"⚠️  low margin {emb_margin:.4f}: {resolved_slug}"
        elif q["topic_id"] == new_topic["id"]:
            stats["same_topic"] += 1
            action = "same_topic"
            status = f"= same {resolved_slug} ({llm['confidence']:.2f}, margin {emb_margin:.4f})"
        else:
            action = "update"
            status = f"✓ {resolved_slug} ({llm['confidence']:.2f}, margin {emb_margin:.4f})"
            stats["by_slug"].setdefault(resolved_slug, 0)
            stats["by_slug"][resolved_slug] += 1

            if args.dry_run:
                stats["dry_run_would_update"] += 1
            else:
                update_assignment(
                    conn,
                    question_id=q["id"],
                    topic_id=new_topic["id"],
                    section_id=new_topic["section_slug"] and conn.execute(
                        "SELECT id FROM curriculum_sections WHERE section_slug = ?",
                        (new_topic["section_slug"],),
                    ).fetchone()["id"],
                    confidence=llm["confidence"],
                    margin=emb_margin,
                    method="hybrid_reclassifier_v1",
                    model=f"{args.embedding_model}+{args.model}",
                    notes=f"{llm.get('reason', '')} {alias_note or ''}".strip(),
                )
                stats["updated"] += 1

        log_entry = {
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "question_id": q["id"],
            "source_exam": q["source_exam"],
            "source_number": q["source_number"],
            "current_topic_id": q["topic_id"],
            "current_slug": q["current_slug"],
            "llm_slug": llm["slug"],
            "resolved_slug": resolved_slug,
            "alias_action": alias_action,
            "alias_note": alias_note,
            "confidence": llm["confidence"],
            "embedding_top1": top1,
            "embedding_top2": top2,
            "embedding_margin": emb_margin,
            "candidate_slugs": [
                {
                    "slug": c["slug"],
                    "similarity": round(c["similarity"], 6),
                    "keyword_boost": round(c.get("keyword_boost", 0.0), 6),
                    "adjusted_similarity": round(c.get("adjusted_similarity", c["similarity"]), 6),
                    "section": c["section_slug"],
                }
                for c in candidates
            ],
            "action": action,
            "reason": llm.get("reason", ""),
            "error": error,
            "prompt_preview": (q["prompt"] or "")[:220],
        }
        write_log(args.log_path, log_entry)

        print(f"[{idx:3}/{len(targets)}] Q#{q['id']} {q['source_exam']}#{q['source_number'] or '—'}: {status}")

        if not args.dry_run and idx % 20 == 0:
            conn.commit()

    if not args.dry_run:
        conn.commit()

    elapsed = time.monotonic() - start
    conn.close()

    print("\n" + "=" * 60)
    print("📊 SUMMARY")
    print("=" * 60)
    print(f"   Total:                 {stats['total']}")
    print(f"   Updated:               {stats['updated']}")
    print(f"   Dry-run would update:  {stats['dry_run_would_update']}")
    print(f"   Same topic:            {stats['same_topic']}")
    print(f"   Below threshold:       {stats['below_threshold']}")
    print(f"   Below margin:          {stats['below_margin']}")
    print(f"   No match:              {stats['no_match']}")
    print(f"   Errors:                {stats['errors']}")
    print(f"   Elapsed:               {elapsed:.1f}s")
    print(f"   Log:                   {args.log_path}")

    if stats["by_slug"]:
        print("\n📈 Proposed/updated distribution:")
        for slug, count in sorted(stats["by_slug"].items(), key=lambda x: -x[1]):
            print(f"   {slug}: {count}")

    if args.dry_run:
        print("\n(dry-run: нищо не е записано в DB)")


if __name__ == "__main__":
    main()
