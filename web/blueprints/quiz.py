from __future__ import annotations

import sqlite3

from flask import Blueprint, abort, redirect, render_template, request, session, url_for


quiz_bp = Blueprint("quiz", __name__)


def _app_module():
    from web import app as web_app

    return web_app


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
def quiz_start(assignment_id):
    quiz = _app_module()
    conn = quiz.quiz_db()
    assignment = quiz.quiz_fetch_assignment(conn, assignment_id)
    if not assignment:
        conn.close()
        abort(404)

    if request.method == "POST":
        student_name = " ".join((request.form.get("student_name") or "").strip().split())
        name_error = quiz.quiz_student_name_error(student_name)
        if name_error:
            mixed_status = quiz.quiz_assignment_mixed_status(assignment["question_plan_json"])
            conn.close()
            return render_template(
                "quiz_start.html",
                assignment=assignment,
                mixed_status=mixed_status,
                name_error=name_error,
                student_name_value=student_name,
            ), 400

        existing = conn.execute("""
            SELECT *
            FROM quiz_attempts
            WHERE assignment_id = ?
              AND student_name = ?
        """, (assignment_id, student_name)).fetchone()

        if existing:
            attempt_id = int(existing["id"])
            conn.close()
            require_quiz_attempt_access(attempt_id)
            if existing["submitted_at"]:
                return redirect(url_for("quiz_result", attempt_id=attempt_id))
            return redirect(url_for("quiz_attempt", attempt_id=attempt_id))

        assignment_question_plan = quiz.quiz_parse_assignment_question_plan(assignment["question_plan_json"])
        if (assignment["question_plan_json"] or "").strip() and assignment_question_plan is None:
            conn.close()
            abort(400)

        if assignment_question_plan:
            seed = quiz.quiz_seed(assignment_id, student_name)
            question_ids_json = quiz._quiz_json.dumps(assignment_question_plan, ensure_ascii=False)
            score_total = len([
                qid
                for qid in assignment_question_plan["question_ids"]
                if qid not in set(assignment_question_plan["open_question_ids"])
            ])
        else:
            seed, question_ids = quiz.quiz_pick_questions(conn, assignment, student_name)
            question_ids_json = quiz._quiz_json.dumps(question_ids)
            score_total = len(question_ids)

        try:
            cur = conn.execute("""
                INSERT INTO quiz_attempts (assignment_id, student_name, seed, question_ids_json, score_total)
                VALUES (?, ?, ?, ?, ?)
            """, (
                assignment_id,
                student_name,
                seed,
                question_ids_json,
                score_total,
            ))
        except sqlite3.IntegrityError:
            existing = conn.execute("""
                SELECT *
                FROM quiz_attempts
                WHERE assignment_id = ?
                  AND student_name = ?
            """, (assignment_id, student_name)).fetchone()
            conn.close()
            if existing:
                attempt_id = int(existing["id"])
                mark_quiz_attempt_owner(attempt_id)
                if existing["submitted_at"]:
                    return redirect(url_for("quiz_result", attempt_id=attempt_id))
                return redirect(url_for("quiz_attempt", attempt_id=attempt_id))
            raise
        attempt_id = cur.lastrowid
        conn.commit()
        conn.close()

        mark_quiz_attempt_owner(attempt_id)
        return redirect(url_for("quiz_attempt", attempt_id=attempt_id))

    mixed_status = quiz.quiz_assignment_mixed_status(assignment["question_plan_json"])
    conn.close()
    return render_template(
        "quiz_start.html",
        assignment=assignment,
        mixed_status=mixed_status,
        name_error=None,
        student_name_value="",
    )


@quiz_bp.route("/quiz/attempt/<int:attempt_id>", methods=["GET", "POST"])
def quiz_attempt(attempt_id):
    require_quiz_attempt_access(attempt_id)
    quiz = _app_module()
    conn = quiz.quiz_db()
    attempt = conn.execute("SELECT * FROM quiz_attempts WHERE id = ?", (attempt_id,)).fetchone()
    if not attempt:
        conn.close()
        abort(404)

    assignment = quiz.quiz_fetch_assignment(conn, int(attempt["assignment_id"]))
    if not assignment:
        conn.close()
        abort(404)

    if attempt["submitted_at"]:
        conn.close()
        return redirect(url_for("quiz_result", attempt_id=attempt_id))

    attempt_question_plan = quiz.quiz_parse_attempt_question_plan(attempt["question_ids_json"])
    open_question_ids = (
        attempt_question_plan["open_question_ids"]
        if attempt_question_plan["mixed_open_enabled"]
        else []
    )
    question_ids, skipped_count = quiz.filter_renderable_attempt_question_ids(
        conn,
        attempt_question_plan["question_ids"],
        open_question_ids=open_question_ids,
    )
    seed = attempt["seed"]

    attempt_mixed_status = {
        "is_mixed": bool(attempt_question_plan["mixed_open_enabled"]),
        "open_count": (
            len(attempt_question_plan["open_question_ids"])
            if attempt_question_plan["mixed_open_enabled"]
            else 0
        ),
        "combined_score": bool(
            attempt_question_plan["include_open_answers_in_final_score"]
        ),
    }

    if request.method == "POST":
        if not question_ids:
            conn.close()
            return render_template(
                "quiz_attempt.html",
                assignment=assignment,
                attempt=attempt,
                questions=[],
                remaining_seconds=None,
                timer_expired=False,
                stale_attempt_message=quiz.STALE_ATTEMPT_MESSAGE,
                submit_error=None,
                skipped_question_count=skipped_count,
                mixed_status=attempt_mixed_status,
            )

        question_types = quiz.quiz_question_types_by_id(conn, question_ids)
        mc_question_ids = [
            qid
            for qid in question_ids
            if question_types.get(int(qid)) == "multiple_choice"
        ]

        remaining = quiz.quiz_remaining_seconds(assignment, attempt)
        server_timer_expired = remaining is not None and remaining <= 0
        is_timer_submit = request.form.get("submission_reason") == "timer_expired"
        if not (
            is_timer_submit
            and server_timer_expired
        ) and not quiz.quiz_has_any_submitted_answer(
            request.form,
            mc_question_ids=mc_question_ids,
            question_ids=question_ids,
            open_question_ids=open_question_ids,
        ):
            questions = quiz.quiz_load_questions(conn, question_ids, seed, include_correct=False)
            if not questions:
                remaining = None
            conn.close()
            return render_template(
                "quiz_attempt.html",
                assignment=assignment,
                attempt=attempt,
                questions=questions,
                remaining_seconds=remaining,
                timer_expired=server_timer_expired,
                stale_attempt_message=quiz.STALE_ATTEMPT_MESSAGE if not questions else None,
                submit_error=quiz.QUIZ_EMPTY_SUBMISSION_MESSAGE,
                skipped_question_count=skipped_count,
                mixed_status=attempt_mixed_status,
            ), 400

        correct_by_qid = quiz.quiz_correct_mc_letters_by_id(conn, mc_question_ids)

        score = 0
        # Stale invalid IDs and planned open fields are excluded from MC scoring.
        total = len(mc_question_ids)

        for qid in mc_question_ids:
            chosen = request.form.get(f"q_{qid}")
            correct_letter = correct_by_qid.get(int(qid))
            is_correct = 1 if chosen and correct_letter and chosen == correct_letter else 0
            score += is_correct

            conn.execute("""
                INSERT INTO quiz_answers (attempt_id, question_id, chosen_letter, is_correct)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(attempt_id, question_id)
                DO UPDATE SET chosen_letter = excluded.chosen_letter,
                              is_correct = excluded.is_correct
            """, (attempt_id, qid, chosen, is_correct))

        quiz.quiz_record_planned_open_text_answers(
            conn,
            attempt_id=attempt_id,
            question_ids=question_ids,
            open_question_ids=open_question_ids,
            form=request.form,
        )

        conn.execute("""
            UPDATE quiz_attempts
            SET submitted_at = CURRENT_TIMESTAMP,
                score_correct = ?,
                score_total = ?
            WHERE id = ?
        """, (score, total, attempt_id))

        conn.commit()
        conn.close()

        quiz.quiz_write_attempt_note(attempt_id)
        quiz.quiz_write_assignment_note(int(assignment["id"]), request.host_url)

        return redirect(url_for("quiz_result", attempt_id=attempt_id))

    questions = quiz.quiz_load_questions(conn, question_ids, seed, include_correct=False)
    remaining = quiz.quiz_remaining_seconds(assignment, attempt)
    if not questions:
        remaining = None
    timer_expired = remaining is not None and remaining <= 0
    conn.close()

    return render_template(
        "quiz_attempt.html",
        assignment=assignment,
        attempt=attempt,
        questions=questions,
        remaining_seconds=remaining,
        timer_expired=timer_expired,
        stale_attempt_message=quiz.STALE_ATTEMPT_MESSAGE if not questions else None,
        submit_error=None,
        skipped_question_count=skipped_count,
        mixed_status=attempt_mixed_status,
    )


@quiz_bp.route("/quiz/attempt/<int:attempt_id>/result")
def quiz_result(attempt_id):
    require_quiz_attempt_access(attempt_id)
    quiz = _app_module()
    conn = quiz.quiz_db()
    attempt = conn.execute("SELECT * FROM quiz_attempts WHERE id = ?", (attempt_id,)).fetchone()
    if not attempt:
        conn.close()
        abort(404)

    assignment = quiz.quiz_fetch_assignment(conn, int(attempt["assignment_id"]))
    if not assignment:
        conn.close()
        abort(404)

    if not attempt["submitted_at"]:
        conn.close()
        return redirect(url_for("quiz_attempt", attempt_id=attempt_id))

    attempt_question_plan = quiz.quiz_parse_attempt_question_plan(attempt["question_ids_json"])
    stored_qids = attempt_question_plan["question_ids"]
    # MC questions remain the only scored result items; open answers render as read-only review.
    qids, _skipped_count = quiz.filter_renderable_attempt_question_ids(conn, stored_qids)
    original_question_count = len(stored_qids or [])
    renderable_question_count = len(qids)
    skipped_question_count = max(0, original_question_count - renderable_question_count)
    questions = quiz.quiz_load_result_questions(conn, attempt, qids, attempt["seed"])
    difficulty_breakdown = quiz.quiz_difficulty_breakdown_from_questions(questions)
    open_text_answers = quiz.fetch_quiz_text_answers_for_attempt(conn, attempt_id)
    open_text_subtotal = (
        quiz.quiz_text_answer_informational_subtotal(open_text_answers)
        if open_text_answers
        else None
    )
    combined_score = quiz.quiz_combined_score_summary(
        attempt,
        open_text_subtotal,
        enabled=bool(attempt_question_plan["include_open_answers_in_final_score"]),
    )
    time_taken = quiz.quiz_attempt_duration_display(attempt)
    success_rate = quiz.quiz_attempt_success_rate_display(attempt)
    conn.close()

    return render_template(
        "quiz_result.html",
        assignment=assignment,
        attempt=attempt,
        questions=questions,
        time_taken=time_taken,
        success_rate=success_rate,
        difficulty_breakdown=difficulty_breakdown,
        open_text_answers=open_text_answers,
        open_text_subtotal=open_text_subtotal,
        combined_score=combined_score,
        stale_attempt_message=quiz.STALE_ATTEMPT_MESSAGE if not questions else None,
        original_question_count=original_question_count,
        renderable_question_count=renderable_question_count,
        skipped_question_count=skipped_question_count,
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
