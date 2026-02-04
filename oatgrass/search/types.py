"""Shared data structures for the search helpers."""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class GazelleSearchResult:
    """Minimal Gazelle result used by the search coordinator."""

    group_id: int
    title: str
    site_name: str
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class TorrentInfo:
    """Individual torrent within an edition."""
    torrent_id: int
    edition_id: Optional[int]
    media: str
    format: str
    encoding: str
    size: int
    remaster_year: Optional[int] = None
    remaster_title: Optional[str] = None
    remaster_label: Optional[str] = None
    remaster_catalog: Optional[str] = None


@dataclass
class EditionInfo:
    """Edition metadata and its torrents."""
    edition_id: Optional[int]
    year: Optional[int]
    title: Optional[str]
    label: Optional[str]
    catalog: Optional[str]
    torrents: List[TorrentInfo] = field(default_factory=list)


@dataclass
class GroupInfo:
    """Group-level metadata."""
    group_id: int
    name: str
    artist: str
    year: Optional[int]
    release_type: Optional[str]
    editions: List[EditionInfo] = field(default_factory=list)
