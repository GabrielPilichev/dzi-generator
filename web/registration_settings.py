"""Whether registration is enabled, and with which mailer (design 4.4, 7.3 and 7.5).

``app.config[CONFIG_KEY]`` holds a :class:`RegistrationSettings` when registration is
enabled and ``None`` (or nothing) when it is not. Nothing else enables it:

* a development run (``web.app``) enables it when the mail settings are complete and
  usable for development (``mail.load_settings(..., production=False)``); a setting
  that is set but unusable gives one warning and leaves registration disabled;
* a production start (``web.production``) enables it only while the build's gate
  ``registration.PRODUCTION_ACTIVATION`` is on, and then only with settings usable
  for production (no ``console`` or ``file`` backend). While the gate is off it starts
  normally whatever the settings hold, keeps registration disabled and names the
  settings that are present in one warning, never their values.

No setting and no environment variable overrides the gate. A configured backend never
enables registration by itself in production.

Standard library and ``web.services.mail`` only: no Flask, no blueprint.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping

from web.services import mail

CONFIG_KEY = "REGISTRATION"


@dataclass(frozen=True)
class RegistrationSettings:
    """An enabled registration: the mailer every send goes through and the hourly
    total budget (``DZI_MAIL_HOURLY_CAP``, 1 to 300)."""

    mailer: mail.Mailer = field(repr=False)
    total_cap: int = mail.MAX_HOURLY_CAP


def configured_names(environ: Mapping[str, str]) -> list[str]:
    """The mail settings that are set, by name. Never a value."""
    return [name for name in mail.SETTING_NAMES if environ.get(name)]


def from_environment(environ: Mapping[str, str], *, production: bool) -> RegistrationSettings | None:
    """The settings these values give, or ``None``: no backend configured, or any problem.

    Gates are not decided here; the caller decides whether to ask at all.
    """
    settings = mail.load_settings(environ, production=production)
    if settings is None:
        return None
    return RegistrationSettings(mailer=mail.build_mailer(settings, production=production),
                                total_cap=settings.hourly_cap)
