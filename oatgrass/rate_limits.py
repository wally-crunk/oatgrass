"""Central API rate-limit settings and shared Gazelle limiter state."""

from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass, field

# Gazelle trackers (RED/OPS): minimum interval between calls to the same server.
GAZELLE_MIN_INTERVAL_SECONDS = 2.0
GAZELLE_WAIT_LOG_THRESHOLD_SECONDS = 1.75
GAZELLE_RATE_LIMIT_WINDOW_SECONDS = 10.0

# Discogs API: conservative spacing used by existing ANV lookup flow.
DISCOGS_MIN_INTERVAL_SECONDS = 2.4
DISCOGS_MAX_CONCURRENT_REQUESTS = 25


_GAZELLE_TRACKER_REQUEST_LIMITS: dict[tuple[str, str], int] = {
    ("red", "api_key"): 10,
    ("red", "standard"): 5,
    ("ops", "api_key"): 5,
}


@dataclass
class _GazelleBucket:
    lock: asyncio.Lock
    last_request_started: float = 0.0
    request_starts: deque[float] = field(default_factory=deque)


_gazelle_buckets: dict[tuple[str, str], _GazelleBucket] = {}
_gazelle_buckets_lock = asyncio.Lock()


def _normalize_server_key(base_url: str) -> str:
    return base_url.rstrip("/").lower()


def _normalize_auth_mode(auth_mode: str) -> str:
    return (auth_mode or "api_key").strip().lower()


def _resolve_tracker_request_limit(tracker_name: str | None, auth_mode: str) -> int | None:
    if not tracker_name:
        return None
    return _GAZELLE_TRACKER_REQUEST_LIMITS.get((tracker_name.strip().lower(), _normalize_auth_mode(auth_mode)))


def _prune_window(bucket: _GazelleBucket, now: float, window_seconds: float) -> None:
    if window_seconds <= 0:
        bucket.request_starts.clear()
        return
    cutoff = now - window_seconds
    while bucket.request_starts and bucket.request_starts[0] <= cutoff:
        bucket.request_starts.popleft()


async def _get_or_create_bucket(base_url: str, auth_mode: str) -> _GazelleBucket:
    key = (_normalize_server_key(base_url), _normalize_auth_mode(auth_mode))
    bucket = _gazelle_buckets.get(key)
    if bucket is not None:
        return bucket

    async with _gazelle_buckets_lock:
        bucket = _gazelle_buckets.get(key)
        if bucket is None:
            bucket = _GazelleBucket(lock=asyncio.Lock())
            _gazelle_buckets[key] = bucket
        return bucket


async def enforce_gazelle_min_interval(
    base_url: str,
    min_interval_seconds: float = GAZELLE_MIN_INTERVAL_SECONDS,
    tracker_name: str | None = None,
    auth_mode: str = "api_key",
) -> float:
    """
    Enforce shared per-server Gazelle spacing.

    Returns the wait time applied (seconds).
    """
    bucket = await _get_or_create_bucket(base_url, auth_mode)
    request_limit = _resolve_tracker_request_limit(tracker_name, auth_mode)
    window_seconds = GAZELLE_RATE_LIMIT_WINDOW_SECONDS if request_limit else 0.0
    async with bucket.lock:
        now = time.monotonic()
        effective_min_interval = max(0.0, float(min_interval_seconds))
        min_wait = effective_min_interval - (now - bucket.last_request_started)
        _prune_window(bucket, now, window_seconds)
        window_wait = 0.0
        if request_limit and len(bucket.request_starts) >= request_limit:
            window_wait = bucket.request_starts[0] + window_seconds - now
        wait = max(min_wait, window_wait, 0.0)
        if wait > 0:
            await asyncio.sleep(wait)
            now = time.monotonic()
            _prune_window(bucket, now, window_seconds)
        bucket.last_request_started = now
        if request_limit:
            bucket.request_starts.append(now)
        return wait


def _reset_gazelle_rate_limits_for_tests() -> None:
    """Test helper to clear shared limiter state."""
    _gazelle_buckets.clear()
