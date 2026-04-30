"""Per-file pipeline: download → unstructured → embed → upsert.

Supports multiple embedding providers writing to per-provider Pinecone
indexes in parallel. Image blobs (when extracted) are embedded directly
by multimodal providers (Gemini); text-only providers (OpenAI) get only
text chunks.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
import tempfile
from dataclasses import dataclass
from typing import Optional

from .config import Settings
from .drive_discovery import DriveRef
from .embeddings import BaseEmbedder
from .graph_client import GraphClient
from .logging_config import get_logger
from .pinecone_store import PineconeStore
from .state import FileVectorRecord, StateStore
from .unstructured_proc import is_supported_extension, partition_and_chunk

log = get_logger(__name__)


@dataclass
class ProcessResult:
    file_id: str
    indexed: bool
    chunk_count: int = 0
    image_count: int = 0
    skipped_reason: Optional[str] = None
    error: Optional[str] = None


class Processor:
    def __init__(
        self,
        settings: Settings,
        graph: GraphClient,
        embedders: dict[str, BaseEmbedder],
        pinecone: PineconeStore,
        state: StateStore,
    ) -> None:
        self._settings = settings
        self._graph = graph
        self._embedders = embedders  # provider -> BaseEmbedder
        self._pinecone = pinecone
        self._state = state
        os.makedirs(settings.tmp_dir, exist_ok=True)

    @property
    def has_multimodal(self) -> bool:
        return any("image" in e.capabilities for e in self._embedders.values())

    async def process_file(self, drive: DriveRef, item: dict) -> ProcessResult:
        file_id = item["id"]
        file_name = item.get("name", "<unknown>")
        size = int(item.get("size") or 0)
        last_modified = item.get("lastModifiedDateTime", "")
        web_url = item.get("webUrl")

        if "folder" in item:
            return ProcessResult(file_id=file_id, indexed=False, skipped_reason="folder")
        if not is_supported_extension(file_name):
            return ProcessResult(file_id=file_id, indexed=False, skipped_reason="unsupported_ext")
        if size > self._settings.max_file_bytes:
            log.info("process.skip.too_large", file=file_name, size=size)
            return ProcessResult(file_id=file_id, indexed=False, skipped_reason="too_large")

        existing = self._state.get_file_record(drive.drive_id, file_id)
        if existing and existing.last_modified == last_modified and existing.last_modified:
            return ProcessResult(file_id=file_id, indexed=False, skipped_reason="unchanged")

        namespace = self._pinecone.namespace_for_drive(drive.drive_id)

        suffix = os.path.splitext(file_name)[1] or ""
        local_dir = tempfile.mkdtemp(dir=self._settings.tmp_dir, prefix="item_")
        local_path = os.path.join(local_dir, f"file{suffix}")
        try:
            content_hash = await self._download(drive.drive_id, file_id, local_path)

            if existing and existing.content_hash == content_hash:
                self._state.upsert_file_record(
                    FileVectorRecord(
                        file_id=file_id,
                        drive_id=drive.drive_id,
                        namespace=namespace,
                        content_hash=content_hash,
                        vector_ids=existing.vector_ids,
                        last_modified=last_modified,
                    )
                )
                return ProcessResult(
                    file_id=file_id, indexed=False, skipped_reason="content_unchanged"
                )

            extract_images = self.has_multimodal and self._settings.gemini_embed_images
            parsed = await asyncio.to_thread(
                partition_and_chunk, local_path, self._settings, extract_images=extract_images
            )

            if not parsed.chunks and not parsed.images:
                self._state.upsert_file_record(
                    FileVectorRecord(
                        file_id=file_id,
                        drive_id=drive.drive_id,
                        namespace=namespace,
                        content_hash=content_hash,
                        vector_ids=[],
                        last_modified=last_modified,
                    )
                )
                return ProcessResult(
                    file_id=file_id, indexed=False, skipped_reason="empty_after_parse"
                )

            base_meta = {
                "drive_id": drive.drive_id,
                "drive_type": drive.drive_type,
                "drive_owner": drive.owner_label,
                "file_id": file_id,
                "file_name": file_name,
                "file_path": _path_str(item),
                "web_url": web_url or "",
                "last_modified": last_modified,
                "mime_type": _mime(item),
            }

            # Delete prior vectors across whatever providers we've written to,
            # before upserting fresh ones.
            if existing and existing.vector_ids:
                # vector_ids stored as list of "provider::vid" strings (new schema)
                # or plain ids (legacy single-provider schema). Handle both.
                by_provider: dict[str, list[str]] = {}
                for raw in existing.vector_ids:
                    if "::" in raw and raw.split("::", 1)[0] in {"openai", "gemini"}:
                        prov, vid = raw.split("::", 1)
                        by_provider.setdefault(prov, []).append(vid)
                    else:
                        # legacy: treat as openai
                        by_provider.setdefault("openai", []).append(raw)
                for prov, ids in by_provider.items():
                    await self._pinecone.delete_ids_for(prov, namespace, ids)

            new_ids: list[str] = []
            text_count = 0
            image_count = 0

            # ─── Per provider: embed and upsert ───────────────────────
            for prov, embedder in self._embedders.items():
                # Texts
                if parsed.chunks and "text" in embedder.capabilities:
                    texts = [c.text for c in parsed.chunks]
                    text_vecs = await embedder.embed_texts(texts)
                    payload: list[tuple[str, list[float], dict]] = []
                    for i, (chunk, vec) in enumerate(zip(parsed.chunks, text_vecs)):
                        # Deterministic ID: re-uploading the same file/chunk
                        # produces the same id, so Pinecone upsert overwrites
                        # cleanly even if SQLite state is lost.
                        vid = f"text::{file_id}::{i}"
                        meta = {
                            **base_meta,
                            **chunk.metadata,
                            "modality": "text",
                            "chunk_index": i,
                            "text": chunk.text,
                            "embedding_provider": prov,
                        }
                        payload.append((vid, vec, meta))
                        new_ids.append(f"{prov}::{vid}")
                    await self._pinecone.upsert_for(prov, namespace, payload)
                    text_count = len(parsed.chunks)

                # Images (only multimodal providers)
                if parsed.images and "image" in embedder.capabilities:
                    image_payload: list[tuple[str, list[float], dict]] = []
                    for j, blob in enumerate(parsed.images):
                        try:
                            vec = await embedder.embed_image(blob.data, blob.mime)
                        except Exception as e:  # noqa: BLE001
                            log.warning("process.image_embed.error", file=file_name, err=str(e))
                            continue
                        vid = f"image::{file_id}::{j}"
                        meta = {
                            **base_meta,
                            **blob.metadata,
                            "modality": "image",
                            "image_index": j,
                            "image_mime": blob.mime,
                            "embedding_provider": prov,
                        }
                        image_payload.append((vid, vec, meta))
                        new_ids.append(f"{prov}::{vid}")
                    if image_payload:
                        await self._pinecone.upsert_for(prov, namespace, image_payload)
                    image_count = len(parsed.images)

            self._state.upsert_file_record(
                FileVectorRecord(
                    file_id=file_id,
                    drive_id=drive.drive_id,
                    namespace=namespace,
                    content_hash=content_hash,
                    vector_ids=new_ids,
                    last_modified=last_modified,
                )
            )
            log.info(
                "process.indexed",
                file=file_name,
                chunks=text_count,
                images=image_count,
                providers=list(self._embedders.keys()),
                drive=drive.owner_label,
            )
            return ProcessResult(
                file_id=file_id,
                indexed=True,
                chunk_count=text_count,
                image_count=image_count,
            )

        finally:
            shutil.rmtree(local_dir, ignore_errors=True)

    async def handle_deletion(self, drive: DriveRef, file_id: str) -> ProcessResult:
        existing = self._state.get_file_record(drive.drive_id, file_id)
        if not existing:
            return ProcessResult(file_id=file_id, indexed=False, skipped_reason="unknown_delete")

        # Group by provider as in process_file().
        by_provider: dict[str, list[str]] = {}
        for raw in existing.vector_ids:
            if "::" in raw and raw.split("::", 1)[0] in {"openai", "gemini"}:
                prov, vid = raw.split("::", 1)
                by_provider.setdefault(prov, []).append(vid)
            else:
                by_provider.setdefault("openai", []).append(raw)
        for prov, ids in by_provider.items():
            await self._pinecone.delete_ids_for(prov, existing.namespace, ids)

        self._state.delete_file_record(drive.drive_id, file_id)
        log.info("process.deleted", file_id=file_id, drive=drive.owner_label)
        return ProcessResult(file_id=file_id, indexed=False, skipped_reason="deleted")

    async def _download(self, drive_id: str, item_id: str, dest: str) -> str:
        h = hashlib.sha256()
        async with self._graph.stream_drive_item(drive_id, item_id) as resp:
            with open(dest, "wb") as f:
                async for chunk in resp.aiter_bytes(chunk_size=64 * 1024):
                    f.write(chunk)
                    h.update(chunk)
        return h.hexdigest()


def _path_str(item: dict) -> str:
    parent = item.get("parentReference") or {}
    parent_path = parent.get("path", "") or ""
    name = item.get("name", "")
    if parent_path:
        return f"{parent_path}/{name}"
    return name


def _mime(item: dict) -> str:
    f = item.get("file") or {}
    return str(f.get("mimeType", "") or "")
