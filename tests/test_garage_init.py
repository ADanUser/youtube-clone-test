from youtube_copy.config import Settings
from youtube_copy.infrastructure.garage_init import expected_role, role_matches


def test_expected_layout_role_uses_exact_capacity_and_zone() -> None:
    settings = Settings(
        _env_file=None,
        garage_layout_zone="test-zone",
        garage_layout_capacity_bytes=42,
    )

    role = expected_role(settings, "node-id")

    assert role == {"id": "node-id", "zone": "test-zone", "capacity": 42, "tags": []}
    assert role_matches(role, role)


def test_layout_role_rejects_a_capacity_mismatch() -> None:
    expected = {"id": "node", "zone": "local", "capacity": 42, "tags": []}
    actual = {**expected, "capacity": 43}

    assert not role_matches(actual, expected)
