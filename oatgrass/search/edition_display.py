"""Format edition data for display (Stage 3)."""

from typing import List, Optional, TextIO

from oatgrass.search.types import EditionInfo, GroupInfo
from oatgrass.search.edition_matcher import EditionMatch
from oatgrass import logger


def display_editions(
    source: GroupInfo,
    target: Optional[GroupInfo],
    source_name: str,
    target_name: str,
    output: Optional[TextIO] = None,
) -> None:
    """Display source and target editions side-by-side (Stage 3)."""
    logger.log(f"Source ({source_name}):")
    for idx, edition in enumerate(source.editions, 1):
        logger.log(f"    Edition {idx}:")
        logger.log(f"        Edition tuple: {_format_edition_tuple(edition)}")
    
    if target:
        logger.log(f"Target ({target_name}):")
        for idx, edition in enumerate(target.editions, 1):
            logger.log(f"    Edition {idx}:")
            logger.log(f"        Edition tuple: {_format_edition_tuple(edition)}")
    else:
        logger.log(f"Target ({target_name}): No match found")


def display_edition_matches(
    matches: List[EditionMatch],
    min_confidence: int,
    output: Optional[TextIO] = None,
) -> None:
    """Display edition matching results (Stage 4)."""
    logger.log(f"Minimum confidence required: {min_confidence}%")
    
    matched_count = sum(1 for m in matches if m.target_edition is not None)
    
    for idx, match in enumerate(matches, 1):
        if match.target_edition:
            logger.log(f"Source Edition {idx}: matches target")
            logger.log(f"    Confidence {match.confidence}% ({_format_contributions(match.contributions)})")
        else:
            logger.log(f"Source Edition {idx}: no match in target")
    
    logger.log(f"Matched ({matched_count}/{len(matches)}) Editions")


def _format_edition_tuple(edition: EditionInfo) -> str:
    """Format edition as: ID X / YEAR / TITLE / LABEL / CATALOG."""
    parts = []
    
    # Edition ID
    if edition.edition_id is not None:
        parts.append(f"ID {edition.edition_id}")
    else:
        parts.append("ID (none)")
    
    # Year
    if edition.year:
        parts.append(str(edition.year))
    else:
        parts.append("(no year)")
    
    # Title
    if edition.title:
        parts.append(edition.title)
    else:
        parts.append("(no title)")
    
    # Label
    if edition.label:
        parts.append(edition.label)
    else:
        parts.append("(no label)")
    
    # Catalog
    if edition.catalog:
        parts.append(edition.catalog)
    else:
        parts.append("(no catalog)")
    
    return " / ".join(parts)


def _format_contributions(contributions: dict) -> str:
    """Format confidence contributions breakdown."""
    parts = []
    if "year" in contributions:
        parts.append(f"Year {contributions['year']}/50")
    if "title" in contributions:
        parts.append(f"Title {contributions['title']}/25")
    if "catalog" in contributions:
        parts.append(f"Catalog {contributions['catalog']}/15")
    if "label" in contributions:
        parts.append(f"Label {contributions['label']}/10")
    if contributions.get("size_bonus", 0) > 0:
        parts.append(f"Size +{contributions['size_bonus']}%")
    return "; ".join(parts)
