"""Per-file pipeline: download -> unstructured -> embed -> upsert to Pinecone.

Concurrency: this module is intentionally I/O-async, but the unstructured
partition step is CPU/IO heavy and sync — we run it in a thread.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
import tempfile
import uuid
from dataclasses import dataclass
from typing import Optional

from .config import Settings
from .drive_discovery import DriveRef
from .embeddings import Embedder
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
    skipped_reason: Optional[str] = None
    error: Optional[str] = None


class Processor:
    def __init__(
        self,
        settings: Settings,
        graph: GraphClient,
        embedder: Embedder,
        pinecone: PineconeStore,
        state: StateStore,
    ) -> None:
        self._settings = settings
        self._graph = graph
        self._embedder = embedder
        self._pinecone = pinecone
        self._state = state
        os.makedirs(settings.tmp_dir, exist_ok=True)

    async def process_file(
        self, drive: DriveRef, item: dict
    ) -> ProcessResult:
        """Index or re-index a single driveItem."""
        file_id = item["id"]
        file_name = item.get("name", "<unknown>")
        size = int(item.get("size") or 0)
        last_modified = item.get("lastModifiedDateTime", "")
        web_url = item.get("webUrl")

        # Fast skips
        if "folder" in item:
            return ProcessResult(file_id=file_id, indexed=False, skipped_reason="folder")
        if not is_supported_extension(file_name):
            return ProcessResult(file_id=file_id, indexed=False, skipped_reason="unsupported_ext")
        if size > self._settings.max_file_bytes:
            log.info(
                "process.skip.too_large",
                file=file_name,
                size=size,
                max=self._settings.max_file_bytes,
            )
            return ProcessResult(file_id=file_id, indexed=False, skipped_reason="too_large")

        # Skip if already up to date (compare lastModifiedDateTime)
        existing = self._state.get_file_record(drive.drive_id, file_id)
        if existing and existing.last_modified == last_modified and existing.last_modified:
            return ProcessResult(file_id=file_id, indexed=False, skipped_reason="unchanged")

        namespace = self._pinecone.namespace_for_drive(drive.drive_id)

        # ─── Download to a tempfile ───────────────────────────────────
        suffix = os.path.splitext(file_name)[1] or ""
        local_dir = tempfile.mkdtemp(dir=self._settings.tmp_dir, prefix="item_")
        local_path = os.path.join(local_dir, f"file{suffix}")
        try:
            content_hash = await self._download(drive.drive_id, file_id, local_path)

            # If we have a prior content hash and it matches, just refresh
            # the lastModified bookkeeping — content is identical.
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

            # ─── Partition + chunk (sync, in a thread) ────────────────
            chunks = await asyncio.to_thread(
                partition_and_chunk, local_path, self._settings
            )
            if not chunks:
                # File parsed to nothing useful — still record it so we don't
                # re-attempt it every cycle.
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

            # ─── Embed ────────────────────────────────────────────────
            texts = [c.text for c in chunks]
            vectors = await self._embedder.embed(texts)

            # ─── Build upsert payload ─────────────────────────────────
            new_ids: list[str] = []
            payload: list[tuple[str, list[float], dict]] = []
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
            for i, (chunk, vec) in enumerate(zip(chunks, vectors)):
                vid = f"{file_id}::{i}::{uuid.uuid4().hex[:8]}"
                meta = {
                    **base_meta,
                    **chunk.metadata,
                    "chunk_index": i,
                    "text": chunk.text,  # store text for retrieval-time display
                }
                payload.append((vid, vec, meta))
                new_ids.append(vid)

            # ─── Delete old chunks (if any) before upserting new ones ─
            if existing and existing.vector_ids:
                await self._pinecone.delete_ids(namespace, existing.vector_ids)

            await self._pinecone.upsert(namespace, payload)

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
                chunks=len(new_ids),
                drive=drive.owner_label,
            )
            return ProcessResult(file_id=file_id, indexed=True, chunk_count=len(new_ids))

        finally:
            shutil.rmtree(local_dir, ignore_errors=True)

    async def handle_deletion(self, drive: DriveRef, file_id: str) -> ProcessResult:
        """Remove a file's vectors when delta reports it deleted."""
        existing = self._state.get_file_record(drive.drive_id, file_id)
        if not existing:
            return ProcessResult(file_id=file_id, indexed=False, skipped_reason="unknown_delete")
        await self._pinecone.delete_ids(existing.namespace, existing.vector_ids)
        self._state.delete_file_record(drive.drive_id, file_id)
        log.info("process.deleted", file_id=file_id, drive=drive.owner_label)
        return ProcessResult(file_id=file_id, indexed=False, skipped_reason="deleted")

    # ─── helpers ──────────────────────────────────────────────────────
    async def _download(self, drive_id: str, item_id: str, dest: str) -> str:
        """Stream-download item content; return sha256 hex digest."""
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
        # parent path looks like "/drive/root:/Folder/Sub"
        return f"{parent_path}/{name}"
    return name


def _mime(item: dict) -> str:
    f = item.get("file") or {}
    return str(f.get("mimeType", "") or "")
