"""Assessment motion is decorative and isolated from feature state."""

from pathlib import Path

from tests.auth_helpers import sign_in_admin
from tests.support import IsolatedAppTestCase, web_app
from tests.test_homepage_search import _HTMLProbe


ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "web" / "templates"
STATIC = ROOT / "web" / "static"


class AssessmentMotionTest(IsolatedAppTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.app = web_app.app
        cls.app.config.update(TESTING=True)

    def setUp(self):
        self.client = self.app.test_client()

    def test_feedback_hooks_are_only_on_safe_inner_content(self):
        expected = {
            "quiz_start.html": '<h1 data-motion="feedback">',
            "quiz_result.html": '<span class="result-score-label" data-motion="feedback">',
            "teacher_dashboard.html": '<h1 data-motion="feedback">',
        }
        for name, hook in expected.items():
            with self.subTest(name=name):
                html = (TEMPLATES / name).read_text(encoding="utf-8")
                self.assertIn(hook, html)
                self.assertEqual(html.count('data-motion="feedback"'), 1)
                self.assertNotRegex(html, r'<(?:section|form|article)[^>]*data-motion=')

    def test_no_question_image_or_assessment_controller_owns_motion(self):
        for name in ("quiz_attempt.html", "_question_visuals.html"):
            self.assertNotIn("data-motion", (TEMPLATES / name).read_text(encoding="utf-8"))
        for name in ("quiz-draft-autosave.js", "quiz-progress-indicator.js"):
            self.assertNotIn("data-motion", (STATIC / "js" / name).read_text(encoding="utf-8"))
        attempt = (TEMPLATES / "quiz_attempt.html").read_text(encoding="utf-8")
        self.assertIn('<progress', attempt)
        self.assertIn('type="radio"', attempt)
        self.assertIn('form.submit();', attempt)

    def test_management_forms_tables_and_practical_context_stay_static(self):
        names = (
            "teacher_new.html", "teacher_assignment.html", "teacher_assignments.html",
            "teacher_results.html", "teacher_practical_submissions.html",
            "open_question_candidates.html", "practical_tasks.html",
            "dzi_index.html", "dzi_source.html",
        )
        for name in names:
            with self.subTest(name=name):
                html = (TEMPLATES / name).read_text(encoding="utf-8")
                self.assertNotIn("data-motion", html)
        practical = (TEMPLATES / "practical_tasks.html").read_text(encoding="utf-8")
        self.assertIn("{% if not (attempt is defined and attempt) %}", practical)

    def test_teacher_dashboard_hook_is_scoped_and_tables_are_static(self):
        sign_in_admin(self.client, web_app.app.config["DB_PATH"])
        response = self.client.get("/teacher")
        self.assertEqual(response.status_code, 200)
        page = _HTMLProbe(response.get_data(as_text=True))
        self.assertIn("teacher-management-page", page.first("body")["class"].split())
        self.assertEqual(
            [(tag, attrs.get("data-motion")) for tag, attrs in page.elements() if "data-motion" in attrs],
            [("h1", "feedback")],
        )
        self.assertFalse(any("data-motion" in attrs for _, attrs in page.elements("tr")))

    def test_reduced_motion_and_local_dependency_contract(self):
        css = (STATIC / "css" / "motion.css").read_text(encoding="utf-8")
        js = (STATIC / "js" / "motion.js").read_text(encoding="utf-8")
        self.assertIn(".student-assessment-attempt-page .quiz-option-letter", css)
        self.assertIn("transition-duration: 0ms", css.split("@media (prefers-reduced-motion: reduce)", 1)[1])
        self.assertIn("if (!canRunFirstPaintMotion()) return", js)
        self.assertIn('student-assessment-result-page .result-score-label[data-motion="feedback"]', js)
        self.assertNotIn("requestAnimationFrame", js)
        self.assertNotIn('addEventListener("scroll"', js)
        for dependency in ("gsap", "ScrollTrigger", "Lenis", "Three", "Rive"):
            self.assertNotIn(dependency, js)


if __name__ == "__main__":
    import unittest

    unittest.main()
