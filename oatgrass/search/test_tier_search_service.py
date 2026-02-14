from __future__ import annotations

import pytest

from oatgrass.search.tier_search_service import search_with_tiers


class _FakeClient:
    def __init__(self, responses: list[dict]) -> None:
        self.responses = list(responses)
        self.calls: list[dict] = []

    async def search(self, **kwargs) -> dict:
        self.calls.append(kwargs)
        if self.responses:
            return self.responses.pop(0)
        return {"response": {"results": []}}


@pytest.mark.asyncio
async def test_search_with_tiers_tier1_selects_best_hit() -> None:
    client = _FakeClient(
        [
            {
                "response": {
                    "results": [
                        {"groupId": 11, "artist": "Other Artist", "groupName": "Wrong Album", "groupYear": 1970},
                        {"groupId": 22, "artist": "Miles Davis", "groupName": "Kind of Blue", "groupYear": 1959},
                    ]
                }
            }
        ]
    )

    result = await search_with_tiers(
        client,
        artist="Miles Davis",
        album="Kind of Blue",
        year=1959,
        release_type=1,
        media="CD",
        max_tier=1,
    )

    assert result is not None
    assert result["groupId"] == 22
    assert len(client.calls) == 1
    assert client.calls[0]["artistname"] == "Miles Davis"
    assert client.calls[0]["groupname"] == "Kind of Blue"
    assert client.calls[0]["release_type"] == 1
    assert client.calls[0]["media"] == "CD"


@pytest.mark.asyncio
async def test_search_with_tiers_max_tier_1_stops_after_tier1() -> None:
    client = _FakeClient(
        [
            {"response": {"results": []}},
            {"response": {"results": [{"groupId": 99}]}},
        ]
    )

    result = await search_with_tiers(client, artist="A", album="B", year=2000, max_tier=1)

    assert result is None
    assert len(client.calls) == 1


@pytest.mark.asyncio
async def test_search_with_tiers_tier1_retries_without_leading_artist_article() -> None:
    client = _FakeClient(
        [
            {"response": {"results": []}},
            {
                "response": {
                    "results": [
                        {
                            "groupId": 1969,
                            "artist": "Black Eyed Peas",
                            "groupName": "The E•N•D",
                            "groupYear": 2009,
                        }
                    ]
                }
            },
        ]
    )

    result = await search_with_tiers(
        client,
        artist="The Black Eyed Peas",
        album="The E•N•D",
        year=2009,
        release_type=1,
        max_tier=1,
    )

    assert result is not None
    assert result["groupId"] == 1969
    assert len(client.calls) == 2
    assert client.calls[1]["artistname"] == "Black Eyed Peas"


@pytest.mark.asyncio
async def test_search_with_tiers_max_tier_2_stops_before_tier3() -> None:
    client = _FakeClient(
        [
            {"response": {"results": []}},
            {"response": {"results": []}},
            {"response": {"results": [{"groupId": 77}]}},
        ]
    )

    result = await search_with_tiers(client, artist="A", album="B", year=2000, max_tier=2)

    assert result is None
    assert len(client.calls) == 2


@pytest.mark.asyncio
async def test_search_with_tiers_rejects_invalid_max_tier() -> None:
    client = _FakeClient([])

    with pytest.raises(ValueError, match="max_tier must be between 1 and 4"):
        await search_with_tiers(client, artist="A", album="B", year=2000, max_tier=0)


@pytest.mark.asyncio
async def test_search_with_tiers_accepts_string_query_year_without_crashing() -> None:
    client = _FakeClient(
        [
            {
                "response": {
                    "results": [
                        {"groupId": 22, "artist": "Miles Davis", "groupName": "Kind of Blue", "groupYear": 1959},
                    ]
                }
            }
        ]
    )

    result = await search_with_tiers(
        client,
        artist="Miles Davis",
        album="Kind of Blue",
        year="1959",  # type: ignore[arg-type]
        max_tier=1,
    )
    assert result is not None
    assert result["groupId"] == 22


@pytest.mark.asyncio
async def test_search_with_tiers_accepts_string_result_year_without_crashing() -> None:
    client = _FakeClient(
        [
            {
                "response": {
                    "results": [
                        {"groupId": 22, "artist": "Miles Davis", "groupName": "Kind of Blue", "groupYear": "1959"},
                    ]
                }
            }
        ]
    )

    result = await search_with_tiers(
        client,
        artist="Miles Davis",
        album="Kind of Blue",
        year=1959,
        max_tier=1,
    )
    assert result is not None
    assert result["groupId"] == 22
