"""Tracker-specific Authorization header formatting."""

from __future__ import annotations

from oatgrass.tracker_profile import resolve_tracker_profile


def build_tracker_auth_header(tracker_name: str, api_key: str) -> str:
    """
    Return the Authorization header value for a tracker.

    Keep rules explicit per tracker to avoid assuming all Gazelle variants
    share identical auth behavior.
    """
    normalized = (tracker_name or "").strip().lower()
    key = (api_key or "").strip()
    profile = resolve_tracker_profile(normalized)
    if profile.token_auth:
        return key if key.lower().startswith("token ") else f"token {key}"
    return key
