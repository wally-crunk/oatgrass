from oatgrass.tracker_auth import build_tracker_auth_header


def test_tracker_auth_header_red_uses_raw_key() -> None:
    assert build_tracker_auth_header("RED", "red-key") == "red-key"


def test_tracker_auth_header_ops_uses_token_prefix() -> None:
    assert build_tracker_auth_header("OPS", "ops-key") == "token ops-key"


def test_tracker_auth_header_ops_preserves_existing_prefix() -> None:
    assert build_tracker_auth_header("ops", "token ops-key") == "token ops-key"


def test_tracker_auth_header_unknown_tracker_falls_back_to_raw() -> None:
    assert build_tracker_auth_header("other", "abc123") == "abc123"
