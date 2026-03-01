"""Shared encoding ranking rules for display and candidate selection."""

import re

_BITRATE_RE = re.compile(r"\b(\d{2,4})\b")
_VBR_RE = re.compile(r"\bv(\d)\b")


def encoding_candidate_rank(encoding: str) -> int:
    """Rank encoding quality for candidate selection (higher is better)."""
    enc = encoding.lower()
    if "24bit" in enc or "24-bit" in enc:
        return 3
    if "lossless" in enc or "flac" in enc:
        return 2
    return 1


def encoding_display_key(encoding: str) -> tuple[int, int, int, str]:
    """Sort key for display output: highest quality first, deterministic fallback."""
    enc = encoding.lower()
    rank = encoding_candidate_rank(encoding)
    if rank == 3:
        return (0, 0, 0, enc)
    if rank == 2:
        return (1, 0, 0, enc)
    if bitrate := _BITRATE_RE.search(enc):
        return (2, 0, -int(bitrate.group(1)), enc)
    if vbr := _VBR_RE.search(enc):
        return (2, 1, int(vbr.group(1)), enc)
    if "apx" in enc:
        return (2, 1, 1, enc)
    if "aps" in enc:
        return (2, 1, 2, enc)
    return (2, 2, 0, enc)
