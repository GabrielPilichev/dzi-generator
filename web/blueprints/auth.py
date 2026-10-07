"""Tester and admin sign-in, sign-out and UI-profile routes (roadmap PR E5).

``/tester``, ``/tester/login``, ``/tester/logout``, ``/admin/login``,
``/admin/logout`` and ``/profile/<profile>``. The URLs, methods, session keys
and redirects are unchanged; only the owner and the endpoint names
(``auth.*``) moved. Authorization stays in :mod:`web.access` and same-origin
request integrity in :mod:`web.request_integrity`: this module only declares
each view's policy. ``ui_profile`` is cosmetic UI state, never authorization.
The module imports neither ``web.app`` nor another controller.
"""

from __future__ import annotations

import hmac
import os

from flask import Blueprint, abort, redirect, render_template, request, session, url_for

from web.access import ADMIN, PROFILE_SWITCH, PUBLIC, access_policy, current_principal
from web.redirects import safe_redirect_target
from web.request_integrity import SAME_ORIGIN, integrity_policy

auth_bp = Blueprint("auth", __name__)

VALID_UI_PROFILES = {"admin", "tester"}


def tester_password_configured() -> bool:
    return bool(os.environ.get("DZI_TESTER_PASSWORD"))


def admin_password_configured() -> bool:
    return bool(os.environ.get("DZI_ADMIN_PASSWORD"))


def password_matches(submitted_password: str | None, configured_password: str | None) -> bool:
    if not isinstance(submitted_password, str) or not isinstance(configured_password, str):
        return False
    return hmac.compare_digest(
        submitted_password.encode("utf-8", errors="surrogatepass"),
        configured_password.encode("utf-8", errors="surrogatepass"),
    )


# Template state follows the request principal, so an account identity decides it
# and legacy keys in the same cookie cannot contradict it. For a legacy session the
# values are what the keys always produced.
def is_admin_authenticated() -> bool:
    return current_principal().is_admin


def is_tester_authenticated() -> bool:
    return current_principal().is_teacher


def can_generate_tests() -> bool:
    return current_principal().is_teacher


@auth_bp.app_context_processor
def inject_tester_auth():
    return {
        "tester_authenticated": is_tester_authenticated(),
        "tester_password_configured": tester_password_configured(),
        "can_generate_tests": can_generate_tests(),
    }


@auth_bp.app_context_processor
def inject_admin_auth():
    return {
        "admin_authenticated": is_admin_authenticated(),
        "admin_password_configured": admin_password_configured(),
    }


@auth_bp.route("/profile/<profile>")
@access_policy(PROFILE_SWITCH)
def switch_profile(profile: str):
    if profile not in VALID_UI_PROFILES:
        abort(404)

    session["ui_profile"] = profile
    fallback = "auth.tester_home" if profile == "tester" else "browsing.index"
    next_url = safe_redirect_target(request.args.get("next"), fallback)
    return redirect(next_url)


@auth_bp.route("/tester")
@access_policy(PUBLIC)
def tester_home():
    return render_template("tester_home.html")


@auth_bp.route("/tester/login", methods=["GET", "POST"])
@access_policy(PUBLIC)
@integrity_policy(SAME_ORIGIN)
def tester_login():
    error = None
    next_url = safe_redirect_target(
        request.args.get("next") or request.form.get("next"),
        "auth.tester_home",
    )

    if request.method == "POST":
        configured_password = os.environ.get("DZI_TESTER_PASSWORD", "")
        submitted_password = request.form.get("password", "")

        if not configured_password:
            error = "Тестерската парола не е зададена. Стартирай сървъра с DZI_TESTER_PASSWORD."
        elif password_matches(submitted_password, configured_password):
            session["tester_authenticated"] = True
            session["ui_profile"] = "tester"
            return redirect(next_url)
        else:
            error = "Грешна парола."

    return render_template("tester_login.html", error=error, next_url=next_url)


@auth_bp.route("/tester/logout")
@access_policy(PUBLIC)
def tester_logout():
    session.pop("tester_authenticated", None)
    if not session.get("admin_authenticated"):
        session["ui_profile"] = "tester"
    return redirect(url_for("auth.tester_home"))


@auth_bp.route("/admin/login", methods=["GET", "POST"])
@access_policy(PUBLIC)
@integrity_policy(SAME_ORIGIN)
def admin_login():
    error = None
    next_url = safe_redirect_target(
        request.args.get("next") or request.form.get("next"),
        "teacher.teacher_dashboard",
    )

    if request.method == "POST":
        configured_password = os.environ.get("DZI_ADMIN_PASSWORD", "")
        submitted_password = request.form.get("password", "")

        if not configured_password:
            error = "Админ паролата не е зададена. Стартирай сървъра с DZI_ADMIN_PASSWORD."
        elif password_matches(submitted_password, configured_password):
            session["admin_authenticated"] = True
            session["ui_profile"] = "admin"
            return redirect(next_url)
        else:
            error = "Грешна парола."

    return render_template("admin_login.html", error=error, next_url=next_url)


@auth_bp.route("/admin/logout")
@access_policy(ADMIN)
def admin_logout():
    session.pop("admin_authenticated", None)
    session["ui_profile"] = "tester"
    return redirect(url_for("browsing.index"))
