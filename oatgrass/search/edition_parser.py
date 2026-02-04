"""Parse edition data from Gazelle browse responses."""

from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple

from oatgrass.search.types import EditionInfo, GroupInfo, TorrentInfo


def parse_group_from_browse(result: Dict[str, Any], tracker_name: str) -> GroupInfo:
    """Extract group and edition data from browse result."""
    group_id = result.get("groupId") or result.get("group_id") or 0
    group_name = result.get("groupName") or result.get("groupname") or ""
    group_year = result.get("groupYear") or result.get("groupyear")
    release_type = result.get("releaseType") or result.get("releasetype") or ""
    
    # Extract artist
    artist = ""
    if result.get("artist"):
        artist = result["artist"]
    elif result.get("artists"):
        artists = result["artists"]
        if isinstance(artists, list) and artists:
            artist = artists[0].get("name", "")
    
    # Group torrents by edition
    torrents_data = result.get("torrents", [])
    editions_map: Dict[int, List[TorrentInfo]] = defaultdict(list)
    
    for torrent in torrents_data:
        torrent_info = _parse_torrent(torrent)
        edition_key = torrent_info.edition_id if torrent_info.edition_id is not None else -1
        editions_map[edition_key].append(torrent_info)
    
    # Build edition objects
    editions: List[EditionInfo] = []
    for edition_id, torrents in sorted(editions_map.items()):
        # Use first torrent's remaster fields for edition metadata
        first = torrents[0]
        edition = EditionInfo(
            edition_id=first.edition_id,
            year=first.remaster_year,
            title=first.remaster_title,
            label=first.remaster_label,
            catalog=first.remaster_catalog,
            torrents=torrents,
        )
        editions.append(edition)
    
    return GroupInfo(
        group_id=int(group_id),
        name=group_name,
        artist=artist,
        year=int(group_year) if group_year else None,
        release_type=release_type,
        editions=editions,
    )


def parse_group_hybrid(
    group_data: Dict[str, Any],
    torrents_data: List[Dict[str, Any]],
    browse_result: Optional[Dict[str, Any]],
    tracker_name: str
) -> GroupInfo:
    """Parse group using torrentgroup data + browse editionId mapping.
    
    Args:
        group_data: Group metadata from torrentgroup response
        torrents_data: Torrents list from torrentgroup response
        browse_result: Optional browse result to get editionId mapping
        tracker_name: Tracker name for logging
    """
    group_id = group_data.get("id") or 0
    group_name = group_data.get("name") or ""
    group_year = group_data.get("year")
    release_type = group_data.get("releaseType") or ""
    
    # Extract artist
    artist = ""
    music_info = group_data.get("musicInfo", {})
    artists = music_info.get("artists", [])
    if artists and isinstance(artists, list):
        artist = artists[0].get("name", "")
    
    # Build editionId mapping from browse if available
    edition_id_map: Dict[Tuple, Optional[int]] = {}
    if browse_result:
        browse_torrents = browse_result.get("torrents", [])
        for bt in browse_torrents:
            # Map by remaster metadata tuple
            key = _make_edition_key(bt)
            edition_id_map[key] = bt.get("editionId")
    
    # Parse all torrents and assign editionId
    torrents: List[TorrentInfo] = []
    for torrent in torrents_data:
        torrent_info = _parse_torrent_from_group(torrent)
        
        # Try to assign editionId from browse mapping using remaster metadata
        if edition_id_map:
            key = _make_edition_key(torrent)
            if key in edition_id_map:
                torrent_info.edition_id = edition_id_map[key]
            else:
                # If exact match not found, all torrents with same remaster metadata
                # belong to same edition - assign first matching editionId
                for mapped_key, mapped_id in edition_id_map.items():
                    if _editions_match(key, mapped_key):
                        torrent_info.edition_id = mapped_id
                        break
        
        torrents.append(torrent_info)
    
    # Group torrents by edition
    # CRITICAL: When editionId is available, group ONLY by editionId
    # Trackers separate different media types into different editions even with identical metadata
    editions_map: Dict[Any, List[TorrentInfo]] = defaultdict(list)
    for torrent in torrents:
        if torrent.edition_id is not None:
            # Use editionId as the key - this respects tracker's CD/SACD splits
            key = torrent.edition_id
        else:
            # Fallback: group by remaster metadata tuple
            key = (
                torrent.remaster_year,
                torrent.remaster_title or "",
                torrent.remaster_label or "",
                torrent.remaster_catalog or "",
            )
        editions_map[key].append(torrent)
    
    # Build edition objects
    editions: List[EditionInfo] = []
    # Sort with None-safe key function
    def sort_key(item):
        key, _ = item
        if isinstance(key, int):
            return (0, key)  # editionId: sort first by type, then by value
        else:
            # Tuple key: replace None with empty values for sorting
            return (1, tuple(x if x is not None else '' if isinstance(x, str) else 0 for x in key))
    
    for key, torrents_list in sorted(editions_map.items(), key=sort_key):
        first = torrents_list[0]
        edition = EditionInfo(
            edition_id=first.edition_id,
            year=first.remaster_year,
            title=first.remaster_title,
            label=first.remaster_label,
            catalog=first.remaster_catalog,
            torrents=torrents_list,
        )
        editions.append(edition)
    
    return GroupInfo(
        group_id=int(group_id),
        name=group_name,
        artist=artist,
        year=int(group_year) if group_year else None,
        release_type=release_type,
        editions=editions,
    )


def _make_edition_key(torrent: Dict[str, Any]) -> Tuple:
    """Create edition key from torrent remaster fields."""
    return (
        torrent.get("remasterYear") or torrent.get("remasteryear"),
        (torrent.get("remasterTitle") or torrent.get("remastertitle") or "").strip(),
        (torrent.get("remasterRecordLabel") or torrent.get("remasterrecordlabel") or "").strip(),
        (torrent.get("remasterCatalogueNumber") or torrent.get("remastercataloguenumber") or "").strip(),
    )


def _editions_match(key1: Tuple, key2: Tuple) -> bool:
    """Check if two edition keys represent the same edition."""
    # Match if year and at least one other field matches
    if key1[0] != key2[0]:  # Year must match
        return False
    # If year matches and title/label/catalog all match, it's the same edition
    return key1[1:] == key2[1:]


def _parse_torrent(torrent: Dict[str, Any]) -> TorrentInfo:
    """Extract torrent info from browse torrent entry."""
    year = torrent.get("remasterYear")
    return TorrentInfo(
        torrent_id=int(torrent.get("torrentId") or torrent.get("id") or 0),
        edition_id=torrent.get("editionId"),
        media=torrent.get("media", ""),
        format=torrent.get("format", ""),
        encoding=torrent.get("encoding", ""),
        size=int(torrent.get("size", 0)),
        remaster_year=int(year) if year and year != 0 else None,
        remaster_title=torrent.get("remasterTitle") or torrent.get("remastertitle") or "",
        remaster_label=torrent.get("remasterRecordLabel") or torrent.get("remasterrecordlabel") or "",
        remaster_catalog=torrent.get("remasterCatalogueNumber") or torrent.get("remastercataloguenumber") or "",
    )


def _parse_torrent_from_group(torrent: Dict[str, Any]) -> TorrentInfo:
    """Extract torrent info from torrentgroup torrent entry.
    
    RED torrentgroup includes editionId directly in torrent data.
    OPS torrentgroup does NOT include editionId (must use browse instead).
    """
    year = torrent.get("remasterYear")
    return TorrentInfo(
        torrent_id=int(torrent.get("id") or 0),
        edition_id=torrent.get("editionId"),  # RED has this, OPS doesn't
        media=torrent.get("media", ""),
        format=torrent.get("format", ""),
        encoding=torrent.get("encoding", ""),
        size=int(torrent.get("size", 0)),
        remaster_year=int(year) if year and year != 0 else None,
        remaster_title=torrent.get("remasterTitle") or "",
        remaster_label=torrent.get("remasterRecordLabel") or "",
        remaster_catalog=torrent.get("remasterCatalogueNumber") or "",
    )
