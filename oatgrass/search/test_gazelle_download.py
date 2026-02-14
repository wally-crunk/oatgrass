from __future__ import annotations

import asyncio

import pytest

from oatgrass.config import TrackerConfig
from oatgrass.search import gazelle_client


class _FakeResponseCtx:
    def __init__(
        self,
        *,
        status: int = 200,
        payload: dict | None = None,
        headers: dict[str, str] | None = None,
        reason: str = "OK",
    ) -> None:
        self.status = status
        self.reason = reason
        self._payload = payload or {}
        self.headers = headers or {}
        self.request_info = None
        self.history = ()

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def json(self) -> dict:
        return self._payload

    async def text(self) -> str:
        return str(self._payload)


class _FakeSession:
    def __init__(self, *args, **kwargs) -> None:  # pragma: no cover - signature compatibility
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def get(self, *args, **kwargs):
        return _FakeResponseCtx()

    async def close(self) -> None:
        self.closed = True


class _SequencedSession(_FakeSession):
    def __init__(self, responses: list[_FakeResponseCtx]) -> None:
        super().__init__()
        self._responses = responses
        self.calls = 0

    def get(self, *args, **kwargs):
        idx = min(self.calls, len(self._responses) - 1)
        self.calls += 1
        return self._responses[idx]


class _FakeLog:
    def api_request(self, *_args, **_kwargs) -> None:
        return None

    def api_response(self, *_args, **_kwargs) -> None:
        return None

    def api_retry(self, *_args, **_kwargs) -> None:
        return None

    def api_failed(self, *_args, **_kwargs) -> None:
        return None

    def debug(self, *_args, **_kwargs) -> None:
        return None

    def api_wait_debug(self, *_args, **_kwargs) -> None:
        return None

    def api_wait(self, *_args, **_kwargs) -> None:
        return None


def test_profile_endpoints_delegate_to_request(monkeypatch) -> None:
    tracker = TrackerConfig(name="OPS", url="https://ops.example", api_key="token")
    adapter = gazelle_client.GazelleServiceAdapter(tracker)
    seen_params: list[dict] = []

    async def _fake_request(params: dict) -> dict:
        seen_params.append(dict(params))
        return {"status": "success", "response": {}}

    monkeypatch.setattr(adapter, "_request", _fake_request)

    profile = asyncio.run(adapter.get_index())
    torrents = asyncio.run(
        adapter.get_user_torrents(list_type="snatched", user_id=7, limit=50, offset=100)
    )
    asyncio.run(adapter.close())

    assert profile["status"] == "success"
    assert torrents["status"] == "success"
    assert seen_params == [
        {"action": "index"},
        {"action": "user_torrents", "type": "snatched", "id": 7, "limit": 50, "offset": 100},
    ]


def test_request_does_not_retry_http_400(monkeypatch: pytest.MonkeyPatch) -> None:
    tracker = TrackerConfig(name="OPS", url="https://ops.example", api_key="token")
    adapter = gazelle_client.GazelleServiceAdapter(tracker)
    session = _SequencedSession(
        [_FakeResponseCtx(status=400, payload={"error": "bad request"}, reason="Bad Request")]
    )

    async def _fake_ensure_session():
        return session

    async def _fake_enforce() -> None:
        return None

    async def _no_sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr(adapter, "_ensure_session", _fake_ensure_session)
    monkeypatch.setattr(adapter, "_enforce_interval", _fake_enforce)
    monkeypatch.setattr(gazelle_client.asyncio, "sleep", _no_sleep)
    monkeypatch.setattr(gazelle_client.logger, "get_logger", lambda: _FakeLog())

    with pytest.raises(gazelle_client.aiohttp.ClientResponseError) as exc_info:
        asyncio.run(adapter.get_index())
    assert exc_info.value.status == 400
    assert session.calls == 1
    asyncio.run(adapter.close())


def test_request_retries_http_429_with_retry_after(monkeypatch: pytest.MonkeyPatch) -> None:
    tracker = TrackerConfig(name="OPS", url="https://ops.example", api_key="token")
    adapter = gazelle_client.GazelleServiceAdapter(tracker)
    session = _SequencedSession(
        [
            _FakeResponseCtx(status=429, payload={"error": "throttle"}, headers={"Retry-After": "3"}),
            _FakeResponseCtx(status=200, payload={"status": "success", "response": {"id": 1}}),
        ]
    )
    sleeps: list[float] = []

    async def _fake_ensure_session():
        return session

    async def _fake_enforce() -> None:
        return None

    async def _record_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(adapter, "_ensure_session", _fake_ensure_session)
    monkeypatch.setattr(adapter, "_enforce_interval", _fake_enforce)
    monkeypatch.setattr(gazelle_client.asyncio, "sleep", _record_sleep)
    monkeypatch.setattr(gazelle_client.logger, "get_logger", lambda: _FakeLog())

    payload = asyncio.run(adapter.get_index())
    assert payload["status"] == "success"
    assert session.calls == 2
    assert sleeps == [3]
    asyncio.run(adapter.close())
