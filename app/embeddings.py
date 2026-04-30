"""Embedding providers — OpenAI (text-only) and Gemini Embedding 2 (multimodal).

Each provider implements the ``BaseEmbedder`` interface:
    * ``embed_texts(texts)``     → list[list[float]]
    * ``embed_image(bytes, mime)`` → list[float]   (raises if not multimodal)
    * ``capabilities`` → set of {"text", "image"}

The orchestrator picks one or both based on Settings.embedding_provider.
"""
from __future__ import annotations

import asyncio
import base64
from abc import ABC, abstractmethod
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


# ─── Base class ──────────────────────────────────────────────────────
class BaseEmbedder(ABC):
    name: str = "base"
    capabilities: set[str] = set()
    dimensions: int = 0

    @abstractmethod
    async def embed_texts(self, texts: list[str]) -> list[list[float]]: ...

    async def embed_query(self, text: str) -> list[float]:
        """Embed a search query. Default: same as document; Gemini overrides
        this to use task_type=RETRIEVAL_QUERY which yields markedly better
        retrieval quality."""
        return (await self.embed_texts([text]))[0]

    async def embed_image(self, data: bytes, mime: str) -> list[float]:
        raise NotImplementedError(f"{self.name} does not support image embedding")


def _batched(items: list, n: int) -> Iterable[list]:
    for i in range(0, len(items), n):
        yield items[i : i + n]


# ─── OpenAI embedder ─────────────────────────────────────────────────
class OpenAIEmbedder(BaseEmbedder):
    name = "openai"
    capabilities = {"text"}

    def __init__(self, settings: Settings) -> None:
        if not settings.openai_api_key:
            raise ValueError("OpenAI API key is empty")
        self._settings = settings
        self._client = AsyncOpenAI(api_key=settings.openai_api_key)
        self.dimensions = settings.openai_embedding_dimensions

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        out: list[list[float]] = []
        for batch in _batched(texts, self._settings.embedding_batch_size):
            out.extend(await self._embed_batch(batch))
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
        raise RuntimeError("openai embed retry loop exhausted")  # pragma: no cover


# ─── Gemini embedder (multimodal) ────────────────────────────────────
class GeminiEmbedder(BaseEmbedder):
    """Google Gemini Embedding 2 — text + images in same vector space.

    Uses the Google AI Studio API key path via the `google-genai` SDK.
    For Vertex AI service-account auth, swap the client construction.
    """

    name = "gemini"
    capabilities = {"text", "image"}

    def __init__(self, settings: Settings) -> None:
        if not settings.gemini_api_key:
            raise ValueError("Gemini API key is empty")
        # Local import keeps cold-start cheap when only OpenAI is used.
        from google import genai
        from google.genai import types as genai_types

        self._settings = settings
        self._client = genai.Client(api_key=settings.gemini_api_key)
        self._types = genai_types
        self.dimensions = settings.gemini_embedding_dimensions
        self._batch_size = max(1, min(settings.embedding_batch_size, 100))

    # ─── Text ─────────────────────────────────────────────────────────
    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        out: list[list[float]] = []
        for batch in _batched(texts, self._batch_size):
            out.extend(await self._embed_text_batch(batch))
        return out

    def _embed_config(self, *, for_query: bool = False):
        """Build EmbedContentConfig with sensible task_type and dimensionality."""
        cfg = {
            "output_dimensionality": self._settings.gemini_embedding_dimensions,
            "task_type": "RETRIEVAL_QUERY" if for_query else "RETRIEVAL_DOCUMENT",
        }
        return self._types.EmbedContentConfig(**cfg)

    async def _embed_text_batch(self, batch: list[str]) -> list[list[float]]:
        async for retry in AsyncRetrying(
            stop=stop_after_attempt(5),
            wait=wait_exponential(multiplier=1, min=1, max=20),
            retry=retry_if_exception_type(Exception),
            reraise=True,
        ):
            with retry:
                # google-genai SDK is sync; offload to a thread.
                resp = await asyncio.to_thread(
                    self._client.models.embed_content,
                    model=self._settings.gemini_embedding_model,
                    contents=batch,
                    config=self._embed_config(),
                )
                return [list(e.values) for e in resp.embeddings]
        raise RuntimeError("gemini embed retry loop exhausted")  # pragma: no cover

    async def embed_query(self, text: str) -> list[float]:
        """Use RETRIEVAL_QUERY task_type for queries — the matching pair to
        RETRIEVAL_DOCUMENT used during indexing. Gemini's embedding quality
        on retrieval is significantly better when the pair is correct."""
        async for retry in AsyncRetrying(
            stop=stop_after_attempt(4),
            wait=wait_exponential(multiplier=1, min=1, max=20),
            retry=retry_if_exception_type(Exception),
            reraise=True,
        ):
            with retry:
                resp = await asyncio.to_thread(
                    self._client.models.embed_content,
                    model=self._settings.gemini_embedding_model,
                    contents=[text],
                    config=self._embed_config(for_query=True),
                )
                return list(resp.embeddings[0].values)
        raise RuntimeError("gemini query-embed retry loop exhausted")  # pragma: no cover

    # ─── Image ────────────────────────────────────────────────────────
    async def embed_image(self, data: bytes, mime: str) -> list[float]:
        """Embed an image binary into the same vector space as text.

        Per the Gemini Embedding 2 docs the simplest pattern is to pass a
        Part.from_bytes(...) (or a list containing it) as `contents`.
        """
        async for retry in AsyncRetrying(
            stop=stop_after_attempt(4),
            wait=wait_exponential(multiplier=1, min=1, max=20),
            retry=retry_if_exception_type(Exception),
            reraise=True,
        ):
            with retry:
                part = self._types.Part.from_bytes(data=data, mime_type=mime)
                resp = await asyncio.to_thread(
                    self._client.models.embed_content,
                    model=self._settings.gemini_embedding_model,
                    contents=[part],
                    config=self._embed_config(),
                )
                emb = resp.embeddings[0]
                return list(emb.values)
        raise RuntimeError("gemini image-embed retry loop exhausted")  # pragma: no cover


# ─── Factory ─────────────────────────────────────────────────────────
def build_embedders(
    settings: Settings, *, errors_out: dict[str, str] | None = None
) -> dict[str, BaseEmbedder]:
    """Return a {provider_name: embedder} dict for the configured providers.

    Each provider is built independently — one failing provider does NOT
    prevent the others from being constructed. Failures are captured in
    `errors_out` (if supplied) so the admin UI can surface them.
    """
    out: dict[str, BaseEmbedder] = {}
    for name in settings.providers():
        try:
            if name == "openai":
                out["openai"] = OpenAIEmbedder(settings)
            elif name == "gemini":
                out["gemini"] = GeminiEmbedder(settings)
        except Exception as e:  # noqa: BLE001
            err = f"{type(e).__name__}: {e}"
            log.warning("embedder.init.skip", provider=name, err=err)
            if errors_out is not None:
                errors_out[name] = err
    return out


# ─── Back-compat alias ───────────────────────────────────────────────
# Older code in main.py and admin_routes.py imports `Embedder`; keep it
# as a thin wrapper that picks the first/primary provider.
class Embedder:
    """Legacy single-provider façade. Picks the first configured provider."""

    def __init__(self, settings: Settings) -> None:
        embedders = build_embedders(settings)
        if not embedders:
            raise ValueError("No embedding provider is configured")
        # Preserve the user's order: openai before gemini in 'both'.
        for k in ("openai", "gemini"):
            if k in embedders:
                self._impl = embedders[k]
                break

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return await self._impl.embed_texts(texts)
