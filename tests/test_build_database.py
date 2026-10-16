"""G2: the database builder (migrations + canonical content) and its failure safety.

Every database here is built in a temporary directory. ``data/questions.db`` is never read
by the builder; one test proves it with a spy on every way a file could be opened or copied.
"""

import builtins
import io
import os
import shutil
import sqlite3
import tempfile
import unittest
from contextlib import closing, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from src import build_database as builder
from src import canonical_export as export
from src import canonical_load as load
from src import canonical_tables as ct
from tests.support import ROOT

RUNTIME_DB = (ROOT / "data" / "questions.db").resolve()
CANONICAL = ROOT / "data" / "canonical"
MANIFEST = export.load_manifest(CANONICAL)
SIDECARS = ("-wal", "-shm", "-journal")

_SHARED = {}


def shared_build():
    """One real build for the read-only assertions (a build costs about 1.5 s)."""
    if not _SHARED:
        directory = tempfile.TemporaryDirectory(prefix="learnpilot-build-test-")
        unittest.addModuleCleanup(directory.cleanup)
        path = Path(directory.name) / "nested" / "built.db"   # the parent does not exist yet
        _SHARED["result"] = builder.build_database(path)
        _SHARED["path"] = path
    return _SHARED["path"], _SHARED["result"]


def connect(path):
    return sqlite3.connect(f"{Path(path).resolve().as_uri()}?mode=ro", uri=True)


class TemporaryDirectoryTestCase(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="learnpilot-build-test-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)

    def assert_nothing_left_behind(self, destination):
        self.assertFalse(os.path.lexists(destination), f"{destination} must not exist after a failed build")
        for suffix in SIDECARS:
            self.assertFalse(os.path.lexists(str(destination) + suffix))
        leftovers = [p.name for p in destination.parent.iterdir()] if destination.parent.exists() else []
        self.assertEqual(leftovers, [], "temporary build artifacts were not cleaned up")


class BuiltDatabaseTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.path, cls.result = shared_build()

    def test_fresh_build_succeeds_and_creates_missing_parent_directories(self):
        self.assertTrue(self.path.is_file())
        self.assertEqual(self.result.path, self.path)
        self.assertEqual([p.name for p in self.path.parent.iterdir()], ["built.db"])

    def test_no_sidecar_files_and_rollback_journal_mode(self):
        for suffix in SIDECARS:
            self.assertFalse(Path(str(self.path) + suffix).exists())
        with closing(connect(self.path)) as conn:
            self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0], "delete")
            self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")

    def test_every_migration_is_applied_in_order(self):
        files = export.migration_files()
        self.assertEqual(files[0], "000_baseline.sql")
        with closing(connect(self.path)) as conn:
            applied = [row[0] for row in conn.execute("SELECT filename FROM schema_migrations ORDER BY filename")]
        self.assertEqual(applied, files)
        self.assertEqual(self.result.migration_level, files[-1])
        self.assertEqual(self.result.migration_level, MANIFEST["schema"]["migration_level"])

    def test_counts_and_normalized_hashes_match_the_manifest(self):
        self.assertEqual(sorted(self.result.tables), sorted(MANIFEST["tables"]))
        with closing(connect(self.path)) as conn:
            for table, entry in MANIFEST["tables"].items():
                self.assertEqual(ct.table_digest(conn, table), (entry["rows"], entry["sha256"]), table)
        self.assertEqual(self.result.tables["questions"][0], MANIFEST["tables"]["questions"]["rows"])

    def test_stable_ids_are_exactly_the_canonical_ids(self):
        with closing(connect(self.path)) as conn:
            for table, entry in MANIFEST["tables"].items():
                if entry["primary_key"] != ["id"]:
                    continue
                canonical_ids = [row["id"] for row in ct.read_table_rows(CANONICAL / entry["file"])]
                built_ids = [row[0] for row in conn.execute(f'SELECT id FROM "{table}" ORDER BY id')]
                self.assertEqual(built_ids, canonical_ids, table)

    def test_sqlite_sequence_matches_the_manifest_and_nothing_else(self):
        expected = {table: entry["sqlite_sequence"] for table, entry in MANIFEST["tables"].items()
                    if entry["sqlite_sequence"] is not None}
        with closing(connect(self.path)) as conn:
            actual = dict(conn.execute("SELECT name, seq FROM sqlite_sequence").fetchall())
        self.assertEqual(actual, expected)
        self.assertEqual(self.result.sequences, expected)

    def test_foreign_keys_are_clean(self):
        with closing(connect(self.path)) as conn:
            conn.execute("PRAGMA foreign_keys = ON")
            self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_runtime_tables_are_empty(self):
        with closing(connect(self.path)) as conn:
            for table in ct.tables_of(ct.RUNTIME):
                # Empty, except the rows a migration itself seeds (the lifecycle clock row).
                self.assertEqual(conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0],
                                 ct.MIGRATION_SEEDED_ROWS.get(table, 0), table)

    def test_operational_tables_are_are_empty_after_a_build(self):
        with closing(connect(self.path)) as conn:
            for table in ("obsidian_notes", "note_question_links", "generated_exams", "scrape_log"):
                self.assertEqual(conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0], 0, table)

    def test_parity_with_the_canonical_files_is_clean(self):
        from src import canonical_parity as parity

        with closing(sqlite3.connect(self.path)) as conn:
            conn.execute("PRAGMA foreign_keys = ON")
            self.assertEqual(parity.compare_to_canonical(conn, CANONICAL), [])
        self.assertEqual(builder.validation_problems(self.path), [])

    def test_identity_counts_of_the_g1_dataset(self):
        counts = {table: rows for table, (rows, _) in self.result.tables.items()}
        for table, expected in {
            "curricula": 11, "curriculum_sections": 67, "curriculum_topics": 180, "questions": 1678,
            "multiple_choice_options": 6275, "fill_in_subquestions": 162, "exams": 11,
            "exam_tasks": 196, "practical_task_resources": 77,
        }.items():
            self.assertEqual(counts[table], expected, table)


class BuildReproducibilityTest(TemporaryDirectoryTestCase):
    def test_two_builds_are_logically_identical(self):
        first, _ = shared_build()
        second = self.root / "two.db"
        builder.build_database(second)
        self.assertEqual(builder.logical_fingerprint(first), builder.logical_fingerprint(second))
        fingerprint = builder.logical_fingerprint(first)
        self.assertEqual(fingerprint["migrations"], export.migration_files())
        self.assertEqual(fingerprint["foreign_key_violations"], 0)
        self.assertEqual(
            {table: count for table, count in fingerprint["non_canonical_rows"].items() if count},
            ct.MIGRATION_SEEDED_ROWS)

    def test_fingerprint_detects_a_logical_difference(self):
        first, _ = shared_build()
        second = self.root / "two.db"
        shutil.copyfile(first, second)
        with closing(sqlite3.connect(second)) as conn:
            conn.execute("UPDATE questions SET prompt = prompt || ' changed' WHERE id = (SELECT MIN(id) FROM questions)")
            conn.commit()
        self.assertNotEqual(builder.logical_fingerprint(first), builder.logical_fingerprint(second))


class NoRuntimeDatabaseDependencyTest(TemporaryDirectoryTestCase):
    def test_build_never_opens_or_copies_the_runtime_database(self):
        touched = []
        real_open, real_connect = builtins.open, sqlite3.connect
        copies = {name: getattr(shutil, name) for name in ("copyfile", "copy", "copy2", "copyfileobj")}

        def is_runtime_db(candidate):
            try:
                return Path(os.fspath(candidate)).resolve() == RUNTIME_DB
            except (TypeError, ValueError, OSError):
                return False

        def spy_open(file, *args, **kwargs):
            if is_runtime_db(file):
                touched.append(("open", file))
            return real_open(file, *args, **kwargs)

        def spy_connect(database, *args, **kwargs):
            target = database
            if isinstance(database, str) and database.startswith("file:"):
                target = database.split("?", 1)[0][len("file:"):]
            if is_runtime_db(target):
                touched.append(("connect", database))
            return real_connect(database, *args, **kwargs)

        def spy_copy(name):
            def copier(src, dst, *args, **kwargs):
                if is_runtime_db(src):
                    touched.append((name, src))
                return copies[name](src, dst, *args, **kwargs)
            return copier

        with patch.object(builtins, "open", spy_open), patch.object(sqlite3, "connect", spy_connect), \
                patch.multiple(shutil, **{name: spy_copy(name) for name in ("copyfile", "copy", "copy2")}):
            builder.build_database(self.root / "built.db")
        self.assertEqual(touched, [])

    def test_builder_source_copies_no_database(self):
        source = (ROOT / "src" / "build_database.py").read_text(encoding="utf-8")
        self.assertNotIn("copyfile", source)
        self.assertNotIn("shutil.copy", source)


class FailureSafetyTest(TemporaryDirectoryTestCase):
    def test_existing_destination_is_refused_and_left_untouched(self):
        destination = self.root / "existing.db"
        destination.write_bytes(b"precious")
        with self.assertRaisesRegex(builder.BuildError, "already exists"):
            builder.build_database(destination)
        self.assertEqual(destination.read_bytes(), b"precious")
        self.assertEqual([p.name for p in self.root.iterdir()], ["existing.db"])

    def test_existing_directory_symlink_or_stale_sidecar_destination_is_refused(self):
        (self.root / "dir.db").mkdir()
        (self.root / "link.db").symlink_to(self.root / "nowhere")
        (self.root / "stale.db-wal").write_bytes(b"stale")
        for name in ("dir.db", "link.db", "stale.db"):
            with self.subTest(name=name), self.assertRaises(builder.BuildError):
                builder.build_database(self.root / name)
        self.assertFalse((self.root / "stale.db").exists())

    def test_migration_failure_leaves_nothing_behind(self):
        destination = self.root / "out" / "built.db"
        with patch.object(load.manage_migrations, "apply_migrations", return_value=1):
            with self.assertRaisesRegex(builder.BuildError, "migrations failed"):
                builder.build_database(destination)
        self.assert_nothing_left_behind(destination)

    def test_migration_sql_error_leaves_nothing_behind(self):
        destination = self.root / "out" / "built.db"
        with patch.object(load.manage_migrations, "apply_migrations", side_effect=sqlite3.OperationalError("boom")):
            with self.assertRaisesRegex(builder.BuildError, "boom"):
                builder.build_database(destination)
        self.assert_nothing_left_behind(destination)

    def test_missing_canonical_file_fails_and_leaves_nothing_behind(self):
        canonical = self.root / "canonical"
        shutil.copytree(CANONICAL, canonical)
        (canonical / "questions.jsonl").unlink()
        destination = self.root / "out" / "built.db"
        with self.assertRaisesRegex(builder.BuildError, "questions"):
            builder.build_database(destination, canonical)
        self.assert_nothing_left_behind(destination)

    def test_missing_manifest_fails(self):
        canonical = self.root / "canonical"
        shutil.copytree(CANONICAL, canonical)
        (canonical / "manifest.json").unlink()
        destination = self.root / "out" / "built.db"
        with self.assertRaises(builder.BuildError):
            builder.build_database(destination, canonical)
        self.assert_nothing_left_behind(destination)

    def test_corrupt_canonical_content_hash_mismatch_fails(self):
        canonical = self.root / "canonical"
        shutil.copytree(CANONICAL, canonical)
        path = canonical / "questions.jsonl"
        text = path.read_text(encoding="utf-8")
        path.write_text(text.replace('"points":', '"points": ', 1), encoding="utf-8")  # no longer normalized
        destination = self.root / "out" / "built.db"
        with self.assertRaises(builder.BuildError):
            builder.build_database(destination, canonical)
        self.assert_nothing_left_behind(destination)

    def test_changed_value_with_stale_manifest_hash_fails(self):
        canonical = self.root / "canonical"
        shutil.copytree(CANONICAL, canonical)
        path = canonical / "curricula.jsonl"
        lines = path.read_text(encoding="utf-8").split("\n")
        lines[0] = lines[0].replace('"grade":8', '"grade":9', 1)
        path.write_text("\n".join(lines), encoding="utf-8")
        destination = self.root / "out" / "built.db"
        with self.assertRaisesRegex(builder.BuildError, "sha256|inconsistent"):
            builder.build_database(destination, canonical)
        self.assert_nothing_left_behind(destination)

    def test_canonical_load_conflict_fails(self):
        destination = self.root / "out" / "built.db"
        with patch.object(load, "insert_rows", side_effect=ct.CanonicalError("curricula: key [1] already exists with different content")):
            with self.assertRaisesRegex(builder.BuildError, "already exists with different content"):
                builder.build_database(destination)
        self.assert_nothing_left_behind(destination)

    def tampered_problems(self, tamper):
        """``validation_problems`` of a copy of the shared build after ``tamper`` (no extra build)."""
        built, _ = shared_build()
        copy = self.root / "tampered.db"
        shutil.copyfile(built, copy)
        with closing(sqlite3.connect(copy)) as conn:
            conn.execute("PRAGMA foreign_keys = OFF")
            tamper(conn)
            conn.commit()
        return "\n".join(builder.validation_problems(copy))

    def test_foreign_key_failure_is_detected(self):
        message = self.tampered_problems(lambda conn: conn.execute(
            "INSERT INTO multiple_choice_options (question_id, option_letter, option_text) VALUES (999999, 'Z', 'x')"))
        self.assertIn("foreign key", message)

    def test_parity_failure_is_detected(self):
        message = self.tampered_problems(lambda conn: conn.execute(
            "UPDATE questions SET prompt = prompt || '!' WHERE id = (SELECT MIN(id) FROM questions)"))
        self.assertIn("questions", message)
        self.assertIn("hash differs", message)

    def test_accidental_runtime_row_is_detected(self):
        message = self.tampered_problems(lambda conn: conn.execute(
            "INSERT INTO quiz_assignments (section_id, title_bg, question_count) "
            "VALUES ((SELECT MIN(id) FROM curriculum_sections), 'leaked', 1)"))
        self.assertIn("quiz_assignments", message)
        self.assertIn("sqlite_sequence has entries for non-canonical tables", message)

    def test_accidental_operational_row_is_detected(self):
        message = self.tampered_problems(lambda conn: conn.execute(
            "INSERT INTO obsidian_notes (file_path, note_type) VALUES ('x.md', 'topic')"))
        self.assertIn("obsidian_notes", message)

    def test_wrong_sequence_is_detected(self):
        message = self.tampered_problems(lambda conn: conn.execute(
            "UPDATE sqlite_sequence SET seq = seq + 1 WHERE name = 'questions'"))
        self.assertIn("sqlite_sequence", message)

    def test_missing_migration_record_is_detected(self):
        message = self.tampered_problems(lambda conn: conn.execute(
            "DELETE FROM schema_migrations WHERE filename = (SELECT MAX(filename) FROM schema_migrations)"))
        self.assertIn("migration", message)

    def test_a_failed_validation_during_a_real_build_leaves_nothing_behind(self):
        real_load = load.load_canonical

        def tampering_load(db_path, canonical_dir=ct.CANONICAL_DIR):
            result = real_load(db_path, canonical_dir)
            with closing(sqlite3.connect(db_path)) as conn:
                conn.execute("UPDATE questions SET prompt = prompt || '!' WHERE id = (SELECT MIN(id) FROM questions)")
                conn.commit()
            return result

        destination = self.root / "out" / "built.db"
        with patch.object(load, "load_canonical", tampering_load):
            with self.assertRaisesRegex(builder.BuildError, "validation failed"):
                builder.build_database(destination)
        self.assert_nothing_left_behind(destination)

    def test_failure_never_removes_a_preexisting_sibling_file(self):
        sibling = self.root / "keep.txt"
        sibling.write_text("keep")
        with patch.object(load.manage_migrations, "apply_migrations", return_value=1):
            with self.assertRaises(builder.BuildError):
                builder.build_database(self.root / "built.db")
        self.assertEqual([p.name for p in self.root.iterdir()], ["keep.txt"])

    def test_destination_appearing_during_the_build_is_not_overwritten(self):
        destination = self.root / "built.db"
        real_validation = builder.validation_problems

        def validation_then_race(path, canonical_dir=ct.CANONICAL_DIR):
            problems = real_validation(path, canonical_dir)
            destination.write_bytes(b"someone else")
            return problems

        with patch.object(builder, "validation_problems", validation_then_race):
            with self.assertRaisesRegex(builder.BuildError, "appeared while building"):
                builder.build_database(destination)
        self.assertEqual(destination.read_bytes(), b"someone else")
        self.assertEqual([p.name for p in self.root.iterdir()], ["built.db"])


class CommandLineTest(TemporaryDirectoryTestCase):
    def run_main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            status = builder.main(list(argv), out=out, err=err)
        return status, out.getvalue(), err.getvalue()

    def test_output_is_required(self):
        with self.assertRaises(SystemExit), redirect_stderr(io.StringIO()):
            builder.parse_args([])

    def test_success_prints_a_summary_and_exits_zero(self):
        destination = self.root / "cli" / "built.db"
        status, out, err = self.run_main("--output", str(destination))
        self.assertEqual((status, err), (0, ""))
        self.assertTrue(destination.is_file())
        self.assertIn("migration level: 024", out)
        self.assertIn("questions: 1678", out)
        self.assertIn("validation", out)

    def test_existing_destination_exits_nonzero_without_overwriting(self):
        destination = self.root / "built.db"
        destination.write_bytes(b"keep me")
        status, out, err = self.run_main("--output", str(destination))
        self.assertEqual(status, 1)
        self.assertIn("refusing to overwrite", err)
        self.assertEqual(destination.read_bytes(), b"keep me")

    def test_failure_exits_nonzero_and_leaves_no_database(self):
        canonical = self.root / "canonical"
        shutil.copytree(CANONICAL, canonical)
        (canonical / "exams.jsonl").unlink()
        destination = self.root / "out" / "built.db"
        status, out, err = self.run_main("--output", str(destination), "--canonical-dir", str(canonical))
        self.assertEqual(status, 1)
        self.assertTrue(err.startswith("error:"))
        self.assertFalse(destination.exists())

    def test_module_entry_point_runs_from_a_clean_interpreter(self):
        import subprocess
        import sys

        destination = self.root / "module.db"
        result = subprocess.run([sys.executable, "-B", "-m", "src.build_database", "--output", str(destination)],
                                cwd=ROOT, capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(destination.is_file())
        self.assertEqual(builder.validation_problems(destination), [])


if __name__ == "__main__":
    unittest.main()
