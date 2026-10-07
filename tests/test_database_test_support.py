"""G2: normal test databases are built from migrations + canonical content, never from a repository database.

Covers the template/clone architecture of ``tests.support`` and the explicit runtime fixtures of
``tests.runtime_fixtures``. The local runtime database appears here only as something that must not be touched.
"""

import builtins
import hashlib
import os
import shutil
import sqlite3
import stat
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from src import build_database as builder
from src import canonical_tables as ct
from tests import runtime_fixtures as fixtures
from tests import support
from tests.support import CANONICAL, IT_ONLY, ROOT, build_test_database, canonical_template

RUNTIME_DB = (ROOT / "data" / "questions.db").resolve()


def tree_digest(*roots):
    digest = hashlib.sha256()
    for root in roots:
        for path in sorted(Path(root).rglob("*")):
            if path.is_file():
                digest.update(str(path.relative_to(ROOT)).encode() + path.read_bytes())
    return digest.hexdigest()


def runtime_counts(path):
    with closing(sqlite3.connect(path)) as conn:
        return {table: conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
                for table in ct.tables_of(ct.RUNTIME)}


class TemporaryDirectoryTestCase(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="learnpilot-support-test-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)


class TemplateIsCanonicalBuiltTest(TemporaryDirectoryTestCase):
    def test_creating_a_test_database_never_reads_or_copies_the_runtime_database(self):
        touched = []
        real_open, real_copyfile = builtins.open, shutil.copyfile

        def is_runtime_db(candidate):
            try:
                return Path(os.fspath(candidate)).resolve() == RUNTIME_DB
            except (TypeError, ValueError, OSError):
                return False

        def spy_open(file, *args, **kwargs):
            if is_runtime_db(file):
                touched.append(("open", file))
            return real_open(file, *args, **kwargs)

        def spy_copyfile(src, dst, *args, **kwargs):
            if is_runtime_db(src):
                touched.append(("copyfile", src))
            return real_copyfile(src, dst, *args, **kwargs)

        # A fresh template state forces a real build inside this test.
        with patch.object(support, "_template", {}), \
                patch.object(builtins, "open", spy_open), patch.object(shutil, "copyfile", spy_copyfile):
            build_test_database(self.root / "one.db")
            build_test_database(self.root / "two.db", IT_ONLY)
        self.assertEqual(touched, [])

    def test_template_is_logically_the_builder_output(self):
        built = self.root / "built.db"
        builder.build_database(built)
        self.assertEqual(builder.logical_fingerprint(canonical_template()),
                         builder.logical_fingerprint(built))
        self.assertEqual(builder.validation_problems(canonical_template()), [])

    def test_template_is_read_only_and_not_a_test_database(self):
        template = canonical_template()
        self.assertFalse(template.stat().st_mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))
        clone = build_test_database(self.root / "clone.db")
        self.assertNotEqual(clone, template)
        self.assertTrue(os.access(clone, os.W_OK))
        self.assertEqual(canonical_template(), template)         # one template per process

    def test_template_modification_is_detected(self):
        with patch.object(support, "_template", {}):
            template = canonical_template()
            template.chmod(0o644)
            with open(template, "ab") as handle:
                handle.write(b"tamper")
            with self.assertRaisesRegex(RuntimeError, "modified"):
                canonical_template()
            with self.assertRaisesRegex(RuntimeError, "modified"):
                build_test_database(self.root / "clone.db")

    def test_template_is_derived_from_tracked_inputs_only(self):
        self.assertRegex(support._template_inputs_digest(), r"^[0-9a-f]{64}$")
        canonical_template()
        self.assertEqual(support._template["inputs"], support._template_inputs_digest())

    def test_unknown_profile_is_rejected(self):
        with self.assertRaises(ValueError):
            canonical_template("legacy_seed")


class IsolationTest(TemporaryDirectoryTestCase):
    def test_writable_databases_are_independent_copies(self):
        template_sha = hashlib.sha256(canonical_template().read_bytes()).hexdigest()
        one = build_test_database(self.root / "one.db")
        two = build_test_database(self.root / "two.db")
        with closing(sqlite3.connect(one)) as conn:
            conn.execute("DELETE FROM multiple_choice_options")
            conn.execute("UPDATE questions SET prompt = 'changed'")
            fixtures.add_submitted_attempt(conn)
            conn.commit()
        self.assertEqual(builder.logical_fingerprint(two), builder.logical_fingerprint(canonical_template()))
        self.assertEqual(hashlib.sha256(canonical_template().read_bytes()).hexdigest(), template_sha)
        self.assertEqual(builder.validation_problems(canonical_template()), [])

    def test_existing_destination_is_refused(self):
        existing = self.root / "existing.db"
        existing.write_bytes(b"x")
        with self.assertRaises(FileExistsError):
            build_test_database(existing)
        self.assertEqual(existing.read_bytes(), b"x")

    def test_clones_have_no_sidecars_and_use_a_rollback_journal(self):
        clone = build_test_database(self.root / "clone.db")
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ["clone.db"])
        with closing(sqlite3.connect(clone)) as conn:
            self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0], "delete")

    def test_tests_do_not_modify_tracked_inputs(self):
        before = tree_digest(ROOT / "data" / "canonical", ROOT / "web" / "migrations")
        clone = build_test_database(self.root / "clone.db")
        with closing(sqlite3.connect(clone)) as conn:
            conn.execute("UPDATE questions SET prompt = 'x'")
            conn.commit()
        self.assertEqual(tree_digest(ROOT / "data" / "canonical", ROOT / "web" / "migrations"), before)


class RuntimeFixtureTest(TemporaryDirectoryTestCase):
    def test_a_fresh_test_database_has_no_runtime_rows(self):
        counts = runtime_counts(build_test_database(self.root / "fresh.db"))
        self.assertEqual(counts, {table: 0 for table in ct.tables_of(ct.RUNTIME)})

    def test_fixtures_create_only_the_requested_deterministic_rows(self):
        path = build_test_database(self.root / "fixture.db")
        with closing(sqlite3.connect(path)) as conn:
            conn.execute("PRAGMA foreign_keys = ON")
            assignment_id, attempt_id = fixtures.add_submitted_attempt(conn)
            conn.commit()
            self.assertEqual((assignment_id, attempt_id), (1, 1))
            question_ids = fixtures.first_multiple_choice_question_ids(conn)
            self.assertEqual(len(question_ids), 3)
            row = conn.execute("SELECT student_name, seed, submitted_at, score_correct, score_total "
                               "FROM quiz_attempts").fetchone()
            self.assertEqual(row, ("Fixture student", fixtures.FIXTURE_SEED, fixtures.FIXTURE_SUBMITTED_AT, 3, 3))
            self.assertEqual([r[0] for r in conn.execute("SELECT question_id FROM quiz_answers ORDER BY id")],
                             question_ids)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM quiz_answers WHERE is_correct = 1").fetchone()[0], 3)
            self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
        self.assertEqual(runtime_counts(path), {
            "quiz_assignments": 1, "quiz_attempts": 1, "quiz_answers": 3, "quiz_text_answers": 0,
            "practical_submissions": 0, "practical_submission_files": 0, "users": 0,
            "totp_credentials": 0, "totp_recovery_codes": 0})

    def test_fixture_ids_do_not_depend_on_other_tests(self):
        results = []
        for name in ("a.db", "b.db"):
            path = build_test_database(self.root / name)
            with closing(sqlite3.connect(path)) as conn:
                results.append(fixtures.add_submitted_attempt(conn, student_name="Same"))
                conn.commit()
        self.assertEqual(results[0], results[1])

    def test_fixture_state_does_not_leak_into_the_next_database(self):
        first = build_test_database(self.root / "first.db")
        with closing(sqlite3.connect(first)) as conn:
            fixtures.add_submitted_attempt(conn)
            conn.commit()
        second = build_test_database(self.root / "second.db")
        self.assertEqual(sum(runtime_counts(second).values()), 0)
        self.assertEqual(sum(runtime_counts(canonical_template()).values()), 0)

    def test_the_old_seed_smoke_rows_are_not_a_hidden_fixture(self):
        # The retired seed had 2 assignments, 4 attempts and 19 answers. They are not recreated.
        counts = runtime_counts(build_test_database(self.root / "fresh.db"))
        self.assertEqual((counts["quiz_assignments"], counts["quiz_attempts"], counts["quiz_answers"]), (0, 0, 0))


class ContentProfileTest(TemporaryDirectoryTestCase):
    def test_it_only_profile_removes_physics_and_biology_and_keeps_everything_else(self):
        canonical = build_test_database(self.root / "canonical.db")
        it_only = build_test_database(self.root / "it_only.db", IT_ONLY)
        with closing(sqlite3.connect(canonical)) as full, closing(sqlite3.connect(it_only)) as slim:
            slim.execute("PRAGMA foreign_keys = ON")
            self.assertEqual(slim.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.assertEqual(slim.execute("SELECT DISTINCT subject FROM curricula ORDER BY 1").fetchall(),
                             [("Информационни технологии",)])
            self.assertEqual(slim.execute("SELECT COUNT(*) FROM questions WHERE subject IN ('physics','biology')")
                             .fetchone()[0], 0)
            self.assertEqual(slim.execute("SELECT COUNT(*) FROM curricula").fetchone()[0], 5)
            self.assertEqual(slim.execute("SELECT COUNT(*) FROM questions").fetchone()[0], 832)
            self.assertEqual(slim.execute("SELECT COUNT(*) FROM exams").fetchone()[0],
                             full.execute("SELECT COUNT(*) FROM exams").fetchone()[0])
            # Identity of what remains is untouched: the IT/DZI rows are byte-for-byte the canonical rows.
            for table in ("curricula", "questions", "exam_tasks", "multiple_choice_options"):
                keep = {tuple(r) for r in slim.execute(f'SELECT * FROM "{table}"')}
                self.assertTrue(keep <= {tuple(r) for r in full.execute(f'SELECT * FROM "{table}"')}, table)
        self.assertEqual(sum(runtime_counts(it_only).values()), 0)

    def test_profile_template_is_also_read_only_and_cloned(self):
        template = canonical_template(IT_ONLY)
        self.assertFalse(template.stat().st_mode & stat.S_IWUSR)
        self.assertNotEqual(template, canonical_template(CANONICAL))


if __name__ == "__main__":
    unittest.main()
