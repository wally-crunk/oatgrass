"""Match editions between source and target trackers (Stage 4)."""

import numpy as np
from dataclasses import dataclass
from difflib import SequenceMatcher
from scipy.optimize import linear_sum_assignment
from typing import Dict, List, Optional

from oatgrass.search.types import EditionInfo, GroupInfo


@dataclass
class EditionMatch:
    """Matched edition pair with confidence score."""
    source_edition: EditionInfo
    target_edition: Optional[EditionInfo]
    confidence: int
    contributions: Dict[str, int]


def match_editions(
    source_group: GroupInfo,
    target_group: Optional[GroupInfo],
    min_confidence: int = 50
) -> List[EditionMatch]:
    """Match source editions to target editions using optimal assignment.
    
    Uses Hungarian algorithm to find globally optimal edition matching,
    preventing greedy false positives where early matches block better matches.
    
    Args:
        source_group: Source tracker group with editions
        target_group: Target tracker group with editions (or None if not found)
        min_confidence: Minimum confidence threshold (0-100)
    
    Returns:
        List of EditionMatch objects
    """
    if not target_group:
        # No target group - all source editions are unmatched
        return [
            EditionMatch(
                source_edition=ed,
                target_edition=None,
                confidence=0,
                contributions={}
            )
            for ed in source_group.editions
        ]
    
    sources = source_group.editions
    targets = target_group.editions
    
    # Build confidence matrix for ALL source-target pairs
    matrix = np.zeros((len(sources), len(targets)))
    contributions_map = {}
    
    for i, source_ed in enumerate(sources):
        for j, target_ed in enumerate(targets):
            confidence, contributions = _score_edition_match(source_ed, target_ed)
            matrix[i, j] = confidence
            contributions_map[(i, j)] = contributions
    
    # Find optimal assignment using Hungarian algorithm (maximize total confidence)
    row_ind, col_ind = linear_sum_assignment(-matrix)  # Negative for maximization
    
    # Build matches, applying threshold
    matches = []
    for i, j in zip(row_ind, col_ind):
        confidence = int(matrix[i, j])
        if confidence >= min_confidence:
            matches.append(EditionMatch(
                source_edition=sources[i],
                target_edition=targets[j],
                confidence=confidence,
                contributions=contributions_map[(i, j)]
            ))
        else:
            # Below threshold - no match
            matches.append(EditionMatch(
                source_edition=sources[i],
                target_edition=None,
                confidence=0,
                contributions={}
            ))
    
    # Cross-match correction: check if swapping matches improves media alignment
    matches = _correct_cross_matches(matches)
    
    return matches


def _score_edition_match(source: EditionInfo, target: EditionInfo) -> tuple[int, Dict[str, int]]:
    """Score edition match using TABLE 4.1 rubric.
    
    Returns:
        (total_confidence, contributions_dict)
    """
    # MEDIA VETO: If no overlapping media types, veto the match
    source_media = {t.media for t in source.torrents}
    target_media = {t.media for t in target.torrents}
    if not (source_media & target_media):
        # No overlapping media - these are different editions
        return 0, {}
    
    contributions: Dict[str, int] = {}
    
    # Year (50% weight)
    year_score = 0
    if source.year and target.year:
        if source.year == target.year:
            year_score = 50
        elif abs(source.year - target.year) == 1:
            year_score = 48
    contributions["year"] = year_score
    
    # Title (25% weight)
    title_score = 0
    if source.title and target.title:
        ratio = _string_similarity(source.title, target.title)
        title_score = round(ratio * 25)
    elif not source.title and not target.title:
        # Both empty - minimal credit (Strategy A: Empty Title Penalty)
        title_score = 5
    contributions["title"] = title_score
    
    # Catalog Number (15% weight)
    catalog_score = 0
    if source.catalog and target.catalog:
        ratio = _string_similarity(source.catalog, target.catalog)
        catalog_score = round(ratio * 15)
    elif not source.catalog and not target.catalog:
        # Both empty - perfect match
        catalog_score = 15
    contributions["catalog"] = catalog_score
    
    # Label (10% weight)
    label_score = 0
    if source.label and target.label:
        ratio = _string_similarity(source.label, target.label)
        label_score = round(ratio * 10)
    elif not source.label and not target.label:
        # Both empty - perfect match
        label_score = 10
    contributions["label"] = label_score
    
    # Torrent Size Bonus (+10% per matching torrent)
    size_bonus = _calculate_size_bonus(source, target)
    contributions["size_bonus"] = size_bonus
    
    # Strategy D: Metadata Match Requirement
    # Require at least ONE of (catalog, label) to score >50% similarity
    catalog_match = catalog_score > 7  # >50% of 15
    label_match = label_score > 5      # >50% of 10
    
    if not (catalog_match or label_match):
        # Neither catalog nor label match sufficiently - veto the match
        return 0, {}
    
    total = sum(contributions.values())
    return min(total, 150), contributions  # Cap at 150% (100% base + 50% bonus max)


def _string_similarity(s1: str, s2: str) -> float:
    """Calculate string similarity ratio (0.0 to 1.0)."""
    if not s1 or not s2:
        return 0.0
    
    # Normalize: lowercase and strip
    s1_norm = s1.lower().strip()
    s2_norm = s2.lower().strip()
    
    if s1_norm == s2_norm:
        return 1.0
    
    return SequenceMatcher(None, s1_norm, s2_norm).ratio()


def _sizes_match(size1: int, size2: int) -> bool:
    """Check if two torrent sizes match (exact or within 0.1% tolerance)."""
    if size1 == size2:
        return True
    diff = abs(size1 - size2)
    larger = max(size1, size2)
    return (diff / larger) < 0.001  # 0.1% tolerance


def _size_confidence_bonus(size1: int, size2: int) -> int:
    """Calculate confidence bonus for size matching.
    
    Returns:
        10 for exact match, 8 for fuzzy match (<0.1%), 0 for no match
    """
    if size1 == size2:
        return 10  # Exact match
    if _sizes_match(size1, size2):
        return 8  # Fuzzy match (within 0.1%)
    return 0  # No match


def _calculate_size_bonus(source: EditionInfo, target: EditionInfo) -> int:
    """Calculate torrent size matching bonus (10% exact, 8% fuzzy per match, max 50%)."""
    # Build map of target torrents by (media, encoding) -> size
    target_map: Dict[tuple, int] = {}
    for t in target.torrents:
        key = (t.media.lower(), t.encoding.lower())
        target_map[key] = t.size
    
    # Count matching sizes with bonus scoring
    total_bonus = 0
    for source_torrent in source.torrents:
        key = (source_torrent.media.lower(), source_torrent.encoding.lower())
        if key in target_map:
            bonus = _size_confidence_bonus(source_torrent.size, target_map[key])
            total_bonus += bonus
    
    return min(total_bonus, 50)  # Cap at 50%


def _correct_cross_matches(matches: List[EditionMatch]) -> List[EditionMatch]:
    """Check if swapping matched editions improves media alignment.
    
    Example: If Edition A (CD) matched to Edition X (SACD) and
             Edition B (SACD) matched to Edition Y (CD),
             swap to A->Y and B->X for better media alignment.
    """
    # Only consider matched editions with similar metadata
    matched = [(i, m) for i, m in enumerate(matches) if m.target_edition and m.confidence >= 80]
    
    if len(matched) < 2:
        return matches
    
    # Check all pairs for potential swaps
    for i in range(len(matched)):
        for j in range(i + 1, len(matched)):
            idx_i, match_i = matched[i]
            idx_j, match_j = matched[j]
            
            # Get media types
            source_i_media = set(t.media for t in match_i.source_edition.torrents)
            source_j_media = set(t.media for t in match_j.source_edition.torrents)
            target_i_media = set(t.media for t in match_i.target_edition.torrents)
            target_j_media = set(t.media for t in match_j.target_edition.torrents)
            
            # Check if current matching has no media overlap
            current_overlap_i = len(source_i_media & target_i_media)
            current_overlap_j = len(source_j_media & target_j_media)
            current_total = current_overlap_i + current_overlap_j
            
            # Check if swapping would improve media overlap
            swapped_overlap_i = len(source_i_media & target_j_media)
            swapped_overlap_j = len(source_j_media & target_i_media)
            swapped_total = swapped_overlap_i + swapped_overlap_j
            
            # Swap if it improves media alignment
            if swapped_total > current_total:
                # Swap the target editions
                matches[idx_i] = EditionMatch(
                    source_edition=match_i.source_edition,
                    target_edition=match_j.target_edition,
                    confidence=match_i.confidence,  # Keep original confidence
                    contributions=match_i.contributions
                )
                matches[idx_j] = EditionMatch(
                    source_edition=match_j.source_edition,
                    target_edition=match_i.target_edition,
                    confidence=match_j.confidence,
                    contributions=match_j.contributions
                )
                # Mark as corrected
                matches[idx_i].was_cross_match_corrected = True
                matches[idx_j].was_cross_match_corrected = True
    
    return matches
