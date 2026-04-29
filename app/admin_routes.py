"""HTML pages and JSON API for the admin UI.

Mounted from main.py. All HTML is server-rendered Jinja2; client behaviour
lives in /static/js/admin.js as plain JS (no build step).
"""
from __future__ import annotations

import asyncio
import os
import shutil
import sys
import time
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
    if _app_state.pinecone is None:
        s["pinecone_error"] = "Not configured"
        return s
    try:
        s["pinecone"] = await _app_state.pinecone.index_stats()
    except Exception as e:  # noqa: BLE001
        s["pinecone_error"] = str(e)
    s["providers"] = list((getattr(_app_state, "embedders", {}) or {}).keys())
    return s


@api.get("/runs")
async def api_runs(limit: int = 20) -> dict:
    return {"runs": _app_state.state.latest_runs(limit=limit)}


@api.post("/sync")
async def api_trigger_sync() -> dict:
    if _app_state.orchestrator is None:
        return {"status": "skipped", "reason": "Not configured. Fill in Graph/OpenAI/Pinecone settings first."}
    asyncio.create_task(_app_state.orchestrator.run_sync())
    return {"status": "started"}


# ─── Test connections ────────────────────────────────────────────────
@api.post("/test/graph")
async def api_test_graph() -> dict:
    if _app_state.graph is None:
        return {"ok": False, "error": "Graph not configured (missing tenant/client/secret)"}
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
    embedders = getattr(_app_state, "embedders", {}) or {}
    embedder = embedders.get("openai")
    if embedder is None:
        return {"ok": False, "error": "OpenAI not configured (missing api key or not selected as provider)"}
    try:
        vec = await embedder.embed_texts(["Loki AI test."])
        return {"ok": True, "dim": len(vec[0]), "model": _app_state.settings.openai_embedding_model}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}


@api.post("/test/gemini")
async def api_test_gemini() -> dict:
    embedders = getattr(_app_state, "embedders", {}) or {}
    embedder = embedders.get("gemini")
    if embedder is None:
        return {"ok": False, "error": "Gemini not configured (missing api key or not selected as provider)"}
    try:
        vec = await embedder.embed_texts(["Loki AI test."])
        return {"ok": True, "dim": len(vec[0]), "model": _app_state.settings.gemini_embedding_model}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}


@api.post("/test/pinecone")
async def api_test_pinecone() -> dict:
    if _app_state.pinecone is None:
        return {"ok": False, "error": "Pinecone not configured (missing api key)"}
    try:
        per_index = await _app_state.pinecone.index_stats()
        return {"ok": True, "indexes": per_index}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}


# ─── Aggregate health check ──────────────────────────────────────────
@api.get("/health")
async def api_health() -> dict:
    """Run every health check in parallel and return a single status payload.

    Each check returns:
        { "ok": bool, "status": "ok|warn|err", "detail": str, "ms": int }
    """
    checks: dict[str, Any] = {}

    async def timed(name: str, coro):
        t = time.monotonic()
        try:
            res = await coro
        except Exception as e:  # noqa: BLE001
            res = {"ok": False, "status": "err", "detail": str(e)}
        ms = int((time.monotonic() - t) * 1000)
        if "status" not in res:
            res["status"] = "ok" if res.get("ok") else "err"
        res["ms"] = ms
        checks[name] = res

    await asyncio.gather(
        timed("application", _check_application()),
        timed("graph", _check_graph()),
        timed("openai", _check_provider("openai")),
        timed("gemini", _check_provider("gemini")),
        timed("pinecone_openai", _check_pinecone_index("openai")),
        timed("pinecone_gemini", _check_pinecone_index("gemini")),
        timed("state_db", _check_state_db()),
        timed("disk", _check_disk()),
        timed("scheduler", _check_scheduler()),
        timed("sync_history", _check_sync_history()),
    )

    overall = "ok"
    for v in checks.values():
        if v["status"] == "err":
            overall = "err"
            break
        if v["status"] == "warn" and overall != "err":
            overall = "warn"
    return {"overall": overall, "generated_at": time.time(), "checks": checks}


# ─── Individual health checks ────────────────────────────────────────
async def _check_application() -> dict:
    s = _app_state.settings
    return {
        "ok": True,
        "detail": (
            f"Python {sys.version.split()[0]} · "
            f"provider={s.embedding_provider.value} · "
            f"scope={s.sync_scope.value}"
        ),
    }


async def _check_graph() -> dict:
    if _app_state.graph is None:
        return {"ok": False, "status": "warn", "detail": "Ikke konfigurert"}
    try:
        data = await _app_state.graph.get_json("/organization")
        org = (data.get("value") or [{}])[0]
        name = org.get("displayName") or org.get("id") or "ukjent"
        return {"ok": True, "detail": f"Tenant: {name}"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "detail": f"Feil: {e}"}


async def _check_provider(name: str) -> dict:
    embedders = getattr(_app_state, "embedders", {}) or {}
    embedder = embedders.get(name)
    if embedder is None:
        s = _app_state.settings
        wanted = s.embedding_provider.value
        if wanted == name or wanted == "both":
            return {"ok": False, "status": "warn", "detail": "Mangler API-nøkkel"}
        return {"ok": True, "status": "ok", "detail": "Ikke valgt som provider"}
    try:
        vec = await embedder.embed_texts(["health"])
        return {"ok": True, "detail": f"{embedder.dimensions} dim · model {embedder.name}"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "detail": f"Feil: {e}"}


async def _check_pinecone_index(provider: str) -> dict:
    pc = _app_state.pinecone
    s = _app_state.settings
    expected_name = (
        s.resolved_openai_index() if provider == "openai" else s.resolved_gemini_index()
    )
    is_active = provider in s.providers()
    if not expected_name:
        return {
            "ok": True if not is_active else False,
            "status": "ok" if not is_active else "warn",
            "detail": "Ikke konfigurert" if not is_active else "Provider er valgt men indeksnavn mangler",
        }
    if pc is None:
        return {"ok": False, "status": "warn", "detail": "Pinecone-klient ikke konfigurert"}
    try:
        stats = await pc.index_stats_for(provider)
        if "error" in stats:
            return {"ok": False, "detail": stats["error"]}
        total = int(stats.get("total_vector_count") or stats.get("totalVectorCount") or 0)
        return {"ok": True, "detail": f"{expected_name}: {total} vektorer"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "detail": f"Feil: {e}"}


async def _check_state_db() -> dict:
    try:
        s = _app_state.state.stats()
        return {
            "ok": True,
            "detail": f"{s.get('indexed_files', 0)} filer · {s.get('tracked_drives', 0)} drives",
        }
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "detail": f"Feil: {e}"}


async def _check_disk() -> dict:
    paths = [_app_state.settings.state_dir, _app_state.settings.tmp_dir]
    try:
        total, used, free = shutil.disk_usage(paths[0])
        free_gb = free / (1024 ** 3)
        used_pct = 100.0 * used / total if total else 0
        status = "ok"
        if free_gb < 0.5:
            status = "err"
        elif free_gb < 2:
            status = "warn"
        return {
            "ok": status != "err",
            "status": status,
            "detail": f"{free_gb:.1f} GB ledig · {used_pct:.0f}% brukt på {paths[0]}",
        }
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "detail": f"Kunne ikke lese disk: {e}"}


async def _check_scheduler() -> dict:
    sch = getattr(_app_state, "scheduler", None)
    if sch is None or not sch.running:
        return {"ok": False, "status": "warn", "detail": "Ikke kjørende"}
    job = sch.get_job("onedrive-sync")
    if job is None:
        return {
            "ok": False,
            "status": "warn",
            "detail": "Sync-jobben er ikke registrert (orchestrator ikke aktivert)",
        }
    next_run = job.next_run_time
    return {
        "ok": True,
        "detail": f"Neste kjøring: {next_run.isoformat() if next_run else 'ukjent'}",
    }


async def _check_sync_history() -> dict:
    try:
        runs = _app_state.state.latest_runs(limit=5)
        if not runs:
            return {"ok": True, "status": "warn", "detail": "Ingen kjøringer ennå"}
        last = runs[0]
        errors = int(last.get("errors") or 0)
        indexed = int(last.get("files_indexed") or 0)
        if last.get("finished_at") is None:
            return {"ok": True, "status": "warn", "detail": "Kjøring pågår"}
        status = "ok" if errors == 0 else "warn"
        return {
            "ok": True,
            "status": status,
            "detail": f"Siste: {indexed} indeksert, {errors} feil",
        }
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "detail": f"Feil: {e}"}


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
