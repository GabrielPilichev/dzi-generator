import sqlite3
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MIGRATION_PATH = ROOT / "web" / "migrations" / "008_practical_submissions.sql"


class PracticalSubmissionsMigrationSqlTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sql = MIGRATION_PATH.read_text(encoding="utf-8")

    def assertSqlContains(self, text):
        self.assertIn(text, self.sql)

    def test_creates_practical_submissions_table(self):
        self.assertSqlContains("CREATE TABLE IF NOT EXISTS practical_submissions")
        self.assertSqlContains("id INTEGER PRIMARY KEY AUTOINCREMENT")
        self.assertSqlContains("quiz_attempt_id INTEGER NOT NULL")
        self.assertSqlContains("exam_task_id INTEGER NOT NULL")
        self.assertSqlContains("status TEXT NOT NULL DEFAULT 'draft'")
        self.assertSqlContains("submitted_at TEXT")
        self.assertSqlContains("created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP")
        self.assertSqlContains("updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP")

    def test_creates_practical_submission_files_table(self):
        self.assertSqlContains("CREATE TABLE IF NOT EXISTS practical_submission_files")
        self.assertSqlContains("practical_submission_id INTEGER NOT NULL")
        self.assertSqlContains("stored_path TEXT NOT NULL UNIQUE")
        self.assertSqlContains("original_filename TEXT NOT NULL")
        self.assertSqlContains("size_bytes INTEGER")
        self.assertSqlContains("mime_type TEXT")
        self.assertSqlContains("uploaded_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP")
        self.assertSqlContains("is_deleted INTEGER NOT NULL DEFAULT 0")

    def test_contains_constraints_and_foreign_keys(self):
        self.assertSqlContains("CHECK (status IN ('draft', 'submitted', 'reviewed'))")
        self.assertSqlContains("UNIQUE (quiz_attempt_id, exam_task_id)")
        self.assertSqlContains("CHECK (length(stored_path) > 0)")
        self.assertSqlContains("CHECK (length(original_filename) > 0)")
        self.assertSqlContains("CHECK (size_bytes IS NULL OR size_bytes >= 0)")
        self.assertSqlContains("CHECK (is_deleted IN (0, 1))")
        self.assertSqlContains("FOREIGN KEY (quiz_attempt_id) REFERENCES quiz_attempts(id) ON DELETE CASCADE")
        self.assertSqlContains("FOREIGN KEY (exam_task_id) REFERENCES exam_tasks(id) ON DELETE CASCADE")
        self.assertSqlContains(
            "FOREIGN KEY (practical_submission_id) REFERENCES practical_submissions(id) ON DELETE CASCADE"
        )

    def test_contains_indexes(self):
        self.assertSqlContains("idx_practical_submissions_attempt")
        self.assertSqlContains("ON practical_submissions(quiz_attempt_id)")
        self.assertSqlContains("idx_practical_submissions_exam_task")
        self.assertSqlContains("ON practical_submissions(exam_task_id)")
        self.assertSqlContains("idx_practical_submission_files_submission")
        self.assertSqlContains("ON practical_submission_files(practical_submission_id)")

    def test_does_not_add_grading_table(self):
        self.assertNotIn("practical_submission_grades", self.sql)
        self.assertNotIn("manual_score", self.sql)
        self.assertNotIn("teacher_note", self.sql)

    def test_migration_executes_against_in_memory_stub_schema(self):
        conn = sqlite3.connect(":memory:")
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            conn.executescript("""
                CREATE TABLE quiz_attempts (id INTEGER PRIMARY KEY);
                CREATE TABLE exam_tasks (id INTEGER PRIMARY KEY);
            """)
            conn.executescript(self.sql)

            submission_columns = {
                row[1]: row
                for row in conn.execute("PRAGMA table_info(practical_submissions)").fetchall()
            }
            self.assertEqual(
                set(submission_columns),
                {
                    "id",
                    "quiz_attempt_id",
                    "exam_task_id",
                    "status",
                    "submitted_at",
                    "created_at",
                    "updated_at",
                },
            )
            self.assertEqual(submission_columns["quiz_attempt_id"][3], 1)
            self.assertEqual(submission_columns["exam_task_id"][3], 1)
            self.assertEqual(submission_columns["status"][4], "'draft'")

            file_columns = {
                row[1]: row
                for row in conn.execute("PRAGMA table_info(practical_submission_files)").fetchall()
            }
            self.assertEqual(
                set(file_columns),
                {
                    "id",
                    "practical_submission_id",
                    "stored_path",
                    "original_filename",
                    "size_bytes",
                    "mime_type",
                    "uploaded_at",
                    "is_deleted",
                },
            )
            self.assertEqual(file_columns["practical_submission_id"][3], 1)
            self.assertEqual(file_columns["stored_path"][3], 1)
            self.assertEqual(file_columns["original_filename"][3], 1)
            self.assertEqual(file_columns["is_deleted"][4], "0")

            index_names = {
                row[1]
                for row in conn.execute("PRAGMA index_list(practical_submissions)").fetchall()
            }
            self.assertIn("idx_practical_submissions_attempt", index_names)
            self.assertIn("idx_practical_submissions_exam_task", index_names)

            file_index_names = {
                row[1]
                for row in conn.execute("PRAGMA index_list(practical_submission_files)").fetchall()
            }
            self.assertIn("idx_practical_submission_files_submission", file_index_names)

            submission_foreign_keys = conn.execute(
                "PRAGMA foreign_key_list(practical_submissions)"
            ).fetchall()
            self.assertEqual(
                {(row[3], row[2], row[4], row[6]) for row in submission_foreign_keys},
                {
                    ("exam_task_id", "exam_tasks", "id", "CASCADE"),
                    ("quiz_attempt_id", "quiz_attempts", "id", "CASCADE"),
                },
            )
            file_foreign_keys = conn.execute(
                "PRAGMA foreign_key_list(practical_submission_files)"
            ).fetchall()
            self.assertEqual(
                {(row[3], row[2], row[4], row[6]) for row in file_foreign_keys},
                {("practical_submission_id", "practical_submissions", "id", "CASCADE")},
            )

            unique_columns = []
            for row in conn.execute("PRAGMA index_list(practical_submissions)").fetchall():
                if row[2]:
                    unique_columns.append([
                        column_row[2]
                        for column_row in conn.execute(f"PRAGMA index_info({row[1]})").fetchall()
                    ])
            self.assertIn(["quiz_attempt_id", "exam_task_id"], unique_columns)

            conn.execute("INSERT INTO quiz_attempts (id) VALUES (1)")
            conn.execute("INSERT INTO exam_tasks (id) VALUES (26)")
            conn.execute("""
                INSERT INTO practical_submissions (quiz_attempt_id, exam_task_id)
                VALUES (1, 26)
            """)
            submission = conn.execute("""
                SELECT id, status, submitted_at, created_at, updated_at
                FROM practical_submissions
            """).fetchone()
            self.assertEqual(submission[1], "draft")
            self.assertIsNone(submission[2])
            self.assertIsNotNone(submission[3])
            self.assertIsNotNone(submission[4])

            conn.execute("""
                INSERT INTO practical_submission_files (
                    practical_submission_id, stored_path, original_filename,
                    size_bytes, mime_type
                )
                VALUES (?, ?, ?, ?, ?)
            """, (
                submission[0],
                "data/uploads/practical/1/26/result.xlsx",
                "result.xlsx",
                42,
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            ))
            stored_file = conn.execute("""
                SELECT stored_path, original_filename, size_bytes, mime_type, is_deleted
                FROM practical_submission_files
            """).fetchone()
            self.assertEqual(stored_file[0], "data/uploads/practical/1/26/result.xlsx")
            self.assertEqual(stored_file[1], "result.xlsx")
            self.assertEqual(stored_file[2], 42)
            self.assertEqual(stored_file[4], 0)

            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("""
                    INSERT INTO practical_submissions (quiz_attempt_id, exam_task_id)
                    VALUES (1, 26)
                """)
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("""
                    INSERT INTO practical_submissions (quiz_attempt_id, exam_task_id, status)
                    VALUES (1, 26, 'graded')
                """)
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("""
                    INSERT INTO practical_submission_files (
                        practical_submission_id, stored_path, original_filename, size_bytes
                    )
                    VALUES (?, ?, ?, ?)
                """, (submission[0], "data/uploads/practical/1/26/result.xlsx", "copy.xlsx", 1))
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("""
                    INSERT INTO practical_submission_files (
                        practical_submission_id, stored_path, original_filename, size_bytes
                    )
                    VALUES (?, ?, ?, ?)
                """, (submission[0], "data/uploads/practical/1/26/bad.xlsx", "bad.xlsx", -1))
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("""
                    INSERT INTO practical_submission_files (
                        practical_submission_id, stored_path, original_filename, is_deleted
                    )
                    VALUES (?, ?, ?, ?)
                """, (submission[0], "data/uploads/practical/1/26/deleted.xlsx", "deleted.xlsx", 2))
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("""
                    INSERT INTO practical_submission_files (
                        practical_submission_id, stored_path, original_filename
                    )
                    VALUES (?, ?, ?)
                """, (999, "data/uploads/practical/1/26/missing.xlsx", "missing.xlsx"))

            conn.execute("DELETE FROM quiz_attempts WHERE id = 1")
            submission_count = conn.execute("SELECT COUNT(*) FROM practical_submissions").fetchone()[0]
            file_count = conn.execute("SELECT COUNT(*) FROM practical_submission_files").fetchone()[0]
            self.assertEqual(submission_count, 0)
            self.assertEqual(file_count, 0)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
