"""Registry-driven homepage, navigation, subject routes and teacher form.

Both classes run against a temporary copy of the seed database. The second one
imports synthetic Biology content through the reviewed importer into that copy,
never into the repository database.
"""

import re
import sqlite3
from unittest.mock import patch

from src.import_reviewed_subject_questions import import_batch
from src.subject_assessment import AssessmentScope
from src.subject_registry import PLATFORM_GRADES, SUBJECTS
from tests.support import IsolatedAppTestCase, web_app
from tests.test_homepage_search import _HTMLProbe

CARD = re.compile(r'<article\s+class="([^"]*home-subject-card[^"]*)"')
CARD_TITLE = re.compile(r'<h2 class="home-subject-title">([^<]+)</h2>')
SLUGS = [subject.route_slug for subject in SUBJECTS]
TITLES = [subject.title for subject in SUBJECTS]
NON_IT = [subject for subject in SUBJECTS if subject.id != "it"]


def _cards(html):
    classes = [set(value.split()) for value in CARD.findall(html)]
    return list(zip(CARD_TITLE.findall(html), classes))


def _subject_nav(html):
    """Ordered, de-duplicated subject links in the navigation menu -> class sets."""
    found = {}
    for _, attrs in _HTMLProbe(html).elements("a"):
        href = attrs.get("href") or ""
        classes = set((attrs.get("class") or "").split())
        parts = href.strip("/").split("/")
        if "nav-menu-link" in classes and len(parts) == 2 and parts[0] == "subjects":
            found.setdefault(parts[1], classes)
    return found


class _RouteTestCase(IsolatedAppTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.app = web_app.app

    def setUp(self):
        self.client = self.app.test_client()

    def login(self):
        with self.client.session_transaction() as session:
            session["tester_authenticated"] = True
            session["admin_authenticated"] = True

    def html(self, path, status=200):
        response = self.client.get(path)
        self.assertEqual(response.status_code, status, path)
        return response.get_data(as_text=True)

    def assignment_count(self):
        conn = web_app.quiz_db()
        try:
            return conn.execute("SELECT COUNT(*) FROM quiz_assignments").fetchone()[0]
        finally:
            conn.close()

    def section_id(self, slug):
        conn = web_app.quiz_db()
        try:
            return conn.execute(
                "SELECT id FROM curriculum_sections WHERE section_slug = ?", (slug,)
            ).fetchone()[0]
        finally:
            conn.close()


class SeedSubjectRoutesTest(_RouteTestCase):
    def test_homepage_lists_every_subject_in_registry_order(self):
        cards = _cards(self.html("/"))
        self.assertEqual([title for title, _ in cards], TITLES)

    def test_only_it_is_active_on_the_seed(self):
        html = self.html("/")
        for subject, (title, classes) in zip(SUBJECTS, _cards(html)):
            with self.subTest(subject=subject.id):
                if subject.id == "it":
                    self.assertIn("home-subject-card--active", classes)
                    self.assertNotIn("mode-card--inactive", classes)
                else:
                    self.assertIn("home-subject-card--placeholder", classes)
                    self.assertIn("mode-card--inactive", classes)
        self.assertEqual(html.count('class="home-subject-status"'), len(SUBJECTS) - 1)
        self.assertIn("В подготовка", html)

    def test_preparation_subjects_stay_searchable_by_name_and_terms(self):
        html = self.html("/")
        for subject in SUBJECTS:
            with self.subTest(subject=subject.id):
                self.assertIn(subject.title, html)
                search_texts = re.findall(r'data-search-text="([^"]*)"', html)
                self.assertTrue(any(
                    subject.title in text and subject.search_terms in text for text in search_texts
                ))

    def test_navigation_follows_registry_order_and_derived_status(self):
        self.login()
        for path in ("/", "/tester", "/subjects/biology", "/grade/8"):
            with self.subTest(path=path):
                nav = _subject_nav(self.html(path))
                self.assertEqual(list(nav), SLUGS)
                for subject in SUBJECTS:
                    placeholder = "nav-menu-link--placeholder" in nav[subject.route_slug]
                    self.assertEqual(placeholder, subject.id != "it", subject.id)

    def test_database_error_degrades_every_subject_to_preparation(self):
        self.login()
        boom = sqlite3.OperationalError("simulated availability failure")
        with patch.object(web_app, "subject_content_grades", side_effect=boom), \
                self.assertLogs(web_app.app.logger, level="WARNING") as logs:
            html = self.html("/")
            nav = _subject_nav(html)
            self.html("/subjects/information-technologies")
        self.assertTrue(any("Subject availability lookup failed" in line for line in logs.output))
        cards = _cards(html)
        self.assertEqual([title for title, _ in cards], TITLES)
        self.assertTrue(all("mode-card--inactive" in classes for _, classes in cards))
        self.assertEqual(list(nav), SLUGS)
        self.assertTrue(all("nav-menu-link--placeholder" in classes for classes in nav.values()))

    def test_every_subject_and_declared_grade_renders(self):
        for subject in SUBJECTS:
            with self.subTest(subject=subject.id):
                self.assertIn(subject.title, self.html(f"/subjects/{subject.route_slug}"))
                for grade in subject.grades:
                    self.html(f"/subjects/{subject.route_slug}/{grade}")

    def test_preparation_subject_pages_explain_the_state(self):
        for subject in NON_IT:
            with self.subTest(subject=subject.id):
                page = _HTMLProbe(self.html(f"/subjects/{subject.route_slug}")).text
                self.assertIn("Съдържанието идва скоро", page)
                self.assertIn("В подготовка", page)
                grade_page = _HTMLProbe(self.html(f"/subjects/{subject.route_slug}/8")).text
                self.assertIn("В подготовка", grade_page)
                self.assertIn(subject.title, grade_page)

    def test_it_subject_page_shows_active_content(self):
        page = _HTMLProbe(self.html("/subjects/information-technologies")).text
        self.assertIn("Съдържанието вече е налично", page)
        self.assertIn("Текущото съдържание с класове, теми, въпроси и тренировъчни тестове.", page)

    def test_unsupported_grades_are_not_found(self):
        for subject in SUBJECTS:
            for grade in (0, 7, 13, 99):
                with self.subTest(subject=subject.id, grade=grade):
                    self.html(f"/subjects/{subject.route_slug}/{grade}", 404)
        self.html("/subjects/biology/eight", 404)
        self.html("/subjects/biology/8.5", 404)

    def test_unknown_ids_and_aliases_never_fall_back_to_a_subject(self):
        for slug in ("it", "IT", "informatika_it", "informatika", "Biology", "unknown", "Информатика"):
            with self.subTest(slug=slug):
                self.html(f"/subjects/{slug}", 404)
                self.html(f"/subjects/{slug}/8", 404)
                self.html(f"/subjects/{slug}/8/anything", 404)

    def test_topic_group_routes_exist_only_for_configured_placeholders(self):
        self.html("/subjects/physics/8/mechanics")
        self.html("/subjects/physics/9/mechanics")
        self.html("/subjects/physics/8/astrophysics", 404)  # a grade 12 group
        for subject in NON_IT:
            if subject.id != "physics":
                with self.subTest(subject=subject.id):
                    self.html(f"/subjects/{subject.route_slug}/8/mechanics", 404)

    def test_legacy_routes_are_unchanged(self):
        self.html("/grade/8")
        self.html("/grade/7", 404)
        self.html("/section/grade8-computer-systems")
        self.html("/section/grade12-dzi-preparation")
        self.html("/subjects/information-technologies/8")
        # Pre-registry Physics URLs stay valid as permanent redirects.
        for legacy, canonical in (("/physics", "/subjects/physics"),
                                  ("/physics/grade/9", "/subjects/physics/9")):
            response = self.client.get(legacy)
            self.assertEqual(response.status_code, 301, legacy)
            self.assertTrue(response.headers["Location"].endswith(canonical), legacy)
        self.html("/physics/grade/7", 404)

    def test_it_class_links_use_the_legacy_grade_routes(self):
        hrefs = _HTMLProbe(self.html("/")).hrefs()
        for grade in PLATFORM_GRADES:
            self.assertIn(f"/grade/{grade}", hrefs)
            self.assertNotIn(f"/subjects/information-technologies/{grade}", hrefs)

    def test_teacher_form_defaults_to_it_with_dzi_features(self):
        self.login()
        html = self.html("/teacher/new")
        hrefs = _HTMLProbe(html).hrefs()
        self.assertIn("/teacher/new?subject=information-technologies", hrefs)
        for subject in NON_IT:
            self.assertNotIn(f"/teacher/new?subject={subject.route_slug}", hrefs, subject.id)
        self.assertIn("practical-assignment-panel", html)
        self.assertIn("ДЗИ източник за отворени въпроси", html)
        self.assertIn("Информационни технологии — 8. клас", html)
        self.assertNotIn("Все още няма раздели", html)

    def test_teacher_form_for_preparation_subjects_is_empty_and_not_dzi(self):
        self.login()
        for subject in NON_IT:
            with self.subTest(subject=subject.id):
                html = self.html(f"/teacher/new?subject={subject.route_slug}")
                self.assertIn(
                    f"Все още няма раздели по {subject.title.lower()} с въпроси за тест.", html
                )
                self.assertNotIn("practical-assignment-panel", html)
                self.assertNotIn("ДЗИ източник", html)
                self.assertIn("Източник за отворени въпроси", html)
                # The selected subject stays reachable; other preparation subjects do not.
                hrefs = _HTMLProbe(html).hrefs()
                self.assertIn(f"/teacher/new?subject={subject.route_slug}", hrefs)
                others = [s for s in NON_IT if s.id != subject.id]
                for other in others:
                    self.assertNotIn(f"/teacher/new?subject={other.route_slug}", hrefs)

    def test_teacher_form_rejects_unknown_subjects(self):
        self.login()
        self.html("/teacher/new?subject=not-a-subject", 404)
        response = self.client.post("/teacher/new", data={
            "subject": "not-a-subject", "section_id": "1", "question_count": "1",
        })
        self.assertEqual(response.status_code, 400)

    def test_preparation_subject_cannot_create_an_assignment_from_another_subject(self):
        self.login()
        it_section = self.section_id("grade8-computer-systems")
        before = self.assignment_count()
        for subject in NON_IT:
            with self.subTest(subject=subject.id):
                response = self.client.post("/teacher/new", data={
                    "subject": subject.route_slug, "section_id": str(it_section),
                    "question_count": "1", "open_count": "0",
                })
                self.assertEqual(response.status_code, 400)
        self.assertEqual(self.assignment_count(), before)


def _biology_batch():
    return {
        "format_version": 1, "subject": "biology", "grade": 8,
        "source": "reviewed:biology:registry-routes-test-v1",
        "curriculum": {"subject": "biology", "grade": 8,
                       "education_type": "synthetic-registry-test", "title": "Synthetic biology 8"},
        "section": {"slug": "biology-g8-synthetic-section",
                    "title": "Synthetic biology section", "grade": 8},
        "topic": {"slug": "biology-g8-synthetic-topic",
                  "title": "Synthetic biology topic", "grade": 8},
        "questions": [
            {"number": number, "subject": "biology", "grade": 8,
             "topic_slug": "biology-g8-synthetic-topic", "type": "multiple_choice",
             "prompt": f"Synthetic biology question {number}?",
             "options": {"A": "One", "B": "Two", "C": "Three", "D": "Four"},
             "correct": "B"}
            for number in (1, 2, 3)
        ],
    }


class SyntheticBiologySubjectTest(_RouteTestCase):
    """Content-derived availability: a subject activates by data alone, no route work."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        import_batch(web_app.QUIZ_DB_PATH, _biology_batch(), apply=True)
        cls.section_slug = "biology-g8-synthetic-section"

    def test_homepage_activates_biology_and_only_biology(self):
        html = self.html("/")
        cards = dict(_cards(html))
        self.assertNotIn("mode-card--inactive", cards["Биология"])
        self.assertIn("home-subject-card--active", cards["Биология"])
        for subject in NON_IT:
            if subject.id != "biology":
                self.assertIn("mode-card--inactive", cards[subject.title], subject.id)
        self.assertEqual(html.count('class="home-subject-status"'), len(SUBJECTS) - 2)
        self.assertIn("/subjects/biology/8", _HTMLProbe(html).hrefs())
        self.assertNotIn("/subjects/biology/9", _HTMLProbe(html).hrefs())
        self.assertIn("Synthetic biology section", html)  # searchable through the card

    def test_navigation_status_is_derived_on_pages_that_are_not_subject_pages(self):
        self.login()
        for path in ("/tester", "/", "/grade/8", "/subjects/information-technologies"):
            with self.subTest(path=path):
                nav = _subject_nav(self.html(path))
                self.assertNotIn("nav-menu-link--placeholder", nav["biology"])
                self.assertNotIn("nav-menu-link--placeholder", nav["information-technologies"])
                self.assertIn("nav-menu-link--placeholder", nav["physics"])
                self.assertIn("nav-menu-link--placeholder", nav["chemistry"])

    def test_subject_page_and_grade_pages(self):
        page = _HTMLProbe(self.html("/subjects/biology")).text
        self.assertIn("Съдържанието вече е налично", page)
        self.assertIn("Съдържание по класове, раздели и въпроси по биология.", page)
        grade8 = self.html("/subjects/biology/8")
        self.assertIn("Synthetic biology section", grade8)
        # Links use the short slug; the stored, namespaced slug stays a valid URL.
        self.assertIn("/subjects/biology/8/synthetic-section", _HTMLProbe(grade8).hrefs())
        self.assertNotIn(f"/subjects/biology/8/{self.section_slug}", _HTMLProbe(grade8).hrefs())
        # A declared grade without content is an intentional preparation shell.
        shell = _HTMLProbe(self.html("/subjects/biology/9")).text
        self.assertIn("Темите за 9. клас са в подготовка", shell)
        self.assertNotIn("Synthetic biology section", shell)
        self.html("/subjects/biology/7", 404)
        self.html("/subjects/biology/13", 404)

    def test_section_page_shows_questions_and_respects_subject_and_grade(self):
        html = self.html(f"/subjects/biology/8/{self.section_slug}")
        self.assertIn("Synthetic biology question 1?", html)
        self.assertIn("Synthetic biology question 3?", html)
        short = self.html("/subjects/biology/8/synthetic-section")
        self.assertIn("Synthetic biology question 1?", short)
        self.assertIn("Synthetic biology question 3?", short)
        self.html(f"/subjects/biology/9/{self.section_slug}", 404)
        self.html(f"/subjects/physics/8/{self.section_slug}", 404)
        self.html(f"/subjects/information-technologies/8/{self.section_slug}", 404)
        self.html("/subjects/biology/8/no-such-section", 404)

    def test_legacy_it_routes_do_not_expose_biology(self):
        self.html(f"/section/{self.section_slug}", 404)
        for path in ("/grade/8", "/subjects/information-technologies/8", "/subjects/physics/8"):
            with self.subTest(path=path):
                html = self.html(path)
                self.assertNotIn("Synthetic biology", html)
                self.assertNotIn("/subjects/biology/8", _HTMLProbe(html).hrefs())

    def test_teacher_form_lists_biology_only_for_biology(self):
        self.login()
        it_page = self.html("/teacher/new")
        self.assertIn("/teacher/new?subject=biology", _HTMLProbe(it_page).hrefs())
        self.assertNotIn("Synthetic biology section", it_page)
        self.assertNotIn("/teacher/new?subject=physics", _HTMLProbe(it_page).hrefs())

        biology_page = self.html("/teacher/new?subject=biology")
        self.assertIn("Биология — 8. клас — Synthetic biology section (3 въпроса)", biology_page)
        self.assertNotIn("Информационни технологии — 8. клас", biology_page)
        self.assertNotIn("Все още няма раздели", biology_page)
        self.assertNotIn("practical-assignment-panel", biology_page)
        self.assertNotIn("ДЗИ източник", biology_page)

    def test_biology_assignment_is_created_and_scoped_to_biology(self):
        self.login()
        section_id = self.section_id(self.section_slug)
        response = self.client.post("/teacher/new", data={
            "subject": "biology", "section_id": str(section_id),
            "question_count": "5", "open_count": "0",
        })
        self.assertEqual(response.status_code, 302)
        assignment_id = int(response.headers["Location"].rstrip("/").split("/")[-1])
        conn = web_app.quiz_db()
        try:
            row = conn.execute(
                "SELECT section_id, question_count FROM quiz_assignments WHERE id = ?",
                (assignment_id,),
            ).fetchone()
            self.assertEqual((row["section_id"], row["question_count"]), (section_id, 3))
            self.assertEqual(AssessmentScope(conn, section_id).subject, "biology")
        finally:
            conn.close()
        self.assertEqual(self.client.get(f"/teacher/assignment/{assignment_id}").status_code, 200)

    def test_subjects_cannot_be_mixed_when_creating_assignments(self):
        self.login()
        biology_section = self.section_id(self.section_slug)
        it_section = self.section_id("grade8-computer-systems")
        before = self.assignment_count()
        for subject, section in (("it", biology_section), ("physics", biology_section),
                                 ("biology", it_section)):
            with self.subTest(subject=subject, section=section):
                response = self.client.post("/teacher/new", data={
                    "subject": subject, "section_id": str(section),
                    "question_count": "1", "open_count": "0",
                })
                self.assertEqual(response.status_code, 400)
        self.assertEqual(self.assignment_count(), before)
