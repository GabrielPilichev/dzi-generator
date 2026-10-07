"""Generated quiz notes: frontmatter string values are content, never structure.

A teacher edits an assignment title; the note writer stores it in the note's
frontmatter; ``src/sync_vault.py`` later reads that frontmatter. Whatever the
title contains must come back as ONE scalar and must not add keys, end the
frontmatter or turn the note into a curriculum topic.
"""

import sqlite3
import unittest
from contextlib import closing

from src.sync_vault import detect_note_type, parse_frontmatter, sync_vault
from tests.auth_helpers import create_account, set_account_session
from tests.support import IsolatedAppTestCase, web_app
from tests.test_account_login import SAME_ORIGIN
from tests.test_assignment_owner_routes import first_section_with_mc_questions
from web.access import account_principal
from web.services import quiz

# The reviewed attack class: backslash + quote, newlines, injected keys, `---`.
MALICIOUS_TITLE = (
    'Injected\\"\n'
    "type: topic\n"
    "class: [12]\n"
    "aliases: [Injected alias]\n"
    "---\n"
    "Injected curriculum description"
)

REPRESENTATIVE_STRINGS = (
    ("bulgarian", "Контролна работа № 3 — Електронни таблици"),
    ("quote", 'Тест "А" група'),
    ("single quote", "Тест 'А' група"),
    ("backslash", "C:\\Users\\ученик"),
    ("backslash before quote", 'край\\"'),
    ("trailing backslash", "край\\"),
    ("newline", "ред 1\nред 2"),
    ("crlf", "ред 1\r\nред 2"),
    ("carriage return", "ред 1\rред 2"),
    ("tab", "колона\tколона"),
    ("control characters", "a\x01b\x1fc\x7fd\x85e"),
    ("unicode line separators", "a\u2028b\u2029c"),
    ("delimiter", "---"),
    ("delimiter line", "преди\n---\nслед"),
    ("colon", "Тема: формули"),
    ("yaml key value", "type: topic"),
    ("yaml keys on lines", "x\ntype: topic\nclass: [12]\naliases: [a]"),
    ("brackets", "[12, 11]"),
    ("braces", "{a: 1}"),
    ("comment", "# не е коментар"),
    ("list item", "- елемент"),
    ("yaml literals", "null"),
    ("number like", "12"),
    ("empty", ""),
    ("padded", "  интервали  "),
    ("unicode", "Ünïcödé 日本語 😀 ё"),
    ("attack", MALICIOUS_TITLE),
)

ASSIGNMENT_NOTE_KEYS = [
    "title", "type", "section_slug", "class", "question_count", "time_limit_minutes",
    "created_at", "attempts_count", "average_score", "status", "tags",
]


class QuizFrontmatterSerializerTest(unittest.TestCase):
    def test_string_values_round_trip_as_one_scalar(self):
        for label, value in REPRESENTATIVE_STRINGS:
            with self.subTest(label=label):
                text = quiz.quiz_frontmatter({
                    "title": value, "type": "quiz_assignment", "tags": ["quiz", "assignment"],
                }) + "\n\nтяло\n"
                lines = text.split("\n")
                # One line per key: the value never spills into further lines.
                self.assertEqual(lines[0], "---")
                self.assertTrue(lines[1].startswith('title: "') and lines[1].endswith('"'))
                self.assertEqual(lines[2:5], ['type: "quiz_assignment"', "tags: [quiz, assignment]", "---"])
                self.assertEqual(len(text.splitlines()), 7)

                parsed, body = parse_frontmatter(text)
                self.assertEqual(list(parsed), ["title", "type", "tags"])
                self.assertIsInstance(parsed["title"], str)
                self.assertEqual(parsed["title"], value)
                self.assertEqual(parsed["type"], "quiz_assignment")
                self.assertEqual(parsed["tags"], ["quiz", "assignment"])
                self.assertEqual(body, "\nтяло\n")

    def test_every_string_field_is_protected(self):
        text = quiz.quiz_frontmatter({
            "title": MALICIOUS_TITLE,
            "student_name": 'Иван\\"\ntype: topic\n---',
            "section_slug": "x\ntype: topic",
            "created_at": "2026-01-01\n---\ntype: topic",
        }) + "\n"
        parsed, body = parse_frontmatter(text)
        self.assertEqual(parsed, {
            "title": MALICIOUS_TITLE,
            "student_name": 'Иван\\"\ntype: topic\n---',
            "section_slug": "x\ntype: topic",
            "created_at": "2026-01-01\n---\ntype: topic",
        })
        self.assertEqual(body, "")

    def test_list_items_cannot_become_structure(self):
        hostile = 'a, b]\ntype: topic\n---\n"'
        text = quiz.quiz_frontmatter({"tags": ["quiz", "с интервал", hostile, 12], "type": "quiz_attempt"}) + "\n"
        self.assertEqual(len(text.splitlines()), 4)
        parsed, _body = parse_frontmatter(text)
        self.assertEqual(parsed, {"tags": ["quiz", "с интервал", hostile, 12], "type": "quiz_attempt"})

    def test_ordinary_values_keep_their_representation(self):
        self.assertEqual(
            quiz.quiz_frontmatter({
                "title": "Контролна работа — 12 клас",
                "type": "quiz_assignment",
                "class": 12,
                "time_limit_minutes": None,
                "average_score": 87.5,
                "tags": ["quiz", "assignment"],
            }),
            "---\n"
            'title: "Контролна работа — 12 клас"\n'
            'type: "quiz_assignment"\n'
            "class: 12\n"
            "time_limit_minutes: null\n"
            "average_score: 87.5\n"
            "tags: [quiz, assignment]\n"
            "---",
        )

    def test_parser_keeps_reading_hand_written_frontmatter(self):
        text = (
            "---\n"
            'title: "Формули"\n'
            "area: 'spreadsheets'\n"
            'path: "C:\\docs"\n'
            "class: [11, 12]\n"
            'aliases: ["SUMIF", "сума, ако"]\n'
            "level: 3\n"
            "related:\n"
            "  - sumif\n"
            "  - countif\n"
            "---\n"
            "тяло\n"
        )
        parsed, body = parse_frontmatter(text)
        self.assertEqual(parsed, {
            "title": "Формули", "area": "spreadsheets", "path": "C:\\docs", "class": [11, 12],
            "aliases": ["SUMIF", "сума, ако"], "level": 3, "related": ["sumif", "countif"],
        })
        self.assertEqual(body, "тяло\n")


class AssignmentNoteFrontmatterTest(IsolatedAppTestCase):
    """The real edit route, the real note writer and the real vault sync, on temporary state."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        web_app.app.config.update(TESTING=True)
        with closing(web_app.quiz_db()) as conn:
            cls.section_id, _question_ids = first_section_with_mc_questions(conn)

    def setUp(self):
        self.assertTrue(web_app.QUIZ_VAULT_PATH.is_relative_to(self.isolation_root))
        self.assertTrue(web_app.QUIZ_DB_PATH.is_relative_to(self.isolation_root))
        self.teacher = create_account(web_app.QUIZ_DB_PATH, "teacher", display_name="Учител")
        with closing(web_app.quiz_db()) as conn:
            self.assignment_id = quiz.quiz_create_assignment(
                conn, principal=account_principal(self.teacher["id"], "teacher", "Учител"),
                section_id=self.section_id, title_bg="Първоначално заглавие", question_count=1)
            conn.commit()
        self.client = web_app.app.test_client()
        set_account_session(self.client, self.teacher["id"], self.teacher["session_key"])

    def edit_title(self, title):
        return self.client.post(f"/teacher/assignment/{self.assignment_id}/edit",
                                data={"title_bg": title, "time_limit_minutes": "10"},
                                headers=SAME_ORIGIN)

    def stored_title(self):
        with closing(web_app.quiz_db()) as conn:
            return conn.execute("SELECT title_bg FROM quiz_assignments WHERE id = ?",
                                (self.assignment_id,)).fetchone()[0]

    def note_path(self):
        notes = list((web_app.QUIZ_VAULT_PATH / "Generated" / "Quizzes").glob(
            f"*-assignment-{self.assignment_id}-*.md"))
        self.assertEqual(len(notes), 1)
        return notes[0]

    def topic_rows(self):
        with closing(sqlite3.connect(str(web_app.QUIZ_DB_PATH))) as conn:
            return conn.execute(
                "SELECT topic_slug, title_bg, note_path, description FROM curriculum_topics ORDER BY id"
            ).fetchall()

    def synced_note(self, rel_path):
        with closing(sqlite3.connect(str(web_app.QUIZ_DB_PATH))) as conn:
            return conn.execute(
                "SELECT note_type, title, classes, topic_id FROM obsidian_notes WHERE file_path = ?",
                (rel_path,)).fetchone()

    def run_sync(self):
        return sync_vault(web_app.QUIZ_VAULT_PATH, web_app.QUIZ_DB_PATH, do_gc=False)

    def assert_assignment_schema(self, parsed, title):
        self.assertEqual(list(parsed), ASSIGNMENT_NOTE_KEYS)
        self.assertEqual(parsed["title"], title)
        self.assertEqual(parsed["type"], "quiz_assignment")
        self.assertEqual(parsed["question_count"], 1)
        self.assertEqual(parsed["time_limit_minutes"], 10)
        self.assertEqual(parsed["status"], "open")
        self.assertEqual(parsed["tags"], ["quiz", "assignment"])

    def test_malicious_title_stays_one_scalar_in_the_generated_note(self):
        response = self.edit_title(MALICIOUS_TITLE)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.stored_title(), MALICIOUS_TITLE)

        content = self.note_path().read_text(encoding="utf-8")
        lines = content.split("\n")
        closing_index = lines.index("---", 1)
        frontmatter_lines = lines[1:closing_index]
        # The real closing delimiter comes after every key: the injected `---`
        # did not end the frontmatter early.
        self.assertEqual([line.partition(":")[0] for line in frontmatter_lines], ASSIGNMENT_NOTE_KEYS)
        self.assertEqual(sum(line.startswith("title:") for line in frontmatter_lines), 1)
        for injected in ("type: topic", "class: [12]", "aliases: [Injected alias]"):
            self.assertNotIn(injected, frontmatter_lines)

        parsed, body = parse_frontmatter(content)
        self.assert_assignment_schema(parsed, MALICIOUS_TITLE)
        self.assertIsInstance(parsed["title"], str)
        self.assertNotIn("aliases", parsed)
        self.assertNotEqual(parsed["class"], [12])
        self.assertTrue(body.lstrip("\n").startswith("# Injected"))
        self.assertEqual(detect_note_type("Generated/Quizzes/" + self.note_path().name, parsed),
                         "quiz_assignment")

    def test_malicious_title_cannot_create_a_curriculum_topic_through_sync(self):
        self.run_sync()  # baseline: whatever the temporary vault already holds
        topics_before = self.topic_rows()

        self.assertEqual(self.edit_title(MALICIOUS_TITLE).status_code, 302)
        note = self.note_path()
        rel_path = str(note.relative_to(web_app.QUIZ_VAULT_PATH))
        stats = self.run_sync()

        self.assertEqual(stats["topics_created"], 0)
        self.assertEqual(stats["topics_updated"], 0)
        self.assertEqual(stats["topic_classes_added"], 0)
        self.assertEqual(self.topic_rows(), topics_before)
        self.assertFalse([row for row in self.topic_rows()
                          if row[0] == note.stem or row[2] == rel_path])
        # The note is still synced, as the quiz assignment it is.
        note_type, title, _classes, topic_id = self.synced_note(rel_path)
        self.assertEqual(note_type, "quiz_assignment")
        self.assertEqual(title, MALICIOUS_TITLE)
        self.assertIsNone(topic_id)

    def test_ordinary_bulgarian_title_note_is_unchanged(self):
        title = "Контролна работа № 2: Електронни таблици (12. клас)"
        self.assertEqual(self.edit_title(title).status_code, 302)
        self.assertEqual(self.stored_title(), title)

        content = self.note_path().read_text(encoding="utf-8")
        self.assertIn(f'title: "{title}"\n', content)
        self.assertIn('type: "quiz_assignment"\n', content)
        self.assertIn("tags: [quiz, assignment]\n", content)
        self.assertIn(f"\n# {title}\n", content)
        parsed, _body = parse_frontmatter(content)
        self.assert_assignment_schema(parsed, title)

        rel_path = str(self.note_path().relative_to(web_app.QUIZ_VAULT_PATH))
        topics_before = self.topic_rows()
        self.run_sync()
        self.assertEqual(self.topic_rows(), topics_before)
        note_type, synced_title, _classes, topic_id = self.synced_note(rel_path)
        self.assertEqual((note_type, synced_title, topic_id), ("quiz_assignment", title, None))


if __name__ == "__main__":
    unittest.main()
