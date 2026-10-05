"""Same-grade IT/Physics browsing, using only the isolated test database."""

from src.subject_browsing import SubjectContentBrowser
from tests.subject_fixtures import add_curriculum, add_membership, add_question, add_section, add_topic
from tests.support import IsolatedAppTestCase, web_app
from tests.test_homepage_search import _HTMLProbe


class SubjectScopedBrowsingTest(IsolatedAppTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.app = web_app.app
        with cls.app.app_context():
            cls.baseline_it_grade8 = next(
                row["question_count"] for row in web_app.fetch_grades_with_counts()
                if row["grade"] == 8
            )

        conn = web_app.quiz_db()
        try:
            it_curriculum = add_curriculum(conn, "IT", 8, "f2-it")
            physics_curriculum = add_curriculum(conn, "physics", 8, "f2-physics")
            cls.it_section = add_section(
                conn, it_curriculum, "f2-it-grade8", "F2 IT раздел", grade=8
            )
            cls.physics_section = add_section(
                conn, physics_curriculum, "f2-physics-grade8", "F2 Физика раздел", grade=8
            )
            cls.it_topic = add_topic(conn, cls.it_section, "f2-it-topic", "F2 IT тема", grade=8)
            cls.physics_topic = add_topic(
                conn, cls.physics_section, "f2-physics-topic", "F2 Физика тема", grade=8
            )
            cls._question(conn, cls.it_topic, "IT", 1, "F2 IT въпрос")
            cls._question(conn, cls.physics_topic, "physics", 2, "F2 Физика въпрос")
            cls._question(conn, cls.physics_topic, "IT", 3, "F2 конфликтен предмет")
            cross_topic = add_topic(
                conn, cls.physics_section, "f2-cross-topic", "F2 конфликтна тема", grade=8
            )
            add_membership(conn, cross_topic, cls.it_section)
            cls._question(conn, cross_topic, "physics", 4, "F2 конфликтно членство")
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def _question(conn, topic_id, subject, number, prompt):
        # Only the first two questions carry an answer option.
        options = {"A": f"F2 отговор {number}"} if number in (1, 2) else None
        return add_question(conn, topic_id, subject, number, source="f2-browser-fixture",
                            prompt=prompt, options=options)

    def setUp(self):
        self.client = self.app.test_client()

    def _page(self, path):
        response = self.client.get(path)
        return response, _HTMLProbe(response.get_data(as_text=True))

    def test_physics_subject_and_class_use_database_content_not_it_grade(self):
        subject_response, subject_page = self._page("/subjects/physics")
        class_response, class_page = self._page("/subjects/physics/8")
        self.assertEqual(subject_response.status_code, 200)
        self.assertIn("Съдържанието вече е налично", subject_page.text)
        self.assertIn("/subjects/physics/8", subject_page.hrefs())
        self.assertEqual(class_response.status_code, 200)
        self.assertIn("F2 Физика раздел", class_page.text)
        self.assertNotIn("F2 IT раздел", class_page.text)
        self.assertIn("/subjects/physics/8/f2-physics-grade8", class_page.hrefs())
        self.assertNotIn("/grade/8", class_page.hrefs())

    def test_it_grade_page_remains_it_only_and_grade_overview_lists_subjects(self):
        response, page = self._page("/subjects/information-technologies/8")
        self.assertEqual(response.status_code, 200)
        self.assertIn("F2 IT раздел", page.text)
        self.assertNotIn("F2 Физика раздел", page.text)
        # /grade/8 shows subjects, never one subject's sections.
        response, overview = self._page("/grade/8")
        self.assertEqual(response.status_code, 200)
        for path in ("/subjects/information-technologies/8", "/subjects/physics/8"):
            self.assertIn(path, overview.hrefs())
        self.assertNotIn("F2 IT раздел", overview.text)
        self.assertNotIn("F2 Физика раздел", overview.text)
        self.assertNotIn("/section/f2-it-grade8", overview.hrefs())

    def test_physics_group_shows_only_its_owned_topics_questions_and_options(self):
        response, page = self._page("/subjects/physics/8/f2-physics-grade8")
        self.assertEqual(response.status_code, 200)
        self.assertIn("F2 Физика тема", page.text)
        self.assertIn("F2 Физика въпрос", page.text)
        self.assertIn("F2 отговор 2", page.text)
        for excluded in (
            "F2 IT тема", "F2 IT въпрос", "F2 конфликтен предмет",
            "F2 конфликтна тема", "F2 конфликтно членство",
        ):
            self.assertNotIn(excluded, page.text)

    def test_it_group_and_legacy_section_do_not_show_physics(self):
        for path in (
            "/subjects/information-technologies/8/f2-it-grade8",
            "/section/f2-it-grade8",
        ):
            with self.subTest(path=path):
                response, page = self._page(path)
                self.assertEqual(response.status_code, 200)
                self.assertIn("F2 IT въпрос", page.text)
                self.assertNotIn("F2 Физика въпрос", page.text)
                self.assertNotIn("F2 конфликтно членство", page.text)

    def test_wrong_subject_or_grade_cannot_open_section(self):
        for path in (
            "/subjects/physics/8/f2-it-grade8",
            "/subjects/information-technologies/8/f2-physics-grade8",
            "/subjects/physics/9/f2-physics-grade8",
            "/subjects/physics/8/grade12-dzi-preparation",
            "/section/f2-physics-grade8",
        ):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 404)

    def test_unknown_subject_is_not_treated_as_it(self):
        for path in (
            "/subjects/unmapped-subject",
            "/subjects/unmapped-subject/8",
            "/subjects/unmapped-subject/8/f2-it-grade8",
        ):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 404)

    def test_empty_physics_class_retains_provisional_placeholder(self):
        response, page = self._page("/subjects/physics/9")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Темите за 9. клас са в подготовка", page.text)
        self.assertIn("/subjects/physics/9/mechanics", page.hrefs())
        self.assertNotIn("F2 Физика раздел", page.text)
        self.assertEqual(self.client.get("/subjects/physics/8/mechanics").status_code, 404)

    def test_subject_counts_use_owned_questions_not_same_grade_or_bad_data(self):
        with self.app.app_context():
            it_counts = SubjectContentBrowser(web_app.get_db(), "it").grade_counts()
            physics_counts = SubjectContentBrowser(web_app.get_db(), "physics").grade_counts()
            legacy_it_counts = web_app.fetch_grades_with_counts()
        self.assertEqual(next(row for row in it_counts if row["grade"] == 8)["question_count"],
                         self.baseline_it_grade8 + 1)
        self.assertEqual(next(row for row in legacy_it_counts if row["grade"] == 8)["question_count"],
                         self.baseline_it_grade8 + 1)
        self.assertEqual(next(row for row in physics_counts if row["grade"] == 8), {
            "grade": 8, "topic_count": 1, "question_count": 1,
        })
        self.assertIn("1 въпроса", self._page("/subjects/physics")[1].text)

    def test_homepage_subject_counts_links_and_search_text_stay_separate(self):
        response, page = self._page("/")
        self.assertEqual(response.status_code, 200)
        cards = {
            attrs.get("data-search-text", ""): attrs
            for tag, attrs in page.elements("article")
            if "home-subject-card" in (attrs.get("class") or "")
        }
        it_search = next(text for text in cards if "Информационни технологии" in text)
        physics_search = next(text for text in cards if "Физика" in text)
        self.assertIn("F2 IT раздел", it_search)
        self.assertIn("F2 IT тема", it_search)
        self.assertNotIn("F2 Физика раздел", it_search)
        self.assertIn("F2 Физика раздел", physics_search)
        self.assertIn("F2 Физика тема", physics_search)
        self.assertNotIn("F2 IT раздел", physics_search)
        self.assertNotIn("F2 конфликтна тема", physics_search)
        self.assertIn("/subjects/information-technologies/8", page.hrefs())
        self.assertIn("/subjects/physics/8", page.hrefs())
        self.assertNotIn("/section/f2-physics-grade8", page.hrefs())

    def test_physics_breadcrumbs_keep_subject_and_class_context(self):
        response, page = self._page("/subjects/physics/8/f2-physics-grade8")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Физика", page.text)
        self.assertIn("8. клас", page.text)
        self.assertIn("/subjects/physics", page.hrefs())
        self.assertIn("/subjects/physics/8", page.hrefs())
        with self.client.session_transaction() as session:
            session["admin_authenticated"] = True
        _, authenticated_page = self._page("/subjects/physics/8/f2-physics-grade8")
        physics_nav = next(
            attrs for tag, attrs in authenticated_page.elements("a")
            if attrs.get("href") == "/subjects/physics"
            and "nav-menu-link" in (attrs.get("class") or "")
        )
        self.assertNotIn("nav-menu-link--placeholder", physics_nav["class"])

    def test_known_unclassified_legacy_section_url_remains_available(self):
        response, page = self._page("/section/unknown-grade-unclassified")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("F2 Физика въпрос", page.text)

    def test_existing_dzi_section_stays_available(self):
        response, page = self._page("/section/grade12-dzi-preparation")
        self.assertEqual(response.status_code, 200)
        self.assertIn("ДЗИ", page.text)

    def test_subject_browsing_is_public_for_anonymous_and_authenticated_users(self):
        paths = ("/subjects/physics", "/subjects/physics/8",
                 "/subjects/physics/8/f2-physics-grade8", "/grade/8")
        for path in paths:
            with self.subTest(path=path, profile="anonymous"):
                self.assertEqual(self.client.get(path).status_code, 200)
        with self.client.session_transaction() as session:
            session["admin_authenticated"] = True
        for path in paths:
            with self.subTest(path=path, profile="admin"):
                self.assertEqual(self.client.get(path).status_code, 200)
