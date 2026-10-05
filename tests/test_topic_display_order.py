"""Migration 010 and explicit topic order, on in-memory or temporary databases only."""

import sqlite3
import unittest

from src.subject_browsing import SubjectContentBrowser, has_topic_order
from tests.subject_fixtures import (
    add_curriculum, add_question, add_section, add_topic, apply_pending_migrations,
    four_options, set_topic_order,
)
from tests.support import ROOT, IsolatedAppTestCase, web_app
from tests.test_homepage_search import _HTMLProbe

MIGRATION = "010_topic_section_display_order.sql"
MIGRATION_SQL = (ROOT / "web" / "migrations" / MIGRATION).read_text(encoding="utf-8")

# The browsing tables as they were before migration 010 (unused columns omitted,
# topic_section_assignments verbatim).
PRE_010_SCHEMA = """
CREATE TABLE curricula (id INTEGER PRIMARY KEY, grade INTEGER NOT NULL, subject TEXT NOT NULL);
CREATE TABLE curriculum_sections (
    id INTEGER PRIMARY KEY, curriculum_id INTEGER, section_slug TEXT UNIQUE NOT NULL,
    title_bg TEXT NOT NULL, class INTEGER NOT NULL
);
CREATE TABLE curriculum_topics (
    id INTEGER PRIMARY KEY, topic_slug TEXT UNIQUE NOT NULL, title_bg TEXT NOT NULL,
    description TEXT, section_id INTEGER
);
CREATE TABLE topic_section_assignments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    topic_id INTEGER NOT NULL REFERENCES curriculum_topics(id) ON DELETE CASCADE,
    section_id INTEGER NOT NULL REFERENCES curriculum_sections(id) ON DELETE CASCADE,
    relationship_type TEXT NOT NULL DEFAULT 'covers',
    is_primary INTEGER DEFAULT 0,
    confidence REAL DEFAULT 1.0,
    source_type TEXT DEFAULT 'manual_mapping',
    notes TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (topic_id, section_id)
);
CREATE TABLE questions (
    id INTEGER PRIMARY KEY, subject TEXT, topic_id INTEGER, prompt TEXT,
    question_type TEXT, difficulty TEXT, is_ai_generated INTEGER DEFAULT 0, quality_score REAL
);
"""


class Migration010SqlTest(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.addCleanup(self.conn.close)
        self.conn.executescript(PRE_010_SCHEMA)
        self.conn.execute("INSERT INTO curricula (id, grade, subject) VALUES (1, 8, 'physics')")
        self.conn.execute(
            "INSERT INTO curriculum_sections (id, curriculum_id, section_slug, title_bg, class) "
            "VALUES (1, 1, 'physics-g8-s', 'Section', 8)"
        )
        # Titles and ids are deliberately not in the same order.
        for topic_id, title in ((1, "Charlie"), (2, "Alpha"), (3, "Bravo")):
            self.conn.execute(
                "INSERT INTO curriculum_topics (id, topic_slug, title_bg, section_id) "
                "VALUES (?, ?, ?, 1)", (topic_id, f"physics-g8-{title.lower()}", title),
            )
            self.conn.execute(
                "INSERT INTO topic_section_assignments (topic_id, section_id, is_primary, notes) "
                "VALUES (?, 1, 1, ?)", (topic_id, f"note {topic_id}"),
            )
        self.conn.commit()

    def titles(self):
        browser = SubjectContentBrowser(self.conn, "physics")
        return [topic["topic_title"] for topic in browser.topics_in_section(1)]

    def test_migration_is_one_additive_nullable_column(self):
        statements = [line for line in MIGRATION_SQL.splitlines()
                      if line.strip() and not line.strip().startswith("--")]
        self.assertEqual(" ".join(" ".join(statements).split()),
                         "PRAGMA foreign_keys = ON; ALTER TABLE topic_section_assignments "
                         "ADD COLUMN display_order INTEGER NULL;")
        for forbidden in ("DROP", "DELETE", "UPDATE", "CREATE TABLE", "RENAME"):
            self.assertNotIn(forbidden, MIGRATION_SQL.upper())

    def test_column_is_added_and_existing_rows_survive_unordered(self):
        before = [tuple(row) for row in self.conn.execute(
            "SELECT * FROM topic_section_assignments ORDER BY id")]
        self.assertFalse(has_topic_order(self.conn))
        self.conn.executescript(MIGRATION_SQL)
        self.assertTrue(has_topic_order(self.conn))
        column = next(row for row in self.conn.execute(
            "PRAGMA table_info(topic_section_assignments)") if row["name"] == "display_order")
        self.assertEqual((column["type"], column["notnull"], column["dflt_value"]),
                         ("INTEGER", 0, None))
        after = [tuple(row) for row in self.conn.execute(
            "SELECT * FROM topic_section_assignments ORDER BY id")]
        self.assertEqual([row[:-1] for row in after], before)
        self.assertEqual({row[-1] for row in after}, {None})
        self.assertEqual(self.conn.execute("PRAGMA foreign_key_check").fetchall(), [])
        # The UNIQUE (topic_id, section_id) constraint is still enforced.
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO topic_section_assignments (topic_id, section_id) VALUES (1, 1)")

    def test_legacy_order_is_identical_before_and_after_the_migration(self):
        before = self.titles()
        self.assertEqual(before, ["Alpha", "Bravo", "Charlie"])
        self.conn.executescript(MIGRATION_SQL)
        self.assertEqual(self.titles(), before)

    def test_a_database_without_the_column_browses_in_the_legacy_order(self):
        # A development database still awaiting the migration must keep working.
        self.assertFalse(has_topic_order(self.conn))
        self.assertEqual(self.titles(), ["Alpha", "Bravo", "Charlie"])

    def test_explicit_order_applies_once_the_column_exists(self):
        self.conn.executescript(MIGRATION_SQL)
        set_topic_order(self.conn, 1, 1, 1)          # Charlie first
        self.assertEqual(self.titles(), ["Charlie", "Alpha", "Bravo"])


class Migration010RunnerTest(IsolatedAppTestCase):
    """The real runner on a temporary copy of the seed database."""

    def test_runner_adds_the_column_and_keeps_every_assignment(self):
        db_path = web_app.QUIZ_DB_PATH
        legacy_columns = ("id, topic_id, section_id, relationship_type, is_primary, "
                          "confidence, source_type, notes, created_at")
        with sqlite3.connect(db_path) as conn:
            already_migrated = has_topic_order(conn)
            before = conn.execute(
                f"SELECT {legacy_columns} FROM topic_section_assignments ORDER BY id").fetchall()
            counts = {table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                      for table in ("curriculum_topics", "curriculum_sections", "questions")}
        conn.close()
        self.assertGreater(len(before), 500)

        apply_pending_migrations(db_path)
        apply_pending_migrations(db_path)             # recorded, so a second run is a no-op

        with sqlite3.connect(db_path) as conn:
            self.assertTrue(has_topic_order(conn))
            self.assertEqual(conn.execute(
                f"SELECT {legacy_columns} FROM topic_section_assignments ORDER BY id").fetchall(),
                before)
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM schema_migrations WHERE filename = ?", (MIGRATION,)
            ).fetchone()[0], 1)
            if not already_migrated:
                self.assertEqual(conn.execute(
                    "SELECT COUNT(*) FROM topic_section_assignments "
                    "WHERE display_order IS NOT NULL").fetchone()[0], 0)
            for table, count in counts.items():
                self.assertEqual(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], count)
            self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        conn.close()


class TopicOrderBrowsingTest(IsolatedAppTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        apply_pending_migrations(web_app.QUIZ_DB_PATH)
        conn = web_app.quiz_db()
        try:
            curriculum = add_curriculum(conn, "physics", 8, "topic-order")
            cls.ordered = add_section(conn, curriculum, "physics-g8-ordered",
                                      "Synthetic ordered section", grade=8, display_order=1)
            cls.legacy = add_section(conn, curriculum, "physics-g8-legacy",
                                     "Synthetic legacy section", grade=8, display_order=2)
            # (title, slug suffix, explicit order). Title order, slug order and the
            # explicit order all differ.
            cls.topics = {}
            for number, (title, suffix, order) in enumerate((
                ("Alpha", "t5", 3), ("Bravo", "t4", None), ("Charlie", "t3", 1),
                ("Delta", "t2", None), ("Echo", "t1", 2),
            ), 1):
                topic_id = add_topic(conn, cls.ordered, f"physics-g8-{suffix}",
                                     f"Synthetic {title}", grade=8)
                set_topic_order(conn, topic_id, cls.ordered, order)
                add_question(conn, topic_id, "physics", number, source="topic-order",
                             prompt=f"Question {title}?", options=four_options())
                cls.topics[title] = topic_id
            for number, (title, suffix) in enumerate(
                    (("Golf", "u1"), ("Foxtrot", "u3"), ("Hotel", "u2")), 10):
                topic_id = add_topic(conn, cls.legacy, f"physics-g8-{suffix}",
                                     f"Synthetic {title}", grade=8)
                add_question(conn, topic_id, "physics", number, source="topic-order",
                             prompt=f"Question {title}?", options=four_options())
            conn.commit()
        finally:
            conn.close()

    def browser(self, subject="physics"):
        conn = web_app.quiz_db()
        self.addCleanup(conn.close)
        return SubjectContentBrowser(conn, subject, legacy_it=subject == "it")

    @staticmethod
    def names(rows, key="topic_title"):
        return [row[key].replace("Synthetic ", "").replace("Question ", "").rstrip("?")
                for row in rows]

    def test_explicit_order_comes_first_then_the_legacy_title_order(self):
        topics = self.browser().topics_in_section(self.ordered)
        self.assertEqual(self.names(topics), ["Charlie", "Echo", "Alpha", "Bravo", "Delta"])

    def test_mixed_order_is_deterministic(self):
        first = self.browser().topics_in_section(self.ordered)
        for _ in range(3):
            self.assertEqual(self.browser().topics_in_section(self.ordered), first)

    def test_equal_explicit_orders_fall_back_to_title(self):
        conn = web_app.quiz_db()
        try:
            for title in ("Alpha", "Charlie", "Echo"):
                set_topic_order(conn, self.topics[title], self.ordered, 7)
            # Uncommitted: only this connection sees the tie.
            topics = SubjectContentBrowser(conn, "physics").topics_in_section(self.ordered)
            self.assertEqual(self.names(topics), ["Alpha", "Charlie", "Echo", "Bravo", "Delta"])
        finally:
            conn.rollback()
            conn.close()

    def test_questions_follow_the_topic_order(self):
        questions = self.browser().questions_in_section(self.ordered)
        # Ordered topics first; the rest in the legacy topic-slug order (t2 before t4).
        self.assertEqual(self.names(questions, "prompt"),
                         ["Charlie", "Echo", "Alpha", "Delta", "Bravo"])

    def test_a_section_without_explicit_order_keeps_the_legacy_order(self):
        browser = self.browser()
        self.assertEqual(self.names(browser.topics_in_section(self.legacy)),
                         ["Foxtrot", "Golf", "Hotel"])                    # by title
        self.assertEqual(self.names(browser.questions_in_section(self.legacy), "prompt"),
                         ["Golf", "Hotel", "Foxtrot"])                    # by topic slug

    def test_section_and_topic_pages_use_the_same_order(self):
        client = web_app.app.test_client()
        html = client.get("/subjects/physics/8/ordered").get_data(as_text=True)
        page = _HTMLProbe(html)
        links = [attrs["href"].rsplit("/", 1)[1] for _, attrs in page.elements("a")
                 if "stretched-link" in (attrs.get("class") or "")]
        self.assertEqual(links, ["t3", "t1", "t5", "t4", "t2"])
        positions = [html.index(f"Question {title}?")
                     for title in ("Charlie", "Echo", "Alpha", "Delta", "Bravo")]
        self.assertEqual(positions, sorted(positions))
        self.assertEqual(client.get("/subjects/physics/8/ordered/t3").status_code, 200)

    def test_existing_it_content_keeps_its_order(self):
        conn = web_app.quiz_db()
        self.addCleanup(conn.close)
        self.assertEqual(conn.execute(
            """
            SELECT COUNT(*) FROM topic_section_assignments tsa
            JOIN curriculum_sections cs ON cs.id = tsa.section_id
            JOIN curricula c ON c.id = cs.curriculum_id
            WHERE c.subject = 'Информационни технологии' AND tsa.display_order IS NOT NULL
            """).fetchone()[0], 0)
        browser = SubjectContentBrowser(conn, "it", legacy_it=True)
        for slug in ("grade8-computer-systems", "grade11-m1-summary", "grade12-dzi-preparation"):
            with self.subTest(section=slug):
                section_id = conn.execute(
                    "SELECT id FROM curriculum_sections WHERE section_slug = ?", (slug,)
                ).fetchone()[0]
                topics = [row["topic_id"] for row in browser.topics_in_section(section_id)]
                legacy_topics = [row[0] for row in conn.execute(
                    """
                    SELECT t.id FROM topic_section_assignments tsa
                    JOIN curriculum_topics t ON t.id = tsa.topic_id
                    WHERE tsa.section_id = ? ORDER BY t.title_bg, t.id
                    """, (section_id,))]
                self.assertTrue(topics)
                self.assertEqual(topics, [tid for tid in legacy_topics if tid in set(topics)])

                questions = [row["question_id"] for row in browser.questions_in_section(section_id)]
                legacy_questions = [row[0] for row in conn.execute(
                    """
                    SELECT DISTINCT q.id FROM topic_section_assignments tsa
                    JOIN curriculum_topics t ON t.id = tsa.topic_id
                    JOIN questions q ON q.topic_id = t.id
                    WHERE tsa.section_id = ?
                      AND (q.is_ai_generated = 0 OR q.quality_score >= 1.0)
                    ORDER BY t.topic_slug, q.id
                    """, (section_id,))]
                self.assertTrue(questions)
                self.assertEqual(questions,
                                 [qid for qid in legacy_questions if qid in set(questions)])


if __name__ == "__main__":
    unittest.main()
