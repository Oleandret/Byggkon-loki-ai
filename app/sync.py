"""Top-level sync orchestrator.

A single `run_sync()` call:
  1. Discovers drives in scope.
  2. For each drive, runs a delta query (resuming from the last deltaLink).
  3. Routes each change through Processor (index / re-index / delete).
  4. Persists the new deltaLink only after all items in this run are
     successfully processed (so a crash mid-run replays from the last good
     checkpoint).
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Optional

from .config import Settings
from .drive_discovery import DriveRef, discover_drives
from .embeddings import Embedder
from .graph_client import GraphClient, GraphError
from .logging_config import get_logger
from .pinecone_store import PineconeStore
from .processor import Processor
from .state import StateStore

log = get_logger(__name__)


@dataclass
class RunStats:
    drives_scanned: int = 0
    files_indexed: int = 0
    files_deleted: int = 0
    files_skipped: int = 0
    errors: int = 0
    error_samples: list[str] = field(default_factory=list)


class SyncOrchestrator:
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
        self._state = state
        self._processor = Processor(settings, graph, embedder, pinecone, state)
        self._semaphore = asyncio.Semaphore(settings.process_concurrency)
        self._running = False
        self._lock = asyncio.Lock()

    async def run_sync(self) -> RunStats:
        async with self._lock:
            if self._running:
                log.info("sync.skip.already_running")
                return RunStats()
            self._running = True
        try:
            return await self._run_inner()
        finally:
            self._running = False

    async def _run_inner(self) -> RunStats:
        stats = RunStats()
        run_id = self._state.start_run()
        log.info("sync.start", run_id=run_id, scope=self._settings.sync_scope.value)

        try:
            drives = await discover_drives(self._graph, self._settings)
            log.info("sync.drives.discovered", count=len(drives))

            for drive in drives:
                stats.drives_scanned += 1
                try:
                    await self._sync_drive(drive, stats)
                except Exception as e:  # noqa: BLE001
                    stats.errors += 1
                    msg = f"drive {drive.owner_label} ({drive.drive_id}): {e}"
                    if len(stats.error_samples) < 10:
                        stats.error_samples.append(msg)
                    log.error("sync.drive.error", drive=drive.owner_label, err=str(e))

        finally:
            self._state.finish_run(
                run_id,
                drives_scanned=stats.drives_scanned,
                files_indexed=stats.files_indexed,
                files_deleted=stats.files_deleted,
                files_skipped=stats.files_skipped,
                errors=stats.errors,
                notes=" | ".join(stats.error_samples)[:1000],
            )
            log.info(
                "sync.done",
                run_id=run_id,
                drives=stats.drives_scanned,
                indexed=stats.files_indexed,
                deleted=stats.files_deleted,
                skipped=stats.files_skipped,
                errors=stats.errors,
            )
        return stats

    async def _sync_drive(self, drive: DriveRef, stats: RunStats) -> None:
        delta_link = self._state.get_delta_link(drive.drive_id)
        log.info(
            "sync.drive.start",
            drive=drive.owner_label,
            drive_id=drive.drive_id,
            resuming=bool(delta_link),
        )

        new_delta_link: Optional[str] = None
        pending: list[asyncio.Task] = []

        async def _route(item: dict) -> None:
            async with self._semaphore:
                await self._route_item(drive, item, stats)

        try:
            try:
                async for item, link in self._graph.iter_drive_delta(
                    drive.drive_id, delta_link=delta_link
                ):
                    if link:
                        new_delta_link = link
                    if item.get("__deltaLinkOnly__"):
                        continue
                    pending.append(asyncio.create_task(_route(item)))

                    # Keep the in-flight set bounded so memory stays sane on
                    # huge drives.
                    if len(pending) >= 200:
                        done, still = await asyncio.wait(
                            pending, return_when=asyncio.FIRST_COMPLETED
                        )
                        pending = list(still)
                        for d in done:
                            exc = d.exception()
                            if exc:
                                stats.errors += 1
                                if len(stats.error_samples) < 10:
                                    stats.error_samples.append(str(exc))
            except GraphError as e:
                # 410 Gone on the deltaLink means the token expired — do a full
                # resync from scratch for this drive.
                if e.status == 410:
                    log.warning("sync.drive.delta_gone", drive=drive.owner_label)
                    self._state.set_delta_link(drive.drive_id, "", drive.as_metadata())
                    return
                raise

            if pending:
                done, _ = await asyncio.wait(pending)
                for d in done:
                    exc = d.exception()
                    if exc:
                        stats.errors += 1
                        if len(stats.error_samples) < 10:
                            stats.error_samples.append(str(exc))
        finally:
            # Cancel anything left if we're bailing out early.
            for t in pending:
                if not t.done():
                    t.cancel()

        if new_delta_link:
            self._state.set_delta_link(
                drive.drive_id, new_delta_link, drive.as_metadata()
            )
            log.info(
                "sync.drive.checkpointed", drive=drive.owner_label
            )
        else:
            log.warning(
                "sync.drive.no_delta_link",
                drive=drive.owner_label,
                hint="Graph returned no @odata.deltaLink; will retry next run.",
            )

    async def _route_item(
        self, drive: DriveRef, item: dict, stats: RunStats
    ) -> None:
        # Deletions arrive as items with a `deleted` facet.
        if "deleted" in item:
            try:
                res = await self._processor.handle_deletion(drive, item["id"])
                if res.skipped_reason == "deleted":
                    stats.files_deleted += 1
                else:
                    stats.files_skipped += 1
            except Exception as e:  # noqa: BLE001
                stats.errors += 1
                if len(stats.error_samples) < 10:
                    stats.error_samples.append(f"delete {item.get('id')}: {e}")
                log.error("sync.delete.error", item_id=item.get("id"), err=str(e))
            return

        try:
            res = await self._processor.process_file(drive, item)
            if res.indexed:
                stats.files_indexed += 1
            elif res.skipped_reason:
                stats.files_skipped += 1
        except Exception as e:  # noqa: BLE001
            stats.errors += 1
            if len(stats.error_samples) < 10:
                stats.error_samples.append(f"{item.get('name')}: {e}")
            log.error(
                "sync.process.error",
                file=item.get("name"),
                file_id=item.get("id"),
                err=str(e),
            )
