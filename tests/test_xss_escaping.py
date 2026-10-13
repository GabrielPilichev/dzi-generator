"""HTML-escaping / XSS regression tests for user-controlled content.

These tests do not assert that any text is *sanitized at write time* — the
codebase intentionally stores user input verbatim and relies on Jinja2
autoescape at render time. The tests below lock in that contract:

- Every place a user-supplied value (filename, student name, teacher note,
  open answer, accepted answer) is rendered, the raw HTML payload must not
  appear and the HTML-escaped form must appear instead.
- Practical-page templates must never expose ``stored_path``.

If a regression slips a ``|safe`` filter or a ``Markup(...)`` wrapper onto
any of these fields, the corresponding test fails.
"""

import sqlite3
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch


from tests.auth_helpers import add_users_schema, create_account, set_account_session, sign_in_admin
from tests.support import IsolatedAppTestCase, web_app
from tests.support import add_lifecycle_schema
from web import oauth_state
from web.services import google_oidc, identities, oidc_accounts


SCRIPT_PAYLOAD = '<script>alert("xss")</script>'
SCRIPT_PAYLOAD_ESCAPED = "&lt;script&gt;alert(&#34;xss&#34;)&lt;/script&gt;"

IMG_FILENAME_PAYLOAD = '<img src=x onerror=alert(1)>.zip'
IMG_FILENAME_ESCAPED_FRAGMENT = "&lt;img src=x onerror=alert(1)&gt;.zip"

STUDENT_NAME_PAYLOAD = '<svg onload=alert("name")>'
STUDENT_NAME_ESCAPED_FRAGMENT = "&lt;svg onload=alert(&#34;name&#34;)&gt;"

TEACHER_NOTE_PAYLOAD = '<script>alert("teacher-note")</script>'
TEACHER_NOTE_ESCAPED_FRAGMENT = "&lt;script&gt;alert(&#34;teacher-note&#34;)&lt;/script&gt;"


# ---------------------------------------------------------------------------
# Practical: teacher review page
# ---------------------------------------------------------------------------

class PracticalTeacherReviewPageEscapingTest(IsolatedAppTestCase):
    """Mirrors the setUp in test_practical_submission_review for isolation."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.project_root = Path(self.temp_dir.name)
        self.db_path = self.project_root / "questions.db"
        self.upload_root = self.project_root / "uploads" / "practical"
        self.stored_file = self.upload_root / "101" / "260" / "1" / "stored.xlsx"
        self.stored_file.parent.mkdir(parents=True)
        self.stored_file.write_bytes(b"solution bytes")

        self._create_db()

        self.old_project_root = web_app.PROJECT_ROOT
        self.old_db_path = web_app.app.config["DB_PATH"]
        self.old_upload_root = web_app.app.config["PRACTICAL_UPLOAD_ROOT"]
        self.addCleanup(self._restore_app_config)

        web_app.PROJECT_ROOT = self.project_root
        web_app.app.config.update(
            TESTING=True,
            DB_PATH=str(self.db_path),
            PRACTICAL_UPLOAD_ROOT=str(self.upload_root),
        )
        self.client = web_app.app.test_client()
        sign_in_admin(self.client, self.db_path)

    def _restore_app_config(self):
        web_app.PROJECT_ROOT = self.old_project_root
        web_app.app.config["DB_PATH"] = self.old_db_path
        web_app.app.config["PRACTICAL_UPLOAD_ROOT"] = self.old_upload_root

    def _create_db(self):
        conn = sqlite3.connect(self.db_path)
        try:
            add_users_schema(conn)
            conn.executescript("""
                CREATE TABLE exams (
                    id INTEGER PRIMARY KEY,
                    subject TEXT, level TEXT, year INTEGER,
                    session TEXT, variant INTEGER, format_version TEXT
                );
                CREATE TABLE quiz_assignments (id INTEGER PRIMARY KEY, title_bg TEXT);
                CREATE TABLE quiz_attempts (
                    id INTEGER PRIMARY KEY,
                    user_id INTEGER,
                    assignment_id INTEGER, student_name TEXT,
                    seed TEXT, question_ids_json TEXT, submitted_at TEXT
                );
                CREATE TABLE exam_tasks (
                    id INTEGER PRIMARY KEY,
                    exam_id INTEGER NOT NULL, task_number INTEGER NOT NULL,
                    task_kind TEXT NOT NULL, points INTEGER NOT NULL
                );
                CREATE TABLE practical_submissions (
                    id INTEGER PRIMARY KEY,
                    quiz_attempt_id INTEGER NOT NULL,
                    exam_task_id INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'draft',
                    manual_score REAL, manual_score_max REAL,
                    teacher_note TEXT, reviewed_at TEXT,
                    submitted_at TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE practical_submission_files (
                    id INTEGER PRIMARY KEY,
                    practical_submission_id INTEGER NOT NULL,
                    stored_path TEXT NOT NULL,
                    original_filename TEXT NOT NULL,
                    size_bytes INTEGER, mime_type TEXT,
                    uploaded_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    is_deleted INTEGER NOT NULL DEFAULT 0
                );
            """)
            conn.execute("""
                INSERT INTO exams (id, subject, level, year, session, variant, format_version)
                VALUES (6, 'informatika_it', 'DZI', 2025, 'may', 2, 'dzi_it_pp_2025_format')
            """)
            add_lifecycle_schema(conn)
            conn.execute("INSERT INTO quiz_assignments (id, title_bg) VALUES (1, 'ДЗИ проба')")
            conn.execute("""
                INSERT INTO quiz_attempts (id, assignment_id, student_name, seed, question_ids_json)
                VALUES (101, 1, ?, 'seed', '[]')
            """, (STUDENT_NAME_PAYLOAD,))
            conn.execute("""
                INSERT INTO exam_tasks (id, exam_id, task_number, task_kind, points)
                VALUES (260, 6, 26, 'practical_spreadsheet', 15)
            """)
            conn.execute("""
                INSERT INTO practical_submissions (
                    id, quiz_attempt_id, exam_task_id, status, updated_at
                )
                VALUES (1, 101, 260, 'draft', '2026-05-13 09:00:00')
            """)
            conn.execute("""
                INSERT INTO practical_submission_files (
                    id, practical_submission_id, stored_path, original_filename,
                    size_bytes, mime_type, uploaded_at, is_deleted
                )
                VALUES (
                    1, 1,
                    'data/uploads/practical/101/260/1/stored.xlsx',
                    ?, 14,
                    'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                    '2026-05-13 09:00:01', 0
                )
            """, (IMG_FILENAME_PAYLOAD,))
            conn.commit()
        finally:
            conn.close()

    def _get_review_html(self) -> str:
        response = self.client.get("/teacher/practical-submissions")
        self.assertEqual(response.status_code, 200)
        return response.get_data(as_text=True)

    def test_uploaded_filename_with_html_payload_is_escaped(self):
        html = self._get_review_html()
        self.assertNotIn(IMG_FILENAME_PAYLOAD, html)
        self.assertIn(IMG_FILENAME_ESCAPED_FRAGMENT, html)

    def test_student_name_with_html_payload_is_escaped(self):
        html = self._get_review_html()
        self.assertNotIn(STUDENT_NAME_PAYLOAD, html)
        self.assertIn(STUDENT_NAME_ESCAPED_FRAGMENT, html)

    def test_stored_path_is_not_exposed_on_teacher_review_page(self):
        html = self._get_review_html()
        self.assertNotIn("data/uploads/practical/101/260/1/stored.xlsx", html)
        self.assertNotIn(str(self.upload_root), html)


# ---------------------------------------------------------------------------
# Practical: student status page
# ---------------------------------------------------------------------------

class PracticalStudentPageEscapingTest(IsolatedAppTestCase):
    """Mirrors test_practical_student_uploads setUp; inserts payloads directly."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.project_root = Path(self.temp_dir.name)
        self.db_path = self.project_root / "questions.db"
        self.batch_dir = self.project_root / "data" / "import_batches"
        self.upload_root = self.project_root / "uploads" / "practical"
        self.batch_dir.mkdir(parents=True)
        self._create_db()
        self._write_batch()
        self._insert_payload_submission()

        self.old_project_root = web_app.PROJECT_ROOT
        self.old_db_path = web_app.app.config["DB_PATH"]
        self.old_batch_dir = web_app.app.config["PRACTICAL_TASK_BATCH_DIR"]
        self.old_upload_root = web_app.app.config["PRACTICAL_UPLOAD_ROOT"]
        self.addCleanup(self._restore_app_config)

        web_app.PROJECT_ROOT = self.project_root
        web_app.app.config.update(
            TESTING=True,
            DB_PATH=str(self.db_path),
            PRACTICAL_TASK_BATCH_DIR=str(self.batch_dir),
            PRACTICAL_UPLOAD_ROOT=str(self.upload_root),
        )
        self.client = web_app.app.test_client()
        with self.client.session_transaction() as sess:
            sess[web_app.quiz_attempt_owner_session_key(101)] = True

    def _restore_app_config(self):
        web_app.PROJECT_ROOT = self.old_project_root
        web_app.app.config["DB_PATH"] = self.old_db_path
        web_app.app.config["PRACTICAL_TASK_BATCH_DIR"] = self.old_batch_dir
        web_app.app.config["PRACTICAL_UPLOAD_ROOT"] = self.old_upload_root

    def _create_db(self):
        conn = sqlite3.connect(self.db_path)
        try:
            conn.executescript("""
                CREATE TABLE exams (
                    id INTEGER PRIMARY KEY,
                    subject TEXT, level TEXT, year INTEGER,
                    session TEXT, variant INTEGER, format_version TEXT
                );
                CREATE TABLE quiz_attempts (
                    id INTEGER PRIMARY KEY, assignment_id INTEGER,
                    user_id INTEGER,
                    student_name TEXT, seed TEXT,
                    question_ids_json TEXT, submitted_at TEXT
                );
                CREATE TABLE exam_tasks (
                    id INTEGER PRIMARY KEY,
                    exam_id INTEGER NOT NULL, task_number INTEGER NOT NULL,
                    task_kind TEXT NOT NULL, points INTEGER NOT NULL,
                    prompt TEXT, rubric TEXT
                );
                CREATE TABLE practical_tasks (
                    task_id INTEGER PRIMARY KEY,
                    work_environment TEXT, expected_outputs_json TEXT, notes TEXT
                );
                CREATE TABLE practical_task_resources (
                    id INTEGER PRIMARY KEY, exam_task_id INTEGER NOT NULL,
                    resource_path TEXT NOT NULL, original_filename TEXT,
                    label_bg TEXT, file_size_bytes INTEGER, sha256 TEXT
                );
                CREATE TABLE practical_submissions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    quiz_attempt_id INTEGER NOT NULL,
                    exam_task_id INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'draft',
                    manual_score REAL, manual_score_max REAL,
                    teacher_note TEXT, reviewed_at TEXT,
                    submitted_at TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE (quiz_attempt_id, exam_task_id)
                );
                CREATE TABLE practical_submission_files (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    practical_submission_id INTEGER NOT NULL,
                    stored_path TEXT NOT NULL UNIQUE,
                    original_filename TEXT NOT NULL,
                    size_bytes INTEGER, mime_type TEXT,
                    uploaded_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    is_deleted INTEGER NOT NULL DEFAULT 0
                );
            """)
            conn.execute("""
                INSERT INTO exams (id, subject, level, year, session, variant, format_version)
                VALUES (6, 'informatika_it', 'DZI', 2025, 'may', 2, 'dzi_it_pp_2025_format')
            """)
            conn.execute("""
                INSERT INTO quiz_attempts (id, assignment_id, student_name, seed, question_ids_json)
                VALUES (101, 1, 'Student One', 'seed', '[]')
            """)
            add_lifecycle_schema(conn, assignment_ids=(1,))
            conn.execute("""
                INSERT INTO exam_tasks (id, exam_id, task_number, task_kind, points)
                VALUES (260, 6, 26, 'practical_spreadsheet', 15)
            """)
            conn.execute("""
                INSERT INTO practical_tasks (task_id, work_environment)
                VALUES (260, 'spreadsheet')
            """)
            conn.commit()
        finally:
            conn.close()

    def _write_batch(self):
        import json
        payload = {
            "source_slug": "may_2025_v2",
            "tasks": [{
                "task_number": 26,
                "prompt_bg": "Task 26 prompt",
                "instructions_bg": "Task 26 instructions",
                "expected_outputs": ["solution.xlsx"],
            }],
        }
        (self.batch_dir / "may_2025_v2_practical_tasks_26_28.json").write_text(
            json.dumps(payload, ensure_ascii=False),
            encoding="utf-8",
        )

    def _insert_payload_submission(self):
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("""
                INSERT INTO practical_submissions (
                    id, quiz_attempt_id, exam_task_id, status,
                    manual_score, manual_score_max, teacher_note, reviewed_at,
                    updated_at
                )
                VALUES (
                    1, 101, 260, 'reviewed',
                    10, 15, ?, '2026-05-13 09:30:00',
                    '2026-05-13 09:30:00'
                )
            """, (TEACHER_NOTE_PAYLOAD,))
            conn.execute("""
                INSERT INTO practical_submission_files (
                    id, practical_submission_id, stored_path, original_filename,
                    size_bytes, uploaded_at, is_deleted
                )
                VALUES (
                    1, 1,
                    'data/uploads/practical/101/260/1/stored.xlsx',
                    ?, 14,
                    '2026-05-13 09:00:01', 0
                )
            """, (IMG_FILENAME_PAYLOAD,))
            conn.commit()
        finally:
            conn.close()

    def _get_student_html(self) -> str:
        response = self.client.get("/quiz/attempt/101/practical/may_2025_v2")
        self.assertEqual(response.status_code, 200)
        return response.get_data(as_text=True)

    def test_uploaded_filename_with_html_payload_is_escaped_on_student_page(self):
        html = self._get_student_html()
        self.assertNotIn(IMG_FILENAME_PAYLOAD, html)
        self.assertIn(IMG_FILENAME_ESCAPED_FRAGMENT, html)

    def test_teacher_note_with_html_payload_is_escaped_on_student_page(self):
        html = self._get_student_html()
        self.assertNotIn(TEACHER_NOTE_PAYLOAD, html)
        self.assertIn(TEACHER_NOTE_ESCAPED_FRAGMENT, html)
        # Sanity: the surrounding Bulgarian label is preserved.
        self.assertIn("Бележка от проверяващ", html)

    def test_stored_path_is_not_exposed_on_student_page(self):
        html = self._get_student_html()
        self.assertNotIn("data/uploads/practical/101/260/1/stored.xlsx", html)
        self.assertNotIn(str(self.upload_root), html)

    def test_upload_helper_strips_html_chars_from_submitted_filenames(self):
        """The helper sanitizes ``<`` and ``>`` to ``_`` before storage.

        This is defense-in-depth alongside the template-side escaping above:
        even if a future template change accidentally bypassed autoescape,
        no executable HTML chars would survive in original_filename.
        """
        from src.practical_uploads import sanitize_original_filename
        sanitized = sanitize_original_filename(IMG_FILENAME_PAYLOAD)
        self.assertNotIn("<", sanitized)
        self.assertNotIn(">", sanitized)
        self.assertTrue(sanitized.endswith(".zip"))


# ---------------------------------------------------------------------------
# Google sign-in: what a Google profile supplies
# ---------------------------------------------------------------------------

class GoogleProvidedTextEscapingTest(IsolatedAppTestCase):
    """The name and the address of a Google profile are chosen by whoever owns it.

    They are stored verbatim (the name of an account created through Google, the
    address kept with the identity) and held in the session while a link awaits its
    confirmation. Every page that shows them escapes them.
    """

    NAME = '<svg onload=alert("name")>'
    NAME_ESCAPED = "&lt;svg onload=alert(&#34;name&#34;)&gt;"
    EMAIL = '"><img/src=x/onerror=alert(1)>@example.bg'
    EMAIL_ESCAPED = "&#34;&gt;&lt;img/src=x/onerror=alert(1)&gt;@example.bg"

    def setUp(self):
        self.app = web_app.app
        self.db_path = self.app.config["DB_PATH"]
        provider = google_oidc.provider_config(
            "tests.apps.googleusercontent.com", "synthetic-client-secret", "http://localhost")
        enabled = patch.dict(self.app.config, {oauth_state.PROVIDER_CONFIG_KEY: provider})
        enabled.start()
        self.addCleanup(enabled.stop)
        self.client = self.app.test_client()

    def assert_escaped(self, html):
        for raw in ("<svg onload", "<img/src=x"):
            self.assertNotIn(raw, html)

    def test_the_account_page_of_an_account_created_through_google(self):
        account = create_account(self.db_path, "student", display_name=self.NAME)
        with closing(sqlite3.connect(self.db_path)) as conn:
            identities.add_identity(conn, account["id"], issuer=google_oidc.ISSUER, subject="hostile-subject",
                                    provider="google", email_at_link=self.EMAIL)
            conn.commit()
        set_account_session(self.client, account["id"], account["session_key"])
        for path in ("/account", "/account?google=linked", "/", "/account/password"):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200)
                html = response.get_data(as_text=True)
                self.assert_escaped(html)
                self.assertIn(self.NAME_ESCAPED, html)
        self.assertIn(self.EMAIL_ESCAPED, self.client.get("/account").get_data(as_text=True))

    def test_the_confirmation_page_of_a_link(self):
        account = create_account(self.db_path, "teacher", display_name=self.NAME, email="t-xss@example.bg",
                                 password="correct horse battery")
        issued_at = int(time.time())
        set_account_session(self.client, account["id"], account["session_key"], iat=issued_at)
        with self.client.session_transaction() as sess:
            sess[oauth_state.LINK_SLOT] = {
                "v": 1, "id": "i" * 43, "inst": oauth_state.INSTANCE, "exp": oauth_state.monotonic_seconds() + 120,
                "uid": account["id"],
                "skd": oidc_accounts.session_key_digest(self.app.config["SECRET_KEY"], account["session_key"]),
                "siat": issued_at, "sub": "hostile-subject", "email": self.EMAIL, "name": self.NAME,
            }
        response = self.client.get("/auth/google/link/confirm")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assert_escaped(html)
        # Both profiles are named, and both names are escaped.
        self.assertEqual(html.count(self.NAME_ESCAPED), 3)   # the Google name, the account's, and the header
        self.assertIn(self.EMAIL_ESCAPED, html)
        # A wrong password renders the page again, still escaped.
        again = self.client.post("/auth/google/link/confirm", data={"current_password": "wrong"},
                                 headers={"Origin": "http://localhost"}).get_data(as_text=True)
        self.assert_escaped(again)
        self.assertIn("Грешна текуща парола.", again)

    def test_an_outcome_code_is_looked_up_and_never_echoed(self):
        account = create_account(self.db_path, "student")
        set_account_session(self.client, account["id"], account["session_key"])
        payload = '<script>alert("code")</script>'
        for path in ("/account", "/login"):
            for client in (self.client, self.app.test_client()):
                html = client.get(path, query_string={"google": payload}, follow_redirects=True).get_data(as_text=True)
                self.assertNotIn(payload, html)
                self.assertNotIn("alert(&#34;code&#34;)", html)


if __name__ == "__main__":
    unittest.main()
