from __future__ import annotations

import pytest

from oatgrass import api_verification


class _FakeResponse:
    def __init__(self, status: int, payload: dict, reason: str = "OK") -> None:
        self.status = status
        self.reason = reason
        self._payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def json(self) -> dict:
        return self._payload


class _FakeSession:
    def __init__(self, response: _FakeResponse) -> None:
        self._response = response
        self.calls: list[dict] = []

    def get(self, url: str, *, headers: dict, timeout: int):
        self.calls.append({"url": url, "headers": dict(headers), "timeout": timeout})
        return self._response


@pytest.mark.asyncio
async def test_verify_gazelle_tracker_ops_uses_token_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _no_wait(*_args, **_kwargs) -> float:
        return 0.0

    monkeypatch.setattr(api_verification, "enforce_gazelle_min_interval", _no_wait)

    payload = {"status": "success", "response": {"username": "u", "id": 1}}
    session = _FakeSession(_FakeResponse(200, payload))
    result = await api_verification.verify_gazelle_tracker(
        session,
        api_key="ops-key",
        url="https://orpheus.network",
        name="OPS",
        timeout=5,
    )

    assert result[1] is True
    assert session.calls[0]["headers"]["Authorization"] == "token ops-key"


@pytest.mark.asyncio
async def test_verify_gazelle_tracker_red_uses_raw_key(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _no_wait(*_args, **_kwargs) -> float:
        return 0.0

    monkeypatch.setattr(api_verification, "enforce_gazelle_min_interval", _no_wait)

    payload = {"status": "success", "response": {"username": "u", "id": 1}}
    session = _FakeSession(_FakeResponse(200, payload))
    result = await api_verification.verify_gazelle_tracker(
        session,
        api_key="red-key",
        url="https://redacted.sh",
        name="RED",
        timeout=5,
    )

    assert result[1] is True
    assert session.calls[0]["headers"]["Authorization"] == "red-key"
