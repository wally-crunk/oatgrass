"""Aggregate and prioritize upload candidates (Stage 6)."""

from dataclasses import dataclass
from typing import List

from oatgrass.search.edition_comparison import EditionComparison, EncodingComparison
from oatgrass.search.types import TorrentInfo
from oatgrass import logger


@dataclass
class UploadCandidate:
    """Single upload candidate with context."""
    source_torrent: TorrentInfo
    edition_year: int
    edition_title: str
    media: str
    encoding: str
    size: int
    priority: int  # Higher = more important


def find_upload_candidates(comparisons: List[EditionComparison]) -> List[UploadCandidate]:
    """Extract and prioritize upload candidates from edition comparisons."""
    candidates = []
    
    for comp in comparisons:
        edition = comp.match.source_edition
        
        for media_comp in comp.media_comparisons:
            # Check if this media exists on target at all
            has_target_media = any(
                enc.target_torrent is not None 
                for enc in media_comp.encodings
            )
            
            for enc_comp in media_comp.encodings:
                if enc_comp.is_upload_candidate and enc_comp.source_torrent:
                    # Priority 20 if new media, Priority 10 if new encoding within existing media
                    priority = 20 if not has_target_media else 10
                    
                    candidates.append(UploadCandidate(
                        source_torrent=enc_comp.source_torrent,
                        edition_year=edition.year or 0,
                        edition_title=edition.title or "(no title)",
                        media=media_comp.media,
                        encoding=enc_comp.encoding,
                        size=enc_comp.source_torrent.size,
                        priority=priority
                    ))
    
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
