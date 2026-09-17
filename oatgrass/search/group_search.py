from __future__ import annotations

import asyncio
import re
import time
from typing import Optional
from urllib.parse import parse_qs, urlparse

from pathlib import Path

from oatgrass.config import OatgrassConfig, TrackerConfig
from oatgrass import rate_limits
from oatgrass.progress_timing import (
    build_task_timing_phrase,
    format_elapsed_clock,
    format_progress_bar,
    format_remaining,
)
from oatgrass.search.formatters import (
    emit as _emit,
    emit_error as _emit_error,
    emit_success as _emit_success,
    emit_warning as _emit_warning,
    emit_result_candidate as _emit_result_candidate,
    emit_result_possible_candidate as _emit_result_possible_candidate,
    emit_result_duplicate as _emit_result_duplicate,
    emit_progress as _emit_progress,
    display_value as _display_value,
    format_compact_result as _format_compact_result,
    format_size as _format_size,
    format_task_context_line as _format_task_context_line,
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
    describe_exception,
    optional_list_of_dicts,
    response_payload,
    run_with_retries,
)
from oatgrass.search.tier_search_service import search_with_tiers
from oatgrass.search.candidate_policy import (
    CandidatePolicy,
    PolicySummary,
)
from oatgrass.search.policy_candidate_resolution import (
    build_no_match_candidates_from_entry,
    resolve_policy_candidates,
)
from oatgrass import logger

SEARCH_ENTRY_MAX_ATTEMPTS = 3
COLLAGE_FETCH_MAX_ATTEMPTS = 3
_ALNUM_RE = re.compile(r"[a-z0-9]", re.IGNORECASE)


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
            f"retrying in {delay}s (attempt {attempt}/{max_attempts}): {describe_exception(exc)}"
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
            f"retrying in {delay}s (attempt {attempt}/{max_attempts}): {describe_exception(exc)}"
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


def _has_alnum_text(value: str | None) -> bool:
    return bool(_ALNUM_RE.search((value or "").strip()))


def _is_placeholder_only_search_context(search_context: SearchContext) -> bool:
    return not _has_alnum_text(search_context.artist) and not _has_alnum_text(search_context.album)


async def _evaluate_no_match_policy_candidates(
    entry: dict,
    *,
    source_tracker: TrackerConfig,
    source_client: GazelleServiceAdapter,
    policy: CandidatePolicy,
    enrichment_cache: dict[int, dict] | None = None,
) -> tuple[list[tuple[str, int]], list[str], PolicySummary]:
    candidates = build_no_match_candidates_from_entry(entry)
    return await resolve_policy_candidates(
        candidates,
        source_tracker=source_tracker,
        source_client=source_client,
        policy=policy,
        enrichment_cache=enrichment_cache,
    )


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
    total_pages: int | None = None
    log = logger.get_logger()
    started_at = time.monotonic()
    # Multi-page collages can take a long time under heavy pacing (each page
    # is its own paced request) with nothing else printed between pages --
    # without this, a large collage looks completely frozen until every page
    # has been fetched. live_progress() renders a task line (this page, its
    # projected remaining time) and, only while a wait is actually in
    # progress, a braced "waiting" line under it -- two separate facts
    # instead of one long line straining to say both, and Rich's Live tracks
    # its own rendered height so a wrap on a narrow terminal can't corrupt
    # it the way the old single-row "\r" overwrite could.
    with log.live_progress():
        while True:
            page_label = f"[Page {page} of {total_pages}]" if total_pages else f"[Page {page}]"
            # None until total_pages is known (page 1) -- there's nothing
            # meaningful to project a total from yet.
            remaining_pages = (total_pages - page + 1) if total_pages else None

            def _current_task_text(
                _page_label: str = page_label,
                _remaining_pages: int | None = remaining_pages,
                _page: int = page,
                _total_pages: int | None = total_pages,
            ) -> str:
                # Recomputed on every render (not a frozen string) so elapsed
                # keeps ticking for as long as this task line is on screen --
                # including while a wait line is braced under it, which can
                # itself run for tens of seconds.
                elapsed_text = format_elapsed_clock(time.monotonic() - started_at)
                participant_count = rate_limits.get_last_known_participant_count(tracker.name)
                session_word = "session" if participant_count == 1 else "sessions"
                # The remaining-time figure is projected from the tracker's
                # own pacing scheme (requests/window * remaining pages *
                # last-known participant count) rather than this call's
                # historical throughput -- a blended average of fetch
                # latency and pacing waits reacts far too slowly to a real
                # regime change (e.g. other OATGRASS sessions joining
                # mid-run).
                if _remaining_pages is None:
                    per_page = rate_limits.estimate_pacing_wait_seconds(
                        tracker.name, "collage", remaining_requests=1, active_participant_count=participant_count
                    )
                    pace_text = f"~{round(per_page)}s/page" if per_page is not None else "pace unknown"
                    bar = ""
                else:
                    total_wait = rate_limits.estimate_pacing_wait_seconds(
                        tracker.name, "collage", remaining_requests=_remaining_pages, active_participant_count=participant_count
                    )
                    pace_text = f"~{format_remaining(total_wait)} left" if total_wait is not None else "pace unknown"
                    # Narrower than the default (25): this line's budget also
                    # has to fit page/session numbers that can run to several
                    # digits, unlike the wait line's fixed-width wording.
                    bar = f" {format_progress_bar((_page - 1) / _total_pages, width=18)}"
                return f"{_page_label} —— {elapsed_text} elapsed, {pace_text} ({participant_count} {session_word}){bar}"

            log.set_live_task(_current_task_text)

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
                    f"retrying in {delay}s (attempt {attempt}/{max_attempts}): {describe_exception(exc)}"
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
        source_key = tracker_key or max(config.trackers)
        source_tracker = _resolve_tracker_by_key(config.trackers, source_key)
        _, opposite_tracker = _pick_opposite_tracker(config.trackers, source_key)
        entries = await _fetch_group_entries_with_retries(source_tracker, group_id)

    return entries, collage_url, source_tracker, opposite_tracker


def _emit_final_candidates(
    entries: list[dict],
    cross_upload_candidates: list[tuple[str, int]],
    policy_summary: PolicySummary | None = None,
    placeholder_skipped: int = 0,
) -> None:
    if not cross_upload_candidates and not entries:
        return

    _emit("")
    _emit_progress("[End of Run]")

    if cross_upload_candidates:
        _emit("Explore the following for possible upload:", indent=3)

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
            _emit(f"{priority_label}:", indent=3)
            for url in urls:
                _emit(url, indent=6)
    elif entries:
        _emit("No cross-upload candidates found.", indent=3)

    if policy_summary is not None:
        _emit("Policy summary:", indent=3)
        _emit(f"Promoted: {policy_summary.promoted}", indent=6)
        _emit(f"Demoted: {policy_summary.demoted}", indent=6)
        _emit(f"Excluded by policy: {policy_summary.excluded_by_policy}", indent=6)
        _emit(f"Dropped duplicate 24-bit Vinyl: {policy_summary.duplicate_24bit}", indent=6)
        _emit(f"Suppressed total: {policy_summary.suppressed_total}", indent=6)
    if placeholder_skipped > 0:
        _emit(
            f"Skipped {placeholder_skipped} item(s): source artist/album metadata is non-alphanumeric, can't search.",
            indent=3,
        )


async def run_group_search_workflow(
    config: OatgrassConfig,
    target: str,
    tracker_key: str | None = None,
    strict: bool = False,
    log: bool = True,
    abbrev: bool = False,
    debug: bool = False,
    basic: bool = False,
    no_discogs: bool = False,
    candidate_policy: CandidatePolicy = CandidatePolicy.STANDARD,
    output_dir: Path | None = None,
) -> None:
    log_path: Path | None = None
    if log:
        out_dir = output_dir or Path("output")
        log_path = _next_run_path(out_dir)
        log_instance = logger.OatgrassLogger(log_path, debug=debug, show_pacing_status=not abbrev)
        logger.set_logger(log_instance)
    else:
        logger.set_logger(logger.OatgrassLogger(debug=debug, show_pacing_status=not abbrev))
    
    collage_url = None
    entries: list[dict] = []
    source_tracker: TrackerConfig | None = None
    opposite_tracker: TrackerConfig | None = None
    gazelle_client: GazelleServiceAdapter | None = None
    source_client: GazelleServiceAdapter | None = None
    discogs_service: Optional[object] = None
    discogs_cache: dict[str, list[str]] = {}

    try:
        from oatgrass.rate_limits import describe_slow_mode

        # Always announce (not describe_slow_mode_once()): this run gets its
        # own fresh log file and must document its own settings regardless of
        # whether a prior menu screen already showed the notice elsewhere.
        if slow_mode_note := describe_slow_mode():
            logger.info(slow_mode_note)
        try:
            entries, collage_url, source_tracker, opposite_tracker = await _load_entries_for_target(
                config,
                target,
                tracker_key,
            )
        except ValueError as exc:
            _emit_error(str(exc))
            return
        except Exception as exc:  # pragma: no cover
            _emit_error(f"Failed to load entries: {exc}")
            return

        if not entries:
            _emit_warning("No entries found for the provided input.")
            return

        if not source_tracker or not opposite_tracker:
            _emit_error("Tracker configuration is incomplete.")
            return

        total = len(entries)
        source_label = collage_url or f"group {target}"
        _emit("Search mode: find cross-upload candidates")
        _emit(f"Source input: {source_label}")
        _emit(f"Source tracker: {source_tracker.name}")
        _emit(f"Opposite tracker: {opposite_tracker.name}")
        if candidate_policy != CandidatePolicy.STANDARD:
            total_source_torrents = sum(
                len(item.get("torrents") or []) for item in entries if isinstance(item, dict)
            )
            _emit(f"Candidate policy: {candidate_policy.value}")
            _emit(f"Policy rules: evaluating {total_source_torrents} source torrent(s) at startup.")
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
            _emit_error(f"Could not initialize Gazelle client: {exc}")
            return
        
        if config.api_keys.discogs_key and not no_discogs:
            try:
                from oatgrass.search.discogs_service import DiscogsService
                discogs_service = DiscogsService(config.api_keys.discogs_key)
            except Exception as e:
                _emit_warning(f"Discogs initialization failed: {e}. Tier 5 search will be skipped.")

        cross_upload_candidates = []
        policy_summary = PolicySummary()
        enrichment_cache: dict[int, dict] = {}
        placeholder_skipped = 0
        show_task_context = not abbrev
        started_at = time.monotonic()

        try:
            with logger.get_logger().live_progress():
                for idx, entry in enumerate(entries, start=1):
                    search_context = _build_search_context(entry)
                    source_gid = _group_id(entry)
                    source_group_label = source_gid if source_gid is not None else "?"
                    def _emit_task_context(target_gid: int | None) -> None:
                        if show_task_context:
                            _emit(
                                _format_task_context_line(
                                    source_tracker.name,
                                    source_group_label,
                                    opposite_tracker.name,
                                    target_gid,
                                ),
                                indent=3,
                            )

                    # Computed unconditionally (not gated on abbrev): compact mode
                    # folds this into its one-line result instead of a separate
                    # header, but it must not drop the elapsed/remaining/ETA data
                    # entirely -- that was a real information loss, not just a
                    # density trade.
                    timing_phrase = build_task_timing_phrase(
                        total=total,
                        completed=idx - 1,
                        started_at=started_at,
                    )
                    if not abbrev:
                        _emit("")
                        _emit_progress(f"[Task {idx} of {total}] —— {timing_phrase}")

                        def _current_task_text(_idx: int = idx, _total: int = total) -> str:
                            # Recomputed on every render (not a frozen string) so
                            # elapsed keeps ticking while a pacing-wait line is
                            # braced under this entry's task header.
                            return f"[Task {_idx} of {_total}] —— {build_task_timing_phrase(total=_total, completed=_idx - 1, started_at=started_at)}"

                        logger.get_logger().set_live_task(_current_task_text)

                    hit = None
                    used_tier = 1
                    try:
                        if _is_placeholder_only_search_context(search_context):
                            placeholder_skipped += 1
                            if not abbrev:
                                _emit_warning(
                                    "Skipping target search: source artist/album metadata is placeholder-only.",
                                    indent=3,
                                )
                        else:
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
                                    if not abbrev:
                                        # One external network round-trip with nothing else
                                        # printed around it -- without this, it can look
                                        # frozen between the static "querying" line above
                                        # and whatever prints next.
                                        logger.get_logger().status(
                                            f"   Querying Discogs for '{search_context.artist}' name variations..."
                                        )
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
                                            _emit_success("Tier 5 match found", indent=3)
                                        break
                                    await asyncio.sleep(0.5)

                        collage_max = _collage_max_size(entry)
                        if not basic and hit and source_gid:
                            from oatgrass.search.edition_aware_mode import process_entry_edition_aware
                            try:
                                _target_gid, edition_candidates, suppression_messages, entry_summary = await process_entry_edition_aware(
                                    entry, source_tracker, opposite_tracker,
                                    source_client, gazelle_client,
                                    _emit,
                                    _emit_warning,
                                    abbrev,
                                    candidate_policy=candidate_policy,
                                    show_context_line=show_task_context,
                                    enrichment_cache=enrichment_cache,
                                    emit_result_candidate_func=_emit_result_candidate,
                                    emit_result_duplicate_func=_emit_result_duplicate,
                                )
                                policy_summary.merge(entry_summary)
                                if not abbrev:
                                    for message in suppression_messages:
                                        _emit(message, indent=3)
                                if edition_candidates:
                                    cross_upload_candidates.extend(edition_candidates)
                                if idx < total:
                                    await asyncio.sleep(0.005)
                                continue
                            except Exception as e:
                                if not abbrev:
                                    if logger.is_internal_defect(e):
                                        # Distinguish a genuine internal bug from a routine
                                        # failure (throttle, network hiccup) reaching this
                                        # catch, so a real defect isn't shown with the same
                                        # reassuring "Falling back to basic mode" wording as
                                        # expected pacing pushback.
                                        _emit_warning(
                                            f"Edition-aware processing hit an unexpected internal error "
                                            f"({type(e).__name__}: {e}); falling back to basic mode.",
                                            indent=3,
                                        )
                                    else:
                                        _emit_warning(
                                            f"Edition-aware processing failed ({e}); falling back to basic mode.",
                                            indent=3,
                                        )

                        if not hit:
                            if candidate_policy != CandidatePolicy.STANDARD and source_gid is not None and source_client is not None:
                                no_match_candidates, suppression_messages, entry_summary = await _evaluate_no_match_policy_candidates(
                                    entry,
                                    source_tracker=source_tracker,
                                    source_client=source_client,
                                    policy=candidate_policy,
                                    enrichment_cache=enrichment_cache,
                                )
                                policy_summary.merge(entry_summary)
                                if not abbrev:
                                    _emit_task_context(None)
                                    _emit(
                                        "No matching group found on the opposite tracker.",
                                        indent=3,
                                    )
                                    for message in suppression_messages:
                                        _emit(message, indent=3)
                                    if no_match_candidates:
                                        _emit_result_candidate(
                                            f"No matching group found. {len(no_match_candidates)} policy-eligible upload candidate(s).",
                                            indent=3,
                                        )
                                    else:
                                        _emit("No matching group found. No policy-eligible upload candidates.", indent=3)
                                cross_upload_candidates.extend(no_match_candidates)
                                if idx < total:
                                    await asyncio.sleep(0.005)
                                continue

                            if abbrev:
                                if source_gid is not None:
                                    suggestion = _cross_upload_url(source_tracker, source_gid)
                                    cross_upload_candidates.append((suggestion, 100))  # Priority 100 for missing group
                                    compact = _format_compact_result(
                                        idx, total, timing_phrase, source_tracker, source_gid,
                                        opposite_tracker, None, collage_max, None, used_tier, suggestion
                                    )
                                    _emit(compact)
                            else:
                                _emit_task_context(None)
                                if source_gid is not None:
                                    suggestion = _cross_upload_url(source_tracker, source_gid)
                                    cross_upload_candidates.append((suggestion, 100))  # Priority 100 for missing group
                                    # No group at all on the opposite tracker is the
                                    # clearest possible verdict: this is a candidate,
                                    # not a warning about the run.
                                    _emit_result_candidate(
                                        "No matching group found on the opposite tracker.",
                                        indent=3,
                                    )
                                    _emit("Suggestion:", indent=3)
                                    _emit(
                                        f"  Explore {suggestion} for possible cross-upload to {opposite_tracker.name.upper()}",
                                        indent=3,
                                    )
                                else:
                                    _emit_warning(
                                        "No matching group found on the opposite tracker.",
                                        indent=3,
                                    )
                        else:
                            search_max = _extract_search_max(hit)
                            hit_group_id = hit.get('groupId') if isinstance(hit, dict) else hit.group_id
                            hit_title = hit.get('groupName', 'Unknown') if isinstance(hit, dict) else hit.title

                            if abbrev:
                                compact = _format_compact_result(
                                    idx, total, timing_phrase, source_tracker, source_gid or 0,
                                    opposite_tracker, hit_group_id, collage_max, search_max, used_tier
                                )
                                _emit(compact)
                            else:
                                _emit_task_context(hit_group_id)
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
                                    _emit_warning("Cannot determine max-size match (missing data).", indent=3)
                                elif collage_max == search_max:
                                    # Target already has this exact torrent -- a
                                    # match-verdict, not confirmation the run itself
                                    # went fine, so it gets its own vocabulary rather
                                    # than [OK].
                                    _emit_result_duplicate(
                                        f"[Target, {opposite_tracker.name.upper()}] Max size matches.",
                                        indent=3,
                                    )
                                else:
                                    # Group exists but this size doesn't -- ambiguous
                                    # (could be a genuine gap, could be mislabeled),
                                    # worth a human look. Not an operational warning.
                                    _emit_result_possible_candidate(
                                        f"[Target, {opposite_tracker.name.upper()}] Max size mismatch.",
                                        indent=3,
                                    )
                    except rate_limits.PacingCoordinationUnavailable:
                        raise
                    except Exception as exc:
                        if not logger.was_reported(exc):
                            logger.warning(
                                f"Entry failed after retries ({source_tracker.name.upper()} group #{source_group_label}): {exc}"
                            )
                        if not abbrev:
                            _emit_warning(
                                "Entry failed after retry attempts; skipping this entry.",
                                indent=3,
                            )

                    if idx < total:
                        await asyncio.sleep(0.005)
        finally:
            _emit_final_candidates(
                entries,
                cross_upload_candidates,
                policy_summary if candidate_policy != CandidatePolicy.STANDARD else None,
                placeholder_skipped=placeholder_skipped,
            )
    finally:
        if source_client is not None:
            await source_client.close()
        if gazelle_client is not None:
            await gazelle_client.close()
        if log_path:
            logger.get_logger().log(f"Output mirrored to {log_path}", "[INFO] ")
        logger.get_logger().close()
