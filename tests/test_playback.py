"""Real PostgreSQL/Garage playback, grants, HTTP boundaries and resource lifetime."""

import asyncio
import hashlib
import os
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock, patch

import pytest
import pytest_asyncio
from sqlalchemy import select, update
from starlette.requests import ClientDisconnect

from test_accounts_api import ApiContext, csrf_headers, fetch_csrf, register
from test_accounts_api import api_context as api_context
from youtube_copy.accounts.dependencies import CSRF_COOKIE_NAME
from youtube_copy.config import get_settings
from youtube_copy.http.api.playback import MediaResponse
from youtube_copy.infrastructure.database.models import Channel, Session, User, Video, VideoAsset
from youtube_copy.infrastructure.media import MediaStorage
from youtube_copy.videos.ranges import UnsatisfiableRangeError, byte_range

DATA = bytes(range(256)) * 4096


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        (None, None),
        ("bytes=0-0", (0, 0)),
        ("bytes=6-", (6, 9)),
        ("bytes=-3", (7, 9)),
        ("bytes=-30", (0, 9)),
        ("bytes=4-99", (4, 9)),
        ("bytes=3-2", None),
        ("bytes=0-1,3-4", None),
        ("items=0-1", None),
        ("bytes=-", None),
        ("bytes=+1-2", None),
        ("bytes=\u0661-\u0662", None),
        ("bytes=0-" + "9" * 5000, (0, 9)),
        ("bytes=-" + "9" * 5000, (0, 9)),
    ],
)
def test_range_parser(header, expected):
    assert byte_range(header, 10) == expected


@pytest.mark.parametrize(
    "header", ["bytes=10-", "bytes=99-100", "bytes=-0", "bytes=" + "9" * 5000 + "-"]
)
def test_unsatisfiable(header):
    with pytest.raises(UnsatisfiableRangeError):
        byte_range(header, 10)


@pytest_asyncio.fixture
async def media_case(api_context: ApiContext):
    if os.environ.get("YOUTUBE_RUN_S3_TESTS") != "1":
        pytest.skip("set YOUTUBE_RUN_S3_TESTS=1 for real Garage playback")
    api = api_context
    name = "viewer-" + uuid.uuid4().hex[:12]
    response = await register(
        api.client, name, name + "@example.com", "correct horse battery staple"
    )
    owner_id = uuid.UUID(response.json()["id"])
    owner_cookies = dict(api.client.cookies)
    video_id, generation = uuid.uuid4(), uuid.uuid4()
    storage = MediaStorage(get_settings())
    keys = []
    try:
        async with api.sessions() as db:
            channel = await db.scalar(select(Channel).where(Channel.user_id == owner_id))
            db.add(
                Video(
                    id=video_id,
                    channel_id=channel.id,
                    title="<script>private</script>",
                    processing_state="ready",
                    visibility="private",
                )
            )
            await db.flush()
            for kind, prefix, suffix, body in [
                ("mp4", "processed", ".mp4", DATA),
                ("thumbnail", "thumbnails", ".jpg", b"jpeg-test"),
            ]:
                key = f"{prefix}/{video_id}/{generation}{suffix}"
                keys.append(key)
                await asyncio.to_thread(
                    storage.client.put_object, Bucket=storage.bucket, Key=key, Body=body
                )
                db.add(
                    VideoAsset(
                        video_id=video_id,
                        kind=kind,
                        generation_id=generation,
                        object_key=key,
                        size_bytes=len(body),
                        mime_type="video/mp4",
                        sha256=hashlib.sha256(body).hexdigest(),
                    )
                )
            await db.commit()
        yield api, video_id, owner_id, owner_cookies
    finally:
        for key in keys:
            await asyncio.to_thread(storage.client.delete_object, Bucket=storage.bucket, Key=key)
        storage.close()


async def test_real_ranges_and_head(media_case):
    api, video_id, _, _ = media_case
    url = f"/api/v1/videos/{video_id}/stream"
    full = await api.client.get(url)
    assert full.status_code == 200 and full.content == DATA
    assert full.headers["content-type"] == "video/mp4"
    assert full.headers["accept-ranges"] == "bytes"
    assert full.headers["cache-control"] == "private, no-store"
    etag = full.headers["etag"]
    for header, start, end in [
        ("bytes=0-0", 0, 0),
        ("bytes=100-199", 100, 199),
        ("bytes=1048570-", len(DATA) - 6, len(DATA) - 1),
        ("bytes=-127", len(DATA) - 127, len(DATA) - 1),
        ("bytes=1048570-9999999", len(DATA) - 6, len(DATA) - 1),
        ("bytes=-99999999", 0, len(DATA) - 1),
    ]:
        part = await api.client.get(url, headers={"Range": header})
        assert part.status_code == 206, part.text[:100]
        assert part.content == DATA[start : end + 1]
        assert part.headers["content-range"] == f"bytes {start}-{end}/{len(DATA)}"
        assert int(part.headers["content-length"]) == end - start + 1
    for header in ["bytes=0-1,4-5", "garbage", "bytes=10-2"]:
        response = await api.client.get(url, headers={"Range": header})
        assert response.status_code == 200 and response.content == DATA
    for header in ["bytes=9999999-", "bytes=-0"]:
        response = await api.client.get(url, headers={"Range": header})
        assert response.status_code == 416 and response.content == b""
        assert response.headers["content-range"] == f"bytes */{len(DATA)}"
    for validator, status in [
        (etag, 206),
        ('"other"', 200),
        ("W/" + etag, 200),
        ("Tue, 10 Nov 2026 00:00:00 GMT", 200),
    ]:
        response = await api.client.get(url, headers={"Range": "bytes=2-5", "If-Range": validator})
        assert response.status_code == status
        assert response.content == (DATA[2:6] if status == 206 else DATA)
    with patch.object(MediaStorage, "open", side_effect=AssertionError("HEAD must not GET")):
        head = await api.client.head(url, headers={"Range": "bytes=9999999-"})
    assert head.status_code == 200 and head.content == b""
    assert head.headers["content-length"] == str(len(DATA))
    page = await api.client.get(f"/watch/{video_id}")
    assert page.status_code == 200 and "controls" in page.text
    assert "<script>private</script>" not in page.text
    assert "&lt;script&gt;" in page.text


async def assert_denied(api, video_id):
    with patch.object(
        MediaStorage, "__init__", side_effect=AssertionError("unauthorized S3 access")
    ):
        for method, suffix in [
            ("GET", "/stream"),
            ("HEAD", "/stream"),
            ("GET", "/thumbnail"),
            ("HEAD", "/thumbnail"),
            ("GET", ""),
        ]:
            result = await api.client.request(
                method, f"/api/v1/videos/{video_id}{suffix}", headers={"Range": "bytes=9999999-"}
            )
            assert result.status_code == 404
            assert "content-range" not in result.headers and "etag" not in result.headers
            assert result.headers["cache-control"] == "private, no-store"
        assert (await api.client.get(f"/watch/{video_id}")).status_code == 404


async def test_roles_grants_revocation_and_sessions(media_case):
    api, video_id, owner_id, owner_cookies = media_case
    api.client.cookies.clear()
    await assert_denied(api, video_id)
    api.client.cookies.set("youtube_session", "invalid")
    await assert_denied(api, video_id)
    api.client.cookies.clear()
    name = "guest-" + uuid.uuid4().hex[:12]
    response = await register(
        api.client, name, name + "@example.com", "correct horse battery staple"
    )
    viewer_id = uuid.UUID(response.json()["id"])
    viewer_cookies = dict(api.client.cookies)
    grant_url = f"/api/v1/videos/{video_id}/grants/{viewer_id}"
    for role in ["user", "moderator", "admin"]:
        async with api.sessions() as db:
            await db.execute(update(User).where(User.id == viewer_id).values(role=role))
            await db.commit()
        await assert_denied(api, video_id)
        for method in ["PUT", "DELETE"]:
            result = await api.client.request(
                method, grant_url, headers=csrf_headers(api.client.cookies[CSRF_COOKIE_NAME])
            )
            assert result.status_code == 404
    api.client.cookies.clear()
    api.client.cookies.update(owner_cookies)
    assert (await api.client.put(grant_url)).status_code == 403
    for _ in range(2):
        assert (
            await api.client.put(
                grant_url, headers=csrf_headers(api.client.cookies[CSRF_COOKIE_NAME])
            )
        ).status_code == 204
    api.client.cookies.clear()
    api.client.cookies.update(viewer_cookies)
    for role in ["user", "moderator", "admin"]:
        async with api.sessions() as db:
            await db.execute(update(User).where(User.id == viewer_id).values(role=role))
            await db.commit()
        result = await api.client.get(
            f"/api/v1/videos/{video_id}/stream", headers={"Range": "bytes=0-1"}
        )
        assert result.status_code == 206 and result.content == DATA[:2]
        assert (
            await api.client.get(f"/api/v1/videos/{video_id}/thumbnail")
        ).content == b"jpeg-test"
    async with api.sessions() as db:
        await db.execute(update(User).where(User.id == viewer_id).values(is_active=False))
        await db.commit()
    await assert_denied(api, video_id)
    async with api.sessions() as db:
        await db.execute(update(User).where(User.id == viewer_id).values(is_active=True))
        await db.commit()
    api.client.cookies.clear()
    token = await fetch_csrf(api.client)
    logged_in = await api.client.post(
        "/api/v1/auth/login",
        headers=csrf_headers(token),
        json={"login": name + "@example.com", "password": "correct horse battery staple"},
    )
    assert logged_in.status_code == 200
    viewer_cookies = dict(api.client.cookies)
    api.client.cookies.clear()
    api.client.cookies.update(owner_cookies)
    assert (
        await api.client.delete(
            grant_url, headers=csrf_headers(api.client.cookies[CSRF_COOKIE_NAME])
        )
    ).status_code == 204
    api.client.cookies.clear()
    api.client.cookies.update(viewer_cookies)
    await assert_denied(api, video_id)
    # Even the owner loses access when their session expires or account is blocked.
    api.client.cookies.clear()
    api.client.cookies.update(owner_cookies)
    async with api.sessions() as db:
        await db.execute(
            update(Session)
            .where(Session.user_id == owner_id)
            .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
        await db.commit()
    await assert_denied(api, video_id)


@pytest.mark.parametrize("owner_role", ["user", "moderator", "admin"])
async def test_public_and_unplayable_states(media_case, owner_role):
    api, video_id, owner_id, owner_cookies = media_case
    async with api.sessions() as db:
        await db.execute(update(Video).where(Video.id == video_id).values(visibility="public"))
        await db.commit()
    api.client.cookies.clear()
    assert (await api.client.get(f"/api/v1/videos/{video_id}/stream")).content == DATA
    async with api.sessions() as db:
        await db.execute(update(User).where(User.id == owner_id).values(role=owner_role))
        await db.commit()
    for cookies in [{}, owner_cookies]:
        api.client.cookies.clear()
        api.client.cookies.update(cookies)
        for values in [
            {"moderation_state": "hidden"},
            {"processing_state": "pending"},
            {"processing_state": "processing"},
            {"processing_state": "failed"},
            {"processing_state": "uploading"},
            {"deleted_at": datetime.now(UTC)},
        ]:
            async with api.sessions() as db:
                await db.execute(update(Video).where(Video.id == video_id).values(**values))
                await db.commit()
            await assert_denied(api, video_id)
            async with api.sessions() as db:
                await db.execute(
                    update(Video)
                    .where(Video.id == video_id)
                    .values(moderation_state="visible", processing_state="ready", deleted_at=None)
                )
                await db.commit()
    async with api.sessions() as db:
        await db.execute(update(User).where(User.id == owner_id).values(is_active=False))
        await db.commit()
    await assert_denied(api, video_id)


async def test_storage_failure_is_safe_and_closes(media_case):
    api, video_id, _, _ = media_case
    with (
        patch.object(MediaStorage, "head", side_effect=OSError("internal-key")),
        patch.object(MediaStorage, "close", autospec=True) as closed,
    ):
        response = await api.client.get(f"/api/v1/videos/{video_id}/stream")
        assert response.status_code == 503 and "internal-key" not in response.text
        assert closed.call_count == 1


async def test_response_closes_on_disconnect_and_send_failure():
    for spec in ["2.0", "2.4"]:
        storage = Mock()
        storage.read.return_value = b"x"
        response = MediaResponse(storage, 100, 200, {})

        async def receive():
            return {"type": "http.disconnect"}

        async def send(message, spec=spec):
            if spec == "2.4":
                raise OSError("client disconnected")
            await asyncio.sleep(0.01)

        try:
            await response({"type": "http", "asgi": {"spec_version": spec}}, receive, send)
        except Exception:
            assert spec == "2.4"
        storage.close.assert_called_once()


async def test_response_reads_bounded_chunks_and_closes_after_read_error():
    for fail in [False, True]:
        storage = Mock()
        chunks = []

        def read(amount, fail=fail):
            assert amount <= 256 * 1024
            if fail:
                raise OSError("read failure")
            return b"x" * amount

        storage.read.side_effect = read
        response = MediaResponse(storage, 1024 * 1024 + 1, 200, {})

        async def receive():
            await asyncio.sleep(100)

        async def send(message, chunks=chunks):
            if message["type"] == "http.response.body":
                chunks.append(message["body"])

        try:
            await response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send)
            assert not fail
            assert sum(map(len, chunks)) == 1024 * 1024 + 1
        except ClientDisconnect:
            assert fail
        storage.close.assert_called_once()


async def test_real_mp4_seek_over_http(media_case, tmp_path):
    import shutil
    import socket

    import uvicorn

    from youtube_copy.infrastructure.database.session import get_db_session
    from youtube_copy.main import create_app

    if not shutil.which("ffmpeg"):
        pytest.skip("FFmpeg required for HTTP seek test")
    api, video_id, _, _ = media_case
    movie = tmp_path / "seek.mp4"
    process = await asyncio.create_subprocess_exec(
        "ffmpeg",
        "-v",
        "error",
        "-f",
        "lavfi",
        "-i",
        "testsrc2=size=320x240:rate=25",
        "-t",
        "12",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-threads",
        "1",
        "-g",
        "25",
        "-movflags",
        "+faststart",
        str(movie),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, error = await process.communicate()
    assert process.returncode == 0, error.decode()
    body = movie.read_bytes()
    async with api.sessions() as db:
        asset = await db.scalar(
            select(VideoAsset).where(VideoAsset.video_id == video_id, VideoAsset.kind == "mp4")
        )
        key = asset.object_key
        asset.size_bytes = len(body)
        asset.sha256 = hashlib.sha256(body).hexdigest()
        await db.execute(update(Video).where(Video.id == video_id).values(visibility="public"))
        await db.commit()
    storage = MediaStorage(get_settings())
    try:
        await asyncio.to_thread(
            storage.client.put_object, Bucket=storage.bucket, Key=key, Body=body
        )
    finally:
        storage.close()
    app = create_app(api.settings)

    async def session():
        async with api.sessions() as db:
            yield db

    app.dependency_overrides[get_db_session] = session
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="off"))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    ranges = []
    original = MediaStorage.open

    def track(self, key, etag, selected):
        ranges.append(selected)
        return original(self, key, etag, selected)

    try:
        async with asyncio.timeout(10):
            while not server.started:
                await asyncio.sleep(0.01)
        with patch.object(MediaStorage, "open", track):
            process = await asyncio.create_subprocess_exec(
                "ffmpeg",
                "-v",
                "error",
                "-ss",
                "9",
                "-i",
                f"http://127.0.0.1:{port}/api/v1/videos/{video_id}/stream",
                "-frames:v",
                "1",
                "-f",
                "image2pipe",
                "-c:v",
                "mjpeg",
                "-threads",
                "1",
                "-",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                frame, error = await asyncio.wait_for(process.communicate(), 30)
            finally:
                if process.returncode is None:
                    process.kill()
                    await process.wait()
            assert process.returncode == 0, error.decode()
            assert frame.startswith(b"\xff\xd8")
            assert any(selected and selected[0] > 0 for selected in ranges), ranges
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 10)
        sock.close()


async def test_cancel_during_read_waits_before_closing():
    import threading

    entered, release = threading.Event(), threading.Event()
    storage = Mock()

    def read(amount):
        entered.set()
        assert release.wait(5)
        return b"x"

    storage.read.side_effect = read
    response = MediaResponse(storage, 1, 200, {})

    async def receive():
        await asyncio.sleep(30)

    async def send(message):
        pass

    task = asyncio.create_task(
        response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send)
    )
    try:
        async with asyncio.timeout(5):
            while not entered.is_set():
                await asyncio.sleep(0.01)
        task.cancel()
        await asyncio.sleep(0.01)
        storage.close.assert_not_called()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    storage.close.assert_called_once()
