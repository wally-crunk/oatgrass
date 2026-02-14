from __future__ import annotations

import pytest

from oatgrass.config import TrackerConfig
from oatgrass.profile.retriever import ProfileTorrent
from oatgrass.profile.search_service import (
    _evaluate_profile_entry,
    _filter_candidates_for_source_torrent,
    _find_torrent_in_group,
)
from oatgrass.search.types import TorrentInfo


class _FakeCandidate:
    def __init__(self, torrent_id: int) -> None:
        self.source_torrent = TorrentInfo(torrent_id, 1, "WEB", "FLAC", "Lossless", 1)


def _entry(group_id: int | None = 11, torrent_id: int | None = 22) -> ProfileTorrent:
    return ProfileTorrent(
        tracker="OPS",
        list_type="snatched",
        group_id=group_id,
        torrent_id=torrent_id,
        group_name="Demo Group",
        artist_name="Demo Artist",
        artist_id=1,
        media="WEB",
        format="FLAC",
        encoding="Lossless",
        metadata={},
    )


class _FakeClient:
    def __init__(self) -> None:
        self.group_calls = []
        self.search_calls = []
        self.torrent_calls = []

    async def get_group(self, group_id: int):
        self.group_calls.append(group_id)
        return {
            "response": {
                "group": {"name": "Demo Group", "year": 2001, "musicInfo": {"artists": [{"name": "Demo Artist"}]}},
                "torrents": [{"id": 22, "media": "WEB", "format": "FLAC", "encoding": "Lossless", "size": 10}],
            }
        }

    async def get_torrent(self, torrent_id: int):
        self.torrent_calls.append(torrent_id)
        return {
            "response": {
                "group": {"id": 11, "name": "Demo Group", "year": 2001, "musicInfo": {"artists": [{"name": "Demo Artist"}]}},
                "torrent": {"id": torrent_id, "media": "WEB", "format": "FLAC", "encoding": "Lossless", "size": 10},
            }
        }

    async def search(self, **kwargs):
        self.search_calls.append(kwargs)
        return {"response": {"results": []}}


def test_find_torrent_in_group_matches_id() -> None:
    torrents = [{"id": 2}, {"id": 3}]
    assert _find_torrent_in_group(torrents, 3) == {"id": 3}
    assert _find_torrent_in_group(torrents, 99) is None


def test_filter_candidates_for_source_torrent_blocks_siblings() -> None:
    good = _FakeCandidate(22)
    sibling = _FakeCandidate(23)
    filtered = _filter_candidates_for_source_torrent([good, sibling], 22)
    assert filtered == [good]


@pytest.mark.asyncio
async def test_evaluate_profile_entry_returns_priority_100_when_target_missing(monkeypatch):
    source_tracker = TrackerConfig(name="OPS", url="https://ops.example", api_key="token")
    target_tracker = TrackerConfig(name="RED", url="https://red.example", api_key="token")

    source_client = _FakeClient()
    target_client = _FakeClient()

    async def _fake_search_with_tiers(*_args, **_kwargs):
        return None

    monkeypatch.setattr("oatgrass.search.tier_search_service.search_with_tiers", _fake_search_with_tiers)

    candidates, skipped = await _evaluate_profile_entry(
        _entry(),
        source_tracker=source_tracker,
        opposite_tracker=target_tracker,
        source_client=source_client,
        target_client=target_client,
    )

    assert skipped is False
    assert candidates == [("https://ops.example/torrents.php?torrentid=22", 100)]


@pytest.mark.asyncio
async def test_evaluate_profile_entry_skips_missing_ids() -> None:
    source_tracker = TrackerConfig(name="OPS", url="https://ops.example", api_key="token")
    target_tracker = TrackerConfig(name="RED", url="https://red.example", api_key="token")

    source_client = _FakeClient()
    target_client = _FakeClient()

    candidates, skipped = await _evaluate_profile_entry(
        _entry(group_id=None, torrent_id=None),
        source_tracker=source_tracker,
        opposite_tracker=target_tracker,
        source_client=source_client,
        target_client=target_client,
    )

    assert skipped is True
    assert candidates == []


@pytest.mark.asyncio
async def test_evaluate_profile_entry_enriches_from_torrent_endpoint_when_group_missing(monkeypatch):
    source_tracker = TrackerConfig(name="OPS", url="https://ops.example", api_key="token")
    target_tracker = TrackerConfig(name="RED", url="https://red.example", api_key="token")
    source_client = _FakeClient()
    target_client = _FakeClient()

    async def _fake_search_with_tiers(*_args, **_kwargs):
        return None

    monkeypatch.setattr("oatgrass.search.tier_search_service.search_with_tiers", _fake_search_with_tiers)

    candidates, skipped = await _evaluate_profile_entry(
        _entry(group_id=None, torrent_id=22),
        source_tracker=source_tracker,
        opposite_tracker=target_tracker,
        source_client=source_client,
        target_client=target_client,
    )

    assert skipped is False
    assert candidates == [("https://ops.example/torrents.php?torrentid=22", 100)]
    assert source_client.torrent_calls == [22]


@pytest.mark.asyncio
async def test_evaluate_profile_entry_flags_missing_target_encoding_candidate(monkeypatch):
    source_tracker = TrackerConfig(name="OPS", url="https://ops.example", api_key="token")
    target_tracker = TrackerConfig(name="OPS2", url="https://ops2.example", api_key="token")
    source_client = _FakeClient()
    target_client = _FakeClient()

    async def _fake_search_with_tiers(*_args, **_kwargs):
        return {
            "groupId": 999,
            "groupName": "Demo Group",
            "groupYear": 2001,
            "artist": "Demo Artist",
            "torrents": [],
        }

    monkeypatch.setattr("oatgrass.search.tier_search_service.search_with_tiers", _fake_search_with_tiers)

    def _candidate_resolver(_source_group, _target_group):
        # Simulates "target group exists but this source encoding is missing".
        return [(22, 10)]

    candidates, skipped = await _evaluate_profile_entry(
        _entry(group_id=11, torrent_id=22),
        source_tracker=source_tracker,
        opposite_tracker=target_tracker,
        source_client=source_client,
        target_client=target_client,
        candidate_resolver=_candidate_resolver,
    )

    assert skipped is False
    assert candidates == [("https://ops.example/torrents.php?torrentid=22", 10)]
