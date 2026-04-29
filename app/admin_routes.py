"""HTML pages and JSON API for the admin UI.

Mounted from main.py. All HTML is server-rendered Jinja2; client behaviour
lives in /static/js/admin.js as plain JS (no build step).
"""
from __future__ import annotations

import asyncio
import os
from typing import Any, Optional

from fastapi import APIRouter, Cookie, Depends, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .auth import (
    COOKIE_NAME,
    get_auth_manager,
    require_session,
)
from .logging_config import get_logger

log = get_logger(__name__)


router = APIRouter()


# Templates are wired up in main.py and passed in via dependency.
_templates: Optional[Jinja2Templates] = None
_app_state = None  # set from main.py to avoid import cycle


def configure(templates: Jinja2Templates, app_state) -> None:
    global _templates, _app_state
    _templates = templates
    _app_state = app_state


def _ctx(request: Request, **extra) -> dict[str, Any]:
    settings = _app_state.settings
    auth_required = get_auth_manager().is_configured()
    cookie = request.cookies.get(COOKIE_NAME)
    is_logged_in = get_auth_manager().is_valid(cookie) if auth_required else False
    return {
        "request": request,
        "brand_name": settings.brand_name,
        "brand_owner": settings.brand_owner,
        "auth_required": auth_required,
        "is_logged_in": is_logged_in,
        **extra,
    }


# ─── Public HTML pages ────────────────────────────────────────────────
@router.get("/", response_class=HTMLResponse)
async def landing(request: Request) -> Response:
    return _templates.TemplateResponse("landing.html", _ctx(request))


@router.get("/help", response_class=HTMLResponse)
async def help_page(request: Request) -> Response:
    return _templates.TemplateResponse("help.html", _ctx(request))


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, error: Optional[str] = None) -> Response:
    auth = get_auth_manager()
    if not auth.is_configured():
        return _templates.TemplateResponse(
            "login.html",
            _ctx(request, error="Admin-passord er ikke satt. Sett ADMIN_PASSWORD i miljøet og start på nytt."),
        )
    return _templates.TemplateResponse("login.html", _ctx(request, error=error))


@router.post("/login")
async def login_submit(request: Request, password: str = Form(...)) -> Response:
    auth = get_auth_manager()
    if not auth.is_configured():
        return RedirectResponse("/login", status_code=303)
    if not auth.verify_password(password):
        log.warning("admin.login.failed", ip=_ip(request))
        return RedirectResponse("/login?error=Feil+passord", status_code=303)

    token, max_age = auth.create_session_cookie()
    response = RedirectResponse("/admin", status_code=303)
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        max_age=max_age,
        httponly=True,
        samesite="lax",
        secure=_secure_cookies(request),
    )
    log.info("admin.login.ok", ip=_ip(request))
    return response


@router.post("/logout")
async def logout(request: Request) -> Response:
    response = RedirectResponse("/", status_code=303)
    response.delete_cookie(COOKIE_NAME, samesite="lax")
    return response


# ─── Protected admin HTML page ───────────────────────────────────────
@router.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request) -> Response:
    auth = get_auth_manager()
    cookie = request.cookies.get(COOKIE_NAME)
    if auth.is_configured() and not auth.is_valid(cookie):
        return RedirectResponse("/login", status_code=303)
    return _templates.TemplateResponse("admin.html", _ctx(request))


# ─── JSON API (protected) ────────────────────────────────────────────
api = APIRouter(prefix="/api", dependencies=[Depends(require_session)])


@api.get("/fields")
async def api_fields() -> dict:
    from .settings_store import fields_for_ui
    return {"fields": fields_for_ui()}


@api.get("/settings")
async def api_get_settings() -> dict:
    """Current overrides + their effective merged values (secrets masked)."""
    overrides = _app_state.settings_store.get_overrides(reveal_secrets=False)
    # The effective value is what's actually in use right now.
    effective = _app_state.settings.model_dump()
    # Mask secrets in effective view too.
    from .settings_store import FIELDS, _mask  # type: ignore
    masked_effective: dict[str, Any] = {}
    for f in FIELDS:
        v = effective.get(f.key, "")
        if f.kind == "password":
            masked_effective[f.key] = _mask(str(v))
        else:
            masked_effective[f.key] = v
    return {"overrides": overrides, "effective": masked_effective}


@api.patch("/settings")
async def api_patch_settings(payload: dict) -> dict:
    updates = payload.get("updates") or {}
    if not isinstance(updates, dict):
        raise HTTPException(400, "updates must be a JSON object")

    restart_keys = _app_state.settings_store.set_overrides(updates)

    # Rebuild the live Settings instance so non-restart fields take effect now.
    _app_state.settings = _app_state.settings_store.effective_settings()

    # Reschedule the sync job if interval/cron changed.
    try:
        _app_state.scheduler_reschedule()
    except Exception as e:  # noqa: BLE001
        log.warning("admin.scheduler.reschedule.error", err=str(e))

    return {"ok": True, "restart_required_for": restart_keys}


@api.get("/stats")
async def api_stats() -> dict:
    s = _app_state.state.stats()
    try:
        idx = await _app_state.pinecone.index_stats()
        s["pinecone"] = idx if isinstance(idx, dict) else dict(idx)
    except Exception as e:  # noqa: BLE001
        s["pinecone_error"] = str(e)
    return s


@api.get("/runs")
async def api_runs(limit: int = 20) -> dict:
    return {"runs": _app_state.state.latest_runs(limit=limit)}


@api.post("/sync")
async def api_trigger_sync() -> dict:
    asyncio.create_task(_app_state.orchestrator.run_sync())
    return {"status": "started"}


# ─── Test connections ────────────────────────────────────────────────
@api.post("/test/graph")
async def api_test_graph() -> dict:
    try:
        # /me would be a delegated call; for app-only we hit /organization
        # which works with even minimal scopes.
        data = await _app_state.graph.get_json("/organization")
        org = (data.get("value") or [{}])[0]
        return {
            "ok": True,
            "tenant_display_name": org.get("displayName"),
            "tenant_id": org.get("id"),
        }
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}


@api.post("/test/openai")
async def api_test_openai() -> dict:
    try:
        vec = await _app_state.embedder.embed(["Loki AI test."])
        return {"ok": True, "dim": len(vec[0])}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}


@api.post("/test/pinecone")
async def api_test_pinecone() -> dict:
    try:
        stats = await _app_state.pinecone.index_stats()
        out = stats if isinstance(stats, dict) else dict(stats)
        return {"ok": True, "stats": out}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}


# ─── helpers ─────────────────────────────────────────────────────────
def _ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "?"


def _secure_cookies(request: Request) -> bool:
    # Trust X-Forwarded-Proto for Railway/behind-proxy deployments.
    proto = request.headers.get("x-forwarded-proto")
    if proto:
        return proto.lower() == "https"
    return request.url.scheme == "https"
