"""Tracker-selection helpers for profile menu flows."""

from __future__ import annotations

from oatgrass.config import OatgrassConfig, TrackerConfig


def configured_profile_trackers(config: OatgrassConfig) -> list[tuple[str, TrackerConfig]]:
    return [(key, tracker) for key, tracker in config.trackers.items() if tracker.api_key]


def resolve_profile_tracker(
    config: OatgrassConfig,
    tracker_key: str | None = None,
) -> tuple[str, TrackerConfig]:
    trackers = configured_profile_trackers(config)
    if not trackers:
        raise ValueError("No configured tracker with API key found.")
    if tracker_key is None:
        return trackers[0]
    for key, tracker in trackers:
        if key.lower() == tracker_key.lower():
            return key, tracker
    raise ValueError(f"Tracker '{tracker_key}' is not configured with an API key.")
