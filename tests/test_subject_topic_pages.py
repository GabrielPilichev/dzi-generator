"""Generic subject sections, topics, grouping and structure-only presentation.

Everything runs on a temporary canonical-built database with synthetic rows.
Titles are deliberately artificial; no curriculum is implied for any subject.
"""

import unittest

from src.subject_assessment import AssessmentScope
from src.subject_browsing import public_description, public_slug, stored_slug_candidates
from tests.subject_fixtures import (
    add_curriculum, add_membership, add_module, add_question, add_section, add_topic,
    four_options,
)
from tests.support import IsolatedAppTestCase, web_app, IT_ONLY
from tests.test_homepage_search import _HTMLProbe

TOOLBAR_IDS = ("question-search", "question-type-filter", "question-difficulty-filter",
               "toggle-correct", "bookmarks-only", "shuffle-questions", "reset-view")


class SlugRulesTest(unittest.TestCase):
    def test_public_slug_drops_only_the_matching_subject_grade_namespace(self):
        self.assertEqual(public_slug("physics", 8, "physics-g8-mechanics"), "mechanics")
        self.assertEqual(public_slug("physics", 9, "physics-g8-mechanics"), "physics-g8-mechanics")
        self.assertEqual(public_slug("biology", 8, "physics-g8-mechanics"), "physics-g8-mechanics")
        self.assertEqual(public_slug("it", 8, "grade8-computer-systems"), "grade8-computer-systems")
        self.assertEqual(public_slug("physics", 8, "physics-g8-"), "physics-g8-")

    def test_url_slugs_try_the_namespaced_form_then_the_exact_slug(self):
        self.assertEqual(stored_slug_candidates("physics", 8, "mechanics"),
                         ("physics-g8-mechanics", "mechanics"))
        self.assertEqual(stored_slug_candidates("it", 12, "grade12-dzi-preparation"),
                         ("it-g12-grade12-dzi-preparation", "grade12-dzi-preparation"))

    def test_only_prose_descriptions_are_public(self):
        self.assertEqual(public_description("  Кратко описание.  "), "Кратко описание.")
        for value in (None, "", "   ", "- [[spreadsheets-moc]]", 5):
            self.assertIsNone(public_description(value))


class _PageTestCase(IsolatedAppTestCase):
    def setUp(self):
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

    def content(self, path):
        """The page without its own address (login links carry ?next=<path>)."""
        return self.page(path)[0].replace(f"next={path}\"", "next=<this page>\"")


class SubjectTopicPagesTest(_PageTestCase):
    content_profile = IT_ONLY  # these tests pin Physics/Biology as subjects without content

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        conn = web_app.quiz_db()
        try:
            it_section = conn.execute(
                "SELECT id FROM curriculum_sections WHERE section_slug = 'grade8-computer-systems'"
            ).fetchone()[0]

            physics8 = add_curriculum(conn, "physics", 8, "topic-pages")
            # The stored slug is namespaced; its short form is a current placeholder URL.
            mechanics = add_section(conn, physics8, "physics-g8-mechanics",
                                    "Synthetic mechanics section", grade=8, display_order=1)
            other = add_section(conn, physics8, "physics-g8-other",
                                "Synthetic other section", grade=8, display_order=2)
            motion = add_topic(conn, mechanics, "physics-g8-motion", "Synthetic motion topic",
                               grade=8, description="Synthetic prose description.")
            forces = add_topic(conn, mechanics, "physics-g8-forces", "Synthetic forces topic",
                               grade=8, description="- [[physics-moc]]")
            add_topic(conn, mechanics, "plain-topic", "Synthetic plain topic", grade=8)
            elsewhere = add_topic(conn, other, "physics-g8-elsewhere",
                                  "Synthetic elsewhere topic", grade=8)
            conflicted = add_topic(conn, mechanics, "physics-g8-conflicted",
                                   "Synthetic conflicted topic", grade=8)
            add_membership(conn, conflicted, it_section)

            def question(topic_id, number, prompt, subject="physics"):
                return add_question(conn, topic_id, subject, number, source="topic-pages",
                                    prompt=prompt, options=four_options(f"Option {number}"),
                                    link_topic=True)

            question(motion, 1, "Synthetic motion question one?")
            question(motion, 2, "Synthetic motion question two?")
            question(forces, 3, "Synthetic forces question?")
            question(elsewhere, 4, "Synthetic elsewhere question?")
            question(conflicted, 5, "Synthetic conflicted question?")
            question(motion, 6, "Synthetic mislabelled question?", subject="IT")

            biology8 = add_curriculum(conn, "biology", 8, "topic-pages")
            cells = add_section(conn, biology8, "biology-g8-cells",
                                "Synthetic cells section", grade=8)
            cell = add_topic(conn, cells, "biology-g8-cell", "Synthetic cell topic", grade=8)
            question(cell, 7, "Synthetic cell question?", subject="biology")
            conn.commit()
        finally:
            conn.close()

    def test_topic_page_shows_the_topic_its_description_and_only_its_questions(self):
        html, page = self.page("/subjects/physics/8/mechanics/motion")
        self.assertEqual(page.first("h1")["data-motion-item"], "headline")
        self.assertIn("Synthetic motion topic", page.text)
        self.assertIn("Synthetic prose description.", page.text)
        self.assertIn("2 въпроса по темата", page.text)
        self.assertIn("Synthetic motion question one?", html)
        self.assertIn("Synthetic motion question two?", html)
        for absent in ("Synthetic forces question?", "Synthetic elsewhere question?",
                       "Synthetic conflicted question?", "Synthetic mislabelled question?",
                       "Synthetic cell question?"):
            self.assertNotIn(absent, html)
        self.assertEqual(html.count('class="question"'), 2)

    def test_topic_page_has_breadcrumbs_review_tools_and_the_section_test_action(self):
        html, page = self.page("/subjects/physics/8/mechanics/motion")
        hrefs = page.hrefs()
        for crumb in ("/", "/subjects/physics", "/subjects/physics/8",
                      "/subjects/physics/8/mechanics"):
            self.assertIn(crumb, hrefs)
        # Tests stay section-scoped: the action names the subject and the section only.
        self.assertIn("/teacher/new?subject=physics&section=physics-g8-mechanics", hrefs)
        self.assertNotIn("topic", next(href for href in hrefs if href.startswith("/teacher/new")))
        self.assertIn("Тестът се създава от целия раздел", page.text)
        for control in TOOLBAR_IDS:
            self.assertIn(control, page.ids())
        toolbar = next(attrs for _, attrs in page.elements("section")
                       if "data-section-tools" in attrs)
        self.assertEqual(toolbar["data-section-slug"], "physics-g8-mechanics/physics-g8-motion")
        self.assertIn("js/section-tools.js", html)

    def test_vault_link_residue_is_never_shown_as_a_description(self):
        html, page = self.page("/subjects/physics/8/mechanics/forces")
        self.assertIn("Synthetic forces question?", html)
        self.assertNotIn("[[", html)
        self.assertNotIn("physics-moc", html)

    def test_short_and_stored_slugs_name_the_same_pages(self):
        expected = self.content("/subjects/physics/8/mechanics/motion")
        for path in ("/subjects/physics/8/physics-g8-mechanics/motion",
                     "/subjects/physics/8/mechanics/physics-g8-motion",
                     "/subjects/physics/8/physics-g8-mechanics/physics-g8-motion"):
            with self.subTest(path=path):
                self.assertEqual(self.content(path), expected)
        # Generated links are always the short form, whichever URL was requested.
        self.assertEqual(self.content("/subjects/physics/8/physics-g8-mechanics"),
                         self.content("/subjects/physics/8/mechanics"))
        # A slug outside the namespace keeps its exact form.
        html, _ = self.page("/subjects/physics/8/mechanics/plain-topic")
        self.assertIn("Synthetic plain topic", html)

    def test_mismatched_or_cross_subject_combinations_are_not_found(self):
        for path in (
            "/subjects/biology/8/mechanics/motion",                      # other subject
            "/subjects/information-technologies/8/mechanics/motion",
            "/subjects/biology/8/physics-g8-mechanics/physics-g8-motion",
            "/subjects/physics/9/mechanics/motion",                      # other grade
            "/subjects/physics/9/physics-g8-mechanics/physics-g8-motion",
            "/subjects/physics/7/mechanics/motion",                      # undeclared grade
            "/subjects/physics/8/mechanics/elsewhere",                   # topic of another section
            "/subjects/physics/8/other/motion",
            "/subjects/physics/8/mechanics/cell",                        # topic of another subject
            "/subjects/physics/8/mechanics/biology-g8-cell",
            "/subjects/biology/8/cells/motion",
            "/subjects/physics/8/mechanics/conflicted",                  # cross-subject membership
            "/subjects/physics/8/mechanics/no-such-topic",
            "/subjects/physics/8/no-such-section/motion",
            "/subjects/physics/10/mechanics/motion",                     # placeholder group: no topics
            "/subjects/it/8/mechanics/motion",                           # ids and aliases are not URLs
            "/subjects/unknown/8/mechanics/motion",
        ):
            with self.subTest(path=path):
                self.page(path, 404)

    def test_topic_without_questions_keeps_the_section_test_action(self):
        html, page = self.page("/subjects/physics/8/mechanics/plain-topic")
        self.assertIn("По тази тема още няма публикувани въпроси.", page.text)
        self.assertIn("Въпросите по темата са в подготовка", page.text)
        self.assertNotIn("data-section-tools", html)
        self.assertNotIn("section-tools.js", html)
        self.assertIn("/teacher/new?subject=physics&section=physics-g8-mechanics", page.hrefs())

    def test_section_page_links_topics_and_offers_the_review_tools(self):
        html, page = self.page("/subjects/physics/8/mechanics")
        hrefs = page.hrefs()
        for slug in ("motion", "forces", "plain-topic"):
            self.assertIn(f"/subjects/physics/8/mechanics/{slug}", hrefs)
        self.assertNotIn("/subjects/physics/8/mechanics/conflicted", hrefs)
        self.assertFalse(any("physics-g8-" in href for href in hrefs if "/subjects/" in href))
        counts = dict(zip(
            [attrs["href"].rsplit("/", 1)[1] for _, attrs in page.elements("a")
             if "stretched-link" in (attrs.get("class") or "")],
            [text for text in page.text_parts if text.endswith("въпр.")],
        ))
        self.assertEqual(counts, {"forces": "1 въпр.", "motion": "2 въпр.", "plain-topic": "0 въпр."})
        for control in TOOLBAR_IDS:
            self.assertIn(control, page.ids())
        self.assertIn("/teacher/new?subject=physics&section=physics-g8-mechanics", hrefs)
        self.assertIn("Създай тест от раздела", page.text)
        self.assertEqual(html.count('class="question"'), 3)
        self.assertNotIn("Synthetic mislabelled question?", html)

    def test_placeholder_url_now_serves_the_stored_section(self):
        html, page = self.page("/subjects/physics/8/mechanics")
        self.assertIn("Synthetic mechanics section", page.text)
        self.assertNotIn("Тематична група в подготовка", page.text)
        # The grade has stored sections, so its other provisional groups are retired.
        self.page("/subjects/physics/8/thermal-phenomena", 404)
        # A grade without stored sections keeps its placeholder groups.
        _, placeholder = self.page("/subjects/physics/9/mechanics")
        self.assertIn("Тематична група в подготовка", placeholder.text)

    def test_topic_pages_are_public_and_absent_from_the_legacy_it_routes(self):
        self.page("/subjects/physics/8/mechanics/motion")
        self.login()
        self.page("/subjects/physics/8/mechanics/motion")
        self.page("/section/physics-g8-mechanics", 404)
        html, _ = self.page("/grade/8")
        self.assertNotIn("Synthetic mechanics section", html)

    def test_test_action_opens_the_section_scoped_teacher_flow(self):
        self.login()
        _, topic_page = self.page("/subjects/physics/8/mechanics/motion")
        action = next(href for href in topic_page.hrefs() if href.startswith("/teacher/new"))
        html, form = self.page(action)
        selected = [attrs for _, attrs in form.elements("option") if "selected" in attrs]
        self.assertEqual(len(selected), 1)
        self.assertIn("Физика — 8. клас — Synthetic mechanics section (3 въпроса)", html)

        response = self.client.post("/teacher/new", data={
            "subject": "physics", "section_id": selected[0]["value"],
            "question_count": "10", "open_count": "0",
        })
        self.assertEqual(response.status_code, 302)
        assignment_id = int(response.headers["Location"].rstrip("/").split("/")[-1])
        conn = web_app.quiz_db()
        try:
            row = conn.execute(
                "SELECT section_id, question_count, question_plan_json FROM quiz_assignments "
                "WHERE id = ?", (assignment_id,),
            ).fetchone()
            # Every eligible question of the section, not only the topic's two.
            self.assertEqual((row["question_count"], row["question_plan_json"]), (3, None))
            self.assertEqual(AssessmentScope(conn, row["section_id"]).subject, "physics")
            columns = {info[1] for info in conn.execute("PRAGMA table_info(quiz_assignments)")}
            self.assertNotIn("topic_id", columns)
        finally:
            conn.close()


class SharedSectionComponentsTest(_PageTestCase):
    """The IT section page and the generic section page render the same components."""

    SLUG = "grade8-computer-systems"

    def test_legacy_and_generic_section_pages_share_questions_and_tools(self):
        legacy_html, legacy = self.page(f"/section/{self.SLUG}")
        generic_html, generic = self.page(f"/subjects/information-technologies/8/{self.SLUG}")

        def articles(page):
            return [attrs for _, attrs in page.elements("article")
                    if "question" in (attrs.get("class") or "").split()]

        self.assertTrue(articles(legacy))
        self.assertEqual(articles(generic), articles(legacy))
        for page in (legacy, generic):
            for control in TOOLBAR_IDS:
                self.assertIn(control, page.ids())
            toolbar = next(attrs for _, attrs in page.elements("section")
                           if "data-section-tools" in attrs)
            # Same section, same browser-side bookmarks on both pages.
            self.assertEqual(toolbar["data-section-slug"], self.SLUG)
        for html in (legacy_html, generic_html):
            self.assertEqual(html.count('id="questions-list"'), 1)
            self.assertEqual(html.count("js/section-tools.js"), 1)
            self.assertIn("Преговорен режим", html)

    def test_each_page_keeps_its_own_test_action_and_navigation(self):
        _, legacy = self.page(f"/section/{self.SLUG}")
        _, generic = self.page(f"/subjects/information-technologies/8/{self.SLUG}")
        self.assertIn(f"/teacher/new?section={self.SLUG}", legacy.hrefs())
        self.assertIn("/subjects/information-technologies/8", legacy.hrefs())
        self.assertNotIn("/grade/8", legacy.hrefs())
        self.assertIn(
            f"/teacher/new?subject=information-technologies&section={self.SLUG}", generic.hrefs()
        )
        self.assertIn("/subjects/information-technologies", generic.hrefs())
        # Existing IT slugs are outside the namespace and stay exact in topic links.
        topic_links = [href for href in generic.hrefs()
                       if href.startswith(f"/subjects/information-technologies/8/{self.SLUG}/")]
        self.assertTrue(topic_links)
        self.page(topic_links[0])

    def test_no_subject_specific_templates_or_styles_remain(self):
        from tests.support import ROOT
        templates = ROOT / "web" / "templates"
        self.assertFalse((templates / "physics.html").exists())
        self.assertFalse((templates / "subject_topic_group.html").exists())
        css = (ROOT / "web" / "static" / "css" / "ui-pass9.css").read_text(encoding="utf-8")
        for name in ("physics", "biology", "chemistry", "mathematics", "informatics"):
            self.assertNotIn(name, css)
        self.assertNotIn("home-subject-card--it", css)
        self.assertIn(".home-subject-card--active", css)
        self.assertFalse(hasattr(web_app, "PHYSICS_TOPIC_GROUPS_BY_GRADE"))


class StructureOnlySubjectTest(_PageTestCase):
    """Sections and topics without any question: browsable, not a ready question bank."""
    content_profile = IT_ONLY  # these tests pin Physics/Biology as subjects without content

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        conn = web_app.quiz_db()
        try:
            chemistry8 = add_curriculum(conn, "chemistry", 8, "structure-only")
            section = add_section(conn, chemistry8, "chemistry-g8-outline",
                                  "Synthetic outline section", grade=8)
            add_topic(conn, section, "chemistry-g8-outline-topic",
                      "Synthetic outline topic", grade=8)
            conn.commit()
        finally:
            conn.close()

    def card(self, html, title):
        for _, attrs in _HTMLProbe(html).elements("article"):
            if title in (attrs.get("data-search-text") or ""):
                return set((attrs.get("class") or "").split())
        self.fail(f"No homepage card for {title}")

    def test_homepage_card_is_browsable_but_not_an_active_question_bank(self):
        html, page = self.page("/")
        classes = self.card(html, "Химия")
        self.assertIn("home-subject-card--structure", classes)
        self.assertNotIn("home-subject-card--active", classes)
        self.assertNotIn("mode-card--inactive", classes)
        self.assertIn("Предмет в подготовка", page.text)
        self.assertIn("Въпросите са в подготовка", page.text)
        self.assertIn("/subjects/chemistry/8", page.hrefs())
        # Subjects with questions and subjects without sections are unchanged.
        self.assertIn("home-subject-card--active", self.card(html, "Информационни технологии"))
        self.assertIn("mode-card--inactive", self.card(html, "Биология"))

    def test_subject_page_describes_structure_without_claiming_content(self):
        _, page = self.page("/subjects/chemistry")
        self.assertIn("Разделите и темите са подредени", page.text)
        self.assertIn("Въпросите и тестовете по химия са в подготовка", page.text)
        self.assertIn("Разделите за 8. клас са подредени. Въпросите са в подготовка.", page.text)
        self.assertIn("Въпроси в подготовка", page.text)
        self.assertNotIn("Съдържанието вече е налично", page.text)
        self.assertNotIn("0 въпроса", page.text)
        self.assertNotIn("Активен", page.text)
        # Other grades keep the plain preparation wording.
        self.assertIn("Място за бъдещи теми и упражнения за 9. клас.", page.text)

    def test_grade_section_and_topic_pages_show_structure_and_no_test_action(self):
        _, grade = self.page("/subjects/chemistry/8")
        self.assertIn("Synthetic outline section", grade.text)
        self.assertIn("Въпросите са в подготовка", grade.text)
        self.assertIn("/subjects/chemistry/8/outline", grade.hrefs())

        html, section = self.page("/subjects/chemistry/8/outline")
        self.assertIn("Въпросите за този раздел са в подготовка.", section.text)
        self.assertIn("В този раздел още няма публикувани въпроси.", section.text)
        self.assertIn("/subjects/chemistry/8/outline/outline-topic", section.hrefs())
        self.assertNotIn("data-section-tools", html)
        self.assertFalse(any(href.startswith("/teacher/new") for href in section.hrefs()))

        html, topic = self.page("/subjects/chemistry/8/outline/outline-topic")
        self.assertIn("Synthetic outline topic", topic.text)
        self.assertIn("По тази тема още няма публикувани въпроси.", topic.text)
        self.assertFalse(any(href.startswith("/teacher/new") for href in topic.hrefs()))

    def test_navigation_status_stays_the_cheap_section_based_one(self):
        self.login()
        html, _ = self.page("/")
        links = {attrs["href"]: set((attrs.get("class") or "").split())
                 for _, attrs in _HTMLProbe(html).elements("a")
                 if "nav-menu-link" in (attrs.get("class") or "")}
        self.assertNotIn("nav-menu-link--placeholder", links["/subjects/chemistry"])
        self.assertIn("nav-menu-link--placeholder", links["/subjects/biology"])

    def test_teacher_form_has_no_section_to_offer(self):
        self.login()
        html, _ = self.page("/teacher/new?subject=chemistry")
        self.assertIn("Все още няма раздели по химия с въпроси за тест.", html)


class GradeModuleGroupingTest(_PageTestCase):
    """Modules group a generic grade page when present; they are never required."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        conn = web_app.quiz_db()
        try:
            mathematics10 = add_curriculum(conn, "mathematics", 10, "grouping")
            first = add_module(conn, mathematics10, "synthetic-module-one",
                               "Synthetic module one", number=91001, grade=10)
            second = add_module(conn, mathematics10, "synthetic-module-two",
                                "Synthetic module two", number=91002, grade=10)
            add_section(conn, mathematics10, "mathematics-g10-a", "Synthetic section A",
                        grade=10, module_id=first, display_order=1)
            add_section(conn, mathematics10, "mathematics-g10-b", "Synthetic section B",
                        grade=10, module_id=first, display_order=2)
            add_section(conn, mathematics10, "mathematics-g10-c", "Synthetic section C",
                        grade=10, module_id=second, display_order=1)
            add_section(conn, mathematics10, "mathematics-g10-loose", "Synthetic loose section",
                        grade=10, display_order=1)

            mathematics9 = add_curriculum(conn, "mathematics", 9, "grouping")
            add_section(conn, mathematics9, "mathematics-g9-a", "Synthetic section D", grade=9)
            add_section(conn, mathematics9, "mathematics-g9-b", "Synthetic section E", grade=9)
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def headings(page):
        return [text for text in page.text_parts if text.startswith("Synthetic module")
                or text in ("Други раздели", "Модул")]

    def test_sections_are_grouped_by_module_with_ungrouped_sections_last(self):
        html, page = self.page("/subjects/mathematics/10")
        text = page.text
        order = [text.index(marker) for marker in (
            "Synthetic module one", "Synthetic section A", "Synthetic section B",
            "Synthetic module two", "Synthetic section C",
            "Други раздели", "Synthetic loose section",
        )]
        self.assertEqual(order, sorted(order))
        self.assertEqual(text.count("Модул"), 2)
        self.assertNotIn("91001", text)
        self.assertEqual(html.count('data-motion="stagger"'), 1)
        for slug in ("a", "b", "c", "loose"):
            self.assertIn(f"/subjects/mathematics/10/{slug}", page.hrefs())

    def test_a_grade_without_modules_stays_a_flat_list(self):
        html, page = self.page("/subjects/mathematics/9")
        self.assertIn("Съдържание за 9. клас", page.text)
        self.assertIn("Synthetic section D", page.text)
        self.assertNotIn("Модул", page.text)
        self.assertNotIn("Други раздели", page.text)
        self.assertEqual(html.count('data-motion="stagger"'), 1)

    def test_it_groups_only_grades_that_have_several_modules(self):
        _, grade11 = self.page("/subjects/information-technologies/11")
        self.assertIn("Обработка и анализ на данни", grade11.text)
        self.assertIn("Мултимедия", grade11.text)
        html, grade8 = self.page("/subjects/information-technologies/8")
        self.assertIn("Съдържание за 8. клас", grade8.text)
        self.assertEqual(html.count('data-motion="stagger"'), 1)
        # /grade/<n> is the subject overview and leads to the page above.
        _, overview = self.page("/grade/11")
        self.assertIn("/subjects/information-technologies/11", overview.hrefs())
        self.assertNotIn("Обработка и анализ на данни", overview.text)


if __name__ == "__main__":
    unittest.main()
