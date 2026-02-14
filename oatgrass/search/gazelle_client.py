"""Minimal Gazelle client adapter reusing MAESTRO-style service patterns."""

from __future__ import annotations

import asyncio
import time
from typing import Any, Awaitable, Callable, Dict, Optional, TypeVar

import aiohttp

from oatgrass.config import TrackerConfig
from oatgrass.rate_limits import (
    GAZELLE_MIN_INTERVAL_SECONDS,
    GAZELLE_WAIT_LOG_THRESHOLD_SECONDS,
    enforce_gazelle_min_interval,
)
from oatgrass.search.protocols import GazelleClient
from oatgrass.search.types import GazelleSearchResult
from oatgrass.tracker_auth import build_tracker_auth_header
from oatgrass import logger
from oatgrass.__version__ import __version__

DEFAULT_USER_AGENT = f"Oatgrass/{__version__}"
_T = TypeVar("_T")


class GazelleServiceAdapter(GazelleClient):
    """Simple Gazelle API adapter for browsed searches."""

    def __init__(
        self,
        tracker: TrackerConfig,
        timeout: int = 10,
        max_concurrency: int = 3,
        min_interval_seconds: float = GAZELLE_MIN_INTERVAL_SECONDS,
    ):
        if not tracker.api_key:
            raise ValueError("Gazelle tracker API key is required for search adapter.")

        self.tracker = tracker
        self.timeout = timeout
        self.base_url = tracker.url.rstrip("/")
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._min_interval_seconds = max(0.0, float(min_interval_seconds))
        self._auth_mode = "api_key"
        self._session: aiohttp.ClientSession | None = None
        self._session_lock = asyncio.Lock()

    async def search(
        self,
        searchstr: Optional[str] = None,
        artist: Optional[str] = None,
        album: Optional[str] = None,
        groupname: Optional[str] = None,
        artistname: Optional[str] = None,
        year: Optional[int | str] = None,
        release_type: Optional[int] = None,
        media: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Search using ajax.php?action=browse."""
        params: Dict[str, Any] = {"action": "browse"}
        
        if searchstr:
            params["searchstr"] = searchstr
        if artist or artistname:
            params["artistname"] = artist or artistname
        if album or groupname:
            params["groupname"] = album or groupname
        if year is not None:
            params["year"] = str(year)
        if release_type:
            params["releasetype"] = release_type
        if media:
            params["media"] = media

        return await self._request(params)
    
    async def get_group(self, group_id: int) -> Dict[str, Any]:
        """Get torrent group details using torrents.php?action=torrentgroup."""
        params = {"action": "torrentgroup", "id": group_id}
        return await self._request(params)

    async def get_collage(self, collage_id: int, page: int = 1) -> Dict[str, Any]:
        """Get collage details using ajax.php?action=collage."""
        params = {"action": "collage", "id": collage_id, "page": page}
        return await self._request(params)

    async def get_index(self) -> Dict[str, Any]:
        """Get index payload for current authenticated user."""
        return await self._request({"action": "index"})

    async def get_user_torrents(
        self,
        *,
        list_type: str,
        user_id: int,
        limit: int,
        offset: int,
    ) -> Dict[str, Any]:
        """Get paginated user_torrents payload."""
        params = {
            "action": "user_torrents",
            "type": list_type,
            "id": user_id,
            "limit": limit,
            "offset": offset,
        }
        return await self._request(params)

    async def get_torrent(self, torrent_id: int) -> Dict[str, Any]:
        """Get torrent details using ajax.php?action=torrent."""
        params = {"action": "torrent", "id": torrent_id}
        return await self._request(params)

    async def _request(self, params: Dict[str, Any]) -> Dict[str, Any]:
        status, data, elapsed_ms = await self._request_with_retries(
            params,
            lambda response: response.json(),
        )
        logger.get_logger().api_response(status, data, elapsed_ms)
        return data

    async def _request_with_retries(
        self,
        params: Dict[str, Any],
        parser: Callable[[aiohttp.ClientResponse], Awaitable[_T]],
    ) -> tuple[int, _T, float]:
        url = f"{self.base_url}/ajax.php"
        logger.get_logger().api_request("GET", url, params)
        max_retries = 3
        request_start = time.time()

        async with self._semaphore:
            await self._enforce_interval()
            session = await self._ensure_session()
            for attempt in range(max_retries):
                try:
                    async with session.get(url, params=params) as response:
                        if response.status >= 400:
                            text = await response.text()
                            exc = aiohttp.ClientResponseError(
                                request_info=response.request_info,
                                history=response.history,
                                status=response.status,
                                message=text,
                                headers=response.headers,
                            )
                            # Retry only transient server failures and explicit throttling.
                            if attempt < max_retries - 1 and response.status in {429, 500, 502, 503, 504}:
                                delay = self._retry_delay_seconds(attempt=attempt, retry_after=response.headers.get("Retry-After"))
                                logger.get_logger().api_retry(self.tracker.name.upper(), attempt + 1, max_retries, delay)
                                await asyncio.sleep(delay)
                                continue
                            raise exc
                        data = await parser(response)
                        elapsed_ms = (time.time() - request_start) * 1000
                        return response.status, data, elapsed_ms
                except (asyncio.TimeoutError, aiohttp.ClientConnectionError, aiohttp.ServerTimeoutError):
                    if attempt < max_retries - 1:
                        delay = 2 ** (attempt + 1)
                        logger.get_logger().api_retry(self.tracker.name.upper(), attempt + 1, max_retries, delay)
                        await asyncio.sleep(delay)
                    else:
                        logger.get_logger().api_failed(self.tracker.name.upper(), max_retries)
                        raise

    @staticmethod
    def _retry_delay_seconds(*, attempt: int, retry_after: str | None) -> int:
        if retry_after:
            try:
                value = int(float(retry_after))
            except (TypeError, ValueError):
                value = 0
            if value > 0:
                return value
        return 2 ** (attempt + 1)

    async def _enforce_interval(self) -> None:
        wait = await enforce_gazelle_min_interval(
            self.base_url,
            min_interval_seconds=self._min_interval_seconds,
            tracker_name=self.tracker.name,
            auth_mode=self._auth_mode,
        )
        log = logger.get_logger()
        log.api_wait_debug(self.tracker.name.upper(), wait)
        if wait > GAZELLE_WAIT_LOG_THRESHOLD_SECONDS:
            log.api_wait(self.tracker.name.upper(), wait)

    async def _ensure_session(self) -> aiohttp.ClientSession:
        session = self._session
        if session is not None and not session.closed:
            return session

        async with self._session_lock:
            session = self._session
            if session is None or session.closed:
                timeout = aiohttp.ClientTimeout(total=self.timeout)
                self._session = aiohttp.ClientSession(
                    headers=self._get_headers(),
                    timeout=timeout,
                )
            return self._session

    def _get_headers(self) -> Dict[str, str]:
        auth = build_tracker_auth_header(self.tracker.name, self.tracker.api_key)
        return {"Authorization": auth, "User-Agent": DEFAULT_USER_AGENT}

    def _map_result(self, result: Dict[str, Any]) -> GazelleSearchResult:
        group_id = (
            result.get("groupId")
            or result.get("group_id")
            or result.get("groupID")
            or int(result.get("groupid", 0))
        )
        title = (
            result.get("groupName")
            or result.get("groupname")
            or result.get("title")
            or result.get("name")
            or ""
        )
        metadata = {k.lower(): v for k, v in result.items()}
        return GazelleSearchResult(
            group_id=int(group_id or 0),
            title=str(title),
            site_name=self.tracker.name.upper(),
            metadata=metadata,
        )
    
    async def close(self) -> None:
        """Close any open connections."""
        async with self._session_lock:
            session = self._session
            self._session = None
        if session is not None and not session.closed:
            await session.close()
