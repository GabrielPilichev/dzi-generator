"""Production configuration for the Gunicorn entry point (``web.wsgi:app``).

Development and tests keep importing ``web.app`` directly. Only this path
enforces secrets, explicit state paths outside the code checkout, no schema
changes from the web app, a fully migrated schema and exactly one trusted proxy hop.
It also refuses a schema that is newer than this build, an unusable lifecycle
clock row and a missing timezone database, and makes lifecycle writes use the
host's real clock evidence (docs/ASSIGNMENT_LIFECYCLE_DESIGN.md, 3.6 and 12.6).
And it refuses a database whose restore is in progress or was left unfinished
(docs/DEPLOYMENT.md, section 17): that holds after a reboot as well.
Sign-in is by account. Google sign-in is the one optional addition, and this build
keeps it disabled in production whatever is configured (``google_sign_in_provider``).
Error messages name settings, never their values.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Mapping

from werkzeug.middleware.proxy_fix import ProxyFix

from web.oauth_state import PROVIDER_CONFIG_KEY
from web.public_url import (
    PUBLIC_BASE_URL_CONFIG,
    PUBLIC_BASE_URL_ENV,
    normalize_public_host,
    public_base_url_for_host,
)
from web.services import google_oidc
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


def restore_lock_error(environ: Mapping[str, str]) -> str | None:
    """Why a restore forbids serving the configured database, or ``None``.

    ``deploy/learnpilot-restore.sh`` leaves a lock next to the database from
    before it stops the service until the restore has finished or was undone.
    While it is there the live state may be partly replaced: a snapshot's files
    next to the database they do not belong to. systemd starts an enabled unit
    after a reboot whatever a script was in the middle of, so the refusal is here.
    """
    raw = environ.get(DB_ENV)
    if not raw or not Path(raw).is_absolute():
        return None
    from web.services import lifecycle

    if not lifecycle.restore_lock_present(raw):
        return None
    return (
        "Refusing to start LearnPilot: a restore of this database is in progress or was left unfinished ("
        + lifecycle.restore_lock_path(raw) + " exists). The service starts again when "
        "deploy/learnpilot-restore.sh has finished: run it again from the beginning "
        "(docs/DEPLOYMENT.md, section 17). Do not remove the lock by hand."
    )


def _google_credentials(environ: Mapping[str, str]) -> tuple[str | None, str | None]:
    return environ.get(google_oidc.CLIENT_ID_ENV) or None, environ.get(google_oidc.CLIENT_SECRET_ENV) or None


def google_sign_in_errors(environ: Mapping[str, str]) -> list[str]:
    """Why the two Google settings forbid a start. Empty while the build's gate is off.

    While ``google_oidc.PRODUCTION_ACTIVATION`` is off, the presence of credentials is
    never a reason to refuse to start, whatever they hold. Once it is on they follow
    the rule of the optional ``DZI_TOTP_KEY``: both unset is fine, and a value that is
    set must be usable, so exactly one value or a malformed one refuses.
    """
    if not google_oidc.PRODUCTION_ACTIVATION:
        return []
    client_id, client_secret = _google_credentials(environ)
    if client_id is None and client_secret is None:
        return []
    return google_oidc.credential_problems(client_id, client_secret)


def google_sign_in_provider(environ: Mapping[str, str], public_base_url):
    """The provider a production start enables, or ``None``: the activation gate.

    Google sign-in is not activated for real users before the identity recovery
    tooling exists. The build holds that line with one constant,
    ``google_oidc.PRODUCTION_ACTIVATION``, which no setting overrides. While it is
    off the provider is disabled, and configured credentials are reported once, by
    the names of the settings and never by their values.
    """
    client_id, client_secret = _google_credentials(environ)
    if not google_oidc.PRODUCTION_ACTIVATION:
        configured = [name for name, value in (
            (google_oidc.CLIENT_ID_ENV, client_id), (google_oidc.CLIENT_SECRET_ENV, client_secret)) if value]
        if configured:
            logging.getLogger(__name__).warning(
                "Google sign-in credentials are configured (%s), but this build does not enable "
                "Google sign-in.", ", ".join(configured))
        return None
    return google_oidc.provider_config(client_id, client_secret, public_base_url)


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

    return errors + google_sign_in_errors(environ) + _state_path_errors(environ) + _reference_root_errors(environ)


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


def unknown_schema_migrations(db_path, open_read_only) -> list[str]:
    """Applied migrations this build has no file for; never writes.

    A build that is older than the schema would run without the enforcement a
    later migration belongs to: for the lifecycle that means serving closed
    assignments as open. Such a build must fail closed instead of starting.
    """
    from src.manage_migrations import migration_filenames

    conn = open_read_only(db_path)
    try:
        has_table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_migrations'"
        ).fetchone()
        applied = [row[0] for row in conn.execute(
            "SELECT filename FROM schema_migrations ORDER BY filename"
        )] if has_table else []
    finally:
        conn.close()
    shipped = set(migration_filenames(MIGRATIONS_DIR))
    return [filename for filename in applied if filename not in shipped]


def lifecycle_clock_problem(db_path, open_read_only) -> str | None:
    """Why the lifecycle clock row cannot be used, or ``None``; never writes.

    Existence and well-formedness only: a ``suspect`` clock still starts (read
    pages work, lifecycle writes are refused until an operator confirms it).
    """
    from web.services import lifecycle

    conn = open_read_only(db_path)
    try:
        return lifecycle.clock_row_problem(conn)
    finally:
        conn.close()


def timezone_data_problem() -> str | None:
    """Deadlines are computed in named IANA zones; without the zone database every
    deadline form would fail. Nothing ever falls back to UTC or the server zone."""
    from web.services import lifecycle

    try:
        if lifecycle.DEFAULT_TIMEZONE not in lifecycle.timezone_names():
            return f"the IANA timezone database does not know {lifecycle.DEFAULT_TIMEZONE}"
    except Exception as exc:
        return f"the IANA timezone database is not available ({type(exc).__name__})"
    return None


LIFECYCLE_LOGGER = "learnpilot.lifecycle"


def configure_lifecycle_logging() -> None:
    """Send lifecycle events (create, edit, close, refused submissions) to stderr.

    Gunicorn captures stderr into journald. The lines are for diagnosis: what was
    created, edited, closed or refused, and when. They are not a record of the
    lifecycle and nothing is reconciled from them after a restore; only the
    database a restore replaces is evidence (docs/DEPLOYMENT.md, section 26).
    Without a handler only warnings would reach the journal.
    """
    logger = logging.getLogger(LIFECYCLE_LOGGER)
    if not any(getattr(handler, "learnpilot_lifecycle", False) for handler in logger.handlers):
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(name)s %(levelname)s %(message)s"))
        handler.learnpilot_lifecycle = True
        logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False


OIDC_LOGGER = "learnpilot.oidc"


def configure_oidc_logging() -> None:
    """Send Google sign-in events (account created, linked, refused, failed) to stderr.

    Like the lifecycle lines they are for diagnosis and carry a fixed vocabulary: an
    outcome, an intent, a local user id, an allow-listed provider error code or an
    exception's class name. Never a code, a state, a token, a subject or an email.
    Without a handler only warnings would reach the journal.
    """
    logger = logging.getLogger(OIDC_LOGGER)
    if not any(getattr(handler, "learnpilot_oidc", False) for handler in logger.handlers):
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(name)s %(levelname)s %(message)s"))
        handler.learnpilot_oidc = True
        logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False


# True only while create_production_app imports web.app. That module configures a
# development run at import; what it would say about the Google settings there is
# wrong for production, where the public base URL is set afterwards.
_production_startup = False


def production_startup_in_progress() -> bool:
    return _production_startup


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
    locked = restore_lock_error(environ)
    if locked:
        raise ProductionConfigError(locked)
    errors = production_config_errors(environ)
    if errors:
        raise ProductionConfigError(
            "Refusing to start LearnPilot in production:\n"
            + "\n".join(f"  - {error}" for error in errors)
            + "\nSee docs/DEPLOYMENT.md (environment configuration)."
        )

    global _production_startup
    _production_startup = True
    try:
        from web import app as web_app
    finally:
        _production_startup = False

    pending = pending_schema_migrations(web_app.DB_PATH, web_app.open_existing_database_read_only)
    if pending:
        raise ProductionConfigError(
            "Refusing to start LearnPilot: database schema has pending migrations: "
            + ", ".join(pending)
            + f". Back up the database, then run `{migration_command()}` "
            "from the application directory."
        )

    unknown = unknown_schema_migrations(web_app.DB_PATH, web_app.open_existing_database_read_only)
    if unknown:
        raise ProductionConfigError(
            "Refusing to start LearnPilot: the database has applied migrations this build "
            "does not ship: " + ", ".join(unknown)
            + ". This build is older than the schema; deploy the build that ships them. "
            "A build without assignment lifecycle enforcement must never serve this "
            "database (docs/DEPLOYMENT.md, rollback considerations)."
        )

    clock_problem = lifecycle_clock_problem(web_app.DB_PATH, web_app.open_existing_database_read_only)
    if clock_problem:
        raise ProductionConfigError(
            "Refusing to start LearnPilot: the assignment lifecycle clock row is unusable ("
            + clock_problem + "). It is repaired only by the operator confirmation "
            "`python3 -m src.manage_lifecycle --db \"$DZI_DB\" clock-recover ...`, never by "
            "re-running a migration or a seed."
        )

    zone_problem = timezone_data_problem()
    if zone_problem:
        raise ProductionConfigError(
            "Refusing to start LearnPilot: " + zone_problem + ". Install the system tzdata package."
        )

    # Lifecycle writes in production rest on the host's real boot identity,
    # boot-time clock and synchronisation report; web.app alone uses the
    # development environment, which requires no synchronisation evidence.
    from web.services import lifecycle
    lifecycle.configure_clock_environment(lifecycle.SystemClockEnvironment())
    configure_lifecycle_logging()
    configure_oidc_logging()

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
    # The callback address is built on that base and on nothing a request carries.
    flask_app.config[PROVIDER_CONFIG_KEY] = google_sign_in_provider(
        environ, flask_app.config[PUBLIC_BASE_URL_CONFIG])
    if not isinstance(flask_app.wsgi_app, ProxyFix):
        flask_app.wsgi_app = trusted_proxy(flask_app.wsgi_app)
    return flask_app
