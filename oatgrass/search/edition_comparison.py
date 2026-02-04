"""Compare media/encoding between matched editions (Stage 5)."""

from dataclasses import dataclass
from typing import Dict, List, Optional, Set

from oatgrass.search.edition_matcher import EditionMatch
from oatgrass.search.types import TorrentInfo
from oatgrass import logger


def _sizes_match(size1: int, size2: int) -> bool:
    """Check if two torrent sizes match (exact or within 0.1% tolerance).
    
    Returns False if either size is None.
    """
    if size1 is None or size2 is None:
        return False
    if size1 == size2:
        return True
    diff = abs(size1 - size2)
    larger = max(size1, size2)
    return (diff / larger) < 0.001  # 0.1% tolerance


@dataclass
class EncodingComparison:
    """Comparison of a specific encoding within a media type."""
    encoding: str
    source_torrent: Optional[TorrentInfo]
    target_torrent: Optional[TorrentInfo]
    is_upload_candidate: bool
    status: str


@dataclass
class MediaComparison:
    """Comparison of a specific media type within an edition."""
    media: str
    encodings: List[EncodingComparison]
    has_lossless: bool


@dataclass
class EditionComparison:
    """Full comparison of matched edition pair."""
    match: EditionMatch
    media_comparisons: List[MediaComparison]
    
    def has_warning(self) -> bool:
        """Check if this comparison has any warnings."""
        return getattr(self.match, 'has_media_mismatch_warning', False)


def compare_editions(matches: List[EditionMatch]) -> List[EditionComparison]:
    """Compare media/encoding for matched editions."""
    # Build set of all target sizes across ALL editions for size filtering (with fuzzy matching)
    all_target_torrents: List[TorrentInfo] = []
    for match in matches:
        if match.target_edition:
            all_target_torrents.extend(match.target_edition.torrents)
    
    comparisons = []
    
    for match in matches:
        if not match.target_edition:
            # No target - all source torrents are candidates
            comparisons.append(_compare_unmatched_edition(match, all_target_torrents))
        else:
            # Compare matched editions
            comparisons.append(_compare_matched_edition(match, all_target_torrents))
    
    return comparisons


def _compare_unmatched_edition(match: EditionMatch, all_target_torrents: List[TorrentInfo]) -> EditionComparison:
    """Handle source edition with no target match."""
    media_map: Dict[str, List[TorrentInfo]] = {}
    
    for torrent in match.source_edition.torrents:
        media_map.setdefault(torrent.media, []).append(torrent)
    
    media_comparisons = []
    for media, torrents in sorted(media_map.items()):
        has_lossless = any(_is_lossless(t.encoding) for t in torrents)
        
        encodings = []
        for torrent in torrents:
            is_lossless = _is_lossless(torrent.encoding)
            
            # Size-based filter: suppress if size exists anywhere in target (fuzzy match)
            size_exists = any(
                t.media == torrent.media and 
                t.encoding == torrent.encoding and 
                _sizes_match(t.size, torrent.size)
                for t in all_target_torrents
            )
            
            if size_exists:
                is_candidate = False
                status = "Suppressed (exact size exists in different target edition)"
            else:
                # Upload candidate if lossless OR no lossless exists
                is_candidate = is_lossless or not has_lossless
                status = "Upload candidate!" if is_candidate else "Ignored (FLAC already found)"
            
            encodings.append(EncodingComparison(
                encoding=torrent.encoding,
                source_torrent=torrent,
                target_torrent=None,
                is_upload_candidate=is_candidate,
                status=status
            ))
        
        media_comparisons.append(MediaComparison(
            media=media,
            encodings=encodings,
            has_lossless=has_lossless
        ))
    
    return EditionComparison(match=match, media_comparisons=media_comparisons)


def _compare_matched_edition(match: EditionMatch, all_target_torrents: List[TorrentInfo]) -> EditionComparison:
    """Compare source and target editions."""
    # Build media -> encoding -> torrent maps
    source_map: Dict[str, Dict[str, TorrentInfo]] = {}
    target_map: Dict[str, Dict[str, TorrentInfo]] = {}
    
    for t in match.source_edition.torrents:
        source_map.setdefault(t.media, {})[t.encoding] = t
    
    for t in match.target_edition.torrents:
        target_map.setdefault(t.media, {})[t.encoding] = t
    
    # Check for media overlap - warn if high confidence but no overlap
    source_media = set(source_map.keys())
    target_media = set(target_map.keys())
    media_overlap = source_media & target_media
    
    if not media_overlap and match.confidence >= 90:
        # High confidence match but zero overlapping media - suspicious!
        match.has_media_mismatch_warning = True
    
    # Get all media types
    all_media = sorted(set(source_map.keys()) | set(target_map.keys()))
    
    media_comparisons = []
    for media in all_media:
        source_encodings = source_map.get(media, {})
        target_encodings = target_map.get(media, {})
        
        # Check if lossless exists on either side
        has_lossless = (
            any(_is_lossless(enc) for enc in source_encodings.keys()) or
            any(_is_lossless(enc) for enc in target_encodings.keys())
        )
        
        # Get all encodings for this media
        all_encodings = sorted(set(source_encodings.keys()) | set(target_encodings.keys()))
        
        encoding_comparisons = []
        for encoding in all_encodings:
            source_torrent = source_encodings.get(encoding)
            target_torrent = target_encodings.get(encoding)
            
            is_lossless = _is_lossless(encoding)
            
            # Determine status
            if source_torrent and target_torrent:
                # Both have it - use fuzzy size matching
                if _sizes_match(source_torrent.size, target_torrent.size):
                    if source_torrent.size == target_torrent.size:
                        status = "Exact size match"
                    else:
                        status = f"Fuzzy size match (source: {source_torrent.size:,}, target: {target_torrent.size:,})"
                else:
                    status = f"Size mismatch (source: {source_torrent.size:,}, target: {target_torrent.size:,})"
                is_candidate = False
            elif source_torrent and not target_torrent:
                # Source has it, target doesn't
                # Size-based filter: suppress if size exists anywhere in target (fuzzy match)
                size_exists = any(
                    t.media == source_torrent.media and 
                    t.encoding == source_torrent.encoding and 
                    _sizes_match(t.size, source_torrent.size)
                    for t in all_target_torrents
                )
                
                if size_exists:
                    status = "Suppressed (exact size exists in different target edition)"
                    is_candidate = False
                elif is_lossless or not has_lossless:
                    status = "Upload candidate!"
                    is_candidate = True
                else:
                    status = "Ignored (FLAC already found)"
                    is_candidate = False
            elif target_torrent and not source_torrent:
                # Target has it, source doesn't
                status = "Target only"
                is_candidate = False
            else:
                status = "Unknown"
                is_candidate = False
            
            encoding_comparisons.append(EncodingComparison(
                encoding=encoding,
                source_torrent=source_torrent,
                target_torrent=target_torrent,
                is_upload_candidate=is_candidate,
                status=status
            ))
        
        media_comparisons.append(MediaComparison(
            media=media,
            encodings=encoding_comparisons,
            has_lossless=has_lossless
        ))
    
    return EditionComparison(match=match, media_comparisons=media_comparisons)


def _is_lossless(encoding: str) -> bool:
    """Check if encoding is lossless."""
    enc_lower = encoding.lower()
    return any(x in enc_lower for x in ["flac", "lossless", "24bit"])


def display_edition_comparisons(comparisons: List[EditionComparison], source_name: str, target_name: str):
    """Display Stage 5 results."""
    logger.log(f"Source: {source_name}")
    logger.log(f"Target: {target_name}")
    logger.log("")
    
    for idx, comp in enumerate(comparisons, 1):
        match = comp.match
        source_ed = match.source_edition
        
        logger.log(f"Source Edition {idx}: {source_ed.year} / {source_ed.title or '(no title)'} / {source_ed.label or '(no label)'} / {source_ed.catalog or '(no catalog)'}")
        
        if match.target_edition:
            target_ed = match.target_edition
            logger.log(f"Target Edition: {target_ed.year} / {target_ed.title or '(no title)'} / {target_ed.label or '(no label)'} / {target_ed.catalog or '(no catalog)'}")
            logger.log(f"  Confidence: {match.confidence}%")
            
            # Show warning if media types don't overlap
            if getattr(match, 'has_media_mismatch_warning', False):
                source_media = sorted(set(t.media for t in source_ed.torrents))
                target_media = sorted(set(t.media for t in target_ed.torrents))
                logger.log(f"  ⚠️  WARNING: High confidence match but NO overlapping media types!")
                logger.log(f"      Source media: {', '.join(source_media)}")
                logger.log(f"      Target media: {', '.join(target_media)}")
                logger.log(f"      These may be intentionally separate editions. Verify before uploading.")
        else:
            logger.log("Target Edition: (none)")
        
        logger.log("")
        
        # Display media/encoding comparisons
        for media_comp in comp.media_comparisons:
            logger.log(f"  Media: {media_comp.media}")
            
            for enc_comp in media_comp.encodings:
                logger.log(f"    Encoding: {enc_comp.encoding}")
                
                if enc_comp.source_torrent:
                    logger.log(f"      Source: {source_name} Torrent {enc_comp.source_torrent.torrent_id} ({enc_comp.source_torrent.size:,})")
                
                if enc_comp.target_torrent:
                    logger.log(f"      Target: {target_name} Torrent {enc_comp.target_torrent.torrent_id} ({enc_comp.target_torrent.size:,})")
                
                logger.log(f"      Status: {enc_comp.status}")
                logger.log("")
        
        logger.log("")
