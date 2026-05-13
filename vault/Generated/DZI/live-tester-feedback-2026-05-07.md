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
