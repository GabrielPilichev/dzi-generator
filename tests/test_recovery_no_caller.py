"""RZ1/RE10, RZ2 as replaced in scope 8.3, and RR6/RR15: the only recovery issuer is the
service, which exactly one production module calls (the blueprint); entry-point and runbook seams.

Scope 8.2 added ``web/services/recovery.py``, which inserts recovery rows and
recovery-purpose ledger rows; RZ1 was narrowed accordingly (that module and the migration
are the only writers). Scope 8.3 adds the blueprint ``web/blueprints/recovery.py``, and RZ2
("nothing imports the service") is replaced by a precise caller policy: the blueprint is the
only module that calls the service's writers, ``web.production`` reads its gate constant and
nothing else, and no CLI, WSGI entry point or deployment file reaches it. The policy is
mirrored by the whole-suite audit (``tests/recovery_audit.py``), which allows a route-driven
insert only while a recovery test drives the blueprint and denies every other intermediary.
These are static and import-level checks of this repository; they are not the reason the
build is safe (design 7.3): the 8.1 cleanup, restore and snapshot integration is.
"""
import ast
import hashlib
import importlib
import inspect
import json
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
BLUEPRINT = ROOT/"web/blueprints/recovery.py"
PRODUCTION = ROOT/"web/production.py"
SERVICE_WRITERS = ("request", "resend", "redeem", "cancel", "dispatch", "settle_send")
# The only modules that may name the service (scope 8.3), and what each may use of it: the
# blueprint the public API, ``web.production`` the gate constant and nothing else.
SERVICE_API = frozenset({"request", "resend", "redeem", "cancel", "pending_view", "dispatch", "settle_send",
                         "binding_hash", "NotSendable", "Outcome", "Settlement"})
SERVICE_USERS = {BLUEPRINT: SERVICE_API, PRODUCTION: frozenset({"PRODUCTION_ACTIVATION"})}
# Any way of naming the service module (not recovery_scrub) in source code.
SERVICE_REFERENCE = re.compile(
    r"web\.services\.recovery(?!_scrub)\b|from\s+web\.services\s+import\s+[^\n]*\brecovery\b(?!_scrub)"
    r"|services/recovery\.py|['\"]recovery['\"]")


def service_aliases(tree):
    """Local names bound to the service module by an import, and whether any import names it."""
    aliases = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "web.services":
            aliases |= {a.asname or a.name for a in node.names if a.name == "recovery"}
        elif isinstance(node, ast.ImportFrom) and node.module == "web.services.recovery":
            aliases.add(None)  # a from-import of names out of the service: never allowed
        elif isinstance(node, ast.Import):
            aliases |= {a.asname or a.name for a in node.names if a.name == "web.services.recovery"}
    return aliases


# The instrumented interpreter of ``NoCallerTest.run_audited``. It takes its parameters as one JSON
# argument, builds the stub modules named by ``chain`` (outermost first) and reports
# {"denied": <exception class or None>, "rows": <recovery rows seen before the rollback or None>,
#  "second": ...}. ``second`` (one module name) runs a second, direct INSERT with the very same SQL text on
# the same connection after the first call, to show whether a statement prepared for one caller is
# authorized again for the next; ``fresh`` is the audit's ``fresh_prepare``.
AUDITED_PROBE = r'''
import json, sys
params = json.loads(sys.argv[1])
sys.path[:0] = params["paths"]
import sqlite3
from datetime import datetime, timezone
from tests.recovery_audit import install
install(params["log"], fresh_prepare=params["fresh"])
from web.services import recovery

SERVICE = "def call(conn):\n    return recovery.request(conn, email='audit@example.bg', previous_binding=None, secret_key='k', clock=clock, sender_busy=False)\n"
INSERT = ("def call(conn, digit='a'):\n    return conn.execute(\"INSERT INTO password_recoveries (binding_hash, state, is_shadow, email, "
          "created_at, expires_at) VALUES (?, 'pending', 1, ?, ?, ?)\", (digit * 64, 'fixture@example.bg', "
          "'2026-10-20 00:00:00', '2026-10-20 00:30:00'))\n")
CALLER = "def call(conn):\n    return inner(conn)\n"

def load(name, source, **names):
    namespace = dict(names, __name__=name)
    exec(compile(source, name, "exec"), namespace)
    return namespace["call"]

clock = lambda: datetime(2026, 10, 20, tzinfo=timezone.utc)
chain = params["chain"]
if chain:
    call = load(chain[-1], SERVICE if params["via_service"] else INSERT, recovery=recovery, clock=clock)
    for name in reversed(chain[:-1]):
        call = load(name, CALLER, inner=call)
else:
    # Straight from this top level, whose module name is ``__main__``.
    call = lambda conn: (recovery.request(conn, email="audit@example.bg", previous_binding=None, secret_key="k",
                                          clock=clock, sender_busy=False))
conn = sqlite3.connect(params["db"])
conn.execute("BEGIN IMMEDIATE")
outcome = dict(denied=None, rows=None, second=None)
try:
    call(conn)
    outcome["rows"] = conn.execute("SELECT COUNT(*) FROM password_recoveries").fetchone()[0]
except sqlite3.DatabaseError as exc:
    outcome["denied"] = type(exc).__name__
if params["second"] is not None:
    # The identical SQL text, other values, from another module, on the same connection.
    again = load(params["second"], INSERT)
    try:
        again(conn, "b")
        outcome["second"] = dict(denied=None, rows=conn.execute("SELECT COUNT(*) FROM password_recoveries").fetchone()[0])
    except sqlite3.DatabaseError as exc:
        outcome["second"] = dict(denied=type(exc).__name__)
conn.rollback()
conn.close()
print(json.dumps(outcome))
'''


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
        # Scope 8.3: the routes are the one caller of the service. Their templates are the only
        # recovery templates; neither the blueprint nor a template writes the database itself.
        self.assertTrue(BLUEPRINT.exists())
        self.assertEqual(sorted(p.name for p in (ROOT/"web/templates").rglob("*recover*")),
                         ["recover_message.html", "recover_request.html", "recover_verify.html"])
        for sql in sql_strings(BLUEPRINT):
            self.assertNotRegex(sql, r"(?is)^\s*(SELECT|INSERT|UPDATE|DELETE|REPLACE|WITH)\b")
        self.assertEqual([sql for sql in sql_strings(BLUEPRINT) if re.match(r"(?i)\s*BEGIN\b", sql)],
                         ["BEGIN IMMEDIATE"])

    def test_only_cli_backup_import_scrub_and_scrub_has_only_finalizing_writes(self):
        """What may import anything recovery-named: the CLI and the backup module the scrub; the
        blueprint the service and the settings; the composition roots the blueprint and the settings."""
        allowed = {
            ROOT/"src/manage_accounts.py": {"recovery_scrub", "web.services.recovery_scrub"},
            ROOT/"src/backup_sqlite.py": {"recovery_scrub", "web.services.recovery_scrub"},
            SERVICE: {"recovery_scrub", "web.services.recovery_scrub"},
            BLUEPRINT: {"recovery", "web.services.recovery", "web.recovery_settings", "recovery_settings"},
            PRODUCTION: {"recovery", "web.services.recovery", "web.recovery_settings", "recovery_settings"},
            ROOT/"web/app.py": {"recovery_bp", "web.blueprints.recovery", "web.recovery_settings", "recovery_settings"},
        }
        for directory in ("web", "src"):
            for path in (ROOT/directory).rglob("*.py"):
                tree = ast.parse(path.read_text())
                for node in ast.walk(tree):
                    if isinstance(node, (ast.Import, ast.ImportFrom)):
                        names = [a.name for a in node.names] + [getattr(node, "module", "") or ""]
                        for name in names:
                            if "recovery" in name:
                                self.assertIn(path, allowed, f"{path.relative_to(ROOT)} imports {name}")
                                self.assertIn(name, allowed[path], f"{path.relative_to(ROOT)} imports {name}")
        for name, handler in manage_accounts.COMMANDS.items():
            if "recovery_scrub" in inspect.getsource(handler):
                self.assertIn(name, {"registrations-cleanup", "registrations-restore-gate", "registrations-restore-status"})

    def test_rz2_only_the_blueprint_calls_the_service_and_production_reads_its_gate(self):
        """RZ2 as replaced in scope 8.3 (static): exactly two web or src files import the service, the
        blueprint (the public API only) and ``web.production`` (the gate constant only); no other file
        names the module, loads it by string or calls a writer; no CLI handler references it."""
        users = {}
        for directory in ("web", "src"):
            for path in (ROOT/directory).rglob("*.py"):
                if path == SERVICE:
                    continue
                source = path.read_text()
                tree = ast.parse(source)
                aliases = service_aliases(tree)
                if path in SERVICE_USERS:
                    self.assertNotIn(None, aliases, path)
                    users[path] = aliases
                    used = {node.attr for node in ast.walk(tree)
                            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
                            and node.value.id in aliases}
                    self.assertTrue(used, path)
                    self.assertLessEqual(used, SERVICE_USERS[path], (str(path), used - SERVICE_USERS[path]))
                    continue
                self.assertEqual(aliases, set(), str(path))
                if path != ROOT/"web/app.py":    # the composition root names the blueprint (the string "recovery")
                    self.assertNotRegex(source, SERVICE_REFERENCE, str(path))
                self.assertNotRegex(source, r"\brecovery\.(?:%s)\s*\(" % "|".join(SERVICE_WRITERS), str(path))
        self.assertEqual(set(users), set(SERVICE_USERS))
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

    def test_the_blueprint_commits_only_in_its_one_transaction_helper(self):
        """The caller owns BEGIN IMMEDIATE, COMMIT and rollback, in one place."""
        tree = ast.parse(BLUEPRINT.read_text())
        commits = [(function.name, node.lineno) for function in ast.walk(tree)
                   if isinstance(function, ast.FunctionDef)
                   for node in ast.walk(function)
                   if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                   and node.func.attr in ("commit", "rollback", "execute")]
        self.assertEqual({name for name, _ in commits}, {"_write", "_rollback"})

    def test_fresh_interpreter_app_wsgi_blueprints_and_operation_commands(self):
        env = _state_dir(self, populated=True)
        script = '''
import importlib, os, pathlib, sqlite3, sys
import web.wsgi
import web.production
for path in pathlib.Path('web/blueprints').glob('*.py'):
    importlib.import_module('web.blueprints.' + path.stem)
import web.app
loaded = [name for name in sys.modules if name == 'web.services.recovery']
assert loaded == ['web.services.recovery'], 'the blueprint imports the service'
rules = sorted((rule.rule, rule.endpoint) for app in (web.wsgi.app,) for rule in app.url_map.iter_rules()
               if 'recover' in rule.rule or 'recover' in rule.endpoint)
assert rules == [('/recover', 'recovery.start'), ('/recover/cancel', 'recovery.cancel'),
                 ('/recover/resend', 'recovery.resend'), ('/recover/verify', 'recovery.verify')], rules
assert web.wsgi.app.config['RECOVERY'] is None, 'the production gate is off: recovery is disabled'
from web.services import recovery
assert recovery.PRODUCTION_ACTIVATION is False
for app in (web.wsgi.app,):
    hooks = [app.before_request_funcs, app.after_request_funcs, app.teardown_request_funcs,
             app.url_value_preprocessors, app.template_context_processors]
    for registry in hooks:
        for functions in registry.values():
            assert not any('services.recovery' in (getattr(f, '__module__', '') or '') for f in functions)
    # The blueprint's only hooks: the response headers and the template context.
    assert not any('recovery' in (getattr(f, '__module__', '') or '')
                   for f in app.before_request_funcs.get(None, []))
    assert not any('recovery' in (getattr(f, '__module__', '') or '') for f in app.teardown_appcontext_funcs)
with sqlite3.connect(os.environ['DZI_DB']) as conn:
    assert conn.execute('SELECT COUNT(*) FROM password_recoveries').fetchone()[0] == 0
    assert conn.execute('SELECT COUNT(*) FROM registration_sends WHERE recovery_id IS NOT NULL').fetchone()[0] == 0
print('entrypoints: gate off; routes answer 404; recovery storage empty')
'''
        result = _run_python(script, env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("recovery storage empty", result.stdout)

    def test_operation_commands_do_not_load_the_service_or_the_blueprint(self):
        """No operator command, backup module or restore helper reaches the service (RZ2, narrowed)."""
        env = _state_dir(self, populated=True)
        script = '''
import importlib, sys
for module in ('manage_accounts', 'manage_lifecycle', 'backup_sqlite', 'backup_application',
               'build_database', 'manage_migrations', 'restore_preflight', 'restore_credential_diff'):
    importlib.import_module('src.' + module)
loaded = sorted(name for name in sys.modules if name in ('web.services.recovery', 'web.blueprints.recovery',
                                                        'web.recovery_settings', 'web.app'))
assert loaded == [], loaded
print('operation commands: no recovery service, no blueprint, no application')
'''
        result = _run_python(script, env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("operation commands: no recovery service", result.stdout)

    # ------------------------------------------------------------------ RZ1 audit callers

    def run_audited(self, chain, *, via_service=True, second=None, fresh=True):
        """Run one INSERT in an instrumented interpreter and return (outcome, audit reports).

        ``chain`` names the modules between the interpreter's top level and the INSERT,
        outermost first; each is a stub compiled under that ``__name__``, so the frame the
        audit sees carries it. The innermost one calls ``recovery.request`` (``via_service``)
        or inserts a recovery row itself. Everything runs inside ``BEGIN IMMEDIATE`` and is
        rolled back: the outcome is read before that. ``-S``: no site processing, so an
        interpreter whose site already installs the audit (an instrumented whole-suite run)
        does not report these deliberate violations there. ``second`` names the module of a second,
        direct INSERT with the same SQL text on the same connection, after the first call;
        ``fresh`` is the audit's ``fresh_prepare``."""
        log = self.root / f"audit-{len(list(self.root.glob('audit-*.jsonl')))}.jsonl"
        packages = [path for key, path in sysconfig.get_paths().items() if key in ("purelib", "platlib")]
        params = dict(paths=[str(ROOT), *packages], log=str(log), db=str(self.path), chain=chain,
                      via_service=via_service, second=second, fresh=fresh)
        result = subprocess.run([sys.executable, "-B", "-S", "-c", AUDITED_PROBE, json.dumps(params)],
                                cwd=ROOT, capture_output=True, text=True, timeout=120,
                                env={"PATH": "", "PYTHONDONTWRITEBYTECODE": "1"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        reports = [json.loads(line) for line in log.read_text().splitlines()]
        self.assertTrue(reports)
        # Secret-safe output: counters and a process id only, no path, address or key.
        for report in reports:
            self.assertEqual(set(report), {"pid", "connections", "checked_empty", "fixture_storage",
                                           "absent_storage", "unreadable_or_closed", "violations"})
            self.assertTrue(all(type(value) is int for value in report.values()), report)
        text = log.read_text() + result.stdout + result.stderr
        for secret in (str(self.root), "audit@example.bg", "fixture@example.bg"):
            self.assertNotIn(secret, text)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM password_recoveries").fetchone()[0], 0)
        return json.loads(result.stdout.strip().splitlines()[-1]), reports

    def assert_denied(self, chain, *, via_service=True):
        outcome, reports = self.run_audited(chain, via_service=via_service)
        self.assertEqual(outcome, dict(denied="DatabaseError", rows=None, second=None), chain)
        self.assertEqual(sum(report["violations"] for report in reports), 1, chain)

    def assert_allowed(self, chain, *, via_service=True):
        outcome, reports = self.run_audited(chain, via_service=via_service)
        self.assertEqual(outcome, dict(denied=None, rows=1, second=None), chain)
        self.assertEqual(sum(report["violations"] for report in reports), 0, chain)

    def test_audit_allows_the_service_and_inserts_called_directly_from_a_recovery_test(self):
        """The service entered straight from a recovery test or fixture module stores its row and
        is no violation; so is a fixture's own INSERT (the 8.1 allowance, unchanged)."""
        for name in ("tests.test_recovery_probe", "test_recovery_probe", "tests.recovery_support",
                     "tests.recovery_service_support"):
            with self.subTest(service=name):
                self.assert_allowed([name])
        for name in ("tests.test_recovery_probe", "tests.recovery_support"):
            with self.subTest(insert=name):
                self.assert_allowed([name], via_service=False)

    def test_audit_denies_a_service_insert_reached_outside_the_recovery_tests(self):
        """The RZ1 audit as adapted for 8.2: in an instrumented interpreter, the service called
        from a module that is not a recovery test (here ``__main__``, then another test module)
        is denied at the INSERT and counted as one violation; nothing is stored."""
        self.assert_denied([])
        self.assert_denied(["tests.test_registration_probe"])
        self.assert_denied(["tests.test_recoveryish_probe"])  # prefix is ``test_recovery_``, not a substring

    def test_audit_denies_a_production_intermediary_between_a_recovery_test_and_the_service(self):
        """A recovery test on the stack is not enough: the first caller outside the audit and the
        service must itself be a recovery test or fixture. A production-named module that the
        test calls, and that calls ``recovery.request``, is denied, however many of them there are
        and whatever recovery fixture frames sit above them."""
        for chain in (["tests.test_recovery_probe", "web.blueprints.recovery_view"],
                      ["tests.recovery_service_support", "tests.test_recovery_probe", "web.app"],
                      ["tests.test_recovery_probe", "src.manage_accounts", "web.services.accounts"]):
            with self.subTest(chain=chain):
                self.assert_denied(chain)
        # A production module inserting directly is no service call and no fixture either.
        self.assert_denied(["tests.test_recovery_probe", "web.services.accounts"], via_service=False)

    def test_audit_allows_the_blueprint_only_while_a_recovery_test_drives_it(self):
        """Scope 8.3: the blueprint is the one production caller the audit names. Its service insert
        is allowed when a recovery test or fixture is up the same stack (a route test, through the
        framework's frames), and denied with no recovery test above it, or with another test above
        it, however it is reached."""
        for outer in ("tests.test_recovery_probe", "tests.recovery_route_support"):
            with self.subTest(allowed_under=outer):
                self.assert_allowed([outer, "web.blueprints.recovery"])
        self.assert_allowed(["tests.test_recovery_probe", "flask.app", "web.blueprints.recovery"])
        for chain in (["web.blueprints.recovery"],                                 # nothing above it
                      ["tests.test_registration_probe", "web.blueprints.recovery"],  # another test drives it
                      ["__main__", "web.blueprints.recovery"]):
            with self.subTest(denied=chain):
                self.assert_denied(chain)

    def test_audit_still_denies_every_other_intermediary_when_the_blueprint_is_on_the_stack(self):
        """The blueprint's name is no pass for what it calls: a production-named module (or a
        lookalike of the blueprint) between it and the service is the first caller and is denied; so
        is a module that merely contains the blueprint's name."""
        for chain in (["tests.test_recovery_probe", "web.blueprints.recovery", "web.blueprints.recovery_view"],
                      ["tests.test_recovery_probe", "web.blueprints.recovery", "web.services.accounts"],
                      ["tests.test_recovery_probe", "web.blueprints.recovery_extra"],
                      ["tests.test_recovery_probe", "web.blueprints.recovery.helpers"],
                      ["tests.test_recovery_probe", "xweb.blueprints.recovery"]):
            with self.subTest(chain=chain):
                self.assert_denied(chain)

    def test_a_prepared_insert_allowed_for_one_caller_is_authorized_again_for_the_next(self):
        """The carried regression (8.2 obligation 3, the prepared-statement cache): an INSERT prepared
        on a connection for an allowed caller, then the identical SQL text from a disallowed caller on
        that same connection. With the audit as installed (``fresh_prepare``, ``cached_statements=0``)
        the second is denied at its own preparation and counted as a violation."""
        outcome, reports = self.run_audited(["tests.test_recovery_probe"], via_service=False,
                                            second="web.services.accounts")
        self.assertEqual(outcome, dict(denied=None, rows=1, second=dict(denied="DatabaseError")))
        self.assertEqual(sum(report["violations"] for report in reports), 1)

    def test_with_the_statement_cache_the_same_regression_goes_unseen(self):
        """The limitation, pinned so that it cannot be forgotten or silently regained: with the
        per-connection statement cache (``fresh_prepare=False``) SQLite does not prepare the second
        statement again, the authorizer is not asked, and the disallowed caller's insert is stored
        and not counted. This is why the audit opens connections with ``cached_statements=0``."""
        outcome, reports = self.run_audited(["tests.test_recovery_probe"], via_service=False,
                                            second="web.services.accounts", fresh=False)
        self.assertEqual(outcome, dict(denied=None, rows=1, second=dict(denied=None, rows=2)))
        self.assertEqual(sum(report["violations"] for report in reports), 0)

    def test_restore_script_unchanged_and_nginx_changed_only_by_the_recovery_locations(self):
        base = "755058fa1a4fbd42a14acecaf750a48b676b99c8"
        previous = subprocess.check_output(["git", "show", f"{base}:deploy/learnpilot-restore.sh"], cwd=ROOT)
        self.assertEqual((ROOT/"deploy/learnpilot-restore.sh").read_bytes(), previous)
        # Scope 8.3 adds the /recover locations to the template and nothing else: with that one block
        # removed, the file is byte-identical to its pre-recovery content.
        template = (ROOT/"deploy/nginx/learnpilot.conf").read_text()
        begin = template.index("    # Password recovery (docs/DEPLOYMENT.md, section 38;")
        end = template.index("    # The callback address carries the code and the state in its query string.")
        previous = subprocess.check_output(["git", "show", f"{base}:deploy/nginx/learnpilot.conf"], cwd=ROOT)
        self.assertEqual((template[:begin] + template[end:]).encode("utf-8"), previous)
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
