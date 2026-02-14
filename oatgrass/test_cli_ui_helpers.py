from __future__ import annotations

import importlib
import sys
import types

import pytest


def _load_cli_module(monkeypatch: pytest.MonkeyPatch):
    # cli.py imports scipy at module import time.
    monkeypatch.setitem(sys.modules, "scipy", types.ModuleType("scipy"))
    import oatgrass.cli as cli

    return importlib.reload(cli)


def test_ui_info_warn_error_emit_prefixed_messages(monkeypatch: pytest.MonkeyPatch) -> None:
    cli = _load_cli_module(monkeypatch)
    lines: list[str] = []
    monkeypatch.setattr(cli.console, "print", lambda msg, *_args, **_kwargs: lines.append(str(msg)))

    cli._ui_info("hello")
    cli._ui_warn("careful")
    cli._ui_error("boom")

    assert lines == [
        "[cyan][INFO][/cyan] hello",
        "[yellow][WARNING][/yellow] careful",
        "[red][ERROR][/red] boom",
    ]


def test_ui_prompt_with_and_without_default(monkeypatch: pytest.MonkeyPatch) -> None:
    cli = _load_cli_module(monkeypatch)
    calls: list[tuple[str, str | None]] = []

    def _fake_ask(label: str, default: str | None = None) -> str:
        calls.append((label, default))
        return "answer"

    monkeypatch.setattr(cli.Prompt, "ask", _fake_ask)

    assert cli._ui_prompt("Label") == "answer"
    assert cli._ui_prompt("Label2", default="X") == "answer"
    assert calls == [("Label", None), ("Label2", "X")]


def test_warn_missing_scipy_startup_emits_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    cli = _load_cli_module(monkeypatch)
    lines: list[str] = []
    monkeypatch.setattr(cli, "_has_scipy", lambda: False)
    monkeypatch.setattr(cli.console, "print", lambda msg, *_args, **_kwargs: lines.append(str(msg)))

    cli._warn_missing_scipy_startup()

    assert any("scipy not found" in line for line in lines)
    assert any("source venv/bin/activate" in line for line in lines)


def test_warn_missing_scipy_hint_only_emits_once(monkeypatch: pytest.MonkeyPatch) -> None:
    cli = _load_cli_module(monkeypatch)
    lines: list[str] = []
    monkeypatch.setattr(cli.console, "print", lambda msg, *_args, **_kwargs: lines.append(str(msg)))

    cli._warn_missing_scipy_hint()
    cli._warn_missing_scipy_hint()

    assert sum("source venv/bin/activate" in line for line in lines) == 1
