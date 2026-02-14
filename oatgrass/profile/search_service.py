"""Option 2 profile-list search service."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Iterable

from oatgrass import logger
from oatgrass.config import OatgrassConfig, TrackerConfig
from oatgrass.profile.retriever import ListType, ProfileTorrent
from oatgrass.profile.tracker_selection import resolve_profile_tracker
from oatgrass.search.gazelle_client import GazelleServiceAdapter
from oatgrass.search.search_mode import _next_run_path, _pick_opposite_tracker


@dataclass(frozen=True)
class ProfileSearchResult:
    list_type: ListType
    processed: int
    skipped: int
    candidate_urls: list[tuple[str, int]]


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

    source_client = GazelleServiceAdapter(source_tracker)
    target_client = GazelleServiceAdapter(opposite_tracker)
    skipped = 0
    candidates: list[tuple[str, int]] = []
    try:
        total = len(entries)
        for idx, entry in enumerate(entries, start=1):
            logger.info(
                f"[Task {idx} of {total}] group={entry.group_id} torrent={entry.torrent_id} "
                f"{entry.group_name or ''}".strip()
            )
            try:
                entry_candidates, was_skipped = await _evaluate_profile_entry(
                    entry,
                    source_tracker,
                    opposite_tracker,
                    source_client,
                    target_client,
                )
            except Exception as exc:  # pragma: no cover - guard for network/API errors
                logger.warning(f"Entry failed: {exc}")
                skipped += 1
                continue
            if was_skipped:
                skipped += 1
                continue
            if entry_candidates:
                logger.info(
                    f"  Missing on destination: {len(entry_candidates)} candidate(s) for source torrent {entry.torrent_id}"
                )
                candidates.extend(entry_candidates)
            else:
                logger.info("  Destination already has this source torrent encoding (or equivalent)")
            if idx < total:
                await asyncio.sleep(0.01)
    finally:
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
