from __future__ import annotations

from urllib.parse import urlparse

from oatgrass.config import TrackerConfig


def is_url(target: str) -> bool:
    return target.startswith("http://") or target.startswith("https://")


def is_group_url(path: str) -> bool:
    lowered = path.lower()
    return "torrents.php" in lowered or "torrentgroup" in lowered


def cross_upload_url(tracker: TrackerConfig, group_id: int) -> str:
    return f"{tracker.url.rstrip('/')}/torrents.php?id={group_id}"


def find_tracker_by_url(trackers: dict[str, TrackerConfig], collage_url: str) -> tuple[str, TrackerConfig]:
    parsed = urlparse(collage_url)
    for key, tracker in trackers.items():
        tracker_netloc = urlparse(tracker.url).netloc
        normalized_target = parsed.netloc.lower()
        if tracker_netloc and tracker_netloc.lower() in normalized_target:
            return key, tracker
        normalized_base = tracker.url.rstrip("/").lower()
        if collage_url.lower().startswith(normalized_base):
            return key, tracker
    raise ValueError("Collage URL does not match any configured tracker")
