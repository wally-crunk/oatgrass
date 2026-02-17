from __future__ import annotations

import pytest
from aiohttp import ClientResponseError, RequestInfo
from multidict import CIMultiDict
from rich.console import Console
from yarl import URL
from types import SimpleNamespace

from oatgrass.config import TrackerConfig
from oatgrass.profile.menu_service import (
    ProfileMenuService,
    ProfileListSummary,
    build_profile_summary,
    render_profile_summaries,
)
from oatgrass.profile.retriever import ProfileTorrent


def _entry(name: str, size: int) -> ProfileTorrent:
    return ProfileTorrent(
        tracker="OPS",
        list_type="snatched",
        group_id=1,
        torrent_id=2,
        group_name=name,
        artist_name="Artist",
        artist_id=3,
        media="WEB",
        format="FLAC",
        encoding="Lossless",
        metadata={"size": size},
    )


def test_build_profile_summary_uses_first_three_and_total_size() -> None:
    summary = build_profile_summary(
        "snatched",
        [_entry("One", 100), _entry("Two", 200), _entry("Three", 300), _entry("Four", 400)],
    )
    assert summary.count == 4
    assert summary.total_size == 1_000
    assert summary.first_three == ("One", "Two", "Three")


def test_build_profile_summary_reads_torrent_size_field() -> None:
    item = _entry("One", 0)
    item = ProfileTorrent(
        tracker=item.tracker,
        list_type=item.list_type,
        group_id=item.group_id,
        torrent_id=item.torrent_id,
        group_name=item.group_name,
        artist_name=item.artist_name,
        artist_id=item.artist_id,
        media=item.media,
        format=item.format,
        encoding=item.encoding,
        metadata={"torrentSize": "1,234"},
    )
    summary = build_profile_summary("snatched", [item])
    assert summary.total_size == 1_234


def test_render_profile_summaries_outputs_table_text() -> None:
    console = Console(record=True)
    render_profile_summaries(
        console,
        "OPS",
        [ProfileListSummary("snatched", 4, 1_000, ("One", "Two", "Three"))],
    )
    output = console.export_text()
    assert "Profile List Summary (OPS)" in output
    assert "snatched" in output
    assert "1,000" in output


@pytest.mark.asyncio
async def test_menu_service_fetches_all_lists_with_task_counters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, int, int]] = []

    class _FakeRetriever:
        def __init__(self, _tracker: TrackerConfig) -> None:
            self.closed = False

        async def fetch(self, list_type, task_index=1, task_total=1):
            calls.append((list_type, task_index, task_total))
            return [_entry(list_type, 100)]

        async def close(self):
            self.closed = True

    monkeypatch.setattr(
        "oatgrass.profile.menu_service.resolve_tracker_profile",
        lambda _: SimpleNamespace(list_types=("snatched", "uploaded", "downloaded")),
    )
    tracker = TrackerConfig(name="OPS", url="https://orpheus.network", api_key="token")
    service = ProfileMenuService(tracker, retriever_factory=_FakeRetriever)
    results = await service.fetch_all_lists()
    await service.close()

    assert set(results) == {"snatched", "uploaded", "downloaded"}
    assert calls == [
        ("snatched", 1, 3),
        ("uploaded", 2, 3),
        ("downloaded", 3, 3),
    ]


@pytest.mark.asyncio
async def test_menu_service_fetches_selected_lists_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, int, int]] = []

    class _FakeRetriever:
        def __init__(self, _tracker: TrackerConfig) -> None:
            self.closed = False

        async def fetch(self, list_type, task_index=1, task_total=1):
            calls.append((list_type, task_index, task_total))
            return [_entry(list_type, 100)]

        async def close(self):
            self.closed = True

    monkeypatch.setattr(
        "oatgrass.profile.menu_service.resolve_tracker_profile",
        lambda _: SimpleNamespace(list_types=("seeding", "leeching", "uploaded", "snatched")),
    )

    tracker = TrackerConfig(name="OPS", url="https://orpheus.network", api_key="token")
    service = ProfileMenuService(tracker, retriever_factory=_FakeRetriever)
    results = await service.fetch_all_lists(["leeching"])
    await service.close()

    assert set(results) == {"leeching"}
    assert calls == [("leeching", 1, 1)]


@pytest.mark.asyncio
async def test_menu_service_handles_client_response_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request_info = RequestInfo(
        URL("https://redacted.sh/ajax.php"),
        "GET",
        CIMultiDict(),
        URL("https://redacted.sh/ajax.php"),
    )
    exc = ClientResponseError(
        request_info,
        (),
        status=400,
        message="downloaded is not a valid type",
    )

    class _FakeRetriever:
        def __init__(self, _tracker: TrackerConfig) -> None:
            self.closed = False

        async def fetch(self, list_type, task_index=1, task_total=1):
            if list_type == "downloaded":
                raise exc
            return [_entry("Entry", 100)]

        async def close(self):
            self.closed = True

    def _resolve(_tracker_name: str) -> SimpleNamespace:
        return SimpleNamespace(list_types=("downloaded", "snatched"))

    monkeypatch.setattr(
        "oatgrass.profile.menu_service.resolve_tracker_profile",
        _resolve,
    )

    tracker = TrackerConfig(name="RED", url="https://redacted.sh", api_key="token")
    service = ProfileMenuService(tracker, retriever_factory=_FakeRetriever)
    results = await service.fetch_all_lists()
    await service.close()

    assert results["snatched"]
    assert results["downloaded"] == []
