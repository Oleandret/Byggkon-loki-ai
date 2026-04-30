"""FastAPI app: HTML pages (landing/admin/help/login), JSON admin API,
APScheduler sync job. Uvicorn entrypoint.

Run with:  uvicorn app.main:app --host 0.0.0.0 --port $PORT
"""
from __future__ import annotations

import asyncio
import os
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import admin_routes
from .auth import AuthManager, set_auth_manager
from .config import Settings, get_settings
from .embeddings import BaseEmbedder, build_embedders
from .graph_client import GraphClient
from .logging_config import configure_logging, get_logger
from .pinecone_store import PineconeStore
from .settings_store import SettingsStore
from .state import StateStore
from .sync import SyncOrchestrator

log = get_logger(__name__)


HERE = Path(__file__).parent


class AppState:
    """Holder for long-lived dependencies. Mutable so the admin UI can
    swap in a freshly-merged Settings without restart."""
    settings: Settings
    settings_store: SettingsStore
    state: StateStore
    graph: GraphClient
    embedders: dict[str, BaseEmbedder]  # provider -> embedder
    pinecone: PineconeStore
    orchestrator: SyncOrchestrator
    scheduler: AsyncIOScheduler
    init_errors: dict[str, str]  # name -> last init error string

    def scheduler_reschedule(self) -> None:
        """Replace the sync job with a fresh trigger derived from current settings."""
        if not getattr(self, "scheduler", None):
            return
        try:
            self.scheduler.remove_job("onedrive-sync")
        except Exception:  # noqa: BLE001
            pass
        self.scheduler.add_job(
            self.orchestrator.run_sync,
            trigger=_build_trigger(self.settings),
            id="onedrive-sync",
            max_instances=1,
            coalesce=True,
            misfire_grace_time=300,
        )
        log.info("scheduler.rescheduled")


_state = AppState()


def _build_trigger(settings: Settings):
    if settings.sync_cron and settings.sync_interval_minutes <= 0:
        return CronTrigger.from_crontab(settings.sync_cron)
    return IntervalTrigger(minutes=max(1, settings.sync_interval_minutes))


@asynccontextmanager
async def lifespan(app: FastAPI):
    bootstrap_settings = get_settings()
    configure_logging(bootstrap_settings.log_level)

    # Auto-generate a session secret if missing so the app still boots.
    if not bootstrap_settings.admin_session_secret:
        from .config import reload_settings
        os.environ["ADMIN_SESSION_SECRET"] = secrets.token_urlsafe(48)
        bootstrap_settings = reload_settings()

    # SettingsStore lives next to the main state DB and overlays env vars.
    _state.settings_store = SettingsStore(
        bootstrap_settings.state_dir,
        fernet_key_seed=bootstrap_settings.admin_session_secret,
    )
    _state.settings = _state.settings_store.effective_settings()

    log.info(
        "app.start",
        brand=_state.settings.brand_name,
        owner=_state.settings.brand_owner,
    )

    # State + downstream clients. Wrap each in try/except so a missing or
    # invalid credential can't keep the app from booting — admin UI must
    # come up so the user can configure.
    _state.state = StateStore(_state.settings.state_dir)

    # Track per-client init errors so the admin UI / test endpoints can
    # surface the *real* reason a client is missing.
    _state.init_errors = {}

    def _safe(name, fn):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001
            err = f"{type(e).__name__}: {e}"
            log.warning("client.init.skip", client=name, err=err)
            _state.init_errors[name] = err
            return None

    _state.graph = _safe("graph", lambda: GraphClient(_state.settings))

    # Build embedders independently per provider so one failure doesn't kill
    # the other.
    embedder_errs: dict[str, str] = {}
    _state.embedders = build_embedders(_state.settings, errors_out=embedder_errs)
    for prov, err in embedder_errs.items():
        _state.init_errors[prov] = err

    _state.pinecone = _safe("pinecone", lambda: PineconeStore(_state.settings))

    if _state.graph and _state.embedders and _state.pinecone:
        _state.orchestrator = SyncOrchestrator(
            _state.settings, _state.graph, _state.embedders, _state.pinecone, _state.state
        )
    else:
        _state.orchestrator = None
        log.warning(
            "orchestrator.disabled",
            hint="Configure Graph + at least one embedding provider + Pinecone in /admin to enable.",
        )

    # Auth manager always reads the *current* settings.
    set_auth_manager(AuthManager(lambda: _state.settings))

    # Templates + static + admin routes.
    templates = Jinja2Templates(directory=str(HERE / "templates"))
    app.mount(
        "/static",
        StaticFiles(directory=str(HERE / "static")),
        name="static",
    )
    admin_routes.configure(templates, _state)
    app.include_router(admin_routes.router)
    app.include_router(admin_routes.api)

    # ─── MCP server (mounted under /mcp) ─────────────────────────────
    if _state.settings.mcp_enabled:
        try:
            from .mcp_server import build_mcp_server, MCPBearerAuth
            mcp = build_mcp_server(_state)
            _state.mcp_session_manager = mcp.session_manager
            wrapped = MCPBearerAuth(
                mcp.streamable_http_app(),
                token_provider=lambda: _state.settings.mcp_bearer_token,
            )
            app.mount("/mcp", wrapped)
            log.info("mcp.mounted", path="/mcp")
        except Exception as e:  # noqa: BLE001
            log.warning("mcp.mount.failed", err=str(e))
            _state.mcp_session_manager = None
    else:
        _state.mcp_session_manager = None

    # Scheduler.
    _state.scheduler = AsyncIOScheduler()
    if _state.orchestrator is not None:
        _state.scheduler.add_job(
            _state.orchestrator.run_sync,
            trigger=_build_trigger(_state.settings),
            id="onedrive-sync",
            max_instances=1,
            coalesce=True,
            misfire_grace_time=300,
        )
    _state.scheduler.start()

    if _state.settings.sync_on_startup and _state.orchestrator is not None:
        asyncio.create_task(_run_startup_sync())

    # MCP session manager needs to be running to handle requests.
    mcp_sm_ctx = None
    sm = getattr(_state, "mcp_session_manager", None)
    if sm is not None:
        mcp_sm_ctx = sm.run()
        await mcp_sm_ctx.__aenter__()

    try:
        yield
    finally:
        log.info("app.shutdown")
        if mcp_sm_ctx is not None:
            try:
                await mcp_sm_ctx.__aexit__(None, None, None)
            except Exception as e:  # noqa: BLE001
                log.warning("mcp.shutdown.error", err=str(e))
        if getattr(_state, "scheduler", None):
            _state.scheduler.shutdown(wait=False)
        if getattr(_state, "graph", None) is not None:
            await _state.graph.aclose()


async def _run_startup_sync() -> None:
    try:
        await _state.orchestrator.run_sync()
    except Exception as e:  # noqa: BLE001
        log.error("startup_sync.error", err=str(e))


app = FastAPI(
    title="Loki AI for Byggkon",
    description="OneDrive → Unstructured → Pinecone knowledge sync",
    lifespan=lifespan,
)


# Public health endpoints (not behind auth) ──────────────────────────
@app.get("/healthz")
async def healthz() -> dict:
    return {"status": "ok"}
