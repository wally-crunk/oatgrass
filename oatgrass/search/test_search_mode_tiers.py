from __future__ import annotations

import pytest

from oatgrass.config import OatgrassConfig, TrackerConfig
from oatgrass.search import search_mode


def _entry() -> dict:
    return {
        "group": {
            "id": 123,
            "name": "Example Album",
            "year": 1999,
            "releaseType": 1,
            "musicInfo": {"artists": [{"name": "Example Artist"}]},
        },
        "torrents": [{"media": "CD", "size": 12345}],
    }


def _tracker(name: str, url: str) -> TrackerConfig:
    return TrackerConfig(name=name, url=url, api_key="token")


def _config() -> OatgrassConfig:
    return OatgrassConfig(
        trackers={
            "ops": _tracker("OPS", "https://ops.example"),
            "red": _tracker("RED", "https://red.example"),
        }
    )


class _FakeAdapter:
    def __init__(self, tracker: TrackerConfig) -> None:
        self.tracker = tracker

    async def close(self) -> None:
        return None


@pytest.mark.asyncio
async def test_run_search_mode_strict_uses_tier1_only(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    async def _fake_load_entries_for_target(*_args, **_kwargs):
        return [_entry()], None, _tracker("OPS", "https://ops.example"), _tracker("RED", "https://red.example")

    async def _fake_search_with_tiers(
        _client,
        artist,
        album,
        year,
        release_type=None,
        media=None,
        max_tier=4,
    ):
        captured["artist"] = artist
        captured["album"] = album
        captured["year"] = year
        captured["release_type"] = release_type
        captured["media"] = media
        captured["max_tier"] = max_tier
        return {"groupId": 42, "groupName": "Example Album", "maxSize": 12345}

    async def _fake_sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr(search_mode, "_load_entries_for_target", _fake_load_entries_for_target)
    monkeypatch.setattr(search_mode, "GazelleServiceAdapter", _FakeAdapter)
    monkeypatch.setattr(search_mode, "search_with_tiers", _fake_search_with_tiers)
    monkeypatch.setattr(search_mode, "_emit", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(search_mode, "_emit_final_candidates", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(search_mode.asyncio, "sleep", _fake_sleep)

    await search_mode.run_search_mode(_config(), target="123", strict=True, basic=True, log=False)

    assert captured["artist"] == "Example Artist"
    assert captured["album"] == "Example Album"
    assert captured["year"] == 1999
    assert captured["release_type"] == 1
    assert captured["media"] == "CD"
    assert captured["max_tier"] == 1


@pytest.mark.asyncio
async def test_run_search_mode_nonstrict_uses_full_tier_range(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    async def _fake_load_entries_for_target(*_args, **_kwargs):
        return [_entry()], None, _tracker("OPS", "https://ops.example"), _tracker("RED", "https://red.example")

    async def _fake_search_with_tiers(
        _client,
        _artist,
        _album,
        _year,
        release_type=None,
        media=None,
        max_tier=4,
    ):
        captured["release_type"] = release_type
        captured["media"] = media
        captured["max_tier"] = max_tier
        return {"groupId": 42, "groupName": "Example Album", "maxSize": 12345}

    async def _fake_sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr(search_mode, "_load_entries_for_target", _fake_load_entries_for_target)
    monkeypatch.setattr(search_mode, "GazelleServiceAdapter", _FakeAdapter)
    monkeypatch.setattr(search_mode, "search_with_tiers", _fake_search_with_tiers)
    monkeypatch.setattr(search_mode, "_emit", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(search_mode, "_emit_final_candidates", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(search_mode.asyncio, "sleep", _fake_sleep)

    await search_mode.run_search_mode(_config(), target="123", strict=False, basic=True, log=False)

    assert captured["release_type"] == 1
    assert captured["media"] == "CD"
    assert captured["max_tier"] == 4
