"""Pinecone store with multi-provider (multi-index) support.

For each embedding provider we configure (OpenAI, Gemini), there's a
separate Pinecone index — possibly with different dimensions. Operations
are routed to the right index by provider name.

Default namespace strategy: one namespace per drive_id, so deletes can be
scoped cheaply on serverless (which doesn't support filter-based delete).
PINECONE_NAMESPACE overrides this with a single shared namespace.
"""
from __future__ import annotations

import asyncio
from typing import Iterable, Optional

from pinecone import Pinecone, ServerlessSpec

from .config import Settings
from .logging_config import get_logger

log = get_logger(__name__)


class _SingleIndex:
    """Wraps a single Pinecone index handle for one provider."""

    def __init__(self, pc: Pinecone, name: str, default_namespace: str) -> None:
        self._name = name
        self._index = pc.Index(name)
        self._default_namespace = default_namespace

    def namespace_for_drive(self, drive_id: str) -> str:
        if self._default_namespace:
            return self._default_namespace
        safe = drive_id.replace("!", "_").replace("/", "_")
        return safe[:64]

    async def upsert(
        self,
        namespace: str,
        items: list[tuple[str, list[float], dict]],
    ) -> None:
        if not items:
            return
        for batch in _batched(items, 100):
            payload = [
                {"id": vid, "values": vec, "metadata": md} for vid, vec, md in batch
            ]
            await asyncio.to_thread(self._index.upsert, vectors=payload, namespace=namespace)
        log.info("pinecone.upsert.done", index=self._name, namespace=namespace, count=len(items))

    async def delete_ids(self, namespace: str, ids: list[str]) -> None:
        if not ids:
            return
        for batch in _batched(ids, 1000):
            await asyncio.to_thread(self._index.delete, ids=list(batch), namespace=namespace)
        log.info("pinecone.delete.done", index=self._name, namespace=namespace, count=len(ids))

    async def index_stats(self) -> dict:
        stats = await asyncio.to_thread(self._index.describe_index_stats)
        if isinstance(stats, dict):
            return stats
        try:
            return stats.to_dict()
        except AttributeError:
            return dict(stats)


class PineconeStore:
    """Container for one or more provider indexes.

    Behaviour mirrors the old single-index API for back-compat:
      * ``namespace_for_drive(drive_id)`` returns a namespace usable on any of
        the underlying indexes (we use the same namespace per drive across
        providers, so cross-referencing stays simple).
      * ``upsert(namespace, items)`` writes to the *primary* index. For
        provider-specific writes use ``upsert_for(provider, ...)``.
    """

    def __init__(self, settings: Settings) -> None:
        if not settings.pinecone_api_key:
            raise ValueError("Pinecone API key is empty")
        self._settings = settings
        self._pc = Pinecone(api_key=settings.pinecone_api_key)
        self._indexes: dict[str, _SingleIndex] = {}

        ns = settings.pinecone_namespace

        for prov in settings.providers():
            name = (
                settings.resolved_openai_index() if prov == "openai"
                else settings.resolved_gemini_index() if prov == "gemini"
                else ""
            )
            if not name:
                log.warning(
                    "pinecone.index.skip",
                    provider=prov,
                    hint=f"Set PINECONE_INDEX_{prov.upper()} to enable.",
                )
                continue
            self._indexes[prov] = _SingleIndex(self._pc, name, ns)

    @property
    def providers(self) -> list[str]:
        return list(self._indexes.keys())

    @property
    def primary(self) -> Optional[_SingleIndex]:
        # Prefer OpenAI as primary if configured (back-compat); else first.
        if "openai" in self._indexes:
            return self._indexes["openai"]
        return next(iter(self._indexes.values()), None)

    def namespace_for_drive(self, drive_id: str) -> str:
        idx = self.primary
        if idx is None:
            # No live index — return a sensible namespace so the orchestrator
            # can still record bookkeeping. Actual writes will be skipped.
            return drive_id.replace("!", "_").replace("/", "_")[:64]
        return idx.namespace_for_drive(drive_id)

    # ─── Per-provider operations ──────────────────────────────────────
    async def upsert_for(
        self,
        provider: str,
        namespace: str,
        items: list[tuple[str, list[float], dict]],
    ) -> None:
        idx = self._indexes.get(provider)
        if idx is None:
            log.warning("pinecone.upsert.no_index", provider=provider)
            return
        await idx.upsert(namespace, items)

    async def delete_ids_for(self, provider: str, namespace: str, ids: list[str]) -> None:
        idx = self._indexes.get(provider)
        if idx is None:
            return
        await idx.delete_ids(namespace, ids)

    async def index_stats_for(self, provider: str) -> dict:
        idx = self._indexes.get(provider)
        if idx is None:
            return {"error": f"Index not configured for provider {provider!r}"}
        return await idx.index_stats()

    # ─── Aggregate stats (for the dashboard) ─────────────────────────
    async def index_stats(self) -> dict:
        out: dict = {}
        total = 0
        for name, idx in self._indexes.items():
            try:
                s = await idx.index_stats()
                out[name] = s
                total += int(s.get("total_vector_count") or s.get("totalVectorCount") or 0)
            except Exception as e:  # noqa: BLE001
                out[name] = {"error": str(e)}
        out["total_vector_count"] = total
        return out

    # ─── Back-compat: legacy single-index API ────────────────────────
    async def upsert(
        self, namespace: str, items: list[tuple[str, list[float], dict]]
    ) -> None:
        idx = self.primary
        if idx is None:
            log.warning("pinecone.upsert.no_primary")
            return
        await idx.upsert(namespace, items)

    async def delete_ids(self, namespace: str, ids: list[str]) -> None:
        idx = self.primary
        if idx is None:
            return
        await idx.delete_ids(namespace, ids)


def ensure_indexes(
    settings: Settings,
    *,
    cloud: str = "aws",
    region: str = "us-east-1",
) -> dict:
    """Create any configured per-provider indexes that don't yet exist.

    Returns a {provider: description-dict} mapping.
    """
    pc = Pinecone(api_key=settings.pinecone_api_key)
    existing = {idx["name"] for idx in pc.list_indexes()}
    out: dict = {}

    targets = []
    if "openai" in settings.providers():
        targets.append((
            "openai",
            settings.resolved_openai_index(),
            settings.openai_embedding_dimensions,
        ))
    if "gemini" in settings.providers():
        targets.append((
            "gemini",
            settings.resolved_gemini_index(),
            settings.gemini_embedding_dimensions,
        ))

    for prov, name, dim in targets:
        if not name:
            log.warning("pinecone.bootstrap.skip", provider=prov, reason="no index name set")
            continue
        if name not in existing:
            log.info("pinecone.bootstrap.create", provider=prov, name=name, dim=dim)
            pc.create_index(
                name=name,
                dimension=dim,
                metric="cosine",
                spec=ServerlessSpec(cloud=cloud, region=region),
            )
        out[prov] = pc.describe_index(name).to_dict()
    return out


# ─── Back-compat alias for the old bootstrap script ──────────────────
def ensure_index(settings: Settings, *, cloud: str = "aws", region: str = "us-east-1") -> dict:
    return ensure_indexes(settings, cloud=cloud, region=region)


def _batched(items: Iterable, n: int) -> Iterable[list]:
    buf: list = []
    for x in items:
        buf.append(x)
        if len(buf) >= n:
            yield buf
            buf = []
    if buf:
        yield buf
