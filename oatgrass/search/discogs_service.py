"""Minimal Discogs service for artist name variation lookups."""

import asyncio
import difflib
import time
import urllib.parse
from typing import Optional

import aiohttp


class DiscogsService:
    """Minimal Discogs client for artist ANV lookups."""
    
    def __init__(self, token: str, user_agent: str = "Oatgrass/0.0.1"):
        self.token = token
        self.user_agent = user_agent
        self.base_url = "https://api.discogs.com"
        self.rate_limit = asyncio.Semaphore(25)
        self.last_request = 0
        self.headers = {
            "Authorization": f"Discogs token={self.token}",
            "User-Agent": self.user_agent
        }
    
    async def _make_request(self, endpoint: str) -> dict:
        """Make rate-limited request to Discogs API."""
        async with self.rate_limit:
            now = time.time()
            if now - self.last_request < 2.4:  # ~25 requests per minute
                await asyncio.sleep(2.4 - (now - self.last_request))
            
            self.last_request = time.time()
            
            timeout = aiohttp.ClientTimeout(total=30)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(
                    f"{self.base_url}{endpoint}",
                    headers=self.headers
                ) as response:
                    if response.status == 429:
                        retry_after = int(response.headers.get("Retry-After", 60))
                        await asyncio.sleep(retry_after)
                        return await self._make_request(endpoint)
                    
                    response.raise_for_status()
                    return await response.json()
    
    async def get_artist_variations(
        self,
        artist: str,
        album: str,
        year: Optional[int] = None
    ) -> list[str]:
        """
        Get artist name variations from Discogs.
        
        Returns list: [canonical_name, real_name, best_anv]
        """
        query_string = urllib.parse.urlencode({'q': artist, 'type': 'artist'})
        result = await self._make_request(f"/database/search?{query_string}")
        
        if not result.get('results'):
            return []
        
        # Find best match in top 3 by difflib similarity
        # NOTE: We don't trust Discogs search ranking (as of 2025). Example:
        # Query "Maalem John Doe" returns:
        #   #1: Maâlem James Doe (wrong - honorific match)
        #   #2: Maleem John Doe (correct - but ranked lower)
        # Discogs ranks by character similarity, not semantic meaning.
        # Maalem/Maleem/Maâlem are the same honorific but treated as distinct strings.
        candidates = []
        for search_result in result['results'][:3]:
            artist_id = search_result['id']
            artist_data = await self._make_request(f"/artists/{artist_id}")
            
            canonical = artist_data.get('name', '')
            anvs = artist_data.get('namevariations', [])
            
            # Check if exact match in canonical or ANVs
            if canonical.lower() == artist.lower() or any(anv.lower() == artist.lower() for anv in anvs):
                ratio = 1.0
            else:
                ratio = difflib.SequenceMatcher(None, artist.lower(), canonical.lower()).ratio()
            
            candidates.append((ratio, artist_data))
        
        if not candidates:
            return []
        
        # Get best match
        best_artist = max(candidates, key=lambda x: x[0])[1]
        variations = [best_artist.get('name')]
        
        # Add real name if present
        if best_artist.get('realname'):
            variations.append(best_artist['realname'])
        
        # Add best ANV by difflib similarity
        anvs = best_artist.get('namevariations', [])
        if anvs:
            filtered = [anv for anv in anvs if anv.isascii() and abs(len(anv) - len(artist)) <= 10]
            if filtered:
                best_anv = max(filtered, key=lambda x: difflib.SequenceMatcher(None, artist.lower(), x.lower()).ratio())
                if difflib.SequenceMatcher(None, artist.lower(), best_anv.lower()).ratio() >= 0.8:
                    variations.append(best_anv)
        
        return variations
