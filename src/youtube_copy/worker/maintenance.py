"""Idempotent recovery and conservative collection of old, unreferenced generations."""

import asyncio
import fcntl
import shutil
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import delete, exists, func, select, update

from youtube_copy.infrastructure.database.models import ProcessingJob, Video, VideoAsset
from youtube_copy.worker.contracts import DeletePayload, Lease, ProcessingError
from youtube_copy.worker.queue import Queue
from youtube_copy.worker.storage import Storage, managed_key


class Maintenance:
    def __init__(self, queue: Queue, storage: Storage) -> None:
        self.queue, self.storage = queue, storage

    async def protected(self, key: str) -> bool:
        parsed = managed_key(key)
        if parsed is None:
            return True
        video_id, _generation = parsed
        async with self.queue.sessions() as db:
            referenced = await db.scalar(select(exists().where(VideoAsset.object_key == key)))
            busy = await db.scalar(
                select(
                    exists().where(
                        Video.id == video_id,
                        Video.processing_state.in_(("uploading", "processing")),
                    )
                )
            )
            leased = await db.scalar(
                select(
                    exists().where(
                        ProcessingJob.video_id == video_id,
                        ProcessingJob.status == "running",
                        ProcessingJob.lease_until > func.now(),
                    )
                )
            )
            return bool(referenced or busy or leased)

    async def sweep(self) -> None:
        cutoff = datetime.now(UTC) - timedelta(
            seconds=self.queue.settings.worker_orphan_grace_seconds
        )
        # Reject publication of expired uploads before touching their potential S3 objects.
        async with self.queue.sessions.begin() as db:
            await db.execute(
                update(Video)
                .where(
                    Video.processing_state == "uploading",
                    Video.updated_at < cutoff,
                )
                .values(processing_state="failed", failure_code="upload_expired")
            )
        for prefix in ("originals/", "processed/", "thumbnails/"):
            token = None
            while True:
                page = await asyncio.to_thread(self.storage.objects, prefix, token)
                for item in page.get("Contents", []):
                    if item["LastModified"] < cutoff and not await self.protected(item["Key"]):
                        await asyncio.to_thread(self.storage.delete, item["Key"])
                if not page.get("IsTruncated"):
                    break
                token = page["NextContinuationToken"]
        key_marker = upload_marker = None
        while True:
            page = await asyncio.to_thread(self.storage.multiparts, key_marker, upload_marker)
            for item in page.get("Uploads", []):
                if item["Initiated"] < cutoff and not await self.protected(item["Key"]):
                    await asyncio.to_thread(self.storage.abort, item["Key"], item["UploadId"])
            if not page.get("IsTruncated"):
                break
            key_marker, upload_marker = page["NextKeyMarker"], page["NextUploadIdMarker"]
        await self.clean_directories(cutoff.timestamp())

    async def clean_directories(self, cutoff: float) -> None:
        root = self.queue.settings.worker_temp_dir
        if not root.exists():
            return
        for path in root.iterdir():
            if path.is_symlink() or not path.is_dir() or not path.name.startswith("job-"):
                continue
            try:
                token = uuid.UUID(path.name[4:])
                if path.stat().st_mtime >= cutoff:
                    continue
            except (ValueError, FileNotFoundError):
                continue
            async with self.queue.sessions() as db:
                active = await db.scalar(
                    select(
                        exists().where(
                            ProcessingJob.lease_token == token,
                            ProcessingJob.status == "running",
                            ProcessingJob.lease_until > func.now(),
                        )
                    )
                )
            if not active:
                await asyncio.to_thread(self.remove_unlocked, path)

    @staticmethod
    def remove_unlocked(path: Path) -> None:
        try:
            with (path / ".lock").open("a") as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    return
                shutil.rmtree(path)
        except FileNotFoundError:
            pass

    async def delete_video(self, lease: Lease) -> None:
        command = DeletePayload.model_validate(lease.payload)
        if lease.video_id is None or any(
            managed_key(key) is None or managed_key(key)[0] != lease.video_id
            for key in command.keys
        ):
            raise ProcessingError("invalid_delete_key")
        async with self.queue.sessions() as db:
            deleted = await db.scalar(
                select(Video.id).where(Video.id == lease.video_id, Video.deleted_at.is_not(None))
            )
        if deleted is None:
            raise ProcessingError("video_not_deleted")
        for key in command.keys:
            if not await self.queue.heartbeat(lease):
                return
            await asyncio.to_thread(self.storage.delete, key)
        async with self.queue.sessions.begin() as db:
            job = await db.scalar(
                select(ProcessingJob).where(*self.queue.lease_filter(lease)).with_for_update()
            )
            if job is None:
                return
            await db.execute(
                delete(VideoAsset).where(
                    VideoAsset.video_id == lease.video_id, VideoAsset.object_key.in_(command.keys)
                )
            )
            self.queue.clear_lease(job, "succeeded")
