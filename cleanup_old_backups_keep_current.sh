#!/usr/bin/env bash
set -euo pipefail

echo "Keeping only:"
echo "  data/questions.backup-current-stable-before-ui.db"
echo "  web/app.py.current-stable-before-ui.bak"
echo "  web/templates.current-stable-before-ui.bak"
echo "  vault.backup-current-stable-before-ui"
echo

test -f data/questions.backup-current-stable-before-ui.db
test -f web/app.py.current-stable-before-ui.bak
test -d web/templates.current-stable-before-ui.bak
test -d vault.backup-current-stable-before-ui

echo "Deleting old DB backups..."
find data -maxdepth 1 -type f -name 'questions.backup-*.db' \
  ! -name 'questions.backup-current-stable-before-ui.db' \
  -print -delete

echo
echo "Deleting old app.py backups..."
find web -maxdepth 1 -type f -name 'app.py.*.bak' \
  ! -name 'app.py.current-stable-before-ui.bak' \
  -print -delete

echo
echo "Deleting old templates backups..."
find web -maxdepth 1 -type d -name 'templates.*.bak' \
  ! -name 'templates.current-stable-before-ui.bak' \
  -print -exec rm -rf {} +

echo
echo "Deleting old vault backups..."
find . -maxdepth 1 -type d -name 'vault.backup-*' \
  ! -name './vault.backup-current-stable-before-ui' \
  -print -exec rm -rf {} +

echo
echo "Done. Remaining backups:"
echo
echo "=== DB ==="
ls -lh data/questions.backup-*.db 2>/dev/null || true
echo
echo "=== app.py ==="
ls -lh web/app.py.*.bak 2>/dev/null || true
echo
echo "=== templates ==="
ls -d web/templates.*.bak 2>/dev/null || true
echo
echo "=== vault ==="
ls -d vault.backup-* 2>/dev/null || true
