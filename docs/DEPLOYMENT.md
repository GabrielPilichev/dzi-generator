# Production deployment (Ubuntu 24.04, 1 vCPU, 2 GB RAM, 30 GB disk)

```
Internet -> Nginx (80/443) -> Gunicorn 127.0.0.1:8000 (1 sync worker) -> Flask (web.wsgi:app) -> SQLite/WAL
```

Templates in `deploy/` already use the layout below and install without path
edits. Replace `learnpilot.example.com` in the Nginx template with the real
domain (production: `learnpilot.bg`) only on the server; never commit domains, IP addresses or secrets.

| Path | Purpose | Owner / mode |
| --- | --- | --- |
| `/opt/learnpilot/app` | Git checkout (code, templates, static files) | `root`, world-readable; read-only to the service |
| `/opt/learnpilot/venv` | Python virtual environment | `root` |
| `/etc/learnpilot/learnpilot.env` | Secrets and state paths | `root:root` `0600` |
| `/etc/learnpilot/backup.env` | Backup state paths only | `root:root` `0600` |
| `/var/lib/learnpilot/data/questions.db` | Live database (`DZI_DB`) | `learnpilot` `0600` (directory `0750`) |
| `/var/lib/learnpilot/data/reference` | Official DZI reference resources (`DZI_REFERENCE_ROOT`); read, never written, by the app; not in snapshots | `learnpilot` `0750` |
| `/var/lib/learnpilot/uploads/practical` | Student uploads (`DZI_UPLOAD_ROOT`) | `learnpilot` `0750` |
| `/var/lib/learnpilot/vault` | Generated quiz notes (`DZI_VAULT`) | `learnpilot` `0750` |
| `/var/backups/learnpilot` | Completed application snapshots | `learnpilot` `0700` |
| `/var/lib/learnpilot/lifecycle-operator.log` | Append-only log of lifecycle operator actions (`src.manage_lifecycle --audit-log`, section 26) | `learnpilot` `0600` |

The repository does not track a database (`data/questions.db` is an ignored local
runtime file). Production state must stay
outside the checkout so a `git pull` can never replace live data; startup
refuses state paths inside `/opt/learnpilot/app`.

## 1. Ubuntu prerequisites

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y git rsync python3 python3-venv nginx sqlite3 certbot python3-certbot-nginx ufw
```

Add 1 GB of swap as emergency protection only:

```bash
sudo fallocate -l 1G /swapfile && sudo chmod 600 /swapfile
sudo mkswap /swapfile && sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
echo 'vm.swappiness=10' | sudo tee /etc/sysctl.d/99-learnpilot-swap.conf && sudo sysctl --system
```

## 2. Dedicated user and directories

```bash
sudo adduser --system --group --home /var/lib/learnpilot --no-create-home learnpilot
sudo install -d -o root -g root -m 0755 /opt/learnpilot
sudo install -d -o root -g learnpilot -m 0750 /etc/learnpilot
sudo install -d -o learnpilot -g learnpilot -m 0750 /var/lib/learnpilot \
  /var/lib/learnpilot/data /var/lib/learnpilot/data/reference \
  /var/lib/learnpilot/uploads /var/lib/learnpilot/uploads/practical /var/lib/learnpilot/vault
sudo install -d -o learnpilot -g learnpilot -m 0700 /var/backups/learnpilot
# The restore script's own records and guard (section 34); root's only. The script makes it if it is missing.
sudo install -d -o root -g root -m 0700 /var/lib/learnpilot-restore

# Keep SSH access while enabling the firewall before public HTTP/HTTPS setup.
sudo ufw allow OpenSSH
sudo ufw allow 'Nginx Full'
sudo ufw enable
sudo ufw status verbose
```

## 3. Repository deployment

```bash
sudo git clone <repository-url> /opt/learnpilot/app
cd /opt/learnpilot/app && sudo git checkout <release-commit-or-tag>
```

For a private GitHub repository, provision read-only deploy-key or other
authenticated Git access before cloning. Keep the private key outside the
checkout; do not commit it or place it in the service environment file.

`data/reference/` (official DZI PDFs/ZIP resources for practical-task
downloads) is git-ignored and is never placed in the production checkout. The
database stores these resources as `data/reference/...`; in production
`DZI_REFERENCE_ROOT` (section 6) maps that prefix to
`/var/lib/learnpilot/data/reference`. If those downloads are needed, copy the
tree from a trusted workstation, keeping the same layout below `data/reference/`:

```bash
rsync -a data/reference/ <admin>@<server>:/tmp/learnpilot-reference/   # on the workstation
sudo rsync -a --chown=learnpilot:learnpilot --chmod=D755,F644 \
  /tmp/learnpilot-reference/ /var/lib/learnpilot/data/reference/
```

The workstation copy remains the source: snapshots (section 16) do not contain
reference data.

## 4. Python virtual environment

```bash
sudo python3 -m venv /opt/learnpilot/venv
sudo /opt/learnpilot/venv/bin/pip install --upgrade pip
```

## 5. Dependencies

```bash
sudo /opt/learnpilot/venv/bin/pip install -r /opt/learnpilot/app/requirements.txt
```

`requirements.txt` constrains Flask, Werkzeug and Gunicorn (the production server).
It also declares `segno` (pure Python, no dependencies of its own), which draws the
assignment QR codes of section 25; the application does not start without it.
Google sign-in (section 29) adds `Authlib`, `joserfc`, `requests` and `urllib3`,
and raises the floor of `cryptography` to 45.0.1, which Authlib 1.8 and joserfc
require. They are imported when the application is loaded, like every other
dependency: the application does not start without them, whether Google sign-in
is enabled or not, so install the requirements before restarting on this build.
Gunicorn is held to one major version on purpose: the error-log filter of
section 18 depends on the wording of one of its messages.
Record the first-deploy runtime and installed versions as deployment evidence:

```bash
/opt/learnpilot/venv/bin/python --version
/opt/learnpilot/venv/bin/pip freeze
```

## 6. Environment configuration

```bash
sudo install -o root -g root -m 0600 \
  /opt/learnpilot/app/deploy/learnpilot.env.example /etc/learnpilot/learnpilot.env
python3 -c 'import secrets; print(secrets.token_urlsafe(48))'   # value for DZI_SECRET_KEY
sudoedit /etc/learnpilot/learnpilot.env
```

| Variable | Required | Rule |
| --- | --- | --- |
| `DZI_SECRET_KEY` | yes | at least 32 characters; keep stable (rotating it signs everyone out) |
| `DZI_TOTP_KEY` | for admin accounts | Fernet key that encrypts admin TOTP secrets (`python3 -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'`). Normal TOTP verification needs it: unset or wrong, a TOTP code fails closed (503). Single-use recovery codes do not need it and stay usable as the temporary recovery path. Unset: startup is allowed. A malformed configured value is a configuration error and stops startup. Keep it stable and backed up apart from database backups; if it is lost, restore the correct key, or use `totp-reset` and re-enrol each admin |
| `DZI_DB` | yes | absolute path of the existing live database, outside the checkout |
| `DZI_UPLOAD_ROOT` | yes | absolute, existing, writable directory outside the checkout |
| `DZI_VAULT` | yes | absolute, existing, writable directory outside the checkout |
| `DZI_REFERENCE_ROOT` | no | absolute, existing, readable directory outside the checkout holding the contents of `data/reference/`. Unset: the app looks in the checkout's `data/reference/`, which production does not have, so practical-task downloads show as unavailable |
| `DZI_PUBLIC_HOST` | yes | the real public DNS hostname in Nginx `server_name`, without scheme or port. It is exactly **one** trusted hostname: a future `www` alias must be deliberately supported in the application (and Certbot), not merely added to Nginx, or Flask answers it with 400 |
| `DZI_PUBLIC_BASE_URL` | **must be unset** | development-only override of the address in student links and QR codes (section 25). Production always uses `https://` + `DZI_PUBLIC_HOST`; startup rejects this variable when it is set |
| `DZI_GOOGLE_CLIENT_ID`, `DZI_GOOGLE_CLIENT_SECRET` | no; **leave both unset** | Google sign-in (section 29). This build keeps Google sign-in disabled in production whatever they hold: set, they only cause one startup warning that names them. In a later build that enables it, both are set together or neither: the client id is visible ASCII of at most 255 characters ending in `.apps.googleusercontent.com`, the secret 16 to 255 visible ASCII characters, and startup then rejects one without the other or a malformed value. The secret is never typed on a command line or stored in the repository |
| `DZI_SOURCE_URL` | no | optional footer link |
| `DZI_SESSION_COOKIE_SECURE` | no | leave unset; any false value is rejected (HTTPS-only cookies) |
| `FLASK_DEBUG` | no | must not be enabled; startup rejects it |

`web.wsgi` validates all of this before importing the app and stops with a
message naming the problem settings (never their values). Development runs
(`python3 web/app.py`, tests, the tunnel runner) keep their existing defaults.
Production values: `DZI_DB=/var/lib/learnpilot/data/questions.db`,
`DZI_UPLOAD_ROOT=/var/lib/learnpilot/uploads/practical`,
`DZI_VAULT=/var/lib/learnpilot/vault`,
`DZI_REFERENCE_ROOT=/var/lib/learnpilot/data/reference`,
`DZI_PUBLIC_HOST=learnpilot.bg`. An environment file installed before
`DZI_REFERENCE_ROOT` existed needs that line added by hand (`sudoedit`); the
Gunicorn check in section 10 validates it before a restart.
systemd reads `EnvironmentFile` as manager before dropping to `User=learnpilot`;
the service user does not need read access to the root-owned secret file.

## 7. Writable paths and permissions

The systemd unit makes the whole filesystem read-only for the service
(`ProtectSystem=strict`) except `/var/lib/learnpilot`. The app writes only:

- `DZI_DB` and its `-wal`/`-shm` sidecars (the directory must be writable);
- uploads under `DZI_UPLOAD_ROOT` (files become `0640` via `UMask=0027`);
- quiz notes and DZI training sets under `DZI_VAULT`;
- temporary upload spool files in the service's private `/tmp`.

It only reads `DZI_REFERENCE_ROOT`. Only stored `data/reference/...` resource
paths resolve there; `data/assets/...` always resolves inside the checkout.

Nginx (`www-data`) needs read access to `/opt/learnpilot/app/web/static/`
(the default world-readable checkout is enough). Uploads are never served by
Nginx; downloads go through the app's authorization checks.

**Uploads**: `src/practical_uploads.py` allows 25 MiB per file
(`MAX_UPLOAD_BYTES`), at most 20 files per request (`MAX_FILES_PER_REQUEST`)
and a 101 MiB request body (`MAX_REQUEST_BYTES`: four maximum-size files plus
1 MiB of multipart overhead, enforced by Flask `MAX_CONTENT_LENGTH`). Nginx
uses `client_max_body_size 102m`, just above the application limit, so every
request the app accepts passes the proxy; change both together. Larger
requests are rejected (Nginx 413, or the app's upload error message). A
maximum-size request is buffered once by Nginx and once in the service's
private `/tmp`. The upload filesystem keeps at least 2 GiB free after the
whole incoming request. Each quiz attempt and practical task may retain at
most 100 MiB and 40 files in total across requests (including soft-deleted
rows). A rejected request stores no files or database rows. Files are stored
under token names and never extracted. Review `DZI_UPLOAD_ROOT` growth with
`sudo du -sh /var/lib/learnpilot/uploads` and `df -h`.

The DZI training class mix is tracked at `deploy/dzi-training-class-mix.json`.
The teacher generator reads `DZI_DB` and writes both Markdown and JSON under
`DZI_VAULT/Generated/DZI-Training/sets`; it does not write to the checkout.

## 8. Database preparation (first deployment)

Create the live database from the migrations and the canonical content with the
builder (it creates a new file and refuses to overwrite one). An existing
live database is never rebuilt; it is migrated and backed up (sections 16-20).

```bash
cd /opt/learnpilot/app
sudo /opt/learnpilot/venv/bin/python -m src.build_database \
  --output /var/lib/learnpilot/data/questions.db
sudo chown learnpilot:learnpilot /var/lib/learnpilot/data/questions.db
sudo -u learnpilot sqlite3 /var/lib/learnpilot/data/questions.db 'PRAGMA journal_mode=WAL;'
```

(`/var/lib/learnpilot/data` must already exist, owned by `learnpilot`; see section 2.)

The `PRAGMA journal_mode=WAL;` above is the only place WAL is chosen. Journal mode
is persistent database state, so the application (`web/db.py`) never sets it:
opening connections and serving requests leave the file's journal mode and header
untouched. The restore script of section 17 sets it on a restored database; repeat
the pragma for any database created some other way.

## 9. Schema / pre-start step

Production never changes schema on import: `web.wsgi` disables the
development-only quiz-table guard in `web.app`, and startup refuses to run
while any `web/migrations/*.sql` file is missing from `schema_migrations`.
Schema changes happen only through this explicit step, with the service
stopped or before restarting it:

```bash
cd /opt/learnpilot/app
sudo -u learnpilot /opt/learnpilot/venv/bin/python -m src.manage_migrations \
  --db /var/lib/learnpilot/data/questions.db --dry-run
# Only if migrations are pending: take a complete snapshot (section 16) first, then
sudo -u learnpilot /opt/learnpilot/venv/bin/python -m src.manage_migrations \
  --db /var/lib/learnpilot/data/questions.db --apply
```

The runner's own automatic backup covers only the repository database
(`data/questions.db` in a checkout), so always back up the production database
first. That automatic backup is taken with the SQLite backup API, so it is a
consistent standalone file that includes committed pages still in the WAL, and
if it cannot be created or fails `quick_check` nothing is migrated. It is a
developer convenience, not a substitute for the section 16 snapshot. The
development-only request-time `ALTER TABLE` guard for teacher answer review is
disabled in production as well, so no request path runs schema code.

### Table-rebuild migrations

SQLite cannot alter or drop a table-level constraint; the table has to be
rebuilt (create the new table, copy the rows, drop the old table, rename the
new one). On the runner's normal connection foreign keys are enforced, and
there `DROP TABLE` first deletes the table's rows: that cascades into every
`ON DELETE CASCADE` child and fails on every other referencing table. A
migration that needs a rebuild therefore opts in with this exact comment line,
alone on its line, before the file's first SQL statement:

```sql
-- learnpilot-migration: table-rebuild
```

Use it only for a rebuild of a table that other tables reference. Additive
migrations (new table, new column, new index) stay ordinary and keep foreign
keys enforced. Any other comment line starting with `-- learnpilot` is refused
as a malformed directive, in `--dry-run` as well as `--apply`. `--dry-run`
marks such a file `(table-rebuild)`.

What the runner does for a marked file, and only for a marked file:

1. Switches `foreign_keys` off **before** the transaction (SQLite ignores the
   pragma inside one) and confirms it is off.
2. Opens one transaction (`BEGIN IMMEDIATE`) and runs the file's SQL in it.
   While the file runs, a SQLite authorizer refuses every statement that could
   end or replace that transaction or reach another database file: `COMMIT`,
   `END`, `ROLLBACK`, a second `BEGIN`, `SAVEPOINT`, `RELEASE`, `ROLLBACK TO`,
   `ATTACH` and `DETACH`. SQLite's own parser makes that decision, so the
   position of the statement in the file does not matter, and the same words
   in comments, string literals or a trigger body are not affected.
3. Runs `PRAGMA foreign_key_check` **before** committing.
4. Inserts the `schema_migrations` row in the same transaction and commits.
5. Switches `foreign_keys` back on and confirms it, on success and on every
   failure. An ordinary migration is refused while enforcement is off.

If the SQL fails, contains a refused statement, the check reports any
violation, or the ledger row cannot be written, the whole transaction rolls
back: schema, data and `schema_migrations` are exactly as before, the run stops
with a non-zero exit status and later migrations are not attempted. Schema and
ledger can only commit together, in the runner's single `COMMIT`. Because the
check covers the whole database, a violation that existed before the migration
also blocks it; fix it first.

The runner guarantees that the transaction is atomic. `PRAGMA
foreign_key_check` validates only the foreign-key relationships of rows that
exist: it does not validate the resulting schema, and with empty referencing
tables (a fresh database) it has nothing to check. Nor does it show that the
copy is complete: a rebuild that silently leaves rows behind without orphaning
any child row passes. The migration's own tests remain responsible for the
resulting schema (every expected table, index, trigger and view, also on a
fresh database) and for row completeness (row counts and ids of the old and
the rebuilt table).

Rules for the author of such a file:

- No transaction, savepoint, `ATTACH` or `DETACH` statement: the runner owns
  the transaction and the file is rolled back at the first one. A leading
  `PRAGMA foreign_keys = ON;` is inert inside the transaction; do not set any
  other pragma, because connection-level pragmas outlive the transaction.
- Use exactly this order: create the replacement table, copy the rows, `DROP`
  the old table, `RENAME` the replacement to the original name. It is the only
  supported order and behaves the same on every tested SQLite build. Do not
  rename the old table away first: depending on the SQLite build and its
  `legacy_alter_table` setting, the referencing tables may follow the renamed
  table, even with foreign keys off. Test the migration on the interpreter
  that production runs.
- The runner preserves nothing by itself. The file must recreate every index,
  trigger and view of the rebuilt table and copy rows with their ids.
- Carry the `AUTOINCREMENT` high-water mark over explicitly, by updating the
  new table's `sqlite_sequence` row from the old one before dropping the old
  table. Otherwise the sequence drops to `MAX(id)` and the id of a deleted row
  is reused.

A table-rebuild migration is a **high-risk migration**. On production, apply it
only with the service stopped (no other writer) and a complete section 16
snapshot taken immediately before and confirmed; restoring that snapshot
(section 17) is the only way back after a successful apply. Run
`PRAGMA foreign_key_check` on the snapshot beforehand so a pre-existing
violation is found before the maintenance window.

`015_quiz_attempts_user.sql` is the one migration that uses the directive. It
rebuilds `quiz_attempts`: adds the nullable `user_id` (the owning account;
every existing row keeps NULL and nothing is linked to an account by name),
removes the table-level `UNIQUE (assignment_id, student_name)` and replaces it
with two partial unique indexes (one attempt per account and assignment; one
per name among the attempts without an account), keeps every row, id and the
`AUTOINCREMENT` high-water mark, and leaves the rows of `quiz_answers`,
`quiz_text_answers` and `practical_submissions` untouched. A database that
already recorded `016` plans and applies `015` when it arrives, because pending
migrations are every file not in `schema_migrations`, in filename order; a
fresh database runs it before `016`.

**Before deploying the version that contains 015 for a class:** starting a quiz
now requires a signed-in account. Students without an account can no
longer start one, so create their accounts first (section 22). Attempts started
before the deployment stay reachable from the browser that started them.

### One-time baseline adoption (`000_baseline.sql`)

`web/migrations/000_baseline.sql` is the schema-only baseline: the core tables
that existed before migration 001 (curriculum, exams, questions and their
indexes). It contains no data and nothing that 001-010 create. A **fresh, empty**
database runs it through a normal `--apply`, followed by 001-010.

A database that already has this schema (the production database included) does
**not** run 000; it records it. Adoption is bookkeeping only (one
`schema_migrations` row), it never creates or alters application schema, and it
is refused unless every baseline table, column, index and constraint is really
present. Until 000 is recorded, startup refuses the database like any other
pending migration, and `--apply` refuses a non-empty database that has not
recorded 000.

Rollout order for the first release that contains 000, from the new release
checkout and before the service is restarted onto it:

1. Take a complete snapshot (section 16).
2. `python3 -m src.manage_migrations --db <production db> --baseline-dry-run`
   must report `000_baseline.sql: baselineable` and no remaining pending
   migrations. Anything else: stop and investigate; do not adopt.
3. `python3 -m src.manage_migrations --db <production db> --baseline-apply`.
4. Deploy and restart as usual.

Canonical content remains separate from the schema baseline and is not part of
this step.

Before the first public start, run the complete suite inside the server venv
with isolated temporary test databases, and retain the version evidence:

```bash
cd /opt/learnpilot/app
/opt/learnpilot/venv/bin/python --version
/opt/learnpilot/venv/bin/pip freeze
/opt/learnpilot/venv/bin/python -B -m unittest discover -s tests
```

## 10. Gunicorn

`deploy/gunicorn.conf.py`: bind `127.0.0.1:8000`, 1 `sync` worker, 1 thread,
30 s timeout, `preload_app` (configuration errors stop the master), access and
error logs on stdout/stderr, no daemon mode, no reloader. The access-log format
has no query string and the error log passes a redacting logger class; both are
described in section 18, and `loglevel` must stay `info`. Check configuration,
production settings and schema without binding a port:

```bash
sudo systemd-run --quiet --pipe --wait --uid=learnpilot --gid=learnpilot \
  -p EnvironmentFile=/etc/learnpilot/learnpilot.env -p WorkingDirectory=/opt/learnpilot/app \
  /opt/learnpilot/venv/bin/gunicorn --config deploy/gunicorn.conf.py --check-config
```

`systemd-run` reads the environment file exactly as the service unit does
(shell sourcing would interpret `$`, quotes and spaces differently). The check
imports the application, so it shows what a start would log: with either Google
setting present, the one warning of section 29.

## 11. systemd

```bash
sudo cp /opt/learnpilot/app/deploy/systemd/learnpilot.service /etc/systemd/system/
grep -n '/opt/learnpilot' /etc/systemd/system/learnpilot.service
sudo systemctl daemon-reload
sudo systemctl enable --now learnpilot
systemctl status learnpilot
curl -fsS http://127.0.0.1:8000/healthz
```

The unit runs as `learnpilot`, reads secrets only from the environment file,
restarts on failure (with a start limit for configuration errors) and stops
gracefully (SIGTERM, 30 s grace). Use `sudo systemctl restart learnpilot` to
deploy code: with `preload_app`, a reload would keep the old code.

## 12. Nginx

```bash
sudo cp /opt/learnpilot/app/deploy/nginx/learnpilot.conf /etc/nginx/sites-available/learnpilot
sudoedit /etc/nginx/sites-available/learnpilot        # replace learnpilot.example.com
sudo ln -s /etc/nginx/sites-available/learnpilot /etc/nginx/sites-enabled/learnpilot
sudo rm /etc/nginx/sites-enabled/default               # the template has its own default_server
sudo nginx -t && sudo systemctl reload nginx
```

The template proxies to `127.0.0.1:8000`, overwrites `X-Forwarded-For/Proto/Host`
(the app trusts exactly one proxy hop), buffers requests and responses so slow
clients never hold the single worker, gzips text responses, serves
`/static/` directly with a short cache (assets are not fingerprinted), drops
unknown host names and denies hidden files. The application also validates the
configured public Host after ProxyFix; only local health probes may use
`localhost` or `127.0.0.1`. The template includes `nosniff`, `DENY` framing and
`strict-origin-when-cross-origin` referrer headers.

Only POSTs are rate limited per public IP: each login has 5 requests/minute
with burst 10; quiz start/submission has 20 requests/second with burst 90;
practical uploads have 2 requests/second with burst 45. "Quiz start" is the
student entry `/quiz/s/<public_token>`; the retired numeric `/quiz/<id>`
(section 24) stays in the same bucket as defense in depth, although the
application answers 404 for it. Empty GET keys are
not charged. These bursts allow at least 30 students behind one school NAT to
submit together; Flask ownership, file quotas and the disk floor still enforce
the application rules. Nginx responds 429 when a bucket is exhausted.

The one GET that is limited is the Google callback (section 29), in a zone of
its own keyed for every method. That location also keeps the callback's query
string, which carries the authorization code and the state, out of both Nginx
logs, and answers every error status with a redirect. The Google routes are in
the template whether or not Google sign-in is enabled: disabled, the
application answers 404 behind them.

## 13. DNS

Create an `A` record (and `AAAA` if the VPS has IPv6) for the domain pointing
at the VPS. Wait until `dig +short <your-domain>` returns the server address
before requesting a certificate.

## 14. HTTPS / Certbot

```bash
sudo certbot --nginx -d <your-domain>     # choose the HTTP -> HTTPS redirect
sudo certbot renew --dry-run
```

Certbot adds the TLS listener and redirect to the site file. Session cookies
are `Secure` (and `HttpOnly`, `SameSite=Lax`; section 23), so logging in works
only over HTTPS. After HTTPS works, HSTS can
be added to the TLS server block:
`add_header Strict-Transport-Security "max-age=31536000" always;`

## 15. Firewall

The firewall was enabled in section 2, before public deployment. Keep SSH,
HTTP (Certbot challenge) and HTTPS open. Never open port 8000; Gunicorn listens
on loopback only. After Certbot, configure a TLS default server to reject
unknown names once the certificate paths are known; do not guess certificate
paths in the pre-Certbot template.

## 16. Backup

The scheduled snapshot combines the existing WAL-safe SQLite online backup
with practical uploads and `DZI_VAULT/Generated` (quiz notes and DZI training
sets). It creates a private `snapshot-<UTC timestamp>/` containing
`questions.db` and `files.tar.gz`, then publishes the complete directory.
The live SQLite file is never placed in the tar archive.

A snapshot is all-or-nothing: if `snapshot-<stamp>/` exists, both files were
written and verified; if anything fails the command exits non-zero, no
snapshot is published and every partial file is removed. There is deliberately
no database-only fallback, so a directory named `snapshot-*` always means a
complete snapshot.

The database in a snapshot is **logically scrubbed of unverified registrations**
(registration PR 5, decision D18 (b); section 34): in the isolated copy the
backup API wrote, never in the live database, every pending registration becomes
`void` and loses its address, names, password hash and code hash, before the
copy is checked, flushed, published and hashed. Accounts, profiles, canonical
content and the send ledger are kept. This is a logical change and not an
erasure; section 34 lists what it does not remove.

Reference data (`DZI_REFERENCE_ROOT`) is **not** in the snapshot. It is static
official source material that the application never changes, provisioned from
the workstation copy (section 3). Keep that copy; after losing the server,
re-provision reference data from it rather than from a snapshot.

Before doing any work the tool estimates the space needed from what it will
actually store (the database and its WAL, the uploads tree and
`DZI_VAULT/Generated`; never SHM, env files or other vault content), adds tar
overhead and 10 %, assumes no compression, and requires 256 MiB of headroom on
top. If the backup filesystem has less free space it stops immediately with
"not enough free space". Uploads may legitimately fill the disk down to the
application's 2 GiB reserve, so keep the backup directory on a filesystem with
room (or a separate volume) and move old snapshots off the server regularly.
The tool refuses layouts where the backup directory, uploads, vault or
database overlap or nest inside one another.

Configure the backup unit with only state paths, without application secrets:

```bash
sudo install -o root -g root -m 0600 \
  /opt/learnpilot/app/deploy/learnpilot-backup.env.example /etc/learnpilot/backup.env
sudoedit /etc/learnpilot/backup.env  # confirm the same external state paths as learnpilot.env
cd /opt/learnpilot/app
sudo -u learnpilot /opt/learnpilot/venv/bin/python -m src.backup_application \
  --db /var/lib/learnpilot/data/questions.db \
  --uploads /var/lib/learnpilot/uploads/practical \
  --vault /var/lib/learnpilot/vault --output-dir /var/backups/learnpilot
```

The SQLite snapshot uses `src.backup_sqlite`, verifies `PRAGMA quick_check`
and stores files with mode `0600` in a `0700` directory. During heavy live
uploads, a file can change after the DB snapshot but before the tar finishes;
for a strict point-in-time restore, briefly pause writes or take a maintenance
snapshot. Verify backups with a test restore.

Daily schedule:

```bash
sudo cp /opt/learnpilot/app/deploy/systemd/learnpilot-backup.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now learnpilot-backup.timer
sudo systemctl start learnpilot-backup.service
systemctl status learnpilot-backup.service
```

Inspect results and failures:

```bash
systemctl is-failed learnpilot-backup.service        # "failed" = last run did not complete
systemctl list-timers learnpilot-backup.timer        # next/last run
journalctl -u learnpilot-backup.service -n 50 --no-pager   # "LearnPilot backup failed: ..." reasons
sudo ls -1dt /var/backups/learnpilot/snapshot-* | head -3  # newest complete snapshots
df -h /var/backups/learnpilot
```

No snapshot is taken while a restore is in progress or was left unfinished
(section 17): the backup tool finds the restore lock next to the database and
exits with "a restore of this database is in progress", and the unit is not
even started. A snapshot of a half-replaced state would look complete. The
tool looks for the lock twice: before it reads anything, and again before it
publishes, so a restore that begins while a snapshot is being made publishes
nothing ("a restore of this database began while the snapshot was being
made"). The restore script stops the timer and starts it again when it has
finished. It sees a backup that systemd is running; one started by hand, from
a shell, it does not see: do not run a backup by hand during a restore.

The lock alone does not cover a backup that spans a whole restore, as one
started by hand can: there is no lock before the restore and none after it.
The tool therefore also notes, when it starts, which database file and which
uploads and vault directories are at its paths (by device and inode number,
not by name), reads the archive from those two directories themselves, and
checks again before it publishes. If any of the three has been replaced by
another, or is gone, it exits with "the state changed hands while the snapshot
was being made", names which, and publishes nothing: the copy could hold the
database of one state and the files of another. Run the backup again.

What this cannot see is a state that was overwritten in place, by hand: files
copied over the same database file or into the same directories keep their
identity. The restore script never does that, and nobody else should.

A failed run leaves no `snapshot-*` directory. Hidden `.snapshot-*` directories
are staging only; one can remain only after a hard kill (power loss) and is
safe to delete. There is no notification recipient configured, so check
`systemctl --failed` and the newest snapshot date regularly; an `OnFailure`
alert would not notify anyone.

A backup on the same VPS is **not disaster recovery**. After each scheduled
run, copy completed snapshots to a separate machine. Example initiated from
the administrator's Mac (prepare a private local destination first):

```bash
mkdir -p "$HOME/learnpilot-offsite" && chmod 700 "$HOME/learnpilot-offsite"
rsync -a --partial --rsync-path='sudo rsync' \
  <admin>@<server>:/var/backups/learnpilot/snapshot-<stamp>/ \
  "$HOME/learnpilot-offsite/snapshot-<stamp>/"
```

The admin account must have narrowly controlled sudo permission for the
read-only transfer, or an equivalent secure SSH/SFTP retrieval method. Backup
archives contain no env file, SSH key or other server credential. Keep 14 days
of completed local snapshots and at least 7 daily plus 4 weekly copies
off-server. Check off-server copies before manual local pruning. No automatic
deletion is installed; review only known snapshot directories under the
dedicated backup root:

```bash
sudo find /var/backups/learnpilot -mindepth 1 -maxdepth 1 \
  -type d -name 'snapshot-*' -mtime +14 -print
```

After verification, delete individual printed snapshot paths deliberately.
Also review any legacy manually created `pre-deploy-*.db` backups when
retiring old releases; those are not part of the snapshot naming policy.
Watch `df -h /var/backups/learnpilot` so local snapshots do not consume the
30 GB disk or the upload free-space reserve if they share a filesystem.

## 17. Restore

A snapshot is `snapshot-<stamp>/questions.db` plus `files.tar.gz`. The archive
holds exactly two trees, relative to `/var/lib/learnpilot`: `uploads/practical/`
(`DZI_UPLOAD_ROOT`) and `vault/` (`DZI_VAULT`, only its top directory and
`Generated/`). The restore assumes the default paths from
`learnpilot.env.example`.

Reference data is not part of a snapshot and a restore does not disturb it:
only the database files are moved out of `/var/lib/learnpilot/data`, never the
directory, so `/var/lib/learnpilot/data/reference` stays in place. On a rebuilt
server, provision it again as in section 3.

A restore is carried out by **one script**, `deploy/learnpilot-restore.sh`:

```bash
bash /opt/learnpilot/app/deploy/learnpilot-restore.sh \
    --snapshot /var/backups/learnpilot/snapshot-<stamp> \
    --operator "Your Full Name" \
    --evidence-note "<what made the restore necessary, and why the database being replaced is still trusted>"
```

Do not carry out its steps by hand, and do not paste them into a shell one at a
time. Typed that way, a step that fails does not stop the ones after it, and the
service ends up started on a database that nobody held. The script stops at the
first step that does not succeed, and the only command in it that starts the
service is its last one. Do not pipe it into another command either (`| tee`
would report `tee`'s exit status, not the script's); to keep a transcript, run
it under `script`.

**Do not put the snapshot in place yourself first**, not even "to save time".
What the script needs most is the database that was live when the restore
began, untouched, at the path the service uses: it is the only thing that knows
what was closed and changed after the snapshot. A copy of the snapshot in its
place says "nothing happened", and the script refuses it.

`--operator` is your full name as one quoted argument. It is recorded with
every lifecycle action in the audit log (section 26). `--evidence-note` says
why the database that is being replaced can still be trusted about deadlines
and closures, which it usually can: a restore is rarely needed because of them.
If it cannot be trusted or read at all, see "Without the replaced database"
below. If the server clock was wrong for a while before the restore, add
`--suspect-from` and `--suspect-to` (UTC), as for a clock incident (section 26).

### What the script does

Each line is a gate: the script reads its exit status and stops on anything but
success.

1. **Pre-flight, with the service still running and nothing modified.** The
   programs it needs are installed (`src.restore_preflight` does the rest of
   this step); the snapshot is complete (its database
   passes `PRAGMA quick_check`, its archive can be read to its end and holds
   only files under `uploads/` and `vault/`); the state directory is writable,
   has room for a second copy, and is one filesystem **all the way down**:
   nothing is mounted on or anywhere inside `uploads` or `vault` (a volume at
   `uploads/practical`, for one), and none of them, nor the database, is a
   symbolic link; the service is set, once and plainly, to the three paths
   that are about to be replaced (see "What the service's settings have to
   look like" below); no backup is running; the restore's own directory,
   `/var/lib/learnpilot-restore`, is root's with mode 0700 and nothing above it
   can be changed by anyone else (it is made if it is missing), and every
   record in it is a plain file of root's (section 34). Anything wrong here ends
   the script before it has stopped or changed anything.
2. **Lock, pause backups, stop.** It makes the directory
   `/var/lib/learnpilot/data/restore-in-progress`, stops the backup timer if it
   was active, looks at the backup service once more, stops the registration
   cleanup timer if it was active and requires `learnpilot-registration-cleanup.service`
   to be `inactive` or `failed` (a stopped timer does not stop a run that has
   begun; any other state ends the restore), and stops the service. All of that
   is before the live database is checkpointed and fingerprinted. Before it
   stops a timer, it writes a note that the timer is to be started again in
   the restore's own directory, which the service cannot write
   (`/var/lib/learnpilot-restore/pending-backup-timer`,
   `/var/lib/learnpilot-restore/pending-registration-cleanup-timer`), and flushes
   it to disk (`sync` of the note and of that directory). Who holds the restore
   is recorded there too (`restore-owner`), not in the lock: the lock directory
   sits in a directory the service can write, and it is only the signal that
   keeps the application and the units from starting. Whether it is there or
   not decides nothing for the script. Taking the restore (or taking over an
   abandoned one) with the record of its holder, writing a note and stopping its
   timer, and later starting a timer and its note's removal are each one step
   under one guard, `flock` on that directory: no other run of the script comes
   in between. Every command the script then has root or the service user carry
   out holds a second lock, `/var/lib/learnpilot-restore/lifetime`, for as long
   as it runs, and first checks that its run still holds the restore. An
   abandoned restore is taken over only if its recorded holder is not running
   and no command it started is left (section 34). The notes do not block
   anything; only the lock does.
   While that directory exists the application refuses to start and no backup
   is taken. That holds after a reboot too: it is checked by `web.wsgi` and by
   the backup tool themselves, and by a `ConditionPathExists=` line in both
   unit files. Whether a backup is running is read from the state systemd
   reports for `learnpilot-backup.service`, not from `is-active`: a backup
   that is running is `activating`, which `is-active` does not count as
   active. Only `inactive` and `failed` let the restore go on; any state the
   script does not know stops it. The second look is the one that counts: a
   backup that started after the first is seen then, and with the lock held
   none can start afterwards.
3. **Make a copy** in `/var/lib/learnpilot/restore-staging-<stamp>/`: the
   snapshot database, and the archive extracted next to it. Everything up to
   step 8 happens to this copy. The live database, uploads and vault are only
   read.
4. **Migrate the copy** to the schema of the deployed build
   (`src.manage_migrations --dry-run`, then `--apply`). A snapshot taken before
   a migration does not have it; one from before 018 has no lifecycle columns
   at all. Then **the registration gate** on the copy
   (`manage_accounts registrations-restore-gate`, decision D20 (a); section 34):
   every pending registration is voided and scrubbed, and registration is held.
   A hold the snapshot already carries is kept as it was. Nothing of the gate
   runs after step 8.
5. **Hold every assignment** on the copy: `restore-hold --pre-restore-db` with
   the live database, which also carries over the holds and the clock incident
   the live database has. `restore-status --expect-held all` then counts the
   holds itself: it is not enough that the hold step reported success.
6. **Reconcile** from the live database: `reconcile --pre-restore-db
   --snapshot-db`. This is the only step that releases a hold, and it first
   checks that the live database really is the later state of the snapshot
   (section 26, "Restore").
7. **Confirm the clock.** Only now, when it is used, does the script ask for
   the current UTC time. Read it from a device other than this server at that
   moment and type it in; it cannot be given on the command line, because a
   reading taken before the copying is stale by the time it is used.
   `clock-recover` refuses a reading that the server's clock is more than 5 s
   behind or 60 s ahead of, and the script then asks again, three times at
   most. The clock comes after reconciliation on purpose: recovery holds every
   test whose deadline, as reconciled, falls where the clocks disagree.
8. **Check and count:** `verify`, `registrations-restore-status --expect held`
   (no pending registration, the control row present, the hold set), and
   `restore-status`, which fails if any assignment is still held for the
   restore (see "Assignments that stay held").
   Then, because time has passed since the databases were looked at: the copy
   has to be byte for byte the one that was verified, its files the ones that
   were extracted, and the live database the one that was reconciled against.
   If anything wrote to any of them in the meantime, nothing is replaced.
9. **Replace the live state.** First the script records, in its own directory,
   that the replacement begins (`/var/lib/learnpilot-restore/swap-state`,
   flushed to disk). That record stays until a restore has finished, or until
   this run has moved everything back itself: a later run that finds it knows
   that the live state may be half replaced, whatever has become of the lock
   directory. The live uploads and vault are moved to
   `/var/lib/learnpilot/replaced-<stamp>/` and the copy's are moved into their
   place. The database comes last and is replaced by a single rename; the one
   that was live stays in `replaced-<stamp>/` under a second name. Until that
   rename, the database at the live path is the one that was live.
10. **Check what is now in place:** `verify`, `registrations-restore-status
    --expect held` and `restore-status` again.
11. **Unlock and start:** the lock is removed, the service started, `/healthz`
    requested (if that fails the service is stopped again); the run holds the
    restore until then. Once the service runs, the restore's records go (its
    holder, and the replacement recorded as begun), and the backup
    timer and the registration cleanup timer are started if each had been active.
    A timer that was not active is not started. Wherever the script starts the
    backup timer again after a failure, it starts the cleanup timer too. A
    timer's note is removed only after the timer was started successfully, and
    a timer is started and its note removed only while no restore is held or
    left unfinished: if another restore has taken over in the meantime, the
    timer and its note are left to that restore. If a timer cannot be started (or its note
    cannot be removed), the other one is still started, the script tries once
    more, and it ends **not 0**, with the note kept (`NOT ACTIVE AGAIN YET`).
    The next run of the script starts a noted timer first thing, in its
    pre-flight, when no restore lock is there (with a lock, when that run ends),
    and does not end with 0 while it cannot.

**Registration stays closed after every restore** until an operator runs
`registrations-release-hold --acknowledge-lost-history` (section 34). The script
never releases the hold, on any path.

### What the service's settings have to look like

The script replaces three things: `/var/lib/learnpilot/data/questions.db`,
`/var/lib/learnpilot/uploads/practical` and `/var/lib/learnpilot/vault`. It
goes ahead only if the service uses exactly these, and it can tell only if the
settings leave no doubt:

- The service reads **one** environment file, and it is mandatory
  (`EnvironmentFile=/etc/learnpilot/learnpilot.env`, without a leading `-`).
  The script asks systemd which file that is; it is not given a name.
- In that file `DZI_DB`, `DZI_UPLOAD_ROOT` and `DZI_VAULT` are each set
  **exactly once**, as a plain `NAME=/path` line: no quotes, no `export`, no
  spaces around `=`. That is how `learnpilot.env.example` writes them.
- None of the three is set in the unit itself as well (`Environment=`, in the
  unit file or in a drop-in).

Anything else is refused before the service is stopped. The reason is that
systemd uses the *last* definition of a name: with a second `DZI_DB` further
down the file, the service runs on a database the restore never looked at. A
line that is commented out does not count. The script names the setting and
how often it is set; it never prints a value from that file.

`uploads` and `vault` are each replaced as a whole, by renaming the directory.
A filesystem mounted inside one of them would be set aside with it, and the
restored files written to the disk of the state directory instead, without any
error. A symbolic link would be replaced by a directory and what it points to
left where it is. Both are refused in pre-flight; `data/reference` is not
moved and may be a filesystem of its own. A state directory laid out like that
cannot be restored by this script as it is.

The state directory itself may not be a symbolic link either
(`/var/lib/learnpilot` pointing at a directory on a volume): it is refused as
it stands, before anything behind the link is looked at. The kernel lists what
is mounted under real paths, and under the link's name a filesystem mounted
inside the directory would not be found. Give the real directory instead, in
the service's settings and in `LEARNPILOT_STATE`. A link *above* the state
directory (`/var/lib` on another disk) is fine: everything is compared under
the directory's real path.

### How it ends

The last lines the script prints say which of these happened. Only exit status
0 means that the service is running.

| Last message | Exit | Service | Lock | Live state |
|---|---|---|---|---|
| `RESTORE COMPLETE: no assignment is held for the restore.` | 0 | running | gone | Restored. |
| `RESTORED, NOT RECOVERED: N assignment(s) stay held` | 0 | running | gone | Restored. `N` assignments are closed to everyone, as acknowledged with `--accept-held N`. |
| `Nothing was touched, and the service was left as it was.` | not 0 | as before | none | Untouched: an argument, the snapshot or another pre-flight check. |
| `Nothing was replaced` ... `THE SERVICE WAS NOT STARTED.` | not 0 | stopped | gone | Exactly as before the script ran. Its copy was removed. |
| `INTERRUPTED WHILE REPLACING the live uploads and vault.` | not 0 | stopped | **stays** | The database is still the one that was live; uploads or vault are partly replaced. |
| `INTERRUPTED WHILE REPLACING the live state. The copy was NOT put in place` | not 0 | stopped | **stays** | With `--replaced-database-unreadable` only: the old database was being set aside and could not be put back whole. It is at its path or in `replaced-<stamp>/`. |
| `The database HAD BEEN REPLACED when the script was stopped` ... `The lock stays.` | not 0 | stopped | **stays** | Restored, and stopped before the final check. The database that was live is in `replaced-<stamp>/`, and only there. |
| `WHETHER THE DATABASE WAS REPLACED COULD NOT BE ESTABLISHED` | not 0 | stopped | **stays** | Not known. Nothing was moved back. |
| `The restored state is in place` ... `The lock stays.` | not 0 | stopped | **stays** | Restored; a final check failed. |
| `The restore itself is complete and the lock is released; the service is not running.` | not 0 | stopped | gone | Restored and complete. The start or its health check failed, or the script was stopped during it. |
| `The service is running on the restored state: the script was stopped after it had started it.` | not 0 | running | gone | Restored and complete. The script started the noted timers again and says, as on success, that registration is held and how many assignments stay held (`RESTORED, NOT RECOVERED`); if a timer could not be started, `NOT ACTIVE AGAIN YET` follows. |
| `NOT ACTIVE AGAIN YET: a timer that was active before the restore is still stopped.` | not 0 | as said above it | as said above it | A timer could not be started again, or its note could not be removed, or another restore has taken the lock since; the note in `/var/lib/learnpilot-restore` stays and the exit status is not 0. The warning above it says which. Find out why (`systemctl status <timer>`) and correct it; the next run of the script starts the noted timer before anything else (section 34). |

`RESTORE COMPLETE` says that no assignment is held **because of the restore**,
and nothing more. Tests that were closed are closed, deadlines are what they
were, and a test can still be held for a clock review (`hold-list`): it is not
a promise that every test accepts answers.

### If it stops

The script never starts the service after a failure, and there is no option
that makes it do so. **There is also no way to finish a run that stopped
half-way**: nothing it left on disk proves that its checks were made, or that
the live state is still the one they were made against. Every case below ends
the same way: correct the cause and run the script again, from the beginning.
It makes a new copy from the snapshot and passes every gate again, against the
live state as it is then.

- **Stopped from outside** (Ctrl-C, a closed terminal, `kill`): the same as
  any other failure, with `STOPPED: interrupted (SIG...)`. What the script
  then takes back is decided by what is on the disk, not by how far it
  believes it got: a step can be cut off after it has renamed something and
  before the script has noted it. The cases are the ones below.
- **Nothing was replaced.** Read the line that begins with `STOPPED:` and the
  output of the command above it. If the replacement had begun, the script has
  already moved everything back. The lock is released and the backup timer runs
  again; the service is stopped. (If you decide not to restore after all, the
  state it would serve is the one from before the attempt.)
- **The database had been replaced when the script was stopped.** The copy
  that passed every gate is at the live path, with the snapshot's files.
  Nothing is taken back, because the database that was live now exists only
  in `replaced-<stamp>/`; its write-ahead log, if it had one, is there with
  it and never beside the new database. The lock stays. Run the script again.
- **Whether the database was replaced could not be established.** The copy
  the script verified is, byte for byte, neither at the live path nor where
  it was staged: something else wrote to one of them. Nothing was moved and
  the lock stays. Do not remove the lock. Find out what wrote there, then run
  the script again; if you cannot, take a copy of the whole state directory
  with the service stopped before anything else is tried.
- **Interrupted while replacing**, or the machine went down in the middle. The
  lock stays, so nothing starts and nothing is backed up, also after a reboot.
  Do not move anything by hand and do not remove the lock. Run the script
  again. What the interrupted run had set aside stays in its
  `replaced-<stamp>/`, and its unused copy stays in `restore-staging-<stamp>/`:
  the script lists such directories every time it runs, never uses them and
  never deletes them. Delete them yourself when the restore is done and you
  are sure nothing in them is needed.
- **The restored state is in place, the lock stays.** Run the script again. It
  treats the state now in place as the one being replaced, which is correct:
  that state passed every gate and was never served.
- **The restore itself is complete, the service is not running.** The start
  or its health check failed, and the script stopped the service again. Look
  at `journalctl -u learnpilot -n 50`, correct what kept the service from
  starting, and start it as on any other day (section 11).

If the script says `another restore is running`, one is: the process recorded
as the holder of the restore (`/var/lib/learnpilot-restore/restore-owner`) is a
running restore, whether the lock directory is there or not. If no restore is
running and its records are there, an earlier run did not finish: running the
script again takes it over and says so. Two runs started together cannot both
take it over.

If the script says `a command an earlier restore started is still running`, the
earlier run is gone but something it started is not: a move, a copy, a
migration of the copy, a checkpoint. It holds
`/var/lib/learnpilot-restore/lifetime`, and while it does the script takes
nothing over: it could still change the live state under a new restore. Nothing
was touched. See what holds it with
`sudo fuser -v /var/lib/learnpilot-restore/lifetime`. If it is making progress,
let it end. If it is stuck, end it (`sudo kill PID`,
then `sudo kill -9 PID`) and run `fuser` again until nothing is listed. A
process that even `kill -9` does not end (state `D` in `ps`, waiting for a disk)
ends only with a reboot. Then run the script again: it takes the restore over
and starts from the beginning. Do not edit or delete the files in
`/var/lib/learnpilot-restore` to get past this message.

### Assignments that stay held

`reconcile` restores, for every assignment, what the replaced database knew and
the snapshot did not: a closure made after the snapshot, an extended or
shortened deadline. Where it cannot do that with certainty (the two databases
contradict each other about a closure, a hold cannot be read), it leaves the
assignment **held**. A held assignment is served to nobody: students, the
teacher and the admin get "временно недостъпна" (503) for it.

The script does not treat that as a completed recovery. It stops before
replacing anything, lists the held assignments with exit status 3, and prints
`RECOVERY INCOMPLETE`. If restoring is still the right decision, run it again
and acknowledge exactly that number:

```bash
bash /opt/learnpilot/app/deploy/learnpilot-restore.sh \
    --snapshot /var/backups/learnpilot/snapshot-<stamp> \
    --operator "Your Full Name" \
    --evidence-note "<as before>" \
    --accept-held 2
```

`--accept-held` releases nothing. It only states that you have seen the number;
any other number is refused. The service then runs with those assignments
closed, and the script ends with `RESTORED, NOT RECOVERED`. **Nothing reopens
them afterwards**: see section 26, "Restore", before telling a teacher anything
else.

### Without the replaced database

When the database being replaced cannot be read, or its lifecycle columns
cannot be trusted (it is the reason for the restore), or the script has refused
it as evidence, there is nothing to reconcile from:

```bash
bash /opt/learnpilot/app/deploy/learnpilot-restore.sh \
    --snapshot /var/backups/learnpilot/snapshot-<stamp> \
    --operator "Your Full Name" \
    --replaced-database-unreadable --accept-held <number of assignments>
```

**Every assignment in the snapshot then stays held**, permanently as far as
this release is concerned. That is deliberate. The snapshot does not know which
assignments were closed or changed after it was taken, and serving it as it is
would reopen closed tests. Run the script once without `--accept-held` to see
the number.

Two refusals are worth knowing before reaching for that option:

- *"it shows no lifecycle activity after the snapshot"*: the lifecycle clock of
  the live database stands exactly where the snapshot's stood. Only that clock
  is asked: an added account, a sign-in or an edited title does not count. A
  copy of the snapshot looks like this, and so does a database that really was
  live but in which no test was started, submitted, created, edited or closed
  since the snapshot. The two cannot be told apart, so it is refused and
  holding everything is the result. No option makes the script accept such a
  database (section 26, "Restore", says why, and what the limit of it is).
- *"it is not at the schema of this build"*: the live database has a migration
  pending (this release was installed and never migrated). Apply the pending
  migrations to it first (section 9), then run the script.

Everything set aside stays in `replaced-<stamp>/` until you delete it
deliberately. A snapshot's files can be slightly newer than its database (see
section 16); uploads without a database row are harmless leftovers.

## 18. Logs

```bash
systemctl status learnpilot
journalctl -u learnpilot -f
journalctl -u learnpilot --since "1 hour ago" -p warning
journalctl -u learnpilot-backup -n 50
sudo tail -f /var/log/nginx/access.log /var/log/nginx/error.log
```

Gunicorn access/error logs, Flask errors and app warnings go to journald; the
app writes no log files into the checkout. Nginx logs rotate with logrotate.

**No query string is logged by Gunicorn**, for any route. The access-log line
is the forwarded client address, then method, path and protocol
(`"%(m)s %(U)s %(H)s"`), status, size and duration: a `?next=...`, a
`?download=1` and the code and state of a Google callback are not in it. For a
request that Gunicorn itself failed (its own 400 or 500) the line has no path
at all and reads `"GET None HTTP/1.1"`; the path of such a request is in the
error-log line beside it.

**Gunicorn's error log passes a filter** (`RedactingLogger` in
`deploy/gunicorn.conf.py`): a line that begins with `Invalid request from ip=`
is cut at its first quotation mark, and every line is cut at its first `?`.
What was cut reads `[redacted]`. Gunicorn quotes in those lines what its parser
refused (a request line, a header line), and a callback address or a cookie
would otherwise be there. The reason for the refusal stays readable.

- The filter rewrites the message of a line. **It does not rewrite a
  traceback**, which ends with an exception's own text. The application's
  Google sign-in code never lets an exception's text reach a log: it logs class
  names. Whether bytes of a request body could reach a traceback is the subject
  of section 29, "What the log statements assume".
- Do not raise `loglevel` to `debug`. At that level Gunicorn logs the unread
  buffer of a client that disconnects in the middle of a request, a cookie
  included, with or without the filter.

**Google sign-in lines** are on the logger `learnpilot.oidc`, with a fixed
vocabulary: an outcome, an intent, a local user id, an allow-listed provider
error code or an exception's class name. For example
`learnpilot.oidc INFO callback outcome=created intent=signin user=42 reason=-`
and `learnpilot.oidc WARNING retire_not_recorded objects=pending
reason=OperationalError user=17`. No such line holds a code, a state, a token,
a subject or an email.

Cap the journal, for example:

```bash
sudo mkdir -p /etc/systemd/journald.conf.d
printf '[Journal]\nSystemMaxUse=200M\n' | sudo tee /etc/systemd/journald.conf.d/learnpilot.conf
sudo systemctl restart systemd-journald
```

## 19. Health check

Nginx only answers the real hostname, and Flask trusts exactly
`DZI_PUBLIC_HOST`, so a direct Gunicorn request needs the matching `Host`:

```bash
curl -fsS -H 'Host: learnpilot.bg' http://127.0.0.1:8000/healthz   # Gunicorn directly
curl -fsS https://learnpilot.bg/healthz                            # through Nginx
```

`GET /healthz` returns `200 ok` after opening the database read-only, reading
its header and reading the single lifecycle clock row (two cheap queries), or
`503 unavailable` without details. It renders no templates, sets no cookie, runs
no content queries and is not cached. It never creates a missing database.

`503 degraded` means the database answers but the lifecycle clock is `suspect`
(or its row is unusable): pages are served, and every lifecycle write (starting
or submitting a test, uploading, creating, editing, duplicating or closing one)
is refused with 503 until an operator confirms the clock. See section 26,
"Clock incident".

### Registration cleanup (once the timer is installed, section 34)

```bash
systemctl show -p ActiveState,Result,ExecMainStatus learnpilot-registration-cleanup.service
systemctl list-timers learnpilot-registration-cleanup.timer
sudo -u learnpilot /opt/learnpilot/venv/bin/python -m src.manage_accounts --db "$DZI_DB" registrations-cleanup --check
```

The service should be `inactive` (or briefly `activating`) with `Result=success`.
`failed` means the last run did not scrub: the database was busy, the clock is
behind the registration high-water mark, or a restore lock exists. The
`--check` command exits non-zero when a pending registration is older than an
hour, which is the logical-retention target of section 34 missed.

## 20. Deploy / update procedure

Production data lives outside the checkout (`/var/lib/learnpilot/...`) and is
never replaced by repository files. Any `data/questions.db` in the
checkout is local runtime state and is not the live database. Do not change Gunicorn workers or threads
(section "2 GB VPS sizing"). Do not apply migrations blindly.

**1. Verify the checkout is clean and on `main`.**

```bash
cd /opt/learnpilot/app
git branch --show-current          # expect: main
git status --short                 # expect: no output
```

Stop and investigate if either differs; never `git reset --hard` over
unexplained local changes.

**2. Record the current release commit** (needed for rollback):

```bash
PREV=$(git rev-parse HEAD); echo "$PREV"
```

**3. Create a complete pre-deploy snapshot** (SQLite database, practical
uploads and `vault/Generated`) with the existing systemd backup unit, which
reads `/etc/learnpilot/backup.env` (section 16):

```bash
sudo systemctl start learnpilot-backup.service    # blocks until finished
systemctl is-failed learnpilot-backup.service     # must NOT print "failed"
sudo ls -1dt /var/backups/learnpilot/snapshot-* | head -1   # note the new snapshot
```

The newest `snapshot-<stamp>/` must be from just now and contain
`questions.db` and `files.tar.gz`. Equivalent manual command:

```bash
sudo -u learnpilot /opt/learnpilot/venv/bin/python -m src.backup_application \
  --db /var/lib/learnpilot/data/questions.db \
  --uploads /var/lib/learnpilot/uploads/practical \
  --vault /var/lib/learnpilot/vault --output-dir /var/backups/learnpilot
```

Do not continue if the snapshot failed. A database-only `src.backup_sqlite`
copy is not a sufficient pre-deploy backup.

**4. Fetch:**

```bash
git fetch origin
```

**5. Update the checkout to the intended `main` release** without discarding
anything (fast-forward only; the command fails rather than merging):

```bash
git log --oneline HEAD..origin/main        # review what will be deployed
sudo git merge --ff-only origin/main       # or: sudo git checkout <reviewed-release-commit>
git rev-parse HEAD
```

**6. Install requirements into the production venv:**

```bash
sudo /opt/learnpilot/venv/bin/pip install -r requirements.txt
```

**7. Migration dry run** against the production database:

```bash
sudo -u learnpilot /opt/learnpilot/venv/bin/python -m src.manage_migrations \
  --db /var/lib/learnpilot/data/questions.db --dry-run
```

**8. If migrations are pending**, first confirm explicitly that the step 3
snapshot succeeded (exit status, newest `snapshot-*` directory with both files).
Only then apply, and only the migrations the dry run listed:

```bash
sudo -u learnpilot /opt/learnpilot/venv/bin/python -m src.manage_migrations \
  --db /var/lib/learnpilot/data/questions.db --apply
```

If nothing is pending, skip this step. The runner's own automatic backup covers
only the repository database, not production. A pending migration that the dry
run marks `(table-rebuild)` is high-risk: read "Table-rebuild migrations" in
section 9 and apply it only with the service stopped.

**9. Check Gunicorn configuration** (section 10):

```bash
sudo systemd-run --quiet --pipe --wait --uid=learnpilot --gid=learnpilot \
  -p EnvironmentFile=/etc/learnpilot/learnpilot.env -p WorkingDirectory=/opt/learnpilot/app \
  /opt/learnpilot/venv/bin/gunicorn --config deploy/gunicorn.conf.py --check-config
```

**10. Restart the service** (`preload_app` means a reload would keep old code):

```bash
sudo systemctl restart learnpilot
systemctl status learnpilot --no-pager
```

**11. Verify `/healthz`** with the correct `Host` header:

```bash
curl -fsS -H 'Host: learnpilot.bg' http://127.0.0.1:8000/healthz
curl -fsS https://learnpilot.bg/healthz
```

**12. Smoke-test** over HTTPS: the homepage, a subject page, a teacher account
sign-in at `/login` and test creation; check `journalctl -u learnpilot --since "10 minutes ago" -p warning`.
With one worker, a restart causes a few seconds of unavailability. For the
lifecycle (section 26): a closed test refuses a start and an edit, an open one
accepts both.

**13. Rollback** if a step fails: see section 21, using `$PREV` from step 2 and
the step 3 snapshot.

## 21. Rollback considerations

- Code: `cd /opt/learnpilot/app && sudo git checkout <previous-commit>` (the
  commit recorded in step 2 of section 20), reinstall requirements into
  `/opt/learnpilot/venv`, restart, and verify `/healthz` with
  `-H 'Host: learnpilot.bg'`. Return to `main` afterwards (`git checkout main`)
  once the fix is ready.
- Migrations are forward-only. Startup blocks *pending* migrations and, from the
  lifecycle release on, also a database that has applied migrations the build
  does not ship: an older build refuses a newer schema instead of running on
  it. If a release's migration is not backward compatible, restore the
  pre-deploy snapshot (section 17), which discards database writes, uploads and
  vault changes made after it; decide that explicitly. A restore does not by
  itself make an older build usable: which builds may serve the restored
  database is restricted per migration, so read the "Rollback" part of that
  migration's own section first (26 for migration 018, 27 for migration 019,
  28 for migration 020).
- **Never roll back to a release from before the assignment lifecycle (migration
  018).** Such code starts on the migrated schema without errors and ignores
  every deadline and closure: students could start and submit closed tests and
  teachers could edit them. That is a reopening, which the lifecycle forbids.
  When a lifecycle-era release is bad: stop the service (or answer a
  maintenance page), prefer a forward fix, and if a rollback is unavoidable use
  a build that still enforces the lifecycle and ships every applied migration.
  Remove pre-lifecycle release artefacts from the server's rollback options when
  018 is deployed. Section 26, "Rollback".
- If no migration was applied, a code rollback needs no data restore.
- Rolling back to code from before the account-auth cutover (the release of
  Accounts PR9) brings the removed shared-password logins back. Read section
  23, "Rollback", before doing it.
- Browsers may keep old CSS/JS for up to one hour (static cache lifetime).

## 22. Account management CLI

`src.manage_accounts` is operator tooling for the `users` table (migration 012).
Accounts are the only way to sign in to production (section 23), and this CLI is
the only way to create them and the only recovery path for a locked-out admin.

The target database is never defaulted to `data/questions.db`. Pass `--db <path>`
or set `DZI_DB` (`--db` wins). The file must already exist and already have the
`users` table; the command never creates, migrates or rebuilds a database.

```bash
python3 -m src.manage_accounts --db "$DZI_DB" create --email teacher@example.bg --name "Име Фамилия" --role teacher
python3 -m src.manage_accounts --db "$DZI_DB" set-password --email teacher@example.bg
python3 -m src.manage_accounts --db "$DZI_DB" set-role --email teacher@example.bg --role admin
python3 -m src.manage_accounts --db "$DZI_DB" disable --email teacher@example.bg
python3 -m src.manage_accounts --db "$DZI_DB" enable --email teacher@example.bg
python3 -m src.manage_accounts --db "$DZI_DB" sign-out --email teacher@example.bg
python3 -m src.manage_accounts --db "$DZI_DB" clear-throttle --email teacher@example.bg
python3 -m src.manage_accounts --db "$DZI_DB" list

# An account created through Google sign-in has no email: select it by its id
# (the first column of `list`). --email and --id exclude each other.
python3 -m src.manage_accounts --db "$DZI_DB" disable --id 42
python3 -m src.manage_accounts --db "$DZI_DB" enable --id 42
python3 -m src.manage_accounts --db "$DZI_DB" sign-out --id 42
python3 -m src.manage_accounts --db "$DZI_DB" set-role --id 42 --role teacher

# Admin TOTP (migration 016; enrol and confirm need DZI_TOTP_KEY in the environment)
python3 -m src.manage_accounts --db "$DZI_DB" totp-enroll --email admin@example.bg
python3 -m src.manage_accounts --db "$DZI_DB" totp-confirm --email admin@example.bg
python3 -m src.manage_accounts --db "$DZI_DB" totp-recovery-codes --email admin@example.bg
python3 -m src.manage_accounts --db "$DZI_DB" totp-reset --email admin@example.bg

# Read-only check that an admin can sign in (needs DZI_TOTP_KEY; section 23).
# It reads a src.backup_sqlite snapshot, never the live WAL database.
python3 -m src.backup_sqlite --db "$DZI_DB" --output /var/backups/learnpilot/cutover-check.db
python3 -m src.manage_accounts --db /var/backups/learnpilot/cutover-check.db cutover-check
```

- Accounts are addressed by (normalized) email. Roles: `student`, `teacher`, `admin`.
  This CLI is the only way to create or promote an admin.
- `disable`, `enable`, `sign-out` and `set-role` take exactly one of `--email`
  and `--id N` (a positive number; an unknown one is `error: no account with id
  N`). An account created through Google sign-in has no email and is reached by
  its id only. The other commands keep `--email`: they act on a password or on
  an admin's second factor, and such an account has an email.
- Every message names the account by its id, for example `disabled account 42
  (no email, student); existing sessions invalidated` and `role of account 17
  (ivan@example.bg) is now teacher; existing sessions invalidated`.
- `set-role --role admin` is refused for an account without a password, and
  changes nothing: an admin signs in with a password and a second factor only,
  so the account would have no way in. Promoting an account created through
  Google to `teacher` is allowed; the person then signs in with Google as a
  teacher.
- Passwords are never accepted as arguments or from the environment. By default
  the command prompts twice without echo. For controlled automation use
  `--password-stdin`: stdin must be exactly one line (an optional trailing
  newline is removed); empty or multi-line input is rejected.
- Password length: 10 to 256 characters; admin accounts need at least 14.
  `set-role --role admin` cannot see the old password, so follow it with
  `set-password` if the current one is shorter than 14 characters.
- `set-password`, `set-role`, `disable`, `enable` and `sign-out` rotate the
  account's session key, invalidating its future sessions.
- `clear-throttle` lifts the login lockout of one email (ten failed sign-ins in
  fifteen minutes block it for fifteen minutes) and touches no other email. The
  email need not belong to an account. Throttle rows are keyed with the
  application's secret, so `DZI_SECRET_KEY` must be set in the command's
  environment to the value the running application uses; without it the command
  fails and changes nothing. It needs the `login_throttle` table (migration
  013), succeeds when the email has no lockout, and never prints the secret or
  the stored key.
- `list` prints id, email, name, role, active state, whether a password is set
  (`password`: `yes`/`no`), the Google sign-in of the account (`google`: the
  address Google reported when the identity was added, `yes` when it reported
  none, `-` without one), and the created and last login times. It never
  prints a password hash, a session key or a Google subject. With it, finding
  an account created through Google, disabling and enabling it, ending its
  sessions and changing its role need no SQL. Removing or moving a Google
  identity is not something this CLI does (section 29).
- Admin TOTP: an account with role `admin` cannot sign in until it has a
  confirmed TOTP credential. `totp-enroll` (admins only) stores a new secret
  encrypted with `DZI_TOTP_KEY` and prints the `otpauth://` provisioning URI once;
  add it to the authenticator app and do not keep a copy. The credential stays
  inactive until `totp-confirm` verifies a current code (prompted without echo,
  or `--code-stdin`); it then prints ten single-use recovery codes once, stored
  only as scrypt hashes. `totp-recovery-codes` replaces them all.
  `totp-enroll`, `totp-confirm` and `totp-recovery-codes` require the account to
  currently be an admin; a demoted account is rejected without any change.
  `totp-reset` works for any role, because it only removes credentials (the
  operator recovery path for a lost device, and cleanup after a demotion). Enrol, confirm and reset rotate the session key. Both
  `DZI_TOTP_KEY` values must match the running application's; the key is never
  printed. Migration 016 itself rotates the session keys of existing admin
  accounts, so no admin session issued before it survives the upgrade.
- `cutover-check` changes nothing: no row, no database file and no `-wal`,
  `-shm` or `-journal` file. It prints `cutover-check: PASS` (exit 0) only if at
  least one admin account is active, has an email, has a password hash that
  sign-in can verify, has a confirmed TOTP credential that the `DZI_TOTP_KEY` in
  the command's environment decrypts, and has an unused recovery code; the
  tables of migrations 012, 013 and 016 must exist. Otherwise it prints `cutover-check: FAIL`
  with the reasons (exit 1). One viable admin is enough; other admins may be
  unenrolled or disabled.
  - The password hash is checked structurally only (`method$salt$digest` as the
    account service stores it: `scrypt` with its three parameters, or the older
    `pbkdf2` form sign-in still accepts, a salt, and a hex digest of the right
    length). No password is tried, nothing is hashed and no hash is upgraded; a
    damaged hash is reported as "cannot verify" and repaired with `set-password`.
  - It verifies no code, so no TOTP step or recovery code is consumed, and it
    prints no secret, code, key or hash.
  - **It does not read the live database.** The live database is in WAL mode,
    and SQLite cannot read a WAL database, even read-only, without creating or
    rewriting its `-wal`/`-shm` files; ignoring those files instead would miss
    commits that are still only in the WAL. The command therefore reads the
    file header first and refuses, before SQLite opens anything, a WAL-mode
    database or one with a sidecar file next to it. The refusal (exit 1, on
    stderr) names the two commands above; it is a restriction of the preflight
    and says nothing about the health of the database. Give it a snapshot made
    by `src.backup_sqlite`, which reads one consistent view including the
    committed WAL content and writes a single self-contained file. Never give
    it a file made with `cp`: a copy of the live main file omits the WAL, and
    the command refuses it for the same reason.
  - The result describes the snapshot. Take a new snapshot after any account
    change and before relying on the result.
- Errors go to stderr with a nonzero exit status; each command commits only after
  it fully succeeds.

## 23. Account-only authentication (Accounts PR9, PR10)

Account authentication is the only authentication LearnPilot has. Accounts PR9
switched the shared tester/admin passwords off in production; PR10 removed
their implementation from the application.

- Sign-in is `/login` (and `POST /login/totp` for an admin); sign-out is
  `POST /logout`. The former `/tester`, `/tester/login`, `/tester/logout`,
  `/admin/login`, `/admin/logout` and `/profile/<profile>` do not exist and
  answer 404 like any unknown URL.
- Nothing reads the session keys the shared logins once wrote
  (`tester_authenticated`, `admin_authenticated`, `ui_profile`). A cookie that
  still carries them, including one signed before the deployment, authorizes
  nothing and looks signed out; the keys go away when that browser signs in or
  out.
- An account admin still needs the second factor: the password alone grants
  nothing until a TOTP or recovery code completes the sign-in.
- Teachers manage only their own assignments and practical submissions.
  Assignments without an owner (`owner_user_id IS NULL`) stay unowned and are
  visible to and manageable by an account admin only; nothing claims or
  backfills them.
- A denied anonymous request goes to `/login`; a signed-in account without the
  needed role gets 403.

There is no setting that enables shared sign-in, in production or anywhere else:
`DZI_LEGACY_SHARED_LOGIN`, `DZI_ADMIN_PASSWORD` and `DZI_TESTER_PASSWORD` are
not read, required or validated by any code. Delete those lines from
`/etc/learnpilot/learnpilot.env` if an older env file still has them; leaving
them has no effect. (The PR9 release refused to start with
`DZI_LEGACY_SHARED_LOGIN` enabled; from PR10 the variable is simply unknown.)

**Upgrading to the PR10 release.** It has no migration and needs no data
change. The Nginx site template lost the `lp_admin_login` and `lp_tester_login`
zones and the `location = /admin/login` and `location = /tester/login` blocks.
An installed site file that still has them keeps working (the application
answers 404 behind them); to remove them, re-apply the template as in section
12, keeping the Certbot-managed TLS lines, then `sudo nginx -t` and
`sudo systemctl reload nginx`. The `/login` and `/login/totp` limits
(`lp_login`) are unchanged and must stay.

**`DZI_SECRET_KEY` is not rotated for any of this.** Rotating it would sign out
every account and discard the browser markers of attempts started before
accounts (an account-owned attempt is reached again by signing in). It is not needed: old cookies stay valid as cookies, but
nothing in them other than an account identity is read.

### Admin sign-in preflight

Before deploying a release, and after any change to admin accounts, confirm
that an admin can still sign in: accounts are the only way in. Deploy as in
section 20 (complete snapshot first).

The preflight never touches the live database (section 22): it reads a fresh
`src.backup_sqlite` snapshot of it. Take the snapshot while the application
runs; it contains everything committed up to that moment, including what is
still in the WAL. Then run the check on the snapshot with the service's own
environment, so that it uses the application's `DZI_TOTP_KEY` (the same
`systemd-run` form as the Gunicorn check in section 10; `--db` overrides the
`DZI_DB` of the environment file):

```bash
cd /opt/learnpilot/app
CHECK_DB=/var/backups/learnpilot/cutover-check-$(date -u +%Y%m%dT%H%M%SZ).db

# 1. Consistent snapshot of the current committed state (WAL-safe; not `cp`).
sudo -u learnpilot /opt/learnpilot/venv/bin/python -m src.backup_sqlite \
  --db /var/lib/learnpilot/data/questions.db --output "$CHECK_DB"

# 2. Read-only check of the snapshot.
sudo systemd-run --quiet --pipe --wait --uid=learnpilot --gid=learnpilot \
  -p EnvironmentFile=/etc/learnpilot/learnpilot.env -p WorkingDirectory=/opt/learnpilot/app \
  /opt/learnpilot/venv/bin/python -m src.manage_accounts --db "$CHECK_DB" cutover-check

# 3. The snapshot holds password hashes and encrypted TOTP secrets: remove it.
sudo rm -- "$CHECK_DB"
```

Pointing the check at `/var/lib/learnpilot/data/questions.db` itself is refused
with these instructions. The application never runs this check, at startup or
otherwise; it is the operator's gate.

1. **Before deploying**, while the previous release still runs: make sure
   `DZI_TOTP_KEY` is set in `/etc/learnpilot/learnpilot.env` and unchanged since
   the admin was enrolled.
2. **Preflight** (read-only, repeatable):
   the three commands above. Continue only on `cutover-check: PASS`. On `FAIL`,
   fix what it names on the live database with the commands of section 22
   (`create --role admin`, `enable`, `set-password`, `totp-enroll` +
   `totp-confirm`, `totp-recovery-codes`), then take a **new** snapshot and run
   it again; the old snapshot does not see the repair. The
   preflight proves the stored state is usable; it cannot prove the operator
   still holds the password, the authenticator and the recovery codes.
3. **Prove a real admin sign-in on the running release**: sign in at `/login`
   with the admin account's password and a TOTP code, and open `/teacher`.
4. **Check the recovery path without spending the last code.** Confirm the
   printed recovery codes are where they are supposed to be stored. If you want
   to exercise one, sign in with it once (it is consumed) and then issue a fresh
   set with `totp-recovery-codes`, which voids the old ones; never test with the
   only remaining code.
5. **Deploy** the release (section 20).
6. **Verify the account flows:**
   - admin: password, then TOTP, then `/teacher`, `/teacher/assignments`
     (every assignment, unowned ones included) and
     `/teacher/practical-submissions`;
   - teacher: sign in at `/login`, create a test at `/teacher/new`, see only own
     rows in `/teacher/assignments` and `/teacher/practical-submissions`, and get
     404 for another teacher's assignment URL and 403 for `/teacher`;
   - sign out with the "Изход" button (`POST /logout`) and sign in again.
7. **Verify the removed shared logins answer 404:**

   ```bash
   curl -s -o /dev/null -w '%{http_code}\n' https://learnpilot.bg/tester/login   # 404
   curl -s -o /dev/null -w '%{http_code}\n' https://learnpilot.bg/admin/login    # 404
   curl -s -o /dev/null -w '%{http_code}\n' -X POST -H 'Origin: https://learnpilot.bg' \
     -d 'password=x' https://learnpilot.bg/admin/login                            # 404
   ```

8. **Verify a stale session authorizes nothing** (only relevant on the first
   deployment after the shared logins were in use): in a browser that was signed
   in through `/tester/login` or `/admin/login` back then (do not clear its
   cookies), open `/teacher` and `/teacher/new`: both must redirect to
   `/login`, and the header must show "Вход", not a profile.
9. Keep shell access for `src.manage_accounts`: it is the recovery path.

### Recovering a locked-out admin

There is no shared-password or other break-glass login. Everything goes through
the CLI on the server, and none of it needs the application to be reconfigured:

| Problem | Recovery |
| --- | --- |
| Forgotten password | `set-password --email <admin>` |
| Too many failed attempts (429) | `clear-throttle --email <admin>` (needs `DZI_SECRET_KEY`) |
| Lost authenticator, recovery codes at hand | sign in with a recovery code, then `totp-reset`, `totp-enroll`, `totp-confirm` |
| Lost authenticator and recovery codes | `totp-reset --email <admin>`, then `totp-enroll` and `totp-confirm` |
| Recovery codes used up or lost | `totp-recovery-codes --email <admin>` |
| `DZI_TOTP_KEY` lost or replaced (TOTP answers 503) | restore the key; or sign in with a recovery code; or `totp-reset` and enrol again under the new key |
| Account disabled or demoted | `enable --email <admin>`, `set-role --email <admin> --role admin` |
| No admin account at all | `create --email <admin> --name "<name>" --role admin`, then `totp-enroll`, `totp-confirm` |

Run `cutover-check` on a new snapshot afterwards to confirm an admin can sign in
again.

The application does not check any of this at startup, deliberately: a server
whose only admin has lost the second factor must still start, so that teachers
and students keep working and the operator can repair the account with the CLI.
Shared authentication cannot be enabled because it no longer exists; the
preflight, run by the operator, guarantees that an admin can get in.

### Rollback

A rollback to the PR9 release or later stays account-only. The PR9 release
still contains the shared-login code but can never enable it in production; if
you roll back to exactly that release, make sure the env file has no enabled
`DZI_LEGACY_SHARED_LOGIN` line, because that release refuses to start with one.

A rollback to code from before PR9 (section 21) brings shared authentication
back: that code requires `DZI_ADMIN_PASSWORD` and `DZI_TESTER_PASSWORD` (startup
fails without them), serves `/tester/login` and `/admin/login` again and honours
the old session keys of any cookie signed with the current `DZI_SECRET_KEY`.
Browsers that still hold such a cookie from that time would be signed in again.

If that matters for a rollback, set new shared passwords in the env file before
starting the old code and, to void the old cookies as well, rotate
`DZI_SECRET_KEY`. Rotation is a deliberate emergency measure with a real cost: it
signs out every account and makes the attempts started before accounts
unreachable for their students (account-owned attempts are reached again by
signing in). It is never part of a normal deployment.

### Session cookie: `SameSite=Lax`

The session cookie is `Secure; HttpOnly; SameSite=Lax`. Up to the release that
changed it, it was `SameSite=Strict`. A signed-in person who follows a link to
LearnPilot from another site (a quiz link in a web chat or a classroom page,
later the redirect back from a Google or Microsoft sign-in) now arrives signed
in. Requests that change something are not affected: the application refuses
every cross-origin POST, with or without the cookie.

That release has no migration, no new or changed environment variable and no
Nginx change. It ends no session and `DZI_SECRET_KEY` is not rotated. `SameSite`
is not configurable, and `DZI_SESSION_COOKIE_SECURE` stays unset in production
(section 6). Deploy it as in section 20; there is no data step.

**Sessions from before the deployment.** A browser keeps the attributes a
cookie was set with. A session that was signed in before the deployment stays
`Strict` in that browser until the application next writes the cookie (a
sign-in, a sign-out or a password change) or the browser session ends. Until
then that browser behaves as before the change: a page opened from another
site is shown as to a signed-out visitor. Nobody has to be signed out for the
change.

**Verify** in a browser after the deployment:

1. Sign in and look at the `session` cookie in the developer tools: `Secure`,
   `HttpOnly` and `SameSite` `Lax`.
2. Still signed in, follow a link to the site from a page on another site: the
   page opens signed in.
3. A cross-origin POST is still refused, signed in or not:

   ```bash
   curl -s -o /dev/null -w '%{http_code}\n' -X POST -H 'Origin: https://example.org' \
     https://learnpilot.bg/logout                                                   # 403
   ```

**Rollback.** Deploy the previous release (section 21); there is no data to
undo. A cookie already issued as `Lax` stays `Lax` in its browser until it is
next written, and the older code accepts it unchanged: the attribute is the
browser's rule for sending the cookie, and the server never reads it.

## 24. Public assignment links (migration 017)

Students open an assignment only at `/quiz/s/<public_token>`. The sequential
`/quiz/<assignment_id>` entry is retired (see "Numeric student links are
retired" below). The token is stored in
`quiz_assignments.public_token` (nullable, unique index
`idx_quiz_assignments_public_token`).

- **Format.** `secrets.token_urlsafe(32)`: 256 bits from the operating system's
  CSPRNG, 43 URL-safe characters (`A-Z a-z 0-9 - _`), no padding.
- **Generation.** Every assignment created by the application, a duplicate
  included, gets its own token at creation. A token is never changed
  afterwards: editing title or time limit keeps it. Uniqueness is enforced by
  the index; a collision is retried a bounded number of times.
- **What the token is.** It names an assignment and nothing else. The page is
  public; starting an attempt still needs a signed-in account, and attempts stay
  account-owned (`docs/AUTH_ROUTE_CONTRACT.md`, "Quiz Attempts"). A malformed,
  unknown or missing token is one generic 404.
- **Management URLs** (`/teacher/assignment/<id>...`) keep the numeric id.

### Deployment order

Migration `017_assignment_public_token.sql` is additive (`ALTER TABLE ... ADD
COLUMN` and a unique index; no table rebuild) and generates no tokens:
assignments that already exist keep `public_token` NULL. SQLite's
`randomblob()` is deliberately not used for them. They get tokens from an
explicit operator command.

The order matters: the rate limit for the new route is in place before the
application starts serving it.

1. Section 20, steps 1-8: snapshot, update the checkout, apply migration 017.
   Do **not** restart yet. The running (old) application ignores the new column.
2. Add the token-route limit to the installed Nginx site and validate it. The
   installed file is a locally edited copy (server name, Certbot's TLS block),
   so copy the `location ~ ^/quiz/s/[A-Za-z0-9_-]+$ { ... }` block from
   `deploy/nginx/learnpilot.conf` into `/etc/nginx/sites-available/learnpilot`
   next to the existing `/quiz/[0-9]+` block, then:

```bash
sudo nginx -t && sudo systemctl reload nginx
sudo nginx -T 2>/dev/null | grep -A2 'location ~ \^/quiz/s/'   # expect the lp_quiz_write limit
```

   Do not continue if `nginx -t` fails. Until the application is restarted the
   old code answers 404 on `/quiz/s/...`, so the new block changes nothing yet.
3. Section 20, steps 9-12: check the Gunicorn configuration, restart the
   application, verify `/healthz`, smoke-test. `/quiz/s/<token>` is now served,
   already rate limited.
4. Backfill the existing assignments, after the restart so that no assignment
   is created by the old code in between. Dry run first:

```bash
sudo -u learnpilot /opt/learnpilot/venv/bin/python -m src.manage_assignment_tokens \
  --db /var/lib/learnpilot/data/questions.db --dry-run
sudo -u learnpilot /opt/learnpilot/venv/bin/python -m src.manage_assignment_tokens \
  --db /var/lib/learnpilot/data/questions.db
```

The command needs the database named explicitly (`--db` or `DZI_DB`), never
creates or migrates it, and is never run by the application. `--dry-run` only
reads: it opens the database read-only, generates no token, starts no
transaction and takes no writer lock, so it is safe next to the running
service. Without `--dry-run` it fills only
NULL tokens, never replaces an existing one and touches no other column or
table. The run is a single transaction: if it is interrupted nothing is
changed, and running it again fills whatever is still NULL. It prints counts
and assignment ids, never a token. Exit status 0 with
`OK: all N assignment(s) have a unique valid token` means it is complete; 1
means an existing token is not a valid token (the ids are listed and nothing
was changed: inspect those rows by hand); 2 is an operator or database error.
Running it again later is harmless and is also the verification step.

Until the backfill has run, an older assignment has no student link: its
teacher page and the assignments list say that the link is missing instead of
offering one, and its generated note records that there is none. Nothing falls
back to the numeric link in these places, and the token route answers 404 for
such an assignment.

### Numeric student links are retired

`/quiz/<assignment_id>` no longer exists as a student entry. Every such URL,
GET or POST, for any session, answers the generic 404 of an unknown URL:

- no redirect to the token URL and no numeric-to-token lookup of any kind;
- no difference between an existing assignment, one without a token and a
  missing one;
- no sign-in redirect, no token in the response, no attempt started or resumed.

Consequences for operation:

- **Previously distributed numeric student links stop working** the moment
  this release is deployed (links pasted in chats or class pages, and the
  "Споделяем линк" line of vault notes written before migration 017, which are
  not rewritten). Teachers share the token link from the assignment page
  instead; students who already started keep their attempt and find it under
  "Моите опити" (`/account/attempts`).
- **Still numeric and unaffected:** the teacher-management URLs
  (`/teacher/assignment/<id>`, `/edit`, `/duplicate`, `/results`,
  `/results.csv`; owner or admin only) and the account-owned attempt URLs
  (`/quiz/attempt/<id>`, `/quiz/attempt/<id>/result`).
- The Nginx template keeps the `^/quiz/[0-9]+$` location with the quiz-write
  limit as defense in depth; the installed site needs no change for this
  release, and the `/quiz/s/` limit from the token release must stay.

**Prerequisites before deploying the retirement release.** Both must already
be true on the production database, otherwise older assignments are left with
no student link at all:

1. Migration `017_assignment_public_token.sql` is applied and the token release
   is deployed and verified (the "Deployment order" above, including the Nginx
   `/quiz/s/` limit).
2. The backfill reports complete. Run it again as the check; `--dry-run` only
   reads, and the result must show `without a token: 0` and exit status 0:

```bash
sudo -u learnpilot /opt/learnpilot/venv/bin/python -m src.manage_assignment_tokens \
  --db /var/lib/learnpilot/data/questions.db --dry-run
```

The retirement release itself has no migration. Tell teachers beforehand that
old numeric links will stop opening and that they should re-share the link
shown on each assignment page.

QR codes for the student link are not part of this release; they are planned
as a separate change on top of the token URL.

### Rollback

The migration is additive, so code from before this release runs on the
migrated schema (the column is simply ignored) and no data restore is needed.
Token links do **not** survive such a rollback, though: the older code has no
`/quiz/s/<public_token>` route, so every token link already shared with
students (copied links, generated notes) answers 404 until the new version is
deployed again. The tokens stay in the database and the same links work again
afterwards. While rolled back, students can only enter through the numeric
`/quiz/<assignment_id>` link, which the older code shows on the teacher pages.
Assignments created by the older code have no token; after rolling forward
again, run the backfill once more.

Rolling back only the retirement release (to the token release) needs no data
change: token links keep working and the numeric entry answers again.

## 25. QR sharing of assignment links

The teacher's assignment page (`/teacher/assignment/<id>`) shows the student
link, a copy button and a QR code of that link with a download button. No
migration and no new production setting are involved.

- **What is encoded.** Exactly the absolute student link
  `https://<DZI_PUBLIC_HOST>/quiz/s/<public_token>`, the same one shown on the
  page. Never the numeric `/quiz/<id>` (retired), an attempt or result URL, a
  management URL or anything from the session.
- **Canonical public URL.** In production the base is always
  `https://` + `DZI_PUBLIC_HOST`, set at startup by `web.wsgi`
  (`PUBLIC_BASE_URL` in the Flask configuration; `web/public_url.py`). The
  `Host` and `X-Forwarded-*` headers of the request never reach the QR code or
  the displayed link, so a request on a loopback address (a local probe) still
  yields the public HTTPS link. `DZI_PUBLIC_HOST` is already required and
  validated (section 6); `DZI_PUBLIC_BASE_URL` must stay unset and startup
  refuses it. Startup and link building share one hostname check
  (`normalize_public_host`): an ASCII DNS name of two or more labels, with an
  internationalized name only in its `xn--` form, and no IP address or other
  numeric-looking host. If the configured base is ever unusable the endpoint
  answers 503 and the page shows neither a link nor a QR code but a notice
  that the public address is not configured; it never falls back to the
  request.
- **Endpoint.** `GET /teacher/assignment/<id>/qr.svg` (inline) and the same
  with `?download=1` (attachment, `assignment_<id>_qr.svg`, named from the
  numeric id only). No other parameter is read. Policy `assignment_owner`: the
  owning teacher or an admin (after the second factor); another teacher and a
  missing assignment are the same 404, a student gets 403 and an anonymous
  session (a pending second factor included) is redirected to sign-in. An
  assignment with no valid token (not yet backfilled, section 24) is 404 and
  its page keeps the backfill notice instead of a link and QR code.
- **No storage.** The SVG is generated in memory on each request by `segno`
  (error correction M or better, 4-module quiet zone, black on white). Nothing
  is written to the database, the vault, uploads or a cache, and no external
  service, CDN or browser-side library is used. Responses carry
  `Cache-Control: no-store`, `X-Content-Type-Options: nosniff` and
  `Content-Security-Policy: default-src 'none'; sandbox`; the SVG is one group
  of two paths with no scripts, styles, links or remote references.
- **Deployment.** Install requirements (step 6 of section 20) before the
  restart, because the release adds `segno`. Nginx needs no change: the route
  is proxied by `location /` and is not rate limited separately.

Verify after deploying, signed in as a teacher who owns an assignment:

```bash
# Anonymous: redirected to sign-in, no image.
curl -sS -o /dev/null -w '%{http_code} %{redirect_url}\n' \
  https://learnpilot.bg/teacher/assignment/<id>/qr.svg
```

1. Open `/teacher/assignment/<id>`: the QR code is shown under the student
   link and "Изтегли QR кода (SVG)" downloads `assignment_<id>_qr.svg`.
2. Scan the code on the screen with a phone camera (and once from a printed
   copy of the downloaded file). The phone must offer exactly the link in the
   "Линк за ученици" field: `https://`, the public hostname and
   `/quiz/s/<43 characters>`.
3. Open it on the phone: the assignment's start page appears, and starting
   asks for sign-in as for any student link.
4. Signed in as a different teacher, the same `qr.svg` URL answers 404.

For a development run reached from another device, set
`DZI_PUBLIC_BASE_URL` to the address that device uses before starting the app
(for example the `https://<name>.trycloudflare.com` of a quick tunnel, or
`http://<LAN address>:5001`). Unset, a QR code is produced only for requests
made to `localhost`, `127.0.0.1` or `::1` and encodes that loopback address,
which a phone cannot open; requests on any other host get neither a student
link nor a QR code on the page, only the notice that the public address is
not configured.

Rollback: redeploying the previous release removes the QR code and the route;
student links and tokens are unaffected, and already printed codes keep working
because they contain only the token link.

## 26. Assignment lifecycle (migration 018)

Tests have an optional deadline, can be closed manually, and are never
reopened. Design and guarantees: `docs/ASSIGNMENT_LIFECYCLE_DESIGN.md`
(this release is its PR #326). Nothing here deletes anything: retention cleanup
and durable learning history are later, separate releases.

- **Deadline.** Optional. The form offers 23:59 local time, 14 calendar days
  ahead, in an IANA timezone stored per test (preselected `Europe/Sofia`, or the
  teacher's most recent choice). "Through 23:59" means the whole minute is
  usable: the stored cutoff is the next local midnight, in UTC. At most 180 days
  ahead, counted from the day the deadline is set or edited. Every create,
  duplicate and edit request must state "with deadline" or "no deadline";
  nothing is defaulted on the server.
- **Closure.** By the deadline, or by `POST /teacher/assignment/<id>/close`
  (owner or admin). Both are final: no new attempt, and title, time limit and
  deadline are frozen. A correction is a copy (a new test with a new link and
  its own deadline choice).
- **Grace.** A student who started before the closure can still submit for 15
  minutes after it. A submission at exactly closure + 15 minutes is refused.
- **Existing tests** keep NULL in every lifecycle column: open, no deadline,
  no zone (shown in `Europe/Sofia` until the teacher picks one on first edit).
  Nothing is backfilled.
- **No job.** Expiry is computed on every request from the stored deadline; no
  timer, cron job or service is installed by this release.

### Deployment order

The standard procedure of section 20, with these points:

1. Migration `018_assignment_lifecycle.sql` is additive (eight nullable columns
   on `quiz_assignments`, and the single-row `lifecycle_clock` table). It is not
   a table rebuild. Apply it with the service stopped: from this release on, the
   application refuses to start on a schema without it, and older code must not
   serve a schema with it.
2. Before starting, confirm the host's clock evidence and verify:

   ```bash
   cd /opt/learnpilot/app
   LIFECYCLE=(sudo -u learnpilot /opt/learnpilot/venv/bin/python -m src.manage_lifecycle
              --db /var/lib/learnpilot/data/questions.db)
   "${LIFECYCLE[@]}" clock-status     # expect: boot id available, boot-time clock available, clock synchronised: yes
   "${LIFECYCLE[@]}" verify           # expect: OK
   timedatectl show -p NTPSynchronized --value   # expect: yes
   python3 -c 'import zoneinfo; print(zoneinfo.ZoneInfo("Europe/Sofia"))'   # the tzdata package is installed
   ```

   `clock-status` must report all three pieces of host evidence. If "clock
   synchronised" is `no` or `unreadable`, fix time synchronisation
   (`systemd-timesyncd` or `chrony`) before starting: without it the first
   lifecycle write after every reboot is refused, and after ten minutes of
   uptime the clock is marked `suspect`.
3. Start, check `/healthz` (`ok`), then smoke-test: create a test with the
   offered deadline, close it, confirm that the student link shows "затворен"
   and that an edit is refused.
4. Tell teachers: existing tests have no deadline; new ones are offered 23:59
   local time 14 days ahead.

No new environment variable, secret, service or timer is needed. Install the
two unit files again (`learnpilot.service` as in section 11,
`learnpilot-backup.service` as in section 16, then `sudo systemctl
daemon-reload`): each has a new `ConditionPathExists=` line that keeps it from
starting during a restore.

### What production start-up refuses

In addition to the checks of section 9, `web.wsgi` refuses to start when

- a restore of the database is in progress or was left unfinished: the lock
  `restore-in-progress` is next to it (section 17). Only the restore script
  removes it;
- the database has an applied migration the build does not ship (an older build
  on a newer schema);
- the `lifecycle_clock` row is missing, duplicated or malformed. It is repaired
  only by `clock-recover`, never by re-running the migration or a seed;
- the IANA timezone database is not available.

A `suspect` clock does **not** stop start-up: pages are served, lifecycle
writes are refused and `/healthz` answers `503 degraded`.

### Clock evidence

Deadlines and the grace window are decided by the server's UTC clock. Every
lifecycle write checks it against the shared `lifecycle_clock` row: it may not
run behind the highest time already used (more than 5 s), and within one boot it
may not differ from the boot anchor plus elapsed boot time by more than 60 s, in
either direction, cumulatively. After a reboot the operating system's
"synchronised" report (`adjtimex`, what `timedatectl` shows) is required before
the first lifecycle write. These checks detect faults; they do not prove that
the clock is right. The development server (`web.app` imported directly) does
not require the synchronisation report; `web.wsgi` always does.

### Clock incident

Symptoms: `/healthz` answers `503 degraded`; teachers and students get "временно
недостъпна" (503) when starting, submitting, uploading, creating, editing,
duplicating or closing; `journalctl -u learnpilot -p err` shows `lifecycle:
clock fault detected`. The state is sticky: a clock that looks right again
changes nothing. Only this procedure clears it, and nothing in it closes a test
or ends its submissions.

```bash
cd /opt/learnpilot/app
LIFECYCLE=(sudo -u learnpilot /opt/learnpilot/venv/bin/python -m src.manage_lifecycle
           --db /var/lib/learnpilot/data/questions.db)
AUDIT=(--operator "Your Full Name" --audit-log /var/lib/learnpilot/lifecycle-operator.log)

"${LIFECYCLE[@]}" clock-status                           # the fault and the old high-water mark
# 1. Correct the clock and verify it against a source outside this server.
# 2. See what would be held, and whether the clocks agree; nothing is written.
"${LIFECYCLE[@]}" clock-inspect --verified-utc "2026-10-08 12:05:00" \
    --suspect-from "2026-10-08 11:40:00" --suspect-to "2026-10-08 12:04:00"   # the interval is optional
# 3. Hold what is uncertain and confirm the clock, in one transaction.
"${LIFECYCLE[@]}" clock-recover --verified-utc "2026-10-08 12:05:00" \
    --suspect-from "2026-10-08 11:40:00" --suspect-to "2026-10-08 12:04:00" "${AUDIT[@]}"
"${LIFECYCLE[@]}" hold-list
```

`LIFECYCLE` and `AUDIT` are Bash arrays and are always expanded as
`"${LIFECYCLE[@]}"` and `"${AUDIT[@]}"`. Written as plain strings and expanded
unquoted, an operator name of two words would be split into two arguments. The
later examples in this section use the same two arrays.

`--verified-utc` is the current UTC time read from an independent device. It,
and not the server's clock, decides everything in the recovery: what is held,
the retention floors, and the new high-water mark, below which lifecycle time
can never fall again. The server's clock only has to agree with it: it may be
at most 5 s behind the verified time and at most 60 s ahead of it. Otherwise
`clock-recover` is refused, nothing is written and the state stays `suspect`:
correct the clock and run it again with a fresh reading. (`clock-inspect`
reports the same refusal in advance.)

`clock-recover` holds, with the reason `clock_review`, every test that has a
start, submission or closure time later than the verified time or inside the
declared suspect interval, and every test whose deadline or grace end falls
after the verified time and at or before either the old high-water mark or the
server's current reading, without having been recorded: those are the tests
whose state the two clocks disagree about. A held test is not served at all
(503) until its hold is resolved. Tests that are only "open with a deadline in
the future" are not held. A deadline that the verified time has already passed
is simply closed from then on; the next request records it.

Resolve a `clock_review` hold only after checking that test's times against the
verified clock:

```bash
"${LIFECYCLE[@]}" hold-resolve --assignment <id> --reason clock_review \
    --evidence "<what was checked>" "${AUDIT[@]}"
```

It is refused while any time of that test is still later than the clock. A
closure that was really recorded during the incident stays: closures are final,
and the teacher's remedy is a copy.

**A hold the tool cannot read.** `hold-list`, `restore-status` and `verify` flag
a stored hold that is not the known reasons, each once, in their fixed order: an
unknown word, a repeated reason (`clock_review,clock_review`), a stray comma
(`clock_review,`), a space, a value that is not text. Such a test stays held,
and no command resolves anything from it: `hold-resolve` refuses it and
`reconcile` skips it, because removing one readable reason could release the
test while discarding whatever the rest was meant to say. A restore does not
tidy such a value either. When the database being replaced carries one,
`restore-hold` copies it into the restored test as the exact text it is, with
nothing dropped, reordered or deduplicated, so the restored test is exactly as
unresolvable as the one it replaces. The tool has no repair command. Find out
how the value came to be stored, decide what the hold should be, and correct
the stored value deliberately, with the service stopped and the decision
written down next to the audit log.

### Restore

A restore puts back an older copy of the database. Whatever happened to a test
after that copy was taken is missing from it, in both directions: a closure is
gone, which would reopen the test, and so is a deadline extension, which would
close it early. The application cannot tell that its database was replaced. So
the rule is: **after a restore no test is served until its state has been
established from the database that was replaced.** `deploy/learnpilot-restore.sh`
(section 17) runs the commands below in this order and stops at the first that
does not succeed. From its first change to its last it keeps a lock next to the
database, and the application does not start while the lock is there.

- `restore-hold` adds the reason `restore_pending` to **every** test (kept
  alongside any reason it already has), marks the clock `suspect`, and, given
  the database being replaced, carries over the holds and the clock incident
  that database has. A hold is carried only onto the same test, never onto
  another one that happens to have the same number. Run it again safely at any
  time.
- `restore-status --expect-held all` counts the holds and fails unless every
  test carries one. It writes nothing.
- `reconcile --pre-restore-db --snapshot-db` takes the database being replaced
  as the latest state, in both directions: a lost deadline extension is
  restored as much as a lost closure. It never clears or changes a closure
  marker, never moves the deadline of a test that is already closed, never
  lowers a retention floor, and resolves only `restore_pending`. A test whose
  evidence contradicts the restored row is left untouched and held with
  `restore_unreconciled`. **It is the only command that releases a test after
  a restore.** It also hands on the replaced database's clock: how far it had
  run, and an incident that was open.
- `clock-recover`: as above, and after `reconcile`, never before it. Recovery
  holds every test whose deadline lies where the clocks disagree, and it can
  only do that for the deadlines it sees. A deadline that reconciliation
  brought in afterwards would come back open. `reconcile` therefore refuses to
  run on a clock that has already been confirmed.
- `restore-status --expect-held N` succeeds only when exactly `N` tests are
  still held for the restore (default 0). `N` above zero is an acknowledgement
  and releases nothing.

**What the replaced database has to be.** Before it releases anything,
`reconcile` checks the database it is given, as a whole, and refuses it
(exit 2, nothing written) unless

- it is at the schema of this build, as the database this build was serving is;
- it holds every test and every attempt of the restored copy **as the same
  row**: the same section, creation time, plan, owner and public link, the same
  student and start time. The same number is not enough. Another database, or
  another school's, is refused;
- its lifecycle went on **after** the snapshot: its lifecycle clock stands
  later than the snapshot's (for a snapshot from before migration 018: later
  than where a clock stands that has never been used). Every lifecycle write
  raises that clock: a test started or submitted, created, edited or closed.
  A clock that stands earlier is an older database; one that stands exactly
  where the snapshot's stood shows nothing that came later. Both are refused.

Nothing else is asked about what came later. A difference in any other table
is not lifecycle history: a copy of the snapshot with a new account in it, a
sign-in or an edited title passes every other check, says nothing about
deadlines and closures, and is refused like the plain copy it is.

*The limit of this.* A database that really was live, but in which no
lifecycle write was made after the snapshot, cannot be told from a copy of the
snapshot and is refused like one. That happens after a quiet night (a backup
at 03:00, a restore before the first lesson), with a release that was
installed and never used, and when the only lifecycle writes fell in the very
second the snapshot was taken. The restore can then be made only with
`--replaced-database-unreadable`, and every test stays held. It is refused
rather than assumed because the opposite mistake reopens closed tests.

These checks establish what the file is. They cannot establish where it came
from, and nothing a file contains could. That is settled by the procedure: the
restore script reads the database at the path the service is configured to
use, in place, with the service stopped by the script itself, and never takes
that path from the operator. Run `reconcile` by hand only if you can say the
same of the file you give it.

**What is not evidence.** The application writes a line to its log for every
create, edit and close (`lifecycle event=...`). Those lines are for diagnosis.
They do not establish a test's state, and no command reads them:

- A log cannot show that it is complete. A missing closure line looks exactly
  like a test that was never closed, and a test that the log does not mention
  is not thereby unchanged.
- Its lines are text. Nothing distinguishes a line the application wrote from
  one typed later, and a SHA-256 of a file says only that the file was not
  changed after the digest was taken.
- It does not give the order of two changes made in the same second, and lines
  can arrive out of order. An obsolete deadline would be taken for the current
  one.

The same holds for what a person remembers or states. `reconcile-partial` and
`reconcile-record` are names that exist only to say this: whatever they are
given, they exit with status 4, explain why, and read and write nothing.

**Tests that stay held.** A test that `reconcile` could not establish stays
held: it answers 503 to everyone, including its teacher. That is the design
working, not a fault to be worked around.

- Without a readable database to reconcile from, **every** test of the snapshot
  stays held (section 17, "Without the replaced database").
- This release has no way to release such a test. The record that could do it,
  a durable lifecycle event log written outside the database, is deferred (it
  is decision I5 of the design and is **not** implemented). Until it exists the
  hold stays.
- **Never clear a hold by hand to bring a test back.** An `UPDATE` that sets
  `lifecycle_hold` to NULL reopens whatever the restore reopened: a test that
  had been closed takes new attempts, and nothing records that it happened.
  There is no emergency override, and none is to be improvised.
- The teacher's remedy is a new test, made on the normal creation form. It gets
  a new link and its own deadline. (A held test cannot be copied: copying reads
  its lifecycle state.)
- Say so plainly to the people affected: the test and its results are
  unavailable (the data as of the snapshot is kept, but a held test shows
  nothing, to its teacher either), and a new test is needed.

Assignments, attempts and submissions made after the snapshot are not in the
restored database; that is inherent in a restore.

### Exit status of `manage_lifecycle`

Only 0 means that a command did everything it is for. A command that prints
its result and exits with another status has **not** established that a test
is safe to serve.

| Status | Meaning |
|---|---|
| 0 | Done. For a check (`verify`, `hold-list`, `restore-status`): nothing to report. |
| 1 | Refused by a lifecycle rule, or a check has findings. Nothing was written. |
| 2 | An operator, input or database problem. Nothing was written. |
| 3 | Done and committed, but recovery is incomplete: tests remain held (`reconcile`, `restore-status`). |
| 4 | A recovery operation this build does not provide. Nothing was read or written. |

### Verification before traffic is reopened

After any deploy, rollback or clock incident: `"${LIFECYCLE[@]}" verify` (exit
0). After a restore the script runs it, twice. It checks that every stored
lifecycle time parses, that no start, submission or closure time is later than
the clock (`--hold-exceptions` holds those tests), that holds are readable,
that the clock row is well-formed and `ok`, and that the timezone database
works. A deadline in the future is expected; only one further ahead than the
180-day maximum allows is reported, as a data anomaly. Also verify the clock by
hand against an independent source.

`verify` passes on a database with held tests: a hold is a safe state. It
cannot tell that a database was restored without `restore-hold`, which is why a
restore goes through the script and is never finished by `verify` followed by
a start. For the same reason an interrupted restore is never "completed": it
is run again.

### Operator audit log

Every `manage_lifecycle` command that writes needs `--operator` and
`--audit-log`. Give the operator's name as one quoted argument (the `AUDIT`
array above does, and so does the restore script). Each action is appended to
that file as one JSON line: who, when, the database it was run on, what was
held or resolved, and the evidence relied on. During a restore the database
named there is the copy under `restore-staging-<stamp>/`. Keep the file under
`/var/lib/learnpilot/` so it is outside the checkout; it is not part of the
snapshot archive, so copy it with your backups.

### Rollback

- Forward fix first. A rollback target must still enforce the lifecycle and
  ship migration 018; never a build from before it (section 21).
- The schema is not reverted: the columns, their values and `lifecycle_clock`
  stay. Closure markers are never cleared, by a migration or by hand.
- If code from before the lifecycle served traffic after activation anyway,
  that is an incident: stop the service, restore enforcing code, and before
  reopening traffic run

  ```bash
  "${LIFECYCLE[@]}" exposure-audit --from "<start UTC>" --to "<end UTC>" \
      --snapshot-db "/var/backups/learnpilot/snapshot-<stamp>/questions.db"   # the snapshot is optional
  ```

  It lists attempts started after a closure, submissions and uploads after the
  cutoff and, against the snapshot, edits to closed tests and overwritten
  submissions. Nothing is changed automatically; what to do with that work is
  decided by the teacher and the operator.

## 27. External identity storage (migration 019)

Migration `019_user_identities.sql` adds the `user_identities` table and its
index: where the Google and Microsoft sign-in identities of accounts will be
stored (`docs/ACCOUNTS_DESIGN.md`, sections 5 and 13).

**This release is storage only. Provider sign-in is not implemented.** There
is no new route, page, dependency, environment variable, secret, provider
registration or Nginx change, and no application code reads or writes the
table. Sign-in works exactly as before (section 23) and the table stays empty
until a later release adds OIDC sign-in.

### Deployment order

The standard procedure of section 20, with nothing added to it.

- The migration is additive: one new table and one index. It is not a table
  rebuild, alters no existing table and writes no row. Accounts, password
  hashes, session keys, second factors and ownership are untouched, and nobody
  is signed out.
- What the dry run of step 7 lists depends on the database it is run against,
  and this guide does not assume that database's migration level. On a
  database that already has every migration through
  `018_assignment_lifecycle.sql` it lists exactly `019_user_identities.sql`.
  If it lists earlier migrations too, the database is behind that level: each
  of them has conditions of its own, described where it is introduced (section
  9 for a table rebuild, section 26 for migration 018), and this section does
  not replace them.
- Apply in step 8, after the step 3 snapshot, only what the dry run listed.
  From this release on the application refuses to start on a schema without
  migration 019 (section 9).
- No account, operator or teacher has anything to do afterwards.

### Backup and restore

`user_identities` is part of the live database, so the snapshot of section 16
and the restore of section 17 cover it with no change to either. A snapshot
from before this release is restored as before: the restore script migrates
its copy to the schema of the deployed build, which adds the empty table. The
table is runtime state: it is never exported to `data/canonical`, and a
database built by `src.build_database` has it empty.

### Rollback

**There is no authentication behaviour to revert.** This release only adds
storage that nothing reads or writes: sign-in, sessions, roles and every page
are what they were before it, so a rollback past it would change nothing about
how anyone signs in. Forward fix first.

- **Once migration 019 is applied, every build that serves this database ships
  `019_user_identities.sql`, a recovery build included.** Migrations are
  forward-only, and a build that does not ship an applied migration refuses to
  start on the database (section 21). If application code has to be reverted,
  revert the code and keep the migration, together with the metadata that
  belongs to it: the `user_identities` entry in `src/canonical_tables.py`,
  and in `data/canonical/manifest.json` the migration list and level and the
  runtime table lists. A build that ships the migration without them fails its
  own canonical and database-build checks. The table stays, empty; it is not
  dropped, by a migration or by hand.
- **A snapshot restore is not a route to a build from before 019.** Section 21
  names the pre-deploy snapshot for a migration that is not backward
  compatible. Here that leads nowhere, whichever build runs the restore:
  - *A lifecycle build from before 019.* Reconciliation refuses the database
    being replaced as evidence: it has migration 019 applied, that build does
    not ship it, and so it "is not at the schema of this build" (section 26,
    "Restore"). The script stops there, nothing is replaced and the service
    stays stopped.
  - *This build, or a later one.* The script migrates its copy of the snapshot
    to the schema of the deployed build (section 17, step 4), so a snapshot
    from before 019 has migration 019 applied to it. The restored database is
    at 019 again, and a build from before 019 refuses to start on it as it
    did on the one that was replaced.
- **Do not reach for `--replaced-database-unreadable` to get past that
  refusal.** The option is for a database that cannot be read or trusted, not
  for one that an older build does not recognise, and it leaves every
  assignment held with nothing to release them afterwards (section 17,
  "Without the replaced database").
- **The restore procedure and its holds are unchanged.** A restore that is
  needed for its own reasons is still the one script of section 17, run from
  start to finish with a build that ships every applied migration. No step of
  it is skipped or done by hand to suit a build, and an assignment it leaves
  held stays held (section 26, "Restore").
- The lifecycle rule of section 26 is unchanged: never a build from before
  migration 018.

## 28. OIDC consumption storage (migration 020)

Migration `020_oidc_consumed_transactions.sql` adds the
`oidc_consumed_transactions` table: where a later release will record that an
OIDC sign-in transaction was used, so that the same transaction cannot be used
a second time (`docs/ACCOUNTS_DESIGN.md`, sections 5 and 13;
`docs/reviews/OIDC_CONSUMPTION_SCOPE.md`).

**This release is storage only. Provider sign-in is not implemented, and
neither is replay protection.** There is no new route, page, dependency,
environment variable, secret, service, timer, Nginx or Gunicorn change, and no
application code reads or writes the table. Sign-in works exactly as before
(section 23).

The table is empty **in this release**, because nothing writes to it. Do not
read that as a property of the table, and do not expect it to stay small by
itself: once a later release adds OIDC sign-in, it holds a row for every
sign-in whose expiry has not yet passed by more than a minute, and in addition
whatever earlier boots of the server left behind, which can add up (see "Rows,
and who removes them").

This release was verified with Python 3.12, which is what production runs
(`/opt/learnpilot/venv/bin/python --version`, section 5). The storage service
tells a repeated use apart as `AlreadyConsumed` through `sqlite_errorcode`,
which Python provides from 3.11 on. On an older Python a repeated use is still
refused, as a plain `sqlite3.IntegrityError`, and the service still neither
commits nor rolls back. That is a note on this release's verification and
behaviour; it does not set a minimum Python version for the project.

### Deployment order

The standard procedure of section 20, with nothing added to it.

- The migration is additive: one new table, without an index and without a
  row. It is not a table rebuild and alters no existing table. Accounts,
  password hashes, session keys, second factors, external identities and
  ownership are untouched, and nobody is signed out.
- What the dry run of step 7 lists depends on the database it is run against,
  and this guide does not assume that database's migration level. On a
  database that already has every migration through `019_user_identities.sql`
  it lists exactly `020_oidc_consumed_transactions.sql`. If it lists earlier
  migrations too, the database is behind that level: each of them has
  conditions of its own, described where it is introduced (section 9 for a
  table rebuild, section 26 for migration 018, section 27 for migration 019),
  and this section does not replace them.
- Apply in step 8, after the step 3 snapshot, only what the dry run listed.
  From this release on the application refuses to start on a schema without
  migration 020 (section 9).
- No account, operator or teacher has anything to do afterwards.

### Backup and restore

`oidc_consumed_transactions` is part of the live database, so the snapshot of
section 16 and the restore of section 17 cover it with no change to either;
`deploy/learnpilot-restore.sh` is not changed. A snapshot from before this
release is restored as before: the restore script migrates its copy to the
schema of the deployed build (section 17, step 4), which adds the empty table.
The table is operational state: it is never exported to `data/canonical`, and
a database built by `src.build_database` has it empty.

The table plays no part in reconciliation. Whether the database being replaced
is accepted as evidence is decided by its schema, its tests and attempts, and
its lifecycle clock (section 26, "Restore"); a difference in this table is not
lifecycle history, in either direction.

What a restore means for the records themselves matters only once a release
writes them, which this one does not:

- A snapshot holds the table as it was when the snapshot was taken. Records
  written afterwards are not in the restored database.
- **The storage does not make that loss harmless by itself.** Two things
  together do. The restore script stops the service in its step 2 and starts
  it in its step 11, which begins a new run of the service; and the sign-in
  code has to refuse every sign-in transaction that an earlier run issued.
  That second part is a requirement on the release that adds sign-in. It is
  not implemented, and that release has to show it before it is deployed.
- For the same reason the rule of section 17 gains one more consequence: a
  database that is rolled back or replaced **under a running service** loses
  records without a new run beginning, and a transaction used in that interval
  could be accepted once more for the rest of its lifetime. The restore is
  carried out by the one script, never by hand.

### Rows, and who removes them

Nothing in this release writes a row. These rules are for the release that
does, and they are stated here so that nothing is built on the opposite
assumption in the meantime:

- **No job removes rows, and none is to be added:** no timer, cron entry or
  maintenance script. The service that records a use deletes, in the same
  transaction, the rows whose expiry has passed by more than a minute. That is
  the only deletion.
- **Never delete rows from this table by hand, and never empty it "to tidy
  up".** A row that is removed makes its transaction usable again until that
  transaction expires.
- **A table that is not empty is normal, and it is not limited to a few
  minutes of sign-ins.** A reboot restarts the monotonic clock near zero. The
  rows that were in the table at that moment stay until a sign-in is recorded
  after the new boot's clock has passed their stored expiry plus the
  allowance. That takes as long as the server had been up before the reboot,
  and it does not happen at all if the server is rebooted again sooner. Every
  such reboot leaves its own rows behind, so rows of several earlier boots can
  accumulate. Each is one 64-character hash and one number. **Leave them.**
  Removal stays by expiry only: no job, script or index is to be added for
  them.
- **Those leftover rows are harmless only on one condition:** the sign-in code
  refuses every sign-in transaction that an earlier run of the service issued,
  before it consults this table. Their expiry values belong to a clock that no
  longer exists, and nothing in the table tells them apart from current rows.
  That check is a requirement on the release that adds sign-in. It is not
  implemented, and that release has to show it before it is deployed.
- `expires_at` is not a date. It is a number of seconds of the server's
  monotonic clock, which counts from boot. Do not read it as a time of day,
  and do not compare it with one.

### One host, one clock

Those expiry values are comparable only between processes that share one
monotonic clock. The deployment meets this today, and the release that adds
sign-in depends on it staying so:

- every process that writes to the database runs on **one host**. Two servers
  on one database file do not share a clock;
- none of them runs in a Linux time namespace of its own (a container or unit
  configured with a separate time namespace has a separate monotonic clock).
  The systemd unit of section 11 uses none; keep it that way;
- the number of Gunicorn workers does not matter for this: workers on one host
  share the clock. (Do not take that as a reason to raise the worker count;
  section "2 GB VPS sizing" still applies.)

### Rollback

**There is no behaviour to revert.** This release only adds storage that
nothing reads or writes: sign-in, sessions, roles and every page are what they
were before it. Forward fix first.

- **Once migration 020 is applied, every build that serves this database ships
  `020_oidc_consumed_transactions.sql`, a recovery build included.** Migrations
  are forward-only, and a build that does not ship an applied migration
  refuses to start on the database (section 21). If application code has to be
  reverted, revert the code and keep the migration, together with the metadata
  that belongs to it: the `oidc_consumed_transactions` entry in
  `src/canonical_tables.py`, and in `data/canonical/manifest.json` the
  migration list and level and the operational table lists. A build that ships
  the migration without them fails its own canonical and database-build
  checks. The table stays; it is not dropped or emptied, by a migration or by
  hand.
- **A snapshot restore is not a route to a build from before 020,** for the
  reasons section 27 gives for migration 019, which hold here unchanged:
  - *A lifecycle build from before 020.* Reconciliation refuses the database
    being replaced as evidence: it has migration 020 applied, that build does
    not ship it, and so it "is not at the schema of this build" (section 26,
    "Restore"). The script stops there, nothing is replaced and the service
    stays stopped.
  - *This build, or a later one.* The script migrates its copy of the snapshot
    to the schema of the deployed build (section 17, step 4), so a snapshot
    from before 020 has migration 020 applied to it. The restored database is
    at 020 again, and a build from before 020 refuses to start on it as it
    did on the one that was replaced.
- **Do not reach for `--replaced-database-unreadable` to get past that
  refusal.** The option is for a database that cannot be read or trusted, not
  for one that an older build does not recognise, and it leaves every
  assignment held with nothing to release them afterwards (section 17,
  "Without the replaced database").
- **The restore procedure and its holds are unchanged.** A restore that is
  needed for its own reasons is still the one script of section 17, run from
  start to finish with a build that ships every applied migration. No step of
  it is skipped or done by hand to suit a build, and an assignment it leaves
  held stays held (section 26, "Restore").
- The rules of sections 26 and 27 are unchanged: never a build from before
  migration 018, and every build that serves a database at 019 ships
  migration 019.

## 29. Google sign-in (PR #331): shipped disabled

**Google sign-in is not activated, and this build cannot activate it in
production.** Nothing in this section has been carried out on any server: no
client was registered with Google, no credential exists, and none of the checks
below was run. The section says what the build does when it is deployed, what
changes in the logs, and what activation will need.

Specification: `docs/reviews/GOOGLE_OIDC_DESIGN.md`; what the code does and
guarantees: `docs/ACCOUNTS_DESIGN.md` §13; routes: `docs/AUTH_ROUTE_CONTRACT.md`.

### What the build does

- It has no migration. Its tables came with migrations 019 and 020 (sections
  27 and 28), which a database must have before this build serves it; startup
  refuses otherwise, as for every pending migration.
- **The activation gate.** `PRODUCTION_ACTIVATION` in
  `web/services/google_oidc.py` is `False`. While it is, `web.wsgi`:
  - starts normally whatever `DZI_GOOGLE_CLIENT_ID` and
    `DZI_GOOGLE_CLIENT_SECRET` hold: unset, one of them, both, well-formed or
    not. The presence of credentials is never a reason to refuse to start;
  - keeps Google sign-in disabled;
  - logs one warning if either is set, naming the settings and never their
    values: `Google sign-in credentials are configured (...), but this build
    does not enable Google sign-in.` The configuration check of section 10
    shows the same line.

  No setting and no environment variable overrides the constant. The release
  that delivers identity recovery tooling sets it to `True`; from then on both
  settings unset means disabled, both valid means enabled, and exactly one or a
  malformed one stops the start with a message that names the setting.
- **Disabled means:** no Google button on the sign-in page and no link form on
  the account page; `/auth/google/start`, `/auth/google/link`,
  `/auth/google/callback`, `/auth/google/link/confirm` and
  `/auth/google/link/cancel` answer the generic 404 page (a cross-origin POST
  is still 403); no page shows the outcome of a Google sign-in, so
  `/login?google=unavailable`, the address Nginx redirects a failed callback
  to, is the ordinary sign-in page; password and TOTP sign-in are unchanged.
- What is new for every visitor whether it is enabled or not: the account page
  `/account` with a „Профил“ link in the navigation, the notice on
  `/account/password` for an account without a password, the operator CLI's
  `--id` selection and `list` columns (section 22), and the log changes below.

### Deploying this build

The standard procedure of section 20, with no migration step. Then check:

```bash
curl -s https://learnpilot.bg/login | grep -c 'auth/google'        # 0: no button
curl -s -o /dev/null -w '%{http_code}\n' -X POST \
  -H 'Origin: https://learnpilot.bg' https://learnpilot.bg/auth/google/start   # 404
journalctl -u learnpilot -n 50 | grep 'Google sign-in'              # nothing, with both settings unset
```

Install the new Nginx template as in section 12 (keep the lines Certbot added
to the server block), then `sudo nginx -t && sudo systemctl reload nginx`. The
`log_format lp_no_args` line and the two new zones belong to the `http`
context, at the top of the file beside the existing zones.

### Logs: what changed, and what it rests on

- **Gunicorn** (section 18): no query string in the access log; the error log
  passes the redacting filter; `loglevel` stays `info`.
- **Nginx, the callback location.** `= /auth/google/callback` has its own
  access-log format, `lp_no_args`, which records `$request_method $uri
  $server_protocol` instead of `$request`: the path without its query string.
  `$limit_req_status` and `$upstream_status` keep a refused or failed callback
  visible, because for the browser it ends as a 302. `error_log /dev/null
  emerg` discards every error-log line for requests in that location and in
  `@oidc_leave`: a refusal by the limit and an upstream failure would
  otherwise be logged with the full request line.
- **Every error status of the callback address leaves the address.** Whether
  Nginx produced it (429 from the limit, 502 and 504 from the upstream) or the
  application did (`proxy_intercept_errors`), the answer is a 302 to
  `/login?google=unavailable` with `Cache-Control: no-store`. No error page is
  ever a document at the address that carries the code and the state. The
  application's own redirects pass through. A callback that Nginx refuses
  never reaches the application: its transaction is not spent and its code is
  not redeemed; the person starts again.
- **The main `error_log` must stay at `error` or stricter.** `info` or `debug`
  there would record request lines for every location.
- A code or a state that survives somewhere outside the server (a browser's
  history) is already spent: the code at Google, the transaction in the
  consumption table.

**What the log statements assume.** Three settings that exist for other
reasons, and that the test suite pins:

1. Gunicorn's `bind` is a loopback address, so a visitor reaches it only
   through Nginx.
2. `MAX_CONTENT_LENGTH` is a number for every request. With it, Werkzeug gives
   every reader of a request body a wrapper that turns the server's read error
   into a 400, which Flask answers without logging.
3. Gunicorn runs the **sync** worker: `worker_class = "sync"` with
   `threads = 1`. A worker class that keeps connections alive reads the unread
   body of a request itself, outside that wrapper.

What they are for: Gunicorn's body reader raises exceptions whose text quotes
the bytes it could not use, and a traceback is not filtered. Nginx parses a
client's chunked body itself and frames what it sends on, so a client's own
chunk framing does not reach Gunicorn; and the wrapper of item 2 covers a body
that the application reads. `proxy_request_buffering on` is the capacity
setting it always was (section 12). It decides when a request is sent on; it
is neither validation nor redaction, and no statement about logs rests on it.
**None of the three has been verified on the server.** Do not change the
worker class or `threads` for capacity without re-reading this paragraph.

### One instance per run

A sign-in or a link in flight is bound to the run of the service that started
it. `preload_app = True` creates that run's value once, in the master, and
every worker inherits it. A deploy is `systemctl restart learnpilot`, which
ends every sign-in and link in flight: the person sees „Времето изтече. Опитай
отново.“ and starts again. A `HUP` keeps the preloaded application and its
value.

**Not supported while Google sign-in is enabled:** a second master on the same
database (Gunicorn's `USR2` re-execution, a second unit, or a process started
by hand beside the service). Single use would still hold, but sign-ins in
flight would fail at the other master, and a cancellation that reaches the
other master could not record that a link has ended.

### Rollback and restore

- **Rollback.** The build has no migration, so its code can be reverted on the
  same database. Under an earlier build, accounts created through Google
  remain and cannot sign in, their existing sessions are ordinary account
  sessions and stay valid until they expire, and linked identities are inert.
  A rollback target must still ship migrations 019 and 020 (sections 27 and
  28). The records in `oidc_consumed_transactions` stay and are not deleted
  for a rollback.
- **Restore.** The one script of section 17, unchanged. The service that
  starts afterwards is a new run, so every sign-in and link issued before the
  restore is refused, and a consumption record lost with the restore cannot
  matter. Users, identities and session keys return to the snapshot: a student
  created through Google after the snapshot is created again as a new account
  at the next sign-in, and a link confirmed after it is gone.
- Nobody deletes rows of `oidc_consumed_transactions`, and no job does
  (section 28).

### Accounts created through Google

They have no email and no password. Find them with `list` (the `google`
column) and manage them by `--id` (section 22). **This release cannot remove
or move a Google identity.** These mistakes can happen once the provider is
active, and none can be corrected without recovery tooling that does not exist
yet:

- a Google identity confirmed onto the wrong account (two people at one
  browser within three minutes);
- a student account created by someone who meant to link Google to an existing
  account, for example a teacher whose account has another email;
- a Google-only account whose Google account is lost: it can be disabled,
  enabled, signed out and given a role, and not signed in to.

Do not correct any of them with SQL.

### Before activation: none of this is done

Activation is a later release and a separate decision. It needs all of the
following; items 8–10 are outstanding checks that have not been run, and
activation stays OFF (`PRODUCTION_ACTIVATION` is `False`) until they pass:

1. Migration 020 deployed (section 28).
2. Identity recovery tooling delivered, in the release that opens the gate.
3. Privacy: a public privacy-policy page (Google's consent screen needs one),
   the data-controller questions for minors, and school Workspace
   administrators allowing the app for accounts marked under 18.
4. The log changes deployed and checked on the server (below).
5. A Google Cloud client of type "Web application" with exactly one authorized
   redirect URI, `https://<DZI_PUBLIC_HOST>/auth/google/callback`, no
   JavaScript origins, and a consent screen with the scopes `openid`, `email`
   and `profile` only. A development run uses a **separate** client.
6. Outbound HTTPS as the service user to `oauth2.googleapis.com` and
   `www.googleapis.com`.
7. The real-provider checks of the design's section 11.2.
8. **Real token and key responses checked against the encoding policy
   (outstanding, not run).** The build sends `Accept-Encoding: identity` on
   its requests to Google and refuses a compressed response (a
   `Content-Encoding` other than identity). Before activation, confirm with
   Google's real token endpoint and JWKS endpoint, in the activation
   window, that their answers arrive uncompressed and are accepted. The tests
   use mocked transport and cannot show this. A refusal here means sign-in
   would fail for everyone.
9. **Production and service tests against the server's real dependencies
   (outstanding, not run).** Run `python3 -m unittest
   tests.test_google_oidc_production tests.test_google_oidc_service` in the
   environment the server actually runs, with the dependency versions
   installed there. The suite so far ran in an assembled environment of
   already-installed packages; it did not exercise the versions production
   installs.
10. **Clean installation from `requirements.txt` (outstanding, not run).**
    Create a fresh virtual environment, install only from the declared
    `requirements.txt`, and confirm the application imports and the checks of
    items 8 and 9 pass there. The assembled test environment validated
    neither installation from the file nor the declared dependency floors; a
    floor that is too low or a missing package would show only here, before
    deployment.
11. Independent review of the implementation.

The off switch, once it is active: remove the two lines from
`/etc/learnpilot/learnpilot.env` and restart. Rotating the secret: a new
secret at Google and in the env file, then a restart; no session ends, and
sign-ins in flight end with „Времето изтече“.

### The log check (not run)

To be run once, by the operator, on the server, in a deployment window, with
Google sign-in still disabled. `<host>` is the value of `DZI_PUBLIC_HOST`.

1. Request `https://<host>/auth/google/callback?code=LOGTEST&state=LOGTEST`
   once; then about sixty times within a few seconds, to pass the burst; then
   once while the service is stopped. Each answer is a 302 to
   `/login?google=unavailable`, and the page it leads to is the ordinary
   sign-in page with no message about Google.
2. Send one request whose query contains a raw space
   (`.../callback?code=LOGTEST x&state=LOGTEST`, from a client that does not
   encode it): once through Nginx and once on the server straight to
   `127.0.0.1:8000`. Then the same with an apostrophe after the second value
   (`...&state=LOGTEST'y`), which makes Gunicorn quote the text with the other
   quotation mark.
3. The two body probes below, each once.
4. None of `LOGTEST`, `LPPROBE_NGINX_BODY` and `LPPROBE_DIRECT_BODY` appears
   in `journalctl -u learnpilot`, `/var/log/nginx/access.log` or
   `/var/log/nginx/error.log` after all of these requests.
5. `ss -ltnp` shows Gunicorn listening on `127.0.0.1:8000` and on no other
   address, and the journal of the running start holds `Using worker: sync`
   (Gunicorn prints `gthread` there when `threads` is above 1).
6. `nginx -T | grep proxy_request_buffering` shows `on` and no `off`, and
   `nginx -T | grep error_log` shows no level looser than `error`.
7. Note the versions the statements above were derived from sources of:
   `nginx -v`, and the lines for Flask, Werkzeug and Gunicorn of
   `/opt/learnpilot/venv/bin/pip freeze`.

**The two body probes.** Rules for both:

- **Nothing real.** The bytes below are the whole request. They hold no email
  address, no password, no cookie, no `state`, no code and no other
  credential, and none is added.
- **Never repeated with a real value,** here or anywhere, and not among the
  callback requests that the later real-provider checks repeat with a real
  `state`.
- **Raw HTTP/1.1.** Every line ends with CRLF, written `\r\n` below, the
  malformed chunk-size line included. A client that encodes or frames a body
  itself cannot send them. Through Nginx the client speaks TLS and does not
  offer HTTP/2.
- **`Connection: close`, and a client timeout of 5 seconds** in total, for
  connecting, sending and reading. At the timeout the client closes its
  socket, whatever it has received.
- **One run of each, inside the deployment window.** Not in a loop, not from a
  monitoring job, and not a second time in that window, whatever the first
  result was.

*Probe N, of Nginx's parser.* To `<host>`, port 443, over TLS:

```
POST /login HTTP/1.1\r\n
Host: <host>\r\n
Content-Type: application/x-www-form-urlencoded\r\n
Transfer-Encoding: chunked\r\n
Connection: close\r\n
\r\n
LPPROBE_NGINX_BODY\r\n
```

With `proxy_request_buffering on`, as deployed, it passes when all four hold:
the answer has status 400; Nginx's error log has one new entry for it with the
text `client sent invalid chunked body`; Gunicorn's access log, in the
journal, has no entry for it, while Nginx's own access log shows it with 400;
and `LPPROBE_NGINX_BODY` is in none of the three logs. These criteria are no
test for a server with buffering off.

*Probe D, of Werkzeug's wrapper as deployed.* On the server, to
`127.0.0.1:8000`, in plain HTTP. Its control request comes first and must
answer 200, the sign-in page; if it does not, probe D is not sent:

```
GET /login HTTP/1.1\r\n
Host: <host>\r\n
X-Forwarded-Proto: https\r\n
X-Forwarded-Host: <host>\r\n
X-Forwarded-For: 192.0.2.77\r\n
Connection: close\r\n
\r\n
```

Then the probe, with the same headers:

```
POST /login HTTP/1.1\r\n
Host: <host>\r\n
Origin: https://<host>\r\n
X-Forwarded-Proto: https\r\n
X-Forwarded-Host: <host>\r\n
X-Forwarded-For: 192.0.2.77\r\n
Content-Type: application/x-www-form-urlencoded\r\n
Transfer-Encoding: chunked\r\n
Connection: close\r\n
\r\n
LPPROBE_DIRECT_BODY\r\n
```

It passes when all five hold: the control answered 200; the answer has status
400 and Gunicorn's access log has its entry, `POST /login` with 400, from
`192.0.2.77`; the journal has no line `Invalid request from ip=` and no line
`Error handling request` for it; the journal has no traceback for it, in
particular no `Exception on /login [POST]` and no `Socket error processing
request.`; and `LPPROBE_DIRECT_BODY` is in none of the three logs.

**Any other result is not a pass,** for either probe: another status, a
missing or an additional log entry, a marker in any log, a client that ran
into its timeout, or a result that cannot be attributed to the probe. A 403
for probe D means that the same-origin check refused the request before the
body was read; the probe is not sent again, the cause is found and corrected,
and a new run, with its control request, is scheduled. A 500, a traceback or a
marker in a log is a finding, and activation stops there.

## 30. Registration storage (migration 021)

Migration `021_registration_schema.sql` adds the storage that email/password
registration will use: one nullable column, `users.email_verified_at`, and five
tables, `user_profiles`, `teacher_subjects`, `pending_registrations`,
`registration_sends` and `registration_control`
(`docs/reviews/EMAIL_REGISTRATION_DESIGN.md`, section 6.1; PR 1 of section 9;
contract in `docs/reviews/EMAIL_REGISTRATION_PR1_STATUS.md`).

**This release is storage and validation only. Registration is not
implemented.** There is no route, page, mail sender, setting, secret, service,
timer, Nginx or Gunicorn change, and nothing in the running application reads
or writes the new tables or the new column. Sign-in, sessions, roles, Google
sign-in and admin TOTP work exactly as before (sections 23 and 29). Nothing is
activated, and no gate exists yet to turn on.

### Deployment order

The standard procedure of section 20, with nothing added to it.

- The migration is additive. It is not a table rebuild. `ALTER TABLE users ADD
  COLUMN email_verified_at TEXT` leaves every existing row NULL ("never
  verified here"); no row is written, no table rebuilt, and accounts, password
  hashes, session keys, second factors, identities, assignments and attempts
  are untouched. Nobody is signed out.
- On a database that already has every migration through
  `020_oidc_consumed_transactions.sql` the dry run of step 7 lists exactly
  `021_registration_schema.sql`. If it lists earlier migrations too, the
  database is behind that level and each of them has the conditions described
  where it is introduced (section 9 for a table rebuild, sections 26 to 28).
- Apply in step 8, after the step 3 snapshot, only what the dry run listed.
  From this release on the application refuses to start on a schema without
  migration 021 (section 9).
- The new tables start empty and stay empty: nothing writes to them. The
  profile service (`web/services/profiles.py`) has no caller.
- No account, operator or teacher has anything to do afterwards.

### Backup and restore

The tables are part of the live database, so the snapshot of section 16 and the
restore of section 17 cover them with no change; `deploy/learnpilot-restore.sh`
is not changed. A snapshot from before this release is restored as before: the
script migrates its copy to the schema of the deployed build (section 17,
step 4), which adds the empty tables and the NULL column. The tables are
runtime state, never exported to `data/canonical`, and a database built by
`src.build_database` has them empty. The restore hold and the scrub of
unverified registrations (`registration_control`) are PR 5 and not here.

### Rollback

**There is no behaviour to revert.** Forward fix first.

- **Once migration 021 is applied, every build that serves this database ships
  `021_registration_schema.sql`, a recovery build included.** Migrations are
  forward-only, and a build that does not ship an applied migration refuses to
  start on the database (section 21). If application code has to be reverted,
  revert the code and keep the migration, together with its metadata: the five
  entries in `src/canonical_tables.py`, and in `data/canonical/manifest.json`
  the migration list and level and the runtime table lists. A build that ships
  the migration without them fails its own canonical and database-build checks.
  The tables and the column stay; they are not dropped or emptied.
- **A snapshot restore is not a route to a build from before 021,** for the
  reasons sections 27 and 28 give for migrations 019 and 020, which hold here
  unchanged. Do not use `--replaced-database-unreadable` to get past that
  refusal.
- Code from before this release names its `users` columns and runs unchanged
  against the migrated schema.

## 31. Mail interface and sender process (registration PR 2): no caller

This release adds `web/services/mail.py` and `web/services/mail_sender.py`
(`docs/reviews/EMAIL_REGISTRATION_DESIGN.md`, section 4; PR 2 of section 9;
contract in `docs/reviews/EMAIL_REGISTRATION_PR2_STATUS.md`).

**Nothing calls them. Registration is not implemented and no message is ever
sent.** There is no route, page, migration, table, secret, timer, unit, Nginx
or Gunicorn change. `web.wsgi`, `web/production.py` and
`deploy/learnpilot.env.example` are not changed: a production start neither
reads nor validates a mail setting in this release, and starts exactly as
before with or without one. Sign-in, sessions, roles, Google sign-in and admin
TOTP work as before (sections 23 and 29).

### Deploying this build

The standard procedure of section 20, with nothing added to it. No migration
is pending because of this release, nobody is signed out, and there is nothing
to configure. **Do not add mail settings to
`/etc/learnpilot/learnpilot.env` for this release.** They would be ignored.
Choosing a provider, a sender address and credentials is an activation
prerequisite that is not done (design, sections 0.3 and 4.4).

### The settings a later release will read

Listed so that they are not invented differently later. None has a default
that sends anything; with `DZI_MAIL_BACKEND` unset, mail is disabled, which is
not an error.

| Setting | Rule |
| --- | --- |
| `DZI_MAIL_BACKEND` | `smtp`. `console` and `file` show the code in clear and are refused for production. |
| `DZI_MAIL_FROM` | The no-reply sender: `address` or `Name <address>`. Required; there is no default sender. |
| `DZI_SMTP_HOST`, `DZI_SMTP_PORT`, `DZI_SMTP_SECURITY` | All three required for `smtp`. Security is `starttls` or `tls` (both verify the certificate and the host name), or `none` for a loopback host only. |
| `DZI_SMTP_USERNAME`, `DZI_SMTP_PASSWORD` | Optional, set together. The password belongs in the environment file, never on a command line or in the repository. |
| `DZI_MAIL_SPOOL_DIR` | `file` backend only (development): an existing directory outside the checkout. |
| `DZI_MAIL_HOURLY_CAP` | 1 to 300; 300 when unset. |

### What the sender process is, for whoever operates it later

One send starts one short-lived child of the Gunicorn worker:
`<venv>/bin/python -I -S /opt/learnpilot/app/web/services/mail_sender.py`. It
gets its settings and the message on standard input, never as arguments or
environment variables; its environment holds only the locale and, when set,
`SSL_CERT_FILE` and `SSL_CERT_DIR`; it inherits no descriptor of the worker
and its standard error is the null device.

The time limits are three separate things. Creating the process is not
bounded. Once `Popen` has returned, the worker exchanges data with the sender
for at most eight seconds and then decides to kill it; killing and reaping it
may add up to two more seconds. The sender also ends itself nine seconds after
its first statements ran, which is what ends it when its worker has died.
There is no bound on the whole request, and senders can overlap for a few
seconds when Gunicorn replaces a worker (design, section 4.2).
`KillMode=mixed` in the unit ends a sender on a service stop. One sender at a
time per worker rests on the pinned `worker_class = "sync"` and `threads = 1`
of `deploy/gunicorn.conf.py`. A worker that ignores `SIGCHLD` starts no sender
at all; Gunicorn's sync worker does not ignore it.

**None of this was validated on the VPS or under the real unit.** The
independent reviewer exercised the code on Linux (the `epoll` selector, fake
peers over real TLS, inherited signal states); that is not the systemd
sandbox, not a provider and not a capacity measurement. The forced hang, the
worker killed during a send, descriptor and environment isolation under
systemd's sandboxing, the worker's inherited signal state and the capacity
measurement remain activation prerequisites (design, section 0.3; test plan,
section L).

### Rollback

**There is no behaviour to revert.** Reverting the code removes two modules
that nothing imports. No data is involved.

## 32. Registration service and migration 022 (registration PR 3): no caller

This release adds `web/services/registration.py`, three functions in
`web/services/accounts.py` and migration
`022_registration_issuance_identity.sql`
(`docs/reviews/EMAIL_REGISTRATION_DESIGN.md`, sections 2, 3, 4.3 and 6.3; PR 3
of section 9; contract in `docs/reviews/EMAIL_REGISTRATION_PR3_STATUS.md`).

**Nothing calls the service. Registration cannot be reached and no message is
ever sent.** There is no route, page, gate, secret, CLI command, timer, unit,
Nginx or Gunicorn change, and `web.wsgi`, `web/production.py` and `deploy/`
are not changed. The tables of migration 021 stay empty. Sign-in, sessions,
roles, Google sign-in and admin TOTP work as before (sections 23 and 29).

### Deployment order

The standard procedure of section 20, with nothing added to it.

- The migration is additive and is not a table rebuild: `ALTER TABLE
  registration_sends ADD COLUMN issuance_id TEXT` with a CHECK, and one
  partial unique index. No row is written. `registration_sends` is empty in
  every deployment, because nothing has ever written to it; if it were not,
  each existing row would keep its values and get no identity (the service
  then never sends or settles such a row, and still counts it in every
  limit). Accounts, profiles and every other table are untouched. Nobody is
  signed out.
- On a database that already has every migration through
  `021_registration_schema.sql` the dry run of step 7 lists exactly
  `022_registration_issuance_identity.sql`. If it lists earlier migrations
  too, the database is behind that level; see the section that introduces
  each (section 30 for 021).
- Apply in step 8, after the step 3 snapshot, only what the dry run listed.
  From this release on the application refuses to start on a schema without
  migration 022 (section 9).
- There is nothing to configure and nothing for an account, operator or
  teacher to do afterwards.

The cleanup timer, the restore gate and the snapshot step that registration
needs before it is ever enabled are not part of this release (design, section
9, PR 5) and must not be assumed to exist.

### Backup and restore

Unchanged. A snapshot from before this release is migrated to the deployed
schema by the restore script (section 17, step 4), which adds the NULL column
and the index. The table is runtime state and is never exported to
`data/canonical`.

### Rollback

**There is no behaviour to revert.** Forward fix first.

- **Once migration 022 is applied, every build that serves this database ships
  `022_registration_issuance_identity.sql`, a recovery build included**
  (section 21). If application code has to be reverted, revert the code and
  keep the migration together with its entry in the migration list and level
  of `data/canonical/manifest.json`. The column and the index stay; they are
  not dropped.
- A snapshot restore is not a route to a build from before 022, for the
  reasons sections 27, 28 and 30 give for migrations 019 to 021. Do not use
  `--replaced-database-unreadable` to get past that refusal.
- Reverting only the code removes a module that nothing imports and three
  functions that nothing calls.

## 33. Student registration routes (registration PR 4): shipped disabled

This release adds the student registration routes (`/register`,
`/register/student`, `/register/verify`, `/register/resend`,
`/register/cancel`, and `/register/teacher`, which is always 404), their
pages, the production wiring of the mail settings, the
`learnpilot.registration` log handler, the Nginx `lp_register` zone and three
operator commands (`docs/reviews/EMAIL_REGISTRATION_DESIGN.md`, sections 6.2
to 6.6 and 7.3 to 7.5; contract in
`docs/reviews/EMAIL_REGISTRATION_PR4_STATUS.md`; routes in
`docs/AUTH_ROUTE_CONTRACT.md`, "Student registration").

**Registration is not activated, and this build cannot activate it in
production.** Nothing in this section has been carried out on any server: no
provider, sender address or credential exists, no message was sent, and none
of the checks below was run.

### What the build does

- **No migration.** It needs migrations 021 and 022 (sections 30 and 32),
  which a database must have before this build serves it.
- **The activation gates.** `PRODUCTION_ACTIVATION` and `TEACHER_ACTIVATION`
  in `web/services/registration.py` are `False`. While the first is, `web.wsgi`:
  - starts normally whatever the `DZI_MAIL_*` and `DZI_SMTP_*` settings hold
    (unset, partial, malformed, or a `console`/`file` backend): mail settings
    are never a reason to refuse to start;
  - keeps registration disabled: every `/register...` route answers the
    generic 404 page (a cross-origin POST is still 403), the sign-in page has
    no „Регистрирай се“ link, no row is created and no message is sent;
  - logs one warning if any mail setting is set, naming the settings and never
    their values: `Mail settings are configured (...), but this build does not
    enable registration.`

  No setting and no environment variable overrides either constant; a
  configured backend never enables registration by itself. A later,
  separately authorised release sets `PRODUCTION_ACTIVATION` to `True` only
  after the prerequisites of design 0.3; from then on a configured backend must
  be usable for production (`smtp` only: `console` and `file` stop the start,
  as does any malformed setting, named and never echoed) and no backend means
  disabled. `/register/teacher` stays 404 until teacher quotas exist (PR 7).
- **Logs.** `learnpilot.registration` gets a handler like the lifecycle and
  Google loggers (stderr, so the journal). Lines are an event name with row
  ids, an outcome or an exception class name; never an address, a name, a
  password, a code, the binding token, an issuance identity or a message.
  Lines named `*_decided` are written inside a transaction before its commit
  and are not evidence that anything became durable; `submitted`,
  `send_settled`, `resent`, `activated`, `email_taken`, `wrong_code`,
  `cancelled` and `gate_off` are written only after the commit returned.
- What is new for every visitor whether it is enabled or not: nothing visible.
  `manage_accounts list` gains two columns, `verified` (the time a
  registration verified the address; `-` for every CLI or Google account,
  which means "never verified by registration", not "unusable") and `type`
  (the declared profile type; `-` without a profile).

### Deploying this build

The standard procedure of section 20, with no migration step. **Do not add
mail settings to `/etc/learnpilot/learnpilot.env` for this release:** they
would only produce the warning above. Then check:

```bash
curl -s https://learnpilot.bg/login | grep -c '/register'                     # 0: no link
curl -s -o /dev/null -w '%{http_code}\n' https://learnpilot.bg/register       # 404
curl -s -o /dev/null -w '%{http_code}\n' -X POST \
  -H 'Origin: https://learnpilot.bg' https://learnpilot.bg/register/student    # 404
```

Install the new Nginx template as in section 12 (keep the lines Certbot added
to the server block), then `sudo nginx -t && sudo systemctl reload nginx`. The
`lp_register` zone belongs to the `http` context beside the existing zones.
`/register/student`, `/register/teacher` and `/register/resend` use it
(`burst=90 nodelay`, refill two a minute); `/register/verify` uses the
sign-in zone `lp_login`. The values are proposals, to be tuned on a
classroom-sized run before activation; the zone keys on the client address
Nginx sees, so it is only as good as the one trusted proxy hop. Every address
under `/register` (including `/register` and `/register/cancel`, which have no
limit) is answered with exactly one `Referrer-Policy: no-referrer`: its location
repeats the server-level `X-Content-Type-Options` and `X-Frame-Options`
(`add_header` in a location replaces the inherited ones) and hides the
application's copy of the policy. After installing the template, check once that
`curl -sI https://learnpilot.bg/register` shows a single `Referrer-Policy`. A template
change is not evidence that the server's Nginx accepts it: `nginx -t` on the
server is.

### Operator commands (`src.manage_accounts`)

| Command | Does |
| --- | --- |
| `registrations-cleanup` | Expires and scrubs pending registrations past their 30 minutes, deletes final rows after 24 hours and send records after 48 hours. Needs no secret. Refuses while the restore lock exists, and changes nothing (non-zero exit) while the clock is behind the registration high-water mark. |
| `registrations-cleanup --check` | Changes nothing; exits non-zero when a pending registration is older than an hour (the cleanup has not run, or cannot). |
| `registrations-clock-accept` | After the operator has checked the server's time: sets the high-water mark to the present, ending a "clock behind" refusal. Touches no hold and no registration. |
| `clear-registration-limit --email <email>` | Deletes the send history of one address (needs `DZI_SECRET_KEY`). It says before it acts that the address may then receive up to 8 more codes the same day, which weakens the guessing bound for it. |

```bash
sudo -u learnpilot /opt/learnpilot/venv/bin/python -m src.manage_accounts --db "$DZI_DB" registrations-cleanup --check
```

The cleanup timer, the restore gate, the hold commands, the restore script's
handling of registration and the snapshot scrub were not part of that release;
they are PR 5 (section 34).

### Rollback

**There is no behaviour to revert while the gate is off.** Reverting the code
removes routes that answer 404, three operator commands and two `list`
columns. No data is involved; the tables of migrations 021 and 022 stay, and
every build that serves the database keeps shipping those migrations
(sections 30 and 32).

## 34. Registration operations (registration PR 5): cleanup timer, restore hold, snapshot scrub

Scope 8.1 extends these existing commands to password-recovery storage in
migration 025. See section 37 for counts, fail-closed schema checks, backup
scrub and the mandatory restore isolation/credential-repair procedure. The
script and its ownership/lifetime-lock protocol remain unchanged.

This release adds the operational pieces registration needs before it is ever
enabled (`docs/reviews/EMAIL_REGISTRATION_DESIGN.md`, sections 3.9, 6.5, 6.6; PR 5
of section 9; decisions D18 (b) and D20 (a); contract and open evidence in
`docs/reviews/EMAIL_REGISTRATION_PR5_STATUS.md`). **Registration stays off: no
gate is turned on and no migration is added.** Nothing here has been run on a
server.

### What the build adds

- `deploy/systemd/learnpilot-registration-cleanup.service` and `.timer`: the
  existing `registrations-cleanup` command every ten minutes (`OnCalendar=*:0/10`,
  `Persistent=true`). A template, like the backup units: the paths are those of
  the backup units (`/opt/learnpilot/venv`, `/opt/learnpilot/app`, the database
  from `/etc/learnpilot/backup.env`, user `learnpilot`); change them together
  with the other units. It needs no secret and reads the backup unit's
  settings file, which holds state paths only.
- `deploy/learnpilot-restore.sh`: stops the cleanup timer, requires the cleanup
  service idle, runs the gate on the staged copy and checks the result (section 17).
- `src.manage_accounts`: `registrations-restore-gate`, `registrations-restore-status`
  and `registrations-release-hold`.
- `src.backup_sqlite`: the logical scrub of the snapshot copy (section 16).

### Installing the cleanup timer

Once, on the server, after this build is deployed and migrations 021 and 022 are
applied. Not part of a routine deploy: do it deliberately.

```bash
sudo cp deploy/systemd/learnpilot-registration-cleanup.service deploy/systemd/learnpilot-registration-cleanup.timer /etc/systemd/system/
sudo systemd-analyze verify /etc/systemd/system/learnpilot-registration-cleanup.service /etc/systemd/system/learnpilot-registration-cleanup.timer
sudo systemctl daemon-reload
sudo systemctl enable --now learnpilot-registration-cleanup.timer
sudo systemctl start learnpilot-registration-cleanup.service     # one run now, to see it work
systemctl show -p ActiveState,Result learnpilot-registration-cleanup.service
```

Run `systemd-analyze verify` before enabling: the units have not been validated by
systemd. The cleanup refuses while the restore lock exists (the unit's
`ConditionPathExists=` and the command itself) and does nothing while the clock is
behind the registration high-water mark. A run that cannot take the database's
write lock fails and leaves the unit `failed`; the next tick tries again. The unit
is a oneshot: a tick that arrives during a run starts nothing. Do not give it
`Restart=`.

### Restore: what happens to registration

After every restore, registration is closed. On the staged copy the gate voids
and scrubs every pending registration (the recovery credentials of PR 8 do not
exist yet and will be added to the gate with that PR) and sets the hold
`restore`; a hold the snapshot already had is kept with its time. The ledger of
the database that was replaced is **not** carried over and nothing is merged.
Sign-in is not affected. Pending registrants start again after the hold ends.

Check, then release, with the service running:

```bash
sudo -u learnpilot /opt/learnpilot/venv/bin/python -m src.manage_accounts --db "$DZI_DB" registrations-restore-status
sudo -u learnpilot /opt/learnpilot/venv/bin/python -m src.manage_accounts --db "$DZI_DB" registrations-release-hold --acknowledge-lost-history
```

`registrations-release-hold` prints the consequence first and refuses without the
flag. By giving it you accept that the send accounting since the snapshot is
gone: an address that already received 8 codes in a day may receive up to 8 more
(80 guesses against its codes, once), and the total hourly budget and the circuit
start from the snapshot's state. It clears the hold and nothing else: **not** the
clock high-water mark (if the clock is behind it, the command says so and
registration still answers "unavailable" until the clock passes the mark or you
have checked the time and run `registrations-clock-accept`) and no limit. It
refuses while a restore lock exists, and looks for the lock again once it holds
SQLite's write lock (a restore that begins while the command waits stops it,
with the hold and the mark unchanged). **Nothing releases the hold automatically,
whatever the clock does.**

The cleanup timer comes back on every path the backup timer does (section 17,
steps 2 and 11). Its intent is a note in the restore's own directory,
`/var/lib/learnpilot-restore`, written and flushed (file, then directory) before
the timer is stopped, and removed only after the timer was started again.

**What decides is in that directory, not in the lock directory.** The lock
directory (`/var/lib/learnpilot/data/restore-in-progress`) keeps the
application, the backup and the cleanup from starting, and that is all it does:
it sits in a directory the service can write, so the service user could remove
or replace it. The script never takes it as evidence. It records who holds the
restore in `restore-owner`, and that a replacement of the live state has begun in
`swap-state` (flushed to disk before the first move, and kept until a restore has
finished or the run that began it has moved everything back).

**Two locks, always taken in this order.**

1. *The guard*, `flock` on the directory. Each of these is one step under it:
   taking the restore (or taking over an abandoned one) together with the record
   of its holder; writing a timer's note, flushing it and stopping the timer,
   which only the holder does; recording the replacement as begun, or as taken
   back; starting a timer and then removing its note, which happens only while no
   restore is held or left unfinished (no `restore-owner`, no `swap-state`, and
   no lock directory either); giving the restore back. A run stopped while it
   takes the restore removes the lock directory only if it is empty and no
   `swap-state` is recorded: a half-replaced state stays locked.
2. *The lifetime lock*, `flock` on `lifetime` in that directory. Every command
   the script has root or the service user carry out once it holds the restore
   (a copy, a move, a checkpoint, a migration of the copy, the stop and start of
   the service) is run as `sudo flock -s lifetime sh -c ... COMMAND`: it checks,
   under the lock, that its run is still the recorded holder, and then becomes
   the command, which keeps the lock's descriptor. The service user's commands
   are run through `setpriv`, not `sudo -u`, because `sudo` closes the
   descriptors it was given before it runs a command. So the lock is held by the
   process that does the work, and by whatever that process starts, until the
   last of them has ended or died, whatever became of the script. (`setpriv`
   comes with util-linux. The pre-flight already runs the service user's
   commands through it, so a `setpriv` without `--reset-env` stops the script
   before anything is changed.)

Taking over an abandoned restore takes the lifetime lock exclusively, without
waiting, and only under the guard. Only the step that takes the restore holds
both locks, and it never waits for the second. So a restore is taken over only
if its recorded holder is not a running restore **and** no command it started is
alive. A command that starts after the takeover finds a new holder recorded and
is not carried out. The lock directory being there, missing or replaced changes
none of this.

So a run that recovers a timer can never remove the note of a restore that holds
it. A run that finished and finds a new restore holding one leaves the timer and
the note to that restore (and ends not 0). A command left over from a killed run
can never write over the state a later restore put in place. The locks are the
kernel's: each goes away with the processes that hold it, a crash included. A
signal to the script, or a `SIGKILL` of it, does not cut a step under the guard
short (it is carried out by processes of its own); a power loss, or a kill of
every process at once, can, between any two of its commands.

**A command that does not end.** If a command an earlier restore started hangs
(a disk that does not answer), it keeps the lifetime lock, and every later run
refuses to take the restore over (`a command an earlier restore started is still
running`). That is deliberate: two runs that both write the live state are worse
than a restore that waits. What to do is in section 17, "If it stops":
`sudo fuser -v /var/lib/learnpilot-restore/lifetime`, end that process, and
check that nothing is listed any more. Only a reboot ends one that `kill -9`
does not.

A run that is stopped (signal) or fails after the service was started retries
the start, keeps its exit status, and still says what the restored state holds
(registration held, `RESTORED, NOT RECOVERED: N assignment(s)`). A run killed
outright leaves the note, and the next run of the script starts the timer before
anything else (or, if a lock is left, at its end). Starting a timer that already
runs changes nothing, so a note can be acted on more than once. The script never
ends with 0 while a noted timer is not active, and it says apart a timer that
could not be started from one that was started and whose note could not be
removed.

**How durable a note is.** A note is on disk before its timer is stopped: a
stopped timer always has its note, after any crash. Its removal is flushed as
well, but only after the timer was started: a power loss between the two can
bring the note back. The next run of the script then starts that timer once more,
which changes nothing while it runs. If you had stopped that timer deliberately
in between, that run starts it again and says so (`... timer is active again.`).

#### The restore's own directory

`/var/lib/learnpilot-restore` (`LEARNPILOT_RESTORE_RECORDS` for the script):
root's, mode 0700, and outside `/var/lib/learnpilot`, which the service can
write. Section 2 makes it; the script makes it on its first run if it is missing.
In its pre-flight, before anything is stopped, the script checks that every
directory from `/` down to it is a directory and not a symbolic link, belongs to
root and can be written by no one else (a sticky directory, such as `/tmp`, is
accepted above it), that it is itself root's, with mode 0700 and without an
access control list, and that each record in it is absent or a plain file of
root's with a single name. Anything else (a symbolic link, dangling or not, a
directory, a pipe, a second name of another file) stops the script, which then
repairs nothing and writes, reads and removes nothing through it. Every step
under the guard checks the same again before it uses a record. As only root can
change anything there, what is checked is what is used. The guard is that
directory, and the lifetime lock a file in it, so both are protected as the
records are. The records are `restore-owner`, `swap-state`, `lifetime` and the
two `pending-*-timer` notes.

**Upgrading to this script.** No released version kept a note outside the lock.
A lock an earlier version left is taken over: in the same step, before the new
holder is recorded, its markers (`backup-timer-was-active`,
`registration-cleanup-timer-was-active`, `swap-started`) are written as notes
and as `swap-state`. Then every note and `swap-state` there is flushed to disk,
including one an earlier attempt wrote and could not flush: a retry flushes
again and never takes a record being there for its flush. If any of that cannot
be done, nothing changes hands, and the lock and its markers are as they were. A `pid` file in it is
ignored, and nothing is ever written into the lock directory. An earlier
version's restore that is still running is therefore not seen: its holder is
not recorded and its commands do not hold the lifetime lock. **Do not deploy this
script while a restore of an earlier version is running**; let it finish, or
make sure that it and everything it started have ended, first. Compatibility
with a running older restore is not implemented.
Only the build of this PR that was reviewed, 39157f5 (never merged), kept notes in
`/var/lib/learnpilot/restore-pending-*`. The script refuses to run while one
is there, and does not trust it: the service can write that directory. Look at
it. If the timer it names should run, bring it back with the command below, then
remove the old file (`sudo rm -f -- /var/lib/learnpilot/restore-pending-<timer>`).

#### Bringing a noted timer back without a restore

A noted timer that systemd refused to start stays noted until it is started. After
correcting the cause, the next run of the script starts it first. To bring it
back without a restore, take the guard, make sure that no restore is held or left
unfinished (no `restore-owner`, no `swap-state`, and no lock directory either),
start the timer, and only then remove its note, all in one command:

```bash
sudo flock /var/lib/learnpilot-restore sh -ec '
  if [ -e /var/lib/learnpilot-restore/restore-owner ] || [ -L /var/lib/learnpilot-restore/restore-owner ] \
    || [ -e /var/lib/learnpilot-restore/swap-state ] || [ -L /var/lib/learnpilot-restore/swap-state ] \
    || [ -e /var/lib/learnpilot/data/restore-in-progress ] || [ -L /var/lib/learnpilot/data/restore-in-progress ]; then
    echo "a restore is running or was left unfinished: the timer stays stopped for it" >&2
    exit 1
  fi
  systemctl start learnpilot-registration-cleanup.timer
  rm -f -- /var/lib/learnpilot-restore/pending-registration-cleanup-timer
  sync -- /var/lib/learnpilot-restore'
```

(The backup timer likewise: `learnpilot-backup.timer`, `pending-backup-timer`.) It
fails, and removes nothing, if the start fails. It refuses as well when anything
at all stands at one of those three names (a link, a directory): that is not
what the script leaves, and nothing is decided from it. A `swap-state` without
`restore-owner` and without a lock directory still means that the live state may
be half replaced: the timers stay stopped, and the script is run again, which
finishes the restore and then starts them. Never remove a note in any other
way: without the guard, or while a restore is held or left unfinished, it can be
the note of a restore that is running, and that timer would then stay stopped for
good. Never remove `restore-owner`, `swap-state` or `lifetime` by hand: a restore
left unfinished is ended by running the script again, which takes it over.

A database replaced by hand, outside the script, gets none of this (section 17).

### Snapshots: what the scrub does and does not do

A snapshot's database holds no pending registration and no personal or credential
column of one. It is **a logical scrub**: no physical erasure is claimed or
provided. Old bytes can remain in free pages and in unused space inside pages of
the snapshot file, in the write-ahead log of the live database, in earlier
snapshots, in off-server copies of earlier snapshots, and in storage below the
application (filesystem or provider snapshots). The scrub changes no snapshot
already published and does not touch the live database, which stays as it is. No
rebuild of the file (`VACUUM`) and no `secure_delete` setting is used. A database
from before the registration storage (no `pending_registrations` table) is backed
up unchanged; a table that exists without the columns of migration 021 stops the
backup (non-zero exit, nothing published). Snapshots
keep accounts, profiles and the ledger's keyed address hashes for as long as they
are kept; the privacy notice (D16) has to say so.

### Backup and restore of this release

No schema change. Operators of an older snapshot: the restore script migrates the
copy first (section 17, step 4), then applies the gate.

### Rollback

Disable the timer (`sudo systemctl disable --now learnpilot-registration-cleanup.timer`)
and revert the code. A restore with the older script would not hold registration:
do not use an older script on a database that has registration data. A hold already
set stays in `registration_control` until released. An older script does not read
`/var/lib/learnpilot-restore`. Roll back only when no restore is running or left
unfinished: `restore-owner` and `swap-state` absent, and nothing listed by
`sudo fuser -v /var/lib/learnpilot-restore/lifetime`. A restore left unfinished
is finished with this script first, because an older one would neither see that
a replacement had begun nor keep a left-over command out. Then bring back any
timer noted there (command above).

## 35. Account profile flow and migration 023 (registration PR 6): shipped disabled

This release adds migration `023_profile_required.sql`, the profile page
(`/account/profile`), the profile on `/account`, the mark `profile_required_at`
written at Google onboarding, the profile check at the start of a new quiz
attempt, and the build gate that keeps all of it off
(`docs/reviews/EMAIL_REGISTRATION_DESIGN.md`, sections 6.1, 7.3 and 7.4; PR 6 of
section 9; contract in `docs/reviews/EMAIL_REGISTRATION_PR6_STATUS.md`; routes in
`docs/AUTH_ROUTE_CONTRACT.md`, "Account profile").

**The profile flow is not activated, and this build cannot activate it in
production.** Nothing in this section has been carried out on any server, and no
activation prerequisite of the design (0.3) is met.

### What the build does

- **The gate.** `PRODUCTION_ACTIVATION` in `web/services/profiles.py` is `False`.
  While it is, `web.wsgi`:
  - answers `/account/profile` with the generic 404 page for every signed-in
    account (an anonymous visitor is sent to `/login?next=...`, as for every
    account page; a cross-origin POST is still 403);
  - shows no profile section and no link on `/account`;
  - redirects after Google onboarding exactly as before and writes no mark;
  - starts a new attempt exactly as before, whatever `profile_required_at`
    holds (a mark that exists stays stored and is ignored).

  No setting and no environment variable overrides the constant in production.
  `DZI_PROFILES_ENABLED` is read by a **development** run (`web.app`) only, to
  turn the flow on locally (`1`, `true`, `yes` or `on`); `web.production` never
  reads it to decide anything. If it is set while the constant is off, the start
  logs one warning that names the setting and never its value: `DZI_PROFILES_ENABLED
  is set, but this build does not enable account profiles.` It is never a reason to
  refuse to start. A later, separately authorised release sets the constant to
  `True`, after the prerequisites of design 0.3, and the profile gate is the
  step after the student gate in the rollout order of design 7.5.
- **Migration 023** adds one nullable column, `users.profile_required_at`; see
  below. Nothing reads it while the gate is off. It is not part of the loaded
  account (`accounts._USER_COLUMNS` is unchanged), so no request reads it.
- **The mark** is written in one place only: the transaction in which Google
  onboarding creates a student, and only while the gate is on. **No existing
  account is marked and no operator command marks one** (there is no
  `require-profile`): every account that exists when the migration runs has NULL.
- **The check** is made inside `quiz_start_or_resume_attempt`, under the writer
  lock, at the point where an attempt would be created, after an existing attempt
  and a closed assignment have been handled as before. It creates no row.
- `manage_accounts list` is unchanged. `set-role` writes neither a profile nor the
  mark.

### Deployment order

The standard procedure of section 20, with nothing added to it.

- The migration is additive and is not a table rebuild: `ALTER TABLE users ADD
  COLUMN profile_required_at TEXT`. No row is written; every existing account,
  its password hash, session key, second factor, identity, profile, assignment
  and attempt is untouched. Nobody is signed out.
- On a database that already has every migration through
  `022_registration_issuance_identity.sql` the dry run of step 7 lists exactly
  `023_profile_required.sql`. If it lists earlier migrations too, the database is
  behind that level; see the section that introduces each (sections 30 and 32 for
  021 and 022).
- Apply in step 8, after the step 3 snapshot, only what the dry run listed. From
  this release on the application refuses to start on a schema without migration
  023 (section 9).
- Nothing is configured and nothing is for an account, operator or teacher to do
  afterwards. Do not set `DZI_PROFILES_ENABLED` in `/etc/learnpilot/learnpilot.env`:
  it would only produce the warning above.

Check after deploying that the flow is invisible:

```bash
# Signed in as any account: 404. (Anonymous: a redirect to /login, as for every account page.)
curl -s -o /dev/null -w '%{http_code}\n' -b "$SESSION_COOKIE" https://learnpilot.bg/account/profile
curl -s -b "$SESSION_COOKIE" https://learnpilot.bg/account | grep -c '/account/profile'   # 0
```

### Backup and restore

Unchanged. The column is part of the live database, so the snapshot of section 16
and the restore of section 17 cover it; `deploy/learnpilot-restore.sh` is not
changed. A snapshot from before this release is migrated to the deployed schema by
the restore script (section 17, step 4), which adds the NULL column. `users` is
runtime state and is never exported to `data/canonical`.

### Rollback

**There is no behaviour to revert while the gate is off.** Forward fix first.

- **Once migration 023 is applied, every build that serves this database ships
  `023_profile_required.sql`, a recovery build included** (section 21). If
  application code has to be reverted, revert the code and keep the migration
  together with its entry in the migration list and level of
  `data/canonical/manifest.json`. The column stays; it is not dropped or emptied.
  Code from before this release names its `users` columns and runs unchanged
  against the migrated schema.
- A snapshot restore is not a route to a build from before 023, for the reasons
  sections 27, 28 and 30 give for migrations 019 to 021. Do not use
  `--replaced-database-unreadable` to get past that refusal.
- **Turning the gate back off after it was on** (a later release) is a code change
  and a restart: `/account/profile` is 404 again, the link and the check are gone,
  and the marks and profiles written meanwhile stay in the database and are
  ignored. No data is deleted.

## 36. Assignment quotas, migration 024 and gated teacher registration (registration PR 7): shipped disabled

This release adds migration `024_assignment_creations.sql`, per-account limits on creating assignments, and the
teacher registration form with its activation behind a build gate that stays off
(`docs/reviews/EMAIL_REGISTRATION_PR7_SCOPE.md`; contract and results in `docs/reviews/EMAIL_REGISTRATION_PR7_STATUS.md`;
routes in `docs/AUTH_ROUTE_CONTRACT.md`, "Creation limits" and "Student registration").

**The teacher gate is not activated, and this build cannot activate it in production.** Nothing in this section has been carried out
on any server, and no activation prerequisite of the design (0.3) is met. **The quota limits are different from the registration
gates: they are not behind a gate.** They apply as soon as this build runs, to every teacher account that exists, whether the
operator created it or not (below).

### What the build does

- **Limits, per account (not per person).** At most **20 assignment creations in a rolling 24 hours** and at most **10 open
  assignments**. New tests (both kinds) and copies of a test all count; the decision is made inside `quiz_create_assignment`, in the same
  writer transaction as the insert, with the time read after the lock is held. Closing a test, or a deadline that passes, frees an
  open slot. **Neither closing nor deleting a test returns a creation to the allowance.** A person with several addresses has several
  allowances; nothing here limits a person.
- **Refusals** answer the form again (status 429 for the daily limit, 409 for the open limit, 503 when the account's history cannot be placed; the daily refusal comes first when both limits apply; a held but otherwise open assignment counts as open, and a passed deadline frees a slot) with a Bulgarian message (its wording is not approved), and write nothing:
  no assignment, token, ledger row, clock mark or vault note.
- **Who is limited.** Accounts of role `teacher`. **Accounts of role `admin` are recorded in the ledger and never refused** (approved by Gabriel;
  `EMAIL_REGISTRATION_PR7_SCOPE.md`, I6). The role is read from the
  `users` row under the lock, so an operator's `set-role` takes effect at the next creation. An assignment with no owner belongs to nobody.
- **Existing teachers and assignments.** Nothing is closed, hidden or edited. Migration 024 writes **no** ledger row. The first creation of each
  account records, under the writer lock and from the `created_at` they carry, every owned assignment that has no ledger row (below), so a creation
  made shortly before the deploy still counts in its window; every open assignment counts toward the 10. A teacher who already has 10 or more open assignments keeps them all and can manage, edit and close them; they cannot create or copy
  another until fewer than 10 are open. Expect this for any long-time teacher: tell them before the deploy.
- **The ledger is verified at every creation.** Before the limits are decided, the creating account's history is reconciled (below). If it cannot be
  placed the creation is refused with status 503 and nothing is written, until an operator corrects it.
- **There is no override.** No command raises or clears a limit, and none was added. A teacher at a limit closes tests or waits.
- **The teacher gate.** `TEACHER_ACTIVATION` in `web/services/registration.py` is `False`. While it is: `/register/teacher` is the generic 404
  (after the same-origin check), the chooser has no teacher entry, and a teacher registration that is pending is voided and scrubbed at its next
  resend or verification and never becomes a student. No setting and no environment variable overrides the constant in production, and
  **no development setting was added** for it (a local try-out edits the constant). With the student gate also off, nothing of registration is reachable.
- **Activation of a teacher** (a later, separately authorised release only): a correct code creates the `teacher` account, its profile and its
  one to three subject rows in one transaction, and a database without the `assignment_creations` table refuses to do it.

### Accounting recovery and timestamp rules

Code that does not know the ledger (a rollback or recovery build that kept migration 024 but not the quota admission) creates owned assignments with
no ledger row and no limit. Returning to this code does not forgive them: the next creation by the owning account records them, in the same writer
transaction, by the age their `created_at` shows, and then decides. **What is recovered:** every surviving assignment row. **What is not:** a row that was
deleted before any admission recorded it; nothing can reconstruct it.

**Limits of recovery, stated plainly.** (1) Assignment rows deleted before they were recorded **cannot be reconciled**: the ledger and the table both lack them. (2) A build that
creates assignments without this admission, running at the same time as or between PR 7 builds, is **prohibited**: it enforces nothing while it runs, and recovery afterwards
only counts the surviving rows. (3) After **known history loss** (rows deleted before they were recorded, a restore to an older snapshot, a replaced database) this build cannot
see the gap and **does not hold creation**. **Creation stays closed until a separately reviewed recovery procedure exists** (approved by Gabriel as an operational requirement; it is not a claim that
an automatic hold or recovery mechanism exists, and no such procedure is written yet). Keep the creation block of "Rollback" in place meanwhile; nothing in the application enforces that. None of these limits is a quota exception: they are the
stated boundary of what the code can know.

Interpretation of `quiz_assignments.created_at` (the only writer was `CURRENT_TIMESTAMP`, UTC): the canonical form; ISO with `T`; fractional seconds of
any number of digits (see **precision** below); `Z` or a numeric offset up to 14 h (converted to UTC before anything else is decided); **no offset means UTC**;
**a date alone means 23:59:59 UTC of that day**. Anything else is *unplaceable*:
NULL, empty or padded text, a number, another layout, an impossible date or offset, a value before 2000-01-01 (the epoch included) and a value more than a day
after the server's writer clock. A value up to a day ahead is kept as it is and counts until it is a day old (conservative, and bounded by that day).

**ASCII digits only.** Every digit must be `0`–`9`; digits of other scripts (Arabic-Indic, fullwidth, Devanagari and the like, which Python's `\d` and `int()` would otherwise read) are unplaceable like any
other unusable value. This cleanup was committed as `5f834ae`, which was independently reviewed (APPROVE WITH NON-BLOCKING NOTES).

**Precision.** The ledger counts whole seconds, but the instant in `created_at` is judged exactly, to the last digit given: a fraction is never truncated and never
limited to six digits. A **nonzero** fraction is rounded **up** to the next whole second (`…09:00:00.999999999Z` is recorded as `…09:00:01`, so it stays in the window
until 24 hours after that second); a zero fraction (`.000`, however many zeros) is the second itself. Rounding up can keep an allowance spent a moment longer than the
exact instant would; it can never refund one early. The lower bound (2000-01-01) and the future bound (writer clock plus one day) are checked on the exact instant,
before any rounding: exactly the bound is accepted, any positive excess, however small, is unplaceable. The date-only mapping, the 2000-01-01 cutoff, the one-day tolerance
and the round-up are approved by Gabriel; the possible sub-second difference of the window that whole-second accounting causes is accepted. The 14-hour offset limit and the
reading of a time without an offset as UTC are not separately approved.

**Operator handling of a refused account (status 503, log line `needs review` at start-up):**

1. Do not edit the live file in place. Take a snapshot (section 16) and inspect its `questions.db` with `immutable=1`:
   `SELECT a.id, a.owner_user_id, a.created_at FROM quiz_assignments a LEFT JOIN assignment_creations c ON c.assignment_id = a.id WHERE a.owner_user_id IS NOT NULL AND c.id IS NULL;`
   lists the unrecorded assignments; the unplaceable ones are those whose `created_at` is outside the list above. A ledger row whose owner differs from its assignment's:
   `SELECT c.assignment_id FROM assignment_creations c JOIN quiz_assignments a ON a.id = c.assignment_id WHERE a.owner_user_id IS NOT c.owner_user_id;`
2. Decide each value from evidence (the assignment's attempts' `started_at`, the vault note named for its date, the snapshot history). A guess that is too old refunds
   allowance, so when in doubt choose the later time.
3. Correct it in one reviewed statement per row, in a maintenance window under the application's write lock (stop the service or use the restore gate's discipline of
   section 17), e.g. `UPDATE quiz_assignments SET created_at = '2026-03-01 09:00:00' WHERE id = 123;`. No command was added. The next creation by that account is admitted
   on its whole history.

The start-up notice (`assignment creation history needs review: N unrecorded assignment(s) with an unusable created_at and M inconsistent ledger row(s)`) is logged on the lifecycle
logger; it names counts only and never prevents a start, and it is **not** what protects the limits: the admission is.

### Deployment order

The standard procedure of section 20, with nothing added to it.

- Migration 024 is additive (one new table, one index, no row written) and is not a table rebuild.
  `quiz_assignments`, `users` and every other table are untouched. Nobody is signed out.
- On a database that already has every migration through `023_profile_required.sql` the dry run of step 7 lists exactly
  `024_assignment_creations.sql`. If it lists earlier migrations too, the database is behind that level.
- The migration writes no rows, so a database just migrated has an empty ledger; the first creation of each account records its history.
- Apply in step 8, after the step 3 snapshot. From this release on the application refuses to start on a schema without migration 024
  (section 9), and the creation of an assignment needs the table.
- Nothing is configured. Do not look for a setting: there is none.

Check afterwards, read-only, **on a fresh snapshot** (section 16) and not on the live file: a plain open of a WAL database can write its
sidecars, and `immutable=1` on the live file can miss frames still in the WAL. `immutable=1` on the snapshot's copy is safe, because nothing writes it:

```bash
SNAP=/var/backups/learnpilot/snapshot-<stamp>/questions.db   # a snapshot taken after the deploy
sqlite3 "file:$SNAP?immutable=1" "SELECT COUNT(*) FROM assignment_creations;"        # 0 until accounts create again
sqlite3 "file:$SNAP?immutable=1" \
  "SELECT owner_user_id, COUNT(*) AS open_now FROM quiz_assignments WHERE owner_user_id IS NOT NULL AND closed_at IS NULL \
   AND expiry_observed_at IS NULL AND submissions_closed_at IS NULL GROUP BY owner_user_id HAVING COUNT(*) > 10;"
curl -s -o /dev/null -w '%{http_code}\n' https://learnpilot.bg/register/teacher   # 404
```

The second query lists teachers who start above the open limit (their count ignores deadlines that passed but were not yet observed). The start-up log
shows the notice above if any assignment has an unusable `created_at`; fix those before teachers return to creating.

### Backup and restore

The ledger is part of the live database, so the snapshot of section 16 and the restore of section 17 carry it, and the restore script is not
changed. **A restore returns the ledger to the snapshot's moment**; assignments created after the snapshot are lost with it, and the ledger rows with them.
A snapshot from before this release is migrated by the restore script to an empty ledger, and its assignments are recorded at the next creation of
their owners, exactly as after the deploy. `assignment_creations` is runtime state and is never exported to `data/canonical`.

### Retention

The ledger holds an account id, an assignment number and a time: no name, email or text. It is kept as long as the account (accounts are
disabled, never deleted); Gabriel approved keeping the creation history for the account's lifetime, **subject to the unresolved privacy and retention activation prerequisite (D16)**: its purpose,
visibility and period are not settled, and nothing here is a legal conclusion. **The assignment retention cleanup of `docs/ASSIGNMENT_LIFECYCLE_DESIGN.md`, when it is written, must not touch
`assignment_creations`**: the table has no foreign key to `quiz_assignments` precisely so that deleting an assignment cannot cascade into it, and a test
pins that nothing but the quota module writes it.

### Rollback

**Forward fix first. A rollback or recovery build must keep the quota admission.**

- Once migration 024 is applied, every build that serves this database ships `024_assignment_creations.sql` (section 21) **and
  `web/services/assignment_quota.py` with its call in `quiz_create_assignment`**. If code has to be reverted, revert around them: keep the
  migration with its entry in the migration list and level of `data/canonical/manifest.json`, the table, the module and the call.
- **Do not run a build that creates assignments without the admission against a 024 database.** Such a build records nothing and enforces nothing, so a
  teacher could create without limit. This build cannot prevent it; the next PR 7 admission recovers the surviving rows (above) but cannot undo the
  assignments themselves. If an older build is unavoidable, block the creation routes first (Nginx: return 503 for `POST /teacher/new` and
  `POST /teacher/assignment/*/duplicate`) and lift the block only when this build is back.
- A snapshot restore is not a route to a build from before 024. Do not use `--replaced-database-unreadable` to get past that refusal.
- To lift the limits without a revert there is no switch. This is a code change and a deploy.

## 37. Password recovery storage and restore credentials

Scope 8.1 (merged, PR #343): migration `025_password_recoveries.sql` and its operations.
Scope 8.2 (merged, PR #344, at `4f88f7c`) added `web/services/recovery.py`, which can
create recovery rows. Scope 8.3 (authorized, implemented for independent review; section 38)
adds the routes that are its one production caller, behind the build gate
`recovery.PRODUCTION_ACTIVATION`, which is off. Rows the service creates are handled by the
operations below exactly as fixture rows are. No deployment or activation is authorized. All
production gates remain OFF. Operator password recovery remains
`manage_accounts set-password`; no account/TOTP API changes in these scopes.

025 is additive: an empty runtime table, nullable `registration_sends.recovery_id`
with an at-most-one-purpose CHECK, and two indexes. Legacy ledger inserts with
both purpose ids NULL remain supported. Existing rows and canonical JSONL are
unchanged. A rollback/recovery build serving a 025 database must keep the
migration, cleanup/gate/status integration, fail-closed scrub and settlement
guard. A build without that migration refuses the migrated database.

### Existing registration operations now include recovery

Section 34's commands and cleanup timer are unchanged entry points:

- `registrations-cleanup` expires and logically scrubs recovery rows at their
  30-minute expiry (also implausibly future-created rows); deletes final rows
  24 hours after creation; registration's existing ledger deletion covers both
  purposes at 48 hours. The timer's 15-minute interval gives an approximately
  45-minute logical target on a quiet site. It needs no secret. Both scrubs run
  in one writer transaction with the same clock, after restore-lock checks;
  a clock behind the shared mark changes nothing. Holds do not block cleanup.
- `registrations-cleanup --check` also fails for a recovery pending more than
  one hour. `registrations-restore-gate` voids/scrubs all pending recoveries
  alongside registration, atomically; an existing hold and high-water mark
  stay intact. `registrations-restore-status --expect held` also requires zero
  pending recoveries. The restore script and its ownership/lifetime locks are
  unchanged; its existing calls perform these checks.
- `registrations-release-hold --acknowledge-lost-history` releases the shared
  accounting hold only. It repairs no account. The address may receive eight
  further registration or recovery codes; repeated exceptions accumulate.
  `clear-registration-limit --email` (still requires `DZI_SECRET_KEY`) deletes
  both purposes' address history and stops codes whose issuance it removed.
  `registrations-clock-accept` remains the only way to lower the shared mark.
- `src.backup_sqlite` scrubs the isolated copy before final `quick_check`,
  fsync, publication and hashing, reporting `recoveries_scrubbed`. It never
  scrubs the live source. Historical absence is accepted only when 025 is not
  recorded; missing recorded storage, missing columns and unknown columns
  fail closed. Nothing is published on a scrub failure. A future migration
  adding recovery columns must update the allow-list and scrub together.

These are **logical-only** retention and scrub guarantees. Free pages, unused
page space, WAL, earlier snapshots and copies below the application can retain
old bytes. Published snapshots are never rewritten. No physical-remnant bound
or erasure claim is made. Before promoting a formerly recovered/demoted account
back to admin, consider a TOTP reset and new enrolment: a teacher's password
sign-in never rechecks leftover TOTP state.

### Mandatory restore isolation (RD18)

The unchanged restore script **starts the service before it returns**.
Service start is distinct from reopening to users. Old password hashes,
session keys, role/active state and TOTP state return with a snapshot. The
registration hold does not block sign-in and a sign-out does not replace an
old password.

**Verified maintenance isolation is mandatory**, from before the restore
until credential repair and validation finish. **No fallback** to an exposed
window exists. If any condition cannot be established, the restore is
**postponed**: do not run the restore script. If isolation fails after service
start, keep the block and postpone public reopening, as described below.
These are instructions for a separately authorized operator rehearsal/restore;
this scope operates no host service, tunnel, forwarder, firewall or Nginx.

Use an external replacement maintenance site, **server-level** `return 503;`,
with no `proxy_pass` anywhere. Do not add a return location to the normal site:
its exact/regex locations would still proxy. Cover all application server names
and all public listeners: HTTP/HTTPS, ports 80/443, IPv4/IPv6; retain the TLS
certificate. This form also blocks ACME challenges, including locations added
by Certbot. Check `certbot certificates` first: if a certificate is inside its
renewal window, renew before establishing the block. Renewal is unavailable
during the block. Return a fixed 503 body without links or subresources.

Define `log_format lp_no_args` in the maintenance file's HTTP context, and use
`access_log /var/log/nginx/access.log lp_no_args;` in every maintenance server.
Use the argument-free fields of the normal template, including `$uri`, status
and upstream address/status; never log `$request`, `$request_uri`, `$args` or
`$query_string`. The server-level setting must cover the OAuth callback too;
the normal site's callback location no longer exists in this replacement.

After the operator's `nginx -t` and reload, verify all of these before restore,
again after service start, and immediately before reopening:

| Evidence (read-only inspection) | Required condition |
| --- | --- |
| Every effective Nginx server/listener; `ps` workers | Every ingress returns 503; no old worker remains, including "worker process is shutting down". Existing requests/keep-alives under the previous configuration have drained. |
| Outside requests to `/`, `/login`, `/healthz`, `/recover`, `/auth/google/callback?code=x&state=y` | 503 from Nginx, argument-free log with `up=-`; nothing reaches the application. |
| `ss -ltnp`, `ss -lunp` | Backend only on `127.0.0.1:8000`; no second public proxy/forwarder. Listener inspection is necessary, **not sufficient**: outbound tunnels and NAT need no listener. |
| `ss -tnp state established '( dport = :8000 or sport = :8000 )'` | Every established peer accounted for: only the transient direct health check/operator requests. Also inspect after the script stops the old service. |
| `ps -eo pid,user,args`; `systemctl list-units --type=service --state=running` | No reachable tunnel or forwarder: cloudflared, ngrok, socat, autossh, Tailscale serve/funnel, or another proxy. |
| SSH clients with `-R`, `-L`, `-D`; backend peers; `sshd -T` | No SSH forwarding path. Reverse SSH listens on the remote host; inbound forwarding can appear as an `sshd` backend connection. Account for outbound SSH, not just local listeners. |
| `nft list ruleset`; `iptables-save -t nat`; `ip6tables-save -t nat`; `sysctl net.ipv4.conf.all.route_localnet` | No DNAT/REDIRECT/forward to loopback or port 8000; `route_localnet=0`. |
| Host shell access | Local users/processes can still reach loopback; every such user must be trusted. |
| `LEARNPILOT_HEALTH_URL` | Unset (default `http://127.0.0.1:8000/healthz`) or another direct loopback URL. Keep the script's direct health check; a public URL receives 503 and makes the restore fail. |

These inspections do not authorize stopping/reconfiguring discovered services,
tunnels or firewalls. A failed condition postpones the restore until the host's
operator resolves it and verifies every condition again.

Before reopening, read `journalctl -u learnpilot --since "<start>" --no-pager`,
where `<start>` comes from `systemctl show learnpilot -p ActiveEnterTimestamp`.
Gunicorn's argument-free access log must contain only the script's health check
and accounted-for operator requests. An unexpected forwarded address means
Nginx proxied a request; an unexpected `-` client means a direct loopback
client. The log detects access after it happened and prevents nothing. An
**absence of lines is not proof** of isolation (timeouts, dropped/rate-limited
logs, and forged forwarded headers). Isolation rests on the verified conditions.

**Isolation failure:** keep the maintenance block; reopening is postponed.
Have the operator resolve the cause, verify all conditions again, stop account
activity, take a new pre-repair snapshot/reference time and repeat repair and
validation for every potentially reached account, or all accounts if the reach
cannot be bounded. Changes made after service start are absent from the two
comparison inputs; repair may override them. This is never an accepted window.

### Prepare stable offline evidence and compare

Order (design 6.6, steps 3–7): run the restore script unchanged under the
verified block; then **stop account activity and recheck isolation** (every
condition in the table above, the backend's established peers above all, and
the Gunicorn access log since the service start). Anything unaccounted for is
an isolation failure: keep the block and follow that procedure. Only then
prepare and compare the evidence below, take the pre-repair snapshot and
capture `T_REF`, and repair.

Use the original published `<snapshot>/questions.db`, not the staged/migrated
copy and not the live restored database. Prepare the replaced input from
`$STATE/replaced-<stamp>/questions.db` without opening that original in SQLite:

1. Establish **quiescence**: its device/inode differs from live `--db`; `fuser`
   or `lsof` shows no process with it open; no rollback journal; WAL absent or
   empty. Record device, inode, size, mtime, ctime and SHA-256, plus sidecars.
2. In a new operator-only directory (`install -d -m 0700`), byte-copy only the
   main file with `install -m 0400`. Do not copy a nonempty WAL away from its
   database or ignore it. Unexplained WAL activity makes evidence unavailable.
3. Recheck quiescence, identity, metadata, hash and sidecars on the original;
   the byte copy's hash must equal the original's. Any difference stops this
   preparation. Same before/after hashes alone are not proof of stable input:
   the original must be unopened and the operator directory must exclude writers.
4. Convert the isolated byte copy via the online backup API:
   `python3 -B -m src.backup_sqlite --db <byte-copy> --output <operator-dir>/replaced.db`.
   This includes committed WAL pages through SQLite, sets DELETE journal mode,
   validates and prints SHA-256. SQLite may create sidecars beside the byte copy;
   it never opens the kept original. Use the published `replaced.db`, not the
   byte copy, for comparison. Scrubs write no compared account/TOTP column.

Run (no secret needed):

```sh
python3 -B -m src.manage_accounts --db "$DZI_DB" restore-credential-diff \
  --snapshot <original-snapshot>/questions.db --replaced <operator-dir>/replaced.db \
  --snapshot-sha256 <published-hash> --replaced-sha256 <prepared-hash>
```

Expected hashes are optional, recommended. Live `--db`/`DZI_DB` is required and
examined only by stat, never opened. Inputs must be regular files, not symlinks
or hard links to live, and two different files (not one path or hard link given
twice), with the complete 100-byte SQLite header, read/write
versions 1/1, no `-journal`, `-wal` (even empty) or `-shm`. WAL headers without
sidecars are also refused. The command brackets immutable read-only SQLite
reads with identity, size, mtime, ctime, header, sidecar and hash checks; any
lasting change refuses evidence. A change made and undone between checks is
not excluded by these checks: preparation and isolation remain obligations.

Historical schemas are read at their own levels (users since 012, TOTP since
016); extra columns are ignored. Absence is accepted only when migration
bookkeeping exists and does not record that table's migration. Missing recorded
storage, missing required credential columns or bad quick_check is unavailable
evidence. Migration 016 rotates admin session keys in the restored copy, so a
pre-016 snapshot can conservatively report sessions revived for an admin even
though the migration already ended them. Later migrations that write compared
columns must document their effect here.

Nothing is classified from a refused input. If replaced evidence is
unavailable or refused (a missing path supplied to `--replaced`, a refused
pre-check, a change during the read, an expected-hash mismatch, an unsupported
schema, or a replaced input that is the same file as the snapshot, hard links
included), every snapshot account is **uncertain (no evidence)** with its P/N
repair, and no comparison class is reported. If snapshot evidence is
unavailable, no accounts are classified: treat every live account as uncertain, using the pre-repair snapshot
to determine whether it has a password. Any repair finding or unavailable
evidence exits nonzero. Zero means only "unchanged as far as compared" or
accounts created after the snapshot, never proof that credentials are safe.

### Repair categories and validation, before reopening

Keep isolation verified. An optional pre-restore comparison to a fresh live
backup is only a preview; it misses changes before service stop. Run the restore
script unchanged under the block, stop account activity and recheck isolation
(connections and the access log since the service start), and only then
prepare/compare the final evidence above. Cleanup/backup timers do not write
accounts. With activity still stopped, take the pre-repair snapshot with the
WAL-safe tool:

```sh
python3 -B -m src.backup_sqlite --db "$DZI_DB" --output "$DIR/pre-repair.db"
T_REF=$(date -u '+%Y-%m-%d %H:%M:%S')
```

`DIR` is the operator-only directory. Capture `T_REF` **after activity stopped**
and immediately before the first repair, never at service start. Review all
accounts/categories/findings. Commands below are subcommands of
`python3 -B -m src.manage_accounts --db "$DZI_DB"`. Passwords are prompted,
never put in argv, environment, files or logs. The comparison performs no repairs.

| Ordered class | Category and repair |
| --- | --- |
| Unavailable replaced evidence; snapshot-only id; id with changed email | Uncertain: P if snapshot has a password, otherwise N. Also reset any snapshot TOTP state. |
| Equal hashes with unequal timestamps; unequal hashes with NULL hash or unreliable timestamps | Uncertain password evidence: P/N by snapshot password. Reliable means both exact SQLite UTC timestamps and replaced strictly later. Equal, malformed, NULL or regressed timestamps are not reliable. |
| Unequal non-NULL hashes with reliable timestamps | Password reverted: P. |
| Equal hashes/timestamps, different session key | Sessions revived: K. |
| Id only in replaced; equal compared credentials | Created after snapshot (absent after restore); unchanged as far as compared. No class repair. Findings below still apply to accounts in both. |

| Category/finding | Feasible commands |
| --- | --- |
| P: restored password | `set-password --email <snapshot-address>` with a new reviewed password, **or** `disable --id <id>`. Never sign-out alone. If replaced had no password, disable is closest: there is no CLI password-removal command. |
| N: no restored password | `sign-out --id <id>` or `disable --id <id>`. Never `set-password`: it would grant a password the restored account does not have. |
| K: key only | `sign-out --id <id>`. |
| State differs (every account in both, irrespective of class) | Reapply replaced state with `disable --id`, `enable --id`, `set-role --id <id> --role <role>`. A passwordless restored account cannot become admin: disable instead. These commands rotate the key. |
| Second factor differs (presence, ciphertext, confirmation, recovery-code set or used_at); uncertain identity/no evidence with snapshot TOTP | `totp-reset --email <snapshot-address>` and new enrolment before next admin sign-in. Enrol/confirm require admin; demoted accounts first clear leftover state, then enrol only if later promoted. No secret is printed by the comparison. |

Hand a replacement password to the verified owner out of band, or discard it.
Scope 8.1 has **no email recovery**, so do not promise it. Even after future
activation, legacy/CLI accounts, never-verified accounts and admins are ineligible;
email recovery would also wait for hold release. Admins need new TOTP enrolment.

Every repair command must exit 0 with its confirmation. Then take a post-repair
snapshot, never query the live WAL database directly:

```sh
python3 -B -m src.backup_sqlite --db "$DZI_DB" --output "$DIR/post-repair.db"
sqlite3 -readonly "file:$DIR/post-repair.db?immutable=1" "
  ATTACH 'file:$DIR/pre-repair.db?immutable=1' AS pre;
  SELECT u.id, u.email, u.role, u.is_active,
         u.password_hash IS NOT NULL AS has_password,
         u.password_changed_at,
         u.password_changed_at >= '$T_REF' AS stamped_since_ref,
         u.password_hash IS NOT pre_u.password_hash AS hash_changed,
         u.session_key IS NOT pre_u.session_key AS key_rotated
    FROM users u JOIN pre.users pre_u ON pre_u.id=u.id ORDER BY u.id;"
```

Use only prepared rollback-mode snapshots without sidecars; keep DIR/T_REF
operator-controlled, without SQL quote characters. The query prints flags, not
hashes or keys. `manage_accounts list` does not expose these validation fields.

- P replacement: has_password, stamped_since_ref, hash_changed, key_rotated all
  1. `>=` permits same-second repairs: timestamps have whole-second precision.
  A timestamp is not proof the intended password landed; salted hash changes
  show a new write, not a different password. Confirm the command and absence
  of other writers; use the **recipient's first sign-in** after reopening to
  verify a handed-over password, repeating set-password if it fails. A discarded
  replacement needs the old credential invalidated, not that sign-in proof.
- Disable: active 0, key rotated. Sign-out: key rotated, hash unchanged.
  State repair: reviewed role/active match replaced (or the documented disable
  fallback), key rotated. TOTP reset: no rows for that id in either TOTP table
  of the post-repair snapshot, key rotated; enrol again before admin sign-in.
- Every unrepaired account: hash/key unchanged, role/active as before. Check
  account membership as well; no account creation/deletion is part of repair.

Only after validation for every account, rechecking every isolation condition
and reading the service-start access log again, may the operator restore the
normal Nginx site, test/reload it and verify `/login` is served normally. If a
condition fails, use the isolation-failure procedure and postpone reopening.
Finally release the shared registration hold when lost accounting is accepted.
Credential repair is separate from `registrations-release-hold`; that command
does not repair old passwords, sessions or TOTP.

Host rehearsal, real Nginx acceptance, Linux timer behavior, provider delivery,
capacity, privacy/wording decisions and independent security review remain
outstanding. Portable tests do not establish host isolation or authorize rollout.

## 38. Password recovery routes (scope 8.3): shipped disabled

This release adds the recovery routes (`/recover`, `/recover/verify`, `/recover/resend`,
`/recover/cancel`), their three pages, the „Забравена парола?“ link and the `recovery=done`
notice on `/login`, the settings module `web/recovery_settings.py`, the `learnpilot.recovery` log
handler and the Nginx `/recover` locations (`docs/reviews/PASSWORD_RECOVERY_DESIGN.md`, sections 2,
7.1, 7.5 and 8.1; contract in `docs/reviews/PASSWORD_RECOVERY_8_3_STATUS.md`; routes in
`docs/AUTH_ROUTE_CONTRACT.md`, "Password recovery").

**Recovery is not activated, and this build cannot activate it in production.** Nothing in this
section has been carried out on any server: no provider, sender address or credential exists, no
message was sent, no Nginx accepted the template, and none of the checks below was run. Section 37
(storage, cleanup, the restore gate, the credential runbook and the mandatory maintenance isolation)
applies unchanged and must be rehearsed before any activation.

### What the build does

- **No migration.** It needs migration 025 (section 37), which a database must have before this
  build serves it.
- **The activation gate.** `PRODUCTION_ACTIVATION` in `web/services/recovery.py` is `False`. It is
  independent of `registration.PRODUCTION_ACTIVATION`, `TEACHER_ACTIVATION` and
  `profiles.PRODUCTION_ACTIVATION`. While it is off, `web.wsgi`:
  - starts normally whatever the `DZI_MAIL_*` and `DZI_SMTP_*` settings hold (unset, partial,
    malformed, or a `console`/`file` backend): mail settings are never a reason to refuse to start;
  - keeps recovery disabled: every `/recover...` route answers the generic 404 page (a cross-origin POST
    is still 403), `/login` has no „Забравена парола?“ link and does not show the `recovery=done` notice,
    no row is created and no message is sent;
  - logs one warning if any mail setting is set, naming the settings and never their values: `Mail
    settings are configured (...), but this build does not enable password recovery.` (Registration logs
    its own, separate warning.)

  No setting and no environment variable overrides the constant, and none is read for it; a configured
  backend never enables recovery by itself. **A later, separately authorized release** sets it to `True`
  only after the prerequisites of design 9 and section 37; from then on a configured backend must be usable
  for production (`smtp` only: `console` and `file` stop the start, as does any malformed setting, named
  and never echoed) and no backend means disabled. Registration's mail problems and recovery's are the same
  settings and are reported once while either gate is on.
- **Settings.** `RecoverySettings` (the mailer and the hourly total budget, `DZI_MAIL_HOURLY_CAP`) under
  `app.config["RECOVERY"]`, built from the same `mail.load_settings` and `mail.build_mailer` as
  registration's. A development run (`web.app`) enables recovery as it enables registration (usable
  development mail settings, `DZI_MAIL_BACKEND=console` shows the code in the terminal), and warns by setting
  name when they are unusable: `Password recovery is disabled: ...`.
- **One budget.** A recovery issuance is a row of the same send ledger as a registration one, so the per-address
  caps (5 an hour, 8 in 24 h 15 min), the total budget (300 an hour, or `DZI_MAIL_HOURLY_CAP`), the circuit, the
  high-water mark and the restore hold are shared with registration (RD5, RD6). A reset clears nothing of the
  sign-in throttle (RD8 (a)) and wrong recovery codes do not count in it (RD10).
- **Logs.** `learnpilot.recovery` gets a handler like the registration one (stderr, so the journal). Lines are
  an event name with row ids, an outcome or an exception class name; never an address, a password, a code, the
  binding token, any hash, key or digest, an issuance identity, a message, or whether a row is a shadow.
  `user=` appears on `redeemed` only. Lines named `*_decided` are written by the service inside a transaction before
  its commit and are not evidence that anything became durable; `requested`, `resent`, `wrong_code`, `redeemed`,
  `invalidated`, `expired`, `cancelled`, `unavailable` and `send_settled` are written by the route only after the
  commit returned, and `request_failed`, `resend_failed`, `cancel_failed`, `redeem_failed`, `dispatch_failed` and
  `settlement_failed` name an exception class.

### Deploying this build

The standard procedure of section 20, with no migration step. **Do not add mail settings to
`/etc/learnpilot/learnpilot.env` for this release:** they would only produce the warnings above. Then check
(these send nothing and write nothing):

```bash
curl -s https://learnpilot.bg/login | grep -c '/recover'                          # 0: no link
curl -s -o /dev/null -w '%{http_code}\n' https://learnpilot.bg/recover            # 404
curl -s -o /dev/null -w '%{http_code}\n' -X POST \
  -H 'Origin: https://learnpilot.bg' https://learnpilot.bg/recover/verify         # 404
curl -sI https://learnpilot.bg/recover | grep -ci '^referrer-policy: no-referrer'  # 1
```

Install the new Nginx template as in section 12 (keep the lines Certbot added to the server block), then
`sudo nginx -t && sudo systemctl reload nginx`. The template adds four locations, as under `/register` (section 33):

| Location | Limit | Why |
| --- | --- | --- |
| `= /recover` | `lp_register`, `burst=90 nodelay` | A POST sends mail; the zone keys on POSTs only, so the form (a GET) is not limited |
| `= /recover/resend` | `lp_register`, `burst=90 nodelay` | Sends mail |
| `= /recover/verify` | `lp_login`, `burst=40 nodelay` | A guess at a credential: the sign-in budget |
| `/recover/` (prefix) | none | `/recover/cancel` (a POST that sends nothing) and any unknown path under it, which the application answers 404 |

Every address under `/recover` is answered with exactly one `Referrer-Policy: no-referrer`: each location repeats the
server-level `X-Content-Type-Options` and `X-Frame-Options` (`add_header` in a location replaces the inherited ones) and
hides the application's copy of the policy. `/recovery` and `/recover-x` are not recovery routes and fall to `location /`. The
values are registration's proposals, to be tuned on a classroom-sized run that mixes both purposes before activation. **The
template does not contain the maintenance configuration of section 37**: that block is an operator step on the host, built and
verified for a restore, never part of the repository's template. A template change is not evidence that the server's Nginx
accepts it: `nginx -t` on the server is, and no `nginx` binary was available when this was written (the checks in
`tests/test_recovery_production.py` are static).

### Rollback

**There is no behaviour to revert while the gate is off.** Reverting the code removes routes that answer 404, one log handler
and four Nginx locations. The table of migration 025 stays, and every build that serves the database keeps shipping the
migration and its cleanup, restore and snapshot integration (section 37). If recovery was ever activated, pending rows that
a rollback leaves behind are unreachable and are scrubbed by the next cleanup after they expire; nothing redeems them.

### Still outstanding before any activation

The Linux, systemd and Nginx evidence of design 9 (including the real `nginx -t` and one `Referrer-Policy` observed on each
`/recover` address, a 404 and a 429 included), the restore credential runbook rehearsed with the required maintenance isolation,
provider delivery and the timing of a recovery code and a notice (no equal timing is claimed), the capacity of `set_password`
under the writer lock, RD15 (the exact Bulgarian wording, still proposed), D16 extended to recovery (including the neutral
notices to typed addresses), the configuration of D17 and an independent security review of every scope.

## 2 GB VPS sizing

Initial configuration from capacity testing: Gunicorn `workers = 1`,
`worker_class = "sync"`, `threads = 1`, `timeout = 30`.

**One worker is a validated requirement, not just a default.** Do not raise
`workers` merely because the VPS has 2 GB of RAM. The practical-upload quota is
safe across processes (it uses SQLite's write lock), but `quiz_practical_upload`
holds that lock (`BEGIN IMMEDIATE`) while it saves files to disk. With more than
one worker, a slow upload can keep another worker's write (for example a quiz
submission) waiting past SQLite's 5-second busy timeout, which fails that
request with "database is locked". Before increasing the worker count, move
slow file I/O outside the write transaction (or otherwise shorten the lock),
then re-run the concurrency tests on the real VPS.

- Threaded serving collapsed under concurrent requests because of GIL
  contention; one sync worker handles one request at a time and lets Nginx
  queue the rest.
- Two workers are not automatically better: on 1 vCPU, CPU-bound pages only
  time-slice, and each worker holds its own browsing cache. Re-measure before
  changing workers or threads.
- Actual CPU capacity must be verified on the real VPS (local measurements do
  not transfer); watch latency, `top` (including CPU steal) and `vmstat 5`.
- The 1 GB swap is emergency protection. Sustained swap use (`free -h`,
  non-zero `si`/`so` in `vmstat 5`) indicates capacity pressure: investigate or
  resize rather than relying on swap.

## Known performance limitations (deferred)

- **Public browsing cache invalidation**: the process cache of subject browsing
  results is dropped after every database or WAL write (quiz submissions
  included). Cold browsing paths can still execute roughly 11k-13k SQL
  statements.
- **`/teacher/new`** was measured at around 53k SQL statements per request.
- **Large DZI pages**: a representative DZI section produced about 3.18 MB of
  uncompressed HTML. Nginx gzip reduces transfer size, not render cost.

These belong to later optimization work.
