"""Registration PR 4: build gates, production and development starts, Nginx (test plan K1 to K4).

A production start mutates process state, so it runs in a fresh interpreter against a
temporary database, as the Google gate tests do. Every mail setting is synthetic; no
start here sends a message (the only requests made are GETs and refused POSTs) and no
socket is opened.
"""

import logging
import re
import unittest
from unittest.mock import patch

from tests.support import ROOT
from tests.test_google_oidc_production import report_of
from tests.test_production_deployment import _run_python, _state_dir
from web import production, registration_settings
from web.services import mail
from web.services import registration as registration_service

HOST = "learnpilot.example.com"
SMTP_PASSWORD = "SYNTHETIC-SMTP-PASSWORD-never-printed"
VALID = {
    mail.BACKEND_ENV: "smtp",
    mail.FROM_ENV: "LearnPilot <no-reply@example.org>",
    mail.SMTP_HOST_ENV: "smtp.example.org",
    mail.SMTP_PORT_ENV: "587",
    mail.SMTP_SECURITY_ENV: "starttls",
    mail.SMTP_USERNAME_ENV: "synthetic-user",
    mail.SMTP_PASSWORD_ENV: SMTP_PASSWORD,
}
CONSOLE = {mail.BACKEND_ENV: "console", mail.FROM_ENV: "no-reply@example.org"}
MALFORMED = (
    {**VALID, mail.SMTP_PORT_ENV: "not-a-port"},
    {**VALID, mail.FROM_ENV: "not an address"},
    {**VALID, mail.SMTP_SECURITY_ENV: "none"},           # plain transport to a remote host
    {**VALID, mail.SMTP_USERNAME_ENV: ""},               # a password without a user name
    {mail.BACKEND_ENV: "smtp"},
    {mail.BACKEND_ENV: "carrier-pigeon", mail.FROM_ENV: "no-reply@example.org"},
)
WARNING_TEXT = "but this build does not enable registration"

GATE_PROBE = r'''
import json, logging, os, sys
from web.services import registration
if os.environ.get("PROBE_ACTIVATION") == "on":
    registration.PRODUCTION_ACTIVATION = True
try:
    from web.wsgi import app
except Exception as exc:
    print(json.dumps({"started": False, "error": type(exc).__name__, "message": str(exc)}))
    sys.exit(0)
from web.registration_settings import CONFIG_KEY
settings = app.config.get(CONFIG_KEY)
proxy = {"X-Forwarded-Proto": "https", "X-Forwarded-Host": "learnpilot.example.com",
         "X-Forwarded-For": "203.0.113.7"}
client = app.test_client()
login = client.get("/login", headers=proxy).get_data(as_text=True)
chooser = client.get("/register", headers=proxy)
report = {
    "started": True,
    "enabled": settings is not None,
    "mailer": type(settings.mailer).__name__ if settings is not None else None,
    "login_link": "/register" in login,
    "chooser": chooser.status_code,
    "logger": [logging.getLogger("learnpilot.registration").level,
               logging.getLogger("learnpilot.registration").propagate,
               len(logging.getLogger("learnpilot.registration").handlers)],
}
if settings is None:
    # Nothing can be sent while disabled: a same-origin submission is the generic 404.
    origin = {**proxy, "Origin": "https://learnpilot.example.com"}
    report["submit"] = client.post("/register/student", headers=origin, data={
        "first_name": "Ана", "last_name": "Иванова", "email": "ana@example.bg", "grade": "9",
        "password": "a long enough passphrase", "confirm_password": "a long enough passphrase"}).status_code
    report["verify"] = client.post("/register/verify", headers=origin, data={"code": "123456"}).status_code
print(json.dumps(report))
'''

DEVELOPMENT_PROBE = r'''
import json
from web.app import app
from web.registration_settings import CONFIG_KEY
settings = app.config.get(CONFIG_KEY)
print(json.dumps({"enabled": settings is not None,
                  "mailer": type(settings.mailer).__name__ if settings is not None else None}))
'''


class GateConstantTest(unittest.TestCase):
    def test_both_gates_are_off_and_set_only_in_the_service_module(self):
        self.assertIs(registration_service.PRODUCTION_ACTIVATION, False)
        self.assertIs(registration_service.TEACHER_ACTIVATION, False)
        source = (ROOT / "web/services/registration.py").read_text(encoding="utf-8")
        self.assertIn("\nPRODUCTION_ACTIVATION = False\n", source)
        self.assertIn("\nTEACHER_ACTIVATION = False\n", source)
        for needle in ("os.environ", "getenv", "import os"):
            self.assertNotIn(needle, source)
        for name in ("TEACHER_ACTIVATION",):
            found = []
            for directory in ("web", "src", "deploy"):
                for path in (ROOT / directory).rglob("*.py"):
                    found += [str(path.relative_to(ROOT))
                              for _ in re.findall(rf"\b{name}\s*=[^=]", path.read_text(encoding="utf-8"))]
            self.assertEqual(found, ["web/services/registration.py"])

    def test_off_mail_settings_never_refuse_a_start_and_enable_nothing(self):
        for environ in ({}, VALID, CONSOLE, *MALFORMED):
            with self.subTest(settings=sorted(environ)):
                self.assertEqual(production.registration_errors(environ), [])
                if environ:
                    with self.assertLogs("web.production", logging.WARNING) as logs:
                        self.assertIsNone(production.registration_production_settings(environ))
                    self.assertEqual(len(logs.output), 1)
                    self.assertIn(WARNING_TEXT, logs.output[0])
                    for name, value in environ.items():
                        if value:
                            self.assertIn(name, logs.output[0])
                            self.assertNotIn(value, logs.output[0])
                else:
                    with self.assertNoLogs("web.production", logging.DEBUG):
                        self.assertIsNone(production.registration_production_settings(environ))

    def test_on_valid_settings_enable_it_and_bad_ones_are_named_startup_errors(self):
        with patch.object(registration_service, "PRODUCTION_ACTIVATION", True):
            self.assertEqual(production.registration_errors(VALID), [])
            settings = production.registration_production_settings(VALID)
            self.assertIsInstance(settings.mailer, mail.SmtpMailer)
            self.assertEqual(settings.total_cap, 300)
            # No backend: disabled, which is not an error.
            self.assertEqual(production.registration_errors({}), [])
            self.assertIsNone(production.registration_production_settings({}))
            for environ in (CONSOLE, {**CONSOLE, mail.BACKEND_ENV: "file"}, *MALFORMED):
                with self.subTest(settings=sorted(environ)):
                    errors = production.registration_errors(environ)
                    self.assertTrue(errors)
                    text = "\n".join(errors)
                    self.assertRegex(text, r"DZI_(MAIL|SMTP)_[A-Z_]+")
                    self.assertNotIn(SMTP_PASSWORD, text)
                    self.assertTrue(set(errors) <= set(production.production_config_errors(environ)))
            self.assertIn("console", "\n".join(production.registration_errors(CONSOLE)))


class ProductionStartTest(unittest.TestCase):
    def start(self, settings, activation="off"):
        env = {**_state_dir(self, populated=True), **settings, "PROBE_ACTIVATION": activation}
        result = _run_python(GATE_PROBE, env)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn(SMTP_PASSWORD, result.stdout + result.stderr)
        return report_of(result), result.stderr

    def test_the_build_starts_normally_with_registration_unreachable(self):
        for name, settings in {"none": {}, "valid": VALID, "console": CONSOLE, "malformed": MALFORMED[0]}.items():
            with self.subTest(settings=name):
                report, stderr = self.start(settings)
                self.assertEqual((report["started"], report["enabled"], report["login_link"]), (True, False, False))
                self.assertEqual((report["chooser"], report["submit"], report["verify"]), (404, 404, 404))
                warnings = [line for line in stderr.splitlines() if WARNING_TEXT in line]
                self.assertEqual(len(warnings), 0 if name == "none" else 1)
                self.assertNotIn("Registration is disabled", stderr)  # the development warning
                self.assertEqual(report["logger"], [logging.INFO, False, 1])

    def test_with_the_gate_patched_on_valid_settings_enable_it(self):
        report, stderr = self.start(VALID, activation="on")
        self.assertEqual((report["started"], report["enabled"], report["mailer"]), (True, True, "SmtpMailer"))
        self.assertEqual((report["chooser"], report["login_link"]), (200, True))
        self.assertNotIn(WARNING_TEXT, stderr)

    def test_with_the_gate_patched_on_a_development_backend_or_bad_settings_refuse_to_start(self):
        for settings in (CONSOLE, MALFORMED[0]):
            with self.subTest(settings=sorted(settings)):
                report, _ = self.start(settings, activation="on")
                self.assertEqual((report["started"], report["error"]), (False, "ProductionConfigError"))
                self.assertIn("Refusing to start LearnPilot in production", report["message"])
                self.assertRegex(report["message"], r"DZI_(MAIL|SMTP)_[A-Z_]+")


class DevelopmentStartTest(unittest.TestCase):
    def start(self, settings):
        result = _run_python(DEVELOPMENT_PROBE, {"DZI_SECRET_KEY": "k" * 40, "DZI_DB": ":memory:", **settings})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn(SMTP_PASSWORD, result.stdout + result.stderr)
        return report_of(result), result.stderr

    def test_usable_settings_enable_it_in_development_only(self):
        self.assertEqual(self.start(CONSOLE)[0], {"enabled": True, "mailer": "ConsoleMailer"})
        self.assertEqual(self.start(VALID)[0], {"enabled": True, "mailer": "SmtpMailer"})

    def test_without_settings_it_is_disabled_and_nothing_is_said(self):
        report, stderr = self.start({})
        self.assertEqual(report, {"enabled": False, "mailer": None})
        self.assertNotIn("Registration", stderr)

    def test_unusable_settings_warn_by_name_and_leave_it_disabled(self):
        report, stderr = self.start(MALFORMED[0])
        self.assertEqual(report["enabled"], False)
        self.assertIn("Registration is disabled", stderr)
        self.assertIn(mail.SMTP_PORT_ENV, stderr)
        self.assertNotIn("not-a-port", stderr)


class SettingsModuleTest(unittest.TestCase):
    def test_configured_names_are_names_only(self):
        self.assertEqual(registration_settings.configured_names({}), [])
        names = registration_settings.configured_names(VALID)
        self.assertEqual(set(names), set(VALID))
        self.assertNotIn(SMTP_PASSWORD, repr(names))
        self.assertNotIn(SMTP_PASSWORD, repr(registration_settings.from_environment(VALID, production=False)))


class NginxTemplateTest(unittest.TestCase):
    """K4, statically: a template change is not evidence that a real Nginx accepts it."""

    def setUp(self):
        self.text = (ROOT / "deploy/nginx/learnpilot.conf").read_text(encoding="utf-8")

    def location(self, path):
        match = re.search(r"location = " + re.escape(path) + r" \{(.*?)\n    \}", self.text, re.S)
        self.assertIsNotNone(match, path)
        return match.group(1)

    def test_the_register_zone_is_keyed_like_the_login_zone(self):
        self.assertIn("limit_req_zone $learnpilot_post_client zone=lp_register:1m rate=2r/m;", self.text)
        self.assertIn("limit_req_zone $learnpilot_post_client zone=lp_login:1m rate=1r/s;", self.text)

    def test_submissions_and_resends_use_it_and_the_code_form_uses_the_sign_in_budget(self):
        for path in ("/register/student", "/register/teacher", "/register/resend"):
            block = self.location(path)
            self.assertIn("limit_req zone=lp_register burst=90 nodelay;", block)
            self.assertIn("proxy_pass http://127.0.0.1:8000;", block)
        self.assertIn("limit_req zone=lp_login burst=40 nodelay;", self.location("/register/verify"))

    def test_sign_in_and_google_protections_are_unchanged(self):
        for path, zone in (("/login", "lp_login burst=40"), ("/login/totp", "lp_login burst=40"),
                           ("/auth/google/start", "lp_oidc_start burst=40"),
                           ("/auth/google/link/confirm", "lp_login burst=40"),
                           ("/auth/google/callback", "lp_oidc_callback burst=40")):
            self.assertIn(f"limit_req zone={zone} nodelay;", self.location(path))
        self.assertIn("proxy_set_header X-Forwarded-For $remote_addr;", self.text)


if __name__ == "__main__":
    unittest.main()
