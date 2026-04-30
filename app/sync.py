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
from .embeddings import BaseEmbedder
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
        embedders: dict[str, BaseEmbedder],
        pinecone: PineconeStore,
        state: StateStore,
    ) -> None:
        self._settings = settings
        self._graph = graph
        self._state = state
        self._processor = Processor(settings, graph, embedders, pinecone, state)
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
        self._run_id = run_id  # used by helpers for event-attribution
        self._state.reset_progress()
        log.info("sync.start", run_id=run_id, scope=self._settings.sync_scope.value)
        self._state.record_event(
            run_id=run_id, level="info", event="run.start",
            message=f"Starter synkronisering · scope={self._settings.sync_scope.value} · "
                    f"providers={','.join(self._settings.providers())}",
        )

        try:
            self._state.record_event(
                run_id=run_id, level="info", event="discovery.start",
                message="Henter drives fra Microsoft Graph…",
            )
            try:
                drives = await discover_drives(self._graph, self._settings)
            except Exception as e:  # noqa: BLE001
                log.error("sync.discovery.error", err=str(e))
                self._state.record_event(
                    run_id=run_id, level="error", event="discovery.error",
                    message=f"Discovery feilet: {e}",
                )
                stats.errors += 1
                stats.error_samples.append(f"discovery: {e}")
                drives = []
            log.info("sync.drives.discovered", count=len(drives))
            self._state.record_event(
                run_id=run_id, level="info", event="discovery.done",
                message=f"Fant {len(drives)} drives å synkronisere",
            )
            for drv in drives:
                self._state.upsert_drive_progress(
                    drv.drive_id,
                    drive_label=drv.owner_label,
                    phase="discovering",
                )

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
                    self._state.record_event(
                        run_id=run_id, level="error", event="drive.error",
                        drive_id=drive.drive_id, drive_label=drive.owner_label,
                        message=str(e),
                    )

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
            self._state.record_event(
                run_id=run_id, level="info", event="run.done",
                message=(
                    f"Ferdig · {stats.drives_scanned} drives · "
                    f"{stats.files_indexed} indeksert · "
                    f"{stats.files_deleted} slettet · "
                    f"{stats.files_skipped} hoppet over · "
                    f"{stats.errors} feil"
                ),
            )
            # Keep events table from growing unbounded.
            try:
                self._state.prune_events(keep_last=10000)
            except Exception:  # noqa: BLE001
                pass
        return stats

    async def _sync_drive(self, drive: DriveRef, stats: RunStats) -> None:
        delta_link = self._state.get_delta_link(drive.drive_id)
        log.info(
            "sync.drive.start",
            drive=drive.owner_label,
            drive_id=drive.drive_id,
            resuming=bool(delta_link),
        )
        self._state.record_event(
            run_id=getattr(self, "_run_id", None), level="info", event="drive.start",
            drive_id=drive.drive_id, drive_label=drive.owner_label,
            message=f"Starter drive · {'fortsetter fra delta-checkpoint' if delta_link else 'full initial sync'}",
        )

        # Estimate file count for progress tracking (best-effort).
        estimated_total: Optional[int] = None
        try:
            estimated_total = await self._graph.estimate_drive_file_count(drive.drive_id)
        except Exception as e:  # noqa: BLE001
            log.debug("sync.drive.estimate.skip", err=str(e))

        self._state.upsert_drive_progress(
            drive.drive_id,
            drive_label=drive.owner_label,
            estimated_total=estimated_total,
            phase="syncing",
            mark_started=True,
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
            self._state.record_event(
                run_id=getattr(self, "_run_id", None), level="info", event="drive.done",
                drive_id=drive.drive_id, drive_label=drive.owner_label,
                message="Drive ferdig · delta-checkpoint lagret",
            )
        else:
            log.warning(
                "sync.drive.no_delta_link",
                drive=drive.owner_label,
                hint="Graph returned no @odata.deltaLink; will retry next run.",
            )
            self._state.record_event(
                run_id=getattr(self, "_run_id", None), level="warn", event="drive.no_delta",
                drive_id=drive.drive_id, drive_label=drive.owner_label,
                message="Graph returnerte ingen @odata.deltaLink — prøver igjen neste runde",
            )
        self._state.upsert_drive_progress(drive.drive_id, phase="done")

    def _path_for(self, item: dict) -> str:
        """Build the full path string for an item ("/Documents/Subfolder/foo.pdf")."""
        parent = (item.get("parentReference") or {}).get("path", "") or ""
        # Graph reports parent path as e.g. "/drives/{id}/root:/Folder/Sub"
        # Normalise to just the path portion.
        if ":" in parent:
            parent = parent.split(":", 1)[1] or ""
        name = item.get("name", "") or ""
        full = f"{parent}/{name}".replace("//", "/")
        if not full.startswith("/"):
            full = "/" + full
        return full.lower()

    def _passes_path_filter(self, drive: DriveRef, item: dict) -> bool:
        """Apply include/exclude path filters and per-user folder selections."""
        path = self._path_for(item)
        includes = self._settings.include_paths_list()
        excludes = self._settings.exclude_paths_list()

        # Per-user folder selections override the global include list when
        # the drive matches a configured user.
        selections = self._settings.folder_selections()
        if selections:
            user_paths = selections.get(drive.owner_label) or []
            if user_paths:
                if not any(path.startswith(p) for p in user_paths):
                    return False

        if includes and not any(path.startswith(p) for p in includes):
            return False
        if excludes and any(path.startswith(p) for p in excludes):
            return False
        return True

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

        # Folders surface in delta but aren't files; count them in seen but
        # don't try to process.
        if "folder" in item:
            self._state.increment_drive_progress(drive.drive_id, seen_delta=1)
            return

        # Path filtering (include/exclude + folder selections)
        if not self._passes_path_filter(drive, item):
            self._state.increment_drive_progress(drive.drive_id, seen_delta=1)
            stats.files_skipped += 1
            return

        # Live progress: bump seen and surface current file.
        self._state.increment_drive_progress(
            drive.drive_id,
            seen_delta=1,
            current_file=item.get("name"),
        )

        try:
            res = await self._processor.process_file(drive, item)
            if res.indexed:
                stats.files_indexed += 1
                self._state.increment_drive_progress(drive.drive_id, processed_delta=1)
                self._state.record_event(
                    run_id=getattr(self, "_run_id", None), level="info", event="file.indexed",
                    drive_id=drive.drive_id, drive_label=drive.owner_label,
                    file_name=item.get("name"),
                    message=f"{res.chunk_count} tekst-chunks · {res.image_count} bilder",
                )
            elif res.skipped_reason:
                stats.files_skipped += 1
                # Only log non-trivial skips so we don't drown the log in
                # "unchanged" entries on every delta run.
                if res.skipped_reason not in ("unchanged", "content_unchanged"):
                    self._state.record_event(
                        run_id=getattr(self, "_run_id", None), level="info",
                        event="file.skipped",
                        drive_id=drive.drive_id, drive_label=drive.owner_label,
                        file_name=item.get("name"),
                        message=f"Hoppet over: {res.skipped_reason}",
                    )
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
            self._state.record_event(
                run_id=getattr(self, "_run_id", None), level="error", event="file.error",
                drive_id=drive.drive_id, drive_label=drive.owner_label,
                file_name=item.get("name"),
                message=str(e)[:500],
            )
