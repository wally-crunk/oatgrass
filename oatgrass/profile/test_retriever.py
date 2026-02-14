import json
from pathlib import Path
from typing import Any

import pytest

from oatgrass.config import TrackerConfig
from oatgrass.profile.retriever import ProfileRetriever


FIXTURE_DIR = Path(__file__).parent / "fixtures"


def _load_json(name: str) -> dict[str, Any]:
    return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))


class _FixtureService:
    def __init__(self, tracker: TrackerConfig, payload: dict[str, Any]) -> None:
        self.tracker = tracker
        self._payload = payload

    async def get_index(self) -> dict[str, Any]:
        return {"response": {"id": 77}}

    async def get_user_torrents(
        self,
        *,
        list_type: str,
        user_id: int,
        limit: int,
        offset: int,
    ) -> dict[str, Any]:
        _ = list_type, user_id, limit, offset
        return self._payload

    async def close(self) -> None:
        return None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("fixture_name", "tracker_name", "list_type", "expected_group", "expected_encoding"),
    [
        ("ops_user_torrents.json", "OPS", "snatched", 101, "24bit Lossless"),
        ("ops_user_torrents.json", "OPS", "uploaded", 102, "Lossless"),
        ("ops_user_torrents.json", "OPS", "downloaded", 103, "Lossless"),
        ("red_user_torrents.json", "RED", "snatched", 301, "V0 (VBR)"),
        ("red_user_torrents.json", "RED", "uploaded", 302, "Lossless"),
        ("red_user_torrents.json", "RED", "downloaded", 303, "320"),
    ],
)
async def test_retriever_parses_fixture_lists(
    fixture_name: str,
    tracker_name: str,
    list_type: str,
    expected_group: int,
    expected_encoding: str,
) -> None:
    tracker = TrackerConfig(name=tracker_name, url="https://example.invalid", api_key="token")
    payload = _load_json(fixture_name)
    retriever = ProfileRetriever(tracker, service_factory=lambda _: _FixtureService(tracker, payload))

    entries = await retriever.fetch(list_type)  # type: ignore[arg-type]
    await retriever.close()

    assert entries
    assert entries[0].tracker == tracker_name
    assert entries[0].group_id == expected_group
    assert entries[0].encoding == expected_encoding


@pytest.mark.asyncio
async def test_retriever_skips_possible_non_music_fixture_rows() -> None:
    tracker = TrackerConfig(name="OPS", url="https://example.invalid", api_key="token")
    payload = _load_json("ops_user_torrents.json")
    retriever = ProfileRetriever(tracker, service_factory=lambda _: _FixtureService(tracker, payload))

    entries = await retriever.fetch("snatched")
    await retriever.close()

    assert len(entries) == 1
    assert entries[0].group_id == 101


@pytest.mark.asyncio
async def test_retriever_fails_after_second_malformed_numeric_fixture_row() -> None:
    tracker = TrackerConfig(name="OPS", url="https://example.invalid", api_key="token")
    payload = _load_json("malformed_user_torrents.json")
    retriever = ProfileRetriever(tracker, service_factory=lambda _: _FixtureService(tracker, payload))

    with pytest.raises(ValueError, match="encountered 2 malformed numeric rows"):
        await retriever.fetch("snatched")
    await retriever.close()


@pytest.mark.asyncio
async def test_retriever_rejects_null_response_payload() -> None:
    tracker = TrackerConfig(name="OPS", url="https://example.invalid", api_key="token")
    payload = _load_json("null_payload_user_torrents.json")
    retriever = ProfileRetriever(tracker, service_factory=lambda _: _FixtureService(tracker, payload))

    with pytest.raises(ValueError, match="missing 'response' object"):
        await retriever.fetch("snatched")
    await retriever.close()
