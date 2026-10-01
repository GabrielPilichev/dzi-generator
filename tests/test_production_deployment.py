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
from tests.support import ROOT, IsolatedAppTestCase, copy_seed_database, web_app
from web import production


SECRET_KEY = "test-only-secret-key-" + "k" * 32
ADMIN_PASSWORD = "test-only-admin-passphrase"
TESTER_PASSWORD = "test-only-tester-passphrase"
DEPLOY = ROOT / "deploy"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _state_dir(test: unittest.TestCase, *, seed: bool) -> dict:
    tmp = tempfile.TemporaryDirectory(prefix="learnpilot-prod-")
    test.addCleanup(tmp.cleanup)
    root = Path(tmp.name).resolve()
    db = root / "questions.db"
    if seed:
        copy_seed_database(ROOT / "data/questions.db", db)
    else:
        db.write_bytes(b"")
    (root / "uploads").mkdir()
    (root / "vault").mkdir()
    return {
        "DZI_SECRET_KEY": SECRET_KEY,
        "DZI_ADMIN_PASSWORD": ADMIN_PASSWORD,
        "DZI_TESTER_PASSWORD": TESTER_PASSWORD,
        "DZI_DB": str(db),
        "DZI_UPLOAD_ROOT": str(root / "uploads"),
        "DZI_VAULT": str(root / "vault"),
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
        self.env = _state_dir(self, seed=False)

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

    def test_weak_local_only_or_shared_credentials_are_rejected(self):
        cases = {
            "short secret key": {"DZI_SECRET_KEY": "too-short"},
            "local admin password": {"DZI_ADMIN_PASSWORD": "admin123"},
            "local tester password": {"DZI_TESTER_PASSWORD": "tester123"},
            "short password": {"DZI_ADMIN_PASSWORD": "short-pw"},
            "shared password": {"DZI_TESTER_PASSWORD": ADMIN_PASSWORD},
        }
        for label, changes in cases.items():
            with self.subTest(label):
                errors = self.errors(**changes)
                self.assertTrue(errors, label)
                self.assertNotIn(next(iter(changes.values())), "\n".join(errors))

    def test_insecure_cookies_and_debug_are_rejected(self):
        for value in ("0", "false", "off", "maybe"):
            with self.subTest(cookie=value):
                self.assertIn("DZI_SESSION_COOKIE_SECURE must be unset or true in production",
                              self.errors(DZI_SESSION_COOKIE_SECURE=value))
        for value in ("1", "true", "on", "yes"):
            with self.subTest(debug=value):
                self.assertIn("FLASK_DEBUG must not be enabled in production",
                              self.errors(FLASK_DEBUG=value))

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
wrong = client.post("/tester/login", data={"password": "wrong"}, headers={**proxy, **origin})
forged = client.post("/tester/login", data={"password": "wrong"},
                     headers={**proxy, "Origin": "https://evil.example"})
unproxied = client.post("/tester/login", data={"password": "wrong"}, headers=origin)
login = app.test_client().post("/tester/login", data={"password": os.environ["DZI_TESTER_PASSWORD"]},
                               headers={**proxy, **origin})
wsgi = app.wsgi_app
print(json.dumps({
    "unchanged_by_import": before == after_import,
    "debug": app.debug, "testing": app.testing,
    "cookie_secure": app.config["SESSION_COOKIE_SECURE"],
    "scheme": app.config["PREFERRED_URL_SCHEME"],
    "db": app.config["DB_PATH"], "quiz_db": str(web_app.QUIZ_DB_PATH),
    "uploads": app.config["PRACTICAL_UPLOAD_ROOT"], "vault": str(web_app.QUIZ_VAULT_PATH),
    "skip_migration": os.environ.get("DZI_SKIP_STARTUP_MIGRATION"),
    "schema_on_demand": web_app.SCHEMA_CHANGES_ON_DEMAND,
    "max_content_length": app.config["MAX_CONTENT_LENGTH"],
    "proxy": isinstance(wsgi, ProxyFix) and [wsgi.x_for, wsgi.x_proto, wsgi.x_host, wsgi.x_port, wsgi.x_prefix],
    "health": [health.status_code, health.get_data(as_text=True)],
    "wrong_password": wrong.status_code, "forged_origin": forged.status_code,
    "unproxied": unproxied.status_code,
    "login": [login.status_code, login.headers.get("Set-Cookie", "")],
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
        env = _state_dir(self, seed=True)
        result = _run_python(PRODUCTION_PROBE, env)
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
        self.assertEqual(report["skip_migration"], "1")
        self.assertFalse(report["schema_on_demand"])
        self.assertEqual(report["max_content_length"], MAX_REQUEST_BYTES)
        self.assertEqual(report["proxy"], [1, 1, 1, 0, 0])
        self.assertEqual(report["health"], [200, "ok\n"])
        # Same-origin POST guard sees the public HTTPS origin only via the proxy.
        self.assertEqual(report["wrong_password"], 200)
        self.assertEqual(report["forged_origin"], 403)
        self.assertEqual(report["unproxied"], 403)
        status, cookie = report["login"]
        self.assertEqual(status, 302)
        for attribute in ("Secure", "HttpOnly", "SameSite=Strict"):
            self.assertIn(attribute, cookie)
        for secret in (SECRET_KEY, ADMIN_PASSWORD, TESTER_PASSWORD):
            self.assertNotIn(secret, result.stdout + result.stderr)

    def test_missing_secret_stops_before_the_app_is_imported(self):
        env = _state_dir(self, seed=True)
        del env["DZI_ADMIN_PASSWORD"]
        result = _run_python(FAILING_STARTUP_PROBE, env)
        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, 3, output)
        self.assertIn("ProductionConfigError", output)
        self.assertIn("Refusing to start LearnPilot in production", output)
        self.assertIn("DZI_ADMIN_PASSWORD is not set", output)
        self.assertIn("app imported: False", output)
        self.assertIn("db unchanged: True", output)
        for secret in (SECRET_KEY, TESTER_PASSWORD):
            self.assertNotIn(secret, output)

    def test_pending_migration_stops_startup_without_changing_schema(self):
        env = _state_dir(self, seed=True)
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
}))
'''


class DatabaseAndStartupSchemaConfigTest(unittest.TestCase):
    def probe(self, env):
        result = _run_python(CONFIG_PROBE, {"DZI_SECRET_KEY": SECRET_KEY, **env})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return json.loads(result.stdout.strip().splitlines()[-1])

    def test_default_paths_point_browsing_and_quiz_at_the_same_seed(self):
        report = self.probe({"DZI_SKIP_STARTUP_MIGRATION": "1"})
        expected = str(ROOT / "data" / "questions.db")
        self.assertEqual((report["db"], report["db_global"], report["quiz_db"]),
                         (expected, expected, expected))
        self.assertEqual(report["uploads"], str(ROOT / "data" / "uploads" / "practical"))

    def test_dzi_db_and_upload_root_configure_one_database_and_upload_dir(self):
        env = _state_dir(self, seed=False)
        report = self.probe({"DZI_DB": env["DZI_DB"], "DZI_UPLOAD_ROOT": env["DZI_UPLOAD_ROOT"],
                             "DZI_SKIP_STARTUP_MIGRATION": "1"})
        self.assertEqual((report["db"], report["db_global"], report["quiz_db"]),
                         (env["DZI_DB"],) * 3)
        self.assertEqual(report["uploads"], env["DZI_UPLOAD_ROOT"])

    def test_development_import_keeps_startup_quiz_migration(self):
        env = _state_dir(self, seed=False)
        report = self.probe({"DZI_DB": env["DZI_DB"]})
        self.assertTrue({"quiz_assignments", "quiz_attempts", "quiz_answers"} <= set(report["tables"]))

    def test_skip_flag_prevents_any_schema_change_on_import(self):
        env = _state_dir(self, seed=False)
        before = _sha256(Path(env["DZI_DB"]))
        report = self.probe({"DZI_DB": env["DZI_DB"], "DZI_SKIP_STARTUP_MIGRATION": "1"})
        self.assertEqual(report["tables"], [])
        self.assertEqual(_sha256(Path(env["DZI_DB"])), before)


class RequestPathSchemaPolicyTest(unittest.TestCase):
    """The teacher-review column guard is the only DDL reachable from a request."""

    def legacy_db(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        self.addCleanup(conn.close)
        conn.execute("CREATE TABLE quiz_text_answers (id INTEGER PRIMARY KEY, raw_answer TEXT)")
        return conn

    def columns(self, conn):
        return {row["name"] for row in conn.execute("PRAGMA table_info(quiz_text_answers)")}

    def test_production_runs_no_sql_at_all_from_the_column_guard(self):
        conn = self.legacy_db()
        statements = []
        conn.set_trace_callback(statements.append)
        with patch.object(web_app, "SCHEMA_CHANGES_ON_DEMAND", False):
            web_app.quiz_ensure_text_answer_teacher_status_column(conn)
        conn.set_trace_callback(None)
        self.assertEqual(statements, [])
        self.assertNotIn("teacher_override_status", self.columns(conn))

    def test_development_keeps_the_self_healing_column_guard(self):
        conn = self.legacy_db()
        with patch.object(web_app, "SCHEMA_CHANGES_ON_DEMAND", True):
            web_app.quiz_ensure_text_answer_teacher_status_column(conn)
            web_app.quiz_ensure_text_answer_teacher_status_column(conn)  # idempotent
        self.assertIn("teacher_override_status", self.columns(conn))

    def test_no_other_schema_statements_exist_in_request_code(self):
        source = (ROOT / "web" / "app.py").read_text(encoding="utf-8")
        blueprint = (ROOT / "web" / "blueprints" / "quiz.py").read_text(encoding="utf-8")
        ddl = re.findall(r"\b(?:CREATE|ALTER|DROP)\s+(?:TABLE|INDEX|TRIGGER|VIEW)\b", source + blueprint)
        self.assertEqual(ddl, ["ALTER TABLE"])
        self.assertEqual(source.count("executescript("), 1)  # quiz_apply_migration only
        self.assertEqual(len(re.findall(r"(?m)^\s*quiz_apply_migration\(\)$", source)), 1)


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
        self.assertLessEqual(len(statements), 2)
        self.assertTrue(all(s.lstrip().upper().startswith("PRAGMA") for s in statements), statements)

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
                     "Restart=on-failure", "WorkingDirectory=/srv/learnpilot/app",
                     "ReadWritePaths=/var/lib/learnpilot", "Type=notify"):
            self.assertIn(line, unit.splitlines())
        self.assertRegex(unit, r"ExecStart=/srv/learnpilot/venv/bin/gunicorn --config \S+gunicorn\.conf\.py")
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

    def test_templates_and_docs_contain_no_secret_values(self):
        env_example = self.read("deploy/learnpilot.env.example")
        for name in production.REQUIRED_SECRETS:
            self.assertRegex(env_example, rf"(?m)^{name}=$")
        for name in ("DZI_DB", "DZI_UPLOAD_ROOT", "DZI_VAULT"):
            self.assertRegex(env_example, rf"(?m)^{name}=/var/lib/learnpilot/")
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
