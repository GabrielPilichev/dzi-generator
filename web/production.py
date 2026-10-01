"""Production configuration for the Gunicorn entry point (``web.wsgi:app``).

Development and tests keep importing ``web.app`` directly. Only this path
enforces secrets, explicit state paths outside the code checkout, no schema
changes on import, a fully migrated schema, and exactly one trusted proxy hop.
Error messages name settings, never their values.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping

from werkzeug.middleware.proxy_fix import ProxyFix


PROJECT_ROOT = Path(__file__).resolve().parent.parent
MIGRATIONS_DIR = PROJECT_ROOT / "web" / "migrations"

REQUIRED_SECRETS = ("DZI_SECRET_KEY", "DZI_ADMIN_PASSWORD", "DZI_TESTER_PASSWORD")
MIN_SECRET_KEY_LENGTH = 32
MIN_PASSWORD_LENGTH = 12
# Local-only values documented in AGENTS.md/README must never reach production.
LOCAL_ONLY_PASSWORDS = frozenset({"admin123", "tester123"})

DB_ENV = "DZI_DB"
STATE_DIRECTORY_ENVS = ("DZI_UPLOAD_ROOT", "DZI_VAULT")
SKIP_STARTUP_MIGRATION_ENV = "DZI_SKIP_STARTUP_MIGRATION"
TRUE_VALUES = {"1", "true", "yes", "on"}
FLASK_DEBUG_OFF_VALUES = {"0", "false", "no"}  # Flask's own get_debug_flag() rule.


class ProductionConfigError(RuntimeError):
    """Startup refuses to serve with unsafe or ambiguous production settings."""


def _outside_checkout(path: Path) -> bool:
    resolved = path.resolve()
    return resolved != PROJECT_ROOT and PROJECT_ROOT not in resolved.parents


def _state_path_errors(environ: Mapping[str, str]) -> list[str]:
    errors = []
    for name in (DB_ENV, *STATE_DIRECTORY_ENVS):
        raw = environ.get(name)
        if not raw:
            errors.append(f"{name} is not set")
            continue
        path = Path(raw)
        if not path.is_absolute():
            errors.append(f"{name} must be an absolute path")
        elif not _outside_checkout(path):
            # The repository tracks a seed questions.db; a deploy (git pull,
            # fresh clone) must never be able to replace live data or uploads.
            errors.append(f"{name} must be outside the application checkout")
        elif name == DB_ENV:
            if not path.is_file():
                errors.append(f"{name} must point to an existing database file")
            elif not os.access(path, os.R_OK | os.W_OK):
                errors.append(f"{name} must be readable and writable by the service user")
            elif not os.access(path.parent, os.W_OK | os.X_OK):
                errors.append(f"{name} directory must be writable (SQLite WAL/SHM files)")
        elif not path.is_dir():
            errors.append(f"{name} must be an existing directory")
        elif not os.access(path, os.W_OK | os.X_OK):
            errors.append(f"{name} must be writable by the service user")
    return errors


def production_config_errors(environ: Mapping[str, str]) -> list[str]:
    errors = [f"{name} is not set" for name in REQUIRED_SECRETS if not environ.get(name)]

    secret_key = environ.get("DZI_SECRET_KEY") or ""
    if secret_key and len(secret_key) < MIN_SECRET_KEY_LENGTH:
        errors.append(f"DZI_SECRET_KEY must be at least {MIN_SECRET_KEY_LENGTH} characters")

    admin = environ.get("DZI_ADMIN_PASSWORD") or ""
    tester = environ.get("DZI_TESTER_PASSWORD") or ""
    for name, value in (("DZI_ADMIN_PASSWORD", admin), ("DZI_TESTER_PASSWORD", tester)):
        if value and (len(value) < MIN_PASSWORD_LENGTH or value in LOCAL_ONLY_PASSWORDS):
            errors.append(
                f"{name} must be a non-default password of at least {MIN_PASSWORD_LENGTH} characters"
            )
    if admin and admin == tester:
        # The tester login would otherwise accept the admin credential.
        errors.append("DZI_ADMIN_PASSWORD and DZI_TESTER_PASSWORD must differ")

    cookie_secure = (environ.get("DZI_SESSION_COOKIE_SECURE") or "").strip().lower()
    if cookie_secure and cookie_secure not in TRUE_VALUES:
        errors.append("DZI_SESSION_COOKIE_SECURE must be unset or true in production")

    flask_debug = (environ.get("FLASK_DEBUG") or "").strip().lower()
    if flask_debug and flask_debug not in FLASK_DEBUG_OFF_VALUES:
        errors.append("FLASK_DEBUG must not be enabled in production")

    return errors + _state_path_errors(environ)


def pending_schema_migrations(db_path, open_read_only) -> list[str]:
    """Migration files not recorded in schema_migrations; never writes."""
    from src.manage_migrations import migration_filenames, pending_migrations

    conn = open_read_only(db_path)
    try:
        has_table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_migrations'"
        ).fetchone()
        applied = [row[0] for row in conn.execute(
            "SELECT filename FROM schema_migrations"
        )] if has_table else []
    finally:
        conn.close()
    return pending_migrations(migration_filenames(MIGRATIONS_DIR), applied)


def trusted_proxy(wsgi_app):
    """Trust exactly one hop: Nginx on loopback sets X-Forwarded-For/Proto/Host.

    Gunicorn binds 127.0.0.1 only, so no client can reach the app without Nginx
    overwriting these headers. Port and prefix headers are not trusted.
    """
    return ProxyFix(wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=0, x_prefix=0)


def create_production_app(environ: Mapping[str, str] | None = None):
    environ = os.environ if environ is None else environ
    errors = production_config_errors(environ)
    if errors:
        raise ProductionConfigError(
            "Refusing to start LearnPilot in production:\n"
            + "\n".join(f"  - {error}" for error in errors)
            + "\nSee docs/DEPLOYMENT.md (environment configuration)."
        )

    # web.app reads this at import time; production imports never change schema.
    os.environ[SKIP_STARTUP_MIGRATION_ENV] = "1"
    from web import app as web_app

    pending = pending_schema_migrations(web_app.DB_PATH, web_app.open_existing_database_read_only)
    if pending:
        raise ProductionConfigError(
            "Refusing to start LearnPilot: database schema has pending migrations: "
            + ", ".join(pending)
            + ". Back up the database, then run "
            "`python3 -m src.manage_migrations --db \"$DZI_DB\" --apply` "
            "from the application directory."
        )

    flask_app = web_app.app
    flask_app.config.update(
        DEBUG=False,
        TESTING=False,
        SESSION_COOKIE_SECURE=True,
        PREFERRED_URL_SCHEME="https",
    )
    if not isinstance(flask_app.wsgi_app, ProxyFix):
        flask_app.wsgi_app = trusted_proxy(flask_app.wsgi_app)
    return flask_app
