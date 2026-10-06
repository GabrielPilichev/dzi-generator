from __future__ import annotations

from flask import Blueprint, abort, redirect, render_template, request, session, url_for

from web.access import ATTEMPT_OWNER, PUBLIC, access_policy
from web.request_integrity import SAME_ORIGIN, integrity_policy
from web.services import quiz as quiz_service


quiz_bp = Blueprint("quiz", __name__)


def quiz_attempt_owner_session_key(attempt_id: int) -> str:
    return f"quiz_attempt_owner:{int(attempt_id)}"


def mark_quiz_attempt_owner(attempt_id: int) -> None:
    session[quiz_attempt_owner_session_key(attempt_id)] = True


def has_quiz_attempt_access(attempt_id: int) -> bool:
    return session.get(quiz_attempt_owner_session_key(attempt_id)) is True


def require_quiz_attempt_access(attempt_id: int) -> None:
    if not has_quiz_attempt_access(attempt_id):
        abort(404)


@quiz_bp.route("/quiz/<int:assignment_id>", methods=["GET", "POST"])
@access_policy(PUBLIC)
@integrity_policy(SAME_ORIGIN)
def quiz_start(assignment_id):
    start = None
    name_error = None
    student_name = ""
    with quiz_service.quiz_connection() as conn:
        assignment = quiz_service.quiz_fetch_assignment(conn, assignment_id)
        if not assignment:
            abort(404)

        if request.method == "POST":
            student_name = " ".join((request.form.get("student_name") or "").strip().split())
            name_error = quiz_service.quiz_student_name_error(student_name)
            if not name_error:
                try:
                    start = quiz_service.quiz_start_or_resume_attempt(conn, assignment, student_name)
                except quiz_service.QuizPlanError:
                    abort(400)

    mixed_status = quiz_service.quiz_assignment_mixed_status(assignment["question_plan_json"])
    if name_error:
        return render_template(
            "quiz_start.html",
            assignment=assignment,
            mixed_status=mixed_status,
            name_error=name_error,
            student_name_value=student_name,
        ), 400

    if start is not None:
        if start.created:
            mark_quiz_attempt_owner(start.attempt_id)
            return redirect(url_for("quiz_attempt", attempt_id=start.attempt_id))
        require_quiz_attempt_access(start.attempt_id)
        if start.submitted:
            return redirect(url_for("quiz_result", attempt_id=start.attempt_id))
        return redirect(url_for("quiz_attempt", attempt_id=start.attempt_id))

    return render_template(
        "quiz_start.html",
        assignment=assignment,
        mixed_status=mixed_status,
        name_error=None,
        student_name_value="",
    )


@quiz_bp.route("/quiz/attempt/<int:attempt_id>", methods=["GET", "POST"])
@access_policy(ATTEMPT_OWNER)
@integrity_policy(SAME_ORIGIN)
def quiz_attempt(attempt_id):
    require_quiz_attempt_access(attempt_id)
    submitted_now = False
    with quiz_service.quiz_connection() as conn:
        attempt = quiz_service.quiz_fetch_attempt(conn, attempt_id)
        if not attempt:
            abort(404)

        assignment = quiz_service.quiz_fetch_assignment(conn, int(attempt["assignment_id"]))
        if not assignment:
            abort(404)

        if attempt["submitted_at"]:
            return redirect(url_for("quiz_result", attempt_id=attempt_id))

        attempt_question_plan = quiz_service.quiz_parse_attempt_question_plan(attempt["question_ids_json"])
        open_question_ids = quiz_service.quiz_attempt_open_question_ids(attempt_question_plan)
        question_ids, skipped_count = quiz_service.filter_renderable_attempt_question_ids(
            conn,
            attempt_question_plan["question_ids"],
            open_question_ids=open_question_ids,
        )
        seed = attempt["seed"]
        attempt_mixed_status = quiz_service.quiz_attempt_mixed_status(attempt_question_plan)

        questions = []
        remaining = None
        timer_expired = False
        stale_attempt_message = None
        submit_error = None
        status_code = 200

        if request.method == "POST":
            if not question_ids:
                stale_attempt_message = quiz_service.STALE_ATTEMPT_MESSAGE
            else:
                mc_question_ids = quiz_service.quiz_multiple_choice_question_ids(conn, question_ids)

                remaining = quiz_service.quiz_remaining_seconds(assignment, attempt)
                server_timer_expired = remaining is not None and remaining <= 0
                is_timer_submit = request.form.get("submission_reason") == "timer_expired"
                if not (
                    is_timer_submit
                    and server_timer_expired
                ) and not quiz_service.quiz_has_any_submitted_answer(
                    request.form,
                    mc_question_ids=mc_question_ids,
                    question_ids=question_ids,
                    open_question_ids=open_question_ids,
                ):
                    questions = quiz_service.quiz_load_questions(conn, question_ids, seed, include_correct=False)
                    if not questions:
                        remaining = None
                    timer_expired = server_timer_expired
                    stale_attempt_message = quiz_service.STALE_ATTEMPT_MESSAGE if not questions else None
                    submit_error = quiz_service.QUIZ_EMPTY_SUBMISSION_MESSAGE
                    status_code = 400
                else:
                    quiz_service.quiz_submit_attempt(
                        conn,
                        attempt_id,
                        mc_question_ids=mc_question_ids,
                        question_ids=question_ids,
                        open_question_ids=open_question_ids,
                        form=request.form,
                    )
                    submitted_now = True
        else:
            questions = quiz_service.quiz_load_questions(conn, question_ids, seed, include_correct=False)
            remaining = quiz_service.quiz_remaining_seconds(assignment, attempt)
            if not questions:
                remaining = None
            timer_expired = remaining is not None and remaining <= 0
            stale_attempt_message = quiz_service.STALE_ATTEMPT_MESSAGE if not questions else None

    if submitted_now:
        quiz_service.quiz_write_attempt_note(attempt_id)
        quiz_service.quiz_write_assignment_note(int(assignment["id"]), request.host_url)

        return redirect(url_for("quiz_result", attempt_id=attempt_id))

    return render_template(
        "quiz_attempt.html",
        assignment=assignment,
        attempt=attempt,
        questions=questions,
        remaining_seconds=remaining,
        timer_expired=timer_expired,
        stale_attempt_message=stale_attempt_message,
        submit_error=submit_error,
        skipped_question_count=skipped_count,
        mixed_status=attempt_mixed_status,
    ), status_code


@quiz_bp.route("/quiz/attempt/<int:attempt_id>/result")
@access_policy(ATTEMPT_OWNER)
def quiz_result(attempt_id):
    require_quiz_attempt_access(attempt_id)
    with quiz_service.quiz_connection() as conn:
        attempt = quiz_service.quiz_fetch_attempt(conn, attempt_id)
        if not attempt:
            abort(404)

        assignment = quiz_service.quiz_fetch_assignment(conn, int(attempt["assignment_id"]))
        if not assignment:
            abort(404)

        if not attempt["submitted_at"]:
            return redirect(url_for("quiz_attempt", attempt_id=attempt_id))

        attempt_question_plan = quiz_service.quiz_parse_attempt_question_plan(attempt["question_ids_json"])
        context = quiz_service.quiz_result_context(conn, attempt, attempt_question_plan)

    return render_template(
        "quiz_result.html",
        assignment=assignment,
        attempt=attempt,
        **context,
    )


def register_legacy_quiz_endpoint_aliases(app) -> None:
    aliases = (
        ("/quiz/<int:assignment_id>", "quiz_start", quiz_start, ("GET", "POST")),
        ("/quiz/attempt/<int:attempt_id>", "quiz_attempt", quiz_attempt, ("GET", "POST")),
        ("/quiz/attempt/<int:attempt_id>/result", "quiz_result", quiz_result, ("GET",)),
    )
    for rule, endpoint, view_func, methods in aliases:
        if endpoint not in app.view_functions:
            app.add_url_rule(rule, endpoint=endpoint, view_func=view_func, methods=methods)
