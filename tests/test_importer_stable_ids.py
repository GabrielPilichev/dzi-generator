"""G2: importers keep stable ids; new rows get safe ids; explicit-id conflicts fail and roll back.

Everything runs on temporary databases built from migrations + canonical content (or from
migrations alone). The real tracked structure files and reviewed batches are re-applied to the
canonical database to prove they keep every existing identity.
"""

import glob
import hashlib
import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from src import canonical_export as export
from src import canonical_load as load
from src import canonical_tables as ct
from src import import_dzi_questions_json as dzi_import
from src import stable_ids
from src.import_reviewed_subject_questions import import_batch
from src.import_subject_structure import StructureImportError, import_structure
from tests.support import CANONICAL, IT_ONLY, ROOT, build_test_database

CANONICAL_DIR = ROOT / "data" / "canonical"
MANIFEST = export.load_manifest(CANONICAL_DIR)


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def canonical_digests(path):
    return load.canonical_digests(path)


def chemistry_structure():
    prefix = "chemistry-g8-"
    return {
        "format_version": 1, "subject": "chemistry", "grade": 8,
        "curriculum": {"education_type": "synthetic-stable-id-test", "title": "Synthetic chemistry 8"},
        "sections": [
            {"slug": prefix + "one", "title": "Synthetic one", "topics": [
                {"slug": prefix + "topic-a", "title": "Synthetic topic A"},
                {"slug": prefix + "topic-b", "title": "Synthetic topic B"}]},
            {"slug": prefix + "two", "title": "Synthetic two", "topics": []},
        ],
    }


def chemistry_batch(number=1, source="reviewed:chemistry:stable-id-test-v1"):
    return {
        "format_version": 1, "subject": "chemistry", "grade": 8, "source": source,
        "curriculum": {"subject": "chemistry", "grade": 8, "education_type": "synthetic-stable-id-test",
                       "title": "Synthetic chemistry 8"},
        "section": {"slug": "chemistry-g8-one", "title": "Synthetic one", "grade": 8},
        "topic": {"slug": "chemistry-g8-topic-a", "title": "Synthetic topic A", "grade": 8},
        "questions": [{"number": n, "subject": "chemistry", "grade": 8, "topic_slug": "chemistry-g8-topic-a",
                       "type": "multiple_choice", "prompt": f"Synthetic question {n}?",
                       "options": {"A": "One", "B": "Two", "C": "Three", "D": "Four"}, "correct": "B"}
                      for n in range(1, number + 1)],
    }


class TemporaryDatabaseTestCase(unittest.TestCase):
    profile = CANONICAL

    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="learnpilot-ids-test-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.db = build_test_database(self.root / "work.db", self.profile)

    def connect(self):
        conn = sqlite3.connect(self.db)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        self.addCleanup(conn.close)
        return conn


# --------------------------------------------------------------------------- stable_ids helpers

class AllocationTest(unittest.TestCase):
    def conn_with(self, maximum=None, sequence=None):
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        conn.execute("CREATE TABLE questions (id INTEGER PRIMARY KEY AUTOINCREMENT, source_exam TEXT, source_number INTEGER)")
        if maximum:
            conn.execute("INSERT INTO questions (id, source_exam, source_number) VALUES (?, 'x', 1)", (maximum,))
        if sequence:
            conn.execute("UPDATE sqlite_sequence SET seq = ? WHERE name = 'questions'", (sequence,))
        return conn

    def test_new_ids_start_above_the_canonical_maximum_and_sequence(self):
        floor = max(MANIFEST["tables"]["questions"]["max_id"], MANIFEST["tables"]["questions"]["sqlite_sequence"])
        self.assertEqual(stable_ids.allocate_id(self.conn_with(), "questions"), floor + 1)
        self.assertEqual(stable_ids.allocate_id(self.conn_with(maximum=5), "questions"), floor + 1)

    def test_new_ids_start_above_the_target_maximum_and_sequence(self):
        floor = MANIFEST["tables"]["questions"]["sqlite_sequence"]
        self.assertEqual(stable_ids.allocate_id(self.conn_with(maximum=floor + 50), "questions"), floor + 51)
        self.assertEqual(stable_ids.allocate_id(self.conn_with(maximum=3, sequence=floor + 90), "questions"), floor + 91)

    def test_a_deleted_historical_id_is_never_handed_out_again(self):
        conn = self.conn_with()
        floor = stable_ids.allocate_id(conn, "questions")
        conn.execute("INSERT INTO questions (id, source_exam, source_number) VALUES (?, 'x', 1)", (floor,))
        conn.execute("DELETE FROM questions")
        self.assertEqual(stable_ids.allocate_id(conn, "questions"), floor + 1)

    def test_without_a_canonical_directory_the_target_alone_decides(self):
        empty = Path(tempfile.mkdtemp(prefix="learnpilot-no-canonical-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(empty, ignore_errors=True))
        self.assertEqual(stable_ids.allocate_id(self.conn_with(maximum=7), "questions", empty), 8)

    def test_claim_rules(self):
        conn = self.conn_with(maximum=3)
        high = MANIFEST["tables"]["questions"]["sqlite_sequence"]
        self.assertEqual(stable_ids.claim_id(conn, "questions", high + 1000), high + 1000)
        for bad in (0, -1, "7", 7.0, True, None):
            with self.subTest(bad=bad), self.assertRaises(stable_ids.IdConflict):
                stable_ids.claim_id(conn, "questions", bad)
        with self.assertRaisesRegex(stable_ids.IdConflict, "already exists"):
            stable_ids.claim_id(conn, "questions", 3)

    def test_historical_id_only_restores_its_own_canonical_row(self):
        with closing(sqlite3.connect(":memory:")) as conn:
            conn.execute("CREATE TABLE questions (id INTEGER PRIMARY KEY AUTOINCREMENT, source_exam TEXT, source_number INTEGER)")
            first = next(ct.read_table_rows(CANONICAL_DIR / "questions.jsonl"))
            key = {"source_exam": first["source_exam"], "source_number": first["source_number"]}
            self.assertEqual(stable_ids.claim_id(conn, "questions", first["id"], key), first["id"])
            with self.assertRaisesRegex(stable_ids.IdConflict, "different canonical content"):
                stable_ids.claim_id(conn, "questions", first["id"], {"source_exam": "other", "source_number": 1})
            with self.assertRaisesRegex(stable_ids.IdConflict, "different canonical content"):
                stable_ids.claim_id(conn, "questions", first["id"])
            ids = {row["id"] for row in ct.read_table_rows(CANONICAL_DIR / "questions.jsonl")}
            gap = next(i for i in range(1, MANIFEST["tables"]["questions"]["max_id"]) if i not in ids)
            with self.assertRaisesRegex(stable_ids.IdConflict, "never recycled"):
                stable_ids.claim_id(conn, "questions", gap, {"source_exam": "new", "source_number": 1})


# --------------------------------------------------------------------------- subject structure importer

class StructureImporterIdentityTest(TemporaryDatabaseTestCase):
    def test_tracked_structure_files_keep_every_canonical_identity(self):
        before = canonical_digests(self.db)
        files = sorted(glob.glob(str(ROOT / "data" / "subject_structures" / "*" / "*.json")))
        self.assertEqual(len(files), 6)
        for file in files:
            structure = json.loads(Path(file).read_text(encoding="utf-8"))
            self.assertFalse(import_structure(self.db, structure)["has_changes"], file)
            self.assertFalse(import_structure(self.db, structure, apply=True)["has_changes"], file)
        self.assertEqual(canonical_digests(self.db), before)

    def test_reimport_after_apply_keeps_the_ids_it_assigned(self):
        structure = chemistry_structure()
        import_structure(self.db, structure, apply=True)
        with self.connect() as conn:
            first = [tuple(r) for r in conn.execute(
                "SELECT id, section_slug FROM curriculum_sections WHERE section_slug LIKE 'chemistry-g8-%' ORDER BY id")]
            curriculum = conn.execute("SELECT id FROM curricula WHERE subject = 'chemistry'").fetchone()[0]
        self.assertFalse(import_structure(self.db, structure, apply=True)["has_changes"])
        with self.connect() as conn:
            self.assertEqual(first, [tuple(r) for r in conn.execute(
                "SELECT id, section_slug FROM curriculum_sections WHERE section_slug LIKE 'chemistry-g8-%' ORDER BY id")])
            self.assertEqual(curriculum, conn.execute("SELECT id FROM curricula WHERE subject = 'chemistry'").fetchone()[0])

    def test_new_rows_take_ids_above_the_canonical_and_target_maximum(self):
        import_structure(self.db, chemistry_structure(), apply=True)
        with self.connect() as conn:
            for table, where in (("curricula", "subject = 'chemistry'"),
                                 ("curriculum_sections", "section_slug LIKE 'chemistry-g8-%'"),
                                 ("curriculum_topics", "topic_slug LIKE 'chemistry-g8-%'")):
                floor = max(MANIFEST["tables"][table]["max_id"], MANIFEST["tables"][table]["sqlite_sequence"])
                ids = [r[0] for r in conn.execute(f"SELECT id FROM {table} WHERE {where} ORDER BY id")]
                self.assertTrue(ids and all(i > floor for i in ids), (table, ids, floor))
            self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_a_database_with_low_sequences_still_cannot_collide_with_canonical_ids(self):
        bare = self.root / "bare.db"
        load.migrated_empty_database(bare)             # migrations only: no canonical rows, no sequences
        import_structure(bare, chemistry_structure(), apply=True)
        with closing(sqlite3.connect(bare)) as conn:
            curriculum = conn.execute("SELECT id FROM curricula").fetchone()[0]
        self.assertGreater(curriculum, MANIFEST["tables"]["curricula"]["max_id"])

    def test_explicit_ids_are_used_when_they_do_not_conflict(self):
        explicit = {"curricula": 500, "sections": {"chemistry-g8-one": 600, "chemistry-g8-two": 601},
                    "topics": {"chemistry-g8-topic-a": 700, "chemistry-g8-topic-b": 701}}
        import_structure(self.db, chemistry_structure(), apply=True, explicit_ids=explicit)
        with self.connect() as conn:
            self.assertEqual(conn.execute("SELECT id FROM curricula WHERE subject = 'chemistry'").fetchone()[0], 500)
            self.assertEqual([tuple(r) for r in conn.execute(
                "SELECT section_slug, id FROM curriculum_sections WHERE section_slug LIKE 'chemistry-%' ORDER BY id")],
                [("chemistry-g8-one", 600), ("chemistry-g8-two", 601)])
            self.assertEqual([r[0] for r in conn.execute(
                "SELECT id FROM curriculum_topics WHERE topic_slug LIKE 'chemistry-%' ORDER BY id")], [700, 701])
            self.assertEqual(conn.execute("SELECT seq FROM sqlite_sequence WHERE name = 'curricula'").fetchone()[0], 500)

    def test_explicit_id_conflict_fails_and_rolls_back_everything(self):
        before = sha256(self.db)
        existing_curriculum = {"curricula": 1}
        with self.assertRaisesRegex(StructureImportError, "already exists"):
            import_structure(self.db, chemistry_structure(), apply=True, explicit_ids=existing_curriculum)
        # A conflict discovered after earlier rows were written (the 2nd section) rolls them all back.
        late = {"curricula": 500, "sections": {"chemistry-g8-one": 600, "chemistry-g8-two": 1}}
        with self.assertRaisesRegex(StructureImportError, "already exists"):
            import_structure(self.db, chemistry_structure(), apply=True, explicit_ids=late)
        self.assertEqual(sha256(self.db), before)
        with self.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM curricula WHERE subject = 'chemistry'").fetchone()[0], 0)

    def test_dry_run_reports_explicit_id_conflicts_too(self):
        with self.assertRaises(StructureImportError):
            import_structure(self.db, chemistry_structure(), explicit_ids={"curricula": 1})

    def test_recycling_a_historical_id_for_different_content_fails(self):
        with self.connect() as conn:
            ids = {r[0] for r in conn.execute("SELECT id FROM curriculum_topics")}
        gap = next(i for i in range(1, MANIFEST["tables"]["curriculum_topics"]["max_id"]) if i not in ids)
        before = sha256(self.db)
        with self.assertRaisesRegex(StructureImportError, "never recycled"):
            import_structure(self.db, chemistry_structure(), apply=True,
                             explicit_ids={"topics": {"chemistry-g8-topic-a": gap}})
        self.assertEqual(sha256(self.db), before)

    def test_requesting_a_different_id_for_an_existing_row_never_remaps(self):
        structure = json.loads((ROOT / "data/subject_structures/physics/grade-8.json").read_text(encoding="utf-8"))
        with self.connect() as conn:
            curriculum = conn.execute("SELECT id FROM curricula WHERE subject = 'physics' AND grade = 8").fetchone()[0]
        before = sha256(self.db)
        with self.assertRaisesRegex(StructureImportError, "differs from the requested id"):
            import_structure(self.db, structure, apply=True, explicit_ids={"curricula": curriculum + 100})
        self.assertEqual(sha256(self.db), before)
        self.assertFalse(import_structure(self.db, structure, apply=True,
                                          explicit_ids={"curricula": curriculum})["has_changes"])


class StructureImporterRestoresCanonicalIdsTest(TemporaryDatabaseTestCase):
    profile = IT_ONLY

    def test_a_canonical_identity_can_be_restored_with_its_own_explicit_ids_only(self):
        structure = json.loads((ROOT / "data/subject_structures/physics/grade-8.json").read_text(encoding="utf-8"))
        curricula = {(r["subject"], r["grade"], r["education_type"]): r["id"]
                     for r in ct.read_table_rows(CANONICAL_DIR / "curricula.jsonl")}
        sections = {r["section_slug"]: r["id"] for r in ct.read_table_rows(CANONICAL_DIR / "curriculum_sections.jsonl")}
        topics = {r["topic_slug"]: r["id"] for r in ct.read_table_rows(CANONICAL_DIR / "curriculum_topics.jsonl")}
        wanted_sections = [s["slug"] for s in structure["sections"]]
        wanted_topics = [t["slug"] for s in structure["sections"] for t in s["topics"]]
        explicit = {"curricula": curricula[("physics", 8, structure["curriculum"]["education_type"])],
                    "sections": {slug: sections[slug] for slug in wanted_sections},
                    "topics": {slug: topics[slug] for slug in wanted_topics}}
        # The IT-only database dropped those rows but remembers their ids as used (sequence), so they
        # are only acceptable for exactly this canonical content.
        import_structure(self.db, structure, apply=True, explicit_ids=explicit)
        with self.connect() as conn:
            self.assertEqual({r["section_slug"]: r["id"] for r in conn.execute(
                "SELECT section_slug, id FROM curriculum_sections WHERE section_slug LIKE 'physics-g8-%'")},
                {slug: sections[slug] for slug in wanted_sections})
            self.assertEqual({r["topic_slug"]: r["id"] for r in conn.execute(
                "SELECT topic_slug, id FROM curriculum_topics WHERE topic_slug LIKE 'physics-g8-%'")},
                {slug: topics[slug] for slug in wanted_topics})
            self.assertEqual(conn.execute("SELECT id FROM curricula WHERE subject = 'physics' AND grade = 8").fetchone()[0],
                             explicit["curricula"])

    def test_the_same_historical_ids_are_refused_for_other_content(self):
        structure = chemistry_structure()
        physics_curriculum = next(r["id"] for r in ct.read_table_rows(CANONICAL_DIR / "curricula.jsonl")
                                  if r["subject"] == "physics")
        before = sha256(self.db)
        with self.assertRaisesRegex(StructureImportError, "different canonical content"):
            import_structure(self.db, structure, apply=True, explicit_ids={"curricula": physics_curriculum})
        self.assertEqual(sha256(self.db), before)


# --------------------------------------------------------------------------- reviewed question importer

class ReviewedQuestionImporterIdentityTest(TemporaryDatabaseTestCase):
    def test_every_tracked_reviewed_batch_reuses_the_canonical_rows(self):
        before = canonical_digests(self.db)
        batches = [json.loads(Path(f).read_text(encoding="utf-8"))
                   for f in sorted(glob.glob(str(ROOT / "data/import_batches/subjects/*/*/*.json")))]
        batches = [b for b in batches if isinstance(b, dict) and "section" in b]
        self.assertGreater(len(batches), 30)
        for batch in batches:
            summary = import_batch(self.db, batch, apply=True)
            self.assertEqual(sum(summary[key]["inserted"] for key in summary), 0)
        self.assertEqual(canonical_digests(self.db), before)

    def test_new_questions_and_options_take_safe_ids_and_keep_them_on_reimport(self):
        batch = chemistry_batch(number=2)
        self.assertEqual(import_batch(self.db, batch, apply=True)["questions"], {"inserted": 2, "reused": 0})
        with self.connect() as conn:
            rows = [tuple(r) for r in conn.execute(
                "SELECT id, source_number FROM questions WHERE source_exam = ? ORDER BY source_number",
                (batch["source"],))]
            options = [tuple(r) for r in conn.execute(
                "SELECT id, option_letter FROM multiple_choice_options WHERE question_id IN (?, ?) ORDER BY id",
                (rows[0][0], rows[1][0]))]
        floor = max(MANIFEST["tables"]["questions"]["max_id"], MANIFEST["tables"]["questions"]["sqlite_sequence"])
        self.assertTrue(all(i > floor for i, _ in rows), rows)
        option_floor = MANIFEST["tables"]["multiple_choice_options"]["sqlite_sequence"]
        self.assertTrue(all(i > option_floor for i, _ in options), options)
        self.assertEqual(import_batch(self.db, batch, apply=True)["questions"], {"inserted": 0, "reused": 2})
        with self.connect() as conn:
            self.assertEqual(rows, [tuple(r) for r in conn.execute(
                "SELECT id, source_number FROM questions WHERE source_exam = ? ORDER BY source_number",
                (batch["source"],))])

    def test_explicit_question_id_is_used_when_free(self):
        batch = chemistry_batch()
        free = MANIFEST["tables"]["questions"]["sqlite_sequence"] + 100
        import_batch(self.db, batch, apply=True, explicit_ids={"questions": {1: free}})
        with self.connect() as conn:
            self.assertEqual(conn.execute("SELECT id FROM questions WHERE source_exam = ?", (batch["source"],)).fetchone()[0], free)

    def test_explicit_id_conflicts_fail_and_roll_back_the_whole_batch(self):
        before = sha256(self.db)
        batch = chemistry_batch(number=2)
        free = MANIFEST["tables"]["questions"]["sqlite_sequence"] + 100
        existing = next(r["id"] for r in ct.read_table_rows(CANONICAL_DIR / "questions.jsonl"))
        with self.assertRaisesRegex(stable_ids.IdConflict, "already exists"):
            # question 1 is fine, question 2 collides: question 1 and the structure must roll back.
            import_batch(self.db, batch, apply=True, explicit_ids={"questions": {1: free, 2: existing}})
        self.assertEqual(sha256(self.db), before)
        with self.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM curricula WHERE subject = 'chemistry'").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM questions WHERE source_exam = ?",
                                          (batch["source"],)).fetchone()[0], 0)

    def test_requesting_another_id_for_an_existing_question_never_remaps(self):
        batch = chemistry_batch()
        import_batch(self.db, batch, apply=True)
        with self.connect() as conn:
            stored = conn.execute("SELECT id FROM questions WHERE source_exam = ?", (batch["source"],)).fetchone()[0]
        before = sha256(self.db)
        with self.assertRaisesRegex(stable_ids.IdConflict, "differs from the requested id"):
            import_batch(self.db, batch, apply=True, explicit_ids={"questions": {1: stored + 1000}})
        self.assertEqual(sha256(self.db), before)
        self.assertEqual(import_batch(self.db, batch, apply=True, explicit_ids={"questions": {1: stored}})["questions"],
                         {"inserted": 0, "reused": 1})

    def test_recycled_question_id_is_refused(self):
        ids = {r["id"] for r in ct.read_table_rows(CANONICAL_DIR / "questions.jsonl")}
        gap = next(i for i in range(1, MANIFEST["tables"]["questions"]["max_id"]) if i not in ids)
        before = sha256(self.db)
        with self.assertRaisesRegex(stable_ids.IdConflict, "never recycled"):
            import_batch(self.db, chemistry_batch(), apply=True, explicit_ids={"questions": {1: gap}})
        self.assertEqual(sha256(self.db), before)


# --------------------------------------------------------------------------- DZI importer

def multi_subquestion_question(conn):
    """The lowest-id DZI question with at least three canonical subquestions."""
    return conn.execute(
        "SELECT question_id FROM fill_in_subquestions GROUP BY question_id HAVING COUNT(*) >= 3 "
        "ORDER BY question_id LIMIT 1").fetchone()[0]


def task_from_rows(conn, question_id):
    rows = conn.execute(
        "SELECT subquestion_number, subquestion_text, correct_answer, answer_alternatives, points "
        "FROM fill_in_subquestions WHERE question_id = ? ORDER BY subquestion_number", (question_id,)).fetchall()
    return {"prompt": "unused", "points": 3, "subquestions": [
        {"prompt": text, "answers": json.loads(answer), "points": points,
         **({"answer_alternatives": json.loads(alternatives)} if alternatives else {})}
        for _, text, answer, alternatives, points in rows]}


class DziSubquestionIdentityTest(TemporaryDatabaseTestCase):
    def setUp(self):
        super().setUp()
        self.conn = self.connect()
        self.question_id = multi_subquestion_question(self.conn)

    def rows(self):
        return [tuple(r) for r in self.conn.execute(
            "SELECT id, subquestion_number, subquestion_text, correct_answer, answer_alternatives, points "
            "FROM fill_in_subquestions WHERE question_id = ? ORDER BY subquestion_number", (self.question_id,))]

    def test_reimporting_the_same_task_changes_nothing(self):
        before = self.rows()
        task = task_from_rows(self.conn, self.question_id)
        self.assertEqual(len(before), 3)
        with self.conn:
            dzi_import.replace_fill_in_subquestions(self.conn, self.question_id, task)
        self.assertEqual(self.rows(), before)

    def test_matching_subquestion_retains_its_id_and_takes_the_new_values(self):
        before = self.rows()
        task = task_from_rows(self.conn, self.question_id)
        task["subquestions"][1]["prompt"] = "(2) corrected text"
        task["subquestions"][1]["answers"] = ["corrected"]
        with self.conn:
            dzi_import.replace_fill_in_subquestions(self.conn, self.question_id, task)
        after = self.rows()
        self.assertEqual([r[0] for r in after], [r[0] for r in before])
        self.assertEqual(after[1][2:4], ("(2) corrected text", '["corrected"]'))
        self.assertEqual((after[0], after[2]), (before[0], before[2]))

    def test_genuinely_new_subquestion_gets_a_new_safe_id(self):
        before = self.rows()
        task = task_from_rows(self.conn, self.question_id)
        task["subquestions"].append({"prompt": "(4) new", "answers": ["x"], "points": 1})
        with self.conn:
            dzi_import.replace_fill_in_subquestions(self.conn, self.question_id, task)
        after = self.rows()
        self.assertEqual([r[0] for r in after[:3]], [r[0] for r in before])
        new_id = after[3][0]
        floor = max(MANIFEST["tables"]["fill_in_subquestions"]["max_id"],
                    MANIFEST["tables"]["fill_in_subquestions"]["sqlite_sequence"])
        self.assertGreater(new_id, floor)
        self.assertEqual(after[3][1], 4)

    def test_stale_subquestion_is_deleted_deliberately_and_its_id_is_never_reused(self):
        before = self.rows()
        task = task_from_rows(self.conn, self.question_id)
        task["subquestions"] = task["subquestions"][:2]               # the payload no longer lists number 3
        with self.conn:
            dzi_import.replace_fill_in_subquestions(self.conn, self.question_id, task)
        shrunk = self.rows()
        self.assertEqual([r[0] for r in shrunk], [r[0] for r in before[:2]])   # 1 and 2 keep their ids
        stale_id = before[2][0]
        task = task_from_rows(self.conn, self.question_id)
        task["subquestions"].append({"prompt": "(3) again", "answers": ["y"], "points": 1})
        with self.conn:
            dzi_import.replace_fill_in_subquestions(self.conn, self.question_id, task)
        regrown = self.rows()
        self.assertEqual(regrown[2][1], 3)
        self.assertNotEqual(regrown[2][0], stale_id)                  # a retired id is not recycled
        self.assertGreater(regrown[2][0], stale_id)

    def test_single_subquestion_task_without_a_list_is_keyed_by_number_one(self):
        with self.conn:
            single = self.conn.execute(
                "SELECT question_id FROM fill_in_subquestions GROUP BY question_id HAVING COUNT(*) = 1 "
                "ORDER BY question_id LIMIT 1").fetchone()[0]
        before = [tuple(r) for r in self.conn.execute(
            "SELECT id, subquestion_number FROM fill_in_subquestions WHERE question_id = ?", (single,))]
        task = {"prompt": "changed prompt", "points": 2, "answers": ["a"]}
        with self.conn:
            dzi_import.replace_fill_in_subquestions(self.conn, single, task)
        self.assertEqual([tuple(r) for r in self.conn.execute(
            "SELECT id, subquestion_number FROM fill_in_subquestions WHERE question_id = ?", (single,))], before)
        self.assertEqual(self.conn.execute("SELECT subquestion_text, points FROM fill_in_subquestions WHERE id = ?",
                                           (before[0][0],)).fetchone()[:], ("changed prompt", 2))

    def test_failure_after_the_replacement_rolls_the_subquestions_back(self):
        before = self.rows()
        task = task_from_rows(self.conn, self.question_id)
        task["subquestions"] = task["subquestions"][:1]
        task["subquestions"][0]["prompt"] = "will be rolled back"
        with self.assertRaisesRegex(RuntimeError, "later step failed"):
            with self.conn:
                dzi_import.replace_fill_in_subquestions(self.conn, self.question_id, task)
                raise RuntimeError("later step failed")
        self.assertEqual(self.rows(), before)

    def test_foreign_keys_and_unique_numbers_hold_after_updates(self):
        task = task_from_rows(self.conn, self.question_id)
        task["subquestions"].append({"prompt": "(4)", "answers": ["z"], "points": 1})
        with self.conn:
            dzi_import.replace_fill_in_subquestions(self.conn, self.question_id, task)
        self.assertEqual(self.conn.execute("PRAGMA foreign_key_check").fetchall(), [])
        numbers = [r[1] for r in self.rows()]
        self.assertEqual(numbers, sorted(set(numbers)))


class DziOptionAndQuestionIdentityTest(TemporaryDatabaseTestCase):
    def setUp(self):
        super().setUp()
        self.conn = self.connect()
        self.question_id = self.conn.execute(
            "SELECT q.id FROM questions q WHERE q.exam_id IS NOT NULL AND q.question_type = 'multiple_choice' "
            "ORDER BY q.id LIMIT 1").fetchone()[0]

    def options(self):
        return [tuple(r) for r in self.conn.execute(
            "SELECT id, option_letter, option_text, is_correct FROM multiple_choice_options "
            "WHERE question_id = ? ORDER BY option_letter", (self.question_id,))]

    def task(self):
        return {"options": [{"letter": letter, "text": text, "is_correct": bool(correct)}
                            for _, letter, text, correct in self.options()]}

    def test_matching_options_keep_their_ids_and_new_text(self):
        before = self.options()
        task = self.task()
        task["options"][0]["text"] = "corrected option text"
        with self.conn:
            dzi_import.replace_multiple_choice_options(self.conn, self.question_id, task)
        after = self.options()
        self.assertEqual([r[0] for r in after], [r[0] for r in before])
        self.assertEqual(after[0][2], "corrected option text")
        self.assertEqual(after[1:], before[1:])

    def test_new_option_letter_gets_a_safe_id_and_a_dropped_letter_is_deleted(self):
        before = self.options()                                  # the DZI options use Cyrillic letters
        dropped = before[-1]
        task = self.task()
        task["options"] = task["options"][:-1] + [{"letter": "E", "text": "extra", "is_correct": False}]
        with self.conn:
            dzi_import.replace_multiple_choice_options(self.conn, self.question_id, task)
        after = {r[1]: r for r in self.options()}
        self.assertEqual(set(after), {r[1] for r in before[:-1]} | {"E"})
        for row in before[:-1]:
            self.assertEqual(after[row[1]][0], row[0])           # surviving letters keep their ids
        self.assertGreater(after["E"][0], MANIFEST["tables"]["multiple_choice_options"]["sqlite_sequence"])
        self.assertNotIn(dropped[0], [r[0] for r in after.values()])

    def test_existing_question_keeps_its_id_when_the_importer_updates_it(self):
        row = self.conn.execute("SELECT * FROM questions WHERE id = ?", (self.question_id,)).fetchone()
        exam = self.conn.execute("SELECT * FROM exams WHERE id = ?", (row["exam_id"],)).fetchone()
        task = {"task_number": row["source_number"], "task_kind": "multiple_choice", "points": row["points"],
                "prompt": row["prompt"] + " (edited)", "topic_slug": row["topic"]}
        with self.conn:
            question_id, action = dzi_import.upsert_question(
                self.conn, row["source_exam"], exam, task, row["topic_id"])
        self.assertEqual((question_id, action), (self.question_id, "updated"))

    def test_new_question_gets_an_id_above_the_canonical_and_target_maximum(self):
        row = self.conn.execute("SELECT * FROM questions WHERE id = ?", (self.question_id,)).fetchone()
        exam = self.conn.execute("SELECT * FROM exams WHERE id = ?", (row["exam_id"],)).fetchone()
        task = {"task_number": 99, "task_kind": "multiple_choice", "points": 1, "prompt": "new", "topic_slug": None}
        with self.conn:
            question_id, action = dzi_import.upsert_question(self.conn, row["source_exam"], exam, task, None)
        self.assertEqual(action, "inserted")
        self.assertGreater(question_id, MANIFEST["tables"]["questions"]["sqlite_sequence"])

    def test_failed_import_rolls_back_option_changes(self):
        before = self.options()
        task = self.task()
        task["options"][0]["text"] = "rolled back"
        with self.assertRaises(RuntimeError):
            with self.conn:
                dzi_import.replace_multiple_choice_options(self.conn, self.question_id, task)
                raise RuntimeError("boom")
        self.assertEqual(self.options(), before)


if __name__ == "__main__":
    unittest.main()
