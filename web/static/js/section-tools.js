(function () {
  const root = document.querySelector("[data-section-tools]");
  if (!root) return;

  const sectionSlug = root.dataset.sectionSlug;
  const storageKey = `dzi:bookmarks:${sectionSlug}`;

  const searchInput = document.getElementById("question-search");
  const typeFilter = document.getElementById("question-type-filter");
  const difficultyFilter = document.getElementById("question-difficulty-filter");
  const summary = document.getElementById("filter-summary");
  const bookmarkSummary = document.getElementById("bookmark-summary");
  const list = document.getElementById("questions-list");

  const toggleCorrect = document.getElementById("toggle-correct");
  const shuffleButton = document.getElementById("shuffle-questions");
  const resetButton = document.getElementById("reset-view");
  const bookmarksOnlyButton = document.getElementById("bookmarks-only");
  const shortcutsButton = document.getElementById("shortcuts-button");
  const shortcutsPopover = document.getElementById("shortcuts-popover");

  const originalQuestions = Array.from(document.querySelectorAll("#questions-list .question"));
  let questions = [...originalQuestions];
  let answersShown = false;
  let bookmarksOnly = false;
  let syncingAnswers = false;

  function readBookmarks() {
    try {
      const parsed = JSON.parse(localStorage.getItem(storageKey) || "[]");
      return new Set(parsed.map(String));
    } catch {
      return new Set();
    }
  }

  function writeBookmarks(set) {
    localStorage.setItem(storageKey, JSON.stringify(Array.from(set)));
  }

  let bookmarks = readBookmarks();

  function normalize(value) {
    return (value || "").toLowerCase().trim();
  }

  function matchesType(questionType, selectedType) {
    if (!selectedType) return true;
    if (selectedType === "open") {
      return questionType !== "multiple_choice" && questionType !== "true_false";
    }
    return questionType === selectedType;
  }

  function matchesDifficulty(questionDifficulty, selectedDifficulty) {
    if (!selectedDifficulty) return true;
    return (questionDifficulty || "unknown") === selectedDifficulty;
  }

  function updateBookmarkUI() {
    questions.forEach((q) => {
      const id = String(q.dataset.questionId);
      const button = q.querySelector(".bookmark-button");
      const active = bookmarks.has(id);
      q.classList.toggle("is-bookmarked", active);
      if (button) {
        button.classList.toggle("active", active);
        button.setAttribute("aria-pressed", active ? "true" : "false");
        button.title = active ? "Премахни маркирането" : "Маркирай въпроса";
      }
    });

    bookmarkSummary.textContent = `Маркирани: ${bookmarks.size}`;
    bookmarksOnlyButton.classList.toggle("active", bookmarksOnly);
  }

  function applyFilters() {
    const query = normalize(searchInput.value);
    const selectedType = typeFilter.value;
    const selectedDifficulty = difficultyFilter.value;
    let shown = 0;

    questions.forEach((q) => {
      const text = q.dataset.searchText || "";
      const questionType = q.dataset.questionType || "";
      const difficulty = q.dataset.difficulty || "unknown";
      const id = String(q.dataset.questionId);

      const ok =
        (!query || text.includes(query)) &&
        matchesType(questionType, selectedType) &&
        matchesDifficulty(difficulty, selectedDifficulty) &&
        (!bookmarksOnly || bookmarks.has(id));

      q.classList.toggle("is-hidden", !ok);
      if (ok) shown += 1;
    });

    summary.textContent = `Показани: ${shown} от ${questions.length}`;
    updateBookmarkUI();
  }

  function setAnswerVisibility(shown) {
    answersShown = shown;
    syncingAnswers = true;
    questions.forEach((q) => {
      q.querySelectorAll(".answer-details").forEach((details) => {
        details.open = answersShown;
      });
    });
    syncingAnswers = false;
    toggleCorrect.classList.toggle("active", answersShown);
    toggleCorrect.setAttribute("aria-pressed", answersShown ? "true" : "false");
    toggleCorrect.title = answersShown ? "Скрий всички отговори (h)" : "Покажи всички отговори (h)";
    const label = toggleCorrect.querySelector(".button-label");
    if (label) label.textContent = answersShown ? "Скрий всички отговори" : "Покажи всички отговори";
  }

  function syncAnswerToggleFromDetails() {
    const answerDetails = questions.flatMap((q) => Array.from(q.querySelectorAll(".answer-details")));
    const openCount = answerDetails.filter((details) => details.open).length;
    const mostlyOpen = answerDetails.length > 0 && openCount >= Math.ceil(answerDetails.length / 2);

    answersShown = mostlyOpen;
    toggleCorrect.classList.toggle("active", mostlyOpen);
    toggleCorrect.setAttribute("aria-pressed", mostlyOpen ? "true" : "false");
    toggleCorrect.title = mostlyOpen ? "Скрий всички отговори (h)" : "Покажи всички отговори (h)";
    const label = toggleCorrect.querySelector(".button-label");
    if (label) label.textContent = mostlyOpen ? "Скрий всички отговори" : "Покажи всички отговори";
  }

  function shuffleQuestions() {
    questions = [...questions].sort(() => Math.random() - 0.5);
    questions.forEach((q) => list.appendChild(q));
    shuffleButton.classList.add("active");
    applyFilters();
  }

  function resetView() {
    searchInput.value = "";
    typeFilter.value = "";
    difficultyFilter.value = "";
    bookmarksOnly = false;
    questions = [...originalQuestions];
    questions.forEach((q) => list.appendChild(q));
    shuffleButton.classList.remove("active");
    setAnswerVisibility(false);
    applyFilters();
  }

  originalQuestions.forEach((q) => {
    const button = q.querySelector(".bookmark-button");
    if (button) {
      button.addEventListener("click", () => {
        const id = String(q.dataset.questionId);
        if (bookmarks.has(id)) bookmarks.delete(id);
        else bookmarks.add(id);
        writeBookmarks(bookmarks);
        applyFilters();
      });
    }

    q.querySelectorAll(".answer-details").forEach((details) => {
      details.addEventListener("toggle", () => {
        if (!syncingAnswers) syncAnswerToggleFromDetails();
      });
    });
  });

  searchInput.addEventListener("input", applyFilters);
  typeFilter.addEventListener("change", applyFilters);
  difficultyFilter.addEventListener("change", applyFilters);
  toggleCorrect.addEventListener("click", () => setAnswerVisibility(!answersShown));
  shuffleButton.addEventListener("click", shuffleQuestions);
  resetButton.addEventListener("click", resetView);
  bookmarksOnlyButton.addEventListener("click", () => {
    bookmarksOnly = !bookmarksOnly;
    applyFilters();
  });

  shortcutsButton.addEventListener("click", () => {
    shortcutsPopover.hidden = !shortcutsPopover.hidden;
  });

  document.addEventListener("click", (event) => {
    if (!shortcutsPopover.hidden && !event.target.closest(".shortcuts-wrap")) {
      shortcutsPopover.hidden = true;
    }
  });

  document.addEventListener("keydown", (event) => {
    const tag = (document.activeElement && document.activeElement.tagName || "").toLowerCase();
    const typing = tag === "input" || tag === "textarea" || tag === "select";

    if (event.key === "/" && !typing) {
      event.preventDefault();
      searchInput.focus();
      return;
    }

    if (event.key === "Escape") {
      searchInput.value = "";
      searchInput.blur();
      shortcutsPopover.hidden = true;
      applyFilters();
      return;
    }

    if (typing) return;

    if (event.key.toLowerCase() === "s") {
      event.preventDefault();
      shuffleQuestions();
    }

    if (event.key.toLowerCase() === "h") {
      event.preventDefault();
      setAnswerVisibility(!answersShown);
    }
  });

  setAnswerVisibility(false);
  applyFilters();
})();
