"""The one DZI-question predicate (roadmap PR C2): rules, seed pin, dependencies.

``web.services.dzi`` only identifies official DZI content. Nothing renders through
it yet, so these tests pin the predicate itself: what the seed contains, which
rows each rule admits or refuses, and that its SQL and Python forms agree.
Synthetic rows live in an in-memory copy of a temporary seed copy; the tracked
seed database is never written.
"""

import ast
import re
import sqlite3
import subprocess
import sys
import unittest
from contextlib import closing
from pathlib import Path

from tests.support import IsolatedAppTestCase, web_app
from web import db as web_db
from web.services import dzi
from web.text_values import unique_text_values

ROOT = Path(__file__).resolve().parents[1]

# Seed pin: 7 modern exams (25 task-linked questions each) plus the 3 legacy
# parser exams that have no task links. A later import that falls outside the
# predicate (or a rule change that drops these) fails this table.
SEED_DZI_COUNTS = {
    "aug_2022_v2": 25, "aug_2023_v2": 25, "aug_2024_v2": 25,
    "may_2022_v1": 25, "may_2023_v2": 25, "may_2024_v1": 25, "may_2025_v2": 25,
    "aug_2023": 22, "jun_2022": 22, "may_2023": 21,
}
SEED_NON_DZI_SOURCES = {
    "classroom_tests_alt_2026", "classroom_tests_8_12_2026",
    "ai_generated_20260429", "ai_generated_20260504",
}
LEGACY_SOURCES = {"aug_2023", "jun_2022", "may_2023"}


def source_counts(conn, where):
    return dict(conn.execute(
        f"SELECT q.source_exam, COUNT(*) FROM questions q WHERE {where} "
        "GROUP BY q.source_exam"
    ).fetchall())


class SeedPinTest(IsolatedAppTestCase):
    def setUp(self):
        conn = web_db.connect_db(web_app.DB_PATH, query_only=True)
        self.addCleanup(conn.close)
        self.conn = conn

    def test_seed_dzi_questions_are_pinned_per_source(self):
        self.assertEqual(source_counts(self.conn, dzi.dzi_question_sql("q")), SEED_DZI_COUNTS)
        self.assertEqual(sum(SEED_DZI_COUNTS.values()), 240)

    def test_everything_else_in_the_seed_is_outside_the_predicate(self):
        denied = source_counts(self.conn, f"NOT {dzi.dzi_question_sql('q')}")
        # The canonical data also holds the reviewed Physics/Biology batches; they are never DZI.
        reviewed = {source for source in denied if source.startswith(("reviewed:physics:", "reviewed:biology:"))}
        self.assertGreater(len(reviewed), 0)
        self.assertEqual(set(denied) - reviewed, SEED_NON_DZI_SOURCES)
        total = self.conn.execute("SELECT COUNT(*) FROM questions").fetchone()[0]
        self.assertEqual(sum(denied.values()) + 240, total)

    def test_legacy_parser_questions_count_without_task_links(self):
        linked = {r[0] for r in self.conn.execute("SELECT question_id FROM exam_task_questions")}
        legacy = {
            r[0] for r in self.conn.execute(
                "SELECT id FROM questions WHERE source_exam IN ('aug_2023','may_2023','jun_2022')")
        }
        self.assertEqual(len(legacy), 65)
        self.assertFalse(legacy & linked)
        self.assertTrue(all(dzi.is_dzi_question(self.conn, qid) for qid in legacy))

    def test_python_and_sql_forms_agree_on_every_seed_question(self):
        sql_ids = {r[0] for r in self.conn.execute(
            f"SELECT q.id FROM questions q WHERE {dzi.dzi_question_sql('q')}")}
        py_ids = {r[0] for r in self.conn.execute("SELECT id FROM questions")
                  if dzi.is_dzi_question(self.conn, r[0])}
        self.assertEqual(sql_ids, py_ids)
        self.assertEqual(len(sql_ids), 240)


class PredicateRulesTest(IsolatedAppTestCase):
    """Allow and deny rows with distinctive prompts, so leaks are visible as text."""

    ALLOW_LINKED = "SENTINEL-ALLOW-task-linked"
    ALLOW_LEGACY = "SENTINEL-ALLOW-legacy-dzi-exam"
    DENY = {
        "SENTINEL-DENY-ai-generated-on-dzi-exam",
        "SENTINEL-DENY-subject-question-no-exam",
        "SENTINEL-DENY-nvo-exam",
        "SENTINEL-DENY-dzi-exam-no-source-number",
        "SENTINEL-DENY-classroom",
    }

    def setUp(self):
        with closing(web_db.connect_db(web_app.DB_PATH, query_only=True)) as seed:
            self.conn = sqlite3.connect(":memory:")
            seed.backup(self.conn)
        self.conn.row_factory = sqlite3.Row
        self.addCleanup(self.conn.close)
        self.build_fixtures()

    def exam(self, level, year):
        return self.conn.execute(
            "INSERT INTO exams (subject, level, year, session, variant, source_file) "
            "VALUES ('informatika_it', ?, ?, 'sentinel', 1, ?)",
            (level, year, f"sentinel-{level}-{year}.pdf"),
        ).lastrowid

    def question(self, prompt, *, exam_id=None, number=None, ai=0, source="sentinel_src"):
        return self.conn.execute(
            "INSERT INTO questions (exam_id, source_exam, source_number, question_type, "
            "points, prompt, is_ai_generated) VALUES (?, ?, ?, 'multiple_choice', 1, ?, ?)",
            (exam_id, source, number, prompt, ai),
        ).lastrowid

    def build_fixtures(self):
        dzi_exam = self.exam("DZI", 2099)
        nvo_exam = self.exam("NVO_7", 2099)
        task = self.conn.execute(
            "INSERT INTO exam_tasks (exam_id, task_number, exam_part, task_kind, points) "
            "VALUES (?, 1, 1, 'multiple_choice', 1)", (dzi_exam,)).lastrowid

        # Allowed: linked to a task (no source_number needed) and legacy parser rows.
        self.allow_linked = self.question(self.ALLOW_LINKED, exam_id=dzi_exam)
        self.conn.execute(
            "INSERT INTO exam_task_questions (task_id, question_id, role) VALUES (?, ?, 'alternate')",
            (task, self.allow_linked))
        self.allow_legacy = self.question(self.ALLOW_LEGACY, exam_id=dzi_exam, number=7)

        # Denied: each carries part of the DZI signature but not enough of it.
        self.deny_ids = [
            self.question("SENTINEL-DENY-ai-generated-on-dzi-exam", exam_id=dzi_exam, number=8, ai=1),
            self.question("SENTINEL-DENY-subject-question-no-exam", number=9),
            self.question("SENTINEL-DENY-nvo-exam", exam_id=nvo_exam, number=10),
            self.question("SENTINEL-DENY-dzi-exam-no-source-number", exam_id=dzi_exam),
            self.question("SENTINEL-DENY-classroom", source="classroom_sentinel", ai=1),
        ]

    def prompts_selected_by_predicate(self):
        return {r[0] for r in self.conn.execute(
            f"SELECT q.prompt FROM questions q WHERE {dzi.dzi_question_sql('q')} "
            "AND q.prompt LIKE 'SENTINEL-%'")}

    def test_allowed_sentinels_are_selected_and_denied_sentinels_never_appear(self):
        selected = self.prompts_selected_by_predicate()
        self.assertEqual(selected, {self.ALLOW_LINKED, self.ALLOW_LEGACY})
        self.assertFalse(selected & self.DENY)
        rendered = "\n".join(sorted(selected))
        for sentinel in self.DENY:
            self.assertNotIn(sentinel, rendered)

    def test_python_form_matches_each_rule(self):
        self.assertTrue(dzi.is_dzi_question(self.conn, self.allow_linked))
        self.assertTrue(dzi.is_dzi_question(self.conn, self.allow_legacy))
        for question_id in self.deny_ids:
            self.assertFalse(dzi.is_dzi_question(self.conn, question_id), question_id)

    def test_unknown_question_is_not_dzi(self):
        self.assertFalse(dzi.is_dzi_question(self.conn, 10**9))

    def test_null_ai_flag_is_treated_as_not_ai_generated(self):
        self.conn.execute("UPDATE questions SET is_ai_generated = NULL WHERE id = ?", (self.allow_legacy,))
        self.assertTrue(dzi.is_dzi_question(self.conn, self.allow_legacy))

    def test_task_link_alone_is_enough_but_legacy_rule_needs_all_parts(self):
        self.conn.execute("DELETE FROM exam_task_questions WHERE question_id = ?", (self.allow_linked,))
        self.assertFalse(dzi.is_dzi_question(self.conn, self.allow_linked))  # no source_number
        self.conn.execute("UPDATE questions SET exam_id = NULL WHERE id = ?", (self.allow_legacy,))
        self.assertFalse(dzi.is_dzi_question(self.conn, self.allow_legacy))

    def test_fragment_composes_under_a_custom_alias(self):
        row = self.conn.execute(
            f"SELECT COUNT(*) FROM questions AS qq WHERE qq.prompt LIKE 'SENTINEL-%' "
            f"AND {dzi.dzi_question_sql('qq')}").fetchone()
        self.assertEqual(row[0], 2)

    def test_alias_is_validated(self):
        for bad in ("", "q; DROP TABLE questions", "q.id", "1q", "q q"):
            with self.assertRaises(ValueError, msg=bad):
                dzi.dzi_question_sql(bad)


class DependencyBoundaryTest(unittest.TestCase):
    def imported_modules(self, relative):
        tree = ast.parse((ROOT / relative).read_text(encoding="utf-8"))
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.update(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                names.add(node.module or "")
        return names

    def test_dzi_service_imports_neither_web_app_nor_the_quiz_service(self):
        imported = self.imported_modules("web/services/dzi.py")
        self.assertFalse({n for n in imported if n.startswith("web")}, imported)
        self.assertFalse({n for n in imported if n.startswith("src")}, imported)

    def test_text_values_is_standard_library_only(self):
        self.assertEqual(self.imported_modules("web/text_values.py"), set())

    def test_quiz_and_web_app_share_the_generic_helper_without_re_owning_it(self):
        for relative in ("web/services/quiz.py", "web/app.py"):
            source = (ROOT / relative).read_text(encoding="utf-8")
            self.assertIn("from web.text_values import unique_text_values", source, relative)
            self.assertIsNone(re.search(r"^def unique_text_values", source, re.M), relative)
        tree = ast.parse((ROOT / "web/app.py").read_text(encoding="utf-8"))
        from_quiz = {
            a.name for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module == "web.services.quiz"
            for a in node.names
        }
        self.assertNotIn("unique_text_values", from_quiz)

    def test_importing_dzi_service_does_not_load_web_app_or_the_quiz_service(self):
        code = (
            "import sys, web.services.dzi\n"
            "print('web.app' in sys.modules, 'web.services.quiz' in sys.modules)\n"
        )
        result = subprocess.run(
            [sys.executable, "-B", "-c", code], cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "False False")


class UniqueTextValuesTest(unittest.TestCase):
    def test_strips_drops_empty_and_keeps_first_seen_order(self):
        self.assertEqual(
            unique_text_values([" a ", "b", "a", "", "  ", "b ", 3, "3"]), ["a", "b", "3"])

    def test_empty_input(self):
        self.assertEqual(unique_text_values([]), [])


if __name__ == "__main__":
    unittest.main()
