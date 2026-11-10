"""Server-rendered user-facing pages backed by shared application services."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from youtube_copy.accounts.dependencies import (
    get_current_auth,
    get_optional_auth,
    require_moderator,
)
from youtube_copy.accounts.service import AuthContext, list_accounts_as_admin
from youtube_copy.channels.service import ChannelNotFoundError, get_channel_by_handle
from youtube_copy.http.html.uploads import templates
from youtube_copy.infrastructure.database.session import get_db_session
from youtube_copy.moderation.service import list_reports
from youtube_copy.social.service import list_own_playlists, subscription_state, view_playlist
from youtube_copy.videos.catalog import owner_catalog, public_catalog

router = APIRouter()
Database = Annotated[AsyncSession, Depends(get_db_session)]
Viewer = Annotated[AuthContext | None, Depends(get_optional_auth)]


def context(request: Request, auth: AuthContext | None, **values: object) -> dict[str, object]:
    return {"request": request, "auth": auth, **values}


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
async def home(
    request: Request, db: Database, auth: Viewer, page: int = Query(1, ge=1)
) -> HTMLResponse:
    videos = await public_catalog(db, auth, page=page)
    return templates.TemplateResponse(request, "home.html", context(request, auth, videos=videos))


@router.get("/search", response_class=HTMLResponse, include_in_schema=False)
async def search(
    request: Request,
    db: Database,
    auth: Viewer,
    q: str = Query("", max_length=200),
    page: int = Query(1, ge=1),
) -> HTMLResponse:
    query = q.strip()
    videos = await public_catalog(db, auth, page=page, query=query) if query else None
    return templates.TemplateResponse(
        request, "search.html", context(request, auth, videos=videos, query=query)
    )


@router.get("/channel/{handle}", response_class=HTMLResponse, include_in_schema=False)
async def channel(
    handle: str, request: Request, db: Database, auth: Viewer, page: int = Query(1, ge=1)
) -> HTMLResponse:
    try:
        profile = await get_channel_by_handle(db, handle)
    except ChannelNotFoundError as error:
        raise HTTPException(404, "channel_not_found") from error
    videos = await public_catalog(db, auth, page=page, channel_id=profile.id)
    subscribers, subscribed = await subscription_state(db, profile, auth)
    return templates.TemplateResponse(
        request,
        "channel.html",
        context(
            request,
            auth,
            profile=profile,
            videos=videos,
            subscribers=subscribers,
            subscribed=subscribed,
        ),
    )


@router.get("/login", response_class=HTMLResponse, include_in_schema=False)
async def login_page(request: Request, auth: Viewer) -> Response:
    if auth:
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(request, "auth.html", context(request, auth, mode="login"))


@router.get("/register", response_class=HTMLResponse, include_in_schema=False)
async def register_page(request: Request, auth: Viewer) -> Response:
    if auth:
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(request, "auth.html", context(request, auth, mode="register"))


@router.get("/studio", response_class=HTMLResponse, include_in_schema=False)
async def studio(
    request: Request,
    db: Database,
    auth: Annotated[AuthContext, Depends(get_current_auth)],
    page: int = Query(1, ge=1),
) -> HTMLResponse:
    videos = await owner_catalog(db, auth, page=page)
    return templates.TemplateResponse(
        request,
        "studio.html",
        context(request, auth, videos=videos),
        headers={"Cache-Control": "private, no-store"},
    )


@router.get("/playlists", response_class=HTMLResponse, include_in_schema=False)
async def playlists_page(
    request: Request,
    db: Database,
    auth: Annotated[AuthContext, Depends(get_current_auth)],
) -> HTMLResponse:
    playlists = await list_own_playlists(db, auth)
    return templates.TemplateResponse(
        request,
        "playlists.html",
        context(request, auth, playlists=playlists),
        headers={"Cache-Control": "private, no-store"},
    )


@router.get("/playlist/{playlist_id}", response_class=HTMLResponse, include_in_schema=False)
async def playlist_page(
    playlist_id: uuid.UUID, request: Request, db: Database, auth: Viewer
) -> HTMLResponse:
    playlist = await view_playlist(db, playlist_id, auth)
    return templates.TemplateResponse(
        request,
        "playlist.html",
        context(request, auth, view=playlist),
        headers={"Cache-Control": "private, no-store"},
    )


@router.get("/moderation", response_class=HTMLResponse, include_in_schema=False)
async def moderation_page(
    request: Request,
    db: Database,
    auth: Annotated[AuthContext, Depends(require_moderator)],
) -> HTMLResponse:
    reports = await list_reports(db, auth)
    users = await list_accounts_as_admin(db) if auth.user.role == "admin" else []
    return templates.TemplateResponse(
        request,
        "moderation.html",
        context(request, auth, reports=reports, users=users),
        headers={"Cache-Control": "private, no-store"},
    )
