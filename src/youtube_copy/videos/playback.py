"""Shared viewing policy and owner-managed grants, independent of HTTP/S3."""

import uuid

from sqlalchemy import delete, exists, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from youtube_copy.accounts.service import AuthContext
from youtube_copy.infrastructure.database.models import (
    Channel,
    User,
    Video,
    VideoAccessGrant,
    VideoAsset,
)
from youtube_copy.videos.errors import UploadError
from youtube_copy.videos.schemas import VideoStatus


def can_view_video(user_id: uuid.UUID | None) -> ColumnElement[bool]:
    """Correlated predicate reusable before pagination; roles never bypass privacy."""
    # Catalog/playlist queries also join Channel: only Video may correlate outward.
    owner_active = (
        exists()
        .correlate(Video)
        .where(Channel.id == Video.channel_id, User.id == Channel.user_id, User.is_active.is_(True))
    )
    audience = Video.visibility == "public"
    if user_id is not None:
        audience = or_(
            audience,
            exists()
            .correlate(Video)
            .where(Channel.id == Video.channel_id, Channel.user_id == user_id),
            exists()
            .correlate(Video)
            .where(VideoAccessGrant.video_id == Video.id, VideoAccessGrant.user_id == user_id),
        )
    return (
        (Video.processing_state == "ready")
        & (Video.moderation_state == "visible")
        & Video.deleted_at.is_(None)
        & owner_active
        & audience
    )


async def view_video(
    db: AsyncSession, video_id: uuid.UUID, auth: AuthContext | None
) -> VideoStatus:
    video = await db.scalar(
        select(Video).where(Video.id == video_id, can_view_video(auth.user.id if auth else None))
    )
    if video is None:
        raise UploadError(404, "video_not_found")
    result = VideoStatus.model_validate(video)
    await db.commit()
    return result


async def view_asset(
    db: AsyncSession, video_id: uuid.UUID, kind: str, auth: AuthContext | None
) -> tuple[str, int]:
    row = (
        await db.execute(
            select(VideoAsset.object_key, VideoAsset.size_bytes)
            .join(Video, Video.id == VideoAsset.video_id)
            .where(
                Video.id == video_id,
                VideoAsset.kind == kind,
                can_view_video(auth.user.id if auth else None),
            )
        )
    ).one_or_none()
    if row is None:
        raise UploadError(404, "video_not_found")
    result = (row.object_key, row.size_bytes)
    # Release the transaction/connection before waiting on S3 or a slow client.
    await db.commit()
    return result


async def change_grant(
    db: AsyncSession, video_id: uuid.UUID, user_id: uuid.UUID, auth: AuthContext, *, grant: bool
) -> None:
    video = await db.scalar(
        select(Video)
        .where(
            Video.id == video_id,
            Video.channel_id == auth.channel.id,
            Video.deleted_at.is_(None),
        )
        .with_for_update()
    )
    if video is None:
        raise UploadError(404, "video_not_found")
    if grant:
        if not await db.scalar(select(User.id).where(User.id == user_id, User.is_active.is_(True))):
            raise UploadError(404, "user_not_found")
        await db.execute(
            insert(VideoAccessGrant)
            .values(video_id=video_id, user_id=user_id, granted_by=auth.user.id)
            .on_conflict_do_nothing(index_elements=["video_id", "user_id"])
        )
    else:
        await db.execute(
            delete(VideoAccessGrant).where(
                VideoAccessGrant.video_id == video_id, VideoAccessGrant.user_id == user_id
            )
        )
    await db.commit()
