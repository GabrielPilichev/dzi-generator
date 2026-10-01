# Production deployment (Ubuntu 24.04, single small VPS)

```
Internet -> Nginx (80/443) -> Gunicorn 127.0.0.1:8000 (1 sync worker) -> Flask (web.wsgi:app) -> SQLite/WAL
```

Templates live in `deploy/`. Replace `learnpilot.example.com` with the real
domain only on the server; never commit domains, IP addresses or secrets.

| Path | Purpose | Owner / mode |
| --- | --- | --- |
| `/srv/learnpilot/app` | Git checkout (code, templates, static files) | `root`, world-readable; read-only to the service |
| `/srv/learnpilot/venv` | Python virtual environment | `root` |
| `/etc/learnpilot/learnpilot.env` | Secrets and state paths | `root:learnpilot` `0640` |
| `/var/lib/learnpilot/questions.db` | Live database (`DZI_DB`) | `learnpilot` `0600` |
| `/var/lib/learnpilot/uploads/practical` | Student uploads (`DZI_UPLOAD_ROOT`) | `learnpilot` `0750` |
| `/var/lib/learnpilot/vault` | Generated quiz notes (`DZI_VAULT`) | `learnpilot` `0750` |
| `/var/backups/learnpilot` | Database and file backups | `learnpilot` `0700` |

The repository tracks a seed `data/questions.db`. Production state must stay
outside the checkout so a `git pull` can never replace live data; startup
refuses state paths inside `/srv/learnpilot/app`.

## 1. Ubuntu prerequisites

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y git python3 python3-venv nginx sqlite3 certbot python3-certbot-nginx ufw
```

Add 1 GB of swap as emergency protection only (see "1 GB VPS sizing"):

```bash
sudo fallocate -l 1G /swapfile && sudo chmod 600 /swapfile
sudo mkswap /swapfile && sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
echo 'vm.swappiness=10' | sudo tee /etc/sysctl.d/99-learnpilot-swap.conf && sudo sysctl --system
```

## 2. Dedicated user and directories

```bash
sudo adduser --system --group --home /var/lib/learnpilot --no-create-home learnpilot
sudo install -d -o root -g root -m 0755 /srv/learnpilot
sudo install -d -o root -g learnpilot -m 0750 /etc/learnpilot
sudo install -d -o learnpilot -g learnpilot -m 0750 /var/lib/learnpilot \
  /var/lib/learnpilot/uploads /var/lib/learnpilot/uploads/practical /var/lib/learnpilot/vault
sudo install -d -o learnpilot -g learnpilot -m 0700 /var/backups/learnpilot
```

## 3. Repository deployment

```bash
sudo git clone <repository-url> /srv/learnpilot/app
cd /srv/learnpilot/app && sudo git checkout <release-commit-or-tag>
```

`data/reference/` (official DZI PDFs/ZIP resources for practical-task
downloads) is git-ignored. If those downloads are needed, copy it from a trusted
workstation into `/srv/learnpilot/app/data/reference/` (read-only, root-owned):

```bash
rsync -a data/reference/ <admin>@<server>:/tmp/learnpilot-reference/   # on the workstation
sudo rsync -a --chown=root:root /tmp/learnpilot-reference/ /srv/learnpilot/app/data/reference/
```

## 4. Python virtual environment

```bash
sudo python3 -m venv /srv/learnpilot/venv
sudo /srv/learnpilot/venv/bin/pip install --upgrade pip
```

## 5. Dependencies

```bash
sudo /srv/learnpilot/venv/bin/pip install -r /srv/learnpilot/app/requirements.txt
```

`requirements.txt` pins Flask, Werkzeug and Gunicorn (the production server).

## 6. Environment configuration

```bash
sudo install -o root -g learnpilot -m 0640 \
  /srv/learnpilot/app/deploy/learnpilot.env.example /etc/learnpilot/learnpilot.env
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
| `DZI_SOURCE_URL` | no | optional footer link |
| `DZI_SESSION_COOKIE_SECURE` | no | leave unset; any false value is rejected (HTTPS-only cookies) |
| `FLASK_DEBUG` | no | must not be enabled; startup rejects it |

`web.wsgi` validates all of this before importing the app and stops with a
message naming the problem settings (never their values). Development runs
(`python3 web/app.py`, tests, the tunnel runner) keep their existing defaults.

## 7. Writable paths and permissions

The systemd unit makes the whole filesystem read-only for the service
(`ProtectSystem=strict`) except `/var/lib/learnpilot`. The app writes only:

- `DZI_DB` and its `-wal`/`-shm` sidecars (the directory must be writable);
- uploads under `DZI_UPLOAD_ROOT` (files become `0640` via `UMask=0027`);
- quiz notes under `DZI_VAULT`;
- temporary upload spool files in the service's private `/tmp`.

Nginx (`www-data`) needs read access to `/srv/learnpilot/app/web/static/`
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
private `/tmp`, so keep about 250 MB of disk free. Files are stored under
token names and never extracted. No retention policy exists, so
nothing is deleted automatically. A failed upload request can leave saved
files without database rows; review `DZI_UPLOAD_ROOT` growth with
`sudo du -sh /var/lib/learnpilot/uploads` and `df -h`.

## 8. Database preparation (first deployment)

Create the live database from the tracked seed with the online backup tool
(safe for WAL databases; never `cp` a live SQLite file):

```bash
cd /srv/learnpilot/app
sudo /srv/learnpilot/venv/bin/python -m src.backup_sqlite \
  --db data/questions.db --output /var/lib/learnpilot/questions.db
sudo chown learnpilot:learnpilot /var/lib/learnpilot/questions.db
sudo -u learnpilot sqlite3 /var/lib/learnpilot/questions.db 'PRAGMA journal_mode=WAL;'
```

## 9. Schema / pre-start step

Production never changes schema on import: `web.wsgi` disables the
development-only quiz-table guard in `web.app`, and startup refuses to run
while any `web/migrations/*.sql` file is missing from `schema_migrations`.
Schema changes happen only through this explicit step, with the service
stopped or before restarting it:

```bash
cd /srv/learnpilot/app
sudo -u learnpilot /srv/learnpilot/venv/bin/python -m src.manage_migrations \
  --db /var/lib/learnpilot/questions.db --dry-run
# Only if migrations are pending: take a backup (section 16) first, then
sudo -u learnpilot /srv/learnpilot/venv/bin/python -m src.manage_migrations \
  --db /var/lib/learnpilot/questions.db --apply
```

The runner's own automatic backup covers only the repository database, so
always back up the production database first. The development-only
request-time `ALTER TABLE` guard for teacher answer review is disabled in
production as well, so no request path runs schema code.

## 10. Gunicorn

`deploy/gunicorn.conf.py`: bind `127.0.0.1:8000`, 1 `sync` worker, 1 thread,
30 s timeout, `preload_app` (configuration errors stop the master), access and
error logs on stdout/stderr, no daemon mode, no reloader. Check configuration,
production settings and schema without binding a port:

```bash
sudo systemd-run --quiet --pipe --wait --uid=learnpilot --gid=learnpilot \
  -p EnvironmentFile=/etc/learnpilot/learnpilot.env -p WorkingDirectory=/srv/learnpilot/app \
  /srv/learnpilot/venv/bin/gunicorn --config deploy/gunicorn.conf.py --check-config
```

`systemd-run` reads the environment file exactly as the service unit does
(shell sourcing would interpret `$`, quotes and spaces differently).

## 11. systemd

```bash
sudo cp /srv/learnpilot/app/deploy/systemd/learnpilot.service /etc/systemd/system/
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
sudo cp /srv/learnpilot/app/deploy/nginx/learnpilot.conf /etc/nginx/sites-available/learnpilot
sudoedit /etc/nginx/sites-available/learnpilot        # replace learnpilot.example.com
sudo ln -s /etc/nginx/sites-available/learnpilot /etc/nginx/sites-enabled/learnpilot
sudo rm /etc/nginx/sites-enabled/default               # the template has its own default_server
sudo nginx -t && sudo systemctl reload nginx
```

The template proxies to `127.0.0.1:8000`, overwrites `X-Forwarded-For/Proto/Host`
(the app trusts exactly one proxy hop), buffers requests and responses so slow
clients never hold the single worker, gzips text responses, serves
`/static/` directly with a short cache (assets are not fingerprinted), drops
unknown host names and denies hidden files.

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

```bash
sudo ufw allow OpenSSH
sudo ufw allow 'Nginx Full'
sudo ufw enable
sudo ufw status verbose
```

Never open port 8000; Gunicorn listens on loopback only.

## 16. Backup

The database uses WAL: copying only `questions.db` can miss committed data.
Use the online backup tool, which reads one consistent snapshot while the app
runs, verifies it with `PRAGMA quick_check`, writes a single `0600` file and
never overwrites or writes inside the checkout:

```bash
cd /srv/learnpilot/app
sudo -u learnpilot /srv/learnpilot/venv/bin/python -m src.backup_sqlite \
  --db /var/lib/learnpilot/questions.db --output-dir /var/backups/learnpilot
```

(`sqlite3 /var/lib/learnpilot/questions.db ".backup '/var/backups/learnpilot/manual.db'"`
is an equivalent supported method; run `PRAGMA quick_check` on the result.)

Daily schedule:

```bash
sudo cp /srv/learnpilot/app/deploy/systemd/learnpilot-backup.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now learnpilot-backup.timer
sudo systemctl start learnpilot-backup.service && journalctl -u learnpilot-backup -n 20
```

Uploads and quiz notes are files, not database rows. Back them up after the
database so every file the database backup references is included:

```bash
sudo tar -C /var/lib/learnpilot -czf \
  /var/backups/learnpilot/files-$(date -u +%Y%m%dT%H%M%SZ).tar.gz uploads vault
```

Copy `/var/backups/learnpilot` to another machine regularly; a backup on the
same disk does not survive loss of the VPS. Nothing prunes backups
automatically; review and delete old ones deliberately, for example
`sudo find /var/backups/learnpilot -name 'questions-*.db' -mtime +30 -print`.

## 17. Restore

```bash
sudo systemctl stop learnpilot
sudo -u learnpilot sqlite3 /var/backups/learnpilot/<backup>.db 'PRAGMA quick_check;'   # expect: ok
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
sudo install -d -o learnpilot -g learnpilot -m 0700 /var/lib/learnpilot/replaced-$STAMP
sudo sh -c "mv /var/lib/learnpilot/questions.db* /var/lib/learnpilot/replaced-$STAMP/"
sudo install -o learnpilot -g learnpilot -m 0600 /var/backups/learnpilot/<backup>.db \
  /var/lib/learnpilot/questions.db
sudo -u learnpilot sqlite3 /var/lib/learnpilot/questions.db 'PRAGMA journal_mode=WAL;'
cd /srv/learnpilot/app && sudo -u learnpilot /srv/learnpilot/venv/bin/python -m src.manage_migrations \
  --db /var/lib/learnpilot/questions.db --dry-run
sudo systemctl start learnpilot && curl -fsS http://127.0.0.1:8000/healthz
```

The database and its sidecars are moved together, so the replaced copy stays
consistent. Restore files with
`sudo tar -C /var/lib/learnpilot -xzf /var/backups/learnpilot/files-<stamp>.tar.gz`.

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

```bash
curl -fsS http://127.0.0.1:8000/healthz      # Gunicorn directly
curl -fsS https://<your-domain>/healthz      # through Nginx
```

`GET /healthz` returns `200 ok` after opening the database read-only and
reading its header (one cheap query), or `503 unavailable` without details.
It renders no templates, sets no cookie, runs no content queries and is not
cached. It never creates a missing database.

## 20. Deploy / update procedure

```bash
cd /srv/learnpilot/app
sudo -u learnpilot /srv/learnpilot/venv/bin/python -m src.backup_sqlite \
  --db /var/lib/learnpilot/questions.db --output /var/backups/learnpilot/pre-deploy-$(date -u +%Y%m%dT%H%M%SZ).db
sudo git fetch && sudo git checkout <new-release>
sudo /srv/learnpilot/venv/bin/pip install -r requirements.txt
# Section 9: migrations dry-run, then --apply only if pending.
# Section 10: gunicorn --check-config.
sudo systemctl restart learnpilot
curl -fsS http://127.0.0.1:8000/healthz
```

Then smoke-test the homepage, a subject page, tester login and test creation
over HTTPS. With one worker, a restart causes a few seconds of unavailability.
The tracked seed database in the checkout may change with a release; it is
never the live database.

## 21. Rollback considerations

- Code: check out the previous release, reinstall requirements, restart.
- Migrations are forward-only. Startup blocks only *pending* migrations; older
  code usually runs on a newer, additively migrated schema. If a release's
  migration is not backward compatible, restore the pre-deploy database backup
  (section 17), which discards writes made after it; decide that explicitly.
- Browsers may keep old CSS/JS for up to one hour (static cache lifetime).

## 1 GB VPS sizing

Initial configuration from capacity testing: Gunicorn `workers = 1`,
`worker_class = "sync"`, `threads = 1`, `timeout = 30`.

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
