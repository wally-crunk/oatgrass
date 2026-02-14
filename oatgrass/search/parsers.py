from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import parse_qs, urlparse

from oatgrass.search.types import GazelleSearchResult


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


def as_int(value: object) -> int | None:
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        cleaned = value.replace(",", "").strip()
        if cleaned.isdigit():
            return int(cleaned)
    return None


def parse_collage_url(collage_url: str) -> tuple[int, int]:
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


def build_search_context(entry: dict) -> SearchContext:
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


def group_id(entry: dict) -> int | None:
    group = entry.get("group", entry)
    value = group.get("id")
    return as_int(value)


def collage_max_size(entry: dict) -> int | None:
    group = entry.get("group", entry)
    for key in ("maxsize", "max_size", "maxSize"):
        candidate = group.get(key)
        parsed = as_int(candidate)
        if parsed is not None:
            return parsed

    torrents = entry.get("torrents") or []
    sizes: list[int] = []
    for torrent in torrents:
        size = torrent.get("size")
        parsed = as_int(size)
        if parsed is not None:
            sizes.append(parsed)
    return max(sizes) if sizes else None


def extract_search_max(hit) -> int | None:
    if isinstance(hit, dict):
        for key in ("maxsize", "max_size", "maxSize"):
            value = hit.get(key)
            if value is not None:
                parsed = as_int(value)
                if parsed is not None:
                    return parsed
        return None

    for key in ("maxsize", "max_size"):
        value = hit.metadata.get(key)
        if value is not None:
            parsed = as_int(value)
            if parsed is not None:
                return parsed
    return None
