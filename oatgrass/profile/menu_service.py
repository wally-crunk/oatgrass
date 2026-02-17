"""Service helpers for profile-list menu operations."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterable

from aiohttp import ClientResponseError

from rich.console import Console
from rich.table import Table

from oatgrass import logger
from oatgrass.config import TrackerConfig
from oatgrass.profile.retriever import (
    ListType,
    ProfileRetriever,
    ProfileTorrent,
)
from oatgrass.tracker_profile import resolve_tracker_profile


@dataclass(frozen=True)
class ProfileListSummary:
    list_type: ListType
    count: int
    total_size: int
    first_three: tuple[str, str, str]


def _safe_size(entry: ProfileTorrent) -> int:
    for key in ("torrentSize", "size", "torrent_size"):
        value = entry.metadata.get(key)
        if value in (None, ""):
            continue
        if isinstance(value, str):
            value = value.replace(",", "")
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return 0


def build_profile_summary(list_type: ListType, entries: Iterable[ProfileTorrent]) -> ProfileListSummary:
    items = list(entries)
    preview = tuple((item.group_name or "(unnamed)") for item in items[:3])
    while len(preview) < 3:
        preview = (*preview, "-")
    total_size = sum(_safe_size(item) for item in items)
    return ProfileListSummary(
        list_type=list_type,
        count=len(items),
        total_size=total_size,
        first_three=preview[:3],
    )


def render_profile_summaries(console: Console, tracker_name: str, summaries: Iterable[ProfileListSummary]) -> None:
    table = Table(title=f"Profile List Summary ({tracker_name})")
    table.add_column("List", style="cyan", no_wrap=True)
    table.add_column("Count", style="green", justify="right")
    table.add_column("Total Size (bytes)", style="cyan", justify="right")
    table.add_column("First Three Entries", style="yellow")
    for summary in summaries:
        first_three = " | ".join(summary.first_three)
        table.add_row(summary.list_type, f"{summary.count:,}", f"{summary.total_size:,}", first_three)
    console.print(table)


class ProfileMenuService:
    """Fetch all profile lists and derive summary information."""

    def __init__(
        self,
        tracker: TrackerConfig,
        retriever_factory: Callable[[TrackerConfig], ProfileRetriever] | None = None,
    ) -> None:
        self.tracker = tracker
        self._retriever_factory = retriever_factory or ProfileRetriever
        self._retriever = self._retriever_factory(tracker)

    async def fetch_all_lists(self) -> dict[ListType, list[ProfileTorrent]]:
        list_types = resolve_tracker_profile(self.tracker.name).list_types
        total_tasks = len(list_types)
        results: dict[ListType, list[ProfileTorrent]] = {}
        for idx, list_type in enumerate(list_types, start=1):
            try:
                results[list_type] = await self._retriever.fetch(
                    list_type,
                    task_index=idx,
                    task_total=total_tasks,
                )
            except ClientResponseError as exc:
                tracker_name = self.tracker.name.upper()
                message = exc.message or str(exc)
                logger.warning(
                    f"{tracker_name} rejected the {list_type} list ({exc.status}): {message}"
                )
                results[list_type] = []
        return results

    async def close(self) -> None:
        await self._retriever.close()
