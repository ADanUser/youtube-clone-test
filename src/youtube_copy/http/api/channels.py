"""Public and owner-only channel profile endpoints."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from youtube_copy.accounts.dependencies import require_authenticated_csrf
from youtube_copy.accounts.service import AuthContext
from youtube_copy.channels.schemas import ChannelResponse, ChannelUpdateRequest
from youtube_copy.channels.service import (
    ChannelConflictError,
    ChannelNotFoundError,
    get_channel_by_handle,
    update_own_channel,
)
from youtube_copy.infrastructure.database.models import Channel
from youtube_copy.infrastructure.database.session import get_db_session

router = APIRouter(prefix="/channels", tags=["channels"])


def channel_response(channel: Channel) -> ChannelResponse:
    return ChannelResponse(
        id=channel.id,
        handle=channel.handle,
        title=channel.title,
        description=channel.description,
        created_at=channel.created_at,
        updated_at=channel.updated_at,
    )


@router.get("/{handle}", response_model=ChannelResponse)
async def channel_profile(
    handle: str, db: Annotated[AsyncSession, Depends(get_db_session)]
) -> ChannelResponse:
    try:
        channel = await get_channel_by_handle(db, handle)
    except ChannelNotFoundError as error:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "channel not found") from error
    return channel_response(channel)


@router.patch("/me/profile", response_model=ChannelResponse)
async def update_channel_profile(
    command: ChannelUpdateRequest,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    auth: Annotated[AuthContext, Depends(require_authenticated_csrf)],
) -> ChannelResponse:
    try:
        channel = await update_own_channel(db, auth.channel.id, command)
    except ChannelNotFoundError as error:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "channel not found") from error
    except ChannelConflictError as error:
        raise HTTPException(status.HTTP_409_CONFLICT, "channel handle already exists") from error
    return channel_response(channel)
