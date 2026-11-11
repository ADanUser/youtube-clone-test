"""Social actions and playlists with access checks in one application layer."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import delete, exists, false, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from youtube_copy.accounts.service import AuthContext
from youtube_copy.infrastructure.database.models import (
    Channel,
    Comment,
    Like,
    Playlist,
    PlaylistItem,
    Subscription,
    User,
    Video,
)
from youtube_copy.social.schemas import CommentCreate, PlaylistCreate
from youtube_copy.videos.errors import UploadError
from youtube_copy.videos.playback import can_view_video


@dataclass(frozen=True)
class PlaylistVideo:
    id: uuid.UUID
    title: str
    channel_title: str
    channel_handle: str
    position: int


@dataclass(frozen=True)
class PlaylistView:
    playlist: Playlist
    owner_handle: str
    owner_title: str
    videos: list[PlaylistVideo]
    own: bool


@dataclass(frozen=True)
class CommentView:
    id: uuid.UUID
    body: str
    username: str
    channel_handle: str
    created_at: datetime
    own: bool


@dataclass(frozen=True)
class VideoSocial:
    like_count: int
    liked: bool
    comments: list[CommentView]
    playlists: list[Playlist]


async def _require_viewable(db: AsyncSession, video_id: uuid.UUID, user_id: uuid.UUID) -> Video:
    video = await db.scalar(select(Video).where(Video.id == video_id, can_view_video(user_id)))
    if video is None:
        raise UploadError(404, "video_not_found")
    return video


async def video_social(
    db: AsyncSession, video_id: uuid.UUID, auth: AuthContext | None
) -> VideoSocial:
    user_id = auth.user.id if auth else None
    like_count = int(
        await db.scalar(select(func.count()).select_from(Like).where(Like.video_id == video_id))
        or 0
    )
    liked = bool(
        user_id
        and await db.scalar(
            select(exists().where(Like.video_id == video_id, Like.user_id == user_id))
        )
    )
    rows = (
        await db.execute(
            select(Comment, User, Channel)
            .join(User, User.id == Comment.user_id)
            .join(Channel, Channel.user_id == User.id)
            .where(
                Comment.video_id == video_id, Comment.deleted_at.is_(None), User.is_active.is_(True)
            )
            .order_by(Comment.created_at.desc(), Comment.id.desc())
            .limit(100)
        )
    ).all()
    playlists = []
    if auth:
        playlists = list(
            await db.scalars(
                select(Playlist)
                .where(Playlist.owner_id == auth.user.id)
                .order_by(Playlist.created_at.desc())
            )
        )
    return VideoSocial(
        like_count=like_count,
        liked=liked,
        comments=[
            CommentView(
                id=comment.id,
                body=comment.body,
                username=user.username_normalized,
                channel_handle=channel.handle,
                created_at=comment.created_at,
                own=bool(auth and comment.user_id == auth.user.id),
            )
            for comment, user, channel in rows
        ],
        playlists=playlists,
    )


async def set_like(
    db: AsyncSession, video_id: uuid.UUID, auth: AuthContext, *, enabled: bool
) -> None:
    await _require_viewable(db, video_id, auth.user.id)
    if enabled:
        await db.execute(
            insert(Like)
            .values(video_id=video_id, user_id=auth.user.id)
            .on_conflict_do_nothing(index_elements=["video_id", "user_id"])
        )
    else:
        await db.execute(
            delete(Like).where(Like.video_id == video_id, Like.user_id == auth.user.id)
        )
    await db.commit()


async def create_comment(
    db: AsyncSession, video_id: uuid.UUID, auth: AuthContext, command: CommentCreate
) -> CommentView:
    await _require_viewable(db, video_id, auth.user.id)
    comment = Comment(video_id=video_id, user_id=auth.user.id, body=command.body)
    db.add(comment)
    await db.flush()
    result = CommentView(
        id=comment.id,
        body=comment.body,
        username=auth.user.username_normalized,
        channel_handle=auth.channel.handle,
        created_at=comment.created_at,
        own=True,
    )
    await db.commit()
    return result


async def delete_comment(db: AsyncSession, comment_id: uuid.UUID, auth: AuthContext) -> None:
    changed = await db.scalar(
        update(Comment)
        .where(
            Comment.id == comment_id, Comment.user_id == auth.user.id, Comment.deleted_at.is_(None)
        )
        .values(deleted_at=func.now())
        .returning(Comment.id)
    )
    if changed is None:
        raise UploadError(404, "comment_not_found")
    await db.commit()


async def set_subscription(
    db: AsyncSession, channel_id: uuid.UUID, auth: AuthContext, *, enabled: bool
) -> None:
    channel = await db.scalar(select(Channel).where(Channel.id == channel_id))
    if channel is None:
        raise UploadError(404, "channel_not_found")
    if channel.user_id == auth.user.id:
        raise UploadError(409, "self_subscription_forbidden")
    if enabled:
        await db.execute(
            insert(Subscription)
            .values(subscriber_id=auth.user.id, channel_id=channel_id)
            .on_conflict_do_nothing(index_elements=["subscriber_id", "channel_id"])
        )
    else:
        await db.execute(
            delete(Subscription).where(
                Subscription.subscriber_id == auth.user.id,
                Subscription.channel_id == channel_id,
            )
        )
    await db.commit()


async def subscription_state(
    db: AsyncSession, channel: Channel, auth: AuthContext | None
) -> tuple[int, bool]:
    count = int(
        await db.scalar(
            select(func.count())
            .select_from(Subscription)
            .where(Subscription.channel_id == channel.id)
        )
        or 0
    )
    subscribed = bool(
        auth
        and await db.scalar(
            select(
                exists().where(
                    Subscription.subscriber_id == auth.user.id,
                    Subscription.channel_id == channel.id,
                )
            )
        )
    )
    return count, subscribed


async def create_playlist(db: AsyncSession, auth: AuthContext, command: PlaylistCreate) -> Playlist:
    playlist = Playlist(owner_id=auth.user.id, **command.model_dump())
    db.add(playlist)
    await db.commit()
    await db.refresh(playlist)
    return playlist


async def add_playlist_video(
    db: AsyncSession, playlist_id: uuid.UUID, video_id: uuid.UUID, auth: AuthContext
) -> None:
    playlist = await db.scalar(
        select(Playlist)
        .where(Playlist.id == playlist_id, Playlist.owner_id == auth.user.id)
        .with_for_update()
    )
    if playlist is None:
        raise UploadError(404, "playlist_not_found")
    video = await _require_viewable(db, video_id, auth.user.id)
    if video.visibility == "private" and video.channel_id != auth.channel.id:
        raise UploadError(403, "private_video_playlist_forbidden")
    position = (
        int(
            await db.scalar(
                select(func.coalesce(func.max(PlaylistItem.position), -1)).where(
                    PlaylistItem.playlist_id == playlist_id
                )
            )
        )
        + 1
    )
    await db.execute(
        insert(PlaylistItem)
        .values(playlist_id=playlist_id, video_id=video_id, position=position)
        .on_conflict_do_nothing(index_elements=["playlist_id", "video_id"])
    )
    await db.commit()


async def reorder_playlist(
    db: AsyncSession, playlist_id: uuid.UUID, video_ids: list[uuid.UUID], auth: AuthContext
) -> None:
    playlist = await db.scalar(
        select(Playlist)
        .where(Playlist.id == playlist_id, Playlist.owner_id == auth.user.id)
        .with_for_update()
    )
    if playlist is None:
        raise UploadError(404, "playlist_not_found")
    existing = list(
        await db.scalars(
            select(PlaylistItem.video_id)
            .where(PlaylistItem.playlist_id == playlist_id)
            .order_by(PlaylistItem.position)
        )
    )
    if len(video_ids) != len(set(video_ids)) or set(video_ids) != set(existing):
        raise UploadError(422, "playlist_order_mismatch")
    await db.execute(
        update(PlaylistItem)
        .where(PlaylistItem.playlist_id == playlist_id)
        .values(position=PlaylistItem.position + len(existing) + 1)
    )
    for position, video_id in enumerate(video_ids):
        await db.execute(
            update(PlaylistItem)
            .where(PlaylistItem.playlist_id == playlist_id, PlaylistItem.video_id == video_id)
            .values(position=position)
        )
    await db.commit()


async def remove_playlist_video(
    db: AsyncSession, playlist_id: uuid.UUID, video_id: uuid.UUID, auth: AuthContext
) -> None:
    owned = await db.scalar(
        select(Playlist.id)
        .where(Playlist.id == playlist_id, Playlist.owner_id == auth.user.id)
        .with_for_update()
    )
    if owned is None:
        raise UploadError(404, "playlist_not_found")
    await db.execute(
        delete(PlaylistItem).where(
            PlaylistItem.playlist_id == playlist_id, PlaylistItem.video_id == video_id
        )
    )
    remaining = list(
        await db.scalars(
            select(PlaylistItem.video_id)
            .where(PlaylistItem.playlist_id == playlist_id)
            .order_by(PlaylistItem.position)
        )
    )
    for position, remaining_id in enumerate(remaining):
        await db.execute(
            update(PlaylistItem)
            .where(
                PlaylistItem.playlist_id == playlist_id,
                PlaylistItem.video_id == remaining_id,
            )
            .values(position=position)
        )
    await db.commit()


async def list_own_playlists(db: AsyncSession, auth: AuthContext) -> list[Playlist]:
    return list(
        await db.scalars(
            select(Playlist)
            .where(Playlist.owner_id == auth.user.id)
            .order_by(Playlist.created_at.desc())
        )
    )


async def view_playlist(
    db: AsyncSession, playlist_id: uuid.UUID, auth: AuthContext | None
) -> PlaylistView:
    owner_filter = Playlist.owner_id == auth.user.id if auth else false()
    row = (
        await db.execute(
            select(Playlist, Channel)
            .join(Channel, Channel.user_id == Playlist.owner_id)
            .where(
                Playlist.id == playlist_id,
                (Playlist.visibility == "public") | owner_filter,
            )
        )
    ).one_or_none()
    if row is None:
        raise UploadError(404, "playlist_not_found")
    playlist, owner_channel = row
    rows = (
        await db.execute(
            select(Video, Channel, PlaylistItem.position)
            .join(PlaylistItem, PlaylistItem.video_id == Video.id)
            .join(Channel, Channel.id == Video.channel_id)
            .where(
                PlaylistItem.playlist_id == playlist_id,
                can_view_video(auth.user.id if auth else None),
            )
            .order_by(PlaylistItem.position)
        )
    ).all()
    return PlaylistView(
        playlist=playlist,
        owner_handle=owner_channel.handle,
        owner_title=owner_channel.title,
        videos=[
            PlaylistVideo(
                id=video.id,
                title=video.title,
                channel_title=channel.title,
                channel_handle=channel.handle,
                position=position,
            )
            for video, channel, position in rows
        ],
        own=bool(auth and playlist.owner_id == auth.user.id),
    )
