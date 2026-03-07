"""Central tracker capability and policy definitions."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TrackerProfile:
    list_types: tuple[str, ...]
    request_limit: int | None
    token_auth: bool = False
    group_policy_fields_complete: bool = False


_TRACKER_PROFILES: dict[str, TrackerProfile] = {
    "ops": TrackerProfile(
        list_types=("snatched", "uploaded", "seeding", "leeching"),
        request_limit=5,
        token_auth=True,
        group_policy_fields_complete=True,
    ),
    "red": TrackerProfile(
        list_types=("seeding", "leeching", "uploaded", "snatched"),
        request_limit=10,
        token_auth=False,
        # RED torrentgroup often omits trumpable_reasons/logChecksum; policy mode
        # may need action=torrent enrichment until RED API parity changes.
        group_policy_fields_complete=False,
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


def tracker_needs_policy_enrichment(tracker_name: str | None) -> bool:
    """Return True when policy fields are known incomplete on torrentgroup payloads.

    Unknown trackers default to False so callers don't crash in tests with synthetic
    tracker names (for example OPS2).
    """
    profile = _TRACKER_PROFILES.get(_normalize_tracker_name(tracker_name))
    return profile is not None and not profile.group_policy_fields_complete
