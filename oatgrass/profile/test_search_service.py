from __future__ import annotations

from datetime import datetime, timezone

import pytest

from oatgrass.config import TrackerConfig
from oatgrass.profile.retriever import ProfileTorrent
from oatgrass.profile import search_service
from oatgrass.profile.search_service import (
    _ProgressState,
    _evaluate_profile_entry,
    _filter_candidates_for_source_torrent,
    _find_torrent_in_group,
    _format_duration,
    _progress_timing_text,
    _render_progress_line,
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


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (0, "0s"),
        (59, "59s"),
        (60, "1m00s"),
        (61, "1m01s"),
        (3599, "59m59s"),
        (3600, "1h00m00s"),
        (3661, "1h01m01s"),
    ],
)
def test_format_duration_boundary_values(seconds: int, expected: str) -> None:
    assert _format_duration(seconds) == expected


def test_progress_timing_text_eta_unknown_when_no_completed(monkeypatch: pytest.MonkeyPatch) -> None:
    state = _ProgressState(total=10, started_at=100.0, completed=0)
    monkeypatch.setattr(search_service.time, "monotonic", lambda: 135.0)

    elapsed_text, eta_text, finish_text = _progress_timing_text(state)

    assert elapsed_text == "35s"
    assert eta_text is None
    assert finish_text is None


def test_progress_timing_text_eta_known_with_24h_finish_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    state = _ProgressState(total=10, started_at=100.0, completed=5)
    monkeypatch.setattr(search_service.time, "monotonic", lambda: 200.0)

    class _FixedDateTime:
        @classmethod
        def now(cls):
            class _Now:
                def astimezone(self):
                    return datetime(2026, 2, 16, 21, 28, 34, tzinfo=timezone.utc)

            return _Now()

    monkeypatch.setattr(search_service, "datetime", _FixedDateTime)

    elapsed_text, eta_text, finish_text = _progress_timing_text(state)

    assert elapsed_text == "1m40s"
    assert eta_text == "1m40s"
    assert finish_text == "21:30:14"


def test_render_progress_line_uses_unobtrusive_working_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    state = _ProgressState(total=20, started_at=50.0, completed=4)
    monkeypatch.setattr(search_service.time, "monotonic", lambda: 90.0)

    class _FixedDateTime:
        @classmethod
        def now(cls):
            class _Now:
                def astimezone(self):
                    return datetime(2026, 2, 16, 11, 20, 0, tzinfo=timezone.utc)

            return _Now()

    monkeypatch.setattr(search_service, "datetime", _FixedDateTime)
    line = _render_progress_line(state)
    assert line.startswith("   Working: ")
    assert "elapsed, ETA " in line
    assert line.endswith(")")


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


@pytest.mark.asyncio
async def test_evaluate_profile_entry_group_only_returns_no_candidates_when_group_exists(monkeypatch):
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
        raise AssertionError("candidate_resolver should not be called in group-only mode")

    candidates, skipped = await _evaluate_profile_entry(
        _entry(group_id=11, torrent_id=22),
        source_tracker=source_tracker,
        opposite_tracker=target_tracker,
        source_client=source_client,
        target_client=target_client,
        group_only=True,
        candidate_resolver=_candidate_resolver,
    )

    assert skipped is False
    assert candidates == []


@pytest.mark.asyncio
async def test_evaluate_profile_entry_group_only_still_marks_missing_group(monkeypatch):
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
        group_only=True,
    )

    assert skipped is False
    assert candidates == [("https://ops.example/torrents.php?torrentid=22", 100)]
