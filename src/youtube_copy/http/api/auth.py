"""Registration, login, logout, and current-session endpoints."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Cookie, Depends, HTTPException, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from youtube_copy.accounts.dependencies import (
    CSRF_COOKIE_NAME,
    SESSION_COOKIE_NAME,
    client_rate_limit_key,
    get_current_auth,
    get_request_settings,
    require_authenticated_csrf,
    require_preauth_csrf,
)
from youtube_copy.accounts.schemas import (
    AuthenticatedAccountResponse,
    CsrfTokenResponse,
    LoginRequest,
    RegisterRequest,
)
from youtube_copy.accounts.security import issue_preauth_csrf
from youtube_copy.accounts.service import (
    AccountConflictError,
    AuthContext,
    InvalidCredentialsError,
    login_account,
    logout_session,
    register_account,
)
from youtube_copy.config import Settings
from youtube_copy.http.api.responses import (
    authenticated_account_response,
    clear_auth_cookies,
    set_auth_cookies,
)
from youtube_copy.infrastructure.database.session import get_db_session

router = APIRouter(prefix="/auth", tags=["authentication"])


def _set_preauth_cookie(response: Response, token: str, settings: Settings) -> None:
    response.set_cookie(
        CSRF_COOKIE_NAME,
        token,
        max_age=settings.preauth_csrf_ttl_seconds,
        httponly=False,
        secure=settings.cookie_secure,
        samesite="lax",
        path="/",
    )


async def _enforce_rate_limit(request: Request, key: str, limit: int, window_seconds: int) -> None:
    decision = await request.app.state.auth_rate_limiter.check(key, limit, window_seconds)
    if not decision.allowed:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "too many authentication attempts",
            headers={"Retry-After": str(decision.retry_after_seconds)},
        )


@router.get("/csrf", response_model=CsrfTokenResponse)
async def csrf_token(
    response: Response,
    settings: Annotated[Settings, Depends(get_request_settings)],
) -> CsrfTokenResponse:
    token = issue_preauth_csrf(settings.load_auth_secret())
    _set_preauth_cookie(response, token, settings)
    return CsrfTokenResponse(csrf_token=token)


@router.post(
    "/register",
    response_model=AuthenticatedAccountResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_preauth_csrf)],
)
async def register(
    command: RegisterRequest,
    request: Request,
    response: Response,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    settings: Annotated[Settings, Depends(get_request_settings)],
    client_key: Annotated[str, Depends(client_rate_limit_key)],
) -> AuthenticatedAccountResponse:
    await _enforce_rate_limit(
        request,
        f"register:{client_key}",
        settings.registration_rate_limit_attempts,
        settings.registration_rate_limit_window_seconds,
    )
    try:
        auth, issued = await register_account(db, command, settings.session_ttl_seconds)
    except AccountConflictError as error:
        raise HTTPException(status.HTTP_409_CONFLICT, "account already exists") from error
    set_auth_cookies(response, issued, settings)
    return authenticated_account_response(auth)


@router.post(
    "/login",
    response_model=AuthenticatedAccountResponse,
    dependencies=[Depends(require_preauth_csrf)],
)
async def login(
    command: LoginRequest,
    request: Request,
    response: Response,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    settings: Annotated[Settings, Depends(get_request_settings)],
    client_key: Annotated[str, Depends(client_rate_limit_key)],
    previous_token: Annotated[str | None, Cookie(alias=SESSION_COOKIE_NAME)] = None,
) -> AuthenticatedAccountResponse:
    await _enforce_rate_limit(
        request,
        f"login:{client_key}",
        settings.login_rate_limit_attempts,
        settings.login_rate_limit_window_seconds,
    )
    try:
        auth, issued = await login_account(
            db,
            command.login,
            command.password,
            settings.session_ttl_seconds,
            previous_token,
        )
    except InvalidCredentialsError as error:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid credentials") from error
    set_auth_cookies(response, issued, settings)
    return authenticated_account_response(auth)


@router.get("/me", response_model=AuthenticatedAccountResponse)
async def current_account(
    auth: Annotated[AuthContext, Depends(get_current_auth)],
) -> AuthenticatedAccountResponse:
    return authenticated_account_response(auth)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    response: Response,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    settings: Annotated[Settings, Depends(get_request_settings)],
    auth: Annotated[AuthContext, Depends(require_authenticated_csrf)],
) -> None:
    await logout_session(db, auth.session.id)
    clear_auth_cookies(response, settings)
