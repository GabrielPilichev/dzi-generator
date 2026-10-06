"""SQL budgets and correctness of the public browsing cache, using only temp DBs."""

from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import ExitStack
from pathlib import Path
import sqlite3
import tempfile
from threading import Barrier, Event, Lock
import unittest
from unittest.mock import patch

from src.subject_browse_cache import BrowseResultCache, database_identity
import src.subject_browse_cache as cache_module
from tests.support import isolated_app, web_app


def traced_get(path):
    """Includes connection setup PRAGMAs and every cold-loader connection."""
    statements = []
    connect = sqlite3.connect

    def traced(*args, **kwargs):
        conn = connect(*args, **kwargs)
        conn.set_trace_callback(statements.append)
        return conn

    with patch.object(sqlite3, "connect", traced):
        response = web_app.app.test_client().get(path)
    return response, statements


class BrowseResultCacheTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="browse-cache-", dir="/tmp")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.db = self.root / "one.db"
        self._create(self.db, "first")
        self.cache = BrowseResultCache(max_entries=2)

    def _create(self, path, value):
        conn = sqlite3.connect(path)
        try:
            conn.execute("CREATE TABLE content (value TEXT)")
            conn.execute("INSERT INTO content VALUES (?)", (value,))
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def _read(conn):
        return [{"nested": [conn.execute("SELECT value FROM content").fetchone()[0]]}]

    def read(self, path=None, key="value"):
        return self.cache.get(path or self.db, key, self._read)

    def test_defensive_copies_database_isolation_and_bounded_generations(self):
        result = self.read()
        result[0]["nested"].append("caller mutation")
        self.assertEqual(self.read(), [{"nested": ["first"]}])
        other = self.root / "two.db"
        self._create(other, "second")
        self.assertEqual(self.read(other), [{"nested": ["second"]}])
        self.assertEqual(self.read(), [{"nested": ["first"]}])
        for key in range(8):
            self.read(key=key)
        self.assertEqual(len(self.cache._values), 2)
        self.read(other)
        self.assertEqual(len(self.cache._values), 1)
        self.assertFalse(self.cache._pending)

    def test_rollback_journal_commit_and_file_replacement_invalidate(self):
        self.read()
        conn = sqlite3.connect(self.db)
        try:
            conn.execute("UPDATE content SET value = 'newer'")
            conn.commit()
        finally:
            conn.close()
        self.assertEqual(self.read(), [{"nested": ["newer"]}])
        replacement = self.root / "replacement.db"
        self._create(replacement, "replaced")
        replacement.replace(self.db)
        self.assertEqual(self.read(), [{"nested": ["replaced"]}])

    def test_wal_commit_checkpoint_and_same_size_reuse_invalidate(self):
        writer = sqlite3.connect(self.db)
        self.addCleanup(writer.close)
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=0")
        self.read()
        main_stamp = database_identity(self.db)[1]
        writer.execute("UPDATE content SET value = 'other'")
        writer.commit()
        self.assertEqual(database_identity(self.db)[1], main_stamp)
        self.assertEqual(self.read(), [{"nested": ["other"]}])
        wal = Path(str(self.db) + "-wal")
        wal_size = wal.stat().st_size
        writer.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        writer.execute("UPDATE content SET value = 'third'")
        writer.commit()
        self.assertEqual(wal.stat().st_size, wal_size)
        self.assertEqual(self.read(), [{"nested": ["third"]}])

    def test_changed_during_compute_and_failed_compute_are_not_cached(self):
        def changing(conn):
            result = self._read(conn)
            writer = sqlite3.connect(self.db)
            try:
                writer.execute("UPDATE content SET value = 'after'")
                writer.commit()
            finally:
                writer.close()
            return result

        self.assertEqual(self.cache.get(self.db, "value", changing), [{"nested": ["first"]}])
        self.assertEqual(self.read(), [{"nested": ["after"]}])
        with self.assertRaisesRegex(ValueError, "failed"):
            self.cache.get(self.db, "failure", lambda conn: (_ for _ in ()).throw(ValueError("failed")))
        self.assertEqual(self.read(key="failure"), [{"nested": ["after"]}])
        self.assertFalse(self.cache._pending)

    def test_cold_loader_cannot_write_content(self):
        with self.assertRaises(sqlite3.OperationalError):
            self.cache.get(self.db, "write", lambda conn:
                           conn.execute("UPDATE content SET value = 'forbidden'"))
        self.assertEqual(self.read(), [{"nested": ["first"]}])

    def test_concurrent_cold_key_has_one_computation(self):
        start = Barrier(6)
        waiting = Event()
        release = Event()
        counter_lock = Lock()
        waiter_count = 0
        compute_count = 0

        class ObservedFuture(Future):
            def result(inner, timeout=None):
                nonlocal waiter_count
                with counter_lock:
                    waiter_count += 1
                    if waiter_count == 4:
                        waiting.set()
                return super().result(timeout)

        def compute(conn):
            nonlocal compute_count
            with counter_lock:
                compute_count += 1
            if not release.wait(10):
                raise AssertionError("test did not release cold computation")
            return self._read(conn)

        def request():
            start.wait(timeout=10)
            return self.cache.get(self.db, "cold", compute)

        with patch.object(cache_module, "Future", ObservedFuture), ThreadPoolExecutor(5) as pool:
            futures = [pool.submit(request) for _ in range(5)]
            start.wait(timeout=10)
            try:
                self.assertTrue(waiting.wait(10), "all other callers must wait on the same computation")
            finally:
                release.set()
            results = [future.result(timeout=10) for future in futures]
        self.assertEqual(compute_count, 1)
        self.assertEqual(results, [[{"nested": ["first"]}]] * 5)
        results[0][0]["nested"].append("one caller")
        self.assertEqual(results[1], [{"nested": ["first"]}])


class PublicBrowseCacheTest(unittest.TestCase):
    def setUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(isolated_app())
        stack.enter_context(patch.object(cache_module, "browse_cache", BrowseResultCache()))

    GENERIC_SECTION = "/subjects/information-technologies/8/grade8-computer-systems"

    def _generic_topic(self):
        """A topic page of GENERIC_SECTION, addressed through the public URL."""
        with web_app.app.test_client() as client:
            html = client.get(self.GENERIC_SECTION).get_data(as_text=True)
        prefix = f'href="{self.GENERIC_SECTION}/'
        self.assertIn(prefix, html)
        return f"{self.GENERIC_SECTION}/" + html.split(prefix, 1)[1].split('"', 1)[0]

    def _assert_read_only(self, statements):
        for sql in statements:
            self.assertIn(sql.strip().split()[0].upper(), {"SELECT", "PRAGMA"}, sql)
            if sql.strip().upper().startswith("PRAGMA"):
                self.assertIn(sql.strip().upper(), {
                    "PRAGMA FOREIGN_KEYS=ON", "PRAGMA DATABASE_LIST", "PRAGMA QUERY_ONLY=ON",
                })

    def test_topic_page_adds_no_query_path_beyond_its_section(self):
        topic = self._generic_topic()
        with patch.object(cache_module, "browse_cache", BrowseResultCache()):
            section, section_sql = traced_get(self.GENERIC_SECTION)
            # First visit of the topic: everything it needs is the section's cached result.
            first, first_sql = traced_get(topic)
            again, again_sql = traced_get(topic)
        self.assertEqual((section.status_code, first.status_code), (200, 200))
        self.assertEqual(first.data, again.data)
        self.assertGreater(len(section_sql), 3)
        self.assertLessEqual(len(first_sql), 3, first_sql)
        self.assertLessEqual(len(again_sql), 3, again_sql)
        self._assert_read_only(section_sql + first_sql + again_sql)

    def test_cold_topic_page_costs_no_more_than_its_section(self):
        topic = self._generic_topic()
        with patch.object(cache_module, "browse_cache", BrowseResultCache()):
            _, section_sql = traced_get(self.GENERIC_SECTION)
        with patch.object(cache_module, "browse_cache", BrowseResultCache()):
            cold, topic_sql = traced_get(topic)
        self.assertEqual(cold.status_code, 200)
        self.assertLessEqual(len(topic_sql), len(section_sql))
        self._assert_read_only(topic_sql)

    def test_public_statement_budgets_and_no_writes(self):
        # Seed-size regression ceilings: retain the original F1 resolver on
        # cold loads; warm reads have only WAL setup, availability, and DB path.
        paths = ("/", "/grade/12", "/section/grade12-dzi-preparation",
                 "/subjects/information-technologies", self.GENERIC_SECTION,
                 self._generic_topic())
        for path in paths:
            with self.subTest(path=path), patch.object(cache_module, "browse_cache", BrowseResultCache()):
                cold, cold_sql = traced_get(path)
                warm, warm_sql = traced_get(path)
                self.assertEqual(cold.status_code, 200)
                self.assertEqual(warm.status_code, 200)
                self.assertEqual(cold.data, warm.data)
                self.assertLessEqual(len(cold_sql), 15000, (path, len(cold_sql)))
                self.assertLessEqual(len(warm_sql), 3, (path, warm_sql))
                for sql in cold_sql + warm_sql:
                    self.assertIn(sql.strip().split()[0].upper(), {"SELECT", "PRAGMA"}, sql)
                    if sql.strip().upper().startswith("PRAGMA"):
                        self.assertIn(sql.strip().upper(), {
                            "PRAGMA FOREIGN_KEYS=ON", "PRAGMA DATABASE_LIST", "PRAGMA QUERY_ONLY=ON",
                        })

    def test_committed_content_edit_is_visible_on_next_public_request(self):
        client = web_app.app.test_client()
        self.assertEqual(client.get("/").status_code, 200)
        self.assertEqual(client.get("/section/grade12-dzi-preparation").status_code, 200)
        with web_app.app.app_context():
            section = web_app.fetch_section("grade12-dzi-preparation")
            question = web_app.fetch_questions_in_section(section["section_id"])[0]
            topic_id, question_id = question["topic_id"], question["question_id"]
        writer = sqlite3.connect(web_app.app.config["DB_PATH"])
        try:
            writer.execute("UPDATE curriculum_topics SET title_bg = ? WHERE id = ?",
                           ("Cache reviewed title", topic_id))
            writer.execute("UPDATE questions SET prompt = ? WHERE id = ?",
                           ("Cache reviewed prompt", question_id))
            writer.commit()
            self.assertIn("Cache reviewed title", client.get("/").get_data(as_text=True))
            self.assertIn("Cache reviewed prompt", client.get("/section/grade12-dzi-preparation").get_data(as_text=True))
        finally:
            writer.close()

    def test_transaction_local_results_are_not_shared(self):
        with web_app.app.app_context():
            browser = web_app.subject_browser()
            original = browser.section("grade12-dzi-preparation")
            conn = web_app.get_db()
            conn.execute("UPDATE curriculum_sections SET title_bg = 'Uncommitted' WHERE id = ?",
                         (original["section_id"],))
            self.assertEqual(browser.section("grade12-dzi-preparation")["section_title"], "Uncommitted")
            conn.rollback()
            self.assertEqual(browser.section("grade12-dzi-preparation"), original)


if __name__ == "__main__":
    unittest.main()
