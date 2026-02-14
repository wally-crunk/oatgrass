"""Tracker-specific Authorization header formatting."""

from __future__ import annotations

_TOKEN_AUTH_TRACKERS = {"ops"}


def build_tracker_auth_header(tracker_name: str, api_key: str) -> str:
    """
    Return the Authorization header value for a tracker.

    Keep rules explicit per tracker to avoid assuming all Gazelle variants
    share identical auth behavior.
    """
    normalized = (tracker_name or "").strip().lower()
    key = (api_key or "").strip()
    if normalized in _TOKEN_AUTH_TRACKERS:
        return key if key.lower().startswith("token ") else f"token {key}"
    return key
