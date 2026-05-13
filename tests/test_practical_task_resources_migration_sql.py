import sqlite3
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MIGRATION_PATH = ROOT / "web" / "migrations" / "007_practical_task_resources.sql"


class PracticalTaskResourcesMigrationSqlTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sql = MIGRATION_PATH.read_text(encoding="utf-8")

    def assertSqlContains(self, text):
        self.assertIn(text, self.sql)

    def test_creates_practical_task_resources_table(self):
        self.assertSqlContains("CREATE TABLE IF NOT EXISTS practical_task_resources")
        self.assertSqlContains("id INTEGER PRIMARY KEY AUTOINCREMENT")
        self.assertSqlContains("exam_task_id INTEGER NOT NULL")
        self.assertSqlContains("resource_path TEXT NOT NULL")
        self.assertSqlContains("original_filename TEXT")
        self.assertSqlContains("label_bg TEXT")
        self.assertSqlContains("file_size_bytes INTEGER")
        self.assertSqlContains("sha256 TEXT")
        self.assertSqlContains("created_at TEXT DEFAULT CURRENT_TIMESTAMP")

    def test_contains_resource_constraints(self):
        self.assertSqlContains("CHECK (length(resource_path) > 0)")
        self.assertSqlContains("CHECK (file_size_bytes IS NULL OR file_size_bytes >= 0)")
        self.assertSqlContains("CHECK (sha256 IS NULL OR length(sha256) = 64)")
        self.assertSqlContains("UNIQUE (exam_task_id, resource_path)")

    def test_contains_foreign_key_and_index(self):
        self.assertSqlContains("FOREIGN KEY (exam_task_id) REFERENCES exam_tasks(id) ON DELETE CASCADE")
        self.assertSqlContains("idx_practical_task_resources_exam_task")
        self.assertSqlContains("ON practical_task_resources(exam_task_id)")

    def test_does_not_denormalize_task_identity(self):
        self.assertNotIn("source_slug", self.sql)
        self.assertNotIn("task_number", self.sql)

    def test_migration_executes_against_in_memory_stub_schema(self):
        conn = sqlite3.connect(":memory:")
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            conn.executescript("""
                CREATE TABLE exam_tasks (
                    id INTEGER PRIMARY KEY
                );
            """)
            conn.executescript(self.sql)

            columns = {
                row[1]: row
                for row in conn.execute("PRAGMA table_info(practical_task_resources)").fetchall()
            }
            self.assertEqual(
                set(columns),
                {
                    "id",
                    "exam_task_id",
                    "resource_path",
                    "original_filename",
                    "label_bg",
                    "file_size_bytes",
                    "sha256",
                    "created_at",
                },
            )
            self.assertEqual(columns["exam_task_id"][2], "INTEGER")
            self.assertEqual(columns["exam_task_id"][3], 1)
            self.assertEqual(columns["resource_path"][2], "TEXT")
            self.assertEqual(columns["resource_path"][3], 1)

            index_names = {
                row[1]
                for row in conn.execute("PRAGMA index_list(practical_task_resources)").fetchall()
            }
            self.assertIn("idx_practical_task_resources_exam_task", index_names)

            foreign_keys = conn.execute("PRAGMA foreign_key_list(practical_task_resources)").fetchall()
            self.assertEqual(len(foreign_keys), 1)
            self.assertEqual(foreign_keys[0][2], "exam_tasks")
            self.assertEqual(foreign_keys[0][3], "exam_task_id")
            self.assertEqual(foreign_keys[0][4], "id")
            self.assertEqual(foreign_keys[0][6], "CASCADE")

            unique_indexes = [
                row
                for row in conn.execute("PRAGMA index_list(practical_task_resources)").fetchall()
                if row[2]
            ]
            unique_columns = []
            for row in unique_indexes:
                columns_for_index = [
                    column_row[2]
                    for column_row in conn.execute(f"PRAGMA index_info({row[1]})").fetchall()
                ]
                unique_columns.append(columns_for_index)
            self.assertIn(["exam_task_id", "resource_path"], unique_columns)

            conn.execute("INSERT INTO exam_tasks (id) VALUES (26)")
            conn.execute("""
                INSERT INTO practical_task_resources (
                    exam_task_id, resource_path, original_filename, label_bg,
                    file_size_bytes, sha256
                )
                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                26,
                "data/reference/May_2025/zad_26/Shipments.xlsx",
                "Shipments.xlsx",
                "Task 26 resource",
                12345,
                "a" * 64,
            ))

            stored = conn.execute("""
                SELECT exam_task_id, resource_path, original_filename, label_bg,
                       file_size_bytes, sha256
                FROM practical_task_resources
            """).fetchone()
            self.assertEqual(
                stored,
                (
                    26,
                    "data/reference/May_2025/zad_26/Shipments.xlsx",
                    "Shipments.xlsx",
                    "Task 26 resource",
                    12345,
                    "a" * 64,
                ),
            )

            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("""
                    INSERT INTO practical_task_resources (exam_task_id, resource_path)
                    VALUES (?, ?)
                """, (26, "data/reference/May_2025/zad_26/Shipments.xlsx"))

            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("""
                    INSERT INTO practical_task_resources (exam_task_id, resource_path)
                    VALUES (?, ?)
                """, (999, "data/reference/May_2025/zad_26/missing.xlsx"))

            conn.execute("DELETE FROM exam_tasks WHERE id = 26")
            remaining = conn.execute("SELECT COUNT(*) FROM practical_task_resources").fetchone()[0]
            self.assertEqual(remaining, 0)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
