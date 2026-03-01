"""Format edition data for display (Stage 3)."""

from typing import Callable, List, Optional, TextIO

from oatgrass.search.types import EditionInfo, GroupInfo
from oatgrass.search.edition_matcher import EditionMatch
from oatgrass import logger


EmitFunc = Callable[..., None]


def _resolve_emit(emit_func: Optional[EmitFunc]) -> EmitFunc:
    return emit_func or logger.log


def display_editions(
    source: GroupInfo,
    target: Optional[GroupInfo],
    source_name: str,
    target_name: str,
    output: Optional[TextIO] = None,
    emit_func: Optional[EmitFunc] = None,
    base_indent: int = 0,
) -> None:
    """Display source and target editions side-by-side (Stage 3)."""
    emit = _resolve_emit(emit_func)
    emit(f"Source ({source_name}):", indent=base_indent)
    for idx, edition in enumerate(source.editions, 1):
        emit(f"Edition {idx}:", indent=base_indent + 4)
        emit(f"Edition tuple: {_format_edition_tuple(edition)}", indent=base_indent + 8)

    if target:
        emit(f"Target ({target_name}):", indent=base_indent)
        for idx, edition in enumerate(target.editions, 1):
            emit(f"Edition {idx}:", indent=base_indent + 4)
            emit(f"Edition tuple: {_format_edition_tuple(edition)}", indent=base_indent + 8)
    else:
        emit(f"Target ({target_name}): No match found", indent=base_indent)


def display_edition_matches(
    matches: List[EditionMatch],
    min_confidence: int,
    output: Optional[TextIO] = None,
    emit_func: Optional[EmitFunc] = None,
    base_indent: int = 0,
) -> None:
    """Display edition matching results (Stage 4)."""
    emit = _resolve_emit(emit_func)
    emit(f"Minimum confidence required: {min_confidence}%", indent=base_indent)

    matched_count = sum(1 for m in matches if m.target_edition is not None)

    for idx, match in enumerate(matches, 1):
        if match.target_edition:
            emit(f"Source Edition {idx}: matches target", indent=base_indent)
            emit(
                f"Confidence {match.confidence}% ({_format_contributions(match.contributions)})",
                indent=base_indent + 4,
            )
        else:
            emit(f"Source Edition {idx}: no match in target", indent=base_indent)

    emit(f"Matched ({matched_count}/{len(matches)}) Editions", indent=base_indent)


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
