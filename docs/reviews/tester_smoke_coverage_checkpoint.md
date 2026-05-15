# Tester Flow Smoke Coverage Checkpoint

Docs-only checkpoint for the automated tester-flow smoke coverage added after
the 2026-05 tester feedback batch. Manual localhost/tunnel testing is not
available right now, so this note separates what is covered by automated tests
from what still needs real-browser and phone validation later.

## Automated Smoke Coverage Now In Place

The automated smoke coverage checks that key routes render without HTTP 500 and
include stable UI markers for the recently fixed tester-reported flows.

- Homepage loads and includes the topic search input.
- Homepage includes DZI preparation / navigation markers.
- Homepage includes the mobile profile/login entry for tester/admin access.
- Tester and admin login with a Bulgarian wrong password returns a normal login
  error instead of HTTP 500.
- Section review pages include answer reveal controls, the show-all/hide-all
  control, copy button markup, filters, and `section-tools.js`.
- Active quiz attempt pages include timer, autosave script, progress indicator,
  and low-time warning markers.
- Quiz result pages include duration, success rate, difficulty breakdown, and
  wrong-answer feedback markers.
- Practical task pages for `may_2025_v2` render and expose resource download
  links where available.
- Invalid practical resource/download IDs return a safe response, currently
  HTTP 404, rather than HTTP 500.
- The default verification block remains the full test suite, SQLite foreign
  key check, DZI state audit, open-question readiness audit, and git status.

## Still Requires Manual Testing Later

These checks require a real browser, phone viewport, runtime tunnel behavior, or
human judgment and are not fully proven by template-level smoke tests.

- Phone opens a class page at the top, not at the bottom.
- DZI review button works on an actual phone.
- Mobile filters do not block or cover content.
- Cloudflare tunnel login flow works with the runtime `ProxyFix` setup.
- Practical resource downloads work through the tunnel.
- ZIP upload works through the browser.
- Teacher/admin can download an uploaded ZIP and save a score/note.
- Autosave restores after a real browser refresh.
- Progress and timer warning update live while the attempt is open.
- Copy button actually writes to the clipboard.
- Clickable cards feel correct on touch devices.

## Recommended Manual Test Message For Testers

```text
Здравейте! Моля, тествайте LearnPilot през този линк:
<TESTER_LOGIN_URL>

Парола за тестер:
<TESTER_PASSWORD>

Моля, проверете:
- вход като тестер и отваряне на тест;
- начална страница, търсене по теми и ДЗИ подготовка;
- страница с клас/раздел на телефон;
- преглед на въпроси: показване/скриване на отговори и копиране;
- решаване на тест: таймер, предупреждение, прогрес и възстановяване след refresh;
- резултати: време, успеваемост, трудност и обяснения при грешка;
- практически задачи: сваляне на ресурс, ZIP upload, преглед от учител.

Ако видите проблем, изпратете:
- screenshot или кратко видео;
- точния линк/route;
- телефон/браузър;
- какви стъпки доведоха до проблема.
```

## Safe Verification Command Block

```bash
python3 -m unittest discover -s tests
sqlite3 "file:data/questions.db?mode=ro" "SELECT * FROM pragma_foreign_key_check;"
python3 src/audit_dzi_state.py
python3 src/audit_open_question_readiness.py
git status --short
```

## Non-Goals

- No feature changes in this docs PR.
- No DB/import/schema changes.
- No generated quiz artifacts.
- No server/tunnel start/stop.
