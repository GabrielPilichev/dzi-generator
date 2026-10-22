"""Password recovery 8.3: the build gate, the settings, production and development starts, the other
gates, and the Nginx template (test plan RP1 to RP6).

The activation gate of the build: ``recovery.PRODUCTION_ACTIVATION`` is False and nothing overrides
it. A production start mutates process state, so it runs in a fresh interpreter against a temporary
database, as the registration and Google gate tests do. Every mail setting is synthetic; no start
here sends a message and no socket is opened. The Nginx tests are **static**: a template change is
not evidence that a real Nginx accepts it, and no ``nginx`` binary was available (activation
evidence, design 9).
"""

import itertools
import logging
import re
import unittest
from unittest.mock import patch

from tests.recovery_route_support import RecoveryRouteTestCase
from tests.support import ROOT
from tests.test_google_oidc_production import report_of
from tests.test_production_deployment import _run_python, _state_dir
from tests.test_registration_production import CONSOLE, MALFORMED, SMTP_PASSWORD, VALID, NginxTemplateTest
from web import production, recovery_settings
from web.recovery_settings import CONFIG_KEY, RecoverySettings
from web.registration_settings import CONFIG_KEY as REGISTRATION_KEY, RegistrationSettings
from web.services import mail
from web.services import profiles as profiles_service
from web.services import recovery as recovery_service
from web.services import registration as registration_service
from web.services.mail import InMemoryMailer

WARNING_TEXT = "but this build does not enable password recovery"

GATE_PROBE = r'''
import json, logging, os, sys
from web.services import recovery
if os.environ.get("PROBE_ACTIVATION") == "on":
    recovery.PRODUCTION_ACTIVATION = True
try:
    from web.wsgi import app
except Exception as exc:
    print(json.dumps({"started": False, "error": type(exc).__name__, "message": str(exc)}))
    sys.exit(0)
from web.recovery_settings import CONFIG_KEY
from web.registration_settings import CONFIG_KEY as REGISTRATION_KEY
settings = app.config.get(CONFIG_KEY)
proxy = {"X-Forwarded-Proto": "https", "X-Forwarded-Host": "learnpilot.example.com",
         "X-Forwarded-For": "203.0.113.7"}
origin = {**proxy, "Origin": "https://learnpilot.example.com"}
client = app.test_client()
login = client.get("/login", headers=proxy).get_data(as_text=True)
form = client.get("/recover", headers=proxy)
report = {
    "started": True,
    "enabled": settings is not None,
    "mailer": type(settings.mailer).__name__ if settings is not None else None,
    "registration_enabled": app.config.get(REGISTRATION_KEY) is not None,
    "login_link": "/recover" in login,
    "form": form.status_code,
    "logger": [logging.getLogger("learnpilot.recovery").level, logging.getLogger("learnpilot.recovery").propagate,
               len(logging.getLogger("learnpilot.recovery").handlers)],
}
if settings is None:
    # Nothing can be created or sent while disabled: a same-origin POST is the generic 404.
    report["request"] = client.post("/recover", headers=origin, data={"email": "ana@example.bg"}).status_code
    report["verify"] = client.get("/recover/verify", headers=proxy).status_code
    report["redeem"] = client.post("/recover/verify", headers=origin, data={
        "code": "12345678", "password": "a long enough passphrase",
        "confirm_password": "a long enough passphrase"}).status_code
    report["resend"] = client.post("/recover/resend", headers=origin).status_code
    report["cancel"] = client.post("/recover/cancel", headers=origin).status_code
print(json.dumps(report))
'''

DEVELOPMENT_PROBE = r'''
import json
from web.app import app
from web.recovery_settings import CONFIG_KEY
from web.registration_settings import CONFIG_KEY as REGISTRATION_KEY
settings = app.config.get(CONFIG_KEY)
registration = app.config.get(REGISTRATION_KEY)
print(json.dumps({"enabled": settings is not None,
                  "mailer": type(settings.mailer).__name__ if settings is not None else None,
                  "registration": registration is not None}))
'''


class GateConstantTest(unittest.TestCase):
    def test_the_gate_is_off_and_set_only_in_the_service_module(self):
        self.assertIs(recovery_service.PRODUCTION_ACTIVATION, False)
        source = (ROOT / "web/services/recovery.py").read_text(encoding="utf-8")
        self.assertIn("\nPRODUCTION_ACTIVATION = False\n", source)
        for needle in ("os.environ", "getenv", "import os"):
            self.assertNotIn(needle, source)
        # Nothing assigns it anywhere else: not a setting, not a module, not a deploy file.
        found = []
        for directory in ("web", "src", "deploy"):
            for path in (ROOT / directory).rglob("*.py"):
                text = path.read_text(encoding="utf-8")
                if re.search(r"\brecovery(?:_service)?\.PRODUCTION_ACTIVATION\s*=[^=]", text):
                    found.append(str(path.relative_to(ROOT)))
        self.assertEqual(found, [])

    def test_no_environment_variable_or_setting_names_the_recovery_gate(self):
        """No setting overrides the gate: the code reads the environment only for mail settings."""
        for path in [*(ROOT / "web").rglob("*.py"), *(ROOT / "src").rglob("*.py"), *(ROOT / "deploy").rglob("*")]:
            if path.is_file() and path.suffix in (".py", ".conf", ".sh", ".service", ".env", ".example", ""):
                text = path.read_text(encoding="utf-8", errors="ignore")
                self.assertNotRegex(text, r"(?i)DZI_RECOVER", str(path))
                self.assertNotRegex(text, r"\bRECOVERY_(?:ENABLED|ACTIVATION)\b", str(path))     # an environment-style name

    def test_the_settings_module_reads_nothing_by_itself(self):
        source = (ROOT / "web/recovery_settings.py").read_text(encoding="utf-8")
        for needle in ("os.environ", "getenv", "import os", "flask"):
            self.assertNotIn(needle, source)

    def test_off_mail_settings_never_refuse_a_start_and_enable_nothing(self):
        for environ in ({}, VALID, CONSOLE, *MALFORMED, {"DZI_RECOVERY_ENABLED": "1", **VALID}):
            with self.subTest(settings=sorted(environ)):
                self.assertEqual(production.recovery_errors(environ), [])
                if environ:
                    with self.assertLogs("web.production", logging.WARNING) as logs:
                        self.assertIsNone(production.recovery_production_settings(environ))
                    self.assertEqual(len(logs.output), 1)
                    self.assertIn(WARNING_TEXT, logs.output[0])
                    for name, value in environ.items():
                        if name in mail.SETTING_NAMES and value:
                            self.assertIn(name, logs.output[0])
                            self.assertNotIn(value, logs.output[0])
                else:
                    with self.assertNoLogs("web.production", logging.DEBUG):
                        self.assertIsNone(production.recovery_production_settings(environ))

    def test_on_valid_settings_enable_it_and_bad_ones_are_named_startup_errors_without_values(self):
        with patch.object(recovery_service, "PRODUCTION_ACTIVATION", True):
            self.assertEqual(production.recovery_errors(VALID), [])
            settings = production.recovery_production_settings(VALID)
            self.assertIsInstance(settings, RecoverySettings)
            self.assertIsInstance(settings.mailer, mail.SmtpMailer)
            self.assertEqual(settings.total_cap, 300)
            self.assertNotIn(SMTP_PASSWORD, repr(settings))
            # No backend: disabled, which is not an error.
            self.assertEqual(production.recovery_errors({}), [])
            self.assertIsNone(production.recovery_production_settings({}))
            for environ in (CONSOLE, {**CONSOLE, mail.BACKEND_ENV: "file"}, *MALFORMED):
                with self.subTest(settings=sorted(environ)):
                    errors = production.recovery_errors(environ)
                    self.assertTrue(errors)
                    text = "\n".join(errors)
                    self.assertRegex(text, r"DZI_(MAIL|SMTP)_[A-Z_]+")
                    self.assertNotIn(SMTP_PASSWORD, text)
                    self.assertTrue(set(errors) <= set(production.production_config_errors(environ)))
            self.assertIn("console", "\n".join(production.recovery_errors(CONSOLE)))

    def test_the_hourly_cap_comes_from_the_same_mail_settings_as_registrations(self):
        with patch.object(recovery_service, "PRODUCTION_ACTIVATION", True):
            settings = production.recovery_production_settings({**VALID, mail.HOURLY_CAP_ENV: "40"})
        self.assertEqual(settings.total_cap, 40)
        self.assertEqual(recovery_settings.from_environment({**VALID, mail.HOURLY_CAP_ENV: "40"},
                                                            production=False).total_cap, 40)

    def test_mail_problems_are_reported_once_while_either_gate_is_on(self):
        env = {**_ENV_BASE, **MALFORMED[0]}
        with patch.object(recovery_service, "PRODUCTION_ACTIVATION", True):
            only_recovery = [e for e in production.production_config_errors(env) if "DZI_SMTP" in e]
        with patch.object(registration_service, "PRODUCTION_ACTIVATION", True):
            only_registration = [e for e in production.production_config_errors(env) if "DZI_SMTP" in e]
        with patch.object(recovery_service, "PRODUCTION_ACTIVATION", True), \
                patch.object(registration_service, "PRODUCTION_ACTIVATION", True):
            both = [e for e in production.production_config_errors(env) if "DZI_SMTP" in e]
        self.assertTrue(only_recovery)
        self.assertEqual(only_recovery, only_registration)
        self.assertEqual(both, only_recovery)
        self.assertEqual([e for e in production.production_config_errors(env) if "DZI_SMTP" in e], [])

    def test_the_settings_object_hides_its_mailer(self):
        settings = RecoverySettings(mailer=InMemoryMailer(), total_cap=7)
        self.assertNotIn("InMemoryMailer", repr(settings))
        self.assertEqual(settings.total_cap, 7)


_ENV_BASE = {"DZI_SECRET_KEY": "k" * 40, "DZI_PUBLIC_HOST": "learnpilot.example.com"}


class ProductionStartTest(unittest.TestCase):
    def start(self, settings, activation="off"):
        env = {**_state_dir(self, populated=True), **settings, "PROBE_ACTIVATION": activation}
        result = _run_python(GATE_PROBE, env)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn(SMTP_PASSWORD, result.stdout + result.stderr)
        return report_of(result), result.stderr

    def test_the_build_starts_normally_with_recovery_unreachable(self):
        """RP1: with and without mail settings, and no environment variable enables it."""
        for name, settings in {"none": {}, "valid": VALID, "console": CONSOLE, "malformed": MALFORMED[0],
                               "a guessed switch": {"DZI_RECOVERY_ENABLED": "1", "DZI_PASSWORD_RECOVERY": "true",
                                                    **VALID}}.items():
            with self.subTest(settings=name):
                report, stderr = self.start(settings)
                self.assertEqual((report["started"], report["enabled"], report["login_link"]), (True, False, False))
                self.assertEqual(report["form"], 404)
                self.assertEqual((report["request"], report["verify"], report["redeem"], report["resend"],
                                  report["cancel"]), (404,) * 5)
                warnings = [line for line in stderr.splitlines() if WARNING_TEXT in line]
                self.assertEqual(len(warnings), 0 if name == "none" else 1)
                self.assertNotIn("Password recovery is disabled", stderr)       # the development warning
                self.assertEqual(report["logger"], [logging.INFO, False, 1])

    def test_with_the_gate_patched_on_valid_settings_enable_it_and_registration_stays_off(self):
        report, stderr = self.start(VALID, activation="on")
        self.assertEqual((report["started"], report["enabled"], report["mailer"]), (True, True, "SmtpMailer"))
        self.assertEqual((report["form"], report["login_link"]), (200, True))
        self.assertFalse(report["registration_enabled"])      # independent: registration's gate is still off
        self.assertNotIn(WARNING_TEXT, stderr)

    def test_with_the_gate_patched_on_a_development_backend_or_bad_settings_refuse_to_start(self):
        """RP2: malformed mail settings are start-up errors naming the variable without its value;
        console and file are refused."""
        for settings in (CONSOLE, {**CONSOLE, mail.BACKEND_ENV: "file"}, MALFORMED[0], MALFORMED[3]):
            with self.subTest(settings=sorted(settings)):
                report, _ = self.start(settings, activation="on")
                self.assertEqual((report["started"], report["error"]), (False, "ProductionConfigError"))
                self.assertIn("Refusing to start LearnPilot in production", report["message"])
                self.assertRegex(report["message"], r"DZI_(MAIL|SMTP)_[A-Z_]+")

    def test_the_gate_on_with_no_mail_backend_starts_with_recovery_disabled(self):
        report, _ = self.start({}, activation="on")
        self.assertEqual((report["started"], report["enabled"]), (True, False))


class DevelopmentStartTest(unittest.TestCase):
    def start(self, settings):
        result = _run_python(DEVELOPMENT_PROBE, {"DZI_SECRET_KEY": "k" * 40, "DZI_DB": ":memory:", **settings})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn(SMTP_PASSWORD, result.stdout + result.stderr)
        return report_of(result), result.stderr

    def test_usable_settings_enable_it_in_development_as_they_enable_registration(self):
        self.assertEqual(self.start(CONSOLE)[0], {"enabled": True, "mailer": "ConsoleMailer", "registration": True})
        self.assertEqual(self.start(VALID)[0], {"enabled": True, "mailer": "SmtpMailer", "registration": True})

    def test_without_settings_it_is_disabled_and_nothing_is_said(self):
        report, stderr = self.start({})
        self.assertEqual(report, {"enabled": False, "mailer": None, "registration": False})
        self.assertNotIn("recovery", stderr.lower())

    def test_unusable_settings_warn_by_name_and_leave_it_disabled(self):
        report, stderr = self.start(MALFORMED[0])
        self.assertEqual((report["enabled"], report["registration"]), (False, False))
        self.assertIn("Password recovery is disabled", stderr)
        self.assertIn(mail.SMTP_PORT_ENV, stderr)
        self.assertNotIn("not-a-port", stderr)


class IndependentGatesTest(RecoveryRouteTestCase):
    """RP3: for each combination of the four gates, the recovery routes answer by the recovery gate
    alone and the others are unchanged."""

    def test_every_combination_of_the_registration_teacher_profile_and_recovery_gates(self):
        for registration, teacher, profiles, recovery in itertools.product((False, True), repeat=4):
            mailer = InMemoryMailer()
            with patch.dict(self.app.config, {
                    REGISTRATION_KEY: RegistrationSettings(mailer=mailer) if registration else None,
                    CONFIG_KEY: RecoverySettings(mailer=mailer) if recovery else None,
                    profiles_service.CONFIG_KEY: profiles}), \
                    patch.object(registration_service, "TEACHER_ACTIVATION", teacher):
                client = self.app.test_client()
                with self.subTest(registration=registration, teacher=teacher, profiles=profiles, recovery=recovery):
                    self.assertEqual(client.get("/recover").status_code, 200 if recovery else 404)
                    self.assertEqual(client.get("/recover/verify").status_code, 302 if recovery else 404)
                    self.assertEqual(client.post("/recover/cancel", headers={"Origin": "http://localhost"}).status_code,
                                     303 if recovery else 404)
                    self.assertEqual(client.get("/register").status_code, 200 if registration else 404)
                    self.assertEqual(client.get("/register/student").status_code, 200 if registration else 404)
                    self.assertEqual(client.get("/register/teacher").status_code,
                                     200 if registration and teacher else 404)
                    self.assertEqual(client.get("/account/profile").status_code, 302 if profiles else 404)
                    login = self.body(client.get("/login"))
                    self.assertEqual("Забравена парола?" in login, recovery)
                    self.assertEqual("Регистрирай се" in login, registration)

    def test_recovery_needs_neither_registration_nor_the_other_gates(self):
        with patch.dict(self.app.config, {REGISTRATION_KEY: None, profiles_service.CONFIG_KEY: False}), \
                patch.object(registration_service, "TEACHER_ACTIVATION", False):
            self.verified()
            self.recover()
            self.assertEqual(self.redeem(self.last_code()).status_code, 303)


class RecoveryNginxTemplateTest(unittest.TestCase):
    """RP5, RP6, statically (design 7.5): effective locations, headers and limits of every recovery address,
    resolved as Nginx resolves them (the resolver of the registration test, reused)."""

    ADDRESSES = ("/recover", "/recover/verify", "/recover/resend", "/recover/cancel", "/recover/",
                 "/recover/unknown", "/recover/verify/", "/recover/%2e%2e")

    def setUp(self):
        self.helper = NginxTemplateTest("test_the_register_zone_is_keyed_like_the_login_zone")
        self.helper.setUp()
        self.text = self.helper.text

    def test_every_recovery_address_has_exactly_one_effective_no_referrer_and_hides_the_applications(self):
        for uri in self.ADDRESSES:
            with self.subTest(uri=uri):
                path, body = self.helper.resolve(uri)
                self.assertTrue(path.startswith("/recover"), (uri, path))
                headers = self.helper.effective_headers(body)
                self.assertEqual([value for name, value in headers if name == "Referrer-Policy"], ["no-referrer"])
                self.assertIn("proxy_hide_header Referrer-Policy;", body)
                self.assertIn(("X-Content-Type-Options", "nosniff"), headers)
                self.assertIn(("X-Frame-Options", "DENY"), headers)
                self.assertIn("proxy_pass http://127.0.0.1:8000;", body)
                self.assertNotIn("proxy_set_header", body)
                self.assertNotIn("proxy_pass_request", body)

    def test_the_exact_locations_and_the_prefix_exist_as_designed(self):
        found = {(modifier, path) for modifier, path, _ in self.helper.locations() if path.startswith("/recover")}
        self.assertEqual(found, {("=", "/recover"), ("=", "/recover/verify"), ("=", "/recover/resend"),
                                 ("", "/recover/")})
        for uri, expected in (("/recover", "/recover"), ("/recover/verify", "/recover/verify"),
                              ("/recover/resend", "/recover/resend"), ("/recover/cancel", "/recover/"),
                              ("/recover/unknown", "/recover/"), ("/recover/", "/recover/")):
            self.assertEqual(self.helper.resolve(uri)[0], expected, uri)

    def test_limits_zones_and_the_unlimited_addresses(self):
        expected = {"/recover": "lp_register burst=90 nodelay", "/recover/resend": "lp_register burst=90 nodelay",
                    "/recover/verify": "lp_login burst=40 nodelay", "/recover/cancel": None,
                    "/recover/unknown": None, "/recover/": None}
        for uri, limit in expected.items():
            with self.subTest(uri=uri):
                _, body = self.helper.resolve(uri)
                self.assertEqual(re.findall(r"limit_req zone=(.*);", body), [limit] if limit else [])
        # Zones are keyed on POSTs only, so the GET forms are not limited; the zones themselves are unchanged.
        self.assertIn("limit_req_zone $learnpilot_post_client zone=lp_register:1m rate=2r/m;", self.text)
        self.assertIn("limit_req_zone $learnpilot_post_client zone=lp_login:1m rate=1r/s;", self.text)

    def test_lookalike_addresses_fall_to_the_general_location(self):
        for uri in ("/recovery", "/recover-x", "/recoverx", "/recovery/verify", "/Recover", "/recoveries"):
            with self.subTest(uri=uri):
                path, body = self.helper.resolve(uri)
                self.assertEqual(path, "/")
                headers = self.helper.effective_headers(body)
                self.assertEqual([v for n, v in headers if n == "Referrer-Policy"], ["strict-origin-when-cross-origin"])

    def test_the_locations_headers_equal_the_servers_apart_from_the_referrer_policy(self):
        server = dict(self.helper.add_headers(self.helper.server_level()))
        for modifier, path, body in self.helper.locations():
            if path.startswith("/recover"):
                mine = dict(self.helper.add_headers(body))
                self.assertEqual({k: v for k, v in mine.items() if k != "Referrer-Policy"},
                                 {k: v for k, v in server.items() if k != "Referrer-Policy"}, path)
                self.assertEqual(mine["Referrer-Policy"], "no-referrer", path)
                self.assertEqual(len(self.helper.add_headers(body)), len(server), path)

    def test_the_zone_counts_and_every_other_protection_are_as_before_plus_the_three_recovery_limits(self):
        self.assertEqual(len(re.findall(r"limit_req zone=lp_login\b", self.text)), 5)       # four before, plus verify
        self.assertEqual(len(re.findall(r"limit_req zone=lp_register\b", self.text)), 5)    # three before, plus two
        for path, zone in (("/login", "lp_login burst=40"), ("/login/totp", "lp_login burst=40"),
                           ("/auth/google/start", "lp_oidc_start burst=40"),
                           ("/auth/google/link/confirm", "lp_login burst=40"),
                           ("/auth/google/callback", "lp_oidc_callback burst=40")):
            self.assertIn(f"limit_req zone={zone} nodelay;", self.helper.location(path))
        self.assertIn("proxy_set_header X-Forwarded-For $remote_addr;", self.text)

    def test_the_maintenance_configuration_is_not_in_the_template(self):
        """RD18 is an operator step on the host (scope: out of scope for the template)."""
        for needle in ("return 503", "maintenance", "lp_maintenance"):
            self.assertNotIn(needle, self.text)
        self.assertEqual(self.text.count("log_format"), 1)       # only the existing lp_no_args

    def test_the_application_and_the_template_together_give_one_value_stated_not_proved(self):
        """RP6: the application sets its own no-referrer on every recovery response (the route tests
        assert it), and the template hides it and sets the single value; combined, one value reaches
        the browser. This is two static facts, not evidence of a real Nginx (design 9)."""
        source = (ROOT / "web/blueprints/recovery.py").read_text(encoding="utf-8")
        self.assertIn('response.headers["Referrer-Policy"] = "no-referrer"', source)
        for uri in self.ADDRESSES[:6]:
            _, body = self.helper.resolve(uri)
            self.assertIn("proxy_hide_header Referrer-Policy;", body)
            self.assertEqual(self.helper.add_headers(body).count(("Referrer-Policy", "no-referrer")), 1)


if __name__ == "__main__":
    unittest.main()
