"""Cross-request subject availability and the compact home overview, on temp DBs only.

Availability is shared through the browse cache only by requests that have opened
no connection; the home page caches a summary instead of every section. Both must
keep the cache's invalidation, concurrency and ownership guarantees.
"""

from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import ExitStack, closing
from copy import deepcopy
from pathlib import Path
import sqlite3
from threading import Barrier, Event, Lock
import unittest
from unittest.mock import Mock, patch

from src.subject_browse_cache import BrowseResultCache, database_identity
import src.subject_browse_cache as cache_module
from src.subject_browsing import SubjectContentBrowser, subject_content_grades
from src.subject_content import IT
from web.blueprints.browsing import DZI_SECTION_SLUG
from web.request_db import get_request_db, request_db_if_open
from web.services import browsing as browsing_service
from web.services import subject_catalog as catalog_module
from web.services.subject_catalog import CONTENT_GRADES_KEY, subject_catalog
from tests.auth_helpers import login_as
from tests.support import IT_ONLY, build_test_database, isolated_app, web_app

# Physics owns sections only where a section's class equals its curriculum's grade.
HIDE_PHYSICS = "UPDATE curricula SET grade = grade + 100 WHERE subject = 'physics'"
SHOW_PHYSICS = "UPDATE curricula SET grade = grade - 100 WHERE subject = 'physics'"
FAILED = "Subject availability lookup failed"
IT_GRADES = [8, 9, 10, 11, 12]


def current_statuses():
    return {subject["id"]: subject["status"] for subject in subject_catalog()}


def statuses():
    """What a new request that has opened no connection sees."""
    with web_app.app.app_context():
        return current_statuses()


def traced_get(path):
    """The response, every SQL statement and the number of connections opened."""
    statements, connections = [], []
    connect = sqlite3.connect

    def traced(*args, **kwargs):
        conn = connect(*args, **kwargs)
        connections.append(conn)
        conn.set_trace_callback(statements.append)
        return conn

    with patch.object(sqlite3, "connect", traced):
        response = web_app.app.test_client().get(path)
    return response, statements, len(connections)


class _CacheTestCase(unittest.TestCase):
    def setUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        self.root = stack.enter_context(isolated_app())
        self.cache = BrowseResultCache()
        stack.enter_context(patch.object(cache_module, "browse_cache", self.cache))
        self.db = Path(web_app.app.config["DB_PATH"])

    def commit(self, sql, parameters=()):
        with closing(sqlite3.connect(self.db)) as conn:
            conn.execute(sql, parameters)
            conn.commit()


class SubjectAvailabilityCacheTest(_CacheTestCase):
    def test_anonymous_requests_share_one_read_and_open_no_connection(self):
        with web_app.app.app_context():
            preparing = next(s["slug"] for s in subject_catalog() if s["status"] == "in_preparation")
        self.cache._values.clear()
        reads = Mock(wraps=subject_content_grades)
        with patch.object(catalog_module, "subject_content_grades", reads):
            for path in ("/login", f"/subjects/{preparing}"):
                with self.subTest(path=path):
                    first, _, _ = traced_get(path)
                    again, statements, connections = traced_get(path)
                    self.assertEqual((first.status_code, again.status_code), (200, 200))
                    self.assertEqual(first.data, again.data)
                    self.assertEqual((statements, connections), ([], 0))
        self.assertEqual(reads.call_count, 1)

    def test_committed_change_is_read_by_the_next_request(self):
        client = web_app.app.test_client()
        self.assertEqual(statuses()["physics"], "active")
        self.assertNotIn("В подготовка", client.get("/subjects/physics").get_data(as_text=True))
        self.commit(HIDE_PHYSICS)
        self.assertEqual(statuses()["physics"], "in_preparation")
        self.assertIn("В подготовка", client.get("/subjects/physics").get_data(as_text=True))
        self.commit(SHOW_PHYSICS)
        self.assertEqual(statuses()["physics"], "active")

    def test_wal_commit_and_checkpoint_are_read_by_the_next_request(self):
        writer = sqlite3.connect(self.db)
        self.addCleanup(writer.close)
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=0")
        self.assertEqual(statuses()["physics"], "active")
        main_stamp = database_identity(self.db)[1]
        writer.execute(HIDE_PHYSICS)
        self.assertEqual(statuses()["physics"], "active")  # uncommitted in another connection
        writer.commit()
        self.assertEqual(database_identity(self.db)[1], main_stamp)  # only the WAL changed
        self.assertEqual(statuses()["physics"], "in_preparation")
        writer.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        self.assertEqual(statuses()["physics"], "in_preparation")
        writer.execute(SHOW_PHYSICS)
        writer.commit()
        self.assertEqual(statuses()["physics"], "active")

    def test_replaced_or_switched_database_file_is_read_again(self):
        self.assertEqual(statuses()["physics"], "active")
        other = build_test_database(self.root / "it-only.db", IT_ONLY)
        with patch.dict(web_app.app.config, {"DB_PATH": str(other)}):
            self.assertEqual(statuses()["physics"], "in_preparation")
        self.assertEqual(statuses()["physics"], "active")
        other.replace(self.db)
        self.assertEqual(statuses()["physics"], "in_preparation")
        self.assertEqual(statuses()["it"], "active")

    def test_a_request_connection_is_queried_directly_and_shares_nothing(self):
        with web_app.app.app_context():
            get_request_db()
            self.assertEqual(current_statuses()["physics"], "active")
        self.assertFalse(self.cache._values)

        with web_app.app.app_context():
            conn = get_request_db()
            conn.execute(HIDE_PHYSICS)
            self.assertTrue(conn.in_transaction)
            self.assertEqual(current_statuses()["physics"], "in_preparation")
            self.assertFalse(self.cache._values)
            # Another request reads, and shares, only what is committed.
            self.assertEqual(statuses()["physics"], "active")
            conn.rollback()
        self.assertEqual(self.cache._values[CONTENT_GRADES_KEY]["physics"], frozenset({8, 9, 10}))
        self.assertEqual(statuses()["physics"], "active")

    def test_change_during_the_read_is_not_published(self):
        def changing(conn):
            result = subject_content_grades(conn)
            self.commit(HIDE_PHYSICS)
            return result

        with patch.object(catalog_module, "subject_content_grades", changing):
            self.assertEqual(statuses()["physics"], "active")
        self.assertNotIn(CONTENT_GRADES_KEY, self.cache._values)
        self.assertEqual(statuses()["physics"], "in_preparation")

    def test_failed_read_degrades_is_not_cached_and_recovers(self):
        boom = sqlite3.OperationalError("simulated availability failure")
        for _ in range(2):
            with patch.object(catalog_module, "subject_content_grades", side_effect=boom) as reads, \
                    self.assertLogs(web_app.app.logger, level="WARNING") as logs:
                self.assertEqual(set(statuses().values()), {"in_preparation"})
            self.assertEqual(reads.call_count, 1)
            self.assertTrue(any(FAILED in line for line in logs.output))
            self.assertFalse(self.cache._values)
            self.assertFalse(self.cache._pending)
        self.assertEqual(statuses()["it"], "active")

    def test_requests_waiting_on_a_failed_read_degrade_too(self):
        waiting, release = Event(), Event()

        class ObservedFuture(Future):
            def result(inner, timeout=None):
                waiting.set()
                return super().result(timeout)

        def failing(conn):
            if not release.wait(10):
                raise AssertionError("test did not release the failing read")
            raise sqlite3.OperationalError("simulated availability failure")

        with patch.object(catalog_module, "subject_content_grades", failing), \
                patch.object(cache_module, "Future", ObservedFuture), \
                self.assertLogs(web_app.app.logger, level="WARNING") as logs, \
                ThreadPoolExecutor(2) as pool:
            futures = [pool.submit(statuses) for _ in range(2)]
            try:
                self.assertTrue(waiting.wait(10), "the second request must wait on the first read")
            finally:
                release.set()
            results = [future.result(timeout=10) for future in futures]
        for result in results:
            self.assertEqual(set(result.values()), {"in_preparation"})
        self.assertEqual(sum(FAILED in line for line in logs.output), 2)
        self.assertFalse(self.cache._values)
        self.assertFalse(self.cache._pending)
        self.assertEqual(statuses()["it"], "active")

    def test_concurrent_cold_requests_read_once_and_own_their_catalogs(self):
        start = Barrier(4)
        waiting, release = Event(), Event()
        counter_lock = Lock()
        waiter_count = 0
        reads = Mock(wraps=subject_content_grades)

        class ObservedFuture(Future):
            def result(inner, timeout=None):
                nonlocal waiter_count
                with counter_lock:
                    waiter_count += 1
                    if waiter_count == 3:
                        waiting.set()
                return super().result(timeout)

        def slow(conn):
            if not release.wait(10):
                raise AssertionError("test did not release the cold read")
            return reads(conn)

        def request():
            start.wait(timeout=10)
            with web_app.app.app_context():
                return subject_catalog()

        with patch.object(catalog_module, "subject_content_grades", slow), \
                patch.object(cache_module, "Future", ObservedFuture), ThreadPoolExecutor(4) as pool:
            futures = [pool.submit(request) for _ in range(4)]
            try:
                self.assertTrue(waiting.wait(10), "all other requests must wait on the same read")
            finally:
                release.set()
            catalogs = [future.result(timeout=10) for future in futures]
        self.assertEqual(reads.call_count, 1)
        self.assertEqual([catalog[0]["content_grades"] for catalog in catalogs], [IT_GRADES] * 4)
        catalogs[0][0]["content_grades"].append(99)
        self.assertEqual(catalogs[1][0]["content_grades"], IT_GRADES)
        self.assertFalse(self.cache._pending)

    def test_callers_cannot_change_what_later_requests_read(self):
        with web_app.app.app_context():
            catalog_module._content_grades()
            grades = catalog_module._content_grades()  # a warm read
            grades["it"] = frozenset()
            del grades["physics"]
            entry = subject_catalog()[0]
            self.assertEqual((entry["id"], entry["status"]), ("it", "active"))
            entry["status"] = "in_preparation"
            entry["content_grades"].clear()
            entry["grade_paths"].clear()
        with web_app.app.app_context():
            entry = subject_catalog()[0]
            self.assertEqual((entry["status"], entry["content_grades"]), ("active", IT_GRADES))
            self.assertTrue(entry["grade_paths"])
            self.assertEqual(current_statuses()["physics"], "active")

    def test_memory_and_missing_databases_use_the_request_connection(self):
        for path in (":memory:", self.root / "missing.db"):
            with self.subTest(path=path), patch.dict(web_app.app.config, {"DB_PATH": str(path)}), \
                    patch.object(self.cache, "get", side_effect=AssertionError("shared")), \
                    self.assertLogs(web_app.app.logger, level="WARNING") as logs:
                with web_app.app.app_context():
                    self.assertEqual(set(current_statuses().values()), {"in_preparation"})
                    self.assertIsNotNone(request_db_if_open())
                self.assertTrue(any(FAILED in line for line in logs.output))


class SubjectAvailabilityProductionShapesTest(_CacheTestCase):
    """Production opens one connection per request and keeps no writer open."""

    def enable_wal(self):
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute("PRAGMA journal_mode=WAL").fetchone()[0], "wal")

    def test_wal_commits_with_the_writer_closed_are_read_by_the_next_request(self):
        self.enable_wal()
        self.assertEqual(statuses()["physics"], "active")
        self.assertIn(CONTENT_GRADES_KEY, self.cache._values)
        for sql, expected in ((HIDE_PHYSICS, "in_preparation"), (SHOW_PHYSICS, "active")):
            with self.subTest(expected=expected):
                self.commit(sql)  # opens, commits and closes its own connection
                self.assertEqual(statuses()["physics"], expected)
                self.assertEqual(statuses()["physics"], expected)  # and again, warm
                self.assertEqual(self.cache._values[CONTENT_GRADES_KEY]["physics"] == frozenset(),
                                 expected == "in_preparation")

    @unittest.skipUnless(hasattr(sqlite3.Connection, "setconfig"), "needs Python 3.12 setconfig")
    def test_leftover_wal_frames_are_read_and_never_published_under_a_stale_identity(self):
        self.enable_wal()
        self.assertEqual(statuses()["physics"], "active")
        writer = sqlite3.connect(self.db)
        writer.setconfig(sqlite3.SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE, True)
        writer.execute(HIDE_PHYSICS)
        writer.commit()
        writer.close()
        wal = Path(str(self.db) + "-wal")
        self.assertGreater(wal.stat().st_size, 0, "committed frames must remain in the WAL")
        before = database_identity(self.db)

        self.assertEqual(statuses()["physics"], "in_preparation")  # frames are visible to a new reader
        # The reader may checkpoint the WAL as it closes, changing the identity it was
        # computed under; whatever is cached must still be the committed content.
        published = self.cache._values.get(CONTENT_GRADES_KEY)
        if published is not None:
            self.assertEqual(published["physics"], frozenset())
        self.assertNotEqual(database_identity(self.db), before)

        self.assertEqual(statuses()["physics"], "in_preparation")
        self.assertEqual(self.cache._values[CONTENT_GRADES_KEY]["physics"], frozenset())
        self.assertEqual(statuses()["physics"], "in_preparation")

    def test_commit_during_a_pending_cold_read_reaches_later_requests(self):
        self.enable_wal()
        started, release = Event(), Event()
        computations = []

        def slow_first(conn):
            result = subject_content_grades(conn)  # the owner has read the old content
            computations.append(result["physics"])
            if len(computations) == 1:
                started.set()
                if not release.wait(10):
                    raise AssertionError("test did not release the first read")
            return result

        with patch.object(catalog_module, "subject_content_grades", slow_first), \
                ThreadPoolExecutor(2) as pool:
            owner = pool.submit(statuses)
            self.assertTrue(started.wait(10), "the first read must be in flight")
            self.commit(HIDE_PHYSICS)
            try:
                late = pool.submit(statuses).result(timeout=10)  # arrives after the commit
            finally:
                release.set()
            owner_result = owner.result(timeout=10)
        self.assertEqual(owner_result["physics"], "active")  # the read began before the commit
        self.assertEqual(late["physics"], "in_preparation")
        self.assertEqual(len(computations), 2)
        self.assertEqual(statuses()["physics"], "in_preparation")
        self.assertEqual(self.cache._values[CONTENT_GRADES_KEY]["physics"], frozenset())
        self.assertFalse(self.cache._pending)


class SignedInAvailabilityTest(_CacheTestCase):
    def test_signed_in_requests_query_their_own_connection_and_publish_nothing(self):
        statuses()  # an anonymous request warms the shared entry
        self.assertIn(CONTENT_GRADES_KEY, self.cache._values)
        for role in ("student", "teacher", "admin"):
            with self.subTest(role=role):
                self.cache._values.clear()
                client = web_app.app.test_client()
                login_as(client, self.db, role)
                reads = Mock(wraps=subject_content_grades)
                with patch.object(catalog_module, "subject_content_grades", reads):
                    for path in ("/", "/subjects/physics"):
                        self.assertEqual(client.get(path).status_code, 200)
                self.assertEqual(reads.call_count, 2)  # one direct read per request
                self.assertNotIn(CONTENT_GRADES_KEY, self.cache._values)

    def test_signed_in_requests_see_commits_while_the_shared_entry_is_warm(self):
        client = web_app.app.test_client()
        login_as(client, self.db, "teacher")
        statuses()
        self.assertEqual(self.cache._values[CONTENT_GRADES_KEY]["physics"], frozenset({8, 9, 10}))
        self.commit(HIDE_PHYSICS)
        ACTIVE = 'subject-pill">Активен'  # the page's own status pill, not the navigation's
        self.assertNotIn(ACTIVE, client.get("/subjects/physics").get_data(as_text=True))
        # The signed-in request neither reads nor refreshes the shared entry: it is still
        # the old value, and only the next anonymous lookup notices the new identity.
        self.assertEqual(self.cache._values[CONTENT_GRADES_KEY]["physics"], frozenset({8, 9, 10}))
        self.assertEqual(statuses()["physics"], "in_preparation")
        self.assertEqual(self.cache._values[CONTENT_GRADES_KEY]["physics"], frozenset())
        self.commit(SHOW_PHYSICS)
        self.assertIn(ACTIVE, client.get("/subjects/physics").get_data(as_text=True))

    def test_an_open_request_connection_sees_its_own_uncommitted_change(self):
        statuses()
        with web_app.app.app_context():
            conn = get_request_db()
            conn.execute(HIDE_PHYSICS)
            self.assertEqual(current_statuses()["physics"], "in_preparation")
            self.assertEqual(statuses()["physics"], "active")  # others still see committed data
            conn.rollback()


class HomeOverviewTest(_CacheTestCase):
    def overview(self, **kwargs):
        with web_app.app.app_context():
            return browsing_service.subject_browser(IT).overview(IT_GRADES, **kwargs)

    def test_overview_has_the_totals_and_only_the_requested_section(self):
        with closing(sqlite3.connect(self.db)) as conn:
            conn.row_factory = sqlite3.Row
            direct = SubjectContentBrowser(conn, IT, legacy_it=True)
            sections = [s for grade in IT_GRADES for s in direct.sections_for_grade(grade)]
            totals = {
                "question_count": sum(row["question_count"] for row in direct.grade_counts()),
                "search_content": direct.search_titles(sections),
            }
            dzi_section = next(s for s in sections if s["section_slug"] == DZI_SECTION_SLUG)
        self.assertGreater(totals["question_count"], 0)
        self.assertEqual(self.overview(section_slug=DZI_SECTION_SLUG), {**totals, "section": dzi_section})
        self.assertEqual(self.overview(), {**totals, "section": None})
        self.assertEqual(self.overview(section_slug="no-such-section"), {**totals, "section": None})
        cached = self.cache._values[(IT, True, "overview", tuple(IT_GRADES), DZI_SECTION_SLUG)]
        self.assertEqual(set(cached), {"section", "question_count", "search_content"})

    def test_overview_copies_are_owned_by_the_caller(self):
        first = self.overview(section_slug=DZI_SECTION_SLUG)
        original = deepcopy(first)
        first["section"]["section_title"] = "caller mutation"
        first["section"]["extra"] = []
        first["question_count"] = -1
        del first["search_content"]
        again = self.overview(section_slug=DZI_SECTION_SLUG)
        self.assertEqual(again, original)
        self.assertIsNot(again["section"], self.overview(section_slug=DZI_SECTION_SLUG)["section"])

    def test_home_page_dzi_card_follows_the_section(self):
        client = web_app.app.test_client()
        section = self.overview(section_slug=DZI_SECTION_SLUG)["section"]
        html = client.get("/").get_data(as_text=True)
        stats = html.split('class="home-dzi-stats"', 1)[1].split("</div>", 1)[0]
        self.assertIn(f"<span>{section['question_count']} въпроса</span>", stats)
        self.commit("UPDATE curriculum_sections SET title_bg = 'Кеширан преглед' WHERE section_slug = ?",
                    (DZI_SECTION_SLUG,))
        self.assertIn("26 27 28 Кеширан преглед", client.get("/").get_data(as_text=True))

    def test_home_page_renders_without_the_dzi_section(self):
        client = web_app.app.test_client()
        self.assertEqual(client.get("/").status_code, 200)
        self.commit("UPDATE curriculum_sections SET section_slug = 'grade12-renamed' WHERE section_slug = ?",
                    (DZI_SECTION_SLUG,))
        response = client.get("/")
        self.assertEqual(response.status_code, 200)
        stats = response.get_data(as_text=True).split('class="home-dzi-stats"', 1)[1].split("</div>", 1)[0]
        self.assertNotIn("въпроса", stats)
        self.assertIsNone(self.overview(section_slug=DZI_SECTION_SLUG)["section"])

    def test_warm_home_page_runs_no_content_query(self):
        active = sum(status == "active" for status in statuses().values())
        cold, cold_sql, _ = traced_get("/")
        warm, warm_sql, connections = traced_get("/")
        self.assertEqual((cold.status_code, warm.status_code), (200, 200))
        self.assertEqual(cold.data, warm.data)
        self.assertGreater(len(cold_sql), len(warm_sql))
        # One request connection: its setup PRAGMA and one cache-identity PRAGMA per active subject.
        self.assertEqual(connections, 1)
        self.assertEqual(sorted(sql.strip().upper() for sql in warm_sql),
                         ["PRAGMA DATABASE_LIST"] * active + ["PRAGMA FOREIGN_KEYS=ON"])


if __name__ == "__main__":
    unittest.main()
