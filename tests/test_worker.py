"""Real PostgreSQL queue races and Garage/FFmpeg pipeline integration."""

import asyncio
import hashlib
import os
import threading
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
import pytest_asyncio
from botocore.exceptions import EndpointConnectionError
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from test_accounts_api import ApiContext
from test_accounts_api import api_context as api_context
from test_uploads import draft, headers, state
from test_uploads import upload_api as upload_api
from test_worker_media import make_video
from youtube_copy.config import Settings, get_settings
from youtube_copy.infrastructure.database.models import (
    Channel,
    ProcessingJob,
    User,
    Video,
    VideoAsset,
)
from youtube_copy.worker.contracts import Artifact, LeaseLostError, MediaInfo, ProcessingError
from youtube_copy.worker.main import Worker
from youtube_copy.worker.queue import Queue
from youtube_copy.worker.storage import Storage

pytestmark = pytest.mark.postgres


async def test_api_upload_reaches_ready(
    worker_context: "Context", upload_api: ApiContext, tmp_path: Path
) -> None:
    ctx = worker_context
    path = tmp_path / "api.mp4"
    make_video(path)
    upload_api.settings.upload_max_bytes = 1024 * 1024
    video_id = await draft(upload_api)
    ctx.videos.append(uuid.UUID(video_id))
    response = await upload_api.client.post(
        f"/api/v1/videos/{video_id}/upload", headers=headers(upload_api), content=path.read_bytes()
    )
    assert response.status_code == 202
    assert (await state(upload_api, video_id))["processing_state"] == "processing"
    assert await ctx.worker.run_once()
    assert (await state(upload_api, video_id))["processing_state"] == "ready"


@dataclass
class Context:
    queue: Queue
    storage: Storage
    worker: Worker
    settings: Settings
    sessions: async_sessionmaker[AsyncSession]
    channel_id: uuid.UUID
    videos: list[uuid.UUID] = field(default_factory=list)

    async def enqueue(self, path: Path) -> uuid.UUID:
        video_id, upload_id = uuid.uuid4(), uuid.uuid4()
        key = f"originals/{video_id}/{upload_id}"
        content = path.read_bytes()
        await asyncio.to_thread(
            self.storage.client.put_object, Bucket=self.storage.bucket, Key=key, Body=content
        )
        async with self.sessions.begin() as db:
            db.add(
                Video(
                    id=video_id,
                    channel_id=self.channel_id,
                    title="worker test",
                    upload_id=upload_id,
                    processing_state="processing",
                )
            )
            await db.flush()
            db.add(
                VideoAsset(
                    video_id=video_id,
                    kind="original",
                    generation_id=upload_id,
                    object_key=key,
                    size_bytes=len(content),
                    mime_type="application/octet-stream",
                    sha256=hashlib.sha256(content).hexdigest(),
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
        self.videos.append(video_id)
        return video_id

    async def status(self, video_id: uuid.UUID) -> tuple[str, str | None, str, int]:
        async with self.sessions() as db:
            return tuple(
                (
                    await db.execute(
                        select(
                            Video.processing_state,
                            Video.failure_code,
                            ProcessingJob.status,
                            ProcessingJob.attempts,
                        )
                        .join(ProcessingJob, ProcessingJob.video_id == Video.id)
                        .where(Video.id == video_id, ProcessingJob.kind == "transcode")
                    )
                ).one()
            )


@pytest_asyncio.fixture
async def worker_context(migrated_database_url: str, tmp_path: Path) -> AsyncIterator[Context]:
    if os.environ.get("YOUTUBE_RUN_S3_TESTS") != "1":
        pytest.skip("requires real Garage and FFmpeg test image")
    settings = get_settings().model_copy(
        update={
            "worker_temp_dir": tmp_path / "work",
            "worker_disk_reserve_bytes": 0,
            "worker_max_output_bytes": 20 * 1024 * 1024,
            "worker_heartbeat_seconds": 0.2,
            "worker_lease_seconds": 10,
        }
    )
    engine = create_async_engine(migrated_database_url, poolclass=NullPool)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions.begin() as db:
        # Fixture DB is explicitly disposable. Prevent jobs from earlier API tests
        # from being consumed by the worker under test.
        await db.execute(
            update(ProcessingJob).values(status="failed", lease_token=None, lease_until=None)
        )
        user_id, channel_id = uuid.uuid4(), uuid.uuid4()
        db.add(
            User(
                id=user_id,
                email_normalized=f"{user_id}@example.test",
                username_normalized=str(user_id),
                password_hash="test-only",
            )
        )
        await db.flush()
        db.add(Channel(id=channel_id, user_id=user_id, handle=str(channel_id), title="Worker test"))
    storage = Storage(settings)
    queue = Queue(sessions, settings)
    context = Context(
        queue, storage, Worker(queue, storage, settings), settings, sessions, channel_id
    )
    try:
        yield context
    finally:
        for video_id in context.videos:
            for prefix in ("originals", "processed", "thumbnails"):
                page = await asyncio.to_thread(storage.objects, f"{prefix}/{video_id}/")
                for obj in page.get("Contents", []):
                    await asyncio.to_thread(storage.delete, obj["Key"])
        storage.close()
        await engine.dispose()


async def test_worker_publishes_both_assets_atomically(
    worker_context: Context, tmp_path: Path
) -> None:
    ctx = worker_context
    original = tmp_path / "video.mkv"
    make_video(original)
    video_id = await ctx.enqueue(original)
    assert await ctx.worker.run_once()
    assert await ctx.status(video_id) == ("ready", None, "succeeded", 1)
    async with ctx.sessions() as db:
        assets = list(await db.scalars(select(VideoAsset).where(VideoAsset.video_id == video_id)))
        video = await db.get(Video, video_id)
        assert (video.width, video.height) == (320, 180)
        assert video.visibility == "private" and video.moderation_state == "visible"
    assert {asset.kind for asset in assets} == {"original", "mp4", "thumbnail"}
    for asset in assets:
        obj = ctx.storage.client.get_object(Bucket=ctx.storage.bucket, Key=asset.object_key)
        try:
            data = obj["Body"].read()
        finally:
            obj["Body"].close()
        assert len(data) == asset.size_bytes
        assert hashlib.sha256(data).hexdigest() == asset.sha256
    assert not list(ctx.settings.worker_temp_dir.glob("job-*"))
    assert not await ctx.worker.run_once()


async def test_bad_media_fails_without_retry(worker_context: Context, tmp_path: Path) -> None:
    path = tmp_path / "fake.mp4"
    path.write_bytes(b"fake MIME and extension are not video")
    video_id = await worker_context.enqueue(path)
    await worker_context.worker.run_once()
    assert await worker_context.status(video_id) == ("failed", "invalid_media", "failed", 1)


async def test_skip_locked_and_no_duplicate_claim(worker_context: Context, tmp_path: Path) -> None:
    ctx = worker_context
    path = tmp_path / "source"
    path.write_bytes(b"test")
    first, second = await ctx.enqueue(path), await ctx.enqueue(path)
    async with ctx.sessions.begin() as locked:
        await locked.scalar(
            select(ProcessingJob).where(ProcessingJob.video_id == first).with_for_update()
        )
        lease = await asyncio.wait_for(ctx.queue.claim(), 3)
        assert lease.video_id == second
    leases = await asyncio.gather(ctx.queue.claim(), ctx.queue.claim())
    assert sum(item is not None for item in leases) == 1
    assert next(item for item in leases if item is not None).video_id == first


async def test_expiry_backoff_attempt_limit_and_stale_publish(
    worker_context: Context, tmp_path: Path
) -> None:
    ctx = worker_context
    path = tmp_path / "source"
    path.write_bytes(b"test")
    video_id = await ctx.enqueue(path)
    first = await ctx.queue.claim()
    source = await ctx.queue.source(first)
    assert await ctx.queue.heartbeat(first)
    for attempt in range(1, 4):
        async with ctx.sessions.begin() as db:
            await db.execute(
                update(ProcessingJob)
                .where(ProcessingJob.video_id == video_id)
                .values(lease_until=func.now() - timedelta(seconds=1))
            )
        assert not await ctx.queue.heartbeat(first)
        assert await ctx.queue.recover() == 1
        status = await ctx.status(video_id)
        assert status[3] == attempt
        if attempt < 3:
            assert status[:3] == ("processing", None, "queued")
            async with ctx.sessions.begin() as db:
                job = await db.scalar(
                    select(ProcessingJob).where(ProcessingJob.video_id == video_id)
                )
                assert job.run_after > datetime.now(UTC) + timedelta(
                    seconds=20 if attempt == 1 else 100
                )
                job.run_after = datetime.now(UTC) - timedelta(seconds=1)
            newer = await ctx.queue.claim()
            assert newer.token != first.token
        else:
            assert status == ("failed", "lease_expired", "failed", 3)
    assets = [
        Artifact("mp4", f"processed/{video_id}/{first.token}.mp4", 1, "0" * 64, "video/mp4"),
        Artifact(
            "thumbnail", f"thumbnails/{video_id}/{first.token}.jpg", 1, "0" * 64, "image/jpeg"
        ),
    ]
    assert not await ctx.queue.publish(first, source, MediaInfo(1000, 320, 180), assets)


async def test_storage_failure_retries_without_ready(
    worker_context: Context, tmp_path: Path
) -> None:
    ctx = worker_context
    path = tmp_path / "source"
    path.write_bytes(b"test")
    video_id = await ctx.enqueue(path)
    with patch.object(
        ctx.storage, "download", side_effect=EndpointConnectionError(endpoint_url="http://test")
    ):
        await ctx.worker.run_once()
    assert await ctx.status(video_id) == ("processing", None, "queued", 1)
    assert not list(ctx.settings.worker_temp_dir.glob("job-*"))


async def test_heartbeat_continues_while_pipeline_runs(
    worker_context: Context, tmp_path: Path
) -> None:
    ctx = worker_context
    path = tmp_path / "source"
    path.write_bytes(b"test")
    video_id = await ctx.enqueue(path)
    entered, release = threading.Event(), threading.Event()

    def wait_pipeline(*_args):
        entered.set()
        release.wait(10)
        raise ProcessingError("test_end")

    with patch.object(ctx.worker, "pipeline", wait_pipeline):
        task = asyncio.create_task(ctx.worker.run_once())
        assert await asyncio.to_thread(entered.wait, 5)
        try:
            async with ctx.sessions() as db:
                initial = await db.scalar(
                    select(ProcessingJob.lease_until).where(ProcessingJob.video_id == video_id)
                )
            await asyncio.sleep(0.6)
            async with ctx.sessions() as db:
                extended = await db.scalar(
                    select(ProcessingJob.lease_until).where(ProcessingJob.video_id == video_id)
                )
            assert extended > initial
            assert not await ctx.queue.recover()
        finally:
            release.set()
            await task


async def test_superseded_generation_cannot_publish(
    worker_context: Context, tmp_path: Path
) -> None:
    ctx = worker_context
    path = tmp_path / "source"
    path.write_bytes(b"test")
    video_id = await ctx.enqueue(path)
    lease = await ctx.queue.claim()
    source = await ctx.queue.source(lease)
    async with ctx.sessions.begin() as db:
        await db.execute(update(Video).where(Video.id == video_id).values(upload_id=uuid.uuid4()))
    assets = [
        Artifact("mp4", "unused", 1, "0" * 64, "video/mp4"),
        Artifact("thumbnail", "unused2", 1, "0" * 64, "image/jpeg"),
    ]
    assert not await ctx.queue.publish(lease, source, MediaInfo(1000, 320, 180), assets)
    assert (await ctx.status(video_id))[0] == "processing"
    async with ctx.sessions() as db:
        assert (
            len(list(await db.scalars(select(VideoAsset).where(VideoAsset.video_id == video_id))))
            == 1
        )


async def test_partial_s3_output_never_becomes_ready(
    worker_context: Context, tmp_path: Path
) -> None:
    ctx = worker_context
    path = tmp_path / "input.mp4"
    make_video(path)
    video_id = await ctx.enqueue(path)
    upload = ctx.storage.upload

    def fail_thumbnail(path, key, kind, stop, deadline):
        if kind == "thumbnail":
            raise EndpointConnectionError(endpoint_url="http://test")
        return upload(path, key, kind, stop, deadline)

    with patch.object(ctx.storage, "upload", fail_thumbnail):
        await ctx.worker.run_once()
    assert await ctx.status(video_id) == ("processing", None, "queued", 1)
    async with ctx.sessions.begin() as db:
        assets = list(await db.scalars(select(VideoAsset).where(VideoAsset.video_id == video_id)))
        assert [asset.kind for asset in assets] == ["original"]
        await db.execute(
            update(ProcessingJob)
            .where(ProcessingJob.video_id == video_id)
            .values(run_after=func.now())
        )
    await ctx.worker.run_once()
    assert await ctx.status(video_id) == ("ready", None, "succeeded", 2)
    outputs = ctx.storage.objects(f"processed/{video_id}/")["Contents"]
    assert len(outputs) == 2  # Old attempt is isolated and left for protected-period cleanup.


async def test_stop_then_recover_and_finish(worker_context: Context, tmp_path: Path) -> None:
    ctx = worker_context
    path = tmp_path / "input.mp4"
    make_video(path)
    video_id = await ctx.enqueue(path)
    entered = threading.Event()

    def stopped_pipeline(lease, source, stop):
        entered.set()
        assert stop.wait(5)
        raise LeaseLostError()

    with patch.object(ctx.worker, "pipeline", stopped_pipeline):
        task = asyncio.create_task(ctx.worker.run_once())
        assert await asyncio.to_thread(entered.wait, 5)
        ctx.worker.stop()
        await task
    assert await ctx.status(video_id) == ("processing", None, "running", 1)
    async with ctx.sessions.begin() as db:
        await db.execute(
            update(ProcessingJob)
            .where(ProcessingJob.video_id == video_id)
            .values(lease_until=func.now() - timedelta(seconds=1))
        )
    restarted = Worker(ctx.queue, ctx.storage, ctx.settings)
    await ctx.queue.recover()
    async with ctx.sessions.begin() as db:
        await db.execute(
            update(ProcessingJob)
            .where(ProcessingJob.video_id == video_id)
            .values(run_after=func.now())
        )
    await restarted.run_once()
    assert await ctx.status(video_id) == ("ready", None, "succeeded", 2)


async def test_cleanup_protects_references_leases_and_recent_objects(
    worker_context: Context, tmp_path: Path
) -> None:
    ctx = worker_context
    source = tmp_path / "input"
    source.write_bytes(b"test")
    video_id = await ctx.enqueue(source)
    lease = await ctx.queue.claim()
    original = await ctx.queue.source(lease)
    orphan_video = uuid.uuid4()
    ctx.videos.append(orphan_video)
    old_key = f"processed/{orphan_video}/{uuid.uuid4()}.mp4"
    recent_key = f"processed/{orphan_video}/{uuid.uuid4()}.mp4"
    leased_key = f"processed/{video_id}/{uuid.uuid4()}.mp4"
    for key in (old_key, recent_key, leased_key):
        ctx.storage.client.put_object(Bucket=ctx.storage.bucket, Key=key, Body=b"output")
    old = datetime.now(UTC) - timedelta(days=2)
    recent = datetime.now(UTC)
    pages = {
        "originals/": [{"Key": original.object_key, "LastModified": old}],
        "processed/": [
            {"Key": old_key, "LastModified": old},
            {"Key": recent_key, "LastModified": recent},
            {"Key": leased_key, "LastModified": old},
        ],
        "thumbnails/": [],
    }
    multipart_key = f"originals/{orphan_video}/{uuid.uuid4()}"
    multipart = ctx.storage.client.create_multipart_upload(
        Bucket=ctx.storage.bucket, Key=multipart_key
    )
    with (
        patch.object(
            ctx.storage, "objects", side_effect=lambda prefix, token: {"Contents": pages[prefix]}
        ),
        patch.object(
            ctx.storage,
            "multiparts",
            return_value={
                "Uploads": [
                    {
                        "Key": multipart_key,
                        "UploadId": multipart["UploadId"],
                        "Initiated": old,
                    }
                ]
            },
        ),
    ):
        await ctx.worker.maintenance.sweep()
    keys = {item["Key"] for item in ctx.storage.objects(f"processed/{orphan_video}/")["Contents"]}
    assert old_key not in keys and recent_key in keys
    assert (
        ctx.storage.client.head_object(Bucket=ctx.storage.bucket, Key=leased_key)["ContentLength"]
        == 6
    )
    assert not any(
        item["UploadId"] == multipart["UploadId"]
        for item in ctx.storage.multiparts().get("Uploads", [])
    )


async def test_expired_upload_and_local_directory_recovery(
    worker_context: Context, tmp_path: Path
) -> None:
    import fcntl

    ctx = worker_context
    path = tmp_path / "input"
    path.write_bytes(b"test")
    video_id = await ctx.enqueue(path)
    old = datetime.now(UTC) - timedelta(days=2)
    async with ctx.sessions.begin() as db:
        await db.execute(
            update(Video)
            .where(Video.id == video_id)
            .values(processing_state="uploading", updated_at=old)
        )
    root = ctx.settings.worker_temp_dir
    root.mkdir()
    stale, active = root / f"job-{uuid.uuid4()}", root / f"job-{uuid.uuid4()}"
    for directory in (stale, active):
        directory.mkdir()
        (directory / ".lock").touch()
        os.utime(directory, (old.timestamp(), old.timestamp()))
    with (active / ".lock").open("r") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        with (
            patch.object(ctx.storage, "objects", return_value={}),
            patch.object(ctx.storage, "multiparts", return_value={}),
        ):
            await ctx.worker.maintenance.sweep()
        assert active.exists()
    assert not stale.exists()
    assert (await ctx.status(video_id))[:2] == ("failed", "upload_expired")


async def test_delete_is_idempotent_and_cannot_touch_foreign_video(
    worker_context: Context, tmp_path: Path
) -> None:
    ctx = worker_context
    path = tmp_path / "input"
    path.write_bytes(b"test")
    first, foreign = await ctx.enqueue(path), await ctx.enqueue(path)
    async with ctx.sessions.begin() as db:
        first_key = await db.scalar(
            select(VideoAsset.object_key).where(VideoAsset.video_id == first)
        )
        foreign_key = await db.scalar(
            select(VideoAsset.object_key).where(VideoAsset.video_id == foreign)
        )
        await db.execute(update(ProcessingJob).values(status="succeeded"))
        await db.execute(update(Video).where(Video.id == first).values(deleted_at=func.now()))
        db.add(
            ProcessingJob(
                video_id=first,
                kind="delete",
                deduplication_key=f"delete-bad:{first}",
                payload={"keys": [foreign_key]},
            )
        )
    await ctx.worker.run_once()
    assert (
        ctx.storage.client.head_object(Bucket=ctx.storage.bucket, Key=foreign_key)["ContentLength"]
        == 4
    )
    async with ctx.sessions.begin() as db:
        bad = await db.scalar(
            select(ProcessingJob).where(ProcessingJob.deduplication_key == f"delete-bad:{first}")
        )
        assert bad.status == "failed" and bad.last_error == "invalid_delete_key"
        db.add(
            ProcessingJob(
                video_id=first,
                kind="delete",
                deduplication_key=f"delete-good:{first}",
                payload={"keys": [first_key]},
            )
        )
    # Simulate a previous attempt removing the object before its database commit.
    ctx.storage.delete(first_key)
    await ctx.worker.run_once()
    async with ctx.sessions() as db:
        assert not await db.scalar(select(VideoAsset.id).where(VideoAsset.video_id == first))
        assert await db.scalar(select(Video.id).where(Video.id == first))  # audit remains


@pytest.mark.parametrize("after_commit", [False, True])
async def test_commit_failure_does_not_delete_outputs(
    worker_context: Context, tmp_path: Path, after_commit: bool
) -> None:
    from sqlalchemy import event
    from sqlalchemy.exc import SQLAlchemyError
    from sqlalchemy.orm import Session as SyncSession

    ctx = worker_context
    path = tmp_path / "input.mp4"
    make_video(path)
    video_id = await ctx.enqueue(path)
    publish = ctx.queue.publish

    def reject_outputs(session):
        if any(isinstance(obj, VideoAsset) and obj.kind == "mp4" for obj in session.new):
            raise SQLAlchemyError("injected commit failure")

    async def ambiguous(*args):
        result = await publish(*args)
        assert result
        raise SQLAlchemyError("injected lost acknowledgement")

    if after_commit:
        with patch.object(ctx.queue, "publish", ambiguous):
            await ctx.worker.run_once()
        assert await ctx.status(video_id) == ("ready", None, "succeeded", 1)
    else:
        event.listen(SyncSession, "before_commit", reject_outputs)
        try:
            await ctx.worker.run_once()
        finally:
            event.remove(SyncSession, "before_commit", reject_outputs)
        assert await ctx.status(video_id) == ("processing", None, "running", 1)
        async with ctx.sessions() as db:
            assert not await db.scalar(
                select(VideoAsset.id).where(
                    VideoAsset.video_id == video_id, VideoAsset.kind == "mp4"
                )
            )
    assert len(ctx.storage.objects(f"processed/{video_id}/")["Contents"]) == 1
    assert len(ctx.storage.objects(f"thumbnails/{video_id}/")["Contents"]) == 1


@pytest.mark.parametrize("fault", ["size", "checksum", "disk"])
async def test_worker_resource_and_integrity_checks(
    worker_context: Context, tmp_path: Path, fault: str
) -> None:
    ctx = worker_context
    path = tmp_path / "input.mp4"
    make_video(path)
    video_id = await ctx.enqueue(path)
    if fault == "disk":
        ctx.settings.worker_disk_reserve_bytes = 10**18
    else:
        async with ctx.sessions.begin() as db:
            values = {"size_bytes": 1} if fault == "size" else {"sha256": "0" * 64}
            await db.execute(
                update(VideoAsset).where(VideoAsset.video_id == video_id).values(**values)
            )
    await ctx.worker.run_once()
    expected = {
        "size": ("failed", "original_size_invalid", "failed", 1),
        "checksum": ("failed", "original_checksum_mismatch", "failed", 1),
        "disk": ("processing", None, "queued", 1),
    }
    assert await ctx.status(video_id) == expected[fault]
    assert not list(ctx.settings.worker_temp_dir.glob("job-*"))
