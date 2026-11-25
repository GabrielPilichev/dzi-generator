"""Content-versioned static URLs.

Static files keep their names, so a cache in front of the app (Cloudflare, a browser) may
serve an old stylesheet beside new HTML after a deploy until its copy expires. Every
``url_for('static', filename=...)`` therefore gets ``?v=<hash>``: the first 12 hex digits
of the file's SHA-256. A changed file gets a new URL, which no cache holds yet; an
unchanged file keeps its URL and stays cached.

Hashes are computed once per file and process. Production restarts on every deploy
(``preload_app``; docs/DEPLOYMENT.md section 20), so a running process never serves a
file that changed under it. A missing file gets no version, and the URL stays as Flask
builds it.

This module must not import ``web.app``.
"""

from __future__ import annotations

import hashlib
from functools import lru_cache
from pathlib import Path

from flask import Flask

VERSION_ARG = "v"
VERSION_LENGTH = 12


@lru_cache(maxsize=None)
def file_version(path: str) -> str | None:
    """The short content hash of ``path``, or None if it is not a readable file."""
    try:
        data = Path(path).read_bytes()
    except OSError:
        return None
    return hashlib.sha256(data).hexdigest()[:VERSION_LENGTH]


def install(app: Flask) -> None:
    """Add ``?v=<hash>`` to every static URL ``app`` builds."""
    static_root = Path(app.static_folder).resolve()

    @app.url_defaults
    def add_static_version(endpoint, values):
        if endpoint != "static" or VERSION_ARG in values:
            return
        filename = values.get("filename")
        if not filename:
            return
        path = (static_root / filename).resolve()
        # Never hash anything outside the static folder.
        if static_root not in path.parents:
            return
        version = file_version(str(path))
        if version:
            values[VERSION_ARG] = version
