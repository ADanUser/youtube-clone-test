"""SQLAlchemy 2 mappings for the PostgreSQL domain schema."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Base class with deterministic names for migration constraints."""

    metadata = MetaData(
        naming_convention={
            "ix": "ix_%(table_name)s_%(column_0_N_name)s",
            "uq": "uq_%(table_name)s_%(column_0_N_name)s",
            "ck": "ck_%(table_name)s_%(constraint_name)s",
            "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
            "pk": "pk_%(table_name)s",
        }
    )


class UUIDPrimaryKeyMixin:
    """Application-generated UUID primary key."""

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


class CreatedAtMixin:
    """UTC timestamp set by PostgreSQL."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class UpdatedAtMixin:
    """UTC timestamps for mutable rows."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class User(UUIDPrimaryKeyMixin, UpdatedAtMixin, Base):
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint("role IN ('user', 'moderator', 'admin')", name="role_allowed"),
    )

    email_normalized: Mapped[str] = mapped_column(String(254), unique=True)
    username_normalized: Mapped[str] = mapped_column(String(64), unique=True)
    password_hash: Mapped[str] = mapped_column(Text)
    role: Mapped[str] = mapped_column(String(16), server_default=text("'user'"))
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))


class Session(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    __tablename__ = "sessions"
    __table_args__ = (
        Index("ix_sessions_expires_at", "expires_at"),
        Index("ix_sessions_user_id", "user_id"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE")
    )
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    csrf_token_hash: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Channel(UUIDPrimaryKeyMixin, UpdatedAtMixin, Base):
    __tablename__ = "channels"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), unique=True
    )
    handle: Mapped[str] = mapped_column(String(64), unique=True)
    title: Mapped[str] = mapped_column(String(120))
    description: Mapped[str] = mapped_column(Text, server_default=text("''"))


class Video(UUIDPrimaryKeyMixin, UpdatedAtMixin, Base):
    __tablename__ = "videos"
    __table_args__ = (
        CheckConstraint("visibility IN ('public', 'private')", name="visibility_allowed"),
        CheckConstraint(
            "processing_state IN ('pending', 'uploading', 'processing', 'ready', 'failed')",
            name="processing_state_allowed",
        ),
        CheckConstraint(
            "moderation_state IN ('visible', 'hidden')", name="moderation_state_allowed"
        ),
        CheckConstraint("duration_ms IS NULL OR duration_ms >= 0", name="duration_nonnegative"),
        CheckConstraint("width IS NULL OR width > 0", name="width_positive"),
        CheckConstraint("height IS NULL OR height > 0", name="height_positive"),
        Index("ix_videos_channel_created_id", "channel_id", "created_at", "id"),
        Index(
            "ix_videos_public_feed",
            text("created_at DESC"),
            text("id DESC"),
            postgresql_where=text(
                "visibility = 'public' AND processing_state = 'ready' "
                "AND moderation_state = 'visible' AND deleted_at IS NULL"
            ),
        ),
        Index(
            "ix_videos_title_trgm",
            "title",
            postgresql_using="gin",
            postgresql_ops={"title": "gin_trgm_ops"},
        ),
    )

    channel_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("channels.id", ondelete="RESTRICT")
    )
    title: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, server_default=text("''"))
    visibility: Mapped[str] = mapped_column(String(8), server_default=text("'private'"))
    processing_state: Mapped[str] = mapped_column(String(16), server_default=text("'pending'"))
    moderation_state: Mapped[str] = mapped_column(String(8), server_default=text("'visible'"))
    upload_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    duration_ms: Mapped[int | None] = mapped_column(BigInteger)
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    failure_code: Mapped[str | None] = mapped_column(String(64))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class VideoAsset(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    __tablename__ = "video_assets"
    __table_args__ = (
        CheckConstraint("kind IN ('original', 'mp4', 'thumbnail')", name="kind_allowed"),
        CheckConstraint("size_bytes > 0", name="size_positive"),
        UniqueConstraint("video_id", "kind"),
    )

    video_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("videos.id", ondelete="RESTRICT")
    )
    kind: Mapped[str] = mapped_column(String(16))
    generation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    object_key: Mapped[str] = mapped_column(Text, unique=True)
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    mime_type: Mapped[str] = mapped_column(String(128))
    sha256: Mapped[str] = mapped_column(String(64))


class VideoAccessGrant(CreatedAtMixin, Base):
    __tablename__ = "video_access_grants"
    __table_args__ = (
        Index("ix_video_access_grants_user_id", "user_id"),
        Index("ix_video_access_grants_granted_by", "granted_by"),
    )

    video_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("videos.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    granted_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT")
    )


class Like(CreatedAtMixin, Base):
    __tablename__ = "likes"
    __table_args__ = (Index("ix_likes_user_id", "user_id"),)

    video_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("videos.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )


class Comment(UUIDPrimaryKeyMixin, UpdatedAtMixin, Base):
    __tablename__ = "comments"
    __table_args__ = (
        CheckConstraint("char_length(body) BETWEEN 1 AND 5000", name="body_length"),
        Index("ix_comments_video_created_id", "video_id", "created_at", "id"),
        Index("ix_comments_user_id", "user_id"),
    )

    video_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("videos.id", ondelete="RESTRICT")
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT")
    )
    body: Mapped[str] = mapped_column(Text)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Subscription(CreatedAtMixin, Base):
    __tablename__ = "subscriptions"
    __table_args__ = (Index("ix_subscriptions_channel_id", "channel_id"),)

    subscriber_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    channel_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("channels.id", ondelete="CASCADE"), primary_key=True
    )


class Playlist(UUIDPrimaryKeyMixin, UpdatedAtMixin, Base):
    __tablename__ = "playlists"
    __table_args__ = (
        CheckConstraint("visibility IN ('public', 'private')", name="visibility_allowed"),
        Index("ix_playlists_owner_created", "owner_id", "created_at"),
    )

    owner_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT")
    )
    title: Mapped[str] = mapped_column(String(200))
    visibility: Mapped[str] = mapped_column(String(8), server_default=text("'private'"))


class PlaylistItem(CreatedAtMixin, Base):
    __tablename__ = "playlist_items"
    __table_args__ = (
        CheckConstraint("position >= 0", name="position_nonnegative"),
        UniqueConstraint("playlist_id", "position"),
        Index("ix_playlist_items_video_id", "video_id"),
    )

    playlist_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("playlists.id", ondelete="CASCADE"), primary_key=True
    )
    video_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("videos.id", ondelete="RESTRICT"), primary_key=True
    )
    position: Mapped[int] = mapped_column(Integer)


class Report(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    __tablename__ = "reports"
    __table_args__ = (
        CheckConstraint("char_length(reason) BETWEEN 1 AND 2000", name="reason_length"),
        CheckConstraint("status IN ('open', 'resolved', 'rejected')", name="status_allowed"),
        CheckConstraint(
            "(status = 'open' AND resolved_by IS NULL AND resolved_at IS NULL) OR "
            "(status IN ('resolved', 'rejected') AND resolved_by IS NOT NULL "
            "AND resolved_at IS NOT NULL)",
            name="resolution_consistent",
        ),
        Index(
            "uq_reports_open_video_reporter",
            "video_id",
            "reporter_id",
            unique=True,
            postgresql_where=text("status = 'open'"),
        ),
        Index("ix_reports_status_created", "status", "created_at"),
        Index("ix_reports_video_id", "video_id"),
        Index("ix_reports_reporter_id", "reporter_id"),
        Index("ix_reports_resolved_by", "resolved_by"),
    )

    video_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("videos.id", ondelete="RESTRICT")
    )
    reporter_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT")
    )
    reason: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), server_default=text("'open'"))
    resolved_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT")
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ModerationAction(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    __tablename__ = "moderation_actions"
    __table_args__ = (
        CheckConstraint("action IN ('hide', 'restore')", name="action_allowed"),
        CheckConstraint("char_length(reason) BETWEEN 1 AND 2000", name="reason_length"),
        Index("ix_moderation_actions_video_created", "video_id", "created_at"),
        Index("ix_moderation_actions_actor_id", "actor_id"),
        Index("ix_moderation_actions_report_id", "report_id"),
    )

    video_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("videos.id", ondelete="RESTRICT")
    )
    actor_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT")
    )
    report_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("reports.id", ondelete="RESTRICT")
    )
    action: Mapped[str] = mapped_column(String(16))
    reason: Mapped[str] = mapped_column(Text)


class ProcessingJob(UUIDPrimaryKeyMixin, UpdatedAtMixin, Base):
    __tablename__ = "processing_jobs"
    __table_args__ = (
        CheckConstraint("kind IN ('transcode', 'delete', 'cleanup')", name="kind_allowed"),
        CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'failed')", name="status_allowed"
        ),
        CheckConstraint("attempts >= 0", name="attempts_nonnegative"),
        CheckConstraint("max_attempts > 0", name="max_attempts_positive"),
        CheckConstraint("attempts <= max_attempts", name="attempts_within_maximum"),
        CheckConstraint(
            "(status = 'running' AND lease_until IS NOT NULL AND lease_token IS NOT NULL) OR "
            "(status <> 'running' AND lease_until IS NULL AND lease_token IS NULL)",
            name="lease_consistent",
        ),
        Index(
            "ix_processing_jobs_queued_run_after",
            "run_after",
            postgresql_where=text("status = 'queued'"),
        ),
        Index(
            "ix_processing_jobs_running_lease_until",
            "lease_until",
            postgresql_where=text("status = 'running'"),
        ),
        Index("ix_processing_jobs_video_id", "video_id"),
    )

    video_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("videos.id", ondelete="RESTRICT")
    )
    kind: Mapped[str] = mapped_column(String(16))
    deduplication_key: Mapped[str] = mapped_column(Text, unique=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    status: Mapped[str] = mapped_column(String(16), server_default=text("'queued'"))
    attempts: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    max_attempts: Mapped[int] = mapped_column(Integer, server_default=text("3"))
    run_after: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_token: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    last_error: Mapped[str | None] = mapped_column(Text)
