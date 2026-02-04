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
    
    # Check for scipy (required for Hungarian algorithm in edition matching)
    try:
        import scipy
    except ImportError:
        print("Error: scipy is required for edition matching (Hungarian algorithm)")
        print("Please install: pip install scipy")
        print("Or activate venv: source venv/bin/activate")
        sys.exit(1)
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
    console.print("[S] Search mode")
    console.print("[Q] Quit")
    console.print()

    choice = Prompt.ask("Choice", default="V").upper()
    if choice == "V":
        asyncio.run(verify_api_keys(config))
        console.print("[cyan][INFO][/cyan] Verification complete. Goodbye!")
    elif choice == "S":
        # Prompt for URL/ID
        search_mode_target = Prompt.ask("Collage or group URL/ID").strip()
        
        # If bare ID, prompt for source tracker
        tracker_key = None
        if not (search_mode_target.startswith("http://") or search_mode_target.startswith("https://")):
            console.print("\nSource tracker (for bare ID):")
            console.print("  [R] RED (default)")
            console.print("  [O] OPS")
            tracker_choice = Prompt.ask("Source tracker", default="R").strip().upper()
            tracker_key = "ops" if tracker_choice == "O" else "red"
        
        # Prompt for output mode
        console.print("\nOutput mode:")
        console.print("  [A] Abbreviated - One line per group")
        console.print("  [N] Normal (default) - Album matching + brief candidate summary")
        console.print("  [V] Verbose - Full edition details, confidence scores")
        console.print("  [D] Debug - API calls, JSON responses, timestamps")
        output_choice = Prompt.ask("Output mode", default="N").strip().upper()
        abbrev = output_choice == "A"
        verbose = output_choice == "V"
        debug = output_choice == "D"
        
        # Prompt for matching mode
        console.print("\nMatching mode:")
        console.print("  [E] Edition-aware (default) - Match at edition/media/encoding level")
        console.print("  [G] Group-only - Stop when group is found")
        matching_choice = Prompt.ask("Matching mode", default="E").strip().upper()
        basic = matching_choice == "G"
        
        # Prompt for fallback mode
        console.print("\nFallback mode:")
        console.print("  [F] Full 5-tier search (default) - Exact + normalization + Discogs")
        console.print("  [D] Disable Discogs (4-tier) - Skip artist name variations")
        console.print("  [X] Exact match only (1-tier) - Fastest, may miss matches")
        fallback_choice = Prompt.ask("Fallback mode", default="F").strip().upper()
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
    --verify          Verify keys and exit (non-interactive)
    -c, --config PATH Path to config.toml (file or directory)
    -o, --output DIR  Output directory for run logs (default: ./output)

Output modes:
    -a, --abbrev      Abbreviated output (one line per group)
    -n, --normal      Normal output mode (default)
    -v, --verbose     Verbose output with full edition details
    -d, --debug       Debug mode with API calls, JSON responses, timestamps

Matching modes:
  --search-editions   Search at edition level (default)
        · Matches editions by year, title, label, catalog (fuzzy matching)
        · Compares individual torrents within matched editions
        · Finds missing encodings (e.g., WEB 24bit FLAC on source but not target)
        · Ignores lossy formats when lossless exists
        → Fewer false positives, requires some review

  --search-groups     Search at group level (ignore editions)
        · No edition or encoding analysis
        → More false positives, requires more review

Fallback modes:
  --no-discogs        Disable Discogs artist name variation (disable tier 5)
        · Faster results
        · Automatically chosen if no Discogs key is available

  --no-fallback       No fallback tiers, exact match only (disable tiers 2-5)
        · Fastest results
        · Will miss groups that are slightly spelled differently between trackers

SEARCH STRATEGY (5-tier default):
    Tier 1: Exact match
    Tier 2: Light normalization (lowercase, HTML unescape)
    Tier 3: Aggressive normalization (strip punctuation, remove stopwords)
    Tier 4: Colon cutoff (truncate at first colon)
    Tier 5: Discogs ANV fallback (artist name variations, requires API key)

EXAMPLES:
    oatgrass https://red.foo/collages.php?id=12345
    oatgrass -v https://red.foo/torrents.php?id=67890
    oatgrass -a https://ops.bar/collages.php?id=999
    oatgrass --verify

CONFIGURATION:
    Requires config.toml with API keys for RED, OPS, and optionally Discogs.
    Searched in: --config PATH, ./config.toml, or repo root.

EXIT CODES:
    0    Success
    1    Failure (missing config, invalid keys, or error)
"""
    print(help_text)


def main():
    """Entry point"""
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument('-h', '--help', action='store_true', help='Show help')
    parser.add_argument('--verify', action='store_true', help='Verify keys and exit')
    parser.add_argument('-c', '--config', metavar='PATH', help='Path to config.toml (file or directory)')
    parser.add_argument('-o', '--output', metavar='DIR', help='Output directory for run logs (default: ./output)')
    parser.add_argument('-a', '--abbrev', action='store_true', help='Abbreviated output for search mode')
    parser.add_argument('-n', '--normal', action='store_true', help='Normal output mode (default)')
    parser.add_argument('-v', '--verbose', action='store_true', help='Verbose output with full edition details')
    parser.add_argument('-d', '--debug', action='store_true', help='Debug mode with API calls, JSON responses, timestamps')
    parser.add_argument('--search-editions', action='store_true', help='Search at edition level (default)')
    parser.add_argument('--search-groups', action='store_true', help='Search at group level (ignore editions)')
    parser.add_argument('--no-discogs', action='store_true', help='Disable Discogs artist name variation (disable tier 5)')
    parser.add_argument('--no-fallback', action='store_true', help='No fallback tiers, exact match only (disable tiers 2-5)')
    parser.add_argument('url_or_id', nargs='?', help='Collage URL, group URL, or group ID')

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
        
        if args.url_or_id:
            # Check for conflicting flags
            if sum([args.abbrev, args.verbose, args.debug]) > 1:
                console.print("[red][ERROR][/red] Cannot use multiple output modes (--abbrev, --verbose, --debug)")
                sys.exit(1)
            if args.search_editions and args.search_groups:
                console.print("[red][ERROR][/red] Cannot use both --search-editions and --search-groups")
                sys.exit(1)
            
            output_dir = Path(args.output).expanduser() if args.output else Path("output")
            
            asyncio.run(
                run_search_mode(
                    config,
                    args.url_or_id,
                    strict=args.no_fallback,
                    abbrev=args.abbrev,
                    verbose=args.verbose,
                    debug=args.debug,
                    basic=args.search_groups,
                    no_discogs=args.no_discogs,
                    output_dir=output_dir,
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
