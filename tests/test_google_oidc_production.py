"""Google OIDC, test plan B6: configuration and deployment.

The activation gate of the build (decision P2), the callback address, the Gunicorn log
changes against Gunicorn's real error handler and parser, what a failing body read
does, and the Nginx and environment templates.

Nothing here needs systemd, Nginx, a network or a real credential. A production start
mutates process state, so it runs in fresh interpreters against temporary databases.
Gunicorn is driven in-process: its handler, its logger, its request parser and its
body reader are the real ones, and no socket and no worker process exist.
"""

import ast
import inspect
import io
import json
import logging
import re
import runpy
import unittest
from datetime import timedelta
from unittest.mock import patch

from gunicorn import config as gunicorn_config
from gunicorn.http import errors as gunicorn_errors
from gunicorn.http import message as gunicorn_message
from gunicorn.http import wsgi as gunicorn_wsgi
from gunicorn.http.parser import RequestParser
from gunicorn.workers.base import Worker

from tests.google_oidc_support import CLIENT_ID, CLIENT_SECRET, GoogleAppTestCase
from tests.support import ROOT, web_app
from tests.test_production_deployment import _run_python, _state_dir
from web import oauth_state, production
from web.services import google_oidc

DEPLOY = ROOT / "deploy"
HOST = "learnpilot.example.com"
GOOD = {google_oidc.CLIENT_ID_ENV: CLIENT_ID, google_oidc.CLIENT_SECRET_ENV: CLIENT_SECRET}
WARNING_TEXT = "but this build does not enable Google sign-in"
# Synthetic values that must never reach a log line.
CODE, STATE, COOKIE = "4/0AX-SECRET-CODE", "SECRET-STATE-" + "s" * 30, "session=SECRET-COOKIE-VALUE"
SECRETS = (CODE, STATE, "SECRET-COOKIE-VALUE")
CALLBACK_URI = f"/auth/google/callback?code={CODE}&state={STATE}"
MARKER = "LPPROBE_TEST_BODY_MARKER"

GATE_PROBE = r'''
import json, logging, os, sys
from web.services import google_oidc
if os.environ.get("PROBE_ACTIVATION") == "on":
    google_oidc.PRODUCTION_ACTIVATION = True
try:
    from web.wsgi import app
except Exception as exc:
    print(json.dumps({"started": False, "error": type(exc).__name__, "message": str(exc)}))
    sys.exit(0)
from web import oauth_state
provider = app.config.get(oauth_state.PROVIDER_CONFIG_KEY)
proxy = {"X-Forwarded-Proto": "https", "X-Forwarded-Host": "learnpilot.example.com",
         "X-Forwarded-For": "203.0.113.7"}
client = app.test_client()
page = client.get("/login", headers=proxy)
started = client.post("/auth/google/start", headers={**proxy, "Origin": "https://learnpilot.example.com"})
loopback = app.test_client().post("/auth/google/start", headers={"Origin": "http://localhost"})
oidc_logger = logging.getLogger("learnpilot.oidc")
print(json.dumps({
    "started": True,
    "enabled": provider is not None,
    "redirect_uri": provider.redirect_uri if provider else None,
    "login": page.status_code,
    "button": "/auth/google/start" in page.get_data(as_text=True),
    "start": started.status_code,
    "location": started.headers.get("Location"),
    "loopback_start": loopback.status_code,
    "oidc_logger": [oidc_logger.level, oidc_logger.propagate, len(oidc_logger.handlers)],
}))
'''

DEVELOPMENT_PROBE = r'''
import json
from web.app import app
from web import oauth_state
provider = app.config.get(oauth_state.PROVIDER_CONFIG_KEY)
print(json.dumps({"enabled": provider is not None, "redirect_uri": provider.redirect_uri if provider else None}))
'''


def report_of(result) -> dict:
    return json.loads(result.stdout.strip().splitlines()[-1])


# --------------------------------------------------------------------------- the activation gate


class ActivationGateTest(unittest.TestCase):
    """Decision P2: one constant in the build. While it is off, production starts
    normally whatever the two settings hold, keeps the provider disabled and logs one
    warning. No setting and no environment variable overrides it."""

    BASE = "https://" + HOST
    MALFORMED = (
        {google_oidc.CLIENT_ID_ENV: "not-a-client-id", google_oidc.CLIENT_SECRET_ENV: CLIENT_SECRET},
        {google_oidc.CLIENT_ID_ENV: CLIENT_ID, google_oidc.CLIENT_SECRET_ENV: "short"},
        {google_oidc.CLIENT_ID_ENV: "with space" + google_oidc.CLIENT_ID_SUFFIX, google_oidc.CLIENT_SECRET_ENV: "x" * 300},
    )
    ONE_OF_TWO = (
        {google_oidc.CLIENT_ID_ENV: CLIENT_ID},
        {google_oidc.CLIENT_SECRET_ENV: CLIENT_SECRET},
        {google_oidc.CLIENT_ID_ENV: CLIENT_ID, google_oidc.CLIENT_SECRET_ENV: ""},
    )

    def test_the_constant_is_off_in_this_build_and_nothing_else_sets_it(self):
        self.assertIs(google_oidc.PRODUCTION_ACTIVATION, False)
        assignments = []
        for directory in ("web", "src", "deploy"):
            for path in (ROOT / directory).rglob("*.py"):
                text = path.read_text(encoding="utf-8")
                assignments += [str(path.relative_to(ROOT)) for _ in re.findall(r"PRODUCTION_ACTIVATION\s*=[^=]", text)]
        # The registration gate of PR 4 and the profile gate of PR 6 are their own constants,
        # each in its own module.
        self.assertEqual(sorted(assignments), ["web/services/google_oidc.py", "web/services/profiles.py",
                                               "web/services/registration.py"])
        # No environment variable reads into it: the module reads none at all.
        source = (ROOT / "web/services/google_oidc.py").read_text(encoding="utf-8")
        self.assertIn("\nPRODUCTION_ACTIVATION = False\n", source)
        for needle in ("os.environ", "getenv", "import os"):
            self.assertFalse(needle in source, needle)

    def test_off_the_presence_of_credentials_never_refuses_a_start(self):
        for environ in ({}, GOOD, *self.ONE_OF_TWO, *self.MALFORMED):
            with self.subTest(settings=sorted(environ)):
                self.assertEqual(production.google_sign_in_errors(environ), [])

    def test_off_the_provider_is_disabled_and_one_warning_names_the_settings_and_no_value(self):
        for environ in (GOOD, *self.ONE_OF_TWO, *self.MALFORMED):
            with self.subTest(settings=sorted(environ)):
                with self.assertLogs("web.production", logging.WARNING) as logs:
                    provider = production.google_sign_in_provider(environ, self.BASE)
                self.assertIsNone(provider)
                self.assertEqual(len(logs.output), 1)
                self.assertIn(WARNING_TEXT, logs.output[0])
                for name, value in environ.items():
                    (self.assertIn if value else self.assertNotIn)(name, logs.output[0])
                    if value:
                        self.assertNotIn(value, logs.output[0])
        # None is logged when both are unset.
        for environ in ({}, {google_oidc.CLIENT_ID_ENV: "", google_oidc.CLIENT_SECRET_ENV: ""}):
            with self.assertNoLogs("web.production", logging.DEBUG):
                self.assertIsNone(production.google_sign_in_provider(environ, self.BASE))

    def test_on_both_valid_values_enable_the_provider(self):
        with patch.object(google_oidc, "PRODUCTION_ACTIVATION", True), self.assertNoLogs("web.production", logging.DEBUG):
            self.assertEqual(production.google_sign_in_errors(GOOD), [])
            provider = production.google_sign_in_provider(GOOD, self.BASE)
            self.assertEqual((provider.client_id, provider.client_secret), (CLIENT_ID, CLIENT_SECRET))
            # The callback address comes from DZI_PUBLIC_HOST and from nothing else.
            self.assertEqual(provider.redirect_uri, f"https://{HOST}/auth/google/callback")
            # Both unset: disabled, the default. No base URL: disabled.
            self.assertEqual(production.google_sign_in_errors({}), [])
            self.assertIsNone(production.google_sign_in_provider({}, self.BASE))
            self.assertIsNone(production.google_sign_in_provider(GOOD, None))

    def test_on_exactly_one_value_or_a_malformed_one_refuses_with_the_settings_name_only(self):
        with patch.object(google_oidc, "PRODUCTION_ACTIVATION", True):
            for environ in (*self.ONE_OF_TWO, *self.MALFORMED):
                with self.subTest(settings=sorted(environ)):
                    errors = production.google_sign_in_errors(environ)
                    self.assertTrue(errors)
                    text = "\n".join(errors)
                    self.assertRegex(text, r"DZI_GOOGLE_CLIENT_(ID|SECRET)")
                    for value in environ.values():
                        if value and value not in (CLIENT_ID,):
                            self.assertNotIn(value, text)
                    self.assertNotIn(CLIENT_SECRET, text)
                    # The same errors stop the whole start.
                    self.assertTrue(set(errors) <= set(production.production_config_errors(environ)))


class ProductionStartTest(unittest.TestCase):
    """The same, through ``web.wsgi`` in a fresh interpreter."""

    def start(self, settings, activation="off"):
        env = {**_state_dir(self, populated=True), **settings, "PROBE_ACTIVATION": activation}
        result = _run_python(GATE_PROBE, env)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for value in (CLIENT_SECRET, "short"):
            if value in settings.values() and value != "short":
                self.assertNotIn(value, result.stdout + result.stderr)
        return report_of(result), result.stderr

    def test_the_build_of_this_pr_starts_normally_and_keeps_google_disabled(self):
        cases = {
            "neither": {},
            "both": GOOD,
            "one": {google_oidc.CLIENT_ID_ENV: CLIENT_ID},
            "malformed": {google_oidc.CLIENT_ID_ENV: "not-a-client-id", google_oidc.CLIENT_SECRET_ENV: "short"},
        }
        for name, settings in cases.items():
            with self.subTest(settings=name):
                report, stderr = self.start(settings)
                self.assertEqual((report["started"], report["enabled"], report["login"]), (True, False, 200))
                # No button, and the route answers 404 to a same-origin POST.
                self.assertEqual((report["button"], report["start"], report["redirect_uri"]), (False, 404, None))
                warnings_logged = [line for line in stderr.splitlines() if WARNING_TEXT in line]
                self.assertEqual(len(warnings_logged), 0 if name == "neither" else 1)
                # What a development run would say about these settings is not said here.
                self.assertNotIn("Google sign-in is disabled", stderr)
                self.assertNotIn("not-a-client-id", stderr)
                # The log handler is attached like the lifecycle logger's.
                self.assertEqual(report["oidc_logger"], [logging.INFO, False, 1])

    def test_with_the_gate_open_valid_settings_enable_it_on_the_public_host_only(self):
        report, stderr = self.start(GOOD, activation="on")
        self.assertEqual((report["started"], report["enabled"], report["button"]), (True, True, True))
        self.assertEqual(report["redirect_uri"], f"https://{HOST}/auth/google/callback")
        self.assertEqual(report["start"], 303)
        self.assertIn("redirect_uri=https%3A%2F%2Flearnpilot.example.com%2Fauth%2Fgoogle%2Fcallback", report["location"])
        # A loopback probe is not the public address: no flow starts there.
        self.assertIn(report["loopback_start"], (403, 404))
        self.assertNotIn(WARNING_TEXT, stderr)

    def test_with_the_gate_open_one_value_or_a_malformed_one_refuses_to_start(self):
        for settings in ({google_oidc.CLIENT_SECRET_ENV: CLIENT_SECRET},
                         {google_oidc.CLIENT_ID_ENV: CLIENT_ID, google_oidc.CLIENT_SECRET_ENV: "short"}):
            with self.subTest(settings=sorted(settings)):
                report, _ = self.start(settings, activation="on")
                self.assertEqual((report["started"], report["error"]), (False, "ProductionConfigError"))
                self.assertIn("Refusing to start LearnPilot in production", report["message"])
                self.assertRegex(report["message"], r"DZI_GOOGLE_CLIENT_(ID|SECRET)")
                self.assertNotIn(CLIENT_SECRET, report["message"])
                self.assertNotIn("short", report["message"].replace("at most", ""))


class DevelopmentStartTest(unittest.TestCase):
    """A development run never refuses to start over these settings."""

    def start(self, settings):
        result = _run_python(DEVELOPMENT_PROBE, {"DZI_SECRET_KEY": "k" * 40, "DZI_DB": ":memory:", **settings})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return report_of(result), result.stderr

    def test_both_settings_and_a_base_url_enable_it(self):
        report, stderr = self.start({**GOOD, "DZI_PUBLIC_BASE_URL": "http://localhost:5001/"})
        self.assertEqual(report, {"enabled": True, "redirect_uri": "http://localhost:5001/auth/google/callback"})
        self.assertNotIn("Google sign-in", stderr)
        # The gate is the production build's: it does not apply to a development run.
        self.assertIs(google_oidc.PRODUCTION_ACTIVATION, False)

    def test_without_the_settings_it_is_disabled_and_nothing_is_said(self):
        report, stderr = self.start({"DZI_PUBLIC_BASE_URL": "http://localhost:5001"})
        self.assertEqual(report, {"enabled": False, "redirect_uri": None})
        self.assertNotIn("Google", stderr)

    def test_without_a_configured_base_url_it_is_disabled(self):
        # The request-derived loopback address is not used for this flow.
        report, stderr = self.start(GOOD)
        self.assertEqual(report["enabled"], False)
        self.assertIn("Google sign-in is disabled", stderr)
        self.assertIn("DZI_PUBLIC_BASE_URL", stderr)
        self.assertNotIn(CLIENT_SECRET, stderr)

    def test_a_missing_or_malformed_value_warns_and_leaves_it_disabled(self):
        cases = (
            ({google_oidc.CLIENT_ID_ENV: CLIENT_ID}, google_oidc.CLIENT_SECRET_ENV),
            ({google_oidc.CLIENT_SECRET_ENV: CLIENT_SECRET}, google_oidc.CLIENT_ID_ENV),
            ({google_oidc.CLIENT_ID_ENV: "malformed-client-id", google_oidc.CLIENT_SECRET_ENV: CLIENT_SECRET},
             google_oidc.CLIENT_ID_ENV),
            ({google_oidc.CLIENT_ID_ENV: CLIENT_ID, google_oidc.CLIENT_SECRET_ENV: "too-short"},
             google_oidc.CLIENT_SECRET_ENV),
        )
        for settings, named in cases:
            with self.subTest(settings=sorted(settings)):
                report, stderr = self.start({**settings, "DZI_PUBLIC_BASE_URL": "http://localhost:5001"})
                self.assertEqual(report["enabled"], False)
                self.assertIn("Google sign-in is disabled", stderr)
                self.assertIn(named, stderr)
                for value in ("malformed-client-id", "too-short", CLIENT_SECRET):
                    self.assertNotIn(value, stderr)


# --------------------------------------------------------------------------- Gunicorn


def gunicorn_settings() -> dict:
    return runpy.run_path(str(DEPLOY / "gunicorn.conf.py"))


class FakeSocket:
    """What Gunicorn writes its own error response to."""

    def __init__(self):
        self.sent = b""

    def gettimeout(self):
        return 0.0

    def sendall(self, data):
        self.sent += data

    send = sendall


class GunicornTestCase(unittest.TestCase):
    """Gunicorn's real logger class with the settings of the deployment template."""

    def setUp(self):
        self.settings = gunicorn_settings()
        self.cfg = gunicorn_config.Config()
        for name in ("access_log_format", "loglevel", "logger_class", "accesslog", "errorlog"):
            self.cfg.set(name, self.settings[name])
        self.error_log, self.access_log = logging.getLogger("gunicorn.error"), logging.getLogger("gunicorn.access")
        saved = [(log, list(log.handlers), list(log.filters), log.level, log.propagate)
                 for log in (self.error_log, self.access_log)]
        self.addCleanup(self.restore, saved)
        self.log = self.cfg.logger_class(self.cfg)
        # Capture what the real handlers would write, formatted as they format it.
        self.output = io.StringIO()
        for log in (self.error_log, self.access_log):
            formatters = [handler.formatter for handler in log.handlers]
            for handler in list(log.handlers):
                log.removeHandler(handler)
            capture = logging.StreamHandler(self.output)
            capture.setFormatter(formatters[0] if formatters else None)
            log.addHandler(capture)
        self.worker = Worker.__new__(Worker)
        self.worker.log, self.worker.cfg = self.log, self.cfg

    def restore(self, saved):
        for log, handlers, filters, level, propagate in saved:
            log.handlers[:], log.filters[:] = handlers, filters
            log.setLevel(level)
            log.propagate = propagate

    def parser(self, raw: bytes) -> RequestParser:
        return RequestParser(self.cfg, [raw], ("127.0.0.1", 50000))

    def handle_error(self, request, error) -> str:
        """Run Gunicorn's own error handler; returns what it logged and what it answered."""
        client = FakeSocket()
        Worker.handle_error(self.worker, request, client, ("192.0.2.77", 50000), error)
        return self.output.getvalue(), client.sent.decode("latin1")

    def logged(self) -> str:
        return self.output.getvalue()

    def assert_no_secret(self, text):
        for secret in SECRETS:
            self.assertNotIn(secret, text)


class GunicornSettingsTest(GunicornTestCase):
    def test_the_access_log_format_has_no_request_line_and_no_query_string(self):
        fmt = self.settings["access_log_format"]
        self.assertNotIn("%(r)s", fmt)
        self.assertNotIn("%(q)s", fmt)
        self.assertEqual(fmt, '%({x-forwarded-for}i)s "%(m)s %(U)s %(H)s" %(s)s %(b)s %(L)ss')

    def test_the_log_level_is_not_debug(self):
        # At debug Gunicorn logs the unread buffer of a client that disconnects in
        # the middle of a request, a cookie included, with or without the filter.
        self.assertEqual(self.settings["loglevel"], "info")
        self.assertGreater(self.error_log.level, logging.DEBUG)

    def test_the_logger_class_installs_its_filter_on_the_error_logger(self):
        logger_class = self.settings["logger_class"]
        self.assertTrue(issubclass(logger_class, __import__("gunicorn.glogging").glogging.Logger))
        self.assertIs(self.cfg.logger_class, logger_class)
        filters = [f for f in self.error_log.filters if isinstance(f, self.settings["RedactingErrorLogFilter"])]
        self.assertEqual(len(filters), 1)
        # A second set-up (a reload) does not add it twice.
        self.log.setup(self.cfg)
        filters = [f for f in self.error_log.filters if isinstance(f, self.settings["RedactingErrorLogFilter"])]
        self.assertEqual(len(filters), 1)

    def test_preload_app_keeps_one_instance_value_for_a_run(self):
        # Availability, and the one statement that relies on one instance per run:
        # a retirement reports an object of another instance as dead.
        self.assertIs(self.settings["preload_app"], True)
        self.assertEqual(self.settings["workers"], 1)

    def test_the_three_assumptions_of_the_log_statements(self):
        # 1. Gunicorn is reached only from this host, through Nginx.
        self.assertRegex(self.settings["bind"], r"^127\.0\.0\.1:\d+$")
        # 2. A request-wide body limit, so that every reader of a body gets Werkzeug's wrapper.
        limit = web_app.app.config["MAX_CONTENT_LENGTH"]
        self.assertIs(type(limit), int)
        self.assertGreater(limit, 0)
        # 3. The sync worker, which handles one request for each connection. With
        #    more than one thread Gunicorn replaces it by the threaded worker.
        self.assertEqual((self.settings["worker_class"], self.settings["threads"]), ("sync", 1))

    def test_the_production_application_keeps_the_body_limit(self):
        result = _run_python(
            "from web.wsgi import app\nprint(type(app.config['MAX_CONTENT_LENGTH']).__name__, "
            "app.config['MAX_CONTENT_LENGTH'] > 0)", _state_dir(self, populated=True))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout.strip().splitlines()[-1], "int True")

    def test_no_module_reads_the_raw_input_or_lifts_the_body_limit(self):
        offenders = []
        for directory in ("web", "src"):
            for path in sorted((ROOT / directory).rglob("*.py")):
                text = path.read_text(encoding="utf-8")
                if "wsgi.input" in text or re.search(r"max_content_length\s*=", text) \
                        or re.search(r"MAX_CONTENT_LENGTH[\"']\]\s*=\s*None", text):
                    offenders.append(str(path.relative_to(ROOT)))
        self.assertEqual(offenders, [])
        self.assertEqual((ROOT / "web/app.py").read_text(encoding="utf-8").count('app.config["MAX_CONTENT_LENGTH"] ='), 1)


class GunicornErrorLogTest(GunicornTestCase):
    """No line carries a code, a state or a cookie (design 8.4)."""

    # The refused text as a parser exception would quote it: plain, with an apostrophe
    # after the secret, with one before it, with a double quote, and with both marks.
    # Python's %r picks the quotation mark by what the text contains.
    TEXTS = (
        f"x&code={CODE}&state={STATE} HTTP/1.1",
        f"x&state={STATE}'y; Cookie: {COOKIE}",
        f"it's x&code={CODE}&state={STATE}",
        f'x&state={STATE}"y; Cookie: {COOKIE}',
        f"it's \"x&code={CODE}&state={STATE}",
    )

    # The nine classes whose text quotes what was refused, with %r.
    QUOTING = (
        gunicorn_errors.InvalidRequestLine, gunicorn_errors.InvalidRequestMethod, gunicorn_errors.InvalidHTTPVersion,
        gunicorn_errors.InvalidHeader, gunicorn_errors.InvalidHeaderName, gunicorn_errors.InvalidProxyLine,
        gunicorn_errors.ForbiddenProxyRequest, gunicorn_errors.UnsupportedTransferCoding,
        gunicorn_errors.ObsoleteFolding,
    )

    def bad_request_exceptions(self, text):
        """One of every parser exception class that the handler answers as a bad request."""
        return [cls(text) for cls in self.QUOTING] + [
            gunicorn_errors.LimitRequestHeaders(text),
            gunicorn_errors.ConfigurationProblem(text),
            gunicorn_errors.LimitRequestLine(5000, 4094),
            gunicorn_errors.InvalidSchemeHeaders(),
        ]

    def fresh_output(self):
        self.output.seek(0)
        self.output.truncate()

    def test_the_handler_logs_no_secret_for_any_parser_exception(self):
        # Each built with a code, a state and a cookie value, as a refused callback
        # request would carry them.
        text = f"GET {CALLBACK_URI} HTTP/1.1; Cookie: {COOKIE}"
        errors = self.bad_request_exceptions(text)
        self.assertEqual(len(errors), 13)
        for error in errors:
            with self.subTest(error=type(error).__name__):
                self.fresh_output()
                logged, answered = self.handle_error(None, error)
                self.assert_no_secret(logged)
                self.assertIn("Invalid request from ip=192.0.2.77: ", logged)
                self.assertEqual(logged.count("\n"), 1)

    def test_either_quotation_mark(self):
        # A text that Python quotes with the other mark, because it contains an
        # apostrophe, leaves nothing of itself in the line. No text here has a "?".
        count = 0
        for text in self.TEXTS:
            self.assertNotIn("?", text)
            for value in (text, text.encode("utf-8")):
                for cls in self.QUOTING:
                    with self.subTest(error=cls.__name__, kind=type(value).__name__, text=text[:20]):
                        error = cls(value)
                        self.assertTrue(any(secret in str(error) for secret in SECRETS))
                        self.fresh_output()
                        logged, _ = self.handle_error(None, error)
                        self.assert_no_secret(logged)
                        # The reason before the first quotation mark is kept.
                        reason = re.split("['\"]", str(error), maxsplit=1)[0]
                        # (The repr of bytes begins with a "b" before its quotation mark.)
                        self.assertTrue(reason.endswith((": ", "from ", ": b", "from b")), reason)
                        self.assertIn(f"Invalid request from ip=192.0.2.77: {reason}[redacted]", logged)
                        count += 1
        self.assertEqual(count, 90)

    def test_the_classes_that_do_not_quote_are_only_ever_given_fixed_text(self):
        # LimitRequestHeaders and ConfigurationProblem put their argument into the
        # message as it is. The filter keeps a message that has no quotation mark and
        # no "?", so what it guarantees for these two rests on what Gunicorn passes
        # them: a string literal, or a text whose every request-derived part is
        # quoted with %r. A Gunicorn that passes request text here fails this test.
        for cls in (gunicorn_errors.LimitRequestHeaders, gunicorn_errors.ConfigurationProblem):
            self.assertNotIn("%r", inspect.getsource(cls))
        raised = {"LimitRequestHeaders": [], "ConfigurationProblem": []}
        for module in (gunicorn_message, gunicorn_wsgi):
            for node in ast.walk(ast.parse(inspect.getsource(module))):
                if isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call) \
                        and getattr(node.exc.func, "id", None) in raised:
                    raised[node.exc.func.id].append(node.exc.args[0])
        self.assertGreaterEqual(len(raised["LimitRequestHeaders"]), 3)
        for argument in raised["LimitRequestHeaders"]:
            self.assertIsInstance(argument, ast.Constant, ast.unparse(argument))
            self.assertIsInstance(argument.value, str)
        self.assertEqual(len(raised["ConfigurationProblem"]), 1)
        argument = raised["ConfigurationProblem"][0]
        self.assertIsInstance(argument, ast.BinOp)
        self.assertIsInstance(argument.op, ast.Mod)
        template = argument.left.value
        self.assertEqual(template.count("%"), template.count("%r"))
        # As Gunicorn builds it, the request path is cut with everything after it.
        self.fresh_output()
        error = gunicorn_errors.ConfigurationProblem(
            "Request path %r does not start with SCRIPT_NAME %r" % (f"/x-{STATE}", "/app"))
        logged, _ = self.handle_error(None, error)
        self.assert_no_secret(logged)
        self.assertIn("Configuration problem: Request path [redacted]", logged)

    def test_a_cut_at_the_apostrophe_alone_would_have_leaked(self):
        # Why the filter cuts at whichever mark comes first.
        message = "Invalid request from ip=192.0.2.77: " + str(gunicorn_errors.InvalidHTTPVersion(self.TEXTS[1]))
        self.assertIn('"x&state=', message)   # quoted with the other mark
        self.assertIn(STATE, message.split("'")[0])
        record = logging.LogRecord("gunicorn.error", logging.WARNING, __file__, 1, message, (), None)
        self.settings["RedactingErrorLogFilter"]().filter(record)
        self.assertEqual(record.getMessage(), "Invalid request from ip=192.0.2.77: Invalid HTTP Version: [redacted]")

    def test_the_real_parser_on_a_callback_request(self):
        cases = {
            "a space inside the query": f"GET /auth/google/callback?code={CODE} x&state={STATE} HTTP/1.1\r\n"
                                        f"Host: {HOST}\r\n\r\n",
            "an apostrophe after the second value": f"GET /auth/google/callback?code={CODE} x&state={STATE}'y HTTP/1.1\r\n"
                                                    f"Host: {HOST}\r\n\r\n",
            "no version": f"GET /auth/google/callback?code={CODE}&state={STATE}\r\nHost: {HOST}\r\n\r\n",
            "a header line without a colon": f"GET /login HTTP/1.1\r\nHost: {HOST}\r\nCookie {COOKIE}\r\n\r\n",
            "a header line without a colon, with an apostrophe": f"GET /login HTTP/1.1\r\nHost: {HOST}\r\n"
                                                                 f"Cookie it's {COOKIE}\r\n\r\n",
        }
        for name, raw in cases.items():
            with self.subTest(case=name):
                with self.assertRaises(gunicorn_errors.ParseException) as raised:
                    next(self.parser(raw.encode("latin1")))
                # Gunicorn's own text does quote what it refused.
                self.assertTrue(any(secret in str(raised.exception) for secret in SECRETS))
                self.fresh_output()
                logged, _ = self.handle_error(None, raised.exception)
                self.assertTrue(logged.startswith("["), logged)
                self.assert_no_secret(logged)

    CALLBACK_REQUEST = (f"GET {CALLBACK_URI} HTTP/1.1\r\nHost: {HOST}\r\nX-Forwarded-For: 203.0.113.9\r\n"
                        f"Cookie: {COOKIE}\r\n\r\n").encode("latin1")

    def test_an_escaping_exception_and_a_worker_abort_with_a_callback_uri(self):
        for name, error in (("an escaping exception", RuntimeError("boom")), ("a worker abort", SystemExit(1))):
            with self.subTest(case=name):
                request = next(self.parser(self.CALLBACK_REQUEST))
                self.assertEqual(request.uri, CALLBACK_URI)
                self.fresh_output()
                logged, _ = self.handle_error(request, error)
                self.assert_no_secret(logged)
                self.assertIn("Error handling request /auth/google/callback[redacted]", logged)
                # The access line Gunicorn writes for a request it failed: no query
                # string, and in fact no path at all (its error path has none to give).
                self.assertRegex(logged, r'203\.0\.113\.9 "GET (None|/auth/google/callback) HTTP/1\.1" 500')
                self.assertNotIn("?", logged.replace("[redacted]", ""))

    def test_the_access_line_of_an_answered_callback_has_the_path_and_no_query_string(self):
        request = next(self.parser(self.CALLBACK_REQUEST))
        response, environ = gunicorn_wsgi.create(request, FakeSocket(), ("127.0.0.1", 50000), ("127.0.0.1", 8000), self.cfg)
        self.assertIn(CODE, environ["QUERY_STRING"])
        response.status = "302 FOUND"
        self.fresh_output()
        self.log.access(response, request, environ, timedelta(milliseconds=12))
        logged = self.logged()
        self.assertRegex(logged, r'^203\.0\.113\.9 "GET /auth/google/callback HTTP/1\.1" 302 \S+ 0\.012000s\n$')
        self.assert_no_secret(logged)
        self.assertNotIn("?", logged)

    def test_an_ordinary_message_is_unchanged(self):
        self.log.info("Booting worker with pid: %s", 4242)
        self.log.warning("Worker with pid %s was terminated due to signal %s", 4242, 9)
        self.log.error("Invalid request from ip=192.0.2.77: Request Line is too large (5000 > 4094)")
        logged = self.logged()
        self.assertIn("Booting worker with pid: 4242", logged)
        self.assertIn("Worker with pid 4242 was terminated due to signal 9", logged)
        self.assertIn("Request Line is too large (5000 > 4094)", logged)
        self.assertNotIn("[redacted]", logged)

    def test_every_other_message_is_cut_at_its_first_question_mark(self):
        self.log.error("Error handling request %s", "/account?google=linked&next=/x")
        self.log.info("redirecting to %s", "/login?next=/teacher")
        self.assertIn("Error handling request /account[redacted]", self.logged())
        self.assertIn("redirecting to /login[redacted]", self.logged())
        self.assertNotIn("next=", self.logged())

    def test_a_message_that_cannot_be_formatted_is_still_cut(self):
        self.log.error("request %s and %s", f"/x?state={STATE}")   # one argument too few
        self.assert_no_secret(self.logged())

    def test_tracebacks_are_not_filtered(self):
        # A characterisation of a stated limit, not a control. The filter rewrites a
        # record's message. A traceback is added to the line afterwards, by the
        # formatter, and ends with the exception's own text.
        form = f"email=person%40example.bg&password={MARKER}"
        try:
            raise gunicorn_errors.InvalidChunkSize(form.encode("ascii"))
        except OSError:
            self.log.exception("Error handling request %s", f"/login?next={STATE}")
        logged = self.logged()
        self.assertIn("Error handling request /login[redacted]", logged)
        self.assertNotIn(STATE, logged)
        self.assertIn("Traceback (most recent call last)", logged)
        self.assertIn(MARKER, logged)


class WorkerClassCharacterisationTest(GunicornTestCase):
    """Why the worker class is an assumption (design 8.4): a characterisation.

    It is not a control, and no threaded or asynchronous worker is run. A worker that
    keeps connections alive asks its parser for the next request, and the parser first
    reads what the previous one left unread, through Gunicorn's own body reader:
    outside the application and outside Werkzeug's wrapper.
    """

    def unread_body(self, connection_header: str) -> bytes:
        return (f"POST /login HTTP/1.1\r\nHost: {HOST}\r\nTransfer-Encoding: chunked\r\n{connection_header}\r\n"
                f"{MARKER}\r\n").encode("latin1") + b"GET /healthz HTTP/1.1\r\nHost: x\r\n\r\n"

    def test_the_next_request_on_a_kept_connection_reads_the_unread_body(self):
        parser = self.parser(self.unread_body(""))
        first = next(parser)
        self.assertEqual((first.method, first.path), ("POST", "/login"))
        with self.assertRaises(OSError) as raised:
            next(parser)
        self.assertIsInstance(raised.exception, gunicorn_errors.InvalidChunkSize)
        self.assertIn(MARKER, str(raised.exception))
        self.assertIsNone(raised.exception.errno)

    def test_with_connection_close_the_parser_stops_and_reads_nothing(self):
        parser = self.parser(self.unread_body("Connection: close\r\n"))
        next(parser)
        with self.assertRaises(StopIteration):
            next(parser)


class FailingBodyReadTest(GoogleAppTestCase):
    """A failing body read answers 400 and logs nothing (design 8.4, mechanism 3).

    The WSGI environment is the one Gunicorn builds, with ``wsgi.input_terminated``
    set, and the input is Gunicorn's real body reader. With the application's
    request-wide limit, Werkzeug gives every reader of the body a wrapper that turns
    the reader's exception into a 400.
    """

    FORM = f"email=person%40example.bg&password={MARKER}&current_password={MARKER}&new_password={MARKER}"

    def bodies(self) -> dict:
        form = self.FORM.encode("ascii")
        return {
            # The whole form stands where a chunk size belongs.
            "a malformed chunk size": form + b"\r\n",
            "a missing chunk terminator": b"%x\r\n" % len(form) + form + b"XY0\r\n\r\n",
            "a truncated chunk": b"%x\r\n" % (len(form) + 50) + form,
        }

    def routes(self) -> dict:
        signed_in = self.app.test_client()
        self.account = self.signed_in("teacher", client=signed_in)
        linking = self.app.test_client()
        self.linking_account = self.signed_in("teacher", client=linking)
        self.link_until_pending(linking)
        self.linking_client = linking
        return {
            "/login": None,
            "/account/password": self.cookie(signed_in),
            "/auth/google/link/confirm": self.cookie(linking),
        }

    def request(self, path, cookie, body, *, framing):
        """One request through Gunicorn's parser and into the application."""
        head = (f"POST {path} HTTP/1.1\r\nHost: localhost\r\nOrigin: http://localhost\r\n"
                "Content-Type: application/x-www-form-urlencoded\r\n")
        if cookie:
            head += f"Cookie: session={cookie}\r\n"
        raw = (head + framing + "Connection: close\r\n\r\n").encode("latin1") + body
        cfg = gunicorn_config.Config()
        request = next(RequestParser(cfg, [raw], ("127.0.0.1", 50000)))
        _, environ = gunicorn_wsgi.create(request, FakeSocket(), ("127.0.0.1", 50000), ("127.0.0.1", 8000), cfg)
        self.assertIs(environ["wsgi.input_terminated"], True)
        errors = io.StringIO()
        environ["wsgi.errors"] = errors
        answered = {}

        def start_response(status, headers, exc_info=None):
            answered["status"] = int(status.split()[0])
            answered["headers"] = headers

        body_out = b"".join(self.app.wsgi_app(environ, start_response))
        return answered["status"], dict(answered["headers"]), body_out.decode("utf-8", "replace"), errors.getvalue()

    def snapshot(self):
        return (
            self.query("SELECT COUNT(*) FROM login_throttle"),
            self.query("SELECT id, password_hash, session_key, last_login_at FROM users ORDER BY id"),
            self.consumption_rows(),
            self.identity_rows(),
            self.session(self.linking_client).get(oauth_state.LINK_SLOT),
        )

    def captured_logs(self):
        output = io.StringIO()
        handler = logging.StreamHandler(output)
        handler.setLevel(logging.DEBUG)
        root = logging.getLogger()
        previous = root.level
        root.addHandler(handler)
        root.setLevel(logging.DEBUG)
        self.addCleanup(root.removeHandler, handler)
        self.addCleanup(root.setLevel, previous)
        return output

    def test_a_malformed_or_incomplete_chunked_body_answers_400_and_logs_nothing(self):
        routes = self.routes()
        before = self.snapshot()
        output = self.captured_logs()
        for path, cookie in routes.items():
            for name, body in self.bodies().items():
                with self.subTest(path=path, body=name):
                    status, headers, page, errors = self.request(
                        path, cookie, body, framing="Transfer-Encoding: chunked\r\n")
                    self.assertEqual(status, 400)
                    # With every logger at DEBUG and wsgi.errors captured: no line
                    # holds the marker, and no traceback is logged.
                    logged = output.getvalue() + errors
                    self.assertNotIn(MARKER, logged)
                    self.assertNotIn("Traceback", logged)
                    self.assertNotIn(MARKER, page)
                    self.assertNotIn("Set-Cookie", headers)
        # Nothing was done with any of these requests: no throttle failure, no
        # session, no changed password, no record, no identity, and the pending link
        # is still in the session.
        self.assertEqual(self.snapshot(), before)

    def test_without_the_limit_the_same_read_would_log_the_body(self):
        # The control that shows what the limit is credited with: remove it, and
        # Gunicorn's exception escapes with the form in its text.
        self.routes()
        output = self.captured_logs()
        with patch.dict(self.app.config, {"MAX_CONTENT_LENGTH": None, "PROPAGATE_EXCEPTIONS": False}):
            status, _, _, errors = self.request(
                "/login", None, self.bodies()["a malformed chunk size"], framing="Transfer-Encoding: chunked\r\n")
        self.assertEqual(status, 500)
        self.assertIn(MARKER, output.getvalue() + errors)

    def test_an_interrupted_body_with_a_length_is_read_as_the_shorter_form(self):
        routes = self.routes()
        output = self.captured_logs()
        shorter = b"email=person%40example.bg&current_password=wrong-" + MARKER.encode("ascii")
        expected = {
            "/login": "Грешен имейл или парола.",
            "/account/password": "Грешна текуща парола.",
            "/auth/google/link/confirm": "Грешна текуща парола.",
        }
        for path, cookie in routes.items():
            with self.subTest(path=path):
                status, _, page, errors = self.request(
                    path, cookie, shorter, framing=f"Content-Length: {len(shorter) + 200}\r\n")
                # Nothing is raised and nothing is logged: the route answers as for the shorter form.
                self.assertEqual(status, 200)
                self.assertIn(expected[path], page)
                logged = output.getvalue() + errors
                self.assertNotIn(MARKER, logged)
                self.assertNotIn("Traceback", logged)
        self.assertEqual((self.identity_rows(), self.consumption_rows()[1:]), ([], []))


# --------------------------------------------------------------------------- templates


class NginxTemplateTest(unittest.TestCase):
    def setUp(self):
        self.conf = (DEPLOY / "nginx" / "learnpilot.conf").read_text(encoding="utf-8")

    def location(self, name: str) -> str:
        match = re.search(rf"\n    location {re.escape(name)} \{{\n(.*?)\n    \}}", self.conf, re.S)
        self.assertIsNotNone(match, name)
        return match.group(1)

    def directives(self, block: str) -> list[str]:
        return [line.strip() for line in block.splitlines() if line.strip() and not line.strip().startswith("#")]

    def test_the_zones(self):
        zones = {name: key for key, name in re.findall(r"limit_req_zone (\S+) zone=(\w+):1m rate=1r/s;", self.conf)}
        # A start is a POST; the callback is a GET, so its zone is keyed for every method.
        self.assertEqual(zones["lp_oidc_start"], "$learnpilot_post_client")
        self.assertEqual(zones["lp_oidc_callback"], "$binary_remote_addr")
        self.assertEqual(zones["lp_login"], "$learnpilot_post_client")

    def test_the_post_locations(self):
        for path, zone in (("= /auth/google/start", "lp_oidc_start"), ("= /auth/google/link", "lp_oidc_start"),
                           ("= /auth/google/link/confirm", "lp_login")):   # the confirmation carries a password
            with self.subTest(location=path):
                self.assertEqual(self.directives(self.location(path)), [
                    f"limit_req zone={zone} burst=40 nodelay;", "proxy_pass http://127.0.0.1:8000;"])
        # The cancellation carries no password and is not limited beyond the application.
        self.assertNotIn("location = /auth/google/link/cancel", self.conf)

    def test_every_limit_is_nodelay(self):
        limits = re.findall(r"^\s*limit_req zone=.*$", self.conf, re.M)
        self.assertGreaterEqual(len(limits), 9)
        for limit in limits:
            self.assertIn(" nodelay;", limit)

    def test_the_callback_location(self):
        self.assertEqual(self.directives(self.location("= /auth/google/callback")), [
            "limit_req zone=lp_oidc_callback burst=40 nodelay;",
            "access_log /var/log/nginx/access.log lp_no_args;",
            "error_log /dev/null emerg;",
            'add_header X-Content-Type-Options "nosniff" always;',
            'add_header X-Frame-Options "DENY" always;',
            'add_header Referrer-Policy "no-referrer" always;',
            "proxy_intercept_errors on;",
            "error_page 400 403 404 405 429 500 502 503 504 = @oidc_leave;",
            "proxy_pass http://127.0.0.1:8000;",
        ])

    def test_every_error_status_of_the_callback_leaves_the_address(self):
        self.assertEqual(self.directives(self.location("@oidc_leave")), [
            "access_log /var/log/nginx/access.log lp_no_args;",
            "error_log /dev/null emerg;",
            'add_header X-Content-Type-Options "nosniff" always;',
            'add_header X-Frame-Options "DENY" always;',
            'add_header Referrer-Policy "no-referrer" always;',
            'add_header Cache-Control "no-store" always;',
            "return 302 /login?google=unavailable;",
        ])
        # A named location: a path would serve the sign-in page at the callback address.
        self.assertNotRegex(self.conf, r"error_page [0-9 ]+=? */")
        # The application's own redirects are not intercepted.
        self.assertNotRegex(self.conf, r"error_page[^;]*\b30[0-9]\b")

    def test_the_callback_log_format_has_no_query_string(self):
        match = re.search(r"log_format lp_no_args (.*?);", self.conf, re.S)
        self.assertIsNotNone(match)
        fmt = match.group(1)
        for forbidden in ("$request ", "$request'", "$args", "$request_uri", "$query_string", "$http_referer",
                          "$http_cookie", "$arg_"):
            self.assertNotIn(forbidden, fmt)
        self.assertNotRegex(fmt, r"\$request\b(?!_method)")
        self.assertIn('"$request_method $uri $server_protocol"', fmt)
        self.assertIn("lr=$limit_req_status up=$upstream_status", fmt)
        # In the http context, beside the zones and before the server blocks.
        self.assertLess(self.conf.index("log_format lp_no_args"), self.conf.index("server {"))

    def test_request_buffering_stays_pinned_as_the_capacity_setting_it_is(self):
        # It decides when a request is sent on. It is neither validation nor redaction.
        self.assertIn("proxy_request_buffering on;", self.conf)
        self.assertNotIn("proxy_request_buffering off", self.conf)

    def test_the_main_error_log_is_not_made_more_talkative(self):
        self.assertEqual(re.findall(r"error_log\s+(\S+)\s+(\w+);", self.conf),
                         [("/dev/null", "emerg"), ("/dev/null", "emerg")])


class EnvironmentTemplateTest(unittest.TestCase):
    def test_both_variables_are_there_empty_and_commented(self):
        text = (DEPLOY / "learnpilot.env.example").read_text(encoding="utf-8")
        for name in (google_oidc.CLIENT_ID_ENV, google_oidc.CLIENT_SECRET_ENV):
            with self.subTest(name=name):
                self.assertRegex(text, rf"(?m)^# {name}=$")
                self.assertNotRegex(text, rf"(?m)^\s*{name}=")
        self.assertIn("https://DZI_PUBLIC_HOST/auth/google/callback", text)

    def test_the_requirements(self):
        lines = (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
        for line in ("Authlib>=1.8,<2", "joserfc>=1.6.1,<2", "requests>=2.32,<3", "urllib3>=2.2,<3",
                     "cryptography>=45.0.1,<51", "gunicorn>=23,<24"):
            self.assertIn(line, lines)
        # The log filter depends on the wording of one Gunicorn message: one major version.
        self.assertEqual([line for line in lines if line.lower().startswith("gunicorn")], ["gunicorn>=23,<24"])


if __name__ == "__main__":
    unittest.main()
