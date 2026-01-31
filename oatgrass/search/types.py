"""Shared data structures for the search helpers."""

from dataclasses import dataclass, field
from typing import Any, Dict


@dataclass
class GazelleSearchResult:
    """Minimal Gazelle result used by the search coordinator."""

    group_id: int
    title: str
    site_name: str
    metadata: Dict[str, Any] = field(default_factory=dict)
