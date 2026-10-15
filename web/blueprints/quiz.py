"""Student quiz pages: assignment entry, the attempt, its result and "my attempts".

Starting an attempt needs an account (:func:`web.access.can_take_quizzes`:
student, teacher or admin); the attempt then belongs to that account alone. Every attempt page goes through
:func:`web.attempt_access.require_quiz_attempt_access`, which also keeps the
attempts started before accounts reachable from the browser that started them.

Lifecycle (docs/ASSIGNMENT_LIFECYCLE_DESIGN.md): authorization is decided first,
then the assignment's hold, then its state. Whether an attempt may be started or
submitted is decided by the service inside its writer transaction; what a page
shows comes from the same state function (:func:`web.services.lifecycle.evaluate`).
A held assignment and an unusable clock raise
``lifecycle.LifecycleUnavailable``, which ``web.app`` answers with 503.

Profile gate (docs/reviews/EMAIL_REGISTRATION_DESIGN.md, 7.4): while it is on, the same
service decision, after resuming and after the closed notice, may answer "profile
required" instead of creating an attempt; this view then redirects (303) to the profile
form. Nothing else here is ever blocked by a missing profile.
"""

from __future__ import annotations

from flask import Blueprint, abort, current_app, redirect, render_template, request, url_for

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
from web.services import lifecycle, profiles
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
    #
    # The token keeps resolving after closure: the page then shows a closed
    # notice instead of the start form, and an account that already has an
    # attempt is sent to it (or to its result).
    principal = current_principal()
    start = None
    existing = None
    with quiz_service.quiz_connection() as conn:
        assignment = quiz_service.quiz_fetch_assignment_by_public_token(conn, public_token)
        if not assignment:
            abort(404)
        lifecycle.require_served(assignment)
        assignment_id = int(assignment["id"])
        clock = lifecycle.read_clock(conn)
        view = lifecycle.evaluate(assignment, clock.now)

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
                    # Decided inside the service, under its writer lock, and only where
                    # an attempt would be created (docs/reviews/EMAIL_REGISTRATION_DESIGN.md
                    # 7.4). This view looks up no attempt of its own.
                    profile_gate=profiles.enabled_in(current_app.config),
                )
            except quiz_service.QuizPlanError:
                abort(400)
        else:
            if view.is_closed and can_take_quizzes(principal):
                existing = quiz_service.quiz_find_account_attempt(conn, assignment_id, principal.user_id)
            lifecycle.observe_if_needed(conn, assignment_id, view)

    closed = view.is_closed
    status_code = 200
    if start is not None:
        if start.closed:
            # Decided under the writer lock, at the instant the lock was obtained.
            closed, status_code = True, 409
        elif start.profile_required:
            # No attempt was created. The profile form returns to this page by GET: the
            # student presses the button again, and nothing starts by itself.
            return redirect(url_for("account.profile", next=request.path), code=303)
        elif start.submitted:
            return redirect(url_for("quiz.quiz_result", attempt_id=start.attempt_id))
        else:
            return redirect(url_for("quiz.quiz_attempt", attempt_id=start.attempt_id))
    elif existing is not None:
        if existing["submitted_at"]:
            return redirect(url_for("quiz.quiz_result", attempt_id=int(existing["id"])))
        return redirect(url_for("quiz.quiz_attempt", attempt_id=int(existing["id"])))

    return render_template(
        "quiz_start.html",
        assignment=assignment,
        mixed_status=quiz_service.quiz_assignment_mixed_status(assignment["question_plan_json"]),
        can_start=can_take_quizzes(principal),
        login_url=url_for("account.login", next=request.path),
        lifecycle=lifecycle.describe(view),
        assignment_closed=closed,
        starts_unavailable=not clock.healthy,
    ), status_code


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
        lifecycle.require_served(assignment)

        if attempt["submitted_at"]:
            return redirect(url_for("quiz.quiz_result", attempt_id=attempt_id))

        clock = lifecycle.read_clock(conn)
        view = lifecycle.evaluate(assignment, clock.now)
        # What this request shows. Whether a POST is accepted is decided again by
        # the submission service, under the writer lock.
        attempt_closed = not lifecycle.attempt_submittable(view, attempt)

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
                if attempt_closed:
                    status_code = 409
                else:
                    stale_attempt_message = quiz_service.STALE_ATTEMPT_MESSAGE
            else:
                mc_question_ids = quiz_service.quiz_multiple_choice_question_ids(conn, question_ids)

                remaining = quiz_service.quiz_remaining_seconds(assignment, attempt)
                server_timer_expired = remaining is not None and remaining <= 0
                is_timer_submit = request.form.get("submission_reason") == "timer_expired"
                empty_submission = not (
                    is_timer_submit
                    and server_timer_expired
                ) and not quiz_service.quiz_has_any_submitted_answer(
                    request.form,
                    mc_question_ids=mc_question_ids,
                    question_ids=question_ids,
                    open_question_ids=open_question_ids,
                )
                # ``submission_reason`` and the client timer only ever allow an
                # empty submission; they decide nothing about the lifecycle cutoff.
                submission = quiz_service.quiz_submit_attempt(
                    conn,
                    attempt_id,
                    mc_question_ids=mc_question_ids,
                    question_ids=question_ids,
                    open_question_ids=open_question_ids,
                    form=request.form,
                    empty_submission=empty_submission,
                )
                if submission.outcome == quiz_service.SUBMIT_SUBMITTED:
                    submitted_now = True
                elif submission.outcome == quiz_service.SUBMIT_ALREADY_SUBMITTED:
                    return redirect(url_for("quiz.quiz_result", attempt_id=attempt_id))
                elif submission.outcome == quiz_service.SUBMIT_CLOSED:
                    attempt_closed, status_code = True, 409
                    remaining = None
                else:
                    questions = quiz_service.quiz_load_questions(conn, question_ids, seed, include_correct=False)
                    if not questions:
                        remaining = None
                    timer_expired = server_timer_expired
                    stale_attempt_message = quiz_service.STALE_ATTEMPT_MESSAGE if not questions else None
                    submit_error = quiz_service.QUIZ_EMPTY_SUBMISSION_MESSAGE
                    status_code = 400
        else:
            if not attempt_closed:
                questions = quiz_service.quiz_load_questions(conn, question_ids, seed, include_correct=False)
                remaining = quiz_service.quiz_remaining_seconds(assignment, attempt)
                if not questions:
                    remaining = None
                timer_expired = remaining is not None and remaining <= 0
                stale_attempt_message = quiz_service.STALE_ATTEMPT_MESSAGE if not questions else None
                if remaining is not None and view.in_grace:
                    # In grace the page shows the earlier of the timer end and E + G.
                    remaining = min(remaining, max(0, int((view.submit_cutoff - view.now).total_seconds())))
            lifecycle.observe_if_needed(conn, int(assignment["id"]), view)

    if submitted_now:
        # Side effects belong to the request that returned ``submitted``, and to no other.
        quiz_service.quiz_write_attempt_note(attempt_id)
        quiz_service.quiz_write_assignment_note(int(assignment["id"]), request.host_url)

        return redirect(url_for("quiz.quiz_result", attempt_id=attempt_id))

    return render_template(
        "quiz_attempt.html",
        assignment=assignment,
        attempt=attempt,
        questions=[] if attempt_closed else questions,
        remaining_seconds=None if attempt_closed else remaining,
        timer_expired=timer_expired,
        stale_attempt_message=None if attempt_closed else stale_attempt_message,
        submit_error=submit_error,
        skipped_question_count=skipped_count,
        mixed_status=attempt_mixed_status,
        lifecycle=lifecycle.describe(view),
        attempt_closed=attempt_closed,
        attempt_in_grace=view.is_closed and not attempt_closed,
        submissions_unavailable=not clock.healthy,
    ), status_code


@quiz_bp.route("/quiz/attempt/<int:attempt_id>/result")
@access_policy(ATTEMPT_OWNER)
def quiz_result(attempt_id):
    # One read transaction: access is decided on the same snapshot that is rendered.
    with quiz_service.quiz_connection() as conn, lifecycle.read_snapshot(conn):
        attempt = require_quiz_attempt_access(conn, attempt_id)

        assignment = quiz_service.quiz_fetch_assignment(conn, int(attempt["assignment_id"]))
        if not assignment:
            abort(404)
        lifecycle.require_served(assignment)

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
