from __future__ import annotations

import importlib
import sys
import types
from types import SimpleNamespace

import pytest

from oatgrass.config import OatgrassConfig, TrackerConfig
from oatgrass.profile.retriever import ProfileTorrent
from oatgrass.profile.session_state import ProfileSessionState


def _config() -> OatgrassConfig:
    return OatgrassConfig(
        trackers={
            "red": TrackerConfig(name="RED", url="https://redacted.sh", api_key="red-key"),
            "ops": TrackerConfig(name="OPS", url="https://orpheus.network", api_key="ops-key"),
        }
    )


def _entry(list_type: str = "snatched", group_id: int = 11, torrent_id: int = 22) -> ProfileTorrent:
    return ProfileTorrent(
        tracker="OPS",
        list_type=list_type,
        group_id=group_id,
        torrent_id=torrent_id,
        group_name="Demo Album",
        artist_name="Demo Artist",
        artist_id=1,
        media="CD",
        format="FLAC",
        encoding="Lossless",
        metadata={},
    )


def _seeded_session_state(entry: ProfileTorrent):
    class _SeededSessionState(ProfileSessionState):
        def __init__(self) -> None:
            super().__init__()
            self.set_snapshot(
                "ops",
                {"snatched": [entry], "uploaded": [], "seeding": [], "leeching": []},
            )

    return _SeededSessionState


def _load_cli_module(monkeypatch: pytest.MonkeyPatch):
    # cli.py guards import-time search dependencies by importing scipy.
    monkeypatch.setitem(sys.modules, "scipy", types.ModuleType("scipy"))
    import oatgrass.cli as cli

    return importlib.reload(cli)


@pytest.mark.parametrize(
    ("raw_choice", "expected"),
    [
        ("U", "uploaded"),
        ("L", "leeching"),
        ("snatched", "snatched"),
        ("A", "all"),
        ("alpha", None),
        ("", None),
    ],
)
def test_prompt_profile_list_choice_handles_good_bad_null_inputs(
    monkeypatch: pytest.MonkeyPatch, raw_choice: str, expected: str | None
) -> None:
    cli = _load_cli_module(monkeypatch)
    monkeypatch.setattr(cli.Prompt, "ask", lambda *_args, **_kwargs: raw_choice)

    available = ["seeding", "leeching", "uploaded", "snatched"]
    assert cli._prompt_profile_list_choice(available) == expected


def test_prompt_source_tracker_choice_uses_cached_default(monkeypatch: pytest.MonkeyPatch) -> None:
    cli = _load_cli_module(monkeypatch)
    defaults: dict[str, str] = {}

    def _fake_ask(_label: str, default: str = "") -> str:
        defaults["source_default"] = default
        return "red"

    monkeypatch.setattr(cli.console, "print", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(cli.Prompt, "ask", _fake_ask)

    selected = cli._prompt_source_tracker_choice(_config(), cached_tracker="ops")
    assert defaults["source_default"] == "OPS"
    assert selected == "red"


def test_prompt_source_tracker_choice_rejects_unknown_tracker(monkeypatch: pytest.MonkeyPatch) -> None:
    cli = _load_cli_module(monkeypatch)
    monkeypatch.setattr(cli.console, "print", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(cli.Prompt, "ask", lambda *_args, **_kwargs: "invalid")

    with pytest.raises(ValueError, match="not configured"):
        cli._prompt_source_tracker_choice(_config(), cached_tracker=None)


def test_ensure_cache_for_followup_action_decline_refetch(monkeypatch: pytest.MonkeyPatch) -> None:
    cli = _load_cli_module(monkeypatch)
    cache = ProfileSessionState()
    monkeypatch.setattr(cli.console, "print", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(cli.Prompt, "ask", lambda *_args, **_kwargs: "N")

    called: list[tuple[str | None]] = []

    async def _fake_summary_menu(_config: OatgrassConfig, tracker_key: str | None = None):
        called.append((tracker_key,))
        return "ops", {"snatched": [_entry()]}

    monkeypatch.setattr(cli, "_run_profile_summary_menu", _fake_summary_menu)

    ok = cli._ensure_cache_for_followup_action(
        _config(),
        cache=cache,
        list_types=["snatched"],
        option_choice="M",
        tracker_key="ops",
    )
    assert ok is False
    assert called == []


def test_ensure_cache_for_followup_action_refetches_and_sets_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cli = _load_cli_module(monkeypatch)
    cache = ProfileSessionState()
    monkeypatch.setattr(cli.console, "print", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(cli.Prompt, "ask", lambda *_args, **_kwargs: "F")

    async def _fake_summary_menu(_config: OatgrassConfig, tracker_key: str | None = None):
        return "ops", {"snatched": [_entry()], "uploaded": [], "seeding": [], "leeching": []}

    monkeypatch.setattr(cli, "_run_profile_summary_menu", _fake_summary_menu)

    ok = cli._ensure_cache_for_followup_action(
        _config(),
        cache=cache,
        list_types=["snatched"],
        option_choice="M",
        tracker_key="ops",
    )
    assert ok is True
    assert cache.has_list("ops", "snatched")


def test_ensure_cache_for_followup_action_false_when_requested_list_stays_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cli = _load_cli_module(monkeypatch)
    cache = ProfileSessionState()
    monkeypatch.setattr(cli.console, "print", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(cli.Prompt, "ask", lambda *_args, **_kwargs: "F")

    async def _fake_summary_menu(_config: OatgrassConfig, tracker_key: str | None = None):
        return "ops", {"snatched": [], "uploaded": [_entry(list_type="uploaded")], "seeding": [], "leeching": []}

    monkeypatch.setattr(cli, "_run_profile_summary_menu", _fake_summary_menu)

    ok = cli._ensure_cache_for_followup_action(
        _config(),
        cache=cache,
        list_types=["snatched"],
        option_choice="M",
        tracker_key="ops",
    )
    assert ok is False


def test_ensure_cache_for_followup_action_uses_cached_without_fetch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cli = _load_cli_module(monkeypatch)
    cache = ProfileSessionState()
    cache.set_snapshot("ops", {"snatched": [_entry()], "uploaded": [], "seeding": [], "leeching": []})
    monkeypatch.setattr(cli, "_prompt_profile_source_choice", lambda _default: "cached")

    called = {"summary": False}

    async def _fake_summary_menu(_config: OatgrassConfig, tracker_key: str | None = None):
        called["summary"] = True
        return "ops", {"snatched": [_entry()]}

    monkeypatch.setattr(cli, "_run_profile_summary_menu", _fake_summary_menu)

    ok = cli._ensure_cache_for_followup_action(
        _config(),
        cache=cache,
        list_types=["snatched"],
        option_choice="M",
        tracker_key="ops",
    )
    assert ok is True
    assert called["summary"] is False


def test_ensure_cache_for_followup_action_loads_from_disk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cli = _load_cli_module(monkeypatch)
    cache = ProfileSessionState()
    monkeypatch.setattr(cli.console, "print", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        cli.Prompt,
        "ask",
        lambda label, **_kwargs: "/tmp/fake.profile-lists.json" if label == "Profile list JSON path" else "L",
    )
    monkeypatch.setattr(
        cli,
        "_load_profile_lists_from_disk",
        lambda *_args, **_kwargs: {"snatched": [_entry()], "uploaded": [], "seeding": [], "leeching": []},
    )

    ok = cli._ensure_cache_for_followup_action(
        _config(),
        cache=cache,
        list_types=["snatched"],
        option_choice="M",
        tracker_key="ops",
    )
    assert ok is True
    assert cache.has_list("ops", "snatched")


def test_ensure_cache_for_followup_action_invalid_source_choice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cli = _load_cli_module(monkeypatch)
    cache = ProfileSessionState()
    monkeypatch.setattr(cli.console, "print", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(cli.Prompt, "ask", lambda *_args, **_kwargs: "Z")
    ok = cli._ensure_cache_for_followup_action(
        _config(),
        cache=cache,
        list_types=["snatched"],
        option_choice="M",
        tracker_key="ops",
    )
    assert ok is False


@pytest.mark.parametrize("menu_choice", ["M", "2"])
def test_main_menu_option_two_delegates_to_profile_search(
    monkeypatch: pytest.MonkeyPatch,
    menu_choice: str,
) -> None:
    cli = _load_cli_module(monkeypatch)
    seeded_entry = _entry()

    prompts = iter([menu_choice, "ops", "snatched", "C", "Y", "", "Q"])
    delegated: dict[str, object] = {}
    rendered: dict[str, object] = {}

    async def _fake_profile_search(*, config, source_tracker_key, list_type, entries, group_only=False):
        delegated["source_tracker_key"] = source_tracker_key
        delegated["list_type"] = list_type
        delegated["entries"] = list(entries)
        delegated["group_only"] = group_only
        delegated["config"] = config
        return SimpleNamespace(candidate_urls=[("https://ops/torrents.php?torrentid=22", 10)], processed=1, skipped=0)

    monkeypatch.setattr(cli, "ProfileSessionState", _seeded_session_state(seeded_entry))
    monkeypatch.setattr(cli, "run_profile_list_search", _fake_profile_search)
    monkeypatch.setattr(
        cli,
        "_display_profile_search_result",
        lambda candidate_urls, processed, skipped: rendered.update(
            {"candidate_urls": candidate_urls, "processed": processed, "skipped": skipped}
        ),
    )
    monkeypatch.setattr(cli, "display_config_table", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(cli.console, "clear", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(cli.console, "print", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(cli.Prompt, "ask", lambda *_args, **_kwargs: next(prompts))

    cli.main_menu(_config())

    assert delegated["source_tracker_key"] == "ops"
    assert delegated["list_type"] == "snatched"
    assert delegated["entries"] == [seeded_entry]
    assert rendered["processed"] == 1
    assert rendered["skipped"] == 0


@pytest.mark.parametrize("menu_choice", ["M", "2"])
def test_main_menu_option_two_can_abort_after_estimate(
    monkeypatch: pytest.MonkeyPatch,
    menu_choice: str,
) -> None:
    cli = _load_cli_module(monkeypatch)
    seeded_entry = _entry()

    prompts = iter([menu_choice, "ops", "snatched", "C", "c", "", "Q"])
    delegated = {"called": False}

    async def _fake_profile_search(*, config, source_tracker_key, list_type, entries, group_only=False):
        delegated["called"] = True
        return SimpleNamespace(candidate_urls=[], processed=0, skipped=0)

    monkeypatch.setattr(cli, "ProfileSessionState", _seeded_session_state(seeded_entry))
    monkeypatch.setattr(cli, "run_profile_list_search", _fake_profile_search)
    monkeypatch.setattr(cli, "display_config_table", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(cli.console, "clear", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(cli.console, "print", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(cli.Prompt, "ask", lambda *_args, **_kwargs: next(prompts))

    cli.main_menu(_config())

    assert delegated["called"] is False


@pytest.mark.parametrize("menu_choice", ["M", "2"])
def test_main_menu_option_two_warns_when_scipy_missing(
    monkeypatch: pytest.MonkeyPatch,
    menu_choice: str,
) -> None:
    cli = _load_cli_module(monkeypatch)
    seeded_entry = _entry()
    lines: list[str] = []
    delegated = {"called": False}

    async def _fake_profile_search(*, config, source_tracker_key, list_type, entries, group_only=False):
        delegated["called"] = True
        return SimpleNamespace(candidate_urls=[], processed=0, skipped=0)

    prompts = iter([menu_choice, "ops", "snatched", "C", "N", "", "Q"])
    monkeypatch.setattr(cli, "ProfileSessionState", _seeded_session_state(seeded_entry))
    monkeypatch.setattr(cli, "_has_scipy", lambda: False)
    monkeypatch.setattr(cli, "run_profile_list_search", _fake_profile_search)
    monkeypatch.setattr(cli, "display_config_table", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(cli.console, "clear", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(cli.console, "print", lambda msg="", *_args, **_kwargs: lines.append(str(msg)))
    monkeypatch.setattr(cli.Prompt, "ask", lambda *_args, **_kwargs: next(prompts))

    cli.main_menu(_config())

    assert delegated["called"] is False
    assert any("edition-aware matching is unavailable for option 2/M" in line for line in lines)


@pytest.mark.parametrize("menu_choice", ["G", "1"])
def test_main_menu_option_one_accepts_letter_or_number(
    monkeypatch: pytest.MonkeyPatch,
    menu_choice: str,
) -> None:
    cli = _load_cli_module(monkeypatch)
    calls = {"summary_called": 0}
    prompts = iter([menu_choice, "ops", "snatched", "Q"])

    async def _fake_summary_menu(_config: OatgrassConfig, tracker_key: str | None = None):
        calls["summary_called"] += 1
        return "ops", {"snatched": [_entry()], "uploaded": [], "seeding": [], "leeching": []}

    monkeypatch.setattr(cli, "_run_profile_summary_menu", _fake_summary_menu)
    monkeypatch.setattr(cli, "display_config_table", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(cli.console, "clear", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(cli.console, "print", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(cli.Prompt, "ask", lambda *_args, **_kwargs: next(prompts))

    cli.main_menu(_config())

    assert calls["summary_called"] == 1


def test_run_search_mode_prompt_falls_back_to_group_mode_when_scipy_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cli = _load_cli_module(monkeypatch)
    delegated: dict[str, object] = {}
    prompts = iter(["123", "R", "N", "F"])
    lines: list[str] = []

    async def _fake_run_search_mode(
        _config,
        search_mode_target,
        tracker_key=None,
        strict=False,
        abbrev=False,
        verbose=False,
        debug=False,
        basic=False,
        no_discogs=False,
    ):
        delegated["target"] = search_mode_target
        delegated["basic"] = basic
        delegated["tracker_key"] = tracker_key

    monkeypatch.setattr(cli, "_has_scipy", lambda: False)
    monkeypatch.setattr(cli, "run_search_mode", _fake_run_search_mode)
    monkeypatch.setattr(cli.console, "print", lambda msg="", *_args, **_kwargs: lines.append(str(msg)))
    monkeypatch.setattr(cli.Prompt, "ask", lambda *_args, **_kwargs: next(prompts))

    cli._run_search_mode_prompt(_config())

    assert delegated["target"] == "123"
    assert delegated["tracker_key"] == "red"
    assert delegated["basic"] is True
    assert any("matching mode is fixed to Group-only" in line for line in lines)
    assert not any("Matching mode" in line for line in lines)


def test_largest_duration_unit_uses_single_largest_unit(monkeypatch: pytest.MonkeyPatch) -> None:
    cli = _load_cli_module(monkeypatch)

    assert cli._largest_duration_unit(120) == (2.0, "minutes")
    assert cli._largest_duration_unit(7_200) == (2.0, "hours")
    assert cli._largest_duration_unit(172_800) == (2.0, "days")


def test_show_profile_search_estimate_skips_when_under_one_minute(monkeypatch: pytest.MonkeyPatch) -> None:
    cli = _load_cli_module(monkeypatch)
    lines: list[str] = []
    monkeypatch.setattr(cli.console, "print", lambda msg, *_args, **_kwargs: lines.append(str(msg)))

    # With OPS source and RED target, per-row estimate is 4 calls x 2s = 8s (< 60s).
    cli._show_profile_search_estimate(_config(), "ops", "snatched", 1)

    assert lines == []


@pytest.mark.parametrize("invalid_choice", ["GG", "X", "\u2603"])
def test_main_menu_invalid_choice_is_handled_and_warns(
    monkeypatch: pytest.MonkeyPatch, invalid_choice: str
) -> None:
    cli = _load_cli_module(monkeypatch)
    lines: list[str] = []
    prompts = iter([invalid_choice, "", "Q"])

    monkeypatch.setattr(cli, "display_config_table", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(cli.console, "clear", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(cli.console, "print", lambda msg="", *_args, **_kwargs: lines.append(str(msg)))
    monkeypatch.setattr(cli.Prompt, "ask", lambda *_args, **_kwargs: next(prompts))

    cli.main_menu(_config())

    assert any("Unknown choice" in line for line in lines)


def test_main_menu_quit_message_includes_elapsed(monkeypatch: pytest.MonkeyPatch) -> None:
    cli = _load_cli_module(monkeypatch)
    lines: list[str] = []
    prompts = iter(["Q"])

    # Simulate a 12.5s session.
    monkeypatch.setattr(cli, "_CLI_SESSION_START_MONOTONIC", 100.0)
    monkeypatch.setattr(cli.time, "monotonic", lambda: 112.5)
    monkeypatch.setattr(cli, "display_config_table", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(cli.console, "clear", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(cli.console, "print", lambda msg="", *_args, **_kwargs: lines.append(str(msg)))
    monkeypatch.setattr(cli.Prompt, "ask", lambda *_args, **_kwargs: next(prompts))

    cli.main_menu(_config())

    assert any("Goodbye! Elapsed 12.5s" in line for line in lines)
