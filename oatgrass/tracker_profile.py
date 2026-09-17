"""Central tracker capability and policy definitions."""

from __future__ import annotations

from dataclasses import dataclass


OPS_PACING_SCHEME_2025 = "ops_pacing_scheme_2025"
RED_PACING_SCHEME_2025 = "red_pacing_scheme_2025"


@dataclass(frozen=True)
class RateLimitRule:
    requests: int
    seconds: float


@dataclass(frozen=True)
class PacingLimit:
    bucket_name: str
    rule: RateLimitRule


class PacingScheme:
    scheme_id: str

    def limits_for(self, profile: "TrackerProfile", action: str | None) -> tuple[PacingLimit, ...]:
        raise NotImplementedError

    def estimate_seconds_per_request(self, profile: "TrackerProfile", action: str | None) -> float | None:
        """Steady-state interval this scheme's own rule implies for one
        caller acting alone -- a floor, not a promise (ignores concurrent
        participants and throttle backoff, both applied by the caller).
        Default derives it straight from limits_for()'s rule(s), which is
        correct for any simple rate-window scheme; a future scheme whose
        pacing isn't shaped like "N requests per window" should override
        this rather than force a fit."""
        limits = self.limits_for(profile, action)
        if not limits:
            return None
        return max(limit.rule.seconds / limit.rule.requests for limit in limits)


class GlobalWindowPacingScheme(PacingScheme):
    def __init__(self, scheme_id: str) -> None:
        self.scheme_id = scheme_id

    def limits_for(self, profile: "TrackerProfile", action: str | None) -> tuple[PacingLimit, ...]:
        if profile.request_limit is None:
            return ()
        return (PacingLimit("default", RateLimitRule(profile.request_limit, profile.request_window_seconds)),)


class SharedAjaxPacingScheme(PacingScheme):
    def __init__(self, scheme_id: str) -> None:
        self.scheme_id = scheme_id

    def limits_for(self, profile: "TrackerProfile", action: str | None) -> tuple[PacingLimit, ...]:
        normalized_action = (action or "").strip().lower()
        rule = None
        if profile.action_limits and normalized_action in profile.action_limits:
            rule = profile.action_limits[normalized_action]
        elif profile.request_limit is not None:
            rule = RateLimitRule(profile.request_limit, profile.request_window_seconds)
        if rule is None:
            return ()
        bucket = "ajax" if normalized_action in profile.shared_ajax_actions else "default"
        return (PacingLimit(bucket, rule),)


@dataclass(frozen=True)
class TrackerProfile:
    list_types: tuple[str, ...]
    request_limit: int | None
    request_window_seconds: float = 10.0
    pacing_scheme: str = RED_PACING_SCHEME_2025
    action_limits: dict[str, RateLimitRule] | None = None
    shared_ajax_actions: tuple[str, ...] = ()
    token_auth: bool = False
    group_policy_fields_complete: bool = False


_TRACKER_PROFILES: dict[str, TrackerProfile] = {
    "ops": TrackerProfile(
        list_types=("snatched", "uploaded", "seeding", "leeching"),
        request_limit=5,
        pacing_scheme=OPS_PACING_SCHEME_2025,
        action_limits={
            "browse": RateLimitRule(5, 10.0),
            "collage": RateLimitRule(5, 60.0),
            "torrentgroup": RateLimitRule(15, 60.0),
            "user": RateLimitRule(4, 60.0),
            "usersearch": RateLimitRule(5, 60.0),
            "wiki": RateLimitRule(5, 60.0),
        },
        shared_ajax_actions=("browse", "collage", "torrentgroup", "user", "usersearch", "wiki"),
        token_auth=True,
        group_policy_fields_complete=True,
    ),
    "red": TrackerProfile(
        list_types=("seeding", "leeching", "uploaded", "snatched"),
        request_limit=10,
        request_window_seconds=10.0,
        pacing_scheme=RED_PACING_SCHEME_2025,
        token_auth=False,
        # RED torrentgroup often omits trumpable_reasons/logChecksum; policy mode
        # may need action=torrent enrichment until RED API parity changes.
        group_policy_fields_complete=False,
    ),
}

PACING_SCHEMES: dict[str, PacingScheme] = {
    OPS_PACING_SCHEME_2025: SharedAjaxPacingScheme(OPS_PACING_SCHEME_2025),
    RED_PACING_SCHEME_2025: GlobalWindowPacingScheme(RED_PACING_SCHEME_2025),
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


def resolve_action_rate_limit(
    tracker_name: str | None,
    action: str | None,
) -> RateLimitRule | None:
    """Return a source-derived action rule, falling back to tracker default."""
    profile = resolve_tracker_profile(tracker_name)
    normalized_action = (action or "").strip().lower()
    if profile.action_limits and normalized_action in profile.action_limits:
        return profile.action_limits[normalized_action]
    if profile.request_limit is None:
        return None
    return RateLimitRule(profile.request_limit, profile.request_window_seconds)


def resolve_bucket_name(tracker_name: str | None, action: str | None) -> str:
    """Return the limiter bucket name for a tracker action."""
    profile = resolve_tracker_profile(tracker_name)
    normalized_action = (action or "").strip().lower()
    if normalized_action and normalized_action in profile.shared_ajax_actions:
        return "ajax"
    return "default"


def resolve_pacing_limits(tracker_name: str | None, action: str | None) -> tuple[str, tuple[PacingLimit, ...]]:
    profile = resolve_tracker_profile(tracker_name)
    scheme = PACING_SCHEMES.get(profile.pacing_scheme)
    if scheme is None:
        raise ValueError(f"Unsupported pacing scheme '{profile.pacing_scheme}' for tracker '{tracker_name}'.")
    return scheme.scheme_id, scheme.limits_for(profile, action)


def resolve_estimated_seconds_per_request(tracker_name: str | None, action: str | None) -> float | None:
    """Steady-state per-request interval this tracker's own pacing scheme
    implies, for display estimates -- see PacingScheme.estimate_seconds_per_request."""
    profile = resolve_tracker_profile(tracker_name)
    scheme = PACING_SCHEMES.get(profile.pacing_scheme)
    if scheme is None:
        raise ValueError(f"Unsupported pacing scheme '{profile.pacing_scheme}' for tracker '{tracker_name}'.")
    return scheme.estimate_seconds_per_request(profile, action)
