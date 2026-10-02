"""Bounded, versioned cache of public browsing data, never SQLite connections.

Only the latest database identity is retained (production uses one database).
Switching files or changing either the database or its WAL drops all old values.
Cold computations use fresh query-only connections, so an old request connection
cannot publish data from a replaced file under the replacement's identity.
"""

from collections import OrderedDict
from concurrent.futures import Future
from copy import deepcopy
from pathlib import Path
import sqlite3
from threading import Lock

from src.subject_browsing import SubjectContentBrowser
from src.subject_content import IT, canonical_subject


def database_identity(path):
    """Fail closed on stat errors; absent/empty WALs contain no newer content."""
    path = Path(path).resolve()

    def stamp(file):
        stat = file.stat()
        return (stat.st_dev, stat.st_ino, stat.st_size,
                stat.st_mtime_ns, stat.st_ctime_ns)

    try:
        main = stamp(path)
        try:
            wal = stamp(Path(str(path) + "-wal"))
            if wal[2] == 0:
                wal = None
        except FileNotFoundError:
            wal = None
        return (str(path), main, wal)
    except OSError:
        return None


class BrowseResultCache:
    def __init__(self, max_entries=128):
        self.max_entries = max_entries
        self._lock = Lock()
        self._identity = None
        self._values = OrderedDict()
        self._pending = {}

    @staticmethod
    def _load(path, compute):
        # mode=rw never creates a missing DB, but lets SQLite initialize absent
        # WAL/SHM sidecars. mode=ro cannot do that on all SQLite builds. Enforce
        # read-only SQL before invoking any loader; do not change journal mode.
        conn = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=rw", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA query_only=ON")
            return compute(conn)
        finally:
            conn.close()

    def get(self, path, key, compute):
        # Only lookup/publication and filesystem stamps hold this lock. SQL,
        # waiting for another computation, and defensive copies stay outside.
        with self._lock:
            identity = database_identity(path)
            if identity != self._identity:
                self._identity = identity
                self._values = OrderedDict()
                self._pending = {}
            values, pending = self._values, self._pending
            hit = identity is not None and key in values
            if hit:
                result = values[key]
                values.move_to_end(key)
            elif identity is not None:
                owner = key not in pending
                future = pending.setdefault(key, Future())

        if identity is None:
            return self._load(path, compute)
        if hit:
            return deepcopy(result)
        if not owner:
            return deepcopy(future.result())

        try:
            result = deepcopy(self._load(path, compute))
            with self._lock:
                # A write during the read must never seed the next version.
                if values is self._values and database_identity(path) == identity:
                    values[key] = result
                    while len(values) > self.max_entries:
                        values.popitem(last=False)
            future.set_result(result)
            return deepcopy(result)
        except BaseException as exc:
            future.set_exception(exc)
            raise
        finally:
            with self._lock:
                pending.pop(key, None)


browse_cache = BrowseResultCache()


class CachedSubjectBrowser:
    """Request-local facade; only detached results enter the process cache."""

    def __init__(self, conn, subject, *, legacy_it=False):
        self.conn = conn
        self.subject = canonical_subject(subject)
        if self.subject is None:
            raise ValueError(f"Unknown content subject: {subject!r}")
        self.legacy_it = legacy_it and self.subject == IT
        self.path = next(row[2] for row in conn.execute("PRAGMA database_list")
                         if row[1] == "main")

    def _read(self, key, compute):
        def load(conn):
            return compute(SubjectContentBrowser(conn, self.subject, legacy_it=self.legacy_it))

        # Memory databases and transaction-local/uncommitted data cannot be
        # shared. Preserve their original connection's visibility semantics.
        if not self.path or self.conn.in_transaction:
            return load(self.conn)
        return browse_cache.get(self.path, (self.subject, self.legacy_it, *key), load)

    def grade_counts(self):
        return self._read(("grades",), lambda browser: browser.grade_counts())

    def overview(self, grades):
        def compute(browser):
            sections = [section for grade in grades
                        for section in browser.sections_for_grade(grade)]
            return {
                "sections": sections,
                "question_count": sum(int(row["question_count"]) for row in browser.grade_counts()),
                "search_content": browser.search_titles(sections),
            }

        # Keep the original shared ownership memoization across all grades on
        # a cold homepage request, rather than recomputing each grade in isolation.
        return self._read(("overview", tuple(grades)), compute)

    def sections_for_grade(self, grade):
        return self._read(("sections", grade), lambda browser: browser.sections_for_grade(grade))

    def section(self, slug, *, grade=None, legacy_unclassified=False):
        return self._read(("section", slug, grade, legacy_unclassified), lambda browser:
                          browser.section(slug, grade=grade, legacy_unclassified=legacy_unclassified))

    def section_for_url(self, url_slug, grade):
        return self._read(("section_url", url_slug, grade), lambda browser:
                          browser.section_for_url(url_slug, grade))

    def topics_in_section(self, section_id):
        return self._read(("topics", section_id), lambda browser: browser.topics_in_section(section_id))

    def question_details(self, section_id, decorate):
        return self._read(("question_details", section_id), lambda browser:
                          decorate(browser.conn, browser.questions_in_section(section_id)))
