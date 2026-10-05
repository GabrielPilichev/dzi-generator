"""Approved-candidate export and its hand-off to the existing reviewed importer."""

import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from src.export_approved_question_candidates import (
    ExportError, build_export, main, write_export,
)
from src.import_reviewed_subject_questions import import_batch, validate_batch
from src.question_candidates import CandidateDatasetError, validate_dataset
from tests.question_candidate_fixtures import (
    OTHER_TOPIC, RUN_ID, TOPIC, candidate, dataset, directory_state, review,
)
from tests.support import ROOT, copy_seed_database

BATCH_FILE = f"{RUN_ID}.synthetic-topic.json"
PROVENANCE_FILE = f"{RUN_ID}.provenance.json"


def mixed_dataset():
    return dataset(
        candidate(1, status="approved", difficulty="easy"),
        candidate(2, status="rejected"),
        candidate(3, status="approved", source_id="third-party-source",
                  transformation="original_from_research", explanation="Synthetic rationale."),
        candidate(4, status="rejected", topic=OTHER_TOPIC),
    )


class ExportTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="learnpilot-candidate-export-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.db_dir = self.root / "db"
        self.db_dir.mkdir()
        self.db_path = self.db_dir / "questions.db"
        copy_seed_database(ROOT / "data/questions.db", self.db_path)

    def test_only_approved_candidates_are_exported(self):
        export = build_export(mixed_dataset())
        self.assertEqual(sorted(export["files"]), [PROVENANCE_FILE, BATCH_FILE])
        batch = export["files"][BATCH_FILE]
        self.assertEqual(validate_batch(batch), "physics")
        self.assertEqual(batch["source"], f"reviewed:physics:{RUN_ID}.synthetic-topic")
        self.assertEqual([q["number"] for q in batch["questions"]], [1, 2])
        self.assertEqual([q["prompt"] for q in batch["questions"]],
                         [candidate(1)["prompt"], candidate(3)["prompt"]])
        self.assertEqual(export["report"]["exported_questions"], 2)
        self.assertEqual(
            export["report"]["not_exported"],
            [{"candidate_id": f"{RUN_ID}-002", "reason": "review status rejected"},
             {"candidate_id": f"{RUN_ID}-004", "reason": "review status rejected"}])

    def test_pending_and_needs_revision_never_slip_through(self):
        for status in ("pending", "needs_revision", "rejected"):
            with self.subTest(status=status), self.assertRaisesRegex(ExportError, "no approved"):
                build_export(dataset(candidate(1, status=status)))
        for status in ("pending", "needs_revision"):
            data = dataset(candidate(1, status="approved"), candidate(2, status=status))
            with self.subTest(status=status):
                with self.assertRaisesRegex(ExportError, "pending or needs_revision"):
                    build_export(data)
                export = build_export(data, allow_undecided=True)
                self.assertEqual(len(export["files"][BATCH_FILE]["questions"]), 1)
                self.assertEqual(export["report"]["undecided_topics"], [TOPIC])
                self.assertEqual(export["report"]["not_exported"][0]["reason"],
                                 f"review status {status}")
        # A stored flag or an edited status string does not approve anything.
        forged = candidate(1, review=review("pending", reviewer="Test Reviewer",
                                            reviewed_at="2026-01-11T08:00:00Z", notes="approved"))
        with self.assertRaisesRegex(ExportError, "no approved"):
            build_export(dataset(forged))

    def test_invalid_or_unfinished_dataset_is_refused(self):
        with self.assertRaises(CandidateDatasetError):
            build_export(dataset(candidate(1, status="approved", correct="E")))
        with self.assertRaisesRegex(ExportError, "not finished"):
            build_export(dataset(candidate(1, status="approved"), finished=False))
        twin = candidate(2, status="approved", prompt=candidate(1)["prompt"],
                         options=candidate(1)["options"])
        with self.assertRaisesRegex(CandidateDatasetError, "unresolved flag"):
            build_export(dataset(candidate(1, status="approved"), twin))
        verbatim = candidate(1, status="approved", source_id="third-party-source")
        with self.assertRaisesRegex(CandidateDatasetError, "verbatim"):
            build_export(dataset(verbatim))

    def test_types_the_importer_cannot_take_are_reported_not_dropped(self):
        base = {key: value for key, value in candidate(2, status="approved").items()
                if key not in ("options", "correct")}
        data = dataset(
            candidate(1, status="approved"),
            dict(base, type="true_false", correct=True),
            candidate(3, status="approved", options={"A": "One", "B": "Two", "C": "Three"}),
        )
        export = build_export(data)
        self.assertEqual(len(export["files"][BATCH_FILE]["questions"]), 1)
        reasons = {item["candidate_id"]: item["reason"]
                   for item in export["report"]["not_exported"]}
        self.assertIn("true_false", reasons[f"{RUN_ID}-002"])
        self.assertIn("A, B, C, D", reasons[f"{RUN_ID}-003"])
        self.assertEqual(export["files"][PROVENANCE_FILE]["not_exported"],
                         export["report"]["not_exported"])

    def test_provenance_is_preserved_beside_the_batches(self):
        export = build_export(mixed_dataset())
        provenance = export["files"][PROVENANCE_FILE]
        self.assertEqual(provenance["run"]["id"], RUN_ID)
        self.assertEqual(provenance["run"]["limits"]["max_pages"], 40)
        (batch,) = provenance["batches"]
        self.assertEqual((batch["file"], batch["source"]),
                         (BATCH_FILE, export["files"][BATCH_FILE]["source"]))
        first, second = batch["questions"]
        self.assertEqual((first["number"], first["candidate_id"], first["transformation"]),
                         (1, f"{RUN_ID}-001", "extracted"))
        self.assertEqual(first["source"]["url"], "https://authority.example.invalid/paper.pdf")
        self.assertEqual(first["source"]["type"], "official")
        self.assertEqual(first["source_ref"], {"exam_session": "synthetic-2025",
                                               "question_number": 1})
        self.assertEqual(first["difficulty"], "easy")
        self.assertEqual(first["review"]["reviewer"], "Test Reviewer")
        self.assertEqual((second["source"]["type"], second["transformation"]),
                         ("third_party", "original_from_research"))
        self.assertEqual(second["research"], {"concept": "Synthetic concept"})
        self.assertEqual(second["explanation"], "Synthetic rationale.")
        # What cannot reach the database is named, not silently dropped.
        for name in ("source url", "transformation", "explanation", "difficulty"):
            self.assertIn(name, export["report"]["provenance_not_stored_in_database"])
            self.assertIn(name, provenance["not_stored_in_database"])

    def test_one_batch_per_topic(self):
        data = mixed_dataset()
        data["candidates"][3]["review"] = review("approved")
        export = build_export(data)
        other = f"{RUN_ID}.other-topic.json"
        self.assertEqual(sorted(export["files"]), sorted([BATCH_FILE, other, PROVENANCE_FILE]))
        self.assertEqual(validate_batch(export["files"][other]), "physics")
        self.assertEqual(export["files"][other]["topic"]["slug"], OTHER_TOPIC)
        self.assertEqual(export["files"][other]["questions"][0]["number"], 1)

    def test_write_never_overwrites(self):
        export = build_export(mixed_dataset())
        out = self.root / "out"
        written = write_export(export, out)
        self.assertEqual(sorted(path.name for path in written), [PROVENANCE_FILE, BATCH_FILE])
        before = directory_state(out)
        with self.assertRaisesRegex(ExportError, "overwrite"):
            write_export(export, out)
        self.assertEqual(directory_state(out), before)

    def test_exported_batch_imports_with_the_existing_importer(self):
        export = build_export(mixed_dataset())
        batch = json.loads(json.dumps(export["files"][BATCH_FILE]))
        before = directory_state(self.db_dir)
        summary = import_batch(self.db_path, batch)                    # the importer's dry run
        self.assertEqual(summary["questions"], {"inserted": 2, "reused": 0})
        self.assertEqual(directory_state(self.db_dir), before)

        import_batch(self.db_path, batch, apply=True)                  # temporary database only
        conn = sqlite3.connect(self.db_path)
        rows = conn.execute(
            "SELECT source_number, subject, level, question_type, prompt, exam_id "
            "FROM questions WHERE source_exam = ? ORDER BY source_number", (batch["source"],)
        ).fetchall()
        conn.close()
        self.assertEqual(rows, [
            (1, "physics", "8", "multiple_choice", candidate(1)["prompt"], None),
            (2, "physics", "8", "multiple_choice", candidate(3)["prompt"], None)])
        # Exact re-import of the same export is a no-op, as the importer promises.
        self.assertEqual(import_batch(self.db_path, batch, apply=True)["questions"],
                         {"inserted": 0, "reused": 2})
        # The run's own imported rows are not duplicates of its candidates...
        report = validate_dataset(mixed_dataset(), db_path=self.db_path)
        self.assertTrue(report.ok, report.as_dict())
        self.assertEqual(report.codes("flags"), {"target_not_in_db"})   # the unused other topic
        # ...but the same questions offered by another run are.
        other = json.loads(json.dumps(mixed_dataset()).replace(RUN_ID, "physics-g8-later-run"))
        report = validate_dataset(other, db_path=self.db_path)
        self.assertEqual(report.codes(), {"approved_blocked"})
        self.assertEqual(report.codes("flags"), {"duplicate_existing_exact", "target_not_in_db"})

    def test_export_with_db_is_read_only(self):
        before = directory_state(self.db_dir)
        export = build_export(mixed_dataset(), db_path=self.db_path)
        write_export(export, self.root / "out")
        self.assertEqual(directory_state(self.db_dir), before)

    def test_cli_writes_files_only_and_has_no_database_write_mode(self):
        input_path = self.root / "run.json"
        input_path.write_text(json.dumps(mixed_dataset()), encoding="utf-8")
        original = input_path.read_bytes()
        out = self.root / "export"
        before = directory_state(self.db_dir)
        with redirect_stdout(io.StringIO()) as output:
            main(["--input", str(input_path), "--output-dir", str(out), "--db", str(self.db_path)])
        report = json.loads(output.getvalue())
        self.assertEqual(report["exported_questions"], 2)
        self.assertEqual(sorted(Path(path).name for path in report["written"]),
                         [PROVENANCE_FILE, BATCH_FILE])
        self.assertEqual(directory_state(self.db_dir), before)
        self.assertEqual(input_path.read_bytes(), original)
        self.assertEqual(validate_batch(json.loads((out / BATCH_FILE).read_text("utf-8"))),
                         "physics")
        for flag in ("--apply", "--apply-to-db", "--import"):
            with self.subTest(flag=flag), redirect_stderr(io.StringIO()), \
                    self.assertRaises(SystemExit):
                main(["--input", str(input_path), "--output-dir", str(self.root / "other"),
                      flag])
        self.assertFalse((self.root / "other").exists())
        with redirect_stderr(io.StringIO()) as errors, self.assertRaises(SystemExit) as caught:
            main(["--input", str(input_path), "--output-dir", str(out)])
        self.assertEqual(caught.exception.code, 1)
        self.assertIn("overwrite", errors.getvalue())


if __name__ == "__main__":
    unittest.main()
