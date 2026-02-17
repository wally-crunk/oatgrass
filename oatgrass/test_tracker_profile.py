from __future__ import annotations

import pytest

from oatgrass.tracker_profile import resolve_tracker_profile


def test_resolve_tracker_profile_list_types_per_tracker() -> None:
    assert resolve_tracker_profile("ops").list_types == (
        "snatched",
        "uploaded",
        "seeding",
        "leeching",
    )
    assert resolve_tracker_profile("red").list_types == (
        "seeding",
        "leeching",
        "uploaded",
        "snatched",
    )


def test_resolve_tracker_profile_exposes_request_limits_and_token_auth() -> None:
    assert resolve_tracker_profile("ops").request_limit == 5
    assert resolve_tracker_profile("red").request_limit == 10
    assert resolve_tracker_profile("ops").token_auth is True
    assert resolve_tracker_profile("red").token_auth is False


def test_resolve_tracker_profile_normalizes_tracker_name() -> None:
    assert resolve_tracker_profile("  OPS  ") == resolve_tracker_profile("ops")
    assert resolve_tracker_profile(" RED ").request_limit == 10


def test_resolve_tracker_profile_rejects_unknown_tracker() -> None:
    with pytest.raises(ValueError, match="Unsupported tracker"):
        resolve_tracker_profile("other")


def test_resolve_tracker_profile_rejects_missing_tracker() -> None:
    with pytest.raises(ValueError, match="Unsupported tracker"):
        resolve_tracker_profile(None)
