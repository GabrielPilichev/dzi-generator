"""Student practice routes (C2): start, attempt, submit, result and repeat.

Registered by ``web.app`` only while ``web.services.practice.ROUTES_ACTIVATION`` is armed,
which it is not in this build: the ordinary application has no ``/practice`` route.
Governed by docs/STUDENT_PRACTICE_DESIGN.md (sections 3 to 6) and the contract in
docs/AUTH_ROUTE_CONTRACT.md ("Student practice").

Every view declares ``signed_in``; every view that accepts an unsafe method also declares
``same_origin``, which the application's own before-request hook enforces before any view
runs. The only inputs are the ids in the path and, on submit, the chosen option keys by
question id. The owner is always the active principal: no role, account id, score,
timestamp, label, plan or seed is ever read from a request. Another account, a teacher or an
admin gets the generic 404, exactly like a missing session, and a session is never
distinguishable by who owns it.

Writes go through the practice services only (start and submit); a ``GET`` or ``HEAD`` reads one
snapshot and writes nothing. A stale, abandoned, unavailable or malformed state is answered
with a short Bulgarian page that names no stored state; any other inconsistent stored state is
a server error (the generic 500 page). Both are logged by endpoint and exception class only: never
an id, a title or any part of a stored plan.

The two start URLs also answer a signed-in ``GET`` or ``HEAD`` with a read-only confirmation of the
same section or topic scope (``practice_start.html``). It exists because a signed-out ``POST`` is sent
to ``/login?next=<path>`` and the sign-in returns with a ``GET``: the page validates the scope with the
service's read-only helpers, writes nothing, never starts, resumes or abandons anything and never
replays the original request. Its only action is an explicit same-origin ``POST`` to the very same URL,
which is the unchanged start. Whether a new session can be created (the minimum) or an open one
resumed is decided by that ``POST`` alone.
"""

from __future__ import annotations

import re

from flask import Blueprint, abort, current_app, redirect, render_template, request, url_for

from web.access import SIGNED_IN, access_policy, current_principal
from web.request_integrity import SAME_ORIGIN, integrity_policy
from web.services import practice as practice_service
from web.services import quiz as quiz_service

practice_bp = Blueprint("practice", __name__)

_MAX_ID = 2 ** 63 - 1
# ``q_<question id>``: the only form fields read. The value is the stored option key.
_CHOICE_FIELD = re.compile(r"q_([1-9][0-9]{0,18})")

MESSAGE_SCOPE_UNAVAILABLE = "Това упражнение не е налично."
MESSAGE_NOT_ENOUGH_QUESTIONS = (
    "Все още няма достатъчно въпроси за упражнение в тази област. Опитай отново по-късно."
)
MESSAGE_UNAVAILABLE = "Това упражнение не може да бъде показано в момента."
MESSAGE_STALE = (
    "Въпросите от това упражнение вече не могат да бъдат показани. Можеш да започнеш ново упражнение."
)
MESSAGE_ABANDONED = (
    "Този опит е заменен и не може да бъде предаден. Продължи с упражнението от бутона по-долу."
)
MESSAGE_EMPTY_SUBMISSION = "Избери поне един отговор, преди да предадеш упражнението."


def _is_id(value: int) -> bool:
    return 0 < value <= _MAX_ID


def _start_url(section_id: int, topic_id: int | None) -> str:
    if topic_id is None:
        return url_for("practice.start_section", section_id=section_id)
    return url_for("practice.start_topic", section_id=section_id, topic_id=topic_id)


def _unavailable(message: str, status: int, *, restart_url: str | None = None, restart_label: str | None = None):
    return render_template(
        "practice_unavailable.html", message=message, restart_url=restart_url, restart_label=restart_label
    ), status


def _state_error(error: practice_service.PracticeStateError):
    """An inconsistent stored state: the generic 500, logged by class only (no ids, no content)."""
    current_app.logger.error(
        "Practice state refused: endpoint=%s error=%s", request.endpoint, type(error).__name__
    )
    abort(500)


def _plan_error(error: practice_service.PracticePlanError):
    """A malformed stored plan: the safe 409 page. The log names the endpoint and the exception class only.

    The exception's own message carries a session id and a reason; neither is logged, nor is any
    label or plan content. The row stays open and nothing was written (the service rolled back).
    """
    current_app.logger.error(
        "Practice plan refused: endpoint=%s error=%s", request.endpoint, type(error).__name__
    )
    return _unavailable(MESSAGE_UNAVAILABLE, 409)


def _choices_from_form(form) -> dict[int, str]:
    """Chosen option keys by question id. A field sent twice is ambiguous and counts as unanswered."""
    choices: dict[int, str] = {}
    for name in form.keys():
        match = _CHOICE_FIELD.fullmatch(name)
        if match is None:
            continue
        values = form.getlist(name)
        if len(values) == 1:
            choices[int(match.group(1))] = values[0]
    return choices


# ---------------------------------------------------------------------------
# Start
# ---------------------------------------------------------------------------

def _start(section_id: int, topic_id: int | None):
    # The scope is the path; the owner is the principal. Nothing else is read.
    if not _is_id(section_id) or (topic_id is not None and not _is_id(topic_id)):
        return _unavailable(MESSAGE_SCOPE_UNAVAILABLE, 404)
    try:
        with quiz_service.quiz_connection() as conn:
            start = practice_service.start_or_resume_practice(conn, current_principal(), section_id, topic_id)
    except practice_service.PracticeAccessError:
        return render_template("403.html"), 403
    except practice_service.PracticePlanError as error:
        # A stored plan the service refuses to read: nothing was written, nothing is disclosed.
        return _plan_error(error)
    except practice_service.PracticeStateError as error:
        _state_error(error)

    if start.outcome in (practice_service.OUTCOME_CREATED, practice_service.OUTCOME_RESUMED):
        return redirect(url_for("practice.attempt", session_id=start.session_id), code=303)
    if start.outcome == practice_service.OUTCOME_EMPTY_POOL:
        return _unavailable(MESSAGE_NOT_ENOUGH_QUESTIONS, 409)
    if start.outcome == practice_service.OUTCOME_ACCOUNT_UNAVAILABLE:
        return render_template("403.html"), 403
    return _unavailable(MESSAGE_SCOPE_UNAVAILABLE, 404)  # scope_unavailable


def _confirm(section_id: int, topic_id: int | None):
    """``GET``/``HEAD`` of a start URL: read-only confirmation of the scope; nothing is persisted.

    No start, resume, abandon or session lookup happens here, so an open, stale or thin state of the
    account cannot change and is not revealed. The connection refuses writes outright, so a later
    edit cannot make this read write by accident.
    """
    if not _is_id(section_id) or (topic_id is not None and not _is_id(topic_id)):
        return _unavailable(MESSAGE_SCOPE_UNAVAILABLE, 404)
    with quiz_service.quiz_connection() as conn:
        conn.execute("PRAGMA query_only = ON")
        scope = practice_service.read_practice_scope(conn, section_id, topic_id)
    if scope is None:
        return _unavailable(MESSAGE_SCOPE_UNAVAILABLE, 404)
    # The action is rebuilt from the validated scope, which equals the requested path.
    return render_template(
        "practice_start.html", scope=scope, start_url=_start_url(scope.section_id, scope.topic_id)
    )


def _start_or_confirm(section_id: int, topic_id: int | None):
    if request.method == "POST":
        return _start(section_id, topic_id)
    return _confirm(section_id, topic_id)


@practice_bp.route("/practice/section/<int:section_id>/start", methods=["GET", "POST"])
@access_policy(SIGNED_IN)
@integrity_policy(SAME_ORIGIN)
def start_section(section_id):
    return _start_or_confirm(section_id, None)


@practice_bp.route("/practice/section/<int:section_id>/topic/<int:topic_id>/start", methods=["GET", "POST"])
@access_policy(SIGNED_IN)
@integrity_policy(SAME_ORIGIN)
def start_topic(section_id, topic_id):
    return _start_or_confirm(section_id, topic_id)


# ---------------------------------------------------------------------------
# Attempt and submit
# ---------------------------------------------------------------------------

def _attempt_response(view: practice_service.PracticeAttempt, *, submit_error: str | None = None):
    """What an attempt view looks like to its owner."""
    outcome = view.outcome
    if outcome == practice_service.OUTCOME_NOT_FOUND:
        abort(404)
    if outcome == practice_service.OUTCOME_ACCOUNT_UNAVAILABLE:
        return render_template("403.html"), 403
    if outcome == practice_service.OUTCOME_ALREADY_SUBMITTED:
        return redirect(url_for("practice.result", session_id=view.session_id))
    if outcome == practice_service.OUTCOME_OPEN:
        return render_template(
            "practice_attempt.html",
            session_id=view.session_id,
            questions=view.questions,
            skipped_question_count=view.skipped_question_count,
            submit_error=submit_error,
        ), (400 if submit_error else 200)

    restart_url = _start_url(view.section_id, view.topic_id)
    if outcome == practice_service.OUTCOME_ABANDONED:
        return _unavailable(MESSAGE_ABANDONED, 409, restart_url=restart_url, restart_label="Към упражнението")
    if outcome == practice_service.OUTCOME_STALE:
        return _unavailable(MESSAGE_STALE, 409, restart_url=restart_url, restart_label="Ново упражнение")
    return _unavailable(MESSAGE_UNAVAILABLE, 409)  # scope_unavailable


@practice_bp.route("/practice/session/<int:session_id>", methods=["GET", "POST"])
@access_policy(SIGNED_IN)
@integrity_policy(SAME_ORIGIN)
def attempt(session_id):
    if not _is_id(session_id):
        abort(404)
    principal = current_principal()
    try:
        with quiz_service.quiz_connection() as conn:
            if request.method != "POST":
                return _attempt_response(practice_service.load_practice_attempt(conn, principal, session_id))

            submission = practice_service.submit_practice(
                conn, principal, session_id, _choices_from_form(request.form)
            )
            if submission.outcome in (practice_service.OUTCOME_SUBMITTED, practice_service.OUTCOME_ALREADY_SUBMITTED):
                return redirect(url_for("practice.result", session_id=session_id), code=303)
            if submission.outcome == practice_service.OUTCOME_NOT_FOUND:
                abort(404)
            if submission.outcome == practice_service.OUTCOME_ACCOUNT_UNAVAILABLE:
                return render_template("403.html"), 403
            # Nothing was written (an empty answer sheet, an abandoned or stale session, a scope
            # that went away). Show the owner the page the same snapshot rules give now.
            view = practice_service.load_practice_attempt(conn, principal, session_id)
            error = MESSAGE_EMPTY_SUBMISSION if submission.outcome == practice_service.OUTCOME_EMPTY else None
            return _attempt_response(view, submit_error=error)
    except practice_service.PracticeAccessError:
        return render_template("403.html"), 403
    except practice_service.PracticePlanError as error:
        return _plan_error(error)
    except practice_service.PracticeStateError as error:
        _state_error(error)


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------

@practice_bp.route("/practice/session/<int:session_id>/result")
@access_policy(SIGNED_IN)
def result(session_id):
    if not _is_id(session_id):
        abort(404)
    try:
        with quiz_service.quiz_connection() as conn:
            loaded = practice_service.load_practice_result(conn, current_principal(), session_id)
    except practice_service.PracticeAccessError:
        return render_template("403.html"), 403
    except practice_service.PracticePlanError as error:
        return _plan_error(error)
    except practice_service.PracticeStateError as error:
        _state_error(error)

    if loaded.outcome == practice_service.OUTCOME_ACCOUNT_UNAVAILABLE:
        return render_template("403.html"), 403
    if not loaded.loaded:
        abort(404)  # missing, foreign, still open or abandoned: one indistinguishable answer
    return render_template(
        "practice_result.html",
        result=loaded,
        success_rate=(200 * loaded.score_correct + loaded.score_total) // (2 * loaded.score_total),  # half up
        new_attempt_url=_start_url(loaded.section_id, loaded.topic_id),
        **loaded.review_context(),
    )
