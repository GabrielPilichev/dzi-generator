"""Ownership when quiz attempt creation loses a uniqueness race.

An attempt belongs to the account that started it. Two requests of one account
can both miss the lookup and both insert; the unique index on
``(assignment_id, user_id)`` keeps one row and the loser resumes it. The recovery
looks the attempt up by the account again, so it can only ever land on the
account's own attempt, never on a competitor's.
"""

import sqlite3
from unittest.mock import patch

from tests.auth_helpers import create_account, set_account_session
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
            "SELECT id, submitted_at FROM quiz_attempts WHERE assignment_id = ? AND user_id = ?"
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

    def _client(self, account):
        client = self.app.test_client()
        set_account_session(client, account["id"], account["session_key"])
        return client

    def setUp(self):
        db_path = self.app.config["DB_PATH"]
        # The same display name on purpose: the name must never decide anything.
        self.owner_account = create_account(db_path, display_name="Race Student")
        self.competitor_account = create_account(db_path, display_name="Race Student")
        self.owner = self._client(self.owner_account)
        self.competitor = self._client(self.competitor_account)

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

        response = self.owner.post(f"/quiz/{assignment_id}")
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

    def _start(self, client, assignment_id, *, force_conflict):
        if not force_conflict:
            return client.post(f"/quiz/{assignment_id}"), None

        wrappers = []

        def racing_quiz_db():
            wrapper = _FirstAttemptLookupMiss(web_app.quiz_db())
            wrappers.append(wrapper)
            return wrapper

        with patch.object(quiz_service, "quiz_db", side_effect=racing_quiz_db):
            response = client.post(f"/quiz/{assignment_id}")
        self.assertEqual(len(wrappers), 1)
        return response, wrappers[0]

    def _attempts(self, assignment_id):
        connection = web_app.quiz_db()
        try:
            return [
                (int(row["id"]), row["user_id"])
                for row in connection.execute(
                    "SELECT id, user_id FROM quiz_attempts WHERE assignment_id = ? ORDER BY id",
                    (assignment_id,),
                )
            ]
        finally:
            connection.close()

    def test_normal_existing_owner_can_resume(self):
        assignment_id, attempt_id = self._create_owned_attempt()

        response, _wrapper = self._start(self.owner, assignment_id, force_conflict=False)

        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith(f"/quiz/attempt/{attempt_id}"))
        self.assertEqual(self._attempts(assignment_id), [(attempt_id, self.owner_account["id"])])

    def test_conflict_recovery_resumes_current_owner(self):
        assignment_id, attempt_id = self._create_owned_attempt()

        response, wrapper = self._start(self.owner, assignment_id, force_conflict=True)

        self.assertEqual(wrapper.insert_conflicts, 1)
        self.assertEqual(wrapper.attempt_lookups, 2)
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith(f"/quiz/attempt/{attempt_id}"))
        self.assertEqual(self._attempts(assignment_id), [(attempt_id, self.owner_account["id"])])
        self.assertEqual(self.owner.get(f"/quiz/attempt/{attempt_id}").status_code, 200)

    def test_another_account_never_conflicts_with_or_lands_on_the_owners_attempt(self):
        assignment_id, attempt_id = self._create_owned_attempt()

        for force_conflict in (False, True):
            with self.subTest(force_conflict=force_conflict):
                competitor = self._client(create_account(self.app.config["DB_PATH"], display_name="Race Student"))
                response, wrapper = self._start(competitor, assignment_id, force_conflict=force_conflict)

                # Same assignment, same display name, different account: its own row.
                self.assertEqual(response.status_code, 302)
                own_attempt_id = int(response.headers["Location"].rsplit("/", 1)[-1])
                self.assertNotEqual(own_attempt_id, attempt_id)
                if wrapper is not None:
                    self.assertEqual(wrapper.insert_conflicts, 0)
                self.assertEqual(competitor.get(f"/quiz/attempt/{own_attempt_id}").status_code, 200)
                self.assertEqual(competitor.get(f"/quiz/attempt/{attempt_id}").status_code, 404)
                self.assertEqual(self.owner.get(f"/quiz/attempt/{own_attempt_id}").status_code, 404)
        self.assertEqual(self._attempts(assignment_id)[0], (attempt_id, self.owner_account["id"]))
        self.assertEqual(len({user_id for _attempt, user_id in self._attempts(assignment_id)}), 3)

    def test_conflict_recovery_does_not_grant_a_browser_marker(self):
        assignment_id, attempt_id = self._create_owned_attempt()

        self._start(self.owner, assignment_id, force_conflict=True)

        for client in (self.owner, self.competitor):
            with client.session_transaction() as session:
                self.assertFalse([key for key in session if key.startswith("quiz_attempt_owner:")])
        self.assertEqual(self.competitor.get(f"/quiz/attempt/{attempt_id}").status_code, 404)

    def test_submitted_attempt_uses_same_owner_rule_in_both_paths(self):
        for force_conflict in (False, True):
            with self.subTest(force_conflict=force_conflict):
                assignment_id, attempt_id = self._create_owned_attempt(submitted=True)

                owner_response, _ = self._start(self.owner, assignment_id, force_conflict=force_conflict)

                self.assertEqual(owner_response.status_code, 302)
                self.assertTrue(
                    owner_response.headers["Location"].endswith(f"/quiz/attempt/{attempt_id}/result")
                )
                self.assertEqual(self.competitor.get(f"/quiz/attempt/{attempt_id}/result").status_code, 404)

    def test_an_integrity_error_that_is_not_the_accounts_own_row_is_not_swallowed(self):
        # The recovery only resumes the account's own attempt. Any other constraint
        # failure (here: an account id that does not exist) stays an error.
        connection = web_app.quiz_db()
        try:
            cursor = connection.execute(
                "INSERT INTO quiz_assignments (section_id, title_bg, question_count) VALUES (?, ?, 1)",
                (self.section_id, self.section_title),
            )
            connection.commit()
            assignment = quiz_service.quiz_fetch_assignment(connection, int(cursor.lastrowid))
            with self.assertRaises(sqlite3.IntegrityError):
                quiz_service.quiz_start_or_resume_attempt(
                    connection, assignment, user_id=987654321, display_name="Никой")
        finally:
            connection.close()
        self.assertEqual(self._attempts(int(assignment["id"])), [])
