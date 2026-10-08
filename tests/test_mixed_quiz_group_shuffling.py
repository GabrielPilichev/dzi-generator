"""Mixed quizzes shuffle their questions within the closed and the open group.

A mixed assignment stores one plan (``quiz_assignments.question_plan_json``):
closed questions and open questions. When an account starts its attempt, each
group is shuffled on its own and the result is persisted in the attempt's
``question_ids_json``, the same field every attempt page already reads its
order from. Nothing reshuffles afterwards, and the groups never mix.

Randomness is deterministic here: the helper takes its ``rng`` as an argument,
and the route tests replace the attempt's rng with one that reverses, so a
shuffle is always visible and no test depends on two shuffles differing.

Everything runs on the class's temporary database and vault.
"""

import json
import random
import re
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

from tests import runtime_fixtures
from tests.support import ROOT, web_app, student_entry_path
from tests.test_account_login import SAME_ORIGIN
from tests.test_account_owned_attempts import AccountAttemptTestCase
from web.assignment_tokens import new_public_token
from web.services import quiz


class ReversingRng:
    """A stand-in for ``random.Random`` whose shuffle is a visible, fixed permutation."""

    def shuffle(self, items):
        items.reverse()


def reversing_order():
    return mock.patch.object(quiz, "_quiz_attempt_order_rng", lambda seed: ReversingRng())


def no_shuffle_allowed():
    def fail(seed):
        raise AssertionError("the question order was shuffled again")

    return mock.patch.object(quiz, "_quiz_attempt_order_rng", fail)


def mixed_plan(closed_ids, open_ids, *, combined=False):
    return {
        "mixed_open_enabled": True,
        "question_ids": list(closed_ids) + list(open_ids),
        "open_question_ids": list(open_ids),
        "include_open_answers_in_final_score": combined,
    }


class ShufflePlanWithinGroupsTest(unittest.TestCase):
    CLOSED = [11, 12, 13, 14]
    OPEN = [21, 22, 23]

    def test_each_group_is_shuffled_on_its_own(self):
        plan = mixed_plan(self.CLOSED, self.OPEN, combined=True)

        shuffled = quiz.quiz_shuffle_plan_within_groups(plan, ReversingRng())

        self.assertEqual(shuffled["question_ids"], [14, 13, 12, 11, 23, 22, 21])
        self.assertEqual(shuffled["open_question_ids"], [23, 22, 21])
        self.assertTrue(shuffled["mixed_open_enabled"])
        self.assertTrue(shuffled["include_open_answers_in_final_score"])

    def test_the_assignment_plan_is_not_modified(self):
        plan = mixed_plan(self.CLOSED, self.OPEN)
        before = json.dumps(plan)

        quiz.quiz_shuffle_plan_within_groups(plan, ReversingRng())

        self.assertEqual(json.dumps(plan), before)

    def test_group_boundaries_and_membership_hold_for_every_seed(self):
        plan = mixed_plan(self.CLOSED, self.OPEN)
        for seed in range(200):
            with self.subTest(seed=seed):
                shuffled = quiz.quiz_shuffle_plan_within_groups(plan, random.Random(seed))
                self.assertCountEqual(shuffled["question_ids"][:4], self.CLOSED)
                self.assertCountEqual(shuffled["question_ids"][4:], self.OPEN)
                self.assertEqual(shuffled["open_question_ids"], shuffled["question_ids"][4:])

    def test_same_seed_gives_the_same_order(self):
        plan = mixed_plan(self.CLOSED, self.OPEN)
        self.assertEqual(
            quiz.quiz_shuffle_plan_within_groups(plan, random.Random("seed")),
            quiz.quiz_shuffle_plan_within_groups(plan, random.Random("seed")),
        )

    def test_an_interleaved_plan_keeps_each_position_in_its_group(self):
        plan = {
            "mixed_open_enabled": True,
            "question_ids": [11, 21, 12, 22, 13],
            "open_question_ids": [21, 22],
            "include_open_answers_in_final_score": False,
        }

        shuffled = quiz.quiz_shuffle_plan_within_groups(plan, ReversingRng())

        self.assertEqual(shuffled["question_ids"], [13, 22, 12, 21, 11])
        self.assertEqual(shuffled["open_question_ids"], [22, 21])

    def test_single_question_groups_stay_put(self):
        plan = mixed_plan([11], [21])
        self.assertEqual(quiz.quiz_shuffle_plan_within_groups(plan, ReversingRng()), plan)

    def test_attempt_rng_depends_only_on_the_attempt_seed(self):
        first = quiz._quiz_attempt_order_rng("seed-a")
        again = quiz._quiz_attempt_order_rng("seed-a")
        self.assertEqual(first.random(), again.random())


class MixedShuffleTestCase(AccountAttemptTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        with closing(web_app.quiz_db()) as conn:
            cls.closed_ids = quiz.quiz_section_question_ids(conn, cls.section_id)[:3]
            cls.open_ids = [cls._insert_open_question(conn, number) for number in (1, 2, 3)]
            cls.correct_letters = quiz.quiz_correct_mc_letters_by_id(conn, cls.closed_ids)
            conn.commit()

    @staticmethod
    def _insert_open_question(conn, number):
        cursor = conn.execute("""
            INSERT INTO questions (
                source_exam, source_number, question_type, topic, difficulty,
                points, prompt, has_image, is_ai_generated, quality_score
            )
            VALUES ('temp-group-shuffle-open', ?, 'fill_in', 'test', 'medium', 1, ?, 0, 0, NULL)
        """, (number, f"Попълнете липсващата стойност {number}."))
        question_id = int(cursor.lastrowid)
        conn.execute("""
            INSERT INTO fill_in_subquestions (
                question_id, subquestion_number, correct_answer, answer_alternatives
            )
            VALUES (?, 1, ?, NULL)
        """, (question_id, f"отговор{number}"))
        return question_id

    def mixed_assignment(self, *, plan=None, owner=None):
        plan = plan or mixed_plan(self.closed_ids, self.open_ids)
        with closing(web_app.quiz_db()) as conn:
            assignment_id = runtime_fixtures.add_assignment(
                conn, section_id=self.section_id, title="Смесен тест",
                question_count=len(plan["question_ids"]), question_plan=plan,
                public_token=new_public_token())
            if owner is not None:
                conn.execute("UPDATE quiz_assignments SET owner_user_id = ? WHERE id = ?",
                             (owner["id"], assignment_id))
            conn.commit()
        return assignment_id

    def stored_plan(self, attempt_id):
        return json.loads(self.attempt(attempt_id)["question_ids_json"])

    def assignment_plan_json(self, assignment_id):
        return self.query("SELECT question_plan_json FROM quiz_assignments WHERE id = ?",
                          (assignment_id,))[0][0]

    def rendered_question_ids(self, client, attempt_id):
        response = client.get(f"/quiz/attempt/{attempt_id}")
        self.assertEqual(response.status_code, 200)
        return [int(qid) for qid in re.findall(r'data-question-id="(\d+)"', response.get_data(as_text=True))]

    def started(self, assignment_id, account=None):
        """(client, attempt id) of a fresh account whose order was reversed within each group."""
        client = self.client(account or self.account())
        with reversing_order():
            attempt_id = self.start(client, assignment_id)
        return client, attempt_id

    @property
    def reversed_closed(self):
        return list(reversed(self.closed_ids))

    @property
    def reversed_open(self):
        return list(reversed(self.open_ids))


class NewAttemptShuffleTest(MixedShuffleTestCase):
    def test_new_attempt_persists_an_order_shuffled_within_each_group(self):
        assignment_id = self.mixed_assignment()
        assignment_plan_before = self.assignment_plan_json(assignment_id)

        _client, attempt_id = self.started(assignment_id)

        stored = self.stored_plan(attempt_id)
        self.assertEqual(stored["question_ids"], self.reversed_closed + self.reversed_open)
        self.assertEqual(stored["open_question_ids"], self.reversed_open)
        self.assertTrue(stored["mixed_open_enabled"])
        self.assertEqual(self.attempt(attempt_id)["score_total"], len(self.closed_ids))
        # The teacher's assignment keeps its own plan and membership.
        self.assertEqual(self.assignment_plan_json(assignment_id), assignment_plan_before)

    def test_default_rng_keeps_groups_and_membership(self):
        assignment_id = self.mixed_assignment()
        client = self.client(self.account())

        attempt_id = self.start(client, assignment_id)

        stored = self.stored_plan(attempt_id)
        closed_count = len(self.closed_ids)
        self.assertCountEqual(stored["question_ids"][:closed_count], self.closed_ids)
        self.assertCountEqual(stored["question_ids"][closed_count:], self.open_ids)
        self.assertEqual(stored["open_question_ids"], stored["question_ids"][closed_count:])

    def test_the_page_renders_the_persisted_order(self):
        assignment_id = self.mixed_assignment()
        client, attempt_id = self.started(assignment_id)

        self.assertEqual(self.rendered_question_ids(client, attempt_id),
                         self.reversed_closed + self.reversed_open)

    def test_open_only_plan_is_shuffled_and_renders(self):
        assignment_id = self.mixed_assignment(plan=mixed_plan([], self.open_ids))
        client, attempt_id = self.started(assignment_id)

        self.assertEqual(self.stored_plan(attempt_id)["question_ids"], self.reversed_open)
        self.assertEqual(self.rendered_question_ids(client, attempt_id), self.reversed_open)
        self.assertEqual(self.attempt(attempt_id)["score_total"], 0)

    def test_closed_only_assignment_keeps_its_existing_selection(self):
        assignment_id = self.assignment()
        student = self.account()
        with closing(web_app.quiz_db()) as conn:
            assignment = quiz.quiz_fetch_assignment(conn, assignment_id)
            _seed, expected = quiz.quiz_pick_questions(
                conn, assignment, quiz.quiz_account_seed_identity(student["id"]))
        client = self.client(student)

        # A closed-only attempt never goes through the group shuffle.
        with no_shuffle_allowed():
            attempt_id = self.start(client, assignment_id)

        self.assertEqual(self.stored_plan(attempt_id), expected)
        self.assertEqual(self.rendered_question_ids(client, attempt_id), expected)


class StableOrderTest(MixedShuffleTestCase):
    def test_refresh_and_repeated_start_never_reshuffle(self):
        assignment_id = self.mixed_assignment()
        client, attempt_id = self.started(assignment_id)
        row = tuple(self.attempt(attempt_id))
        order = self.rendered_question_ids(client, attempt_id)

        with no_shuffle_allowed():
            self.assertEqual(self.rendered_question_ids(client, attempt_id), order)
            self.assertEqual(self.rendered_question_ids(client, attempt_id), order)
            self.assertEqual(self.start(client, assignment_id), attempt_id)
            self.assertEqual(client.get(student_entry_path(assignment_id)).status_code, 200)

        self.assertEqual(tuple(self.attempt(attempt_id)), row)
        self.assertEqual(len(self.attempts_of(assignment_id)), 1)

    def test_resume_from_another_browser_shows_the_same_order(self):
        assignment_id = self.mixed_assignment()
        student = self.account()
        client, attempt_id = self.started(assignment_id, student)
        order = self.rendered_question_ids(client, attempt_id)
        row = tuple(self.attempt(attempt_id))

        other_browser = self.client(student)
        with no_shuffle_allowed():
            self.assertEqual(self.start(other_browser, assignment_id), attempt_id)
            self.assertEqual(self.rendered_question_ids(other_browser, attempt_id), order)

        self.assertEqual(tuple(self.attempt(attempt_id)), row)

    def test_an_empty_submission_keeps_the_order(self):
        assignment_id = self.mixed_assignment()
        client, attempt_id = self.started(assignment_id)
        row = tuple(self.attempt(attempt_id))

        response = client.post(f"/quiz/attempt/{attempt_id}", data={}, headers=SAME_ORIGIN)

        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            [int(qid) for qid in re.findall(r'data-question-id="(\d+)"', response.get_data(as_text=True))],
            self.reversed_closed + self.reversed_open)
        self.assertEqual(tuple(self.attempt(attempt_id)), row)

    def test_racing_starts_of_one_account_compute_the_same_plan(self):
        # The order comes from the attempt seed, so the request that loses the
        # insert race would have stored exactly what the winner stored.
        assignment_id = self.mixed_assignment()
        student = self.account()
        with closing(web_app.quiz_db()) as conn:
            assignment = quiz.quiz_fetch_assignment(conn, assignment_id)
            seed = quiz.quiz_seed(assignment_id, quiz.quiz_account_seed_identity(student["id"]))
            plan = quiz.quiz_parse_assignment_question_plan(assignment["question_plan_json"])
            expected = quiz.quiz_shuffle_plan_within_groups(plan, quiz._quiz_attempt_order_rng(seed))

        attempt_id = self.start(self.client(student), assignment_id)

        self.assertEqual(self.stored_plan(attempt_id), expected)
        self.assertEqual(self.attempt(attempt_id)["seed"], seed)


class IndependentPlansTest(MixedShuffleTestCase):
    def test_each_account_gets_its_own_persisted_plan(self):
        assignment_id = self.mixed_assignment()
        first_client, first_id = self.started(assignment_id)
        first_row = tuple(self.attempt(first_id))

        # The second account's order comes from its own seed, not from the first attempt.
        second_client = self.client(self.account())
        second_id = self.start(second_client, assignment_id)

        self.assertNotEqual(first_id, second_id)
        self.assertNotEqual(self.attempt(first_id)["seed"], self.attempt(second_id)["seed"])
        self.assertEqual(tuple(self.attempt(first_id)), first_row)
        self.assertCountEqual(self.stored_plan(second_id)["question_ids"],
                              self.closed_ids + self.open_ids)
        self.assertEqual(self.rendered_question_ids(first_client, first_id),
                         self.reversed_closed + self.reversed_open)
        self.assertEqual(self.rendered_question_ids(second_client, second_id),
                         self.stored_plan(second_id)["question_ids"])

    def test_another_account_cannot_read_or_change_the_plan(self):
        assignment_id = self.mixed_assignment()
        _client, attempt_id = self.started(assignment_id)

        self.assert_denied_everywhere(self.client(self.account()), attempt_id, "another student")
        self.assert_denied_everywhere(self.client(self.account("teacher")), attempt_id, "a teacher")
        self.assert_denied_everywhere(self.client(), attempt_id, "anonymous")

    def test_the_browser_cannot_submit_an_order(self):
        assignment_id = self.mixed_assignment()
        forged = json.dumps(mixed_plan(self.closed_ids, self.open_ids))
        forged_fields = {
            "question_ids_json": forged,
            "question_plan_json": forged,
            "question_ids": ",".join(str(qid) for qid in self.closed_ids),
            "open_question_ids": "",
            "seed": "chosen-by-the-browser",
        }
        client = self.client(self.account())
        with reversing_order():
            attempt_id = self.start(client, assignment_id, data=forged_fields)
        row = tuple(self.attempt(attempt_id))
        self.assertEqual(self.stored_plan(attempt_id)["question_ids"],
                         self.reversed_closed + self.reversed_open)

        # Neither a repeated start nor the answer form can rewrite it.
        self.start(client, assignment_id, data=forged_fields)
        self.assertEqual(tuple(self.attempt(attempt_id)), row)
        client.post(f"/quiz/attempt/{attempt_id}",
                    data={**forged_fields, f"q_{self.closed_ids[0]}": "А"}, headers=SAME_ORIGIN)
        submitted = self.attempt(attempt_id)
        self.assertEqual(submitted["question_ids_json"], row[submitted.keys().index("question_ids_json")])
        self.assertEqual(submitted["seed"], row[submitted.keys().index("seed")])


class AnswersAndScoringTest(MixedShuffleTestCase):
    def submit_all(self, client, attempt_id):
        """Answer the first closed question correctly, the others wrongly, and every open one."""
        right_id = self.closed_ids[0]
        data = {f"q_{right_id}": self.correct_letters[right_id]}
        for qid in self.closed_ids[1:]:
            data[f"q_{qid}"] = "няма такава буква"
        for number, qid in enumerate(self.open_ids, 1):
            # The first open answer is right, the rest are wrong.
            data[f"open_q_{qid}_1"] = "отговор1" if number == 1 else f"грешка-{qid}"
        response = client.post(f"/quiz/attempt/{attempt_id}", data=data, headers=SAME_ORIGIN)
        self.assertEqual(response.status_code, 302)
        return data

    def test_answers_and_scores_stay_with_their_questions(self):
        assignment_id = self.mixed_assignment()
        client, attempt_id = self.started(assignment_id)

        data = self.submit_all(client, attempt_id)

        closed = {row["question_id"]: row for row in self.query(
            "SELECT question_id, chosen_letter, is_correct FROM quiz_answers WHERE attempt_id = ?",
            (attempt_id,))}
        self.assertCountEqual(closed, self.closed_ids)
        for qid in self.closed_ids:
            self.assertEqual(closed[qid]["chosen_letter"], data[f"q_{qid}"])
            self.assertEqual(closed[qid]["is_correct"], int(qid == self.closed_ids[0]))

        open_rows = {row["question_id"]: row for row in self.query(
            "SELECT question_id, raw_answer, is_correct FROM quiz_text_answers WHERE attempt_id = ?",
            (attempt_id,))}
        self.assertCountEqual(open_rows, self.open_ids)
        for qid in self.open_ids:
            self.assertEqual(open_rows[qid]["raw_answer"], data[f"open_q_{qid}_1"])
            self.assertEqual(open_rows[qid]["is_correct"], int(qid == self.open_ids[0]))

        attempt = self.attempt(attempt_id)
        self.assertEqual((attempt["score_correct"], attempt["score_total"]), (1, len(self.closed_ids)))

    def test_completed_attempt_stays_stable(self):
        assignment_id = self.mixed_assignment()
        client, attempt_id = self.started(assignment_id)
        plan_json = self.attempt(attempt_id)["question_ids_json"]
        self.submit_all(client, attempt_id)
        row = tuple(self.attempt(attempt_id))

        with no_shuffle_allowed():
            result = client.get(f"/quiz/attempt/{attempt_id}/result")
            self.assertEqual(result.status_code, 200)
            self.assertEqual(client.get(f"/quiz/attempt/{attempt_id}").status_code, 302)
            restart = client.post(student_entry_path(assignment_id), headers=SAME_ORIGIN)
            self.assertEqual(restart.headers["Location"], f"/quiz/attempt/{attempt_id}/result")
            # The locked attempt takes no second submission.
            client.post(f"/quiz/attempt/{attempt_id}",
                        data={f"q_{self.closed_ids[1]}": self.correct_letters[self.closed_ids[1]]},
                        headers=SAME_ORIGIN)

        self.assertEqual(tuple(self.attempt(attempt_id)), row)
        self.assertEqual(self.attempt(attempt_id)["question_ids_json"], plan_json)
        # The result lists the closed questions in the attempt's own order.
        page = result.get_data(as_text=True)
        with closing(web_app.quiz_db()) as conn:
            prompts = {int(q["id"]): q["prompt"] for q in quiz.quiz_load_questions(
                conn, self.closed_ids, "seed")}
        positions = [page.find(str(web_app.app.jinja_env.filters["e"](prompts[qid])))
                     for qid in self.reversed_closed]
        self.assertNotIn(-1, positions)
        self.assertEqual(positions, sorted(positions))

    def test_teacher_review_reads_the_original_question_ids(self):
        teacher = self.account("teacher")
        assignment_id = self.mixed_assignment(owner=teacher)
        assignment_plan_before = self.assignment_plan_json(assignment_id)
        client, attempt_id = self.started(assignment_id)
        data = self.submit_all(client, attempt_id)

        response = self.client(teacher).get(f"/teacher/assignment/{assignment_id}/results")

        self.assertEqual(response.status_code, 200)
        page = response.get_data(as_text=True)
        for qid in self.open_ids:
            self.assertIn(f"Q{qid}.1", page)
            self.assertIn(data[f"open_q_{qid}_1"], page)
        self.assertEqual(self.assignment_plan_json(assignment_id), assignment_plan_before)
        self.assertEqual(self.client(teacher).get(f"/teacher/assignment/{assignment_id}").status_code, 200)
        self.assertEqual(
            self.client(self.account("teacher")).get(
                f"/teacher/assignment/{assignment_id}/results").status_code, 404)


class LegacyAttemptTest(MixedShuffleTestCase):
    def unshuffled_attempt(self, assignment_id, *, user_id=None):
        """An attempt created before group shuffling: the assignment's plan, copied as it was."""
        with closing(web_app.quiz_db()) as conn:
            attempt_id = runtime_fixtures.add_attempt(
                conn, assignment_id, student_name="Отпреди", question_ids=[], user_id=user_id)
            conn.execute("UPDATE quiz_attempts SET question_ids_json = ? WHERE id = ?",
                         (self.assignment_plan_json(assignment_id), attempt_id))
            conn.commit()
        return attempt_id

    def test_account_attempt_from_before_the_change_keeps_its_stored_order(self):
        assignment_id = self.mixed_assignment()
        student = self.account()
        attempt_id = self.unshuffled_attempt(assignment_id, user_id=student["id"])
        row = tuple(self.attempt(attempt_id))
        client = self.client(student)

        with no_shuffle_allowed():
            self.assertEqual(self.start(client, assignment_id), attempt_id)
            self.assertEqual(self.rendered_question_ids(client, attempt_id),
                             self.closed_ids + self.open_ids)

        self.assertEqual(tuple(self.attempt(attempt_id)), row)

    def test_historical_attempt_is_read_as_stored(self):
        assignment_id = self.mixed_assignment()
        attempt_id = self.unshuffled_attempt(assignment_id)
        row = tuple(self.attempt(attempt_id))

        with no_shuffle_allowed():
            self.assertEqual(self.rendered_question_ids(self.client(markers=[attempt_id]), attempt_id),
                             self.closed_ids + self.open_ids)

        self.assertEqual(tuple(self.attempt(attempt_id)), row)

    def test_historical_closed_only_attempt_is_read_as_stored(self):
        assignment_id = self.assignment()
        attempt_id = self.historical_attempt(assignment_id)
        expected = self.stored_plan(attempt_id)

        with no_shuffle_allowed():
            self.assertEqual(self.rendered_question_ids(self.client(markers=[attempt_id]), attempt_id),
                             expected)


class PersistentDataTest(MixedShuffleTestCase):
    def test_everything_is_written_to_the_temporary_database(self):
        runtime_db = (ROOT / "data" / "questions.db").resolve()
        test_db = Path(web_app.app.config["DB_PATH"]).resolve()
        self.assertNotEqual(test_db, runtime_db)
        before = runtime_db.stat().st_mtime_ns if runtime_db.exists() else None

        assignment_id = self.mixed_assignment()
        client, attempt_id = self.started(assignment_id)
        self.rendered_question_ids(client, attempt_id)

        self.assertEqual(runtime_db.stat().st_mtime_ns if runtime_db.exists() else None, before)


if __name__ == "__main__":
    unittest.main()
