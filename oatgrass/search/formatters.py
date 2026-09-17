from __future__ import annotations

from rich.text import Text

from oatgrass.config import TrackerConfig
from oatgrass import logger


def _plain(message: str) -> str:
    """Strip any Rich markup (e.g. a leftover [bold] emphasis tag, or a
    literal bracketed label like "[Target, OPS]" that Rich leaves untouched
    since it isn't a real style name) down to plain text. Severity is no
    longer inferred from tags here -- callers state it explicitly by which
    emit_* function they call, so a stray markup tag can't silently pick the
    wrong color the way a [red]/[yellow]/[green] tag used to."""
    return Text.from_markup(message).plain


def emit(message: str, indent: int = 0) -> None:
    """Emit a plain/info-level message to screen and log file via logger."""
    logger.get_logger().info(_plain(message), indent=indent)


def emit_warning(message: str, indent: int = 0) -> None:
    logger.get_logger().warning(_plain(message), indent=indent)


def emit_error(message: str, indent: int = 0) -> None:
    logger.get_logger().error(_plain(message), indent=indent)


def emit_success(message: str, indent: int = 0) -> None:
    logger.get_logger().success(_plain(message), indent=indent)


def emit_result_candidate(message: str, indent: int = 0) -> None:
    logger.get_logger().result_candidate(_plain(message), indent=indent)


def emit_result_possible_candidate(message: str, indent: int = 0) -> None:
    logger.get_logger().result_possible_candidate(_plain(message), indent=indent)


def emit_result_duplicate(message: str, indent: int = 0) -> None:
    logger.get_logger().result_duplicate(_plain(message), indent=indent)


def emit_progress(message: str, indent: int = 0) -> None:
    logger.get_logger().progress(_plain(message), indent=indent)


def format_task_context_line(
    source_tracker_name: str,
    source_gid: int | str,
    target_tracker_name: str,
    target_gid: int | None,
) -> str:
    target_label = target_gid if target_gid is not None else "not found"
    return (
        f"Source: {source_tracker_name.upper()} album #{source_gid}, "
        f"Target: {target_tracker_name.upper()} album #{target_label}"
    )


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
    timing_phrase: str,
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
    # Compact mode is a density trade (one line per entry), not an
    # information cut -- it must still carry the same elapsed/remaining/ETA
    # data as the full [Task N of M] —— <timing> header, just folded into
    # one line instead of its own.
    header = f"[Task {idx} of {total}] —— {timing_phrase} ——"

    if target_gid is None:
        return (
            f"{header} {tier_indicator} {source_name}={source_gid}; "
            f"{target_name} not found; Explore {cross_upload_url}"
        )

    if source_max is None or target_max is None:
        return (
            f"{header} {tier_indicator} {source_name}={source_gid}; "
            f"{target_name}={target_gid}; size unknown"
        )

    if source_max == target_max:
        return (
            f"{header} {tier_indicator} {source_name}={source_gid}; "
            f"{target_name}={target_gid}; {format_size(source_max)} (equal)"
        )

    if source_max > target_max:
        return (
            f"{header} {tier_indicator} {source_name}={source_gid}; "
            f"{target_name}={target_gid}; {format_size(source_max)} vs {format_size(target_max)} (smaller)"
        )

    return (
        f"{header} {tier_indicator} {source_name}={source_gid}; "
        f"{target_name}={target_gid}; {format_size(source_max)} vs {format_size(target_max)} (larger)"
    )
