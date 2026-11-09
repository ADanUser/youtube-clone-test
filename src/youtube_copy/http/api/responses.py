"""Shared JSON response and cookie helpers."""

from __future__ import annotations

from fastapi import Response

from youtube_copy.accounts.dependencies import CSRF_COOKIE_NAME, SESSION_COOKIE_NAME
from youtube_copy.accounts.schemas import AccountResponse, AuthenticatedAccountResponse
from youtube_copy.accounts.service import AuthContext, IssuedSession
from youtube_copy.config import Settings
from youtube_copy.infrastructure.database.models import User


def account_response(user: User) -> AccountResponse:
    return AccountResponse(
        id=user.id,
        email=user.email_normalized,
        username=user.username_normalized,
        role=user.role,
        is_active=user.is_active,
        created_at=user.created_at,
    )


def authenticated_account_response(auth: AuthContext) -> AuthenticatedAccountResponse:
    return AuthenticatedAccountResponse(
        **account_response(auth.user).model_dump(),
        channel_id=auth.channel.id,
        channel_handle=auth.channel.handle,
    )


def set_auth_cookies(response: Response, issued: IssuedSession, settings: Settings) -> None:
    response.set_cookie(
        SESSION_COOKIE_NAME,
        issued.token,
        max_age=settings.session_ttl_seconds,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        path="/",
    )
    response.set_cookie(
        CSRF_COOKIE_NAME,
        issued.csrf_token,
        max_age=settings.session_ttl_seconds,
        httponly=False,
        secure=settings.cookie_secure,
        samesite="lax",
        path="/",
    )


def clear_auth_cookies(response: Response, settings: Settings) -> None:
    for name in (SESSION_COOKIE_NAME, CSRF_COOKIE_NAME):
        response.delete_cookie(
            name,
            httponly=name == SESSION_COOKIE_NAME,
            secure=settings.cookie_secure,
            samesite="lax",
            path="/",
        )
