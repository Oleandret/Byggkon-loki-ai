"""Thin wrapper around Pinecone's serverless SDK.

We default to one namespace per drive (so deletes can be scoped cheaply
without filter-based delete, which isn't supported on serverless). The
caller can override with PINECONE_NAMESPACE for a single shared namespace.
"""
from __future__ import annotations

import asyncio
from typing import Iterable, Optional

from pinecone import Pinecone, ServerlessSpec

from .config import Settings
from .logging_config import get_logger

log = get_logger(__name__)


class PineconeStore:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._pc = Pinecone(api_key=settings.pinecone_api_key)
        self._index = self._pc.Index(settings.pinecone_index)

    def namespace_for_drive(self, drive_id: str) -> str:
        if self._settings.pinecone_namespace:
            return self._settings.pinecone_namespace
        # Pinecone namespaces must be ≤ 64 chars and URL-safe-ish.
        # Drive IDs are long opaque strings; truncate-from-tail for human-ish
        # readability, but keep the full id elsewhere via metadata.
        safe = drive_id.replace("!", "_").replace("/", "_")
        return safe[:64]

    async def upsert(
        self,
        namespace: str,
        items: list[tuple[str, list[float], dict]],
    ) -> None:
        """Upsert a list of (id, vector, metadata). Batched by 100."""
        if not items:
            return
        # Pinecone serverless has a 4MB request cap; 100 vectors at 3072 dims
        # in float32 is well under that.
        for batch in _batched(items, 100):
            payload = [
                {"id": vid, "values": vec, "metadata": md} for vid, vec, md in batch
            ]
            await asyncio.to_thread(
                self._index.upsert, vectors=payload, namespace=namespace
            )
        log.info("pinecone.upsert.done", namespace=namespace, count=len(items))

    async def delete_ids(self, namespace: str, ids: list[str]) -> None:
        if not ids:
            return
        # Pinecone's delete-by-id accepts up to 1000 per call.
        for batch in _batched(ids, 1000):
            await asyncio.to_thread(
                self._index.delete, ids=list(batch), namespace=namespace
            )
        log.info("pinecone.delete.done", namespace=namespace, count=len(ids))

    async def index_stats(self) -> dict:
        return await asyncio.to_thread(self._index.describe_index_stats)


def ensure_index(settings: Settings, *, cloud: str = "aws", region: str = "us-east-1") -> dict:
    """Create the index if it doesn't exist. Used by bootstrap_pinecone.py.

    Returns the index description.
    """
    pc = Pinecone(api_key=settings.pinecone_api_key)
    existing = {idx["name"] for idx in pc.list_indexes()}
    if settings.pinecone_index not in existing:
        log.info(
            "pinecone.index.create",
            name=settings.pinecone_index,
            dim=settings.openai_embedding_dimensions,
        )
        pc.create_index(
            name=settings.pinecone_index,
            dimension=settings.openai_embedding_dimensions,
            metric="cosine",
            spec=ServerlessSpec(cloud=cloud, region=region),
        )
    return pc.describe_index(settings.pinecone_index).to_dict()


def _batched(items: Iterable, n: int) -> Iterable[list]:
    buf: list = []
    for x in items:
        buf.append(x)
        if len(buf) >= n:
            yield buf
            buf = []
    if buf:
        yield buf
