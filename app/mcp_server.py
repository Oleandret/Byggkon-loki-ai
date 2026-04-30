"""MCP (Model Context Protocol) server.

Exposes the indexed Pinecone knowledge base as MCP tools so external LLMs
(Claude Desktop, Cursor, Continue, etc.) can query Byggkon's documents
without any custom integration code on their side.

Tools exposed:
  • search_knowledge(query, top_k?, provider?, drive_id?, modality?)
        Vector-search Pinecone with the same embedder we used for indexing.
        Returns hits with text, file_name, web_url, drive_owner, score.

  • get_file_chunks(file_id, drive_id?, provider?)
        Return every chunk (text + image) we have for a specific file,
        ordered by chunk_index.

  • list_indexed_drives()
        Quick inventory of which OneDrives/SharePoint sites are indexed.

Transport: streamable HTTP, mounted on the same FastAPI app at /mcp.
Auth:      Bearer token. Set MCP_BEARER_TOKEN.
"""
from __future__ import annotations

import asyncio
from typing import Any, Optional

from mcp.server.fastmcp import FastMCP

from .config import Settings
from .logging_config import get_logger

log = get_logger(__name__)


def _hit_to_dict(match: Any) -> dict:
    """Coerce a Pinecone match object into a plain dict."""
    if isinstance(match, dict):
        return match
    out = {
        "id": getattr(match, "id", None),
        "score": getattr(match, "score", None),
        "metadata": getattr(match, "metadata", {}) or {},
    }
    return out


def build_mcp_server(app_state) -> FastMCP:
    """Create the FastMCP instance, wire it to the AppState, return it.

    The caller is expected to:
        app.mount("/mcp", mcp.streamable_http_app())
        # ...and run mcp.session_manager inside the FastAPI lifespan.
    """
    mcp = FastMCP(
        name=f"{app_state.settings.brand_name} ({app_state.settings.brand_owner})",
        stateless_http=True,
        json_response=True,
    )

    # ─── Auth dependency (manual — FastMCP doesn't expose middleware yet) ─
    def _check_auth() -> None:
        # FastMCP routes through Starlette; the simplest pattern is to wrap
        # the streamable_http_app with our own middleware in main.py.
        pass

    # ─── search_knowledge ─────────────────────────────────────────────
    @mcp.tool()
    async def search_knowledge(
        query: str,
        top_k: int = 10,
        provider: Optional[str] = None,
        drive_id: Optional[str] = None,
        modality: Optional[str] = None,
    ) -> dict:
        """Search the Byggkon knowledge base.

        Args:
            query: natural-language query.
            top_k: how many results to return (default 10, max 50).
            provider: 'openai' or 'gemini' — which embedding index to search.
                Default is the one configured as MCP_DEFAULT_PROVIDER.
            drive_id: optional Pinecone namespace filter (typically a
                user's OneDrive id). Limits search to that single drive.
            modality: 'text' or 'image' — restrict to one or the other.
        """
        s: Settings = app_state.settings
        embedders = getattr(app_state, "embedders", {}) or {}
        pc = app_state.pinecone
        if pc is None:
            return {"error": "Pinecone not configured"}

        prov = provider or s.mcp_default_provider
        embedder = embedders.get(prov)
        if embedder is None:
            return {"error": f"Embedder for provider {prov!r} not configured"}

        top_k = max(1, min(int(top_k or 10), 50))

        # 1. Embed the query in the same space as the index.
        try:
            qvec = (await embedder.embed_texts([query]))[0]
        except Exception as e:  # noqa: BLE001
            return {"error": f"embedding failed: {e}"}

        # 2. Query Pinecone (need direct index handle — use _SingleIndex).
        idx_obj = pc._indexes.get(prov)  # noqa: SLF001 — internal access
        if idx_obj is None:
            return {"error": f"No Pinecone index for provider {prov!r}"}

        ns = drive_id or s.pinecone_namespace or ""
        flt: dict[str, Any] = {}
        if modality in ("text", "image"):
            flt["modality"] = modality

        def _query():
            kwargs = dict(
                vector=qvec,
                top_k=top_k,
                include_metadata=True,
            )
            if ns:
                kwargs["namespace"] = ns
            if flt:
                kwargs["filter"] = flt
            return idx_obj._index.query(**kwargs)  # noqa: SLF001

        try:
            res = await asyncio.to_thread(_query)
        except Exception as e:  # noqa: BLE001
            return {"error": f"pinecone query failed: {e}"}

        matches = getattr(res, "matches", None) or (res.get("matches") if isinstance(res, dict) else [])
        hits = []
        for m in matches:
            d = _hit_to_dict(m)
            md = d.get("metadata", {})
            hits.append({
                "score": d.get("score"),
                "file_name": md.get("file_name"),
                "file_path": md.get("file_path"),
                "web_url": md.get("web_url"),
                "drive_owner": md.get("drive_owner"),
                "last_modified": md.get("last_modified"),
                "modality": md.get("modality", "text"),
                "page_number": md.get("page_number"),
                "text": md.get("text"),
                "vector_id": d.get("id"),
            })
        return {"provider": prov, "namespace": ns, "hits": hits}

    # ─── get_file_chunks ──────────────────────────────────────────────
    @mcp.tool()
    async def get_file_chunks(
        file_id: str,
        drive_id: Optional[str] = None,
        provider: Optional[str] = None,
    ) -> dict:
        """Return every chunk (text + image) for one specific file."""
        s: Settings = app_state.settings
        pc = app_state.pinecone
        if pc is None:
            return {"error": "Pinecone not configured"}
        prov = provider or s.mcp_default_provider
        idx_obj = pc._indexes.get(prov)  # noqa: SLF001
        if idx_obj is None:
            return {"error": f"No Pinecone index for provider {prov!r}"}

        ns = drive_id or s.pinecone_namespace or ""

        def _query():
            kwargs = dict(
                vector=[0.0] * (idx_obj._index.describe_index_stats().get("dimension", 3072)),  # noqa: SLF001
                top_k=200,
                include_metadata=True,
                filter={"file_id": file_id},
            )
            if ns:
                kwargs["namespace"] = ns
            return idx_obj._index.query(**kwargs)  # noqa: SLF001

        try:
            res = await asyncio.to_thread(_query)
        except Exception as e:  # noqa: BLE001
            return {"error": f"pinecone query failed: {e}"}

        matches = getattr(res, "matches", None) or (res.get("matches") if isinstance(res, dict) else [])
        chunks = []
        for m in matches:
            d = _hit_to_dict(m)
            md = d.get("metadata", {})
            chunks.append({
                "chunk_index": md.get("chunk_index"),
                "modality": md.get("modality", "text"),
                "page_number": md.get("page_number"),
                "text": md.get("text"),
                "vector_id": d.get("id"),
            })
        chunks.sort(key=lambda c: (c["modality"] != "text", c.get("chunk_index") or 0))
        return {"file_id": file_id, "provider": prov, "chunks": chunks}

    # ─── list_indexed_drives ──────────────────────────────────────────
    @mcp.tool()
    async def list_indexed_drives() -> dict:
        """Return drives that have been indexed at least once."""
        rows = app_state.state.progress_snapshot()
        drives = [
            {
                "drive_id": r.get("drive_id"),
                "drive_label": r.get("drive_label"),
                "files_processed": r.get("files_processed"),
                "phase": r.get("phase"),
            }
            for r in rows
        ]
        return {"drives": drives, "count": len(drives)}

    return mcp


# ─── Bearer-token middleware ─────────────────────────────────────────
class MCPBearerAuth:
    """ASGI middleware enforcing a static bearer token on the /mcp app.

    Wrap mcp.streamable_http_app() with this before mounting on FastAPI.
    """

    def __init__(self, app, token_provider):
        self._app = app
        self._token_provider = token_provider

    async def __call__(self, scope, receive, send):
        if scope.get("type") not in ("http", "websocket"):
            await self._app(scope, receive, send)
            return

        token = self._token_provider()
        if not token:
            # MCP disabled or unconfigured — refuse politely.
            await _send_status(send, 503, b"MCP not configured")
            return

        headers = dict(scope.get("headers") or [])
        auth = headers.get(b"authorization", b"").decode()
        if not auth.lower().startswith("bearer "):
            await _send_status(send, 401, b"Missing bearer token")
            return
        provided = auth.split(" ", 1)[1].strip()
        # Constant-time compare
        import hmac
        if not hmac.compare_digest(provided.encode(), token.encode()):
            await _send_status(send, 403, b"Invalid bearer token")
            return

        await self._app(scope, receive, send)


async def _send_status(send, status: int, body: bytes) -> None:
    await send({
        "type": "http.response.start",
        "status": status,
        "headers": [(b"content-type", b"text/plain; charset=utf-8")],
    })
    await send({"type": "http.response.body", "body": body})
