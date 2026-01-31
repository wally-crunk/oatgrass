from __future__ import annotations

import asyncio
from dataclasses import dataclass
from html import unescape
from typing import Sequence, TextIO
from urllib.parse import parse_qs, urlparse

from pathlib import Path

import aiohttp
from rich.console import Console

from oatgrass.config import OatgrassConfig, TrackerConfig
from oatgrass.search.gazelle_client import DEFAULT_USER_AGENT, GazelleServiceAdapter
from oatgrass.search.types import GazelleSearchResult

console = Console()


def _emit(message: str, log_handle: TextIO | None = None) -> None:
    console.print(message)
    if log_handle:
        log_handle.write(f"{message}\n")


def _next_run_path() -> Path:
    idx = 1
    while True:
        candidate = Path(f"run{idx}.txt")
        if not candidate.exists():
            return candidate
        idx += 1


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


def _context_with_unescaped(context: SearchContext) -> SearchContext:
    return SearchContext(
        artist=unescape(context.artist),
        album=unescape(context.album) if context.album else None,
        year=context.year,
        release_type=context.release_type,
        media=context.media,
    )


def _build_variations(context: SearchContext, loose: bool) -> list[SearchContext]:
    candidates: list[SearchContext] = [context]
    if not loose:
        return candidates

    unescaped = _context_with_unescaped(context)
    if unescaped != context:
        candidates.append(unescaped)

    stripped = SearchContext(
        artist=context.artist,
        album=context.album,
        year=context.year,
        release_type=None,
        media=None,
    )
    if stripped not in candidates:
        candidates.append(stripped)

    no_year = SearchContext(
        artist=context.artist,
        album=context.album,
        year=None,
        release_type=stripped.release_type,
        media=stripped.media,
    )
    if no_year not in candidates:
        candidates.append(no_year)

    if unescaped != context:
        unescaped_no_year = SearchContext(
            artist=unescaped.artist,
            album=unescaped.album,
            year=None,
            release_type=None,
            media=None,
        )
        if unescaped_no_year not in candidates:
            candidates.append(unescaped_no_year)

    return candidates


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


async def run_basic_mode(
    config: OatgrassConfig,
    target: str,
    tracker_key: str | None = None,
    loose: bool = False,
    log: bool = True,
) -> None:
    collage_url = None
    entries: list[dict] = []
    source_key: str | None = None
    source_tracker: TrackerConfig | None = None
    opposite_tracker: TrackerConfig | None = None

    parsed_url = None
    log_path: Path | None = None
    log_handle: TextIO | None = None
    if log:
        log_path = _next_run_path()
        log_handle = log_path.open("w", encoding="utf-8")

    try:
        try:
            if _is_url(target):
                parsed_url = urlparse(target)
                if _is_group_url(parsed_url.path):
                    group_id_candidates = parse_qs(parsed_url.query).get("id")
                    if not group_id_candidates:
                        raise ValueError("Group URL must include an id parameter")
                    group_id = int(group_id_candidates[0])
                    source_key = tracker_key or "red"
                    source_tracker = _resolve_tracker_by_key(config.trackers, source_key)
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
            _emit(f"[red]{exc}[/red]", log_handle)
            return
        except Exception as exc:  # pragma: no cover
            _emit(f"[red]Failed to load entries:[/red] {exc}", log_handle)
            return

        if not entries:
            _emit("[yellow]No entries found for the provided input.[/yellow]", log_handle)
            return

        if not source_tracker or not opposite_tracker:
            _emit("[red]Tracker configuration is incomplete.[/red]", log_handle)
            return

        total = len(entries)
        source_label = collage_url or f"group {target}"
        _emit("[bold]Search mode: find cross-upload candidates[/bold]", log_handle)
        _emit(f"Source input: {source_label}", log_handle)
        _emit(f"Source tracker: {source_tracker.name}", log_handle)
        _emit(f"Opposite tracker: {opposite_tracker.name}", log_handle)
        _emit(f"Collage entries to process: {total}", log_handle)

        try:
            gazelle_client = GazelleServiceAdapter(opposite_tracker)
        except ValueError as exc:
            _emit(f"[red]Could not initialize Gazelle client:[/red] {exc}", log_handle)
            return

        # main loop as before but replace console.print with _emit, using log_handle
        for idx, entry in enumerate(entries, start=1):
            _emit(f"[Task {idx} of {total}]", log_handle)
            search_context = _build_search_context(entry)
            params_desc = search_context.describe() or "none"
            _emit(f"Derived search parameters: {params_desc}", log_handle)

            candidates = _build_variations(search_context, loose)
            hits = []
            used_context = search_context
            for candidate in candidates:
                hits = await gazelle_client.search(
                    candidate.artist,
                    album=candidate.album,
                    year=candidate.year,
                    release_type=candidate.release_type,
                    media=candidate.media,
                )
                if hits:
                    used_context = candidate
                    break
            if used_context != search_context:
                _emit(
                    "[yellow]Fallback parameters applied: "
                    f"{used_context.describe() or 'none'}[/yellow]",
                    log_handle,
                )

            if not hits:
                _emit(
                    "[yellow]No matching group found on the opposite tracker.[/yellow]",
                    log_handle,
                )
                gid = _group_id(entry)
                if gid is not None:
                    suggestion = _cross_upload_url(source_tracker, gid)
                    _emit(
                        "Suggestion:\n"
                        f"  Explore {suggestion} for possible cross-upload to {opposite_tracker.name.upper()}",
                        log_handle,
                    )
            else:
                hit = hits[0]
                collage_max = _collage_max_size(entry)
                search_max = _extract_search_max(hit)
                _emit(
                    f"[Target, {opposite_tracker.name.upper()}] Found group: {hit.title} (ID {hit.group_id})",
                    log_handle,
                )
                source_label = f"[Source, {source_tracker.name.upper()}] Collage max torrent size:"
                source_size = _format_size(collage_max)
                _emit(_display_value(source_label, source_size), log_handle)
                target_label = f"[Target, {opposite_tracker.name.upper()}] Tracker max torrent size:"
                target_label = f"[Target, {opposite_tracker.name.upper()}] Tracker max torrent size:"
                target_size = _format_size(search_max)
                _emit(_display_value(target_label, target_size), log_handle)

                if collage_max is None or search_max is None:
                    _emit("[yellow]Cannot determine max-size match (missing data).[/yellow]", log_handle)
                elif collage_max == search_max:
                    _emit(
                        f"[Target, {opposite_tracker.name.upper()}] [green]Max size matches.[/green]",
                        log_handle,
                    )
                else:
                    _emit(
                        f"[Target, {opposite_tracker.name.upper()}] [magenta]Max size mismatch.[/magenta]",
                        log_handle,
                    )

            if idx < total:
                await asyncio.sleep(0.005)
    finally:
        if log_handle:
            _emit(f"[cyan][INFO][/cyan] Output mirrored to {log_path}", log_handle)
            log_handle.close()
