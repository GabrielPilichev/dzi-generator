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
| `DZI_ADMIN_PASSWORD` | yes | at least 12 characters, not a documented local value |
| `DZI_TESTER_PASSWORD` | yes | at least 12 characters, different from the admin password |
| `DZI_DB` | yes | absolute path of the existing live database, outside the checkout |
| `DZI_UPLOAD_ROOT` | yes | absolute, existing, writable directory outside the checkout |
| `DZI_VAULT` | yes | absolute, existing, writable directory outside the checkout |
| `DZI_REFERENCE_ROOT` | no | absolute, existing, readable directory outside the checkout holding the contents of `data/reference/`. Unset: the app looks in the checkout's `data/reference/`, which production does not have, so practical-task downloads show as unavailable |
| `DZI_PUBLIC_HOST` | yes | the real public DNS hostname in Nginx `server_name`, without scheme or port. It is exactly **one** trusted hostname: a future `www` alias must be deliberately supported in the application (and Certbot), not merely added to Nginx, or Flask answers it with 400 |
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
untouched. Repeat the pragma after restoring a database from a backup (section 17)
and for any database created some other way.

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

The runner's own automatic backup covers only the repository database, so
always back up the production database first. The development-only
request-time `ALTER TABLE` guard for teacher answer review is disabled in
production as well, so no request path runs schema code.

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
error logs on stdout/stderr, no daemon mode, no reloader. Check configuration,
production settings and schema without binding a port:

```bash
sudo systemd-run --quiet --pipe --wait --uid=learnpilot --gid=learnpilot \
  -p EnvironmentFile=/etc/learnpilot/learnpilot.env -p WorkingDirectory=/opt/learnpilot/app \
  /opt/learnpilot/venv/bin/gunicorn --config deploy/gunicorn.conf.py --check-config
```

`systemd-run` reads the environment file exactly as the service unit does
(shell sourcing would interpret `$`, quotes and spaces differently).

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
practical uploads have 2 requests/second with burst 45. Empty GET keys are
not charged. These bursts allow at least 30 students behind one school NAT to
submit together; Flask ownership, file quotas and the disk floor still enforce
the application rules. Nginx responds 429 when a bucket is exhausted.

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
are `Secure`, so logging in works only over HTTPS. After HTTPS works, HSTS can
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
`Generated/`). The commands below assume the default paths from
`learnpilot.env.example`; if yours differ, extract into a scratch directory and
copy `uploads/practical` and `vault` to your configured paths.

Reference data is not part of a snapshot and a restore must not disturb it:
only the database files are moved out of `/var/lib/learnpilot/data`, so
`/var/lib/learnpilot/data/reference` stays in place. On a rebuilt server,
provision it again as in section 3.

Restore everything **while the service is stopped**, start it only at the end:

```bash
SNAP=/var/backups/learnpilot/snapshot-<stamp>
sudo systemctl stop learnpilot

# 1. Verify the snapshot before touching live state.
sudo -u learnpilot sqlite3 "$SNAP/questions.db" 'PRAGMA quick_check;'   # expect: ok
sudo tar -tzf "$SNAP/files.tar.gz" | head                            # expect uploads/practical/... and vault/...

# 2. Move the current state aside, together (database + sidecars, uploads, vault).
#    Not the data directory itself: data/reference stays where it is.
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
sudo install -d -o learnpilot -g learnpilot -m 0700 /var/lib/learnpilot/replaced-$STAMP
sudo sh -c "mv /var/lib/learnpilot/data/questions.db* /var/lib/learnpilot/uploads \
  /var/lib/learnpilot/vault /var/lib/learnpilot/replaced-$STAMP/"

# 3. Restore the database, then the files, as the service user.
sudo install -o learnpilot -g learnpilot -m 0600 "$SNAP/questions.db" /var/lib/learnpilot/data/questions.db
sudo -u learnpilot sqlite3 /var/lib/learnpilot/data/questions.db 'PRAGMA journal_mode=WAL;'
sudo install -d -o learnpilot -g learnpilot -m 0750 /var/lib/learnpilot/uploads /var/lib/learnpilot/vault
sudo -u learnpilot tar -C /var/lib/learnpilot -xzf "$SNAP/files.tar.gz"

# 4. Check the schema, then start and confirm.
cd /opt/learnpilot/app && sudo -u learnpilot /opt/learnpilot/venv/bin/python -m src.manage_migrations \
  --db /var/lib/learnpilot/data/questions.db --dry-run
sudo systemctl start learnpilot && curl -fsS http://127.0.0.1:8000/healthz
```

Everything moved aside stays in `replaced-<stamp>/` until you delete it
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

`GET /healthz` returns `200 ok` after opening the database read-only and
reading its header (one cheap query), or `503 unavailable` without details.
It renders no templates, sets no cookie, runs no content queries and is not
cached. It never creates a missing database.

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
only the repository database, not production.

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

**12. Smoke-test** over HTTPS: the homepage, a subject page, tester login and
test creation; check `journalctl -u learnpilot --since "10 minutes ago" -p warning`.
With one worker, a restart causes a few seconds of unavailability.

**13. Rollback** if a step fails: see section 21, using `$PREV` from step 2 and
the step 3 snapshot.

## 21. Rollback considerations

- Code: `cd /opt/learnpilot/app && sudo git checkout <previous-commit>` (the
  commit recorded in step 2 of section 20), reinstall requirements into
  `/opt/learnpilot/venv`, restart, and verify `/healthz` with
  `-H 'Host: learnpilot.bg'`. Return to `main` afterwards (`git checkout main`)
  once the fix is ready.
- Migrations are forward-only. Startup blocks only *pending* migrations; older
  code usually runs on a newer, additively migrated schema. If a release's
  migration is not backward compatible, restore the pre-deploy snapshot
  (section 17), which discards database writes, uploads and vault changes made
  after it; decide that explicitly.
- If no migration was applied, a code rollback needs no data restore.
- Browsers may keep old CSS/JS for up to one hour (static cache lifetime).

## 22. Account management CLI

`src.manage_accounts` is operator tooling for the `users` table (migration 012).
It is a command reference only: the application does not use account
authentication yet, so this is not a rollout step.

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
```

- Accounts are addressed by (normalized) email. Roles: `student`, `teacher`, `admin`.
  This CLI is the only way to create or promote an admin.
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
- `list` prints id, email, name, role, active state, created and last login
  times; never password hashes or session keys.
- Errors go to stderr with a nonzero exit status; each command commits only after
  it fully succeeds.

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
