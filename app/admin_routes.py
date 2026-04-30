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

    # Hot-reload downstream clients whose credentials/config touched here.
    # No process restart needed — the new clients pick up the new Settings.
    rebuilt: list[str] = []
    touched = set(updates.keys())

    if touched & {"openai_api_key", "openai_embedding_model",
                  "gemini_api_key", "gemini_embedding_model",
                  "gemini_embed_images", "embedding_provider",
                  "embedding_batch_size"}:
        try:
            from .embeddings import build_embedders
            _app_state.embedders = build_embedders(_app_state.settings) or {}
            rebuilt.append("embedders")
        except Exception as e:  # noqa: BLE001
            log.warning("admin.rebuild.embedders.error", err=str(e))

    if touched & {"pinecone_namespace"}:
        # Index credentials and names are flagged as requires_restart; only
        # the namespace can change live (it's used per-call).
        rebuilt.append("pinecone (namespace only)")

    # Rebuild the orchestrator if any client changed and we have the full set.
    if rebuilt and getattr(_app_state, "graph", None) and \
            getattr(_app_state, "pinecone", None) and _app_state.embedders:
        from .sync import SyncOrchestrator
        _app_state.orchestrator = SyncOrchestrator(
            _app_state.settings,
            _app_state.graph,
            _app_state.embedders,
            _app_state.pinecone,
            _app_state.state,
        )
        rebuilt.append("orchestrator")

    # Reschedule the sync job if interval/cron changed.
    try:
        _app_state.scheduler_reschedule()
    except Exception as e:  # noqa: BLE001
        log.warning("admin.scheduler.reschedule.error", err=str(e))

    return {
        "ok": True,
        "restart_required_for": restart_keys,
        "hot_reloaded": rebuilt,
    }


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


@api.get("/events")
async def api_events(
    run_id: int | None = None,
    limit: int = 200,
    since_id: int | None = None,
) -> dict:
    """Recent sync events. Filter by run_id, paginate with since_id."""
    events = _app_state.state.latest_events(
        run_id=run_id, limit=min(limit, 500), since_id=since_id
    )
    return {"events": events, "run_id": run_id}


@api.get("/mcp/info")
async def api_mcp_info(request: Request) -> dict:
    """Connection details for the MCP tab — masked token, URL, status."""
    s = _app_state.settings
    # Build the public URL from the request — works whether you're on
    # localhost or behind Railway's proxy (X-Forwarded-Host).
    fwd_host = request.headers.get("x-forwarded-host")
    fwd_proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    host = fwd_host or request.url.netloc
    base_url = f"{fwd_proto}://{host}"
    mcp_url = f"{base_url}/mcp" if s.mcp_enabled else ""

    masked_token = ""
    if s.mcp_bearer_token:
        from .settings_store import _mask  # type: ignore
        masked_token = _mask(s.mcp_bearer_token)

    return {
        "enabled": s.mcp_enabled,
        "url": mcp_url,
        "bearer_token_masked": masked_token,
        "bearer_token_set": bool(s.mcp_bearer_token),
        "default_provider": s.mcp_default_provider,
        "default_top_k": s.mcp_default_top_k,
        "tools": [
            {"name": "search_knowledge", "args": ["query", "top_k?", "provider?", "drive_id?", "modality?"]},
            {"name": "get_file_chunks", "args": ["file_id", "drive_id?", "provider?"]},
            {"name": "list_indexed_drives", "args": []},
        ],
    }


@api.get("/progress")
async def api_progress() -> dict:
    """Live per-drive progress + aggregate ETA for the current sync run."""
    snap = _app_state.state.progress_snapshot()
    now = time.time()

    total_seen = 0
    total_processed = 0
    total_estimated = 0
    earliest_start = now
    drives_done = 0
    drives_active = 0
    drives_pending = 0

    for row in snap:
        total_seen += int(row.get("files_seen") or 0)
        total_processed += int(row.get("files_processed") or 0)
        if row.get("estimated_total"):
            total_estimated += int(row["estimated_total"])
        if row.get("started_at") and row["started_at"] < earliest_start:
            earliest_start = row["started_at"]
        phase = row.get("phase")
        if phase == "done":
            drives_done += 1
        elif phase == "syncing":
            drives_active += 1
        else:
            drives_pending += 1

    elapsed = max(0.001, now - earliest_start) if snap else 0
    rate = (total_processed / elapsed) if elapsed > 0 else 0
    eta = None
    if rate > 0 and total_estimated and total_processed < total_estimated:
        remaining = max(0, total_estimated - total_processed)
        eta = int(remaining / rate)

    # Most recent run + scheduler next-run.
    last_run = None
    runs = _app_state.state.latest_runs(limit=1)
    if runs:
        last_run = runs[0]

    sched = getattr(_app_state, "scheduler", None)
    next_run_at = None
    if sched is not None and sched.running:
        job = sched.get_job("onedrive-sync")
        if job and job.next_run_time:
            next_run_at = job.next_run_time.timestamp()

    return {
        "now": now,
        "drives": snap,
        "summary": {
            "total_seen": total_seen,
            "total_processed": total_processed,
            "total_estimated": total_estimated or None,
            "drives_done": drives_done,
            "drives_active": drives_active,
            "drives_pending": drives_pending,
            "files_per_minute": round(rate * 60, 1),
            "elapsed_seconds": int(elapsed),
            "eta_seconds": eta,
        },
        "last_run": last_run,
        "next_run_at": next_run_at,
    }


@api.post("/sync")
async def api_trigger_sync() -> dict:
    if _app_state.orchestrator is None:
        return {"status": "skipped", "reason": "Not configured. Fill in Graph/OpenAI/Pinecone settings first."}
    asyncio.create_task(_app_state.orchestrator.run_sync())
    return {"status": "started"}


# ─── Test connections ────────────────────────────────────────────────
@api.post("/test/graph")
async def api_test_graph() -> dict:
    """Test the Graph connection with full diagnostics so we can pinpoint
    exactly where in the chain things fail."""
    s = _app_state.settings
    init_err = (getattr(_app_state, "init_errors", {}) or {}).get("graph")

    # Step 1: are the three creds set in *some* form?
    cred_status = {
        "GRAPH_TENANT_ID": _state_of(s.graph_tenant_id),
        "GRAPH_CLIENT_ID": _state_of(s.graph_client_id),
        "GRAPH_CLIENT_SECRET": _state_of(s.graph_client_secret),
    }
    missing = [k for k, v in cred_status.items() if v["status"] == "missing"]

    # Step 2: did the GraphClient construct successfully at boot?
    if _app_state.graph is None:
        return {
            "ok": False,
            "stage": "client_init",
            "error": init_err or "GraphClient is None (no init error captured)",
            "credentials": cred_status,
            "missing": missing,
            "hint": (
                "Fyll inn alle tre credentials i Railway og redeploy, "
                "eller via /admin → Innstillinger."
                if missing
                else "Sjekk at GRAPH_TENANT_ID er en gyldig GUID. "
                     f"Authority URL: {s.graph_authority_url!r}"
            ),
        }

    # Step 3: try to acquire a token (this is where most cred errors surface)
    try:
        token = await _app_state.graph._get_token()  # noqa: SLF001
    except Exception as e:  # noqa: BLE001
        return {
            "ok": False,
            "stage": "token_acquire",
            "error": str(e),
            "credentials": cred_status,
            "hint": (
                "Token-acquire feilet. Vanligste grunner: feil client_secret, "
                "secret er utløpt, eller tenant_id peker til feil tenant. "
                "Sjekk i Entra ID → App registrations → Certificates & secrets."
            ),
        }

    # Step 4: hit /organization to verify permissions and admin consent.
    try:
        data = await _app_state.graph.get_json("/organization")
        orgs = data.get("value") or []
        if not orgs:
            return {
                "ok": False,
                "stage": "organization_read",
                "error": "Got 200 but no organization in response",
                "raw": data,
            }
        org = orgs[0]
        return {
            "ok": True,
            "stage": "ok",
            "tenant_display_name": org.get("displayName"),
            "tenant_id": org.get("id"),
            "verified_domains": [
                d.get("name") for d in org.get("verifiedDomains") or []
            ][:5],
            "token_acquired": bool(token),
        }
    except Exception as e:  # noqa: BLE001
        msg = str(e)
        hint = "Ukjent feil — se logg."
        if "403" in msg or "Forbidden" in msg.lower():
            hint = (
                "403 Forbidden — admin consent mangler eller feil "
                "permissions. Gå til Entra ID → API permissions → "
                "klikk 'Grant admin consent for {tenant}'. Påkrevd: "
                "Files.Read.All, User.Read.All. Sites.Read.All hvis SharePoint."
            )
        elif "401" in msg or "Unauthorized" in msg.lower():
            hint = (
                "401 Unauthorized — client_secret er sannsynligvis "
                "utløpt eller feil. Lag et nytt secret i Entra ID."
            )
        return {
            "ok": False,
            "stage": "api_call",
            "error": msg,
            "hint": hint,
        }


def _state_of(value: str) -> dict:
    v = (value or "").strip()
    if not v:
        return {"status": "missing", "length": 0, "preview": ""}
    if len(v) <= 8:
        preview = "•" * len(v)
    else:
        preview = f"{v[:4]}…{v[-4:]}"
    return {"status": "set", "length": len(v), "preview": preview}


@api.post("/test/openai")
async def api_test_openai() -> dict:
    s = _app_state.settings
    init_err = (getattr(_app_state, "init_errors", {}) or {}).get("openai")
    cred = _state_of(s.openai_api_key)
    selected = "openai" in s.providers()

    embedder = (getattr(_app_state, "embedders", {}) or {}).get("openai")
    if embedder is None:
        return {
            "ok": False,
            "stage": "client_init",
            "error": init_err or (
                "Provider 'openai' is not selected (EMBEDDING_PROVIDER)"
                if not selected else "OpenAI client failed to construct"
            ),
            "credentials": {"OPENAI_API_KEY": cred},
            "selected_as_provider": selected,
            "hint": (
                "Sett EMBEDDING_PROVIDER=openai eller =both, og fyll inn OPENAI_API_KEY."
                if not selected or cred["status"] == "missing"
                else "Sjekk at API-nøkkelen er gyldig (begynner med sk-)."
            ),
        }
    try:
        vec = await embedder.embed_texts(["Loki AI test."])
    except Exception as e:  # noqa: BLE001
        msg = str(e)
        hint = "Ukjent feil — se logg."
        low = msg.lower()
        if "401" in msg or "invalid" in low and "key" in low:
            hint = "Ugyldig API-nøkkel. Lag en ny på platform.openai.com → API keys."
        elif "model" in low and ("not found" in low or "does not exist" in low):
            hint = (
                f"Modellen '{s.openai_embedding_model}' er ikke tilgjengelig på "
                "denne kontoen. Bytt til text-embedding-3-small eller sjekk kontoens tilganger."
            )
        elif "quota" in low or "rate" in low:
            hint = "Quota- eller rate-limit. Vent litt og prøv igjen, eller øk usage-limit på OpenAI."
        return {"ok": False, "stage": "api_call", "error": msg, "hint": hint}

    return {
        "ok": True,
        "stage": "ok",
        "dim": len(vec[0]),
        "model": s.openai_embedding_model,
        "credentials": {"OPENAI_API_KEY": cred},
    }


@api.post("/test/gemini")
async def api_test_gemini() -> dict:
    s = _app_state.settings
    init_err = (getattr(_app_state, "init_errors", {}) or {}).get("gemini")
    cred = _state_of(s.gemini_api_key)
    selected = "gemini" in s.providers()

    embedder = (getattr(_app_state, "embedders", {}) or {}).get("gemini")
    if embedder is None:
        return {
            "ok": False,
            "stage": "client_init",
            "error": init_err or (
                "Provider 'gemini' is not selected (EMBEDDING_PROVIDER)"
                if not selected else "Gemini client failed to construct"
            ),
            "credentials": {"GEMINI_API_KEY": cred},
            "selected_as_provider": selected,
            "hint": (
                "Sett EMBEDDING_PROVIDER=gemini eller =both, og fyll inn GEMINI_API_KEY "
                "fra https://aistudio.google.com/apikey."
                if not selected or cred["status"] == "missing"
                else "Sjekk at google-genai-pakken er installert (krever ny redeploy etter requirements-endring)."
            ),
        }
    try:
        vec = await embedder.embed_texts(["Loki AI test."])
    except Exception as e:  # noqa: BLE001
        msg = str(e)
        low = msg.lower()
        hint = "Ukjent feil — se logg."
        if "api key" in low or "401" in msg or "permission" in low:
            hint = (
                "API-nøkkel ugyldig eller mangler tillatelser. Lag en ny på "
                "https://aistudio.google.com/apikey og sjekk at modellen er tilgjengelig i din region."
            )
        elif "model" in low and ("not found" in low or "404" in msg):
            hint = (
                f"Modellen '{s.gemini_embedding_model}' eksisterer ikke. Sjekk staving "
                "(skal være 'gemini-embedding-2-preview' eller 'gemini-embedding-2'). "
                "Modellen kan også være regionsbegrenset."
            )
        elif "quota" in low or "rate" in low or "429" in msg:
            hint = "Quota- eller rate-limit. Vent litt eller hev grensen på AI Studio."
        return {"ok": False, "stage": "api_call", "error": msg, "hint": hint}

    return {
        "ok": True,
        "stage": "ok",
        "dim": len(vec[0]),
        "model": s.gemini_embedding_model,
        "credentials": {"GEMINI_API_KEY": cred},
    }


@api.post("/test/pinecone")
async def api_test_pinecone() -> dict:
    s = _app_state.settings
    init_err = (getattr(_app_state, "init_errors", {}) or {}).get("pinecone")
    cred = _state_of(s.pinecone_api_key)

    if _app_state.pinecone is None:
        return {
            "ok": False,
            "stage": "client_init",
            "error": init_err or "Pinecone client failed to construct",
            "credentials": {"PINECONE_API_KEY": cred},
            "hint": (
                "Sett PINECONE_API_KEY i Railway og redeploy."
                if cred["status"] == "missing"
                else "Sjekk at API-nøkkelen er gyldig (begynner med pcsk_ eller pcn-)."
            ),
        }

    indexes_configured = {
        "openai": s.resolved_openai_index() if "openai" in s.providers() else None,
        "gemini": s.resolved_gemini_index() if "gemini" in s.providers() else None,
    }

    try:
        per_index = await _app_state.pinecone.index_stats()
    except Exception as e:  # noqa: BLE001
        return {
            "ok": False,
            "stage": "api_call",
            "error": str(e),
            "credentials": {"PINECONE_API_KEY": cred},
            "indexes_configured": indexes_configured,
        }

    # If any index has an error in its stats, surface it
    issues = [
        f"{name}: {info['error']}"
        for name, info in per_index.items()
        if isinstance(info, dict) and info.get("error")
    ]
    if issues:
        return {
            "ok": False,
            "stage": "index_describe",
            "error": "; ".join(issues),
            "indexes_configured": indexes_configured,
            "indexes": per_index,
            "hint": (
                "Indeksen er konfigurert men finnes ikke i Pinecone. Kjør "
                "`python -m scripts.bootstrap_pinecone` lokalt med riktige env-vars."
            ),
        }

    return {
        "ok": True,
        "stage": "ok",
        "indexes_configured": indexes_configured,
        "indexes": per_index,
    }


# ─── Graph helpers used by the Folders tab ──────────────────────────
@api.get("/graph/users")
async def api_graph_users(limit: int = 200) -> dict:
    """List users in the tenant for the folder picker."""
    if _app_state.graph is None:
        raise HTTPException(503, "Graph not configured")
    out = []
    async for u in _app_state.graph.iter_users():
        if len(out) >= limit:
            break
        out.append({
            "id": u.get("id"),
            "upn": u.get("userPrincipalName"),
            "display_name": u.get("displayName"),
        })
    return {"users": out}


@api.get("/graph/folders")
async def api_graph_folders(user: str) -> dict:
    """Return the top-level folders in a user's OneDrive, plus the
    currently saved selection for that user."""
    if _app_state.graph is None:
        raise HTTPException(503, "Graph not configured")
    drv = await _app_state.graph.get_user_drive(user)
    if not drv:
        raise HTTPException(404, f"No drive for {user}")
    drive_id = drv["id"]
    root_id = await _app_state.graph.get_root_folder_id(drive_id)
    folders: list[dict] = []
    if root_id:
        async for child in _app_state.graph.iter_folder_children(drive_id, root_id):
            if "folder" not in child:
                continue
            name = child.get("name", "")
            path = f"/{name}"
            folders.append({
                "id": child.get("id"),
                "name": name,
                "path": path,
                "child_count": (child.get("folder") or {}).get("childCount", 0),
            })
    selections = _app_state.settings.folder_selections().get(user, [])
    return {"drive_id": drive_id, "user": user, "folders": folders, "selected_paths": selections}


@api.post("/graph/folder-selection")
async def api_graph_folder_selection(payload: dict) -> dict:
    """Save the per-user folder selection back into settings."""
    user = payload.get("user")
    paths = payload.get("paths") or []
    if not user:
        raise HTTPException(400, "user is required")
    import json
    current = _app_state.settings.folder_selections()
    current[user] = list(paths)
    new_value = json.dumps(current)
    _app_state.settings_store.set_overrides({"sync_folder_selections": new_value})
    _app_state.settings = _app_state.settings_store.effective_settings()
    return {"ok": True, "saved": {user: paths}}


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
    init_errs = getattr(_app_state, "init_errors", {}) or {}
    embedder = embedders.get(name)
    if embedder is None:
        s = _app_state.settings
        wanted = s.embedding_provider.value
        if wanted != name and wanted != "both":
            return {"ok": True, "status": "ok", "detail": "Ikke valgt som provider"}
        # Selected but missing — surface the actual init error if we have one.
        err = init_errs.get(name)
        if err:
            return {"ok": False, "status": "warn", "detail": err}
        return {"ok": False, "status": "warn", "detail": "Mangler API-nøkkel"}
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
