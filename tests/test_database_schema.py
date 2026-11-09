"""PostgreSQL integration checks for migrations and domain constraints."""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from youtube_copy.config import get_settings

pytestmark = pytest.mark.postgres

EXPECTED_TABLES = {
    "channels",
    "comments",
    "likes",
    "moderation_actions",
    "playlist_items",
    "playlists",
    "processing_jobs",
    "reports",
    "sessions",
    "subscriptions",
    "users",
    "video_access_grants",
    "video_assets",
    "videos",
}


@pytest.fixture(scope="module")
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


@pytest_asyncio.fixture
async def database_engine(migrated_database_url: str) -> AsyncIterator[AsyncEngine]:
    """Provide an isolated async connection pool for one integration test."""
    engine = create_async_engine(migrated_database_url, poolclass=NullPool)
    yield engine
    await engine.dispose()


async def assert_integrity_error(
    connection: AsyncConnection, statement: str, parameters: dict[str, object]
) -> None:
    """Run a rejected statement inside a savepoint so the test transaction remains usable."""
    with pytest.raises(IntegrityError):
        async with connection.begin_nested():
            await connection.execute(text(statement), parameters)


async def test_schema_objects_and_postgres_indexes(database_engine: AsyncEngine) -> None:
    async with database_engine.connect() as connection:
        table_names = set(
            (
                await connection.execute(
                    text(
                        "SELECT tablename FROM pg_tables "
                        "WHERE schemaname = 'public' AND tablename <> 'alembic_version'"
                    )
                )
            ).scalars()
        )
        extension_exists = await connection.scalar(
            text("SELECT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'pg_trgm')")
        )
        index_definitions = dict(
            (
                await connection.execute(
                    text("SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = 'public'")
                )
            ).all()
        )

    assert table_names == EXPECTED_TABLES
    assert extension_exists is True
    assert "gin_trgm_ops" in index_definitions["ix_videos_title_trgm"]
    assert "WHERE" in index_definitions["ix_videos_public_feed"]
    assert "WHERE" in index_definitions["uq_reports_open_video_reporter"]
    assert "WHERE" in index_definitions["ix_processing_jobs_queued_run_after"]
    assert "WHERE" in index_definitions["ix_processing_jobs_running_lease_until"]


async def test_identity_state_and_foreign_key_constraints(database_engine: AsyncEngine) -> None:
    user_id = uuid.uuid4()
    channel_id = uuid.uuid4()
    video_id = uuid.uuid4()

    async with database_engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO users "
                "(id, email_normalized, username_normalized, password_hash) "
                "VALUES (:id, :email, :username, :password_hash)"
            ),
            {
                "id": user_id,
                "email": "schema@example.test",
                "username": "schema-user",
                "password_hash": "argon2id-test-hash",
            },
        )
        await assert_integrity_error(
            connection,
            "INSERT INTO users "
            "(id, email_normalized, username_normalized, password_hash) "
            "VALUES (:id, 'schema@example.test', 'other-user', 'hash')",
            {"id": uuid.uuid4()},
        )
        await assert_integrity_error(
            connection,
            "INSERT INTO users "
            "(id, email_normalized, username_normalized, password_hash, role) "
            "VALUES (:id, 'role@example.test', 'role-user', 'hash', 'owner')",
            {"id": uuid.uuid4()},
        )
        await connection.execute(
            text(
                "INSERT INTO channels (id, user_id, handle, title) "
                "VALUES (:id, :user_id, 'schema-channel', 'Schema channel')"
            ),
            {"id": channel_id, "user_id": user_id},
        )
        await assert_integrity_error(
            connection,
            "INSERT INTO videos (id, channel_id, title, duration_ms) "
            "VALUES (:id, :channel_id, 'Bad duration', -1)",
            {"id": uuid.uuid4(), "channel_id": channel_id},
        )
        await assert_integrity_error(
            connection,
            "INSERT INTO videos (id, channel_id, title) "
            "VALUES (:id, :channel_id, 'Missing channel')",
            {"id": uuid.uuid4(), "channel_id": uuid.uuid4()},
        )
        await connection.execute(
            text(
                "INSERT INTO videos (id, channel_id, title, processing_state) "
                "VALUES (:id, :channel_id, 'Valid video', 'ready')"
            ),
            {"id": video_id, "channel_id": channel_id},
        )
        await assert_integrity_error(
            connection,
            "INSERT INTO comments (id, video_id, user_id, body) "
            "VALUES (:id, :video_id, :user_id, '')",
            {"id": uuid.uuid4(), "video_id": video_id, "user_id": user_id},
        )


async def test_partial_uniqueness_and_job_lease_constraints(
    database_engine: AsyncEngine,
) -> None:
    reporter_id = uuid.uuid4()
    owner_id = uuid.uuid4()
    channel_id = uuid.uuid4()
    video_id = uuid.uuid4()
    report_id = uuid.uuid4()

    async with database_engine.begin() as connection:
        for user_id, email, username in (
            (reporter_id, "reporter@example.test", "reporter"),
            (owner_id, "owner@example.test", "owner"),
        ):
            await connection.execute(
                text(
                    "INSERT INTO users "
                    "(id, email_normalized, username_normalized, password_hash) "
                    "VALUES (:id, :email, :username, 'hash')"
                ),
                {"id": user_id, "email": email, "username": username},
            )
        await connection.execute(
            text(
                "INSERT INTO channels (id, user_id, handle, title) "
                "VALUES (:id, :user_id, 'report-channel', 'Reports')"
            ),
            {"id": channel_id, "user_id": owner_id},
        )
        await connection.execute(
            text(
                "INSERT INTO videos (id, channel_id, title) "
                "VALUES (:id, :channel_id, 'Reported video')"
            ),
            {"id": video_id, "channel_id": channel_id},
        )
        await connection.execute(
            text(
                "INSERT INTO reports (id, video_id, reporter_id, reason) "
                "VALUES (:id, :video_id, :reporter_id, 'First report')"
            ),
            {"id": report_id, "video_id": video_id, "reporter_id": reporter_id},
        )
        await assert_integrity_error(
            connection,
            "INSERT INTO reports (id, video_id, reporter_id, reason) "
            "VALUES (:id, :video_id, :reporter_id, 'Duplicate open report')",
            {"id": uuid.uuid4(), "video_id": video_id, "reporter_id": reporter_id},
        )
        await connection.execute(
            text(
                "UPDATE reports SET status = 'resolved', resolved_by = :resolver, "
                "resolved_at = now() WHERE id = :id"
            ),
            {"resolver": owner_id, "id": report_id},
        )
        await connection.execute(
            text(
                "INSERT INTO reports (id, video_id, reporter_id, reason) "
                "VALUES (:id, :video_id, :reporter_id, 'New open report')"
            ),
            {"id": uuid.uuid4(), "video_id": video_id, "reporter_id": reporter_id},
        )
        await assert_integrity_error(
            connection,
            "INSERT INTO processing_jobs "
            "(id, kind, deduplication_key, status) "
            "VALUES (:id, 'cleanup', 'invalid-running-job', 'running')",
            {"id": uuid.uuid4()},
        )
        await assert_integrity_error(
            connection,
            "INSERT INTO processing_jobs "
            "(id, kind, deduplication_key, attempts, max_attempts) "
            "VALUES (:id, 'cleanup', 'too-many-attempts', 4, 3)",
            {"id": uuid.uuid4()},
        )
