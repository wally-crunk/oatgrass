"""Minimal Gazelle client adapter reusing MAESTRO-style service patterns."""

from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, Optional, Sequence

import aiohttp

from oatgrass.config import TrackerConfig
from oatgrass.search.protocols import GazelleClient
from oatgrass.search.types import GazelleSearchResult
from oatgrass import logger
from oatgrass.__version__ import __version__

DEFAULT_USER_AGENT = f"Oatgrass/{__version__}"


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

    async def _request(self, params: Dict[str, Any]) -> Dict[str, Any]:
        url = f"{self.base_url}/ajax.php"
        headers = self._get_headers()
        timeout = aiohttp.ClientTimeout(total=self.timeout)
        
        # Debug logging: API request
        logger.get_logger().api_request("GET", url, params)
        
        max_retries = 3
        request_start = time.time()

        async with self._semaphore:
            await self._enforce_interval()
            
            for attempt in range(max_retries):
                try:
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
                            elapsed_ms = (time.time() - request_start) * 1000
                            
                            # Debug logging: API response
                            logger.get_logger().api_response(response.status, data, elapsed_ms)
                            
                            self._last_request = time.monotonic()
                            return data
                except (asyncio.TimeoutError, aiohttp.ClientError) as e:
                    if attempt < max_retries - 1:
                        delay = 2 ** (attempt + 1)  # 2, 4, 8 seconds
                        logger.get_logger().api_retry(self.tracker.name.upper(), attempt + 1, max_retries, delay)
                        await asyncio.sleep(delay)
                    else:
                        logger.get_logger().api_failed(self.tracker.name.upper(), max_retries)
                        raise

    async def _enforce_interval(self) -> None:
        now = time.monotonic()
        wait = self._min_interval - (now - self._last_request)
        if wait > 0:
            if wait > 1.0:  # Only log if waiting more than 1 second
                logger.get_logger().api_wait(self.tracker.name.upper(), wait)
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
    
    async def close(self) -> None:
        """Close any open connections."""
        pass  # No persistent connections to close
