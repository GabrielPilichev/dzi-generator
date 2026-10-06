"""Session ownership when quiz attempt creation loses a uniqueness race."""

import sqlite3
from unittest.mock import patch

from tests.support import IsolatedAppTestCase, web_app
from web.services import quiz as quiz_service


class _FirstAttemptLookupMiss:
    """Hide the initial lookup so SQLite itself rejects the duplicate insert."""

    def __init__(self, connection):
        self.connection = connection
        self.attempt_lookups = 0
        self.insert_conflicts = 0

    def execute(self, sql, parameters=()):
        normalized_sql = " ".join(sql.split())
        if normalized_sql.startswith(
            "SELECT * FROM quiz_attempts WHERE assignment_id = ? AND student_name = ?"
        ):
            self.attempt_lookups += 1
            if self.attempt_lookups == 1:
                return self.connection.execute("SELECT * FROM quiz_attempts WHERE 1 = 0")
        try:
            return self.connection.execute(sql, parameters)
        except sqlite3.IntegrityError:
            if normalized_sql.startswith("INSERT INTO quiz_attempts"):
                self.insert_conflicts += 1
            raise

    def __getattr__(self, name):
        return getattr(self.connection, name)


class QuizAttemptRaceOwnershipTest(IsolatedAppTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.app = web_app.app
        connection = web_app.quiz_db()
        try:
            for section in connection.execute(
                "SELECT id, title_bg FROM curriculum_sections ORDER BY id"
            ):
                if web_app.quiz_section_question_ids(connection, int(section["id"])):
                    cls.section_id = int(section["id"])
                    cls.section_title = section["title_bg"]
                    break
            else:
                raise AssertionError("No section with eligible quiz questions found")
        finally:
            connection.close()

    def setUp(self):
        self.owner = self.app.test_client()
        self.competitor = self.app.test_client()

    def _create_owned_attempt(self, *, submitted=False):
        connection = web_app.quiz_db()
        try:
            cursor = connection.execute(
                """
                INSERT INTO quiz_assignments (section_id, title_bg, question_count)
                VALUES (?, ?, 1)
                """,
                (self.section_id, self.section_title),
            )
            assignment_id = int(cursor.lastrowid)
            connection.commit()
        finally:
            connection.close()

        response = self.owner.post(
            f"/quiz/{assignment_id}", data={"student_name": "Race Student"}
        )
        self.assertEqual(response.status_code, 302)
        attempt_id = int(response.headers["Location"].rsplit("/", 1)[-1])

        if submitted:
            connection = web_app.quiz_db()
            try:
                connection.execute(
                    """
                    UPDATE quiz_attempts
                    SET submitted_at = CURRENT_TIMESTAMP, score_correct = 0
                    WHERE id = ?
                    """,
                    (attempt_id,),
                )
                connection.commit()
            finally:
                connection.close()

        return assignment_id, attempt_id

    def _post_after_forced_conflict(self, client, assignment_id):
        connection = web_app.quiz_db()
        racing_connection = _FirstAttemptLookupMiss(connection)
        try:
            with patch.object(quiz_service, "quiz_db", return_value=racing_connection) as mocked:
                response = client.post(
                    f"/quiz/{assignment_id}", data={"student_name": "Race Student"}
                )
                mocked.assert_called_once_with()
        finally:
            connection.close()

        self.assertEqual(racing_connection.attempt_lookups, 2)
        self.assertEqual(racing_connection.insert_conflicts, 1)
        return response

    def _assert_no_competitor_access(self, attempt_id):
        key = web_app.quiz_attempt_owner_session_key(attempt_id)
        with self.competitor.session_transaction() as session:
            self.assertNotIn(key, session)
        self.assertEqual(
            self.competitor.get(f"/quiz/attempt/{attempt_id}").status_code, 404
        )
        self.assertEqual(
            self.competitor.get(f"/quiz/attempt/{attempt_id}/result").status_code,
            404,
        )

    def test_normal_existing_owner_can_resume(self):
        assignment_id, attempt_id = self._create_owned_attempt()

        response = self.owner.post(
            f"/quiz/{assignment_id}", data={"student_name": "Race Student"}
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], f"/quiz/attempt/{attempt_id}")
        self.assertEqual(self.owner.get(response.headers["Location"]).status_code, 200)

    def test_normal_existing_competitor_is_rejected(self):
        assignment_id, attempt_id = self._create_owned_attempt()

        response = self.competitor.post(
            f"/quiz/{assignment_id}", data={"student_name": "Race Student"}
        )

        self.assertEqual(response.status_code, 404)
        self._assert_no_competitor_access(attempt_id)

    def test_conflict_recovery_does_not_grant_competitor_ownership(self):
        assignment_id, attempt_id = self._create_owned_attempt()

        response = self._post_after_forced_conflict(self.competitor, assignment_id)

        self.assertEqual(response.status_code, 404)
        self._assert_no_competitor_access(attempt_id)
        self.assertEqual(
            self.owner.get(f"/quiz/attempt/{attempt_id}").status_code, 200
        )

    def test_conflict_recovery_resumes_current_owner(self):
        assignment_id, attempt_id = self._create_owned_attempt()

        response = self._post_after_forced_conflict(self.owner, assignment_id)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], f"/quiz/attempt/{attempt_id}")
        self.assertEqual(self.owner.get(response.headers["Location"]).status_code, 200)

    def test_submitted_attempt_uses_same_owner_rule_in_both_paths(self):
        for forced_conflict in (False, True):
            with self.subTest(forced_conflict=forced_conflict):
                assignment_id, attempt_id = self._create_owned_attempt(submitted=True)
                result_path = f"/quiz/attempt/{attempt_id}/result"

                def post(client):
                    if forced_conflict:
                        return self._post_after_forced_conflict(client, assignment_id)
                    return client.post(
                        f"/quiz/{assignment_id}",
                        data={"student_name": "Race Student"},
                    )

                owner_response = post(self.owner)
                self.assertEqual(owner_response.status_code, 302)
                self.assertEqual(owner_response.headers["Location"], result_path)
                self.assertEqual(self.owner.get(result_path).status_code, 200)

                competitor_response = post(self.competitor)
                self.assertEqual(competitor_response.status_code, 404)
                self._assert_no_competitor_access(attempt_id)
