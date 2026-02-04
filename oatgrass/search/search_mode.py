from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from html import unescape
from typing import Sequence, TextIO, Optional
from urllib.parse import parse_qs, urlparse

from pathlib import Path

import aiohttp
from rich.console import Console
from rich.text import Text

from oatgrass.config import OatgrassConfig, TrackerConfig
from oatgrass.search.gazelle_client import DEFAULT_USER_AGENT, GazelleServiceAdapter
from oatgrass.search.types import GazelleSearchResult
from oatgrass import logger

console = Console()


def _emit(message: str, indent: int = 0) -> None:
    """Emit message to screen and log file via logger"""
    padding = " " * max(indent, 0)
    # Use logger for immediate output
    plain = Text.from_markup(message).plain
    logger.log(f"{padding}{plain}")


def _next_run_path(output_dir: Path = Path(".")) -> Path:
    """Find next available runN.txt path in output directory."""
    output_dir.mkdir(parents=True, exist_ok=True)
    
    # Find highest existing run number
    max_num = 0
    for path in output_dir.glob("run*.txt"):
        try:
            num = int(path.stem[3:])  # Extract number from "runN"
            max_num = max(max_num, num)
        except (ValueError, IndexError):
            pass
    
    return output_dir / f"run{max_num + 1}.txt"


@dataclass
class SearchContext:
    artist: str
    album: str | None
    year: int | None
    release_type: int | None
    media: str | None

    def describe(self) -> str:
        items: list[str] = []
        if self.artist:
            items.append(f"artist='{self.artist}'")
        if self.album:
            items.append(f"album='{self.album}'")
        if self.year:
            items.append(f"year={self.year}")
        if self.release_type:
            items.append(f"releasetype={self.release_type}")
        if self.media:
            items.append(f"media='{self.media}'")
        return ", ".join(items)


def _parse_collage_url(collage_url: str) -> tuple[int, int]:
    parsed = urlparse(collage_url)
    qs = parse_qs(parsed.query)
    raw_id = qs.get("id") or qs.get("Collage") or qs.get("collage")
    if not raw_id or not raw_id[0]:
        raise ValueError("Collage URL must include an id parameter")
    try:
        collage_id = int(raw_id[0])
    except ValueError as exc:
        raise ValueError("Collage id must be numeric") from exc
    page_values = qs.get("page")
    page = int(page_values[0]) if page_values and page_values[0].isdigit() else 1
    return collage_id, page


def _find_tracker_by_url(trackers: dict[str, TrackerConfig], collage_url: str) -> tuple[str, TrackerConfig]:
    parsed = urlparse(collage_url)
    for key, tracker in trackers.items():
        tracker_netloc = urlparse(tracker.url).netloc
        normalized_target = parsed.netloc.lower()
        if tracker_netloc and tracker_netloc.lower() in normalized_target:
            return key, tracker
        normalized_base = tracker.url.rstrip("/").lower()
        if collage_url.lower().startswith(normalized_base):
            return key, tracker
    raise ValueError("Collage URL does not match any configured tracker")


def _pick_opposite_tracker(trackers: dict[str, TrackerConfig], source_key: str) -> tuple[str, TrackerConfig]:
    for key, tracker in trackers.items():
        if key != source_key:
            return key, tracker
    raise ValueError("Need at least two configured trackers to run search mode: find cross-upload candidates")


def _build_headers(tracker: TrackerConfig) -> dict[str, str]:
    auth = tracker.api_key
    if tracker.name.lower() != "red":
        auth = f"token {auth}"
    return {"Authorization": auth, "User-Agent": DEFAULT_USER_AGENT}


async def _fetch_collage(tracker: TrackerConfig, collage_id: int, page: int) -> dict:
    base = tracker.url.rstrip("/") + "/ajax.php"
    params = {"action": "collage", "id": collage_id, "page": page}
    timeout = aiohttp.ClientTimeout(total=20)
    async with aiohttp.ClientSession(headers=_build_headers(tracker), timeout=timeout) as session:
        async with session.get(base, params=params) as response:
            response.raise_for_status()
            return await response.json()


def _build_search_context(entry: dict) -> SearchContext:
    group = entry.get("group", entry)
    primary_artist = ""
    for artist in group.get("musicInfo", {}).get("artists", []) or []:
        name = artist.get("name")
        if name:
            primary_artist = name
            break
    album = group.get("name") or entry.get("name")
    year = group.get("year")
    release_type = group.get("releaseType")
    torrents = entry.get("torrents") or []
    media = torrents[0].get("media") if torrents else None
    return SearchContext(
        artist=primary_artist or album or "",
        album=album,
        year=year,
        release_type=release_type,
        media=media,
    )


def _normalize_text(text: str) -> str:
    """Aggressive normalization: strip punctuation, lowercase, remove extra spaces."""
    text = text.lower()
    text = re.sub(r'[^a-z0-9\s]', ' ', text)
    text = re.sub(r'\s+', ' ', text)
    return text.strip()


def _remove_stopwords(text: str) -> str:
    """Remove common stopwords."""
    words = text.split()
    stopwords = {'the', 'a', 'an'}
    return ' '.join(w for w in words if w not in stopwords)


def _remove_volume_indicators(text: str) -> str:
    """Remove volume indicators like 'vol 1', 'volume 2', 'vol i', etc."""
    text = re.sub(r'\bvol(?:ume)?\s*[0-9ivxlcdm]+\b', '', text, flags=re.IGNORECASE)
    text = re.sub(r'\s+', ' ', text)
    return text.strip()


def _tier1_context(context: SearchContext) -> SearchContext:
    """Tier 1: Exact match - artist/album/year only (drop media/release_type)."""
    return SearchContext(
        artist=context.artist,
        album=context.album,
        year=context.year,
        release_type=None,
        media=None,
    )


def _tier2_context(context: SearchContext) -> SearchContext:
    """Tier 2: Light normalization - lowercase + HTML unescape + drop release_type/media."""
    return SearchContext(
        artist=unescape(context.artist).lower(),
        album=unescape(context.album).lower() if context.album else None,
        year=context.year,
        release_type=None,
        media=None,
    )


def _tier3_context(context: SearchContext) -> SearchContext:
    """Tier 3: Aggressive normalization - strip punctuation + remove stopwords + remove volumes + drop year."""
    artist = _normalize_text(unescape(context.artist))
    artist = _remove_stopwords(artist)
    
    album = None
    if context.album:
        album = _normalize_text(unescape(context.album))
        album = _remove_stopwords(album)
        album = _remove_volume_indicators(album)
    
    return SearchContext(
        artist=artist,
        album=album,
        year=None,
        release_type=None,
        media=None,
    )


def _tier4_context(context: SearchContext) -> SearchContext:
    """Tier 4: Colon cutoff - truncate album at first colon, then apply Tier 3 normalization."""
    artist = _normalize_text(unescape(context.artist))
    artist = _remove_stopwords(artist)
    
    album = None
    if context.album:
        # Cut off at first colon
        album_text = unescape(context.album)
        if ':' in album_text:
            album_text = album_text.split(':', 1)[0].strip()
        album = _normalize_text(album_text)
        album = _remove_stopwords(album)
        album = _remove_volume_indicators(album)
    
    return SearchContext(
        artist=artist,
        album=album,
        year=None,
        release_type=None,
        media=None,
    )


def _group_id(entry: dict) -> int | None:
    group = entry.get("group", entry)
    value = group.get("id")
    return _as_int(value)


def _cross_upload_url(tracker: TrackerConfig, group_id: int) -> str:
    return f"{tracker.url.rstrip('/')}/torrents.php?id={group_id}"


def _collage_max_size(entry: dict) -> int | None:
    group = entry.get("group", entry)
    for key in ("maxsize", "max_size", "maxSize"):
        candidate = group.get(key)
        parsed = _as_int(candidate)
        if parsed is not None:
            return parsed

    torrents = entry.get("torrents") or []
    sizes: list[int] = []
    for torrent in torrents:
        size = torrent.get("size")
        parsed = _as_int(size)
        if parsed is not None:
            sizes.append(parsed)
    return max(sizes) if sizes else None


def _format_size(size: int | None) -> str:
    if size is None:
        return "unknown"
    return f"{size:,}"


def _display_value(label: str, value: str) -> str:
    target_col = 40
    value_width = 15
    if len(label) >= target_col:
        return f"{label} {value.rjust(value_width)}"
    return f"{label.ljust(target_col)}{value.rjust(value_width)}"


def _extract_search_max(hit: GazelleSearchResult) -> int | None:
    for key in ("maxsize", "max_size"):
        value = hit.metadata.get(key)
        if value is not None:
            parsed = _as_int(value)
            if parsed is not None:
                return parsed
    return None


def _as_int(value: object) -> int | None:
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        cleaned = value.replace(",", "").strip()
        if cleaned.isdigit():
            return int(cleaned)
    return None


def _is_url(target: str) -> bool:
    return target.startswith("http://") or target.startswith("https://")


def _is_group_url(path: str) -> bool:
    lowered = path.lower()
    return "torrents.php" in lowered or "torrentgroup" in lowered


def _resolve_tracker_by_key(trackers: dict[str, TrackerConfig], key: str) -> TrackerConfig:
    normalized_key = key.lower()
    for name, tracker in trackers.items():
        if name.lower() == normalized_key:
            return tracker
    raise KeyError(f"Tracker '{key}' not found in configuration")


async def _fetch_torrent_group(tracker: TrackerConfig, group_id: int) -> dict:
    base = tracker.url.rstrip("/") + "/ajax.php"
    params = {"action": "torrentgroup", "id": group_id}
    timeout = aiohttp.ClientTimeout(total=20)
    async with aiohttp.ClientSession(headers=_build_headers(tracker), timeout=timeout) as session:
        async with session.get(base, params=params) as response:
            response.raise_for_status()
            return await response.json()


def _format_compact_result(
    idx: int,
    total: int,
    source_tracker: TrackerConfig,
    source_gid: int,
    opposite_tracker: TrackerConfig,
    target_gid: int | None,
    source_max: int | None,
    target_max: int | None,
    tier_used: int = 1,
    cross_upload_url: str | None = None,
) -> str:
    """Format a compact one-line result for abbreviated mode."""
    source_name = source_tracker.name.upper()
    target_name = opposite_tracker.name.upper()
    tier_indicator = f"{tier_used}🔍" if tier_used > 1 else "="
    
    if target_gid is None:
        return f"[Task {idx} of {total}] {tier_indicator} {source_name}={source_gid}; {target_name} not found; Explore {cross_upload_url}"
    
    if source_max is None or target_max is None:
        return f"[Task {idx} of {total}] {tier_indicator} {source_name}={source_gid}; {target_name}={target_gid}; size unknown"
    
    if source_max == target_max:
        return f"[Task {idx} of {total}] {tier_indicator} {source_name}={source_gid}; {target_name}={target_gid}; {_format_size(source_max)} (equal)"
    
    if source_max > target_max:
        return f"[Task {idx} of {total}] {tier_indicator} {source_name}={source_gid}; {target_name}={target_gid}; {_format_size(source_max)} vs {_format_size(target_max)} (smaller)"
    else:
        return f"[Task {idx} of {total}] {tier_indicator} {source_name}={source_gid}; {target_name}={target_gid}; {_format_size(source_max)} vs {_format_size(target_max)} (larger)"


async def run_search_mode(
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
    # Initialize logger with debug mode
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
    source_key: str | None = None
    source_tracker: TrackerConfig | None = None
    opposite_tracker: TrackerConfig | None = None
    discogs_service: Optional[object] = None
    discogs_cache: dict[str, list[str]] = {}  # Cache artist variations

    parsed_url = None

    try:
        try:
            if _is_url(target):
                parsed_url = urlparse(target)
                if _is_group_url(parsed_url.path):
                    group_id_candidates = parse_qs(parsed_url.query).get("id")
                    if not group_id_candidates:
                        raise ValueError("Group URL must include an id parameter")
                    group_id = int(group_id_candidates[0])
                    source_key, source_tracker = _find_tracker_by_url(config.trackers, target)
                    _, opposite_tracker = _pick_opposite_tracker(config.trackers, source_key)
                    group_response = await _fetch_torrent_group(source_tracker, group_id)
                    resp = group_response.get("response", {})
                    group = resp.get("group", {})
                    torrents = resp.get("torrents") or resp.get("torrent") or []
                    entries = [{"group": group, "torrents": torrents}]
                else:
                    collage_url = target
                    collage_id, page = _parse_collage_url(collage_url)
                    source_key, source_tracker = _find_tracker_by_url(config.trackers, collage_url)
                    _, opposite_tracker = _pick_opposite_tracker(config.trackers, source_key)
                    collage_response = await _fetch_collage(source_tracker, collage_id, page)
                    entries = collage_response.get("response", {}).get("torrentgroups") or []
            else:
                try:
                    group_id = int(target)
                except ValueError:
                    raise ValueError("Group id must be numeric")
                source_key = tracker_key or "red"
                source_tracker = _resolve_tracker_by_key(config.trackers, source_key)
                _, opposite_tracker = _pick_opposite_tracker(config.trackers, source_key)
                group_response = await _fetch_torrent_group(source_tracker, group_id)
                resp = group_response.get("response", {})
                group = resp.get("group", {})
                torrents = resp.get("torrents") or resp.get("torrent") or []
                entries = [{"group": group, "torrents": torrents}]
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
        
        # Initialize Discogs service if key is configured and not disabled
        if config.api_keys.discogs_key and not no_discogs:
            try:
                from oatgrass.search.discogs_service import DiscogsService
                discogs_service = DiscogsService(config.api_keys.discogs_key)
            except Exception as e:
                _emit(f"[yellow]Warning: Discogs initialization failed: {e}. Tier 5 search will be skipped.[/yellow]")

        cross_upload_candidates = []  # List of (url, priority) tuples

        for idx, entry in enumerate(entries, start=1):
            search_context = _build_search_context(entry)
            
            if not abbrev:
                _emit(f"[Task {idx} of {total}]")

            # 4-tier search strategy (unless strict mode)
            tiers = [_tier1_context] if strict else [_tier1_context, _tier2_context, _tier3_context, _tier4_context]
            results_list = []
            used_tier = 1
            used_context = search_context
            
            for tier_num, tier_func in enumerate(tiers, start=1):
                candidate = tier_func(search_context)
                
                # Skip if same as previous tier
                if tier_num > 1 and candidate.describe() == used_context.describe():
                    if not abbrev:
                        _emit(f"Tier {tier_num} search: (no further refinement)", indent=3)
                    continue
                
                if not abbrev:
                    _emit(f"Tier {tier_num} search: {candidate.describe() or 'none'}", indent=3)
                
                logger.get_logger().debug(f"Tier {tier_num} API call: {candidate.describe()}")
                
                response = await gazelle_client.search(
                    candidate.artist,
                    album=candidate.album,
                    year=candidate.year,
                    release_type=candidate.release_type,
                    media=candidate.media,
                )
                # Extract results list from API response
                results_list = response.get('response', {}).get('results', []) if isinstance(response, dict) else []
                # Map to GazelleSearchResult objects
                if results_list:
                    results_list = [gazelle_client._map_result(r) for r in results_list]
                if results_list:
                    used_tier = tier_num
                    used_context = candidate
                    if not abbrev:
                        _emit(f"[green]Tier {tier_num} match found[/green]", indent=3)
                    break
                used_context = candidate
            
            # Tier 5: Discogs ANV fallback (only if Tiers 1-4 failed and Discogs is available)
            if not results_list and discogs_service and search_context.artist and search_context.album:
                if not abbrev:
                    _emit("Tier 5 Discogs search: querying artist variations", indent=3)
                
                cache_key = f"{search_context.artist}|{search_context.album}"
                
                # Check cache first
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
                
                # Try each artist variation with Tier 3 normalization
                for artist_variant in discogs_cache.get(cache_key, []):
                    tier3_ctx = _tier3_context(SearchContext(
                        artist=artist_variant,
                        album=search_context.album,
                        year=search_context.year,
                        release_type=None,
                        media=None
                    ))
                    if not abbrev:
                        _emit(f"Tier 5 tracker search: {tier3_ctx.describe() or 'none'}", indent=3)
                    response = await gazelle_client.search(
                        tier3_ctx.artist,
                        album=tier3_ctx.album,
                        year=tier3_ctx.year
                    )
                    results_list = response.get('response', {}).get('results', []) if isinstance(response, dict) else []
                    if results_list:
                        results_list = [gazelle_client._map_result(r) for r in results_list]
                    if results_list:
                        used_tier = 5
                        used_context = tier3_ctx
                        if not abbrev:
                            _emit(f"[green]Tier 5 match found[/green]", indent=3)
                        break
                    await asyncio.sleep(0.5)

            source_gid = _group_id(entry)
            collage_max = _collage_max_size(entry)
            
            # Edition-aware processing (unless --basic flag is set)
            if not basic and results_list and source_gid:
                from oatgrass.search.edition_aware_mode import process_entry_edition_aware
                try:
                    target_gid, edition_candidates = await process_entry_edition_aware(
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
            
            # Basic mode processing (original logic)
            
            if not results_list:
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
                hit = results_list[0]
                search_max = _extract_search_max(hit)
                
                if abbrev:
                    compact = _format_compact_result(
                        idx, total, source_tracker, source_gid or 0,
                        opposite_tracker, hit.group_id, collage_max, search_max, used_tier
                    )
                    _emit(compact)
                else:
                    _emit(
                        f"[Target, {opposite_tracker.name.upper()}] Found group: {hit.title} (ID {hit.group_id})",
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
                            f"[Target, {opposite_tracker.name.upper()}] [magenta]Max size mismatch.[/magenta]",
                            indent=3,
                        )

            if idx < total:
                await asyncio.sleep(0.005)

        if cross_upload_candidates:
            _emit("\n[End of Run]")
            _emit("  Explore the following for possible upload:")
            
            # Group by priority
            by_priority = {}
            for url, priority in cross_upload_candidates:
                by_priority.setdefault(priority, []).append(url)
            
            # Display in priority order (highest first)
            for priority in sorted(by_priority.keys(), reverse=True):
                urls = by_priority[priority]
                priority_label = {
                    100: "Priority 100 (missing group)",
                    50: "Priority 50 (new edition)",
                    20: "Priority 20 (new media)",
                    10: "Priority 10 (new encoding)"
                }.get(priority, f"Priority {priority}")
                
                _emit(f"  {priority_label}:")
                for url in urls:
                    _emit(f"    {url}")
        elif entries:
            _emit("\n[End of Run]")
            _emit("  No cross-upload candidates found.")
    finally:
        if log_path:
            logger.info(f"Output mirrored to {log_path}")
        logger.get_logger().close()
