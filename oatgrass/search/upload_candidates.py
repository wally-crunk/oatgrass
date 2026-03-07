"""Aggregate and prioritize upload candidates (Stage 6)."""

from dataclasses import dataclass
from typing import List

from oatgrass.search.edition_comparison import EditionComparison, EncodingComparison
from oatgrass.search.encoding_order import encoding_candidate_rank
from oatgrass.search.types import TorrentInfo
from oatgrass import logger


@dataclass
class UploadCandidate:
    """Single upload candidate with context."""
    source_torrent: TorrentInfo
    edition_id: int | None
    edition_year: int
    edition_title: str
    media: str
    encoding: str
    size: int
    priority: int  # Higher = more important


def _candidate_rank_key(candidate: UploadCandidate) -> tuple[int, int, int]:
    """Sort key: best encoding first, then size, then stable torrent-id tie-break."""
    return (
        encoding_candidate_rank(candidate.encoding),
        candidate.size,
        -candidate.source_torrent.torrent_id,
    )


def _build_candidate(
    edition,
    media: str,
    enc_comp: EncodingComparison,
    priority: int,
) -> UploadCandidate:
    source_torrent = enc_comp.source_torrent
    assert source_torrent is not None
    return UploadCandidate(
        source_torrent=source_torrent,
        edition_id=edition.edition_id,
        edition_year=edition.year or 0,
        edition_title=edition.title or "(no title)",
        media=media,
        encoding=enc_comp.encoding,
        size=source_torrent.size,
        priority=priority,
    )


def find_upload_candidates(comparisons: List[EditionComparison]) -> List[UploadCandidate]:
    """Extract and prioritize upload candidates from edition comparisons."""
    candidates: List[UploadCandidate] = []

    # Group-level media presence on target (from matched edition comparisons).
    target_media_present = {
        media_comp.media.lower()
        for comp in comparisons
        for media_comp in comp.media_comparisons
        if any(enc.target_torrent is not None for enc in media_comp.encodings)
    }

    for comp in comparisons:
        edition = comp.match.source_edition

        if comp.match.target_edition is None:
            # Missing edition: keep normal 20/10 semantics, but promote one top-value
            # candidate within each unmatched edition to Priority 50.
            unmatched_candidates: List[UploadCandidate] = []
            for media_comp in comp.media_comparisons:
                media_exists_on_target = media_comp.media.lower() in target_media_present
                for enc_comp in media_comp.encodings:
                    if enc_comp.is_upload_candidate and enc_comp.source_torrent:
                        priority = 10 if media_exists_on_target else 20
                        unmatched_candidates.append(
                            _build_candidate(edition, media_comp.media, enc_comp, priority)
                        )

            if unmatched_candidates:
                best_candidate = max(unmatched_candidates, key=_candidate_rank_key)
                best_candidate.priority = 50
                candidates.extend(unmatched_candidates)
            continue

        for media_comp in comp.media_comparisons:
            # Matched edition: Priority 20 if new media, 10 if encoding gap in existing media.
            has_target_media = any(enc.target_torrent is not None for enc in media_comp.encodings)
            for enc_comp in media_comp.encodings:
                if enc_comp.is_upload_candidate and enc_comp.source_torrent:
                    priority = 20 if not has_target_media else 10
                    candidates.append(_build_candidate(edition, media_comp.media, enc_comp, priority))

    # Sort by priority (descending)
    candidates.sort(key=lambda c: c.priority, reverse=True)
    return candidates



def display_upload_candidates(candidates: List[UploadCandidate], source_name: str, target_name: str):
    """Display prioritized upload candidates."""
    if not candidates:
        logger.log(f"No cross-upload candidates found from {source_name} to {target_name}.")
        return
    
    logger.log(f"=== Upload Candidates: {source_name} → {target_name} ===\n")
    logger.log(f"Found {len(candidates)} upload candidate(s):\n")
    
    for idx, candidate in enumerate(candidates, 1):
        logger.log(f"{idx}. Edition: {candidate.edition_year} / {candidate.edition_title}")
        logger.log(f"   Media: {candidate.media} | Encoding: {candidate.encoding}")
        logger.log(f"   Torrent ID: {candidate.source_torrent.torrent_id} | Size: {candidate.size:,} bytes")
        logger.log(f"   Priority: {candidate.priority}")
        logger.log("")
