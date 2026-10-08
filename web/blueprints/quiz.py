"""Student quiz pages: assignment entry, the attempt, its result and "my attempts".

Starting an attempt needs an account (:func:`web.access.can_take_quizzes`:
student, teacher or admin); the attempt then belongs to that account alone. Every attempt page goes through
:func:`web.attempt_access.require_quiz_attempt_access`, which also keeps the
attempts started before accounts reachable from the browser that started them.
"""

from __future__ import annotations

from flask import Blueprint, abort, redirect, render_template, request, url_for

from web.access import (
    ATTEMPT_OWNER,
    PUBLIC,
    QUIZ_TAKER,
    access_policy,
    can_take_quizzes,
    current_principal,
)
from web.attempt_access import require_quiz_attempt_access
from web.request_integrity import SAME_ORIGIN, integrity_policy
from web.services import quiz as quiz_service


quiz_bp = Blueprint("quiz", __name__)

# Templates build student links only through this (None: the assignment has no token yet).
quiz_bp.add_app_template_global(quiz_service.quiz_student_entry_path, "student_quiz_path")


# There is deliberately no "/quiz/<int:assignment_id>" rule. The sequential student
# entry is retired: such a URL matches no rule at all, so Flask answers the same
# generic 404 whether or not an assignment with that id exists, without a lookup,
# a redirect or a token. Students enter only through the token route below.


@quiz_bp.route("/quiz/s/<public_token>", methods=["GET", "POST"])
@access_policy(PUBLIC)
@integrity_policy(SAME_ORIGIN)
def quiz_start_public(public_token):
    # The link a teacher shares, and the only student entry. A malformed, unknown
    # or not yet assigned token is the same 404 and says nothing about which
    # assignments exist. The page itself is public, but an attempt is only ever
    # started for the signed-in account. Nothing submitted with the form says
    # whose attempt it is.
    principal = current_principal()
    start = None
    with quiz_service.quiz_connection() as conn:
        assignment = quiz_service.quiz_fetch_assignment_by_public_token(conn, public_token)
        if not assignment:
            abort(404)

        if request.method == "POST":
            if principal.user_id is None:
                return redirect(url_for("account.login", next=request.path))
            if not can_take_quizzes(principal):
                return render_template("403.html"), 403
            try:
                start = quiz_service.quiz_start_or_resume_attempt(
                    conn,
                    assignment,
                    user_id=principal.user_id,
                    display_name=principal.display_name,
                )
            except quiz_service.QuizPlanError:
                abort(400)

    if start is not None:
        if start.submitted:
            return redirect(url_for("quiz.quiz_result", attempt_id=start.attempt_id))
        return redirect(url_for("quiz.quiz_attempt", attempt_id=start.attempt_id))

    return render_template(
        "quiz_start.html",
        assignment=assignment,
        mixed_status=quiz_service.quiz_assignment_mixed_status(assignment["question_plan_json"]),
        can_start=can_take_quizzes(principal),
        login_url=url_for("account.login", next=request.path),
    )


@quiz_bp.route("/account/attempts")
@access_policy(QUIZ_TAKER)
def my_attempts():
    with quiz_service.quiz_connection() as conn:
        attempts = quiz_service.fetch_account_attempts(conn, current_principal().user_id)
    return render_template("my_attempts.html", attempts=attempts)


@quiz_bp.route("/quiz/attempt/<int:attempt_id>", methods=["GET", "POST"])
@access_policy(ATTEMPT_OWNER)
@integrity_policy(SAME_ORIGIN)
def quiz_attempt(attempt_id):
    submitted_now = False
    with quiz_service.quiz_connection() as conn:
        attempt = require_quiz_attempt_access(conn, attempt_id)

        assignment = quiz_service.quiz_fetch_assignment(conn, int(attempt["assignment_id"]))
        if not assignment:
            abort(404)

        if attempt["submitted_at"]:
            return redirect(url_for("quiz.quiz_result", attempt_id=attempt_id))

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

        return redirect(url_for("quiz.quiz_result", attempt_id=attempt_id))

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
    with quiz_service.quiz_connection() as conn:
        attempt = require_quiz_attempt_access(conn, attempt_id)

        assignment = quiz_service.quiz_fetch_assignment(conn, int(attempt["assignment_id"]))
        if not assignment:
            abort(404)

        if not attempt["submitted_at"]:
            return redirect(url_for("quiz.quiz_attempt", attempt_id=attempt_id))

        attempt_question_plan = quiz_service.quiz_parse_attempt_question_plan(attempt["question_ids_json"])
        context = quiz_service.quiz_result_context(conn, attempt, attempt_question_plan)

    return render_template(
        "quiz_result.html",
        assignment=assignment,
        attempt=attempt,
        **context,
    )
