"""Discover the set of drives to sync, given the configured SYNC_SCOPE.

Returns a list of `DriveRef` objects — each carries the `drive_id` plus
whatever metadata is useful for logging and Pinecone metadata (drive type,
owner, site name, etc.).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .config import Settings, SyncScope
from .graph_client import GraphClient
from .logging_config import get_logger

log = get_logger(__name__)


@dataclass(frozen=True)
class DriveRef:
    drive_id: str
    drive_type: str  # 'personal' | 'business' | 'documentLibrary'
    owner_label: str  # UPN, site name, or 'unknown'
    site_id: Optional[str] = None

    def as_metadata(self) -> dict[str, str]:
        m = {
            "drive_id": self.drive_id,
            "drive_type": self.drive_type,
            "owner": self.owner_label,
        }
        if self.site_id:
            m["site_id"] = self.site_id
        return m


async def discover_drives(graph: GraphClient, settings: Settings) -> list[DriveRef]:
    scope = settings.sync_scope

    if scope == SyncScope.DRIVES_CSV:
        ids = settings.drive_ids_list()
        log.info("discovery.drives_csv", count=len(ids))
        return [
            DriveRef(drive_id=d, drive_type="unknown", owner_label="configured")
            for d in ids
        ]

    drives: list[DriveRef] = []
    seen_ids: set[str] = set()

    if scope == SyncScope.USERS_CSV:
        upns = settings.users_list()
        log.info("discovery.users_csv", count=len(upns))
        for upn in upns:
            drv = await graph.get_user_drive(upn)
            if drv and drv.get("id") and drv["id"] not in seen_ids:
                seen_ids.add(drv["id"])
                drives.append(
                    DriveRef(
                        drive_id=drv["id"],
                        drive_type=drv.get("driveType", "business"),
                        owner_label=upn,
                    )
                )
        return drives

    # all_users (+ optional sharepoint)
    log.info("discovery.users.start")
    user_count = 0
    async for user in graph.iter_users():
        user_count += 1
        if user.get("accountEnabled") is False:
            continue
        upn = user.get("userPrincipalName") or user.get("id")
        if not upn:
            continue
        drv = await graph.get_user_drive(upn)
        if drv and drv.get("id") and drv["id"] not in seen_ids:
            seen_ids.add(drv["id"])
            drives.append(
                DriveRef(
                    drive_id=drv["id"],
                    drive_type=drv.get("driveType", "business"),
                    owner_label=upn,
                )
            )
    log.info("discovery.users.done", users_seen=user_count, drives=len(drives))

    if scope == SyncScope.ALL_USERS_AND_SHAREPOINT:
        log.info("discovery.sharepoint.start")
        site_count = 0
        async for site in graph.iter_sharepoint_sites():
            site_count += 1
            site_id = site.get("id")
            site_label = site.get("displayName") or site.get("name") or site_id or "site"
            if not site_id:
                continue
            try:
                async for drv in graph.iter_site_drives(site_id):
                    if drv.get("id") and drv["id"] not in seen_ids:
                        seen_ids.add(drv["id"])
                        drives.append(
                            DriveRef(
                                drive_id=drv["id"],
                                drive_type=drv.get("driveType", "documentLibrary"),
                                owner_label=str(site_label),
                                site_id=site_id,
                            )
                        )
            except Exception as e:  # noqa: BLE001 — keep going on per-site errors
                log.warning("discovery.site.error", site_id=site_id, err=str(e))
        log.info("discovery.sharepoint.done", sites_seen=site_count)

    return drives
