"""Candidate dataset validation: format, limits, source policy, review, duplicates.

Pure file/in-memory tests. The database checks use a temporary copy of the seed.
"""

import copy
import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from src.question_candidates import (
    CandidateDatasetError, add_candidate, add_source, annotated_copy, new_dataset,
    normalize_text, validate_dataset,
)
from src.question_collection_limits import (
    ABSOLUTE_CEILINGS, DEFAULT_LIMITS, CollectionBudget, CollectionLimitError,
    CollectionLimitReached, CollectionLimits,
)
from src.start_question_collection import main as start_main
from src.validate_question_candidates import main as validate_main
from tests.question_candidate_fixtures import (
    OTHER_TOPIC, RUN_ID, SECTION, TOPIC, candidate, dataset, directory_state, review,
)
from tests.subject_fixtures import add_curriculum, add_question, add_section, add_topic
from tests.support import ROOT, copy_seed_database


class LimitTests(unittest.TestCase):
    def test_defaults_are_valid_and_below_the_ceilings(self):
        limits = CollectionLimits(**DEFAULT_LIMITS)
        self.assertEqual(limits.as_dict(), {
            "max_topics": 3, "max_pages": 40, "max_candidates": 100,
            "max_candidates_per_source": 20, "max_runtime_minutes": 30})
        self.assertEqual(ABSOLUTE_CEILINGS, {
            "max_topics": 10, "max_pages": 100, "max_candidates": 250,
            "max_candidates_per_source": 50, "max_runtime_minutes": 60})

    def test_zero_negative_and_non_integer_limits_are_rejected(self):
        for name in DEFAULT_LIMITS:
            for bad in (0, -1, None, True, 1.0, "40", float("inf")):
                with self.subTest(name=name, bad=bad), self.assertRaises(CollectionLimitError):
                    CollectionLimits(**{**DEFAULT_LIMITS, name: bad})

    def test_absolute_ceilings_are_enforced(self):
        for name, ceiling in ABSOLUTE_CEILINGS.items():
            values = {**ABSOLUTE_CEILINGS, name: ceiling + 1}
            with self.subTest(name=name), self.assertRaisesRegex(CollectionLimitError, "ceiling"):
                CollectionLimits(**values)
        CollectionLimits(**ABSOLUTE_CEILINGS)

    def test_missing_limit_is_never_unlimited(self):
        for name in DEFAULT_LIMITS:
            values = dict(DEFAULT_LIMITS)
            del values[name]
            with self.subTest(name=name), self.assertRaisesRegex(CollectionLimitError, "missing"):
                CollectionLimits.from_mapping(values)
        with self.assertRaises(CollectionLimitError):
            CollectionLimits.from_mapping({**DEFAULT_LIMITS, "max_depth": 5})
        with self.assertRaises(CollectionLimitError):
            CollectionLimits(**{**DEFAULT_LIMITS, "max_candidates": 5})   # per source > total

    def test_budget_stops_at_each_limit(self):
        limits = CollectionLimits(max_topics=1, max_pages=2, max_candidates=3,
                                  max_candidates_per_source=2, max_runtime_minutes=1)
        budget = CollectionBudget(limits, clock=lambda: 0.0)
        budget.record_page()
        budget.record_page()
        with self.assertRaises(CollectionLimitReached) as caught:
            budget.record_page()
        self.assertEqual(caught.exception.stop_reason, "page_limit")
        self.assertEqual(budget.pages_inspected, 2)
        with self.assertRaises(CollectionLimitReached):       # stays stopped
            budget.record_candidate("a")

        budget = CollectionBudget(limits, clock=lambda: 0.0)
        budget.record_candidate("a")
        budget.record_candidate("a")
        self.assertFalse(budget.source_has_room("a"))
        with self.assertRaises(CollectionLimitError):
            budget.record_candidate("a")
        budget.record_candidate("b")
        with self.assertRaises(CollectionLimitReached) as caught:
            budget.record_candidate("c")
        self.assertEqual(caught.exception.stop_reason, "candidate_limit")
        self.assertEqual(budget.finish("manual_stop"), "candidate_limit")

        now = [0.0]
        budget = CollectionBudget(limits, clock=lambda: now[0])
        budget.record_page()
        now[0] = 60.0
        with self.assertRaises(CollectionLimitReached) as caught:
            budget.record_page()
        self.assertEqual(caught.exception.stop_reason, "runtime_limit")

        budget = CollectionBudget(limits, clock=lambda: 0.0)
        with self.assertRaises(CollectionLimitError):
            budget.finish("because")
        self.assertEqual(budget.finish("no_more_sources"), "no_more_sources")


class DatasetValidationTests(unittest.TestCase):
    def assertError(self, data, code):
        report = validate_dataset(data)
        self.assertFalse(report.ok)
        self.assertIn(code, report.codes(), report.as_dict())
        return report

    def test_valid_dataset_and_tracked_example(self):
        report = validate_dataset(dataset())
        self.assertTrue(report.ok, report.as_dict())
        self.assertEqual(report.flags, [])
        self.assertEqual(report.counts["by_status"]["pending"], 1)
        example = json.loads(
            (ROOT / "docs/examples/question_candidates.example.json").read_text(encoding="utf-8"))
        report = validate_dataset(example)
        self.assertTrue(report.ok, report.as_dict())
        self.assertEqual(report.flags, [])

    def test_invalid_format_version_and_kind(self):
        for version in (2, "1", 1.0, True, None):
            data = dataset()
            data["format_version"] = version
            with self.subTest(version=version):
                self.assertError(data, "format_version")
        data = dataset()
        data["kind"] = "reviewed_batch"
        self.assertError(data, "format")
        data = dataset()
        data["extra"] = 1
        self.assertError(data, "format")
        self.assertFalse(validate_dataset([]).ok)

    def test_unknown_subject_and_invalid_grade(self):
        for subject in ("unknown", "informatika_it", None):
            data = dataset()
            data["run"]["subject"] = subject
            with self.subTest(subject=subject):
                self.assertError(data, "unknown_subject")
        for grade in (7, 13, "8", 8.0, True):
            data = dataset()
            data["run"]["grade"] = grade
            with self.subTest(grade=grade):
                self.assertError(data, "invalid_grade")

    def test_slug_shape_and_topic_membership(self):
        data = dataset()
        data["run"]["targets"][0]["topics"][0]["slug"] = "it-g8-synthetic-topic"
        self.assertError(data, "slug")
        data = dataset()
        data["run"]["targets"][0]["section"]["slug"] = "Physics-g8-Section"
        self.assertError(data, "slug")
        self.assertError(dataset(candidate(1, topic="physics-g8-not-targeted")), "topic")
        self.assertError(dataset(candidate(1, section_slug="physics-g8-wrong-section")), "topic")

    def test_invalid_source_type_and_malformed_urls(self):
        data = dataset()
        data["sources"][0]["type"] = "blog"
        self.assertError(data, "source_type")
        for url in ("ftp://authority.example.invalid/a", "authority.example.invalid/a",
                    "https://", "https://nohost", "https://a.example.invalid/a b", "", None):
            data = dataset()
            data["sources"][0]["url"] = url
            with self.subTest(url=url):
                self.assertError(data, "url")

    def test_provenance_is_required(self):
        for key in ("url", "title", "retrieved_at", "type"):
            data = dataset()
            del data["sources"][0][key]
            with self.subTest(missing=key):
                self.assertError(data, "provenance")
        data = dataset()
        data["sources"][0]["retrieved_at"] = "2026-01-10 09:02"        # no UTC offset
        self.assertError(data, "provenance")
        self.assertError(dataset(candidate(1, source_id="missing-source")), "provenance")
        item = candidate(1)
        del item["source_id"]
        self.assertError(dataset(item), "candidate")
        item = candidate(1)
        del item["transformation"]
        self.assertError(dataset(item), "candidate")
        self.assertError(dataset(candidate(1, transformation="copied")), "transformation")
        data = dataset()
        data["sources"][1]["url"] = "https://AUTHORITY.example.invalid/paper.pdf/"
        self.assertError(data, "provenance")                           # same page twice

    def test_official_extracted_candidate_is_accepted(self):
        report = validate_dataset(dataset(candidate(1)))
        self.assertTrue(report.ok)
        self.assertEqual(report.flags, [])
        item = candidate(1)
        del item["source_ref"]
        report = validate_dataset(dataset(item))
        self.assertTrue(report.ok)
        self.assertEqual(report.codes("flags"), {"extract_without_locator"})
        self.assertError(dataset(candidate(1, source_ref={"isbn": "x"})), "provenance")
        self.assertError(dataset(candidate(1, source_ref={})), "provenance")

    def test_third_party_policy(self):
        verbatim = candidate(1, source_id="third-party-source", transformation="extracted")
        report = self.assertError(dataset(verbatim), "third_party_verbatim")
        self.assertIn("original_from_research", report.errors[0].message)

        original = candidate(1, source_id="third-party-source",
                             transformation="original_from_research")
        report = validate_dataset(dataset(original))
        self.assertTrue(report.ok, report.as_dict())
        self.assertEqual(report.flags, [])
        del original["research"]
        self.assertError(dataset(original), "provenance")

        adapted = candidate(1, source_id="third-party-source", transformation="adapted")
        report = validate_dataset(dataset(adapted))
        self.assertTrue(report.ok)
        self.assertEqual(report.codes("flags"), {"third_party_adapted"})

        excepted = copy.deepcopy(verbatim)
        excepted["reuse_exception"] = {"basis": "Written permission on file",
                                       "approved_by": "Test Reviewer"}
        report = validate_dataset(dataset(excepted))
        self.assertTrue(report.ok, report.as_dict())
        self.assertEqual(report.codes("flags"), {"third_party_reuse_exception"})
        excepted["reuse_exception"]["basis"] = " "
        self.assertError(dataset(excepted), "third_party_verbatim")
        # The exception is narrow: it cannot ride along on other candidates.
        official = candidate(1, reuse_exception={"basis": "x", "approved_by": "y"})
        self.assertError(dataset(official), "provenance")

        long_notes = candidate(1, source_id="third-party-source",
                               transformation="original_from_research",
                               research={"concept": "Synthetic", "notes": "x" * 501})
        self.assertIn("research_notes_long", validate_dataset(dataset(long_notes)).codes("flags"))

    def test_review_states(self):
        for status in ("pending", "approved", "rejected", "needs_revision"):
            with self.subTest(status=status):
                report = validate_dataset(dataset(candidate(1, status=status)))
                self.assertTrue(report.ok, report.as_dict())
                self.assertEqual(report.counts["by_status"][status], 1)
        self.assertError(dataset(candidate(1, review=review("accepted"))), "review_state")
        item = candidate(1)
        del item["review"]
        self.assertError(dataset(item), "candidate")
        # Collection is not approval.
        self.assertError(dataset(candidate(1, review=review("approved", reviewer=None))),
                         "review_state")
        self.assertError(dataset(candidate(1, review=review("approved", reviewed_at=None))),
                         "review_state")
        self.assertError(dataset(candidate(
            1, review=review("approved", reviewed_at="2026-01-09T00:00:00Z"))), "review_state")
        self.assertError(dataset(candidate(
            1, review=review("approved", reviewer="Test-Collector"))), "review_state")

    def test_duplicate_candidate_ids(self):
        report = self.assertError(dataset(candidate(1), candidate(2, id=f"{RUN_ID}-001")),
                                  "duplicate_candidate_id")
        self.assertEqual(len(report.errors), 1)
        self.assertError(dataset(candidate(1, id="other-run-001")), "candidate_id")
        self.assertError(dataset(candidate(1, id=f"{RUN_ID}-")), "candidate_id")

    def test_malformed_multiple_choice(self):
        cases = {
            "mc_options": [
                {"options": ["a", "b", "c", "d"]},
                {"options": {"A": "One"}},
                {"options": {"A": "One", "C": "Two"}},
                {"options": {"a": "One", "b": "Two"}},
                {"options": {"A": "One", "B": " "}},
                {"options": {"A": "One", "B": "—", "C": "Three", "D": "Four"}},
                {"options": {letter: letter * 2 for letter in "ABCDEFG"}},
            ],
            "mc_duplicate_options": [
                {"options": {"A": "Same", "B": "same.", "C": "Three", "D": "Four"}},
            ],
            "invalid_correct": [{"correct": "E"}, {"correct": "b"}, {"correct": 2}],
            "unsupported_type": [{"type": "essay"}, {"type": None}],
            "prompt": [{"prompt": ""}, {"prompt": " padded "}, {"prompt": None}],
            "answer": [{"accepted_answers": ["x"]}],
        }
        for code, mutations in cases.items():
            for mutation in mutations:
                with self.subTest(code=code, mutation=mutation):
                    self.assertError(dataset(candidate(1, **mutation)), code)
        item = candidate(1)
        del item["correct"]
        self.assertError(dataset(item), "missing_correct")
        item = candidate(1)
        del item["options"]
        self.assertError(dataset(item), "answer")

    def test_other_question_types(self):
        base = {key: value for key, value in candidate(1).items()
                if key not in ("options", "correct")}
        true_false = dict(base, type="true_false", correct=False)
        self.assertTrue(validate_dataset(dataset(true_false)).ok)
        self.assertError(dataset(dict(true_false, correct="false")), "invalid_correct")
        short = dict(base, type="short_answer", accepted_answers=["42 units", "forty-two"])
        self.assertTrue(validate_dataset(dataset(short)).ok)
        self.assertError(dataset(dict(short, accepted_answers=[])), "answer")
        self.assertError(dataset(dict(short, accepted_answers=["A", "a"])), "answer")
        self.assertError(dataset(dict(short, correct="A")), "answer")

    def test_quality_flags(self):
        def flags(**changes):
            report = validate_dataset(dataset(candidate(1, **changes)))
            self.assertTrue(report.ok, report.as_dict())
            return report.codes("flags")

        self.assertEqual(flags(prompt="Why?"), {"prompt_too_short"})
        self.assertEqual(flags(prompt="x" * 1501), {"prompt_too_long", "prompt_too_short"})
        self.assertEqual(flags(prompt="Which <b>synthetic</b> statement is correct here?"),
                         {"html_residue"})
        self.assertEqual(flags(explanation="See&nbsp;the note."), {"html_residue"})
        self.assertEqual(flags(options={"A": "One", "B": "<script>x</script>",
                                        "C": "Three", "D": "Four"}), {"html_residue"})
        # Comparison signs in a physics prompt are not markup.
        self.assertEqual(flags(prompt="For which synthetic value is a < b and c > d true?"), set())
        self.assertEqual(flags(options={"A": "One", "B": "Two", "C": "Three"}),
                         {"mc_option_count"})
        self.assertEqual(
            flags(prompt="The synthetic unit named Zeta measures which quantity today?",
                  options={"A": "Alpha", "B": "Zeta", "C": "Gamma", "D": "Delta"}),
            {"answer_leak"})
        # Every choice named in the prompt is a normal "which of" question.
        self.assertEqual(
            flags(prompt="Which is larger today: Alpha, Zeta, Gamma or Delta?",
                  options={"A": "Alpha", "B": "Zeta", "C": "Gamma", "D": "Delta"}), set())

    def test_exact_duplicates_are_flagged_not_removed(self):
        twin = candidate(2, prompt="  WHICH synthetic statement about the first test object "
                                   "is correct ?".strip(),
                         options={"A": "Fourth 1", "B": "third 1.", "C": "Second 1",
                                  "D": "First 1"})
        data = dataset(candidate(1), twin, candidate(3))
        report = validate_dataset(data)
        self.assertTrue(report.ok)
        self.assertEqual(len(data["candidates"]), 3)
        self.assertEqual(
            {(flag.candidate_id, flag.code) for flag in report.flags},
            {(f"{RUN_ID}-001", "duplicate_exact"), (f"{RUN_ID}-002", "duplicate_exact")})
        # Same wording with a different answer set is a different question.
        report = validate_dataset(dataset(candidate(1), candidate(2, prompt=candidate(1)["prompt"])))
        self.assertEqual(report.flags, [])

    def test_near_duplicates_are_flagged(self):
        near = candidate(2, prompt="Which synthetic statement about the first test object "
                                   "is correct here?",
                         options={"A": "First 1", "B": "Second 1", "C": "Third 1",
                                  "D": "Something else"})
        report = validate_dataset(dataset(candidate(1), near, candidate(3)))
        self.assertTrue(report.ok)
        self.assertEqual(
            {(flag.candidate_id, flag.code) for flag in report.flags},
            {(f"{RUN_ID}-001", "duplicate_near"), (f"{RUN_ID}-002", "duplicate_near")})
        self.assertIn(f"{RUN_ID}-002", report.flags_for(f"{RUN_ID}-001")[0].message)

    def test_normalization_keeps_operators(self):
        self.assertEqual(normalize_text("  „Колко“   е 5 + 3 ?"), "колко е 5 + 3")
        self.assertNotEqual(normalize_text("5 + 3"), normalize_text("5 - 3"))
        self.assertEqual(normalize_text("ＡＢＣ"), "abc")

    def test_approved_duplicate_is_blocked_until_the_other_is_rejected(self):
        twin = candidate(2, prompt=candidate(1)["prompt"], options=candidate(1)["options"])
        report = validate_dataset(dataset(candidate(1, status="approved"), twin))
        self.assertIn("approved_blocked", report.codes())
        twin["review"] = review("rejected")
        report = validate_dataset(dataset(candidate(1, status="approved"), twin))
        self.assertTrue(report.ok, report.as_dict())
        self.assertEqual(report.flags, [])

    def test_hard_collection_limits_in_a_dataset(self):
        data = dataset(*[candidate(n) for n in range(1, 22)])
        self.assertError(data, "limit")                               # 21 from one source
        data = dataset()
        data["run"]["limits"]["max_candidates_per_source"] = 2
        data["run"]["limits"]["max_candidates"] = 2
        data["candidates"] = [candidate(1), candidate(2),
                              candidate(3, source_id="third-party-source",
                                        transformation="original_from_research")]
        self.assertError(data, "limit")                               # 3 candidates > 2
        data = dataset()
        data["run"]["pages_inspected"] = 41
        self.assertError(data, "limit")
        data = dataset()
        data["run"]["pages_inspected"] = 1                            # two sources, one page
        self.assertError(data, "limit")
        data = dataset()
        data["run"]["limits"]["max_topics"] = 1
        self.assertError(data, "limit")
        for name, ceiling in ABSOLUTE_CEILINGS.items():
            for bad in (0, -5, None, ceiling + 1):
                data = dataset()
                data["run"]["limits"][name] = bad
                with self.subTest(name=name, bad=bad):
                    self.assertError(data, "limit")
            data = dataset()
            del data["run"]["limits"][name]
            self.assertError(data, "limit")

    def test_run_manifest(self):
        data = dataset()
        data["run"]["stop_reason"] = "bored"
        self.assertError(data, "run")
        data = dataset()
        data["run"]["finished_at"] = None
        self.assertError(data, "run")                                 # stop_reason without finish
        data = dataset()
        data["run"]["finished_at"] = "2026-01-10T08:00:00Z"
        self.assertError(data, "run")
        data = dataset()
        data["run"]["finished_at"] = "2026-01-10T09:31:00Z"
        report = validate_dataset(data)
        self.assertTrue(report.ok)
        self.assertEqual(report.codes("flags"), {"runtime_limit_exceeded"})
        self.assertTrue(validate_dataset(dataset(finished=False)).ok)

    def test_collector_submission_api(self):
        limits = CollectionLimits(max_topics=2, max_pages=3, max_candidates=2,
                                  max_candidates_per_source=1, max_runtime_minutes=30)
        template = dataset()
        data = new_dataset(run_id=RUN_ID, started_at="2026-01-10T09:00:00Z", subject="physics",
                           grade=8, curriculum=template["run"]["curriculum"],
                           targets=template["run"]["targets"], limits=limits)
        self.assertEqual((data["sources"], data["candidates"]), ([], []))
        with self.assertRaises(CandidateDatasetError):                # page not counted yet
            add_source(data, template["sources"][0])
        data["run"]["pages_inspected"] = 2
        add_source(data, template["sources"][0])
        add_source(data, template["sources"][1])
        add_candidate(data, candidate(1))
        with self.assertRaises(CandidateDatasetError):                # collectors never approve
            add_candidate(data, candidate(2, status="approved", source_id="third-party-source",
                                          transformation="original_from_research"))
        with self.assertRaisesRegex(CandidateDatasetError, "max_candidates_per_source"):
            add_candidate(data, candidate(2))
        with self.assertRaisesRegex(CandidateDatasetError, "verbatim"):
            add_candidate(data, candidate(2, source_id="third-party-source"))
        add_candidate(data, candidate(2, source_id="third-party-source",
                                      transformation="original_from_research"))
        self.assertEqual(len(data["candidates"]), 2)
        with self.assertRaises(CandidateDatasetError):
            add_candidate(data, candidate(3))
        self.assertEqual(len(data["candidates"]), 2)
        with self.assertRaises(CandidateDatasetError):
            new_dataset(run_id=RUN_ID, started_at="2026-01-10T09:00:00Z", subject="unknown",
                        grade=8, curriculum=template["run"]["curriculum"],
                        targets=template["run"]["targets"], limits=limits)


class DatabaseCheckTests(unittest.TestCase):
    """Optional ``--db`` checks against a temporary copy of the seed database."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="learnpilot-candidates-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.db_dir = self.root / "db"
        self.db_dir.mkdir()
        self.db_path = self.db_dir / "questions.db"
        copy_seed_database(ROOT / "data/questions.db", self.db_path)
        conn = sqlite3.connect(self.db_path)
        with conn:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.close()
        for sidecar in self.db_dir.glob("questions.db-*"):
            sidecar.unlink()

    def seed_structure(self, *, subject="physics", topic_title="physics test topic"):
        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA journal_mode = DELETE")
        with conn:
            curriculum = add_curriculum(conn, subject, 8, "synthetic-f4-test",
                                        title="physics test curriculum")
            section = add_section(conn, curriculum, SECTION, "physics test section", grade=8)
            topic = add_topic(conn, section, TOPIC, topic_title, grade=8)
            add_topic(conn, section, OTHER_TOPIC, "physics other topic", grade=8)
        conn.close()
        return topic

    def test_ownership_validation_is_read_only(self):
        self.seed_structure()
        before = directory_state(self.db_dir)
        report = validate_dataset(dataset(), db_path=self.db_path)
        self.assertTrue(report.ok, report.as_dict())
        self.assertEqual(report.flags, [])
        self.assertEqual(directory_state(self.db_dir), before)

    def test_missing_structure_is_flagged_and_conflicts_are_errors(self):
        before = directory_state(self.db_dir)
        report = validate_dataset(dataset(), db_path=self.db_path)
        self.assertTrue(report.ok)
        self.assertEqual(report.codes("flags"), {"target_not_in_db"})
        self.assertEqual(directory_state(self.db_dir), before)

        self.seed_structure(topic_title="A different stored title")
        report = validate_dataset(dataset(), db_path=self.db_path)
        self.assertIn("db_conflict", report.codes())

    def test_section_owned_by_another_subject_is_rejected(self):
        self.seed_structure(subject="it")
        report = validate_dataset(dataset(), db_path=self.db_path)
        self.assertIn("db_conflict", report.codes())
        self.assertTrue(any("not owned by physics" in item.message for item in report.errors))

    def test_existing_question_duplicates_are_flagged_read_only(self):
        topic = self.seed_structure()
        first = candidate(1)
        conn = sqlite3.connect(self.db_path)
        with conn:
            add_question(conn, topic, "physics", 1, source="reviewed:physics:older-batch",
                         prompt=first["prompt"].upper(), options=first["options"], correct="B")
            add_question(conn, topic, "physics", 1,
                         source=f"reviewed:physics:{RUN_ID}.synthetic-topic",
                         prompt=candidate(3)["prompt"], options=candidate(3)["options"])
            add_question(conn, topic, "IT", 2, source="other-subject",
                         prompt=candidate(2)["prompt"], options=candidate(2)["options"])
        conn.close()
        before = directory_state(self.db_dir)
        data = dataset(first, candidate(2), candidate(3))
        report = validate_dataset(data, db_path=self.db_path)
        self.assertTrue(report.ok, report.as_dict())
        # Only the same-subject question from another source counts; this run's
        # own imported export and other subjects do not.
        self.assertEqual([(flag.candidate_id, flag.code) for flag in report.flags],
                         [(f"{RUN_ID}-001", "duplicate_existing_exact")])
        self.assertEqual(directory_state(self.db_dir), before)

        data["candidates"][0]["review"] = review("approved")
        report = validate_dataset(data, db_path=self.db_path)
        self.assertIn("approved_blocked", report.codes())
        self.assertEqual(directory_state(self.db_dir), before)

    def test_changes_waiting_in_the_write_ahead_log_are_reported(self):
        writer = sqlite3.connect(self.db_path)
        self.addCleanup(writer.close)
        writer.execute("PRAGMA journal_mode = WAL")
        with writer:
            add_curriculum(writer, "physics", 8, "synthetic-f4-test",
                           title="physics test curriculum")
        before = directory_state(self.db_dir)
        self.assertIn("questions.db-wal", before)
        report = validate_dataset(dataset(), db_path=self.db_path)
        self.assertTrue(report.ok)
        self.assertEqual(report.codes("flags"), {"db_wal_pending", "target_not_in_db"})
        self.assertEqual(directory_state(self.db_dir), before)

    def test_missing_database_is_an_error_and_is_not_created(self):
        missing = self.root / "absent.db"
        report = validate_dataset(dataset(), db_path=missing)
        self.assertIn("db", report.codes())
        self.assertFalse(missing.exists())


class CommandLineTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="learnpilot-candidates-cli-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def write(self, name, data):
        path = self.root / name
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return path

    def test_validate_cli_reports_and_never_changes_the_input(self):
        path = self.write("run.json", dataset(candidate(1), candidate(2, prompt="Why?")))
        original = path.read_bytes()
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(validate_main(["--input", str(path)]), 0)
        result = json.loads(output.getvalue())
        self.assertTrue(result["ok"])
        self.assertEqual([flag["code"] for flag in result["flags"]], ["prompt_too_short"])
        self.assertEqual(path.read_bytes(), original)

        annotated = self.root / "review.json"
        with redirect_stdout(io.StringIO()):
            self.assertEqual(validate_main(["--input", str(path), "--annotate", str(annotated)]), 0)
        self.assertEqual(path.read_bytes(), original)
        stored = json.loads(annotated.read_text(encoding="utf-8"))
        self.assertEqual(stored["candidates"][0]["flags"], [])
        self.assertEqual(stored["candidates"][1]["flags"][0]["code"], "prompt_too_short")
        self.assertTrue(validate_dataset(stored).ok)                  # still a valid dataset
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()), \
                self.assertRaises(SystemExit):                        # never overwrites
            validate_main(["--input", str(path), "--annotate", str(annotated)])

        bad = self.write("bad.json", dataset(candidate(1, correct="E")))
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(validate_main(["--input", str(bad)]), 1)
        self.assertFalse(json.loads(output.getvalue())["ok"])
        (self.root / "broken.json").write_text("{", encoding="utf-8")
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            validate_main(["--input", str(self.root / "broken.json")])

    def test_annotated_copy_does_not_touch_its_input(self):
        data = dataset(candidate(1, prompt="Why?"))
        snapshot = copy.deepcopy(data)
        result = annotated_copy(data, validate_dataset(data))
        self.assertEqual(data, snapshot)
        self.assertEqual(result["candidates"][0]["flags"][0]["code"], "prompt_too_short")

    def structure(self):
        return self.write("structure.json", {
            "format_version": 1, "subject": "physics", "grade": 8,
            "curriculum": {"education_type": "synthetic-f4-test",
                           "title": "physics test curriculum"},
            "sections": [{"slug": SECTION, "title": "physics test section", "topics": [
                {"slug": TOPIC, "title": "physics test topic"},
                {"slug": OTHER_TOPIC, "title": "physics other topic"}]}],
        })

    def start(self, *extra, output="run.json"):
        argv = ["--subject", "physics", "--grade", "8", "--topics", TOPIC,
                "--structure", str(self.structure()), "--max-pages", "40",
                "--max-candidates", "100", "--max-per-source", "20",
                "--run-id", RUN_ID, "--output", str(self.root / output)]
        for flag, value in zip(extra[::2], extra[1::2]):
            if flag in argv:
                argv[argv.index(flag) + 1] = value
            else:
                argv += [flag, value]
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as errors:
            try:
                start_main(argv)
            except SystemExit as exc:
                return exc.code, errors.getvalue()
        return 0, ""

    def test_start_cli_creates_an_empty_bounded_run(self):
        self.assertEqual(self.start()[0], 0)
        data = json.loads((self.root / "run.json").read_text(encoding="utf-8"))
        self.assertTrue(validate_dataset(data).ok)
        self.assertEqual((data["sources"], data["candidates"]), ([], []))
        self.assertEqual(data["run"]["limits"], DEFAULT_LIMITS)
        self.assertEqual(data["run"]["targets"], [{
            "section": {"slug": SECTION, "title": "physics test section"},
            "topics": [{"slug": TOPIC, "title": "physics test topic"}]}])
        self.assertEqual((data["run"]["finished_at"], data["run"]["stop_reason"]), (None, None))
        code, message = self.start()                                  # never overwrites
        self.assertEqual(code, 1)
        self.assertIn("rejected", message)

    def test_start_cli_requires_explicit_limits_and_enforces_ceilings(self):
        base = ["--subject", "physics", "--grade", "8", "--topics", TOPIC,
                "--structure", str(self.structure()), "--output", str(self.root / "x.json"),
                "--max-pages", "40", "--max-candidates", "100", "--max-per-source", "20"]
        for required in ("--subject", "--grade", "--topics", "--max-pages",
                         "--max-candidates", "--max-per-source"):
            argv = list(base)
            index = argv.index(required)
            del argv[index:index + 2]
            with self.subTest(missing=required), redirect_stderr(io.StringIO()), \
                    self.assertRaises(SystemExit):
                start_main(argv)
        for flag, value in (("--max-pages", "0"), ("--max-pages", "-1"), ("--max-pages", "101"),
                            ("--max-candidates", "251"), ("--max-per-source", "51"),
                            ("--max-topics", "11"), ("--max-runtime-minutes", "61"),
                            ("--max-runtime-minutes", "0")):
            with self.subTest(flag=flag, value=value):
                code, message = self.start(flag, value, output="y.json")
                self.assertEqual(code, 1, message)
        code, message = self.start("--topics", f"{TOPIC},{OTHER_TOPIC}", "--max-topics", "1",
                                   output="y.json")
        self.assertEqual(code, 1)
        self.assertIn("max_topics", message)
        code, message = self.start("--topics", "physics-g8-unlisted", output="y.json")
        self.assertIn("not in the structure file", message)
        self.assertEqual(sorted(path.name for path in self.root.iterdir()), ["structure.json"])


if __name__ == "__main__":
    unittest.main()
