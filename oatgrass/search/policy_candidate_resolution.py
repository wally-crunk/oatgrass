"""Shared helpers to build and evaluate policy-aware upload candidates."""

from __future__ import annotations

from oatgrass.config import TrackerConfig
from oatgrass.search.candidate_policy import (
    CandidatePolicy,
    PolicySummary,
    apply_candidate_policy,
)
from oatgrass.search.edition_parser import parse_group_from_browse
from oatgrass.search.gazelle_client import GazelleServiceAdapter
from oatgrass.search.parsers import as_int
from oatgrass.search.policy_enrichment import enrich_red_policy_fields
from oatgrass.search.types import TorrentInfo
from oatgrass.search.upload_candidates import UploadCandidate
from oatgrass.tracker_profile import tracker_needs_policy_enrichment


def build_no_match_candidates_from_entry(
    entry: dict,
    *,
    priority: int = 100,
) -> list[UploadCandidate]:
    group = entry.get("group", entry)
    if not isinstance(group, dict):
        group = {}
    raw_torrents = entry.get("torrents")
    if not isinstance(raw_torrents, list):
        return []

    normalized_torrents: list[dict] = []
    for torrent in raw_torrents:
        if not isinstance(torrent, dict):
            continue
        torrent_id = as_int(torrent.get("torrentid") or torrent.get("torrentId") or torrent.get("id"))
        if torrent_id is None:
            continue
        normalized = dict(torrent)
        normalized["torrentId"] = torrent_id
        normalized_torrents.append(normalized)
    if not normalized_torrents:
        return []

    artists = group.get("musicInfo", {}).get("artists", []) if isinstance(group.get("musicInfo"), dict) else []
    artist_name = str(artists[0].get("name") or "") if isinstance(artists, list) and artists and isinstance(artists[0], dict) else ""
    parsed = parse_group_from_browse(
        {
            "groupId": as_int(group.get("id")) or 0,
            "groupName": str(group.get("name") or ""),
            "groupYear": as_int(group.get("year")),
            "releaseType": group.get("releaseType"),
            "artist": artist_name,
            "torrents": normalized_torrents,
        },
        "SRC",
    )
    return [
        UploadCandidate(
            source_torrent=source_torrent,
            edition_id=edition.edition_id,
            edition_year=edition.year or 0,
            edition_title=edition.title or "(no title)",
            media=source_torrent.media,
            encoding=source_torrent.encoding,
            size=source_torrent.size,
            priority=priority,
        )
        for edition in parsed.editions
        for source_torrent in edition.torrents
    ]


def build_no_match_candidates_from_torrent(
    source_torrent: TorrentInfo,
    *,
    edition_id: int | None,
    edition_year: int,
    edition_title: str,
    priority: int = 100,
) -> list[UploadCandidate]:
    return [
        UploadCandidate(
            source_torrent=source_torrent,
            edition_id=edition_id,
            edition_year=edition_year,
            edition_title=edition_title,
            media=source_torrent.media,
            encoding=source_torrent.encoding,
            size=source_torrent.size,
            priority=priority,
        )
    ]


async def resolve_policy_candidates(
    candidates: list[UploadCandidate],
    *,
    source_tracker: TrackerConfig,
    source_client: GazelleServiceAdapter,
    policy: CandidatePolicy,
    enrichment_cache: dict[int, dict] | None = None,
) -> tuple[list[tuple[str, int]], list[str], PolicySummary]:
    if policy != CandidatePolicy.STANDARD and candidates and tracker_needs_policy_enrichment(source_tracker.name):
        if enrichment_cache is None:
            await enrich_red_policy_fields(source_client, candidates)
        else:
            await enrich_red_policy_fields(
                source_client,
                candidates,
                torrent_payload_cache=enrichment_cache,
            )

    policy_outcome = apply_candidate_policy(candidates, policy=policy)
    urls_with_priority = [
        (f"{source_tracker.url.rstrip('/')}/torrents.php?torrentid={c.source_torrent.torrent_id}", c.priority)
        for c in policy_outcome.candidates
    ]
    suppression_messages = [
        (
            "Suppressed: "
            f"{source_tracker.url.rstrip('/')}/torrents.php?torrentid={item.candidate.source_torrent.torrent_id} "
            f"({item.reason_text})"
        )
        for item in policy_outcome.suppressed
    ]
    return urls_with_priority, suppression_messages, policy_outcome.summary
