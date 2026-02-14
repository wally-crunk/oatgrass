from __future__ import annotations

import asyncio

from oatgrass.config import TrackerConfig
from oatgrass.search import gazelle_client, search_mode


class _FakeWaitLogger:
    def __init__(self) -> None:
        self.debug_waits: list[tuple[str, float]] = []
        self.info_waits: list[tuple[str, float]] = []

    def api_wait_debug(self, tracker: str, seconds: float) -> None:
        self.debug_waits.append((tracker, seconds))

    def api_wait(self, tracker: str, seconds: float) -> None:
        self.info_waits.append((tracker, seconds))

    def api_request(self, _method: str, _url: str, _params: dict) -> None:
        return None

    def api_response(self, _status: int, _data: dict, _elapsed_ms: float) -> None:
        return None

    def api_retry(self, _tracker: str, _attempt: int, _max_retries: int, _delay: int) -> None:
        return None

    def api_failed(self, _tracker: str, _max_retries: int) -> None:
        return None


class _FakeResponseCtx:
    def __init__(self, payload: dict) -> None:
        self._payload = payload
        self.status = 200

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def raise_for_status(self) -> None:
        return None

    async def json(self) -> dict:
        return self._payload


class _FakeSession:
    def __init__(self, *args, **kwargs) -> None:  # pragma: no cover - signature compatibility
        self._payload = {"status": "success", "response": {}}
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def get(self, *args, **kwargs):
        return _FakeResponseCtx(self._payload)

    async def close(self) -> None:
        self.closed = True


def test_adapter_wait_logging_emits_debug_always_but_info_only_above_threshold(monkeypatch):
    tracker = TrackerConfig(name="OPS", url="https://ops.example", api_key="token")
    adapter = gazelle_client.GazelleServiceAdapter(tracker)
    fake_log = _FakeWaitLogger()

    monkeypatch.setattr(gazelle_client.logger, "get_logger", lambda: fake_log)

    async def _wait_small(
        _base_url: str,
        min_interval_seconds: float = 2.0,
        tracker_name: str | None = None,
        auth_mode: str = "api_key",
    ) -> float:
        _ = min_interval_seconds
        _ = tracker_name, auth_mode
        return 0.2

    monkeypatch.setattr(gazelle_client, "enforce_gazelle_min_interval", _wait_small)
    asyncio.run(adapter._enforce_interval())

    assert fake_log.debug_waits == [("OPS", 0.2)]
    assert fake_log.info_waits == []

    async def _wait_big(
        _base_url: str,
        min_interval_seconds: float = 2.0,
        tracker_name: str | None = None,
        auth_mode: str = "api_key",
    ) -> float:
        _ = min_interval_seconds
        _ = tracker_name, auth_mode
        return 1.9

    monkeypatch.setattr(gazelle_client, "enforce_gazelle_min_interval", _wait_big)
    asyncio.run(adapter._enforce_interval())
    asyncio.run(adapter.close())

    assert fake_log.debug_waits[-1] == ("OPS", 1.9)
    assert fake_log.info_waits == [("OPS", 1.9)]


def test_search_mode_wait_logging_emits_debug_for_direct_fetch_helpers(monkeypatch):
    tracker = TrackerConfig(name="RED", url="https://red.example", api_key="token")
    fake_log = _FakeWaitLogger()

    monkeypatch.setattr(gazelle_client.logger, "get_logger", lambda: fake_log)
    monkeypatch.setattr(gazelle_client.aiohttp, "ClientSession", _FakeSession)

    async def _wait_small(
        _base_url: str,
        min_interval_seconds: float = 2.0,
        tracker_name: str | None = None,
        auth_mode: str = "api_key",
    ) -> float:
        _ = min_interval_seconds
        _ = tracker_name, auth_mode
        return 0.1

    monkeypatch.setattr(gazelle_client, "enforce_gazelle_min_interval", _wait_small)
    payload = asyncio.run(search_mode._fetch_collage(tracker, collage_id=1, page=1))
    assert payload["status"] == "success"
    assert fake_log.debug_waits == [("RED", 0.1)]
    assert fake_log.info_waits == []

    async def _wait_big(
        _base_url: str,
        min_interval_seconds: float = 2.0,
        tracker_name: str | None = None,
        auth_mode: str = "api_key",
    ) -> float:
        _ = min_interval_seconds
        _ = tracker_name, auth_mode
        return 1.8

    monkeypatch.setattr(gazelle_client, "enforce_gazelle_min_interval", _wait_big)
    payload = asyncio.run(search_mode._fetch_torrent_group(tracker, group_id=2))
    assert payload["status"] == "success"
    assert fake_log.debug_waits[-1] == ("RED", 1.8)
    assert fake_log.info_waits == [("RED", 1.8)]
