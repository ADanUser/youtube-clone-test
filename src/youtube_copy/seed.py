"""Idempotent demo data creation through the real media processing queue."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import secrets
import string
import subprocess
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path

from boto3.s3.transfer import TransferConfig
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from youtube_copy.accounts.security import hash_password
from youtube_copy.config import Settings, get_settings
from youtube_copy.infrastructure.database.models import (
    Channel,
    Comment,
    Like,
    Playlist,
    PlaylistItem,
    ProcessingJob,
    Report,
    Subscription,
    User,
    Video,
    VideoAccessGrant,
    VideoAsset,
)
from youtube_copy.worker.storage import Storage

logger = logging.getLogger(__name__)
NAMESPACE = uuid.UUID("538aa86d-8f09-4bbf-b43b-44434db9734c")


def stable_id(name: str) -> uuid.UUID:
    return uuid.uuid5(NAMESPACE, name)


@dataclass(frozen=True)
class DemoAccount:
    key: str
    email: str
    username: str
    title: str
    role: str


@dataclass(frozen=True)
class DemoVideo:
    key: str
    owner: str
    title: str
    visibility: str
    hidden: bool
    hue: int


ACCOUNTS = (
    DemoAccount("admin", "admin@demo.local", "demo-admin", "Demo Admin", "admin"),
    DemoAccount(
        "moderator", "moderator@demo.local", "demo-moderator", "Demo Moderator", "moderator"
    ),
    DemoAccount("creator", "creator@demo.local", "demo-creator", "Demo Creator", "user"),
    DemoAccount("viewer", "viewer@demo.local", "demo-viewer", "Demo Viewer", "user"),
)
VIDEOS = (
    DemoVideo("public", "creator", "Demo: public video", "public", False, 20),
    DemoVideo("private", "creator", "Demo: private video", "private", False, 120),
    DemoVideo("hidden", "creator", "Demo: hidden by moderation", "public", True, 220),
)


def _new_password(account: DemoAccount) -> str:
    alphabet = string.ascii_letters + string.digits
    suffix = "".join(secrets.choice(alphabet) for _ in range(12))
    return f"Demo-{account.role.title()}-{suffix}!"


def _read_credentials(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    result: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#") or "=" not in line:
            continue
        username, password = line.split("=", 1)
        result[username] = password
    return result


def _write_credentials(path: Path, credentials: dict[str, str]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    content = "# Demo logins. Keep this file private; it is not tracked by Git.\n"
    content += "".join(f"{name}={credentials[name]}\n" for name in sorted(credentials))
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
        path.chmod(0o600)
    finally:
        temporary.unlink(missing_ok=True)


async def ensure_accounts(db: AsyncSession, credentials_path: Path) -> dict[str, User]:
    credentials = await asyncio.to_thread(_read_credentials, credentials_path)
    changed_credentials = False
    users: dict[str, User] = {}
    for account in ACCOUNTS:
        user = await db.scalar(select(User).where(User.username_normalized == account.username))
        if user is None:
            password = _new_password(account)
            user = User(
                id=stable_id(f"user:{account.key}"),
                email_normalized=account.email,
                username_normalized=account.username,
                password_hash=await hash_password(password),
                role=account.role,
                is_active=True,
            )
            db.add(user)
            await db.flush()
            credentials[account.username] = password
            changed_credentials = True
        channel = await db.scalar(select(Channel).where(Channel.user_id == user.id))
        if channel is None:
            db.add(
                Channel(
                    id=stable_id(f"channel:{account.key}"),
                    user_id=user.id,
                    handle=account.username,
                    title=account.title,
                    description="Test channel created by the idempotent demo seed.",
                )
            )
        users[account.key] = user
    await db.commit()
    if changed_credentials:
        await asyncio.to_thread(_write_credentials, credentials_path, credentials)
    return users


def generate_video(path: Path, hue: int) -> None:
    command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-f",
        "lavfi",
        "-i",
        f"testsrc2=size=640x360:rate=24:duration=3,hue=h={hue}",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=660:sample_rate=48000:duration=3",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-shortest",
        "-movflags",
        "+faststart",
        str(path),
    ]
    subprocess.run(command, check=True, timeout=60)


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        while chunk := file.read(256 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def upload_original(storage: Storage, path: Path, key: str, digest: str) -> None:
    storage.client.upload_file(
        str(path),
        storage.bucket,
        key,
        ExtraArgs={"ContentType": "video/mp4", "Metadata": {"sha256": digest}},
        Config=TransferConfig(use_threads=False),
    )
    head = storage.client.head_object(Bucket=storage.bucket, Key=key)
    if head["ContentLength"] != path.stat().st_size:
        raise RuntimeError("seed original verification failed")


async def ensure_video_jobs(
    db: AsyncSession, storage: Storage, users: dict[str, User]
) -> dict[str, Video]:
    channels = {
        key: await db.scalar(select(Channel).where(Channel.user_id == user.id))
        for key, user in users.items()
    }
    videos: dict[str, Video] = {}
    with tempfile.TemporaryDirectory(prefix="youtube-seed-") as directory:
        for definition in VIDEOS:
            video_id = stable_id(f"video:{definition.key}")
            video = await db.get(Video, video_id)
            if video is None:
                video = Video(
                    id=video_id,
                    channel_id=channels[definition.owner].id,  # type: ignore[union-attr]
                    title=definition.title,
                    description="Locally generated test video for the demo environment.",
                    visibility=definition.visibility,
                    moderation_state="visible",
                )
                db.add(video)
                await db.commit()
            videos[definition.key] = video
            if video.processing_state in ("processing", "ready"):
                continue
            generation_id = stable_id(f"generation:{definition.key}")
            path = Path(directory) / f"{definition.key}.mp4"
            await asyncio.to_thread(generate_video, path, definition.hue)
            digest = await asyncio.to_thread(file_sha256, path)
            object_key = f"originals/{video.id}/{generation_id}"
            await asyncio.to_thread(upload_original, storage, path, object_key, digest)
            await db.execute(
                insert(VideoAsset)
                .values(
                    id=stable_id(f"asset:original:{definition.key}"),
                    video_id=video.id,
                    kind="original",
                    generation_id=generation_id,
                    object_key=object_key,
                    size_bytes=path.stat().st_size,
                    mime_type="video/mp4",
                    sha256=digest,
                )
                .on_conflict_do_update(
                    index_elements=["video_id", "kind"],
                    set_={
                        "generation_id": generation_id,
                        "object_key": object_key,
                        "size_bytes": path.stat().st_size,
                        "mime_type": "video/mp4",
                        "sha256": digest,
                    },
                )
            )
            await db.execute(
                insert(ProcessingJob)
                .values(
                    id=stable_id(f"job:{definition.key}"),
                    video_id=video.id,
                    kind="transcode",
                    deduplication_key=f"seed:transcode:{video.id}",
                    payload={"upload_id": str(generation_id)},
                )
                .on_conflict_do_update(
                    index_elements=["deduplication_key"],
                    set_={
                        "status": "queued",
                        "attempts": 0,
                        "run_after": func.now(),
                        "lease_until": None,
                        "lease_token": None,
                        "last_error": None,
                        "payload": {"upload_id": str(generation_id)},
                    },
                    where=ProcessingJob.status == "failed",
                )
            )
            video.upload_id = generation_id
            video.processing_state = "processing"
            video.failure_code = None
            await db.commit()
    return videos


async def wait_until_ready(
    sessions: async_sessionmaker[AsyncSession], video_ids: list[uuid.UUID], timeout: int
) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        async with sessions() as db:
            rows = (
                await db.execute(
                    select(Video.id, Video.processing_state, Video.failure_code).where(
                        Video.id.in_(video_ids)
                    )
                )
            ).all()
        failed = [(video_id, code) for video_id, state, code in rows if state == "failed"]
        if failed:
            raise RuntimeError(f"demo video processing failed: {failed}")
        if len(rows) == len(video_ids) and all(state == "ready" for _, state, _ in rows):
            return
        if asyncio.get_running_loop().time() >= deadline:
            raise TimeoutError("worker did not finish demo videos before the seed timeout")
        await asyncio.sleep(2)


async def ensure_social_data(
    db: AsyncSession, users: dict[str, User], videos: dict[str, Video]
) -> None:
    channels = {
        key: await db.scalar(select(Channel).where(Channel.user_id == user.id))
        for key, user in users.items()
    }
    loaded = {key: await db.get(Video, video.id) for key, video in videos.items()}
    if any(video is None for video in loaded.values()):
        raise RuntimeError("a ready demo video disappeared before social data creation")
    public, private, hidden = loaded["public"], loaded["private"], loaded["hidden"]
    assert public is not None and private is not None and hidden is not None
    public.visibility, public.moderation_state = "public", "visible"
    private.visibility, private.moderation_state = "private", "visible"
    hidden.visibility, hidden.moderation_state = "public", "hidden"
    statements = [
        insert(VideoAccessGrant)
        .values(video_id=private.id, user_id=users["viewer"].id, granted_by=users["creator"].id)
        .on_conflict_do_nothing(),
        insert(Like)
        .values(video_id=public.id, user_id=users["viewer"].id)
        .on_conflict_do_nothing(),
        insert(Like)
        .values(video_id=public.id, user_id=users["moderator"].id)
        .on_conflict_do_nothing(),
        insert(Subscription)
        .values(subscriber_id=users["viewer"].id, channel_id=channels["creator"].id)
        .on_conflict_do_nothing(),
        insert(Comment)
        .values(
            id=stable_id("comment:public:viewer"),
            video_id=public.id,
            user_id=users["viewer"].id,
            body="A demo comment created by the seed script.",
        )
        .on_conflict_do_nothing(),
        insert(Comment)
        .values(
            id=stable_id("comment:private:viewer"),
            video_id=private.id,
            user_id=users["viewer"].id,
            body="The private grant works in this demo.",
        )
        .on_conflict_do_nothing(),
        insert(Playlist)
        .values(
            id=stable_id("playlist:viewer"),
            owner_id=users["viewer"].id,
            title="Demo favorites",
            visibility="public",
        )
        .on_conflict_do_nothing(),
        insert(PlaylistItem)
        .values(playlist_id=stable_id("playlist:viewer"), video_id=public.id, position=0)
        .on_conflict_do_nothing(),
        insert(Report)
        .values(
            id=stable_id("report:public:viewer"),
            video_id=public.id,
            reporter_id=users["viewer"].id,
            reason="Open demo report for the moderation panel.",
            status="open",
        )
        .on_conflict_do_nothing(),
    ]
    for statement in statements:
        await db.execute(statement)
    await db.commit()


async def seed(settings: Settings) -> None:
    engine = create_async_engine(settings.load_database_url(), pool_pre_ping=True)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    storage = await asyncio.to_thread(Storage, settings)
    try:
        async with sessions() as db:
            users = await ensure_accounts(db, settings.seed_credentials_file)
            videos = await ensure_video_jobs(db, storage, users)
        await wait_until_ready(
            sessions, [video.id for video in videos.values()], settings.seed_ready_timeout_seconds
        )
        async with sessions() as db:
            await ensure_social_data(db, users, videos)
    finally:
        await asyncio.to_thread(storage.close)
        await engine.dispose()
    logger.info("demo_seed_complete credentials_file=%s", settings.seed_credentials_file)


def run() -> None:
    settings = get_settings()
    logging.basicConfig(level=settings.log_level)
    asyncio.run(seed(settings))


if __name__ == "__main__":
    run()
