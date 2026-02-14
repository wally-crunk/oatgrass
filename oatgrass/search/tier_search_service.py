"""5-tier search coordinator for edition-aware search."""

import re
from html import unescape
from typing import Optional
from difflib import SequenceMatcher

from oatgrass.search.gazelle_client import GazelleServiceAdapter

STOPWORDS = {"the", "a", "an"}
VERSION_INDICATORS = {"deluxe", "demo", "demos", "remaster", "remastered", "expanded", "edition", "anniversary", "special", "bonus", "live", "outtakes"}
COMPILATION_INDICATORS = {"collection", "compilation", "greatest", "hits", "best", "anthology"}


def _normalize_text(text: str) -> str:
    text = text.lower()
    text = re.sub(r'[^a-z0-9\s]', ' ', text)
    text = re.sub(r'\s+', ' ', text)
    return text.strip()


def _remove_stopwords(text: str) -> str:
    words = text.split()
    return " ".join(w for w in words if w not in STOPWORDS)


def _remove_volume_indicators(text: str) -> str:
    text = re.sub(r'\bvol(?:ume)?\s*[0-9ivxlcdm]+\b', '', text, flags=re.IGNORECASE)
    text = re.sub(r'\s+', ' ', text)
    return text.strip()


def _strip_leading_article(text: str) -> str:
    return re.sub(r"^(?:the|a|an)\s+", "", text, flags=re.IGNORECASE).strip()


def _coerce_year(value: object) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _score_result(
    result: dict,
    query_artist: str,
    query_album: Optional[str],
    query_year: Optional[int | str],
) -> float:
    """Score a search result by how well it matches the query."""
    score = 0.0
    result_artist = result.get("artist", "")
    result_album = result.get("groupName", "")
    result_year = _coerce_year(result.get("groupYear"))
    query_year_value = _coerce_year(query_year)

    if result_artist and query_artist:
        score += SequenceMatcher(None, result_artist.lower(), query_artist.lower()).ratio() * 40

    if result_album and query_album:
        score += SequenceMatcher(None, result_album.lower(), query_album.lower()).ratio() * 40
        result_words = set(result_album.lower().split())
        query_words = set(query_album.lower().split())
        extra_words = result_words - query_words
        if extra_words & VERSION_INDICATORS:
            score -= 15
        if extra_words & COMPILATION_INDICATORS:
            score -= 60

    if query_year_value is not None and result_year is not None:
        year_delta = abs(result_year - query_year_value)
        if year_delta == 0:
            score += 20
        elif year_delta == 1:
            score += 15
        elif year_delta <= 2:
            score += 10

    return score


def _select_best_result(
    hits: list,
    query_artist: str,
    query_album: Optional[str],
    query_year: Optional[int | str],
) -> dict:
    """Select the best matching result from hits."""
    if not hits:
        return None
    return max(hits, key=lambda hit: _score_result(hit, query_artist, query_album, query_year))


async def search_with_tiers(
    client: GazelleServiceAdapter,
    artist: str,
    album: Optional[str],
    year: Optional[int | str],
    release_type: Optional[int] = None,
    media: Optional[str] = None,
    max_tier: int = 4,
) -> Optional[dict]:
    """Search using a tiered strategy, return best match or None."""
    if max_tier < 1 or max_tier > 4:
        raise ValueError("max_tier must be between 1 and 4")

    async def _search(
        *,
        artistname: str,
        groupname: Optional[str],
        year_param: Optional[int | str],
        release_type_param: Optional[int] = None,
        media_param: Optional[str] = None,
    ) -> Optional[dict]:
        results = await client.search(
            artistname=artistname,
            groupname=groupname,
            year=year_param,
            release_type=release_type_param,
            media=media_param,
        )
        hits = results.get("response", {}).get("results", [])
        if not hits:
            return None
        return _select_best_result(hits, artist, album, year)

    result = await _search(
        artistname=artist,
        groupname=album,
        year_param=year,
        release_type_param=release_type,
        media_param=media,
    )
    if result:
        return result

    artist_no_article = _strip_leading_article(artist)
    if artist_no_article and artist_no_article.lower() != artist.lower():
        result = await _search(
            artistname=artist_no_article,
            groupname=album,
            year_param=year,
            release_type_param=release_type,
            media_param=media,
        )
        if result:
            return result

    if max_tier == 1:
        return None
    
    artist_t2 = unescape(artist).lower()
    artist_t2 = re.sub(r"\s*\(\d+\)\s*$", "", artist_t2).strip()
    album_t2 = unescape(album).lower() if album else None
    result = await _search(
        artistname=artist_t2,
        groupname=album_t2,
        year_param=year,
    )
    if result:
        return result
    if max_tier == 2:
        return None

    artist_t3 = _remove_stopwords(_normalize_text(unescape(artist)))
    album_t3 = None
    if album:
        album_t3 = _remove_stopwords(_normalize_text(unescape(album)))
        album_t3 = _remove_volume_indicators(album_t3)

    result = await _search(
        artistname=artist_t3,
        groupname=album_t3,
        year_param=None,
    )
    if result:
        return result
    if max_tier == 3:
        return None

    if album and ":" in album:
        album_t4_left, album_t4_right = (part.strip() for part in unescape(album).split(":", 1))
        album_t4_left = _remove_stopwords(_normalize_text(album_t4_left))
        album_t4_left = _remove_volume_indicators(album_t4_left)
        result = await _search(
            artistname=artist_t3,
            groupname=album_t4_left,
            year_param=None,
        )
        if result:
            return result
        album_t4_right = _remove_stopwords(_normalize_text(album_t4_right))
        album_t4_right = _remove_volume_indicators(album_t4_right)
        result = await _search(
            artistname=artist_t3,
            groupname=album_t4_right,
            year_param=None,
        )
        if result:
            return result

    return None
