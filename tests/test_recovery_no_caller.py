"""RZ1/RE10, RZ2 and RR6/RR15: the only recovery issuer is the 8.2 service, which no
production entry point reaches; entry-point and runbook seams.

Scope 8.2 adds ``web/services/recovery.py``, which inserts recovery rows and
recovery-purpose ledger rows. RZ1 is narrowed accordingly (that module and the migration
are the only writers) and RZ2 pins that nothing outside the tests imports or calls it.
These are static and import-level checks of this repository; they are not the reason the
build is safe (design 7.3): the 8.1 cleanup, restore and snapshot integration is.
"""
import ast
import hashlib
import importlib
import inspect
import re
import subprocess
import sys
import sysconfig

from tests.recovery_support import RecoveryTestCase, ROOT
from tests.test_production_deployment import _state_dir, _run_python
from src import manage_accounts


def sql_strings(path):
    tree = ast.parse(path.read_text())
    # Adjacent literals have already been joined by Python's parser. Include
    # f-string constant parts; raw-source checks below cover constructed writes.
    return [n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)]


SERVICE = ROOT/"web/services/recovery.py"
SERVICE_WRITERS = ("request", "resend", "redeem", "cancel", "dispatch", "settle_send")
# Any way of naming the service module (not recovery_scrub) in source code.
SERVICE_REFERENCE = re.compile(
    r"web\.services\.recovery(?!_scrub)\b|from\s+web\.services\s+import\s+[^\n]*\brecovery\b(?!_scrub)"
    r"|services/recovery\.py|['\"]recovery['\"]")


class NoCallerTest(RecoveryTestCase):
    def test_service_is_the_only_writer_of_recovery_storage(self):
        """RZ1 as narrowed by scope 8.2: only the service inserts recovery rows or writes
        ``registration_sends.recovery_id``, and only with the statements of design 3.3."""
        inserts = [sql for sql in sql_strings(SERVICE)
                   if re.search(r"(?is)INSERT\s+INTO\s+(password_recoveries|registration_sends)", sql)]
        self.assertEqual(len(inserts), 2, inserts)
        self.assertTrue(any(sql.startswith("INSERT INTO password_recoveries (binding_hash, state, is_shadow, "
                                           "email, created_at, expires_at) VALUES (?, 'pending', 1, ?, ?, ?)")
                            for sql in inserts))
        self.assertTrue(any(sql.startswith("INSERT INTO registration_sends (recovery_id, email_hash, kind, "
                                           "attempted_at, issuance_id)") for sql in inserts))
        statements = [sql for sql in sql_strings(SERVICE)
                      if re.match(r"(?is)\s*(SELECT|INSERT|UPDATE|DELETE|REPLACE|WITH)\b", sql)]
        self.assertGreater(len(statements), 10)
        for sql in statements:
            self.assertNotRegex(sql, r"(?is)\b(DELETE\s+FROM|REPLACE\s+INTO|INSERT\s+OR)\b")
            self.assertNotRegex(sql, r"(?is)login_throttle|totp_|user_identities|pending_registrations")
            if re.search(r"(?is)UPDATE\s+users", sql):
                self.fail("the service writes users only through accounts.set_password")
            if re.search(r"(?is)UPDATE\s+registration_sends", sql):
                self.assertIn("SET outcome = ?, settled_at = ?", sql)
                self.assertIn("recovery_id IS NOT NULL", sql)

    def test_no_production_insert_or_ledger_purpose_writer(self):
        for directory in ("web", "src"):
            for path in (ROOT/directory).rglob("*.py"):
                if path == SERVICE:
                    continue  # the 8.2 service, pinned by the test above and by RZ2
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
        self.assertTrue(SERVICE.exists())
        self.assertFalse((ROOT/"web/blueprints/recovery.py").exists())
        self.assertFalse(list((ROOT/"web/templates").rglob("*recover*")))

    def test_only_cli_backup_import_scrub_and_scrub_has_only_finalizing_writes(self):
        allowed = {ROOT/"src/manage_accounts.py", ROOT/"src/backup_sqlite.py", SERVICE}
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

    def test_rz2_no_module_names_or_calls_the_service(self):
        """RZ2 (static): no web, src or deploy file imports the service, names its module,
        loads it by string or calls one of its writers; no CLI handler references it."""
        for directory in ("web", "src"):
            for path in (ROOT/directory).rglob("*.py"):
                if path == SERVICE:
                    continue
                source = path.read_text()
                self.assertNotRegex(source, SERVICE_REFERENCE, str(path))
                self.assertNotRegex(source, r"\brecovery\.(?:%s)\s*\(" % "|".join(SERVICE_WRITERS), str(path))
        for handler in manage_accounts.COMMANDS.values():
            self.assertNotRegex(inspect.getsource(handler), SERVICE_REFERENCE)
        for path in (ROOT/"deploy").rglob("*"):
            if path.is_file():
                self.assertNotRegex(path.read_text(), SERVICE_REFERENCE, str(path))
        self.assertNotRegex((ROOT/"web/wsgi.py").read_text(), r"recover")
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
import web.app
assert 'web.services.recovery' not in sys.modules, 'RZ2: an entry point imported the recovery service'
for app in (web.wsgi.app, web.app.app):
    assert not any('recover' in rule.rule or 'recover' in rule.endpoint for rule in app.url_map.iter_rules())
    hooks = [app.before_request_funcs, app.after_request_funcs, app.teardown_request_funcs,
             app.url_value_preprocessors, app.template_context_processors]
    for registry in hooks:
        for functions in registry.values():
            assert not any('recovery' in (getattr(f, '__module__', '') or '') for f in functions)
    assert not any('recovery' in (getattr(f, '__module__', '') or '') for f in app.teardown_appcontext_funcs)
with sqlite3.connect(os.environ['DZI_DB']) as conn:
    assert conn.execute('SELECT COUNT(*) FROM password_recoveries').fetchone()[0] == 0
    assert conn.execute('SELECT COUNT(*) FROM registration_sends WHERE recovery_id IS NOT NULL').fetchone()[0] == 0
print('entrypoints: no issuer; recovery service not imported; recovery storage empty')
'''
        result = _run_python(script, env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("recovery storage empty", result.stdout)

    def test_audit_denies_a_service_insert_reached_outside_the_recovery_tests(self):
        """The RZ1 audit as adapted for 8.2: in an instrumented interpreter, the service called
        from a non-test module (here ``__main__``) is denied at the INSERT and counted as a
        violation; nothing is stored. Service inserts pass only under a recovery test."""
        log = self.root / "audit.jsonl"
        # ``-S``: no site processing, so an interpreter whose site already installs the audit
        # (an instrumented whole-suite run) does not report this deliberate violation there.
        packages = [path for key, path in sysconfig.get_paths().items() if key in ("purelib", "platlib")]
        script = f'''
import sys
sys.path[:0] = {[str(ROOT), *packages]!r}
import json, sqlite3
from datetime import datetime, timezone
from tests.recovery_audit import install
install({str(log)!r})
from web.services import recovery
conn = sqlite3.connect({str(self.path)!r})
conn.execute("BEGIN IMMEDIATE")
try:
    recovery.request(conn, email="audit@example.bg", previous_binding=None, secret_key="k",
                     clock=lambda: datetime(2026, 10, 20, tzinfo=timezone.utc), sender_busy=False)
except sqlite3.DatabaseError as exc:
    print("denied:", type(exc).__name__)
conn.rollback()
conn.close()
'''
        result = subprocess.run([sys.executable, "-B", "-S", "-c", script], cwd=ROOT, capture_output=True, text=True,
                                timeout=120, env={"PATH": "", "PYTHONDONTWRITEBYTECODE": "1"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("denied: DatabaseError", result.stdout)
        reports = [__import__("json").loads(line) for line in log.read_text().splitlines()]
        self.assertEqual(sum(report["violations"] for report in reports), 1)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM password_recoveries").fetchone()[0], 0)

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
