from __future__ import annotations

import builtins
from rich.text import Text

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
    captured_print: list[tuple[tuple[object, ...], dict]] = []
    captured_screen: list[tuple[object, dict]] = []

    def _fake_print(*args, **kwargs):
        captured_print.append((args, kwargs))

    monkeypatch.setattr(builtins, "print", _fake_print)
    log = oat_logger.OatgrassLogger(debug=False)
    monkeypatch.setattr(log._console, "print", lambda msg, **kwargs: captured_screen.append((msg, kwargs)))
    captured_print.clear()

    log.status("In progress")
    log.info("Done")

    assert len(captured_print) == 2
    assert len(captured_screen) == 1
    status_args, status_kwargs = captured_print[0]
    clear_args, clear_kwargs = captured_print[1]
    done_text, done_kwargs = captured_screen[0]
    assert status_args and str(status_args[0]).startswith("\rIn progress")
    assert status_kwargs.get("end") == ""
    assert clear_args and str(clear_args[0]).startswith("\r")
    assert clear_kwargs.get("end") == ""
    assert isinstance(done_text, Text)
    assert done_text.plain == "Done"
    assert done_kwargs == {}


def test_screen_text_styles_task_and_outcomes(monkeypatch):
    log = oat_logger.OatgrassLogger(debug=False)
    monkeypatch.setattr(log._console, "print", lambda *_args, **_kwargs: None)

    task = log._screen_text("[Task 7/987] 22s elapsed, ETA 1h01m27s (21:10:41)")
    candidate = log._screen_text("   Candidate found: 1 candidate(s) for source torrent #460152")
    match = log._screen_text("   Match found on target. Not a candidate.")
    group_line = log._screen_text("   red group #2671302 torrent #5937931 'THE SHOUT'")
    info = log._screen_text("[INFO] API rate limiting active for OPS; request pacing is enabled.")

    assert task.plain.startswith("[Task 7/987]")
    assert task.spans == []
    assert any(span.style == "yellow" for span in candidate.spans)
    assert any(span.style == "red" for span in match.spans)
    assert any(span.style == "grey50" for span in group_line.spans)
    assert any(span.style == "yellow" for span in group_line.spans)
    assert any(span.style == "cyan" for span in info.spans)


def test_screen_text_preserves_literal_brackets(monkeypatch):
    log = oat_logger.OatgrassLogger(debug=False)
    monkeypatch.setattr(log._console, "print", lambda *_args, **_kwargs: None)

    line = "[Task 1/2] [INFO] literal text should stay literal"
    rendered = log._screen_text(line)

    assert isinstance(rendered, Text)
    assert rendered.plain == line


def test_log_writes_plain_text_to_file(tmp_path, monkeypatch):
    out = tmp_path / "oatgrass.log"
    log = oat_logger.OatgrassLogger(log_file=out, debug=False)
    monkeypatch.setattr(log._console, "print", lambda *_args, **_kwargs: None)

    log.info("[Task 1/2] literal bracketed message")
    log.close()

    text = out.read_text(encoding="utf-8")
    assert "[Task 1/2] literal bracketed message" in text
