"""5-tier search coordinator for edition-aware search."""

import asyncio
import re
from html import unescape
from typing import Optional
from difflib import SequenceMatcher

from oatgrass.search.gazelle_client import GazelleServiceAdapter


def _normalize_text(text: str) -> str:
    text = text.lower()
    text = re.sub(r'[^a-z0-9\s]', ' ', text)
    text = re.sub(r'\s+', ' ', text)
    return text.strip()


def _remove_stopwords(text: str) -> str:
    words = text.split()
    stopwords = {'the', 'a', 'an'}
    return ' '.join(w for w in words if w not in stopwords)


def _remove_volume_indicators(text: str) -> str:
    text = re.sub(r'\bvol(?:ume)?\s*[0-9ivxlcdm]+\b', '', text, flags=re.IGNORECASE)
    text = re.sub(r'\s+', ' ', text)
    return text.strip()


def _score_result(result: dict, query_artist: str, query_album: Optional[str], query_year: Optional[int]) -> float:
    """Score a search result by how well it matches the query.
    
    Returns a score from 0-100 where higher is better.
    """
    score = 0.0
    
    result_artist = result.get('artist', '')
    result_album = result.get('groupName', '')
    result_year = result.get('groupYear', 0)
    
    # Artist match (40 points max)
    if result_artist and query_artist:
        artist_ratio = SequenceMatcher(None, result_artist.lower(), query_artist.lower()).ratio()
        score += artist_ratio * 40
    
    # Album match (40 points max)
    if result_album and query_album:
        album_ratio = SequenceMatcher(None, result_album.lower(), query_album.lower()).ratio()
        score += album_ratio * 40
        
        # Penalize results with extra words (Deluxe, Demos, Remaster, etc.)
        result_words = set(result_album.lower().split())
        query_words = set(query_album.lower().split())
        extra_words = result_words - query_words
        
        # Common extra words that indicate different versions
        version_indicators = {'deluxe', 'demo', 'demos', 'remaster', 'remastered', 'expanded', 
                            'edition', 'anniversary', 'special', 'bonus', 'live', 'outtakes'}
        if extra_words & version_indicators:
            score -= 15  # Significant penalty for version indicators
        
        # Heavy penalty for compilations/collections
        compilation_indicators = {'collection', 'compilation', 'greatest', 'hits', 'best', 'anthology'}
        if extra_words & compilation_indicators:
            score -= 30  # Heavy penalty for compilations
        
        # Heavy penalty for compilations/collections when searching for specific album
        compilation_indicators = {'collection', 'compilation', 'greatest', 'hits', 'best', 'anthology'}
        if extra_words & compilation_indicators:
            score -= 30  # Heavy penalty for compilations
    
    # Year match (20 points max)
    if query_year and result_year:
        if result_year == query_year:
            score += 20
        elif abs(result_year - query_year) == 1:
            score += 15
        elif abs(result_year - query_year) <= 2:
            score += 10
    
    return score


def _select_best_result(hits: list, query_artist: str, query_album: Optional[str], query_year: Optional[int]) -> dict:
    """Select the best matching result from multiple hits."""
    if not hits:
        return None
    if len(hits) == 1:
        return hits[0]
    
    # Score all results
    scored = [(hit, _score_result(hit, query_artist, query_album, query_year)) for hit in hits]
    
    # Return highest scoring result
    best = max(scored, key=lambda x: x[1])
    return best[0]


async def search_with_tiers(
    client: GazelleServiceAdapter,
    artist: str,
    album: Optional[str],
    year: Optional[int],
    release_type: Optional[int] = None,
    media: Optional[str] = None,
) -> Optional[dict]:
    """Search using 5-tier strategy, return best match or None."""
    
    # Tier 1: Exact match
    results = await client.search(artistname=artist, groupname=album, year=year, release_type=release_type, media=media)
    hits = results.get('response', {}).get('results', [])
    if hits:
        return _select_best_result(hits, artist, album, year)
    
    # Tier 2: Light normalization (lowercase + HTML unescape, drop release_type to catch Album/EP mismatches)
    artist_t2 = unescape(artist).lower()
    album_t2 = unescape(album).lower() if album else None
    results = await client.search(artistname=artist_t2, groupname=album_t2, year=year)
    hits = results.get('response', {}).get('results', [])
    if hits:
        return _select_best_result(hits, artist, album, year)
    
    # Tier 3: Aggressive normalization (strip punctuation + remove stopwords + remove volumes, drop year and release_type)
    artist_t3 = _remove_stopwords(_normalize_text(unescape(artist)))
    album_t3 = None
    if album:
        album_t3 = _remove_stopwords(_normalize_text(unescape(album)))
        album_t3 = _remove_volume_indicators(album_t3)
    
    results = await client.search(artistname=artist_t3, groupname=album_t3)
    hits = results.get('response', {}).get('results', [])
    if hits:
        return _select_best_result(hits, artist, album, year)
    
    # Tier 4: Colon cutoff (truncate album at first colon, then apply Tier 3 normalization)
    if album and ':' in album:
        album_t4 = unescape(album).split(':', 1)[0].strip()
        album_t4 = _remove_stopwords(_normalize_text(album_t4))
        album_t4 = _remove_volume_indicators(album_t4)
        
        results = await client.search(artistname=artist_t3, groupname=album_t4)
        hits = results.get('response', {}).get('results', [])
        if hits:
            return _select_best_result(hits, artist, album, year)
    
    # Tier 5: Discogs ANV fallback - not implemented yet (requires Discogs service)
    # TODO: Add Discogs integration
    
    return None
