"""Google as an OpenID Connect provider (docs/reviews/GOOGLE_OIDC_DESIGN.md, sections 5 and 8.1).

Pinned provider metadata, the rules for the two credential settings, the build's
activation constant, the authorization request, the code exchange, the signing keys,
the ID-token checks, and the provider gate that puts all network work of one callback
under one budget.

No Flask and no database: nothing here reads a request, a session or a connection.
The OAuth and JOSE work is Authlib's and joserfc's. What this module owns is what they
do not check: the shape of the provider's responses, the types of the claims, the
bound on every body and the bound on the time a callback may wait.

Never logged, kept or put into an exception text: the authorization code, the PKCE
verifier, the nonce, a token, the client secret, a subject, an email, a provider's
error description. A failure is known by a fixed word or by an exception's class name.
"""

from __future__ import annotations

import base64
import json
import logging
import re
import threading
import time
from dataclasses import dataclass, field

import requests
from authlib.integrations.requests_client import OAuth2Session
from authlib.oidc.core import CodeIDToken
from joserfc import jwt
from joserfc.errors import InvalidKeyIdError
from joserfc.jwk import KeySet
from joserfc.jws import JWSRegistry
from requests.adapters import HTTPAdapter

logger = logging.getLogger("learnpilot.oidc")

# --------------------------------------------------------------------------- activation
#
# The build's activation gate (design 8.1, decision P2). While it is False a production
# start keeps Google sign-in disabled whatever the two credential settings hold. The
# release that delivers the identity recovery tooling (design 10) sets it to True. No
# setting and no environment variable overrides it.
PRODUCTION_ACTIVATION = False

# --------------------------------------------------------------------------- metadata
#
# Equal to Google's discovery document on 2026-10-11. Pinned on purpose: no URL is ever
# taken from a token, a callback parameter, a response body or a redirect.
ISSUER = "https://accounts.google.com"
# Google documents both spellings for the ``iss`` claim. Both exact strings are
# accepted, and ISSUER is what is stored: a closed mapping of two literals to one
# constant, not a normalization.
ACCEPTED_ISSUERS = frozenset({ISSUER, "accounts.google.com"})
AUTHORIZATION_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
JWKS_URI = "https://www.googleapis.com/oauth2/v3/certs"
ID_TOKEN_ALGORITHM = "RS256"
SCOPE = "openid email profile"
PROVIDER = "google"
CALLBACK_PATH = "/auth/google/callback"

# --------------------------------------------------------------------------- configuration

CLIENT_ID_ENV = "DZI_GOOGLE_CLIENT_ID"
CLIENT_SECRET_ENV = "DZI_GOOGLE_CLIENT_SECRET"
CLIENT_ID_SUFFIX = ".apps.googleusercontent.com"
MAX_SETTING_LENGTH = 255
MIN_CLIENT_SECRET_LENGTH = 16


def _is_visible_ascii(value, minimum: int, maximum: int) -> bool:
    return (
        isinstance(value, str)
        and minimum <= len(value) <= maximum
        and all("!" <= ch <= "~" for ch in value)
    )


def credential_problems(client_id, client_secret) -> list[str]:
    """Why these two settings cannot enable the provider. Names settings, never values."""
    problems = []
    if not client_id:
        problems.append(f"{CLIENT_ID_ENV} is not set")
    elif not (
        _is_visible_ascii(client_id, len(CLIENT_ID_SUFFIX) + 1, MAX_SETTING_LENGTH)
        and client_id.endswith(CLIENT_ID_SUFFIX)
    ):
        problems.append(
            f"{CLIENT_ID_ENV} must be a Google OAuth client id (visible ASCII, at most "
            f"{MAX_SETTING_LENGTH} characters, ending in {CLIENT_ID_SUFFIX})"
        )
    if not client_secret:
        problems.append(f"{CLIENT_SECRET_ENV} is not set")
    elif not _is_visible_ascii(client_secret, MIN_CLIENT_SECRET_LENGTH, MAX_SETTING_LENGTH):
        problems.append(
            f"{CLIENT_SECRET_ENV} must be {MIN_CLIENT_SECRET_LENGTH} to {MAX_SETTING_LENGTH} "
            "visible ASCII characters"
        )
    return problems


@dataclass(frozen=True)
class ProviderConfig:
    """An enabled provider: both credentials and the base URL the callback is built on."""

    client_id: str
    client_secret: str = field(repr=False)
    public_base_url: str

    @property
    def redirect_uri(self) -> str:
        return self.public_base_url + CALLBACK_PATH


def provider_config(client_id, client_secret, public_base_url) -> ProviderConfig | None:
    """The enabled provider, or ``None`` when anything it needs is missing or malformed.

    ``public_base_url`` is the configured base (``web.public_url``), already in its
    canonical form. It is never derived from a request.
    """
    if credential_problems(client_id, client_secret):
        return None
    if not isinstance(public_base_url, str) or not public_base_url:
        return None
    return ProviderConfig(client_id, client_secret, public_base_url)


def authorization_url(config: ProviderConfig, *, state: str, nonce: str, code_verifier: str) -> str:
    """The redirect to Google for one transaction (design 1.3).

    Sent: ``response_type=code``, the client id, the configured redirect URI, the scope,
    ``state``, ``nonce``, the S256 challenge of the verifier and
    ``prompt=select_account``. Not sent: ``access_type`` (so no refresh token is
    issued), ``hd``, ``login_hint``, ``include_granted_scopes``, ``response_mode``.
    """
    client = OAuth2Session(
        config.client_id, scope=SCOPE, redirect_uri=config.redirect_uri, code_challenge_method="S256"
    )
    try:
        url, _ = client.create_authorization_url(
            AUTHORIZATION_ENDPOINT,
            state=state,
            code_verifier=code_verifier,
            nonce=nonce,
            prompt="select_account",
        )
    finally:
        client.close()
    return url


# --------------------------------------------------------------------------- limits

NETWORK_BUDGET = 8.0  # seconds, for everything one callback does on the network
PROVIDER_SLOTS = 2  # workers inside the network function, per process
COOLDOWN_SECONDS = 60.0
FAILURES_BEFORE_COOLDOWN = 3
CONNECT_TIMEOUT = 2.0
READ_TIMEOUT = 3.0
# No second request (the key fetch) starts with less than this left of the budget.
MIN_TIME_FOR_REQUEST = 2.0
MAX_RESPONSE_BYTES = 64 * 1024
MAX_ID_TOKEN_LENGTH = 16 * 1024
MAX_KEYS = 16
MIN_RSA_MODULUS_BITS = 2048
KEY_CACHE_MIN_SECONDS = 5 * 60
KEY_CACHE_MAX_SECONDS = 24 * 3600
KEY_CACHE_DEFAULT_SECONDS = 3600
# A set whose refresh failed is still used while it is younger than this.
STALE_KEYS_SECONDS = 24 * 3600
TOKEN_LEEWAY_SECONDS = 60
MAX_TOKEN_AGE_SECONDS = 600
MAX_SUBJECT_LENGTH = 255
_MAX_TIME_CLAIM = 2**40
_READ_SIZE = 16 * 1024

OUTCOME_OK = "ok"
OUTCOME_FAILED = "failed"
OUTCOME_UNAVAILABLE = "unavailable"

# Fixed words for why a call did not succeed. Together with exception class names and
# the allow-listed provider error codes they are everything a reason can be.
REASON_COOLDOWN = "cooldown"
REASON_NO_SLOT = "no_slot"
REASON_START_FAILED = "start_failed"
REASON_OVERRUN = "overrun"
REASON_BUDGET = "budget"
REASON_REDIRECT = "redirect"
REASON_STATUS = "status"
REASON_BODY_TOO_LARGE = "body_too_large"
REASON_ENCODING = "content_encoding"
REASON_NOT_JSON = "not_json"
REASON_NOT_AN_OBJECT = "not_an_object"
REASON_ERROR_RESPONSE = "error_response"
REASON_NO_ID_TOKEN = "id_token_missing"
REASON_KEYS = "signing_keys"
REASON_OTHER_PROVIDER_ERROR = "provider_error"
REASON_CLAIMS = "claims"

# RFC 6749 section 5.2. Anything else a provider puts into ``error`` is not repeated.
PROVIDER_ERROR_CODES = frozenset({
    "invalid_request", "invalid_client", "invalid_grant", "unauthorized_client",
    "unsupported_grant_type", "invalid_scope",
})
# These two mean that the configured credentials are wrong.
CREDENTIAL_ERROR_CODES = frozenset({"invalid_client", "unauthorized_client"})


def _monotonic() -> float:
    """The clock of the budget, the key cache and the cooldown."""
    return time.monotonic()


def _wall_now() -> int:
    """The clock an ID token's ``exp`` and ``iat`` are compared with."""
    return int(time.time())


# --------------------------------------------------------------------------- values


@dataclass(frozen=True, repr=False)
class ExchangeRequest:
    """What one code exchange needs, as plain values handed to the worker thread."""

    code: str
    code_verifier: str
    nonce: str
    redirect_uri: str
    client_id: str
    client_secret: str


@dataclass(frozen=True, repr=False)
class VerifiedIdentity:
    """A verified ID token, reduced to what the application uses.

    ``subject`` passed every check. ``email`` and ``name`` are metadata: each is the
    claim as Google reported it when that is a string, else ``None``. They take no part
    in the validation, and ``web.services.oidc_accounts`` decides what becomes of them.
    The issuer is always :data:`ISSUER`.
    """

    subject: str
    email: str | None
    name: str | None


@dataclass(frozen=True, repr=False)
class ExchangeResult:
    """How a call ended: ``ok`` with an identity, ``failed`` or ``unavailable``."""

    outcome: str
    identity: VerifiedIdentity | None = None
    reason: str | None = None


@dataclass(frozen=True, repr=False)
class KeySnapshot:
    """A copy of the cached signing keys for one worker.

    ``keys`` is ``None`` when the process has no set younger than
    :data:`STALE_KEYS_SECONDS`. ``fresh`` says that the set may be used without asking
    Google again; a set that is not fresh is used only when a fetch fails.
    """

    keys: tuple | None
    fresh: bool


@dataclass(frozen=True, repr=False)
class _FetchedKeys:
    keys: tuple
    max_age: int


_SUCCESS = "success"
_ANSWER = "answer"  # the provider refused the code
_INVALID = "invalid"  # the provider answered, and its token did not pass the checks
_FAILURE = "failure"  # counts for the cooldown


@dataclass(frozen=True, repr=False)
class _WorkResult:
    kind: str
    identity: VerifiedIdentity | None = None
    reason: str | None = None
    fetched: _FetchedKeys | None = None


class _ProviderFailure(Exception):
    """A response or a transport that cannot be used. Carries one fixed word."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class _ProviderAnswer(Exception):
    """A 400 or 401 with an ``error`` code: the provider refused the request."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class _TokenRefused(Exception):
    """An ID token that failed a check this module makes itself."""


# --------------------------------------------------------------------------- transport


def _remaining(deadline: float) -> float:
    return deadline - _monotonic()


def _timeouts(deadline: float) -> tuple[float, float]:
    """Connect and read timeouts, each no larger than what is left of the budget."""
    remaining = _remaining(deadline)
    if remaining <= 0:
        raise _ProviderFailure(REASON_BUDGET)
    return min(CONNECT_TIMEOUT, remaining), min(READ_TIMEOUT, remaining)


class _BoundedBody:
    """A response body as ``requests`` reads it: one bounded read at a time.

    The deadline is checked between reads and the total is capped, so neither a body
    delivered slowly nor an oversized one keeps the worker past its budget. The
    transfer is not decoded: the request asks for the identity encoding and a response
    in any other is refused before its body is read.
    """

    def __init__(self, raw, deadline: float):
        self._raw = raw
        self._deadline = deadline
        self._seen = 0

    def read(self, amt=None, **_ignored):
        if _remaining(self._deadline) <= 0:
            raise _ProviderFailure(REASON_BUDGET)
        chunk = self._raw.read1(_READ_SIZE)
        self._seen += len(chunk)
        if self._seen > MAX_RESPONSE_BYTES:
            raise _ProviderFailure(REASON_BODY_TOO_LARGE)
        return chunk

    def close(self):
        self._raw.close()

    def release_conn(self):
        self._raw.release_conn()


class _BoundedAdapter(HTTPAdapter):
    """The only transport of a provider call: one attempt, bounded in time and size."""

    def __init__(self, deadline: float):
        self._deadline = deadline
        # The default of requests, stated: nothing is retried, and an authorization
        # code is never sent twice.
        super().__init__(max_retries=0)

    def send(self, request, stream=False, timeout=None, verify=True, cert=None, proxies=None):
        response = super().send(
            request, stream=True, timeout=_timeouts(self._deadline), verify=True, cert=None, proxies=None
        )
        encoding = response.headers.get("Content-Encoding", "").strip().lower()
        if encoding not in ("", "identity"):
            response.close()
            raise _ProviderFailure(REASON_ENCODING)
        response.raw = _BoundedBody(response.raw, self._deadline)
        return response


def _prepare_session(session, deadline: float) -> None:
    """Every provider call uses its own session, set up here and closed afterwards."""
    # No proxy, .netrc or CA bundle from the environment. Certificates are verified.
    session.trust_env = False
    adapter = _BoundedAdapter(deadline)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers["Accept-Encoding"] = "identity"


_UNPARSED = object()


def _json_object(response) -> dict:
    """The response body as a JSON object, read through the bounded reader."""
    content = response.content
    try:
        document = json.loads(content)
    except ValueError:
        # Raised below, outside this handler: a JSON error keeps the whole document it
        # failed on, and that must not travel with the failure.
        document = _UNPARSED
    if document is _UNPARSED:
        raise _ProviderFailure(REASON_NOT_JSON)
    if type(document) is not dict:
        raise _ProviderFailure(REASON_NOT_AN_OBJECT)
    return document


def _check_token_response(response):
    """The response rules of design 5.3, run before Authlib parses anything.

    Authlib looks at the status from 500 up only: left alone it returns a 302 with a
    JSON body, or a 200 without ``id_token``, as a token.
    """
    status = response.status_code
    if 300 <= status < 400:
        raise _ProviderFailure(REASON_REDIRECT)
    if status in (400, 401):
        error = _json_object(response).get("error")
        if isinstance(error, str):
            raise _ProviderAnswer(error if error in PROVIDER_ERROR_CODES else REASON_OTHER_PROVIDER_ERROR)
        raise _ProviderFailure(REASON_ERROR_RESPONSE)
    if status != 200:
        raise _ProviderFailure(REASON_STATUS)
    document = _json_object(response)
    if "error" in document:
        raise _ProviderFailure(REASON_ERROR_RESPONSE)
    id_token = document.get("id_token")
    if not isinstance(id_token, str) or not 0 < len(id_token) <= MAX_ID_TOKEN_LENGTH:
        raise _ProviderFailure(REASON_NO_ID_TOKEN)
    return response


def _exchange_code(request: ExchangeRequest, deadline: float) -> tuple[str, str | None]:
    """Redeem the code once. Returns the ID token and the access token, if any."""
    client = OAuth2Session(
        request.client_id,
        request.client_secret,
        token_endpoint_auth_method="client_secret_basic",
        redirect_uri=request.redirect_uri,
    )
    try:
        _prepare_session(client, deadline)
        client.register_compliance_hook("access_token_response", _check_token_response)
        token = client.fetch_token(
            TOKEN_ENDPOINT,
            grant_type="authorization_code",
            code=request.code,
            code_verifier=request.code_verifier,
            allow_redirects=False,
            timeout=_timeouts(deadline),
        )
        access_token = token.get("access_token")
        return token["id_token"], access_token if isinstance(access_token, str) else None
    finally:
        client.close()


# --------------------------------------------------------------------------- signing keys

_MAX_AGE = re.compile(r"(?:^|[\s,])max-age\s*=\s*(\d{1,10})(?:$|[\s,])", re.IGNORECASE)
_JWK_MEMBERS = ("kty", "kid", "use", "alg", "n", "e")


def _cache_seconds(cache_control) -> int:
    """How long a fetched set stays fresh: the response's ``max-age``, clamped."""
    match = _MAX_AGE.search(cache_control) if isinstance(cache_control, str) else None
    if match is None:
        return KEY_CACHE_DEFAULT_SECONDS
    return max(KEY_CACHE_MIN_SECONDS, min(KEY_CACHE_MAX_SECONDS, int(match.group(1))))


def _modulus_bits(value) -> int:
    if not isinstance(value, str) or not value:
        return 0
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except ValueError:
        return 0
    return int.from_bytes(raw, "big").bit_length()


def _usable_keys(document: dict) -> tuple:
    """The RSA signature keys of a key set document; a set without one is refused.

    Only the members of a key that are used are kept, so nothing else a key carries
    (``x5u``, ``jku``) is ever looked at, let alone followed.
    """
    entries = document.get("keys")
    if type(entries) is not list or not 1 <= len(entries) <= MAX_KEYS:
        raise _ProviderFailure(REASON_KEYS)
    usable = []
    for entry in entries:
        if type(entry) is not dict:
            continue
        if entry.get("kty") != "RSA" or entry.get("use", "sig") != "sig":
            continue
        if entry.get("alg", ID_TOKEN_ALGORITHM) != ID_TOKEN_ALGORITHM:
            continue
        if not isinstance(entry.get("kid"), str) or not entry["kid"] or not isinstance(entry.get("e"), str):
            continue
        if _modulus_bits(entry.get("n")) < MIN_RSA_MODULUS_BITS:
            continue
        usable.append({name: entry[name] for name in _JWK_MEMBERS if name in entry})
    if not usable:
        raise _ProviderFailure(REASON_KEYS)
    return tuple(usable)


def _fetch_keys(deadline: float) -> _FetchedKeys:
    """Google's current signing keys, from the pinned address, in one request."""
    if _remaining(deadline) < MIN_TIME_FOR_REQUEST:
        raise _ProviderFailure(REASON_BUDGET)
    session = requests.Session()
    try:
        _prepare_session(session, deadline)
        response = session.get(JWKS_URI, allow_redirects=False, timeout=_timeouts(deadline))
        if response.status_code != 200:
            raise _ProviderFailure(REASON_REDIRECT if 300 <= response.status_code < 400 else REASON_STATUS)
        keys = _usable_keys(_json_object(response))
        return _FetchedKeys(keys=keys, max_age=_cache_seconds(response.headers.get("Cache-Control")))
    finally:
        session.close()


# --------------------------------------------------------------------------- ID token

_JWS_REGISTRY = JWSRegistry(algorithms=[ID_TOKEN_ALGORITHM])


def _decode(id_token: str, keys: tuple):
    """Verify the signature with the key the token names; ``None`` for a key the set lacks.

    A token without a ``kid`` names no key and is treated like one that names an
    unknown key: a key set of one would otherwise verify it.
    """
    key_set = KeySet.import_key_set({"keys": [dict(key) for key in keys]})

    def named_key(obj):
        kid = obj.headers().get("kid")
        if not isinstance(kid, str) or not kid:
            raise InvalidKeyIdError()
        return key_set.get_by_kid(kid)

    try:
        return jwt.decode(id_token, named_key, registry=_JWS_REGISTRY)
    except InvalidKeyIdError:
        return None


def _is_time_claim(value) -> bool:
    # ``type(...) is int``: a bool is an int, a float can be NaN or infinite, and a
    # comparison with NaN is false in both directions.
    return type(value) is int and 0 < value < _MAX_TIME_CLAIM


def _identity_from(token, request: ExchangeRequest, access_token: str | None, now: int) -> VerifiedIdentity:
    """Checks 2 to 6 of design 5.4, on the one payload that joserfc parsed."""
    claims = token.claims
    if type(claims) is not dict:
        raise _TokenRefused()
    # Types first, before anything is compared.
    if not _is_time_claim(claims.get("exp")) or not _is_time_claim(claims.get("iat")):
        raise _TokenRefused()
    if type(claims.get("aud")) is not str or claims["aud"] != request.client_id:
        raise _TokenRefused()
    if "azp" in claims and (type(claims["azp"]) is not str or claims["azp"] != request.client_id):
        raise _TokenRefused()
    if type(claims.get("iss")) is not str or claims["iss"] not in ACCEPTED_ISSUERS:
        raise _TokenRefused()
    if not _is_visible_ascii(claims.get("sub"), 1, MAX_SUBJECT_LENGTH):
        raise _TokenRefused()
    if type(claims.get("nonce")) is not str:
        raise _TokenRefused()

    CodeIDToken(
        claims,
        token.header,
        {
            "iss": {"essential": True, "values": sorted(ACCEPTED_ISSUERS)},
            "aud": {"essential": True, "value": request.client_id},
            "sub": {"essential": True},
            "exp": {"essential": True},
            "iat": {"essential": True},
        },
        {"nonce": request.nonce, "client_id": request.client_id, "access_token": access_token},
    ).validate(now=now, leeway=TOKEN_LEEWAY_SECONDS)

    # The token has just come from the token endpoint.
    if claims["exp"] <= claims["iat"] or claims["iat"] < now - MAX_TOKEN_AGE_SECONDS - TOKEN_LEEWAY_SECONDS:
        raise _TokenRefused()

    email, name = claims.get("email"), claims.get("name")
    return VerifiedIdentity(
        subject=claims["sub"],
        email=email if isinstance(email, str) else None,
        name=name if isinstance(name, str) else None,
    )


def _failure_reason(exc: BaseException) -> str:
    return exc.reason if isinstance(exc, _ProviderFailure) else type(exc).__name__


def fetch_identity(request: ExchangeRequest, keys: KeySnapshot, deadline: float) -> _WorkResult:
    """The network function of one callback: exchange, at most one key fetch, checks.

    It runs in a worker thread and is given plain values only. It touches no
    application state, and everything it learned is in the value it returns.
    """
    try:
        id_token, access_token = _exchange_code(request, deadline)
    except _ProviderAnswer as answer:
        return _WorkResult(_ANSWER, reason=answer.code)
    except Exception as exc:
        return _WorkResult(_FAILURE, reason=_failure_reason(exc))

    fetched = None
    try:
        token = _decode(id_token, keys.keys) if keys.fresh and keys.keys else None
        if token is None:
            # The cached set has expired, there is none, or the token names a key the
            # set lacks. One fetch, inside this callback's budget.
            fetch_reason = None
            try:
                fetched = _fetch_keys(deadline)
            except Exception as exc:
                fetch_reason = _failure_reason(exc)
            if fetched is not None:
                token = _decode(id_token, fetched.keys)
            elif keys.keys:
                token = _decode(id_token, keys.keys)
            if token is None:
                if fetched is None:
                    return _WorkResult(_FAILURE, reason=fetch_reason)
                return _WorkResult(_INVALID, reason=InvalidKeyIdError.__name__, fetched=fetched)
        identity = _identity_from(token, request, access_token, _wall_now())
    except Exception as exc:
        # Google answered and its token did not pass: not a failure for the cooldown.
        reason = REASON_CLAIMS if isinstance(exc, _TokenRefused) else type(exc).__name__
        return _WorkResult(_INVALID, reason=reason, fetched=fetched)
    return _WorkResult(_SUCCESS, identity=identity, fetched=fetched)


# --------------------------------------------------------------------------- provider gate


class _GateState:
    """The three pieces of per-process state. Read and written under the provider lock."""

    __slots__ = ("free_slots", "keys", "keys_fetched_at", "keys_fresh_until", "failures", "cooldown_until")

    def __init__(self, slots: int):
        self.free_slots = slots
        self.keys = None
        self.keys_fetched_at = 0.0
        self.keys_fresh_until = 0.0
        self.failures = 0
        self.cooldown_until = 0.0


class _Task:
    """One call's hand-over between its worker thread and the request thread."""

    __slots__ = ("lock", "done", "begun", "abandoned", "result", "slot_returned")

    def __init__(self):
        self.lock = threading.Lock()
        self.done = threading.Event()
        self.begun = False
        self.abandoned = False
        self.result = None
        self.slot_returned = False


class ProviderGate:
    """One network budget for every provider call of a callback (design 5.7, 5.8).

    The work runs in a daemon thread that the request stops waiting for at the
    deadline. The thread is not cancelled. At most :data:`PROVIDER_SLOTS` workers are
    inside the network function at any moment, and a result that arrives late is never
    read. The slot count, the key cache and the cooldown are availability devices, not
    security state.
    """

    def __init__(self, *, work=None, thread_factory=threading.Thread, slots: int = PROVIDER_SLOTS):
        self.lock = threading.Lock()
        self.state = _GateState(slots)
        self._work = fetch_identity if work is None else work
        self._thread_factory = thread_factory

    # -- slots ---------------------------------------------------------------

    def _take_slot(self) -> bool:
        """Take a slot without waiting."""
        with self.lock:
            if self.state.free_slots <= 0:
                return False
            self.state.free_slots -= 1
            return True

    def _return_slot(self, task: _Task) -> None:
        """Give a task's slot back, exactly once however often it is called."""
        with self.lock:
            if task.slot_returned:
                return
            task.slot_returned = True
            self.state.free_slots += 1

    # -- the worker thread ---------------------------------------------------

    def _begin(self, task: _Task) -> bool:
        """The worker's first action: is the task still wanted?"""
        with task.lock:
            if task.abandoned:
                return False
            task.begun = True
            return True

    def _hand_over(self, task: _Task, result: _WorkResult) -> None:
        """The worker's second action: store the result, unless the task was abandoned."""
        with task.lock:
            if not task.abandoned:
                task.result = result
            task.done.set()

    def _run(self, task: _Task, request: ExchangeRequest, keys: KeySnapshot, deadline: float) -> None:
        """The whole body of a worker thread: three synchronization actions around the
        network function, and nothing else. No application or request context exists
        here, and no connection, cache or cooldown is touched."""
        try:
            if self._begin(task):
                try:
                    result = self._work(request, keys, deadline)
                except BaseException as exc:
                    # The class name and nothing else: the text of a requests, Authlib
                    # or JSON error can hold URLs, a description or a whole token
                    # response.
                    result = _WorkResult(_FAILURE, reason=type(exc).__name__)
                self._hand_over(task, result)
        except BaseException:
            # Nothing may reach threading.excepthook, which prints the traceback. No
            # result is handed over, and the request counts an overrun at its deadline.
            pass
        finally:
            # The thread is not, or no longer, inside the network function.
            self._return_slot(task)

    # -- the request thread --------------------------------------------------

    def cooling_down(self) -> bool:
        with self.lock:
            return _monotonic() < self.state.cooldown_until

    def _admission(self) -> tuple[bool, KeySnapshot]:
        """Whether the provider is cooling down, and a copy of the cached keys."""
        with self.lock:
            now = _monotonic()
            state = self.state
            if now < state.cooldown_until:
                return True, KeySnapshot(None, False)
            if state.keys is None or now - state.keys_fetched_at >= STALE_KEYS_SECONDS:
                return False, KeySnapshot(None, False)
            keys = tuple(dict(key) for key in state.keys)
            return False, KeySnapshot(keys, now < state.keys_fresh_until)

    def _start_cooldown(self, now: float) -> None:
        self.state.failures = 0
        self.state.cooldown_until = now + COOLDOWN_SECONDS

    def _conclude(self, result: _WorkResult | None) -> ExchangeResult:
        """Apply a result that arrived in time, or an overrun, to the cache and the cooldown."""
        with self.lock:
            now = _monotonic()
            if result is None:
                # One overrun is enough: it cost the single worker the whole budget.
                self._start_cooldown(now)
            else:
                if result.fetched is not None:
                    self.state.keys = result.fetched.keys
                    self.state.keys_fetched_at = now
                    self.state.keys_fresh_until = now + result.fetched.max_age
                if result.kind == _FAILURE:
                    self.state.failures += 1
                    if self.state.failures >= FAILURES_BEFORE_COOLDOWN:
                        self._start_cooldown(now)
                else:
                    self.state.failures = 0
        if result is None:
            return ExchangeResult(OUTCOME_UNAVAILABLE, reason=REASON_OVERRUN)
        if result.kind == _SUCCESS:
            return ExchangeResult(OUTCOME_OK, identity=result.identity)
        if result.kind == _FAILURE:
            return ExchangeResult(OUTCOME_UNAVAILABLE, reason=result.reason)
        if result.kind == _ANSWER and result.reason in CREDENTIAL_ERROR_CODES:
            logger.error("provider_rejected_client error=%s", result.reason)
        return ExchangeResult(OUTCOME_FAILED, reason=result.reason)

    def exchange(self, request: ExchangeRequest, *, budget: float | None = None) -> ExchangeResult:
        """Exchange a code and verify the ID token, within one budget.

        A refusal for the cooldown or for lack of a slot, and a worker that could not
        be started, make no request and leave the failure count as it was.
        """
        budget = NETWORK_BUDGET if budget is None else budget
        # Created before the slot is taken, so that the rule for a failed start can
        # run from the first statement after the taking.
        task = _Task()
        cooling, keys = self._admission()
        if cooling:
            return ExchangeResult(OUTCOME_UNAVAILABLE, reason=REASON_COOLDOWN)
        deadline = _monotonic() + budget
        if not self._take_slot():
            return ExchangeResult(OUTCOME_UNAVAILABLE, reason=REASON_NO_SLOT)
        try:
            worker = self._thread_factory(
                target=self._run, args=(task, request, keys, deadline), name="google-oidc", daemon=True
            )
            worker.start()
        except BaseException as exc:
            # Raised before start(), by start() with no thread, or by start() after the
            # thread exists: the exception does not say which. Whoever is not inside
            # the network function gives the slot back, and only one release counts.
            with task.lock:
                task.abandoned = True
                begun = task.begun
            if not begun:
                self._return_slot(task)
            logger.warning("provider_start_failed error=%s", type(exc).__name__)
            if not isinstance(exc, Exception):
                raise
            return ExchangeResult(OUTCOME_UNAVAILABLE, reason=REASON_START_FAILED)

        task.done.wait(max(0.0, _remaining(deadline)))
        # Exactly one of the two: the result is taken, or the task is abandoned.
        with task.lock:
            result = task.result
            if result is None:
                task.abandoned = True
        return self._conclude(result)


# Created at import; no thread is started here. A process forked from a preloading
# master starts with the lock free and every slot free.
GATE = ProviderGate()


def exchange(request: ExchangeRequest, *, budget: float | None = None) -> ExchangeResult:
    return GATE.exchange(request, budget=budget)


def cooling_down() -> bool:
    return GATE.cooling_down()
