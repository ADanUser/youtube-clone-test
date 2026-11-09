"""Video drafts and the upload-to-processing handoff in short database transactions."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Callable

from sqlalchemy import delete, exists, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from youtube_copy.accounts.service import AuthContext
from youtube_copy.config import Settings
from youtube_copy.infrastructure.database.models import (
    ProcessingJob,
    Session,
    User,
    Video,
    VideoAsset,
)
from youtube_copy.infrastructure.uploads import (
    WRITE_CHUNK_BYTES,
    OriginalStorage,
    UploadSlots,
    UploadSpool,
)
from youtube_copy.videos.errors import UploadError
from youtube_copy.videos.schemas import VideoCreate, VideoStatus


async def blocking[T](operation: Callable[..., T], *args: object) -> T:
    """Do not close a spool while an interrupted thread is still using it."""
    task = asyncio.create_task(asyncio.to_thread(operation, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        try:
            await task
        finally:
            raise


def owned_video(video_id: uuid.UUID, channel_id: uuid.UUID) -> tuple[ColumnElement[bool], ...]:
    return (
        Video.id == video_id,
        Video.channel_id == channel_id,
        Video.deleted_at.is_(None),
    )


async def create_video(db: AsyncSession, auth: AuthContext, command: VideoCreate) -> VideoStatus:
    video = Video(channel_id=auth.channel.id, **command.model_dump())
    db.add(video)
    await db.flush()
    result = VideoStatus.model_validate(video)
    await db.commit()
    return result


async def get_upload_status(
    db: AsyncSession, video_id: uuid.UUID, channel_id: uuid.UUID
) -> VideoStatus:
    video = await db.scalar(select(Video).where(*owned_video(video_id, channel_id)))
    if video is None:
        raise UploadError(404, "video_not_found")
    return VideoStatus.model_validate(video)


async def claim_upload(
    db: AsyncSession, video_id: uuid.UUID, channel_id: uuid.UUID, upload_id: uuid.UUID
) -> None:
    changed = await db.scalar(
        update(Video)
        .where(
            *owned_video(video_id, channel_id),
            Video.processing_state.in_(("pending", "failed")),
        )
        .values(processing_state="uploading", upload_id=upload_id, failure_code=None)
        .returning(Video.id)
    )
    if changed is None:
        await db.rollback()
        await get_upload_status(db, video_id, channel_id)
        raise UploadError(409, "upload_state_conflict")
    await db.commit()


async def finish_upload(
    db: AsyncSession,
    auth: AuthContext,
    video_id: uuid.UUID,
    upload_id: uuid.UUID,
    object_key: str,
    spool: UploadSpool,
) -> VideoStatus:
    # Check session/activity again after the long transfer, including logout/blocking.
    valid_session = exists().where(
        Session.id == auth.session.id,
        Session.user_id == User.id,
        User.id == auth.user.id,
        User.is_active.is_(True),
        Session.expires_at > func.now(),
    )
    video = await db.scalar(
        select(Video)
        .where(
            *owned_video(video_id, auth.channel.id),
            Video.upload_id == upload_id,
            Video.processing_state == "uploading",
            valid_session,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if video is None:
        raise UploadError(409, "upload_no_longer_allowed")
    # A retry from failed may replace an earlier original; old objects are reconciled
    # by step 9 after its protection period. Job generations must be fenced there.
    await db.execute(
        delete(VideoAsset).where(VideoAsset.video_id == video_id, VideoAsset.kind == "original")
    )
    db.add(
        VideoAsset(
            video_id=video_id,
            kind="original",
            generation_id=upload_id,
            object_key=object_key,
            size_bytes=spool.size,
            mime_type="application/octet-stream",
            sha256=spool.digest.hexdigest(),
        )
    )
    db.add(
        ProcessingJob(
            video_id=video_id,
            kind="transcode",
            deduplication_key=f"transcode:{video_id}:{upload_id}",
            payload={"upload_id": str(upload_id)},
        )
    )
    video.processing_state = "processing"
    await db.flush()
    await db.refresh(video)
    result = VideoStatus.model_validate(video)
    await db.commit()
    return result


async def fail_upload(
    db: AsyncSession, video_id: uuid.UUID, upload_id: uuid.UUID, code: str
) -> None:
    await db.rollback()
    await db.execute(
        update(Video)
        .where(
            Video.id == video_id,
            Video.upload_id == upload_id,
            Video.processing_state == "uploading",
        )
        .values(processing_state="failed", failure_code=code)
    )
    await db.commit()


async def receive_original(
    db: AsyncSession,
    auth: AuthContext,
    video_id: uuid.UUID,
    chunks: AsyncIterator[bytes],
    settings: Settings,
    slots: UploadSlots,
    storage: OriginalStorage,
    content_length: int | None,
) -> VideoStatus:
    await get_upload_status(db, video_id, auth.channel.id)
    # Release the auth/read transaction before filesystem or network operations.
    await db.commit()
    if content_length is not None and content_length > settings.upload_max_bytes:
        raise UploadError(413, "upload_too_large")
    upload_id = uuid.uuid4()
    spool: UploadSpool | None = None

    def acquire_spool() -> None:
        nonlocal spool
        spool = slots.acquire()

    claimed = False
    try:
        await blocking(acquire_spool)
        assert spool is not None
        await claim_upload(db, video_id, auth.channel.id, upload_id)
        claimed = True
        async with asyncio.timeout(settings.upload_timeout_seconds):
            while True:
                try:
                    chunk = await asyncio.wait_for(
                        anext(chunks), timeout=settings.upload_idle_timeout_seconds
                    )
                except StopAsyncIteration:
                    break
                if spool.size + len(chunk) > settings.upload_max_bytes:
                    raise UploadError(413, "upload_too_large")
                for offset in range(0, len(chunk), WRITE_CHUNK_BYTES):
                    await blocking(spool.write, chunk[offset : offset + WRITE_CHUNK_BYTES])
        if not spool.size:
            raise UploadError(400, "upload_empty")
        if content_length is not None and spool.size != content_length:
            raise UploadError(400, "upload_length_mismatch")
        object_key = f"originals/{video_id}/{upload_id}"
        try:
            await blocking(storage.put, object_key, spool)
        except Exception as error:
            raise UploadError(503, "upload_storage_unavailable") from error
        return await finish_upload(db, auth, video_id, upload_id, object_key, spool)
    except BaseException as error:
        code = "upload_interrupted"
        if isinstance(error, UploadError):
            code = error.code
        elif isinstance(error, TimeoutError):
            code = "upload_timeout"
        elif isinstance(error, OSError):
            code = "upload_disk_full"
        if claimed:
            # Do not delete an S3 object on an ambiguous commit result. Step 9
            # reconciles orphans; the conditional update cannot undo processing.
            cleanup = asyncio.create_task(fail_upload(db, video_id, upload_id, code))
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                await cleanup
        if isinstance(error, TimeoutError):
            raise UploadError(408, code) from error
        if isinstance(error, OSError):
            raise UploadError(507, code) from error
        raise
    finally:
        if spool is not None:
            await blocking(slots.release, spool)
