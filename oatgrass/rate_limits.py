"""Central API rate-limit settings and shared Gazelle limiter state."""

from __future__ import annotations

import asyncio
import os
import tempfile
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from oatgrass import spigot
from oatgrass import logger
from oatgrass.progress_timing import format_progress_bar
from oatgrass.tracker_profile import (
    resolve_action_rate_limit,
    resolve_estimated_seconds_per_request,
    resolve_pacing_limits,
    resolve_tracker_profile,
)

# Gazelle trackers (RED/OPS): minimum interval between calls to the same server.
GAZELLE_MIN_INTERVAL_SECONDS = 2.0
GAZELLE_WAIT_LOG_THRESHOLD_SECONDS = 1.75
GAZELLE_PACING_STATUS_THRESHOLD_SECONDS = 6.0
GAZELLE_RATE_LIMIT_WINDOW_SECONDS = 10.0

# Discogs API: conservative spacing used by existing ANV lookup flow.
DISCOGS_MIN_INTERVAL_SECONDS = 2.4
DISCOGS_MAX_CONCURRENT_REQUESTS = 25
SLOW_MODE_SAFETY_MARGIN = 0.05


@dataclass
class _GazelleBucket:
    lock: asyncio.Lock
    last_request_started: float = 0.0
    request_starts: deque[float] = field(default_factory=deque)


_gazelle_buckets: dict[str, _GazelleBucket] = {}
_gazelle_buckets_lock = asyncio.Lock()
_slow_mode_concurrent_runs: int | None = None
_spigot_store: spigot.SpigotStore | None = None
_spigot_participant_counts: dict[tuple[str, str, str], int] = {}
# Coarser sibling of _spigot_participant_counts, keyed by tracker name only --
# display code (e.g. a collage-pagination progress estimate) doesn't have a
# server_key/account_identity scope on hand and only needs a rough "how many
# sessions were last seen sharing this tracker's pacing" figure, not the
# precise per-scope count the reservation system itself uses to decide waits.
_last_known_participant_count_by_tracker: dict[str, int] = {}


_slow_mode_notice_emitted = False


def set_slow_mode_concurrent_runs(concurrent_runs: int | None) -> None:
    """Set the process-wide slow-mode target run count."""
    global _slow_mode_concurrent_runs, _slow_mode_notice_emitted
    _slow_mode_notice_emitted = False
    if concurrent_runs is None:
        _slow_mode_concurrent_runs = None
        spigot.set_slow_declaration(None)
        return
    if int(concurrent_runs) < 2:
        raise ValueError("slow mode requires at least 2 concurrent runs")
    _slow_mode_concurrent_runs = int(concurrent_runs)
    spigot.set_slow_declaration(_slow_mode_concurrent_runs)


def get_slow_mode_concurrent_runs() -> int | None:
    """Return the active slow-mode target run count."""
    return _slow_mode_concurrent_runs


def get_slow_mode_multiplier() -> float:
    """Return the active base pacing multiplier."""
    concurrent_runs = get_slow_mode_concurrent_runs()
    if concurrent_runs is None:
        return 1.0
    return float(concurrent_runs) + SLOW_MODE_SAFETY_MARGIN


def get_effective_interval(base_interval_seconds: float) -> float:
    """Return the active pacing interval for a base interval."""
    return max(0.0, float(base_interval_seconds)) * get_slow_mode_multiplier()


def get_last_known_participant_count(tracker_name: str) -> int:
    """Last participant count observed for this tracker across any scope, or
    1 (assume alone) before any reservation has been made yet this process."""
    return _last_known_participant_count_by_tracker.get(tracker_name.upper(), 1)


def estimate_pacing_wait_seconds(
    tracker_name: str,
    action: str | None,
    remaining_requests: int,
    active_participant_count: int | None = None,
) -> float | None:
    """Rough projected wait time for `remaining_requests` more calls to this
    tracker/action, from the pacing scheme's own rule -- a display estimate,
    not a scheduling decision. None when the scheme has no rate limit to
    project from (e.g. an unthrottled action)."""
    per_request = resolve_estimated_seconds_per_request(tracker_name, action)
    if per_request is None:
        return None
    if active_participant_count is None:
        active_participant_count = get_last_known_participant_count(tracker_name)
    return get_effective_interval(per_request) * max(1, active_participant_count) * max(0, remaining_requests)


def compute_throttle_retry_delay(
    *,
    retry_after: str | None,
    effective_min_interval: float,
    fallback_delay: float,
) -> float:
    """Choose a conservative 429 retry delay."""
    try:
        retry_after_seconds = float(retry_after) if retry_after else 0.0
    except (TypeError, ValueError):
        retry_after_seconds = 0.0
    floor = max(0.0, float(effective_min_interval))
    if retry_after_seconds > 0:
        return max(retry_after_seconds + 1.0, floor)
    return max(max(0.0, float(fallback_delay)), floor)


def describe_slow_mode() -> str | None:
    """Return a user-facing description of active slow mode."""
    concurrent_runs = get_slow_mode_concurrent_runs()
    if concurrent_runs is None:
        return None
    multiplier = get_slow_mode_multiplier()
    multiplier_text = str(int(multiplier)) if multiplier.is_integer() else f"{multiplier:.2f}"
    return (
        f"Slow mode active: pacing for {concurrent_runs} concurrent runs "
        f"(x{multiplier_text} interval multiplier)."
    )


def describe_slow_mode_once() -> str | None:
    """Like describe_slow_mode(), but only the first call in a session returns it.

    This is for screen-only, no-log-file contexts that redraw repeatedly
    (the interactive CLI menu loop, --verify) where re-announcing on every
    redraw would be noise. It is NOT for search-workflow startup: each
    workflow run gets its own fresh log file and should always document its
    own settings there regardless of what a prior menu screen already
    showed -- consuming this flag from a workflow would make the log file's
    coverage depend on call order (e.g. an earlier menu redraw silently
    starving the very log line this was meant to guarantee). Workflows
    should call describe_slow_mode() directly instead. Resets whenever slow
    mode is (re)configured via set_slow_mode_concurrent_runs().
    """
    global _slow_mode_notice_emitted
    if _slow_mode_notice_emitted:
        return None
    description = describe_slow_mode()
    if description is None:
        return None
    _slow_mode_notice_emitted = True
    return description


def _normalize_server_key(base_url: str) -> str:
    return spigot.canonical_host(base_url)


def _resolve_tracker_request_limit(tracker_name: str) -> int | None:
    return resolve_tracker_profile(tracker_name).request_limit


def _get_spigot_store() -> spigot.SpigotStore:
    global _spigot_store
    if _spigot_store is None:
        _spigot_store = spigot.SpigotStore()
    return _spigot_store


class PacingCoordinationUnavailable(RuntimeError):
    """Raised when local cross-process pacing coordination cannot continue safely.

    Wraps spigot.SpigotError with end-user-safe wording: SPIGOT is an
    internal-only codename never explained to end users, and the raw
    sqlite3 driver text isn't actionable on screen -- both stay in the log
    file (via logger.debug) instead of leaking into the default message.
    """


def _translate_spigot_error(exc: spigot.SpigotError) -> PacingCoordinationUnavailable:
    logger.get_logger().debug(f"SPIGOT pacing coordination failed: {exc}")
    return PacingCoordinationUnavailable(
        "Local pacing coordination is unavailable; cannot continue safely. "
        "(run with --debug for details)"
    )


def _prune_window(bucket: _GazelleBucket, now: float, window_seconds: float) -> None:
    if window_seconds <= 0:
        bucket.request_starts.clear()
        return
    cutoff = now - window_seconds
    while bucket.request_starts and bucket.request_starts[0] <= cutoff:
        bucket.request_starts.popleft()


async def _get_or_create_bucket(base_url: str) -> _GazelleBucket:
    key = _normalize_server_key(base_url)
    bucket = _gazelle_buckets.get(key)
    if bucket is not None:
        return bucket

    async with _gazelle_buckets_lock:
        bucket = _gazelle_buckets.get(key)
        if bucket is None:
            bucket = _GazelleBucket(lock=asyncio.Lock())
            _gazelle_buckets[key] = bucket
        return bucket


def _sleep_chunk(wait_seconds: float) -> float:
    wait = max(0.0, float(wait_seconds))
    return min(wait, min(2.0, max(0.25, wait / 4.0)))


def _format_whole_seconds(seconds: float) -> str:
    whole_seconds = max(0, int(round(float(seconds))))
    noun = "second" if whole_seconds == 1 else "seconds"
    return f"{whole_seconds} {noun}"


def _format_pacing_subject(tracker_name: str, action: str | None) -> str:
    if action:
        return f"{tracker_name.upper()} {action}"
    return f"{tracker_name.upper()} API"


def _format_pacing_status(
    *,
    tracker_name: str,
    action: str | None,
    wait_seconds: float,
    active_participant_count: int,
    wait_total_seconds: float | None = None,
) -> str:
    subject = _format_pacing_subject(tracker_name, action)
    detail = (
        f"{active_participant_count} sessions sharing"
        if active_participant_count != 1
        else "1 session"
    )
    bar = ""
    if wait_total_seconds and wait_total_seconds > 0:
        elapsed_fraction = 1.0 - (max(0.0, wait_seconds) / wait_total_seconds)
        # Narrower than the task line's bar, and "Ns" not "N seconds" --
        # this line shares its 80-column budget with a lot of other wording
        # (tracker/action, participant count) that the task line doesn't
        # have to carry.
        bar = f" {format_progress_bar(elapsed_fraction, width=15)}"
    whole_seconds = max(0, int(round(float(wait_seconds))))
    return f"[pacing] {subject} wait: {whole_seconds}s left; {detail}{bar}"


def _emit_pacing_participant_change(
    *,
    tracker_name: str,
    scope: spigot.SpigotScope,
    active_participant_count: int,
) -> None:
    key = (scope.tracker_name, scope.server_key, scope.account_identity)
    previous = _spigot_participant_counts.get(key)
    _spigot_participant_counts[key] = active_participant_count
    _last_known_participant_count_by_tracker[tracker_name.upper()] = active_participant_count
    if previous is None or previous == active_participant_count:
        return
    if previous <= 1 and active_participant_count <= 1:
        return
    noun = "session" if active_participant_count == 1 else "sessions"
    verb = "is" if active_participant_count == 1 else "are"
    logger.get_logger().progress(
        f"[pacing] {active_participant_count} OATGRASS {noun} {verb} sharing "
        f"{tracker_name.upper()} pacing."
    )


async def enforce_gazelle_min_interval(
    base_url: str,
    tracker_name: str,
    min_interval_seconds: float = GAZELLE_MIN_INTERVAL_SECONDS,
    action: str | None = None,
    account_identity: str | None = None,
) -> float:
    """
    Enforce shared per-server Gazelle spacing.

    Returns the wait time applied (seconds).
    """
    bucket = await _get_or_create_bucket(base_url)
    scheme_id, pacing_limits = resolve_pacing_limits(tracker_name, action)
    primary_limit = pacing_limits[0] if pacing_limits else None
    rule = primary_limit.rule if primary_limit else None
    request_limit = rule.requests if rule else None
    window_seconds = rule.seconds if rule else 0.0
    bucket_name = primary_limit.bucket_name if primary_limit else "default"
    scope = spigot.make_scope(
        base_url=base_url,
        tracker_name=tracker_name,
        account_id=account_identity,
    )
    async with bucket.lock:
        total_wait = 0.0
        wait_started = False
        status_started = False
        status_wait_total: float | None = None
        log = logger.get_logger()
        while True:
            try:
                reservation = _get_spigot_store().reserve_request(
                    scope=scope,
                    bucket_name=bucket_name,
                    action=action,
                    requests=request_limit or 1,
                    seconds=window_seconds,
                    min_interval_seconds=min_interval_seconds,
                    scheme_id=scheme_id,
                    now=time.monotonic(),
                )
            except spigot.SpigotError as exc:
                raise _translate_spigot_error(exc) from exc
            _emit_pacing_participant_change(
                tracker_name=tracker_name,
                scope=scope,
                active_participant_count=reservation.active_participant_count,
            )
            wait = reservation.wait_seconds
            if wait <= 0:
                break
            show_status = bool(getattr(log, "show_pacing_status", True))
            if show_status and (status_started or total_wait + wait > GAZELLE_PACING_STATUS_THRESHOLD_SECONDS):
                if not status_started:
                    status_wait_total = wait
                status_started = True
                log.status(
                    _format_pacing_status(
                        tracker_name=tracker_name,
                        action=action,
                        wait_seconds=wait,
                        active_participant_count=reservation.active_participant_count,
                        wait_total_seconds=status_wait_total,
                    )
                )
            if not wait_started:
                wait_started = True
                target = (
                    f"{reservation.reserved_for_monotonic:.3f}"
                    if reservation.reserved_for_monotonic is not None
                    else "unknown"
                )
                log.debug(
                    f"Rate limiting detail: waiting {wait:.3f}s before next {tracker_name.upper()} API call "
                    "using shared SPIGOT pacing; "
                    f"scope={scope.server_key}/{scope.account_identity} "
                    f"scheme={scheme_id} bucket={bucket_name} action={action or '-'} "
                    f"participants={reservation.active_participant_count} "
                    f"reserved_for={target}"
                )
            chunk = min(wait, 2.0) if status_started else _sleep_chunk(wait)
            total_wait += chunk
            await asyncio.sleep(chunk)
        if wait_started:
            log.debug(
                "Rate limiting detail: shared SPIGOT pacing wait complete; "
                f"tracker={tracker_name.upper()} scope={scope.server_key}/{scope.account_identity} "
                f"scheme={scheme_id} bucket={bucket_name} action={action or '-'} total_wait={total_wait:.3f}s"
            )
        if status_started:
            log.progress(
                f"[paced] waited {_format_whole_seconds(total_wait)} for "
                f"{_format_pacing_subject(tracker_name, action)}"
            )
        now = time.monotonic()
        bucket.last_request_started = now
        _prune_window(bucket, now, window_seconds)
        if request_limit:
            bucket.request_starts.append(now)
        return total_wait


def record_gazelle_throttle(
    base_url: str,
    tracker_name: str,
    *,
    retry_after: str | None = None,
    fallback_delay: float,
    action: str | None = None,
    account_identity: str | None = None,
) -> float:
    """Record a shared Gazelle penalty-box wait after a throttle response."""
    try:
        retry_after_seconds = float(retry_after) if retry_after else None
    except (TypeError, ValueError):
        retry_after_seconds = None
    if retry_after_seconds is not None and retry_after_seconds > 0:
        penalty = retry_after_seconds + 1.0
    else:
        rule = resolve_action_rate_limit(tracker_name, action)
        fallback_floor = rule.seconds if rule else GAZELLE_RATE_LIMIT_WINDOW_SECONDS
        penalty = max(float(fallback_delay), fallback_floor + SLOW_MODE_SAFETY_MARGIN)
    try:
        return _get_spigot_store().record_throttle(
            scope=spigot.make_scope(
                base_url=base_url,
                tracker_name=tracker_name,
                account_id=account_identity,
            ),
            retry_after=retry_after_seconds,
            fallback_seconds=penalty,
            now=time.monotonic(),
        )
    except spigot.SpigotError as exc:
        raise _translate_spigot_error(exc) from exc


def _reset_gazelle_rate_limits_for_tests() -> None:
    """Test helper to clear shared limiter state."""
    global _spigot_store
    _gazelle_buckets.clear()
    _spigot_participant_counts.clear()
    _last_known_participant_count_by_tracker.clear()
    _spigot_store = None
    test_path = Path(tempfile.gettempdir()) / f"oatgrass-spigot-test-{os.getpid()}.sqlite3"
    try:
        test_path.unlink()
    except FileNotFoundError:
        pass
    spigot.set_path_for_tests(test_path)
    spigot.set_session_uuid_for_tests(f"test-session-{os.getpid()}")
    set_slow_mode_concurrent_runs(None)
