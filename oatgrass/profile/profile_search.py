"""Option 2 profile-list search service."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Iterable

from oatgrass import logger
from oatgrass.config import OatgrassConfig, TrackerConfig
from oatgrass.progress_timing import build_task_timing_phrase
from oatgrass.profile.retriever import ListType, ProfileTorrent
from oatgrass.profile.tracker_selection import resolve_profile_tracker
from oatgrass.search.gazelle_client import GazelleServiceAdapter
from oatgrass.search.resilience import (
    optional_list_of_dicts,
    response_payload,
    run_with_retries,
)
from oatgrass.search.group_search import _next_run_path, _pick_opposite_tracker
from oatgrass.search.candidate_policy import (
    CandidatePolicy,
    PolicySummary,
    apply_candidate_policy,
)
from oatgrass.search.policy_enrichment import enrich_red_policy_fields
from oatgrass.search.policy_candidate_resolution import (
    build_no_match_candidates_from_torrent,
    resolve_policy_candidates,
)
from oatgrass.tracker_profile import tracker_needs_policy_enrichment

PROFILE_SEARCH_PROGRESS_HEARTBEAT_SECONDS = 5.0
PROFILE_ENTRY_MAX_ATTEMPTS = 3

@dataclass(frozen=True)
class ProfileSearchResult:
    list_type: ListType
    processed: int
    skipped: int
    candidate_urls: list[tuple[str, int]]
    policy_summary: PolicySummary


@dataclass(frozen=True)
class ProfileEntryEvaluation:
    candidate_urls: list[tuple[str, int]]
    suppressed_messages: list[str]
    policy_summary: PolicySummary


@dataclass
class _ProgressState:
    total: int
    started_at: float
    completed: int = 0
    skipped: int = 0
    candidates: int = 0
    current_index: int = 0
    done: bool = False


@dataclass
class _ProfileLookupCache:
    # Group-scoped caches trim repeated source/target API calls for profile rows
    # that share the same source group_id.
    source_group_payloads: dict[int, tuple[dict, list[dict]]] = field(default_factory=dict)
    source_browse_results: dict[int, dict | None] = field(default_factory=dict)
    target_results: dict[int, dict | None] = field(default_factory=dict)
    target_groups: dict[int, object] = field(default_factory=dict)
    enrichment_torrents: dict[int, dict] = field(default_factory=dict)


def _render_progress_line(state: _ProgressState) -> str:
    phrase = build_task_timing_phrase(
        total=state.total,
        completed=state.completed,
        started_at=state.started_at,
    )
    return f"   Working: {phrase}"


async def _progress_heartbeat(state: _ProgressState) -> None:
    log = logger.get_logger()
    while not state.done:
        await asyncio.sleep(PROFILE_SEARCH_PROGRESS_HEARTBEAT_SECONDS)
        if state.done:
            break
        log.status(_render_progress_line(state))


def _emit_progress_status(
    state: _ProgressState,
    *,
    idx: int,
    skipped: int,
    candidates: int,
) -> None:
    state.completed = idx
    state.skipped = skipped
    state.candidates = candidates
    logger.get_logger().status(_render_progress_line(state))


def _cross_upload_torrent_url(tracker: TrackerConfig, torrent_id: int) -> str:
    return f"{tracker.url.rstrip('/')}/torrents.php?torrentid={torrent_id}"


def _find_torrent_in_group(torrents: list[dict], torrent_id: int) -> dict | None:
    for torrent in torrents:
        try:
            candidate = int(torrent.get("id") or torrent.get("torrentId"))
        except (TypeError, ValueError):
            continue
        if candidate == torrent_id:
            return torrent
    return None


def _to_candidate_urls(source_tracker: TrackerConfig, candidates: Iterable[tuple[int, int]]) -> list[tuple[str, int]]:
    return [(_cross_upload_torrent_url(source_tracker, torrent_id), priority) for torrent_id, priority in candidates]


def _filter_candidates_for_source_torrent(candidates: Iterable[object], source_torrent_id: int) -> list[object]:
    return [c for c in candidates if getattr(c.source_torrent, "torrent_id", None) == source_torrent_id]


def _skip_entry(message: str) -> tuple[ProfileEntryEvaluation, bool]:
    logger.warning(message)
    return ProfileEntryEvaluation(candidate_urls=[], suppressed_messages=[], policy_summary=PolicySummary()), True


def _enrich_profile_entry(
    entry: ProfileTorrent,
    group_id: int,
    torrent_payload: dict,
) -> ProfileTorrent:
    merged_metadata = dict(entry.metadata)
    merged_metadata.update(torrent_payload)
    return replace(
        entry,
        group_id=entry.group_id or group_id,
        media=entry.media or torrent_payload.get("media"),
        format=entry.format or torrent_payload.get("format"),
        encoding=entry.encoding or torrent_payload.get("encoding"),
        metadata=merged_metadata,
    )


def _parse_group_payload(payload: dict, *, context: str) -> tuple[dict, list[dict]]:
    response = response_payload(payload, context)
    group_data = response.get("group", {})
    if not isinstance(group_data, dict):
        group_data = {}
    torrents = response.get("torrents", [])
    if not isinstance(torrents, list):
        torrents = []
    return group_data, torrents


async def _find_source_browse_result(
    source_client: GazelleServiceAdapter,
    artist: str,
    album: str | None,
    year: int | None,
    group_id: int | None,
) -> dict | None:
    browse = await source_client.search(artistname=artist, groupname=album, year=year)
    response = response_payload(browse, "Source browse search")
    results = optional_list_of_dicts(response, "results", "Source browse search")
    for result in results:
        try:
            if int(result.get("groupId") or 0) == group_id:
                return result
        except (TypeError, ValueError):
            continue
    return results[0] if results else None


def _source_torrent_has_edition_id(source_torrent: dict) -> bool:
    value = (
        source_torrent.get("editionId")
        or source_torrent.get("edition_id")
        or source_torrent.get("editionID")
    )
    if value in (None, ""):
        return False
    try:
        return int(value) > 0
    except (TypeError, ValueError):
        return True


async def _evaluate_profile_entry(
    entry: ProfileTorrent,
    source_tracker: TrackerConfig,
    opposite_tracker: TrackerConfig,
    source_client: GazelleServiceAdapter,
    target_client: GazelleServiceAdapter,
    group_only: bool = False,
    candidate_policy: CandidatePolicy = CandidatePolicy.STANDARD,
    candidate_resolver=None,
    lookup_cache: _ProfileLookupCache | None = None,
) -> tuple[ProfileEntryEvaluation, bool]:
    """Return candidate URLs and whether entry was skipped."""
    from oatgrass.search.edition_parser import parse_group_from_browse, parse_group_hybrid
    from oatgrass.search.tier_search_service import search_with_tiers

    if entry.group_id is None or entry.torrent_id is None:
        if entry.torrent_id is None:
            return _skip_entry("Skipping cached row with missing group/torrent IDs")
        torrent_response = await source_client.get_torrent(entry.torrent_id)
        torrent_block = response_payload(torrent_response, "Source torrent")
        group_data = torrent_block.get("group", {})
        source_torrent = torrent_block.get("torrent", {})
        if not isinstance(group_data, dict):
            group_data = {}
        if not isinstance(source_torrent, dict):
            source_torrent = {}
        if not source_torrent:
            return _skip_entry(f"Skipping torrent {entry.torrent_id}: no torrent payload available")
        inferred_group_id = int(group_data.get("id") or 0)
        if not inferred_group_id:
            return _skip_entry(f"Skipping torrent {entry.torrent_id}: no group id in torrent payload")
        entry = _enrich_profile_entry(entry, inferred_group_id, source_torrent)
    else:
        cached_group = (
            lookup_cache.source_group_payloads.get(entry.group_id)
            if lookup_cache is not None
            else None
        )
        if cached_group is None:
            source_group_response = await source_client.get_group(entry.group_id)
            group_data, torrents = _parse_group_payload(source_group_response, context="Source group")
            if lookup_cache is not None:
                lookup_cache.source_group_payloads[entry.group_id] = (group_data, torrents)
        else:
            group_data, torrents = cached_group
        source_torrent = _find_torrent_in_group(torrents, entry.torrent_id) if entry.torrent_id is not None else None
        if not source_torrent and entry.torrent_id is not None:
            torrent_response = await source_client.get_torrent(entry.torrent_id)
            torrent_block = response_payload(torrent_response, "Source torrent")
            source_torrent = torrent_block.get("torrent", {})
            if not isinstance(source_torrent, dict):
                source_torrent = {}
            if source_torrent:
                fallback_group_data = torrent_block.get("group", group_data)
                if isinstance(fallback_group_data, dict):
                    group_data = fallback_group_data
        if not source_torrent:
            return _skip_entry(
                f"Skipping group {entry.group_id}: source torrent {entry.torrent_id} not found in group/torrent responses"
            )
        entry = _enrich_profile_entry(entry, entry.group_id, source_torrent)

    music_info = group_data.get("musicInfo", {})
    if not isinstance(music_info, dict):
        music_info = {}
    artists = music_info.get("artists", []) or []
    if not isinstance(artists, list):
        artists = []
    first_artist = artists[0] if artists and isinstance(artists[0], dict) else {}
    group_artist = first_artist.get("name", "")
    group_name = group_data.get("name") or entry.group_name or ""
    group_year = group_data.get("year")
    search_artist = group_artist or (entry.artist_name or group_name)

    source_browse_result = None
    if not _source_torrent_has_edition_id(source_torrent):
        if entry.group_id is not None and lookup_cache is not None and entry.group_id in lookup_cache.source_browse_results:
            source_browse_result = lookup_cache.source_browse_results[entry.group_id]
        else:
            source_browse_result = await _find_source_browse_result(
                source_client,
                artist=search_artist,
                album=group_name,
                year=group_year,
                group_id=entry.group_id,
            )
            if entry.group_id is not None and lookup_cache is not None:
                lookup_cache.source_browse_results[entry.group_id] = source_browse_result

    source_group = parse_group_hybrid(
        group_data, [source_torrent], source_browse_result, source_tracker.name.upper()
    )

    if entry.group_id is not None and lookup_cache is not None and entry.group_id in lookup_cache.target_results:
        target_result = lookup_cache.target_results[entry.group_id]
    else:
        target_result = await search_with_tiers(
            target_client,
            artist=search_artist,
            album=group_name,
            year=group_year,
        )
        if entry.group_id is not None and lookup_cache is not None:
            lookup_cache.target_results[entry.group_id] = target_result
    if not target_result:
        source_edition = source_group.editions[0] if source_group.editions else None
        source_info = source_edition.torrents[0] if source_edition and source_edition.torrents else None
        if source_info is None:
            return (
                ProfileEntryEvaluation(
                    candidate_urls=_to_candidate_urls(source_tracker, [(entry.torrent_id, 100)]),
                    suppressed_messages=[],
                    policy_summary=PolicySummary(),
                ),
                False,
            )
        no_target_candidates = build_no_match_candidates_from_torrent(
            source_info,
            edition_id=source_edition.edition_id if source_edition else None,
            edition_year=(source_edition.year or 0) if source_edition else 0,
            edition_title=(source_edition.title or "(no title)") if source_edition else "(no title)",
            priority=100,
        )
        candidate_urls, suppressed_messages, policy_summary = await resolve_policy_candidates(
            no_target_candidates,
            source_tracker=source_tracker,
            source_client=source_client,
            policy=candidate_policy,
            enrichment_cache=lookup_cache.enrichment_torrents if lookup_cache is not None else None,
        )
        return (
            ProfileEntryEvaluation(
                candidate_urls=candidate_urls,
                suppressed_messages=suppressed_messages,
                policy_summary=policy_summary,
            ),
            False,
        )
    if group_only:
        return ProfileEntryEvaluation(candidate_urls=[], suppressed_messages=[], policy_summary=PolicySummary()), False

    if opposite_tracker.name.lower() == "red":
        target_gid = int(target_result.get("groupId"))
        target_group = (
            lookup_cache.target_groups.get(target_gid)
            if lookup_cache is not None
            else None
        )
        if target_group is None:
            target_group_response = await target_client.get_group(target_gid)
            target_group_data, target_torrents = _parse_group_payload(target_group_response, context="Target group")
            target_group = parse_group_hybrid(
                target_group_data,
                target_torrents,
                target_result,
                opposite_tracker.name.upper(),
            )
            if lookup_cache is not None:
                lookup_cache.target_groups[target_gid] = target_group
    else:
        target_group = parse_group_from_browse(target_result, opposite_tracker.name.upper())

    if candidate_resolver is not None:
        resolved = candidate_resolver(source_group, target_group)
        filtered = [(torrent_id, priority) for torrent_id, priority in resolved if torrent_id == entry.torrent_id]
        return (
            ProfileEntryEvaluation(
                candidate_urls=_to_candidate_urls(source_tracker, filtered),
                suppressed_messages=[],
                policy_summary=PolicySummary(),
            ),
            False,
        )

    from oatgrass.search.edition_comparison import compare_editions
    from oatgrass.search.edition_matcher import match_editions
    from oatgrass.search.upload_candidates import find_upload_candidates

    matches = match_editions(source_group, target_group, min_confidence=25)
    comparisons = compare_editions(matches)
    upload_candidates = find_upload_candidates(comparisons)
    if candidate_policy != CandidatePolicy.STANDARD and tracker_needs_policy_enrichment(source_tracker.name):
        enrichment_cache = lookup_cache.enrichment_torrents if lookup_cache is not None else None
        if enrichment_cache is None:
            await enrich_red_policy_fields(source_client, upload_candidates)
        else:
            await enrich_red_policy_fields(
                source_client,
                upload_candidates,
                torrent_payload_cache=enrichment_cache,
            )
    policy_outcome = apply_candidate_policy(upload_candidates, policy=candidate_policy)
    filtered_candidates = [
        c for c in policy_outcome.candidates
        if getattr(c.source_torrent, "torrent_id", None) == entry.torrent_id
    ]
    filtered_suppressed = [
        s for s in policy_outcome.suppressed
        if getattr(s.candidate.source_torrent, "torrent_id", None) == entry.torrent_id
    ]
    candidates = [(candidate.source_torrent.torrent_id, candidate.priority) for candidate in filtered_candidates]
    suppressed_messages = [
        (
            "Suppressed: "
            f"{_cross_upload_torrent_url(source_tracker, item.candidate.source_torrent.torrent_id)} "
            f"({item.reason_text})"
        )
        for item in filtered_suppressed
    ]
    return (
        ProfileEntryEvaluation(
            candidate_urls=_to_candidate_urls(source_tracker, candidates),
            suppressed_messages=suppressed_messages,
            policy_summary=policy_outcome.summary,
        ),
        False,
    )


async def run_profile_search_workflow(
    config: OatgrassConfig,
    source_tracker_key: str,
    list_type: ListType,
    entries: list[ProfileTorrent],
    group_only: bool = False,
    candidate_policy: CandidatePolicy = CandidatePolicy.STANDARD,
    abbrev: bool = False,
    output_dir: Path | None = None,
) -> ProfileSearchResult:
    source_key, source_tracker = resolve_profile_tracker(config, source_tracker_key)
    _, opposite_tracker = _pick_opposite_tracker(config.trackers, source_key)

    log_path = _next_run_path(output_dir or Path("output"))
    logger.set_logger(logger.OatgrassLogger(log_path))
    from oatgrass.rate_limits import describe_slow_mode

    if slow_mode_note := describe_slow_mode():
        logger.info(slow_mode_note)
    logger.info("[Profile Search] Cached list mode")
    logger.info(f"Source tracker: {source_tracker.name.upper()}")
    logger.info(f"Target tracker: {opposite_tracker.name.upper()}")
    logger.info(f"List: {list_type}")
    logger.info(f"Rows: {len(entries)}")
    logger.info(f"Matching mode: {'Group-only' if group_only else 'Edition-aware'}")
    logger.info(f"Candidate policy: {candidate_policy.value}")
    if candidate_policy != CandidatePolicy.STANDARD:
        logger.info(f"Policy rules: evaluating {len(entries)} source torrent(s) at startup.")

    source_client = GazelleServiceAdapter(source_tracker)
    target_client = GazelleServiceAdapter(opposite_tracker)
    skipped = 0
    candidates: list[tuple[str, int]] = []
    policy_summary = PolicySummary()
    lookup_cache = _ProfileLookupCache()
    progress = _ProgressState(total=len(entries), started_at=time.monotonic())
    heartbeat_task = asyncio.create_task(_progress_heartbeat(progress))
    try:
        total = len(entries)
        for idx, entry in enumerate(entries, start=1):
            progress.current_index = idx
            group_id = entry.group_id if entry.group_id is not None else "?"
            torrent_id = entry.torrent_id if entry.torrent_id is not None else "?"
            timing_phrase = build_task_timing_phrase(
                total=total,
                completed=idx - 1,
                started_at=progress.started_at,
            )
            logger.info(f"[Task {idx} of {total}] —— {timing_phrase}")
            logger.info(
                f"   {source_tracker.name.lower()} group #{group_id} "
                f"torrent #{torrent_id} '{entry.group_name or ''}'"
            )
            evaluation = ProfileEntryEvaluation(candidate_urls=[], suppressed_messages=[], policy_summary=PolicySummary())
            was_skipped = False
            try:
                evaluation, was_skipped = await run_with_retries(
                    lambda: _evaluate_profile_entry(
                        entry,
                        source_tracker,
                        opposite_tracker,
                        source_client,
                        target_client,
                        group_only=group_only,
                        candidate_policy=candidate_policy,
                        lookup_cache=lookup_cache,
                    ),
                    max_attempts=PROFILE_ENTRY_MAX_ATTEMPTS,
                    on_retry=lambda attempt, max_attempts, delay, exc: logger.warning(
                        f"Transient profile entry failure ({source_tracker.name.upper()} group #{group_id} torrent #{torrent_id}); "
                        f"retrying in {delay}s (attempt {attempt}/{max_attempts}): {exc}"
                    ),
                )
            except Exception as exc:  # pragma: no cover - guard for network/API errors
                logger.warning(
                    f"Entry failed after retries ({source_tracker.name.upper()} group #{group_id} torrent #{torrent_id}): {exc}"
                )
                was_skipped = True

            if was_skipped:
                skipped += 1
            else:
                policy_summary.merge(evaluation.policy_summary)
                if evaluation.suppressed_messages and not abbrev:
                    for message in evaluation.suppressed_messages:
                        logger.info(f"   {message}")

            if was_skipped:
                pass
            elif evaluation.candidate_urls:
                logger.info(
                    f"   Candidate found: {len(evaluation.candidate_urls)} candidate(s) "
                    f"for source torrent #{entry.torrent_id}"
                )
                candidates.extend(evaluation.candidate_urls)
            else:
                logger.info("   Match found on target. Not a candidate.")

            _emit_progress_status(
                progress,
                idx=idx,
                skipped=skipped,
                candidates=len(candidates),
            )
            if not was_skipped and idx < total:
                await asyncio.sleep(0.01)
    finally:
        progress.done = True
        heartbeat_task.cancel()
        await asyncio.gather(heartbeat_task, return_exceptions=True)
        logger.get_logger().clear_status()
        await source_client.close()
        await target_client.close()
        if candidate_policy != CandidatePolicy.STANDARD:
            logger.info("[Policy Summary]")
            logger.info(f"   Promoted: {policy_summary.promoted}")
            logger.info(f"   Demoted: {policy_summary.demoted}")
            logger.info(f"   Excluded by policy: {policy_summary.excluded_by_policy}")
            logger.info(f"   Dropped duplicate 24-bit Vinyl: {policy_summary.duplicate_24bit}")
            logger.info(f"   Suppressed total: {policy_summary.suppressed_total}")
        logger.info(f"Output mirrored to {log_path}")
        logger.get_logger().close()

    processed = len(entries) - skipped
    return ProfileSearchResult(
        list_type=list_type,
        processed=processed,
        skipped=skipped,
        candidate_urls=candidates,
        policy_summary=policy_summary,
    )
