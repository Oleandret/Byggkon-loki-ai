"""RAG chat — embed query → retrieve from Pinecone → call LLM with context.

Two providers, OpenAI and Gemini, each with their own embedder and Pinecone
index. The user picks one (or talks to both side by side). Returns the
assistant's answer plus a list of citations the user can click to open the
underlying file in OneDrive.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

from .config import Settings
from .embeddings import BaseEmbedder
from .logging_config import get_logger
from .pinecone_store import PineconeStore

log = get_logger(__name__)


@dataclass
class Citation:
    n: int  # 1-based index used in [1] markers
    file_name: str
    file_path: str
    web_url: str
    drive_owner: str
    page_number: int | None
    score: float
    snippet: str


@dataclass
class ChatResult:
    provider: str
    answer: str
    citations: list[Citation] = field(default_factory=list)
    error: str | None = None


# ─── Retrieval ────────────────────────────────────────────────────────
async def retrieve_context(
    settings: Settings,
    pinecone: PineconeStore,
    embedder: BaseEmbedder,
    provider: str,
    query: str,
    top_k: int | None = None,
) -> list[Citation]:
    """Embed the query and run a vector search on the provider's index.

    Returns Citation objects ready to be inserted into the context block.
    """
    k = max(1, min(top_k or settings.chat_top_k, 50))
    qvec = await embedder.embed_query(query)

    idx = pinecone._indexes.get(provider)  # noqa: SLF001
    if idx is None:
        return []

    # Run query in a thread (Pinecone SDK is sync).
    def _query():
        return idx._index.query(  # noqa: SLF001
            vector=qvec,
            top_k=k,
            include_metadata=True,
        )

    res = await asyncio.to_thread(_query)
    matches = (
        getattr(res, "matches", None)
        or (res.get("matches") if isinstance(res, dict) else [])
        or []
    )

    citations: list[Citation] = []
    for i, m in enumerate(matches):
        md = getattr(m, "metadata", None) or (m.get("metadata") if isinstance(m, dict) else {}) or {}
        score = getattr(m, "score", None) or (m.get("score") if isinstance(m, dict) else 0.0) or 0.0
        text = md.get("text") or ""
        citations.append(
            Citation(
                n=i + 1,
                file_name=str(md.get("file_name") or "ukjent"),
                file_path=str(md.get("file_path") or ""),
                web_url=str(md.get("web_url") or ""),
                drive_owner=str(md.get("drive_owner") or ""),
                page_number=md.get("page_number"),
                score=float(score),
                snippet=text[:600],
            )
        )
    return citations


def build_context_block(citations: list[Citation], max_chars: int) -> str:
    """Format retrieved chunks into a numbered context block for the LLM."""
    if not citations:
        return "(ingen relevante dokumenter funnet)"
    parts: list[str] = []
    used = 0
    for c in citations:
        header = (
            f"[{c.n}] {c.file_name}"
            + (f" (s. {c.page_number})" if c.page_number else "")
            + f" — {c.drive_owner}"
        )
        body = c.snippet.strip()
        block = f"{header}\n{body}\n"
        if used + len(block) > max_chars:
            break
        parts.append(block)
        used += len(block)
    return "\n".join(parts)


def render_system_prompt(settings: Settings) -> str:
    return settings.chat_system_prompt.replace("{brand_owner}", settings.brand_owner)


def build_user_message(query: str, context_block: str) -> str:
    return (
        f"Spørsmål fra brukeren:\n{query}\n\n"
        f"Kontekst fra interne dokumenter (sitér med [n]):\n{context_block}"
    )


# ─── OpenAI chat ──────────────────────────────────────────────────────
async def call_openai_chat(
    settings: Settings,
    history: list[dict],
    context_block: str,
    user_query: str,
) -> str:
    from openai import AsyncOpenAI

    client = AsyncOpenAI(api_key=settings.openai_api_key)
    messages: list[dict] = [
        {"role": "system", "content": render_system_prompt(settings)},
    ]
    # Include prior turns (already user/assistant alternating).
    for msg in history[-10:]:  # cap history depth
        if msg.get("role") in ("user", "assistant") and msg.get("content"):
            messages.append({"role": msg["role"], "content": msg["content"]})
    messages.append({"role": "user", "content": build_user_message(user_query, context_block)})

    resp = await client.chat.completions.create(
        model=settings.openai_chat_model,
        messages=messages,
        temperature=0.3,
    )
    return resp.choices[0].message.content or ""


# ─── Gemini chat ──────────────────────────────────────────────────────
async def call_gemini_chat(
    settings: Settings,
    history: list[dict],
    context_block: str,
    user_query: str,
) -> str:
    from google import genai
    from google.genai import types as genai_types

    client = genai.Client(api_key=settings.gemini_api_key)

    # Convert OpenAI-style history to Gemini's Content list.
    contents = []
    for msg in history[-10:]:
        role = msg.get("role")
        text = msg.get("content")
        if not text or role not in ("user", "assistant"):
            continue
        contents.append(
            genai_types.Content(
                role="user" if role == "user" else "model",
                parts=[genai_types.Part.from_text(text=text)],
            )
        )
    contents.append(
        genai_types.Content(
            role="user",
            parts=[genai_types.Part.from_text(
                text=build_user_message(user_query, context_block)
            )],
        )
    )

    resp = await asyncio.to_thread(
        client.models.generate_content,
        model=settings.gemini_chat_model,
        contents=contents,
        config=genai_types.GenerateContentConfig(
            system_instruction=render_system_prompt(settings),
            temperature=0.3,
        ),
    )
    # google-genai returns .text on the response.
    return getattr(resp, "text", "") or ""


# ─── Main entry ───────────────────────────────────────────────────────
async def answer(
    *,
    provider: str,
    query: str,
    history: list[dict],
    settings: Settings,
    embedders: dict[str, BaseEmbedder],
    pinecone: PineconeStore,
) -> ChatResult:
    embedder = embedders.get(provider)
    if embedder is None:
        return ChatResult(
            provider=provider,
            answer="",
            error=f"Provider '{provider}' er ikke konfigurert. Sjekk at den er valgt i Innstillinger og at API-nøkkelen er satt.",
        )
    if pinecone is None:
        return ChatResult(provider=provider, answer="", error="Pinecone er ikke konfigurert.")
    if pinecone._indexes.get(provider) is None:  # noqa: SLF001
        return ChatResult(
            provider=provider, answer="",
            error=f"Pinecone-indeksen for '{provider}' er ikke opprettet eller ikke nådd.",
        )

    try:
        citations = await retrieve_context(
            settings, pinecone, embedder, provider, query
        )
    except Exception as e:  # noqa: BLE001
        log.warning("chat.retrieve.error", provider=provider, err=str(e))
        return ChatResult(provider=provider, answer="", error=f"Søk feilet: {e}")

    ctx_block = build_context_block(citations, settings.chat_max_context_chars)

    try:
        if provider == "openai":
            text = await call_openai_chat(settings, history, ctx_block, query)
        elif provider == "gemini":
            text = await call_gemini_chat(settings, history, ctx_block, query)
        else:
            return ChatResult(provider=provider, answer="", error=f"Ukjent provider: {provider}")
    except Exception as e:  # noqa: BLE001
        log.warning("chat.llm.error", provider=provider, err=str(e))
        return ChatResult(
            provider=provider, answer="", citations=citations,
            error=f"LLM-kall feilet: {e}",
        )

    return ChatResult(provider=provider, answer=text, citations=citations)
