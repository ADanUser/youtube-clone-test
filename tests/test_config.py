from pathlib import Path

import pytest
from pydantic import ValidationError

from youtube_copy.config import Settings


def test_file_based_s3_credentials_are_loaded(tmp_path: Path) -> None:
    access_key_file = tmp_path / "access-key-id"
    secret_key_file = tmp_path / "secret-access-key"
    access_key_file.write_text("access-value\n", encoding="utf-8")
    secret_key_file.write_text("secret-value\n", encoding="utf-8")

    settings = Settings(
        _env_file=None,
        s3_access_key_file=access_key_file,
        s3_secret_key_file=secret_key_file,
    )

    assert settings.load_s3_credentials() == ("access-value", "secret-value")


def test_partial_s3_credentials_are_rejected() -> None:
    with pytest.raises(ValidationError, match="access/secret pair"):
        Settings(_env_file=None, s3_access_key_id="only-access")


def test_garage_bootstrap_limits_are_validated() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, garage_init_timeout_seconds=0)
