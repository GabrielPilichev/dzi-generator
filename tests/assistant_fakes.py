"""Scripted stand-ins for the Question Bank Assistant tests (no network, no real database).

``FakeClient`` mimics ``client.beta.messages.create(**kwargs)``: it returns scripted responses
in order and records a deep copy of every request. The builders return ``SimpleNamespace``
objects shaped like the SDK's content blocks and messages. ``build_fixture_db`` writes a tiny
SQLite database with only the tables the assistant tools read; it never touches
``data/questions.db``.
"""

import copy
import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace


class FakeMessages:
    def __init__(self, client):
        self._client = client

    def create(self, **kwargs):
        self._client.calls.append(copy.deepcopy(kwargs))
        if not self._client.script:
            raise AssertionError(
                f"FakeClient script exhausted: call #{len(self._client.calls)} had no scripted "
                "response (the code under test made more model calls than the test expected)"
            )
        return self._client.script.pop(0)


class FakeClient:
    def __init__(self, responses=()):
        self.script = list(responses)
        self.calls = []
        self.beta = SimpleNamespace(messages=FakeMessages(self))

    @property
    def agent_calls(self):
        """Requests that offered tools (the agent loop), as opposed to the router call."""
        return [call for call in self.calls if "tools" in call]

    @property
    def router_calls(self):
        return [call for call in self.calls if "tools" not in call]


def text_block(text):
    return SimpleNamespace(type="text", text=text, citations=None)


def tool_use_block(id, name, input):
    return SimpleNamespace(type="tool_use", id=id, name=name, input=input)


def thinking_block(thinking="Considering the request.", signature="sig-abc123"):
    return SimpleNamespace(type="thinking", thinking=thinking, signature=signature)


def response(content, stop_reason, stop_details=None, input_tokens=11, output_tokens=7):
    return SimpleNamespace(
        id="msg_fake",
        type="message",
        role="assistant",
        model="claude-opus-5-5",
        content=list(content),
        stop_reason=stop_reason,
        stop_details=stop_details,
        usage=SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens),
    )


def router_response(skill, confidence, reason):
    payload = json.dumps({"skill": skill, "confidence": confidence, "reason": reason})
    return response([text_block(payload)], "end_turn")


def file_sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


# --- fixture database -------------------------------------------------------------------

SCHEMA = """
CREATE TABLE curriculum_sections (
    id INTEGER PRIMARY KEY, section_slug TEXT NOT NULL, title_bg TEXT NOT NULL, class INTEGER
);
CREATE TABLE curriculum_topics (
    id INTEGER PRIMARY KEY, topic_slug TEXT NOT NULL UNIQUE, title_bg TEXT NOT NULL,
    section_id INTEGER REFERENCES curriculum_sections(id)
);
CREATE TABLE questions (
    id INTEGER PRIMARY KEY, prompt TEXT NOT NULL, question_type TEXT NOT NULL,
    topic_id INTEGER REFERENCES curriculum_topics(id), is_ai_generated INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE multiple_choice_options (
    id INTEGER PRIMARY KEY, question_id INTEGER NOT NULL REFERENCES questions(id),
    option_letter TEXT NOT NULL, option_text TEXT NOT NULL, is_correct INTEGER NOT NULL
);
"""

SECTIONS = [
    (1, "hardware-9", "Хардуер", 9),
    (2, "spreadsheets-11", "Електронни таблици", 11),
    (3, "web-12", "Уеб дизайн", 12),
]

# topic id -> (slug, title, section id)
TOPICS = [
    (10, "input-output-devices", "Входно-изходни устройства", 1),   # grade 9, 3 questions
    (11, "operating-systems", "Операционни системи", 1),            # grade 9, 1 question
    (12, "binary-numbers", "Двоична бройна система", 1),            # grade 9, 0 questions
    (20, "excel-formulas", "Формули в Excel", 2),                   # grade 11, 2 questions
    (21, "databases-intro", "Въведение в бази данни", 2),           # grade 11, 0 questions
    (30, "html-basics", "Основи на HTML", 3),                       # grade 12, 0 questions
]

QUESTIONS = [
    # Q1/Q2: near-duplicates in grade 9 (Jaccard 5/7 under any word tokenisation).
    (101, "Кой от следните е входно устройство", "multiple_choice", 10, 0),
    (102, "Кой от следните е изходно устройство", "multiple_choice", 10, 1),
    (103, "Колко бита има един байт", "multiple_choice", 10, 0),
    (104, "Какво прави операционната система", "short_answer", 11, 0),
    # Q105: same text as Q101 but grade 11 -> must never count as similar to Q101.
    (105, "Кой от следните е входно устройство", "multiple_choice", 20, 1),
    (106, "Коя функция в Excel сумира диапазон", "multiple_choice", 20, 0),
    # Q107: no topic at all.
    (107, "Опишете какво е алгоритъм", "short_answer", None, 0),
]

OPTIONS = [
    (1001, 101, "А", "Клавиатура", 1),
    (1002, 101, "Б", "Монитор", 0),
    (1003, 101, "В", "Принтер", 0),
    (1004, 106, "А", "SUM", 1),
    (1005, 106, "Б", "COUNT", 0),
]


def build_fixture_db(path):
    path = Path(path)
    with closing(sqlite3.connect(path)) as conn:
        conn.executescript(SCHEMA)
        conn.executemany("INSERT INTO curriculum_sections VALUES (?, ?, ?, ?)", SECTIONS)
        conn.executemany("INSERT INTO curriculum_topics VALUES (?, ?, ?, ?)", TOPICS)
        conn.executemany("INSERT INTO questions VALUES (?, ?, ?, ?, ?)", QUESTIONS)
        conn.executemany("INSERT INTO multiple_choice_options VALUES (?, ?, ?, ?, ?)", OPTIONS)
        conn.commit()
    return path


FIXED_NOW = "2026-11-24T10:00:00+00:00"


def fixed_now():
    return FIXED_NOW


def read_jsonl(path):
    path = Path(path)
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
