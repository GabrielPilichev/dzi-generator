"""Google OIDC, test plan B2: the provider service (``web.services.google_oidc``).

ID-token validation with real RS256 signatures, the rules for the token endpoint's
responses, the signing-key cache, the one network budget with its bound on working
threads, and the cooldown. The fake Google of ``tests.google_oidc_support`` is the
innermost transport, so Authlib's client, the bounded reader, joserfc's verification
and the claim checks are the real ones. Nothing reaches a network.

No test sleeps to produce a timeout: an overrun is a held endpoint and a budget of a
fraction of a second.
"""

import ast
import gc
import hashlib
import hmac
import inspect
import io
import json
import logging
import os
import socket
import threading
import time
import unittest
import warnings
import weakref
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr
from unittest.mock import patch
from urllib.parse import parse_qsl, urlsplit

import flask
import requests
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding
from joserfc import jws
from requests.adapters import HTTPAdapter

from tests.google_oidc_support import (
    ABSENT,
    CLIENT_ID,
    CLIENT_SECRET,
    REDIRECT_URI,
    FakeGoogleMixin,
    GoogleAppTestCase,
    b64url,
    http_response,
    json_response,
    provider,
    signing_key,
)
from tests.support import ROOT
from web import oauth_state
from web.services import google_oidc, identities

OK, FAILED, UNAVAILABLE = google_oidc.OUTCOME_OK, google_oidc.OUTCOME_FAILED, google_oidc.OUTCOME_UNAVAILABLE
STATE_FIELDS = ("free_slots", "keys", "keys_fetched_at", "keys_fresh_until", "failures", "cooldown_until")
SHORT_BUDGET = 0.15
NONCE = "n" * 43
VERIFIER = "v" * 86


class RecordingState(google_oidc._GateState):
    """The gate's state, recording who touches it, with the provider lock held or not."""

    def __getattribute__(self, name):
        if name in STATE_FIELDS:
            object.__getattribute__(self, "_record")(name, "read")
        return object.__getattribute__(self, name)

    def __setattr__(self, name, value):
        if name in STATE_FIELDS and "_gate" in object.__getattribute__(self, "__dict__"):
            self._record(name, "write")
            if name == "free_slots":
                self.highest_free = max(self.highest_free, value)
                self.lowest_free = min(self.lowest_free, value)
        object.__setattr__(self, name, value)

    def _record(self, name, kind):
        gate = object.__getattribute__(self, "__dict__").get("_gate")
        if gate is not None:
            self.accesses.append((name, kind, threading.get_ident(), gate.lock.locked()))


def record_state(gate) -> RecordingState:
    state = RecordingState(google_oidc.PROVIDER_SLOTS)
    for name in STATE_FIELDS:
        object.__setattr__(state, name, object.__getattribute__(gate.state, name))
    state.accesses = []
    state.highest_free = state.lowest_free = object.__getattribute__(state, "free_slots")
    object.__setattr__(state, "_gate", gate)
    gate.state = state
    return state


class ServiceTestCase(FakeGoogleMixin, unittest.TestCase):
    """The provider gate on its own: no application, no database."""

    def setUp(self):
        self.mount_fake_google()
        self.gates = []
        self.use(google_oidc.GATE)
        self.addCleanup(self.assert_bounds_held)

    def use(self, gate):
        """Make ``gate`` the one under test: its state is recorded and its network
        function counts the workers that are inside it."""
        gate.recorded = record_state(gate)
        gate.inside, gate.most_inside, gate.count_lock = 0, 0, threading.Lock()
        work = gate._work

        def counted(request, keys, deadline):
            with gate.count_lock:
                gate.inside += 1
                gate.most_inside = max(gate.most_inside, gate.inside)
            try:
                return work(request, keys, deadline)
            finally:
                with gate.count_lock:
                    gate.inside -= 1

        gate._work = counted
        self.gate = gate
        self.gates.append(gate)
        return gate

    def assert_bounds_held(self):
        # Checked for every test of this module, after its workers have ended.
        self.finish_workers()
        for gate in self.gates:
            self.assertLessEqual(gate.recorded.highest_free, google_oidc.PROVIDER_SLOTS)
            self.assertGreaterEqual(gate.recorded.lowest_free, 0)
            self.assertLessEqual(gate.most_inside, google_oidc.PROVIDER_SLOTS)
            self.assertEqual(self.free_slots(gate), google_oidc.PROVIDER_SLOTS, "a slot was lost")
            self.assertEqual([entry for entry in gate.recorded.accesses if not entry[3]], [],
                             "the provider state was touched without the provider lock")

    def free_slots(self, gate=None) -> int:
        return object.__getattribute__((gate or self.gate).state, "free_slots")

    def failures(self) -> int:
        return object.__getattribute__(self.gate.state, "failures")

    def set_failures(self, count: int) -> None:
        with self.gate.lock:
            self.gate.state.failures = count

    def cooling(self) -> bool:
        return self.gate.cooling_down()

    def end_cooldown(self) -> None:
        with self.gate.lock:
            self.gate.state.cooldown_until = 0.0

    def request(self, **identity) -> google_oidc.ExchangeRequest:
        """An authorization request answered at the fake Google: a code to exchange."""
        url = google_oidc.authorization_url(provider(), state="s" * 43, nonce=NONCE, code_verifier=VERIFIER)
        code = self.google.authorize(url, **identity)
        return google_oidc.ExchangeRequest(
            code=code, code_verifier=VERIFIER, nonce=NONCE, redirect_uri=REDIRECT_URI,
            client_id=CLIENT_ID, client_secret=CLIENT_SECRET)

    def exchange(self, *, budget=None, **identity) -> google_oidc.ExchangeResult:
        return self.gate.exchange(self.request(**identity), budget=budget)

    def assert_ok(self, result, subject="google-subject-1"):
        self.assertEqual((result.outcome, result.reason), (OK, None))
        self.assertEqual(result.identity.subject, subject)

    def assert_refused(self, result, outcome, *, token_requests=1):
        self.assertEqual(result.outcome, outcome, result.reason)
        self.assertIsNone(result.identity)
        self.assertEqual(len(self.google.token_requests), token_requests)

    def raw_token(self, payload: bytes, *, key=None, header=None) -> str:
        key = key or self.google.keys[0]
        return jws.serialize_compact({"alg": "RS256", "kid": key.kid, **(header or {})}, payload, key)

    def hand_signed(self, header: dict, payload: bytes, key=None) -> str:
        """A really signed token with a header that joserfc itself refuses to write."""
        signing_input = f"{b64url(json.dumps(header).encode())}.{b64url(payload)}".encode("ascii")
        signature = (key or self.google.keys[0]).get_op_key("sign").sign(
            signing_input, padding.PKCS1v15(), hashes.SHA256())
        return f"{signing_input.decode('ascii')}.{b64url(signature)}"

    def claims_json(self, **overrides) -> dict:
        return self.google.claims(NONCE, {"sub": "google-subject-1", **overrides})

    def workers(self) -> list:
        return [thread for thread in threading.enumerate() if thread.name == "google-oidc"]


# --------------------------------------------------------------------------- the authorization request


class AuthorizationRequestTest(unittest.TestCase):
    def test_the_url_has_exactly_the_documented_parameters(self):
        url = google_oidc.authorization_url(provider(), state="s" * 43, nonce=NONCE, code_verifier=VERIFIER)
        parts = urlsplit(url)
        self.assertEqual(f"{parts.scheme}://{parts.netloc}{parts.path}", google_oidc.AUTHORIZATION_ENDPOINT)
        query = dict(parse_qsl(parts.query))
        self.assertEqual(len(parse_qsl(parts.query)), len(query))
        self.assertEqual(set(query), {
            "response_type", "client_id", "redirect_uri", "scope", "state", "nonce", "code_challenge",
            "code_challenge_method", "prompt"})
        self.assertEqual(query["response_type"], "code")
        self.assertEqual(query["client_id"], CLIENT_ID)
        self.assertEqual(query["redirect_uri"], REDIRECT_URI)
        self.assertEqual(query["scope"], "openid email profile")
        self.assertEqual((query["state"], query["nonce"]), ("s" * 43, NONCE))
        self.assertEqual(query["code_challenge_method"], "S256")
        self.assertEqual(query["prompt"], "select_account")
        self.assertEqual(query["code_challenge"], b64url(hashlib.sha256(VERIFIER.encode()).digest()))
        # The verifier and the secret never leave the server in a URL.
        self.assertNotIn(VERIFIER, url)
        self.assertNotIn(CLIENT_SECRET, url)
        for absent in ("access_type", "hd", "login_hint", "include_granted_scopes", "response_mode", "claims"):
            self.assertNotIn(absent, query)

    def test_the_pinned_metadata(self):
        self.assertEqual(google_oidc.ISSUER, "https://accounts.google.com")
        self.assertEqual(google_oidc.ACCEPTED_ISSUERS, {"https://accounts.google.com", "accounts.google.com"})
        self.assertEqual(google_oidc.AUTHORIZATION_ENDPOINT, "https://accounts.google.com/o/oauth2/v2/auth")
        self.assertEqual(google_oidc.TOKEN_ENDPOINT, "https://oauth2.googleapis.com/token")
        self.assertEqual(google_oidc.JWKS_URI, "https://www.googleapis.com/oauth2/v3/certs")
        self.assertEqual(google_oidc.ID_TOKEN_ALGORITHM, "RS256")
        self.assertIs(google_oidc.PRODUCTION_ACTIVATION, False)

    def test_the_configuration_rules_name_settings_and_never_values(self):
        good_id, good_secret = CLIENT_ID, CLIENT_SECRET
        self.assertEqual(google_oidc.credential_problems(good_id, good_secret), [])
        bad_ids = ("", None, "plain-text", "x" * 250 + ".apps.googleusercontent.com",
                   "with space.apps.googleusercontent.com", "кирилица.apps.googleusercontent.com",
                   ".apps.googleusercontent.com", "a.apps.googleusercontent.com.evil.example")
        for value in bad_ids:
            with self.subTest(client_id=str(value)[:30]):
                problems = google_oidc.credential_problems(value, good_secret)
                self.assertEqual(len(problems), 1)
                self.assertIn("DZI_GOOGLE_CLIENT_ID", problems[0])
                if value and value != google_oidc.CLIENT_ID_SUFFIX:  # the message names the suffix it expects
                    self.assertNotIn(value, problems[0])
                self.assertIsNone(google_oidc.provider_config(value, good_secret, "https://lp.example"))
        for value in ("", None, "short", "x" * 256, "sixteen chars  !!", "секретсекретсекрет"):
            with self.subTest(client_secret=str(value)[:30]):
                problems = google_oidc.credential_problems(good_id, value)
                self.assertEqual(len(problems), 1)
                self.assertIn("DZI_GOOGLE_CLIENT_SECRET", problems[0])
                if value:
                    self.assertNotIn(value, problems[0])
        self.assertEqual(len(google_oidc.credential_problems(None, None)), 2)
        # No base URL, no provider: the callback address is never taken from a request.
        for base in (None, "", 5):
            self.assertIsNone(google_oidc.provider_config(good_id, good_secret, base))
        config = google_oidc.provider_config(good_id, good_secret, "https://lp.example")
        self.assertEqual(config.redirect_uri, "https://lp.example/auth/google/callback")
        self.assertNotIn(good_secret, repr(config))

    def test_no_value_object_prints_what_it_holds(self):
        request = google_oidc.ExchangeRequest("the-code", VERIFIER, NONCE, REDIRECT_URI, CLIENT_ID, CLIENT_SECRET)
        identity = google_oidc.VerifiedIdentity("the-subject", "person@example.bg", "Име Фамилия")
        result = google_oidc.ExchangeResult(OK, identity)
        text = " ".join((repr(request), str(request), repr(identity), str(identity), repr(result)))
        for secret in ("the-code", VERIFIER, NONCE, CLIENT_SECRET, "the-subject", "person@example.bg", "Име"):
            self.assertNotIn(secret, text)


# --------------------------------------------------------------------------- ID tokens


class IdTokenTest(ServiceTestCase):
    """Each case uses a real signature unless the case is about its absence."""

    def test_accepted_tokens(self):
        cases = {
            "the https issuer": {},
            "the bare issuer": {"iss": "accounts.google.com"},
            "the second published key": {"__key__": self.google.keys[1]},
            "expired inside the leeway": {"exp": int(time.time()) - 30, "iat": int(time.time()) - 400},
            "no azp": {"azp": ABSENT},
            "issued a moment ahead of this clock": {"iat": int(time.time()) + 30},
        }
        for name, claims in cases.items():
            with self.subTest(case=name):
                self.assert_ok(self.exchange(**claims))
        self.assertEqual(len(self.google.jwks_requests), 1)

    def test_the_identity_carries_the_subject_and_the_reported_metadata_only(self):
        result = self.exchange(subject="1234567890", email="Person@Example.BG", name="Иван Иванов",
                               picture="https://example.invalid/p.png", locale="bg", hd="school.example",
                               given_name="Иван", family_name="Иванов", email_verified=True)
        identity = result.identity
        self.assertEqual((identity.subject, identity.email, identity.name),
                         ("1234567890", "Person@Example.BG", "Иван Иванов"))
        self.assertEqual({field.name for field in identity.__dataclass_fields__.values()}, {"subject", "email", "name"})

    def test_a_token_whose_signature_does_not_verify_is_refused(self):
        claims = self.claims_json()
        payload = json.dumps(claims).encode()
        good = self.google.id_token(claims)
        header, body, signature = good.split(".")
        other_key_same_kid = signing_key("impostor-of-key-1", kid="test-key-1")
        unpublished = signing_key("unpublished-key")
        # The classic algorithm confusion: joserfc refuses to build this token (it rejects a
        # public key as an HMAC secret), so it is assembled by hand, MAC'd with the public PEM.
        hs256_input = f"{b64url(json.dumps({'alg': 'HS256', 'kid': 'test-key-1'}).encode())}.{b64url(payload)}".encode("ascii")
        hs256_mac = hmac.new(self.google.keys[0].as_pem(private=False), hs256_input, hashlib.sha256).digest()
        hs256 = f"{hs256_input.decode('ascii')}.{b64url(hs256_mac)}"
        altered = b64url(json.dumps({**claims, "sub": "someone-else"}).encode())
        cases = {
            "alg none": b64url(json.dumps({"alg": "none", "kid": "test-key-1"}).encode()) + "." + body + ".",
            "HS256 keyed with the public key": hs256,
            "another key under a published kid": self.google.id_token(claims, key=other_key_same_kid),
            "an unknown kid": self.google.id_token(claims, key=unpublished),
            "no kid": jws.serialize_compact({"alg": "RS256"}, payload, self.google.keys[0]),
            "a kid that is not a string": self.hand_signed({"alg": "RS256", "kid": 7}, payload),
            "an altered payload": f"{header}.{altered}.{signature}",
            "a truncated signature": good[:-12],
            "an empty signature": f"{header}.{body}.",
            "not a JWT": "definitely-not-a-jwt",
            "two parts": f"{header}.{body}",
            "a payload that is a JSON array": self.raw_token(b'["https://accounts.google.com"]'),
            "a payload that is a JSON string": self.raw_token(b'"text"'),
            "a payload that is not JSON": self.raw_token(b"{not json"),
        }
        for name, token in cases.items():
            with self.subTest(case=name):
                before = len(self.google.token_requests)
                result = self.exchange(__token__=token)
                self.assertEqual((result.outcome, result.identity), (FAILED, None))
                self.assertEqual(len(self.google.token_requests), before + 1)
        # Google answered: none of these is a failure for the cooldown.
        self.assertEqual((self.failures(), self.cooling()), (0, False))

    def test_a_single_published_key_does_not_verify_a_token_that_names_no_key(self):
        # joserfc alone would: with one key in the set it does not need a kid.
        self.google.published = [self.google.keys[0]]
        payload = json.dumps(self.claims_json()).encode()
        token = jws.serialize_compact({"alg": "RS256"}, payload, self.google.keys[0])
        self.assertEqual(self.exchange(__token__=token).outcome, FAILED)
        self.assert_ok(self.exchange())

    def assert_claims_refused(self, cases: dict):
        for name, overrides in cases.items():
            with self.subTest(case=name):
                result = self.exchange(**overrides)
                self.assertEqual((result.outcome, result.identity), (FAILED, None), name)
        self.assertEqual((self.failures(), self.cooling()), (0, False))

    def test_issuer_refused(self):
        self.assert_claims_refused({
            "a trailing slash": {"iss": "https://accounts.google.com/"},
            "upper case": {"iss": "https://ACCOUNTS.GOOGLE.COM"},
            "http": {"iss": "http://accounts.google.com"},
            "a look-alike host": {"iss": "https://accounts.google.com.evil.example"},
            "a longer bare host": {"iss": "accounts.google.com.evil.example"},
            "surrounding space": {"iss": " https://accounts.google.com"},
            "missing": {"iss": ABSENT},
            "a list": {"iss": ["https://accounts.google.com"]},
            "a number": {"iss": 5},
        })

    def test_audience_refused(self):
        other = "someone-else.apps.googleusercontent.com"
        self.assert_claims_refused({
            "another client with our azp": {"aud": other, "azp": CLIENT_ID},
            "ours plus another": {"aud": [CLIENT_ID, other]},
            "a one-element list of ours": {"aud": [CLIENT_ID]},
            "a number": {"aud": 5},
            "an object": {"aud": {"client": CLIENT_ID}},
            "ours with a trailing space": {"aud": CLIENT_ID + " "},
            "missing": {"aud": ABSENT},
            "another azp": {"azp": other},
            "azp as a list": {"azp": [CLIENT_ID]},
            "azp as a number": {"azp": 5},
            "an empty azp": {"azp": ""},
        })

    def test_times_refused(self):
        now = int(time.time())
        cases = {}
        for claim in ("exp", "iat"):
            for name, value in (("Infinity", float("inf")), ("-Infinity", float("-inf")), ("NaN", float("nan")),
                                ("a float", now + 0.5), ("true", True), ("a numeric string", str(now + 60)),
                                ("zero", 0), ("negative", -now), ("2^70", 2**70), ("missing", ABSENT)):
                cases[f"{claim} {name}"] = {claim: value}
        cases.update({
            "iat in the future": {"iat": now + 400, "exp": now + 4000},
            "iat older than ten minutes": {"iat": now - 700, "exp": now + 3000},
            "iat later than exp": {"iat": now, "exp": now - 30},
            "iat equal to exp": {"iat": now, "exp": now},
            "expired": {"iat": now - 500, "exp": now - 100},
        })
        self.assert_claims_refused(cases)

    def test_a_number_too_large_for_a_float_is_refused(self):
        claims = json.dumps(self.claims_json()).replace(f'"exp": {self.claims_json()["exp"]}', '"exp": 1e400')
        self.assertIn("1e400", claims)
        self.assertEqual(self.exchange(__token__=self.raw_token(claims.encode())).outcome, FAILED)

    def test_a_repeated_member_is_read_once_and_that_value_is_what_every_check_sees(self):
        # One parse, by joserfc: the last value. A token that hides a bad audience
        # behind a good one, or a good one behind a bad one, is judged by what is used.
        claims = self.claims_json()
        payload = json.dumps(claims)
        other = "someone-else.apps.googleusercontent.com"
        bad_last = payload[:-1] + f', "aud": "{other}"}}'
        good_last = payload.replace(f'"aud": "{CLIENT_ID}"', f'"aud": "{other}", "aud": "{CLIENT_ID}"')
        self.assertEqual(self.exchange(__token__=self.raw_token(bad_last.encode())).outcome, FAILED)
        self.assert_ok(self.exchange(__token__=self.raw_token(good_last.encode())))

    def test_nonce_refused(self):
        self.assert_claims_refused({
            "another transaction's": {"nonce": "o" * 43},
            "missing": {"nonce": ABSENT},
            "a number": {"nonce": 5},
            "a list": {"nonce": [NONCE]},
        })

    def test_subject_refused(self):
        self.assert_claims_refused({
            "missing": {"subject": ABSENT},
            "empty": {"subject": ""},
            "a number": {"subject": 1234567890},
            "256 characters": {"subject": "s" * 256},
            "with a space": {"subject": "has space"},
            "with a control character": {"subject": "tab\there"},
            "not ASCII": {"subject": "кирилица"},
            "a list": {"subject": ["x"]},
        })
        self.assert_ok(self.exchange(subject="s" * 255), "s" * 255)

    def test_metadata_never_fails_a_token(self):
        cases = {
            "email a number": ({"email": 5}, (None, "Ученик Тестов")),
            "name a list": ({"name": ["a", "b"]}, ("pupil@example.bg", None)),
            "email_verified a string": ({"email_verified": "maybe"}, ("pupil@example.bg", "Ученик Тестов")),
            "no email and no name": ({"email": ABSENT, "name": ABSENT}, (None, None)),
            "email an object": ({"email": {"a": 1}, "name": 7.5}, (None, None)),
        }
        for name, (claims, expected) in cases.items():
            with self.subTest(case=name):
                result = self.exchange(**claims)
                self.assert_ok(result)
                self.assertEqual((result.identity.email, result.identity.name), expected)

    def test_an_at_hash_is_checked_when_present(self):
        self.assertEqual(self.exchange(at_hash="not-the-hash-of-the-access-token").outcome, FAILED)


class MalformedSubjectAtTheApplicationTest(GoogleAppTestCase):
    """A malformed subject is a validation failure, never "identity not found"."""

    def test_no_lookup_is_made_and_no_row_is_written(self):
        for subject in ("", "has space", "s" * 256, 12345, ABSENT):
            with self.subTest(subject=repr(subject)[:20]):
                client = self.app.test_client()
                with patch.object(identities, "find_identity", wraps=identities.find_identity) as lookup, \
                        self.assertLogs("learnpilot.oidc", logging.WARNING):
                    refused = self.google_sign_in(client, subject=subject)
                self.assert_redirect(refused, "/login?google=failed")
                lookup.assert_not_called()
                self.assertEqual((self.user_count(), self.identity_rows()), (0, []))
                self.assertEqual(self.session(client), {})


# --------------------------------------------------------------------------- provider responses


class ProviderResponseTest(ServiceTestCase):
    """Design 5.3, one test per row. In every row exactly one request is made."""

    def respond(self, factory):
        self.google.token_response = lambda request: factory()

    def assert_failure(self, reason=None):
        result = self.gate.exchange(self.request())
        self.assert_refused(result, UNAVAILABLE)
        if reason is not None:
            self.assertEqual(result.reason, reason)
        self.assertEqual(self.failures(), 1)
        self.assertEqual(self.google.jwks_requests, [])
        return result

    def test_200_without_an_id_token(self):
        self.respond(lambda: json_response(200, {"access_token": "at", "token_type": "Bearer"}))
        self.assert_failure(google_oidc.REASON_NO_ID_TOKEN)

    def test_200_with_an_id_token_that_is_not_a_usable_string(self):
        for value in (5, None, "", ["token"], {"t": 1}):
            with self.subTest(id_token=repr(value)):
                self.setUp_fresh()
                self.respond(lambda: json_response(200, {"access_token": "at", "id_token": value}))
                self.assert_failure(google_oidc.REASON_NO_ID_TOKEN)

    def setUp_fresh(self):
        self.finish_workers()
        self.google.requests.clear()
        self.set_failures(0)
        self.end_cooldown()

    def test_a_json_array(self):
        self.respond(lambda: json_response(200, [{"id_token": "x"}]))
        self.assert_failure(google_oidc.REASON_NOT_AN_OBJECT)

    def test_a_body_that_is_not_json(self):
        self.respond(lambda: http_response(200, b"<html>a captive portal with a SECRET-BODY</html>"))
        result = self.assert_failure(google_oidc.REASON_NOT_JSON)
        self.assertNotIn("SECRET-BODY", repr(result.reason))

    def test_a_body_that_is_not_utf8(self):
        self.respond(lambda: http_response(200, b"\xff\xfe\x00{"))
        self.assert_failure(google_oidc.REASON_NOT_JSON)

    def test_a_body_over_64_kib(self):
        token = self.google.id_token(self.claims_json())
        document = {"id_token": token, "access_token": "at", "padding": "p" * (64 * 1024)}
        self.respond(lambda: json_response(200, document))
        self.assert_failure(google_oidc.REASON_BODY_TOO_LARGE)

    def test_the_largest_accepted_body_is_accepted(self):
        token = self.google.id_token(self.claims_json())
        base = json.dumps({"id_token": token, "access_token": "at", "padding": ""})
        document = {"id_token": token, "access_token": "at", "padding": "p" * (64 * 1024 - len(base))}
        self.assertEqual(len(json.dumps(document)), 64 * 1024)
        self.respond(lambda: json_response(200, document))
        self.assert_ok(self.gate.exchange(self.request()))

    def test_an_id_token_over_16_kib(self):
        self.respond(lambda: json_response(200, {"id_token": "t" * (16 * 1024 + 1), "access_token": "at"}))
        self.assert_failure(google_oidc.REASON_NO_ID_TOKEN)

    def test_a_compressed_body_is_refused_before_it_is_read(self):
        self.respond(lambda: http_response(200, b"\x1f\x8b\x08\x00", {"Content-Encoding": "gzip"}))
        self.assert_failure(google_oidc.REASON_ENCODING)
        self.assertEqual(self.google.token_requests[0]["headers"]["Accept-Encoding"], "identity")

    def test_a_redirect_is_a_failure_and_its_location_is_never_requested(self):
        token = self.google.id_token(self.claims_json())
        for name, factory in (
            ("with a JSON body", lambda: json_response(302, {"id_token": token, "access_token": "at"},
                                                       {"Location": "https://evil.example/token"})),
            ("without a body", lambda: http_response(302, b"", {"Location": "https://evil.example/token"})),
            ("a 307 to another path of the same host", lambda: http_response(
                307, b"", {"Location": "https://oauth2.googleapis.com/elsewhere"})),
        ):
            with self.subTest(case=name):
                self.setUp_fresh()
                self.respond(factory)
                self.assert_failure(google_oidc.REASON_REDIRECT)
                self.assertEqual([record["url"] for record in self.google.requests], [google_oidc.TOKEN_ENDPOINT])

    def test_429_and_5xx(self):
        for status in (429, 500, 502, 503, 504):
            with self.subTest(status=status):
                self.setUp_fresh()
                self.respond(lambda: json_response(status, {"error": "temporarily_unavailable"}))
                self.assert_failure()

    def test_any_other_status(self):
        for status in (201, 202, 403, 404, 418):
            with self.subTest(status=status):
                self.setUp_fresh()
                self.respond(lambda: json_response(status, {"id_token": "x"}))
                self.assert_failure(google_oidc.REASON_STATUS)

    def test_a_200_that_carries_an_error_is_a_failure(self):
        token = self.google.id_token(self.claims_json())
        self.respond(lambda: json_response(200, {"error": "invalid_grant", "id_token": token}))
        self.assert_failure(google_oidc.REASON_ERROR_RESPONSE)

    def test_400_invalid_grant_is_a_provider_answer(self):
        self.respond(lambda: json_response(400, {"error": "invalid_grant", "error_description": "DESCRIPTION-SECRET"}))
        with self.assertNoLogs("learnpilot.oidc", logging.DEBUG):
            result = self.gate.exchange(self.request())
        self.assert_refused(result, FAILED)
        self.assertEqual(result.reason, "invalid_grant")
        self.assertEqual((self.failures(), self.cooling()), (0, False))

    def test_401_invalid_client_is_logged_at_error_without_the_description(self):
        for code in ("invalid_client", "unauthorized_client"):
            with self.subTest(code=code):
                self.setUp_fresh()
                self.respond(lambda: json_response(401, {"error": code, "error_description": "DESCRIPTION-SECRET"}))
                with self.assertLogs("learnpilot.oidc", logging.DEBUG) as logs:
                    result = self.gate.exchange(self.request())
                self.assert_refused(result, FAILED)
                self.assertEqual(result.reason, code)
                self.assertEqual(logs.output, [f"ERROR:learnpilot.oidc:provider_rejected_client error={code}"])
                self.assertEqual(self.failures(), 0)

    def test_a_provider_error_code_that_is_not_allow_listed_is_never_repeated(self):
        self.respond(lambda: json_response(400, {"error": "arbitrary text chosen by the provider"}))
        result = self.gate.exchange(self.request())
        self.assert_refused(result, FAILED)
        self.assertEqual(result.reason, google_oidc.REASON_OTHER_PROVIDER_ERROR)

    def test_a_400_without_a_usable_error_is_a_failure(self):
        for document in ({"error": 5}, {"message": "bad"}, {"error": ["invalid_grant"]}):
            with self.subTest(document=document):
                self.setUp_fresh()
                self.respond(lambda: json_response(400, document))
                self.assert_failure(google_oidc.REASON_ERROR_RESPONSE)

    def test_a_connection_error_and_a_tls_error(self):
        cases = {
            "a dropped connection": requests.exceptions.ConnectionError("Connection aborted to https://secret.invalid"),
            "a TLS error": requests.exceptions.SSLError("certificate verify failed for SECRET-HOST"),
            "a connect timeout": requests.exceptions.ConnectTimeout("timed out"),
            "a read timeout": requests.exceptions.ReadTimeout("timed out"),
        }
        for name, error in cases.items():
            with self.subTest(case=name):
                self.setUp_fresh()

                def fail(request, error=error):
                    raise error

                self.google.token_response = fail
                result = self.gate.exchange(self.request())
                self.assert_refused(result, UNAVAILABLE)
                # The class name and nothing of the text.
                self.assertEqual(result.reason, type(error).__name__)
                self.assertEqual(self.failures(), 1)

    def test_one_request_no_retry_and_the_code_is_never_sent_twice(self):
        sent = []
        real_send = requests.Session.send

        def spy(session, request, **kwargs):
            sent.append((request.url, kwargs, session.trust_env, session.verify))
            return real_send(session, request, **kwargs)

        environment = {"HTTPS_PROXY": "http://proxy.invalid:9", "HTTP_PROXY": "http://proxy.invalid:9",
                       "REQUESTS_CA_BUNDLE": "/nonexistent/ca.pem", "CURL_CA_BUNDLE": "/nonexistent/ca.pem"}
        with patch.object(requests.Session, "send", spy), patch.dict(os.environ, environment):
            request = self.request()
            self.assert_ok(self.gate.exchange(request, budget=2.5))
        self.assertEqual([url for url, _, _, _ in sent], [google_oidc.TOKEN_ENDPOINT, google_oidc.JWKS_URI])
        for _, kwargs, trust_env, verify in sent:
            self.assertIs(kwargs["allow_redirects"], False)
            # No proxy, .netrc or CA override from the environment; certificates are verified.
            self.assertIs(trust_env, False)
            self.assertIs(verify, True)
        for record in self.google.requests:
            connect, read = record["timeout"]
            self.assertLessEqual(connect, google_oidc.CONNECT_TIMEOUT)
            self.assertLessEqual(read, google_oidc.READ_TIMEOUT)
            self.assertLessEqual(max(connect, read), 2.5, "no timeout is larger than the time remaining")
            self.assertGreater(min(connect, read), 0)
            self.assertIs(record["verify"], True)
            self.assertFalse(record["proxies"])
            self.assertIsNone(record["cert"])
        # The code went out once, with the client authenticated in the header.
        token_request = self.google.token_requests[0]
        self.assertEqual(token_request["body"].count(request.code), 1)
        self.assertTrue(token_request["headers"]["Authorization"].startswith("Basic "))
        self.assertNotIn(CLIENT_SECRET, token_request["body"])
        self.assertNotIn(CLIENT_SECRET, token_request["url"])
        self.assertIn(VERIFIER, token_request["body"])
        # A second exchange of the same code is refused by the provider, in one request.
        self.assertEqual(self.gate.exchange(request).outcome, FAILED)
        self.assertEqual(len(self.google.token_requests), 2)

    def test_the_transport_is_mounted_without_retries(self):
        adapter = google_oidc._BoundedAdapter(time.monotonic() + 5)
        self.assertEqual(adapter.max_retries.total, 0)
        self.assertIs(adapter.max_retries.read, False)

    def test_no_address_is_ever_taken_from_a_token_or_a_response(self):
        claims = self.claims_json(jwks_uri="https://evil.example/keys", issuer="https://evil.example")
        token = self.google.id_token(claims)
        self.google.token_response = lambda request: json_response(200, {
            "id_token": token, "access_token": "at", "jwks_uri": "https://evil.example/keys",
            "token_endpoint": "https://evil.example/token", "issuer": "https://evil.example",
        }, {"Location": "https://evil.example/", "Link": "<https://evil.example/keys>; rel=jwks"})
        self.google.jwks_response = lambda request: json_response(200, {
            "keys": [{**key.as_dict(private=False), "x5u": "https://evil.example/x5u", "jku": "https://evil.example/jku"}
                     for key in self.google.published],
            "jwks_uri": "https://evil.example/keys"})
        self.assert_ok(self.gate.exchange(self.request()))
        self.assertEqual({record["url"] for record in self.google.requests},
                         {google_oidc.TOKEN_ENDPOINT, google_oidc.JWKS_URI})
        # The members of a key that are not used were dropped before the key was kept.
        with self.gate.lock:
            cached = self.gate.state.keys
        self.assertTrue(all(set(key) <= {"kty", "kid", "use", "alg", "n", "e"} for key in cached))


# --------------------------------------------------------------------------- signing keys


class SigningKeyTest(ServiceTestCase):
    def setUp(self):
        super().setUp()
        self.mono = 10_000.0
        clock = patch.object(google_oidc, "_monotonic", lambda: self.mono)
        clock.start()
        self.addCleanup(clock.stop)

    def cached_keys(self):
        with self.gate.lock:
            return self.gate.state.keys

    def test_the_set_is_cached_within_its_max_age(self):
        self.assert_ok(self.exchange())
        self.mono += 20_999
        self.assert_ok(self.exchange())
        self.assertEqual(len(self.google.jwks_requests), 1)
        self.mono += 2  # past max-age=21000
        self.assert_ok(self.exchange())
        self.assertEqual(len(self.google.jwks_requests), 2)

    def test_the_max_age_is_clamped_and_defaults_to_an_hour(self):
        for header, seconds in ((None, 3600), ("max-age=30", 300), ("public, max-age=999999999", 86400),
                                ("no-transform", 3600), ("s-maxage=10, MAX-AGE = 7200", 7200), (5, 3600)):
            with self.subTest(header=header):
                self.assertEqual(google_oidc._cache_seconds(header), seconds)

    def test_a_new_kid_is_fetched_once(self):
        self.assert_ok(self.exchange())
        rotated = signing_key("rotated-key")
        self.google.published = [self.google.keys[1], rotated]
        self.assert_ok(self.exchange(__key__=rotated))
        self.assertEqual(len(self.google.jwks_requests), 2)
        self.assert_ok(self.exchange(__key__=rotated))
        self.assertEqual(len(self.google.jwks_requests), 2)

    def test_at_most_one_fetch_per_callback(self):
        self.assert_ok(self.exchange())
        unpublished = signing_key("unpublished-key")
        before = len(self.google.jwks_requests)
        result = self.exchange(__key__=unpublished)
        self.assertEqual(result.outcome, FAILED)
        self.assertEqual(len(self.google.jwks_requests), before + 1)

    def test_a_stale_set_is_used_when_a_fetch_fails_within_a_day(self):
        self.assert_ok(self.exchange())
        self.mono += 21_001
        self.google.jwks_response = lambda request: json_response(503, {})
        self.assert_ok(self.exchange())
        self.assertEqual(len(self.google.jwks_requests), 2)
        self.assertEqual(self.failures(), 0)
        # Older than twenty-four hours it is not used any more.
        self.mono += 24 * 3600
        result = self.exchange()
        self.assert_refused(result, UNAVAILABLE, token_requests=3)
        self.assertEqual(self.failures(), 1)

    def test_a_stale_set_without_the_tokens_key_ends_unavailable(self):
        self.assert_ok(self.exchange())
        self.google.jwks_response = lambda request: json_response(503, {})
        result = self.exchange(__key__=signing_key("rotated-key"))
        self.assertEqual((result.outcome, result.reason), (UNAVAILABLE, google_oidc.REASON_STATUS))

    def test_no_set_at_all_ends_unavailable(self):
        self.google.jwks_response = lambda request: json_response(503, {})
        result = self.exchange()
        self.assert_refused(result, UNAVAILABLE)
        self.assertEqual(self.failures(), 1)
        self.assertIsNone(self.cached_keys())

    def test_unusable_key_sets_are_refused(self):
        key = self.google.keys[0].as_dict(private=False)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            small = signing_key("small-key", bits=1024).as_dict(private=False)
        cases = {
            "oversized": lambda: json_response(200, {"keys": [key], "padding": "p" * (64 * 1024)}),
            "not JSON": lambda: http_response(200, b"not json"),
            "an array": lambda: json_response(200, [key]),
            "no keys member": lambda: json_response(200, {"key": key}),
            "an empty list": lambda: json_response(200, {"keys": []}),
            "seventeen keys": lambda: json_response(200, {"keys": [key] * 17}),
            "keys that is no list": lambda: json_response(200, {"keys": {"k": key}}),
            "no RSA key": lambda: json_response(200, {"keys": [{"kty": "EC", "kid": "ec", "crv": "P-256", "x": "x", "y": "y"},
                                                                {"kty": "oct", "kid": "o", "k": "secret"}]}),
            "only an encryption key": lambda: json_response(200, {"keys": [{**key, "use": "enc"}]}),
            "only a key for another algorithm": lambda: json_response(200, {"keys": [{**key, "alg": "RS512"}]}),
            "only a key under 2048 bits": lambda: json_response(200, {"keys": [small]}),
            "a key without a kid": lambda: json_response(200, {"keys": [{k: v for k, v in key.items() if k != "kid"}]}),
            "entries that are no objects": lambda: json_response(200, {"keys": ["key", 5, None]}),
            "a redirect": lambda: http_response(302, b"", {"Location": "https://evil.example/keys"}),
            "a 404": lambda: json_response(404, {"keys": [key]}),
        }
        for name, factory in cases.items():
            with self.subTest(case=name):
                self.google.requests.clear()
                self.set_failures(0)
                self.google.jwks_response = lambda request, factory=factory: factory()
                result = self.exchange()
                self.assertEqual(result.outcome, UNAVAILABLE, name)
                self.assertIsNone(self.cached_keys())
                self.assertEqual([record["url"] for record in self.google.requests],
                                 [google_oidc.TOKEN_ENDPOINT, google_oidc.JWKS_URI])

    def test_a_key_under_2048_bits_is_ignored_beside_a_usable_one(self):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            small = signing_key("small-key", bits=1024)
            self.google.published = [small, self.google.keys[0]]
            self.assert_ok(self.exchange())
            self.assertEqual([key["kid"] for key in self.cached_keys()], ["test-key-1"])
            # A token signed with the small key names a key the set does not hold.
            self.assertEqual(self.exchange(__key__=small).outcome, FAILED)

    def test_a_fetched_set_is_stored_only_when_the_result_arrived_in_time(self):
        release = threading.Event()
        fetched = google_oidc._FetchedKeys(
            keys=tuple(key.as_dict(private=False) for key in self.google.published), max_age=3600)
        identity = google_oidc.VerifiedIdentity("subject", None, None)

        def work(request, keys, deadline):
            release.wait(30)
            return google_oidc._WorkResult(google_oidc._SUCCESS, identity=identity, fetched=fetched)

        gate = self.use(google_oidc.ProviderGate(work=work))
        late = gate.exchange(self.request(), budget=SHORT_BUDGET)
        self.assertEqual((late.outcome, late.reason, late.identity), (UNAVAILABLE, google_oidc.REASON_OVERRUN, None))
        release.set()
        self.finish_workers()
        # The worker went on and returned a fetched set with its result. None of it was read.
        self.assertIsNone(self.cached_keys())
        # The same result in time is stored.
        self.end_cooldown()
        self.assertEqual(gate.exchange(self.request()).outcome, OK)
        self.assertEqual(len(self.cached_keys()), 2)

    def test_the_worker_is_given_a_copy_of_the_cached_set(self):
        self.assert_ok(self.exchange())
        seen = []
        real = self.gate._work

        def work(request, keys, deadline):
            seen.append(keys)
            keys.keys[0]["kid"] = "changed-by-the-worker"
            return real(request, keys, deadline)

        self.gate._work = work
        self.exchange()
        self.assertTrue(seen[0].fresh)
        self.assertEqual([key["kid"] for key in self.cached_keys()], ["test-key-1", "test-key-2"])


# --------------------------------------------------------------------------- the budget and the gate


class BudgetTest(ServiceTestCase):
    def test_a_blocked_endpoint_ends_unavailable_at_the_budget_and_holds_its_slot(self):
        held = self.google.hold()
        started = time.monotonic()
        result = self.exchange(budget=SHORT_BUDGET)
        elapsed = time.monotonic() - started
        self.assertEqual((result.outcome, result.reason, result.identity),
                         (UNAVAILABLE, google_oidc.REASON_OVERRUN, None))
        self.assertGreaterEqual(elapsed, SHORT_BUDGET)
        self.assertLess(elapsed, SHORT_BUDGET + 2.0)
        # The underlying request is not cancelled: the thread goes on and keeps its slot.
        self.assertEqual(len(self.workers()), 1)
        self.assertTrue(self.workers()[0].is_alive())
        self.assertEqual(self.free_slots(), 1)
        self.assertTrue(self.cooling())
        held.set()

    def test_a_late_result_is_discarded(self):
        held = self.google.hold()
        self.exchange(budget=SHORT_BUDGET)
        with self.gate.lock:
            before = {name: getattr(self.gate.state, name) for name in STATE_FIELDS if name != "free_slots"}
        held.set()
        self.finish_workers()
        with self.gate.lock:
            after = {name: getattr(self.gate.state, name) for name in STATE_FIELDS if name != "free_slots"}
        # The key cache and the cooldown are as the overrun left them.
        self.assertEqual(after, before)
        self.assertEqual((after["keys"], after["failures"]), (None, 0))
        self.assertEqual(self.free_slots(), 2)

    def test_what_a_worker_thread_touches(self):
        request_thread = threading.get_ident()
        self.assert_ok(self.exchange())                    # arrives in time
        held = self.google.hold()
        self.end_cooldown()
        self.exchange(budget=SHORT_BUDGET)                 # overruns
        held.set()
        self.finish_workers()
        by_worker = [(name, kind) for name, kind, thread, _ in self.gate.recorded.accesses if thread != request_thread]
        # Its only write beside its own task's result is the slot count. It neither
        # reads nor writes the key cache or the cooldown.
        self.assertEqual(set(by_worker), {("free_slots", "read"), ("free_slots", "write")})
        self.assertEqual(len([entry for entry in by_worker if entry[1] == "write"]), 2)

    def test_worker_exceptions_leave_a_class_name_and_nothing_else(self):
        kept = []

        class TrackedError(RuntimeError):
            """A built-in exception cannot be weakly referenced; a subclass can."""

        def raising(exception_factory):
            def work(request, keys, deadline):
                error = exception_factory()
                try:
                    kept.append(weakref.ref(error))
                except TypeError:
                    kept.append(lambda: None)
                raise error
            return work

        def json_error():
            try:
                json.loads('{"id_token": "TOKEN-SECRET", broken')
            except json.JSONDecodeError as error:
                return error

        cases = {
            "RuntimeError": lambda: RuntimeError("the id token is TOKEN-SECRET"),
            "TrackedError": lambda: TrackedError("the id token is TOKEN-SECRET"),
            "JSONDecodeError": json_error,
            "SystemExit": lambda: SystemExit("TOKEN-SECRET"),
            "KeyboardInterrupt": lambda: KeyboardInterrupt("TOKEN-SECRET"),
        }
        for name, factory in cases.items():
            with self.subTest(case=name):
                gate = self.use(google_oidc.ProviderGate(work=raising(factory)))
                stderr, hook_calls = io.StringIO(), []
                with patch.object(threading, "excepthook", lambda args: hook_calls.append(args)), \
                        redirect_stderr(stderr), self.assertNoLogs(level=logging.DEBUG):
                    result = gate.exchange(self.request())
                    self.finish_workers()
                self.assertEqual((result.outcome, result.reason, result.identity), (UNAVAILABLE, name, None))
                self.assertEqual((stderr.getvalue(), hook_calls), ("", []))
                self.assertEqual(self.free_slots(gate), 2)
                # The exception object is not kept anywhere.
                gc.collect()
                self.assertIsNone(kept[-1]())
                self.assertNotIn("TOKEN-SECRET", repr(result) + repr(result.reason))

    def test_the_provider_lock_is_not_held_during_the_network_call(self):
        held = self.google.hold()
        first = ThreadPoolExecutor(max_workers=1)
        self.addCleanup(first.shutdown)
        pending = first.submit(self.gate.exchange, self.request(), budget=5.0)
        self.assertTrue(self.google.wait_until_entered())
        # While the first call is inside the network function, the lock is free: the
        # state can be read and a second call takes a slot.
        self.assertFalse(self.gate.lock.locked())
        self.assertEqual(self.free_slots(), 1)
        self.assertTrue(self.gate._take_slot())
        self.assertEqual(self.free_slots(), 0)
        with self.gate.lock:
            self.gate.state.free_slots += 1
        held.set()
        self.assert_ok(pending.result(timeout=10))

    def test_two_hundred_calls_from_eight_threads_leave_both_slots_free(self):
        requests_ = [self.request(subject=f"subject-{number}") for number in range(200)]
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(self.gate.exchange, requests_))
        self.finish_workers()
        outcomes = {result.outcome for result in results}
        self.assertLessEqual(outcomes, {OK, UNAVAILABLE})
        self.assertIn(OK, outcomes)
        # A call that found both slots taken was refused at once and made no request.
        refused = [result for result in results if result.outcome == UNAVAILABLE]
        self.assertTrue(all(result.reason == google_oidc.REASON_NO_SLOT for result in refused))
        self.assertEqual(len(self.google.token_requests), 200 - len(refused))
        self.assertEqual(self.free_slots(), 2)
        self.assertGreaterEqual(self.gate.recorded.lowest_free, 0)
        self.assertLessEqual(self.gate.recorded.highest_free, 2)
        self.assertEqual(self.failures(), 0)

    def test_the_worker_function_has_no_application_context_and_no_connection(self):
        touched = {}

        def work(request, keys, deadline):
            for name, touch in (("session", lambda: flask.session.get("uid")), ("g", lambda: flask.g.get("db")),
                                ("request", lambda: flask.request.path),
                                ("current_app", lambda: flask.current_app.config)):
                try:
                    touch()
                    touched[name] = "worked"
                except RuntimeError:
                    touched[name] = "raised"
            return google_oidc.fetch_identity(request, keys, deadline)

        gate = self.use(google_oidc.ProviderGate(work=work))
        app = flask.Flask(__name__)
        app.secret_key = "test"
        with app.test_request_context("/"):
            self.assert_ok(gate.exchange(self.request()))
        self.assertEqual(touched, {"session": "raised", "g": "raised", "request": "raised", "current_app": "raised"})
        # Plain values in, a value out: there is no place for a connection.
        self.assertEqual(list(inspect.signature(google_oidc.fetch_identity).parameters), ["request", "keys", "deadline"])
        self.assertEqual(list(inspect.signature(google_oidc.ProviderGate._run).parameters),
                         ["self", "task", "request", "keys", "deadline"])

    def test_the_module_imports_neither_flask_nor_sqlite3_nor_the_application(self):
        tree = ast.parse((ROOT / "web/services/google_oidc.py").read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add((node.module or "").split(".")[0])
        self.assertEqual(imported & {"flask", "sqlite3", "web", "src", "werkzeug"}, set())
        self.assertLessEqual({"authlib", "joserfc", "requests"}, imported)

    def test_two_blocked_calls_hold_both_slots_and_the_third_is_refused_at_once(self):
        held = self.google.hold()
        pool = ThreadPoolExecutor(max_workers=2)
        self.addCleanup(pool.shutdown)
        first = pool.submit(self.gate.exchange, self.request(subject="one"), budget=5.0)
        second = pool.submit(self.gate.exchange, self.request(subject="two"), budget=5.0)
        deadline = time.monotonic() + 5
        while len(self.google.token_requests) < 2 and time.monotonic() < deadline:
            time.sleep(0.005)
        self.assertEqual((len(self.google.token_requests), self.free_slots()), (2, 0))
        threads = threading.active_count()
        third = self.gate.exchange(self.request(subject="three"), budget=5.0)
        self.assertEqual((third.outcome, third.reason), (UNAVAILABLE, google_oidc.REASON_NO_SLOT))
        # No thread was started and no request was made for it.
        self.assertEqual((threading.active_count(), len(self.google.token_requests)), (threads, 2))
        self.assertEqual(self.failures(), 0)
        held.set()
        self.assert_ok(first.result(timeout=10), "one")
        self.assert_ok(second.result(timeout=10), "two")
        self.finish_workers()
        self.assertEqual(self.free_slots(), 2)
        self.assert_ok(self.exchange(subject="four"), "four")

    def test_fifty_overruns_in_a_row_never_add_more_than_two_threads(self):
        releases = []

        def work(request, keys, deadline):
            release = threading.Event()
            releases.append(release)
            release.wait(30)
            return google_oidc._WorkResult(google_oidc._FAILURE, reason="late")

        gate = self.use(google_oidc.ProviderGate(work=work))
        baseline = threading.active_count()
        request = google_oidc.ExchangeRequest("c", VERIFIER, NONCE, REDIRECT_URI, CLIENT_ID, CLIENT_SECRET)
        for number in range(50):
            self.end_cooldown()
            result = gate.exchange(request, budget=0.01)
            self.assertEqual(result.reason, google_oidc.REASON_OVERRUN, number)
            self.assertLessEqual(threading.active_count(), baseline + 2)
            if number % 2:
                # Two abandoned workers hold both slots: let them end before the next pair.
                for release in releases:
                    release.set()
                self.finish_workers()
                releases.clear()
        self.assertEqual(threading.active_count(), baseline)

    def test_the_budget_covers_the_key_fetch(self):
        mono = [50_000.0]

        def token_then_time_passes(request):
            mono[0] += google_oidc.NETWORK_BUDGET - google_oidc.MIN_TIME_FOR_REQUEST + 0.1
            return self.google._token(request, request.body)

        self.google.token_response = token_then_time_passes
        with patch.object(google_oidc, "_monotonic", lambda: mono[0]):
            result = self.exchange()
        # The exchange left less than two seconds: no second request is started.
        self.assertEqual((result.outcome, result.reason), (UNAVAILABLE, google_oidc.REASON_BUDGET))
        self.assertEqual(self.google.jwks_requests, [])

    def test_a_slow_name_lookup_ends_unavailable_at_the_budget(self):
        looked_up, release = threading.Event(), threading.Event()

        def slow_lookup(host, *args, **kwargs):
            looked_up.set()
            release.wait(30)
            raise socket.gaierror("name resolution is not available in tests")

        # The real transport, with only the resolver replaced: a name lookup is
        # outside every timeout of requests.
        with patch.object(HTTPAdapter, "send", self.real_transport), patch.object(socket, "getaddrinfo", slow_lookup):
            result = self.exchange(budget=SHORT_BUDGET)
            self.assertEqual((result.outcome, result.reason), (UNAVAILABLE, google_oidc.REASON_OVERRUN))
            self.assertTrue(looked_up.is_set())
            self.assertEqual(len(self.workers()), 1)
            release.set()
            self.finish_workers()
        self.assertEqual(self.free_slots(), 2)

    def test_a_body_delivered_slowly_is_abandoned_by_the_reader_at_the_deadline(self):
        mono = [70_000.0]
        reads = []

        class Dripping:
            """One byte for every half second of the replaced clock, for ever."""

            def read1(self, amt=None, **kwargs):
                mono[0] += 0.5
                reads.append(mono[0])
                return b" "

            def close(self):
                pass

            def release_conn(self):
                pass

        self.google.token_response = lambda request: http_response(200, raw=Dripping())
        with patch.object(google_oidc, "_monotonic", lambda: mono[0]):
            result = self.exchange()
        self.assertEqual((result.outcome, result.reason), (UNAVAILABLE, google_oidc.REASON_BUDGET))
        # Abandoned at the deadline, not when the body ended (it never does).
        self.assertEqual(len(reads), int(google_oidc.NETWORK_BUDGET / 0.5))
        self.assertEqual(self.google.jwks_requests, [])


class WorkersInsideTheFunctionTest(ServiceTestCase):
    """The bound is on workers inside the network function, not on thread objects."""

    def test_three_worker_threads_exist_and_only_two_are_inside_the_function(self):
        first_may_begin, inside, release = threading.Event(), threading.Semaphore(0), threading.Event()
        entered = []

        def work(request, keys, deadline):
            entered.append(request.code)
            inside.release()
            release.wait(30)
            return google_oidc._WorkResult(google_oidc._FAILURE, reason="late")

        class StartsThenRaises(threading.Thread):
            def start(self):
                super().start()
                raise RuntimeError("raised after the thread exists")

        gate = self.use(google_oidc.ProviderGate(work=work, thread_factory=StartsThenRaises))
        real_begin = gate._begin
        gate._begin = lambda task: (first_may_begin.wait(30), real_begin(task))[1]

        def request(code):
            return google_oidc.ExchangeRequest(code, VERIFIER, NONCE, REDIRECT_URI, CLIENT_ID, CLIENT_SECRET)

        with self.assertLogs("learnpilot.oidc", logging.WARNING):
            failed = gate.exchange(request("first"))
        self.assertEqual((failed.outcome, failed.reason), (UNAVAILABLE, google_oidc.REASON_START_FAILED))
        # Its thread exists and is held before its first action; the request thread
        # gave the slot back.
        self.assertEqual((len(self.workers()), self.free_slots(gate)), (1, 2))

        gate._begin = real_begin
        gate._thread_factory = threading.Thread
        pool = ThreadPoolExecutor(max_workers=2)
        self.addCleanup(pool.shutdown)
        others = [pool.submit(gate.exchange, request(code), budget=0.3) for code in ("second", "third")]
        self.assertTrue(inside.acquire(timeout=5) and inside.acquire(timeout=5))
        # Three worker threads, two of them inside the function, and no free slot.
        self.assertEqual((len(self.workers()), gate.inside, self.free_slots(gate)), (3, 2, 0))
        first_may_begin.set()
        for other in others:
            self.assertEqual(other.result(timeout=10).reason, google_oidc.REASON_OVERRUN)
        release.set()
        self.finish_workers()
        # The first thread found its task abandoned, never entered the function, and
        # its own release changed nothing.
        self.assertEqual(sorted(entered), ["second", "third"])
        self.assertEqual((self.free_slots(gate), gate.most_inside, gate.recorded.highest_free), (2, 2, 2))


class StartFailureTest(ServiceTestCase):
    """A slot is given back exactly once, whatever happens between it and ``start()``."""

    def plain_request(self, code="code"):
        return google_oidc.ExchangeRequest(code, VERIFIER, NONCE, REDIRECT_URI, CLIENT_ID, CLIENT_SECRET)

    def assert_start_failed(self, gate, error_name, *, requests_made=0):
        with self.assertLogs("learnpilot.oidc", logging.DEBUG) as logs:
            result = gate.exchange(self.request())
        self.assertEqual((result.outcome, result.reason, result.identity),
                         (UNAVAILABLE, google_oidc.REASON_START_FAILED, None))
        # One line, with the class name and nothing of the exception's text.
        self.assertEqual(logs.output, [f"WARNING:learnpilot.oidc:provider_start_failed error={error_name}"])
        self.assertEqual(len(self.google.token_requests), requests_made)
        return result

    def test_a_start_that_raises_without_a_thread(self):
        class NeverStarts(threading.Thread):
            def start(self):
                raise RuntimeError("can't start new thread: SECRET-TEXT")

        gate = self.use(google_oidc.ProviderGate(thread_factory=NeverStarts))
        baseline = threading.active_count()
        for _ in range(10):
            self.assert_start_failed(gate, "RuntimeError")
            self.assertEqual((self.free_slots(gate), threading.active_count()), (2, baseline))
        # The next call succeeds: nothing was lost.
        gate._thread_factory = threading.Thread
        self.assert_ok(gate.exchange(self.request()))

    def test_an_exception_between_the_slot_and_the_start(self):
        def cannot_build(**kwargs):
            raise MemoryError("SECRET-TEXT")

        gate = self.use(google_oidc.ProviderGate(thread_factory=cannot_build))
        baseline = threading.active_count()
        self.assert_start_failed(gate, "MemoryError")
        self.assertEqual((self.free_slots(gate), threading.active_count()), (2, baseline))
        gate._thread_factory = threading.Thread
        self.assert_ok(gate.exchange(self.request()))

    def test_a_system_exit_between_the_slot_and_the_start_is_raised_again_after_the_slot_is_back(self):
        seen = []

        def exits(**kwargs):
            seen.append(self.free_slots(gate))
            raise SystemExit(1)

        gate = self.use(google_oidc.ProviderGate(thread_factory=exits))
        with self.assertLogs("learnpilot.oidc", logging.WARNING), self.assertRaises(SystemExit):
            gate.exchange(self.request())
        self.assertEqual((seen, self.free_slots(gate), self.google.token_requests), ([1], 2, []))

    def test_no_statement_stands_between_the_taking_of_the_slot_and_the_guarded_region(self):
        tree = ast.parse((ROOT / "web/services/google_oidc.py").read_text(encoding="utf-8"))
        exchange = next(node for node in ast.walk(tree)
                        if isinstance(node, ast.FunctionDef) and node.name == "exchange"
                        and any(isinstance(inner, ast.Try) for inner in node.body))
        takes = [index for index, node in enumerate(exchange.body)
                 if isinstance(node, ast.If) and "_take_slot" in ast.unparse(node.test)]
        self.assertEqual(len(takes), 1)
        taking = exchange.body[takes[0]]
        self.assertEqual(ast.unparse(taking.test), "not self._take_slot()")
        self.assertIsInstance(taking.body[-1], ast.Return)
        region = exchange.body[takes[0] + 1]
        self.assertIsInstance(region, ast.Try)
        # The region ends when start() has returned, and it catches everything.
        self.assertIn("start()", ast.unparse(region.body[-1]))
        self.assertEqual(ast.unparse(region.handlers[0].type), "BaseException")
        # The task exists before the slot is taken.
        before = "\n".join(ast.unparse(node) for node in exchange.body[:takes[0]])
        self.assertIn("task = _Task()", before)

    def raising_after_start(self, error, *, wait_for=None):
        class StartsThenRaises(threading.Thread):
            def start(self):
                super().start()
                if wait_for is not None:
                    wait_for.wait(30)
                raise error

        return StartsThenRaises

    def test_a_start_that_raises_after_the_thread_exists_before_the_worker_has_begun(self):
        may_begin = threading.Event()
        gate = self.use(google_oidc.ProviderGate(thread_factory=self.raising_after_start(RuntimeError("late"))))
        real_begin = gate._begin
        begun = []

        def begin(task):
            may_begin.wait(30)
            begun.append(real_begin(task))
            return begun[-1]

        gate._begin = begin
        self.assert_start_failed(gate, "RuntimeError")
        # The request thread gave the slot back; the worker is still before its first action.
        self.assertEqual((self.free_slots(gate), len(self.workers())), (2, 1))
        may_begin.set()
        self.finish_workers()
        # The worker found the task abandoned and made no request; its own release
        # changed nothing.
        self.assertEqual((begun, self.google.token_requests), ([False], []))
        self.assertEqual((self.free_slots(gate), gate.recorded.highest_free), (2, 2))

    def test_a_start_that_raises_after_the_worker_has_begun_leaves_the_slot_with_the_worker(self):
        worker_inside = threading.Event()
        held = self.google.hold()
        gate = self.use(google_oidc.ProviderGate())
        # Another call already holds the first slot, blocked inside the function.
        pool = ThreadPoolExecutor(max_workers=1)
        self.addCleanup(pool.shutdown)
        blocked = pool.submit(gate.exchange, self.request(subject="blocked"), budget=5.0)
        self.assertTrue(self.google.wait_until_entered())

        real_work = gate._work

        def work(request, keys, deadline):
            worker_inside.set()
            return real_work(request, keys, deadline)

        gate._work = work
        gate._thread_factory = self.raising_after_start(RuntimeError("late"), wait_for=worker_inside)
        self.assert_start_failed(gate, "RuntimeError", requests_made=2)
        # The request thread did not give the slot back: the worker is inside the function.
        self.assertEqual(self.free_slots(gate), 0)
        gate._thread_factory = threading.Thread
        further = gate.exchange(self.request(subject="further"))
        self.assertEqual((further.outcome, further.reason), (UNAVAILABLE, google_oidc.REASON_NO_SLOT))
        held.set()
        self.assert_ok(blocked.result(timeout=10), "blocked")
        self.finish_workers()
        # The worker's result was discarded, and the count is exactly two, never three.
        self.assertEqual((self.free_slots(gate), gate.recorded.highest_free), (2, 2))
        with gate.lock:
            self.assertEqual(gate.state.failures, 0)

    def test_a_system_exit_raised_by_start_after_the_thread_exists_is_raised_again(self):
        gate = self.use(google_oidc.ProviderGate(thread_factory=self.raising_after_start(SystemExit(1))))
        with self.assertLogs("learnpilot.oidc", logging.WARNING), self.assertRaises(SystemExit):
            gate.exchange(self.request())
        self.finish_workers()
        self.assertEqual((self.free_slots(gate), gate.recorded.highest_free), (2, 2))

    def test_the_release_function_changes_the_count_once_for_one_task(self):
        gate = self.gate
        task = google_oidc._Task()
        self.assertTrue(gate._take_slot())
        self.assertEqual(self.free_slots(), 1)
        gate._return_slot(task)
        gate._return_slot(task)
        gate._return_slot(task)
        self.assertEqual((self.free_slots(), gate.recorded.highest_free), (2, 2))

    def test_two_hundred_calls_with_the_three_kinds_of_start_mixed_never_exceed_two_free_slots(self):
        counter = [0]
        lock = threading.Lock()

        class Mixed(threading.Thread):
            def start(self):
                with lock:
                    counter[0] += 1
                    kind = counter[0] % 3
                if kind == 0:
                    raise RuntimeError("no thread")
                super().start()
                if kind == 1:
                    raise RuntimeError("after the thread exists")

        gate = self.use(google_oidc.ProviderGate(thread_factory=Mixed))
        requests_ = [self.request(subject=f"subject-{number}") for number in range(200)]
        with self.assertLogs("learnpilot.oidc", logging.WARNING), ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(gate.exchange, requests_))
        self.finish_workers()
        self.assertEqual(len(results), 200)
        self.assertEqual((self.free_slots(gate), gate.recorded.lowest_free >= 0), (2, True))
        self.assertLessEqual(gate.recorded.highest_free, 2)
        self.assertLessEqual(gate.most_inside, 2)


class WorkerReleaseTest(ServiceTestCase):
    """The worker's release is in a ``finally`` from its first statement."""

    def exchange_with_a_failing_action(self, gate):
        stderr, hook_calls = io.StringIO(), []
        with patch.object(threading, "excepthook", lambda args: hook_calls.append(args)), redirect_stderr(stderr):
            result = gate.exchange(self.request(), budget=SHORT_BUDGET)
            self.finish_workers()
        # No result was handed over: the request answers at its deadline, an overrun.
        self.assertEqual((result.outcome, result.reason), (UNAVAILABLE, google_oidc.REASON_OVERRUN))
        self.assertEqual((stderr.getvalue(), hook_calls), ("", []))
        self.assertEqual((self.free_slots(gate), gate.recorded.highest_free), (2, 2))
        with gate.lock:
            self.assertGreater(gate.state.cooldown_until, 0)
        return result

    def test_the_first_action_raises_before_it_marked_that_it_had_begun(self):
        gate = self.use(google_oidc.ProviderGate())

        def begin(task):
            raise RuntimeError("before begun")

        gate._begin = begin
        self.exchange_with_a_failing_action(gate)
        self.assertEqual(self.google.token_requests, [])

    def test_the_first_action_raises_after_it_marked_that_it_had_begun(self):
        gate = self.use(google_oidc.ProviderGate())
        real_begin = gate._begin

        def begin(task):
            real_begin(task)
            raise RuntimeError("after begun")

        gate._begin = begin
        self.exchange_with_a_failing_action(gate)
        self.assertEqual(self.google.token_requests, [])

    def test_the_second_action_raises(self):
        gate = self.use(google_oidc.ProviderGate())

        def hand_over(task, result):
            raise RuntimeError("the result holds a TOKEN-SECRET")

        gate._hand_over = hand_over
        self.exchange_with_a_failing_action(gate)
        self.assertEqual(len(self.google.token_requests), 1)

    def test_combined_with_a_start_that_raised_the_count_ends_at_exactly_two(self):
        for when in ("before begun", "after begun"):
            with self.subTest(first_action_raises=when):
                may_begin = threading.Event()

                class StartsThenRaises(threading.Thread):
                    def start(self):
                        super().start()
                        if when == "after begun":
                            may_begin.set()
                            reached.wait(30)
                        raise RuntimeError("after the thread exists")

                reached = threading.Event()
                gate = self.use(google_oidc.ProviderGate(thread_factory=StartsThenRaises))
                real_begin = gate._begin

                def begin(task):
                    may_begin.wait(30)
                    if when == "after begun":
                        real_begin(task)
                        reached.set()
                    raise RuntimeError("the first action failed")

                gate._begin = begin
                stderr, hook_calls = io.StringIO(), []
                with patch.object(threading, "excepthook", lambda args: hook_calls.append(args)), \
                        redirect_stderr(stderr), self.assertLogs("learnpilot.oidc", logging.WARNING):
                    result = gate.exchange(self.request())
                    may_begin.set()
                    self.finish_workers()
                self.assertEqual(result.reason, google_oidc.REASON_START_FAILED)
                self.assertEqual((stderr.getvalue(), hook_calls), ("", []))
                self.assertEqual((self.free_slots(gate), gate.recorded.highest_free), (2, 2))
                self.assertEqual(self.google.token_requests, [])


# --------------------------------------------------------------------------- the cooldown


class CooldownTest(ServiceTestCase):
    def setUp(self):
        super().setUp()
        self.mono = 30_000.0
        clock = patch.object(google_oidc, "_monotonic", lambda: self.mono)
        clock.start()
        self.addCleanup(clock.stop)

    def fail(self):
        self.google.token_response = lambda request: json_response(503, {})
        result = self.exchange()
        self.google.token_response = None
        self.assertEqual(result.outcome, UNAVAILABLE)
        return result

    def answer(self):
        self.google.token_response = lambda request: json_response(400, {"error": "invalid_grant"})
        result = self.exchange()
        self.google.token_response = None
        self.assertEqual(result.outcome, FAILED)

    def refuse_for_lack_of_a_slot(self):
        with self.gate.lock:
            self.gate.state.free_slots -= 2
        result = self.exchange()
        with self.gate.lock:
            self.gate.state.free_slots += 2
        self.assertEqual(result.reason, google_oidc.REASON_NO_SLOT)

    def fail_to_start(self, factory):
        real = self.gate._thread_factory
        self.gate._thread_factory = factory
        with self.assertLogs("learnpilot.oidc", logging.WARNING):
            result = self.exchange()
        self.gate._thread_factory = real
        self.finish_workers()
        self.assertEqual(result.reason, google_oidc.REASON_START_FAILED)

    def no_thread(self, **kwargs):
        class NeverStarts(threading.Thread):
            def start(self):
                raise RuntimeError("no thread")

        return NeverStarts(**kwargs)

    def cannot_build(self, **kwargs):
        raise RuntimeError("no thread object")

    def thread_then_raise(self, **kwargs):
        class StartsThenRaises(threading.Thread):
            def start(self):
                super().start()
                raise RuntimeError("after the thread exists")

        return StartsThenRaises(**kwargs)

    def overrun(self):
        held = self.google.hold()
        real = google_oidc._monotonic
        # Only the wait of the request thread needs real time.
        result = self.exchange(budget=SHORT_BUDGET)
        held.set()
        self.finish_workers()
        self.google._held = None
        self.assertEqual(result.reason, google_oidc.REASON_OVERRUN)
        del real

    def test_three_consecutive_failures_start_it(self):
        self.fail()
        self.fail()
        self.assertEqual((self.failures(), self.cooling()), (2, False))
        self.fail()
        self.assertTrue(self.cooling())
        # The start of a cooldown sets the count to zero.
        self.assertEqual(self.failures(), 0)
        refused = self.exchange()
        self.assertEqual((refused.outcome, refused.reason), (UNAVAILABLE, google_oidc.REASON_COOLDOWN))
        self.assertEqual(len(self.google.token_requests), 3)

    def test_two_failures_and_a_provider_answer_do_not(self):
        self.fail()
        self.fail()
        self.answer()
        self.assertEqual((self.failures(), self.cooling()), (0, False))
        self.fail()
        self.fail()
        self.assertFalse(self.cooling())

    def test_one_overrun_starts_it(self):
        self.overrun()
        self.assertTrue(self.cooling())
        self.assertEqual(self.failures(), 0)

    def test_after_sixty_seconds_calls_are_made_as_before(self):
        for _ in range(3):
            self.fail()
        self.mono += 59.9
        self.assertTrue(self.cooling())
        self.mono += 0.2
        self.assertFalse(self.cooling())
        # The first call afterwards is not special: there is no probe. Two calls may
        # both go out, a failure counts as one, and it takes three in a row again.
        before = len(self.google.token_requests)
        self.fail()
        self.fail()
        self.assertEqual((len(self.google.token_requests), self.failures(), self.cooling()), (before + 2, 2, False))
        self.fail()
        self.assertTrue(self.cooling())
        self.mono += 61
        self.assert_ok(self.exchange())
        self.overrun()
        self.assertTrue(self.cooling())

    def test_what_resets_the_count(self):
        resets = {
            "a provider answer": self.answer,
            "a token that fails validation": lambda: self.assertEqual(self.exchange(nonce="x" * 43).outcome, FAILED),
            "a success": lambda: self.assert_ok(self.exchange()),
        }
        for name, reset in resets.items():
            with self.subTest(reset=name):
                self.fail()
                self.fail()
                self.assertEqual(self.failures(), 2)
                reset()
                self.assertEqual((self.failures(), self.cooling()), (0, False))

    def test_what_leaves_the_count_exactly_as_it_was(self):
        neutral = {
            "a refusal for lack of a slot": self.refuse_for_lack_of_a_slot,
            "an exception between the slot and the start": lambda: self.fail_to_start(self.cannot_build),
            "a start that raised without a thread": lambda: self.fail_to_start(self.no_thread),
            "a start that raised after the thread exists": lambda: self.fail_to_start(self.thread_then_raise),
        }
        for name, event in neutral.items():
            for count in (0, 1, 2):
                with self.subTest(event=name, count=count):
                    self.set_failures(count)
                    event()
                    # Neither increased nor reset to zero.
                    self.assertEqual((self.failures(), self.cooling()), (count, False))
            with self.subTest(event=name, then="a third failure starts the cooldown"):
                self.set_failures(0)
                self.fail()
                self.fail()
                event()
                self.assertFalse(self.cooling(), "alone it does not start one: it was not counted")
                self.fail()
                self.assertTrue(self.cooling(), "the count was not reset")
                self.end_cooldown()


class CooldownAtTheApplicationTest(GoogleAppTestCase):
    """During the sixty seconds (design 5.8)."""

    def cool_down(self):
        with google_oidc.GATE.lock:
            google_oidc.GATE.state.cooldown_until = time.monotonic() + 60

    def test_start_and_link_create_no_transaction(self):
        self.cool_down()
        self.assert_redirect(self.start(), "/login?google=unavailable")
        self.assertEqual(self.session(), {})
        self.signed_in("teacher")
        before = self.session()
        self.assert_redirect(self.start_link(), "/account?google=unavailable")
        self.assertEqual(self.session(), before)
        self.assertEqual(self.google.requests, [])

    def test_a_callback_is_matched_and_recorded_and_makes_no_request(self):
        started = self.start()
        transaction = self.session()[oauth_state.TX_SLOT]
        code = self.google.authorize(started.headers["Location"])
        self.cool_down()
        with self.assertLogs("learnpilot.oidc", logging.WARNING) as logs:
            answered = self.callback(code=code, state=transaction["state"])
        self.assert_redirect(answered, "/login?google=unavailable")
        self.assertEqual(self.google.requests, [])
        self.assertEqual(self.consumption_rows(),
                         [(self.consumption_key("callback", transaction["state"]), transaction["exp"])])
        self.assertEqual(self.session(), {})
        self.assertIn("reason=cooldown", logs.output[0])
        self.assertEqual(self.user_count(), 0)


if __name__ == "__main__":
    unittest.main()
