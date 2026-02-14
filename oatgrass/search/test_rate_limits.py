from __future__ import annotations

import pytest

from oatgrass import rate_limits


@pytest.mark.asyncio
async def test_gazelle_rate_limit_waits_for_same_server(monkeypatch: pytest.MonkeyPatch) -> None:
    rate_limits._reset_gazelle_rate_limits_for_tests()
    clock = {"now": 100.0}
    waits: list[float] = []

    monkeypatch.setattr(rate_limits.time, "monotonic", lambda: clock["now"])

    async def _fake_sleep(delay: float) -> None:
        waits.append(delay)
        clock["now"] += delay

    monkeypatch.setattr(rate_limits.asyncio, "sleep", _fake_sleep)

    first_wait = await rate_limits.enforce_gazelle_min_interval("https://redacted.sh/")
    second_wait = await rate_limits.enforce_gazelle_min_interval("https://REDActed.sh")

    assert first_wait == 0.0
    assert second_wait == pytest.approx(rate_limits.GAZELLE_MIN_INTERVAL_SECONDS)
    assert waits == [pytest.approx(rate_limits.GAZELLE_MIN_INTERVAL_SECONDS)]


@pytest.mark.asyncio
async def test_gazelle_rate_limit_does_not_cross_throttle_servers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rate_limits._reset_gazelle_rate_limits_for_tests()
    clock = {"now": 200.0}
    waits: list[float] = []

    monkeypatch.setattr(rate_limits.time, "monotonic", lambda: clock["now"])

    async def _fake_sleep(delay: float) -> None:
        waits.append(delay)
        clock["now"] += delay

    monkeypatch.setattr(rate_limits.asyncio, "sleep", _fake_sleep)

    red_wait = await rate_limits.enforce_gazelle_min_interval("https://redacted.sh")
    ops_wait = await rate_limits.enforce_gazelle_min_interval("https://orpheus.network")

    assert red_wait == 0.0
    assert ops_wait == 0.0
    assert waits == []
