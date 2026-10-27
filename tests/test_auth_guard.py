import os
import re
import unittest
import warnings
from pathlib import Path
from unittest.mock import patch


_ROOT = Path(__file__).resolve().parents[1]

from tests.auth_helpers import (
    HISTORICAL_SHARED_LOGIN_SESSION,
    create_account,
    sign_in_admin,
    sign_in_teacher,
)
from tests.support import IsolatedAppTestCase, web_app
from tests.support import NO_DEADLINE_FORM

# A synthetic account the same-origin tests sign in with through the real form.
LOGIN_EMAIL = "guard-teacher@example.bg"
LOGIN_PASSWORD = "correct horse battery"


def _register_dzi_default_denied_test_route(app):
    endpoint = "dzi_test_only_default_denied"
    if endpoint in app.view_functions:
        return

    def view():
        return "test-only DZI endpoint"

    app.add_url_rule("/__test__/dzi-default-denied", endpoint=endpoint, view_func=view)


class AuthGuardTest(IsolatedAppTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.app = web_app.app
        _register_dzi_default_denied_test_route(cls.app)
        cls.app.config.update(TESTING=True)
        conn = web_app.quiz_db()
        try:
            cls.section = cls._first_eligible_section(conn)
            cls.dzi_source_slug = cls._first_dzi_source_slug(conn)
        finally:
            conn.close()
        cls.db_path = cls.app.config["DB_PATH"]
        create_account(cls.db_path, "teacher", email=LOGIN_EMAIL, password=LOGIN_PASSWORD)

    @staticmethod
    def _first_eligible_section(conn):
        rows = conn.execute("""
            SELECT id, section_slug
            FROM curriculum_sections
            ORDER BY id
        """).fetchall()
        for row in rows:
            if web_app.quiz_section_question_ids(conn, int(row["id"])):
                return row
        raise AssertionError("No section with eligible quiz questions found")

    @staticmethod
    def _first_dzi_source_slug(conn):
        row = conn.execute("""
            SELECT year, session, variant
            FROM exams
            WHERE format_version = ?
              AND year = 2025
              AND session = 'may'
              AND variant = 2
            LIMIT 1
        """, (web_app.DZI_FORMAT_VERSION,)).fetchone()
        if row is not None:
            return web_app.dzi_source_slug(row)

        row = conn.execute("""
            SELECT year, session, variant
            FROM exams
            WHERE format_version = ?
            ORDER BY year DESC, session, variant
            LIMIT 1
        """, (web_app.DZI_FORMAT_VERSION,)).fetchone()
        if row is None:
            return "may_2025_v1"
        return web_app.dzi_source_slug(row)

    def setUp(self):
        self.client = self.app.test_client()

    def test_public_gets_and_guard_redirects_do_not_create_session_cookies(self):
        for path in ("/", "/subjects/information-technologies", "/healthz",
                     "/teacher", "/teacher/new"):
            with self.subTest(path=path):
                response = self.app.test_client().get(path)
                self.assertNotIn("Set-Cookie", response.headers)

    def test_signed_in_navigation_does_not_rewrite_the_cookie(self):
        for sign_in in (sign_in_teacher, sign_in_admin):
            with self.subTest(sign_in=sign_in.__name__):
                client = self.app.test_client()
                sign_in(client, self.db_path)
                with client.session_transaction() as sess:
                    before = dict(sess)
                for path in ("/", "/grade/8", "/teacher/new"):
                    self.assertNotIn("Set-Cookie", client.get(path).headers)
                with client.session_transaction() as sess:
                    self.assertEqual(dict(sess), before)
                    self.assertEqual(set(sess), {"uid", "sk", "iat"})

    def test_historical_session_keys_are_neither_used_nor_rewritten(self):
        with self.client.session_transaction() as sess:
            sess.update(HISTORICAL_SHARED_LOGIN_SESSION)
        for path in ("/", "/teacher", "/teacher/new"):
            self.assertNotIn("Set-Cookie", self.client.get(path).headers)
        with self.client.session_transaction() as sess:
            self.assertEqual(dict(sess), HISTORICAL_SHARED_LOGIN_SESSION)

    def test_cookieless_external_entry_does_not_clobber_attempt_cookie(self):
        owner = self.app.test_client()
        with owner.session_transaction() as sess:
            sess[web_app.quiz_attempt_owner_session_key(123)] = True
        external_entry = self.app.test_client().get("/", headers={"Referer": "https://school.example/"})
        self.assertNotIn("Set-Cookie", external_entry.headers)
        with owner.session_transaction() as sess:
            self.assertTrue(sess[web_app.quiz_attempt_owner_session_key(123)])

    def _login_teacher(self):
        return sign_in_teacher(self.client, self.db_path)

    def _login_admin(self):
        return sign_in_admin(self.client, self.db_path)

    def _post_login(self, path="/login", password=LOGIN_PASSWORD, **kwargs):
        return self.client.post(path, data={"email": LOGIN_EMAIL, "password": password}, **kwargs)

    def _create_assignment_as_teacher(self):
        self._login_teacher()
        response = self.client.post("/teacher/new", data={**NO_DEADLINE_FORM,
            "section_id": str(self.section["id"]),
            "question_count": "1",
            "time_limit_minutes": "",
        })
        self.assertEqual(response.status_code, 302)
        location = response.headers["Location"]
        self.assertIn("/teacher/assignment/", location)
        return location

    def _assert_single_current_nav_link(self, body: str, href: str):
        nav_match = re.search(
            r'<nav\b(?=[^>]*class="[^"]*\bnav-links\b)[^>]*>(.*?)</nav>',
            body,
            flags=re.DOTALL,
        )
        self.assertIsNotNone(nav_match)
        nav = nav_match.group(1)
        current_links = [
            tag
            for tag in re.findall(r"<a\b[^>]*>", nav)
            if 'aria-current="page"' in tag
        ]

        self.assertEqual(len(current_links), 1)
        self.assertIn(f'href="{href}"', current_links[0])
        self.assertNotIn('<span class="nav-current"', nav)

    def _mobile_profile_menu(self, body: str) -> str:
        match = re.search(
            r'<details\b(?=[^>]*class="[^"]*\bmobile-profile-menu\b)[^>]*>(.*?)</details>',
            body,
            flags=re.DOTALL,
        )
        self.assertIsNotNone(match)
        menu = match.group(1)
        self.assertRegex(menu, r"<summary\b[^>]*>[^<]+</summary>")
        return menu

    def _assignment_count(self):
        conn = web_app.quiz_db()
        try:
            return conn.execute("SELECT COUNT(*) FROM quiz_assignments").fetchone()[0]
        finally:
            conn.close()

    def test_sessionless_access_is_redirected_to_login(self):
        for path in (
            "/teacher/new",
            "/teacher",
            "/teacher/dzi-training",
            "/dzi",
            f"/dzi/source/{self.dzi_source_slug}",
            "/teacher/assignments",
        ):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 302, path)
            self.assertEqual(response.headers["Location"], f"/login?next={path}", path)

    def test_teacher_account_uses_the_assignment_flow_and_no_admin_page(self):
        response = self.client.get(f"/teacher/new?section={self.section['section_slug']}")
        self.assertEqual(response.status_code, 302)

        self._login_teacher()
        response = self.client.get(f"/teacher/new?section={self.section['section_slug']}")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'id="new-assignment-form"', response.data)
        self.assertIn(
            b'<body class="management-page teacher-management-page management-form-page">',
            response.data,
        )

        assignment_url = self._create_assignment_as_teacher()

        forbidden_paths = (
            "/teacher",
            "/teacher/dzi-training",
            "/dzi",
            f"/dzi/source/{self.dzi_source_slug}",
        )
        for path in forbidden_paths:
            response = self.client.get(path)
            self.assertEqual(response.status_code, 403, path)
            self.assertNotIn("Location", response.headers)

        # The teacher manages the assignment it created (Accounts PR7) ...
        for path in (assignment_url, "/teacher/assignments", f"{assignment_url}/results",
                     f"{assignment_url}/results.csv"):
            self.assertEqual(self.client.get(path).status_code, 200, path)

        # ... and another teacher account gets 404 for it, not a hint that it exists.
        other = self.app.test_client()
        sign_in_teacher(other, self.db_path)
        for path in (assignment_url, f"{assignment_url}/results", f"{assignment_url}/results.csv"):
            self.assertEqual(other.get(path).status_code, 404, path)

    def test_admin_can_access_teacher_and_dzi_pages(self):
        assignment_url = self._create_assignment_as_teacher()
        self.client = self.app.test_client()
        self._login_admin()

        paths = (
            "/teacher",
            "/teacher/assignments",
            "/teacher/dzi-training",
            assignment_url,
            f"{assignment_url}/results",
            "/dzi",
            f"/dzi/source/{self.dzi_source_slug}",
        )
        for path in paths:
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200, path)

    def test_teacher_dashboard_groups_recent_tests_by_class_with_teacher_fallback(self):
        assignment_url = self._create_assignment_as_teacher()
        assignment_id = assignment_url.rstrip("/").split("/")[-1]
        self.client = self.app.test_client()
        self._login_admin()

        response = self.client.get("/teacher")

        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("Последни тестове", body)
        self.assertIn("assignment-groups", body)
        self.assertIn(f'href="/teacher/assignment/{assignment_id}"', body)
        self.assertIn(f'href="/teacher/assignment/{assignment_id}/results"', body)
        self.assertIn("клас", body)
        self.assertIn("Учител: не е посочен", body)

    def test_recent_test_cards_have_primary_clickable_link_and_result_link(self):
        assignment_url = self._create_assignment_as_teacher()
        assignment_id = assignment_url.rstrip("/").split("/")[-1]
        self.client = self.app.test_client()
        self._login_admin()

        response = self.client.get("/teacher")

        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn('class="assignment-mini-card"', body)
        self.assertIn('class="assignment-mini-title stretched-link"', body)
        self.assertIn(f'href="/teacher/assignment/{assignment_id}"', body)
        self.assertIn(f'href="/teacher/assignment/{assignment_id}/results"', body)

    def test_grade_subject_cards_are_whole_card_links(self):
        response = self.client.get(f"/grade/{self.section['class'] if 'class' in self.section.keys() else 8}")
        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertRegex(body, r'<a class="card subject-class-card[^"]*"\s+href="/subjects/')

    def test_current_grade_nav_item_is_link_with_aria_current(self):
        self._login_teacher()
        response = self.client.get("/grade/8")
        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)

        self._assert_single_current_nav_link(body, "/grade/8")

    def test_current_home_nav_item_is_link_with_aria_current(self):
        self._login_teacher()
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)

        self._assert_single_current_nav_link(body, "/")

    def test_authenticated_nav_groups_classes_and_subjects(self):
        self._login_teacher()
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        nav_match = re.search(
            r'<nav\b(?=[^>]*class="[^"]*\bnav-links\b)[^>]*>(.*?)</nav>',
            body,
            flags=re.DOTALL,
        )
        self.assertIsNotNone(nav_match)
        nav = nav_match.group(1)

        self.assertIn(">Начало</a>", nav)
        self.assertIn("Класове", nav)
        self.assertIn("Предмети", nav)
        self.assertIn("ДЗИ", nav)
        for grade in (8, 9, 10, 11, 12):
            self.assertIn(f'href="/grade/{grade}"', nav)
            self.assertIn(f">{grade}. клас</a>", nav)
            self.assertNotRegex(
                nav,
                rf'<a\b(?=[^>]*class="[^"]*\bnav-link\b)(?=[^>]*href="/grade/{grade}")',
            )
        self.assertIn("Информационни технологии", nav)
        self.assertIn('href="/subjects/information-technologies"', nav)
        self.assertIn('href="/subjects/physics"', nav)
        self.assertIn("Физика", nav)
        for slug, subject in (
            ("informatics", "Информатика"),
            ("biology", "Биология"),
            ("mathematics", "Математика"),
            ("chemistry", "Химия"),
        ):
            self.assertIn(subject, nav)
            self.assertIn(f'href="/subjects/{slug}"', nav)
        self.assertIn("В подготовка", nav)
        self.assertNotIn('aria-disabled="true"', nav)

    def test_nav_dropdowns_use_single_explicit_open_state(self):
        self._login_teacher()
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        nav = re.search(
            r'<nav\b(?=[^>]*class="[^"]*\bnav-links\b)[^>]*>(.*?)</nav>',
            body,
            flags=re.DOTALL,
        ).group(1)
        css = (_ROOT / "web" / "static" / "css" / "ui-pass9.css").read_text(encoding="utf-8")

        self.assertNotIn('<details class="nav-menu"', nav)
        self.assertIn('aria-expanded="false"', nav)
        self.assertIn('aria-controls="nav-menu-classes"', nav)
        self.assertIn('aria-controls="nav-menu-subjects"', nav)
        self.assertIn('id="nav-menu-classes" hidden', nav)
        self.assertIn('id="nav-menu-subjects" hidden', nav)
        self.assertIn('classList.toggle("is-open"', body)
        self.assertIn('setMenuOpen(menu, false)', body)
        self.assertIn("panel.hidden = !isOpen", body)
        self.assertIn('document.addEventListener("click"', body)
        self.assertIn('event.key === "Escape"', body)
        self.assertNotIn(".nav-menu[open]", css)
        self.assertRegex(css, r"\.nav-menu-panel\s*\{[^}]*display:\s*none")
        self.assertRegex(css, r"\.nav-menu\.is-open \.nav-menu-panel:not\(\[hidden\]\)\s*\{[^}]*display:\s*grid")

    def test_opening_or_focusing_a_menu_cannot_widen_the_header(self):
        # Regression (PR #350): an open or focused menu switched .nav-links to
        # overflow: visible. The grid item then stopped being a scroll container, its
        # min-content width (every link) became the minimum of the 1fr track, and the
        # account controls were pushed past the header's right edge.
        css = (_ROOT / "web" / "static" / "css" / "ui-pass9.css").read_text(encoding="utf-8")
        columns = re.findall(r"\.top-nav-row\s*\{[^}]*?grid-template-columns:\s*([^;]+);", css)
        self.assertTrue(columns)
        for value in columns:
            self.assertIn("minmax(0, 1fr)", value)
        # No menu state changes the links row's overflow, and with it the row's width.
        self.assertNotRegex(css, r"\.nav-links:(?:focus-within|has\()")

        start = css.index("@media (max-width: 768px) {")
        depth = 0
        for end in range(css.index("{", start), len(css)):
            depth += {"{": 1, "}": -1}.get(css[end], 0)
            if depth == 0:
                break
        mobile = css[start:end]
        # Narrow screens keep scrolling the row; its panels are fixed, so it never clips them.
        self.assertRegex(mobile, r"\.nav-links\s*\{\s*overflow-x:\s*auto")
        self.assertRegex(mobile, r"\.nav-menu-panel\s*\{\s*position:\s*fixed")

    def test_offsets_below_the_header_use_its_measured_height(self):
        # Source text only; tests.test_navigation_browser measures the layout itself. The
        # header wraps onto more rows at some widths, so nothing positions itself below it
        # with the fixed one-row token.
        css = (_ROOT / "web" / "static" / "css" / "ui-pass9.css").read_text(encoding="utf-8")
        self.assertRegex(css, r":root\s*\{\s*--top-nav-height:\s*var\(--header-height\);\s*\}")
        offsets = re.findall(r"(?:top|scroll-padding-top):\s*calc\([^;]*--(?:header|top-nav)-height[^;]*\);", css)
        self.assertEqual(len(offsets), 4)
        for offset in offsets:
            self.assertIn("var(--top-nav-height)", offset)
        body = self.app.test_client().get("/").get_data(as_text=True)
        self.assertIn("new ResizeObserver(", body)
        self.assertIn('"--top-nav-height", header.getBoundingClientRect().height + "px"', body)
        # Unmeasured (no JavaScript), the script-only sticky tools stay in the flow, and so
        # does the header wherever its links can wrap.
        noscript = re.search(r"<noscript>\s*<style>(.*?)</style>", body, re.S).group(1)
        self.assertRegex(noscript, r"\.section-toolbar,\s*\.quiz-progress\s*\{\s*position:\s*static;\s*\}")
        self.assertRegex(noscript, r"@media \(min-width: 769px\) \{ \.top-nav \{ position: relative; \} \}")
        # Menus inside the sticky header end above the viewport's bottom edge and scroll.
        panels = re.search(r"\.nav-menu-panel,\s*\.mobile-profile-panel\s*\{([^}]*max-height[^}]*)\}",
                           re.sub(r"/\*.*?\*/", "", css, flags=re.S)).group(1)
        self.assertIn("max-height: calc(100dvh - var(--top-nav-height)", panels)
        self.assertIn("overflow-y: auto", panels)

    def test_signed_in_account_controls_are_one_compact_menu(self):
        name = "Александра Константинова-Димитрова Петрова"
        client = self.app.test_client()
        sign_in_admin(client, self.db_path, display_name=name)
        body = client.get("/").data.decode("utf-8")
        menu = self._mobile_profile_menu(body)

        # The inline row of name, role and three links no longer competes with the links.
        self.assertNotIn('class="profile-switch"', body)
        self.assertNotIn("mobile-profile-menu--signed-out", body)
        self.assertIn(f'<span class="profile-label">{name}</span>', menu)
        self.assertIn('<span class="profile-link active">Админ</span>', menu)
        self.assertIn('<a class="profile-link" href="/account">Профил</a>', menu)
        self.assertIn('href="/account/password"', menu)
        self.assertIn('<form class="profile-logout-form" method="post" action="/logout">', menu)

        anonymous = self.app.test_client().get("/").data.decode("utf-8")
        self.assertRegex(anonymous, r'<div class="profile-switch"[^>]*>\s*'
                                    r'<a class="profile-link" href="/login\?next=/">Вход</a>')
        self.assertIn('<details class="mobile-profile-menu mobile-profile-menu--signed-out">', anonymous)

    def test_current_dzi_nav_item_is_link_with_aria_current(self):
        self._login_teacher()
        response = self.client.get("/section/grade12-dzi-preparation")
        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)

        self._assert_single_current_nav_link(body, "/section/grade12-dzi-preparation")

    def test_teacher_assignments_list_shows_class_and_teacher_fallback(self):
        assignment_url = self._create_assignment_as_teacher()
        assignment_id = assignment_url.rstrip("/").split("/")[-1]
        self.client = self.app.test_client()
        self._login_admin()

        response = self.client.get("/teacher/assignments")

        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn(f'href="/teacher/assignment/{assignment_id}"', body)
        self.assertIn(f'href="/teacher/assignment/{assignment_id}/results"', body)
        self.assertIn("клас", body)
        self.assertIn("Учител: не е посочен", body)

    def test_future_undeclared_dzi_endpoint_is_denied_by_default(self):
        # The test-only view declares no access policy, so explicit enforcement
        # denies it to every session, admin included (roadmap PR D2).
        path = "/__test__/dzi-default-denied"

        response = self.client.get(path)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], f"/login?next={path}")

        for login in (self._login_teacher, self._login_admin):
            self.client = self.app.test_client()
            login()
            response = self.client.get(path)
            self.assertEqual(response.status_code, 403)
            self.assertNotEqual(response.data, b"test-only DZI endpoint")

    def test_login_rejects_external_next_after_successful_login(self):
        for next_url in ("//evil.example/login", "https://evil.example/login"):
            with self.subTest(next_url=next_url):
                self.client = self.app.test_client()
                response = self._post_login(f"/login?next={next_url}")
                self.assertEqual(response.status_code, 302)
                self.assertEqual(response.headers["Location"], "/teacher/new")

    def test_login_accepts_normal_local_next_after_successful_login(self):
        response = self._post_login("/login?next=/teacher/assignments")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/teacher/assignments")

    def test_denied_request_ignores_an_external_referrer(self):
        response = self.client.get(
            "/teacher", headers={"Referer": "https://evil.example/from-referrer"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/login?next=/teacher")

    def test_mobile_profile_menu_reflects_teacher_account(self):
        self._login_teacher()
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        body = response.data.decode("utf-8")
        menu = self._mobile_profile_menu(body)

        self.assertIn("Учител", menu)
        self.assertIn('method="post" action="/logout"', menu)
        self.assertIn('href="/account/password"', menu)
        for removed in ("/tester/logout", "/admin/logout", "/admin/login", "/tester/login", "Тестер"):
            self.assertNotIn(removed, body)

    def test_mobile_profile_menu_reflects_admin_account(self):
        self._login_admin()
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        body = response.data.decode("utf-8")
        menu = self._mobile_profile_menu(body)

        self.assertIn("Админ", menu)
        self.assertIn('method="post" action="/logout"', menu)
        self.assertNotIn("/admin/logout", body)

    def test_mobile_profile_menu_offers_only_the_account_login_to_anonymous(self):
        body = self.client.get("/").data.decode("utf-8")
        menu = self._mobile_profile_menu(body)

        self.assertIn('href="/login?next=/"', menu)
        for removed in ("/admin/login", "/tester/login", 'action="/logout"'):
            self.assertNotIn(removed, menu)

    def test_login_with_cyrillic_wrong_password_returns_normal_error(self):
        response = self._post_login(password="грешна-парола")

        self.assertEqual(response.status_code, 200)
        self.assertIn("Грешен имейл или парола.".encode("utf-8"), response.data)
        self.assertNotIn("грешна-парола".encode("utf-8"), response.data)

    def test_cross_origin_post_to_login_is_rejected(self):
        response = self._post_login(headers={"Origin": "http://evil.example"})

        self.assertEqual(response.status_code, 403)
        with self.client.session_transaction() as sess:
            self.assertEqual(dict(sess), {})

    def test_scheme_mismatch_post_to_login_is_rejected(self):
        response = self._post_login(headers={"Origin": "https://localhost"})

        self.assertEqual(response.status_code, 403)

    def test_same_origin_post_to_login_still_works(self):
        response = self._post_login(
            "/login?next=/teacher/assignments", headers={"Origin": "http://localhost"})

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/teacher/assignments")

    def test_cross_origin_post_to_teacher_mutation_is_rejected_after_login(self):
        self._login_teacher()
        before_assignments = self._assignment_count()

        response = self.client.post(
            "/teacher/new",
            data={**NO_DEADLINE_FORM,
                "section_id": str(self.section["id"]),
                "question_count": "1",
                "time_limit_minutes": "",
            },
            headers={"Origin": "http://evil.example"},
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(self._assignment_count(), before_assignments)

    def test_same_origin_post_to_teacher_new_still_works(self):
        self._login_teacher()
        before_assignments = self._assignment_count()

        response = self.client.post(
            "/teacher/new",
            data={**NO_DEADLINE_FORM,
                "section_id": str(self.section["id"]),
                "question_count": "1",
                "time_limit_minutes": "",
            },
            headers={"Origin": "http://localhost"},
        )

        self.assertEqual(response.status_code, 302)
        self.assertIn("/teacher/assignment/", response.headers["Location"])
        self.assertEqual(self._assignment_count(), before_assignments + 1)

    def test_get_routes_are_not_blocked_by_cross_origin_headers(self):
        response = self.client.get(
            "/login",
            headers={"Origin": "http://evil.example"},
        )

        self.assertEqual(response.status_code, 200)

    def test_missing_origin_and_referer_is_allowed_in_testing_mode(self):
        self.assertTrue(self.app.config["TESTING"])
        response = self._post_login("/login?next=/teacher/assignments")

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/teacher/assignments")

    def test_missing_origin_and_referer_is_rejected_outside_testing_mode(self):
        previous_testing = self.app.config["TESTING"]
        self.app.config["TESTING"] = False
        try:
            response = self._post_login()
            logout = self.client.post("/logout")
        finally:
            self.app.config["TESTING"] = previous_testing

        self.assertEqual(response.status_code, 403)
        self.assertEqual(logout.status_code, 403)

    def test_same_origin_referer_is_allowed_when_origin_is_missing(self):
        response = self._post_login(
            "/login?next=/teacher/assignments", headers={"Referer": "http://localhost/login"})

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/teacher/assignments")

    def test_cross_origin_referer_is_rejected_when_origin_is_missing(self):
        response = self._post_login(headers={"Referer": "http://evil.example/login"})

        self.assertEqual(response.status_code, 403)

    def test_session_cookie_security_defaults_are_set(self):
        self.assertEqual(self.app.config["SESSION_COOKIE_SAMESITE"], "Lax")
        self.assertTrue(self.app.config["SESSION_COOKIE_HTTPONLY"])

    def test_session_cookie_secure_env_defaults_true(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertTrue(web_app.build_session_cookie_secure())

    def test_session_cookie_secure_env_accepts_explicit_false(self):
        for value in ("0", "false", "no", "off"):
            with self.subTest(value=value):
                with patch.dict(os.environ, {"DZI_SESSION_COOKIE_SECURE": value}, clear=True):
                    self.assertFalse(web_app.build_session_cookie_secure())

    def test_session_cookie_secure_env_accepts_explicit_true(self):
        for value in ("1", "true", "yes", "on"):
            with self.subTest(value=value):
                with patch.dict(os.environ, {"DZI_SESSION_COOKIE_SECURE": value}, clear=True):
                    self.assertTrue(web_app.build_session_cookie_secure())

    def test_secret_key_without_env_is_not_static_fallback(self):
        with patch.dict(os.environ, {}, clear=True):
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                secret_key = web_app.build_secret_key()

        self.assertNotEqual(secret_key, "local-learnpilot-dev-key")
        self.assertGreaterEqual(len(secret_key), 32)
        self.assertTrue(any("DZI_SECRET_KEY is not set" in str(item.message) for item in caught))


if __name__ == "__main__":
    unittest.main()
