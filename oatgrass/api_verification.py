"""
api_verification.py - API key verification service for Oatgrass
"""

import aiohttp
import asyncio
from rich.table import Table
from rich.markup import escape
from .config import OatgrassConfig
from .rate_limits import enforce_gazelle_min_interval
from .tracker_auth import build_tracker_auth_header
from rich.console import Console
from . import __version__

UA = f"Oatgrass/{__version__}"

console = Console()


def _invalid_key_msg(detail: str) -> str:
    """Generate standardized invalid API key message"""
    return f"Invalid API key - {detail}"


async def verify_discogs(session, api_key: str, timeout=10):
    """Verify Discogs API key and get username"""
    headers = {
        'Authorization': f'Discogs token={api_key}',
        'User-Agent': UA,
    }
    api_url = "https://api.discogs.com/oauth/identity"
    
    async with session.get(
        api_url,
        headers=headers,
        timeout=timeout
    ) as response:
        if response.status != 200:
            return "Discogs", False, _invalid_key_msg(f"{response.status} {response.reason}")
        
        data = await response.json()
        if 'username' in data and 'id' in data:
            return "Discogs", True, f"Hello {data['username']} (ID: {data['id']})"
        return "Discogs", False, _invalid_key_msg("no user details found")


async def verify_gazelle_tracker(session, api_key: str, url: str, name: str, timeout=10):
    """Verify Gazelle tracker (RED/OPS) API key and get username"""
    headers = {
        'Authorization': build_tracker_auth_header(name, api_key),
        'User-Agent': UA,
    }
    api_url = f"{url}/ajax.php?action=index"
    await enforce_gazelle_min_interval(url, tracker_name=name)
    
    async with session.get(
        api_url,
        headers=headers,
        timeout=timeout
    ) as response:
        if response.status != 200:
            return name, False, _invalid_key_msg(f"{response.status} {response.reason}")
        
        data = await response.json()
        if 'response' in data:
            resp = data['response']
            if 'username' in resp and 'id' in resp:
                return name, True, f"Hello {resp['username']} (ID: {resp['id']})"
        return name, False, _invalid_key_msg("no user details found")


# Service lookup table: key_name -> (verify_function, display_name)
API_SERVICES = {
    'discogs_key': (verify_discogs, 'Discogs'),
}

async def verify_with_retry(verify_func, service_name, *args, max_retries=2, timeout=10):
    """Wrapper to add retry logic with exponential backoff"""
    for attempt in range(max_retries + 1):
        try:
            return await verify_func(*args, timeout=timeout)
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            if attempt == max_retries:
                return service_name, False, f"Connection failed after {max_retries + 1} attempts"
            
            delay = 1 * (2 ** attempt)  # Exponential backoff: 1s, 2s, 4s...
            console.print(f"[yellow]Retrying {service_name} in {delay}s...[/yellow]")
            await asyncio.sleep(delay)
        except Exception as e:
            # Catch-all to prevent crashes and surface a helpful message
            return service_name, False, f"Unexpected error: {type(e).__name__}: {e}"


async def verify_api_keys(config: OatgrassConfig):
    """Verify all configured API keys"""
    console.print("[cyan][INFO][/cyan] Verifying API Keys...")
    
    api_keys = config.api_keys 
    
    # Apply a session-wide timeout in addition to per-call timeouts
    session_timeout = aiohttp.ClientTimeout(total=40)
    async with aiohttp.ClientSession(headers={"User-Agent": UA}, timeout=session_timeout) as session:
        tasks = []
        
        # API service keys
        for key_name, (verify_func, service_name) in API_SERVICES.items():
            api_key = getattr(api_keys, key_name)
            if api_key:
                tasks.append(verify_with_retry(verify_func, service_name, session, api_key))
            
        # Gazelle tracker API keys
        for tracker_name, tracker in config.trackers.items():
            if tracker.api_key:
                tasks.append(verify_with_retry(verify_gazelle_tracker, tracker_name.upper(),
                    session,
                    tracker.api_key,
                    tracker.url,
                    tracker_name.upper()
                ))

        # Run all verifications concurrently
        # verify_with_retry handles expected and unexpected exceptions and returns a tuple
        results = await asyncio.gather(*tasks)
    
        # Display results
        table = Table(title="API Key Verification Results")
        table.add_column("Service", style="cyan", no_wrap=True)
        table.add_column("Status", style="bold", no_wrap=True)
        table.add_column("Details", style="yellow")
        
        for service, status, details in results:
            status_str = "[green]✓ Valid[/green]" if status else "[red]✗ Invalid[/red]"
            # Clean up and format the details and escape rich markup
            if details:
                details = escape(str(details).strip()[:100])  # Limit length and escape markup
            table.add_row(service, status_str, details or "")
        
        if not results:
            table.add_row("No Keys", "[yellow]⚠ Warning[/yellow]", "No API keys configured")
        
        console.print(table)
        
        # Return True if all verifications passed
        if results:
            return all(status for _, status, _ in results)
        return False  # No keys configured
