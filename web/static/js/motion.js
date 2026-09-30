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

  function play(element, distance, duration, delay = 0, opacity = 1) {
    if (!element || reduced.matches || isInteracting(element)) return;
    finish(element);
    const start = { transform: `translateY(${distance}px)` };
    const end = { transform: "none" };
    if (opacity < 1) {
      start.opacity = opacity;
      end.opacity = 1;
    }
    try {
      const animation = element.animate([start, end], {
        duration, delay, easing, fill: "none"
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
    const presets = [
      ["eyebrow", 4, 180, 0],
      ["headline", 6, 280, 20],
      ["description", 4, 220, 50],
      ["stats", 3, 180, 75]
    ];
    presets.forEach(([name, distance, duration, delay]) => {
      if (mobile.matches && name === "stats") return;
      const element = hero.querySelector(`[data-motion-item="${name}"]`);
      play(element, mobile.matches ? Math.ceil(distance / 2) : distance,
        mobile.matches ? Math.round(duration * 0.8) : duration, delay, 0.88);
    });
  }

  function reveal(target) {
    if (target.id === "home-search-dzi") {
      target.querySelectorAll("[data-motion-item]").forEach((item) => {
        play(item, mobile.matches ? 3 : 6, mobile.matches ? 200 : 250, 0, 0.9);
      });
      return;
    }
    play(target, mobile.matches ? 2 : 4, mobile.matches ? 190 : 210, 0, 0.9);
  }

  function stagger(target) {
    const search = document.getElementById("home-search-input");
    if (search && search.value) return;
    const cards = Array.from(target.children).filter((card) =>
      card.classList.contains("home-subject-card") &&
      !card.classList.contains("is-search-hidden")
    );
    cards.slice(0, 6).forEach((card, index) => {
      play(card.querySelector("[data-motion-item]"), mobile.matches ? 3 : 5,
        mobile.matches ? 190 : 220,
        mobile.matches ? 0 : Math.min(index * 25, 75), 0.9);
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
    }, { once: true });
  } else {
    initHomepage();
    initBrowsing();
  }
})();
