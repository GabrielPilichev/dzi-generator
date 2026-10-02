"""A subject added by one registry entry plus data flows through every generic surface.

The extra subject exists only while a test runs: it is patched into the registry
lookup maps and its rows are imported into a temporary database. Nothing in the
application names it, which is the point of the test.
"""

import re
from contextlib import ExitStack

from src.import_reviewed_subject_questions import import_batch
from src.subject_assessment import AssessmentScope
from src.subject_registry import SUBJECTS, SubjectDefinition
from tests.subject_fixtures import registered_subject
from tests.support import ROOT, IsolatedAppTestCase, web_app
from tests.test_homepage_search import _HTMLProbe

EXTRA = SubjectDefinition(
    id="testology",
    route_slug="testology",
    title="Тестология",
    description="Synthetic subject registered only inside the test suite.",
    search_terms="testologiya synthetic",
    grades=(11, 12),
)
SECTION_SLUG = "testology-g11-basics"
TOPIC_SLUG = "testology-g11-first-topic"
CARD_TITLE = re.compile(r'<h2 class="home-subject-title">([^<]+)</h2>')


def _batch():
    return {
        "format_version": 1, "subject": EXTRA.id, "grade": 11,
        "source": "reviewed:testology:generic-flow-test-v1",
        "curriculum": {"subject": EXTRA.id, "grade": 11,
                       "education_type": "synthetic-generic-flow", "title": "Synthetic testology 11"},
        "section": {"slug": SECTION_SLUG, "title": "Synthetic testology section", "grade": 11},
        "topic": {"slug": TOPIC_SLUG, "title": "Synthetic testology topic", "grade": 11},
        "questions": [
            {"number": number, "subject": EXTRA.id, "grade": 11, "topic_slug": TOPIC_SLUG,
             "type": "multiple_choice", "prompt": f"Synthetic testology question {number}?",
             "options": {"A": "One", "B": "Two", "C": "Three", "D": "Four"}, "correct": "C"}
            for number in (1, 2, 3)
        ],
    }


class _ExtraSubjectTestCase(IsolatedAppTestCase):
    def setUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        self.registration = stack
        stack.enter_context(registered_subject(EXTRA))
        self.client = web_app.app.test_client()

    def login(self):
        with self.client.session_transaction() as session:
            session["tester_authenticated"] = True
            session["admin_authenticated"] = True

    def page(self, path, status=200):
        response = self.client.get(path)
        self.assertEqual(response.status_code, status, path)
        html = response.get_data(as_text=True)
        return html, _HTMLProbe(html)

    @staticmethod
    def card(html):
        for _, attrs in _HTMLProbe(html).elements("article"):
            if EXTRA.title in (attrs.get("data-search-text") or ""):
                return set((attrs.get("class") or "").split())
        return None

    @staticmethod
    def nav_link(html):
        return next((set((attrs.get("class") or "").split())
                     for _, attrs in _HTMLProbe(html).elements("a")
                     if attrs.get("href") == f"/subjects/{EXTRA.route_slug}"
                     and "nav-menu-link" in (attrs.get("class") or "")), None)


class RegisteredSubjectWithoutContentTest(_ExtraSubjectTestCase):
    def test_registration_alone_gives_the_preparation_experience(self):
        html, home = self.page("/")
        self.assertEqual(CARD_TITLE.findall(html), [s.title for s in SUBJECTS] + [EXTRA.title])
        self.assertIn("mode-card--inactive", self.card(html))
        self.assertIn(f"/subjects/{EXTRA.route_slug}", home.hrefs())

        _, subject = self.page("/subjects/testology")
        self.assertIn("Съдържанието идва скоро", subject.text)
        self.assertIn("11.–12. клас", subject.text)
        for grade in EXTRA.grades:
            _, shell = self.page(f"/subjects/testology/{grade}")
            self.assertIn(f"Темите за {grade}. клас са в подготовка", shell.text)
        self.page("/subjects/testology/8", 404)          # not a declared grade
        self.page("/subjects/testology/11/anything", 404)

        self.login()
        html, _ = self.page("/")
        self.assertIn("nav-menu-link--placeholder", self.nav_link(html))
        html, _ = self.page("/teacher/new?subject=testology")
        self.assertIn("Все още няма раздели по тестология с въпроси за тест.", html)


class RegisteredSubjectWithContentTest(_ExtraSubjectTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        with registered_subject(EXTRA):
            import_batch(web_app.QUIZ_DB_PATH, _batch(), apply=True)

    def test_homepage_and_navigation(self):
        html, home = self.page("/")
        self.assertEqual(CARD_TITLE.findall(html)[-1], EXTRA.title)
        classes = self.card(html)
        self.assertIn("home-subject-card--active", classes)
        self.assertNotIn("mode-card--inactive", classes)
        self.assertIn("/subjects/testology/11", home.hrefs())
        self.assertNotIn("/subjects/testology/12", home.hrefs())
        self.assertIn("Synthetic testology section", html)     # searchable through the card
        self.login()
        html, _ = self.page("/grade/8")
        self.assertNotIn("nav-menu-link--placeholder", self.nav_link(html))

    def test_subject_grade_section_and_topic_pages(self):
        _, subject = self.page("/subjects/testology")
        self.assertIn("Съдържанието вече е налично", subject.text)
        self.assertIn("3 въпроса", subject.text)

        _, grade = self.page("/subjects/testology/11")
        self.assertIn("Synthetic testology section", grade.text)
        self.assertIn("/subjects/testology/11/basics", grade.hrefs())
        _, shell = self.page("/subjects/testology/12")
        self.assertIn("Темите за 12. клас са в подготовка", shell.text)

        html, section = self.page("/subjects/testology/11/basics")
        self.assertEqual(html.count('class="question"'), 3)
        self.assertIn("question-search", section.ids())
        self.assertIn("/subjects/testology/11/basics/first-topic", section.hrefs())
        self.assertIn(f"/teacher/new?subject=testology&section={SECTION_SLUG}", section.hrefs())

        html, topic = self.page("/subjects/testology/11/basics/first-topic")
        self.assertIn("Synthetic testology topic", topic.text)
        self.assertEqual(html.count('class="question"'), 3)

    def test_teacher_selection_and_assignment(self):
        self.login()
        it_form, it_probe = self.page("/teacher/new")
        self.assertIn("/teacher/new?subject=testology", it_probe.hrefs())
        self.assertNotIn("Synthetic testology section", it_form)

        html, form = self.page(f"/teacher/new?subject=testology&section={SECTION_SLUG}")
        self.assertIn("Тестология — 11. клас — Synthetic testology section (3 въпроса)", html)
        self.assertNotIn("practical-assignment-panel", html)
        selected = [attrs for _, attrs in form.elements("option") if "selected" in attrs]
        self.assertEqual(len(selected), 1)

        response = self.client.post("/teacher/new", data={
            "subject": "testology", "section_id": selected[0]["value"],
            "question_count": "2", "open_count": "0",
        })
        self.assertEqual(response.status_code, 302)
        assignment_id = int(response.headers["Location"].rstrip("/").split("/")[-1])
        conn = web_app.quiz_db()
        try:
            section_id = conn.execute(
                "SELECT section_id FROM quiz_assignments WHERE id = ?", (assignment_id,)
            ).fetchone()[0]
            self.assertEqual(AssessmentScope(conn, section_id).subject, EXTRA.id)
        finally:
            conn.close()
        self.page(f"/teacher/assignment/{assignment_id}")
        self.page(f"/quiz/{assignment_id}")

        # Another subject cannot use this section.
        response = self.client.post("/teacher/new", data={
            "subject": "it", "section_id": selected[0]["value"],
            "question_count": "1", "open_count": "0",
        })
        self.assertEqual(response.status_code, 400)

    def test_other_subjects_and_legacy_routes_do_not_expose_it(self):
        self.page(f"/section/{SECTION_SLUG}", 404)
        self.page(f"/subjects/physics/11/{SECTION_SLUG}", 404)
        self.page(f"/subjects/information-technologies/11/{SECTION_SLUG}", 404)
        for path in ("/grade/11", "/subjects/information-technologies/11"):
            html, _ = self.page(path)
            self.assertNotIn("Synthetic testology", html)

    def test_the_application_contains_nothing_specific_to_the_subject(self):
        sources = [ROOT / "web" / "app.py", ROOT / "web" / "production.py",
                   *(ROOT / "web" / "blueprints").glob("*.py"),
                   *(ROOT / "web" / "templates").glob("*.html"),
                   *(ROOT / "web" / "static" / "css").glob("*.css"),
                   *(ROOT / "web" / "static" / "js").glob("*.js"),
                   ROOT / "src" / "subject_registry.py"]
        self.assertGreater(len(sources), 30)
        for source in sources:
            text = source.read_text(encoding="utf-8")
            self.assertNotIn(EXTRA.id, text, source.name)
            self.assertNotIn(EXTRA.title, text, source.name)

    def test_removing_the_registry_entry_removes_the_subject_everywhere(self):
        self.page("/subjects/testology/11/basics")
        self.registration.close()
        self.page("/subjects/testology", 404)
        self.page("/subjects/testology/11/basics", 404)
        html, _ = self.page("/")
        self.assertEqual(CARD_TITLE.findall(html), [s.title for s in SUBJECTS])
        self.assertNotIn("Synthetic testology", html)


if __name__ == "__main__":
    import unittest

    unittest.main()
