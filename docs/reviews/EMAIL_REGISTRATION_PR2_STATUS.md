# Email registration, PR 2 — mail interface and isolated sender: status and contract

Design: `docs/reviews/EMAIL_REGISTRATION_DESIGN.md` (sections 4, 3.5, 3.10, 7.5, 9).
Tests: `docs/reviews/EMAIL_REGISTRATION_TEST_PLAN.md`, section C.
Scope: `docs/reviews/EMAIL_REGISTRATION_SCOPE.md`.

**Status:** implemented in draft PR #336. An independent review of commit
`da5f28c53e0add8dffceeff3f12847f4e468c916` returned **REQUEST CHANGES
(narrow)**: two blockers (a flaky test, B1; a false acceptance when `SIGCHLD`
is ignored, B2), one portability note (N1) and a policy question on 4xx
replies, which Gabriel decided. The corrections for all four are below and are
**not independently reviewed**. A second review finding (P1: `smtplib.login`
swallowed a first `AUTH` reply of 421 and tried the next mechanism) is
corrected by making 421 terminal at the reply layer (see "Reply policy"); that
correction is not reviewed either. It is
the mail interface, the sender script, the backends and settings validation.
Nothing calls any of it: no route, registration service, ledger, settlement,
migration, table, unit or production start-up change exists, no gate exists to
turn on, and no message was sent to anyone by this work. Deployment notes:
`docs/DEPLOYMENT.md`, section 31.

## What PR 2 contains

| Part | File |
| --- | --- |
| Interface, settings, backends, the parent side of the exchange | `web/services/mail.py` |
| Standalone sender script (never imported) | `web/services/mail_sender.py` |
| Interface, settings, backends, the script's SMTP exchange against a fake peer | `tests/test_mail_service.py` |
| The process boundary against stand-in children, and the real script as a process | `tests/test_mail_sender_deadline.py` |
| Documents | this file; design 1.3 (the carried-over note); scope status; `docs/DEPLOYMENT.md` 31; `docs/TESTING.md` |

Not changed: `web/production.py`, `web/wsgi.py`, `web/app.py`, every blueprint,
every migration, `data/canonical`, `deploy/`, the vault.

## Interface (`web.services.mail`)

- `OutboundEmail(to, subject, text)`, frozen. Construction raises `ValueError`
  (never holding the value) unless `to` is one ASCII address of at most 254
  characters with nothing that could end a header or a command, `subject` is
  1–200 characters on one line and `text` is 1–2000 characters of plain text;
  control and format characters are refused, a line feed in `text` excepted.
  The address rule is structural. Which addresses the product accepts stays
  the form's rule (design 1.3). `repr` shows neither `to` nor `text`.
- `SendOutcome`: `ACCEPTED`, `NOT_SENT`, `REFUSED`, `UNKNOWN`.
- `Mailer.send(message) -> SendOutcome`: never raises for a delivery problem,
  never retries, creates at most one process.
- `sender_busy() -> bool`: true while this process has a sender it killed and
  could not reap. It polls, never blocks, and clears itself once the child is
  gone. It is a function of the module, not of a mailer, because the state
  belongs to the worker process.
- `settings_problems(environ, *, production)` and
  `load_settings(environ, *, production)`; `build_mailer(settings, *, production)`.
  `production` has no default: every caller states it.
- Backends: `SmtpMailer`, `ConsoleMailer`, `FileMailer`, `InMemoryMailer`.

## Outcome semantics, exactly

Decided by the parent from what the child wrote and how it ended.

| Evidence | Outcome |
| --- | --- |
| A sender of this process is unreaped; the process ignores `SIGCHLD`; the input would exceed 16 KiB; the interpreter or the script path is not absolute. No process is created. | `not_sent` |
| `Popen` raised `OSError`, `ValueError` or `SubprocessError` | `not_sent` |
| End-of-file on the output with nothing written, and the child exited by itself with 10–19 | `not_sent` |
| End-of-file with exactly one `R` and nothing else, exit 0 | `accepted` |
| End-of-file with exactly one `R` and nothing else, exit 20 | `refused` |
| Everything else: the deadline (with or without `R`); 16 bytes of output or any output that is not a single `R`; no end-of-file; a signal; any other status, `30` and `31` included, with the marker or without it; a status that contradicts the marker; `EPIPE` while writing the input, even when the child exited 10–19; any other exception in the parent once the child existed; any other exception from `Popen` | `unknown` |

`KeyboardInterrupt` and `SystemExit` propagate after the cleanup. `accepted`
means the server took the message for the recipient; it is never a statement
about a mailbox.

The script's statuses: `0`; `10` input, `11` lookup, connection or greeting,
`12` TLS, `13` EHLO, `14` AUTH, `15` sender refused, `19` anything else before
the marker (a marker that could not be written included); `20`; `30` for a
failure after the marker that is not a refusal, which the parent gives no
meaning to; `31` when the server answered 421 at any stage.

### Reply policy (approved by Gabriel after the review, option 3)

| Reply | Stage | Status | Outcome |
| --- | --- | --- | --- |
| 421 | any: greeting, EHLO, STARTTLS, AUTH, sender, recipient, data command, message | `31` | `unknown` |
| other 4xx | recipient command | `20` | `refused` |
| other 4xx | data command or the reply to the message | `30` | `unknown` |
| 5xx | recipient command, data command or the reply to the message | `20` | `refused` |
| other 4xx, or 5xx | before the marker | `10`–`19`, as before | `not_sent` |
| unreadable reply, 1xx–3xx where acceptance was expected, lost connection, timeout | after the marker | `30` | `unknown` |

`31` lies outside `10`–`19` on purpose: before the marker a 421 must not be
read as "nothing was sent", and it does not claim that a recipient was
transmitted either. `accepted` and `refused` still need end-of-file, exactly
one `R` and their status. Nothing is retried.

**421 is terminal at the reply layer.** The script connects with two small
subclasses, `_Smtp` (`smtplib.SMTP`) and `_SmtpSsl` (`smtplib.SMTP_SSL`), whose
`getreply()` reads the reply as `smtplib` does and, when its code is 421
(multi-line replies included), closes the connection and raises
`_ServiceUnavailable` with `smtp_code = 421`. It is deliberately not an
`SMTPAuthenticationError` (`smtplib.login` swallows that and tries the next
advertised mechanism, so a first `AUTH` answered 421 could be hidden by the
next attempt's disconnect, 535 or 503) and not an `SMTPException` or `OSError`.
Before the marker the existing handlers map it to `31` through its `smtp_code`;
after the marker `_after_marker` maps it to `31` before the generic handler.
No second `AUTH` and no later command follows a 421. Every other reply, and the
verifying TLS context, are untouched. A 421 to `QUIT` after a `250` for the
message still leaves the message `accepted`, as a failed `QUIT` always did.
`unknown` keeps the code and the
budgets and will count toward the circuit; `refused` will not. The circuit and
the ledger are PR 3 and are not implemented here.

### The `SIGCHLD` guard

With `SIGCHLD` ignored, CPython cannot wait for a child and reports status 0
for it; the reviewer reproduced `R` with exit 30 becoming `ACCEPTED`. `send`
now reads `signal.getsignal(signal.SIGCHLD)` before `Popen`. When it is
`SIG_IGN` it creates no process, logs `sender_error error=sigchld` and returns
`not_sent`. The disposition is never changed and no other signal handling was
added. **Caller assumption:** nothing changes the `SIGCHLD` disposition of the
process while a send is running; the check is made once, before the child
exists. **Limit of the guard:** Python keeps a cached disposition, and
`getsignal` reads that cache. A later change made at C level, outside Python,
is invisible to it, so the guard can be bypassed by code that ignores
`SIGCHLD` that way. The supported Gunicorn `sync` worker sets `SIGCHLD`
through Python, so the cache is accurate there. Callers must not ignore or
change the disposition during a send, by any route. Production code does not
widen the guard (no `ctypes`, no further signal handling); whether the real
worker ignores `SIGCHLD` is activation evidence, test plan L.

## The exchange

`[sys.executable, "-I", "-S", <absolute path of mail_sender.py>]`, no shell,
`close_fds=True`, standard error to the null device, an environment of
`LANG`, `LC_ALL`, `LC_CTYPE`, `SSL_CERT_FILE` and `SSL_CERT_DIR` where the
parent has them and nothing else. The settings and the message go to standard
input as one JSON document. Both pipes are non-blocking in one selector under
one monotonic deadline of 8 seconds from `Popen` returning. At most 16 bytes
of output are read, never more than are missing to 16, and nothing after the
cap, after end-of-file or after a kill. When both pipes are done the child is
waited for only for the time that remains. From `Popen` on, one `try`/`finally`
kills a child that has not ended, waits at most 2 seconds for it and closes
both pipes; a child still unreaped is remembered and logged as
`sender_unreaped`. `Popen.communicate` is not used.

The script resets `SIGALRM` to its default action, unblocks it and arms
`alarm(9)` as its first three statements, before any import but `signal`. It
reads at most 16 KiB plus one byte, checks every value and builds the whole
message before any network call. It gives the server `MAIL FROM:<sender>`
with no options, writes `R` with one `os.write`, and only then issues `RCPT`,
`DATA` and the message itself; `sendmail` is not used. STARTTLS and implicit
TLS use `ssl.create_default_context()` (certificate and host name verified,
TLS 1.2 at least); a server that does not offer STARTTLS ends the attempt, and
plain transport is refused unless the host is loopback, in the settings and
again in the child. Every socket operation has a 3-second timeout.

## Tests

`python3 -B -m unittest tests.test_mail_service tests.test_mail_sender_deadline`
(121 tests, about 27 seconds). No message is sent, nothing is connected to,
and every address, host and credential is synthetic.

| Plan | Where |
| --- | --- |
| C1–C4 | `test_mail_service`: the script's code under the real `smtplib` with a scripted peer; event order, exact command bytes, the parsed message |
| C9, C10 | `test_mail_service.SettingsTest` (the settings functions; see deviation 1 for the production start) |
| C5–C8, C11, C20 | `test_mail_sender_deadline`: stand-in children |
| C12–C14 | both files: static checks, the real command line, environment, descriptors, exact caps |
| C15 | both files: no record at any level, no child traceback |
| C17 | the mail half: unreaped state, `sender_busy`, `send` creating no process. The "no pending row, no ledger row" half needs PR 3 and PR 4 |
| C18, C19 | `AlarmTest`: a killed driver process, the real script's alarm under an ignored and a blocked `SIGALRM` |
| C21 | static: no `communicate`, one capped read |
| C16 | no test asserts a bound the design does not claim |

Not done as written: C5 (d) counts the bytes passed to `os.read` and does not
cap the parent's address space (the plan allows either). C19 runs the real
script's real nine-second alarm, three children at once; only stand-ins have
shortened alarms.

## Deviations and interpretations, for the reviewer

None changes the approved protocol. Each is the smallest reading I could find.

1. **A production start does not refuse `console` and `file` yet (C9, K1, K2);
   the wiring stays deferred to PR 4.**
   Design 7.5 says that while the registration gate is off production "never
   refuses to start" because of mail settings, and the gate constant arrives
   with the registration modules (PR 3 and PR 4). So PR 2 ships the refusal as
   `settings_problems(..., production=True)` and
   `build_mailer(..., production=True)`, which `production_config_errors` is to
   call once the gate exists, and leaves `web/production.py` unchanged. Wiring
   it now would either contradict 7.5 or need a gate this PR may not add.
2. **`Popen` raising is `not_sent` only for `OSError`, `ValueError` and
   `SubprocessError`.** The design row says "`Popen` raised". Any other
   exception from it (a `MemoryError`, for example) does not establish that no
   process exists, so it is `unknown`. Narrower, never wider.
3. **Which replies are a refusal** was a question for the review and is now
   decided: the reply policy above, recorded in design 4.2. It replaces the
   first version of this PR, in which every 4xx after the marker was `refused`.
4. **`sender_busy()` is a module function**, so `Mailer` stays the one-method
   protocol of design 4.1.
5. **Two log events** on `learnpilot.registration`, both warnings:
   `sender_unreaped` and `sender_error error=<word>`, now both listed in design
   3.10. The word is fixed (`sigchld`, `input`) or an exception class name. It
   is never a value and no traceback is attached, so the events are safe to
   hand to whatever logging PR 4 sets up; no handler is configured here.
6. **Three more "no process" cases return `not_sent`**: an input over 16 KiB (no
   valid message reaches it), and an interpreter or script path that is not
   absolute.
7. **Message details the design leaves open:** `Date` and `Message-ID` headers
   (the latter on the sender's domain, so no host name lookup is made for it);
   a base64 body, so the message is 7-bit; the EHLO name is `smtplib`'s default,
   which resolves the local host name inside the child before connecting.
8. **Settings details:** host, port and security are all required for `smtp`
   (no default port); `DZI_MAIL_FROM` is `address` or `Name <address>` with a
   stricter rule than the form's (no hyphen-edge label); credentials are
   printable ASCII, set together; SMTP values are checked whenever they are set.
9. **The in-memory backend** takes an outcome or a callable of the message, as
   `OutboundEmail` has no "kind"; its delay is handed to an injected `sleep`
   and never slept for real.

## Caller obligations (PR 3 and PR 4)

- Read `mail.sender_busy()` **before** anything is admitted and pass it to
  `registration.availability(..., sender_busy=...)`: no row, no ledger entry,
  no hash while it is true.
- Call `send` with no transaction open, after the commit; settle afterwards.
  Map an exception that escapes a backend to `unknown` (test H4). Never retry.
- Treat `ACCEPTED` as "the relay took it" only.
- Pass `production=True` on the production path and report
  `settings_problems` through `production_config_errors` once the gate is on;
  log one warning naming the settings (`mail.SETTING_NAMES`) while it is off.
- Give `learnpilot.registration` a handler as the other loggers have; until
  then only its warnings reach standard error.
- Enforce `MailSettings.hourly_cap`; nothing here counts sends.
- Call `send` from one thread at a time per process. A second thread is not
  refused and no lock was added: the one-sender statement rests on the pinned
  deployment (`worker_class = "sync"`, `threads = 1`). Calls from a threaded
  development server may overlap.
- Do not ignore `SIGCHLD` in a process that sends, and do not change its
  disposition while a send is running.

## Not verified, and what must not be concluded

- **Everything in this worktree ran on macOS** (Darwin 25.5.0, arm64), CPython
  3.12.15, OpenSSL 3.6.5, with the kqueue selector and, as a second run, `poll`
  and `select`. Python 3.13 was not available here, so the command-casing
  correction (N1) was not run on it locally.
- **Linux findings of the independent review, as reported by the reviewer:**
  the sender and the exchange were exercised on Linux with `epoll`, against
  fake peers over real TLS, and with inherited signal states. That is review
  evidence about the code. **It is not deployment evidence:** the actual
  systemd unit and its sandboxing, Gunicorn's worker replacement on the server,
  `sys.executable` being the deployment's virtual-environment interpreter, any
  provider and any capacity figure were not tested by anyone. Those remain the
  activation evidence of design 0.3 and test plan L, and nothing was validated
  on the VPS.
- No real mail server, provider, certificate or DNS name was involved. TLS is
  tested as "the verifying context is the one handed to `smtplib`", not as a
  handshake.
- No capacity figure. Interpreter start-up before the alarm, process creation
  and the whole request remain unbounded, as the design says; senders of
  different processes are not excluded from each other.
