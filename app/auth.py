"""Admin password auth with signed cookie sessions.

The admin password lives in the SettingsStore (or env var as fallback).
We don't store password hashes; the password itself is the secret, and
the cookie is signed with itsdangerous so it can't be forged.
"""
from __future__ import annotations

import hmac
import secrets
from typing import Optional

from fastapi import Cookie, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from itsdangerous import BadSignature, SignatureExpired, TimestampSigner

from .config import Settings
from .logging_config import get_logger

log = get_logger(__name__)


COOKIE_NAME = "loki_session"


class AuthManager:
    def __init__(self, settings_provider) -> None:
        """settings_provider: callable returning the current Settings."""
        self._settings_provider = settings_provider

    def _signer(self) -> TimestampSigner:
        s: Settings = self._settings_provider()
        secret = s.admin_session_secret or "loki-default-session-secret-change-me"
        return TimestampSigner(secret, salt="loki-admin-session")

    def is_configured(self) -> bool:
        s: Settings = self._settings_provider()
        return bool(s.admin_password)

    def verify_password(self, plain: str) -> bool:
        s: Settings = self._settings_provider()
        if not s.admin_password:
            return False
        return hmac.compare_digest(plain.encode(), s.admin_password.encode())

    def create_session_cookie(self) -> tuple[str, int]:
        s: Settings = self._settings_provider()
        token = self._signer().sign(secrets.token_urlsafe(24)).decode()
        max_age = max(1, s.admin_session_hours) * 3600
        return token, max_age

    def is_valid(self, raw: Optional[str]) -> bool:
        if not raw:
            return False
        s: Settings = self._settings_provider()
        max_age = max(1, s.admin_session_hours) * 3600
        try:
            self._signer().unsign(raw, max_age=max_age)
            return True
        except (BadSignature, SignatureExpired):
            return False


# A module-level holder; main.py sets this on startup.
_auth: Optional[AuthManager] = None


def set_auth_manager(mgr: AuthManager) -> None:
    global _auth
    _auth = mgr


def get_auth_manager() -> AuthManager:
    if _auth is None:
        raise RuntimeError("AuthManager not initialised")
    return _auth


# ─── FastAPI dependencies ────────────────────────────────────────────
async def require_session(request: Request) -> None:
    """Use as a FastAPI dependency on protected JSON endpoints."""
    cookie = request.cookies.get(COOKIE_NAME)
    if not get_auth_manager().is_valid(cookie):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not signed in")


async def require_session_html(request: Request):
    """Variant for HTML pages: redirect to /login on failure."""
    cookie = request.cookies.get(COOKIE_NAME)
    if not get_auth_manager().is_valid(cookie):
        return RedirectResponse("/login", status_code=303)
    return None
