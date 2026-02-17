from oatgrass.profile.retriever import ProfileTorrent
from oatgrass.profile.session_state import ProfileSessionState


def _entry(name: str, size: int) -> ProfileTorrent:
    return ProfileTorrent(
        tracker="OPS",
        list_type="snatched",
        group_id=1,
        torrent_id=2,
        group_name=name,
        artist_name="Artist",
        artist_id=3,
        media="WEB",
        format="FLAC",
        encoding="Lossless",
        metadata={"size": size},
    )


def test_session_state_starts_empty() -> None:
    state = ProfileSessionState()
    assert state.is_empty() is True
    assert state.has_list("ops", "snatched") is False


def test_session_state_requires_matching_tracker_and_nonempty_list() -> None:
    state = ProfileSessionState()
    state.set_snapshot(
        "ops",
        {
            "snatched": [_entry("A", 100)],
            "uploaded": [],
            "downloaded": [_entry("B", 200)],
        },
    )
    assert state.is_empty() is False
    assert state.has_list("ops", "snatched") is True
    assert state.has_list("ops", "uploaded") is False
    assert state.has_list("red", "snatched") is False


def test_session_state_keys_lists_by_tracker_and_list_type() -> None:
    state = ProfileSessionState()
    state.set_snapshot(
        "ops",
        {
            "snatched": [_entry("OPS Snatch", 101)],
            "uploaded": [],
            "downloaded": [],
        },
    )
    state.set_snapshot(
        "red",
        {
            "snatched": [_entry("RED Snatch", 202)],
            "uploaded": [],
            "downloaded": [],
        },
    )

    ops_entries = state.get_list("ops", "snatched")
    red_entries = state.get_list("red", "snatched")

    assert len(ops_entries) == 1
    assert ops_entries[0].group_name == "OPS Snatch"
    assert len(red_entries) == 1
    assert red_entries[0].group_name == "RED Snatch"


def test_session_state_replaces_snapshot_for_same_tracker() -> None:
    state = ProfileSessionState()
    state.set_snapshot(
        "ops",
        {
            "snatched": [_entry("First", 111)],
            "uploaded": [],
            "downloaded": [_entry("Old Downloaded", 222)],
        },
    )
    state.set_snapshot(
        "ops",
        {
            "snatched": [_entry("Second", 333)],
            "uploaded": [],
            "downloaded": [],
        },
    )

    current = state.get_list("ops", "snatched")
    removed = state.get_list("ops", "downloaded")
    assert len(current) == 1
    assert current[0].group_name == "Second"
    assert removed == []


def test_session_state_tracker_lookup_is_case_insensitive() -> None:
    state = ProfileSessionState()
    state.set_snapshot(
        "OPS",
        {
            "snatched": [_entry("Case Test", 123)],
            "uploaded": [],
            "downloaded": [],
        },
    )

    assert state.has_list("ops", "snatched") is True
    assert state.has_list("OPS", "snatched") is True
    assert state.get_list("OpS", "snatched")[0].group_name == "Case Test"
