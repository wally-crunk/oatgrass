"""Helper utilities for fetching tracker profile torrent lists."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Callable, List, Literal, Mapping

import aiohttp
from aiohttp import ClientConnectionError, ClientResponseError

from oatgrass import logger
from oatgrass.config import TrackerConfig
from oatgrass.search.gazelle_client import GazelleServiceAdapter

ListType = Literal["snatched", "uploaded", "downloaded", "seeding", "leeching", "snatched-unseeded", "uploaded-unseeded"]
ALL_LIST_TYPES: tuple[ListType, ...] = (
    "snatched",
    "uploaded",
    "downloaded",
    "seeding",
    "leeching",
    "snatched-unseeded",
    "uploaded-unseeded",
)
ALLOWED_LIST_TYPES = set(ALL_LIST_TYPES)
LIST_TYPE_LABELS: dict[ListType, str] = {
    "snatched": "Snatched",
    "uploaded": "Uploaded",
    "downloaded": "Downloaded",
    "seeding": "Seeding",
    "leeching": "Leeching",
    "snatched-unseeded": "Snatched (Unseeded)",
    "uploaded-unseeded": "Uploaded (Unseeded)",
}
def format_list_label(list_type: ListType) -> str:
    return LIST_TYPE_LABELS.get(list_type, list_type.replace("-", " ").title())


DEFAULT_PAGE_SIZE = 500
MAX_MALFORMED_NUMERIC = 2
MAX_PAGE_RETRIES = 3


class MalformedProfileEntryError(ValueError):
    """Raised when a profile row contains malformed numeric IDs."""


@dataclass(frozen=True)
class ProfileTorrent:
    """Represents a single torrent entry from a profile list."""

    tracker: str
    list_type: ListType
    group_id: int | None
    torrent_id: int | None
    group_name: str | None
    artist_name: str | None
    artist_id: int | None
    media: str | None
    format: str | None
    encoding: str | None
    metadata: Mapping[str, Any]


class ProfileRetriever:
    """Fetches tracker-supported profile list entries using a generic row parser."""

    def __init__(
        self,
        tracker: TrackerConfig,
        service_factory: Callable[[TrackerConfig], GazelleServiceAdapter] | None = None,
    ) -> None:
        self.tracker = tracker
        self._service_factory = service_factory or GazelleServiceAdapter
        self._service: GazelleServiceAdapter | None = None
        self._user_id: int | None = None

    async def _ensure_service(self) -> GazelleServiceAdapter:
        if self._service is None:
            self._service = self._service_factory(self.tracker)
        return self._service

    async def _ensure_user_id(self) -> int:
        if self._user_id is not None:
            return self._user_id
        service = await self._ensure_service()
        profile = await service.get_index()
        user_info = profile.get("response", {})
        user_id = user_info.get("id")
        if not user_id:
            raise ValueError("Failed to discover user ID from tracker profile response")
        self._user_id = int(user_id)
        return self._user_id

    async def fetch(
        self,
        list_type: ListType,
        limit: int = DEFAULT_PAGE_SIZE,
        offset: int = 0,
        max_items: int | None = None,
        task_index: int = 1,
        task_total: int = 1,
    ) -> List[ProfileTorrent]:
        """Return profile entries for the requested list type."""
        if list_type not in ALLOWED_LIST_TYPES:
            raise ValueError(f"Unsupported list type '{list_type}'")
        if limit <= 0:
            raise ValueError("limit must be greater than 0")
        if max_items is not None and max_items <= 0:
            raise ValueError("max_items must be greater than 0")
        if offset < 0:
            raise ValueError("offset must be >= 0")

        user_id = await self._ensure_user_id()
        service = await self._ensure_service()
        accepted_entries: list[ProfileTorrent] = []
        malformed_numeric = 0
        possible_non_music = 0
        page = 0
        current_offset = offset
        known_total_pages: int | None = None
        known_total_items: int | None = None

        while True:
            if max_items is not None and len(accepted_entries) >= max_items:
                break
            page += 1
            page_limit = limit if max_items is None else min(limit, max_items - len(accepted_entries))
            page_progress = f"[Page {page}]" if known_total_pages is None else f"[Page {page} of {known_total_pages}]"
            logger.info(
                f"[Task {task_index} of {task_total}] {page_progress} "
                f"Fetching {list_type} (offset={current_offset}, limit={page_limit})"
            )
            data = await self._request_page_with_backoff(
                service,
                user_id=user_id,
                page=page,
                list_type=list_type,
                limit=page_limit,
                offset=current_offset,
            )
            response = data.get("response")
            if not isinstance(response, Mapping):
                raise ValueError("Malformed response payload: missing 'response' object")
            raw_entries = response.get(list_type)
            if raw_entries is None:
                raise ValueError(f"Malformed response payload: missing '{list_type}' list")
            if not isinstance(raw_entries, list):
                raise ValueError(f"Malformed response payload: '{list_type}' is not a list")

            if known_total_pages is None:
                total_value = response.get("total")
                try:
                    total_items = int(total_value) if total_value is not None else None
                except (TypeError, ValueError):
                    total_items = None
                if total_items is not None and total_items >= 0:
                    known_total_items = min(total_items, max_items) if max_items is not None else total_items
                    known_total_pages = max(1, (known_total_items + limit - 1) // limit)

            if not raw_entries:
                break

            for raw_entry in raw_entries:
                if not isinstance(raw_entry, Mapping):
                    possible_non_music += 1
                    logger.warning("Skipping possible non-music profile row (not a mapping)")
                    continue
                try:
                    mapped = self._map_entry(raw_entry, list_type, self.tracker.name)
                except MalformedProfileEntryError as exc:
                    malformed_numeric += 1
                    logger.warning(str(exc))
                    if malformed_numeric >= MAX_MALFORMED_NUMERIC:
                        raise ValueError(
                            f"Aborting profile fetch for {list_type}: "
                            f"encountered {malformed_numeric} malformed numeric rows"
                        ) from exc
                    continue
                if mapped is None:
                    possible_non_music += 1
                    logger.warning("Skipping possible non-music profile row (missing IDs)")
                    continue
                accepted_entries.append(mapped)
                if max_items is not None and len(accepted_entries) >= max_items:
                    break

            current_offset += len(raw_entries)
            if len(raw_entries) < page_limit:
                break

        if max_items is not None and len(accepted_entries) >= max_items:
            logger.warning(
                f"{list_type} cache capped at {max_items:,} items; "
                "results may be partial for this list"
            )

        logger.info(
            f"[Task {task_index} of {task_total}] [Page {page}] Completed {list_type}: "
            f"accepted={len(accepted_entries)}, possible_non_music={possible_non_music}, "
            f"malformed_numeric={malformed_numeric}, "
            f"reported_total={known_total_items if known_total_items is not None else 'unknown'}"
        )
        return accepted_entries

    @staticmethod
    def _map_entry(
        entry: Mapping[str, Any], list_type: ListType, tracker_name: str
    ) -> ProfileTorrent | None:
        def _parse_int(value: Any, field: str) -> int | None:
            if value is None or value == "":
                return None
            try:
                return int(value)
            except (TypeError, ValueError):
                raise MalformedProfileEntryError(
                    f"Malformed profile row: field '{field}' expected numeric value, got {value!r}"
                )

        group_id = _parse_int(entry.get("groupId") or entry.get("group_id"), "groupId")
        torrent_id = _parse_int(entry.get("torrentId") or entry.get("torrent_id"), "torrentId")
        artist_id = _parse_int(entry.get("artistId") or entry.get("artist_id"), "artistId")

        if group_id is None and torrent_id is None:
            return None

        return ProfileTorrent(
            tracker=tracker_name,
            list_type=list_type,
            group_id=group_id,
            torrent_id=torrent_id,
            group_name=entry.get("name") or entry.get("groupName"),
            artist_name=entry.get("artistName"),
            artist_id=artist_id,
            media=entry.get("media"),
            format=entry.get("format"),
            encoding=entry.get("encoding"),
            metadata=dict(entry),
        )

    async def _request_page_with_backoff(
        self,
        service: GazelleServiceAdapter,
        *,
        user_id: int,
        page: int,
        list_type: ListType,
        limit: int,
        offset: int,
    ) -> Mapping[str, Any]:
        """Issue one page request with exponential backoff retries."""
        for attempt in range(1, MAX_PAGE_RETRIES + 1):
            try:
                return await service.get_user_torrents(
                    list_type=list_type,
                    user_id=user_id,
                    limit=limit,
                    offset=offset,
                )
            except ClientResponseError:
                # Do not retry deterministic request errors (e.g., bad params).
                raise
            except (asyncio.TimeoutError, ClientConnectionError, aiohttp.ServerTimeoutError):
                if attempt >= MAX_PAGE_RETRIES:
                    raise
                delay = 2 ** attempt
                logger.warning(
                    f"{self.tracker.name.upper()} timeout while fetching {list_type} page {page}; "
                    f"retrying in {delay}s (attempt {attempt}/{MAX_PAGE_RETRIES})"
                )
                await asyncio.sleep(delay)
        raise RuntimeError("Unreachable retry exit")

    async def close(self) -> None:
        """Close the underlying service."""
        if self._service is not None:
            await self._service.close()
            self._service = None
