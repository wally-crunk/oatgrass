#!/usr/bin/env python3
"""
cli.py - Entry point for OATGRASS - Verify API Keys
"Tracker API, Officially Confirm Access"
"""

try:
    import asyncio
    import sys
    import argparse
    from pathlib import Path
    from rich.console import Console
    from rich.prompt import Prompt
    from rich.table import Table
    from typing import Optional
    import oatgrass as pkg
    from .config import OatgrassConfig, load_config
    from .api_verification import verify_api_keys, API_SERVICES
    from .search.search_mode import run_search_mode
except ImportError as e:
    print(f"Error: Missing required dependency: {e}")
    print("Please install required dependencies: pip install -r requirements.txt")
    sys.exit(1)

console = Console()


def redact_api_key(key: str) -> str:
    """Redact API key showing first 2 and last 2 characters"""
    if not key:
        return ""
    if len(key) <= 4:
        return "****"
    return f"{key[:2]}....{key[-2:]}"


def display_config_table(config: OatgrassConfig):
    """Display current API key configuration status"""
    console.print(f"[cyan][INFO][/cyan] ✓ Read configuration file \"{config.config_path}\"... ok!")
    console.print(f"[cyan][INFO][/cyan] To edit configuration, modify config.toml directly.\n")
    console.print()

    table = Table(title="Current API Key Configuration")
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


def main_menu(config: OatgrassConfig):
    """Main menu for Oatgrass API Key Verifier"""
    console.clear()
    from rich.panel import Panel
    console.print(Panel("[bold blue]OATGRASS - Feed the gazelles[/bold blue]\nFind candidates for cross-uploading"))
    console.print()
    display_config_table(config)
    console.print("[V] Verify API Keys")
    console.print("[B] Search mode")
    console.print("[Q] Quit")
    console.print()

    choice = Prompt.ask("Choice", default="V").upper()
    if choice == "V":
        asyncio.run(verify_api_keys(config))
        console.print("[cyan][INFO][/cyan] Verification complete. Goodbye!")
    elif choice == "B":
        search_mode_target = Prompt.ask("Collage or group URL/ID").strip()
        strict_choice = Prompt.ask("Use strict exact-match only? (y/N)", default="N").strip().lower()
        tracker_choice = Prompt.ask("Optional tracker key override (red/ops or enter to auto)", default="").strip().lower()
        strict = strict_choice in ("y", "yes")
        tracker_key = tracker_choice or None
        asyncio.run(run_search_mode(config, search_mode_target, tracker_key=tracker_key, strict=strict))
        console.print("[cyan][INFO][/cyan] Search mode run complete. Goodbye!")
    else:
        console.print("[cyan][INFO][/cyan] Goodbye!")


def show_help():
    """Display help information"""
    help_text = f"""
    OATGRASS - Feed the gazelles v{getattr(pkg, '__version__', '0.0.0')}
    \"Find candidates for cross-uploading\"

USAGE:
    oatgrass [OPTIONS] [URL_OR_ID]
    python -m oatgrass [OPTIONS] [URL_OR_ID]

ARGUMENTS:
    URL_OR_ID         Collage URL or group URL to process

OPTIONS:
    -h, --help        Show this help message and exit
    -v, --verify      Verify keys and exit (non-interactive)
    -a, --abbrev      Abbreviated output (one line per task)
    -c, --config PATH Path to config.toml (file or directory)
    --strict          Use strict exact-match search only (default: 5-tier search)
    --no-discogs      Disable Discogs artist name variation fallback (Tier 5)

SEARCH MODES:
    Regular (default): 5-tier search strategy for best match rate
      - Tier 1: Exact match
      - Tier 2: Light normalization (lowercase, HTML unescape)
      - Tier 3: Aggressive normalization (strip punctuation, remove stopwords)
      - Tier 4: Colon cutoff (truncate at first colon for subtitle handling)
      - Tier 5: Discogs ANV fallback (artist name variations, requires Discogs API key)
    
    Strict (--strict): Exact match only, no normalization

EXAMPLES:
    oatgrass https://red.foo/collages.php?id=12345
    oatgrass -a https://ops.bar/torrents.php?id=67890
    oatgrass --strict https://red.foo/torrents.php?id=1234567

CONFIGURATION:
    Requires config.toml with API keys for RED, OPS, and optionally Discogs.
    Searched in: --config PATH, ./config.toml, or repo root.

EXIT CODES:
    0    Success (all configured keys valid)
    1    Failure (missing config, invalid keys, or error)
"""
    print(help_text)


def main():
    """Entry point"""
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument('-h', '--help', action='store_true', help='Show help')
    parser.add_argument('-v', '--verify', action='store_true', help='Verify keys and exit')
    parser.add_argument('-a', '--abbrev', action='store_true', help='Abbreviated output for search mode')
    parser.add_argument('--strict', action='store_true', help='Use strict exact-match search only')
    parser.add_argument('--no-discogs', action='store_true', help='Disable Discogs artist name variation fallback')
    parser.add_argument('-c', '--config', metavar='PATH', help='Path to config.toml (file or directory)')
    parser.add_argument('target', nargs='?', help='Collage URL, group URL, or group ID')

    try:
        args = parser.parse_args()
        if args.help:
            show_help()
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

            try:
                pkg_dir = Path(__file__).resolve().parent
                repo_root = pkg_dir.parent
                root_candidate = repo_root / "config.toml"
                if root_candidate.exists() and (
                    (repo_root / ".git").exists() or (repo_root / "pyproject.toml").exists()
                ):
                    return root_candidate
            except Exception:
                pass
            return cwd_candidate

        config_path = resolve_config_path(args.config)
        config = load_config(config_path)
        
        if args.target:
            asyncio.run(
                run_search_mode(
                    config,
                    args.target,
                    strict=args.strict,
                    abbrev=args.abbrev,
                    no_discogs=args.no_discogs,
                )
            )
            sys.exit(0)

        if args.verify:
            console.print("[cyan][INFO][/cyan] Verifying API Keys...")
            result = asyncio.run(verify_api_keys(config))
            sys.exit(0 if result else 1)
        else:
            main_menu(config)
            sys.exit(0)
    except KeyboardInterrupt:
        console.print("[cyan][INFO][/cyan] \nGoodbye!")
        sys.exit(0)
    except Exception as e:
        console.print(f"[red][ERROR][/red] Fatal error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
