"""Subject browsing resolves ownership in batches (no per-question N+1) with unchanged semantics.

The homepage is a global browse route, so its statement budget is measured on the full CANONICAL
database (IT + Physics + Biology), never on an IT-only one.
"""

import shutil
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import src.subject_browse_cache as cache_module
from src.subject_browse_cache import BrowseResultCache
from src.subject_browsing import SubjectContentBrowser
from src.subject_content import question_ownership, section_ownership, topic_ownership
from tests.support import CANONICAL, IsolatedAppTestCase, build_test_database, web_app

HOMEPAGE_COLD_CEILING = 15000


def traced_get(path):
    statements = []
    connect = sqlite3.connect

    def traced(*args, **kwargs):
        conn = connect(*args, **kwargs)
        conn.set_trace_callback(statements.append)
        return conn

    with patch.object(sqlite3, "connect", traced):
        response = web_app.app.test_client().get(path)
    return response, statements


class HomepageBudgetOnCanonicalDataTest(IsolatedAppTestCase):
    content_profile = CANONICAL

    def setUp(self):
        stack = patch.object(cache_module, "browse_cache", BrowseResultCache())
        stack.start()
        self.addCleanup(stack.stop)

    def test_the_database_under_test_is_the_full_canonical_dataset(self):
        with closing(sqlite3.connect(web_app.app.config["DB_PATH"])) as conn:
            subjects = {row[0] for row in conn.execute("SELECT DISTINCT subject FROM questions")}
            self.assertTrue({"physics", "biology"} <= subjects)
            self.assertGreater(conn.execute("SELECT COUNT(*) FROM questions").fetchone()[0], 1600)

    def test_homepage_cold_path_is_within_the_ceiling_and_read_only(self):
        cold, cold_sql = traced_get("/")
        warm, warm_sql = traced_get("/")
        self.assertEqual((cold.status_code, warm.status_code), (200, 200))
        self.assertLessEqual(len(cold_sql), HOMEPAGE_COLD_CEILING)
        self.assertLessEqual(len(warm_sql), 5)
        self.assertEqual(cold.data, warm.data)
        for sql in cold_sql + warm_sql:
            self.assertIn(sql.strip().split(None, 1)[0].upper(), {"SELECT", "PRAGMA", "WITH"}, sql)

    def test_the_repeated_section_lookup_is_gone(self):
        _, statements = traced_get("/")
        repeated = [sql for sql in statements if "WHERE id = " in sql and "FROM curriculum_sections" in sql]
        self.assertLess(len(repeated), 50)           # was ~7,800 identical lookups

    def test_global_page_still_shows_physics_and_biology_content(self):
        html = web_app.app.test_client().get("/").get_data(as_text=True)
        self.assertIn("/subjects/physics", html)
        self.assertIn("/subjects/biology", html)
        for path in ("/subjects/physics", "/subjects/biology", "/subjects/physics/8", "/subjects/biology/8",
                     "/subjects/information-technologies"):
            self.assertEqual(web_app.app.test_client().get(path).status_code, 200, path)

    def test_warm_cache_does_not_change_the_rendered_pages(self):
        for path in ("/", "/subjects/physics/8", "/subjects/information-technologies"):
            cold, _ = traced_get(path)
            warm, warm_sql = traced_get(path)
            self.assertEqual(cold.data, warm.data, path)
            self.assertLessEqual(len(warm_sql), 5, path)


class BatchedResolutionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        directory = tempfile.TemporaryDirectory(prefix="learnpilot-ownership-")
        cls.addClassCleanup(directory.cleanup)
        cls.root = Path(directory.name)
        cls.db = build_test_database(cls.root / "canonical.db")

    def connect(self, path=None):
        conn = sqlite3.connect(path or self.db)
        conn.row_factory = sqlite3.Row
        self.addCleanup(conn.close)
        return conn

    def counted(self, conn):
        statements = []
        conn.set_trace_callback(statements.append)
        return statements

    def test_many_questions_resolve_in_a_constant_number_of_statements(self):
        conn = self.connect()
        ids = [row[0] for row in conn.execute("SELECT id FROM questions ORDER BY id")]
        statements = self.counted(conn)
        browser = SubjectContentBrowser(conn, "physics")
        browser._prime_questions((qid, None) for qid in ids)
        self.assertLess(len(statements), 15, len(statements))
        before = len(statements)
        for qid in ids:                                 # all served from the instance memo
            browser._question_owner(qid)
        self.assertEqual(len(statements), before)

    def test_grade_counts_and_sections_use_bounded_statements(self):
        conn = self.connect()
        statements = self.counted(conn)
        browser = SubjectContentBrowser(conn, "physics")
        counts = browser.grade_counts()
        self.assertEqual([c["grade"] for c in counts], [8, 9, 10])
        self.assertLess(len(statements), 20, len(statements))

    def assert_same_ownership(self, conn, legacy_it):
        question_ids = [r[0] for r in conn.execute("SELECT id FROM questions")] + [999999]
        topic_ids = [r[0] for r in conn.execute("SELECT id FROM curriculum_topics")] + [999999]
        section_ids = [r[0] for r in conn.execute("SELECT id FROM curriculum_sections")] + [999999]
        for primed in (False, True):
            browser = SubjectContentBrowser(conn, "it", legacy_it=legacy_it)
            if primed:
                browser._prime_topics(topic_ids)
                browser._prime_questions((q, None) for q in question_ids)
            for qid in question_ids:
                self.assertEqual(browser._question_owner(qid), question_ownership(conn, qid), (primed, "q", qid))
            for tid in topic_ids:
                self.assertEqual(browser._topic_owner(tid), topic_ownership(conn, tid), (primed, "t", tid))
            for sid in section_ids:
                self.assertEqual(browser._section_owner(sid), section_ownership(conn, sid), (primed, "s", sid))

    def test_ownership_matches_the_per_id_functions_on_canonical_data(self):
        self.assert_same_ownership(self.connect(), legacy_it=False)
        self.assert_same_ownership(self.connect(), legacy_it=True)

    def test_ownership_matches_for_missing_conflicting_and_unclassified_data(self):
        path = build_test_database(self.root / "damaged.db")
        with closing(sqlite3.connect(path)) as conn:
            conn.execute("PRAGMA foreign_keys = OFF")
            it_questions = [r[0] for r in conn.execute(
                "SELECT id FROM questions WHERE subject IN ('IT','informatika_it') ORDER BY id LIMIT 8")]
            conn.execute("UPDATE questions SET subject = NULL WHERE id = ?", (it_questions[0],))
            conn.execute("UPDATE questions SET subject = 'physics' WHERE id = ?", (it_questions[1],))
            conn.execute("UPDATE questions SET topic_id = NULL WHERE id = ?", (it_questions[2],))
            conn.execute("UPDATE questions SET topic_id = 999999 WHERE id = ?", (it_questions[3],))
            conn.execute("UPDATE questions SET subject = 'no-such-subject' WHERE id = ?", (it_questions[4],))
            topic = conn.execute("SELECT topic_id FROM questions WHERE id = ?", (it_questions[5],)).fetchone()[0]
            physics_section = conn.execute(
                "SELECT cs.id FROM curriculum_sections cs JOIN curricula c ON c.id = cs.curriculum_id "
                "WHERE c.subject = 'physics' LIMIT 1").fetchone()[0]
            conn.execute("INSERT INTO topic_section_assignments (topic_id, section_id, is_primary) VALUES (?, ?, 0)",
                         (topic, physics_section))                               # cross-subject membership
            topics = [r[0] for r in conn.execute("SELECT id FROM curriculum_topics WHERE section_id IS NOT NULL LIMIT 6")]
            conn.execute("UPDATE curriculum_topics SET section_id = NULL WHERE id = ?", (topics[0],))
            conn.execute("UPDATE curriculum_topics SET section_id = 999999 WHERE id = ?", (topics[1],))
            conn.execute("DELETE FROM topic_section_assignments WHERE topic_id = ?", (topics[2],))
            sections = [r[0] for r in conn.execute("SELECT id FROM curriculum_sections ORDER BY id LIMIT 4")]
            conn.execute("UPDATE curriculum_sections SET curriculum_id = NULL WHERE id = ?", (sections[0],))
            conn.execute("UPDATE curriculum_sections SET curriculum_id = 999999 WHERE id = ?", (sections[1],))
            conn.execute("UPDATE curricula SET subject = 'no-such-subject' WHERE id = "
                         "(SELECT curriculum_id FROM curriculum_sections WHERE id = ?)", (sections[2],))
            conn.commit()
            conn.row_factory = sqlite3.Row
            self.assert_same_ownership(conn, legacy_it=False)
            self.assert_same_ownership(conn, legacy_it=True)
            statuses = {question_ownership(conn, q).status for q in it_questions} | \
                       {topic_ownership(conn, t).status for t in topics}
            self.assertTrue({"owned", "conflict", "missing", "unclassified", "unknown"} & statuses >= {"conflict", "missing"})

    def test_legacy_it_questions_without_a_declared_subject_are_still_accepted(self):
        path = build_test_database(self.root / "legacy.db")
        with closing(sqlite3.connect(path)) as conn:
            row = conn.execute("SELECT id, topic_id FROM questions WHERE subject IN ('IT','informatika_it') "
                               "AND topic_id IS NOT NULL ORDER BY id LIMIT 1").fetchone()
            conn.execute("UPDATE questions SET subject = NULL WHERE id = ?", (row[0],))
            conn.commit()
            browser = SubjectContentBrowser(conn, "it", legacy_it=True)
            browser._prime_questions([(row[0], row[1])])
            self.assertTrue(browser.owns_question(row[0], row[1]))
            self.assertFalse(SubjectContentBrowser(conn, "it").owns_question(row[0], row[1]))
            self.assertFalse(SubjectContentBrowser(conn, "physics", legacy_it=True).owns_question(row[0], row[1]))


class NoCrossInstanceLeakTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="learnpilot-ownership-leak-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)

    def test_browsers_on_different_databases_do_not_share_ownership(self):
        one = build_test_database(self.root / "one.db")
        two = build_test_database(self.root / "two.db")
        with closing(sqlite3.connect(two)) as conn:
            conn.execute("PRAGMA foreign_keys = OFF")
            conn.execute("UPDATE curricula SET subject = 'physics', education_type = 'moved' WHERE id = 1")   # IT 8th grade -> Physics
            conn.commit()
        section = sqlite3.connect(one).execute("SELECT id FROM curriculum_sections WHERE curriculum_id = 1 LIMIT 1").fetchone()[0]
        with closing(sqlite3.connect(one)) as first, closing(sqlite3.connect(two)) as second:
            a = SubjectContentBrowser(first, "it")
            b = SubjectContentBrowser(second, "it")
            self.assertTrue(a.owns_section(section))
            self.assertFalse(b.owns_section(section))
            self.assertTrue(a.owns_section(section))      # still independent after the other resolved

    def test_a_new_browser_sees_changes_a_previous_browser_never_saw(self):
        path = build_test_database(self.root / "work.db")
        with closing(sqlite3.connect(path)) as conn:
            section = conn.execute("SELECT id FROM curriculum_sections WHERE curriculum_id = 1 LIMIT 1").fetchone()[0]
            before = SubjectContentBrowser(conn, "it")
            self.assertTrue(before.owns_section(section))
            conn.execute("UPDATE curricula SET subject = 'physics', education_type = 'moved' WHERE id = 1")
            conn.commit()
            self.assertFalse(SubjectContentBrowser(conn, "it").owns_section(section))   # fresh instance: no stale cache
            self.assertTrue(before.owns_section(section))                               # memo is per instance


if __name__ == "__main__":
    unittest.main()
