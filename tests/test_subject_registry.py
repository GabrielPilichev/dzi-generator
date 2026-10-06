"""Canonical subject registry: identity, grades, validation and derived availability.

Pure tests use in-memory SQLite. Database-backed tests use a canonical-built database in a
temporary file and never touch the repository database.
"""

import dataclasses
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src import subject_content, subject_registry
from src.import_reviewed_subject_questions import import_batch, validate_batch
from src.subject_browsing import SubjectContentBrowser, subject_content_grades
from src.subject_registry import (
    IT, PHYSICS, PLATFORM_GRADES, SUBJECTS, SubjectDefinition, all_subjects,
    get_subject, resolve_subject_identifier, subject_for_route_slug,
    grade_paths, subject_supports_grade, validate_registry,
)
from tests.support import ROOT, build_test_database

EXPECTED_ORDER = [
    ("it", "information-technologies", "Информационни технологии"),
    ("physics", "physics", "Физика"),
    ("informatics", "informatics", "Информатика"),
    ("biology", "biology", "Биология"),
    ("mathematics", "mathematics", "Математика"),
    ("chemistry", "chemistry", "Химия"),
]


def _definition(**overrides):
    fields = dict(id="alpha", route_slug="alpha", title="Алфа", description="d", search_terms="s",
                  grade_paths=grade_paths(general=PLATFORM_GRADES))
    fields.update(overrides)
    return SubjectDefinition(**fields)


class RegistryIdentityTests(unittest.TestCase):
    def test_six_subjects_in_display_order(self):
        self.assertEqual(
            [(s.id, s.route_slug, s.title) for s in SUBJECTS], EXPECTED_ORDER
        )
        self.assertEqual(all_subjects(), SUBJECTS)

    def test_ids_route_slugs_and_aliases_are_unique(self):
        ids = [s.id for s in SUBJECTS]
        slugs = [s.route_slug for s in SUBJECTS]
        aliases = [alias for s in SUBJECTS for alias in s.legacy_aliases]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(len(slugs), len(set(slugs)))
        self.assertEqual(len(aliases), len(set(aliases)))
        self.assertFalse(set(aliases) & (set(ids) | set(slugs)))

    def test_identifier_resolution(self):
        for value, expected in (
            ("it", "it"), ("IT", "it"), ("informatika_it", "it"),
            ("Информационни технологии", "it"), ("information-technologies", "it"),
            ("physics", "physics"), ("informatics", "informatics"),
            ("biology", "biology"), ("mathematics", "mathematics"),
            ("chemistry", "chemistry"), (" biology ", "biology"),
        ):
            with self.subTest(value=value):
                self.assertEqual(resolve_subject_identifier(value), expected)
                self.assertEqual(subject_content.canonical_subject(value), expected)

    def test_ambiguous_or_unknown_values_never_resolve(self):
        # Bare Bulgarian/Latin "informatics" names are ambiguous with legacy IT/DZI data.
        for value in ("Информатика", "informatika", "Physics", "Biology", "i t", "",
                      "unmapped-subject", None, 8, True, b"it"):
            with self.subTest(value=value):
                self.assertIsNone(resolve_subject_identifier(value))
                self.assertFalse(subject_content.is_known_content_subject(value))
        self.assertEqual(resolve_subject_identifier("informatika_it"), "it")

    def test_route_slugs_are_the_only_url_identifiers(self):
        self.assertEqual(subject_for_route_slug("information-technologies").id, "it")
        self.assertEqual(subject_for_route_slug("biology").id, "biology")
        for value in ("it", "IT", "informatika_it", "Информационни технологии",
                      "Biology", "unknown", "", None, 8):
            with self.subTest(value=value):
                self.assertIsNone(subject_for_route_slug(value))

    def test_get_subject_requires_an_exact_canonical_id(self):
        self.assertEqual(get_subject("it").route_slug, "information-technologies")
        for value in ("IT", "information-technologies", "informatika_it", None, 8):
            with self.subTest(value=value):
                self.assertIsNone(get_subject(value))

    def test_content_contract_is_derived_from_the_registry(self):
        self.assertEqual(subject_content.KNOWN_CONTENT_SUBJECTS, {s.id for s in SUBJECTS})
        self.assertEqual((subject_content.IT, subject_content.PHYSICS), (IT, PHYSICS))
        self.assertEqual(
            subject_content.SUBJECT_ALIASES,
            {"IT": "it", "informatika_it": "it", "Информационни технологии": "it",
             "information-technologies": "it"},
        )


class RegistryCapabilityTests(unittest.TestCase):
    def test_only_it_is_dzi(self):
        self.assertEqual([s.id for s in SUBJECTS if s.dzi], ["it"])

    def test_placeholder_groups_exist_only_for_physics_in_declared_grades(self):
        for subject in SUBJECTS:
            with self.subTest(subject=subject.id):
                if subject.id == PHYSICS:
                    self.assertEqual(set(subject.placeholder_topic_groups), {8, 9, 10})
                    self.assertTrue(all(
                        group["status"] == "in_preparation"
                        for groups in subject.placeholder_topic_groups.values()
                        for group in groups
                    ))
                else:
                    self.assertEqual(dict(subject.placeholder_topic_groups), {})
                self.assertLessEqual(set(subject.placeholder_topic_groups), set(subject.grades))

    def test_supported_grades_per_subject(self):
        expected = {
            "it": (8, 9, 10, 11, 12),
            "physics": (8, 9, 10),
            "informatics": (8,),
            "biology": (8, 9, 10),
            "mathematics": (8, 9, 10, 11, 12),
            "chemistry": (8, 9, 10),
        }
        self.assertEqual({subject.id: subject.grades for subject in SUBJECTS}, expected)

    def test_it_alone_has_an_explicit_active_description(self):
        self.assertIsNotNone(get_subject(IT).active_description)
        for subject in SUBJECTS:
            if subject.id != IT:
                self.assertIsNone(subject.active_description)

    def test_grade_support_is_strict(self):
        for subject in SUBJECTS:
            for grade in PLATFORM_GRADES:
                self.assertEqual(subject_supports_grade(subject.id, grade), grade in subject.grades)
            for grade in (0, 7, 13, -8, 8.0, "8", None, True, False):
                with self.subTest(subject=subject.id, grade=grade):
                    self.assertFalse(subject_supports_grade(subject.id, grade))
        self.assertFalse(subject_supports_grade("unknown", 8))
        self.assertFalse(subject_supports_grade("IT", 8))
        self.assertFalse(subject_supports_grade(None, 8))

    def test_patched_registry_changes_grade_support(self):
        narrowed = dataclasses.replace(get_subject("biology"), grades=(10, 11, 12))
        with patch.dict(subject_registry._SUBJECTS_BY_ID, {"biology": narrowed}):
            self.assertFalse(subject_supports_grade("biology", 8))
            self.assertTrue(subject_supports_grade("biology", 10))
        self.assertTrue(subject_supports_grade("biology", 8))


class RegistryValidationTests(unittest.TestCase):
    def test_shipped_registry_is_valid(self):
        validate_registry(SUBJECTS)

    def test_rejects_duplicate_ids_and_overlapping_identifiers(self):
        cases = {
            "duplicate id": (_definition(), _definition(route_slug="other")),
            "slug equals another id": (_definition(), _definition(id="beta", route_slug="alpha")),
            "alias equals another slug": (
                _definition(), _definition(id="beta", route_slug="beta", legacy_aliases=("alpha",)),
            ),
            "alias shared": (
                _definition(legacy_aliases=("x",)),
                _definition(id="beta", route_slug="beta", legacy_aliases=("x",)),
            ),
            "alias repeated within a subject": (_definition(legacy_aliases=("x", "x")),),
        }
        for label, subjects in cases.items():
            with self.subTest(label), self.assertRaises(ValueError):
                validate_registry(subjects)

    def test_rejects_malformed_grade_declarations(self):
        cases = {
            "outside platform": (7, 8),
            "beyond platform": (12, 13),
            "unsorted": (9, 8),
            "duplicated": (8, 8),
            "bool": (True, 9),
            "string": ("8", 9),
        }
        for label, grades in cases.items():
            with self.subTest(label), self.assertRaises(ValueError):
                validate_registry((_definition(grades=grades),))

    def test_rejects_placeholder_groups_for_undeclared_grades(self):
        groups = {8: ({"slug": "a", "title": "A", "status": "in_preparation"},)}
        with self.assertRaises(ValueError):
            validate_registry((_definition(grades=(9, 10), placeholder_topic_groups=groups),))
        validate_registry((_definition(grades=(8, 9), placeholder_topic_groups=groups),))

    def test_a_subject_may_declare_a_subset_of_grades(self):
        validate_registry((_definition(grades=(10, 11, 12)),))
        validate_registry((_definition(grades=(11,)),))


def _grades_schema():
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE curricula (id INTEGER PRIMARY KEY, grade INTEGER NOT NULL, subject TEXT);
        CREATE TABLE curriculum_sections (
            id INTEGER PRIMARY KEY, curriculum_id INTEGER, class INTEGER NOT NULL
        );
        """
    )
    return conn


class SubjectContentGradesTests(unittest.TestCase):
    def setUp(self):
        self.conn = _grades_schema()
        self.addCleanup(self.conn.close)

    def _content(self, curriculum_id, subject, grade, *section_classes):
        self.conn.execute("INSERT INTO curricula VALUES (?, ?, ?)", (curriculum_id, grade, subject))
        for section_class in section_classes:
            self.conn.execute(
                "INSERT INTO curriculum_sections (curriculum_id, class) VALUES (?, ?)",
                (curriculum_id, section_class),
            )

    def test_empty_database_gives_every_subject_no_content(self):
        result = subject_content_grades(self.conn)
        self.assertEqual(list(result), [s.id for s in SUBJECTS])
        self.assertTrue(all(grades == frozenset() for grades in result.values()))

    def test_grades_are_derived_per_subject_and_aliases_are_canonicalized(self):
        self._content(1, "Информационни технологии", 8, 8)
        self._content(2, "IT", 9, 9)
        self._content(3, "biology", 10, 10, 10)
        self._content(4, "informatika_it", 11, 11)
        result = subject_content_grades(self.conn)
        self.assertEqual(result["it"], frozenset({8, 9, 11}))
        self.assertEqual(result["biology"], frozenset({10}))
        for other in ("physics", "informatics", "mathematics", "chemistry"):
            self.assertEqual(result[other], frozenset())

    def test_class_must_equal_curriculum_grade(self):
        self._content(1, "biology", 9, 10, 8)
        self.assertEqual(subject_content_grades(self.conn)["biology"], frozenset())

    def test_unknown_blank_null_and_curriculumless_rows_are_ignored(self):
        self._content(1, "unmapped-subject", 8, 8)
        self._content(2, "", 9, 9)
        self._content(3, None, 10, 10)
        self._content(4, "Информатика", 11, 11)
        self.conn.execute("INSERT INTO curriculum_sections (curriculum_id, class) VALUES (NULL, 8)")
        result = subject_content_grades(self.conn)
        self.assertTrue(all(grades == frozenset() for grades in result.values()))

    def test_content_in_an_undeclared_grade_does_not_count(self):
        self._content(1, "physics", 8, 8)
        self._content(2, "physics", 10, 10)
        narrowed = dataclasses.replace(get_subject("physics"), grades=(10, 11, 12))
        with patch.dict(subject_registry._SUBJECTS_BY_ID, {"physics": narrowed}):
            self.assertEqual(subject_content_grades(self.conn)["physics"], frozenset({10}))


def _batch(grade=8, subject="biology", number=1):
    prefix = f"{subject}-g{grade}"
    return {
        "format_version": 1, "subject": subject, "grade": grade,
        "source": f"reviewed:{subject}:registry-test-v1",
        "curriculum": {"subject": subject, "grade": grade,
                       "education_type": "synthetic-registry-test", "title": f"{subject} test"},
        "section": {"slug": f"{prefix}-synthetic-section", "title": "Synthetic section",
                    "grade": grade},
        "topic": {"slug": f"{prefix}-synthetic-topic", "title": "Synthetic topic", "grade": grade},
        "questions": [{"number": number, "subject": subject, "grade": grade,
                       "topic_slug": f"{prefix}-synthetic-topic", "type": "multiple_choice",
                       "prompt": f"Synthetic {subject} question {number}?",
                       "options": {"A": "One", "B": "Two", "C": "Three", "D": "Four"},
                       "correct": "B"}],
    }


class RegistryImporterValidationTests(unittest.TestCase):
    def test_all_registered_subjects_validate_for_every_declared_grade(self):
        for subject in SUBJECTS:
            for grade in subject.grades:
                with self.subTest(subject=subject.id, grade=grade):
                    self.assertEqual(validate_batch(_batch(grade, subject.id)), subject.id)

    def test_grades_outside_the_platform_or_of_the_wrong_type_are_rejected(self):
        for grade in (7, 13, 0, "8", 8.0, None, True):
            with self.subTest(grade=grade):
                batch = _batch()
                batch["grade"] = grade
                with self.assertRaisesRegex(ValueError, "grade"):
                    validate_batch(batch)

    def test_grade_must_be_supported_by_the_subject(self):
        narrowed = dataclasses.replace(get_subject("biology"), grades=(10, 11, 12))
        with patch.dict(subject_registry._SUBJECTS_BY_ID, {"biology": narrowed}):
            with self.assertRaisesRegex(ValueError, "grade"):
                validate_batch(_batch(8))
            self.assertEqual(validate_batch(_batch(10)), "biology")
            # A subject that was not narrowed is unaffected.
            self.assertEqual(validate_batch(_batch(8, "chemistry")), "chemistry")

    def test_unknown_and_ambiguous_subjects_are_rejected(self):
        for subject in ("unknown", "Информатика", "informatika"):
            with self.subTest(subject=subject):
                batch = _batch()
                batch["subject"] = subject
                with self.assertRaisesRegex(ValueError, "Unknown content subject"):
                    validate_batch(batch)


class RegistryDatabaseTests(unittest.TestCase):
    """Uses a temporary canonical-built database only (migrations + data/canonical)."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="learnpilot-registry-")
        self.addCleanup(tmp.cleanup)
        self.db_path = Path(tmp.name) / "questions.db"
        build_test_database(self.db_path)

    def connect(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        self.addCleanup(conn.close)
        return conn

    def test_canonical_database_has_content_for_it_physics_and_biology_only(self):
        result = subject_content_grades(self.connect())
        self.assertEqual(result["it"], frozenset({8, 9, 10, 11, 12}))
        self.assertEqual(result["physics"], frozenset({8, 9, 10}))
        self.assertEqual(result["biology"], frozenset({8, 9, 10}))
        for subject in SUBJECTS:
            if subject.id not in (IT, "physics", "biology"):
                self.assertEqual(result[subject.id], frozenset(), subject.id)

    def test_browser_grade_counts_follow_the_declared_grades(self):
        conn = self.connect()
        full = SubjectContentBrowser(conn, IT, legacy_it=True).grade_counts()
        self.assertEqual([row["grade"] for row in full], [8, 9, 10, 11, 12])
        narrowed = dataclasses.replace(get_subject(IT), grades=(11, 12))
        with patch.dict(subject_registry._SUBJECTS_BY_ID, {IT: narrowed}):
            limited = SubjectContentBrowser(conn, IT, legacy_it=True).grade_counts()
        self.assertEqual([row["grade"] for row in limited], [11, 12])
        self.assertEqual(limited, [row for row in full if row["grade"] in (11, 12)])

    def test_dry_run_and_apply_for_a_registered_subject(self):
        before = self.db_path.read_bytes()
        dry = import_batch(self.db_path, _batch(), apply=False)
        self.assertEqual(self.db_path.read_bytes(), before)
        self.assertEqual(dry["questions"], {"inserted": 1, "reused": 0})
        applied = import_batch(self.db_path, _batch(), apply=True)
        self.assertEqual(applied, dry)
        conn = self.connect()
        # Grade 8 Biology content is already canonical; the import adds to it.
        self.assertEqual(subject_content_grades(conn)["biology"], frozenset({8, 9, 10}))
        self.assertEqual(subject_content_grades(conn)["chemistry"], frozenset())
        repeated = import_batch(self.db_path, _batch(), apply=True)
        self.assertEqual(repeated["questions"], {"inserted": 0, "reused": 1})

    def test_unsupported_grade_never_reaches_the_database(self):
        before = self.db_path.read_bytes()
        batch = _batch()
        batch["grade"] = 7
        with self.assertRaisesRegex(ValueError, "grade"):
            import_batch(self.db_path, batch, apply=True)
        self.assertEqual(self.db_path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
