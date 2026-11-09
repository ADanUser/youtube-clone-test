"""FastAPI dependencies for sessions, CSRF, roles, and client identity."""

from __future__ import annotations

import hmac
from typing import Annotated

from fastapi import Cookie, Depends, Header, HTTPException, Request, status
from fastapi.security import APIKeyCookie
from sqlalchemy.ext.asyncio import AsyncSession

from youtube_copy.accounts.security import hash_token, verify_preauth_csrf
from youtube_copy.accounts.service import AuthContext, authenticate_session
from youtube_copy.config import Settings
from youtube_copy.infrastructure.database.session import get_db_session

SESSION_COOKIE_NAME = "youtube_session"
CSRF_COOKIE_NAME = "youtube_csrf"
CSRF_HEADER_NAME = "X-CSRF-Token"

session_cookie_scheme = APIKeyCookie(name=SESSION_COOKIE_NAME, auto_error=False)


def get_request_settings(request: Request) -> Settings:
    return request.app.state.settings


def require_same_origin(
    request: Request, settings: Annotated[Settings, Depends(get_request_settings)]
) -> None:
    expected_origin = str(settings.public_origin).rstrip("/")
    supplied_origin = request.headers.get("origin", "").rstrip("/")
    if not supplied_origin or not hmac.compare_digest(supplied_origin, expected_origin):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "origin validation failed")


async def require_preauth_csrf(
    request: Request,
    settings: Annotated[Settings, Depends(get_request_settings)],
    header_token: Annotated[str | None, Header(alias=CSRF_HEADER_NAME)] = None,
    cookie_token: Annotated[str | None, Cookie(alias=CSRF_COOKIE_NAME)] = None,
) -> None:
    require_same_origin(request, settings)
    if (
        not header_token
        or not cookie_token
        or not hmac.compare_digest(header_token, cookie_token)
        or not verify_preauth_csrf(
            header_token,
            settings.load_auth_secret(),
            settings.preauth_csrf_ttl_seconds,
        )
    ):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "CSRF validation failed")


async def get_current_auth(
    db: Annotated[AsyncSession, Depends(get_db_session)],
    raw_token: Annotated[str | None, Depends(session_cookie_scheme)],
) -> AuthContext:
    auth = await authenticate_session(db, raw_token)
    if auth is None:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "authentication required",
            headers={"WWW-Authenticate": "Cookie"},
        )
    return auth


async def require_authenticated_csrf(
    request: Request,
    auth: Annotated[AuthContext, Depends(get_current_auth)],
    settings: Annotated[Settings, Depends(get_request_settings)],
    header_token: Annotated[str | None, Header(alias=CSRF_HEADER_NAME)] = None,
    cookie_token: Annotated[str | None, Cookie(alias=CSRF_COOKIE_NAME)] = None,
) -> AuthContext:
    require_same_origin(request, settings)
    if (
        not header_token
        or not cookie_token
        or not hmac.compare_digest(header_token, cookie_token)
        or not hmac.compare_digest(hash_token(header_token), auth.session.csrf_token_hash)
    ):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "CSRF validation failed")
    return auth


async def require_admin(
    auth: Annotated[AuthContext, Depends(require_authenticated_csrf)],
) -> AuthContext:
    if auth.user.role != "admin":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "administrator role required")
    return auth


def client_rate_limit_key(
    request: Request, settings: Annotated[Settings, Depends(get_request_settings)]
) -> str:
    if settings.trust_proxy_headers:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            return forwarded.split(",", 1)[0].strip()
    return request.client.host if request.client else "unknown"
