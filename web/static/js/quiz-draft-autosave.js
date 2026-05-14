(function () {
  const form = document.getElementById("quiz-form");
  if (!form) return;

  const attemptId = form.getAttribute("data-attempt-id");
  if (!attemptId) return;

  const storage = (function () {
    try {
      const probeKey = "__learnpilot_probe__";
      window.localStorage.setItem(probeKey, "1");
      window.localStorage.removeItem(probeKey);
      return window.localStorage;
    } catch (_err) {
      return null;
    }
  })();
  if (!storage) return;

  const STORAGE_KEY = "learnpilot:quiz-draft:" + attemptId;
  const STATUS_LABEL = "Черновата е запазена локално.";
  const status = document.getElementById("draft-status");

  function showSaved() {
    if (!status) return;
    status.textContent = STATUS_LABEL;
    status.hidden = false;
  }

  function isAnswerInput(input) {
    if (!input || !input.name) return false;
    if (input.type === "hidden") return false;
    return input.name.startsWith("q_") || input.name.startsWith("open_q_");
  }

  function readDraft() {
    let raw;
    try {
      raw = storage.getItem(STORAGE_KEY);
    } catch (_err) {
      return null;
    }
    if (!raw) return null;
    try {
      const parsed = JSON.parse(raw);
      if (!parsed || typeof parsed !== "object" || !parsed.answers || typeof parsed.answers !== "object") {
        return null;
      }
      return parsed.answers;
    } catch (_err) {
      return null;
    }
  }

  function collectAnswers() {
    const answers = {};
    form.querySelectorAll('input[type="radio"]:checked').forEach(function (radio) {
      if (!isAnswerInput(radio)) return;
      answers[radio.name] = radio.value;
    });
    form.querySelectorAll('input[type="text"], textarea').forEach(function (input) {
      if (!isAnswerInput(input)) return;
      const value = input.value || "";
      if (value === "") return;
      answers[input.name] = value;
    });
    return answers;
  }

  function saveDraft() {
    const payload = {
      version: 1,
      saved_at: new Date().toISOString(),
      answers: collectAnswers(),
    };
    try {
      storage.setItem(STORAGE_KEY, JSON.stringify(payload));
      showSaved();
    } catch (_err) {
      // Quota or other storage error — silently ignore; draft is best-effort.
    }
  }

  function clearDraft() {
    try {
      storage.removeItem(STORAGE_KEY);
    } catch (_err) {
      // ignore
    }
  }

  function restoreDraft() {
    const answers = readDraft();
    if (!answers) return false;
    let restoredAny = false;
    Object.keys(answers).forEach(function (name) {
      const value = answers[name];
      if (typeof value !== "string") return;
      if (!(name.startsWith("q_") || name.startsWith("open_q_"))) return;

      if (name.startsWith("q_")) {
        const radio = form.querySelector(
          'input[type="radio"][name="' + name + '"][value="' + value.replace(/"/g, '\\"') + '"]'
        );
        if (radio) {
          radio.checked = true;
          radio.dispatchEvent(new Event("change", { bubbles: true }));
          restoredAny = true;
        }
      } else {
        const field = form.querySelector('[name="' + name + '"]');
        if (field && (field.tagName === "INPUT" || field.tagName === "TEXTAREA")) {
          field.value = value;
          restoredAny = true;
        }
      }
    });
    return restoredAny;
  }

  // Restore first, then wire up autosave so the restore itself doesn't
  // immediately re-save (the existing draft already has these values).
  restoreDraft();

  let saveTimer = null;
  function scheduleSave() {
    if (saveTimer) clearTimeout(saveTimer);
    saveTimer = setTimeout(saveDraft, 250);
  }

  form.addEventListener("change", function (ev) {
    if (isAnswerInput(ev.target)) scheduleSave();
  });
  form.addEventListener("input", function (ev) {
    if (isAnswerInput(ev.target)) scheduleSave();
  });

  // The draft is intentionally NOT cleared on submit: if the server-side
  // validation fails (e.g. empty submission), the same quiz_attempt page
  // re-renders with an error and the student needs their answers to come
  // back. Clearing only happens when the result page actually loads — a
  // reliable signal that the submission was accepted.

  // Expose for tests / debugging; safe — only attempt-scoped data.
  window.LearnPilotQuizDraft = {
    storageKey: STORAGE_KEY,
    save: saveDraft,
    clear: clearDraft,
    read: readDraft,
  };
})();
