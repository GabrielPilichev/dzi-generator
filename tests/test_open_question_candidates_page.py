import unittest
from pathlib import Path


from tests.support import IsolatedAppTestCase, web_app


def _register_dzi_default_denied_test_route(app):
    endpoint = "dzi_test_only_default_denied"
    if endpoint in app.view_functions:
        return

    def view():
        return "test-only DZI endpoint"

    app.add_url_rule("/__test__/dzi-default-denied", endpoint=endpoint, view_func=view)


class OpenQuestionCandidatesPageTest(IsolatedAppTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.app = web_app.app
        _register_dzi_default_denied_test_route(cls.app)
        cls.app.config.update(TESTING=True)

    def setUp(self):
        self.client = self.app.test_client()

    def _login_admin(self):
        with self.client.session_transaction() as sess:
            sess["admin_authenticated"] = True
            sess["ui_profile"] = "admin"

    def _login_tester(self):
        with self.client.session_transaction() as sess:
            sess["tester_authenticated"] = True
            sess["ui_profile"] = "tester"

    def _insert_linked_image_open_candidate(self):
        conn = web_app.quiz_db()
        try:
            cur = conn.execute("""
                INSERT INTO questions (
                    source_exam, source_number, question_type, topic, difficulty,
                    points, prompt, has_image, is_ai_generated, quality_score
                )
                VALUES (?, ?, 'fill_in', 'test', 'medium', 1, ?, 0, 0, NULL)
            """, (
                "temp-open-candidate-image-indicator",
                901,
                "Отворен кандидат със справочен материал.",
            ))
            question_id = int(cur.lastrowid)
            conn.execute("""
                INSERT INTO fill_in_subquestions (
                    question_id, subquestion_number, correct_answer, answer_alternatives
                )
                VALUES (?, 1, 'клиент', NULL)
            """, (question_id,))

            local_path = "web/static/img/questions/dzi/test_open_candidate_indicator.png"
            asset_cur = conn.execute("""
                INSERT INTO assets (
                    asset_type, original_filename, local_path, mime_type, file_size, notes
                )
                VALUES ('pdf_crop', ?, ?, 'image/png', 1, 'test open candidate image indicator')
            """, (
                Path(local_path).name,
                local_path,
            ))
            asset_id = int(asset_cur.lastrowid)
            conn.execute("""
                INSERT INTO asset_links (
                    asset_id, owner_type, owner_id, role, display_order, caption_bg
                )
                VALUES (?, 'question', ?, 'pdf_crop', 0, 'Тестово изображение към кандидат')
            """, (asset_id, question_id))
            conn.commit()
            return question_id
        finally:
            conn.close()

    def test_admin_can_access_open_question_candidates_page(self):
        self._login_admin()
        response = self.client.get("/admin/open-question-candidates")

        self.assertEqual(response.status_code, 200)
        self.assertIn("Отворени въпроси за смесени тестове".encode("utf-8"), response.data)
        self.assertIn("read-only".encode("utf-8"), response.data)
        self.assertIn("MC-only".encode("utf-8"), response.data)

    def test_tester_cannot_access_open_question_candidates_page(self):
        self._login_tester()
        response = self.client.get("/admin/open-question-candidates")

        self.assertEqual(response.status_code, 302)
        self.assertIn("/admin/login", response.headers["Location"])

    def test_sessionless_user_cannot_access_open_question_candidates_page(self):
        response = self.client.get("/admin/open-question-candidates")

        self.assertEqual(response.status_code, 302)
        self.assertIn("/admin/login", response.headers["Location"])

    def test_page_renders_candidate_count_and_grouping(self):
        conn = web_app.quiz_db()
        try:
            candidates = web_app.fetch_open_question_candidates(conn)
        finally:
            conn.close()

        grouped_sources = sorted({candidate["source_slug"] for candidate in candidates})
        self.assertGreater(len(candidates), 0)

        self._login_admin()
        response = self.client.get("/admin/open-question-candidates")

        self.assertEqual(response.status_code, 200)
        self.assertIn(str(len(candidates)).encode("utf-8"), response.data)
        for source_slug in grouped_sources:
            self.assertIn(source_slug.encode("utf-8"), response.data)
        self.assertIn(b"question_id", response.data)
        self.assertIn("Подвъпроси".encode("utf-8"), response.data)

    def test_page_marks_candidates_with_linked_image(self):
        question_id = self._insert_linked_image_open_candidate()

        self._login_admin()
        response = self.client.get("/admin/open-question-candidates")

        self.assertEqual(response.status_code, 200)
        body = response.data.decode("utf-8")
        row_start = body.find(f"<td>{question_id}</td>")
        self.assertNotEqual(row_start, -1)
        row_end = body.find("</tr>", row_start)
        row_html = body[row_start:row_end]

        self.assertIn("question-image-indicator", row_html)
        self.assertIn("изображение", row_html)
        self.assertNotIn("/static/img/questions/dzi/test_open_candidate_indicator.png", row_html)


if __name__ == "__main__":
    unittest.main()
