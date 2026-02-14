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
