"""Edition-aware search mode wrapper."""

from typing import TextIO, Optional

from oatgrass.config import TrackerConfig
from oatgrass.search.gazelle_client import GazelleServiceAdapter
from oatgrass.search.edition_parser import parse_group_from_browse, parse_group_hybrid
from oatgrass.search.edition_matcher import match_editions
from oatgrass.search.edition_comparison import compare_editions
from oatgrass.search.upload_candidates import find_upload_candidates
from oatgrass.search.tier_search_service import search_with_tiers


async def process_entry_edition_aware(
    entry: dict,
    source_tracker: TrackerConfig,
    opposite_tracker: TrackerConfig,
    source_client: GazelleServiceAdapter,
    target_client: GazelleServiceAdapter,
    emit_func,
    
    abbrev: bool,
    verbose: bool,
) -> tuple[Optional[int], list[tuple[str, int]]]:
    """Process one entry with edition-aware search.
    
    Returns:
        (target_group_id, list_of_(url, priority)_tuples)
    """
    from oatgrass.search.search_mode import _build_search_context, _group_id, _cross_upload_url
    
    search_context = _build_search_context(entry)
    source_gid = _group_id(entry)
    
    if not source_gid:
        return None, []
    
    # Fetch full source group
    group_response = await source_client.get_group(source_gid)
    source_group_data = group_response.get('response', {}).get('group', {})
    source_torrents = group_response.get('response', {}).get('torrents', [])
    
    # Get browse data for editionId
    browse = await source_client.search(
        artistname=search_context.artist,
        groupname=search_context.album,
        year=search_context.year
    )
    browse_results = browse.get('response', {}).get('results', [])
    browse_result = browse_results[0] if browse_results else {}
    
    # Parse source group
    if source_tracker.name.lower() == "red":
        source_group = parse_group_hybrid(source_group_data, source_torrents, browse_result, source_tracker.name.upper())
    else:
        # OPS: use browse result directly (has editionId AND all torrents)
        source_group = parse_group_from_browse(browse_result, source_tracker.name.upper())
    
    # Search target using 5-tier strategy
    target_result = await search_with_tiers(
        target_client,
        search_context.artist,
        search_context.album,
        search_context.year
    )
    
    if not target_result:
        # No match - all source torrents are candidates
        matches = match_editions(source_group, None)
        comparisons = compare_editions(matches)
        candidates = find_upload_candidates(comparisons)
        
        if candidates:
            urls_with_priority = [(f"{source_tracker.url.rstrip('/')}/torrents.php?torrentid={c.source_torrent.torrent_id}", c.priority) for c in candidates]
            if not abbrev:
                emit_func(f"[yellow]No matching group found. {len(candidates)} upload candidate(s).[/yellow]", indent=3)
            return None, urls_with_priority
        return None, []
    
    # Parse target group
    if opposite_tracker.name.lower() == "red":
        # RED as target: need to fetch full torrentgroup (browse is incomplete)
        target_gid = target_result.get('groupId')
        target_group_response = await target_client.get_group(target_gid)
        target_group_data = target_group_response.get('response', {}).get('group', {})
        target_torrents = target_group_response.get('response', {}).get('torrents', [])
        target_group = parse_group_hybrid(target_group_data, target_torrents, target_result, opposite_tracker.name.upper())
    else:
        # OPS as target: browse result has everything
        target_group = parse_group_from_browse(target_result, opposite_tracker.name.upper())
    
    # Match editions and find candidates
    matches = match_editions(source_group, target_group, min_confidence=25)
    comparisons = compare_editions(matches)
    candidates = find_upload_candidates(comparisons)
    
    # Verbose mode: show detailed edition analysis
    if verbose and not abbrev:
        from oatgrass.search.edition_display import display_edition_matches
        from oatgrass.search.edition_comparison import display_edition_comparisons
        
        emit_func("\n   Edition Matching:", indent=3)
        display_edition_matches(matches, min_confidence=25)
        
        emit_func("\n   Media/Encoding Comparison:", indent=3)
        display_edition_comparisons(comparisons, source_tracker.name.upper(), opposite_tracker.name.upper())
    elif not abbrev:
        # In normal mode, show warning if any comparison has media mismatch
        for comp in comparisons:
            if comp.has_warning():
                emit_func("[yellow]⚠️  Warning: Matched editions with different media types (CD vs SACD). Verify carefully.[/yellow]", indent=3)
    
    if candidates:
        urls_with_priority = [(f"{source_tracker.url.rstrip('/')}/torrents.php?torrentid={c.source_torrent.torrent_id}", c.priority) for c in candidates]
        if not abbrev:
            emit_func(f"[green]Found match. {len(candidates)} upload candidate(s) at edition/media/encoding level.[/green]", indent=3)
        return target_group.group_id, urls_with_priority
    
    if not abbrev:
        emit_func(f"[green]Found match. No upload candidates (all editions/torrents match).[/green]", indent=3)
    return target_group.group_id, []
