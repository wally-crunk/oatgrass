from __future__ import annotations

import oatgrass.logger as oat_logger


def test_api_wait_debug_drops_when_debug_disabled(monkeypatch):
    log = oat_logger.OatgrassLogger(debug=False)
    captured: list[tuple[str, str]] = []
    monkeypatch.setattr(log, "log", lambda msg, prefix="": captured.append((prefix, msg)))

    log.api_wait_debug("OPS", 0.321)

    assert captured == []


def test_api_wait_debug_emits_when_debug_enabled(monkeypatch):
    log = oat_logger.OatgrassLogger(debug=True)
    captured: list[tuple[str, str]] = []
    monkeypatch.setattr(log, "log", lambda msg, prefix="": captured.append((prefix, msg)))

    log.api_wait_debug("RED", 1.234)

    assert len(captured) == 1
    prefix, msg = captured[0]
    assert "[DEBUG]" in prefix
    assert "1.234s" in msg
    assert "RED" in msg


def test_api_wait_logs_one_time_note_per_tracker(monkeypatch):
    log = oat_logger.OatgrassLogger(debug=False)
    captured: list[tuple[str, str]] = []
    monkeypatch.setattr(log, "log", lambda msg, prefix="": captured.append((prefix, msg)))

    log.api_wait("OPS", 1.8)
    log.api_wait("OPS", 2.2)
    log.api_wait("RED", 2.0)

    assert captured == [
        ("[INFO] ", "API rate limiting active for OPS; request pacing is enabled."),
        ("[INFO] ", "API rate limiting active for RED; request pacing is enabled."),
    ]
