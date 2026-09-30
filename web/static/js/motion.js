(() => {
  if (window.__learnPilotMotionInitialized) return;
  window.__learnPilotMotionInitialized = true;

  if (!window.matchMedia || !Element.prototype.animate) return;

  const reduced = window.matchMedia("(prefers-reduced-motion: reduce)");
  const mobile = window.matchMedia("(max-width: 768px)");
  const easing = "cubic-bezier(0.2, 0.7, 0.2, 1)";
  const active = new Map();
  const initialized = new WeakSet();
  const completed = new WeakSet();
  const pending = new Set();
  let observer = null;

  function finish(element) {
    const animation = active.get(element);
    if (!animation) return;
    try {
      animation.finish();
    } catch (_) {
      animation.cancel();
    }
    active.delete(element);
  }

  function finishAll() {
    Array.from(active.keys()).forEach(finish);
  }

  function stopViewportMotion() {
    if (observer) observer.disconnect();
    pending.clear();
  }

  function isInteracting(element) {
    const focused = document.activeElement;
    return focused && focused !== document.body &&
      focused !== document.documentElement &&
      (element.contains(focused) || focused.contains(element));
  }

  function playFrames(element, frames, duration, delay = 0, fill = "none") {
    if (!element || reduced.matches || isInteracting(element)) return;
    finish(element);
    try {
      const animation = element.animate(frames, {
        duration, delay, easing, fill
      });
      active.set(element, animation);
      const clear = () => {
        if (active.get(element) === animation) active.delete(element);
      };
      animation.addEventListener("finish", clear, { once: true });
      animation.addEventListener("cancel", clear, { once: true });
    } catch (_) {
      // The final CSS state is already visible.
    }
  }

  function play(element, distance, duration, delay = 0, opacity = 1, fill = "none") {
    const start = { transform: `translateY(${distance}px)` };
    const end = { transform: "none" };
    if (opacity < 1) {
      start.opacity = opacity;
      end.opacity = 1;
    }
    playFrames(element, [start, end], duration, delay, fill);
  }

  function canRunFirstPaintMotion() {
    if (reduced.matches || window.location.hash || document.visibilityState === "hidden") return false;
    const navigation = performance.getEntriesByType &&
      performance.getEntriesByType("navigation")[0];
    if (navigation && navigation.type === "back_forward") return false;
    if (!navigation && performance.navigation && performance.navigation.type === 2) return false;
    const firstPaint = performance.getEntriesByType &&
      performance.getEntriesByType("paint")
        .find((entry) => entry.name === "first-contentful-paint");
    if (firstPaint && performance.now() - firstPaint.startTime > 80) return false;
    if (performance.now() > 700) return false;
    return true;
  }

  function shouldRunIntro() {
    if (!canRunFirstPaintMotion()) return false;
    try {
      if (sessionStorage.getItem("learnpilot:home-intro-seen")) return false;
      sessionStorage.setItem("learnpilot:home-intro-seen", "1");
    } catch (_) {
      // Storage is optional; the initialized set still prevents a replay here.
    }
    return true;
  }

  function runIntro(hero) {
    if (initialized.has(hero)) return;
    initialized.add(hero);
    if (!shouldRunIntro()) return;
    const compact = mobile.matches;
    const item = (name) => hero.querySelector(`[data-motion-item="${name}"]`);
    play(item("eyebrow"), compact ? 6 : 11, compact ? 235 : 280, 0, 0.35);
    play(item("headline"), compact ? 8 : 16, compact ? 400 : 470,
      compact ? 45 : 70, 0.08, "backwards");
    play(item("description"), compact ? 6 : 11, compact ? 290 : 340,
      compact ? 95 : 125, 0.35, "backwards");
    const stats = item("stats");
    if (stats) stats.querySelectorAll("span").forEach((stat, index) => {
      play(stat, compact ? 5 : 9, compact ? 240 : 285,
        (compact ? 130 : 170) + index * (compact ? 25 : 45), 0.45, "backwards");
    });
    play(item("search"), compact ? 4 : 8, compact ? 250 : 290,
      compact ? 190 : 240, 0.7, "backwards");

    const line = hero.querySelector(".home-hero-signature-line");
    const tip = hero.querySelector(".home-hero-signature-tip");
    playFrames(line, [{ transform: "scaleX(0)" }, { transform: "scaleX(1)" }],
      compact ? 290 : 340, compact ? 65 : 85, "backwards");
    playFrames(tip, [
      { transform: "translateX(-5px)", opacity: 0.15 },
      { transform: "none", opacity: 1 }
    ], compact ? 175 : 210, compact ? 300 : 340, "backwards");
  }

  function reveal(target) {
    if (target.id === "home-search-dzi") {
      const items = target.querySelectorAll("[data-motion-item]");
      play(items[0], mobile.matches ? 6 : 13, mobile.matches ? 260 : 330, 0, 0.35);
      play(items[1], mobile.matches ? 5 : 10, mobile.matches ? 250 : 305,
        mobile.matches ? 35 : 55, 0.4, "backwards");
      return;
    }
    if (target.closest(".home-subject-section")) {
      play(target.querySelector('[data-motion-item="subject-eyebrow"]'),
        mobile.matches ? 4 : 10, mobile.matches ? 240 : 290, 0, 0.35);
      play(target.querySelector('[data-motion-item="subject-heading"]'),
        mobile.matches ? 6 : 12, mobile.matches ? 270 : 320,
        mobile.matches ? 15 : 35, 0.35, "backwards");
    }
  }

  function stagger(target) {
    const search = document.getElementById("home-search-input");
    if (search && search.value) return;
    const cards = Array.from(target.children).filter((card) =>
      card.classList.contains("home-subject-card") &&
      !card.classList.contains("is-search-hidden")
    );
    cards.slice(0, 6).forEach((card, index) => {
      play(card.querySelector("[data-motion-item]"), mobile.matches ? 5 : 10,
        mobile.matches ? 250 : 290,
        (mobile.matches ? 35 : 75) + Math.min(index * (mobile.matches ? 12 : 40),
          mobile.matches ? 48 : 120), 0.4, "backwards");
    });
  }

  function browseIntro(hero) {
    if (initialized.has(hero)) return;
    initialized.add(hero);
    if (!canRunFirstPaintMotion()) return;
    play(hero.querySelector('[data-motion-item="headline"]'),
      mobile.matches ? 2 : 4, mobile.matches ? 170 : 200, 0, 0.9);
    play(hero.querySelector('[data-motion-item="description"]'),
      mobile.matches ? 2 : 3, mobile.matches ? 160 : 190,
      mobile.matches ? 0 : 20, 0.92);
  }

  function browseStagger(target) {
    Array.from(target.children).filter((card) =>
      card.matches(".subject-class-card, .subject-topic-group-card, .pinned-card")
    ).slice(0, 6).forEach((card, index) => {
      play(card.querySelector("[data-motion-item]"),
        mobile.matches ? 2 : 4, mobile.matches ? 180 : 210,
        mobile.matches ? 0 : Math.min(index * 20, 60), 0.92);
    });
  }

  function observeOnce(selector, onVisible) {
    if (observer || !("IntersectionObserver" in window) || reduced.matches) return;
    const targets = document.querySelectorAll(selector);
    if (!targets.length) return;

    observer = new IntersectionObserver((entries) => {
      entries.forEach((entry) => {
        const target = entry.target;
        if (!entry.isIntersecting || completed.has(target)) return;
        observer.unobserve(target);
        pending.delete(target);
        completed.add(target);
        if (document.visibilityState === "hidden" || reduced.matches) return;
        onVisible(target);
      });
    }, { rootMargin: "0px 0px 64px 0px", threshold: 0.05 });

    targets.forEach((target) => {
      if (initialized.has(target)) return;
      initialized.add(target);
      pending.add(target);
      observer.observe(target);
    });
  }

  function initHomepage() {
    if (!document.body.classList.contains("home-page")) return;
    const hero = document.querySelector('.home-page [data-motion="intro"]');
    if (hero) runIntro(hero);
    observeOnce(
      '.home-page [data-motion="reveal"], .home-page [data-motion="stagger"]',
      (target) => {
        if (target.dataset.motion === "reveal") reveal(target);
        if (target.dataset.motion === "stagger") stagger(target);
      }
    );

    const search = document.getElementById("home-search-input");
    if (search) {
      const stopForSearch = () => {
        finishAll();
        stopViewportMotion();
      };
      search.addEventListener("input", stopForSearch, { once: true });
      search.addEventListener("search", stopForSearch, { once: true });
    }
  }

  function initBrowsing() {
    if (!document.body.classList.contains("content-browse-page")) return;
    const hero = document.querySelector('.content-browse-page .hero[data-motion="intro"]');
    if (hero) browseIntro(hero);
    if (canRunFirstPaintMotion()) {
      observeOnce('.content-browse-page [data-motion="stagger"]', browseStagger);
    }

    // Only a direct summary activation gets a brief content reveal. The
    // section toolbar's "show all" remains immediate for every answer.
    const armed = new WeakSet();
    document.addEventListener("click", (event) => {
      if (!(event.target instanceof Element)) return;
      const summary = event.target.closest(".content-browse-page .answer-details > summary");
      if (summary) armed.add(summary.parentElement);
    }, true);
    document.addEventListener("toggle", (event) => {
      const details = event.target;
      if (!(details instanceof Element) ||
          !details.matches(".content-browse-page .answer-details") ||
          !armed.has(details)) return;
      armed.delete(details);
      if (details.open) {
        play(details.querySelector("[data-motion-disclosure]"), 2, 110, 0, 0.9);
      }
    }, true);
  }

  function initFeedback() {
    if (!canRunFirstPaintMotion()) return;
    const body = document.body;
    let target = null;
    let duration = 170;
    if (body.classList.contains("student-assessment-start-page")) {
      target = document.querySelector('.student-assessment-start-page .quiz-hero h1[data-motion="feedback"]');
    } else if (body.classList.contains("student-assessment-result-page")) {
      target = document.querySelector('.student-assessment-result-page .result-score-label[data-motion="feedback"]');
      duration = 200;
    } else if (body.classList.contains("teacher-management-page") &&
               body.classList.contains("management-dashboard-page")) {
      target = document.querySelector('.management-dashboard-page .dashboard-header h1[data-motion="feedback"]');
    }
    if (target && !initialized.has(target)) {
      initialized.add(target);
      play(target, mobile.matches ? 1 : 3, mobile.matches ? 150 : duration);
    }
  }

  document.addEventListener("learnpilot:nav-state", (event) => {
    const { panel, open, keyboard } = event.detail || {};
    if (!panel || !panel.matches(".nav-menu-panel")) return;
    const links = panel.querySelectorAll(".nav-menu-link");
    if (!open) {
      links.forEach(finish);
    } else if (!keyboard && !reduced.matches) {
      links.forEach((link) => play(link, 3, 150));
    }
  });

  function finishForInteraction(event) {
    const target = event.target;
    if (!(target instanceof Element)) return;
    const panel = target.closest(".nav-menu-panel");
    const control = target.closest("a, button, input, select, textarea, summary");
    const summary = target.closest(".content-browse-page .answer-details > summary");
    if (summary) finish(summary.parentElement.querySelector("[data-motion-disclosure]"));
    active.forEach((_, element) => {
      if (element.contains(target) || target.contains(element) ||
          (panel && panel.contains(element)) ||
          (control && control.contains(element))) finish(element);
    });
  }
  document.addEventListener("focusin", finishForInteraction, true);
  document.addEventListener("pointerdown", finishForInteraction, true);
  document.addEventListener("keydown", finishForInteraction, true);

  const onPreferenceChange = () => {
    if (reduced.matches) {
      finishAll();
      stopViewportMotion();
    }
  };
  if (reduced.addEventListener) reduced.addEventListener("change", onPreferenceChange);
  else reduced.addListener(onPreferenceChange);

  window.addEventListener("pagehide", () => {
    finishAll();
    if (observer) observer.disconnect();
  });
  window.addEventListener("pageshow", (event) => {
    if (event.persisted && document.body.classList.contains("content-browse-page")) {
      stopViewportMotion();
      return;
    }
    if (event.persisted && observer && !reduced.matches) {
      pending.forEach((target) => observer.observe(target));
    }
  });
  window.addEventListener("beforeprint", () => {
    finishAll();
    stopViewportMotion();
  });

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", () => {
      initHomepage();
      initBrowsing();
      initFeedback();
    }, { once: true });
  } else {
    initHomepage();
    initBrowsing();
    initFeedback();
  }
})();
