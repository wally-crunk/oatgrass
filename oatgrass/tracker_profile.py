"""Central tracker capability and policy definitions."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TrackerProfile:
    list_types: tuple[str, ...]
    request_limit: int | None
    token_auth: bool = False


_TRACKER_PROFILES: dict[str, TrackerProfile] = {
    "ops": TrackerProfile(
        list_types=("snatched", "uploaded", "seeding", "leeching"),
        request_limit=5,
        token_auth=True,
    ),
    "red": TrackerProfile(
        list_types=("seeding", "leeching", "uploaded", "snatched"),
        request_limit=10,
        token_auth=False,
    ),
}


def _normalize_tracker_name(tracker_name: str | None) -> str:
    return (tracker_name or "").strip().lower()


def resolve_tracker_profile(tracker_name: str | None) -> TrackerProfile:
    normalized = _normalize_tracker_name(tracker_name)
    profile = _TRACKER_PROFILES.get(normalized)
    if profile is not None:
        return profile
    supported = ", ".join(name.upper() for name in sorted(_TRACKER_PROFILES))
    raise ValueError(
        f"Unsupported tracker '{tracker_name}'. Supported trackers: {supported}."
    )
