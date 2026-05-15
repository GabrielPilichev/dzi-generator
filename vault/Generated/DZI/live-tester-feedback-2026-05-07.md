---
title: "Live Tester Feedback — 2026-05-07"
type: tester_feedback
tags: [learnpilot, dzi, tester-feedback]
---

# Live Tester Feedback — 2026-05-07

## Tester feedback

- Browse mode looked clickable; users expected to select answers.
- Difficulty/review toolbar felt intrusive.
- DZI test creation was hard to find.
- Teacher flow was useful.
- Randomized questions were liked.
- Student quiz flow was clear and fast.
- Tester requested future mixed open + closed question tests.
- Broken old-bank questions appeared:
  - `Правилен отговор: —`
  - image-dependent question without image

## Changes made in response

- Review-mode banner.
- Collapsed toolbar.
- DZI test CTA.
- Invalid-question filtering.
- Clearer result page.
- Teacher share-link warning.

## Future decisions

- Mixed open/closed question design.
- Auto-grading vs teacher review for open answers.
- Asset extraction/mapping for DZI visual questions.
- Real deployment path.

## Latest tester feedback — 2026-05-13

Planning source: `docs/reviews/tester_feedback_plan.md`.

### P0 — timer / auto-submit bugs

- При задаване на времетраене 30 или 60 минути таймерът не работи; тестът
  приключва веднага.
- При задаване на времетраене 400 минути тестът стартира от 220 минути вместо
  400.

### P0 — authentication crash

- Ако паролата се пише на българска клавиатура, сайтът дава HTTP 500 Internal
  Server Error.

### P0/P1 — practical file download crash

- При натискане на практически файлове дава HTTP 500 Internal Server Error.
- P0 ако блокира всички практически файлове за активните тестери; P1 ако е
  ограничено до конкретен файл или източник.

### P1 — missing answers and explanations

- При грешен затворен отговор да се показва обяснение защо избраният отговор
  не е правилен.
- При грешен отворен отговор да се показва обяснение плюс правилния/приетия
  отговор.
- Когато се отвори тема и се изберат отворените въпроси за преглед, не излизат
  отговорите.
- При задачите за подготовка, въпрос 23 не показва кой е верният отговор.

### P2 — homepage topic search

- На началната страница е хубаво да има търсачка по теми.

## Recommended implementation sequence — 2026-05-13

1. PR A: fix timer duration and accidental auto-submit.
2. PR B: fix login/password 500 and practical file download 500.
3. PR C: show missing answers in open-question review and question 23.
4. PR D: add explanations for wrong MC/open answers.
5. PR E: homepage topic search.

## Non-goals — 2026-05-13

- Do not change DB imports.
- Do not change practical scoring.
- Do not mix practical score into the final score.
- Do not do broad refactors.
- Do not modify `data/questions.db`.
- Do not run migrations.
- Do not import questions/assets.
- Do not touch `vault/Generated/Quizzes/`.

## Checkpoint — 2026-05-15

Canonical planning note: `docs/reviews/tester_feedback_plan.md`.

### Fixed / merged

#### Critical quiz flow

- [x] Timer remaining time and accidental early auto-submit fixes.
- [x] Student validation for blank/too-short names and empty submissions.
- [x] Question randomization, draft autosave, progress indicator, and timer
  warning.

#### Login/download 500s

- [x] Bulgarian password input no longer causes HTTP 500.
- [x] Practical file download HTTP 500 fixed.

#### Review answers/explanations

- [x] Open/fill-in review answers are visible where reveal is intended.
- [x] Question 23 answer visibility fixed.
- [x] Wrong-answer feedback for MC/open answers.
- [x] Escaping/XSS coverage for answer displays.

#### Practical uploads/security

- [x] Practical upload hardening.
- [x] Practical review/download flow kept separate from quiz scoring changes.

#### Mobile/navigation UX

- [x] Homepage topic search.
- [x] Mobile DZI review scroll/filter polish.
- [x] Mobile profile/login entry.
- [x] Separate DZI preparation navigation.
- [x] Recent tests grouped/labeled by context.
- [x] Content/test cards clickable as whole cards where safe.

#### Results/analytics

- [x] Attempt duration display.
- [x] Success-rate display.
- [x] Difficulty breakdown / heatmap-lite display.

#### Review page polish

- [x] Show-all/hide-all reveal controls.
- [x] Review copy buttons.

### Needs manual smoke test

- Timer durations: 30, 60, 400, and 600 minutes.
- Mobile class page opens at top.
- DZI review button on phone.
- Practical file download.
- ZIP upload.
- Teacher review/download/score/note.
- Wrong-answer explanations.
- Homepage search.

### Remaining / not yet done

- Bigger username + password auth redesign, if still desired.
- More advanced analytics/heatmaps, if desired.
- Server-side autosave, if local browser autosave is not enough.
- Practical tasks/resources for sources beyond `may_2025_v2`, if still needed.
- Broader teacher dashboard improvements, if still needed.

### Next recommendation

Run a localhost or tunnel smoke test with testers. Capture screenshots and
exact routes for remaining bugs, then fix only confirmed issues.
