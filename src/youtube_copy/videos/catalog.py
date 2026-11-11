"""Read models for public discovery and the owner's studio."""

from __future__ import annotations

import math
import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from youtube_copy.accounts.service import AuthContext
from youtube_copy.infrastructure.database.models import Channel, Video
from youtube_copy.videos.errors import UploadError
from youtube_copy.videos.playback import can_view_video
from youtube_copy.videos.schemas import VideoUpdate


@dataclass(frozen=True)
class CatalogVideo:
    id: uuid.UUID
    title: str
    description: str
    channel_handle: str
    channel_title: str
    created_at: datetime
    duration_ms: int | None
    visibility: str
    processing_state: str
    failure_code: str | None


@dataclass(frozen=True)
class CatalogPage:
    items: list[CatalogVideo]
    page: int
    pages: int
    total: int


def _page(page: int, total: int, size: int) -> tuple[int, int]:
    pages = max(1, math.ceil(total / size))
    current = min(max(page, 1), pages)
    return current, pages


async def public_catalog(
    db: AsyncSession,
    auth: AuthContext | None,
    *,
    page: int,
    size: int = 12,
    query: str | None = None,
    channel_id: uuid.UUID | None = None,
) -> CatalogPage:
    filters = [Video.visibility == "public", can_view_video(auth.user.id if auth else None)]
    if query:
        filters.append(Video.title.ilike(f"%{query}%"))
    if channel_id:
        filters.append(Video.channel_id == channel_id)
    total = int(await db.scalar(select(func.count()).select_from(Video).where(*filters)) or 0)
    current, pages = _page(page, total, size)
    rows = (
        await db.execute(
            select(Video, Channel)
            .join(Channel, Channel.id == Video.channel_id)
            .where(*filters)
            .order_by(Video.created_at.desc(), Video.id.desc())
            .limit(size)
            .offset((current - 1) * size)
        )
    ).all()
    return CatalogPage(
        items=[
            CatalogVideo(
                id=video.id,
                title=video.title,
                description=video.description,
                channel_handle=channel.handle,
                channel_title=channel.title,
                created_at=video.created_at,
                duration_ms=video.duration_ms,
                visibility=video.visibility,
                processing_state=video.processing_state,
                failure_code=video.failure_code,
            )
            for video, channel in rows
        ],
        page=current,
        pages=pages,
        total=total,
    )


async def owner_catalog(
    db: AsyncSession, auth: AuthContext, *, page: int, size: int = 12
) -> CatalogPage:
    filters = [Video.channel_id == auth.channel.id, Video.deleted_at.is_(None)]
    total = int(await db.scalar(select(func.count()).select_from(Video).where(*filters)) or 0)
    current, pages = _page(page, total, size)
    videos = (
        await db.scalars(
            select(Video)
            .where(*filters)
            .order_by(Video.created_at.desc(), Video.id.desc())
            .limit(size)
            .offset((current - 1) * size)
        )
    ).all()
    return CatalogPage(
        items=[
            CatalogVideo(
                id=video.id,
                title=video.title,
                description=video.description,
                channel_handle=auth.channel.handle,
                channel_title=auth.channel.title,
                created_at=video.created_at,
                duration_ms=video.duration_ms,
                visibility=video.visibility,
                processing_state=video.processing_state,
                failure_code=video.failure_code,
            )
            for video in videos
        ],
        page=current,
        pages=pages,
        total=total,
    )


async def update_owned_video(
    db: AsyncSession, auth: AuthContext, video_id: uuid.UUID, command: VideoUpdate
) -> None:
    video = await db.scalar(
        select(Video).where(
            Video.id == video_id, Video.channel_id == auth.channel.id, Video.deleted_at.is_(None)
        )
    )
    if video is None:
        raise UploadError(404, "video_not_found")
    for field, value in command.model_dump(exclude_unset=True).items():
        setattr(video, field, value)
    await db.commit()
