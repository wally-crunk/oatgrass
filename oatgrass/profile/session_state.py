"""In-memory session cache for profile-list data."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from oatgrass.profile.retriever import ListType, ProfileTorrent


@dataclass
class ProfileSessionState:
    tracker_key: str | None = None
    fetched_at: datetime | None = None
    lists: dict[tuple[str, ListType], list[ProfileTorrent]] = field(default_factory=dict)

    def set_snapshot(self, tracker_key: str, lists: dict[ListType, list[ProfileTorrent]]) -> None:
        normalized_tracker = tracker_key.lower()
        self.tracker_key = tracker_key
        stale_keys = [key for key in self.lists if key[0] == normalized_tracker]
        for key in stale_keys:
            self.lists.pop(key, None)
        for list_type, entries in lists.items():
            self.lists[(normalized_tracker, list_type)] = list(entries)
        self.fetched_at = datetime.now()

    def is_empty(self) -> bool:
        return not self.lists

    def has_list(self, tracker_key: str, list_type: ListType) -> bool:
        return bool(self.lists.get((tracker_key.lower(), list_type)))

    def get_list(self, tracker_key: str, list_type: ListType) -> list[ProfileTorrent]:
        return list(self.lists.get((tracker_key.lower(), list_type), []))
