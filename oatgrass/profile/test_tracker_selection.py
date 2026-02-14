import pytest

from oatgrass.config import OatgrassConfig, TrackerConfig
from oatgrass.profile.tracker_selection import configured_profile_trackers, resolve_profile_tracker


def _config() -> OatgrassConfig:
    return OatgrassConfig(
        trackers={
            "red": TrackerConfig(name="RED", url="https://redacted.sh", api_key="red-key"),
            "ops": TrackerConfig(name="OPS", url="https://orpheus.network", api_key="ops-key"),
            "empty": TrackerConfig(name="EMPTY", url="https://example.invalid", api_key=""),
        }
    )


def test_configured_profile_trackers_excludes_missing_keys() -> None:
    configured = configured_profile_trackers(_config())
    keys = [key for key, _ in configured]
    assert keys == ["red", "ops"]


def test_resolve_profile_tracker_defaults_to_first_available() -> None:
    key, tracker = resolve_profile_tracker(_config())
    assert key == "red"
    assert tracker.name == "RED"


def test_resolve_profile_tracker_accepts_case_insensitive_key() -> None:
    key, tracker = resolve_profile_tracker(_config(), "OPS")
    assert key == "ops"
    assert tracker.name == "OPS"


def test_resolve_profile_tracker_rejects_unknown_key() -> None:
    with pytest.raises(ValueError, match="not configured"):
        resolve_profile_tracker(_config(), "bad")
