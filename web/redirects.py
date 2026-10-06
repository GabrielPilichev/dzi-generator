"""Post-login and profile-switch redirect confinement.

``safe_redirect_target`` accepts only a same-site absolute path. Anything else
(missing, relative, protocol-relative ``//host``, or carrying a scheme or
netloc) falls back to the supplied endpoint or path.
"""

from __future__ import annotations

from urllib.parse import urlparse

from flask import url_for


def safe_redirect_target(candidate: str | None, fallback: str) -> str:
    fallback_url = fallback if fallback.startswith("/") else url_for(fallback)
    if not isinstance(candidate, str) or not candidate:
        return fallback_url
    if not candidate.startswith("/") or candidate.startswith("//"):
        return fallback_url

    parsed = urlparse(candidate)
    if parsed.scheme or parsed.netloc or not parsed.path.startswith("/"):
        return fallback_url
    return candidate
