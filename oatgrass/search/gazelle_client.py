"""Minimal Gazelle client adapter reusing MAESTRO-style service patterns."""

from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, Optional, Sequence

import aiohttp

from oatgrass.config import TrackerConfig
from oatgrass.search.protocols import GazelleClient
from oatgrass.search.types import GazelleSearchResult

DEFAULT_USER_AGENT = "Oatgrass/0.0.1"


class GazelleServiceAdapter(GazelleClient):
    """Simple Gazelle API adapter for browsed searches."""

    def __init__(
        self,
        tracker: TrackerConfig,
        timeout: int = 10,
        max_concurrency: int = 3,
        min_interval: float = 1.0,
    ):
        if not tracker.api_key:
            raise ValueError("Gazelle tracker API key is required for search adapter.")

        self.tracker = tracker
        self.timeout = timeout
        self.base_url = tracker.url.rstrip("/")
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._min_interval = min_interval
        self._last_request = 0.0

    async def search(
        self,
        artist: str,
        album: Optional[str] = None,
        year: Optional[int] = None,
        release_type: Optional[int] = None,
        media: Optional[str] = None,
    ) -> Sequence[GazelleSearchResult]:
        if not artist:
            return []

        params: Dict[str, Any] = {"action": "browse", "artistname": artist}
        if album:
            params["groupname"] = album
        if year:
            params["year"] = year
        if release_type:
            params["releasetype"] = release_type
        if media:
            params["media"] = media

        payload = await self._request(params)
        results = payload.get("response", {}).get("results", [])
        return [self._map_result(result) for result in results]

    async def _request(self, params: Dict[str, Any]) -> Dict[str, Any]:
        url = f"{self.base_url}/ajax.php"
        headers = self._get_headers()
        timeout = aiohttp.ClientTimeout(total=self.timeout)

        async with self._semaphore:
            await self._enforce_interval()
            async with aiohttp.ClientSession(headers=headers, timeout=timeout) as session:
                async with session.get(url, params=params) as response:
                    if response.status >= 400:
                        text = await response.text()
                        raise aiohttp.ClientResponseError(
                            request_info=response.request_info,
                            history=response.history,
                            status=response.status,
                            message=text,
                            headers=response.headers,
                        )
                    data = await response.json()
                    self._last_request = time.monotonic()
                    return data

    async def _enforce_interval(self) -> None:
        now = time.monotonic()
        wait = self._min_interval - (now - self._last_request)
        if wait > 0:
            await asyncio.sleep(wait)

    def _get_headers(self) -> Dict[str, str]:
        auth = self.tracker.api_key
        if self.tracker.name.lower() != "red":
            auth = f"token {self.tracker.api_key}"
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
