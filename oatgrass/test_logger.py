from __future__ import annotations

import builtins

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


def test_status_prints_inline_without_newline(monkeypatch):
    captured: list[tuple[tuple[object, ...], dict]] = []

    def _fake_print(*args, **kwargs):
        captured.append((args, kwargs))

    monkeypatch.setattr(builtins, "print", _fake_print)
    log = oat_logger.OatgrassLogger(debug=False)
    captured.clear()  # ignore startup banner

    log.status("Working row 3/10")

    assert len(captured) == 1
    args, kwargs = captured[0]
    assert args and str(args[0]).startswith("\rWorking row 3/10")
    assert kwargs.get("end") == ""


def test_log_clears_inline_status_before_print(monkeypatch):
    captured: list[tuple[tuple[object, ...], dict]] = []

    def _fake_print(*args, **kwargs):
        captured.append((args, kwargs))

    monkeypatch.setattr(builtins, "print", _fake_print)
    log = oat_logger.OatgrassLogger(debug=False)
    captured.clear()  # ignore startup banner

    log.status("In progress")
    log.info("Done")

    assert len(captured) == 3
    status_args, status_kwargs = captured[0]
    clear_args, clear_kwargs = captured[1]
    done_args, done_kwargs = captured[2]
    assert status_args and str(status_args[0]).startswith("\rIn progress")
    assert status_kwargs.get("end") == ""
    assert clear_args and str(clear_args[0]).startswith("\r")
    assert clear_kwargs.get("end") == ""
    assert done_args == ("Done",)
    assert "end" not in done_kwargs
