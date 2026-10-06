"""G1: classified, identity-preserving canonical content and its round trip.

Every database written here is a temporary file. The tracked seed is only read
through byte copies (``copy_seed_database``) and its hash must not change.
"""

import hashlib
import io
import json
import os
import shutil
import sqlite3
import tempfile
import unittest
from contextlib import closing, redirect_stderr
from pathlib import Path
from unittest.mock import patch

from src import manage_migrations
from src import canonical_export as export
from src import canonical_load as load
from src import canonical_parity as parity
from src import canonical_reconcile as reconcile
from src import canonical_tables as ct
from tests.support import ROOT, copy_seed_database

SEED = ROOT / "data" / "questions.db"
CANONICAL = ROOT / "data" / "canonical"
MAPPINGS = ROOT / "docs" / "reviews" / "confirmed_dzi_image_question_mappings.json"
IDENTITY_ENV = "LEARNPILOT_G1_IDENTITY_MANIFEST"


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def rows_of(path):
    return list(ct.read_table_rows(Path(path)))


def canonical_rows(table, directory=CANONICAL):
    return rows_of(Path(directory) / f"{table}.jsonl")


_SHARED = {}


def shared_rebuild():
    """One canonical rebuild for the whole module (rebuilding is the slow part)."""
    if not _SHARED:
        directory = tempfile.TemporaryDirectory(prefix="learnpilot-canonical-test-")
        unittest.addModuleCleanup(directory.cleanup)
        _SHARED["work"] = Path(directory.name)
        _SHARED["rebuilt"] = _SHARED["work"] / "rebuilt.db"
        load.rebuild_for_parity(_SHARED["rebuilt"])
    return _SHARED["work"], _SHARED["rebuilt"]


class CanonicalTestCase(unittest.TestCase):
    """Shares one canonical rebuild; tests that modify a database copy it first."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.seed_sha_before = sha256(SEED)
        cls.addClassCleanup(cls.assert_seed_untouched)
        cls.work, cls.rebuilt = shared_rebuild()
        cls.manifest = export.load_manifest(CANONICAL)

    @classmethod
    def assert_seed_untouched(cls):
        assert sha256(SEED) == cls.seed_sha_before, "the tracked seed changed during the tests"
        for suffix in ("-wal", "-shm", "-journal"):
            assert not Path(str(SEED) + suffix).exists(), f"seed sidecar {suffix} appeared"

    def temp_path(self, name):
        directory = tempfile.TemporaryDirectory(prefix="learnpilot-canonical-test-")
        self.addCleanup(directory.cleanup)
        return Path(directory.name) / name

    def connect_rebuilt(self):
        conn = sqlite3.connect(self.rebuilt)
        self.addCleanup(conn.close)
        return conn

    def seed_copy(self):
        path = self.temp_path("seed-copy.db")
        copy_seed_database(SEED, path)
        return path

    def canonical_copy(self):
        path = self.temp_path("canonical")
        shutil.copytree(CANONICAL, path)
        return path


class ClassificationTest(CanonicalTestCase):
    def test_every_application_table_is_classified_exactly_once(self):
        conn = self.connect_rebuilt()
        self.assertEqual(ct.classification_problems(conn), [])
        present = set(ct.application_tables(conn))
        self.assertEqual(present - {"sqlite_sequence"}, set(ct.TABLE_CLASSIFICATION) - {"sqlite_sequence"})
        groups = [set(ct.tables_of(kind)) for kind in (ct.CANONICAL, ct.RUNTIME, ct.OPERATIONAL)]
        self.assertEqual(sum(len(group) for group in groups), len(ct.TABLE_CLASSIFICATION))
        self.assertEqual(set().union(*groups), set(ct.TABLE_CLASSIFICATION))

    def test_unclassified_table_is_reported(self):
        path = self.temp_path("extra.db")
        shutil.copyfile(self.rebuilt, path)
        with closing(sqlite3.connect(path)) as conn:
            conn.execute("CREATE TABLE new_unclassified_table (id INTEGER PRIMARY KEY)")
            self.assertEqual(ct.classification_problems(conn),
                             ["table new_unclassified_table has no classification"])
            with self.assertRaises(ct.CanonicalError):
                export.export_database(conn, self.temp_path("out"))

    def test_expected_exclusions(self):
        self.assertEqual(ct.tables_of(ct.RUNTIME), sorted([
            "quiz_assignments", "quiz_attempts", "quiz_answers", "quiz_text_answers",
            "practical_submissions", "practical_submission_files"]))
        self.assertEqual(ct.tables_of(ct.OPERATIONAL), sorted([
            "obsidian_notes", "note_question_links", "generated_exams", "scrape_log",
            "schema_migrations", "sqlite_sequence"]))
        self.assertIn("assessment_events", ct.tables_of(ct.CANONICAL))

    def test_canonical_tables_only_reference_canonical_tables(self):
        order = ct.canonical_load_order(self.connect_rebuilt())
        self.assertEqual(sorted(order), ct.tables_of(ct.CANONICAL))
        self.assertLess(order.index("curricula"), order.index("curriculum_sections"))
        self.assertLess(order.index("questions"), order.index("multiple_choice_options"))
        self.assertEqual(order, self.manifest["canonical_tables"])


class ExclusionTest(CanonicalTestCase):
    def test_runtime_and_operational_tables_are_absent_from_the_tracked_data(self):
        files = {path.stem for path in CANONICAL.glob("*.jsonl")}
        self.assertEqual(files, set(ct.tables_of(ct.CANONICAL)))
        self.assertEqual(files & (set(ct.tables_of(ct.RUNTIME)) | set(ct.tables_of(ct.OPERATIONAL))), set())
        self.assertEqual(self.manifest["runtime_exclusions"], ct.tables_of(ct.RUNTIME))
        self.assertEqual(self.manifest["operational_exclusions"], ct.tables_of(ct.OPERATIONAL))

    def test_no_personal_or_machine_data(self):
        text = "".join(path.read_text(encoding="utf-8") for path in CANONICAL.iterdir())
        self.assertNotIn("student_name", text)
        for marker in ("/Users/", "/home/", "/root/", "/tmp/", "C:\\\\", "password", "trycloudflare"):
            self.assertNotIn(marker, text)

    def test_export_from_a_database_with_runtime_rows_leaves_them_out(self):
        path = self.seed_copy()  # holds 2 assignments, 4 attempts, 19 answers, 228 notes
        # The frozen seed predates 011: adopt the baseline and apply what is pending.
        for flags in (("--baseline-apply",), ("--apply",)):
            manage_migrations.main(["--db", str(path), *flags], out=io.StringIO(), err=io.StringIO())
        with closing(sqlite3.connect(path)) as conn:
            self.assertGreater(conn.execute("SELECT COUNT(*) FROM quiz_attempts").fetchone()[0], 0)
            out = self.temp_path("out")
            export.export_database(conn, out)
        self.assertEqual({p.stem for p in out.glob("*.jsonl")}, set(ct.tables_of(ct.CANONICAL)))
        self.assertNotIn("quiz_attempts.jsonl", {p.name for p in out.iterdir()})

    def test_canonical_rebuild_leaves_non_canonical_tables_empty(self):
        conn = self.connect_rebuilt()
        for table in ct.tables_of(ct.RUNTIME) + ct.tables_of(ct.OPERATIONAL):
            if table in ("schema_migrations", "sqlite_sequence"):
                continue
            self.assertEqual(conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0], 0, table)


class DeterminismTest(CanonicalTestCase):
    def test_export_is_deterministic_and_reproduces_the_tracked_files(self):
        first, second = self.temp_path("one"), self.temp_path("two")
        with closing(sqlite3.connect(self.rebuilt)) as conn:
            export.export_database(conn, first)
            export.export_database(conn, second)
        for path in sorted(first.iterdir()):
            self.assertEqual(path.read_bytes(), (second / path.name).read_bytes(), path.name)
            if path.suffix == ".jsonl":
                self.assertEqual(path.read_bytes(), (CANONICAL / path.name).read_bytes(), path.name)
        again, tracked = export.load_manifest(first), dict(self.manifest)
        again["sources"] = tracked["sources"] = None
        self.assertEqual(again, tracked)

    def test_file_format(self):
        for path in CANONICAL.glob("*.jsonl"):
            data = path.read_bytes()
            self.assertTrue(data == b"" or data.endswith(b"\n"), path.name)
            self.assertNotIn(b"\r", data)
            columns = self.manifest["tables"][path.stem]["columns"]
            for row in rows_of(path):
                self.assertEqual(list(row), columns)
        raw = (CANONICAL / "questions.jsonl").read_text(encoding="utf-8")
        self.assertIn('"exam_id":null', raw)
        self.assertTrue(any(ord(char) > 127 for char in raw), "text must be stored unescaped")

    def test_rows_follow_primary_key_order(self):
        for table, entry in self.manifest["tables"].items():
            keys = [tuple(row[k] for k in entry["primary_key"]) for row in canonical_rows(table)]
            self.assertEqual(keys, sorted(keys), table)

    def test_one_normalization_implementation(self):
        row = {"id": 1, "text": "кирилица", "ratio": 1.0, "count": 1, "missing": None}
        line = ct.serialize_row(list(row), row.values())
        self.assertEqual(line, '{"id":1,"text":"кирилица","ratio":1.0,"count":1,"missing":null}')
        with self.assertRaises(ct.CanonicalError):
            ct.serialize_row(["blob"], [b"\x00"])
        conn = self.connect_rebuilt()
        for table, entry in self.manifest["tables"].items():
            self.assertEqual(ct.table_digest(conn, table), (entry["rows"], entry["sha256"]), table)
            self.assertEqual(ct.file_digest(CANONICAL / entry["file"]), (entry["rows"], entry["sha256"]), table)

    def test_rebuild_twice_gives_identical_table_hashes(self):
        second = self.temp_path("second.db")
        load.rebuild_for_parity(second)
        self.assertEqual(load.canonical_digests(self.rebuilt), load.canonical_digests(second))


class StableIdentityTest(CanonicalTestCase):
    def test_legacy_seed_rows_are_preserved_exactly(self):
        path = self.seed_copy()
        with closing(sqlite3.connect(path)) as conn:
            for table, entry in self.manifest["tables"].items():
                columns, keys = entry["columns"], entry["primary_key"]
                canonical = {tuple(r[k] for k in keys): r for r in canonical_rows(table)}
                select = ", ".join(f'"{c}"' for c in columns)
                for values in conn.execute(f'SELECT {select} FROM "{table}"'):
                    row = dict(zip(columns, values))
                    key = tuple(row[k] for k in keys)
                    self.assertEqual(canonical.get(key), row, f"{table} {key}")

    def test_legacy_natural_keys_keep_their_ids(self):
        path = self.seed_copy()
        with closing(sqlite3.connect(path)) as conn:
            for table, natural in ct.NATURAL_KEYS.items():
                if not natural:
                    continue
                cols = ", ".join(natural)
                stored = {row[:-1]: row[-1] for row in conn.execute(f"SELECT {cols}, id FROM {table}")
                          if None not in row[:-1]}
                canonical = {tuple(r[k] for k in natural): r["id"] for r in canonical_rows(table)
                             if None not in tuple(r[k] for k in natural)}
                for key, row_id in stored.items():
                    self.assertEqual(canonical[key], row_id, f"{table} {key}")

    def test_sequences_never_fall_below_the_seed(self):
        path = self.seed_copy()
        with closing(sqlite3.connect(path)) as conn:
            for table, entry in self.manifest["tables"].items():
                seed_sequence = ct.stored_sequence(conn, table)
                if seed_sequence is not None:
                    self.assertGreaterEqual(entry["sqlite_sequence"], seed_sequence, table)
                if entry["autoincrement"] and entry["max_id"] is not None:
                    self.assertGreaterEqual(entry["sqlite_sequence"], entry["max_id"], table)
        self.assertEqual(self.manifest["tables"]["questions"]["sqlite_sequence"], 1683)
        self.assertEqual(self.manifest["tables"]["curriculum_topics"]["sqlite_sequence"], 181)

    def test_high_level_counts(self):
        expected = {"curricula": 11, "curriculum_sections": 67, "curriculum_topics": 180, "questions": 1678,
                    "multiple_choice_options": 6275, "fill_in_subquestions": 162, "exams": 11,
                    "exam_tasks": 196, "practical_task_resources": 77}
        self.assertEqual({t: self.manifest["tables"][t]["rows"] for t in expected}, expected)

    def test_id_allocation_never_recycles(self):
        entry = self.manifest["tables"]["questions"]
        conn = self.connect_rebuilt()
        self.assertEqual(ct.next_free_id(entry, conn, "questions"), 1684)
        path = self.temp_path("target.db")
        load.rebuild_for_parity(path)
        with closing(sqlite3.connect(path)) as target:
            target.execute("UPDATE sqlite_sequence SET seq = 2000 WHERE name = 'questions'")
            self.assertEqual(ct.next_free_id(entry, target, "questions"), 2001)
            target.execute("DELETE FROM questions WHERE id = 1683")  # deleted ids stay used
            self.assertEqual(ct.next_free_id(entry, target, "questions"), 2001)
        low = dict(entry, max_id=10, sqlite_sequence=10)
        self.assertEqual(ct.next_free_id(low, conn, "questions"), 1684)


class TrackedInputReconciliationTest(CanonicalTestCase):
    """Physics/Biology: tracked values + canonical ids, matched by natural key exactly once."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        seed = cls.work / "seed.db"
        copy_seed_database(SEED, seed)
        cls.scratch = cls.work / "scratch.db"
        reconcile.scratch_import(seed, cls.scratch, ROOT)
        with closing(sqlite3.connect(seed)) as seed_conn, closing(sqlite3.connect(cls.scratch)) as scratch_conn:
            tables = ct.tables_of(ct.CANONICAL)
            cls.seed_rows = {t: reconcile._rows(seed_conn, t) for t in tables}
            cls.scratch_rows = {t: reconcile._rows(scratch_conn, t) for t in tables}
            cls.seed_sequences = {t: ct.stored_sequence(seed_conn, t) or 0 for t in tables}
        cls.identities = {
            table: [{**row} for row in canonical_rows(table)]
            for table in ("curricula", "curriculum_sections", "curriculum_topics", "questions",
                          "multiple_choice_options")
        }
        cls.maps = reconcile.build_id_maps(cls.identities, cls.seed_rows, cls.scratch_rows, cls.seed_sequences)

    def build(self, identities):
        return reconcile.build_id_maps(identities, self.seed_rows, self.scratch_rows, self.seed_sequences)

    def test_every_new_row_is_matched_exactly_once(self):
        expected = {"curricula": 6, "curriculum_sections": 20, "curriculum_topics": 53,
                    "questions": 846, "multiple_choice_options": 3384}
        for table, count in expected.items():
            mapping = self.maps[table]
            self.assertEqual(len(mapping), count, table)
            self.assertEqual(len(set(mapping.values())), count, f"{table}: two rows share an id")
            canonical_new = {r["id"] for r in canonical_rows(table)} - {r["id"] for r in self.seed_rows[table]}
            self.assertEqual(set(mapping.values()), canonical_new, table)

    def test_new_ids_continue_the_seed_sequences(self):
        for table, low, high in (("curricula", 6, 11), ("curriculum_sections", 48, 67),
                                 ("curriculum_topics", 129, 181), ("questions", 838, 1683),
                                 ("multiple_choice_options", 3288, 6671)):
            self.assertEqual((min(self.maps[table].values()), max(self.maps[table].values())), (low, high))
            self.assertGreater(low, self.seed_sequences[table])

    def test_values_come_from_the_tracked_inputs(self):
        canonical = {t: {r["id"]: r for r in canonical_rows(t)}
                     for t in ("curriculum_sections", "curriculum_topics", "questions", "multiple_choice_options",
                               "curricula")}
        options = {}
        for row in canonical["multiple_choice_options"].values():
            options.setdefault(row["question_id"], {})[row["option_letter"]] = (row["option_text"], row["is_correct"])
        scratch_options = {}
        for row in self.scratch_rows["multiple_choice_options"]:
            scratch_options.setdefault(row["question_id"], {})[row["option_letter"]] = (
                row["option_text"], row["is_correct"])
        for row in self.scratch_rows["curriculum_topics"]:
            if row["id"] in self.maps["curriculum_topics"]:
                new = canonical["curriculum_topics"][self.maps["curriculum_topics"][row["id"]]]
                self.assertEqual((new["topic_slug"], new["title_bg"], new["description"]),
                                 (row["topic_slug"], row["title_bg"], row["description"]))
                self.assertEqual(new["section_id"], self.maps["curriculum_sections"][row["section_id"]])
        for row in self.scratch_rows["questions"]:
            if row["id"] not in self.maps["questions"]:
                continue
            new = canonical["questions"][self.maps["questions"][row["id"]]]
            self.assertEqual({k: v for k, v in new.items() if k not in ("id", "topic_id", "created_at")},
                             {k: v for k, v in row.items() if k not in ("id", "topic_id", "created_at")})
            self.assertEqual(new["topic_id"], self.maps["curriculum_topics"][row["topic_id"]])
            self.assertIsNone(new["created_at"])
            self.assertEqual(options[new["id"]], scratch_options[row["id"]])
        self.assertEqual(len(self.maps["questions"]), 846)

    def test_link_rows_follow_their_parents(self):
        topics = {r["id"]: r for r in canonical_rows("curriculum_topics")}
        questions = {r["id"]: r for r in canonical_rows("questions")}
        new_questions = set(self.maps["questions"].values())
        assignments = [r for r in canonical_rows("question_topic_assignments") if r["question_id"] in new_questions]
        self.assertEqual(len(assignments), 846)
        self.assertEqual(len({r["question_id"] for r in assignments}), 846)
        for row in assignments:
            self.assertEqual(row["topic_id"], questions[row["question_id"]]["topic_id"])
            self.assertEqual(row["section_id"], topics[row["topic_id"]]["section_id"])
        ids = sorted(r["id"] for r in assignments)
        self.assertEqual(ids, list(range(self.seed_sequences["question_topic_assignments"] + 1,
                                         self.seed_sequences["question_topic_assignments"] + 847)))
        new_topics = set(self.maps["curriculum_topics"].values())
        membership = [r for r in canonical_rows("topic_section_assignments") if r["topic_id"] in new_topics]
        self.assertEqual(len(membership), 53)
        self.assertEqual(len([r for r in canonical_rows("topic_classes") if r["topic_id"] in new_topics]), 53)

    def test_missing_identity_fails(self):
        broken = {**self.identities, "questions": self.identities["questions"][:-1]}
        with self.assertRaisesRegex(reconcile.ReconcileError, "no identity match"):
            self.build(broken)
        broken = {**self.identities, "curriculum_topics": [r for r in self.identities["curriculum_topics"]
                                                          if r["id"] != 181]}
        with self.assertRaises(reconcile.ReconcileError):
            self.build(broken)

    def test_duplicate_identity_fails(self):
        new_question = next(r for r in self.identities["questions"] if r["id"] == 900)
        duplicate = {**new_question, "id": 5000}
        with self.assertRaisesRegex(reconcile.ReconcileError, "duplicate natural key"):
            self.build({**self.identities, "questions": self.identities["questions"] + [duplicate]})
        same_id = {**self.identities, "questions": self.identities["questions"] + [dict(new_question)]}
        with self.assertRaisesRegex(reconcile.ReconcileError, "duplicate id"):
            self.build(same_id)

    def test_unexpected_conflicting_or_recycled_identity_fails(self):
        extra = {**next(r for r in self.identities["questions"] if r["id"] == 900),
                 "id": 5000, "source_number": 99999}
        with self.assertRaisesRegex(reconcile.ReconcileError, "no tracked source"):
            self.build({**self.identities, "questions": self.identities["questions"] + [extra]})
        conflicting = [dict(r, prompt="changed") if r["id"] == 900 else r for r in self.identities["questions"]]
        with self.assertRaisesRegex(reconcile.ReconcileError, "conflicts"):
            self.build({**self.identities, "questions": conflicting})
        recycled = [dict(r, id=840) if r["id"] == 838 else r for r in self.identities["questions"]]
        with self.assertRaises(reconcile.ReconcileError):
            self.build({**self.identities, "questions": recycled})
        low = [dict(r, id=1) if r["id"] == 838 else r for r in self.identities["questions"]]
        with self.assertRaises(reconcile.ReconcileError):
            self.build({**self.identities, "questions": low})

    @unittest.skipUnless(os.environ.get(IDENTITY_ENV), f"set {IDENTITY_ENV} to the local identity export")
    def test_reproduces_tracked_files_from_the_local_identity_export(self):
        out = self.temp_path("reconciled")
        reconcile.reconcile(SEED, Path(os.environ[IDENTITY_ENV]), out)
        for path in CANONICAL.glob("*.jsonl"):
            self.assertEqual(path.read_bytes(), (out / path.name).read_bytes(), path.name)
        self.assertEqual((CANONICAL / ct.MANIFEST_NAME).read_bytes(), (out / ct.MANIFEST_NAME).read_bytes())


class LoaderTest(CanonicalTestCase):
    def test_load_preserves_explicit_ids_and_relationships(self):
        conn = self.connect_rebuilt()
        self.assertEqual(conn.execute("SELECT MIN(id), MAX(id), COUNT(*) FROM questions").fetchone(), (1, 1683, 1678))
        self.assertEqual(conn.execute("SELECT question_id, option_letter FROM multiple_choice_options "
                                      "WHERE id = 6671").fetchone()[1], "D")
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM questions q JOIN curriculum_topics t ON t.id = q.topic_id "
                                      "JOIN curriculum_sections s ON s.id = t.section_id "
                                      "JOIN curricula c ON c.id = s.curriculum_id WHERE q.id >= 838").fetchone()[0], 846)
        self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_loader_writes_only_canonical_tables(self):
        path = self.temp_path("audited.db")
        load.migrated_empty_database(path)
        written = set()
        real_connect = sqlite3.connect

        def audited_connect(database, *args, **kwargs):
            conn = real_connect(database, *args, **kwargs)
            if str(database) == str(path):
                def authorizer(action, arg1, arg2, db_name, source):
                    if action in (sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE):
                        written.add(arg1)
                    return sqlite3.SQLITE_OK
                conn.set_authorizer(authorizer)
            return conn

        with patch.object(sqlite3, "connect", audited_connect):
            load.load_canonical(path)
        allowed = set(ct.tables_of(ct.CANONICAL)) | {"sqlite_sequence"}
        self.assertEqual(written - allowed, set())
        self.assertTrue(written & allowed)

    def test_second_load_is_idempotent_and_conflicts_roll_back(self):
        path = self.temp_path("reload.db")
        shutil.copyfile(self.rebuilt, path)
        self.assertEqual(sum(load.load_canonical(path).values()), 0)
        with closing(sqlite3.connect(path)) as conn:
            conn.execute("UPDATE questions SET prompt = 'edited' WHERE id = 838")
            conn.execute("DELETE FROM multiple_choice_options WHERE id = 6671")
            conn.commit()
        with self.assertRaisesRegex(ct.CanonicalError, "already exists with different content"):
            load.load_canonical(path)
        with closing(sqlite3.connect(path)) as conn:  # nothing from the failed load was kept
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM multiple_choice_options WHERE id = 6671").fetchone()[0], 0)

    def test_unique_clash_with_other_ids_fails(self):
        path = self.temp_path("clash.db")
        load.migrated_empty_database(path)
        with closing(sqlite3.connect(path)) as conn:
            conn.execute("INSERT INTO curricula (id, grade, subject, education_type, title_bg) "
                         "SELECT 99, grade, subject, education_type, title_bg FROM (SELECT 8 AS grade, 'physics' "
                         "AS subject, 'x' AS education_type, 'y' AS title_bg)")
            conn.execute("INSERT INTO curricula (id, grade, subject, education_type, title_bg) "
                         "VALUES (98, 9, 'Информационни технологии', 'unused', 'z')")
            conn.commit()
        row = next(r for r in canonical_rows("curricula") if r["subject"] == "physics" and r["grade"] == 8)
        with closing(sqlite3.connect(path)) as conn:
            conn.execute("UPDATE curricula SET education_type = ? WHERE id = 99", (row["education_type"],))
            conn.commit()
        with self.assertRaises((ct.CanonicalError, sqlite3.IntegrityError)):
            load.load_canonical(path)

    def test_loader_refuses_a_stale_schema_and_inconsistent_data(self):
        path = self.temp_path("stale.db")
        sqlite3.connect(path).close()
        with self.assertRaises(ct.CanonicalError):
            load.load_canonical(path)
        broken = self.canonical_copy()
        with open(broken / "questions.jsonl", "a", encoding="utf-8", newline="\n") as handle:
            handle.write("{}\n")
        target = self.temp_path("target.db")
        load.migrated_empty_database(target)
        with self.assertRaises(ct.CanonicalError):
            load.load_canonical(target, broken)

    def test_sequences_are_set_and_never_lowered(self):
        conn = self.connect_rebuilt()
        for table, entry in self.manifest["tables"].items():
            if entry["autoincrement"]:
                self.assertEqual(ct.stored_sequence(conn, table), entry["sqlite_sequence"], table)
        path = self.temp_path("higher.db")
        load.migrated_empty_database(path)
        with closing(sqlite3.connect(path)) as target:
            target.execute("INSERT INTO sqlite_sequence (name, seq) VALUES ('questions', 5000)")
            target.commit()
        load.load_canonical(path)
        with closing(sqlite3.connect(path)) as target:
            self.assertEqual(ct.stored_sequence(target, "questions"), 5000)


class ParityTest(CanonicalTestCase):
    def test_rebuild_matches_canonical_content(self):
        self.assertEqual(parity.compare_to_canonical(self.connect_rebuilt()), [])

    def test_schema_matches_a_freshly_migrated_database(self):
        self.assertEqual(parity.schema_parity_problems(self.connect_rebuilt()), [])

    def test_parity_reports_content_differences_with_ids(self):
        path = self.temp_path("drift.db")
        shutil.copyfile(self.rebuilt, path)
        with closing(sqlite3.connect(path)) as conn:
            conn.execute("UPDATE questions SET prompt = prompt || '!' WHERE id = 838")
            conn.execute("DELETE FROM multiple_choice_options WHERE id = 6671")
            conn.execute("UPDATE sqlite_sequence SET seq = seq + 1 WHERE name = 'exams'")
            problems = parity.compare_to_canonical(conn)
        text = "\n".join(problems)
        self.assertIn("questions: normalized hash differs", text)
        self.assertIn("multiple_choice_options: 6274 rows", text)
        self.assertIn("exams: sqlite_sequence", text)
        self.assertIn("(838,)", text)

    def test_runtime_rows_in_a_rebuild_are_reported(self):
        path = self.temp_path("runtime.db")
        shutil.copyfile(self.rebuilt, path)
        with closing(sqlite3.connect(path)) as conn:
            conn.execute("INSERT INTO scrape_log (source, url, status) VALUES ('s', 'u', 'ok')")
            conn.execute("INSERT INTO quiz_assignments (section_id, title_bg, question_count) "
                         "VALUES (1, 'leak', 1)")
            problems = parity.compare_to_canonical(conn)
        text = "\n".join(problems)
        self.assertIn("scrape_log: 1 rows", text)
        self.assertIn("quiz_assignments: 1 rows", text)

    def test_foreign_key_violations_are_reported(self):
        path = self.temp_path("fk.db")
        shutil.copyfile(self.rebuilt, path)
        with closing(sqlite3.connect(path)) as conn:
            conn.execute("PRAGMA foreign_keys = OFF")
            conn.execute("UPDATE multiple_choice_options SET question_id = 999999 "
                         "WHERE id = (SELECT MIN(id) FROM multiple_choice_options)")
            conn.commit()
            self.assertTrue(any("foreign key" in p for p in parity.compare_to_canonical(conn)))


class ReferenceTest(CanonicalTestCase):
    def test_non_fk_references_in_canonical_rows_resolve(self):
        self.assertEqual(parity.embedded_reference_problems(self.connect_rebuilt()), [])

    def test_historical_runtime_references_resolve_against_canonical_ids(self):
        path = self.seed_copy()
        ids = parity.canonical_question_ids()
        with closing(sqlite3.connect(path)) as conn:
            self.assertGreater(conn.execute("SELECT COUNT(*) FROM quiz_attempts").fetchone()[0], 0)
            self.assertEqual(parity.runtime_reference_problems(conn, ids), [])
            conn.execute("UPDATE quiz_attempts SET question_ids_json = '[1, 999999]' WHERE id = "
                         "(SELECT MIN(id) FROM quiz_attempts)")
            conn.execute("UPDATE generated_exams SET question_ids = '[999998]' WHERE id = "
                         "(SELECT MIN(id) FROM generated_exams)")
            text = "\n".join(parity.runtime_reference_problems(conn, ids))
        self.assertIn("quiz_attempts.question_ids_json", text)
        self.assertIn("generated_exams.question_ids", text)

    def test_confirmed_dzi_image_mapping_ids_resolve(self):
        ids = parity.canonical_question_ids()
        self.assertEqual(parity.image_mapping_problems(MAPPINGS, ids), [])
        bogus = self.temp_path("mappings.json")
        bogus.write_text(json.dumps({"mappings": [{"db_question_id": 999999}]}), encoding="utf-8")
        self.assertEqual(len(parity.image_mapping_problems(bogus, ids)), 1)

    def test_tracked_vault_question_ids_resolve(self):
        ids = parity.canonical_question_ids()
        self.assertEqual(parity.vault_reference_problems(ROOT / "vault", ids), [])
        notes = self.temp_path("vault")
        notes.mkdir()
        (notes / "note.md").write_text("---\nquestion_ids: [1, 999999]\n---\n", encoding="utf-8")
        self.assertEqual(len(parity.vault_reference_problems(notes, ids)), 1)

    def test_dangling_topic_and_owner_references_are_reported(self):
        path = self.temp_path("dangling.db")
        shutil.copyfile(self.rebuilt, path)
        with closing(sqlite3.connect(path)) as conn:
            conn.execute("UPDATE questions SET topic_id = 999999 WHERE id = 838")
            conn.execute("UPDATE asset_links SET owner_id = 999999 WHERE id = 1")
            text = "\n".join(parity.embedded_reference_problems(conn))
        self.assertIn("questions.topic_id", text)
        self.assertIn("asset_links", text)


class ManifestTest(CanonicalTestCase):
    def test_tracked_directory_is_consistent(self):
        self.assertEqual(export.check_directory(CANONICAL), [])
        self.assertEqual(self.manifest["schema"]["migration_level"],
                         sorted(p.name for p in (ROOT / "web" / "migrations").glob("*.sql"))[-1])
        self.assertEqual(self.manifest["sources"]["legacy_seed"]["sha256"], reconcile.SEED_SHA256)
        self.assertEqual(self.manifest["sources"]["local_identity"]["source_database_sha256"],
                         reconcile.LOCAL_DB_SHA256)
        self.assertEqual(self.manifest["sources"]["excluded_seed_rows"]["quiz_attempts"], 4)

    def test_manifest_generation_fails_on_inconsistency(self):
        cases = {}
        missing = self.canonical_copy()
        (missing / "questions.jsonl").unlink()
        cases["missing"] = (missing, "canonical file is missing")
        extra = self.canonical_copy()
        (extra / "quiz_attempts.jsonl").write_text("", encoding="utf-8")
        cases["extra"] = (extra, "unexpected file")
        tampered = self.canonical_copy()
        lines = (tampered / "curricula.jsonl").read_text(encoding="utf-8").split("\n")
        (tampered / "curricula.jsonl").write_text("\n".join(lines[:-2]) + "\n", encoding="utf-8")
        cases["tampered"] = (tampered, "rows, manifest says")
        for name, mutate, expected in (
            ("classification", lambda m: m["classification"]["runtime"].append("exams"), "classification"),
            ("level", lambda m: m["schema"].update(migration_level="009_x.sql"), "migration level"),
            ("sequence", lambda m: m["tables"]["questions"].update(sqlite_sequence=5), "below max id"),
            ("max_id", lambda m: m["tables"]["questions"].update(max_id=5), "max_id"),
            ("hash", lambda m: m["tables"]["questions"].update(sha256="0" * 64), "sha256 differs"),
        ):
            directory = self.canonical_copy()
            manifest = export.load_manifest(directory)
            mutate(manifest)
            (directory / ct.MANIFEST_NAME).write_text(export.manifest_text(manifest), encoding="utf-8")
            cases[name] = (directory, expected)
        for name, (directory, expected) in cases.items():
            problems = "\n".join(export.check_directory(directory))
            self.assertIn(expected, problems, name)

    def test_non_normalized_files_are_rejected(self):
        directory = self.canonical_copy()
        path = directory / "curricula.jsonl"
        path.write_bytes(path.read_bytes().replace(b"\n", b"\r\n"))
        self.assertTrue(any("carriage return" in p for p in export.check_directory(directory)))
        path.write_bytes(path.read_bytes().replace(b"\r\n", b"\n").rstrip(b"\n"))
        self.assertTrue(any("final newline" in p for p in export.check_directory(directory)))


class UpdateSafetyTest(CanonicalTestCase):
    def edit_rows(self, directory, table, transform):
        path = directory / f"{table}.jsonl"
        rows = transform(rows_of(path))
        path.write_text("".join(ct.serialize_row(list(r), r.values()) + "\n" for r in rows), encoding="utf-8")

    def test_identical_export_is_safe(self):
        self.assertEqual(export.identity_problems(CANONICAL, CANONICAL), [])

    def test_additions_are_allowed(self):
        new = self.canonical_copy()
        self.edit_rows(new, "questions", lambda rows: rows + [dict(rows[-1], id=2000, source_number=99999)])
        manifest = export.load_manifest(new)
        manifest["tables"]["questions"]["sqlite_sequence"] = 2000
        (new / ct.MANIFEST_NAME).write_text(export.manifest_text(manifest), encoding="utf-8")
        self.assertEqual(export.identity_problems(CANONICAL, new), [])

    def test_removed_rows_changed_ids_and_letters_are_reported(self):
        removed = self.canonical_copy()
        self.edit_rows(removed, "multiple_choice_options", lambda rows: [r for r in rows if r["id"] != 6671])
        text = "\n".join(export.identity_problems(CANONICAL, removed))
        self.assertIn("multiple_choice_options: row count dropped", text)
        self.assertIn("rows removed", text)

        swapped = self.canonical_copy()

        def swap(rows):
            first, second = next(r for r in rows if r["id"] == 838), next(r for r in rows if r["id"] == 839)
            first["source_number"], second["source_number"] = second["source_number"], first["source_number"]
            return rows
        self.edit_rows(swapped, "questions", swap)
        text = "\n".join(export.identity_problems(CANONICAL, swapped))
        self.assertIn("natural keys map to a different id", text)
        self.assertIn("reassigned to a different natural key", text)

        letters = self.canonical_copy()
        self.edit_rows(letters, "multiple_choice_options",
                       lambda rows: [dict(r, option_letter="E") if r["id"] == 6671 else r for r in rows])
        text = "\n".join(export.identity_problems(CANONICAL, letters))
        self.assertIn("reassigned to a different natural key", text)

    def test_runtime_tables_and_lower_sequences_are_reported(self):
        leaked = self.canonical_copy()
        (leaked / "quiz_attempts.jsonl").write_text("", encoding="utf-8")
        self.assertIn("quiz_attempts is included", "\n".join(export.identity_problems(CANONICAL, leaked)))
        lower = self.canonical_copy()
        manifest = export.load_manifest(lower)
        manifest["tables"]["questions"]["sqlite_sequence"] = 1000
        (lower / ct.MANIFEST_NAME).write_text(export.manifest_text(manifest), encoding="utf-8")
        self.assertIn("ids would be recycled", "\n".join(export.identity_problems(CANONICAL, lower)))

    def test_replace_requires_the_explicit_path_and_keeps_data_on_refusal(self):
        target = self.canonical_copy()
        before = {p.name: p.read_bytes() for p in target.iterdir()}
        with closing(sqlite3.connect(self.rebuilt)) as conn:
            export.replace_canonical(conn, target)  # same content: a no-op replacement
        self.assertEqual(before, {p.name: p.read_bytes() for p in target.iterdir()})
        path = self.temp_path("short.db")
        shutil.copyfile(self.rebuilt, path)
        with closing(sqlite3.connect(path)) as conn:
            conn.execute("DELETE FROM multiple_choice_options WHERE question_id = 1683")
            conn.execute("DELETE FROM questions WHERE id = 1683")
            conn.commit()
            with self.assertRaisesRegex(ct.CanonicalError, "refusing to replace"):
                export.replace_canonical(conn, target)
        self.assertEqual(before, {p.name: p.read_bytes() for p in target.iterdir()})

    def test_command_line_never_writes_the_tracked_directory_by_default(self):
        tracked = {p.name: p.read_bytes() for p in CANONICAL.iterdir()}
        with redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                export.main(["--db", str(self.rebuilt), "--out", str(CANONICAL)])
            with self.assertRaises(SystemExit):
                export.main(["--db", str(self.rebuilt)])
            with self.assertRaises(SystemExit):
                export.main(["--db", str(self.rebuilt), "--out", str(self.work), "--replace-canonical"])
        out = self.temp_path("cli")
        self.assertEqual(export.main(["--db", str(self.rebuilt), "--out", str(out)]), 0)
        self.assertEqual({p.name for p in out.iterdir()}, set(tracked))
        self.assertEqual(tracked, {p.name: p.read_bytes() for p in CANONICAL.iterdir()})
        self.assertEqual(export.main(["--check"]), 0)


class SeedSafetyTest(CanonicalTestCase):
    def test_seed_is_unchanged_and_has_no_sidecars(self):
        self.assertEqual(sha256(SEED), self.seed_sha_before)
        for suffix in ("-wal", "-shm", "-journal"):
            self.assertFalse(Path(str(SEED) + suffix).exists())

    def test_seed_is_still_tracked(self):
        self.assertTrue(SEED.is_file(), "G1 must not untrack the seed; that is H")


if __name__ == "__main__":
    unittest.main()
