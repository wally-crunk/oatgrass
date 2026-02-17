#!/usr/bin/env python3
"""
cli.py - Entry point for OATGRASS - Verify API Keys
"Tracker API, Officially Confirm Access"
"""

try:
    import asyncio
    import sys
    import argparse
    import json
    import time
    from dataclasses import asdict
    from pathlib import Path
    from rich.console import Console
    from rich.prompt import Prompt
    from rich.table import Table
    from typing import Literal, Optional, cast
    import oatgrass as pkg
    from .config import OatgrassConfig, TrackerConfig, load_config
    from .api_verification import verify_api_keys, API_SERVICES
    from .rate_limits import GAZELLE_MIN_INTERVAL_SECONDS
    from .profile.menu_service import ProfileMenuService, build_profile_summary, render_profile_summaries
    from .profile.retriever import (
        ListType,
        ProfileTorrent,
        format_list_label,
    )
    from .profile.search_service import run_profile_list_search
    from .profile.session_state import ProfileSessionState
    from .profile.tracker_selection import configured_profile_trackers, resolve_profile_tracker
    from .search.search_mode import run_search_mode, _next_run_path
    from .tracker_profile import resolve_tracker_profile
except ImportError as e:
    print(f"Error: Missing required dependency: {e}")
    print("Please install required dependencies: pip install -r requirements.txt")
    sys.exit(1)

console = Console()
PROFILE_SEARCH_BEST_CASE_CALLS_PER_ROW = 3
PROFILE_SEARCH_BEST_CASE_API_DELAY_SECONDS = GAZELLE_MIN_INTERVAL_SECONDS
_CLI_SESSION_START_MONOTONIC = time.monotonic()
_SCIPY_AVAILABLE: bool | None = None
_SCIPY_HINT_EMITTED = False
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
    console.print(f"[cyan][INFO][/cyan] {message}")


def _ui_warn(message: str) -> None:
    console.print(f"[yellow][WARNING][/yellow] {message}")


def _ui_error(message: str) -> None:
    console.print(f"[red][ERROR][/red] {message}")


def _ui_prompt(label: str, default: str | None = None) -> str:
    if default is None:
        return Prompt.ask(label)
    return Prompt.ask(label, default=default)


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
    return default_yes


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


def _warn_missing_scipy_startup() -> None:
    if _has_scipy():
        return
    _warn_missing_scipy(
        "scipy not found: edition-aware matching is unavailable. "
        "Group-only mode remains available."
    )


def _warn_missing_scipy(message: str) -> None:
    _ui_warn(message)
    _warn_missing_scipy_hint()


def _warn_missing_scipy_hint() -> None:
    global _SCIPY_HINT_EMITTED
    if _SCIPY_HINT_EMITTED:
        return
    _ui_warn("1. Try `source venv/bin/activate` before launching.")
    _SCIPY_HINT_EMITTED = True


def redact_api_key(key: str) -> str:
    """Redact API key showing first 2 and last 2 characters"""
    if not key:
        return ""
    if len(key) <= 4:
        return "****"
    return f"{key[:2]}....{key[-2:]}"


def display_config_table(config: OatgrassConfig):
    """Display current API key configuration status"""
    _ui_info(f"✓ Read configuration file \"{config.config_path}\"... ok!")
    _ui_info("To edit configuration, modify config.toml directly.")

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
        "S": lambda: _run_search_mode_prompt(config),
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
    option_choice: str,
) -> tuple[str, TrackerConfig, list[ListType]] | None:
    tracker_key = _prompt_source_tracker_choice(config, cache.tracker_key)
    tracker_key, tracker = resolve_profile_tracker(config, tracker_key)
    available_list_types = cast(list[ListType], list(resolve_tracker_profile(tracker.name).list_types))
    list_choice = _prompt_profile_list_choice(available_list_types)
    if list_choice is None:
        _ui_prompt("Press Enter to continue", default="")
        return None
    selected_lists = available_list_types if list_choice == "all" else [list_choice]
    if not _ensure_cache_for_followup_action(config, cache, selected_lists, option_choice, tracker_key):
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
    selected = _select_profile_list_action(config, cache, "2")
    if selected is None:
        return

    tracker_key, _tracker, list_types = selected
    group_only_mode = False
    if not _has_scipy():
        _warn_missing_scipy("scipy not found: edition-aware matching is unavailable for option 2/M.")
        if not _ui_prompt_yesno(
            "2. Proceed without edition-aware matching (group-only mode)?",
            default_yes=False,
            allow_cancel=True,
        ):
            _ui_prompt("Press Enter to continue", default="")
            return
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
    if not _ui_prompt_yesno("Continue profile search?", default_yes=True, allow_cancel=True):
        _ui_prompt("Press Enter to continue", default="")
        return

    total_processed = 0
    total_skipped = 0
    all_candidates: list[tuple[str, int]] = []
    for list_type in selected_with_rows:
        entries = cache.get_list(tracker_key, list_type)
        _ui_info(f"Running profile search for '{list_type}' ({len(entries)} row(s))")
        result = asyncio.run(
            run_profile_list_search(
                config=config,
                source_tracker_key=tracker_key,
                list_type=list_type,
                entries=entries,
                group_only=group_only_mode,
            )
        )
        total_processed += result.processed
        total_skipped += result.skipped
        all_candidates.extend(result.candidate_urls)

    deduped_candidates = list(dict.fromkeys(all_candidates))
    _display_profile_search_result(deduped_candidates, total_processed, total_skipped)
    _ui_prompt("Press Enter to continue", default="")


async def _run_profile_summary_menu(config: OatgrassConfig, tracker_key: str | None = None):
    tracker_key, tracker = resolve_profile_tracker(config, tracker_key)
    service = ProfileMenuService(tracker)
    try:
        lists = await service.fetch_all_lists()
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
    option_choice: str,
    tracker_key: str,
) -> bool:
    _, tracker = resolve_profile_tracker(config, tracker_key)

    default_source = "C" if any(cache.has_list(tracker_key, list_type) for list_type in list_types) else "F"
    source = _prompt_profile_source_choice(default_source)
    if source is None:
        return False
    if source == "cached":
        if not any(cache.has_list(tracker_key, list_type) for list_type in list_types):
            joined = ", ".join(list_types)
            _ui_warn(
                f"No cached rows found for selected list(s) [{joined}] on {tracker.name.upper()} for option {option_choice}."
            )
            return False
        return True
    if source == "fetch":
        tracker_key, lists = asyncio.run(_run_profile_summary_menu(config, tracker_key=tracker_key))
        cache.set_snapshot(tracker_key, lists)
    else:  # source == "disk"
        path_raw = _ui_prompt("Profile list JSON path").strip()
        if not path_raw:
            _ui_warn("Path is required.")
            return False
        try:
            loaded = _load_profile_lists_from_disk(
                Path(path_raw).expanduser(),
                tracker_name=tracker.name.upper(),
                allowed_list_types=resolve_tracker_profile(tracker.name).list_types,
            )
        except ValueError as exc:
            _ui_warn(f"Invalid profile list format: {exc}")
            return False
        cache.set_snapshot(tracker_key, loaded)
        _ui_info("Profile lists loaded from disk.")

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
    console.print("  [L] Load from disk")
    choice = _ui_prompt("Source", default=default).strip().lower()
    if choice in {"f", "fetch"}:
        return "fetch"
    if choice in {"c", "cached", "cache"}:
        return "cached"
    if choice in {"l", "load", "disk"}:
        return "disk"
    _ui_warn("Invalid profile source choice.")
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


def _prompt_source_tracker_choice(config: OatgrassConfig, cached_tracker: str | None) -> str:
    trackers = configured_profile_trackers(config)
    if not trackers:
        raise ValueError("No configured tracker with API key found.")
    default_choice = trackers[0][0].upper()
    if cached_tracker:
        for key, _tracker in trackers:
            if key.lower() == cached_tracker.lower():
                default_choice = key.upper()
                break

    console.print("\nSource tracker:")
    for key, tracker in trackers:
        console.print(f"  [{key.upper()}] {tracker.name.upper()} ({tracker.url})")
    selected = _ui_prompt("Source tracker", default=default_choice).strip()
    selected_key, _ = resolve_profile_tracker(config, selected)
    return selected_key


def _run_search_mode_prompt(config: OatgrassConfig) -> None:
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

    search_mode_target = _ui_prompt("Collage or group URL/ID").strip()

    tracker_key = None
    if not (search_mode_target.startswith("http://") or search_mode_target.startswith("https://")):
        tracker_choice = _prompt_menu_choice(
            "Source tracker (for bare ID)",
            "Source tracker",
            [("R", "RED (default)"), ("O", "OPS")],
            default="R",
        )
        tracker_key = "ops" if tracker_choice == "O" else "red"

    output_choice = _prompt_menu_choice(
        "Output mode",
        "Output mode",
        [
            ("A", "Abbreviated - One line per group"),
            ("N", "Normal (default) - Album matching + brief candidate summary"),
            ("V", "Verbose - Full edition details, confidence scores"),
            ("D", "Debug - API calls, JSON responses, timestamps"),
        ],
        default="N",
    )
    abbrev = output_choice == "A"
    verbose = output_choice == "V"
    debug = output_choice == "D"

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
        _warn_missing_scipy("scipy not found: matching mode is fixed to Group-only for this run.")
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

    asyncio.run(run_search_mode(
        config,
        search_mode_target,
        tracker_key=tracker_key,
        strict=no_fallback,
        abbrev=abbrev,
        verbose=verbose,
        debug=debug,
        basic=basic,
        no_discogs=no_discogs,
    ))


def _display_profile_search_result(candidate_urls: list[tuple[str, int]], processed: int, skipped: int) -> None:
    _ui_info(f"Profile search processed={processed}, skipped={skipped}")
    if not candidate_urls:
        _ui_info("No cross-upload candidates found for cached rows.")
        return
    _ui_info("Candidate source torrents to review:")
    for url, priority in sorted(candidate_urls, key=lambda item: item[1], reverse=True):
        console.print(f"  Priority {priority}: {url}")


def _show_profile_search_estimate(
    config: OatgrassConfig,
    source_tracker_key: str,
    list_type: ListType,
    entry_count: int,
) -> None:
    per_row_calls = PROFILE_SEARCH_BEST_CASE_CALLS_PER_ROW
    try:
        from .profile.search_service import _pick_opposite_tracker

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
        per_call_seconds=PROFILE_SEARCH_BEST_CASE_API_DELAY_SECONDS,
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


def show_help(parser: argparse.ArgumentParser) -> None:
    print(f"OATGRASS v{getattr(pkg, '__version__', '0.0.0')} - Find candidates for cross-uploading")
    print()
    parser.print_help()


def main():
    """Entry point"""
    _reset_cli_session_timer()
    parser = argparse.ArgumentParser(add_help=False)
    for args, kwargs in (
        (("-h", "--help"), {"action": "store_true", "help": "Show help"}),
        (("--verify",), {"action": "store_true", "help": "Verify keys and exit"}),
        (("-c", "--config"), {"metavar": "PATH", "help": "Path to config.toml (file or directory)"}),
        (("-o", "--output"), {"metavar": "DIR", "help": "Output directory for run logs (default: ./output)"}),
        (("-a", "--abbrev"), {"action": "store_true", "help": "Abbreviated output for search mode"}),
        (("-n", "--normal"), {"action": "store_true", "help": "Normal output mode (default)"}),
        (("-v", "--verbose"), {"action": "store_true", "help": "Verbose output with full edition details"}),
        (("-d", "--debug"), {"action": "store_true", "help": "Debug mode with API calls, JSON responses, timestamps"}),
        (("--search-editions",), {"action": "store_true", "help": "Search at edition level (default)"}),
        (("--search-groups",), {"action": "store_true", "help": "Search at group level (ignore editions)"}),
        (("--no-discogs",), {"action": "store_true", "help": "Disable Discogs artist name variation (disable tier 5)"}),
        (("--no-fallback",), {"action": "store_true", "help": "No fallback tiers, exact match only (disable tiers 2-5)"}),
    ):
        parser.add_argument(*args, **kwargs)
    parser.add_argument('url_or_id', nargs='?', help='Collage URL, group URL, or group ID')

    try:
        args = parser.parse_args()
        if args.help:
            show_help(parser)
            sys.exit(0)

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
        _warn_missing_scipy_startup()
        
        if args.url_or_id:
            # Check for conflicting flags
            if sum([args.abbrev, args.verbose, args.debug]) > 1:
                _ui_error("Cannot use multiple output modes (--abbrev, --verbose, --debug)")
                sys.exit(1)
            if args.search_editions and args.search_groups:
                _ui_error("Cannot use both --search-editions and --search-groups")
                sys.exit(1)
            
            output_dir = Path(args.output).expanduser() if args.output else Path("output")
            basic_mode = args.search_groups
            if not basic_mode and not _has_scipy():
                _warn_missing_scipy("scipy not found: falling back to --search-groups for this run.")
                basic_mode = True
            
            asyncio.run(
                run_search_mode(
                    config,
                    args.url_or_id,
                    strict=args.no_fallback,
                    abbrev=args.abbrev,
                    verbose=args.verbose,
                    debug=args.debug,
                    basic=basic_mode,
                    no_discogs=args.no_discogs,
                    output_dir=output_dir,
                )
            )
            sys.exit(0)

        if args.verify:
            _ui_info("Verifying API Keys...")
            result = asyncio.run(verify_api_keys(config))
            sys.exit(0 if result else 1)
        else:
            main_menu(config)
            sys.exit(0)
    except KeyboardInterrupt:
        _ui_goodbye_with_elapsed()
        sys.exit(0)
    except Exception as e:
        _ui_error(f"Fatal error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
