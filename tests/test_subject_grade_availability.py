"""Educational grade paths versus LearnPilot content availability.

The registry says, per subject, which educational paths each grade has (general,
profiled, both or none), which grades LearnPilot supports and on which path, and
which general grades depend on the framework plan. Pages render from that
metadata only; what content exists stays a database fact.
"""

from __future__ import annotations

import re
import unittest

from src.subject_registry import (
    GENERAL, PLATFORM_GRADES, PROFILED, SUBJECTS, SubjectDefinition, get_subject,
    grade_paths, subject_grade_paths, subject_supports_grade, validate_registry,
)
from tests.subject_fixtures import registered_subject
from tests.support import ROOT, IsolatedAppTestCase, web_app, IT_ONLY
from tests.test_homepage_search import _HTMLProbe

G, P, GP, NA = (GENERAL,), (PROFILED,), (GENERAL, PROFILED), ()

# Grades 8-12, the reviewed matrix in docs/SUBJECT_CONTENT_CONTRACT.md.
OFFICIAL_PATHS = {
    "it": (G, G, G, P, P),
    "physics": (G, G, G, P, P),
    "informatics": (G, NA, NA, P, P),
    "biology": (G, G, G, P, P),
    "mathematics": (G, G, G, GP, GP),
    "chemistry": (G, G, G, P, P),
}
# General grades that exist only under some framework plans ("G*").
FRAMEWORK_PLAN_DEPENDENT = {
    "it": (),
    "physics": (8,),
    "informatics": (8,),
    "biology": (8,),
    "mathematics": (),
    "chemistry": (8,),
}
# The path LearnPilot supports in each supported grade.
SUPPORTED_PATHS = {
    "it": {8: GENERAL, 9: GENERAL, 10: GENERAL, 11: PROFILED, 12: PROFILED},
    "physics": {8: GENERAL, 9: GENERAL, 10: GENERAL},
    "informatics": {8: GENERAL},
    "biology": {8: GENERAL, 9: GENERAL, 10: GENERAL},
    "mathematics": {8: GENERAL, 9: GENERAL, 10: GENERAL, 11: GENERAL, 12: GENERAL},
    "chemistry": {8: GENERAL, 9: GENERAL, 10: GENERAL},
}
# Grades whose profiled path LearnPilot does not cover.
OUTSIDE_SCOPE = {
    "it": (),
    "physics": (11, 12),
    "informatics": (11, 12),
    "biology": (11, 12),
    "mathematics": (11, 12),
    "chemistry": (11, 12),
}
NOTE_CLASS = "subject-grade-status-note"
CARD = re.compile(r'<a class="(card subject-class-card[^"]*)"\s+href="([^"]+)">(.*?)</a>', re.S)

# A deliberately unusual pattern, to prove pages follow metadata and not "11-12".
ODD = SubjectDefinition(
    id="oddology",
    route_slug="oddology",
    title="Одология",
    description="Synthetic subject registered only inside the test suite.",
    search_terms="oddology synthetic",
    grades=(9, 10),
    grade_paths=grade_paths(general=(9,), profiled=(9, 10, 12)),
    supported_paths={9: PROFILED},
    framework_plan_dependent_grades=(9,),
)


class GradePathRegistryTests(unittest.TestCase):
    def test_every_subject_has_the_reviewed_classification(self):
        self.assertEqual({subject.id for subject in SUBJECTS}, set(OFFICIAL_PATHS))
        for subject in SUBJECTS:
            with self.subTest(subject=subject.id):
                actual = tuple(subject.paths_for(grade) for grade in PLATFORM_GRADES)
                self.assertEqual(actual, OFFICIAL_PATHS[subject.id])
                self.assertEqual(subject.framework_plan_dependent_grades,
                                 FRAMEWORK_PLAN_DEPENDENT[subject.id])
                self.assertEqual(subject.profiled_grades_outside_scope, OUTSIDE_SCOPE[subject.id])

    def test_supported_path_per_supported_grade(self):
        for subject in SUBJECTS:
            with self.subTest(subject=subject.id):
                actual = {grade: subject.supported_path(grade) for grade in PLATFORM_GRADES
                          if subject.supported_path(grade) is not None}
                self.assertEqual(actual, SUPPORTED_PATHS[subject.id])
                self.assertEqual(tuple(actual), subject.grades)
        self.assertEqual(get_subject("it").supported_path(11), PROFILED)
        self.assertEqual(get_subject("mathematics").supported_path(11), GENERAL)
        self.assertEqual(get_subject("mathematics").paths_for(11), (GENERAL, PROFILED))
        self.assertIsNone(get_subject("physics").supported_path(11))

    def test_framework_plan_dependence_is_not_a_path(self):
        for subject in SUBJECTS:
            for grade in subject.framework_plan_dependent_grades:
                with self.subTest(subject=subject.id, grade=grade):
                    self.assertEqual(subject.paths_for(grade), (GENERAL,))
                    self.assertEqual(subject.supported_path(grade), GENERAL)

    def test_lookup_is_strict(self):
        self.assertEqual(subject_grade_paths("physics", 11), (PROFILED,))
        self.assertEqual(subject_grade_paths("mathematics", 11), (GENERAL, PROFILED))
        self.assertEqual(subject_grade_paths("informatics", 9), ())
        for grade in (7, 13, "8", 8.0, None, True):
            self.assertEqual(subject_grade_paths("physics", grade), ())
            self.assertIsNone(get_subject("physics").supported_path(grade))
        self.assertEqual(subject_grade_paths("unknown", 8), ())

    def test_a_path_does_not_make_a_grade_supported(self):
        self.assertFalse(subject_supports_grade("physics", 11))
        self.assertFalse(subject_supports_grade("informatics", 9))
        self.assertTrue(subject_supports_grade("it", 11))
        self.assertTrue(subject_supports_grade("mathematics", 11))

    def test_validation_rejects_inconsistent_declarations(self):
        base = dict(id="alpha", route_slug="alpha", title="Алфа", description="d", search_terms="s")
        both = grade_paths(general=(11,), profiled=(11,))
        self.assertEqual(dict(both), {11: (GENERAL, PROFILED)})
        cases = {
            "supported grade without a path": dict(grades=(8, 9), grade_paths=grade_paths(general=(8,))),
            "no paths at all": dict(grades=(8,)),
            "unknown path": dict(grades=(8,), grade_paths={8: ("vocational",)}),
            "path not a tuple": dict(grades=(8,), grade_paths={8: GENERAL}),
            "empty paths": dict(grades=(8,), grade_paths={8: ()}),
            "grade outside the platform": dict(grades=(8,), grade_paths={7: G, 8: G}),
            "several paths, none chosen": dict(grades=(11,), grade_paths=both),
            "supported path not among the paths": dict(
                grades=(8,), grade_paths=grade_paths(general=(8,)), supported_paths={8: PROFILED}),
            "supported path for an unsupported grade": dict(
                grades=(8,), grade_paths=grade_paths(general=(8,), profiled=(11,)),
                supported_paths={11: PROFILED}),
            "framework-plan grade without a general path": dict(
                grades=(11,), grade_paths=grade_paths(profiled=(11,)),
                framework_plan_dependent_grades=(11,)),
        }
        for label, fields in cases.items():
            with self.subTest(label), self.assertRaises(ValueError):
                validate_registry((SubjectDefinition(**base, **fields),))
        validate_registry((SubjectDefinition(
            **base, grades=(8, 11), grade_paths=grade_paths(general=(8, 11), profiled=(11, 12)),
            supported_paths={11: GENERAL}, framework_plan_dependent_grades=(8,)),))


class GradeAvailabilityPagesTest(IsolatedAppTestCase):
    content_profile = IT_ONLY  # these tests pin Physics/Biology as subjects without content

    def setUp(self):
        self.client = web_app.app.test_client()

    def page(self, path, status=200):
        response = self.client.get(path)
        self.assertEqual(response.status_code, status, path)
        html = response.get_data(as_text=True)
        return html, _HTMLProbe(html)

    def cards(self, path):
        """Grade cards on a subject page: grade number -> (classes, href, inner html)."""
        html, _ = self.page(path)
        found = {}
        for classes, href, inner in CARD.findall(html):
            grade = int(re.search(r'subject-class-number">(\d+)<', inner).group(1))
            found[grade] = (set(classes.split()), href, inner)
        return found

    def test_grade_list_label(self):
        label = web_app.grade_list_label
        self.assertEqual(label((11, 12)), "11.–12. клас")
        self.assertEqual(label((12, 11)), "11.–12. клас")
        self.assertEqual(label((10,)), "10. клас")
        self.assertEqual(label((9, 11, 12)), "9., 11.–12. клас")
        self.assertEqual(label(()), "")

    def test_card_colour_class_follows_the_supported_path(self):
        for subject in SUBJECTS:
            cards = self.cards(f"/subjects/{subject.route_slug}")
            self.assertEqual(tuple(cards), subject.grades, subject.id)
            for grade, (classes, _, inner) in cards.items():
                path = SUPPORTED_PATHS[subject.id][grade]
                other = PROFILED if path == GENERAL else GENERAL
                with self.subTest(subject=subject.id, grade=grade):
                    self.assertIn(f"subject-class-card--{path}", classes)
                    self.assertNotIn(f"subject-class-card--{other}", classes)
                    self.assertEqual("Профилирана подготовка" in inner, path == PROFILED)
                    self.assertEqual(
                        "Изучава се в зависимост от учебния план" in inner,
                        grade in FRAMEWORK_PLAN_DEPENDENT[subject.id],
                    )

    def test_mathematics_upper_grades_are_general_cards_with_a_profiled_note(self):
        cards = self.cards("/subjects/mathematics")
        for grade in (11, 12):
            classes, href, inner = cards[grade]
            self.assertIn("subject-class-card--general", classes)
            self.assertEqual(href, f"/subjects/mathematics/{grade}")
            self.assertNotIn("Профилирана подготовка", inner)
            self.page(href)
        html, probe = self.page("/subjects/mathematics")
        self.assertEqual(html.count(NOTE_CLASS), 1)
        self.assertIn("11.–12. клас — профилирана подготовка", probe.text)
        self.assertEqual(len(CARD.findall(html)), 5)

    def test_colour_is_independent_of_content(self):
        # Every card carries exactly one path class whether or not it has content.
        for subject in SUBJECTS:
            for grade, (classes, _, _) in self.cards(f"/subjects/{subject.route_slug}").items():
                with self.subTest(subject=subject.id, grade=grade):
                    path_classes = classes & {"subject-class-card--general",
                                              "subject-class-card--profiled"}
                    self.assertEqual(len(path_classes), 1)

    def test_profiled_paths_outside_scope_are_explained_not_linked(self):
        for subject in SUBJECTS:
            outside = OUTSIDE_SCOPE[subject.id]
            with self.subTest(subject=subject.id):
                html, probe = self.page(f"/subjects/{subject.route_slug}")
                hrefs = probe.hrefs()
                if not outside:
                    self.assertNotIn(NOTE_CLASS, html)
                    continue
                self.assertEqual(html.count(NOTE_CLASS), 1)
                self.assertIn("11.–12. клас — профилирана подготовка", probe.text)
                self.assertIn("не е включена в LearnPilot", probe.text)
                note = re.search(rf'<aside class="{NOTE_CLASS}".*?</aside>', html, re.S).group(0)
                self.assertIsNone(re.search(r"<a[\s>]", note))
                self.assertNotIn("subject-class-number", note)
                for grade in outside:
                    linked = f"/subjects/{subject.route_slug}/{grade}" in hrefs
                    self.assertEqual(linked, grade in subject.grades)
                for grade in subject.grades:
                    self.assertIn(f"/subjects/{subject.route_slug}/{grade}", hrefs)

    def test_not_applicable_grades_are_omitted(self):
        _, probe = self.page("/subjects/informatics")
        for grade in (9, 10):
            self.assertNotIn(f"/subjects/informatics/{grade}", probe.hrefs())
            self.assertNotIn(f"{grade}. клас", probe.text)
        self.assertIn("/subjects/informatics/8", probe.hrefs())
        self.assertIn("1 клас", probe.text)

    def test_unsupported_grades_have_no_routes(self):
        for subject in SUBJECTS:
            for grade in PLATFORM_GRADES:
                expected = 200 if grade in subject.grades else 404
                with self.subTest(subject=subject.id, grade=grade):
                    self.page(f"/subjects/{subject.route_slug}/{grade}", expected)
                    if expected == 404:
                        self.page(f"/subjects/{subject.route_slug}/{grade}/anything", 404)
        for grade in (0, 7, 13):
            self.page(f"/subjects/physics/{grade}", 404)
        self.page("/physics/grade/11", 404)

    def test_ownership_validation_is_unchanged(self):
        self.page("/section/grade8-computer-systems")
        self.page("/subjects/information-technologies/8/grade8-computer-systems")
        for other in ("physics", "biology", "mathematics"):
            self.page(f"/subjects/{other}/8/grade8-computer-systems", 404)
        self.page("/subjects/information-technologies/9/grade8-computer-systems", 404)

    def test_general_grade_without_content_stays_in_preparation(self):
        # Applicability is not availability: these subjects are general education
        # in grade 8 and have no stored content.
        for slug in ("informatics", "biology", "mathematics", "chemistry"):
            definition = next(s for s in SUBJECTS if s.route_slug == slug)
            self.assertEqual(definition.paths_for(8), (GENERAL,))
            with self.subTest(subject=slug):
                html, probe = self.page(f"/subjects/{slug}")
                self.assertIn("Съдържанието идва скоро", probe.text)
                self.assertIn("В подготовка", probe.text)
                self.assertNotIn("subject-class-card--active", html)
                _, grade_page = self.page(f"/subjects/{slug}/8")
                self.assertIn("Темите за 8. клас са в подготовка", grade_page.text)
        _, mathematics = self.page("/subjects/mathematics/12")
        self.assertIn("Темите за 12. клас са в подготовка", mathematics.text)

    def test_physics_general_grades_remain_functional(self):
        _, probe = self.page("/subjects/physics")
        for grade in (8, 9, 10):
            self.assertIn(f"/subjects/physics/{grade}", probe.hrefs())
            _, grade_page = self.page(f"/subjects/physics/{grade}")
            self.assertIn(f"{grade}. клас", grade_page.text)
            response = self.client.get(f"/physics/grade/{grade}")
            self.assertEqual(response.status_code, 301)

    def test_it_keeps_all_grades_and_marks_the_profiled_ones(self):
        html, probe = self.page("/subjects/information-technologies")
        for grade in PLATFORM_GRADES:
            self.assertIn(f"/subjects/information-technologies/{grade}", probe.hrefs())
            self.page(f"/grade/{grade}")
            self.page(f"/subjects/information-technologies/{grade}")
        self.assertEqual(html.count('class="subject-grade-path"'), 2)
        cards = self.cards("/subjects/information-technologies")
        for grade in (11, 12):
            classes, href, inner = cards[grade]
            self.assertIn("subject-class-card--profiled", classes)
            self.assertEqual(href, f"/subjects/information-technologies/{grade}")
            self.assertIn("Профилирана подготовка", inner)
        self.assertNotIn(NOTE_CLASS, html)
        self.assertIn("Съдържанието вече е налично", probe.text)
        self.page("/section/grade12-dzi-preparation")
        self.page("/dzi", 302)

    def test_pages_follow_metadata_for_any_subject(self):
        # Grade 9: both paths, profiled supported, framework-plan dependent.
        # Grade 10: profiled and supported. Grade 12: profiled, outside scope.
        with registered_subject(ODD):
            cards = self.cards("/subjects/oddology")
            self.assertEqual(tuple(cards), (9, 10))
            for grade in (9, 10):
                self.assertIn("subject-class-card--profiled", cards[grade][0])
                self.assertIn("Профилирана подготовка", cards[grade][2])
            self.assertIn("Изучава се в зависимост от учебния план", cards[9][2])
            self.assertNotIn("Изучава се в зависимост от учебния план", cards[10][2])
            _, probe = self.page("/subjects/oddology")
            self.assertIn("12. клас — профилирана подготовка", probe.text)
            for grade in (8, 11, 12):
                self.assertNotIn(f"/subjects/oddology/{grade}", probe.hrefs())
                self.page(f"/subjects/oddology/{grade}", 404)
            self.page("/subjects/oddology/9")
        self.page("/subjects/oddology", 404)

    def test_no_styling_or_branching_by_grade_number(self):
        css = (ROOT / "web" / "static" / "css" / "ui-pass9.css").read_text(encoding="utf-8")
        blocks = re.findall(r"[^{}]*subject-(?:class-card--(?:general|profiled)|grade-)[^{}]*\{", css)
        self.assertTrue(blocks)
        for selector in blocks:
            self.assertIsNone(re.search(r"\d", selector), selector)
        self.assertIn("--subject-path-color: var(--color-primary)", css)
        self.assertIn("--subject-path-color: var(--color-success)", css)
        template = (ROOT / "web" / "templates" / "subject.html").read_text(encoding="utf-8")
        for pattern in (r"grade\s*(?:>=|<=|>|<|==)\s*\d", r"grade\s+in\s+[\[(]\s*\d"):
            self.assertIsNone(re.search(pattern, template), pattern)

    def test_generic_templates_name_no_subject(self):
        templates = ROOT / "web" / "templates"
        names = ("grade.html", "subject.html", "subject_grade.html", "subject_section.html",
                 "subject_topic.html", "_subject_states.html")
        identifiers = {value for subject in SUBJECTS for value in (subject.id, subject.route_slug)}
        identifiers.discard("it")  # too short to search for; covered by the slug
        for name in names:
            source = (templates / name).read_text(encoding="utf-8")
            for identifier in identifiers:
                with self.subTest(template=name, identifier=identifier):
                    self.assertNotIn(identifier, source)


class GradeOverviewPagesTest(IsolatedAppTestCase):
    """/grade/<n>: the subjects LearnPilot supports in a grade, from the registry."""

    def setUp(self):
        self.client = web_app.app.test_client()

    def page(self, path, status=200):
        response = self.client.get(path)
        self.assertEqual(response.status_code, status, path)
        html = response.get_data(as_text=True)
        return html, _HTMLProbe(html)

    def cards(self, grade):
        """Subject cards on a grade page: href -> (classes, inner html)."""
        html, _ = self.page(f"/grade/{grade}")
        return {href: (set(classes.split()), inner) for classes, href, inner in CARD.findall(html)}

    def test_subjects_come_from_the_registry_for_every_grade(self):
        for grade in PLATFORM_GRADES:
            expected = [subject for subject in SUBJECTS if grade in subject.grades]
            cards = self.cards(grade)
            with self.subTest(grade=grade):
                self.assertEqual(
                    list(cards), [f"/subjects/{subject.route_slug}/{grade}" for subject in expected])
                for subject in expected:
                    classes, inner = cards[f"/subjects/{subject.route_slug}/{grade}"]
                    path = subject.supported_path(grade)
                    self.assertIn(subject.title, inner)
                    self.assertIn(f"subject-class-card--{path}", classes)
                    self.assertEqual("Профилирана подготовка" in inner, path == PROFILED)
                    self.assertEqual(
                        "Изучава се в зависимост от учебния план" in inner,
                        grade in subject.framework_plan_dependent_grades,
                    )
                    self.page(f"/subjects/{subject.route_slug}/{grade}")

    def test_grade_8_is_not_an_it_page(self):
        html, probe = self.page("/grade/8")
        cards = self.cards(8)
        self.assertEqual(len(cards), 6)
        self.assertIn("/subjects/physics/8", cards)
        self.assertNotIn("/section/", " ".join(probe.hrefs()))
        self.assertNotIn(NOTE_CLASS, html)
        flagged = [href for href, (_, inner) in cards.items()
                   if "Изучава се в зависимост от учебния план" in inner]
        self.assertEqual(flagged, [f"/subjects/{slug}/8"
                                   for slug in ("physics", "informatics", "biology", "chemistry")])

    def test_supported_subjects_without_content_are_listed_as_in_preparation(self):
        conn = web_app.quiz_db()
        try:
            from src.subject_browsing import subject_content_grades
            content = subject_content_grades(conn)
        finally:
            conn.close()
        for grade in PLATFORM_GRADES:
            for href, (classes, inner) in self.cards(grade).items():
                subject = next(s for s in SUBJECTS if href == f"/subjects/{s.route_slug}/{grade}")
                has_content = grade in content.get(subject.id, ())
                with self.subTest(grade=grade, subject=subject.id):
                    self.assertEqual("subject-class-card--active" in classes, has_content)
                    if not has_content:
                        self.assertIn('<span class="subject-status-badge">В подготовка</span>', inner)
        self.assertIn("В подготовка", self.cards(8)["/subjects/mathematics/8"][1])

    def test_grades_9_and_10_do_not_show_informatics(self):
        for grade in (9, 10):
            cards = self.cards(grade)
            self.assertNotIn(f"/subjects/informatics/{grade}", cards)
            self.assertEqual(len(cards), 5)
            self.assertNotIn("Информатика", self.page(f"/grade/{grade}")[1].text.split("Избери предмет")[1])

    def test_upper_grades_separate_supported_and_out_of_scope_paths(self):
        for grade in (11, 12):
            cards = self.cards(grade)
            self.assertEqual(list(cards), [f"/subjects/information-technologies/{grade}",
                                           f"/subjects/mathematics/{grade}"])
            self.assertIn("subject-class-card--profiled", cards[f"/subjects/information-technologies/{grade}"][0])
            self.assertIn("subject-class-card--general", cards[f"/subjects/mathematics/{grade}"][0])
            html, probe = self.page(f"/grade/{grade}")
            note = re.search(rf'<aside class="{NOTE_CLASS}".*?</aside>', html, re.S).group(0)
            self.assertIsNone(re.search(r"<a[\s>]", note))
            for title in ("Физика", "Информатика", "Биология", "Математика", "Химия"):
                self.assertIn(title, note)
            self.assertNotIn("Информационни технологии", note)
            self.assertIn("профилирана подготовка", note)
            for slug in ("physics", "informatics", "biology", "chemistry"):
                self.assertNotIn(f"/subjects/{slug}/{grade}", probe.hrefs())

    def test_grade_number_block_only_where_cards_are_grades(self):
        # /grade/<n>: every card is the same grade, so the number is not repeated.
        for grade in PLATFORM_GRADES:
            html, _ = self.page(f"/grade/{grade}")
            self.assertTrue(CARD.findall(html))
            self.assertNotIn("subject-class-number", html)
        # /subjects/<subject>: cards are different grades and keep their number.
        for subject in SUBJECTS:
            html, _ = self.page(f"/subjects/{subject.route_slug}")
            self.assertEqual(html.count('class="subject-class-number"'), len(subject.grades))

    def test_grade_cards_use_concise_copy(self):
        for grade in PLATFORM_GRADES:
            for href, (classes, inner) in self.cards(grade).items():
                text = _HTMLProbe(inner).text
                with self.subTest(href=href):
                    self.assertNotIn(f"{grade}. клас", text)
                    expected = ("Раздели, въпроси и тестове" if "subject-class-card--active" in classes
                                else "Разделите и въпросите се подготвят.")
                    if "Въпроси в подготовка" not in text:
                        self.assertIn(expected, text)

    def test_card_grids_are_intrinsic(self):
        css = (ROOT / "web" / "static" / "css" / "ui-pass9.css").read_text(encoding="utf-8")
        css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
        rule = re.search(
            r"\.cards\.subject-class-grid,\s*\.cards\.subject-topic-group-grid,\s*\.cards\.subject-placeholder-grid \{([^}]*)\}", css)
        self.assertIsNotNone(rule)
        self.assertRegex(rule.group(1), r"repeat\(auto-fill, minmax\(min\(100%, [\d.]+rem\), 1fr\)\)")
        # No other rule may give these grids a column count, a count-based or
        # positional selector, or a per-grade/per-subject variant.
        selectors = re.findall(r"([^{}]*subject-(?:class|topic-group|placeholder)-(?:grid|card)[^{}]*)\{([^}]*)\}", css)
        columns = [body for selector, body in selectors if "grid-template-columns" in body]
        self.assertEqual(len(columns), 1)
        for selector, body in selectors:
            self.assertNotRegex(selector, r"nth-|grade-?\d|cards-\d")
            self.assertNotRegex(body, r"(?<!min-)(?<!max-)\bheight:\s*\d|overflow(?:-x)?:\s*hidden")
        badge = re.search(r"\.subject-status-badge \{[^}]*max-width: 100%[^}]*\}", css)
        self.assertIsNotNone(badge)
        self.assertIn("white-space: normal", badge.group(0))
        self.assertNotRegex(css, r"body\s*\{[^}]*overflow-x:\s*hidden")

    def test_arbitrary_breaking_is_scoped_to_machine_text(self):
        css = (ROOT / "web" / "static" / "css" / "ui-pass9.css").read_text(encoding="utf-8")
        css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
        rules = re.findall(r"([^{}]+)\{([^{}]*)\}", css)
        aggressive = [selector.strip() for selector, body in rules
                      if re.search(r"overflow-wrap:\s*(?:anywhere|break-word)|word-break:\s*(?:break-all|break-word)", body)]
        self.assertEqual(aggressive, [".machine-text,\n.practical-task-source"])
        for name in ("grade.html", "subject.html", "subject_grade.html", "_subject_states.html", "_section_toolbar.html"):
            self.assertNotIn("machine-text", (ROOT / "web" / "templates" / name).read_text(encoding="utf-8"))

    def test_only_platform_grades_have_an_overview(self):
        for grade in (0, 7, 13):
            self.page(f"/grade/{grade}", 404)

    def test_overview_follows_metadata_for_any_subject(self):
        with registered_subject(ODD):
            for grade, shown in ((8, False), (9, True), (10, True), (12, False)):
                self.assertEqual(f"/subjects/oddology/{grade}" in self.cards(grade), shown, grade)
            classes, inner = self.cards(9)["/subjects/oddology/9"]
            self.assertIn("subject-class-card--profiled", classes)
            self.assertIn("Изучава се в зависимост от учебния план", inner)
            note = re.search(rf'<aside class="{NOTE_CLASS}".*?</aside>', self.page("/grade/12")[0], re.S)
            self.assertIn("Одология", note.group(0))
        self.assertNotIn("/subjects/oddology/9", self.cards(9))

    def test_subject_grade_page_explains_framework_plan_dependence(self):
        for subject in SUBJECTS:
            for grade in subject.grades:
                _, probe = self.page(f"/subjects/{subject.route_slug}/{grade}")
                expected = grade in subject.framework_plan_dependent_grades
                with self.subTest(subject=subject.id, grade=grade):
                    self.assertEqual(
                        f"Предметът се изучава в {grade}. клас само при определени учебни планове."
                        in probe.text, expected)

    def test_green_means_profiled_only(self):
        css = (ROOT / "web" / "static" / "css" / "ui-pass9.css").read_text(encoding="utf-8")
        profiled = ("subject-class-card--profiled", "subject-grade-path", "subject-grade-status")
        selector = ""
        for line in css.splitlines():
            if "{" in line:
                selector = line
            if "color-success" in line and "subject-" in selector:
                self.assertTrue(any(name in selector for name in profiled), selector)
        # Question-count and state badges are neutral, whatever the content.
        badge = re.search(r"\.subject-pill,\s*\.subject-status-badge \{[^}]*\}", css).group(0)
        self.assertNotIn("color-success", badge)
        self.assertNotIn("subject-path", badge)
        for name in ("grade.html", "subject.html"):
            template = (ROOT / "web" / "templates" / name).read_text(encoding="utf-8")
            for pattern in (r"grade\s*(?:>=|<=|>|<|==)\s*\d", r"grade\s+in\s+[\[(]\s*\d",
                            r"question_count[^%]*%\}[^{]*profiled", "Зависи от учебния план"):
                self.assertIsNone(re.search(pattern, template), (name, pattern))
            for subject in SUBJECTS:
                if subject.id != "it":
                    self.assertNotIn(subject.id, template)
            self.assertNotIn("information-technologies", template)


if __name__ == "__main__":
    unittest.main()
