"""Production configuration for the Gunicorn entry point (``web.wsgi:app``).

Development and tests keep importing ``web.app`` directly. Only this path
enforces secrets, explicit state paths outside the code checkout, no schema
changes from the web app, a fully migrated schema and exactly one trusted proxy hop.
Sign-in is by account only; there is no other authentication to configure.
Error messages name settings, never their values.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Mapping

from werkzeug.middleware.proxy_fix import ProxyFix

from web.public_url import (
    PUBLIC_BASE_URL_CONFIG,
    PUBLIC_BASE_URL_ENV,
    normalize_public_host,
    public_base_url_for_host,
)
from web.services.totp import TOTP_KEY_ENV
from web.services.totp import key_problem as totp_key_problem


PROJECT_ROOT = Path(__file__).resolve().parent.parent
MIGRATIONS_DIR = PROJECT_ROOT / "web" / "migrations"

REQUIRED_SECRETS = ("DZI_SECRET_KEY",)
MIN_SECRET_KEY_LENGTH = 32

DB_ENV = "DZI_DB"
PUBLIC_HOST_ENV = "DZI_PUBLIC_HOST"
STATE_DIRECTORY_ENVS = ("DZI_UPLOAD_ROOT", "DZI_VAULT")
REFERENCE_ROOT_ENV = "DZI_REFERENCE_ROOT"
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
            # A local data/questions.db may sit inside a checkout; a deploy (git pull,
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


def _reference_root_errors(environ: Mapping[str, str]) -> list[str]:
    """Optional, read-only resources; unset keeps the checkout's data/reference."""
    name = REFERENCE_ROOT_ENV
    raw = environ.get(name)
    if not raw:
        return []
    path = Path(raw)
    if not path.is_absolute():
        return [f"{name} must be an absolute path"]
    if not _outside_checkout(path):
        return [f"{name} must be outside the application checkout"]
    if not path.is_dir():
        return [f"{name} must be an existing directory"]
    if not os.access(path, os.R_OK | os.X_OK):
        return [f"{name} must be readable by the service user"]
    return []


def production_config_errors(environ: Mapping[str, str]) -> list[str]:
    errors = [f"{name} is not set" for name in REQUIRED_SECRETS if not environ.get(name)]

    secret_key = environ.get("DZI_SECRET_KEY") or ""
    if secret_key and len(secret_key) < MIN_SECRET_KEY_LENGTH:
        errors.append(f"DZI_SECRET_KEY must be at least {MIN_SECRET_KEY_LENGTH} characters")

    # Optional: unset means admin TOTP sign-in fails closed. A value that is set must
    # be a usable Fernet key; there is never a generated or derived fallback.
    if environ.get(TOTP_KEY_ENV) and totp_key_problem(environ[TOTP_KEY_ENV]):
        errors.append(f"{TOTP_KEY_ENV} must be a Fernet key (32 url-safe base64-encoded bytes)")

    cookie_secure = (environ.get("DZI_SESSION_COOKIE_SECURE") or "").strip().lower()
    if cookie_secure and cookie_secure not in TRUE_VALUES:
        errors.append("DZI_SESSION_COOKIE_SECURE must be unset or true in production")

    flask_debug = (environ.get("FLASK_DEBUG") or "").strip().lower()
    if flask_debug and flask_debug not in FLASK_DEBUG_OFF_VALUES:
        errors.append("FLASK_DEBUG must not be enabled in production")

    # The same check the shared-link base URL is built with (web.public_url), so a
    # host accepted here can never fail later, when a link or QR code is made.
    if normalize_public_host(environ.get(PUBLIC_HOST_ENV)) is None:
        errors.append("DZI_PUBLIC_HOST must be a configured DNS hostname without scheme or port")

    # Shared links are built on https://DZI_PUBLIC_HOST; a second, possibly
    # different, public address would make that ambiguous.
    if environ.get(PUBLIC_BASE_URL_ENV):
        errors.append(
            f"{PUBLIC_BASE_URL_ENV} must not be set in production "
            f"(the public URL is https://{PUBLIC_HOST_ENV})"
        )

    return errors + _state_path_errors(environ) + _reference_root_errors(environ)


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


def migration_command(db_hint: str = "$DZI_DB") -> str:
    return f'python3 -m src.manage_migrations --db "{db_hint}" --apply'


def warn_if_schema_migrations_pending(db_path, open_read_only) -> list[str]:
    """Local-run check: log pending migrations and carry on; never writes.

    Production does not use this: create_production_app fails hard instead.
    """
    logger = logging.getLogger(__name__)
    try:
        if not Path(db_path).is_file():
            return []  # e.g. an in-memory or not-yet-created database
        pending = pending_schema_migrations(db_path, open_read_only)
    except Exception as exc:  # a warning helper must never stop a local run
        logger.warning("Could not check for pending database migrations: %s", exc)
        return []
    if pending:
        logger.warning(
            "Database schema has pending migrations: %s. The web app does not "
            "change schema; back up the database, then run `%s`.",
            ", ".join(pending), migration_command(),
        )
    return pending


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

    from web import app as web_app

    pending = pending_schema_migrations(web_app.DB_PATH, web_app.open_existing_database_read_only)
    if pending:
        raise ProductionConfigError(
            "Refusing to start LearnPilot: database schema has pending migrations: "
            + ", ".join(pending)
            + f". Back up the database, then run `{migration_command()}` "
            "from the application directory."
        )

    flask_app = web_app.app
    public_host = normalize_public_host(environ[PUBLIC_HOST_ENV])
    flask_app.config.update(
        DEBUG=False,
        TESTING=False,
        SESSION_COOKIE_SECURE=True,
        PREFERRED_URL_SCHEME="https",
        TRUSTED_HOSTS=[public_host, "localhost", "127.0.0.1"],
    )
    # Student links and their QR codes always name the public host over HTTPS,
    # whichever trusted Host (a loopback probe included) a request arrived on.
    flask_app.config[PUBLIC_BASE_URL_CONFIG] = public_base_url_for_host(public_host)
    if not isinstance(flask_app.wsgi_app, ProxyFix):
        flask_app.wsgi_app = trusted_proxy(flask_app.wsgi_app)
    return flask_app
