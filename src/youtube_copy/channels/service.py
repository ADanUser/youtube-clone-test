"""Channel profile application services."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from youtube_copy.channels.schemas import ChannelUpdateRequest
from youtube_copy.infrastructure.database.models import Channel


class ChannelNotFoundError(Exception):
    pass


class ChannelConflictError(Exception):
    pass


async def get_channel_by_handle(db: AsyncSession, handle: str) -> Channel:
    channel = await db.scalar(select(Channel).where(Channel.handle == handle.casefold()))
    if channel is None:
        raise ChannelNotFoundError
    return channel


async def update_own_channel(
    db: AsyncSession, channel_id: uuid.UUID, command: ChannelUpdateRequest
) -> Channel:
    channel = await db.get(Channel, channel_id)
    if channel is None:
        raise ChannelNotFoundError
    changes = command.model_dump(exclude_unset=True)
    for field, value in changes.items():
        if value is not None:
            setattr(channel, field, value)
    try:
        await db.commit()
        await db.refresh(channel)
    except IntegrityError as error:
        await db.rollback()
        raise ChannelConflictError from error
    return channel
