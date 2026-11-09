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


def test_database_password_can_be_loaded_from_a_file(tmp_path: Path) -> None:
    password_file = tmp_path / "database-password"
    password_file.write_text("file-secret\n", encoding="utf-8")
    settings = Settings(
        _env_file=None,
        database_url="postgresql+asyncpg://youtube@postgres/youtube",
        database_password_file=password_file,
    )

    assert settings.load_database_url() == (
        "postgresql+asyncpg://youtube:file-secret@postgres/youtube"
    )


def test_production_requires_a_non_default_authentication_secret() -> None:
    with pytest.raises(ValidationError, match="authentication secret"):
        Settings(_env_file=None, environment="production")


def test_development_authentication_secret_is_ephemeral_and_stable_per_settings() -> None:
    settings = Settings(_env_file=None)

    assert settings.load_auth_secret() == settings.load_auth_secret()
    assert len(settings.load_auth_secret()) >= 32
