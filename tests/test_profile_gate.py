"""Registration PR 6: the profile build gate in production and development (test plan K3, K6, J6, J15).

``profiles.PRODUCTION_ACTIVATION`` is a build constant. While it is off a production start
keeps the whole profile flow off whatever any setting holds; the one development setting
(``DZI_PROFILES_ENABLED``) is read by a development run only. A production start mutates
process state, so it runs in a fresh interpreter against a temporary database, as the
Google and registration gate tests do. No message is sent and no socket is opened.
"""

import logging
import re
import unittest
from unittest.mock import patch

from tests.support import ROOT
from tests.test_google_oidc_production import report_of
from tests.test_production_deployment import _run_python, _state_dir
from web import production
from web.services import google_oidc, profiles
from web.services import registration as registration_service

WARNING_TEXT = "but this build does not enable account profiles"

PRODUCTION_PROBE = r'''
import json, os, sqlite3, sys
from web.services import profiles
if os.environ.get("PROBE_ACTIVATION") == "on":
    profiles.PRODUCTION_ACTIVATION = True
try:
    from web.wsgi import app
except Exception as exc:
    print(json.dumps({"started": False, "error": type(exc).__name__, "message": str(exc)}))
    sys.exit(0)
from web.services import accounts
conn = sqlite3.connect(os.environ["DZI_DB"])
user_id = accounts.create_user(conn, display_name="Ученик Тестов", role="student")
conn.execute("UPDATE users SET profile_required_at = '2026-10-15 09:00:00' WHERE id = ?", (user_id,))
conn.commit()
key = conn.execute("SELECT session_key FROM users WHERE id = ?", (user_id,)).fetchone()[0]
conn.close()
base = "https://learnpilot.example.com"
client = app.test_client()
with client.session_transaction(base_url=base) as state:
    state["uid"], state["sk"], state["iat"] = user_id, key, int(__import__("time").time())
proxy = {"X-Forwarded-Proto": "https", "X-Forwarded-Host": "learnpilot.example.com", "X-Forwarded-For": "203.0.113.7"}
origin = {**proxy, "Origin": base}
page = client.get("/account/profile", headers=proxy, base_url=base)
account = client.get("/account", headers=proxy, base_url=base)
posted = client.post("/account/profile", headers=origin, base_url=base,
                     data={"first_name": "Мария", "last_name": "Петрова", "grade": "9"})
conn = sqlite3.connect(os.environ["DZI_DB"])
report = {
    "started": True,
    "enabled": app.config.get(profiles.CONFIG_KEY),
    "page": page.status_code,
    "account": account.status_code,
    "link": "/account/profile" in account.get_data(as_text=True),
    "post": posted.status_code,
    "profiles": conn.execute("SELECT COUNT(*) FROM user_profiles").fetchone()[0],
    "mark": conn.execute("SELECT profile_required_at FROM users WHERE id = ?", (user_id,)).fetchone()[0],
}
print(json.dumps(report))
'''

DEVELOPMENT_PROBE = r'''
import json
from web.app import app
from web.services import profiles
print(json.dumps({"enabled": app.config.get(profiles.CONFIG_KEY)}))
'''


class GateConstantTest(unittest.TestCase):
    def test_the_gate_is_off_and_the_other_gates_are_as_they_were(self):
        self.assertIs(profiles.PRODUCTION_ACTIVATION, False)
        self.assertIs(registration_service.PRODUCTION_ACTIVATION, False)
        self.assertIs(registration_service.TEACHER_ACTIVATION, False)
        self.assertIs(google_oidc.PRODUCTION_ACTIVATION, False)

    def test_it_is_assigned_in_the_profile_service_only_and_reads_no_setting(self):
        source = (ROOT / "web/services/profiles.py").read_text(encoding="utf-8")
        self.assertIn("\nPRODUCTION_ACTIVATION = False\n", source)
        for needle in ("os.environ", "getenv", "import os"):
            self.assertNotIn(needle, source)
        self.assertEqual(len(re.findall(r"^PRODUCTION_ACTIVATION\s*=", source, re.M)), 1)
        assigned = []
        for directory in ("web", "src", "deploy"):
            for path in (ROOT / directory).rglob("*.py"):
                if re.search(r"\bprofiles(?:_service)?\.PRODUCTION_ACTIVATION\s*=[^=]", path.read_text(encoding="utf-8")):
                    assigned.append(str(path.relative_to(ROOT)))
        self.assertEqual(assigned, [])   # nothing in the build assigns it from outside

    def test_the_development_setting_is_read_by_the_development_start_and_the_production_warning_only(self):
        readers = []
        for directory in ("web", "src", "deploy"):
            for path in (ROOT / directory).rglob("*.py"):
                text = path.read_text(encoding="utf-8")
                if re.search(r"environ(?:\.get|\[)[^\n]*(DEVELOPMENT_ENV|DZI_PROFILES_ENABLED)", text):
                    readers.append(str(path.relative_to(ROOT)))
        # web/production.py reads it only to warn that it is set; it decides nothing from it.
        self.assertEqual(readers, ["web/app.py", "web/production.py"])

    def test_the_settings_names_are_the_documented_ones(self):
        self.assertEqual((profiles.CONFIG_KEY, profiles.DEVELOPMENT_ENV), ("PROFILES_ENABLED", "DZI_PROFILES_ENABLED"))


class ProductionGateFunctionTest(unittest.TestCase):
    def test_off_it_is_disabled_whatever_the_environment_holds(self):  # K3
        for environ in ({}, {"DZI_PROFILES_ENABLED": "1"}, {"DZI_PROFILES_ENABLED": "true"}, {"DZI_PROFILES_ENABLED": "0"},
                        {"PROFILES_ENABLED": "1"}, {"DZI_PROFILES": "1"}):
            with self.subTest(environ=environ):
                self.assertIs(production.profiles_production_enabled(environ), False)
                self.assertEqual(production.production_config_errors(environ).count("DZI_PROFILES_ENABLED"), 0)

    def test_off_a_present_setting_is_reported_once_by_name_and_never_by_value(self):
        with self.assertLogs("web.production", logging.WARNING) as logs:
            self.assertIs(production.profiles_production_enabled({"DZI_PROFILES_ENABLED": "SECRET-LOOKING-VALUE"}), False)
        self.assertEqual(len(logs.output), 1)
        self.assertIn("DZI_PROFILES_ENABLED", logs.output[0])
        self.assertIn(WARNING_TEXT, logs.output[0])
        self.assertNotIn("SECRET-LOOKING-VALUE", logs.output[0])
        with self.assertNoLogs("web.production", logging.DEBUG):
            production.profiles_production_enabled({})

    def test_the_setting_never_refuses_a_start(self):
        base = {"DZI_SECRET_KEY": "k" * 40, "DZI_PUBLIC_HOST": "learnpilot.example.com"}
        self.assertEqual(production.production_config_errors({**base, "DZI_PROFILES_ENABLED": "garbage"}),
                         production.production_config_errors(base))

    def test_with_the_constant_patched_on_it_is_enabled(self):
        with patch.object(profiles, "PRODUCTION_ACTIVATION", True):
            self.assertIs(production.profiles_production_enabled({}), True)
            with self.assertNoLogs("web.production", logging.DEBUG):
                self.assertIs(production.profiles_production_enabled({"DZI_PROFILES_ENABLED": "1"}), True)


class ProductionStartTest(unittest.TestCase):
    def start(self, settings, activation="off"):
        env = {**_state_dir(self, populated=True), **settings, "PROBE_ACTIVATION": activation}
        result = _run_python(PRODUCTION_PROBE, env)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return report_of(result), result.stderr

    def test_the_build_starts_with_the_flow_unreachable_whatever_is_set(self):  # K3, J6
        for name, settings in {"none": {}, "enabled": {"DZI_PROFILES_ENABLED": "1"},
                               "true": {"DZI_PROFILES_ENABLED": "true"}}.items():
            with self.subTest(settings=name):
                report, stderr = self.start(settings)
                self.assertEqual(report["started"], True)
                self.assertIs(report["enabled"], False)
                # A signed-in, marked student: no page, no link, no write.
                self.assertEqual((report["page"], report["account"], report["link"]), (404, 200, False))
                self.assertEqual((report["post"], report["profiles"]), (404, 0))
                self.assertEqual(report["mark"], "2026-10-15 09:00:00")      # stored, untouched, ignored
                warnings = [line for line in stderr.splitlines() if WARNING_TEXT in line]
                self.assertEqual(len(warnings), 0 if name == "none" else 1)

    def test_with_the_constant_patched_on_the_flow_is_reachable(self):
        report, stderr = self.start({}, activation="on")
        self.assertEqual((report["started"], report["enabled"]), (True, True))
        self.assertEqual((report["page"], report["account"], report["link"]), (200, 200, True))
        self.assertEqual((report["post"], report["profiles"]), (303, 1))
        self.assertNotIn(WARNING_TEXT, stderr)


class DevelopmentStartTest(unittest.TestCase):
    def start(self, settings):
        result = _run_python(DEVELOPMENT_PROBE, {"DZI_SECRET_KEY": "k" * 40, "DZI_DB": ":memory:", **settings})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return report_of(result)["enabled"]

    def test_only_a_development_run_reads_the_setting(self):
        self.assertIs(self.start({}), False)
        for value in ("1", "true", "TRUE", " yes ", "on"):
            with self.subTest(value=value):
                self.assertIs(self.start({"DZI_PROFILES_ENABLED": value}), True)
        for value in ("0", "false", "off", "", "maybe", "2"):
            with self.subTest(value=value):
                self.assertIs(self.start({"DZI_PROFILES_ENABLED": value}), False)


if __name__ == "__main__":
    unittest.main()
