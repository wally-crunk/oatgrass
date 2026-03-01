from __future__ import annotations

import time
from datetime import datetime, timedelta


def _whole_seconds(seconds: float) -> int:
    return max(0, int(seconds))


def format_elapsed_clock(seconds: float) -> str:
    total = _whole_seconds(seconds)
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def format_remaining(seconds: float) -> str:
    total = _whole_seconds(seconds)
    if total < 600:
        minutes, secs = divmod(total, 60)
        return f"{minutes}:{secs:02d}"
    if total < 3600:
        return f"{total // 60}m"
    hours, rem = divmod(total, 3600)
    minutes = rem // 60
    if total < 10800:
        return f"{hours}h {minutes}m"
    return f"{hours}h"


def build_task_timing_phrase(
    total: int,
    completed: int,
    started_at: float,
    now_monotonic: float | None = None,
    now_wall: datetime | None = None,
) -> str:
    now_mono = time.monotonic() if now_monotonic is None else now_monotonic
    elapsed = max(0.0, now_mono - started_at)
    elapsed_text = format_elapsed_clock(elapsed)
    remaining_count = max(0, total - completed)

    rate = (completed / elapsed) if elapsed > 0 and completed > 0 else 0.0
    if remaining_count > 0 and rate <= 0:
        return f"{elapsed_text} elapsed, est unknown remaining (ETA --:--)"

    remaining_seconds = (remaining_count / rate) if rate > 0 else 0.0
    remaining_text = format_remaining(remaining_seconds)
    current_wall = datetime.now().astimezone() if now_wall is None else now_wall
    eta_text = (current_wall + timedelta(seconds=remaining_seconds)).strftime("%H:%M")
    return f"{elapsed_text} elapsed, est {remaining_text} remaining (ETA {eta_text})"
