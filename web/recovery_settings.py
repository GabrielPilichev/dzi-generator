"""Whether password recovery is enabled, and with which mailer (design 7.1).

``app.config[CONFIG_KEY]`` holds a :class:`RecoverySettings` when recovery is enabled and
``None`` (or nothing) when it is not. It is a settings object of its own, under its own key,
so that the recovery gate is independent of registration's, but it is built from the same
mail settings (``mail.load_settings`` and ``mail.build_mailer``) and nothing else enables it:

* a development run (``web.app``) enables it when the mail settings are complete and usable
  for development, as it enables registration (``DZI_MAIL_BACKEND=console`` shows the code
  in the terminal); a setting that is set but unusable gives one warning and leaves recovery
  disabled;
* a production start (``web.production``) enables it only while the build's gate
  ``recovery.PRODUCTION_ACTIVATION`` is on, and then only with settings usable for
  production (no ``console`` or ``file`` backend). While the gate is off it starts normally
  whatever the settings hold and keeps recovery disabled.

No setting and no environment variable overrides the gate. A configured backend never
enables recovery by itself in production.

Standard library and ``web.services.mail`` only: no Flask, no blueprint.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping

from web.services import mail

CONFIG_KEY = "RECOVERY"


@dataclass(frozen=True)
class RecoverySettings:
    """An enabled recovery: the mailer every send goes through and the hourly total budget
    (``DZI_MAIL_HOURLY_CAP``, 1 to 300), shared with registration (RD5, RD6)."""

    mailer: mail.Mailer = field(repr=False)
    total_cap: int = mail.MAX_HOURLY_CAP


def from_environment(environ: Mapping[str, str], *, production: bool) -> RecoverySettings | None:
    """The settings these values give, or ``None``: no backend configured, or any problem.

    Gates are not decided here; the caller decides whether to ask at all.
    """
    settings = mail.load_settings(environ, production=production)
    if settings is None:
        return None
    return RecoverySettings(mailer=mail.build_mailer(settings, production=production),
                            total_cap=settings.hourly_cap)
