#!/usr/bin/env python3
"""Run LearnPilot locally behind a single trusted Cloudflare tunnel proxy."""

from __future__ import annotations

import os
import sys

from werkzeug.middleware.proxy_fix import ProxyFix


REQUIRED_ENV_VARS = ("DZI_SECRET_KEY",)


def missing_env_vars() -> list[str]:
    return [name for name in REQUIRED_ENV_VARS if not os.environ.get(name)]


def main() -> int:
    missing = missing_env_vars()
    if missing:
        print(
            "Missing required local/tunnel environment variables: "
            + ", ".join(missing),
            file=sys.stderr,
        )
        print(
            "Set a generated DZI_SECRET_KEY. Sign-in is by account: create accounts "
            "with `python3 -m src.manage_accounts` (admins also need DZI_TOTP_KEY).",
            file=sys.stderr,
        )
        return 2

    from web.app import app

    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
    print("Starting LearnPilot tunnel dev server on http://127.0.0.1:5001")
    print("Start cloudflared separately: cloudflared tunnel --url http://127.0.0.1:5001")
    app.run(host="127.0.0.1", port=5001, debug=False, use_reloader=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
