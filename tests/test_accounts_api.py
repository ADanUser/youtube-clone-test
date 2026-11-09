"""PostgreSQL-backed account, session, CSRF, and channel API scenarios."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from youtube_copy.accounts.dependencies import CSRF_COOKIE_NAME, SESSION_COOKIE_NAME
from youtube_copy.config import Settings
from youtube_copy.infrastructure.database.models import Session, User
from youtube_copy.infrastructure.database.session import get_db_session
from youtube_copy.main import create_app

pytestmark = pytest.mark.postgres
ORIGIN = "http://test"


@dataclass
class ApiContext:
    client: httpx.AsyncClient
    sessions: async_sessionmaker[AsyncSession]
    engine: AsyncEngine
    settings: Settings


@pytest_asyncio.fixture
async def api_context(migrated_database_url: str) -> AsyncIterator[ApiContext]:
    engine = create_async_engine(migrated_database_url, poolclass=NullPool)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    settings = Settings(
        _env_file=None,
        environment="test",
        database_url=migrated_database_url,
        public_origin=ORIGIN,
        auth_secret="integration-test-auth-secret-with-more-than-32-characters",
        login_rate_limit_attempts=100,
        registration_rate_limit_attempts=100,
    )
    app = create_app(settings)

    async def override_db_session() -> AsyncIterator[AsyncSession]:
        async with sessions() as session:
            yield session

    app.dependency_overrides[get_db_session] = override_db_session
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url=ORIGIN) as client:
        yield ApiContext(client, sessions, engine, settings)
    await engine.dispose()


async def fetch_csrf(client: httpx.AsyncClient) -> str:
    response = await client.get("/api/v1/auth/csrf")
    assert response.status_code == 200
    token = response.json()["csrf_token"]
    assert client.cookies[CSRF_COOKIE_NAME] == token
    return token


def csrf_headers(token: str, origin: str = ORIGIN) -> dict[str, str]:
    return {"Origin": origin, "X-CSRF-Token": token}


async def register(
    client: httpx.AsyncClient, username: str, email: str, password: str
) -> httpx.Response:
    token = await fetch_csrf(client)
    return await client.post(
        "/api/v1/auth/register",
        headers=csrf_headers(token),
        json={"email": email, "username": username, "password": password},
    )


async def test_registration_session_csrf_channel_and_logout(api_context: ApiContext) -> None:
    client = api_context.client
    no_csrf = await client.post(
        "/api/v1/auth/register",
        json={
            "email": "no-csrf@example.com",
            "username": "no-csrf",
            "password": "correct horse battery staple",
        },
    )
    assert no_csrf.status_code == 403

    token = await fetch_csrf(client)
    forbidden_role = await client.post(
        "/api/v1/auth/register",
        headers=csrf_headers(token),
        json={
            "email": "forbidden-role@example.com",
            "username": "forbidden-role",
            "password": "correct horse battery staple",
            "role": "admin",
        },
    )
    assert forbidden_role.status_code == 422

    response = await register(
        client,
        "account-flow",
        "Account-Flow@Example.COM",
        "correct horse battery staple",
    )
    assert response.status_code == 201
    account = response.json()
    assert account["role"] == "user"
    assert account["email"] == "account-flow@example.com"
    assert client.cookies[SESSION_COOKIE_NAME]
    authenticated_csrf = client.cookies[CSRF_COOKIE_NAME]
    set_cookie_headers = "\n".join(response.headers.get_list("set-cookie")).lower()
    assert f"{SESSION_COOKIE_NAME}=" in set_cookie_headers
    assert "httponly" in set_cookie_headers
    assert "samesite=lax" in set_cookie_headers

    async with api_context.sessions() as db:
        user = await db.scalar(select(User).where(User.id == account["id"]))
        stored_session = await db.scalar(select(Session).where(Session.user_id == account["id"]))
    assert user is not None and user.password_hash.startswith("$argon2id$")
    assert stored_session is not None
    assert stored_session.token_hash != client.cookies[SESSION_COOKIE_NAME]
    assert stored_session.csrf_token_hash != authenticated_csrf

    current = await client.get("/api/v1/auth/me")
    assert current.status_code == 200

    missing_csrf = await client.patch(
        "/api/v1/channels/me/profile", json={"title": "Updated title"}
    )
    assert missing_csrf.status_code == 403
    wrong_origin = await client.patch(
        "/api/v1/channels/me/profile",
        headers=csrf_headers(authenticated_csrf, "http://attacker.test"),
        json={"title": "Updated title"},
    )
    assert wrong_origin.status_code == 403
    updated = await client.patch(
        "/api/v1/channels/me/profile",
        headers=csrf_headers(authenticated_csrf),
        json={
            "handle": "account-flow-channel",
            "title": "Updated title",
            "description": "Plain text profile",
        },
    )
    assert updated.status_code == 200
    public_profile = await client.get("/api/v1/channels/account-flow-channel")
    assert public_profile.status_code == 200
    assert public_profile.json()["title"] == "Updated title"

    logout_without_csrf = await client.post("/api/v1/auth/logout")
    assert logout_without_csrf.status_code == 403
    logout = await client.post("/api/v1/auth/logout", headers=csrf_headers(authenticated_csrf))
    assert logout.status_code == 204
    assert (await client.get("/api/v1/auth/me")).status_code == 401


async def test_login_rotates_session_and_rate_limit_returns_429(api_context: ApiContext) -> None:
    client = api_context.client
    password = "another correct horse battery staple"
    registered = await register(client, "login-flow", "login-flow@example.com", password)
    assert registered.status_code == 201
    old_session = client.cookies[SESSION_COOKIE_NAME]

    token = await fetch_csrf(client)
    invalid = await client.post(
        "/api/v1/auth/login",
        headers=csrf_headers(token),
        json={"login": "login-flow", "password": "wrong-password"},
    )
    assert invalid.status_code == 401
    logged_in = await client.post(
        "/api/v1/auth/login",
        headers=csrf_headers(token),
        json={"login": "LOGIN-FLOW@EXAMPLE.COM", "password": password},
    )
    assert logged_in.status_code == 200
    assert client.cookies[SESSION_COOKIE_NAME] != old_session
    async with api_context.sessions() as db:
        login_user_id = logged_in.json()["id"]
        active_session_count = await db.scalar(
            select(func.count()).select_from(Session).where(Session.user_id == login_user_id)
        )
    assert active_session_count == 1

    limited_settings = api_context.settings.model_copy(
        update={
            "login_rate_limit_attempts": 2,
            "login_rate_limit_window_seconds": 60,
            "registration_rate_limit_attempts": 1,
            "registration_rate_limit_window_seconds": 60,
        }
    )
    limited_app = create_app(limited_settings)

    async def override_db_session() -> AsyncIterator[AsyncSession]:
        async with api_context.sessions() as session:
            yield session

    limited_app.dependency_overrides[get_db_session] = override_db_session
    transport = httpx.ASGITransport(app=limited_app)
    async with httpx.AsyncClient(transport=transport, base_url=ORIGIN) as limited_client:
        limited_csrf = await fetch_csrf(limited_client)
        for _ in range(2):
            attempt = await limited_client.post(
                "/api/v1/auth/login",
                headers=csrf_headers(limited_csrf),
                json={"login": "missing-user", "password": "wrong-password"},
            )
            assert attempt.status_code == 401
        rejected = await limited_client.post(
            "/api/v1/auth/login",
            headers=csrf_headers(limited_csrf),
            json={"login": "missing-user", "password": "wrong-password"},
        )
        first_registration = await register(
            limited_client,
            "limited-registration-one",
            "limited-registration-one@example.com",
            "limited registration password one",
        )
        second_registration = await register(
            limited_client,
            "limited-registration-two",
            "limited-registration-two@example.com",
            "limited registration password two",
        )
    assert rejected.status_code == 429
    assert int(rejected.headers["Retry-After"]) > 0
    assert first_registration.status_code == 201
    assert second_registration.status_code == 429


async def test_admin_roles_blocking_and_last_admin_guard(api_context: ApiContext) -> None:
    admin_client = api_context.client
    admin_response = await register(
        admin_client,
        "admin-flow",
        "admin-flow@example.com",
        "admin correct horse battery staple",
    )
    assert admin_response.status_code == 201
    admin_id = admin_response.json()["id"]
    async with api_context.sessions() as db:
        await db.execute(update(User).where(User.id == admin_id).values(role="admin"))
        await db.commit()

    user_app = create_app(api_context.settings)

    async def override_db_session() -> AsyncIterator[AsyncSession]:
        async with api_context.sessions() as session:
            yield session

    user_app.dependency_overrides[get_db_session] = override_db_session
    transport = httpx.ASGITransport(app=user_app)
    async with httpx.AsyncClient(transport=transport, base_url=ORIGIN) as user_client:
        user_response = await register(
            user_client,
            "blocked-flow",
            "blocked-flow@example.com",
            "blocked correct horse battery staple",
        )
        assert user_response.status_code == 201
        user_id = user_response.json()["id"]
        user_csrf = user_client.cookies[CSRF_COOKIE_NAME]

        forbidden_admin = await user_client.patch(
            f"/api/v1/admin/users/{user_id}",
            headers=csrf_headers(user_csrf),
            json={"role": "admin"},
        )
        assert forbidden_admin.status_code == 403

        admin_csrf = admin_client.cookies[CSRF_COOKIE_NAME]
        promoted = await admin_client.patch(
            f"/api/v1/admin/users/{user_id}",
            headers=csrf_headers(admin_csrf),
            json={"role": "moderator"},
        )
        assert promoted.status_code == 200
        assert promoted.json()["role"] == "moderator"
        assert (await user_client.get("/api/v1/auth/me")).json()["role"] == "moderator"

        blocked = await admin_client.patch(
            f"/api/v1/admin/users/{user_id}",
            headers=csrf_headers(admin_csrf),
            json={"is_active": False},
        )
        assert blocked.status_code == 200
        assert not blocked.json()["is_active"]
        assert (await user_client.get("/api/v1/auth/me")).status_code == 401
        async with api_context.sessions() as db:
            session_count = await db.scalar(
                select(func.count()).select_from(Session).where(Session.user_id == user_id)
            )
        assert session_count == 0

        blocked_mutation = await user_client.patch(
            "/api/v1/channels/me/profile",
            headers=csrf_headers(user_csrf),
            json={"title": "Must not update"},
        )
        assert blocked_mutation.status_code == 401

    last_admin = await admin_client.patch(
        f"/api/v1/admin/users/{admin_id}",
        headers=csrf_headers(admin_client.cookies[CSRF_COOKIE_NAME]),
        json={"role": "user"},
    )
    assert last_admin.status_code == 409
