"""Protocol definitions for Gazelle and Discogs clients."""

from __future__ import annotations

from typing import Protocol, Sequence

from oatgrass.search.types import GazelleSearchResult


class GazelleClient(Protocol):
    """Minimal Gazelle search client API used by the coordinator."""

    async def search(
        self,
        artist: str,
        album: str | None = None,
        year: int | None = None,
        release_type: int | None = None,
        media: str | None = None,
    ) -> Sequence[GazelleSearchResult]:
        ...


class DiscogsClient(Protocol):
    """Minimal Discogs client API used by the coordinator."""

    async def get_anvs(self, artist: str) -> Sequence[str]:
        ...
