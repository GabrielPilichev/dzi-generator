from __future__ import annotations

import json
import sqlite3
import stat
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import Mock

from src.practical_resource_resolution import (
    MAX_MEMBER_BYTES,
    ResourceResolutionError,
    _zip_info,
    read_zip_member,
    resolve_resource_reference,
)

from tests.auth_helpers import add_users_schema, sign_in_admin, sign_in_teacher
from tests.support import IsolatedAppTestCase, web_app
from web.services import practical as practical_service


class PracticalTaskDownloadsTest(IsolatedAppTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.project_root = Path(self.temp_dir.name)
        self.db_path = self.project_root / "questions.db"
        self.batch_dir = self.project_root / "data" / "import_batches"
        self.resource_dir = self.project_root / "data" / "reference" / "may_2025_v2"
        self.batch_dir.mkdir(parents=True)
        self.resource_dir.mkdir(parents=True)

        self.resource_path = Path("data/reference/may_2025_v2/Shipments.xlsx")
        (self.project_root / self.resource_path).write_bytes(b"fake practical resource")
        self.missing_resource_path = Path("data/reference/may_2025_v2/missing.xlsx")
        self.unicode_resource_path = Path("data/reference/may_2025_v2/данни.xlsx")
        (self.project_root / self.unicode_resource_path).write_bytes(b"unicode resource")

        self._create_db()
        self._write_batch()

        self.old_config = {
            key: web_app.app.config[key]
            for key in ("PROJECT_ROOT", "PRACTICAL_RESOURCE_ROOTS", "PRACTICAL_REFERENCE_ROOT")
        }
        self.old_db_path = web_app.app.config["DB_PATH"]
        self.old_batch_dir = web_app.app.config["PRACTICAL_TASK_BATCH_DIR"]
        self.addCleanup(self._restore_app_config)

        web_app.app.config.update(
            TESTING=True,
            PROJECT_ROOT=self.project_root,
            PRACTICAL_RESOURCE_ROOTS=(
                self.project_root / "data" / "reference",
                self.project_root / "data" / "assets",
            ),
            DB_PATH=str(self.db_path),
            PRACTICAL_TASK_BATCH_DIR=str(self.batch_dir),
        )
        self.client = web_app.app.test_client()
        sign_in_admin(self.client, self.db_path)

    def _restore_app_config(self):
        web_app.app.config.update(self.old_config)
        web_app.app.config["DB_PATH"] = self.old_db_path
        web_app.app.config["PRACTICAL_TASK_BATCH_DIR"] = self.old_batch_dir

    def _create_db(self):
        conn = sqlite3.connect(self.db_path)
        try:
            add_users_schema(conn)
            conn.executescript("""
                CREATE TABLE exams (
                    id INTEGER PRIMARY KEY,
                    subject TEXT,
                    level TEXT,
                    year INTEGER,
                    session TEXT,
                    variant INTEGER,
                    format_version TEXT
                );
                CREATE TABLE exam_tasks (
                    id INTEGER PRIMARY KEY,
                    exam_id INTEGER NOT NULL,
                    task_number INTEGER NOT NULL,
                    task_kind TEXT NOT NULL,
                    points INTEGER NOT NULL,
                    has_assets INTEGER DEFAULT 0,
                    prompt TEXT,
                    rubric TEXT,
                    topic_id INTEGER
                );
                CREATE TABLE exam_task_questions (
                    task_id INTEGER NOT NULL,
                    question_id INTEGER NOT NULL,
                    role TEXT NOT NULL
                );
                CREATE TABLE questions (
                    id INTEGER PRIMARY KEY,
                    prompt TEXT,
                    question_type TEXT,
                    topic_id INTEGER,
                    is_ai_generated INTEGER DEFAULT 0,
                    quality_score REAL
                );
                CREATE TABLE curriculum_topics (
                    id INTEGER PRIMARY KEY,
                    title_bg TEXT
                );
                CREATE TABLE asset_links (
                    id INTEGER PRIMARY KEY,
                    asset_id INTEGER,
                    owner_type TEXT,
                    owner_id INTEGER,
                    role TEXT
                );
                CREATE TABLE practical_tasks (
                    task_id INTEGER PRIMARY KEY,
                    work_environment TEXT,
                    expected_outputs_json TEXT,
                    notes TEXT
                );
                CREATE TABLE practical_task_resources (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    exam_task_id INTEGER NOT NULL,
                    resource_path TEXT NOT NULL,
                    original_filename TEXT,
                    label_bg TEXT,
                    file_size_bytes INTEGER,
                    sha256 TEXT,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE (exam_task_id, resource_path)
                );
            """)
            conn.execute("""
                INSERT INTO exams (
                    id, subject, level, year, session, variant, format_version
                )
                VALUES (6, 'informatika_it', 'DZI', 2025, 'may', 2, 'dzi_it_pp_2025_format')
            """)
            conn.executemany(
                """
                INSERT INTO exam_tasks (id, exam_id, task_number, task_kind, points)
                VALUES (?, 6, ?, ?, ?)
                """,
                [
                    (260, 26, "practical_spreadsheet", 15),
                    (270, 27, "practical_graphics", 20),
                    (280, 28, "practical_web", 20),
                ],
            )
            conn.executemany(
                """
                INSERT INTO practical_tasks (task_id, work_environment)
                VALUES (?, ?)
                """,
                [(260, "spreadsheet"), (270, "graphics"), (280, "web")],
            )
            conn.executemany(
                """
                INSERT INTO practical_task_resources (
                    id, exam_task_id, resource_path, original_filename, label_bg, file_size_bytes, sha256
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (1, 260, str(self.resource_path), "Shipments.xlsx", None, 23, "a" * 64),
                    (2, 270, str(self.missing_resource_path), "missing.xlsx", None, 10, "b" * 64),
                    (3, 280, str(self.unicode_resource_path), "данни.xlsx", "Данни", 16, "d" * 64),
                ],
            )
            conn.commit()
        finally:
            conn.close()

    def _write_batch(self):
        payload = {
            "source_slug": "may_2025_v2",
            "tasks": [
                {
                    "task_number": 26,
                    "prompt_bg": "Задача 26 prompt",
                    "instructions_bg": "Instruction line one\nInstruction line two",
                    "expected_outputs": ["it_23.05.2025_zad_26.xlsx"],
                },
                {
                    "task_number": 27,
                    "prompt_bg": "Task 27 prompt",
                    "instructions_bg": "Task 27 instructions",
                    "expected_outputs": ["task27.zip"],
                },
                {
                    "task_number": 28,
                    "prompt_bg": "Task 28 prompt",
                    "instructions_bg": "Task 28 instructions",
                    "expected_outputs": ["index.html"],
                },
            ],
        }
        (self.batch_dir / "may_2025_v2_practical_tasks_26_28.json").write_text(
            json.dumps(payload, ensure_ascii=False),
            encoding="utf-8",
        )

    def _insert_resource(self, resource_path: str, filename: str = "bad.txt") -> int:
        conn = sqlite3.connect(self.db_path)
        try:
            cur = conn.execute(
                """
                INSERT INTO practical_task_resources (
                    exam_task_id, resource_path, original_filename, file_size_bytes, sha256
                )
                VALUES (260, ?, ?, 1, ?)
                """,
                (resource_path, filename, "c" * 64),
            )
            conn.commit()
            return int(cur.lastrowid)
        finally:
            conn.close()

    def _zip_reference(self, member: str, data: bytes = b"zip member bytes",
                       *, filename: str | None = None) -> int:
        zip_path = self.resource_dir / "resources.zip"
        with zipfile.ZipFile(zip_path, "a", compression=zipfile.ZIP_STORED) as archive:
            archive.writestr(member, data)
        return self._insert_resource(
            "data/reference/may_2025_v2/resources.zip::" + member,
            filename or Path(member).name,
        )

    def _resolve(self, reference: str):
        return resolve_resource_reference(
            reference,
            project_root=self.project_root,
            allowed_roots=web_app.app.config["PRACTICAL_RESOURCE_ROOTS"],
        )

    def test_page_renders_may_2025_practical_tasks_and_resources(self):
        response = self.client.get("/dzi/source/may_2025_v2/practical")

        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("Практически задачи", html)
        self.assertIn("Задача 26", html)
        self.assertIn("Задача 27", html)
        self.assertIn("Задача 28", html)
        self.assertIn("Instruction line one", html)
        self.assertIn("Shipments.xlsx", html)
        self.assertIn("/dzi/practical/resource/1/download", html)
        self.assertIn(
            '<body class="management-page admin-management-page management-detail-page">',
            html,
        )

    def test_dzi_source_page_links_to_practical_tasks_when_resources_exist(self):
        response = self.client.get("/dzi/source/may_2025_v2")

        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("Практически задачи 26–28", html)
        self.assertIn("/dzi/source/may_2025_v2/practical", html)
        self.assertIn("3 файла за изтегляне", html)

    def test_dzi_source_page_does_not_expose_raw_resource_paths(self):
        response = self.client.get("/dzi/source/may_2025_v2")

        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertNotIn("data/reference/", html)
        self.assertNotIn(str(self.project_root), html)

    def test_download_route_returns_valid_resource_file(self):
        response = self.client.get("/dzi/practical/resource/1/download")

        try:
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.data, b"fake practical resource")
            self.assertIn("attachment", response.headers["Content-Disposition"])
            self.assertIn("Shipments.xlsx", response.headers["Content-Disposition"])
        finally:
            response.close()

    def test_download_route_handles_unicode_original_filename(self):
        response = self.client.get("/dzi/practical/resource/3/download")

        try:
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.data, b"unicode resource")
            disposition = response.headers["Content-Disposition"]
            self.assertIn("attachment", disposition)
            self.assertIn("filename", disposition)
            self.assertNotIn(str(self.project_root), disposition)
        finally:
            response.close()

    def test_nonexistent_resource_id_returns_404(self):
        response = self.client.get("/dzi/practical/resource/999/download")

        self.assertEqual(response.status_code, 404)

    def test_path_traversal_resource_row_is_rejected(self):
        resource_id = self._insert_resource("../secret.txt")

        response = self.client.get(f"/dzi/practical/resource/{resource_id}/download")

        self.assertEqual(response.status_code, 404)

    def test_absolute_path_resource_row_is_rejected(self):
        resource_id = self._insert_resource("/etc/passwd", "passwd")

        response = self.client.get(f"/dzi/practical/resource/{resource_id}/download")

        self.assertEqual(response.status_code, 404)

    def test_missing_file_does_not_crash_page_or_download_route(self):
        page = self.client.get("/dzi/source/may_2025_v2/practical")
        download = self.client.get("/dzi/practical/resource/2/download")

        self.assertEqual(page.status_code, 200)
        self.assertIn("файлът липсва", page.get_data(as_text=True))
        self.assertEqual(download.status_code, 404)

    def test_missing_zip_archive_does_not_crash_download_route(self):
        resource_id = self._insert_resource(
            "data/reference/may_2025_v2/resources.zip::task_26/Shipments.xlsx",
            "Shipments.xlsx",
        )

        response = self.client.get(f"/dzi/practical/resource/{resource_id}/download")

        self.assertEqual(response.status_code, 404)

    def test_zip_member_download_returns_bytes_and_safe_filename(self):
        resource_id = self._zip_reference("task_26/Shipments.xlsx", b"spreadsheet bytes")
        response = self.client.get(f"/dzi/practical/resource/{resource_id}/download")
        try:
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.data, b"spreadsheet bytes")
            self.assertIn("attachment", response.headers["Content-Disposition"])
            self.assertIn("Shipments.xlsx", response.headers["Content-Disposition"])
            self.assertEqual(response.headers["Content-Type"], "application/octet-stream")
            self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
        finally:
            response.close()
        page = self.client.get("/dzi/source/may_2025_v2/practical")
        self.assertIn(f"/dzi/practical/resource/{resource_id}/download",
                      page.get_data(as_text=True))

    def test_nested_unicode_member_resolves_without_extraction(self):
        member = "task_28/nested/данни.xlsx"
        resource_id = self._zip_reference(member, b"nested contents")
        response = self.client.get(f"/dzi/practical/resource/{resource_id}/download")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, b"nested contents")
        self.assertIn("attachment", response.headers["Content-Disposition"])
        self.assertFalse((self.resource_dir / "task_28").exists())

    def test_ordinary_file_still_resolves_through_same_helper(self):
        resolved = self._resolve(str(self.resource_path))
        self.assertEqual(resolved.kind, "file")
        self.assertEqual(resolved.path.read_bytes(), b"fake practical resource")

    def _use_external_reference_root(self) -> Path:
        """Reference data outside the checkout, as DZI_REFERENCE_ROOT configures it."""
        root = self.project_root / "state" / "reference"
        (root / "may_2025_v2").mkdir(parents=True)
        (root / "may_2025_v2" / "Shipments.xlsx").write_bytes(b"external reference bytes")
        web_app.app.config["PRACTICAL_REFERENCE_ROOT"] = root
        web_app.app.config["PRACTICAL_RESOURCE_ROOTS"] = (root, self.project_root / "data" / "assets")
        return root

    def _configured_resources(self):
        return practical_service.resources_from_config(web_app.app.config)

    def _resolve_configured(self, reference: str):
        return resolve_resource_reference(
            reference,
            project_root=self.project_root,
            allowed_roots=web_app.app.config["PRACTICAL_RESOURCE_ROOTS"],
            reference_root=web_app.app.config["PRACTICAL_REFERENCE_ROOT"],
        )

    def test_configured_reference_root_serves_files_and_zip_members(self):
        root = self._use_external_reference_root()
        with zipfile.ZipFile(root / "may_2025_v2" / "external.zip", "w") as archive:
            archive.writestr("task_26/data.xlsx", b"external member")
        member_id = self._insert_resource(
            "data/reference/may_2025_v2/external.zip::task_26/data.xlsx", "data.xlsx")

        ordinary = self.client.get("/dzi/practical/resource/1/download")
        try:
            self.assertEqual(ordinary.status_code, 200)
            self.assertEqual(ordinary.data, b"external reference bytes")
        finally:
            ordinary.close()
        member = self.client.get(f"/dzi/practical/resource/{member_id}/download")
        self.assertEqual(member.status_code, 200)
        self.assertEqual(member.data, b"external member")
        self.assertTrue(practical_service.practical_resource_status(
            {"resource_path": str(self.resource_path)}, self._configured_resources())["is_available"])

    def test_configured_reference_root_never_falls_back_to_the_checkout(self):
        self._use_external_reference_root()
        # Present only in the checkout's data/reference.
        self.assertTrue((self.project_root / self.unicode_resource_path).is_file())
        with self.assertRaisesRegex(ResourceResolutionError, "missing_file"):
            self._resolve_configured(str(self.unicode_resource_path))
        status = practical_service.practical_resource_status(
            {"resource_path": str(self.unicode_resource_path)}, self._configured_resources())
        self.assertTrue(status["is_path_safe"])
        self.assertFalse(status["is_available"])

    def test_configured_reference_root_does_not_redirect_assets(self):
        root = self._use_external_reference_root()
        asset = self.project_root / "data" / "assets" / "exams" / "figure.png"
        asset.parent.mkdir(parents=True)
        asset.write_bytes(b"checkout asset")
        (root / "exams").mkdir()
        (root / "exams" / "figure.png").write_bytes(b"not an asset")
        (root.parent / "assets" / "exams").mkdir(parents=True)
        (root.parent / "assets" / "exams" / "figure.png").write_bytes(b"not an asset")

        resolved = self._resolve_configured("data/assets/exams/figure.png")
        self.assertEqual(resolved.path, asset.resolve())
        self.assertEqual(resolved.path.read_bytes(), b"checkout asset")

    def test_configured_reference_root_keeps_containment_checks(self):
        root = self._use_external_reference_root()
        outside = self.project_root / "state" / "secret.txt"
        outside.write_bytes(b"outside")
        (root / "link.txt").symlink_to(outside)
        for reference in ("data/reference/link.txt", "data/reference/../secret.txt",
                          "data/reference", "data/referenceX/may_2025_v2/Shipments.xlsx",
                          str(root / "may_2025_v2" / "Shipments.xlsx")):
            with self.subTest(reference=reference):
                with self.assertRaises(ResourceResolutionError) as caught:
                    self._resolve_configured(reference)
                self.assertIn(caught.exception.reason, {"unsafe_path", "missing_file"})
        with self.assertRaisesRegex(ResourceResolutionError, "unsafe_path"):
            self._resolve_configured("data/reference/link.txt")

    def test_unconfigured_reference_root_keeps_checkout_resolution(self):
        self.assertIsNone(web_app.app.config["PRACTICAL_REFERENCE_ROOT"])
        self.assertEqual(self._resolve_configured(str(self.resource_path)).path,
                         (self.project_root / self.resource_path).resolve())

    def test_missing_member_and_directory_are_rejected(self):
        self._zip_reference("task_26/actual.xlsx")
        for member in ("task_26/missing.xlsx", "task_26/"):
            with self.subTest(member=member):
                resource_id = self._insert_resource(
                    "data/reference/may_2025_v2/resources.zip::" + member)
                response = self.client.get(f"/dzi/practical/resource/{resource_id}/download")
                self.assertEqual(response.status_code, 404)

    def test_archive_and_member_traversal_variants_are_rejected(self):
        self._zip_reference("task_26/actual.xlsx")
        bad_references = (
            "../reference/may_2025_v2/resources.zip::task_26/actual.xlsx",
            "data/reference/may_2025_v2/../resources.zip::task_26/actual.xlsx",
            "/etc/resources.zip::task_26/actual.xlsx",
            "C:/data/resources.zip::task_26/actual.xlsx",
            "\\\\server\\share\\resources.zip::task_26/actual.xlsx",
            "data/reference/may_2025_v2/resources.zip::../secret.xlsx",
            "data/reference/may_2025_v2/resources.zip::task_26/../secret.xlsx",
            "data/reference/may_2025_v2/resources.zip::/etc/passwd",
            "data/reference/may_2025_v2/resources.zip::C:/secret.xlsx",
            "data/reference/may_2025_v2/resources.zip::..\\secret.xlsx",
            "data/reference/may_2025_v2/resources.zip::\\\\server\\share\\secret.xlsx",
            "data/reference/may_2025_v2/resources.zip::task_26//actual.xlsx",
            "data/reference/may_2025_v2/resources.zip::task_26/./actual.xlsx",
            "data/reference/may_2025_v2/resources.zip::task_26/actual.xlsx::extra",
        )
        for reference in bad_references:
            with self.subTest(reference=reference):
                with self.assertRaises(ResourceResolutionError):
                    self._resolve(reference)
                resource_id = self._insert_resource(reference)
                self.assertEqual(self.client.get(
                    f"/dzi/practical/resource/{resource_id}/download").status_code, 404)

    def test_archive_symlink_outside_approved_root_is_rejected(self):
        outside = self.project_root / "outside.zip"
        with zipfile.ZipFile(outside, "w") as archive:
            archive.writestr("file.txt", b"outside")
        (self.resource_dir / "link.zip").symlink_to(outside)
        with self.assertRaisesRegex(ResourceResolutionError, "unsafe_path"):
            self._resolve("data/reference/may_2025_v2/link.zip::file.txt")

    def test_malformed_zip_and_nonzip_archive_are_rejected(self):
        (self.resource_dir / "broken.zip").write_bytes(b"not a zip")
        (self.resource_dir / "notzip.txt").write_bytes(b"not a zip")
        for reference in (
            "data/reference/may_2025_v2/broken.zip::file.txt",
            "data/reference/may_2025_v2/notzip.txt::file.txt",
        ):
            with self.subTest(reference=reference):
                resource_id = self._insert_resource(reference)
                self.assertEqual(self.client.get(
                    f"/dzi/practical/resource/{resource_id}/download").status_code, 404)

    def test_oversized_and_high_ratio_members_are_rejected(self):
        archive_path = self.resource_dir / "large.zip"
        with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_STORED) as archive:
            archive.writestr("big.bin", b"x" * (MAX_MEMBER_BYTES + 1))
        resource_id = self._insert_resource(
            "data/reference/may_2025_v2/large.zip::big.bin")
        self.assertEqual(self.client.get(
            f"/dzi/practical/resource/{resource_id}/download").status_code, 404)

        ratio_path = self.resource_dir / "ratio.zip"
        with zipfile.ZipFile(ratio_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("expands.txt", b"x" * 100_000)
        resource_id = self._insert_resource(
            "data/reference/may_2025_v2/ratio.zip::expands.txt")
        self.assertEqual(self.client.get(
            f"/dzi/practical/resource/{resource_id}/download").status_code, 404)

    def test_unsupported_compression_and_encrypted_metadata_are_rejected(self):
        path = self.resource_dir / "bzip.zip"
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_BZIP2) as archive:
            archive.writestr("file.txt", b"payload")
        resource_id = self._insert_resource(
            "data/reference/may_2025_v2/bzip.zip::file.txt")
        self.assertEqual(self.client.get(
            f"/dzi/practical/resource/{resource_id}/download").status_code, 404)
        info = zipfile.ZipInfo("secret.txt")
        info.flag_bits |= 1
        with self.assertRaisesRegex(ResourceResolutionError, "encrypted_member"):
            _zip_info(Mock(infolist=lambda: [info]), "secret.txt")

    def test_zip_symlink_and_mode_only_directory_members_are_rejected(self):
        path = self.resource_dir / "special.zip"
        link = zipfile.ZipInfo("link.txt")
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        directory = zipfile.ZipInfo("directory_without_slash")
        directory.create_system = 3
        directory.external_attr = (stat.S_IFDIR | 0o755) << 16
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr(link, "outside.txt")
            archive.writestr(directory, "")
        for member in ("link.txt", "directory_without_slash"):
            with self.subTest(member=member):
                resource_id = self._insert_resource(
                    f"data/reference/may_2025_v2/special.zip::{member}")
                self.assertEqual(self.client.get(
                    f"/dzi/practical/resource/{resource_id}/download"
                ).status_code, 404)

    def test_missing_record_and_constructed_path_never_bypass_lookup(self):
        self._zip_reference("task_26/actual.xlsx")
        self.assertEqual(self.client.get(
            "/dzi/practical/resource/99999/download").status_code, 404)
        self.assertEqual(self.client.get(
            "/dzi/practical/resource/data/reference/may_2025_v2/resources.zip::task_26/actual.xlsx/download"
        ).status_code, 404)

    def test_zip_and_ordinary_keep_existing_admin_only_access(self):
        resource_id = self._zip_reference("task_26/actual.xlsx")
        anonymous = web_app.app.test_client()
        for target in (1, resource_id):
            with self.subTest(target=target):
                url = f"/dzi/practical/resource/{target}/download"
                denied = anonymous.get(url)
                self.assertEqual(denied.status_code, 302)
                self.assertEqual(denied.headers["Location"], f"/login?next={url}")
                denied.close()
                teacher = web_app.app.test_client()
                sign_in_teacher(teacher, self.db_path)
                denied_teacher = teacher.get(url)
                self.assertEqual(denied_teacher.status_code, 403)
                denied_teacher.close()
                allowed = self.client.get(url)
                try:
                    self.assertEqual(allowed.status_code, 200)
                    self.assertIn("attachment", allowed.headers["Content-Disposition"])
                finally:
                    allowed.close()

    def test_zip_html_member_is_forced_to_download(self):
        resource_id = self._zip_reference("task_28/page.html", b"<script>bad()</script>")
        response = self.client.get(f"/dzi/practical/resource/{resource_id}/download")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["Content-Type"], "application/octet-stream")
        self.assertIn("attachment", response.headers["Content-Disposition"])
        self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")

    def test_page_does_not_expose_raw_filesystem_paths(self):
        response = self.client.get("/dzi/source/may_2025_v2/practical")

        html = response.get_data(as_text=True)
        self.assertNotIn("data/reference/", html)
        self.assertNotIn(str(self.project_root), html)


if __name__ == "__main__":
    unittest.main()
