"""The per-request SQLite connection shared by every view of one request.

It lives outside ``web.app`` so controllers and services can use the same
connection as the application's own helpers without importing the composition
root. The connection follows the shared policy of :mod:`web.db`, is opened at
``app.config["DB_PATH"]`` on first use and is closed by ``web.app``'s teardown.
"""

from __future__ import annotations

import sqlite3

from flask import current_app, g

from web import db as web_db


def get_request_db() -> sqlite3.Connection:
    """Per-request SQLite connection (web.db policy). Closed in teardown."""
    if "db" not in g:
        g.db = web_db.connect_db(current_app.config["DB_PATH"])
    return g.db


def request_db_if_open() -> sqlite3.Connection | None:
    """The request's connection if it has opened one; this never opens it."""
    return g.get("db")
