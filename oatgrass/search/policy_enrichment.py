"""Policy-mode enrichment helpers for tracker-specific torrent fields."""

from __future__ import annotations

from typing import Any

from oatgrass.search.gazelle_client import GazelleServiceAdapter
from oatgrass.search.upload_candidates import UploadCandidate


def _as_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "yes", "1"}:
            return True
        if lowered in {"false", "no", "0"}:
            return False
    return None


def _parse_reason_list(*values: Any) -> list[str]:
    for value in values:
        if isinstance(value, list):
            return [str(item) for item in value if isinstance(item, (str, int, float, bool))]
    return []


async def enrich_red_policy_fields(
    source_client: GazelleServiceAdapter,
    candidates: list[UploadCandidate],
    *,
    torrent_payload_cache: dict[int, dict] | None = None,
) -> None:
    """Hydrate RED trumpable reason/checksum fields via torrent endpoint when needed.

    RED's torrentgroup payload often omits `trumpable_reasons` and `logChecksum` that
    are available from `action=torrent`.
    """
    by_torrent_id: dict[int, list[UploadCandidate]] = {}
    for candidate in candidates:
        source = candidate.source_torrent
        if source.trumpable is not True:
            continue
        if source.trumpable_reasons:
            continue
        by_torrent_id.setdefault(source.torrent_id, []).append(candidate)

    for torrent_id, bucket in by_torrent_id.items():
        payload = (
            torrent_payload_cache[torrent_id]
            if torrent_payload_cache is not None and torrent_id in torrent_payload_cache
            else await source_client.get_torrent(torrent_id)
        )
        if torrent_payload_cache is not None:
            torrent_payload_cache[torrent_id] = payload
        response = payload.get("response") if isinstance(payload, dict) else {}
        if not isinstance(response, dict):
            continue
        torrent = response.get("torrent")
        if not isinstance(torrent, dict):
            continue

        reasons = _parse_reason_list(torrent.get("trumpable_reasons"), torrent.get("trumpableReasons"))
        description = torrent.get("description") if isinstance(torrent.get("description"), str) else None
        log_checksum = _as_bool(torrent.get("logChecksum"))

        for candidate in bucket:
            source = candidate.source_torrent
            source.trumpable_reasons = reasons
            if description and not source.description:
                source.description = description
            if source.log_checksum is None and log_checksum is not None:
                source.log_checksum = log_checksum
