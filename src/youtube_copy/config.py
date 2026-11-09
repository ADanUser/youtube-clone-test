"""Typed application configuration shared by web and worker processes."""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import AnyHttpUrl, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url


class Settings(BaseSettings):
    """Environment-backed settings with file-based S3 secret support."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="YOUTUBE_",
        env_ignore_empty=True,
        extra="ignore",
    )

    environment: Literal["development", "test", "production"] = "development"
    debug: bool = False
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"

    database_url: SecretStr = SecretStr(
        "postgresql+asyncpg://youtube:development-only@localhost:5432/youtube"
    )
    database_password_file: Path | None = None

    s3_endpoint_url: AnyHttpUrl = AnyHttpUrl("http://localhost:3900")
    s3_region: str = "garage"
    s3_bucket: str = "videos"
    s3_access_key_id: SecretStr | None = None
    s3_secret_access_key: SecretStr | None = None
    s3_access_key_file: Path | None = None
    s3_secret_key_file: Path | None = None

    garage_layout_zone: str = "local"
    garage_layout_capacity_bytes: int = Field(default=10_000_000_000, gt=0)
    garage_app_key_name: str = "youtube-app"
    garage_init_timeout_seconds: int = Field(default=180, gt=0)
    garage_init_poll_seconds: int = Field(default=5, gt=0, le=30)

    @model_validator(mode="after")
    def validate_s3_credential_sources(self) -> "Settings":
        direct = (self.s3_access_key_id, self.s3_secret_access_key)
        files = (self.s3_access_key_file, self.s3_secret_key_file)
        if (direct[0] is None) != (direct[1] is None):
            raise ValueError("S3 credentials must be supplied as an access/secret pair")
        if (files[0] is None) != (files[1] is None):
            raise ValueError("S3 credential file paths must be supplied as a pair")
        if all(value is not None for value in direct + files):
            raise ValueError("choose either direct or file-based S3 credentials, not both")
        return self

    def load_s3_credentials(self) -> tuple[str, str]:
        """Load an application key without exposing it through logs or representations."""
        if self.s3_access_key_id is not None and self.s3_secret_access_key is not None:
            return (
                self.s3_access_key_id.get_secret_value(),
                self.s3_secret_access_key.get_secret_value(),
            )
        if self.s3_access_key_file is not None and self.s3_secret_key_file is not None:
            return (
                self.s3_access_key_file.read_text(encoding="utf-8").strip(),
                self.s3_secret_key_file.read_text(encoding="utf-8").strip(),
            )
        raise RuntimeError("S3 application credentials are not configured")

    def load_database_url(self) -> str:
        """Return the database URL, optionally injecting a file-backed password."""
        raw_url = self.database_url.get_secret_value()
        if self.database_password_file is None:
            return raw_url
        password = self.database_password_file.read_text(encoding="utf-8").strip()
        if not password:
            raise RuntimeError("database password file is empty")
        return make_url(raw_url).set(password=password).render_as_string(hide_password=False)


@lru_cache
def get_settings() -> Settings:
    """Build settings once per process."""
    return Settings()
