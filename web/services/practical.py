"""Practical-task data and file-path rules (roadmap PR E2).

Everything the practical pages, the resource download, student uploads and the
teacher review pages need that is not HTTP: the task listing for an official
exam, resource availability, submission and review queries, and the confined
path of a stored upload.

Each function takes the SQLite connection it reads through and the roots it
resolves paths against, so the module owns neither connection policy nor
configuration and never imports ``web.app`` or a controller. The resource
roots, the batch directory and the upload root are plain application
configuration (``PRACTICAL_*`` keys), read by the controllers per request.

The resource and upload path-safety decisions themselves stay in
``src.practical_resource_resolution`` and ``src.practical_uploads``.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from src.subject_assessment import AssessmentScope
from src.subject_content import IT, canonical_subject
from src.practical_resource_resolution import ResourceResolutionError, resolve_resource_reference
from web.access import Principal, assignment_management_scope
from web.db_introspect import first_existing_column, sqlite_table_columns, sqlite_table_exists
from web.services import dzi_exams
from web.services.dzi_exams import DZI_FORMAT_VERSION, dzi_source_slug, dzi_source_title


@dataclass(frozen=True)
class PracticalResources:
    """Where official practical resources may be read from.

    ``reference_root`` is the optional ``DZI_REFERENCE_ROOT`` relocation of
    ``data/reference``; ``allowed_roots`` are the containment roots.
    """

    project_root: Path
    allowed_roots: tuple[Path, ...]
    reference_root: Path | None


def resources_from_config(config: Mapping) -> PracticalResources:
    return PracticalResources(
        project_root=Path(config["PROJECT_ROOT"]),
        allowed_roots=tuple(config["PRACTICAL_RESOURCE_ROOTS"]),
        reference_root=config["PRACTICAL_REFERENCE_ROOT"],
    )


def load_practical_task_batch(batch_dir: str | Path, source_slug: str) -> dict[int, dict]:
    path = Path(batch_dir) / f"{source_slug}_practical_tasks_26_28.json"
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if payload.get("source_slug") != source_slug:
        return {}
    tasks = payload.get("tasks")
    if not isinstance(tasks, list):
        return {}
    by_number = {}
    for task in tasks:
        if not isinstance(task, dict):
            continue
        number = task.get("task_number")
        if isinstance(number, int):
            by_number[number] = task
    return by_number


def practical_resource_status(resource: dict, resources: PracticalResources) -> dict:
    resource = dict(resource)
    try:
        resolve_resource_reference(
            resource.get("resource_path"),
            project_root=resources.project_root,
            allowed_roots=resources.allowed_roots,
            reference_root=resources.reference_root,
        )
    except ResourceResolutionError as exc:
        resource["is_path_safe"] = exc.reason not in {"unsafe_path", "invalid_archive"}
        resource["is_available"] = False
    else:
        resource["is_path_safe"] = True
        resource["is_available"] = True
    return resource


def safe_download_name(candidate: str | None, fallback: str) -> str:
    name = candidate if isinstance(candidate, str) else ""
    name = name.replace("\x00", "").replace("\r", "").replace("\n", "").strip()
    name = name.replace("/", "_").replace("\\", "_")
    return name or fallback


def fetch_practical_resource(db: sqlite3.Connection, resource_id: int) -> dict | None:
    row = db.execute("""
        SELECT
            ptr.id,
            ptr.exam_task_id,
            ptr.resource_path,
            ptr.original_filename,
            ptr.label_bg,
            ptr.file_size_bytes,
            ptr.sha256,
            et.task_number,
            et.exam_id,
            e.year,
            e.session,
            e.variant
        FROM practical_task_resources ptr
        JOIN exam_tasks et ON et.id = ptr.exam_task_id
        JOIN exams e ON e.id = et.exam_id
        WHERE ptr.id = ?
    """, (resource_id,)).fetchone()
    return dict(row) if row else None


def fetch_practical_tasks(
    db: sqlite3.Connection, source_slug: str, *, batch_dir: str | Path, resources: PracticalResources
) -> tuple[dict, list[dict]] | None:
    exam = dzi_exams.dzi_find_exam(db, source_slug)
    if exam is None:
        return None

    batch_tasks = load_practical_task_batch(batch_dir, source_slug)
    task_rows = db.execute("""
        SELECT
            et.id AS task_id,
            et.task_number,
            et.task_kind,
            et.points,
            et.prompt,
            et.rubric,
            pt.work_environment,
            pt.expected_outputs_json,
            pt.notes
        FROM exam_tasks et
        LEFT JOIN practical_tasks pt ON pt.task_id = et.id
        WHERE et.exam_id = ?
          AND et.task_number BETWEEN 26 AND 28
          AND et.task_kind LIKE 'practical_%'
        ORDER BY et.task_number
    """, (exam["id"],)).fetchall()

    tasks = []
    for row in task_rows:
        task = dict(row)
        batch_task = batch_tasks.get(int(task["task_number"]), {})
        task["prompt_bg"] = batch_task.get("prompt_bg") or task.get("prompt")
        task["instructions_bg"] = batch_task.get("instructions_bg") or task.get("rubric")
        task["expected_outputs"] = batch_task.get("expected_outputs") or dzi_exams.dzi_json_text_list(task.get("expected_outputs_json"))
        resource_rows = db.execute("""
            SELECT id, resource_path, original_filename, label_bg, file_size_bytes, sha256
            FROM practical_task_resources
            WHERE exam_task_id = ?
            ORDER BY id
        """, (task["task_id"],)).fetchall()
        task["resources"] = [
            practical_resource_status(dict(resource), resources)
            for resource in resource_rows
        ]
        tasks.append(task)
    return exam, tasks


def fetch_practical_submission_review(conn: sqlite3.Connection, submission_id: int) -> dict | None:
    if sqlite_table_exists(conn, "practical_submission_grades"):
        columns = sqlite_table_columns(conn, "practical_submission_grades")
        if "practical_submission_id" in columns:
            score_col = first_existing_column(columns, ("score", "manual_score"))
            max_score_col = first_existing_column(columns, ("max_score", "manual_score_max"))
            note_col = first_existing_column(columns, ("teacher_note", "teacher_note_bg", "note"))
            reviewed_at_col = first_existing_column(columns, ("reviewed_at", "created_at", "updated_at"))
            if score_col or note_col:
                selected = []
                aliases = {
                    "score": score_col,
                    "max_score": max_score_col,
                    "teacher_note": note_col,
                    "reviewed_at": reviewed_at_col,
                }
                # SAFE: selected columns and ORDER BY column come from schema columns
                # intersected with fixed candidate whitelists above.
                for alias, column in aliases.items():
                    if column:
                        selected.append(f"{column} AS {alias}")
                    else:
                        selected.append(f"NULL AS {alias}")
                order_by = first_existing_column(columns, ("id", "reviewed_at", "updated_at", "created_at"))
                order_clause = f"ORDER BY {order_by} DESC" if order_by else ""
                row = conn.execute(f"""
                    SELECT {", ".join(selected)}
                    FROM practical_submission_grades
                    WHERE practical_submission_id = ?
                    {order_clause}
                    LIMIT 1
                """, (submission_id,)).fetchone()
                if row is not None:
                    review = dict(row)
                    if any(review.get(key) is not None for key in ("score", "max_score", "teacher_note", "reviewed_at")):
                        return review

    columns = sqlite_table_columns(conn, "practical_submissions")
    score_col = first_existing_column(columns, ("manual_score", "score"))
    max_score_col = first_existing_column(columns, ("manual_score_max", "max_score"))
    note_col = first_existing_column(columns, ("teacher_note", "teacher_note_bg", "note"))
    reviewed_at_col = first_existing_column(columns, ("reviewed_at",))
    if not (score_col or note_col or reviewed_at_col):
        return None

    aliases = {
        "score": score_col,
        "max_score": max_score_col,
        "teacher_note": note_col,
        "reviewed_at": reviewed_at_col,
    }
    selected = []
    # SAFE: selected columns come from schema columns intersected with fixed
    # candidate whitelists above.
    for alias, column in aliases.items():
        if column:
            selected.append(f"{column} AS {alias}")
        else:
            selected.append(f"NULL AS {alias}")
    row = conn.execute(f"""
        SELECT {", ".join(selected)}
        FROM practical_submissions
        WHERE id = ?
    """, (submission_id,)).fetchone()
    if row is None:
        return None
    review = dict(row)
    if any(review.get(key) is not None for key in ("score", "max_score", "teacher_note", "reviewed_at")):
        return review
    return None


def fetch_practical_attempt_page(
    db: sqlite3.Connection, attempt_id: int, source_slug: str, *, batch_dir: str | Path, resources: PracticalResources
) -> tuple[dict, dict, list[dict]] | None:
    attempt_row = db.execute("""
        SELECT *
        FROM quiz_attempts
        WHERE id = ?
    """, (attempt_id,)).fetchone()
    if attempt_row is None:
        return None

    resolved = fetch_practical_tasks(db, source_slug, batch_dir=batch_dir, resources=resources)
    if resolved is None:
        return None
    exam, tasks = resolved

    for task in tasks:
        submission = db.execute("""
            SELECT id, status, submitted_at, created_at, updated_at
            FROM practical_submissions
            WHERE quiz_attempt_id = ? AND exam_task_id = ?
        """, (attempt_id, task["task_id"])).fetchone()
        task["submission"] = dict(submission) if submission else None
        task["review"] = fetch_practical_submission_review(db, int(submission["id"])) if submission else None
        if submission:
            file_rows = db.execute("""
                SELECT id, original_filename, size_bytes, mime_type, uploaded_at, is_deleted
                FROM practical_submission_files
                WHERE practical_submission_id = ?
                  AND is_deleted = 0
                ORDER BY uploaded_at, id
            """, (submission["id"],)).fetchall()
            task["uploaded_files"] = [dict(row) for row in file_rows]
        else:
            task["uploaded_files"] = []
    return dict(attempt_row), exam, tasks


def fetch_practical_upload_task(conn: sqlite3.Connection, *, attempt_id: int, source_slug: str, exam_task_id: int):
    attempt = conn.execute("""
        SELECT *
        FROM quiz_attempts
        WHERE id = ?
    """, (attempt_id,)).fetchone()
    if attempt is None:
        return None, None, None

    exam = dzi_exams.dzi_find_exam(conn, source_slug)
    if exam is None:
        return attempt, None, None

    task = conn.execute("""
        SELECT id, task_number, task_kind
        FROM exam_tasks
        WHERE id = ?
          AND exam_id = ?
          AND task_number BETWEEN 26 AND 28
          AND task_kind LIKE 'practical_%'
    """, (exam_task_id, exam["id"])).fetchone()
    if task is None:
        return attempt, exam, None
    return attempt, exam, task


def practical_uploaded_file_safe_path(stored_path: str | None, upload_root: str | Path) -> Path | None:
    if not isinstance(stored_path, str) or not stored_path.strip():
        return None
    raw_path = Path(stored_path)
    if raw_path.is_absolute() or ".." in raw_path.parts:
        return None
    try:
        relative_path = raw_path.relative_to(Path("data/uploads/practical"))
    except ValueError:
        return None
    upload_root = Path(upload_root)
    candidate = (upload_root / relative_path).resolve(strict=False)
    root = upload_root.resolve(strict=False)
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    return candidate


def fetch_practical_submission_file(db: sqlite3.Connection, file_id: int) -> dict | None:
    """The stored file with the owner of its assignment (file -> submission ->
    attempt -> assignment). ``owner_user_id`` is NULL for an unowned assignment;
    the caller authorizes on it before touching the file on disk."""
    row = db.execute("""
        SELECT
            psf.id,
            psf.practical_submission_id,
            psf.stored_path,
            psf.original_filename,
            psf.size_bytes,
            psf.mime_type,
            psf.uploaded_at,
            psf.is_deleted,
            ps.quiz_attempt_id,
            ps.exam_task_id,
            qt.student_name,
            qt.assignment_id,
            qa.owner_user_id,
            qa.lifecycle_hold,
            et.task_number,
            et.task_kind
        FROM practical_submission_files psf
        JOIN practical_submissions ps ON ps.id = psf.practical_submission_id
        JOIN quiz_attempts qt ON qt.id = ps.quiz_attempt_id
        LEFT JOIN quiz_assignments qa ON qa.id = qt.assignment_id
        JOIN exam_tasks et ON et.id = ps.exam_task_id
        WHERE psf.id = ?
    """, (file_id,)).fetchone()
    return dict(row) if row else None


def fetch_practical_submission_review_rows(
    db: sqlite3.Connection, upload_root: str | Path, *, principal: Principal
) -> list[dict]:
    """Submissions of the assignments ``principal`` manages, with their files.

    Ownership is applied in SQL through the attempt's assignment
    (``assignment_management_scope``): an admin gets every row, a teacher account
    only rows of assignments it owns, anyone else none. Files of other rows are
    never looked up on disk.
    """
    scope_condition, scope_params = assignment_management_scope(principal, "qa.owner_user_id")
    # A held assignment is not served: its submissions are left out of the review
    # list until every hold reason is resolved.
    scope_condition = f"({scope_condition}) AND qa.lifecycle_hold IS NULL"
    # SAFE: scope_condition is a fixed fragment; the owner id is a bound parameter.
    rows = db.execute(f"""
        SELECT
            ps.id AS submission_id,
            ps.status,
            ps.submitted_at,
            ps.created_at,
            ps.updated_at,
            qt.id AS attempt_id,
            qt.student_name,
            qt.assignment_id,
            qa.title_bg AS assignment_title,
            et.id AS exam_task_id,
            et.task_number,
            et.task_kind,
            e.year,
            e.session,
            e.variant,
            psf.id AS file_id,
            psf.original_filename,
            psf.size_bytes,
            psf.mime_type,
            psf.uploaded_at,
            psf.is_deleted,
            psf.stored_path
        FROM practical_submissions ps
        JOIN quiz_attempts qt ON qt.id = ps.quiz_attempt_id
        LEFT JOIN quiz_assignments qa ON qa.id = qt.assignment_id
        JOIN exam_tasks et ON et.id = ps.exam_task_id
        JOIN exams e ON e.id = et.exam_id
        LEFT JOIN practical_submission_files psf
            ON psf.practical_submission_id = ps.id
        WHERE {scope_condition}
        ORDER BY ps.updated_at DESC, ps.id DESC, psf.uploaded_at DESC, psf.id DESC
    """, scope_params).fetchall()

    submissions_by_id: dict[int, dict] = {}
    for row in rows:
        submission_id = int(row["submission_id"])
        if submission_id not in submissions_by_id:
            exam = {
                "year": row["year"],
                "session": row["session"],
                "variant": row["variant"],
            }
            submissions_by_id[submission_id] = {
                "id": submission_id,
                "status": row["status"],
                "submitted_at": row["submitted_at"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
                "attempt_id": row["attempt_id"],
                "student_name": row["student_name"],
                "assignment_id": row["assignment_id"],
                "assignment_title": row["assignment_title"],
                "exam_task_id": row["exam_task_id"],
                "task_number": row["task_number"],
                "task_kind": row["task_kind"],
                "source_slug": dzi_exams.dzi_source_slug(exam),
                "files": [],
            }
        if row["file_id"] is not None:
            safe_path = practical_uploaded_file_safe_path(row["stored_path"], upload_root)
            submissions_by_id[submission_id]["files"].append({
                "id": row["file_id"],
                "original_filename": row["original_filename"],
                "size_bytes": row["size_bytes"],
                "mime_type": row["mime_type"],
                "uploaded_at": row["uploaded_at"],
                "is_deleted": bool(row["is_deleted"]),
                "is_available": bool(safe_path and safe_path.is_file() and not row["is_deleted"]),
            })
    return list(submissions_by_id.values())


def fetch_practical_assignment_options(
    conn: sqlite3.Connection, *, scope: AssessmentScope
) -> list[dict]:
    if not scope.allows_dzi_content:
        return []
    required_tables = ("exams", "exam_tasks", "practical_task_resources")
    if not all(sqlite_table_exists(conn, table) for table in required_tables):
        return []

    rows = conn.execute("""
        SELECT
            e.*,
            COUNT(DISTINCT et.id) AS practical_task_count,
            COUNT(ptr.id) AS resource_count
        FROM exams e
        JOIN exam_tasks et
          ON et.exam_id = e.id
         AND et.task_number BETWEEN 26 AND 28
         AND et.task_kind LIKE 'practical_%'
        JOIN practical_task_resources ptr
          ON ptr.exam_task_id = et.id
        WHERE e.format_version = ? AND e.level = 'DZI'
        GROUP BY e.id
        HAVING resource_count > 0
        ORDER BY e.year DESC, e.session, e.variant
    """, (DZI_FORMAT_VERSION,)).fetchall()

    options = []
    for row in rows:
        if canonical_subject(row["subject"]) != IT:
            continue
        option = dict(row)
        option["source_slug"] = dzi_source_slug(row)
        option["title"] = dzi_source_title(row)
        options.append(option)
    return options
