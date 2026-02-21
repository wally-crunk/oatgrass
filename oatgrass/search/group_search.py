from __future__ import annotations

import asyncio
from typing import Optional
from urllib.parse import parse_qs, urlparse

from pathlib import Path

from oatgrass.config import OatgrassConfig, TrackerConfig
from oatgrass.search.formatters import (
    emit as _emit,
    display_value as _display_value,
    format_compact_result as _format_compact_result,
    format_size as _format_size,
)
from oatgrass.search.parsers import (
    SearchContext,
    build_search_context as _build_search_context,
    collage_max_size as _collage_max_size,
    extract_search_max as _extract_search_max,
    group_id as _group_id,
    parse_collage_url as _parse_collage_url,
)
from oatgrass.search.url_utils import (
    cross_upload_url as _cross_upload_url,
    find_tracker_by_url as _find_tracker_by_url,
    is_group_url as _is_group_url,
    is_url as _is_url,
)
from oatgrass.search.gazelle_client import GazelleServiceAdapter
from oatgrass.search.resilience import (
    optional_list_of_dicts,
    response_payload,
    run_with_retries,
)
from oatgrass.search.tier_search_service import search_with_tiers
from oatgrass import logger

SEARCH_ENTRY_MAX_ATTEMPTS = 3
COLLAGE_FETCH_MAX_ATTEMPTS = 3


async def _search_entry_with_retries(
    client: GazelleServiceAdapter,
    *,
    source_tracker_name: str,
    source_group_label: int | str,
    artist: str,
    album: str | None,
    year: int | None,
    release_type: int | None,
    media: str | None,
    max_tier: int,
) -> dict | None:
    return await run_with_retries(
        lambda: search_with_tiers(
            client,
            artist,
            album,
            year,
            release_type,
            media,
            max_tier=max_tier,
        ),
        max_attempts=SEARCH_ENTRY_MAX_ATTEMPTS,
        on_retry=lambda attempt, max_attempts, delay, exc: logger.warning(
            f"Transient entry failure ({source_tracker_name} group #{source_group_label}); "
            f"retrying in {delay}s (attempt {attempt}/{max_attempts}): {exc}"
        ),
    )


async def _fetch_group_entries_with_retries(tracker: TrackerConfig, group_id: int) -> list[dict]:
    async def _fetch_and_parse() -> list[dict]:
        group_response = await _fetch_torrent_group(tracker, group_id)
        response = response_payload(group_response, "Group")
        group = response.get("group") if isinstance(response.get("group"), dict) else {}
        torrents = response.get("torrents", [])
        if not isinstance(torrents, list):
            torrents = []
        torrents = [torrent for torrent in torrents if isinstance(torrent, dict)]
        if not torrents and isinstance(response.get("torrent"), dict):
            torrent = response.get("torrent")
            torrents = [torrent] if torrent else []
        return [{"group": group, "torrents": torrents}]

    return await run_with_retries(
        _fetch_and_parse,
        max_attempts=SEARCH_ENTRY_MAX_ATTEMPTS,
        on_retry=lambda attempt, max_attempts, delay, exc: logger.warning(
            f"Transient group fetch failure ({tracker.name.upper()} group #{group_id}); "
            f"retrying in {delay}s (attempt {attempt}/{max_attempts}): {exc}"
        ),
    )


def _next_run_path(output_dir: Path = Path(".")) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    max_num = 0
    for path in output_dir.glob("run*.txt"):
        try:
            num = int(path.stem[3:])
            max_num = max(max_num, num)
        except (ValueError, IndexError):
            pass
    return output_dir / f"run{max_num + 1}.txt"


def _pick_opposite_tracker(trackers: dict[str, TrackerConfig], source_key: str) -> tuple[str, TrackerConfig]:
    for key, tracker in trackers.items():
        if key != source_key:
            return key, tracker
    raise ValueError("Need at least two configured trackers to run search mode: find cross-upload candidates")


async def _fetch_collage(tracker: TrackerConfig, collage_id: int, page: int) -> dict:
    adapter = GazelleServiceAdapter(tracker, timeout=20)
    try:
        return await adapter.get_collage(collage_id, page)
    finally:
        await adapter.close()


def _parse_total_collage_pages(collage_response_payload: dict) -> int | None:
    pages_value = collage_response_payload.get("pages")
    try:
        pages = int(pages_value) if pages_value is not None else None
    except (TypeError, ValueError):
        return None
    if pages is None or pages <= 0:
        return None
    return pages


async def _fetch_collage_entries(tracker: TrackerConfig, collage_id: int, start_page: int = 1) -> list[dict]:
    """Fetch collage entries across all pages from start_page onward."""
    entries: list[dict] = []
    page = max(1, start_page)
    while True:
        async def _fetch_and_parse_page() -> tuple[list[dict], int | None]:
            collage_response = await _fetch_collage(tracker, collage_id, page)
            response = response_payload(collage_response, "Collage")
            page_entries = optional_list_of_dicts(response, "torrentgroups", "Collage")
            return page_entries, _parse_total_collage_pages(response)

        page_entries, total_pages = await run_with_retries(
            _fetch_and_parse_page,
            max_attempts=COLLAGE_FETCH_MAX_ATTEMPTS,
            on_retry=lambda attempt, max_attempts, delay, exc: logger.warning(
                f"Transient collage fetch failure ({tracker.name.upper()} collage #{collage_id} page {page}); "
                f"retrying in {delay}s (attempt {attempt}/{max_attempts}): {exc}"
            ),
        )
        if not page_entries:
            break
        entries.extend(page_entries)
        if total_pages is None or page >= total_pages:
            break
        page += 1
    return entries

def _resolve_tracker_by_key(trackers: dict[str, TrackerConfig], key: str) -> TrackerConfig:
    normalized_key = key.lower()
    for name, tracker in trackers.items():
        if name.lower() == normalized_key:
            return tracker
    raise KeyError(f"Tracker '{key}' not found in configuration")


async def _fetch_torrent_group(tracker: TrackerConfig, group_id: int) -> dict:
    adapter = GazelleServiceAdapter(tracker, timeout=20)
    try:
        return await adapter.get_group(group_id)
    finally:
        await adapter.close()


async def _load_entries_for_target(
    config: OatgrassConfig,
    target: str,
    tracker_key: str | None,
) -> tuple[list[dict], str | None, TrackerConfig | None, TrackerConfig | None]:
    entries: list[dict] = []
    collage_url: str | None = None
    source_tracker: TrackerConfig | None = None
    opposite_tracker: TrackerConfig | None = None

    if _is_url(target):
        if _is_group_url(urlparse(target).path):
            group_id_candidates = parse_qs(urlparse(target).query).get("id")
            if not group_id_candidates:
                raise ValueError("Group URL must include an id parameter")
            group_id = int(group_id_candidates[0])
            source_key, source_tracker = _find_tracker_by_url(config.trackers, target)
            _, opposite_tracker = _pick_opposite_tracker(config.trackers, source_key)
            entries = await _fetch_group_entries_with_retries(source_tracker, group_id)
        else:
            collage_url = target
            collage_id, page = _parse_collage_url(collage_url)
            source_key, source_tracker = _find_tracker_by_url(config.trackers, collage_url)
            _, opposite_tracker = _pick_opposite_tracker(config.trackers, source_key)
            entries = await _fetch_collage_entries(source_tracker, collage_id, page)
    else:
        try:
            group_id = int(target)
        except ValueError as exc:
            raise ValueError("Group id must be numeric") from exc
        source_key = tracker_key or "red"
        source_tracker = _resolve_tracker_by_key(config.trackers, source_key)
        _, opposite_tracker = _pick_opposite_tracker(config.trackers, source_key)
        entries = await _fetch_group_entries_with_retries(source_tracker, group_id)

    return entries, collage_url, source_tracker, opposite_tracker


def _emit_final_candidates(
    entries: list[dict],
    cross_upload_candidates: list[tuple[str, int]],
) -> None:
    if cross_upload_candidates:
        _emit("\n[End of Run]")
        _emit("  Explore the following for possible upload:")

        by_priority: dict[int, list[str]] = {}
        for url, priority in cross_upload_candidates:
            by_priority.setdefault(priority, []).append(url)

        for priority in sorted(by_priority.keys(), reverse=True):
            urls = by_priority[priority]
            priority_label = {
                100: "Priority 100 (missing group)",
                50: "Priority 50 (new edition)",
                20: "Priority 20 (new media)",
                10: "Priority 10 (new encoding)",
            }.get(priority, f"Priority {priority}")
            _emit(f"  {priority_label}:")
            for url in urls:
                _emit(f"    {url}")
    elif entries:
        _emit("\n[End of Run]")
        _emit("  No cross-upload candidates found.")


async def run_group_search_workflow(
    config: OatgrassConfig,
    target: str,
    tracker_key: str | None = None,
    strict: bool = False,
    log: bool = True,
    abbrev: bool = False,
    verbose: bool = False,
    debug: bool = False,
    basic: bool = False,
    no_discogs: bool = False,
    output_dir: Path | None = None,
) -> None:
    log_path: Path | None = None
    if log:
        out_dir = output_dir or Path("output")
        log_path = _next_run_path(out_dir)
        log_instance = logger.OatgrassLogger(log_path, debug=debug)
        logger.set_logger(log_instance)
    else:
        logger.set_logger(logger.OatgrassLogger(debug=debug))
    
    collage_url = None
    entries: list[dict] = []
    source_tracker: TrackerConfig | None = None
    opposite_tracker: TrackerConfig | None = None
    gazelle_client: GazelleServiceAdapter | None = None
    source_client: GazelleServiceAdapter | None = None
    discogs_service: Optional[object] = None
    discogs_cache: dict[str, list[str]] = {}

    try:
        try:
            entries, collage_url, source_tracker, opposite_tracker = await _load_entries_for_target(
                config,
                target,
                tracker_key,
            )
        except ValueError as exc:
            _emit(f"[red]{exc}[/red]")
            return
        except Exception as exc:  # pragma: no cover
            _emit(f"[red]Failed to load entries:[/red] {exc}")
            return

        if not entries:
            _emit("[yellow]No entries found for the provided input.[/yellow]")
            return

        if not source_tracker or not opposite_tracker:
            _emit("[red]Tracker configuration is incomplete.[/red]")
            return

        total = len(entries)
        source_label = collage_url or f"group {target}"
        _emit("[bold]Search mode: find cross-upload candidates[/bold]")
        _emit(f"Source input: {source_label}")
        _emit(f"Source tracker: {source_tracker.name}")
        _emit(f"Opposite tracker: {opposite_tracker.name}")
        if collage_url:
            _emit(f"Collage entries to process: {total}")
        else:
            _emit(f"Groups to process: {total}")
        if abbrev:
            _emit("Abbrev mode - will not report when album matches & no candidates found")

        try:
            gazelle_client = GazelleServiceAdapter(opposite_tracker)
            source_client = GazelleServiceAdapter(source_tracker)
        except ValueError as exc:
            _emit(f"[red]Could not initialize Gazelle client:[/red] {exc}")
            return
        
        if config.api_keys.discogs_key and not no_discogs:
            try:
                from oatgrass.search.discogs_service import DiscogsService
                discogs_service = DiscogsService(config.api_keys.discogs_key)
            except Exception as e:
                _emit(f"[yellow]Warning: Discogs initialization failed: {e}. Tier 5 search will be skipped.[/yellow]")

        cross_upload_candidates = []

        for idx, entry in enumerate(entries, start=1):
            search_context = _build_search_context(entry)
            source_gid = _group_id(entry)
            source_group_label = source_gid if source_gid is not None else "?"

            if not abbrev:
                _emit(f"[Task {idx} of {total}]")

            hit = None
            used_tier = 1
            try:
                if strict and not abbrev:
                    _emit(
                        f"Tier 1 search: artist='{search_context.artist}', album='{search_context.album}', year={search_context.year}",
                        indent=3,
                    )

                result = await _search_entry_with_retries(
                    gazelle_client,
                    source_tracker_name=source_tracker.name.upper(),
                    source_group_label=source_group_label,
                    artist=search_context.artist,
                    album=search_context.album,
                    year=search_context.year,
                    release_type=search_context.release_type,
                    media=search_context.media,
                    max_tier=1 if strict else 4,
                )
                if result:
                    hit = result
                    used_tier = 1

                if not hit and not strict and discogs_service and search_context.artist and search_context.album:
                    if not abbrev:
                        _emit("Tier 5 Discogs search: querying artist variations", indent=3)

                    cache_key = f"{search_context.artist}|{search_context.album}"
                    if cache_key not in discogs_cache:
                        try:
                            artist_variations = await discogs_service.get_artist_variations(
                                search_context.artist,
                                search_context.album,
                                search_context.year
                            )
                            discogs_cache[cache_key] = artist_variations
                        except Exception:
                            discogs_cache[cache_key] = []
                    for artist_variant in discogs_cache.get(cache_key, []):
                        if not abbrev:
                            _emit(f"Tier 5 tracker search: artist='{artist_variant}', album='{search_context.album}'", indent=3)
                        result = await _search_entry_with_retries(
                            gazelle_client,
                            source_tracker_name=source_tracker.name.upper(),
                            source_group_label=source_group_label,
                            artist=artist_variant,
                            album=search_context.album,
                            year=search_context.year,
                            release_type=None,
                            media=None,
                            max_tier=4,
                        )
                        if result:
                            hit = result
                            used_tier = 5
                            if not abbrev:
                                _emit(f"[green]Tier 5 match found[/green]", indent=3)
                            break
                        await asyncio.sleep(0.5)

                collage_max = _collage_max_size(entry)
                if not basic and hit and source_gid:
                    from oatgrass.search.edition_aware_mode import process_entry_edition_aware
                    try:
                        _target_gid, edition_candidates = await process_entry_edition_aware(
                            entry, source_tracker, opposite_tracker,
                            source_client, gazelle_client,
                            _emit, abbrev, verbose
                        )
                        if edition_candidates:
                            cross_upload_candidates.extend(edition_candidates)
                        if idx < total:
                            await asyncio.sleep(0.005)
                        continue
                    except Exception as e:
                        if not abbrev:
                            _emit(f"[yellow]Edition-aware processing failed: {e}. Falling back to basic mode.[/yellow]", indent=3)

                if not hit:
                    if abbrev:
                        if source_gid is not None:
                            suggestion = _cross_upload_url(source_tracker, source_gid)
                            cross_upload_candidates.append((suggestion, 100))  # Priority 100 for missing group
                            compact = _format_compact_result(
                                idx, total, source_tracker, source_gid,
                                opposite_tracker, None, collage_max, None, used_tier, suggestion
                            )
                            _emit(compact)
                    else:
                        _emit(
                            "[yellow]No matching group found on the opposite tracker.[/yellow]",
                            indent=3,
                        )
                        if source_gid is not None:
                            suggestion = _cross_upload_url(source_tracker, source_gid)
                            cross_upload_candidates.append((suggestion, 100))  # Priority 100 for missing group
                            _emit("Suggestion:", indent=3)
                            _emit(
                                f"  Explore {suggestion} for possible cross-upload to {opposite_tracker.name.upper()}",
                                indent=3,
                            )
                else:
                    search_max = _extract_search_max(hit)
                    hit_group_id = hit.get('groupId') if isinstance(hit, dict) else hit.group_id
                    hit_title = hit.get('groupName', 'Unknown') if isinstance(hit, dict) else hit.title

                    if abbrev:
                        compact = _format_compact_result(
                            idx, total, source_tracker, source_gid or 0,
                            opposite_tracker, hit_group_id, collage_max, search_max, used_tier
                        )
                        _emit(compact)
                    else:
                        _emit(
                            f"[Target, {opposite_tracker.name.upper()}] Found group: {hit_title} (ID {hit_group_id})",
                            indent=3,
                        )
                        source_label = f"[Source, {source_tracker.name.upper()}] Collage max torrent size:"
                        source_size = _format_size(collage_max)
                        _emit(_display_value(source_label, source_size), indent=3)
                        target_label = f"[Target, {opposite_tracker.name.upper()}] Tracker max torrent size:"
                        target_size = _format_size(search_max)
                        _emit(_display_value(target_label, target_size), indent=3)

                        if collage_max is None or search_max is None:
                            _emit("[yellow]Cannot determine max-size match (missing data).[/yellow]", indent=3)
                        elif collage_max == search_max:
                            _emit(
                                f"[Target, {opposite_tracker.name.upper()}] [green]Max size matches.[/green]",
                                indent=3,
                            )
                        else:
                            _emit(
                                f"[Target, {opposite_tracker.name.upper()}] [yellow]Max size mismatch.[/yellow]",
                                indent=3,
                            )
            except Exception as exc:
                logger.warning(
                    f"Entry failed after retries ({source_tracker.name.upper()} group #{source_group_label}): {exc}"
                )
                if not abbrev:
                    _emit(
                        "[yellow]Entry failed after retry attempts; skipping this entry.[/yellow]",
                        indent=3,
                    )

            if idx < total:
                await asyncio.sleep(0.005)

        _emit_final_candidates(entries, cross_upload_candidates)
    finally:
        if source_client is not None:
            await source_client.close()
        if gazelle_client is not None:
            await gazelle_client.close()
        if log_path:
            logger.info(f"Output mirrored to {log_path}")
        logger.get_logger().close()
