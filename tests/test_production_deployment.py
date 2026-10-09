"""Production entry point, configuration, health check and deployment templates.

Nothing here needs systemd, Nginx, Gunicorn, network access or real
credentials. ``web.production.create_production_app`` mutates process state, so
it only ever runs in fresh interpreters against temporary database copies.
"""

import hashlib
import json
import os
import re
import runpy
import sqlite3
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from werkzeug.test import create_environ

from src.practical_uploads import MAX_REQUEST_BYTES, MAX_UPLOAD_BYTES
from tests.support import ROOT, IsolatedAppTestCase, build_legacy_database, build_test_database, web_app
from web import production
from web.services import accounts


SECRET_KEY = "test-only-secret-key-" + "k" * 32
ADMIN_PASSWORD = "test-only-admin-passphrase"
TESTER_PASSWORD = "test-only-tester-passphrase"
# The removed shared passwords, as an env file that predates their removal still has them.
RETIRED_SHARED_PASSWORDS = {"DZI_ADMIN_PASSWORD": ADMIN_PASSWORD, "DZI_TESTER_PASSWORD": TESTER_PASSWORD}
# The removed bridge switch. No value of it is read, validated or able to enable anything.
RETIRED_FLAG = "DZI_LEGACY_SHARED_LOGIN"
TEACHER_EMAIL = "probe-teacher@example.bg"
TEACHER_PASSWORD = "test-only-teacher-passphrase"
DEPLOY = ROOT / "deploy"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _state_dir(test: unittest.TestCase, *, populated: bool, baseline: bool = True) -> dict:
    tmp = tempfile.TemporaryDirectory(prefix="learnpilot-prod-")
    test.addCleanup(tmp.cleanup)
    root = Path(tmp.name).resolve()
    db = root / "questions.db"
    if populated and baseline:
        build_test_database(db)  # migrations + canonical content, 000 recorded
    elif populated:
        build_legacy_database(db)  # synthetic: predates 000, so the baseline is not adopted
    else:
        db.write_bytes(b"")
    (root / "uploads").mkdir()
    (root / "vault").mkdir()
    return {
        "DZI_SECRET_KEY": SECRET_KEY,
        "DZI_DB": str(db),
        "DZI_UPLOAD_ROOT": str(root / "uploads"),
        "DZI_VAULT": str(root / "vault"),
        "DZI_PUBLIC_HOST": "learnpilot.example.com",
    }


def _run_python(script: str, env: dict, *args: str) -> subprocess.CompletedProcess:
    """Fresh interpreter with ONLY the given variables (no developer DZI_* leakage)."""
    base = {"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", ""),
            "PYTHONDONTWRITEBYTECODE": "1"}
    return subprocess.run(
        [sys.executable, "-B", "-c", script, *args], cwd=ROOT, env={**base, **env},
        capture_output=True, text=True, timeout=120,
    )


class ProductionConfigValidationTest(unittest.TestCase):
    def setUp(self):
        self.env = _state_dir(self, populated=False)

    def errors(self, **changes):
        env = {**self.env, **changes}
        return production.production_config_errors({k: v for k, v in env.items() if v is not None})

    def test_complete_environment_is_accepted(self):
        self.assertEqual(self.errors(), [])
        self.assertEqual(self.errors(DZI_SESSION_COOKIE_SECURE="true", FLASK_DEBUG="0"), [])

    def test_missing_secrets_are_named_without_leaking_values(self):
        for name in production.REQUIRED_SECRETS:
            with self.subTest(name=name):
                errors = self.errors(**{name: None})
                self.assertIn(f"{name} is not set", errors)
                joined = "\n".join(errors)
                for value in (SECRET_KEY, ADMIN_PASSWORD, TESTER_PASSWORD):
                    self.assertNotIn(value, joined)

    def test_short_secret_key_is_rejected(self):
        errors = self.errors(DZI_SECRET_KEY="too-short")
        self.assertTrue(errors)
        self.assertNotIn("too-short", "\n".join(errors))

    def test_shared_passwords_are_neither_required_nor_validated(self):
        self.assertEqual(production.REQUIRED_SECRETS, ("DZI_SECRET_KEY",))
        # Nothing reads them anywhere, so leftovers of any shape are inert.
        for changes in (RETIRED_SHARED_PASSWORDS,
                        {"DZI_ADMIN_PASSWORD": "admin123", "DZI_TESTER_PASSWORD": "admin123"}):
            with self.subTest(changes=sorted(changes)):
                self.assertEqual(self.errors(**changes), [])

    def test_production_config_has_no_shared_login_switch(self):
        # The bridge is gone, so there is no flag to parse, validate or force off:
        # the module does not know the name, and a leftover value of any spelling
        # is as inert as any other unknown variable.
        source = Path(production.__file__).read_text(encoding="utf-8")
        for needle in ("LEGACY", "DZI_ADMIN_PASSWORD", "DZI_TESTER_PASSWORD", "web.access"):
            self.assertNotIn(needle, source)
        for value in ("", "0", "false", "off", "1", "true", "on", "maybe"):
            with self.subTest(value=value):
                self.assertEqual(self.errors(**{RETIRED_FLAG: value}), [])

    def test_insecure_cookies_and_debug_are_rejected(self):
        for value in ("0", "false", "off", "maybe"):
            with self.subTest(cookie=value):
                self.assertIn("DZI_SESSION_COOKIE_SECURE must be unset or true in production",
                              self.errors(DZI_SESSION_COOKIE_SECURE=value))
        for value in ("1", "true", "on", "yes"):
            with self.subTest(debug=value):
                self.assertIn("FLASK_DEBUG must not be enabled in production",
                              self.errors(FLASK_DEBUG=value))

    def test_public_host_is_required_and_rejects_url_or_wildcard(self):
        for value in (None, "", "https://school.example", "*.school.example",
                      "school.example:443", "school.example/evil",
                      f'{"a" * 64}.example.com'):
            with self.subTest(value=value):
                self.assertTrue(any("DZI_PUBLIC_HOST" in error for error in
                                    self.errors(DZI_PUBLIC_HOST=value)))

    def test_public_host_validation_agrees_with_the_shared_link_base_url(self):
        # Each of these once passed startup and then broke (or misdirected) the student
        # link and its QR code: letters that only case-fold to ASCII, hosts a URL
        # parser reads as an IPv4 address, and malformed IDNA labels.
        from web.public_url import normalize_public_base_url, public_base_url_for_host

        for value in ("examp\u017fe.com", "ex\u0131mple.com", "\u0130stanbul.com", "\u212aelvin.example.com",
                      "127.1", "1.2.3.4", "0x7f.1", "999.999.999.999", "school.1",
                      "xn--zz.example.com", "xn--a.com", "localhost"):
            with self.subTest(value=ascii(value)):
                errors = self.errors(DZI_PUBLIC_HOST=value)
                self.assertEqual(len(errors), 1)
                self.assertIn("DZI_PUBLIC_HOST must be a configured DNS hostname", errors[0])
                self.assertNotIn(value, errors[0])
                self.assertIsNone(public_base_url_for_host(value))
        for value, base in (("learnpilot.example.com", "https://learnpilot.example.com"),
                            (" LearnPilot.BG ", "https://learnpilot.bg"),
                            ("xn--e1aybc.xn--90ae", "https://xn--e1aybc.xn--90ae")):
            with self.subTest(value=value):
                self.assertEqual(self.errors(DZI_PUBLIC_HOST=value), [])
                self.assertEqual(public_base_url_for_host(value), base)
                self.assertEqual(normalize_public_base_url(base), base)

    def test_separate_public_base_url_is_rejected_in_production(self):
        # Shared links and QR codes are always https://DZI_PUBLIC_HOST in production.
        for value in ("https://learnpilot.example.com", "https://evil.example", "http://127.0.0.1:5001"):
            with self.subTest(value=value):
                errors = self.errors(DZI_PUBLIC_BASE_URL=value)
                self.assertEqual(len(errors), 1)
                self.assertIn("DZI_PUBLIC_BASE_URL must not be set in production", errors[0])
                self.assertNotIn(value, errors[0])
        self.assertEqual(self.errors(DZI_PUBLIC_BASE_URL=""), [])

    def test_state_paths_must_be_explicit_absolute_and_outside_checkout(self):
        cases = {
            "DZI_DB": [None, "data/questions.db", str(ROOT / "data/questions.db"),
                       str(Path(self.env["DZI_VAULT"]) / "missing.db")],
            "DZI_UPLOAD_ROOT": [None, "uploads", str(ROOT / "data/uploads/practical"),
                                str(Path(self.env["DZI_VAULT"]) / "missing-dir"), self.env["DZI_DB"]],
            "DZI_VAULT": [None, "vault", str(ROOT / "vault")],
        }
        for name, values in cases.items():
            for value in values:
                with self.subTest(name=name, value=value):
                    errors = self.errors(**{name: value})
                    self.assertTrue(any(error.startswith(name) for error in errors), errors)
        self.assertFalse((Path(self.env["DZI_VAULT"]) / "missing.db").exists())

    def test_reference_root_is_optional_and_validated_only_when_set(self):
        self.assertEqual(self.errors(DZI_REFERENCE_ROOT=None), [])
        self.assertEqual(self.errors(DZI_REFERENCE_ROOT=""), [])
        reference = Path(self.env["DZI_DB"]).parent / "reference"
        reference.mkdir()
        self.assertEqual(self.errors(DZI_REFERENCE_ROOT=str(reference)), [])
        cases = {
            "data/reference": "DZI_REFERENCE_ROOT must be an absolute path",
            str(ROOT / "data" / "reference"): "DZI_REFERENCE_ROOT must be outside the application checkout",
            str(reference / "missing"): "DZI_REFERENCE_ROOT must be an existing directory",
            self.env["DZI_DB"]: "DZI_REFERENCE_ROOT must be an existing directory",
        }
        for value, expected in cases.items():
            with self.subTest(value=value):
                self.assertEqual(self.errors(DZI_REFERENCE_ROOT=value), [expected])
        self.assertFalse((reference / "missing").exists())

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root bypasses permissions")
    def test_unreadable_reference_root_is_rejected(self):
        reference = Path(self.env["DZI_DB"]).parent / "reference"
        reference.mkdir()
        reference.chmod(0)
        self.addCleanup(reference.chmod, stat.S_IRWXU)
        self.assertEqual(self.errors(DZI_REFERENCE_ROOT=str(reference)),
                         ["DZI_REFERENCE_ROOT must be readable by the service user"])

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root bypasses permissions")
    def test_unwritable_state_directories_are_rejected(self):
        uploads = Path(self.env["DZI_UPLOAD_ROOT"])
        db_dir = Path(self.env["DZI_DB"]).parent
        uploads.chmod(stat.S_IRUSR | stat.S_IXUSR)
        self.addCleanup(uploads.chmod, stat.S_IRWXU)
        self.assertIn("DZI_UPLOAD_ROOT must be writable by the service user", self.errors())
        uploads.chmod(stat.S_IRWXU)
        db_dir.chmod(stat.S_IRUSR | stat.S_IXUSR)
        self.addCleanup(db_dir.chmod, stat.S_IRWXU)
        self.assertTrue(any("DZI_DB" in error for error in self.errors()))

    def test_trusted_proxy_honours_exactly_one_hop(self):
        seen = {}

        def probe(environ, start_response):
            seen.update(environ)
            start_response("200 OK", [])
            return [b""]

        environ = create_environ("/", "http://127.0.0.1:8000/", headers={
            "X-Forwarded-For": "198.51.100.9, 203.0.113.7",
            "X-Forwarded-Proto": "https",
            "X-Forwarded-Host": "learnpilot.example.com",
            "X-Forwarded-Port": "8443",
            "X-Forwarded-Prefix": "/forged",
        }, environ_overrides={"REMOTE_ADDR": "127.0.0.1"})
        list(production.trusted_proxy(probe)(environ, lambda *args: None))
        self.assertEqual(seen["REMOTE_ADDR"], "203.0.113.7")
        self.assertEqual(seen["wsgi.url_scheme"], "https")
        self.assertEqual(seen["HTTP_HOST"], "learnpilot.example.com")
        self.assertNotEqual(seen.get("SERVER_PORT"), "8443")
        self.assertEqual(seen.get("SCRIPT_NAME", ""), "")


PRODUCTION_PROBE = r'''
import hashlib, json, os, sys
from pathlib import Path
db = Path(os.environ["DZI_DB"])
before = hashlib.sha256(db.read_bytes()).hexdigest()
from web.wsgi import app
from web import app as web_app
from werkzeug.middleware.proxy_fix import ProxyFix
after_import = hashlib.sha256(db.read_bytes()).hexdigest()
proxy = {"X-Forwarded-Proto": "https", "X-Forwarded-Host": "learnpilot.example.com",
         "X-Forwarded-For": "203.0.113.7"}
origin = {"Origin": "https://learnpilot.example.com"}
client = app.test_client()
health = client.get("/healthz", headers=proxy)
public = client.get("/", headers=proxy)
foreign_host = client.get("/", headers={**proxy, "X-Forwarded-Host": "evil.example"})
same_origin = {**proxy, **origin}
wrong = client.post("/login", data={"email": "nobody@example.bg", "password": "wrong"}, headers=same_origin)
forged = client.post("/login", data={"email": "nobody@example.bg", "password": "wrong"},
                     headers={**proxy, "Origin": "https://evil.example"})
unproxied = client.post("/login", data={"email": "nobody@example.bg", "password": "wrong"}, headers=origin)
login = app.test_client().post(
    "/login", data={"email": os.environ["PROBE_EMAIL"], "password": os.environ["PROBE_PASSWORD"]},
    headers=same_origin)
# The shared-password routes do not exist, even with the right shared password.
legacy = {}
for path, name in (("/tester/login", "DZI_TESTER_PASSWORD"), ("/admin/login", "DZI_ADMIN_PASSWORD")):
    fresh = app.test_client()
    legacy["GET " + path] = fresh.get(path, headers=proxy).status_code
    posted = fresh.post(path, data={"password": os.environ[name]}, headers=same_origin)
    legacy["POST " + path] = posted.status_code
    legacy["cookie " + path] = posted.headers.get("Set-Cookie")
# A cookie signed while the bridge existed: its keys authorize nothing.
stale = app.test_client()
with stale.session_transaction(base_url="https://learnpilot.example.com") as sess:
    sess["admin_authenticated"] = True
    sess["tester_authenticated"] = True
    sess["ui_profile"] = "admin"
stale_admin = stale.get("/teacher", headers=proxy)
stale_teacher = stale.get("/teacher/new", headers=proxy)
wsgi = app.wsgi_app
print(json.dumps({
    "unchanged_by_import": before == after_import,
    "debug": app.debug, "testing": app.testing,
    "cookie_secure": app.config["SESSION_COOKIE_SECURE"],
    "scheme": app.config["PREFERRED_URL_SCHEME"],
    "db": app.config["DB_PATH"], "quiz_db": str(web_app.QUIZ_DB_PATH),
    "uploads": app.config["PRACTICAL_UPLOAD_ROOT"], "vault": str(web_app.QUIZ_VAULT_PATH),
    "max_content_length": app.config["MAX_CONTENT_LENGTH"],
    "proxy": isinstance(wsgi, ProxyFix) and [wsgi.x_for, wsgi.x_proto, wsgi.x_host, wsgi.x_port, wsgi.x_prefix],
    "health": [health.status_code, health.get_data(as_text=True)],
    "public_cookie": public.headers.get("Set-Cookie"),
    "health_cookie": health.headers.get("Set-Cookie"),
    "foreign_host": foreign_host.status_code,
    "trusted_hosts": app.config["TRUSTED_HOSTS"],
    "public_base_url": app.config["PUBLIC_BASE_URL"],
    "wrong_password": wrong.status_code, "forged_origin": forged.status_code,
    "unproxied": unproxied.status_code,
    "login": [login.status_code, login.headers.get("Set-Cookie", "")],
    "legacy_config_keys": sorted(key for key in app.config if "LEGACY" in key),
    "auth_rules": sorted(rule.rule for rule in app.url_map.iter_rules()
                         if any(word in rule.rule for word in ("login", "logout", "tester", "profile"))),
    "blueprints": sorted(app.blueprints),
    "legacy": legacy,
    "stale_admin": [stale_admin.status_code, stale_admin.headers.get("Location")],
    "stale_teacher": [stale_teacher.status_code, stale_teacher.headers.get("Location")],
}))
'''

FAILING_STARTUP_PROBE = r'''
import hashlib, os, sys
from pathlib import Path
db = Path(os.environ["DZI_DB"])
before = hashlib.sha256(db.read_bytes()).hexdigest() if db.is_file() else None
try:
    import web.wsgi
except Exception as exc:
    after = hashlib.sha256(db.read_bytes()).hexdigest() if db.is_file() else None
    print(type(exc).__name__)
    print("app imported:", "web.app" in sys.modules)
    print("db unchanged:", before == after)
    print(exc)
    sys.exit(3)
print("started")
'''


class ProductionStartupTest(unittest.TestCase):
    def test_valid_production_startup(self):
        # The worst leftover env file: the shared passwords are still set and the
        # removed switch asks for the bridge. None of it is read; startup succeeds and
        # serves account authentication only.
        env = {**_state_dir(self, populated=True), **RETIRED_SHARED_PASSWORDS, RETIRED_FLAG: "1"}
        with sqlite3.connect(env["DZI_DB"]) as conn:
            accounts.create_user(conn, display_name="Проба Учител", email=TEACHER_EMAIL,
                                 password=TEACHER_PASSWORD, role="teacher")
        conn.close()
        result = _run_python(PRODUCTION_PROBE, {**env, "PROBE_EMAIL": TEACHER_EMAIL,
                                                "PROBE_PASSWORD": TEACHER_PASSWORD})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        report = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertTrue(report["unchanged_by_import"])
        self.assertFalse(report["debug"])
        self.assertFalse(report["testing"])
        self.assertTrue(report["cookie_secure"])
        self.assertEqual(report["scheme"], "https")
        self.assertEqual(report["db"], env["DZI_DB"])
        self.assertEqual(report["quiz_db"], env["DZI_DB"])
        self.assertEqual(report["uploads"], env["DZI_UPLOAD_ROOT"])
        self.assertEqual(report["vault"], env["DZI_VAULT"])
        self.assertEqual(report["max_content_length"], MAX_REQUEST_BYTES)
        self.assertEqual(report["proxy"], [1, 1, 1, 0, 0])
        self.assertEqual(report["health"], [200, "ok\n"])
        self.assertIsNone(report["public_cookie"])
        self.assertIsNone(report["health_cookie"])
        self.assertEqual(report["foreign_host"], 400)
        self.assertIn(env["DZI_PUBLIC_HOST"], report["trusted_hosts"])
        self.assertEqual(report["public_base_url"], "https://" + env["DZI_PUBLIC_HOST"])
        # Same-origin POST guard sees the public HTTPS origin only via the proxy.
        self.assertEqual(report["wrong_password"], 200)
        self.assertEqual(report["forged_origin"], 403)
        self.assertEqual(report["unproxied"], 403)
        status, cookie = report["login"]
        self.assertEqual(status, 302)
        for attribute in ("Secure", "HttpOnly", "SameSite=Strict"):
            self.assertIn(attribute, cookie)
        for secret in (SECRET_KEY, ADMIN_PASSWORD, TESTER_PASSWORD, TEACHER_PASSWORD):
            self.assertNotIn(secret, result.stdout + result.stderr)
        # Account authentication is the only authority (Accounts PR9/PR10).
        self.assertEqual(report["legacy_config_keys"], [])
        self.assertEqual(report["auth_rules"], ["/login", "/login/totp", "/logout"])
        self.assertNotIn("auth", report["blueprints"])
        for path in ("/tester/login", "/admin/login"):
            self.assertEqual(report["legacy"][f"GET {path}"], 404)
            self.assertEqual(report["legacy"][f"POST {path}"], 404)
            self.assertIsNone(report["legacy"][f"cookie {path}"])
        self.assertEqual(report["stale_admin"], [302, "/login?next=/teacher"])
        self.assertEqual(report["stale_teacher"], [302, "/login?next=/teacher/new"])

    def test_startup_needs_no_shared_login_setting_and_no_shared_password(self):
        env = _state_dir(self, populated=True)
        for name in (*RETIRED_SHARED_PASSWORDS, RETIRED_FLAG):
            self.assertNotIn(name, env)
        result = _run_python(
            "from web.wsgi import app\n"
            "client = app.test_client()\n"
            "print(any('LEGACY' in key for key in app.config),"
            " client.get('/admin/login').status_code, client.get('/tester/login').status_code,"
            " client.get('/login', headers={'X-Forwarded-Proto': 'https',"
            " 'X-Forwarded-Host': 'learnpilot.example.com'}).status_code)",
            env,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout.strip().splitlines()[-1], "False 404 404 200")

    def test_wsgi_is_the_only_production_entry_point_and_it_is_account_only(self):
        self.assertEqual((ROOT / "web/wsgi.py").read_text(encoding="utf-8").count("create_production_app"), 2)
        gunicorn = (ROOT / "deploy/gunicorn.conf.py").read_text(encoding="utf-8")
        self.assertIn('wsgi_app = "web.wsgi:app"', gunicorn)
        unit = (ROOT / "deploy/systemd/learnpilot.service").read_text(encoding="utf-8")
        self.assertEqual(len(re.findall(r"(?m)^ExecStart=", unit)), 1)
        self.assertIn("gunicorn --config /opt/learnpilot/app/deploy/gunicorn.conf.py", unit)
        # The local tunnel helper asks for no shared password either.
        from src import run_tunnel_dev

        self.assertEqual(run_tunnel_dev.REQUIRED_ENV_VARS, ("DZI_SECRET_KEY",))

    def test_missing_secret_stops_before_the_app_is_imported(self):
        env = _state_dir(self, populated=True)
        del env["DZI_SECRET_KEY"]
        result = _run_python(FAILING_STARTUP_PROBE, env)
        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, 3, output)
        self.assertIn("ProductionConfigError", output)
        self.assertIn("Refusing to start LearnPilot in production", output)
        self.assertIn("DZI_SECRET_KEY is not set", output)
        self.assertIn("app imported: False", output)
        self.assertIn("db unchanged: True", output)
        self.assertNotIn(SECRET_KEY, output)

    def test_invalid_reference_root_stops_before_the_app_is_imported(self):
        env = _state_dir(self, populated=True)
        env["DZI_REFERENCE_ROOT"] = str(Path(env["DZI_VAULT"]) / "missing-reference")
        result = _run_python(FAILING_STARTUP_PROBE, env)
        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, 3, output)
        self.assertIn("DZI_REFERENCE_ROOT must be an existing directory", output)
        self.assertIn("app imported: False", output)
        self.assertIn("db unchanged: True", output)

    def test_pending_migration_stops_startup_without_changing_schema(self):
        env = _state_dir(self, populated=True)
        db = Path(env["DZI_DB"])
        with sqlite3.connect(db) as conn:
            conn.execute("DELETE FROM schema_migrations WHERE filename = ?",
                         ("009_quiz_text_answer_teacher_status.sql",))
        conn.close()
        before = _sha256(db)
        result = _run_python(FAILING_STARTUP_PROBE, env)
        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, 3, output)
        self.assertIn("pending migrations: 009_quiz_text_answer_teacher_status.sql", output)
        self.assertIn("src.manage_migrations", output)
        self.assertIn("db unchanged: True", output)
        self.assertEqual(_sha256(db), before)

    def test_existing_database_that_has_not_adopted_the_baseline_stops_startup(self):
        # Rollout order: snapshot, --baseline-dry-run, --baseline-apply, then deploy.
        env = _state_dir(self, populated=True, baseline=False)
        db = Path(env["DZI_DB"])
        before = _sha256(db)
        result = _run_python(FAILING_STARTUP_PROBE, env)
        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, 3, output)
        self.assertIn("pending migrations: 000_baseline.sql", output)
        self.assertIn("src.manage_migrations", output)
        self.assertIn("db unchanged: True", output)
        self.assertEqual(_sha256(db), before)


CONFIG_PROBE = r'''
import json, os, sqlite3, sys
from pathlib import Path
real = (Path.cwd() / "data/questions.db").resolve()
connect = sqlite3.connect
def guarded(database, *args, **kwargs):
    raw = os.fspath(database)
    path = raw.split("?", 1)[0].removeprefix("file://") if raw.startswith("file:") else raw
    if Path(path).resolve() == real:
        raise AssertionError("connected to the repository database")
    return connect(database, *args, **kwargs)
sqlite3.connect = guarded
from web import app as web_app
tables = None
if os.environ.get("DZI_DB"):
    with connect(os.environ["DZI_DB"]) as conn:
        tables = sorted(r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'"))
print(json.dumps({
    "db": web_app.app.config["DB_PATH"], "db_global": str(web_app.DB_PATH),
    "quiz_db": str(web_app.QUIZ_DB_PATH),
    "uploads": web_app.app.config["PRACTICAL_UPLOAD_ROOT"], "tables": tables,
    "reference_root": web_app.app.config["PRACTICAL_REFERENCE_ROOT"]
    and str(web_app.app.config["PRACTICAL_REFERENCE_ROOT"]),
    "resource_roots": [str(root) for root in web_app.app.config["PRACTICAL_RESOURCE_ROOTS"]],
    "public_base_url": web_app.app.config.get("PUBLIC_BASE_URL", "unset"),
}))
'''


class DatabaseAndStartupSchemaConfigTest(unittest.TestCase):
    def probe(self, env):
        result = _run_python(CONFIG_PROBE, {"DZI_SECRET_KEY": SECRET_KEY, **env})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return json.loads(result.stdout.strip().splitlines()[-1])

    def test_default_paths_point_browsing_and_quiz_at_the_same_database(self):
        report = self.probe({})
        expected = str(ROOT / "data" / "questions.db")
        self.assertEqual((report["db"], report["db_global"], report["quiz_db"]),
                         (expected, expected, expected))
        self.assertEqual(report["uploads"], str(ROOT / "data" / "uploads" / "practical"))

    def test_dzi_db_and_upload_root_configure_one_database_and_upload_dir(self):
        env = _state_dir(self, populated=False)
        report = self.probe({"DZI_DB": env["DZI_DB"], "DZI_UPLOAD_ROOT": env["DZI_UPLOAD_ROOT"]})
        self.assertEqual((report["db"], report["db_global"], report["quiz_db"]),
                         (env["DZI_DB"],) * 3)
        self.assertEqual(report["uploads"], env["DZI_UPLOAD_ROOT"])

    def test_public_base_url_is_optional_normalized_and_never_guessed(self):
        self.assertEqual(self.probe({})["public_base_url"], "unset")
        self.assertEqual(self.probe({"DZI_PUBLIC_BASE_URL": "HTTPS://Abc-Def.trycloudflare.com/"})["public_base_url"],
                         "https://abc-def.trycloudflare.com")
        # Malformed: configured as None, so links are refused, not built from the request.
        for value in ("abc.trycloudflare.com", "https://abc.trycloudflare.com/app", "ftp://host"):
            with self.subTest(value=value):
                self.assertIsNone(self.probe({"DZI_PUBLIC_BASE_URL": value})["public_base_url"])

    def test_reference_root_defaults_to_the_checkout_and_follows_the_setting(self):
        assets = str(ROOT / "data" / "assets")
        report = self.probe({})
        self.assertIsNone(report["reference_root"])
        self.assertEqual(report["resource_roots"], [str(ROOT / "data" / "reference"), assets])

        env = _state_dir(self, populated=False)
        report = self.probe({"DZI_DB": env["DZI_DB"], "DZI_REFERENCE_ROOT": env["DZI_VAULT"]})
        self.assertEqual(report["reference_root"], env["DZI_VAULT"])
        self.assertEqual(report["resource_roots"], [env["DZI_VAULT"], assets])
        self.assertEqual(report["uploads"], str(ROOT / "data" / "uploads" / "practical"))

    def test_import_does_not_change_an_empty_database(self):
        env = _state_dir(self, populated=False)
        before = _sha256(Path(env["DZI_DB"]))
        report = self.probe({"DZI_DB": env["DZI_DB"]})
        self.assertEqual(report["tables"], [])
        self.assertEqual(_sha256(Path(env["DZI_DB"])), before)


SCHEMA_STATE_PROBE = r'''
import json, os, sqlite3, sys
from web import app as web_app
db = os.environ["DZI_DB"]
client = web_app.app.test_client()
statuses = [client.get(path).status_code for path in ("/", "/healthz")]
with sqlite3.connect(db) as conn:
    version = conn.execute("PRAGMA schema_version").fetchone()[0]
print(json.dumps({"statuses": statuses, "schema_version": version}))
'''


class WebAppNeverChangesSchemaTest(unittest.TestCase):
    """Importing web.app and serving requests leave the database schema alone."""

    def schema_version(self, db):
        with sqlite3.connect(db) as conn:
            return conn.execute("PRAGMA schema_version").fetchone()[0]

    def schema_fingerprint(self, db):
        """Every schema object plus the schema cookie."""
        with sqlite3.connect(db) as conn:
            objects = conn.execute(
                "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name"
            ).fetchall()
            return objects, conn.execute("PRAGMA schema_version").fetchone()[0]

    def probe(self, env):
        result = _run_python(SCHEMA_STATE_PROBE, {"DZI_SECRET_KEY": SECRET_KEY, **env})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return json.loads(result.stdout.strip().splitlines()[-1]), result.stderr

    def migrated_copy(self):
        env = _state_dir(self, populated=True)
        return env["DZI_DB"], env

    def test_import_leaves_a_prepared_database_byte_identical(self):
        db, env = self.migrated_copy()
        before = _sha256(Path(db))
        result = _run_python("from web import app as web_app\nprint('imported')",
                             {"DZI_SECRET_KEY": SECRET_KEY, "DZI_DB": db})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(_sha256(Path(db)), before)
        self.assertNotIn("pending migrations", result.stderr)

    def test_requests_leave_a_prepared_database_schema_unchanged(self):
        db, env = self.migrated_copy()
        before = self.schema_fingerprint(db)
        report, stderr = self.probe({"DZI_DB": db})
        self.assertEqual(report["statuses"][0], 200)
        self.assertEqual(self.schema_fingerprint(db), before)
        self.assertNotIn("pending migrations", stderr)

    def test_requests_leave_a_prepared_database_byte_identical(self):
        # No request connection changes journal mode any more (web.db), so even the
        # header of a rollback-journal copy stays as it was.
        db, env = self.migrated_copy()
        before = _sha256(Path(db))
        report, stderr = self.probe({"DZI_DB": db})
        self.assertEqual(report["statuses"], [200, 200])
        self.assertEqual(_sha256(Path(db)), before)
        self.assertEqual([p.name for p in Path(db).parent.glob("questions.db*")], ["questions.db"])

    def test_pending_migration_is_warned_about_not_applied(self):
        db, env = self.migrated_copy()
        with sqlite3.connect(db) as conn:
            conn.execute("DELETE FROM schema_migrations WHERE filename = ?",
                         ("010_topic_section_display_order.sql",))
        before = self.schema_fingerprint(db)
        report, stderr = self.probe({"DZI_DB": db})
        self.assertEqual(self.schema_fingerprint(db), before)
        self.assertEqual(report["schema_version"], before[1])
        self.assertIn("010_topic_section_display_order.sql", stderr)
        self.assertIn("src.manage_migrations", stderr)
        with sqlite3.connect(db) as conn:
            applied = {r[0] for r in conn.execute("SELECT filename FROM schema_migrations")}
        self.assertNotIn("010_topic_section_display_order.sql", applied)

    def test_unmigrated_database_is_not_given_quiz_tables(self):
        env = _state_dir(self, populated=False)
        db = env["DZI_DB"]
        sqlite3.connect(db).close()
        before = (_sha256(Path(db)), self.schema_version(db))
        result = _run_python("from web import app as web_app\nprint('imported')",
                             {"DZI_SECRET_KEY": SECRET_KEY, "DZI_DB": db})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((_sha256(Path(db)), self.schema_version(db)), before)
        with sqlite3.connect(db) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM sqlite_master").fetchone()[0], 0)

    def test_explicit_migration_tool_still_applies_pending_migrations(self):
        db, env = self.migrated_copy()
        with sqlite3.connect(db) as conn:
            conn.execute("DROP TABLE quiz_answers")
            conn.execute("DELETE FROM schema_migrations WHERE filename = ?",
                         ("001_quiz_tables.sql",))
        pending = production.pending_schema_migrations(
            Path(db), web_app.open_existing_database_read_only)
        self.assertIn("001_quiz_tables.sql", pending)
        result = subprocess.run(
            [sys.executable, "-m", "src.manage_migrations", "--db", db, "--apply"],
            cwd=ROOT, capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(production.pending_schema_migrations(
            Path(db), web_app.open_existing_database_read_only), [])

    def test_no_schema_statements_or_sql_files_in_web_code(self):
        offenders = []
        for path in sorted((ROOT / "web").rglob("*.py")):
            source = path.read_text(encoding="utf-8")
            if re.search(r"\b(?:CREATE|ALTER|DROP)\s+(?:TABLE|INDEX|TRIGGER|VIEW)\b", source, re.I) \
                    or "executescript(" in source or ".sql" in source:
                offenders.append(str(path.relative_to(ROOT)))
        self.assertEqual(offenders, [])
        self.assertFalse(hasattr(web_app, "quiz_apply_migration"))
        self.assertFalse(hasattr(web_app, "SCHEMA_CHANGES_ON_DEMAND"))


class HealthEndpointTest(IsolatedAppTestCase):
    def setUp(self):
        self.client = web_app.app.test_client()

    def test_anonymous_health_check_is_plain_and_uncached(self):
        response = self.client.get("/healthz")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_data(as_text=True), "ok\n")
        self.assertEqual(response.mimetype, "text/plain")
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertNotIn("Set-Cookie", response.headers)
        self.assertEqual(self.client.head("/healthz").status_code, 200)
        self.assertEqual(self.client.post("/healthz").status_code, 405)

    def test_health_check_is_cheap_and_renders_nothing(self):
        statements, connections = [], []
        real_connect = sqlite3.connect

        def counting_connect(*args, **kwargs):
            conn = real_connect(*args, **kwargs)
            connections.append(args[0])
            conn.set_trace_callback(statements.append)
            return conn

        with patch.object(web_app, "subject_catalog", side_effect=AssertionError("catalog")), \
                patch.object(web_app, "render_template", side_effect=AssertionError("template")), \
                patch.object(web_app.sqlite3, "connect", counting_connect):
            response = self.client.get("/healthz")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(connections), 1)
        # The two connection-local policy pragmas from web.db, the one header read,
        # then the single lifecycle clock row (a suspect clock reports "degraded").
        normalized = [" ".join(s.split()).upper() for s in statements]
        self.assertEqual(normalized[:3],
                         ["PRAGMA FOREIGN_KEYS=ON", "PRAGMA QUERY_ONLY=ON", "PRAGMA SCHEMA_VERSION"])
        self.assertEqual(len(normalized), 4)
        self.assertTrue(normalized[3].startswith("SELECT ID, HIGH_WATER_AT, STATE"), normalized[3])
        self.assertTrue(normalized[3].endswith("FROM LIFECYCLE_CLOCK"), normalized[3])

    def test_missing_or_corrupt_database_reports_unavailable_without_details(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        missing = Path(tmp.name) / "missing.db"
        corrupt = Path(tmp.name) / "corrupt.db"
        corrupt.write_bytes(b"not a sqlite database" * 64)
        for path in (missing, corrupt):
            with self.subTest(path=path.name):
                web_app.app.config["DB_PATH"] = str(path)
                with self.assertLogs(web_app.app.logger, level="WARNING"):
                    response = self.client.get("/healthz")
                self.assertEqual(response.status_code, 503)
                body = response.get_data(as_text=True)
                self.assertEqual(body, "unavailable\n")
                self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertFalse(missing.exists())


class DeploymentTemplateTest(unittest.TestCase):
    def read(self, relative):
        return (ROOT / relative).read_text(encoding="utf-8")

    def test_gunicorn_dependency_is_added_without_touching_existing_pins(self):
        lines = self.read("requirements.txt").splitlines()
        self.assertIn("Flask>=3.1,<4", lines)
        self.assertIn("Werkzeug>=3.1,<4", lines)
        self.assertIn("gunicorn>=23,<24", lines)

    def test_gunicorn_configuration_matches_initial_vps_plan(self):
        config = runpy.run_path(str(DEPLOY / "gunicorn.conf.py"))
        expected = {
            "bind": "127.0.0.1:8000", "workers": 1, "worker_class": "sync", "threads": 1,
            "timeout": 30, "wsgi_app": "web.wsgi:app", "daemon": False, "reload": False,
            "accesslog": "-", "errorlog": "-", "forwarded_allow_ips": "127.0.0.1",
            "chdir": str(ROOT),
        }
        for key, value in expected.items():
            with self.subTest(key=key):
                self.assertEqual(config[key], value)

    def test_systemd_unit_runs_unprivileged_with_external_secrets(self):
        unit = self.read("deploy/systemd/learnpilot.service")
        for line in ("User=learnpilot", "EnvironmentFile=/etc/learnpilot/learnpilot.env",
                     "Restart=on-failure", "WorkingDirectory=/opt/learnpilot/app",
                     "ReadWritePaths=/var/lib/learnpilot", "Type=notify"):
            self.assertIn(line, unit.splitlines())
        self.assertRegex(unit, r"ExecStart=/opt/learnpilot/venv/bin/gunicorn --config \S+gunicorn\.conf\.py")
        self.assertNotRegex(unit, r"(?m)^User=root")
        self.assertNotRegex(unit, r"(?m)^Environment=.*DZI_")

    def test_nginx_template_proxies_loopback_and_matches_upload_limit(self):
        conf = self.read("deploy/nginx/learnpilot.conf")
        self.assertEqual(set(re.findall(r"proxy_pass\s+(\S+);", conf)), {"http://127.0.0.1:8000"})
        self.assertIn("server_name learnpilot.example.com;", conf)
        self.assertNotRegex(conf, r"listen\s+\S*443|ssl_certificate")
        self.assertIn("proxy_set_header X-Forwarded-For $remote_addr;", conf)
        self.assertIn("proxy_set_header X-Forwarded-Proto $scheme;", conf)
        # The proxy limit sits just above the application's request-wide limit.
        limit = int(re.search(r"client_max_body_size\s+(\d+)m;", conf).group(1)) * 1024 * 1024
        self.assertEqual(web_app.app.config["MAX_CONTENT_LENGTH"], MAX_REQUEST_BYTES)
        self.assertGreater(limit, MAX_REQUEST_BYTES)
        self.assertLessEqual(limit, MAX_REQUEST_BYTES + 2 * 1024 * 1024)
        self.assertGreaterEqual(MAX_REQUEST_BYTES, 4 * MAX_UPLOAD_BYTES)
        static_alias = re.search(r"location /static/ \{\s+alias (\S+);", conf).group(1)
        self.assertTrue(static_alias.endswith("/web/static/"))
        self.assertEqual(Path(web_app.app.static_folder).resolve(), ROOT / "web" / "static")

    def test_nginx_limits_only_targeted_posts_and_sets_security_headers(self):
        conf = self.read("deploy/nginx/learnpilot.conf")
        self.assertRegex(conf, r"map \$request_method \$learnpilot_post_client\s*\{\s*default \"\";\s*POST \$binary_remote_addr;")
        for zone, rate, burst in (# Account sign-in: classroom-sized, a class shares one NAT.
                                  ("lp_login", "1r/s", 40),
                                  ("lp_quiz_write", "20r/s", 90),
                                  ("lp_upload", "2r/s", 45)):
            self.assertIn(f"zone={zone}:1m rate={rate};", conf)
            self.assertIn(f"limit_req zone={zone} burst={burst} nodelay;", conf)
        # Exactly the zones above: the shared-password logins and their zones are gone.
        self.assertEqual(sorted(re.findall(r"limit_req_zone \S+ zone=(\w+):", conf)),
                         ["lp_login", "lp_quiz_write", "lp_upload"])
        self.assertEqual(sorted(set(re.findall(r"limit_req zone=(\w+)", conf))),
                         ["lp_login", "lp_quiz_write", "lp_upload"])
        for path in ("/login", "/login/totp"):
            self.assertIn(f"location = {path}", conf)
        for removed in ("/admin/login", "/tester/login", "lp_admin_login", "lp_tester_login"):
            self.assertNotIn(removed, conf)
        self.assertRegex(conf, r"location = /login \{\s+limit_req zone=lp_login burst=40 nodelay;")
        self.assertRegex(conf, r"location = /login/totp \{\s+limit_req zone=lp_login burst=40 nodelay;")
        self.assertEqual(len(re.findall(r"limit_req zone=lp_login\b", conf)), 2)
        self.assertIn("location ~ ^/quiz/[0-9]+$", conf)
        self.assertIn("location ~ ^/quiz/attempt/[0-9]+$", conf)
        self.assertIn("location ~ ^/quiz/attempt/[0-9]+/practical/[^/]+/task/[0-9]+/upload$", conf)
        self.assertNotRegex(conf, r"location /static/ \{[^}]*limit_req")
        for header in ("X-Content-Type-Options \"nosniff\" always",
                       "X-Frame-Options \"DENY\" always",
                       "Referrer-Policy \"strict-origin-when-cross-origin\" always"):
            self.assertIn(header, conf)
        self.assertIn("limit_req_status 429", conf)

    def test_backup_unit_has_only_state_paths_and_complete_snapshot(self):
        unit = self.read("deploy/systemd/learnpilot-backup.service")
        timer = self.read("deploy/systemd/learnpilot-backup.timer")
        backup_env = self.read("deploy/learnpilot-backup.env.example")
        self.assertIn("EnvironmentFile=/etc/learnpilot/backup.env", unit)
        self.assertIn("-m src.backup_application", unit)
        self.assertIn("ReadWritePaths=/var/lib/learnpilot /var/backups/learnpilot", unit)
        self.assertIn("UMask=0077", unit)
        self.assertIn("OnCalendar=*-*-* 03:15:00", timer)
        for secret in production.REQUIRED_SECRETS:
            self.assertNotIn(secret, backup_env + unit)
        for path in ("DZI_DB", "DZI_UPLOAD_ROOT", "DZI_VAULT"):
            self.assertIn(path, backup_env)
        # Reference data is provisioned separately; snapshots do not include it.
        self.assertNotIn("DZI_REFERENCE_ROOT", backup_env + unit)

    def test_guide_covers_first_deploy_backup_and_permissions(self):
        guide = self.read("docs/DEPLOYMENT.md")
        for expected in ("rsync", "--chmod=D755,F644", "root:root", "0600",
                         "1 vCPU, 2 GB RAM, 30 GB disk", "python -B -m unittest discover -s tests",
                         "python --version", "pip freeze", "snapshot-", "off-server",
                         "-mtime +14", "systemctl --failed",
                         "DZI_REFERENCE_ROOT=/var/lib/learnpilot/data/reference",
                         "only the database files are moved out of `/var/lib/learnpilot/data`, never the\ndirectory"):
            self.assertIn(expected, guide)
        # Reference data lives outside the checkout and survives a restore: the restore script
        # moves the database files, never the data directory they share with it.
        self.assertNotIn("/opt/learnpilot/app/data/reference", guide)
        self.assertNotIn("mv /var/lib/learnpilot/data /var", guide)
        restore = self.read("deploy/learnpilot-restore.sh")
        self.assertIn("for tree in uploads vault; do", restore)
        self.assertIn('sudo mv -f -- "$STAGED_DB" "$LIVE_DB"', restore)
        # What it moves out of the state directory: the two trees, and the database with its sidecars.
        moved = set(re.findall(r'sh -c "\$MOVE" sh "(\$[A-Z_]+[^"]*)" "\$REPLACED', restore))
        self.assertEqual(moved, {"$STATE/$tree", "$LIVE_DB$sidecar", "$LIVE_DB$part"})
        self.assertNotIn('"$STATE/data"', restore)
        self.assertNotIn("reference", restore)

    def test_templates_and_docs_contain_no_secret_values(self):
        env_example = self.read("deploy/learnpilot.env.example")
        for name in production.REQUIRED_SECRETS:
            self.assertRegex(env_example, rf"(?m)^{name}=$")
        for name in ("DZI_DB", "DZI_UPLOAD_ROOT", "DZI_VAULT"):
            self.assertRegex(env_example, rf"(?m)^{name}=/var/lib/learnpilot/")
        self.assertRegex(env_example, r"(?m)^DZI_REFERENCE_ROOT=/var/lib/learnpilot/data/reference$")
        texts = [self.read(str(p.relative_to(ROOT))) for p in DEPLOY.rglob("*") if p.is_file()
                 and "__pycache__" not in p.parts]
        texts.append(self.read("docs/DEPLOYMENT.md"))
        for text in texts:
            self.assertNotRegex(text, r"(?m)^\s*(DZI_SECRET_KEY|DZI_ADMIN_PASSWORD|DZI_TESTER_PASSWORD)=\S")
            self.assertNotIn("admin123", text)
            self.assertNotIn("tester123", text)

    def test_deployment_guide_covers_operations(self):
        guide = self.read("docs/DEPLOYMENT.md")
        for command in ("systemctl status learnpilot", "journalctl -u learnpilot",
                        "src.backup_sqlite", "src.manage_migrations", "certbot --nginx",
                        "/healthz", "ufw allow", "swapon"):
            self.assertIn(command, guide)


if __name__ == "__main__":
    unittest.main()
