from __future__ import annotations

from rich.console import Console
from rich.text import Text

from oatgrass.config import TrackerConfig
from oatgrass import logger

console = Console()


def emit(message: str, indent: int = 0) -> None:
    """Emit message to screen and log file via logger."""
    padding = " " * max(indent, 0)
    plain = Text.from_markup(message).plain
    logger.log(f"{padding}{plain}")


def format_size(size: int | None) -> str:
    if size is None:
        return "unknown"
    return f"{size:,}"


def display_value(label: str, value: str) -> str:
    target_col = 40
    value_width = 15
    if len(label) >= target_col:
        return f"{label} {value.rjust(value_width)}"
    return f"{label.ljust(target_col)}{value.rjust(value_width)}"


def format_compact_result(
    idx: int,
    total: int,
    source_tracker: TrackerConfig,
    source_gid: int,
    opposite_tracker: TrackerConfig,
    target_gid: int | None,
    source_max: int | None,
    target_max: int | None,
    tier_used: int = 1,
    cross_upload_url: str | None = None,
) -> str:
    source_name = source_tracker.name.upper()
    target_name = opposite_tracker.name.upper()
    tier_indicator = f"{tier_used}🔍" if tier_used > 1 else "="

    if target_gid is None:
        return (
            f"[Task {idx} of {total}] {tier_indicator} {source_name}={source_gid}; "
            f"{target_name} not found; Explore {cross_upload_url}"
        )

    if source_max is None or target_max is None:
        return (
            f"[Task {idx} of {total}] {tier_indicator} {source_name}={source_gid}; "
            f"{target_name}={target_gid}; size unknown"
        )

    if source_max == target_max:
        return (
            f"[Task {idx} of {total}] {tier_indicator} {source_name}={source_gid}; "
            f"{target_name}={target_gid}; {format_size(source_max)} (equal)"
        )

    if source_max > target_max:
        return (
            f"[Task {idx} of {total}] {tier_indicator} {source_name}={source_gid}; "
            f"{target_name}={target_gid}; {format_size(source_max)} vs {format_size(target_max)} (smaller)"
        )

    return (
        f"[Task {idx} of {total}] {tier_indicator} {source_name}={source_gid}; "
        f"{target_name}={target_gid}; {format_size(source_max)} vs {format_size(target_max)} (larger)"
    )
