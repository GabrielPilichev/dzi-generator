"""RZ1/RE10 and RR6/RR15: no production issuer; entry-point and runbook seams."""
import ast
import hashlib
import importlib
import inspect
import re
import subprocess
import sys

from tests.recovery_support import RecoveryTestCase, ROOT
from tests.test_production_deployment import _state_dir, _run_python
from src import manage_accounts


def sql_strings(path):
    tree = ast.parse(path.read_text())
    # Adjacent literals have already been joined by Python's parser. Include
    # f-string constant parts; raw-source checks below cover constructed writes.
    return [n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)]


class NoCallerTest(RecoveryTestCase):
    def test_no_production_insert_or_ledger_purpose_writer(self):
        for directory in ("web", "src"):
            for path in (ROOT/directory).rglob("*.py"):
                for sql in sql_strings(path):
                    self.assertNotRegex(sql, r"(?is)(?:INSERT(?:\s+OR\s+\w+)?\s+INTO|REPLACE\s+INTO)\s+[\"`\[]?password_recoveries")
                    if re.search(r"(?is)(?:INSERT|REPLACE|UPDATE)\s+(?:INTO\s+)?registration_sends", sql):
                        # WHERE recovery_id IS NULL is the settlement guard.
                        write = re.split(r"\bWHERE\b", sql, flags=re.I)[0]
                        self.assertNotIn("recovery_id", write, str(path))
                    # RE10: email verification attests the current address.
                    if re.search(r"(?is)UPDATE\s+users\s+SET", sql):
                        self.assertNotRegex(sql, r"(?i)\bemail\s*=")
                    self.assertNotRegex(sql, r"(?is)(?:INSERT\s+OR\s+REPLACE|REPLACE)\s+INTO\s+users")
                source = path.read_text()
                self.assertNotRegex(source, r"(?is)\b(?:insert|replace)\s+(?:into\s+)?password_recoveries")
        self.assertFalse((ROOT/"web/services/recovery.py").exists())
        self.assertFalse((ROOT/"web/blueprints/recovery.py").exists())

    def test_only_cli_backup_import_scrub_and_scrub_has_only_finalizing_writes(self):
        allowed = {ROOT/"src/manage_accounts.py", ROOT/"src/backup_sqlite.py"}
        for directory in ("web", "src"):
            for path in (ROOT/directory).rglob("*.py"):
                tree = ast.parse(path.read_text())
                for node in ast.walk(tree):
                    if isinstance(node, (ast.Import, ast.ImportFrom)):
                        names = [a.name for a in node.names] + [getattr(node,"module","") or ""]
                        for name in names:
                            if "recovery" in name:
                                self.assertIn(path, allowed)
                                self.assertIn(name, ("recovery_scrub", "web.services.recovery_scrub"))
        for name, handler in manage_accounts.COMMANDS.items():
            if "recovery_scrub" in inspect.getsource(handler):
                self.assertIn(name, {"registrations-cleanup", "registrations-restore-gate", "registrations-restore-status"})
        source = (ROOT/"web/services/recovery_scrub.py").read_text()
        for sql in sql_strings(ROOT/"web/services/recovery_scrub.py"):
            if sql.startswith("UPDATE password_recoveries"):
                self.assertRegex(sql, r"SET state = '(void|expired)'")
            if sql.startswith("DELETE FROM password_recoveries"):
                self.assertIn("state != 'pending'", sql)
        self.assertIn("SCRUB_ASSIGNMENTS", source)
        for path in (ROOT/"deploy").rglob("*"):
            if path.is_file():
                text = path.read_text()
                self.assertNotIn("password_recoveries", text)
                self.assertNotIn("recovery_scrub", text)
                self.assertNotIn("web.services.recovery", text)

    def test_fresh_interpreter_app_wsgi_blueprints_and_operation_commands(self):
        env = _state_dir(self, populated=True)
        script = '''
import importlib, os, pathlib, sqlite3, sys
import web.wsgi
import web.production
for path in pathlib.Path('web/blueprints').glob('*.py'):
    importlib.import_module('web.blueprints.' + path.stem)
for module in ('manage_accounts', 'manage_lifecycle', 'backup_sqlite', 'backup_application',
               'build_database', 'manage_migrations', 'restore_preflight', 'restore_credential_diff'):
    importlib.import_module('src.' + module)
assert 'web.services.recovery' not in sys.modules
assert not any('recover' in rule.rule or 'recover' in rule.endpoint for rule in web.wsgi.app.url_map.iter_rules())
with sqlite3.connect(os.environ['DZI_DB']) as conn:
    assert conn.execute('SELECT COUNT(*) FROM password_recoveries').fetchone()[0] == 0
print('entrypoints: no issuer; recovery storage empty')
'''
        result = _run_python(script, env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("recovery storage empty", result.stdout)

    def test_restore_and_nginx_unchanged_existing_call_order(self):
        base = "755058fa1a4fbd42a14acecaf750a48b676b99c8"
        for name in ("deploy/learnpilot-restore.sh", "deploy/nginx/learnpilot.conf"):
            previous = subprocess.check_output(["git", "show", f"{base}:{name}"], cwd=ROOT)
            self.assertEqual((ROOT/name).read_bytes(), previous)
        source = (ROOT/"deploy/learnpilot-restore.sh").read_text()
        migrate = source.index('"${MIGRATE[@]}" --apply')
        gate = source.index('"${REGISTRATIONS[@]}" registrations-restore-gate')
        fingerprint = source.index('VERIFIED=$("${FINGERPRINT[@]}" "$STAGED_DB")')
        status = source.index('"${REGISTRATIONS[@]}" registrations-restore-status --expect held')
        in_place = source.index('--db "$LIVE_DB" registrations-restore-status --expect held')
        start = source.index('systemctl start "$SERVICE"')
        self.assertTrue(migrate < gate < fingerprint < status < in_place < start)
        self.assertEqual(source.count('systemctl start "$SERVICE"'), 1)
        self.assertIn('REPLACED=$STATE/replaced-$STAMP', source)

    def test_runbook_contract(self):
        text = (ROOT/"docs/DEPLOYMENT.md").read_text().split("## 37. Password recovery storage and restore credentials", 1)[1]
        for term in ("starts the service before it returns", "Verified maintenance isolation is mandatory",
                     "No fallback", "postponed", "server-level", "return 503", "lp_no_args", "log_format",
                     "ss -ltnp", "ss -lunp", "ss -tnp", "ssh", "sshd -T", "nft list ruleset",
                     "iptables-save -t nat", "ip6tables-save -t nat", "route_localnet",
                     "LEARNPILOT_HEALTH_URL", "journalctl -u learnpilot --since",
                     "absence of lines is not proof", "quiescence", "install -m 0400", "immutable=1",
                     "pre-repair.db", "post-repair.db", "date -u '+%Y-%m-%d %H:%M:%S'",
                     "hash_changed", "key_rotated", "password_changed_at >=", "recipient's first sign-in",
                     "set-password --email", "sign-out --id", "totp-reset --email"):
            self.assertIn(term, text)
