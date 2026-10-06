"""Curriculum structure import, exercised only against temporary database copies.

All structures here are synthetic; no real curriculum is implied for any subject.
"""

import copy
import hashlib
import io
import json
import shutil
import sqlite3
import tempfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from src import import_subject_structure
from src.import_reviewed_subject_questions import import_batch
from src.import_subject_structure import (
    StructureImportError, import_structure, main, validate_structure,
)
from src.subject_browsing import SubjectContentBrowser
from src.subject_content import section_ownership, topic_ownership
from tests.subject_fixtures import (
    add_curriculum, add_membership, add_section, add_topic, apply_pending_migrations,
)
from tests.support import ROOT, IsolatedAppTestCase, build_test_database, web_app
from tests.test_homepage_search import _HTMLProbe

EDUCATION_TYPE = "synthetic-structure-test"
STRUCTURE_TABLES = ("curricula", "curriculum_sections", "curriculum_topics",
                    "topic_section_assignments", "topic_classes")


def synthetic_structure(subject="physics", grade=8):
    """Three sections whose file order differs from their alphabetical order."""
    p = f"{subject}-g{grade}-"
    return {
        "format_version": 1, "subject": subject, "grade": grade,
        "curriculum": {"education_type": EDUCATION_TYPE,
                       "title": f"Synthetic {subject} {grade} curriculum"},
        "sections": [
            {"slug": p + "zulu", "title": "Synthetic section Zulu", "topics": [
                {"slug": p + "topic-c", "title": "Synthetic topic C"},
                {"slug": p + "topic-a", "title": "Synthetic topic A",
                 "description": "Synthetic topic description."},
                {"slug": p + "topic-b", "title": "Synthetic topic B"},
            ]},
            {"slug": p + "alpha", "title": "Synthetic section Alpha", "topics": [
                {"slug": p + "topic-d", "title": "Synthetic topic D"},
            ]},
            {"slug": p + "empty", "title": "Synthetic section Empty", "topics": []},
        ],
    }


class _StructureImportTestCase(IsolatedAppTestCase):
    """Each test gets its own migrated copy of the seed, wired into the application."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        template_dir = tempfile.TemporaryDirectory(prefix="learnpilot-structure-template-")
        cls.addClassCleanup(template_dir.cleanup)
        cls.template = Path(template_dir.name) / "questions.db"
        build_test_database(cls.template)
        apply_pending_migrations(cls.template)

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="learnpilot-structure-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.db_path = self.root / "questions.db"
        shutil.copyfile(self.template, self.db_path)
        web_app.DB_PATH = self.db_path
        web_app.QUIZ_DB_PATH = self.db_path
        web_app.app.config["DB_PATH"] = str(self.db_path)

    def connect(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        self.addCleanup(conn.close)
        return conn

    def sha(self):
        return hashlib.sha256(self.db_path.read_bytes()).hexdigest()

    def dump(self, tables=None):
        """Every row of the given tables (all tables by default), for exact comparison."""
        conn = self.connect()
        if tables is None:
            tables = [row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")]
        return {table: [tuple(row) for row in conn.execute(f'SELECT * FROM "{table}" ORDER BY 1')]
                for table in tables}

    def rejected(self, structure, *, apply=True):
        """The problems of a rejected import; the database must be untouched."""
        before = self.sha()
        with self.assertRaises(StructureImportError) as caught:
            import_structure(self.db_path, structure, apply=apply)
        self.assertEqual(self.sha(), before)
        return caught.exception.problems

    def stored_order(self, structure):
        """(section slug, order, [(topic slug, order)]) as stored for this structure."""
        conn = self.connect()
        result = []
        for section in conn.execute(
            """
            SELECT cs.id, cs.section_slug, cs.display_order FROM curriculum_sections cs
            JOIN curricula c ON c.id = cs.curriculum_id
            WHERE c.education_type = ? AND c.grade = ? ORDER BY cs.display_order
            """, (structure["curriculum"]["education_type"], structure["grade"]),
        ):
            topics = [tuple(row) for row in conn.execute(
                """
                SELECT t.topic_slug, tsa.display_order FROM topic_section_assignments tsa
                JOIN curriculum_topics t ON t.id = tsa.topic_id
                WHERE tsa.section_id = ? ORDER BY tsa.display_order
                """, (section["id"],))]
            result.append((section["section_slug"], section["display_order"], topics))
        return result


class StructureDryRunAndApplyTest(_StructureImportTestCase):
    def test_dry_run_reports_the_plan_and_writes_nothing(self):
        before, rows = self.sha(), self.dump()
        report = import_structure(self.db_path, synthetic_structure())
        self.assertEqual(report["mode"], "dry-run")
        self.assertEqual((report["subject"], report["grade"]), ("physics", 8))
        self.assertEqual(report["curriculum"]["action"], "create")
        self.assertEqual(report["counts"], {
            "curricula": {"create": 1, "update": 0, "unchanged": 0},
            "sections": {"create": 3, "update": 0, "unchanged": 0},
            "topics": {"create": 4, "update": 0, "unchanged": 0},
        })
        self.assertTrue(report["has_changes"])
        self.assertEqual([(s["slug"], s["order"]) for s in report["sections"]],
                         [("physics-g8-zulu", 1), ("physics-g8-alpha", 2), ("physics-g8-empty", 3)])
        self.assertEqual([(t["slug"], t["order"]) for t in report["sections"][0]["topics"]],
                         [("physics-g8-topic-c", 1), ("physics-g8-topic-a", 2),
                          ("physics-g8-topic-b", 3)])
        self.assertEqual(self.sha(), before)
        self.assertEqual(self.dump(), rows)

    def test_dry_run_sees_committed_content_that_is_still_in_the_wal(self):
        holder = self.connect()                       # keeps the WAL from being checkpointed away
        # copy_seed_database returns a rollback-journal copy; this test needs WAL.
        holder.execute("PRAGMA journal_mode=WAL").fetchone()
        holder.execute("SELECT COUNT(*) FROM curricula").fetchone()
        structure = synthetic_structure()
        import_structure(self.db_path, structure, apply=True)
        self.assertGreater(Path(str(self.db_path) + "-wal").stat().st_size, 0)
        main_file = self.sha()
        report = import_structure(self.db_path, structure)
        self.assertFalse(report["has_changes"])
        self.assertEqual(self.sha(), main_file)

    def test_apply_creates_the_curriculum_with_ordered_sections_and_topics(self):
        structure = synthetic_structure()
        report = import_structure(self.db_path, structure, apply=True)
        self.assertEqual(report["mode"], "apply")
        conn = self.connect()
        curriculum = conn.execute(
            "SELECT * FROM curricula WHERE education_type = ?", (EDUCATION_TYPE,)).fetchone()
        self.assertEqual(
            (curriculum["subject"], curriculum["grade"], curriculum["title_bg"],
             curriculum["source_type"], curriculum["source_url"]),
            ("physics", 8, "Synthetic physics 8 curriculum", "subject_structure_import", None))
        self.assertEqual(self.stored_order(structure), [
            ("physics-g8-zulu", 1, [("physics-g8-topic-c", 1), ("physics-g8-topic-a", 2),
                                    ("physics-g8-topic-b", 3)]),
            ("physics-g8-alpha", 2, [("physics-g8-topic-d", 1)]),
            ("physics-g8-empty", 3, []),
        ])
        for section in conn.execute(
                "SELECT * FROM curriculum_sections WHERE curriculum_id = ?", (curriculum["id"],)):
            self.assertEqual((section["class"], section["section_type"], section["module_id"],
                              section["source_type"]),
                             (8, "content", None, "subject_structure_import"))
            self.assertEqual(section_ownership(conn, section["id"]).subject, "physics")
        topic = conn.execute(
            "SELECT * FROM curriculum_topics WHERE topic_slug = 'physics-g8-topic-a'").fetchone()
        self.assertEqual((topic["title_bg"], topic["description"]),
                         ("Synthetic topic A", "Synthetic topic description."))
        membership = conn.execute(
            "SELECT * FROM topic_section_assignments WHERE topic_id = ?", (topic["id"],)).fetchall()
        self.assertEqual(len(membership), 1)
        self.assertEqual((membership[0]["section_id"], membership[0]["relationship_type"],
                          membership[0]["is_primary"], membership[0]["source_type"]),
                         (topic["section_id"], "covers", 1, "subject_structure_import"))
        self.assertEqual([row[0] for row in conn.execute(
            "SELECT class FROM topic_classes WHERE topic_id = ?", (topic["id"],))], [8])
        owner = topic_ownership(conn, topic["id"])
        self.assertEqual((owner.status, owner.subject), ("owned", "physics"))

    def test_apply_creates_no_questions_and_touches_only_structure_tables(self):
        before = self.dump()
        import_structure(self.db_path, synthetic_structure(), apply=True)
        after = self.dump()
        for table in before:
            if table not in STRUCTURE_TABLES:
                self.assertEqual(after[table], before[table], table)
        self.assertEqual({table: len(after[table]) - len(before[table]) for table in STRUCTURE_TABLES},
                         {"curricula": 1, "curriculum_sections": 3, "curriculum_topics": 4,
                          "topic_section_assignments": 4, "topic_classes": 4})
        # Every pre-existing structure row is still there, unchanged.
        for table in STRUCTURE_TABLES:
            self.assertTrue(set(before[table]) <= set(after[table]), table)

    def test_browsing_follows_the_file_order(self):
        import_structure(self.db_path, synthetic_structure("chemistry"), apply=True)
        conn = self.connect()
        browser = SubjectContentBrowser(conn, "chemistry")
        sections = browser.sections_for_grade(8)
        self.assertEqual([s["section_slug"] for s in sections],
                         ["chemistry-g8-zulu", "chemistry-g8-alpha", "chemistry-g8-empty"])
        self.assertEqual([t["topic_slug"] for t in browser.topics_in_section(sections[0]["section_id"])],
                         ["chemistry-g8-topic-c", "chemistry-g8-topic-a", "chemistry-g8-topic-b"])

    def test_optional_source_url_is_stored_and_later_filled_but_never_overwritten(self):
        structure = synthetic_structure()
        import_structure(self.db_path, structure, apply=True)
        structure["curriculum"]["source_url"] = "https://example.invalid/synthetic-programme"
        report = import_structure(self.db_path, structure, apply=True)
        self.assertEqual((report["curriculum"]["action"], report["curriculum"]["changes"]),
                         ("update", ["source_url added"]))
        self.assertEqual(self.connect().execute(
            "SELECT source_url FROM curricula WHERE education_type = ?", (EDUCATION_TYPE,)
        ).fetchone()[0], "https://example.invalid/synthetic-programme")
        self.assertFalse(import_structure(self.db_path, structure)["has_changes"])
        structure["curriculum"]["source_url"] = "https://example.invalid/another"
        self.assertIn("curriculum source_url differs", self.rejected(structure)[0])


class StructureIdempotencyTest(_StructureImportTestCase):
    def test_exact_reimport_changes_nothing_and_duplicates_nothing(self):
        structure = synthetic_structure()
        import_structure(self.db_path, structure, apply=True)
        rows = self.dump()
        second = import_structure(self.db_path, copy.deepcopy(structure), apply=True)
        self.assertFalse(second["has_changes"])
        self.assertEqual(second["counts"], {
            "curricula": {"create": 0, "update": 0, "unchanged": 1},
            "sections": {"create": 0, "update": 0, "unchanged": 3},
            "topics": {"create": 0, "update": 0, "unchanged": 4},
        })
        self.assertTrue(all(not item["changes"] for item in second["sections"]))
        self.assertEqual(self.dump(), rows)

    def test_dry_run_after_apply_reports_no_material_changes(self):
        structure = synthetic_structure()
        import_structure(self.db_path, structure, apply=True)
        before, rows = self.sha(), self.dump()
        report = import_structure(self.db_path, structure)
        self.assertEqual(report["mode"], "dry-run")
        self.assertFalse(report["has_changes"])
        self.assertEqual(report["counts"]["topics"], {"create": 0, "update": 0, "unchanged": 4})
        self.assertEqual((self.sha(), self.dump()), (before, rows))

    def test_reordering_the_file_updates_only_the_order(self):
        structure = synthetic_structure()
        import_structure(self.db_path, structure, apply=True)
        ids = {row[0]: row[1] for row in self.connect().execute(
            "SELECT section_slug, id FROM curriculum_sections WHERE section_slug LIKE 'physics-g8-%'")}
        structure["sections"].reverse()
        structure["sections"][2]["topics"].reverse()       # zulu: b, a, c
        report = import_structure(self.db_path, structure, apply=True)
        self.assertEqual(report["counts"]["sections"], {"create": 0, "update": 2, "unchanged": 1})
        self.assertEqual(report["counts"]["topics"], {"create": 0, "update": 2, "unchanged": 2})
        self.assertEqual(report["sections"][0]["changes"], ["order 3 -> 1"])
        self.assertEqual(self.stored_order(structure), [
            ("physics-g8-empty", 1, []),
            ("physics-g8-alpha", 2, [("physics-g8-topic-d", 1)]),
            ("physics-g8-zulu", 3, [("physics-g8-topic-b", 1), ("physics-g8-topic-a", 2),
                                    ("physics-g8-topic-c", 3)]),
        ])
        self.assertEqual({row[0]: row[1] for row in self.connect().execute(
            "SELECT section_slug, id FROM curriculum_sections WHERE section_slug LIKE 'physics-g8-%'"
        )}, ids)
        self.assertFalse(import_structure(self.db_path, structure, apply=True)["has_changes"])

    def test_extending_the_file_creates_only_what_is_new(self):
        structure = synthetic_structure()
        import_structure(self.db_path, structure, apply=True)
        structure["sections"][2]["topics"].append(
            {"slug": "physics-g8-topic-e", "title": "Synthetic topic E"})
        structure["sections"].append(
            {"slug": "physics-g8-new", "title": "Synthetic section New", "topics": []})
        report = import_structure(self.db_path, structure, apply=True)
        self.assertEqual(report["counts"]["sections"], {"create": 1, "update": 0, "unchanged": 3})
        self.assertEqual(report["counts"]["topics"], {"create": 1, "update": 0, "unchanged": 4})
        self.assertEqual(self.stored_order(structure)[2:], [
            ("physics-g8-empty", 3, [("physics-g8-topic-e", 1)]), ("physics-g8-new", 4, []),
        ])

    def test_two_grades_and_two_subjects_do_not_interfere(self):
        for subject, grade in (("physics", 8), ("physics", 9), ("chemistry", 8)):
            import_structure(self.db_path, synthetic_structure(subject, grade), apply=True)
        for subject, grade in (("physics", 8), ("physics", 9), ("chemistry", 8)):
            self.assertFalse(
                import_structure(self.db_path, synthetic_structure(subject, grade))["has_changes"])
        self.assertEqual(self.connect().execute(
            "SELECT COUNT(*) FROM curricula WHERE education_type = ?", (EDUCATION_TYPE,)
        ).fetchone()[0], 3)


class StructureFileValidationTest(_StructureImportTestCase):
    def assertRejected(self, mutate, message):
        structure = synthetic_structure()
        mutate(structure)
        with self.assertRaisesRegex(StructureImportError, message):
            validate_structure(structure)
        self.assertRegex("; ".join(self.rejected(structure)), message)

    def test_unknown_or_ambiguous_subjects_are_rejected(self):
        for value in ("astronomy", "informatika", "Информатика", "", None, 5):
            with self.subTest(subject=value):
                self.assertRejected(lambda s, v=value: s.update(subject=v), "Unknown content subject")

    def test_legacy_alias_resolves_to_it_never_to_informatics(self):
        structure = synthetic_structure("it", 8)
        structure["subject"] = "informatika_it"
        self.assertEqual(validate_structure(structure), "it")

    def test_grades_outside_the_registry_are_rejected(self):
        for value in (7, 13, 0, "8", 8.0, None, True):
            with self.subTest(grade=value):
                self.assertRejected(lambda s, v=value: s.update(grade=v), "grades supported by physics")

    def test_duplicate_sections_and_topics_are_rejected(self):
        self.assertRejected(lambda s: s["sections"].append(copy.deepcopy(s["sections"][0])),
                            "Duplicate section slug: 'physics-g8-zulu'")
        # A topic may be listed once in the whole file, not once per section.
        self.assertRejected(
            lambda s: s["sections"][1]["topics"].append(
                {"slug": "physics-g8-topic-a", "title": "Synthetic topic A"}),
            "Duplicate topic slug: 'physics-g8-topic-a'")

    def test_slugs_must_be_namespaced_for_the_subject_and_grade(self):
        for slug in ("zulu", "physics-g9-zulu", "biology-g8-zulu", "Physics-g8-zulu",
                     "physics-g8-", "physics-g8-zulu_x", "physics-g8-zulu--x", "", None, 3):
            with self.subTest(slug=slug):
                self.assertRejected(lambda s, v=slug: s["sections"][0].update(slug=v),
                                    "must be lowercase, hyphenated and namespaced physics-g8-")
                self.assertRejected(lambda s, v=slug: s["sections"][0]["topics"][0].update(slug=v),
                                    "must be lowercase, hyphenated and namespaced physics-g8-")

    def test_order_is_the_array_position_and_cannot_be_given_explicitly(self):
        for key in ("display_order", "order", "position"):
            with self.subTest(key=key):
                self.assertRejected(lambda s, k=key: s["sections"][0].update({k: 2}),
                                    "order is the position in the array")
                self.assertRejected(lambda s, k=key: s["sections"][0]["topics"][0].update({k: 2}),
                                    "order is the position in the array")
        self.assertRejected(lambda s: s.update(sections={"1": s["sections"][0]}),
                            "sections must be a non-empty array")
        self.assertRejected(lambda s: s.update(sections=[]), "sections must be a non-empty array")
        self.assertRejected(lambda s: s["sections"][0].update(topics=None), "topics must be an array")

    def test_shape_text_and_version_are_strict(self):
        self.assertRejected(lambda s: s.update(format_version=2), "Unsupported format_version")
        self.assertRejected(lambda s: s.update(questions=[]), "unexpected questions")
        self.assertRejected(lambda s: s.pop("curriculum"), "missing curriculum")
        self.assertRejected(lambda s: s["curriculum"].pop("title"), "missing title")
        self.assertRejected(lambda s: s["curriculum"].update(subject="physics"), "unexpected subject")
        self.assertRejected(lambda s: s["sections"][0].update(title=" padded "), "trimmed text")
        self.assertRejected(lambda s: s["sections"][0]["topics"][0].update(title=""), "trimmed text")
        self.assertRejected(lambda s: s["sections"][0].update(module="m1"), "unexpected module")
        self.assertRejected(
            lambda s: s["sections"][0]["topics"][0].update(description="- [[physics-moc]]"),
            "description must be plain prose")
        with self.assertRaisesRegex(StructureImportError, "structure must be an object"):
            validate_structure([synthetic_structure()])


class StructureConflictTest(_StructureImportTestCase):
    def test_section_slug_owned_by_another_subject_is_rejected(self):
        conn = self.connect()
        biology = add_curriculum(conn, "biology", 8, "conflict-test")
        add_section(conn, biology, "physics-g8-zulu", "Foreign section", grade=8)
        conn.commit()
        problems = self.rejected(synthetic_structure())
        self.assertEqual(problems, [
            "section slug 'physics-g8-zulu' already belongs to another curriculum (biology, grade 8)"
        ])

    def test_section_slug_in_another_curriculum_of_the_same_subject_is_rejected(self):
        conn = self.connect()
        other_track = add_curriculum(conn, "physics", 8, "another-track")
        add_section(conn, other_track, "physics-g8-alpha", "Synthetic section Alpha", grade=8)
        conn.commit()
        self.assertEqual(self.rejected(synthetic_structure()), [
            "section slug 'physics-g8-alpha' already belongs to another curriculum (physics, grade 8)"
        ])

    def test_topic_slug_owned_elsewhere_is_rejected(self):
        conn = self.connect()
        biology = add_curriculum(conn, "biology", 8, "conflict-test")
        section = add_section(conn, biology, "biology-g8-cells", "Foreign section", grade=8)
        add_topic(conn, section, "physics-g8-topic-a", "Synthetic topic A", grade=8)
        conn.commit()
        self.assertEqual(self.rejected(synthetic_structure()), [
            "topic slug 'physics-g8-topic-a' already belongs to section 'biology-g8-cells' "
            "(biology, grade 8), not to section 'physics-g8-zulu'"
        ])

    def test_topic_moved_between_sections_of_the_file_is_rejected(self):
        structure = synthetic_structure()
        import_structure(self.db_path, structure, apply=True)
        moved = structure["sections"][0]["topics"].pop(0)
        structure["sections"][1]["topics"].append(moved)
        problems = self.rejected(structure)
        self.assertEqual(len(problems), 2)
        self.assertIn("topic 'physics-g8-topic-c' is stored in section 'physics-g8-zulu' but missing",
                      problems[0])
        self.assertIn("topic slug 'physics-g8-topic-c' already belongs to section 'physics-g8-zulu' "
                      "(physics, grade 8), not to section 'physics-g8-alpha'", problems[1])

    def test_curriculum_identity_collisions_are_rejected(self):
        conn = self.connect()
        add_curriculum(conn, "physics", 8, EDUCATION_TYPE, title="A different stored title")
        conn.commit()
        self.assertEqual(self.rejected(synthetic_structure()), [
            "curriculum title differs from the stored title 'A different stored title'"
        ])
        # Two stored spellings of one subject for the same grade and track are ambiguous.
        structure = synthetic_structure("it", 9)
        for stored in ("IT", "informatika_it"):
            add_curriculum(conn, stored, 9, EDUCATION_TYPE, title=structure["curriculum"]["title"])
        conn.commit()
        self.assertIn("is ambiguous: 2 stored curricula match", self.rejected(structure)[0])

    def test_changed_titles_are_conflicts_not_overwrites(self):
        structure = synthetic_structure()
        import_structure(self.db_path, structure, apply=True)
        structure["sections"][1]["title"] = "Renamed section"
        structure["sections"][0]["topics"][0]["title"] = "Renamed topic"
        structure["sections"][0]["topics"][1]["description"] = "Another description."
        self.assertEqual(self.rejected(structure), [
            "topic 'physics-g8-topic-c' title differs from the stored title 'Synthetic topic C'",
            "topic 'physics-g8-topic-a' description differs from the stored description",
            "section 'physics-g8-alpha' title differs from the stored title 'Synthetic section Alpha'",
        ])
        self.assertEqual(self.connect().execute(
            "SELECT title_bg FROM curriculum_sections WHERE section_slug = 'physics-g8-alpha'"
        ).fetchone()[0], "Synthetic section Alpha")

    def test_contradicting_stored_relationships_are_rejected(self):
        structure = synthetic_structure()
        import_structure(self.db_path, structure, apply=True)
        conn = self.connect()
        topic_id = conn.execute(
            "SELECT id FROM curriculum_topics WHERE topic_slug = 'physics-g8-topic-d'").fetchone()[0]

        conn.execute("UPDATE topic_section_assignments SET is_primary = 0 WHERE topic_id = ?",
                     (topic_id,))
        conn.commit()
        self.assertIn("is not stored as the primary 'covers' member", self.rejected(structure)[0])
        conn.execute("UPDATE topic_section_assignments SET is_primary = 1 WHERE topic_id = ?",
                     (topic_id,))

        conn.execute("INSERT INTO topic_classes (topic_id, class) VALUES (?, 9)", (topic_id,))
        conn.commit()
        self.assertIn("is stored for grades [8, 9], not only grade 8", self.rejected(structure)[0])
        conn.execute("DELETE FROM topic_classes WHERE topic_id = ? AND class = 9", (topic_id,))

        it_section = conn.execute(
            "SELECT id FROM curriculum_sections WHERE section_slug = 'grade8-computer-systems'"
        ).fetchone()[0]
        add_membership(conn, topic_id, it_section)
        conn.commit()
        self.assertEqual(self.rejected(structure), [
            "topic 'physics-g8-topic-d' has conflicting ownership: "
            "conflict/cross_subject_topic_membership"
        ])

    def test_stored_content_missing_from_the_file_is_rejected(self):
        structure = synthetic_structure()
        import_structure(self.db_path, structure, apply=True)
        del structure["sections"][2]                              # an existing section
        del structure["sections"][0]["topics"][1]                 # an existing topic
        problems = self.rejected(structure)
        self.assertEqual(len(problems), 2)
        self.assertIn("section 'physics-g8-empty' is stored in this curriculum but missing", problems[0])
        self.assertIn("topic 'physics-g8-topic-a' is stored in section 'physics-g8-zulu' but missing",
                      problems[1])
        # Nothing was deleted or reordered.
        self.assertEqual(len(self.stored_order(synthetic_structure())), 3)

    def test_a_slug_that_would_shadow_a_short_stored_slug_is_rejected(self):
        conn = self.connect()
        other_track = add_curriculum(conn, "physics", 8, "another-track")
        section = add_section(conn, other_track, "zulu", "Short stored slug", grade=8)
        add_topic(conn, section, "topic-d", "Short stored topic", grade=8)
        conn.commit()
        problems = self.rejected(synthetic_structure())
        self.assertEqual(len(problems), 2)
        self.assertIn("section 'physics-g8-zulu' would shadow the stored section 'zulu'", problems[0])
        self.assertIn("topic 'physics-g8-topic-d' would shadow the stored topic 'topic-d'", problems[1])

    def test_legacy_it_curricula_cannot_be_taken_over(self):
        conn = self.connect()
        stored = conn.execute(
            "SELECT education_type, title_bg FROM curricula WHERE grade = 8 "
            "AND subject = 'Информационни технологии'").fetchone()
        structure = synthetic_structure("it", 8)
        structure["curriculum"] = {"education_type": stored["education_type"],
                                   "title": stored["title_bg"]}
        before = self.dump(STRUCTURE_TABLES)
        problems = self.rejected(structure)
        self.assertTrue(any("section 'grade8-review' is stored in this curriculum but missing" in p
                            for p in problems), problems)
        self.assertGreaterEqual(len(problems), 8)
        self.assertEqual(self.dump(STRUCTURE_TABLES), before)

    def test_a_conflict_or_error_rolls_everything_back(self):
        # A late conflict: the first sections are valid, the last topic is not.
        conn = self.connect()
        biology = add_curriculum(conn, "biology", 8, "conflict-test")
        section = add_section(conn, biology, "biology-g8-cells", "Foreign section", grade=8)
        add_topic(conn, section, "physics-g8-topic-d", "Synthetic topic D", grade=8)
        conn.commit()
        rows = self.dump()
        self.rejected(synthetic_structure())
        self.assertEqual(self.dump(), rows)

        # An error in the middle of the writes.
        conn.execute("DELETE FROM topic_section_assignments WHERE topic_id IN "
                     "(SELECT id FROM curriculum_topics WHERE topic_slug = 'physics-g8-topic-d')")
        conn.execute("DELETE FROM topic_classes WHERE topic_id IN "
                     "(SELECT id FROM curriculum_topics WHERE topic_slug = 'physics-g8-topic-d')")
        conn.execute("DELETE FROM curriculum_topics WHERE topic_slug = 'physics-g8-topic-d'")
        conn.commit()
        rows = self.dump()
        calls = []

        def fail_late(owner, subject, label):
            calls.append(label)
            if len(calls) == 5:
                raise sqlite3.OperationalError("simulated failure during apply")

        with patch.object(import_subject_structure, "_owned", fail_late):
            with self.assertRaisesRegex(sqlite3.OperationalError, "simulated failure"):
                import_structure(self.db_path, synthetic_structure(), apply=True)
        self.assertEqual(len(calls), 5)                       # rows had been inserted by then
        self.assertEqual(self.dump(), rows)
        self.assertEqual(self.connect().execute(
            "SELECT COUNT(*) FROM curricula WHERE education_type = ?", (EDUCATION_TYPE,)
        ).fetchone()[0], 0)

    def test_database_without_the_order_column_or_missing_is_rejected(self):
        old = self.root / "pre-010.db"
        with sqlite3.connect(old) as conn:
            conn.execute("CREATE TABLE topic_section_assignments (id INTEGER PRIMARY KEY, "
                         "topic_id INTEGER, section_id INTEGER)")
        conn.close()
        for apply in (False, True):
            with self.assertRaisesRegex(StructureImportError, "apply the pending migrations"):
                import_structure(old, synthetic_structure(), apply=apply)
        missing = self.root / "missing.db"
        for apply in (False, True):
            with self.assertRaisesRegex(StructureImportError, "Database not found"):
                import_structure(missing, synthetic_structure(), apply=apply)
        self.assertFalse(missing.exists())


class StructureCommandLineTest(_StructureImportTestCase):
    def run_cli(self, structure, *extra):
        path = self.root / "structure.json"
        path.write_text(structure if isinstance(structure, str)
                        else json.dumps(structure, ensure_ascii=False), encoding="utf-8")
        out, err = io.StringIO(), io.StringIO()
        code = 0
        with redirect_stdout(out), redirect_stderr(err):
            try:
                main(["--input", str(path), "--db", str(self.db_path), *extra])
            except SystemExit as exit_:
                code = exit_.code
        return code, out.getvalue(), err.getvalue()

    def test_default_is_a_dry_run_that_prints_the_plan(self):
        before = self.sha()
        code, out, err = self.run_cli(synthetic_structure())
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(self.sha(), before)
        lines = out.splitlines()
        self.assertEqual(lines[0], "Mode: DRY RUN (nothing is written; add --apply to write)")
        self.assertIn("Subject: physics (Физика)", lines)
        self.assertIn("Grade: 8", lines)
        self.assertIn(f"Curriculum: {EDUCATION_TYPE} — Synthetic physics 8 curriculum [create]", lines)
        self.assertIn("  1. physics-g8-zulu — Synthetic section Zulu [create]", lines)
        self.assertIn("       2. physics-g8-topic-a — Synthetic topic A [create]", lines)
        self.assertLess(out.index("physics-g8-zulu"), out.index("physics-g8-alpha"))
        self.assertIn("Sections: 3 to create, 0 to update, 0 unchanged", lines)
        self.assertIn("Topics: 4 to create, 0 to update, 0 unchanged", lines)
        self.assertIn("Questions: none are created or changed by this importer", lines)
        self.assertEqual(lines[-1], "Result: changes would be written with --apply")

    def test_apply_writes_and_a_repeat_reports_no_changes(self):
        code, out, _ = self.run_cli(synthetic_structure(), "--apply")
        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines()[0], "Mode: APPLY")
        self.assertEqual(out.splitlines()[-1], "Result: changes written")
        for extra in ((), ("--apply",)):
            code, out, _ = self.run_cli(synthetic_structure(), *extra)
            self.assertEqual(code, 0)
            self.assertIn("  2. physics-g8-alpha — Synthetic section Alpha [unchanged]", out)
            self.assertEqual(out.splitlines()[-1],
                             "Result: no changes; the database already matches this structure")

    def test_reordering_is_reported_before_it_is_applied(self):
        structure = synthetic_structure()
        self.run_cli(structure, "--apply")
        structure["sections"].reverse()
        code, out, _ = self.run_cli(structure)
        self.assertEqual(code, 0)
        self.assertIn("  1. physics-g8-empty — Synthetic section Empty [update] (order 3 -> 1)", out)

    def test_rejections_exit_with_every_problem_and_leave_the_database_unchanged(self):
        structure = synthetic_structure()
        self.run_cli(structure, "--apply")
        before = self.sha()
        structure["sections"][0]["title"] = "Renamed"
        del structure["sections"][2]
        for extra in ((), ("--apply",)):
            code, out, err = self.run_cli(structure, *extra)
            self.assertEqual((code, out), (1, ""))
            self.assertIn("Import rejected; the database was not changed:", err)
            self.assertEqual(err.count("\n  - "), 2)
            self.assertIn("section 'physics-g8-zulu' title differs", err)
        code, _, err = self.run_cli("{not json")
        self.assertEqual(code, 1)
        self.assertIn("Import rejected", err)
        self.assertEqual(self.sha(), before)


class StructureApplicationFlowTest(_StructureImportTestCase):
    """Structure first, questions afterwards, through the public pages."""

    def page(self, path, status=200):
        response = web_app.app.test_client().get(path)
        self.assertEqual(response.status_code, status, path)
        html = response.get_data(as_text=True)
        return html, _HTMLProbe(html)

    @staticmethod
    def question_batch(structure, section_index, topic_index):
        section = structure["sections"][section_index]
        topic = section["topics"][topic_index]
        subject, grade = structure["subject"], structure["grade"]
        return {
            "format_version": 1, "subject": subject, "grade": grade,
            "source": f"reviewed:{subject}:structure-flow-test-v1",
            "curriculum": {"subject": subject, "grade": grade,
                           "education_type": structure["curriculum"]["education_type"],
                           "title": structure["curriculum"]["title"]},
            "section": {"slug": section["slug"], "title": section["title"], "grade": grade},
            "topic": {"slug": topic["slug"], "title": topic["title"], "grade": grade},
            "questions": [{"number": 1, "subject": subject, "grade": grade,
                           "topic_slug": topic["slug"], "type": "multiple_choice",
                           "prompt": "Synthetic structure-flow question?",
                           "options": {"A": "One", "B": "Two", "C": "Three", "D": "Four"},
                           "correct": "A"}],
        }

    def test_structure_only_subject_is_browsable_in_file_order(self):
        import_structure(self.db_path, synthetic_structure("chemistry"), apply=True)
        html, home = self.page("/")
        self.assertIn("home-subject-card--structure", html)
        self.assertIn("/subjects/chemistry/8", home.hrefs())

        html, grade = self.page("/subjects/chemistry/8")
        links = [href for href in grade.hrefs() if href.startswith("/subjects/chemistry/8/")]
        self.assertEqual(links, ["/subjects/chemistry/8/zulu", "/subjects/chemistry/8/alpha",
                                 "/subjects/chemistry/8/empty"])
        self.assertIn("Въпросите са в подготовка", grade.text)

        html, section = self.page("/subjects/chemistry/8/zulu")
        topic_links = [attrs["href"] for _, attrs in section.elements("a")
                       if "stretched-link" in (attrs.get("class") or "")]
        self.assertEqual(topic_links, [f"/subjects/chemistry/8/zulu/topic-{letter}" for letter in "cab"])
        self.assertIn("В този раздел още няма публикувани въпроси.", section.text)
        _, topic = self.page("/subjects/chemistry/8/zulu/topic-a")
        self.assertIn("Synthetic topic description.", topic.text)
        # Chemistry has no navigation placeholders, so an unknown section stays a 404.
        self.page("/subjects/chemistry/8/mechanics", 404)

    def test_questions_are_imported_afterwards_into_the_same_structure(self):
        structure = synthetic_structure()
        import_structure(self.db_path, structure, apply=True)
        rows = self.dump(STRUCTURE_TABLES)
        summary = import_batch(self.db_path, self.question_batch(structure, 0, 1), apply=True)
        self.assertEqual({key: value for key, value in summary.items()}, {
            "curricula": {"inserted": 0, "reused": 1}, "sections": {"inserted": 0, "reused": 1},
            "topics": {"inserted": 0, "reused": 1}, "questions": {"inserted": 1, "reused": 0},
        })
        self.assertEqual(self.dump(STRUCTURE_TABLES), rows)        # structure and order untouched
        html, section = self.page("/subjects/physics/8/zulu")
        self.assertIn("Synthetic structure-flow question?", html)
        self.assertIn("/teacher/new?subject=physics&section=physics-g8-zulu", section.hrefs())
        # The structure file still matches: questions are not its concern.
        self.assertFalse(import_structure(self.db_path, structure)["has_changes"])

    def test_structure_adopts_rows_the_question_importer_created_first(self):
        structure = synthetic_structure("chemistry")
        structure["sections"] = structure["sections"][1:2]         # alpha with topic d
        import_batch(self.db_path, self.question_batch(structure, 0, 0), apply=True)
        report = import_structure(self.db_path, structure, apply=True)
        self.assertEqual(report["curriculum"]["action"], "unchanged")
        self.assertEqual((report["sections"][0]["action"], report["sections"][0]["changes"]),
                         ("update", ["order None -> 1"]))
        self.assertEqual(report["sections"][0]["topics"][0]["changes"], ["order None -> 1"])
        self.assertEqual(self.stored_order(structure),
                         [("chemistry-g8-alpha", 1, [("chemistry-g8-topic-d", 1)])])
        self.assertEqual(self.connect().execute(
            "SELECT COUNT(*) FROM questions WHERE source_exam LIKE 'reviewed:chemistry:%'"
        ).fetchone()[0], 1)


if __name__ == "__main__":
    import unittest

    unittest.main()
