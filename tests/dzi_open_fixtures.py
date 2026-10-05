"""Explicit exam ownership for synthetic questions in temporary test databases."""

def mark_dzi_question(conn, question_id):
    source = conn.execute("SELECT source_exam FROM questions WHERE id = ?", (question_id,)).fetchone()[0]
    exam = conn.execute("SELECT id FROM exams WHERE source_file = ?", (source,)).fetchone()
    if exam is None:
        exam_id = conn.execute("""
            INSERT INTO exams (subject, level, year, session, variant, source_file)
            VALUES ('informatika_it', 'DZI', 2090, ?, 1, ?)
        """, (source, source)).lastrowid
    else:
        exam_id = exam[0]
    conn.execute("""
        UPDATE questions SET exam_id = ?, subject = 'informatika_it', level = 'DZI', year = 2090
        WHERE id = ?
    """, (exam_id, question_id))
