#!/usr/bin/env python3
"""Entry point for Oatgrass -- feed the gazelles."""

from __future__ import annotations

import sys

# Dependency-free (stdlib only), so safe to import ahead of the risky
# block below -- lets the version print even if a required package is missing.
from oatgrass.__version__ import __version__

try:
    import asyncio
    import argparse
    import json
    import time
    import traceback
    from dataclasses import asdict
    from datetime import datetime
    from pathlib import Path
    from rich.prompt import Prompt
    from rich.table import Table
    from typing import Literal, Optional, cast
    from oatgrass import logger
    from .config import OatgrassConfig, TrackerConfig, load_config
    from .api_verification import verify_api_keys, API_SERVICES
    from .rate_limits import (
        describe_slow_mode_once,
        GAZELLE_MIN_INTERVAL_SECONDS,
        get_effective_interval,
        set_slow_mode_concurrent_runs,
    )
    from .profile.menu_service import ProfileMenuService, build_profile_summary, render_profile_summaries
    from .profile.retriever import (
        ListType,
        ProfileTorrent,
        format_list_label,
    )
    from .profile.profile_search import run_profile_search_workflow
    from .profile.session_state import ProfileSessionState
    from .profile.tracker_selection import configured_profile_trackers, resolve_profile_tracker
    from .search.group_search import run_group_search_workflow, _next_run_path
    from .search.candidate_policy import CandidatePolicy, PolicySummary, parse_candidate_policy
    from .tracker_profile import resolve_tracker_profile
except ImportError as e:
    print(f"Error: Missing required dependency: {e}")
    print("Please install required dependencies: pip install -r requirements.txt")
    print("Consider using venv and `source .venv/bin/activate`")
    sys.exit(1)

class _SharedConsoleProxy:
    """Forwards attribute access to the current shared logger's Console
    rather than holding a fixed instance of its own -- cli.py previously
    kept its own separate Console() for menu/table rendering, meaning two
    live Console objects existed at once (this one, and logger.py's shared
    one) even after api_verification.py and formatters.py were routed onto
    the shared one. logger.set_logger() can also swap the active logger
    mid-session (when a search workflow starts), so caching a single
    Console reference here would go stale; this always resolves fresh.
    """

    def __getattr__(self, name):
        return getattr(logger.get_logger().console, name)


console = _SharedConsoleProxy()
PROFILE_SEARCH_BEST_CASE_CALLS_PER_ROW = 3
_CLI_SESSION_START_MONOTONIC = time.monotonic()
_SCIPY_AVAILABLE: bool | None = None
_SCIPY_STARTUP_WARNING_EMITTED = False
MAIN_MENU_SECTIONS: tuple[tuple[str, tuple[tuple[str, str], ...]], ...] = (
    (
        "Search for Cross-Upload Candidates",
        (
            ("S", "Search a collage or album"),
        ),
    ),
    (
        "Search using Profile",
        (
            ("1", "Get my profile list of previous torrents"),
            ("2", "Search using a cached profile list"),
        ),
    ),
    (
        "Tools",
        (
            ("V", "Verify API Keys"),
        ),
    ),
    (
        "Oatgrass",
        (
            ("Q", "Quit"),
        ),
    ),
)

def _ui_info(message: str) -> None:
    logger.log(message, "[INFO] ")


def _ui_warn(message: str) -> None:
    logger.warning(message)


def _ui_error(message: str) -> None:
    logger.error(message)


# Shared with group_search.py's edition-processing-failure catch, which needs
# the same "expected failure vs. likely bug" distinction.
_is_internal_defect = logger.is_internal_defect


def _write_crash_report() -> Path | None:
    """Best-effort durable copy of the current exception's full traceback.

    Must be called from inside an except block. A search workflow's own
    finally block already closes its OatgrassLogger (and thus its run log
    file) before an uncaught exception reaches this top-level handler, so
    that file is not available to append to by the time we get here -- this
    writes a small standalone file instead, so the traceback survives past
    the terminal scrollback the screen message points at.
    """
    try:
        output_dir = Path("output")
        output_dir.mkdir(parents=True, exist_ok=True)
        crash_path = output_dir / f"crash-{datetime.now().strftime('%Y%m%d-%H%M%S')}.log"
        crash_path.write_text(traceback.format_exc(), encoding="utf-8")
        return crash_path
    except OSError:
        return None


def _emit_slow_mode_info_once() -> None:
    # Delegates to rate_limits' shared once-flag so the interactive menu and
    # whichever search workflow runs afterward don't each print their own
    # copy of the same session-level notice.
    if (description := describe_slow_mode_once()) is not None:
        _ui_info(description)


def _ui_prompt(label: str, default: str | None = None) -> str:
    if default is None:
        return Prompt.ask(label)
    return Prompt.ask(label, default=default)


def _strip_surrounding_quotes(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def _ui_prompt_yesno(
    label: str,
    *,
    default_yes: bool,
    allow_cancel: bool = False,
) -> bool:
    suffix = ("[Y/n" if default_yes else "[y/N") + (", c=cancel]" if allow_cancel else "]")

    choice = _ui_prompt(f"{label} {suffix}", default="Y" if default_yes else "N").strip().lower()
    if not choice:
        return default_yes
    first = choice[0]
    if first == "y":
        return True
    if first == "n":
        return False
    if allow_cancel and first in {"c", "x"}:
        return False
    _ui_warn(f"'{choice}' not recognized; using default ({'yes' if default_yes else 'no'}).")
    return default_yes


def _resolve_candidate_policy_from_flags(args: argparse.Namespace) -> CandidatePolicy:
    policy = parse_candidate_policy(
        perfect=bool(getattr(args, "perfect", False)),
        perfecter=bool(getattr(args, "perfecter", False)),
    )
    if bool(getattr(args, "perfect", False)) and bool(getattr(args, "perfecter", False)):
        _ui_warn("Both --perfect and --perfecter were provided; using --perfecter.")
    return policy


def _reset_cli_session_timer() -> None:
    global _CLI_SESSION_START_MONOTONIC
    _CLI_SESSION_START_MONOTONIC = time.monotonic()


def _format_elapsed_runtime(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.1f}s"
    if seconds < 3_600:
        return f"{seconds / 60:.1f}m"
    if seconds < 86_400:
        return f"{seconds / 3_600:.1f}h"
    return f"{seconds / 86_400:.1f}d"


def _ui_goodbye_with_elapsed() -> None:
    elapsed = max(0.0, time.monotonic() - _CLI_SESSION_START_MONOTONIC)
    _ui_info(f"Goodbye! Elapsed {_format_elapsed_runtime(elapsed)}")


def _has_scipy() -> bool:
    global _SCIPY_AVAILABLE
    if _SCIPY_AVAILABLE is not None:
        return _SCIPY_AVAILABLE
    try:
        import scipy  # noqa: F401
    except Exception:
        _SCIPY_AVAILABLE = False
    else:
        _SCIPY_AVAILABLE = True
    return _SCIPY_AVAILABLE


def _emit_scipy_startup_warning_once() -> None:
    global _SCIPY_STARTUP_WARNING_EMITTED
    if _SCIPY_STARTUP_WARNING_EMITTED or _has_scipy():
        return
    _ui_warn("scipy not found: edition comparisons are disabled.")
    _ui_warn("Try `source venv/bin/activate` before launching oatgrass.")
    _SCIPY_STARTUP_WARNING_EMITTED = True


def redact_api_key(key: str) -> str:
    """Redact API key showing first 2 and last 2 characters"""
    if not key:
        return ""
    if len(key) <= 4:
        return "****"
    return f"{key[:2]}....{key[-2:]}"


def display_config_table(config: OatgrassConfig):
    """Display current API key configuration status"""
    _ui_info(f'Config loaded: "{config.config_path}"')

    table = Table(title="API configuration")
    table.add_column("Service", style="cyan")
    table.add_column("Status", style="green")
    api_keys = config.api_keys.model_dump()
    for service, key in api_keys.items():
        if key:
            status = f"✓ Configured = {redact_api_key(key)}"
        else:
            status = "✗ Not set"
        display_name = API_SERVICES.get(service, (None, service.replace("_", " ").title()))[1]
        table.add_row(display_name, status)
    for tracker_name, tracker in config.trackers.items():
        if tracker.api_key:
            status = f"✓ Configured = {redact_api_key(tracker.api_key)}"
        else:
            status = "✗ Not set"
        table.add_row(f"{tracker_name.upper()} Tracker", status)
    console.print(table)
    console.print()
    console.print()


def main_menu(config: OatgrassConfig):
    """Main menu for Oatgrass API Key Verifier"""
    cache = ProfileSessionState()

    while True:
        _render_main_menu(config)
        choice = Prompt.ask("Choice", default="V").upper()
        should_continue = _handle_main_menu_choice(config, cache, choice)
        if not should_continue:
            return


def _render_main_menu(config: OatgrassConfig) -> None:
    console.clear()
    from rich.panel import Panel

    console.print(Panel("[bold blue]OATGRASS - Feed the gazelles[/bold blue]\nFind candidates for cross-uploading"))
    console.print()
    _emit_slow_mode_info_once()
    display_config_table(config)
    for section_idx, (section_title, items) in enumerate(MAIN_MENU_SECTIONS):
        console.print(section_title)
        for key, label in items:
            console.print(f"    [{key}] {label}")
        if section_idx < len(MAIN_MENU_SECTIONS) - 1:
            console.print()
    console.print()


def _handle_main_menu_choice(config: OatgrassConfig, cache: ProfileSessionState, choice: str) -> bool:
    choice = {"G": "1", "M": "2"}.get(choice, choice)
    if choice == "Q":
        _ui_goodbye_with_elapsed()
        return False

    handlers = {
        "1": lambda: _handle_profile_summary_action(config, cache),
        "2": lambda: _handle_profile_search_action(config, cache),
        "V": lambda: asyncio.run(verify_api_keys(config)),
        "S": lambda: _run_group_search_prompt(config),
    }
    handler = handlers.get(choice)
    if handler is None:
        _ui_warn("Unknown choice. Please select a listed option.")
        _ui_prompt("Press Enter to continue", default="")
        return True
    handler()
    if choice == "V":
        _ui_info("Verification complete.")
        _ui_prompt("Press Enter to continue", default="")
    elif choice == "S":
        _ui_info("Search mode run complete.")
        _ui_prompt("Press Enter to continue", default="")
    return True


ProfileListSelection = ListType | Literal["all"]


def _select_profile_list_action(
    config: OatgrassConfig,
    cache: ProfileSessionState,
) -> tuple[str, TrackerConfig, list[ListType]] | None:
    tracker_choice = _prompt_source_tracker_choice(config, cache.tracker_key, allow_load_from_disk=True)
    selected = (
        _load_profile_lists_into_cache_from_disk(config, cache)
        if tracker_choice == "disk"
        else resolve_profile_tracker(config, tracker_choice)
    )
    if selected is None:
        _ui_prompt("Press Enter to continue", default="")
        return None
    tracker_key, tracker = selected

    available_list_types = cast(list[ListType], list(resolve_tracker_profile(tracker.name).list_types))
    if tracker_choice == "disk":
        available_list_types = [list_type for list_type in available_list_types if cache.has_list(tracker_key, list_type)]
        if not available_list_types:
            _ui_warn("No non-empty lists were found in the loaded snapshot.")
            _ui_prompt("Press Enter to continue", default="")
            return None

    list_choice = _prompt_profile_list_choice(available_list_types)
    if list_choice is None:
        _ui_prompt("Press Enter to continue", default="")
        return None
    selected_lists = available_list_types if list_choice == "all" else [list_choice]
    if tracker_choice != "disk" and not _ensure_cache_for_followup_action(config, cache, selected_lists, tracker_key):
        _ui_prompt("Press Enter to continue", default="")
        return None
    return tracker_key, tracker, selected_lists


def _handle_profile_summary_action(config: OatgrassConfig, cache: ProfileSessionState) -> None:
    try:
        tracker_key = _prompt_source_tracker_choice(config, cache.tracker_key)
        tracker_key, lists = asyncio.run(_run_profile_summary_menu(config, tracker_key=tracker_key))
        cache.set_snapshot(tracker_key, lists)
    except Exception as exc:
        _ui_error(f"Profile summary failed: {exc}")
    else:
        _ui_info("Profile summary complete.")
    _ui_prompt("Press Enter to continue", default="")


def _handle_profile_search_action(config: OatgrassConfig, cache: ProfileSessionState) -> None:
    selected = _select_profile_list_action(config, cache)
    if selected is None:
        return

    tracker_key, _tracker, list_types = selected
    group_only_mode = False
    if not _has_scipy():
        group_only_mode = True

    selected_with_rows = [list_type for list_type in list_types if cache.has_list(tracker_key, list_type)]
    if not selected_with_rows:
        _ui_warn("Selected profile list(s) have no cached rows.")
        _ui_prompt("Press Enter to continue", default="")
        return

    if len(selected_with_rows) > 1:
        _ui_info(f"Selected lists: {', '.join(selected_with_rows)}")
    total_rows = sum(len(cache.get_list(tracker_key, list_type)) for list_type in selected_with_rows)
    _show_profile_search_estimate(config, tracker_key, selected_with_rows[0], total_rows)
    console.print("\nCandidate policy:")
    console.print("  [A] All (default) - current behavior")
    console.print("  [P] Perfect - FLAC-only, quality scoring")
    console.print("  [R] Perfecter - stricter media/encoding policy")
    policy_choice = _ui_prompt("Candidate policy", default="A").strip().upper()
    candidate_policy = {
        "A": CandidatePolicy.STANDARD,
        "P": CandidatePolicy.PERFECT,
        "R": CandidatePolicy.PERFECTER,
    }.get(policy_choice, CandidatePolicy.STANDARD)
    if not _ui_prompt_yesno("Continue profile search?", default_yes=True, allow_cancel=True):
        _ui_prompt("Press Enter to continue", default="")
        return

    total_processed = 0
    total_skipped = 0
    all_candidates: list[tuple[str, int]] = []
    aggregate_policy_summary = PolicySummary()
    for list_type in selected_with_rows:
        entries = cache.get_list(tracker_key, list_type)
        _ui_info(f"Running profile search for '{list_type}' ({len(entries)} row(s))")
        result = asyncio.run(
            run_profile_search_workflow(
                config=config,
                source_tracker_key=tracker_key,
                list_type=list_type,
                entries=entries,
                group_only=group_only_mode,
                candidate_policy=candidate_policy,
            )
        )
        total_processed += result.processed
        total_skipped += result.skipped
        all_candidates.extend(result.candidate_urls)
        aggregate_policy_summary.merge(getattr(result, "policy_summary", PolicySummary()))

    deduped_candidates = list(dict.fromkeys(all_candidates))
    _display_profile_search_result(
        deduped_candidates,
        total_processed,
        total_skipped,
        aggregate_policy_summary if candidate_policy != CandidatePolicy.STANDARD else None,
    )
    _ui_prompt("Press Enter to continue", default="")


async def _run_profile_summary_menu(
    config: OatgrassConfig,
    tracker_key: str | None = None,
    list_types: list[ListType] | None = None,
):
    tracker_key, tracker = resolve_profile_tracker(config, tracker_key)
    service = ProfileMenuService(tracker)
    try:
        lists = await service.fetch_all_lists(list_types)
        summaries = [build_profile_summary(list_type, entries) for list_type, entries in lists.items()]
        render_profile_summaries(console, tracker.name.upper(), summaries)
        saved_path = _persist_profile_lists(lists, tracker.name.upper())
        _ui_info(f"Profile lists persisted to {saved_path}")
        return tracker_key, lists
    finally:
        await service.close()


def _serialize_profile_entries(entries: list[ProfileTorrent]) -> list[dict]:
    serialized: list[dict] = []
    for entry in entries:
        data = asdict(entry)
        metadata = data.get("metadata")
        data["metadata"] = dict(metadata or {})
        serialized.append(data)
    return serialized


def _persist_profile_lists(
    lists: dict[ListType, list[ProfileTorrent]],
    tracker_name: str,
    output_dir: Path | None = None,
) -> Path:
    run_path = _next_run_path(output_dir or Path("output"))
    json_path = run_path.with_suffix(".profile-lists.json")
    payload = {
        "tracker": tracker_name,
        "lists": {
            list_type: _serialize_profile_entries(entries)
            for list_type, entries in lists.items()
        },
    }
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(payload, indent=2))
    return json_path


def _prompt_profile_list_choice(
    available_lists: list[ListType],
) -> ProfileListSelection | None:
    if not available_lists:
        raise ValueError("No available profile lists to choose from.")

    console.print("\nAvailable profile lists:")
    for idx, list_type in enumerate(available_lists, start=1):
        label = format_list_label(list_type)
        console.print(f"  [{idx}] {label} ({list_type})")
    console.print("  [A] All lists")

    choice = _ui_prompt("List").strip().lower()
    if choice.isdigit() and 1 <= int(choice) <= len(available_lists):
        return available_lists[int(choice) - 1]
    if choice in {"a", "all"}:
        return "all"

    aliases: dict[str, ListType] = {}
    for list_type in available_lists:
        label = format_list_label(list_type).lower()
        aliases[list_type.lower()] = list_type
        aliases[label] = list_type
        aliases.setdefault(list_type[0].lower(), list_type)
        aliases.setdefault(label[0], list_type)
    if choice in aliases:
        return aliases[choice]
    _ui_warn("Invalid profile list choice.")
    return None


def _ensure_cache_for_followup_action(
    config: OatgrassConfig,
    cache: ProfileSessionState,
    list_types: list[ListType],
    tracker_key: str,
) -> bool:
    default_source = "C" if any(cache.has_list(tracker_key, list_type) for list_type in list_types) else "F"
    source = _prompt_profile_source_choice(default_source)
    if source is None:
        return False
    if source == "cached":
        if not any(cache.has_list(tracker_key, list_type) for list_type in list_types):
            joined = ", ".join(list_types)
            _ui_warn(
                f"No cached rows found for selected list(s) [{joined}] on {tracker_key.upper()} for option 2/M."
            )
            return False
        return True
    if source == "fetch":
        tracker_key, lists = asyncio.run(
            _run_profile_summary_menu(config, tracker_key=tracker_key, list_types=list_types)
        )
        cache.set_snapshot(tracker_key, lists)

    available = [list_type for list_type in list_types if cache.has_list(tracker_key, list_type)]
    if not available:
        joined = ", ".join(list_types)
        _ui_warn(f"Selected list(s) [{joined}] are empty after source selection.")
        return False
    missing = [list_type for list_type in list_types if list_type not in available]
    if missing:
        _ui_warn(f"Some selected lists are empty and will be skipped: {', '.join(missing)}")
    return True


def _prompt_profile_source_choice(default: str) -> str | None:
    console.print("\nProfile source:")
    console.print("  [F] Fetch now")
    console.print("  [C] Use cached")
    choice = _ui_prompt("Source", default=default).strip().lower()
    if choice not in {"f", "fetch", "c", "cached", "cache"}:
        _ui_warn("Invalid profile source choice.")
        return None
    return "fetch" if choice in {"f", "fetch"} else "cached"


def _load_profile_lists_into_cache_from_disk(
    config: OatgrassConfig,
    cache: ProfileSessionState,
) -> tuple[str, TrackerConfig] | None:
    path_raw = _ui_prompt("Profile list JSON path").strip()
    if not path_raw:
        _ui_warn("Path is required.")
        return None

    snapshot_path = Path(path_raw).expanduser()
    for tracker_key, tracker in configured_profile_trackers(config):
        try:
            loaded = _load_profile_lists_from_disk(
                snapshot_path,
                tracker_name=tracker.name.upper(),
                allowed_list_types=resolve_tracker_profile(tracker.name).list_types,
            )
        except ValueError as exc:
            if str(exc).startswith("Snapshot tracker must match"):
                continue
            _ui_warn(f"Invalid profile list format: {exc}")
            return None
        cache.set_snapshot(tracker_key, loaded)
        _ui_info(f"Profile lists loaded from disk for {tracker.name.upper()}.")
        return tracker_key, tracker
    _ui_warn("Invalid profile list format: Snapshot tracker is not configured with an API key.")
    return None


def _load_profile_lists_from_disk(
    path: Path,
    *,
    tracker_name: str,
    allowed_list_types: tuple[str, ...],
) -> dict[ListType, list[ProfileTorrent]]:
    if not path.exists() or not path.is_file():
        raise ValueError(f"File not found: {path}")
    try:
        payload = json.loads(path.read_text())
    except Exception as exc:
        raise ValueError(f"Failed to read JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("Root must be an object")
    payload_tracker = payload.get("tracker")
    if not isinstance(payload_tracker, str) or payload_tracker.upper() != tracker_name.upper():
        raise ValueError(f"Snapshot tracker must match '{tracker_name}'")
    raw_lists = payload.get("lists")
    if not isinstance(raw_lists, dict):
        raise ValueError("Missing 'lists' object")

    parsed: dict[ListType, list[ProfileTorrent]] = {}
    allowed = set(allowed_list_types)
    for list_name, rows in raw_lists.items():
        if list_name not in allowed:
            raise ValueError(f"Unknown list type '{list_name}'")
        if not isinstance(rows, list):
            raise ValueError(f"List '{list_name}' must be an array")
        parsed_rows: list[ProfileTorrent] = []
        for idx, row in enumerate(rows):
            if not isinstance(row, dict):
                raise ValueError(f"List '{list_name}' entry {idx} must be an object")
            try:
                entry = ProfileTorrent(**row)
            except TypeError as exc:
                raise ValueError(f"List '{list_name}' entry {idx} has unexpected schema") from exc
            if entry.tracker.upper() != tracker_name.upper():
                raise ValueError(f"List '{list_name}' entry {idx} tracker mismatch")
            if entry.list_type != list_name:
                raise ValueError(f"List '{list_name}' entry {idx} list_type mismatch")
            if not isinstance(entry.metadata, dict):
                raise ValueError(f"List '{list_name}' entry {idx} metadata must be an object")
            if entry.group_id is None and entry.torrent_id is None:
                raise ValueError(f"List '{list_name}' entry {idx} missing both group_id and torrent_id")
            parsed_rows.append(entry)
        parsed[cast(ListType, list_name)] = parsed_rows

    for list_name in allowed_list_types:
        parsed.setdefault(cast(ListType, list_name), [])
    return parsed


def _prompt_source_tracker_choice(
    config: OatgrassConfig,
    cached_tracker: str | None,
    *,
    allow_load_from_disk: bool = False,
) -> str:
    trackers = configured_profile_trackers(config)
    if not trackers:
        raise ValueError("No configured tracker with API key found.")
    default_choice = trackers[0][0].upper()
    if cached_tracker:
        default_choice = next(
            (key.upper() for key, _ in trackers if key.lower() == cached_tracker.lower()),
            default_choice,
        )

    console.print("\nSource tracker:")
    for key, tracker in trackers:
        console.print(f"  [{key.upper()}] {tracker.name.upper()} ({tracker.url})")
    if allow_load_from_disk:
        console.print("  [L] Load from file")
    selected = _ui_prompt("Source tracker", default=default_choice).strip()
    if allow_load_from_disk and selected.lower() in {"l", "load"}:
        return "disk"
    return resolve_profile_tracker(config, selected)[0]


def _run_group_search_prompt(config: OatgrassConfig) -> None:
    def _prompt_menu_choice(
        title: str,
        prompt_label: str,
        options: list[tuple[str, str]],
        *,
        default: str,
    ) -> str:
        console.print(f"\n{title}:")
        for key, label in options:
            console.print(f"  [{key}] {label}")
        return _ui_prompt(prompt_label, default=default).strip().upper()

    search_mode_target = _strip_surrounding_quotes(_ui_prompt("Enter Group/Collage URL or ID").strip())

    tracker_key: str | None = None
    if not (search_mode_target.startswith("http://") or search_mode_target.startswith("https://")):
        if not search_mode_target.isdigit() or int(search_mode_target) <= 0:
            _ui_warn("Bare ID must be a positive integer.")
            return

        id_type = _prompt_menu_choice(
            "ID type (for bare ID)",
            "ID type",
            [
                ("G", "Group ID (default)"),
                ("C", "Collage ID"),
            ],
            default="G",
        )
        if id_type not in {"G", "C"}:
            _ui_warn(f"Unknown ID type '{id_type}'. Defaulting to G.")
            id_type = "G"

        tracker_lookup = {key.upper(): key for key in config.trackers}
        default_tracker = max(tracker_lookup)
        tracker_choice = _prompt_menu_choice(
            "Tracker (for bare ID)",
            "Tracker",
            [
                (upper, f"{config.trackers[key].name.upper()} ({config.trackers[key].url})")
                for upper, key in tracker_lookup.items()
            ],
            default=default_tracker,
        )
        selected_tracker_key = tracker_lookup.get(tracker_choice)
        if selected_tracker_key is None:
            _ui_warn(f"Unknown tracker '{tracker_choice}'. Defaulting to {default_tracker}.")
            selected_tracker_key = tracker_lookup[default_tracker]

        if id_type == "C":
            collage_tracker = config.trackers[selected_tracker_key]
            search_mode_target = f"{collage_tracker.url.rstrip('/')}/collages.php?id={search_mode_target}"
        else:
            tracker_key = selected_tracker_key

    output_choice = _prompt_menu_choice(
        "Output mode",
        "Output mode",
        [
            ("N", "Normal (default) - Full edition details, confidence scores"),
            ("C", "Compact - One line per group"),
            ("D", "Debug - API calls, JSON responses, timestamps"),
        ],
        default="N",
    )
    abbrev = output_choice == "C"
    debug = output_choice == "D"

    policy_choice = _prompt_menu_choice(
        "Candidate policy",
        "Candidate policy",
        [
            ("A", "All (default) - current behavior"),
            ("P", "Perfect - FLAC-only, quality scoring"),
            ("R", "Perfecter - stricter media/encoding policy"),
        ],
        default="A",
    )
    candidate_policy = {
        "A": CandidatePolicy.STANDARD,
        "P": CandidatePolicy.PERFECT,
        "R": CandidatePolicy.PERFECTER,
    }.get(policy_choice, CandidatePolicy.STANDARD)

    if _has_scipy():
        matching_choice = _prompt_menu_choice(
            "Matching mode",
            "Matching mode",
            [
                ("E", "Edition-aware (default) - Match at edition/media/encoding level"),
                ("G", "Group-only - Stop when group is found"),
            ],
            default="E",
        )
        basic = matching_choice == "G"
    else:
        basic = True

    fallback_choice = _prompt_menu_choice(
        "Fallback mode",
        "Fallback mode",
        [
            ("F", "Full 5-tier search (default) - Exact + normalization + Discogs"),
            ("D", "Disable Discogs (4-tier) - Skip artist name variations"),
            ("X", "Exact match only (1-tier) - Fastest, may miss matches"),
        ],
        default="F",
    )
    no_fallback = fallback_choice == "X"
    no_discogs = fallback_choice == "D" or (fallback_choice == "F" and not config.api_keys.discogs_key)

    asyncio.run(run_group_search_workflow(
        config,
        search_mode_target,
        tracker_key=tracker_key,
        strict=no_fallback,
        abbrev=abbrev,
        debug=debug,
        basic=basic,
        no_discogs=no_discogs,
        candidate_policy=candidate_policy,
    ))


def _display_profile_search_result(
    candidate_urls: list[tuple[str, int]],
    processed: int,
    skipped: int,
    policy_summary: PolicySummary | None = None,
) -> None:
    _ui_info(f"Profile search processed={processed}, skipped={skipped}")
    if not candidate_urls:
        _ui_info("No cross-upload candidates found for cached rows.")
    else:
        _ui_info("Candidate source torrents to review:")
        for url, priority in sorted(candidate_urls, key=lambda item: item[1], reverse=True):
            console.print(f"  Priority {priority}: {url}")
    if policy_summary is not None:
        _ui_info(
            "Policy summary: "
            f"promoted={policy_summary.promoted}, "
            f"demoted={policy_summary.demoted}, "
            f"excluded_by_policy={policy_summary.excluded_by_policy}, "
            f"duplicate_24bit={policy_summary.duplicate_24bit}, "
            f"suppressed_total={policy_summary.suppressed_total}"
        )


def _show_profile_search_estimate(
    config: OatgrassConfig,
    source_tracker_key: str,
    list_type: ListType,
    entry_count: int,
) -> None:
    per_row_calls = PROFILE_SEARCH_BEST_CASE_CALLS_PER_ROW
    try:
        from .profile.profile_search import _pick_opposite_tracker

        source_key, _ = resolve_profile_tracker(config, source_tracker_key)
        _, target_tracker = _pick_opposite_tracker(config.trackers, source_key)
        # RED target requires one additional group-detail call in the current flow.
        if target_tracker.name.lower() == "red":
            per_row_calls += 1
    except Exception:
        pass

    _show_duration_estimate(
        entry_count=entry_count,
        per_row_calls=per_row_calls,
        per_call_seconds=get_effective_interval(GAZELLE_MIN_INTERVAL_SECONDS),
    )


def _show_duration_estimate(*, entry_count: int, per_row_calls: int, per_call_seconds: float) -> None:
    best_case_seconds = entry_count * per_row_calls * per_call_seconds
    if best_case_seconds < 60:
        return
    per_row_seconds = per_row_calls * per_call_seconds
    duration_value, duration_unit = _largest_duration_unit(best_case_seconds)
    _ui_info("Estimated time required:")
    _ui_info(f"     {entry_count:,} rows,  about {_format_seconds_value(per_row_seconds)} seconds each")
    _ui_info(f"     = {duration_value:.1f} {duration_unit}")


def _largest_duration_unit(total_seconds: float) -> tuple[float, str]:
    if total_seconds >= 86_400:
        return total_seconds / 86_400, "days"
    if total_seconds >= 3_600:
        return total_seconds / 3_600, "hours"
    return total_seconds / 60, "minutes"


def _format_seconds_value(seconds: float) -> str:
    if float(seconds).is_integer():
        return f"{seconds:.0f}"
    return f"{seconds:.1f}"


def _help_header() -> str:
    return f"OATGRASS v{__version__} - Find candidates for cross-uploading"


def show_help(parser: argparse.ArgumentParser) -> None:
    print(_help_header())
    print()
    parser.print_help()


def _resolve_cli_output_modes(args: argparse.Namespace) -> tuple[bool, bool]:
    """Resolve output flags to (abbrev, debug).

    Oatgrass has three real output levels, one flag each: no flag = normal
    (default, full per-entry detail), -q/--quiet = compact (one line per
    entry), -d/--debug = debug. No synonyms, no legacy aliases -- the
    previous four-level model (plus -qq/--quieter, -a/--abbrev, -n/--normal,
    -v/--verbose as deprecated no-op aliases) has been fully removed.

    There is no third "verbose" return value: normal output always shows
    full per-entry detail, so "verbose" was never anything but a synonym for
    "not abbrev" once the four-level model collapsed to three -- carrying it
    as its own parameter through every downstream function was dead weight,
    so it has been removed from the whole call chain, not just from here.
    """
    debug = bool(getattr(args, "debug", False))
    compact = bool(getattr(args, "quiet", False))

    if debug and compact:
        raise ValueError("Cannot combine output modes; choose one of --debug or -q/--quiet")
    return compact, debug


def main():
    """Entry point"""
    _reset_cli_session_timer()
    parser = argparse.ArgumentParser(
        prog="oatgrass",
        usage=(
            "oatgrass [-h] [--verify] [-c PATH] [-o DIR] [--slow [N]] "
            "[--version] "
            "[--search-editions|--search-groups] [--no-discogs] [--no-fallback] [--perfect|--perfecter] "
            "[--debug | -q] [url_or_id]"
        ),
        add_help=False,
        formatter_class=lambda prog: argparse.HelpFormatter(prog, max_help_position=25),
    )
    general = parser.add_argument_group("general options")
    general.add_argument("-h", "--help", action="store_true", help="Show help")
    general.add_argument("--version", action="store_true", help="Show version and exit")
    general.add_argument("--verify", action="store_true", help="Verify keys and exit")
    general.add_argument("-c", "--config", metavar="PATH", help="Path to config.toml (file or directory)")
    general.add_argument("-o", "--output", metavar="DIR", help="Output directory for run logs (default: ./output)")
    general.add_argument(
        "--slow",
        nargs="?",
        type=int,
        const=2,
        metavar="N",
        help="Slow API pacing for N concurrent oatgrass runs (bare --slow implies N=2)",
    )

    search_behavior = parser.add_argument_group("search behavior")
    search_behavior.add_argument("--search-editions", action="store_true", help="Search at edition level (default)")
    search_behavior.add_argument("--search-groups", action="store_true", help="Search at group level (ignore editions)")
    search_behavior.add_argument("--no-discogs", action="store_true", help="Disable Discogs artist name variation (disable tier 5)")
    search_behavior.add_argument("--no-fallback", action="store_true", help="No fallback tiers, exact match only (disable tiers 2-5)")
    search_behavior.add_argument(
        "--perfect",
        action="store_true",
        help="Perfect policy: FLAC-only quality scoring (+20/-20)",
    )
    search_behavior.add_argument(
        "--perfecter",
        action="store_true",
        help="Perfecter policy: stricter media/encoding filtering plus quality scoring",
    )

    output_mode = parser.add_argument_group("output mode (choose one; default is normal)")
    output_mode.add_argument("-q", "--quiet", action="store_true", help="Compact output: one line per entry")
    output_mode.add_argument("-d", "--debug", action="store_true", help="Debug output: API calls, JSON responses, timestamps")
    parser.add_argument('url_or_id', nargs='?', help='Collage URL, group URL, or group ID')

    try:
        set_slow_mode_concurrent_runs(None)
        args = parser.parse_args()
        if args.help:
            show_help(parser)
            sys.exit(0)
        if args.version:
            print(_help_header())
            sys.exit(0)
        if args.slow is not None and args.slow < 2:
            _ui_error("--slow requires an integer >= 2")
            sys.exit(1)
        set_slow_mode_concurrent_runs(args.slow)
        print(f"Welcome to Oatgrass {__version__}")

        def resolve_config_path(args_config: Optional[str]) -> Path:
            if args_config:
                p = Path(args_config).expanduser()
                if p.is_dir():
                    p = p / "config.toml"
                return p

            cwd_candidate = Path.cwd() / "config.toml"
            if cwd_candidate.exists():
                return cwd_candidate

            repo_root = Path(__file__).resolve().parent.parent
            root_candidate = repo_root / "config.toml"
            if root_candidate.exists() and (
                (repo_root / ".git").exists() or (repo_root / "pyproject.toml").exists()
            ):
                return root_candidate
            return cwd_candidate

        config_path = resolve_config_path(args.config)
        config = load_config(config_path)
        _emit_scipy_startup_warning_once()
        
        if args.url_or_id:
            try:
                abbrev, debug = _resolve_cli_output_modes(args)
            except ValueError as exc:
                _ui_error(str(exc))
                sys.exit(1)
            if args.search_editions and args.search_groups:
                _ui_error("Cannot use both --search-editions and --search-groups")
                sys.exit(1)
            
            output_dir = Path(args.output).expanduser() if args.output else Path("output")
            basic_mode = args.search_groups
            if not basic_mode and not _has_scipy():
                basic_mode = True
            candidate_policy = _resolve_candidate_policy_from_flags(args)
            
            asyncio.run(
                run_group_search_workflow(
                    config,
                    args.url_or_id,
                    strict=args.no_fallback,
                    abbrev=abbrev,
                    debug=debug,
                    basic=basic_mode,
                    no_discogs=args.no_discogs,
                    candidate_policy=candidate_policy,
                    output_dir=output_dir,
                )
            )
            sys.exit(0)

        if args.verify:
            _emit_slow_mode_info_once()
            # verify_api_keys() prints its own "Verifying API Keys..." banner;
            # don't duplicate it here.
            result = asyncio.run(verify_api_keys(config))
            sys.exit(0 if result else 1)
        else:
            main_menu(config)
            sys.exit(0)
    except KeyboardInterrupt:
        _ui_goodbye_with_elapsed()
        sys.exit(0)
    except Exception as e:
        if _is_internal_defect(e):
            traceback.print_exc(file=sys.stderr)
            # Also append to the current run's log file if one is still open
            # (an exception raised outside a workflow's own try/finally, e.g.
            # during menu navigation, may not have closed it yet); otherwise
            # fall back to a standalone crash file, since the common case --
            # a defect inside run_group_search_workflow -- already closed the
            # run log via its own finally block before we got here.
            file_handle = getattr(logger.get_logger(), "_file_handle", None)
            if file_handle is not None:
                try:
                    file_handle.write(traceback.format_exc() + "\n")
                    file_handle.flush()
                except OSError:
                    pass
                crash_path = None
            else:
                crash_path = _write_crash_report()
            location = f" Full traceback saved to {crash_path}." if crash_path else ""
            _ui_error(
                f"Unexpected internal error ({type(e).__name__}: {e}). "
                "This looks like a bug, not a normal failure." + location
            )
        else:
            _ui_error(f"Fatal error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
