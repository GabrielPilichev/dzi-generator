"""Assignment lifecycle on practical uploads (docs/ASSIGNMENT_LIFECYCLE_DESIGN.md, 7.4, 11.14).

An upload is a student detail write and uses the same submittable rule as a quiz
submission, decided under the writer lock: accepted while the assignment is open
or the attempt is in its grace window, refused after the cutoff, after
``submissions_closed_at``, for a held assignment and with an unusable clock. A
refused upload leaves no row and no file. Case ids refer to section 15.1.
"""

import io
import unittest
from datetime import timedelta
from pathlib import Path

from unittest.mock import patch

from tests.lifecycle_support import GRACE, SAME_ORIGIN, LifecycleTestCase, ts
from tests.support import web_app
from web.blueprints import practical as practical_blueprint

SOURCE = "may_2025_v2"


class PracticalUploadLifecycleTest(LifecycleTestCase):
    def setUp(self):
        super().setUp()
        self.teacher = self.account("teacher")
        self.student = self.account("student")
        self.browser = self.client(self.student)
        self.E = self.T0 + timedelta(hours=2)
        self.task_id = self.query(
            "SELECT t.id FROM exam_tasks t JOIN exams e ON e.id = t.exam_id "
            "WHERE e.year = 2025 AND e.session = 'may' AND e.variant = 2 AND t.task_number = 26")[0][0]
        self.upload_root = Path(web_app.app.config["PRACTICAL_UPLOAD_ROOT"])

    def fixture(self, **columns):
        assignment_id = self.assignment(self.teacher, deadline_at=self.E, **columns)
        attempt_id = self.attempt(assignment_id, self.student, started_at=self.T0 - timedelta(minutes=30))
        return assignment_id, attempt_id

    def upload(self, client, attempt_id, name="solution.xlsx"):
        return client.post(
            f"/quiz/attempt/{attempt_id}/practical/{SOURCE}/task/{self.task_id}/upload",
            data={"files": (io.BytesIO(b"xlsx bytes"), name)},
            content_type="multipart/form-data", headers=SAME_ORIGIN)

    def stored(self) -> tuple:
        rows = self.query("SELECT COUNT(*) FROM practical_submissions")[0][0]
        files = self.query("SELECT COUNT(*) FROM practical_submission_files")[0][0]
        on_disk = sorted(p.name for p in self.upload_root.rglob("*") if p.is_file())
        return rows, files, len(on_disk)

    def test_l73_accepted_while_open_and_in_grace_refused_after_the_cutoff(self):
        assignment_id, attempt_id = self.fixture()

        accepted = self.upload(self.browser, attempt_id)  # open
        self.assertEqual(accepted.status_code, 302)
        self.assertIn("uploaded=1", accepted.headers["Location"])
        self.assertEqual(self.stored(), (1, 1, 1))
        uploaded = self.query("SELECT uploaded_at FROM practical_submission_files")[0][0]
        self.assertEqual(uploaded, ts(self.T0))  # the transaction's now, not SQLite's clock

        self.clock.set(self.E + GRACE - timedelta(seconds=1))  # the last second of grace
        self.assertIn("uploaded=1", self.upload(self.browser, attempt_id, "second.xlsx").headers["Location"])
        self.assertEqual(self.stored(), (1, 2, 2))
        self.assertEqual(self.query("SELECT updated_at FROM practical_submissions")[0][0],
                         ts(self.E + GRACE - timedelta(seconds=1)))

        self.clock.set(self.E + GRACE)  # exactly the cutoff
        refused = self.upload(self.browser, attempt_id, "late.xlsx")
        self.assertEqual(refused.status_code, 302)
        self.assertIn("upload_error=closed", refused.headers["Location"])
        self.assertEqual(self.stored(), (1, 2, 2))  # no row, and no file left on disk
        self.assertEqual(self.row(assignment_id)["submissions_closed_at"], ts(self.E + GRACE))
        page = self.browser.get(refused.headers["Location"]).get_data(as_text=True)
        self.assertIn('id="upload-closed-notice"', page)
        self.assertIn("Тестът е затворен и вече не приема файлове.", page)

    def test_an_accepted_grace_upload_persists_the_expiry_it_observed(self):
        # Codex finding 1: an upload accepted after the deadline left expiry_observed_at NULL.
        assignment_id, attempt_id = self.fixture()
        when = self.E + timedelta(minutes=4)
        self.clock.set(when)
        self.assertIsNone(self.row(assignment_id)["expiry_observed_at"])
        accepted = self.upload(self.browser, attempt_id)
        self.assertIn("uploaded=1", accepted.headers["Location"])
        row = self.row(assignment_id)
        self.assertEqual(row["expiry_observed_at"], ts(when))  # committed with the upload's own rows
        self.assertEqual((row["closed_at"], row["submissions_closed_at"]), (None, None))
        self.assertEqual(self.stored(), (1, 1, 1))
        self.assertEqual(self.query("SELECT uploaded_at FROM practical_submission_files")[0][0], ts(when))
        # A second accepted upload does not move the marker; a new start is refused by it.
        self.clock.advance(60)
        self.assertIn("uploaded=1", self.upload(self.browser, attempt_id, "second.xlsx").headers["Location"])
        self.assertEqual(self.row(assignment_id)["expiry_observed_at"], ts(when))
        self.assertEqual(self.start(self.client(self.account()), assignment_id).status_code, 409)

    def test_a_failed_grace_upload_leaves_neither_rows_nor_the_observation(self):
        assignment_id, attempt_id = self.fixture()
        self.clock.set(self.E + timedelta(minutes=4))
        refused = self.upload(self.browser, attempt_id, "virus.exe")  # refused before the transaction
        self.assertIn("upload_error=invalid", refused.headers["Location"])
        with patch.object(practical_blueprint, "build_practical_upload_path", side_effect=OSError("disk")):
            failed = self.upload(self.browser, attempt_id)  # fails inside the transaction: rolled back
        self.assertIn("upload_error=invalid", failed.headers["Location"])
        self.assertEqual(self.stored(), (0, 0, 0))
        self.assertIsNone(self.row(assignment_id)["expiry_observed_at"])

    def test_an_attempt_that_was_not_in_flight_cannot_upload_in_the_window(self):
        assignment_id = self.assignment(self.teacher, deadline_at=self.E)
        late_attempt = self.attempt(assignment_id, self.student, started_at=self.E)
        self.clock.set(self.E + timedelta(minutes=2))
        self.assertIn("upload_error=closed", self.upload(self.browser, late_attempt).headers["Location"])
        self.assertEqual(self.stored(), (0, 0, 0))

    def test_l102_the_marker_refuses_an_upload_without_consulting_the_clock(self):
        assignment_id, attempt_id = self.fixture(submissions_closed_at=self.T0 - timedelta(hours=1))
        self.assertLess(self.clock.wall(), self.E)  # by the deadline alone it would still be open
        self.assertIn("upload_error=closed", self.upload(self.browser, attempt_id).headers["Location"])
        self.assertEqual(self.stored(), (0, 0, 0))

    def test_l74_another_accounts_upload_in_grace_is_a_404_with_no_file(self):
        _assignment_id, attempt_id = self.fixture()
        self.clock.set(self.E + timedelta(minutes=3))
        for client in (self.client(self.account()), self.client(self.teacher), self.client()):
            response = self.upload(client, attempt_id)
            self.assertEqual(response.status_code, 404)
            self.assertNotIn("затворен", response.get_data(as_text=True))
        self.assertEqual(self.stored(), (0, 0, 0))

    def test_l78_a_clock_fault_refuses_the_upload_and_writes_no_file(self):
        _assignment_id, attempt_id = self.fixture()
        self.clock.advance(600)
        self.upload(self.browser, attempt_id)
        self.clock.step_wall(-300)
        before = self.stored()
        self.assertEqual(self.upload(self.browser, attempt_id, "again.xlsx").status_code, 503)
        self.assertEqual(self.stored(), before)
        self.assertEqual(self.clock_row()["state"], "suspect")

    def test_l116_a_held_assignment_serves_neither_the_page_nor_the_upload(self):
        assignment_id, attempt_id = self.fixture()
        self.upload(self.browser, attempt_id)
        file_id = self.query("SELECT id FROM practical_submission_files")[0][0]
        owner = self.client(self.teacher)
        self.assertEqual(owner.get(f"/teacher/practical/submission-file/{file_id}/download").status_code, 200)
        self.assertIn("solution.xlsx", owner.get("/teacher/practical-submissions").get_data(as_text=True))

        self.execute("UPDATE quiz_assignments SET lifecycle_hold = 'restore_pending' WHERE id = ?", (assignment_id,))
        before = self.stored()
        self.assertEqual(self.browser.get(f"/quiz/attempt/{attempt_id}/practical/{SOURCE}").status_code, 503)
        self.assertEqual(self.upload(self.browser, attempt_id, "held.xlsx").status_code, 503)
        self.assertEqual(self.upload(self.client(self.account()), attempt_id).status_code, 404)  # a stranger
        self.assertEqual(self.stored(), before)
        # Review of a held assignment's work is not served either.
        self.assertEqual(owner.get(f"/teacher/practical/submission-file/{file_id}/download").status_code, 503)
        self.assertNotIn("solution.xlsx", owner.get("/teacher/practical-submissions").get_data(as_text=True))
        self.assertEqual(
            self.client(self.account("teacher")).get(f"/teacher/practical/submission-file/{file_id}/download")
            .status_code, 404)

    def test_the_submitted_attempt_rule_and_validation_are_unchanged(self):
        assignment_id, attempt_id = self.fixture()
        self.assertIn("upload_error=invalid", self.upload(self.browser, attempt_id, "virus.exe").headers["Location"])
        self.execute("UPDATE quiz_attempts SET submitted_at = ? WHERE id = ?",
                     (ts(self.T0 - timedelta(minutes=1)), attempt_id))
        self.assertIn("upload_error=invalid", self.upload(self.browser, attempt_id).headers["Location"])
        self.assertEqual(self.stored(), (0, 0, 0))
        self.assertEqual(self.lifecycle_columns(assignment_id)["submissions_closed_at"], None)


if __name__ == "__main__":
    unittest.main()
