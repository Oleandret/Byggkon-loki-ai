"""OpenAI embeddings, batched, with retries.

text-embedding-3-large defaults to 3072 dims, but supports `dimensions=`
to truncate. We pass it explicitly so the Pinecone index dimension always
matches.
"""
from __future__ import annotations

import asyncio
from typing import Iterable

from openai import AsyncOpenAI
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from .config import Settings
from .logging_config import get_logger

log = get_logger(__name__)


# OpenAI's hard limit per request is 8192 tokens per input and ~300k tokens
# per request total. Our chunks are ~1500 chars (~400 tokens) so a batch of
# 64 is comfortably safe.


class Embedder:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client = AsyncOpenAI(api_key=settings.openai_api_key)

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed a list of strings, returning vectors in the same order."""
        if not texts:
            return []

        out: list[list[float]] = []
        batch_size = self._settings.embedding_batch_size
        for batch in _batched(texts, batch_size):
            vectors = await self._embed_batch(list(batch))
            out.extend(vectors)
        return out

    async def _embed_batch(self, batch: list[str]) -> list[list[float]]:
        async for retry in AsyncRetrying(
            stop=stop_after_attempt(5),
            wait=wait_exponential(multiplier=1, min=1, max=20),
            retry=retry_if_exception_type(Exception),
            reraise=True,
        ):
            with retry:
                resp = await self._client.embeddings.create(
                    model=self._settings.openai_embedding_model,
                    input=batch,
                    dimensions=self._settings.openai_embedding_dimensions,
                )
                return [d.embedding for d in resp.data]
        # Unreachable.
        raise RuntimeError("embed retry loop exhausted")  # pragma: no cover


def _batched(items: list[str], n: int) -> Iterable[list[str]]:
    for i in range(0, len(items), n):
        yield items[i : i + n]
