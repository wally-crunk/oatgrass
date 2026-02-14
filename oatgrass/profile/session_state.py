"""In-memory session cache for profile-list data."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from oatgrass.profile.retriever import ListType, ProfileTorrent


@dataclass
class ProfileSessionState:
    tracker_key: str | None = None
    fetched_at: datetime | None = None
    lists: dict[ListType, list[ProfileTorrent]] = field(default_factory=dict)

    def set_snapshot(self, tracker_key: str, lists: dict[ListType, list[ProfileTorrent]]) -> None:
        self.tracker_key = tracker_key
        self.lists = {k: list(v) for k, v in lists.items()}
        self.fetched_at = datetime.now()

    def is_empty(self) -> bool:
        return not self.lists

    def has_list(self, tracker_key: str, list_type: ListType) -> bool:
        if self.tracker_key != tracker_key:
            return False
        return bool(self.lists.get(list_type))
