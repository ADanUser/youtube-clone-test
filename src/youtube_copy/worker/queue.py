"""PostgreSQL queue: short row locks, expiring leases and generation-fenced commits."""

import uuid
from datetime import timedelta

from pydantic import ValidationError
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.sql.elements import ColumnElement

from youtube_copy.config import Settings
from youtube_copy.infrastructure.database.models import ProcessingJob, Video, VideoAsset
from youtube_copy.worker.contracts import (
    Artifact,
    Lease,
    MediaInfo,
    ProcessingError,
    Source,
    TranscodePayload,
)


class Queue:
    def __init__(self, sessions: async_sessionmaker[AsyncSession], settings: Settings) -> None:
        self.sessions = sessions
        self.settings = settings

    @staticmethod
    def clear_lease(job: ProcessingJob, status: str) -> None:
        job.status = status
        job.lease_token = None
        job.lease_until = None

    async def fail_locked(
        self, db: AsyncSession, job: ProcessingJob, code: str, retryable: bool
    ) -> None:
        job.last_error = code
        if retryable and job.attempts < job.max_attempts:
            self.clear_lease(job, "queued")
            delay = 30 if job.attempts <= 1 else 120
            job.run_after = await db.scalar(select(func.now() + timedelta(seconds=delay)))
            return
        self.clear_lease(job, "failed")
        if job.kind == "transcode":
            try:
                upload_id = TranscodePayload.model_validate(job.payload).upload_id
            except ValidationError:
                return
            await db.execute(
                update(Video)
                .where(
                    Video.id == job.video_id,
                    Video.upload_id == upload_id,
                    Video.processing_state == "processing",
                    Video.deleted_at.is_(None),
                )
                .values(processing_state="failed", failure_code=code)
            )

    async def recover(self) -> int:
        async with self.sessions.begin() as db:
            jobs = list(
                await db.scalars(
                    select(ProcessingJob)
                    .where(
                        ProcessingJob.status == "running", ProcessingJob.lease_until <= func.now()
                    )
                    .limit(100)
                    .with_for_update(skip_locked=True)
                )
            )
            for job in jobs:
                await self.fail_locked(db, job, "lease_expired", True)
            return len(jobs)

    async def claim(self) -> Lease | None:
        async with self.sessions.begin() as db:
            job = await db.scalar(
                select(ProcessingJob)
                .where(ProcessingJob.status == "queued", ProcessingJob.run_after <= func.now())
                .order_by(ProcessingJob.run_after, ProcessingJob.id)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            if job is None:
                return None
            if job.attempts >= job.max_attempts:
                await self.fail_locked(db, job, "attempts_exhausted", False)
                return None
            lease_until = await db.scalar(
                select(func.now() + timedelta(seconds=self.settings.worker_lease_seconds))
            )
            job.status = "running"
            job.attempts += 1
            job.lease_token = uuid.uuid4()
            job.lease_until = lease_until
            if not isinstance(job.payload, dict):
                await self.fail_locked(db, job, "invalid_job_payload", False)
                return None
            return Lease(job.id, job.lease_token, job.video_id, job.kind, dict(job.payload))

    async def heartbeat(self, lease: Lease) -> bool:
        async with self.sessions.begin() as db:
            found = await db.scalar(
                update(ProcessingJob)
                .where(*self.lease_filter(lease))
                .values(
                    lease_until=func.now() + timedelta(seconds=self.settings.worker_lease_seconds)
                )
                .returning(ProcessingJob.id)
            )
            return found is not None

    @staticmethod
    def lease_filter(lease: Lease) -> tuple[ColumnElement[bool], ...]:
        return (
            ProcessingJob.id == lease.id,
            ProcessingJob.status == "running",
            ProcessingJob.lease_token == lease.token,
            ProcessingJob.lease_until > func.clock_timestamp(),
        )

    async def source(self, lease: Lease) -> Source | None:
        command = TranscodePayload.model_validate(lease.payload)
        async with self.sessions() as db:
            row = (
                await db.execute(
                    select(Video, VideoAsset)
                    .join(VideoAsset, VideoAsset.video_id == Video.id)
                    .where(
                        Video.id == lease.video_id,
                        Video.upload_id == command.upload_id,
                        Video.processing_state == "processing",
                        Video.deleted_at.is_(None),
                        VideoAsset.kind == "original",
                        VideoAsset.generation_id == command.upload_id,
                    )
                )
            ).one_or_none()
            if row is None:
                # Distinguish a superseded/deleted job from a corrupt missing asset.
                current = await db.scalar(
                    select(Video.id).where(
                        Video.id == lease.video_id,
                        Video.upload_id == command.upload_id,
                        Video.processing_state == "processing",
                        Video.deleted_at.is_(None),
                    )
                )
                if current is not None:
                    raise ProcessingError("original_missing")
                return None
            video, asset = row
            expected = f"originals/{video.id}/{command.upload_id}"
            if asset.object_key != expected:
                raise ProcessingError("invalid_original_key")
            return Source(video.id, command.upload_id, expected, asset.size_bytes, asset.sha256)

    async def finish(self, lease: Lease, code: str | None = None) -> bool:
        async with self.sessions.begin() as db:
            job = await db.scalar(
                select(ProcessingJob).where(*self.lease_filter(lease)).with_for_update()
            )
            if job is None:
                return False
            self.clear_lease(job, "succeeded")
            job.last_error = code
            return True

    async def fail(self, lease: Lease, code: str, retryable: bool) -> None:
        async with self.sessions.begin() as db:
            job = await db.scalar(
                select(ProcessingJob).where(*self.lease_filter(lease)).with_for_update()
            )
            if job is not None:
                await self.fail_locked(db, job, code, retryable)

    async def publish(
        self, lease: Lease, source: Source, info: MediaInfo, artifacts: list[Artifact]
    ) -> bool:
        if {a.kind for a in artifacts} != {"mp4", "thumbnail"} or len(artifacts) != 2:
            raise ValueError("both output artifacts are required")
        async with self.sessions.begin() as db:
            job = await db.scalar(
                select(ProcessingJob).where(*self.lease_filter(lease)).with_for_update()
            )
            if job is None:
                return False
            video = await db.scalar(
                select(Video)
                .where(
                    Video.id == source.video_id,
                    Video.upload_id == source.upload_id,
                    Video.processing_state == "processing",
                    Video.deleted_at.is_(None),
                )
                .with_for_update()
            )
            if job.lease_until <= await db.scalar(select(func.clock_timestamp())):
                return False
            self.clear_lease(job, "succeeded")
            if video is None:
                job.last_error = "superseded"
                return False
            await db.execute(
                delete(VideoAsset).where(
                    VideoAsset.video_id == video.id, VideoAsset.kind.in_(("mp4", "thumbnail"))
                )
            )
            for asset in artifacts:
                db.add(
                    VideoAsset(
                        video_id=video.id,
                        generation_id=lease.token,
                        kind=asset.kind,
                        object_key=asset.object_key,
                        size_bytes=asset.size_bytes,
                        sha256=asset.sha256,
                        mime_type=asset.mime_type,
                    )
                )
            video.duration_ms = info.duration_ms
            video.width = info.width
            video.height = info.height
            video.processing_state = "ready"
            video.failure_code = None
            job.last_error = None
            return True
