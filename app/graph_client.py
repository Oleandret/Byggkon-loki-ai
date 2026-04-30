"""Microsoft Graph client (app-only, client credentials).

Wraps:
  * MSAL ConfidentialClientApplication for tenant-wide tokens (with caching).
  * httpx for HTTP, with tenacity retries on 429/5xx and Retry-After respect.
  * Helpers for paged iteration, drive enumeration, delta queries, and
    streamed downloads (so we don't blow memory on large files).
"""
from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Optional

import httpx
import msal
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from .config import Settings
from .logging_config import get_logger

log = get_logger(__name__)


class GraphError(Exception):
    """Raised when Graph returns an unrecoverable error."""

    def __init__(self, status: int, body: str, url: str) -> None:
        super().__init__(f"Graph error {status} on {url}: {body[:500]}")
        self.status = status
        self.body = body
        self.url = url


class _RetryableGraphError(Exception):
    """Internal: signals tenacity to retry."""


class GraphClient:
    """Async Microsoft Graph client.

    A single instance is meant to live for the process lifetime.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

        # Pre-flight: validate that all three creds are present BEFORE we
        # let MSAL parse the authority URL — MSAL's error messages are vague
        # ("invalid authority url") and don't tell you which field is empty.
        missing = []
        if not settings.graph_tenant_id.strip():
            missing.append("GRAPH_TENANT_ID")
        if not settings.graph_client_id.strip():
            missing.append("GRAPH_CLIENT_ID")
        if not settings.graph_client_secret.strip():
            missing.append("GRAPH_CLIENT_SECRET")
        if missing:
            raise ValueError(
                f"Microsoft Graph credentials missing: {', '.join(missing)}. "
                "Set these env vars in Railway and redeploy, or fill them "
                "in via /admin → Innstillinger."
            )

        try:
            self._msal_app = msal.ConfidentialClientApplication(
                client_id=settings.graph_client_id.strip(),
                client_credential=settings.graph_client_secret.strip(),
                authority=settings.graph_authority_url.strip(),
            )
        except ValueError as e:
            raise ValueError(
                f"MSAL rejected the Graph configuration: {e}. "
                f"Authority URL was: {settings.graph_authority_url!r}. "
                "Sjekk at GRAPH_TENANT_ID er en gyldig GUID (eller 'common'/'organizations'), "
                "og at GRAPH_AUTHORITY ikke har trailing slash."
            ) from e

        # MSAL caches tokens internally per scope; we cache the dict it returns
        # so we don't acquire on every request.
        self._token: Optional[str] = None
        self._token_exp: float = 0.0
        self._token_lock = asyncio.Lock()
        self._http = httpx.AsyncClient(
            timeout=httpx.Timeout(60.0, connect=10.0, read=120.0),
            follow_redirects=True,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    # ─── Auth ─────────────────────────────────────────────────────────
    async def _get_token(self) -> str:
        # 60s safety margin
        if self._token and time.time() < self._token_exp - 60:
            return self._token

        async with self._token_lock:
            if self._token and time.time() < self._token_exp - 60:
                return self._token

            # MSAL is sync; offload to a thread.
            result = await asyncio.to_thread(
                self._msal_app.acquire_token_for_client,
                scopes=[self._settings.graph_scope],
            )
            if not result or "access_token" not in result:
                raise GraphError(
                    status=401,
                    body=f"MSAL acquire_token_for_client failed: {result!r}",
                    url=self._settings.graph_authority_url,
                )
            self._token = result["access_token"]
            self._token_exp = time.time() + int(result.get("expires_in", 3600))
            log.info("graph.token.refreshed", expires_in=int(result.get("expires_in", 0)))
            return self._token

    async def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        token = await self._get_token()
        h = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        if extra:
            h.update(extra)
        return h

    # ─── Core HTTP with retries ───────────────────────────────────────
    async def request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
        extra_headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        """Make a Graph request with retries on 429/5xx."""
        if url.startswith("/"):
            url = f"{self._settings.graph_base_url}{url}"

        attempt = 0
        async for retry in AsyncRetrying(
            stop=stop_after_attempt(6),
            wait=wait_exponential(multiplier=1, min=1, max=30),
            retry=retry_if_exception_type(_RetryableGraphError),
            reraise=True,
        ):
            with retry:
                attempt += 1
                headers = await self._headers(extra_headers)
                try:
                    resp = await self._http.request(
                        method, url, params=params, json=json_body, headers=headers
                    )
                except (httpx.ConnectError, httpx.ReadTimeout, httpx.RemoteProtocolError) as e:
                    log.warning("graph.transport.retry", url=url, attempt=attempt, err=str(e))
                    raise _RetryableGraphError() from e

                if resp.status_code in (429, 503) or 500 <= resp.status_code < 600:
                    retry_after = resp.headers.get("Retry-After")
                    if retry_after:
                        try:
                            await asyncio.sleep(min(float(retry_after), 60))
                        except ValueError:
                            pass
                    log.warning(
                        "graph.http.retry",
                        url=url,
                        status=resp.status_code,
                        attempt=attempt,
                    )
                    raise _RetryableGraphError()

                if resp.status_code == 401 and attempt == 1:
                    # Token might have just expired; force a refresh and retry once.
                    self._token = None
                    raise _RetryableGraphError()

                if resp.status_code >= 400:
                    raise GraphError(resp.status_code, resp.text, url)
                return resp
        # Unreachable; AsyncRetrying always returns or raises.
        raise RuntimeError("retry loop exhausted")  # pragma: no cover

    async def get_json(self, url: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        resp = await self.request("GET", url, params=params)
        return resp.json()

    async def iter_paged(
        self, url: str, params: dict[str, Any] | None = None
    ) -> AsyncIterator[dict[str, Any]]:
        """Iterate items across @odata.nextLink pages (non-delta)."""
        next_url: Optional[str] = url
        next_params = params
        while next_url:
            data = await self.get_json(next_url, params=next_params)
            for item in data.get("value", []):
                yield item
            next_url = data.get("@odata.nextLink")
            next_params = None  # nextLink is fully qualified

    # ─── Delta queries ────────────────────────────────────────────────
    async def iter_drive_delta(
        self, drive_id: str, delta_link: Optional[str] = None
    ) -> AsyncIterator[tuple[dict[str, Any], Optional[str]]]:
        """Iterate driveItem changes for a drive.

        Yields (item, delta_link). delta_link is None until the final page,
        on which the caller MUST persist it for the next run.

        If delta_link is provided, picks up from there. Otherwise starts a
        full enumeration (initial sync) and walks until @odata.deltaLink
        appears.
        """
        if delta_link:
            url: str = delta_link
            params: dict[str, Any] | None = None
        else:
            url = f"/drives/{drive_id}/root/delta"
            params = {"$top": 200}

        while True:
            data = await self.get_json(url, params=params)
            params = None  # subsequent links are fully-qualified

            items = data.get("value", [])
            next_link = data.get("@odata.nextLink")
            new_delta = data.get("@odata.deltaLink")

            for item in items:
                # delta_link is only meaningful on the final page; return None
                # for intermediate items so the caller doesn't checkpoint early.
                yield item, None

            if next_link:
                url = next_link
                continue
            if new_delta:
                # Signal end-of-run with a sentinel item containing the deltaLink.
                yield {"__deltaLinkOnly__": True}, new_delta
                return
            return

    # ─── Drive helpers ────────────────────────────────────────────────
    async def get_user_drive(self, user_upn_or_id: str) -> Optional[dict[str, Any]]:
        """Return the user's primary OneDrive (or None if they don't have one)."""
        try:
            return await self.get_json(f"/users/{user_upn_or_id}/drive")
        except GraphError as e:
            if e.status in (403, 404):
                log.info("graph.user_drive.skip", user=user_upn_or_id, status=e.status)
                return None
            raise

    async def iter_users(self) -> AsyncIterator[dict[str, Any]]:
        """Iterate all users in the tenant (paged)."""
        params = {"$select": "id,userPrincipalName,displayName,accountEnabled", "$top": 200}
        async for user in self.iter_paged("/users", params=params):
            yield user

    async def iter_sharepoint_sites(self) -> AsyncIterator[dict[str, Any]]:
        """Iterate all SharePoint sites visible to the app.

        Uses /sites?search=* which is the documented way to enumerate sites.
        """
        async for site in self.iter_paged("/sites", params={"search": "*", "$top": 200}):
            yield site

    async def iter_site_drives(self, site_id: str) -> AsyncIterator[dict[str, Any]]:
        async for drive in self.iter_paged(f"/sites/{site_id}/drives"):
            yield drive

    async def get_drive(self, drive_id: str) -> Optional[dict[str, Any]]:
        try:
            return await self.get_json(f"/drives/{drive_id}")
        except GraphError as e:
            if e.status in (403, 404):
                return None
            raise

    async def estimate_drive_file_count(self, drive_id: str) -> Optional[int]:
        """Best-effort total file count for a drive.

        Strategy: pull the root quota, which gives total bytes used. We can
        also try /root with $expand=children/$count for direct file count,
        but Graph doesn't always honour that. Fall back to None when unsure.
        """
        try:
            root = await self.get_json(
                f"/drives/{drive_id}/root",
                params={"$select": "id,name,folder"},
            )
        except GraphError:
            return None
        # Some drives expose a 'folder.childCount' on root, but it only counts
        # immediate children, not the recursive total. We use a search query
        # filtered by 'file' to get a rough count instead.
        try:
            data = await self.get_json(
                f"/drives/{drive_id}/root/search(q='')",
                params={"$select": "id", "$top": 1, "$count": "true"},
                # ConsistencyLevel header is required for $count on Graph.
            )
            if "@odata.count" in data:
                return int(data["@odata.count"])
        except GraphError:
            pass
        return None

    async def iter_folder_children(
        self, drive_id: str, folder_id: str
    ) -> AsyncIterator[dict[str, Any]]:
        """Iterate immediate children of a folder (paged)."""
        async for item in self.iter_paged(
            f"/drives/{drive_id}/items/{folder_id}/children",
            params={"$select": "id,name,folder,file,parentReference,size,webUrl"},
        ):
            yield item

    async def get_root_folder_id(self, drive_id: str) -> Optional[str]:
        try:
            data = await self.get_json(
                f"/drives/{drive_id}/root", params={"$select": "id"}
            )
            return data.get("id")
        except GraphError:
            return None

    # ─── Download ─────────────────────────────────────────────────────
    @asynccontextmanager
    async def stream_drive_item(
        self, drive_id: str, item_id: str
    ) -> AsyncIterator[httpx.Response]:
        """Stream the binary content of a driveItem.

        Usage:
            async with graph.stream_drive_item(drive_id, item_id) as resp:
                async for chunk in resp.aiter_bytes():
                    ...

        Graph returns 302 to a pre-authenticated download URL; httpx follows it.
        """
        url = f"{self._settings.graph_base_url}/drives/{drive_id}/items/{item_id}/content"
        token = await self._get_token()
        headers = {"Authorization": f"Bearer {token}"}
        async with self._http.stream("GET", url, headers=headers) as resp:
            if resp.status_code >= 400:
                body = await resp.aread()
                raise GraphError(resp.status_code, body.decode(errors="replace"), url)
            yield resp
