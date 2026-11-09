"""Shared fixtures for disposable PostgreSQL integration tests."""

from __future__ import annotations

import os

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import make_url

from youtube_copy.config import get_settings


@pytest.fixture(scope="session")
def migrated_database_url() -> str:
    """Rebuild an explicitly disposable database through both migration paths."""
    if os.environ.get("YOUTUBE_RUN_POSTGRES_TESTS") != "1":
        pytest.skip("set YOUTUBE_RUN_POSTGRES_TESTS=1 to run PostgreSQL integration tests")

    get_settings.cache_clear()
    database_url = get_settings().load_database_url()
    database_name = make_url(database_url).database or ""
    if not database_name.endswith("_test"):
        pytest.fail("PostgreSQL integration tests require a database name ending in '_test'")

    alembic_config = Config("alembic.ini")
    command.downgrade(alembic_config, "base")
    command.upgrade(alembic_config, "head")
    command.downgrade(alembic_config, "20261109_0001")
    command.upgrade(alembic_config, "head")
    return database_url
