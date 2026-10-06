"""Practical-task pages and files (roadmap PR E2).

* the official resource download (admin);
* the student practical page and file upload (``attempt_owner``; the upload is
  also same-origin protected);
* the teacher's submission review page and stored-file download (admin).

Every view declares its access policy (:mod:`web.access`) and, for the one
unsafe method, its request-integrity policy (:mod:`web.request_integrity`);
nothing here reads the session to decide who may enter, except the attempt
ownership check shared with the quiz pages (:mod:`web.attempt_access`).

Data, resource roots and upload-path confinement live in
:mod:`web.services.practical`; the upload limits and file-name rules stay in
``src.practical_uploads``. The module imports neither ``web.app`` nor another
controller. The ``/dzi/...`` and ``/teacher/...`` URLs are unchanged; the URL
hierarchy says nothing about which blueprint owns a view.
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import closing
from io import BytesIO
from pathlib import Path

from flask import Blueprint, abort, current_app, redirect, render_template, request, send_file, url_for
from werkzeug.exceptions import RequestEntityTooLarge

from src.practical_resource_resolution import (
    ResourceResolutionError,
    read_zip_member,
    resolve_resource_reference,
)
from src.practical_uploads import (
    PracticalUploadError,
    build_practical_upload_path,
    sanitize_original_filename,
    validate_upload_capacity,
    validate_upload_extension,
    validate_upload_size,
)
from web import db as web_db
from web.access import ADMIN, ATTEMPT_OWNER, access_policy
from web.attempt_access import require_quiz_attempt_access
from web.request_integrity import SAME_ORIGIN, integrity_policy
from web.services import practical


practical_bp = Blueprint("practical", __name__)


def _request_db():
    return web_db.connect_db(current_app.config["DB_PATH"])


def uploaded_file_size(file_storage) -> int:
    stream = file_storage.stream
    current_position = stream.tell()
    stream.seek(0, os.SEEK_END)
    size = stream.tell()
    stream.seek(current_position)
    return int(size)


def upload_error_redirect(attempt_id: int, source_slug: str, error_code: str):
    return redirect(url_for(
        "practical.quiz_practical_attempt",
        attempt_id=attempt_id,
        source_slug=source_slug,
        upload_error=error_code,
    ))


@practical_bp.route("/dzi/practical/resource/<int:resource_id>/download")
@access_policy(ADMIN)
def dzi_practical_resource_download(resource_id: int):
    with closing(_request_db()) as db:
        resource = practical.fetch_practical_resource(db, resource_id)
    if resource is None:
        current_app.logger.warning(
            "Practical resource download rejected: reason=not_found resource_id=%s",
            resource_id,
        )
        abort(404)

    resources = practical.resources_from_config(current_app.config)
    try:
        resolved = resolve_resource_reference(
            resource.get("resource_path"),
            project_root=resources.project_root,
            allowed_roots=resources.allowed_roots,
            reference_root=resources.reference_root,
        )
        download_name = practical.safe_download_name(resource.get("original_filename"), resolved.filename)
        if resolved.kind == "zip_member":
            response = send_file(
                BytesIO(read_zip_member(resolved)),
                as_attachment=True,
                download_name=download_name,
                mimetype="application/octet-stream",
                max_age=0,
            )
            response.headers["X-Content-Type-Options"] = "nosniff"
            return response
        return send_file(
            resolved.path,
            as_attachment=True,
            download_name=download_name,
            max_age=0,
        )
    except ResourceResolutionError as exc:
        current_app.logger.warning(
            "Practical resource download rejected: reason=%s resource_id=%s exam_task_id=%s task_number=%s",
            exc.reason,
            resource_id,
            resource.get("exam_task_id"),
            resource.get("task_number"),
        )
        abort(404)
    except (OSError, ValueError) as exc:
        current_app.logger.warning(
            "Practical resource download rejected: reason=send_failed resource_id=%s exam_task_id=%s task_number=%s error_type=%s",
            resource_id,
            resource.get("exam_task_id"),
            resource.get("task_number"),
            type(exc).__name__,
        )
        abort(404)


@practical_bp.route("/quiz/attempt/<int:attempt_id>/practical/<source_slug>")
@access_policy(ATTEMPT_OWNER)
def quiz_practical_attempt(attempt_id: int, source_slug: str):
    require_quiz_attempt_access(attempt_id)
    with closing(_request_db()) as db:
        resolved = practical.fetch_practical_attempt_page(
            db, attempt_id, source_slug,
            batch_dir=current_app.config["PRACTICAL_TASK_BATCH_DIR"],
            resources=practical.resources_from_config(current_app.config),
        )
    if resolved is None:
        abort(404)
    attempt, exam, tasks = resolved
    return render_template(
        "practical_tasks.html",
        exam=exam,
        tasks=tasks,
        attempt=attempt,
        upload_success=request.args.get("uploaded") == "1",
        upload_error=request.args.get("upload_error"),
    )


@practical_bp.route("/quiz/attempt/<int:attempt_id>/practical/<source_slug>/task/<int:exam_task_id>/upload", methods=["POST"])
@access_policy(ATTEMPT_OWNER)
@integrity_policy(SAME_ORIGIN)
def quiz_practical_upload(attempt_id: int, source_slug: str, exam_task_id: int):
    require_quiz_attempt_access(attempt_id)
    with closing(_request_db()) as conn:
        return _store_upload(conn, attempt_id, source_slug, exam_task_id)


def _store_upload(conn, attempt_id: int, source_slug: str, exam_task_id: int):
    attempt, exam, task = practical.fetch_practical_upload_task(
        conn,
        attempt_id=attempt_id,
        source_slug=source_slug,
        exam_task_id=exam_task_id,
    )
    if attempt is None or exam is None or task is None:
        current_app.logger.warning(
            "Practical upload rejected: reason=context_not_found attempt_id=%s source_slug=%s exam_task_id=%s",
            attempt_id,
            source_slug,
            exam_task_id,
        )
        abort(404)

    try:
        submitted_files = request.files.getlist("files")
    except RequestEntityTooLarge:
        current_app.logger.warning(
            "Practical upload rejected: reason=request_too_large attempt_id=%s source_slug=%s exam_task_id=%s",
            attempt_id,
            source_slug,
            exam_task_id,
        )
        return upload_error_redirect(attempt_id, source_slug, "too_large")
    uploaded_files = [
        file_storage
        for file_storage in submitted_files
        if file_storage and (file_storage.filename or "").strip()
    ]
    if not uploaded_files:
        current_app.logger.info(
            "Practical upload rejected: reason=empty attempt_id=%s source_slug=%s exam_task_id=%s",
            attempt_id,
            source_slug,
            exam_task_id,
        )
        return upload_error_redirect(attempt_id, source_slug, "empty")

    if len(uploaded_files) > int(current_app.config["PRACTICAL_MAX_FILES_PER_REQUEST"]):
        current_app.logger.warning(
            "Practical upload rejected: reason=too_many_files attempt_id=%s source_slug=%s exam_task_id=%s file_count=%s",
            attempt_id,
            source_slug,
            exam_task_id,
            len(uploaded_files),
        )
        return upload_error_redirect(attempt_id, source_slug, "invalid")

    prepared_files = []
    try:
        for file_storage in uploaded_files:
            original_filename = sanitize_original_filename(file_storage.filename or "")
            validate_upload_extension(original_filename)
            size_bytes = validate_upload_size(
                uploaded_file_size(file_storage),
                max_bytes=int(current_app.config["PRACTICAL_MAX_UPLOAD_BYTES"]),
            )
            file_storage.stream.seek(0)
            prepared_files.append((file_storage, original_filename, size_bytes))
    except PracticalUploadError as exc:
        current_app.logger.warning(
            "Practical upload rejected: reason=validation attempt_id=%s source_slug=%s exam_task_id=%s file_count=%s error_type=%s",
            attempt_id,
            source_slug,
            exam_task_id,
            len(uploaded_files),
            type(exc).__name__,
        )
        return upload_error_redirect(attempt_id, source_slug, "invalid")

    saved_paths = []
    try:
        # Serialize quota checks and inserts across workers. The existing route
        # already held a write transaction while saving files.
        conn.execute("BEGIN IMMEDIATE")
        current_attempt = conn.execute(
            "SELECT submitted_at FROM quiz_attempts WHERE id = ?", (attempt_id,)
        ).fetchone()
        if current_attempt is None or current_attempt["submitted_at"] is not None:
            raise PracticalUploadError("quiz attempt is closed")
        submission = conn.execute("""
            SELECT id, status
            FROM practical_submissions
            WHERE quiz_attempt_id = ? AND exam_task_id = ?
        """, (attempt_id, exam_task_id)).fetchone()
        if submission is not None and submission["status"] != "draft":
            raise PracticalUploadError("practical submission is closed")
        totals = conn.execute("""
            SELECT COUNT(*), COALESCE(SUM(psf.size_bytes), 0)
            FROM practical_submission_files psf
            JOIN practical_submissions ps ON ps.id = psf.practical_submission_id
            WHERE ps.quiz_attempt_id = ? AND ps.exam_task_id = ?
        """, (attempt_id, exam_task_id)).fetchone()
        validate_upload_capacity(
            Path(current_app.config["PRACTICAL_UPLOAD_ROOT"]),
            sum(size for _, _, size in prepared_files),
            existing_bytes=int(totals[1]), existing_files=int(totals[0]),
            incoming_files=len(prepared_files),
        )
        if submission is None:
            cur = conn.execute("""
                INSERT INTO practical_submissions (
                    quiz_attempt_id, exam_task_id, status, updated_at
                )
                VALUES (?, ?, 'draft', CURRENT_TIMESTAMP)
            """, (attempt_id, exam_task_id))
            submission_id = int(cur.lastrowid)
        else:
            submission_id = int(submission["id"])
            conn.execute("""
                UPDATE practical_submissions
                SET updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
            """, (submission_id,))

        for file_storage, original_filename, size_bytes in prepared_files:
            stored = build_practical_upload_path(
                upload_root=Path(current_app.config["PRACTICAL_UPLOAD_ROOT"]),
                attempt_id=attempt_id,
                exam_task_id=exam_task_id,
                submission_id=submission_id,
                original_filename=original_filename,
                size_bytes=size_bytes,
            )
            stored.absolute_path.parent.mkdir(parents=True, exist_ok=True)
            saved_paths.append(stored.absolute_path)
            file_storage.save(stored.absolute_path)
            if stored.absolute_path.stat().st_size != size_bytes:
                raise PracticalUploadError("incomplete practical upload")
            conn.execute("""
                INSERT INTO practical_submission_files (
                    practical_submission_id, stored_path, original_filename,
                    size_bytes, mime_type
                )
                VALUES (?, ?, ?, ?, ?)
            """, (
                submission_id,
                stored.stored_path,
                stored.original_filename,
                size_bytes,
                file_storage.mimetype,
            ))

        conn.commit()
    except (OSError, sqlite3.Error, PracticalUploadError) as exc:
        conn.rollback()
        for path in saved_paths:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        current_app.logger.warning(
            "Practical upload rejected: reason=save_failed attempt_id=%s source_slug=%s exam_task_id=%s file_count=%s error_type=%s",
            attempt_id,
            source_slug,
            exam_task_id,
            len(prepared_files),
            type(exc).__name__,
        )
        return upload_error_redirect(attempt_id, source_slug, "invalid")

    return redirect(url_for(
        "practical.quiz_practical_attempt",
        attempt_id=attempt_id,
        source_slug=source_slug,
        uploaded="1",
    ))


@practical_bp.route("/teacher/practical-submissions")
@access_policy(ADMIN)
def teacher_practical_submissions():
    with closing(_request_db()) as db:
        submissions = practical.fetch_practical_submission_review_rows(
            db, current_app.config["PRACTICAL_UPLOAD_ROOT"]
        )
    return render_template(
        "teacher_practical_submissions.html",
        submissions=submissions,
    )


@practical_bp.route("/teacher/practical/submission-file/<int:file_id>/download")
@access_policy(ADMIN)
def teacher_practical_submission_file_download(file_id: int):
    with closing(_request_db()) as db:
        file_row = practical.fetch_practical_submission_file(db, file_id)
    if file_row is None:
        current_app.logger.warning(
            "Practical submission download rejected: reason=not_found file_id=%s",
            file_id,
        )
        abort(404)
    if file_row["is_deleted"]:
        current_app.logger.warning(
            "Practical submission download rejected: reason=deleted file_id=%s attempt_id=%s exam_task_id=%s",
            file_id,
            file_row.get("quiz_attempt_id"),
            file_row.get("exam_task_id"),
        )
        abort(404)

    safe_path = practical.practical_uploaded_file_safe_path(
        file_row.get("stored_path"), current_app.config["PRACTICAL_UPLOAD_ROOT"]
    )
    if safe_path is None:
        current_app.logger.warning(
            "Practical submission download rejected: reason=unsafe_path file_id=%s attempt_id=%s exam_task_id=%s",
            file_id,
            file_row.get("quiz_attempt_id"),
            file_row.get("exam_task_id"),
        )
        abort(404)
    if not safe_path.is_file():
        current_app.logger.warning(
            "Practical submission download rejected: reason=missing_file file_id=%s attempt_id=%s exam_task_id=%s",
            file_id,
            file_row.get("quiz_attempt_id"),
            file_row.get("exam_task_id"),
        )
        abort(404)

    return send_file(
        safe_path,
        as_attachment=True,
        download_name=file_row["original_filename"] or safe_path.name,
        max_age=0,
    )
