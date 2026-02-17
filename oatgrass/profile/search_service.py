"""Option 2 profile-list search service."""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Iterable

from oatgrass import logger
from oatgrass.config import OatgrassConfig, TrackerConfig
from oatgrass.profile.retriever import ListType, ProfileTorrent
from oatgrass.profile.tracker_selection import resolve_profile_tracker
from oatgrass.search.gazelle_client import GazelleServiceAdapter
from oatgrass.search.search_mode import _next_run_path, _pick_opposite_tracker

PROFILE_SEARCH_PROGRESS_HEARTBEAT_SECONDS = 5.0

@dataclass(frozen=True)
class ProfileSearchResult:
    list_type: ListType
    processed: int
    skipped: int
    candidate_urls: list[tuple[str, int]]


@dataclass
class _ProgressState:
    total: int
    started_at: float
    completed: int = 0
    skipped: int = 0
    candidates: int = 0
    current_index: int = 0
    done: bool = False


def _format_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h{minutes:02d}m{secs:02d}s"
    if minutes:
        return f"{minutes}m{secs:02d}s"
    return f"{secs}s"


def _render_progress_line(state: _ProgressState) -> str:
    return f"   Working: {_timing_phrase(state)}"


def _progress_timing_text(state: _ProgressState) -> tuple[str, str | None, str | None]:
    elapsed = time.monotonic() - state.started_at
    rate = state.completed / elapsed if elapsed > 0 else 0.0
    remaining = max(0, state.total - state.completed)
    eta = remaining / rate if rate > 0 else None
    elapsed_text = _format_duration(elapsed)
    if eta is None:
        return elapsed_text, None, None
    eta_text = _format_duration(eta)
    finish_text = (datetime.now().astimezone() + timedelta(seconds=eta)).strftime("%H:%M:%S")
    return elapsed_text, eta_text, finish_text


def _timing_phrase(state: _ProgressState) -> str:
    elapsed_text, eta_text, finish_text = _progress_timing_text(state)
    if eta_text is None:
        return f"{elapsed_text} elapsed, ETA unknown"
    return f"{elapsed_text} elapsed, ETA {eta_text} ({finish_text})"


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


def _skip_entry(message: str) -> tuple[list[tuple[str, int]], bool]:
    logger.warning(message)
    return [], True


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


async def _find_source_browse_result(
    source_client: GazelleServiceAdapter,
    artist: str,
    album: str | None,
    year: int | None,
    group_id: int,
) -> dict | None:
    browse = await source_client.search(artistname=artist, groupname=album, year=year)
    results = browse.get("response", {}).get("results", [])
    for result in results:
        try:
            if int(result.get("groupId") or 0) == group_id:
                return result
        except (TypeError, ValueError):
            continue
    return results[0] if results else None


async def _evaluate_profile_entry(
    entry: ProfileTorrent,
    source_tracker: TrackerConfig,
    opposite_tracker: TrackerConfig,
    source_client: GazelleServiceAdapter,
    target_client: GazelleServiceAdapter,
    group_only: bool = False,
    candidate_resolver=None,
) -> tuple[list[tuple[str, int]], bool]:
    """Return candidate URLs and whether entry was skipped."""
    from oatgrass.search.edition_parser import parse_group_from_browse, parse_group_hybrid
    from oatgrass.search.tier_search_service import search_with_tiers

    if entry.group_id is None or entry.torrent_id is None:
        if entry.torrent_id is None:
            return _skip_entry("Skipping cached row with missing group/torrent IDs")
        torrent_response = await source_client.get_torrent(entry.torrent_id)
        torrent_block = torrent_response.get("response", {})
        group_data = torrent_block.get("group", {})
        source_torrent = torrent_block.get("torrent", {})
        if not source_torrent:
            return _skip_entry(f"Skipping torrent {entry.torrent_id}: no torrent payload available")
        inferred_group_id = int(group_data.get("id") or 0)
        if not inferred_group_id:
            return _skip_entry(f"Skipping torrent {entry.torrent_id}: no group id in torrent payload")
        entry = _enrich_profile_entry(entry, inferred_group_id, source_torrent)
    else:
        source_group_response = await source_client.get_group(entry.group_id)
        response = source_group_response.get("response", {})
        group_data = response.get("group", {})
        torrents = response.get("torrents", [])
        source_torrent = _find_torrent_in_group(torrents, entry.torrent_id) if entry.torrent_id is not None else None
        if not source_torrent and entry.torrent_id is not None:
            torrent_response = await source_client.get_torrent(entry.torrent_id)
            torrent_block = torrent_response.get("response", {})
            source_torrent = torrent_block.get("torrent", {})
            if source_torrent:
                group_data = torrent_block.get("group", group_data)
        if not source_torrent:
            return _skip_entry(
                f"Skipping group {entry.group_id}: source torrent {entry.torrent_id} not found in group/torrent responses"
            )
        entry = _enrich_profile_entry(entry, entry.group_id, source_torrent)

    artists = group_data.get("musicInfo", {}).get("artists", []) or []
    group_artist = artists[0].get("name", "") if artists else ""
    group_name = group_data.get("name") or entry.group_name or ""
    group_year = group_data.get("year")
    search_artist = group_artist or (entry.artist_name or group_name)

    source_browse_result = await _find_source_browse_result(
        source_client,
        artist=search_artist,
        album=group_name,
        year=group_year,
        group_id=entry.group_id,
    )

    source_group = parse_group_hybrid(
        group_data, [source_torrent], source_browse_result, source_tracker.name.upper()
    )

    target_result = await search_with_tiers(
        target_client,
        artist=search_artist,
        album=group_name,
        year=group_year,
    )
    if not target_result:
        return _to_candidate_urls(source_tracker, [(entry.torrent_id, 100)]), False
    if group_only:
        return [], False

    if opposite_tracker.name.lower() == "red":
        target_gid = int(target_result.get("groupId"))
        target_group_response = await target_client.get_group(target_gid)
        target_group_data = target_group_response.get("response", {}).get("group", {})
        target_torrents = target_group_response.get("response", {}).get("torrents", [])
        target_group = parse_group_hybrid(
            target_group_data,
            target_torrents,
            target_result,
            opposite_tracker.name.upper(),
        )
    else:
        target_group = parse_group_from_browse(target_result, opposite_tracker.name.upper())

    if candidate_resolver is not None:
        resolved = candidate_resolver(source_group, target_group)
        filtered = [(torrent_id, priority) for torrent_id, priority in resolved if torrent_id == entry.torrent_id]
        return _to_candidate_urls(source_tracker, filtered), False

    from oatgrass.search.edition_comparison import compare_editions
    from oatgrass.search.edition_matcher import match_editions
    from oatgrass.search.upload_candidates import find_upload_candidates

    matches = match_editions(source_group, target_group, min_confidence=25)
    comparisons = compare_editions(matches)
    filtered_candidates = [
        c for c in find_upload_candidates(comparisons)
        if getattr(c.source_torrent, "torrent_id", None) == entry.torrent_id
    ]
    candidates = [(candidate.source_torrent.torrent_id, candidate.priority) for candidate in filtered_candidates]
    return _to_candidate_urls(source_tracker, candidates), False


async def run_profile_list_search(
    config: OatgrassConfig,
    source_tracker_key: str,
    list_type: ListType,
    entries: list[ProfileTorrent],
    group_only: bool = False,
    output_dir: Path | None = None,
) -> ProfileSearchResult:
    source_key, source_tracker = resolve_profile_tracker(config, source_tracker_key)
    _, opposite_tracker = _pick_opposite_tracker(config.trackers, source_key)

    log_path = _next_run_path(output_dir or Path("output"))
    logger.set_logger(logger.OatgrassLogger(log_path))
    logger.info("[Profile Search] Cached list mode")
    logger.info(f"Source tracker: {source_tracker.name.upper()}")
    logger.info(f"Target tracker: {opposite_tracker.name.upper()}")
    logger.info(f"List: {list_type}")
    logger.info(f"Rows: {len(entries)}")
    logger.info(f"Matching mode: {'Group-only' if group_only else 'Edition-aware'}")

    source_client = GazelleServiceAdapter(source_tracker)
    target_client = GazelleServiceAdapter(opposite_tracker)
    skipped = 0
    candidates: list[tuple[str, int]] = []
    progress = _ProgressState(total=len(entries), started_at=time.monotonic())
    heartbeat_task = asyncio.create_task(_progress_heartbeat(progress))
    try:
        total = len(entries)
        for idx, entry in enumerate(entries, start=1):
            progress.current_index = idx
            group_id = entry.group_id if entry.group_id is not None else "?"
            torrent_id = entry.torrent_id if entry.torrent_id is not None else "?"
            logger.info(f"[Task {idx}/{total}] {_timing_phrase(progress)}")
            logger.info(
                f"   {source_tracker.name.lower()} group #{group_id} "
                f"torrent #{torrent_id} '{entry.group_name or ''}'"
            )
            entry_candidates: list[tuple[str, int]] = []
            was_skipped = False
            try:
                entry_candidates, was_skipped = await _evaluate_profile_entry(
                    entry,
                    source_tracker,
                    opposite_tracker,
                    source_client,
                    target_client,
                    group_only=group_only,
                )
            except Exception as exc:  # pragma: no cover - guard for network/API errors
                logger.warning(f"Entry failed: {exc}")
                was_skipped = True

            if was_skipped:
                skipped += 1
            elif entry_candidates:
                logger.info(
                    f"   Candidate found: {len(entry_candidates)} candidate(s) "
                    f"for source torrent #{entry.torrent_id}"
                )
                candidates.extend(entry_candidates)
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
        logger.info(f"Output mirrored to {log_path}")
        logger.get_logger().close()

    processed = len(entries) - skipped
    return ProfileSearchResult(
        list_type=list_type,
        processed=processed,
        skipped=skipped,
        candidate_urls=candidates,
    )
