(function () {
  const form = document.getElementById("quiz-form");
  if (!form) return;

  const progress = document.getElementById("quiz-progress");
  if (!progress) return;

  const countEl = document.getElementById("quiz-progress-count");
  const totalEl = document.getElementById("quiz-progress-total");
  const bar = document.getElementById("quiz-progress-bar");

  const cards = Array.from(form.querySelectorAll("[data-question-id]"));
  const total = cards.length;
  if (totalEl) totalEl.textContent = String(total);
  if (bar) bar.max = total;

  function isCardAnswered(card) {
    const type = card.getAttribute("data-question-type") || "";
    if (type === "multiple_choice") {
      return !!card.querySelector('input[type="radio"]:checked');
    }
    // Open-answer card: count as answered when at least one of its
    // subquestion fields holds non-whitespace text.
    const fields = card.querySelectorAll(
      'input[name^="open_q_"], textarea[name^="open_q_"]'
    );
    for (let i = 0; i < fields.length; i += 1) {
      if ((fields[i].value || "").trim() !== "") return true;
    }
    return false;
  }

  function update() {
    let answered = 0;
    cards.forEach(function (card) {
      const ok = isCardAnswered(card);
      card.classList.toggle("is-answered", ok);
      if (ok) answered += 1;
    });
    if (countEl) countEl.textContent = String(answered);
    if (bar) bar.value = answered;
    progress.setAttribute("data-answered", String(answered));
  }

  form.addEventListener("change", update);
  form.addEventListener("input", update);

  // Initial scan picks up any values already restored by the autosave
  // script (which runs synchronously on DOMContentLoaded via its defer).
  update();

  window.LearnPilotQuizProgress = { refresh: update };
})();
