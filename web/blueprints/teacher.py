"""Teacher pages (roadmap PR E3): the dashboard, the assignment list and
detail, quiz creation, assignment edit / duplicate, results and their CSV
export, and the DZI training-set page.

Every view declares its access policy (:mod:`web.access`) and, for the unsafe
methods, its request-integrity policy (:mod:`web.request_integrity`); nothing
here reads the session to decide who may enter. Queries and quiz rules live in
:mod:`web.services.quiz`; the practical-source options come from
:mod:`web.services.practical`. The module imports neither ``web.app`` nor
another controller. The ``/teacher/...`` URLs are unchanged; the practical
submission review pages stay on the practical blueprint.

Assignment management (list, edit, duplicate, results, CSV and the review of an
assignment's open answers) uses the ``assignment_owner`` policy: an admin
manages every assignment, a teacher account only its own, and anything else is
a 404 decided by ``web.access.can_manage_assignment`` on the loaded row. The
list applies the same rule in SQL. Student/public quiz loading is not scoped.

Lifecycle (docs/ASSIGNMENT_LIFECYCLE_DESIGN.md): create, edit, duplicate and
manual close are lifecycle writes. Each validates its deadline against a ``now``
sampled under the writer lock and restates its precondition in SQL
(:mod:`web.services.lifecycle`). Ownership is decided first, then the hold, then
the state, so a lifecycle response never reveals a foreign assignment. A held
assignment and an unusable clock raise ``lifecycle.LifecycleUnavailable``, which
``web.app`` answers with 503.
"""

from __future__ import annotations

import copy
import csv
import io
import json
import math
import subprocess
import sys
import unicodedata
from pathlib import Path

from flask import Blueprint, Response, abort, current_app, redirect, render_template, request, url_for

from src.subject_assessment import AssessmentScope
from src.subject_content import IT, canonical_subject
from src.subject_registry import get_subject
from web.access import (
    ADMIN,
    ASSIGNMENT_OWNER,
    TEACHER,
    access_policy,
    assignment_management_scope,
    can_list_assignments,
    can_manage_assignment,
    current_principal,
)
from web.public_url import trusted_public_base_url
from web.request_integrity import SAME_ORIGIN, integrity_policy
from web.services import assignment_quota, lifecycle, practical
from web.services.qr import qr_svg
from web.services.quiz import (
    QUIZ_DURATION_FALLBACK_BG,
    QUIZ_SUCCESS_RATE_FALLBACK_BG,
    TEACHER_OPEN_ANSWER_STATUSES,
    attach_attempt_question_image_counts,
    attach_question_image_indicators,
    build_mixed_quiz_plan,
    fetch_dzi_pool_health,
    fetch_quiz_text_answers_for_attempt,
    open_question_scope_summary,
    quiz_assignment_mixed_status,
    quiz_can_access_assignment,
    quiz_attempt_duration_display,
    quiz_attempt_success_rate_display,
    quiz_combined_score_summary,
    quiz_connection,
    quiz_create_assignment,
    quiz_db,
    quiz_fetch_assignment,
    quiz_student_entry_path,
    quiz_parse_assignment_question_plan,
    quiz_parse_attempt_question_plan,
    quiz_section_question_ids_by_section,
    quiz_teacher_difficulty_breakdown,
    quiz_text_answer_informational_subtotal,
    quiz_text_auto_points,
    quiz_vault_path,
    quiz_write_assignment_note,
)
from web.services.subject_catalog import subject_catalog


teacher_bp = Blueprint("teacher", __name__)


@teacher_bp.route("/teacher/dzi-training", methods=["GET", "POST"])
@access_policy(ADMIN)
@integrity_policy(SAME_ORIGIN)
def teacher_dzi_training():
    project_root = Path(__file__).resolve().parents[2]
    config_path = project_root / "deploy" / "dzi-training-class-mix.json"
    generator_path = project_root / "src" / "generate_dzi_training_set.py"
    sets_dir = quiz_vault_path() / "Generated" / "DZI-Training" / "sets"

    generated_output = None
    generated_ok = None
    dzi_generation_error = None
    with quiz_connection() as conn:
        dzi_pool_health = fetch_dzi_pool_health(conn, "may_2025_v2")

    def short_generator_output(*parts) -> str:
        text = "\n".join(str(part).strip() for part in parts if part).strip()
        if len(text) > 1200:
            return text[:1200].rstrip() + "\n..."
        return text

    if config_path.exists():
        try:
            config = json.loads(config_path.read_text(encoding="utf-8"))
        except Exception as exc:
            config = {
                "status": "error",
                "classes": {},
                "error": str(exc),
            }
    else:
        config = {
            "status": "missing",
            "classes": {},
            "error": f"Missing config: {config_path}",
        }

    if request.method == "POST":
        raw_count = (request.form.get("count") or "28").strip()
        raw_seed = (request.form.get("seed") or "").strip()
        raw_name = (request.form.get("name") or "").strip()

        try:
            count = int(raw_count)
        except ValueError:
            count = 28

        count = max(1, min(count, 100))
        name = raw_name or f"ДЗИ тренировъчен комплект — {count} въпроса"

        if not generator_path.exists():
            generated_ok = False
            dzi_generation_error = (
                "Не успях да генерирам тренировъчен ДЗИ комплект. "
                "Опитай пак след малко или провери входните данни."
            )
            generated_output = "Скриптът за генериране не е намерен."
        else:
            cmd = [
                sys.executable,
                str(generator_path),
                "--db", str(Path(current_app.config["DB_PATH"]).resolve()),
                "--config", str(config_path.resolve()),
                "--data-output-dir", str(sets_dir.resolve()),
                "--vault-output-dir", str(sets_dir.resolve()),
                "--count",
                str(count),
                "--name",
                name,
            ]

            if raw_seed:
                cmd.extend(["--seed", raw_seed])

            try:
                proc = subprocess.run(
                    cmd,
                    cwd=str(project_root),
                    text=True,
                    capture_output=True,
                    timeout=60,
                )
            except subprocess.TimeoutExpired as exc:
                generated_ok = False
                dzi_generation_error = (
                    "Не успях да генерирам тренировъчен ДЗИ комплект. "
                    "Опитай пак след малко или провери входните данни."
                )
                generated_output = short_generator_output(exc.stdout, exc.stderr)
            else:
                generated_ok = proc.returncode == 0
                generated_output = (proc.stdout or "") + (proc.stderr or "")
                if proc.returncode != 0:
                    dzi_generation_error = (
                        "Не успях да генерирам тренировъчен ДЗИ комплект. "
                        "Опитай пак след малко или провери входните данни."
                    )
                    generated_output = short_generator_output(proc.stdout, proc.stderr)

    recent_sets = []
    if sets_dir.exists():
        for path in sorted(sets_dir.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)[:20]:
            recent_sets.append({
                "name": path.name,
                "relative_path": str(path.relative_to(quiz_vault_path())),
                "size": path.stat().st_size,
                "modified": path.stat().st_mtime,
            })

    return render_template(
        "teacher_dzi_training.html",
        config=config,
        recent_sets=recent_sets,
        generated_output=generated_output,
        generated_ok=generated_ok,
        dzi_generation_error=dzi_generation_error,
        dzi_pool_health=dzi_pool_health,
    )


@teacher_bp.route("/teacher")
@access_policy(ADMIN)
def teacher_dashboard():
    conn = quiz_db()

    stats = conn.execute("""
        SELECT
            (SELECT COUNT(*) FROM quiz_assignments) AS assignments_count,
            (SELECT COUNT(*) FROM quiz_attempts) AS attempts_count,
            (SELECT COUNT(*) FROM quiz_attempts WHERE submitted_at IS NOT NULL) AS submitted_count,
            (SELECT COUNT(*) FROM quiz_attempts WHERE submitted_at IS NULL) AS unfinished_count,
            (
              SELECT AVG(100.0 * score_correct / score_total)
              FROM quiz_attempts
              WHERE submitted_at IS NOT NULL
                AND score_total IS NOT NULL
                AND score_total > 0
            ) AS avg_percent
    """).fetchone()

    recent_assignment_rows = conn.execute("""
        SELECT
            qa.id,
            qa.title_bg,
            qa.question_count,
            qa.time_limit_minutes,
            qa.question_plan_json,
            qa.created_at,
            cs.class,
            cs.section_slug,
            cs.title_bg AS section_title,
            COUNT(DISTINCT qt.id) AS attempts_count,
            SUM(CASE WHEN qt.submitted_at IS NOT NULL THEN 1 ELSE 0 END) AS submitted_count
        FROM quiz_assignments qa
        JOIN curriculum_sections cs ON cs.id = qa.section_id
        LEFT JOIN quiz_attempts qt ON qt.assignment_id = qa.id
        GROUP BY qa.id
        ORDER BY qa.created_at DESC, qa.id DESC
        LIMIT 5
    """).fetchall()

    recent_assignments = []
    for row in recent_assignment_rows:
        row_dict = dict(row)
        row_dict["mixed_status"] = quiz_assignment_mixed_status(row_dict.pop("question_plan_json", None))
        row_dict["teacher_label"] = "не е посочен"
        recent_assignments.append(row_dict)

    recent_assignments_by_class = []
    for class_value in sorted({int(row["class"]) for row in recent_assignments}):
        recent_assignments_by_class.append({
            "class": class_value,
            "assignments": [row for row in recent_assignments if int(row["class"]) == class_value],
        })

    recent_attempts = conn.execute("""
        SELECT
            qt.id,
            qt.assignment_id,
            qt.student_name,
            qt.started_at,
            qt.submitted_at,
            qt.score_correct,
            qt.score_total,
            qa.title_bg AS assignment_title,
            CASE
              WHEN qt.score_total IS NOT NULL AND qt.score_total > 0
              THEN ROUND(100.0 * qt.score_correct / qt.score_total, 1)
              ELSE NULL
            END AS percent
        FROM quiz_attempts qt
        JOIN quiz_assignments qa ON qa.id = qt.assignment_id
        ORDER BY qt.started_at DESC, qt.id DESC
        LIMIT 8
    """).fetchall()

    conn.close()

    return render_template(
        "teacher_dashboard.html",
        stats=stats,
        recent_assignments=recent_assignments,
        recent_assignments_by_class=recent_assignments_by_class,
        recent_attempts=recent_attempts,
    )


@teacher_bp.app_context_processor
def inject_assignment_management():
    """``can_list_assignments`` for navigation: admin or teacher account only."""
    return {"can_list_assignments": can_list_assignments(current_principal())}


def _fetch_managed_assignment(conn, assignment_id: int):
    """The assignment if the current principal may manage it; otherwise close
    ``conn`` and answer 404 (missing, foreign and, for a teacher, unowned alike)."""
    assignment = quiz_fetch_assignment(conn, assignment_id)
    if assignment is None or not can_manage_assignment(
        current_principal(), assignment["owner_user_id"]
    ):
        conn.close()
        abort(404)
    return assignment


def _list_lifecycle(row, now) -> dict:
    """Lifecycle summary of one listed assignment. A held one is listed, not served."""
    summary = lifecycle.describe(lifecycle.evaluate(row, now))
    summary["is_held"] = lifecycle.is_held(row)
    return summary


def _submitted_deadline_form(form) -> dict:
    """The deadline fields exactly as submitted, to show a refused form again."""
    return {
        "choice": form.get("deadline_choice") or "",
        "form_date": form.get("deadline_date") or "",
        "form_time": form.get("deadline_time") or "",
        "zone": form.get("display_timezone") or "",
        "max_days": lifecycle.MAX_DEADLINE_DAYS,
    }


def _creation_error_message(code: str | None) -> str | None:
    """The Bulgarian message of a refused creation: a deadline error or a creation limit."""
    return lifecycle.DEADLINE_ERROR_MESSAGES_BG.get(code) or assignment_quota.MESSAGES_BG.get(code)


def _creation_error_status(code: str | None) -> int:
    """400 for a refused input, 429 or 409 for a creation limit (never a change to the input)."""
    return assignment_quota.STATUS_CODES.get(code, 400)


def _log_creation_refused(principal, exc: assignment_quota.QuotaExceeded) -> None:
    """Diagnosis only, like the lifecycle lines: the account id and the reason, nothing else."""
    lifecycle.logger.info("lifecycle event=create_refused actor_user_id=%s reason=%s", principal.user_id, exc.reason)


def _create_assignment_with_deadline(conn, deadline_request, **fields) -> tuple[int | None, str | None]:
    """Create an assignment in one lifecycle writer transaction.

    ``(assignment_id, None)``, or ``(None, error_code)`` when the deadline or a
    creation limit refuses it; nothing is stored then. The deadline is validated
    against the ``now`` sampled after the writer lock was taken: later than ``now``
    and at most 180 local calendar days ahead. The server never invents a deadline:
    the request states one, or states "no deadline". The creation limits are decided
    by ``quiz_create_assignment`` in this same transaction (``assignment_quota``).
    """
    principal = current_principal()
    try:
        with lifecycle.write_transaction(conn) as transaction:
            transaction.admit()
            try:
                deadline_at = lifecycle.resolve_deadline(deadline_request, transaction.now)
            except lifecycle.DeadlineError as exc:
                transaction.discard()
                return None, exc.code
            assignment_id = quiz_create_assignment(
                conn, principal=principal, deadline_at=deadline_at,
                display_timezone=deadline_request.timezone, transaction=transaction, **fields)
            created_at = transaction.now_text
    except assignment_quota.QuotaExceeded as exc:
        _log_creation_refused(principal, exc)
        return None, exc.reason  # rolled back by the transaction: nothing of this creation exists
    # After the commit, for diagnosis only: no tool reads an assignment's state from the log.
    lifecycle.logger.info(
        "lifecycle event=create assignment_id=%s at=%r deadline_at=%r display_timezone=%r actor_user_id=%s",
        assignment_id, created_at, deadline_at, deadline_request.timezone, principal.user_id)
    return assignment_id, None


def _duplicate_assignment_with_deadline(conn, assignment_id: int, deadline_request) -> tuple[int | None, str | None]:
    """Create a copy of an assignment in one lifecycle writer transaction.

    ``(new_assignment_id, None)``, or ``(None, error_code)`` when the deadline or a
    creation limit refuses it. Everything the copy is made from is decided under the writer lock,
    on the source row re-read inside the transaction, in the fixed order:
    authorization (404), the source's hold (503), its factual lifecycle
    timestamps against the clock, its plan, and only then the fields that are
    copied. A hold that another connection committed after the request first
    looked at the source is therefore seen, and no copy is made of a held source.

    Nothing of the source's lifecycle is copied: the copy gets the deadline and
    zone this request states, and a new owner and public token.
    """
    principal = current_principal()
    try:
        with lifecycle.write_transaction(conn) as transaction:
            source = quiz_fetch_assignment(conn, assignment_id)
            if source is None or not can_manage_assignment(principal, source["owner_user_id"]):
                abort(404)
            transaction.examine(source)  # hold, factual timestamps, admission

            if source["question_plan_json"]:
                plan = quiz_parse_assignment_question_plan(source["question_plan_json"])
                try:
                    if plan is None:
                        raise ValueError("Invalid mixed plan")
                    AssessmentScope(conn, source["section_id"]).validate_plan(
                        plan["question_ids"], plan["open_question_ids"]
                    )
                except ValueError:
                    abort(400)

            try:
                deadline_at = lifecycle.resolve_deadline(deadline_request, transaction.now)
            except lifecycle.DeadlineError as exc:
                return None, exc.code
            # The copy belongs to the acting account, never to the source's owner. It is a
            # creation like any other and counts against the acting account's limits.
            new_assignment_id = quiz_create_assignment(
                conn, principal=principal, section_id=source["section_id"],
                title_bg=quiz_duplicate_title(source["title_bg"]),
                question_count=source["question_count"],
                time_limit_minutes=source["time_limit_minutes"],
                question_plan_json=source["question_plan_json"],
                deadline_at=deadline_at, display_timezone=deadline_request.timezone,
                transaction=transaction)
            created_at = transaction.now_text
    except assignment_quota.QuotaExceeded as exc:
        _log_creation_refused(principal, exc)
        return None, exc.reason  # rolled back by the transaction: no copy, no ledger row
    # After the commit, for diagnosis only: no tool reads an assignment's state from the log.
    lifecycle.logger.info(
        "lifecycle event=create assignment_id=%s at=%r deadline_at=%r display_timezone=%r actor_user_id=%s",
        new_assignment_id, created_at, deadline_at, deadline_request.timezone, principal.user_id)
    return new_assignment_id, None


def _student_quiz_url(assignment) -> str | None:
    """The absolute student link on the trusted public base URL
    (:mod:`web.public_url`), never on this request's ``Host``. ``None`` without a
    valid public token or without a trusted base; the page then says so instead of
    a link. The link shown for sharing and the one a QR code encodes are both this."""
    entry_path = quiz_student_entry_path(assignment)
    base_url = trusted_public_base_url()
    return base_url + entry_path if entry_path and base_url else None


@teacher_bp.route("/teacher/assignments")
@access_policy(ASSIGNMENT_OWNER)
def teacher_assignments():
    conn = quiz_db()

    type_filter = (request.args.get("type") or "all").strip().lower()
    if type_filter not in {"all", "mc", "mixed"}:
        type_filter = "all"

    # Ownership is applied in SQL (idx_quiz_assignments_owner); an admin sees all.
    scope_condition, scope_params = assignment_management_scope(
        current_principal(), "qa.owner_user_id"
    )
    conditions = [scope_condition]
    if type_filter == "mc":
        conditions.append("(qa.question_plan_json IS NULL OR TRIM(qa.question_plan_json) = '')")
    elif type_filter == "mixed":
        conditions.append("qa.question_plan_json IS NOT NULL AND TRIM(qa.question_plan_json) <> ''")
    where_clause = "WHERE " + " AND ".join(conditions)

    # SAFE: where_clause is built from fixed fragments; the owner id is a bound parameter.
    assignment_rows = conn.execute(f"""
        SELECT
            qa.id,
            qa.section_id,
            qa.title_bg,
            qa.question_count,
            qa.time_limit_minutes,
            qa.question_plan_json,
            qa.public_token,
            qa.created_at,
            qa.deadline_at, qa.display_timezone, qa.closed_at, qa.expiry_observed_at,
            qa.submissions_closed_at, qa.retention_base_at, qa.lifecycle_hold,
            cs.class,
            cs.title_bg AS section_title,
            cs.section_slug,
            COUNT(DISTINCT qt.id) AS attempts_count,
            SUM(CASE WHEN qt.submitted_at IS NOT NULL THEN 1 ELSE 0 END) AS submitted_count
        FROM quiz_assignments qa
        JOIN curriculum_sections cs ON cs.id = qa.section_id
        LEFT JOIN quiz_attempts qt ON qt.assignment_id = qa.id
        {where_clause}
        GROUP BY qa.id
        ORDER BY qa.created_at DESC, qa.id DESC
    """, scope_params).fetchall()

    lifecycle_now = lifecycle.read_clock(conn).now
    assignments = []
    for row in assignment_rows:
        row_dict = dict(row)
        row_dict["mixed_status"] = quiz_assignment_mixed_status(row_dict.pop("question_plan_json", None))
        row_dict["teacher_label"] = "не е посочен"
        row_dict["lifecycle"] = _list_lifecycle(row, lifecycle_now)
        assignments.append(row_dict)

    conn.close()

    return render_template(
        "teacher_assignments.html",
        assignments=assignments,
        type_filter=type_filter,
    )


@teacher_bp.route("/teacher/new", methods=["GET", "POST"])
@access_policy(TEACHER)
@integrity_policy(SAME_ORIGIN)
def teacher_new():
    conn = quiz_db()
    raw_subject = (
        request.form.get("subject") if request.method == "POST"
        else request.args.get("subject")
    )
    selected_subject = canonical_subject(raw_subject) if raw_subject else IT
    if selected_subject is None:
        conn.close()
        abort(400 if request.method == "POST" else 404)
    selected_definition = get_subject(selected_subject)
    preselected_section_id = None
    mixed_planning_result = None
    teacher_new_error = None
    deadline_request = None
    form_values = {
        "question_count": 10,
        "time_limit_minutes": "",
        "include_open_questions": False,
        "open_count": 0,
        "source_slug": "",
    }

    if request.method == "POST":
        if any(key.startswith(("question_ids", "open_question_ids", "question_plan"))
               for key in request.form):
            teacher_new_error = "invalid_question_plan"
        raw_section_id = (request.form.get("section_id") or "").strip()
        raw_question_count = (request.form.get("question_count") or "").strip()
        raw_limit = (request.form.get("time_limit_minutes") or "").strip()
        include_open_questions = bool(request.form.get("include_open_questions"))
        include_open_answers_in_final_score = (
            bool(request.form.get("include_open_answers_in_final_score"))
            and current_principal().is_admin
        )
        raw_open_count = (request.form.get("open_count") or "").strip()
        source_slug = (request.form.get("source_slug") or "").strip()
        section_id = None
        requested_count = None
        time_limit = None
        open_count = None
        form_values = {
            "question_count": raw_question_count or 10,
            "time_limit_minutes": raw_limit,
            "include_open_questions": include_open_questions,
            "open_count": raw_open_count or 0,
            "source_slug": source_slug,
        }

        try:
            section_id = int(raw_section_id)
            requested_count = int(raw_question_count or 10)
            open_count = int(raw_open_count or 0)
            if raw_limit:
                time_limit = int(raw_limit)
        except ValueError:
            teacher_new_error = "invalid_number"
        else:
            if section_id <= 0 or requested_count < 1 or open_count < 0:
                teacher_new_error = "invalid_number"
            elif time_limit is not None and (
                time_limit < 1 or time_limit > QUIZ_TIME_LIMIT_MAX_MINUTES
            ):
                teacher_new_error = "time_out_of_range"
            elif open_count > requested_count:
                teacher_new_error = "open_count_exceeds_total"

        if teacher_new_error is None:
            # The deadline choice is explicit: a deadline, or "no deadline", and
            # always a zone. A request without a valid choice creates nothing.
            try:
                deadline_request = lifecycle.parse_deadline_request(request.form)
            except lifecycle.DeadlineError as exc:
                teacher_new_error = exc.code

        if teacher_new_error is None:
            section = conn.execute("""
                SELECT id, title_bg
                FROM curriculum_sections
                WHERE id = ?
            """, (section_id,)).fetchone()
            if not section:
                conn.close()
                abort(404)
            try:
                scope = AssessmentScope(conn, section_id)
            except ValueError:
                teacher_new_error = "invalid_subject"
            else:
                if scope.subject != selected_subject:
                    teacher_new_error = "subject_mismatch"
                elif open_count > 0 and not scope.allows_dzi_open_questions:
                    teacher_new_error = "insufficient_open_questions"
                    preselected_section_id = section_id

        if teacher_new_error is None:
            form_values = {
                "question_count": requested_count,
                "time_limit_minutes": raw_limit,
                "include_open_questions": include_open_questions,
                "open_count": open_count,
                "source_slug": source_slug,
            }

            available = len(quiz_section_question_ids_by_section(conn, {section_id: scope})[section_id])
            if available <= 0:
                conn.close()
                return redirect(url_for("teacher.teacher_new", subject=selected_subject))

            question_count = max(1, min(requested_count, available))

            if open_count > 0:
                closed_count = requested_count - open_count
                plan = build_mixed_quiz_plan(
                    conn,
                    closed_count=closed_count,
                    open_count=open_count,
                    open_source_slug=source_slug or None,
                    section_id=section_id,
                    scope=scope,
                )
                closed_question_ids = [int(question["question_id"]) for question in plan["closed_questions"]]
                open_question_ids = [int(question["question_id"]) for question in plan["open_questions"]]
                mixed_planning_result = {
                    "planning_only": True,
                    "source_slug": source_slug or "всички",
                    "closed_count": len(plan["closed_questions"]),
                    "open_count": len(plan["open_questions"]),
                    "requested_closed_count": plan["requested_closed_count"],
                    "requested_open_count": plan["requested_open_count"],
                    "available_closed_count": plan["available_closed_count"],
                    "available_open_count": plan["available_open_count"],
                    "closed_shortfall": max(0, plan["requested_closed_count"] - plan["available_closed_count"]),
                    "open_shortfall": max(0, plan["requested_open_count"] - plan["available_open_count"]),
                }
                if mixed_planning_result["open_shortfall"]:
                    teacher_new_error = "insufficient_open_questions"
                    preselected_section_id = section_id
                elif mixed_planning_result["closed_shortfall"]:
                    teacher_new_error = "insufficient_closed_questions"
                    preselected_section_id = section_id
                else:
                    question_plan = {
                        "mixed_open_enabled": True,
                        "question_ids": closed_question_ids + open_question_ids,
                        "open_question_ids": open_question_ids,
                        "include_open_answers_in_final_score": include_open_answers_in_final_score,
                    }
                    try:
                        scope.validate_plan(
                            question_plan["question_ids"], question_plan["open_question_ids"],
                            open_source_slug=source_slug or None,
                        )
                    except ValueError:
                        teacher_new_error = "invalid_question_plan"
                        preselected_section_id = section_id
                    else:
                        try:
                            assignment_id, teacher_new_error = _create_assignment_with_deadline(
                                conn, deadline_request, section_id=section_id,
                                title_bg=section["title_bg"],
                                question_count=len(question_plan["question_ids"]),
                                time_limit_minutes=time_limit,
                                question_plan_json=json.dumps(question_plan, ensure_ascii=False),
                            )
                        except lifecycle.LifecycleUnavailable:
                            conn.close()
                            raise
                        if assignment_id is not None:
                            conn.close()

                            quiz_write_assignment_note(assignment_id, request.host_url)
                            return redirect(url_for("teacher.teacher_assignment", assignment_id=assignment_id))
                        preselected_section_id = section_id
            else:
                try:
                    assignment_id, teacher_new_error = _create_assignment_with_deadline(
                        conn, deadline_request, section_id=section_id,
                        title_bg=section["title_bg"], question_count=question_count,
                        time_limit_minutes=time_limit,
                    )
                except lifecycle.LifecycleUnavailable:
                    conn.close()
                    raise
                if assignment_id is not None:
                    conn.close()

                    quiz_write_assignment_note(assignment_id, request.host_url)
                    return redirect(url_for("teacher.teacher_assignment", assignment_id=assignment_id))
                preselected_section_id = section_id

    section_rows = conn.execute("""
        SELECT id, class, title_bg
        FROM curriculum_sections
        ORDER BY class, display_order, title_bg, id
    """).fetchall()

    # Set-based loading: scopes, eligible ids and the DZI open pool are fetched
    # once for the whole page instead of per section and per question.
    selected_scopes = {
        section_id: scope
        for section_id, scope in AssessmentScope.for_all_sections(conn).items()
        if scope.subject == selected_subject
    }
    eligible_ids = quiz_section_question_ids_by_section(conn, selected_scopes)

    sections = []
    practical_source_options = None
    dzi_open_summary = None
    for row in section_rows:
        section = dict(row)
        section_scope = selected_scopes.get(int(section["id"]))
        if section_scope is None:
            continue
        eligible_count = len(eligible_ids[int(section["id"])])
        if eligible_count <= 0:
            continue
        section["question_count"] = eligible_count
        if section_scope.allows_dzi_open_questions:
            # The DZI open pool does not depend on which DZI section asks.
            if dzi_open_summary is None:
                dzi_open_summary = open_question_scope_summary(
                    conn, int(section["id"]), section_scope
                )
            section["open"] = copy.deepcopy(dzi_open_summary)
        else:
            section["open"] = open_question_scope_summary(conn, int(section["id"]), section_scope)
        section["allows_dzi_open_questions"] = section_scope.allows_dzi_open_questions
        section["allows_dzi_content"] = section_scope.allows_dzi_content
        if section_scope.allows_dzi_content and practical_source_options is None:
            practical_source_options = practical.fetch_practical_assignment_options(conn, scope=section_scope)
        sections.append(section)

    section_slug = (request.args.get("section") or "").strip()
    if section_slug:
        preselected = conn.execute("""
            SELECT id
            FROM curriculum_sections
            WHERE section_slug = ?
        """, (section_slug,)).fetchone()
        if preselected and any(int(row["id"]) == int(preselected["id"]) for row in sections):
            preselected_section_id = int(preselected["id"])

    # Only a server-loaded DZI-training section authorizes the DZI open pool.
    selected_section = next(
        (row for row in sections if int(row["id"]) == preselected_section_id), None
    )
    practical_source_options = practical_source_options or []

    if request.method == "POST":
        deadline_form = _submitted_deadline_form(request.form)
    else:
        # The offer: 23:59 local time, 14 calendar days ahead, in the preselected zone.
        deadline_form = lifecycle.default_deadline_form(
            lifecycle.read_clock(conn).now,
            lifecycle.preselected_timezone(conn, current_principal().user_id))

    conn.close()

    teacher_subject_options = [
        subject for subject in subject_catalog()
        if subject["has_real_content"] or subject["id"] == selected_subject
    ]

    return render_template(
        "teacher_new.html",
        sections=sections,
        selected_subject=selected_subject,
        selected_subject_title=selected_definition.title,
        selected_subject_dzi=selected_definition.dzi,
        teacher_subject_options=teacher_subject_options,
        preselected_section_id=preselected_section_id,
        form_values=form_values,
        teacher_new_error=teacher_new_error,
        quiz_time_limit_max_minutes=QUIZ_TIME_LIMIT_MAX_MINUTES,
        mixed_planning_result=mixed_planning_result,
        selected_section_open=selected_section["open"] if selected_section else None,
        any_dzi_sections=any(row["allows_dzi_open_questions"] for row in sections),
        selected_open_applicable=bool(selected_section and selected_section["allows_dzi_open_questions"]),
        selected_dzi_context=bool(selected_section and selected_section["allows_dzi_content"]),
        selected_open_available=next((source["count"] for source in selected_section["open"]["sources"]
                                     if source["source_slug"] == form_values["source_slug"]), 0)
            if selected_section and form_values["source_slug"]
            else selected_section["open"]["count"] if selected_section else 0,
        practical_source_options=practical_source_options,
        deadline_form=deadline_form,
        deadline_error_message=_creation_error_message(teacher_new_error),
        timezone_names=lifecycle.timezone_names(),
    ), _creation_error_status(teacher_new_error) if teacher_new_error else 200


def _render_assignment_detail(conn, assignment, *, status_code: int = 200, edit_error: str | None = None,
                              edit_form_values: dict | None = None, duplicate_error: str | None = None,
                              duplicate_form_values: dict | None = None, lifecycle_message: str | None = None):
    """The teacher's assignment page, with its lifecycle state, on ``conn``.

    The caller has authorized the principal and checked the hold. Lifecycle times
    are shown in the assignment's stored zone, the zone named; a row created
    before the lifecycle migration has no zone and is shown in the application
    default.
    """
    principal = current_principal()
    assignment_id = int(assignment["id"])
    can_manage = can_manage_assignment(principal, assignment["owner_user_id"])

    attempts = conn.execute("""
        SELECT *
        FROM quiz_attempts
        WHERE assignment_id = ?
        ORDER BY started_at
    """, (assignment_id,)).fetchall()
    attempts = attach_attempt_question_image_counts(conn, attempts)

    clock = lifecycle.read_clock(conn)
    view = lifecycle.evaluate(assignment, clock.now)
    presentation = lifecycle.describe(view)
    preselected_zone = lifecycle.preselected_timezone(conn, principal.user_id)
    default_form = lifecycle.default_deadline_form(clock.now, preselected_zone)

    # Edit form: the stored deadline and zone; a row from before the lifecycle
    # migration has no zone yet, so the teacher is asked to select one.
    if presentation["deadline"]:
        edit_deadline_form = {
            "choice": lifecycle.DEADLINE_CHOICE_DEADLINE,
            "form_date": presentation["deadline"]["form_date"],
            "form_time": presentation["deadline"]["form_time"],
        }
    else:
        edit_deadline_form = {
            "choice": lifecycle.DEADLINE_CHOICE_NONE,
            "form_date": default_form["form_date"],
            "form_time": default_form["form_time"],
        }
    edit_deadline_form["zone"] = presentation["zone"] if presentation["zone_is_stored"] else preselected_zone
    edit_deadline_form["max_days"] = lifecycle.MAX_DEADLINE_DAYS

    quiz_url = _student_quiz_url(assignment)
    return render_template(
        "teacher_assignment.html",
        assignment=assignment,
        attempts=attempts,
        quiz_url=quiz_url,
        qr_available=can_manage and quiz_url is not None,
        mixed_status=quiz_assignment_mixed_status(assignment["question_plan_json"]),
        edit_error=edit_error,
        edit_error_message=lifecycle.DEADLINE_ERROR_MESSAGES_BG.get(edit_error),
        edit_form_values=edit_form_values,
        edit_deadline_form=(edit_form_values or {}).get("deadline") or edit_deadline_form,
        duplicate_error=duplicate_error,
        duplicate_error_message=_creation_error_message(duplicate_error),
        duplicate_deadline_form=duplicate_form_values or default_form,
        can_manage=can_manage,
        lifecycle=presentation,
        lifecycle_message=lifecycle_message,
        lifecycle_writes_unavailable=not clock.healthy,
        timezone_names=lifecycle.timezone_names(),
    ), status_code


@teacher_bp.route("/teacher/assignment/<int:assignment_id>")
@access_policy(TEACHER)
def teacher_assignment(assignment_id):
    with quiz_connection() as conn:
        assignment = quiz_fetch_assignment(conn, assignment_id)
        if not quiz_can_access_assignment(assignment, current_principal()):
            abort(404)
        lifecycle.require_served(assignment)
        rendered = _render_assignment_detail(conn, assignment)
        lifecycle.observe_if_needed(
            conn, assignment_id, lifecycle.evaluate(assignment, lifecycle.read_clock(conn).now))
    return rendered


QR_SVG_HEADERS = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    # Opened directly, the image is an inert document: nothing loads or runs.
    "Content-Security-Policy": "default-src 'none'; sandbox",
}


@teacher_bp.route("/teacher/assignment/<int:assignment_id>/qr.svg")
@access_policy(ASSIGNMENT_OWNER)
def teacher_assignment_qr(assignment_id):
    """The student link of a managed assignment as a QR code (SVG).

    Owner or admin only, decided before anything is generated. What is encoded
    comes from the stored token and the configured public base URL alone; the only
    request input, ``download=1``, picks attachment over inline. Nothing is stored.
    The code keeps working for a closed assignment: the link it encodes then leads
    to the closed notice.
    """
    conn = quiz_db()
    assignment = _fetch_managed_assignment(conn, assignment_id)
    conn.close()
    lifecycle.require_served(assignment)
    if quiz_student_entry_path(assignment) is None:
        abort(404)
    student_url = _student_quiz_url(assignment)
    if student_url is None:
        abort(503)  # no trusted public base URL: refuse rather than encode request data

    disposition = "attachment" if request.args.get("download") == "1" else "inline"
    filename = f"assignment_{int(assignment['id'])}_qr.svg"
    return Response(
        qr_svg(student_url),
        mimetype="image/svg+xml",
        headers={**QR_SVG_HEADERS, "Content-Disposition": f'{disposition}; filename="{filename}"'},
    )


QUIZ_DUPLICATE_TITLE_SUFFIX = " (копие)"
QUIZ_TITLE_MAX_LENGTH = 200
QUIZ_TIME_LIMIT_MAX_MINUTES = 600

ASSIGNMENT_CLOSED_MESSAGE = "Тестът е затворен и вече не може да се редактира."


def quiz_duplicate_title(title: str | None) -> str:
    base_title = title or ""
    max_base_length = max(0, QUIZ_TITLE_MAX_LENGTH - len(QUIZ_DUPLICATE_TITLE_SUFFIX))
    return f"{base_title[:max_base_length]}{QUIZ_DUPLICATE_TITLE_SUFFIX}"[:QUIZ_TITLE_MAX_LENGTH]


@teacher_bp.route("/teacher/assignment/<int:assignment_id>/edit", methods=["POST"])
@access_policy(ASSIGNMENT_OWNER)
@integrity_policy(SAME_ORIGIN)
def teacher_assignment_edit(assignment_id):
    """Edit the title, the time limit and the deadline with its zone of an open assignment.

    Exactly these inputs are read (the whitelist of the design, 6.4): a submitted
    owner, token, closure or hold field is ignored. After closure, manual or by
    expiry, everything is frozen: a direct POST changes nothing and gets the page
    with status 409. The freeze is the predicate of the update, not a hidden form.
    """
    with quiz_connection() as conn:
        assignment = _fetch_managed_assignment(conn, assignment_id)
        lifecycle.require_served(assignment)

        raw_title = (request.form.get("title_bg") or "").replace("\x00", "").strip()
        raw_time = (request.form.get("time_limit_minutes") or "").strip()

        error = None
        new_time_limit = None
        deadline_request = None
        if not raw_title:
            error = "title_required"
        elif len(raw_title) > QUIZ_TITLE_MAX_LENGTH:
            error = "title_too_long"
        elif raw_time:
            try:
                new_time_limit = int(raw_time)
            except ValueError:
                error = "time_invalid"
            else:
                if new_time_limit < 1 or new_time_limit > QUIZ_TIME_LIMIT_MAX_MINUTES:
                    error = "time_out_of_range"
        if error is None:
            try:
                deadline_request = lifecycle.parse_deadline_request(request.form)
            except lifecycle.DeadlineError as exc:
                error = exc.code

        # Closure is decided inside the writer transaction, before any input
        # error is reported: a closed assignment is frozen whatever was sent.
        result = lifecycle.edit_assignment(
            conn, assignment_id, title=raw_title, time_limit_minutes=new_time_limit,
            deadline=deadline_request, input_error=error, actor_user_id=current_principal().user_id)

        if result.outcome != lifecycle.EDIT_UPDATED:
            assignment = quiz_fetch_assignment(conn, assignment_id)
            if result.outcome == lifecycle.EDIT_CLOSED:
                return _render_assignment_detail(
                    conn, assignment, status_code=409, lifecycle_message=ASSIGNMENT_CLOSED_MESSAGE)
            return _render_assignment_detail(
                conn, assignment, status_code=400, edit_error=result.error,
                edit_form_values={
                    "title_bg": raw_title,
                    "time_limit_minutes": raw_time,
                    "deadline": _submitted_deadline_form(request.form),
                })

    quiz_write_assignment_note(assignment_id, request.host_url)
    return redirect(url_for("teacher.teacher_assignment", assignment_id=assignment_id))


@teacher_bp.route("/teacher/assignment/<int:assignment_id>/duplicate", methods=["POST"])
@access_policy(ASSIGNMENT_OWNER)
@integrity_policy(SAME_ORIGIN)
def teacher_assignment_duplicate(assignment_id):
    """Create a copy with its own deadline choice, owner and public token.

    The form carries the same deadline or no-deadline choice and zone selector as
    creation and is validated the same way; a request without a valid choice
    creates nothing. Nothing of the source's lifecycle is copied: not its
    deadline, zone, closure or markers. Duplicating a closed assignment stays
    allowed: it is the way to correct one. A held source is never duplicated,
    also when the hold is added while this request is being handled.
    """
    with quiz_connection() as conn:
        # A first look, so that a foreign, missing or held source is answered
        # before the form is read. It decides nothing about the copy: the source
        # is read again, and checked again, under the writer lock.
        source = _fetch_managed_assignment(conn, assignment_id)
        lifecycle.require_served(source)

        new_assignment_id = None
        try:
            deadline_request = lifecycle.parse_deadline_request(request.form)
        except lifecycle.DeadlineError as exc:
            duplicate_error = exc.code
        else:
            new_assignment_id, duplicate_error = _duplicate_assignment_with_deadline(
                conn, assignment_id, deadline_request)

        if new_assignment_id is None:
            return _render_assignment_detail(
                conn, source, status_code=_creation_error_status(duplicate_error),
                duplicate_error=duplicate_error,
                duplicate_form_values=_submitted_deadline_form(request.form))

    quiz_write_assignment_note(new_assignment_id, request.host_url)
    return redirect(url_for("teacher.teacher_assignment", assignment_id=new_assignment_id))


@teacher_bp.route("/teacher/assignment/<int:assignment_id>/close", methods=["POST"])
@access_policy(ASSIGNMENT_OWNER)
@integrity_policy(SAME_ORIGIN)
def teacher_assignment_close(assignment_id):
    """Close an assignment manually. Irreversible: there is no reopening.

    POST only, same-origin, owner or admin. The request body is not read: the
    closure time is the server's ``now`` and the actor is the session principal.
    Closing an assignment that is already closed (manually or by its deadline)
    changes nothing and answers with the same redirect; the page shows the state.
    """
    with quiz_connection() as conn:
        _fetch_managed_assignment(conn, assignment_id)
        outcome = lifecycle.close_assignment(
            conn, assignment_id, principal_user_id=current_principal().user_id)

    if outcome != lifecycle.CLOSE_ALREADY_CLOSED:
        quiz_write_assignment_note(assignment_id, request.host_url)
    return redirect(url_for("teacher.teacher_assignment", assignment_id=assignment_id))


TEACHER_NOTE_MAX_LENGTH = 1000


def quiz_parse_review_points(value: object, *, points_possible: float) -> float:
    raw = "" if value is None else str(value).strip().replace(",", ".")
    if raw == "":
        raise ValueError("points value is required")
    points = float(raw)
    if not math.isfinite(points):
        raise ValueError("points value must be finite")
    if points < 0 or points > points_possible:
        raise ValueError("points value out of range")
    return points


QUIZ_RESULTS_ALLOWED_STATUS = {"all", "submitted", "unsubmitted"}
QUIZ_RESULTS_ALLOWED_OPEN = {"all", "has_open", "no_open"}
QUIZ_RESULTS_ALLOWED_SORTS = {
    "default",
    "name_asc",
    "name_desc",
    "submitted_desc",
    "submitted_asc",
    "mc_desc",
    "mc_asc",
    "open_desc",
    "open_asc",
}
QUIZ_RESULTS_MIXED_ONLY_SORTS = {"open_desc", "open_asc"}


def quiz_parse_results_filter_args(args, *, is_mixed: bool) -> dict:
    q = (args.get("q") or "").strip()
    status = (args.get("status") or "all").strip().lower()
    if status not in QUIZ_RESULTS_ALLOWED_STATUS:
        status = "all"
    open_filter = (args.get("open") or "all").strip().lower()
    if open_filter not in QUIZ_RESULTS_ALLOWED_OPEN:
        open_filter = "all"
    sort = (args.get("sort") or "default").strip().lower()
    if sort not in QUIZ_RESULTS_ALLOWED_SORTS:
        sort = "default"
    if sort in QUIZ_RESULTS_MIXED_ONLY_SORTS and not is_mixed:
        sort = "default"
    return {"q": q, "status": status, "open": open_filter, "sort": sort}


def quiz_filter_results_attempts(attempts, *, filters, attempt_answers_by_id):
    q_lower = (filters["q"] or "").lower()
    status = filters["status"]
    open_filter = filters["open"]

    def keep(attempt):
        if q_lower and q_lower not in (attempt["student_name"] or "").lower():
            return False
        if status == "submitted" and not attempt["submitted_at"]:
            return False
        if status == "unsubmitted" and attempt["submitted_at"]:
            return False
        if open_filter != "all":
            has_open = bool(attempt_answers_by_id.get(int(attempt["id"])))
            if open_filter == "has_open" and not has_open:
                return False
            if open_filter == "no_open" and has_open:
                return False
        return True

    return [a for a in attempts if keep(a)]


def quiz_sort_results_attempts(attempts, *, sort, open_subtotal_by_id):
    if sort == "default":
        return list(attempts)
    submitted = [a for a in attempts if a["submitted_at"]]
    unsubmitted = [a for a in attempts if not a["submitted_at"]]
    if sort == "name_asc":
        return sorted(attempts, key=lambda a: (a["student_name"] or "").casefold())
    if sort == "name_desc":
        return sorted(
            attempts,
            key=lambda a: (a["student_name"] or "").casefold(),
            reverse=True,
        )
    if sort == "submitted_desc":
        submitted.sort(key=lambda a: a["submitted_at"] or "", reverse=True)
        unsubmitted.sort(key=lambda a: a["started_at"] or "", reverse=True)
        return submitted + unsubmitted
    if sort == "submitted_asc":
        submitted.sort(key=lambda a: a["submitted_at"] or "")
        unsubmitted.sort(key=lambda a: a["started_at"] or "")
        return submitted + unsubmitted
    if sort == "mc_desc":
        submitted.sort(
            key=lambda a: float(a["percent"]) if a["percent"] is not None else float("-inf"),
            reverse=True,
        )
        return submitted + unsubmitted
    if sort == "mc_asc":
        submitted.sort(
            key=lambda a: float(a["percent"]) if a["percent"] is not None else float("inf"),
        )
        return submitted + unsubmitted
    if sort == "open_desc":
        submitted.sort(
            key=lambda a: float(
                (open_subtotal_by_id.get(int(a["id"])) or {}).get("awarded") or 0
            ),
            reverse=True,
        )
        return submitted + unsubmitted
    if sort == "open_asc":
        submitted.sort(
            key=lambda a: float(
                (open_subtotal_by_id.get(int(a["id"])) or {}).get("awarded") or 0
            ),
        )
        return submitted + unsubmitted
    return list(attempts)


def _review_open_answer(assignment_id: int):
    """Store a teacher's review of one open answer, in one writer transaction.

    Review stays allowed after closure: closure ends student input, not teacher
    grading. The answer is looked up and updated under the writer lock, so the
    row that was authorized is the row that is changed. A held assignment is not
    served.
    """
    with quiz_connection() as conn:
        assignment = _fetch_managed_assignment(conn, assignment_id)
        lifecycle.require_served(assignment)

        try:
            answer_id = int(request.form.get("text_answer_id", ""))
        except (TypeError, ValueError):
            abort(400)

        override_raw = (request.form.get("teacher_override") or "").strip()
        if override_raw in {"", "0", "none"}:
            teacher_status = None
        elif override_raw == "1":
            teacher_status = "correct"
        elif override_raw in TEACHER_OPEN_ANSWER_STATUSES:
            teacher_status = override_raw
        else:
            abort(400)
        override_value = 1 if teacher_status else 0

        teacher_note = (request.form.get("teacher_note") or "").replace("\x00", "").strip()
        if len(teacher_note) > TEACHER_NOTE_MAX_LENGTH:
            abort(400)
        teacher_note_value = teacher_note or None

        conn.execute("BEGIN IMMEDIATE")
        # The answer must belong to an attempt of this (already authorized)
        # assignment, so an id from another assignment is a 404.
        row = conn.execute("""
            SELECT qta.id, qta.is_correct, qta.points_possible, qa.lifecycle_hold
            FROM quiz_text_answers qta
            JOIN quiz_attempts qt ON qt.id = qta.attempt_id
            JOIN quiz_assignments qa ON qa.id = qt.assignment_id
            WHERE qta.id = ?
              AND qa.id = ?
        """, (answer_id, assignment_id)).fetchone()
        if not row:
            abort(404)
        if row["lifecycle_hold"] is not None:
            raise lifecycle.AssignmentHeld()

        try:
            points_possible = float(row["points_possible"] or 0)
        except (TypeError, ValueError):
            points_possible = 0.0
        if teacher_status:
            points_raw = request.form.get("points_awarded")
            if points_raw is None or str(points_raw).strip() == "":
                points_awarded = points_possible if teacher_status == "correct" else 0.0
            else:
                try:
                    points_awarded = quiz_parse_review_points(points_raw, points_possible=points_possible)
                except (TypeError, ValueError):
                    abort(400)
        else:
            points_awarded = quiz_text_auto_points(row)

        conn.execute("""
            UPDATE quiz_text_answers
            SET teacher_override = ?,
                teacher_override_status = ?,
                points_awarded = ?,
                teacher_note = ?
            WHERE id = ?
        """, (override_value, teacher_status, points_awarded, teacher_note_value, answer_id))
        conn.commit()
        # Best effort, in its own clock-checked transaction: a passed deadline or an
        # ended grace window this request saw is recorded. Never changes the response.
        lifecycle.observe(conn, assignment_id)
    return redirect(url_for("teacher.teacher_assignment_results", assignment_id=assignment_id))


@teacher_bp.route("/teacher/assignment/<int:assignment_id>/results", methods=["GET", "POST"])
@access_policy(ASSIGNMENT_OWNER)
@integrity_policy(SAME_ORIGIN)
def teacher_assignment_results(assignment_id):
    if request.method == "POST":
        return _review_open_answer(assignment_id)

    conn = quiz_db()
    # One read transaction for the whole response: the authorization read and
    # every result row come from the same snapshot.
    conn.execute("BEGIN")
    assignment = _fetch_managed_assignment(conn, assignment_id)
    try:
        lifecycle.require_served(assignment)
    except lifecycle.LifecycleUnavailable:
        conn.close()
        raise
    lifecycle_view = lifecycle.evaluate(assignment, lifecycle.read_clock(conn).now)

    mixed_status = quiz_assignment_mixed_status(assignment["question_plan_json"])
    filters = quiz_parse_results_filter_args(
        request.args, is_mixed=bool(mixed_status["is_mixed"])
    )

    all_attempts = conn.execute("""
        SELECT
            id,
            student_name,
            question_ids_json,
            started_at,
            submitted_at,
            score_correct,
            score_total,
            CASE
              WHEN score_total IS NOT NULL AND score_total > 0
              THEN ROUND(100.0 * score_correct / score_total, 1)
              ELSE NULL
            END AS percent
        FROM quiz_attempts
        WHERE assignment_id = ?
        ORDER BY
            submitted_at IS NULL,
            submitted_at DESC,
            started_at DESC,
            student_name
    """, (assignment_id,)).fetchall()

    attempt_answers_by_id: dict[int, list[dict]] = {}
    open_subtotal_by_id: dict[int, dict] = {}
    combined_score_by_id: dict[int, dict] = {}
    for attempt in all_attempts:
        if not attempt["submitted_at"]:
            continue
        attempt_id_int = int(attempt["id"])
        ans = fetch_quiz_text_answers_for_attempt(conn, attempt_id_int)
        attempt_answers_by_id[attempt_id_int] = ans
        if ans:
            sub = quiz_text_answer_informational_subtotal(ans)
            open_subtotal_by_id[attempt_id_int] = sub
            attempt_plan = quiz_parse_attempt_question_plan(attempt["question_ids_json"])
            combined = quiz_combined_score_summary(
                attempt,
                sub,
                enabled=bool(attempt_plan["include_open_answers_in_final_score"]),
            )
            if combined:
                combined_score_by_id[attempt_id_int] = combined

    filtered_attempts = quiz_filter_results_attempts(
        all_attempts, filters=filters, attempt_answers_by_id=attempt_answers_by_id
    )
    attempts = quiz_sort_results_attempts(
        filtered_attempts,
        sort=filters["sort"],
        open_subtotal_by_id=open_subtotal_by_id,
    )
    attempts = attach_attempt_question_image_counts(conn, attempts)

    open_text_answers = []
    open_subtotals_by_attempt: dict[int, dict] = {}
    combined_scores_by_attempt: dict[int, dict] = {}
    mc_percentages: list[float] = []
    submitted_attempt_ids: list[int] = []
    submitted_attempt_count = 0
    open_answer_attempt_count = 0
    open_answer_total = 0
    open_answer_auto_matched_count = 0
    open_answer_teacher_override_count = 0
    open_subtotal_awarded_total = 0.0
    open_subtotal_possible_total = 0.0
    for attempt in attempts:
        if not attempt["submitted_at"]:
            continue
        attempt_id_int = int(attempt["id"])
        submitted_attempt_ids.append(attempt_id_int)
        submitted_attempt_count += 1
        if attempt["score_total"] is not None and int(attempt["score_total"]) > 0:
            mc_percentages.append(
                100.0 * float(attempt["score_correct"] or 0) / float(attempt["score_total"])
            )
        attempt_answers = attempt_answers_by_id.get(attempt_id_int, [])
        if attempt_answers:
            open_subtotal = open_subtotal_by_id[attempt_id_int]
            open_subtotals_by_attempt[attempt_id_int] = open_subtotal
            if attempt_id_int in combined_score_by_id:
                combined_scores_by_attempt[attempt_id_int] = combined_score_by_id[attempt_id_int]
            open_answer_attempt_count += 1
            open_subtotal_awarded_total += float(open_subtotal.get("awarded") or 0)
            open_subtotal_possible_total += float(open_subtotal.get("possible") or 0)
        for answer in attempt_answers:
            open_answer_total += 1
            if answer.get("is_correct") == 1:
                open_answer_auto_matched_count += 1
            if answer.get("teacher_override") == 1:
                open_answer_teacher_override_count += 1
            open_text_answers.append({
                **answer,
                "student_name": attempt["student_name"],
                "attempt_id": attempt_id_int,
                "attempt_score_correct": attempt["score_correct"],
                "attempt_score_total": attempt["score_total"],
                "submitted_at": attempt["submitted_at"],
            })

    attach_question_image_indicators(conn, open_text_answers)

    totals = conn.execute("""
        SELECT
            COUNT(*) AS attempts_count,
            SUM(CASE WHEN submitted_at IS NOT NULL THEN 1 ELSE 0 END) AS submitted_count,
            SUM(CASE WHEN submitted_at IS NULL THEN 1 ELSE 0 END) AS unfinished_count,
            AVG(CASE
              WHEN submitted_at IS NOT NULL AND score_total > 0
              THEN 100.0 * score_correct / score_total
              ELSE NULL
            END) AS avg_percent
        FROM quiz_attempts
        WHERE assignment_id = ?
    """, (assignment_id,)).fetchone()
    difficulty_breakdown = quiz_teacher_difficulty_breakdown(conn, submitted_attempt_ids)

    conn.rollback()  # end of the read transaction; nothing was written
    lifecycle.observe_if_needed(conn, assignment_id, lifecycle_view)
    conn.close()

    summary = {
        "submitted_attempt_count": submitted_attempt_count,
        "highest_mc_percent": round(max(mc_percentages), 1) if mc_percentages else None,
        "lowest_mc_percent": round(min(mc_percentages), 1) if mc_percentages else None,
        "mixed_open_enabled": bool(mixed_status["is_mixed"]),
        "combined_score_display_enabled": bool(mixed_status["combined_score"]),
        "open_answer_attempt_count": open_answer_attempt_count,
        "open_answer_total": open_answer_total,
        "open_answer_auto_matched_count": open_answer_auto_matched_count,
        "open_answer_teacher_override_count": open_answer_teacher_override_count,
        "open_subtotal_awarded_total": round(open_subtotal_awarded_total, 2),
        "open_subtotal_possible_total": round(open_subtotal_possible_total, 2),
        "difficulty_breakdown": difficulty_breakdown,
    }

    attempts = [
        {
            **dict(attempt),
            "duration_display": quiz_attempt_duration_display(attempt) if attempt["submitted_at"] else QUIZ_DURATION_FALLBACK_BG,
            "success_rate_display": quiz_attempt_success_rate_display(attempt) if attempt["submitted_at"] else QUIZ_SUCCESS_RATE_FALLBACK_BG,
        }
        for attempt in attempts
    ]

    filter_active = (
        bool(filters["q"]) or filters["status"] != "all" or filters["open"] != "all"
    )
    sort_active = filters["sort"] != "default"

    return render_template(
        "teacher_results.html",
        assignment=assignment,
        attempts=attempts,
        open_text_answers=open_text_answers,
        open_subtotals_by_attempt=open_subtotals_by_attempt,
        combined_scores_by_attempt=combined_scores_by_attempt,
        totals=totals,
        mixed_status=mixed_status,
        summary=summary,
        filters=filters,
        filter_active=filter_active,
        sort_active=sort_active,
        lifecycle=lifecycle.describe(lifecycle_view),
    )


QUIZ_RESULTS_EXPORT_COLUMNS = [
    "row_type",
    "assignment_id",
    "assignment_title",
    "attempt_id",
    "student_name",
    "submitted_at",
    "mc_score_correct",
    "mc_score_total",
    "mc_percent",
    "mixed_open_enabled",
    "include_open_answers_in_final_score",
    "open_answer_count",
    "open_subtotal_awarded",
    "open_subtotal_possible",
    "combined_awarded",
    "combined_possible",
    "question_id",
    "subquestion_number",
    "raw_answer",
    "normalized_answer",
    "matched_answer",
    "points_awarded",
    "points_possible",
    "grading_mode",
    "teacher_override",
    "teacher_note",
    "is_correct",
    # Lifecycle of the assignment (UTC as stored; the state as computed at export
    # time). Appended, never inserted, so earlier columns keep their positions.
    "assignment_deadline_at",
    "assignment_closed_at",
    "assignment_lifecycle_state",
]


def _quiz_csv_value(value):
    if value is None:
        return ""
    return value


def _quiz_csv_text(value: str | None) -> str:
    """Prefix formula-like text with an apostrophe before CSV serialization."""
    if value is None:
        return ""
    for char in value:
        if char.isspace() or unicodedata.category(char) in {"Cc", "Cf"}:
            continue
        if char in "=+-@":
            return "'" + value
        break
    return value


def _quiz_format_points(value) -> str:
    try:
        return f"{float(value):.4f}"
    except (TypeError, ValueError):
        return ""


@teacher_bp.route("/teacher/assignment/<int:assignment_id>/results.csv")
@access_policy(ASSIGNMENT_OWNER)
def teacher_assignment_results_export(assignment_id):
    conn = quiz_db()
    # One read transaction: the file is entirely before or entirely after any
    # concurrent commit, and is built in memory before the transaction ends.
    conn.execute("BEGIN")
    assignment = _fetch_managed_assignment(conn, assignment_id)
    try:
        lifecycle.require_served(assignment)
    except lifecycle.LifecycleUnavailable:
        conn.close()
        raise
    lifecycle_view = lifecycle.evaluate(assignment, lifecycle.read_clock(conn).now)
    lifecycle_cells = [
        _quiz_csv_value(assignment["deadline_at"]),
        _quiz_csv_value(assignment["closed_at"]),
        lifecycle_view.state,
    ]

    apply_filters = (request.args.get("filtered") or "").strip().lower() in {
        "1",
        "true",
        "yes",
    }

    attempt_answers_by_id: dict[int, list[dict]] = {}

    if apply_filters:
        all_attempts = conn.execute("""
            SELECT
                id,
                student_name,
                question_ids_json,
                started_at,
                submitted_at,
                score_correct,
                score_total,
                CASE
                  WHEN score_total IS NOT NULL AND score_total > 0
                  THEN ROUND(100.0 * score_correct / score_total, 1)
                  ELSE NULL
                END AS percent
            FROM quiz_attempts
            WHERE assignment_id = ?
            ORDER BY
                submitted_at IS NULL,
                submitted_at DESC,
                started_at DESC,
                student_name
        """, (assignment_id,)).fetchall()

        open_subtotal_by_id: dict[int, dict] = {}
        for attempt in all_attempts:
            if not attempt["submitted_at"]:
                continue
            ans = fetch_quiz_text_answers_for_attempt(conn, int(attempt["id"]))
            attempt_answers_by_id[int(attempt["id"])] = ans
            if ans:
                open_subtotal_by_id[int(attempt["id"])] = (
                    quiz_text_answer_informational_subtotal(ans)
                )

        mixed_status = quiz_assignment_mixed_status(assignment["question_plan_json"])
        filters = quiz_parse_results_filter_args(
            request.args, is_mixed=bool(mixed_status["is_mixed"])
        )
        filtered = quiz_filter_results_attempts(
            all_attempts, filters=filters, attempt_answers_by_id=attempt_answers_by_id
        )
        attempts = quiz_sort_results_attempts(
            filtered,
            sort=filters["sort"],
            open_subtotal_by_id=open_subtotal_by_id,
        )
        filename = f"assignment_{assignment_id}_results_filtered.csv"
    else:
        attempts = conn.execute("""
            SELECT
                id,
                student_name,
                question_ids_json,
                started_at,
                submitted_at,
                score_correct,
                score_total
            FROM quiz_attempts
            WHERE assignment_id = ?
              AND submitted_at IS NOT NULL
            ORDER BY submitted_at DESC, started_at DESC, student_name
        """, (assignment_id,)).fetchall()
        filename = f"assignment_{assignment_id}_results.csv"

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(QUIZ_RESULTS_EXPORT_COLUMNS)

    for attempt in attempts:
        attempt_id_int = int(attempt["id"])

        if not attempt["submitted_at"]:
            writer.writerow([
                "attempt",
                assignment_id,
                _quiz_csv_text(assignment["title_bg"]),
                attempt_id_int,
                _quiz_csv_text(attempt["student_name"]),
                "",
                "",
                "",
                "",
                "",
                "",
                0,
                "",
                "",
                "",
                "",
                "", "", "", "", "", "", "", "", "", "", "",
                *lifecycle_cells,
            ])
            continue

        if attempt_id_int in attempt_answers_by_id:
            attempt_answers = attempt_answers_by_id[attempt_id_int]
        else:
            attempt_answers = fetch_quiz_text_answers_for_attempt(conn, attempt_id_int)
        attempt_plan = quiz_parse_attempt_question_plan(attempt["question_ids_json"])
        mixed_open_enabled = bool(attempt_plan["mixed_open_enabled"])
        combined_enabled = bool(attempt_plan["include_open_answers_in_final_score"])

        open_subtotal = (
            quiz_text_answer_informational_subtotal(attempt_answers)
            if attempt_answers
            else None
        )
        combined_score = quiz_combined_score_summary(
            attempt, open_subtotal, enabled=combined_enabled
        )

        mc_correct = attempt["score_correct"]
        mc_total = attempt["score_total"]
        mc_percent = ""
        if mc_total is not None and int(mc_total) > 0 and mc_correct is not None:
            mc_percent = f"{round(100.0 * float(mc_correct) / float(mc_total), 1)}"

        writer.writerow([
            "attempt",
            assignment_id,
            _quiz_csv_text(assignment["title_bg"]),
            attempt_id_int,
            _quiz_csv_text(attempt["student_name"]),
            attempt["submitted_at"],
            _quiz_csv_value(mc_correct),
            _quiz_csv_value(mc_total),
            mc_percent,
            "1" if mixed_open_enabled else "0",
            "1" if combined_enabled else "0",
            len(attempt_answers),
            _quiz_format_points(open_subtotal["awarded"]) if open_subtotal else "",
            _quiz_format_points(open_subtotal["possible"]) if open_subtotal else "",
            _quiz_format_points(combined_score["combined_awarded"]) if combined_score else "",
            _quiz_format_points(combined_score["combined_possible"]) if combined_score else "",
            "", "", "", "", "", "", "", "", "", "", "",
            *lifecycle_cells,
        ])

        for ans in attempt_answers:
            writer.writerow([
                "open_answer",
                assignment_id,
                _quiz_csv_text(assignment["title_bg"]),
                attempt_id_int,
                _quiz_csv_text(attempt["student_name"]),
                attempt["submitted_at"],
                "",
                "",
                "",
                "1" if mixed_open_enabled else "0",
                "1" if combined_enabled else "0",
                "",
                "",
                "",
                "",
                "",
                _quiz_csv_value(ans.get("question_id")),
                _quiz_csv_value(ans.get("subquestion_number")),
                _quiz_csv_text(ans.get("raw_answer")),
                _quiz_csv_text(ans.get("normalized_answer")),
                _quiz_csv_text(ans.get("matched_answer")),
                _quiz_csv_value(ans.get("points_awarded")),
                _quiz_csv_value(ans.get("points_possible")),
                _quiz_csv_value(ans.get("grading_mode")),
                _quiz_csv_value(ans.get("teacher_override")),
                _quiz_csv_text(ans.get("teacher_note")),
                _quiz_csv_value(ans.get("is_correct")),
                *lifecycle_cells,
            ])

    conn.rollback()  # end of the read transaction; nothing was written
    conn.close()

    return Response(
        buf.getvalue(),
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
