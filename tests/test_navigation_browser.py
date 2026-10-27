"""The header in a real browser: layout, keyboard focus and sticky offsets (PR #350 review).

These are behavioral checks in headless Chromium (``tests.browser_support``), not
source-text assertions; they skip when no Chromium is installed. Zoom is emulated as a
narrower CSS viewport at a higher device scale.
"""

import json
import unittest
from contextlib import closing

from tests.auth_helpers import create_account, set_account_session
from tests.browser_support import Browser, chromium_path
from tests.support import IsolatedAppTestCase, web_app
from web.access import account_principal
from web.services import quiz

# 80 characters (MAX_DISPLAY_NAME_LENGTH) and not one space.
UNBROKEN_NAME = "Александраконстантиновадимитровапетроваивановагеоргиевастояновахристованиколовап"
NORMAL_NAME = "Мария Георгиева"
SECTION = "/section/grade12-dzi-preparation"

NARROW = [(320, 640, 1), (390, 844, 1), (600, 800, 1), (768, 800, 1),
          (640, 400, 2), (384, 400, 2)]           # the last two: 1280px and 768px at 200%
SHORT = [(320, 200, 4), (390, 300, 1), (768, 300, 1), (1280, 360, 1)]   # the first: 1280x800 at 400%
DESKTOP = [(1024, 768, 1), (1440, 900, 1)]

# Every header control, in Tab order: the same with and without JavaScript.
HEADER_CONTROLS = {
    "student": ["LearnPilot начало", "Начало", "Класове", "Предмети", "ДЗИ", "Моите опити", "Профил"],
    "teacher": ["LearnPilot начало", "Начало", "Класове", "Предмети", "ДЗИ", "Моите тестове", "Моите опити",
                "Профил"],
    "admin": ["LearnPilot начало", "Начало", "Класове", "Предмети", "ДЗИ", "Създадени тестове", "Моите опити",
              "Профил"],
}

# The focused control, and its focus ring (outline + offset), against the viewport and
# every box that clips it: those in its containing-block chain (a fixed panel escapes the
# scrolling links row, an absolute one the boxes up to its positioned ancestor).
FOCUSED = r"""(() => {
  const a = document.activeElement;
  if (!a || a === document.body || !a.closest(".top-nav")) return null;
  const style = getComputedStyle(a);
  const ring = style.outlineStyle === "none" ? 0 : parseFloat(style.outlineWidth) + parseFloat(style.outlineOffset);
  const b = a.getBoundingClientRect();
  const up = (e) => {
    const position = getComputedStyle(e).position;
    if (position === "fixed") return null;
    if (position !== "absolute") return e.parentElement;
    for (let p = e.parentElement; p; p = p.parentElement) if (getComputedStyle(p).position !== "static") return p;
    return null;
  };
  let clip = {l: 0, r: innerWidth, t: 0, b: innerHeight};
  for (let e = up(a); e; e = up(e)) {
    const s = getComputedStyle(e);
    if (s.overflowX === "visible" && s.overflowY === "visible") continue;
    const c = e.getBoundingClientRect(), l = c.left + e.clientLeft, t = c.top + e.clientTop;
    clip = {l: Math.max(clip.l, l), r: Math.min(clip.r, l + e.clientWidth),
            t: Math.max(clip.t, t), b: Math.min(clip.b, t + e.clientHeight)};
  }
  const y = b.top + b.height / 2;
  return {text: a.getAttribute("aria-label") || a.textContent.trim().replace(/\s+/g, " "), ring,
          pageFits: document.documentElement.scrollWidth <= document.documentElement.clientWidth,
          hidden: Math.max(clip.l - (b.left - ring), b.right + ring - clip.r,
                           clip.t - (b.top - ring), b.bottom + ring - clip.b, 0),
          covered: [0.1, 0.5, 0.9].filter((f) => {
            const hit = document.elementFromPoint(b.left + b.width * f, y);
            return !hit || !(hit === a || a.contains(hit)); }).length};
})()"""

PROFILE_PANEL = r"""(() => {
  const panel = document.querySelector(".mobile-profile-menu[open] .mobile-profile-panel");
  if (!panel) return null;
  const p = panel.getBoundingClientRect(), label = panel.querySelector(".profile-label");
  const range = document.createRange();
  range.selectNodeContents(label);
  const lines = Array.from(range.getClientRects());
  return {text: label.textContent, panel: [p.left, p.right], vw: innerWidth,
          textLeft: Math.min(...lines.map((r) => r.left)), textRight: Math.max(...lines.map((r) => r.right)),
          lines: new Set(lines.map((r) => Math.round(r.top))).size};
})()"""

BELOW_HEADER = r"""((selector) => {
  const header = document.querySelector(".top-nav").getBoundingClientRect();
  const target = document.querySelector(selector);
  return {headerBottom: header.bottom, headerHeight: header.height,
          top: target.getBoundingClientRect().top, position: getComputedStyle(target).position};
})"""

PAGE_FITS = "document.documentElement.scrollWidth <= document.documentElement.clientWidth"

MENU_STATE = r"""JSON.stringify({
  classes: document.querySelector("[aria-controls=nav-menu-classes]").getAttribute("aria-expanded"),
  subjects: document.querySelector("[aria-controls=nav-menu-subjects]").getAttribute("aria-expanded"),
  profile: !!document.querySelector(".mobile-profile-menu[open]"),
  focus: document.activeElement.textContent.trim()})"""


@unittest.skipUnless(chromium_path(), "no headless Chromium (set LEARNPILOT_CHROMIUM)")
class NavigationBrowserTest(IsolatedAppTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        db_path = web_app.app.config["DB_PATH"]
        cls.users = {role: create_account(db_path, role, display_name=NORMAL_NAME)
                     for role in ("student", "teacher", "admin")}
        cls.unbroken = {role: create_account(db_path, role, display_name=UNBROKEN_NAME)
                        for role in ("student", "admin")}
        with closing(web_app.quiz_db()) as conn:
            section_id = next(int(row[0]) for row in conn.execute("SELECT id FROM curriculum_sections ORDER BY id")
                              if len(web_app.quiz_section_question_ids(conn, int(row[0]))) >= 10)
            cls.assignment_id = quiz.quiz_create_assignment(
                conn, principal=account_principal(cls.users["teacher"]["id"], "teacher", "Учител"),
                section_id=section_id, title_bg="Заглавка", question_count=10,
                time_limit_minutes=None, question_plan_json=None)
            conn.commit()
            token = conn.execute("SELECT public_token FROM quiz_assignments WHERE id = ?",
                                 (cls.assignment_id,)).fetchone()[0]
        started = cls.client_for(cls.users["student"]).post(f"/quiz/s/{token}", headers={"Origin": "http://localhost"})
        cls.attempt_path = started.headers["Location"]
        assert cls.attempt_path.startswith("/quiz/attempt/"), cls.attempt_path
        cls.browser = Browser(web_app.app.test_client())
        cls.addClassCleanup(cls.browser.close)

    def setUp(self):
        self.browser.scripts(True)
        self.browser.media()
        self.browser.blocked.clear()

    def tearDown(self):
        # The pages need nothing but the application: no request left localhost.
        self.assertEqual(self.browser.blocked, [])

    @staticmethod
    def client_for(user):
        client = web_app.app.test_client()
        if user:
            set_account_session(client, user["id"], user["session_key"])
        return client

    def open(self, user, path, width, height=800, scale=1):
        self.browser.client = self.client_for(user)
        self.browser.viewport(width, height, scale)
        self.browser.goto(path)

    def below_header(self, selector):
        return self.browser.eval(f"({BELOW_HEADER})({json.dumps(selector)})")

    def walk_header_focus(self, shift):
        """Every header control reached by Tab (or Shift+Tab), as it is when focused."""
        start = "document.querySelector('.breadcrumb a').focus()" if shift else "document.activeElement.blur()"
        self.browser.eval(start)
        stops = []
        for _ in range(14):
            self.browser.key("Tab", shift=shift)
            stop = self.browser.eval(FOCUSED)
            if stop is None and stops:
                break
            if stop is not None:
                stops.append(stop)
        return stops

    # -- B1 -------------------------------------------------------------------------------

    def test_an_unbroken_maximum_length_name_stays_whole_inside_the_profile_panel(self):
        for role, user in self.unbroken.items():
            for width, height, scale in [(1280, 800, 1), (1024, 768, 1), (768, 800, 1), (390, 844, 1),
                                         (320, 640, 1), (640, 400, 2)]:
                with self.subTest(role=role, width=width, scale=scale):
                    self.open(user, "/", width, height, scale)
                    self.browser.click_selector(".mobile-profile-menu > summary")
                    panel = self.browser.eval(PROFILE_PANEL)
                    self.assertEqual(panel["text"], UNBROKEN_NAME)
                    self.assertGreaterEqual(panel["textLeft"], panel["panel"][0])
                    self.assertLessEqual(panel["textRight"], panel["panel"][1])
                    self.assertGreaterEqual(panel["panel"][0], 0)
                    self.assertLessEqual(panel["panel"][1], panel["vw"])
                    self.assertGreater(panel["lines"], 1)
                    self.assertTrue(self.browser.eval(PAGE_FITS))

    def test_a_normal_name_is_not_broken(self):
        self.open(self.users["admin"], "/", 390, 844)
        self.browser.click_selector(".mobile-profile-menu > summary")
        self.assertEqual(self.browser.eval(PROFILE_PANEL)["lines"], 1)

    # -- B2 -------------------------------------------------------------------------------

    def assert_every_header_control_revealed(self):
        """Tab and Shift+Tab stop at exactly the role's controls, in order, each fully visible
        with its focus ring and the page never wider than the viewport."""
        for role, user in self.users.items():
            for width, height, scale in NARROW + DESKTOP:
                self.open(user, "/", width, height, scale)
                for shift in (False, True):
                    stops = self.walk_header_focus(shift)
                    expected = HEADER_CONTROLS[role][::-1] if shift else HEADER_CONTROLS[role]
                    self.assertEqual([stop["text"] for stop in stops], expected, (role, width, scale, shift))
                    for stop in stops:
                        with self.subTest(role=role, width=width, scale=scale, shift=shift, control=stop["text"]):
                            self.assertGreater(stop["ring"], 0)
                            self.assertLessEqual(stop["hidden"], 0.5)
                            self.assertEqual(stop["covered"], 0)
                            self.assertTrue(stop["pageFits"])

    def test_tab_and_shift_tab_reveal_every_header_control_and_its_focus_ring(self):
        self.assert_every_header_control_revealed()

    def test_without_javascript_tab_and_shift_tab_reveal_every_header_control(self):
        # No script scrolls a focused link into the narrow links row: the row wraps instead.
        self.browser.scripts(False)
        self.assert_every_header_control_revealed()
        self.browser.scripts(True)

    def test_focus_is_revealed_with_reduced_motion(self):
        self.browser.media(prefers_reduced_motion="reduce")
        for width, height, scale in [(320, 640, 1), (390, 844, 1), (640, 400, 2)]:
            self.open(self.users["admin"], "/", width, height, scale)
            self.assertTrue(self.browser.eval("matchMedia('(prefers-reduced-motion: reduce)').matches"))
            for stop in self.walk_header_focus(False) + self.walk_header_focus(True):
                with self.subTest(width=width, control=stop["text"]):
                    self.assertLessEqual(stop["hidden"], 0.5)
                    self.assertEqual(stop["covered"], 0)

    # -- S1 -------------------------------------------------------------------------------

    def assert_clear_of_header(self, selector, *, scroll):
        self.browser.eval(f"window.scrollTo(0, {scroll})")
        self.browser.settle()
        found = self.below_header(selector)
        self.assertEqual(found["position"], "sticky")
        self.assertGreaterEqual(found["top"], found["headerBottom"], found)
        return found

    def test_sticky_tools_stay_below_a_wrapped_header(self):
        for role, width in (("admin", 1024), ("teacher", 860), ("student", 769), ("admin", 1440)):
            with self.subTest(role=role, width=width):
                self.open(self.users[role], SECTION, width)
                found = self.assert_clear_of_header(".section-toolbar", scroll=1500)
                if width < 1100:   # these widths wrap the header, the others keep one row
                    self.assertGreater(found["headerHeight"], 80)
        for width in (1024, 769, 390, 320):
            with self.subTest(page="attempt", width=width):
                self.open(self.users["student"], self.attempt_path, width)
                self.assert_clear_of_header(".quiz-progress", scroll=900)

    def test_sticky_offset_follows_resizing_and_text_size(self):
        self.open(self.users["admin"], SECTION, 1440)
        one_row = self.assert_clear_of_header(".section-toolbar", scroll=1500)
        self.browser.viewport(1024, 800)      # the links now wrap onto a second row
        self.browser.settle()
        wrapped = self.assert_clear_of_header(".section-toolbar", scroll=1600)
        self.assertGreater(wrapped["headerHeight"], one_row["headerHeight"])
        self.browser.viewport(1440, 800)      # and back: no stale, larger offset
        self.browser.settle()
        again = self.assert_clear_of_header(".section-toolbar", scroll=1500)
        self.assertLess(again["top"] - again["headerBottom"], 12)
        self.browser.eval("document.documentElement.style.fontSize = '22px'")   # as a font or text zoom would
        self.browser.settle()
        larger = self.assert_clear_of_header(".section-toolbar", scroll=1700)
        self.assertGreater(larger["headerHeight"], again["headerHeight"])

    def test_anchor_targets_land_below_a_wrapped_header(self):
        path = f"/teacher/assignment/{self.assignment_id}"
        for width in (1024, 860, 1440):
            with self.subTest(width=width, how="load"):
                self.open(self.users["admin"], path + "#duplicate", width)
                found = self.below_header("#duplicate")
                self.assertGreaterEqual(found["top"], found["headerBottom"], found)
            with self.subTest(width=width, how="link"):
                self.browser.eval("window.scrollTo(0, 0)")
                self.browser.click_selector("a[href='#duplicate']")
                found = self.below_header("#duplicate")
                self.assertGreaterEqual(found["top"], found["headerBottom"], found)

    def test_without_javascript_the_sticky_tools_do_not_hide_under_the_header(self):
        self.browser.scripts(False)
        # Widths at which the no-JavaScript header (<details> menus) wraps for that account.
        for path, selector, user, width in ((SECTION, ".section-toolbar", self.users["admin"], 860),
                                            (self.attempt_path, ".quiz-progress", self.users["student"], 769)):
            with self.subTest(selector=selector):
                self.open(user, path, width)
                self.assertFalse(self.browser.eval("!!window.ResizeObserver && "
                                                   "document.documentElement.style.length > 0"))
                # Unmeasured, the header is taller than the fallback offset: the tool scrolls
                # away with the content instead of being pinned partly beneath it.
                self.browser.eval("window.scrollTo(0, 1500)")
                found = self.below_header(selector)
                self.assertGreater(found["headerHeight"], 80)
                self.assertEqual(found["position"], "static")
                self.assertLess(found["top"], 0)
        self.browser.scripts(True)

    def test_without_javascript_the_name_and_menus_still_work(self):
        self.browser.scripts(False)
        for width in (1024, 390):
            with self.subTest(width=width):
                self.open(self.unbroken["admin"], "/", width)
                self.browser.click_selector(".mobile-profile-menu > summary")
                panel = self.browser.eval(PROFILE_PANEL)
                self.assertLessEqual(panel["textRight"], panel["panel"][1])
                self.assertTrue(self.browser.eval(PAGE_FITS))
                self.browser.click_selector(".mobile-profile-menu > summary")
                self.browser.click_selector("details.nojs-nav-menu > summary")
                self.assertTrue(self.browser.eval("!!document.querySelector('details.nojs-nav-menu[open] .nav-menu-panel')"))
        self.browser.scripts(True)

    def test_without_javascript_anchor_targets_are_not_covered(self):
        # Unmeasured, the header wraps at any width, so it is not sticky.
        self.browser.scripts(False)
        for width in (769, 860, 960, 1440, 768, 390, 320):
            with self.subTest(width=width):
                self.open(self.users["admin"], f"/teacher/assignment/{self.assignment_id}#duplicate", width)
                found = self.below_header("#duplicate")
                self.assertGreaterEqual(found["top"], found["headerBottom"] - 0.5, found)

    # -- short viewports and high zoom ----------------------------------------------------

    def assert_menu_items_reachable(self, opener, index, panel):
        """Open a menu with a real click, then Tab to each of its links and buttons: every
        one fully visible with its ring, the panel and page never scrolling sideways."""
        self.browser.click_selector(opener, index)
        self.assertTrue(self.browser.eval(f"""(() => {{
            const trigger = document.querySelectorAll({json.dumps(opener)})[{index}];
            const box = trigger.getBoundingClientRect();
            const hit = document.elementFromPoint(box.left + box.width / 2, box.top + box.height / 2);
            return !!hit && trigger.contains(hit); }})()"""), "the open menu covers its trigger")
        items = f"{panel}.querySelectorAll('a, button')"
        count = self.browser.eval(f"{items}.length")
        self.assertGreater(count, 0)
        for item in range(count):
            for _ in range(4):
                self.browser.key("Tab")
                if self.browser.eval(f"document.activeElement === {items}[{item}]"):
                    break
            else:
                self.fail(f"item {item} not reached by Tab")
            stop = self.browser.eval(FOCUSED)
            with self.subTest(item=stop["text"]):
                self.assertLessEqual(stop["hidden"], 0.5)
                self.assertEqual(stop["covered"], 0)
        self.assertLessEqual(self.browser.eval(f"{panel}.scrollWidth - {panel}.clientWidth"), 0)
        self.assertTrue(self.browser.eval(PAGE_FITS))

    def test_every_menu_item_is_reachable_at_short_heights_and_high_zoom(self):
        menus = (("Профил", ".mobile-profile-menu > summary", "document.querySelector('.mobile-profile-panel')"),
                 ("Класове", "[aria-controls=nav-menu-classes]", "document.getElementById('nav-menu-classes')"),
                 ("Предмети", "[aria-controls=nav-menu-subjects]", "document.getElementById('nav-menu-subjects')"))
        for who, user in (("student", self.users["student"]), ("admin", self.users["admin"]),
                          ("unbroken", self.unbroken["admin"])):
            for width, height, scale in SHORT:
                for menu, opener, panel in menus:
                    with self.subTest(who=who, width=width, height=height, menu=menu):
                        self.open(user, "/", width, height, scale)
                        self.assert_menu_items_reachable(opener, 0, panel)

    def test_without_javascript_every_menu_item_is_reachable(self):
        self.browser.scripts(False)
        nojs_panel = "document.querySelectorAll('details.nojs-nav-menu')[{}].querySelector('.nav-menu-panel')"
        menus = (("Профил", ".mobile-profile-menu > summary", 0, "document.querySelector('.mobile-profile-panel')"),
                 ("Класове", "details.nojs-nav-menu > summary", 0, nojs_panel.format(0)),
                 ("Предмети", "details.nojs-nav-menu > summary", 1, nojs_panel.format(1)))
        for width, height, scale in SHORT + NARROW[:4] + DESKTOP:
            for menu, opener, index, panel in menus:
                with self.subTest(width=width, height=height, menu=menu):
                    self.open(self.users["admin"], "/", width, height, scale)
                    self.assert_menu_items_reachable(opener, index, panel)

    # -- menus and layout (coverage) ------------------------------------------------------

    def menu_state(self):
        return json.loads(self.browser.eval(MENU_STATE))

    def test_one_menu_open_at_a_time_and_escape_or_an_outside_click_closes_it(self):
        for width, height in ((1280, 800), (390, 844)):
            with self.subTest(width=width):
                self.open(self.users["admin"], "/", width, height)
                self.browser.click_selector("[aria-controls=nav-menu-classes]")
                self.assertEqual(self.menu_state()["classes"], "true")
                self.browser.click_selector(".mobile-profile-menu > summary")
                state = self.menu_state()
                self.assertEqual((state["classes"], state["profile"]), ("false", True))
                self.browser.click_selector("[aria-controls=nav-menu-subjects]")
                state = self.menu_state()
                self.assertEqual((state["subjects"], state["profile"]), ("true", False))
                self.browser.click_selector(".mobile-profile-menu > summary")
                self.assertEqual(self.menu_state()["subjects"], "false")
                self.browser.key("Tab")          # into the open panel
                self.browser.key("Escape")
                state = self.menu_state()
                self.assertEqual((state["profile"], state["focus"]), (False, "Профил"))
                self.browser.click_selector(".mobile-profile-menu > summary")
                self.browser.click(3, height - 5)
                self.assertFalse(self.menu_state()["profile"])
                self.browser.click_selector("[aria-controls=nav-menu-classes]")
                self.browser.click(3, height - 5)
                self.assertEqual(self.menu_state()["classes"], "false")

    def test_logout_is_a_post_from_the_profile_menu(self):
        user = create_account(web_app.app.config["DB_PATH"], "student", display_name=NORMAL_NAME)
        self.open(user, "/", 390, 844)
        self.browser.requests.clear()
        self.browser.click_selector(".mobile-profile-menu > summary")
        document = self.browser.document_id()
        self.browser.click_selector(".profile-logout-button")
        self.browser.wait_for_load(after=document)
        self.assertIn(("POST", "/logout"), self.browser.requests)
        self.assertTrue(self.browser.eval("!!document.querySelector('.mobile-profile-menu--signed-out')"))

    def test_narrow_pages_never_scroll_sideways_with_a_menu_open(self):
        for role in (None, "student", "teacher", "admin"):
            user = self.users.get(role)
            for width in (320, 390, 768, 769):
                with self.subTest(role=role, width=width):
                    self.open(user, "/", width)
                    self.assertTrue(self.browser.eval(PAGE_FITS))
                    if user is None:
                        continue
                    for opener in ("[aria-controls=nav-menu-classes]", "[aria-controls=nav-menu-subjects]",
                                   ".mobile-profile-menu > summary"):
                        self.browser.eval("window.scrollTo(0, 0)")
                        self.browser.click_selector(opener)
                        self.assertTrue(self.browser.eval(PAGE_FITS), opener)
                        inside = self.browser.eval(r"""(() => {
                          const panel = Array.from(document.querySelectorAll(
                            ".nav-menu-panel:not([hidden]), .mobile-profile-menu[open] .mobile-profile-panel"))
                            .find((e) => getComputedStyle(e).display !== "none");
                          const b = panel.getBoundingClientRect();
                          return b.left >= 0 && b.right <= innerWidth; })()""")
                        self.assertTrue(inside, opener)
                    self.browser.click(3, 795)


if __name__ == "__main__":
    unittest.main()
