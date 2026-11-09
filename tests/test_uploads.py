"""Upload boundaries with real PostgreSQL, including failure and concurrent requests."""

from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from unittest.mock import patch

import boto3
import pytest
import pytest_asyncio
from botocore import UNSIGNED
from botocore.config import Config
from botocore.exceptions import ClientError
from sqlalchemy import func, select, update
from starlette.requests import ClientDisconnect

from test_accounts_api import (
    ApiContext,
    csrf_headers,
    register,
)
from test_accounts_api import (
    api_context as api_context,
)
from youtube_copy.accounts.dependencies import CSRF_COOKIE_NAME
from youtube_copy.config import get_settings
from youtube_copy.infrastructure.database.models import ProcessingJob, User, VideoAsset
from youtube_copy.infrastructure.uploads import OriginalStorage
from youtube_copy.videos import service

pytestmark = pytest.mark.postgres


@pytest_asyncio.fixture
async def upload_api(api_context: ApiContext, tmp_path: Path) -> ApiContext:
    api_context.settings.upload_temp_dir = tmp_path
    api_context.settings.upload_max_bytes = 1024
    api_context.settings.upload_disk_reserve_bytes = 0
    api_context.settings.upload_concurrency = 2
    username = f"upload-{uuid.uuid4().hex[:16]}"
    result = await register(
        api_context.client, username, f"{username}@example.com", "upload test password"
    )
    assert result.status_code == 201
    return api_context


def headers(api: ApiContext) -> dict[str, str]:
    return {
        **csrf_headers(api.client.cookies[CSRF_COOKIE_NAME]),
        "Content-Type": "application/octet-stream",
    }


async def draft(api: ApiContext, **values: object) -> str:
    result = await api.client.post(
        "/api/v1/videos",
        headers={**headers(api), "Content-Type": "application/json"},
        json={"title": "A video", **values},
    )
    assert result.status_code == 201, result.text
    assert result.json()["visibility"] == values.get("visibility", "private")
    return result.json()["id"]


async def state(api: ApiContext, video_id: str) -> dict:
    result = await api.client.get(f"/api/v1/videos/{video_id}/status")
    assert result.status_code == 200
    assert result.headers["cache-control"] == "private, no-store"
    return result.json()


async def assert_no_assets(api: ApiContext, video_id: str) -> None:
    async with api.sessions() as db:
        assert (
            await db.scalar(
                select(func.count()).select_from(VideoAsset).where(VideoAsset.video_id == video_id)
            )
            == 0
        )
        assert (
            await db.scalar(
                select(func.count())
                .select_from(ProcessingJob)
                .where(ProcessingJob.video_id == video_id)
            )
            == 0
        )
    assert list(api.settings.upload_temp_dir.iterdir()) == []


async def test_upload_handoff_and_metadata(upload_api: ApiContext) -> None:
    api = upload_api
    video_id = await draft(api, title="  A title  ", description="<script>test</script>")
    body = b"not trusted as a media file"
    uploaded = []

    def put(_self, key, spool):
        spool.file.seek(0)
        uploaded.append((key, spool.file.read()))

    with patch.object(OriginalStorage, "put", put):
        result = await api.client.post(
            f"/api/v1/videos/{video_id}/upload", headers=headers(api), content=body
        )
    assert result.status_code == 202, result.text
    assert result.json()["processing_state"] == "processing"
    assert result.json()["title"] == "A title"
    assert "object_key" not in result.text
    async with api.sessions() as db:
        asset = await db.scalar(select(VideoAsset).where(VideoAsset.video_id == video_id))
        job = await db.scalar(select(ProcessingJob).where(ProcessingJob.video_id == video_id))
        assert asset.size_bytes == len(body)
        assert asset.sha256 == hashlib.sha256(body).hexdigest()
        assert asset.mime_type == "application/octet-stream"
        assert uploaded == [(asset.object_key, body)]
        assert asset.object_key.startswith(f"originals/{video_id}/")
        assert job.payload == {"upload_id": str(asset.generation_id)}
        assert job.status == "queued"
    conflict = await api.client.post(
        f"/api/v1/videos/{video_id}/upload", headers=headers(api), content=body
    )
    assert conflict.status_code == 409
    assert list(api.settings.upload_temp_dir.iterdir()) == []


async def test_metadata_allowlist_and_auth(upload_api: ApiContext) -> None:
    api = upload_api
    for values in (
        {"channel_id": str(uuid.uuid4())},
        {"processing_state": "ready"},
        {"title": " "},
    ):
        response = await api.client.post(
            "/api/v1/videos",
            headers={**headers(api), "Content-Type": "application/json"},
            json={"title": "video", **values},
        )
        assert response.status_code == 422
    video_id = await draft(api)
    response = await api.client.post(f"/api/v1/videos/{video_id}/upload", content=b"x")
    assert response.status_code == 403
    response = await api.client.post(
        f"/api/v1/videos/{video_id}/upload",
        headers={**headers(api), "Origin": "http://evil"},
        content=b"x",
    )
    assert response.status_code == 403
    await assert_no_assets(api, video_id)


@pytest.mark.parametrize("role", ["user", "moderator", "admin"])
async def test_foreign_video_is_not_accessible(upload_api: ApiContext, role: str) -> None:
    api = upload_api
    video_id = await draft(api)
    api.client.cookies.clear()
    username = f"foreign-{uuid.uuid4().hex[:16]}"
    response = await register(
        api.client, username, f"{username}@example.com", "upload test password"
    )
    async with api.sessions() as db:
        await db.execute(update(User).where(User.id == response.json()["id"]).values(role=role))
        await db.commit()
    result = await api.client.get(f"/api/v1/videos/{video_id}/status")
    assert result.status_code == 404
    result = await api.client.post(
        f"/api/v1/videos/{video_id}/upload", headers=headers(api), content=b"x"
    )
    assert result.status_code == 404
    api.client.cookies.clear()
    result = await api.client.post(f"/api/v1/videos/{video_id}/upload", content=b"x")
    assert result.status_code == 401
    await assert_no_assets(api, video_id)


@pytest.mark.parametrize("declared", [None, "1", "1025"])
async def test_actual_byte_limit(upload_api: ApiContext, declared: str | None) -> None:
    api = upload_api
    video_id = await draft(api)

    async def chunks():
        for _ in range(5):
            yield b"x" * 256

    request_headers = headers(api)
    if declared is not None:
        request_headers["Content-Length"] = declared
    result = await api.client.post(
        f"/api/v1/videos/{video_id}/upload", headers=request_headers, content=chunks()
    )
    assert result.status_code == 413
    assert (await state(api, video_id))["processing_state"] == (
        "pending" if declared == "1025" else "failed"
    )
    await assert_no_assets(api, video_id)


@pytest.mark.parametrize("case", ["empty", "length", "disconnect", "timeout", "storage"])
async def test_failures_allow_retry(upload_api: ApiContext, case: str) -> None:
    api = upload_api
    video_id = await draft(api)
    api.settings.upload_idle_timeout_seconds = 1

    async def chunks():
        if case == "empty":
            return
        yield b"x"
        if case == "disconnect":
            raise ClientDisconnect()
        if case == "timeout":
            await asyncio.sleep(2)

    request_headers = headers(api)
    if case == "length":
        request_headers["Content-Length"] = "2"
    with patch.object(OriginalStorage, "put", side_effect=RuntimeError("private diagnostics")):
        response = await api.client.post(
            f"/api/v1/videos/{video_id}/upload", headers=request_headers, content=chunks()
        )
    assert response.status_code == {"timeout": 408, "storage": 503}.get(case, 400)
    assert "private diagnostics" not in response.text
    assert (await state(api, video_id))["processing_state"] == "failed"
    await assert_no_assets(api, video_id)
    with patch.object(OriginalStorage, "put"):
        response = await api.client.post(
            f"/api/v1/videos/{video_id}/upload", headers=headers(api), content=b"retry"
        )
    assert response.status_code == 202


async def test_concurrent_uploads_and_cancelled_request(upload_api: ApiContext) -> None:
    api = upload_api
    api.settings.upload_concurrency = 1
    video_id = await draft(api)
    second_id = await draft(api)
    receiving = asyncio.Event()

    async def chunks():
        yield b"first chunk"
        receiving.set()
        await asyncio.Event().wait()

    pending = asyncio.create_task(
        api.client.post(f"/api/v1/videos/{video_id}/upload", headers=headers(api), content=chunks())
    )
    await asyncio.wait_for(receiving.wait(), 5)
    try:
        # While receiving there must be no open transaction/row lock: status reads
        # and a competing claim both complete promptly on independent connections.
        assert (await state(api, video_id))["processing_state"] == "uploading"
        response = await api.client.post(
            f"/api/v1/videos/{second_id}/upload", headers=headers(api), content=b"x"
        )
        assert response.status_code == 503
        api.settings.upload_concurrency = 2
        response = await asyncio.wait_for(
            api.client.post(
                f"/api/v1/videos/{video_id}/upload", headers=headers(api), content=b"x"
            ),
            5,
        )
        assert response.status_code == 409
    finally:
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
    assert (await state(api, video_id))["processing_state"] == "failed"
    await assert_no_assets(api, video_id)
    with patch.object(OriginalStorage, "put"):
        response = await api.client.post(
            f"/api/v1/videos/{video_id}/upload", headers=headers(api), content=b"retry"
        )
    assert response.status_code == 202


@pytest.mark.parametrize("at_admission", [True, False])
async def test_disk_limits(upload_api: ApiContext, at_admission: bool) -> None:
    api = upload_api
    video_id = await draft(api)
    usage = shutil.disk_usage(api.settings.upload_temp_dir)
    full = usage._replace(free=0)
    with patch(
        "youtube_copy.infrastructure.uploads.shutil.disk_usage",
        side_effect=[full if at_admission else usage, full],
    ):
        response = await api.client.post(
            f"/api/v1/videos/{video_id}/upload", headers=headers(api), content=b"payload"
        )
    assert response.status_code == 507
    assert (await state(api, video_id))["processing_state"] == (
        "pending" if at_admission else "failed"
    )
    await assert_no_assets(api, video_id)


async def test_logout_during_transfer_prevents_publication(upload_api: ApiContext) -> None:
    api = upload_api
    video_id = await draft(api)
    saved_headers = headers(api)

    async def chunks():
        yield b"data"
        response = await api.client.post("/api/v1/auth/logout", headers=saved_headers)
        assert response.status_code == 204

    with patch.object(OriginalStorage, "put"):
        response = await api.client.post(
            f"/api/v1/videos/{video_id}/upload", headers=saved_headers, content=chunks()
        )
    assert response.status_code == 409
    await assert_no_assets(api, video_id)


async def test_upload_rate_limit(upload_api: ApiContext) -> None:
    api = upload_api
    api.settings.upload_rate_limit_attempts = 1
    video_id = await draft(api)
    first = await api.client.post(
        f"/api/v1/videos/{video_id}/upload", headers=headers(api), content=b""
    )
    assert first.status_code == 400
    second = await api.client.post(
        f"/api/v1/videos/{video_id}/upload", headers=headers(api), content=b"x"
    )
    assert second.status_code == 429
    assert int(second.headers["retry-after"]) > 0


@pytest.mark.parametrize("after_commit", [False, True])
async def test_database_handoff_failure_is_atomic(
    upload_api: ApiContext, after_commit: bool
) -> None:
    api = upload_api
    video_id = await draft(api)
    finish = service.finish_upload

    async def fail_handoff(db, *args):
        if after_commit:
            await finish(db, *args)
            raise RuntimeError("lost commit acknowledgement")
        with patch.object(db, "commit", side_effect=RuntimeError("commit failed")):
            return await finish(db, *args)

    with (
        patch.object(OriginalStorage, "put"),
        patch.object(service, "finish_upload", fail_handoff),
        pytest.raises(RuntimeError),
    ):
        await api.client.post(
            f"/api/v1/videos/{video_id}/upload", headers=headers(api), content=b"payload"
        )
    assert (await state(api, video_id))["processing_state"] == (
        "processing" if after_commit else "failed"
    )
    if not after_commit:
        await assert_no_assets(api, video_id)
    else:
        async with api.sessions() as db:
            assert await db.scalar(select(VideoAsset).where(VideoAsset.video_id == video_id))
            assert await db.scalar(select(ProcessingJob).where(ProcessingJob.video_id == video_id))


@pytest.mark.parametrize(
    "extra",
    [
        {"Content-Type": "video/mp4"},
        {"Content-Type": "multipart/form-data; boundary=x"},
        {"Content-Encoding": "gzip"},
        {"Content-Length": "-1"},
    ],
)
async def test_upload_transport_contract(upload_api: ApiContext, extra: dict[str, str]) -> None:
    api = upload_api
    video_id = await draft(api)
    response = await api.client.post(
        f"/api/v1/videos/{video_id}/upload", headers={**headers(api), **extra}, content=b"payload"
    )
    assert response.status_code == (400 if "Content-Length" in extra else 415)
    assert (await state(api, video_id))["processing_state"] == "pending"
    await assert_no_assets(api, video_id)


async def test_real_garage_multipart_is_private(upload_api: ApiContext) -> None:
    if os.environ.get("YOUTUBE_RUN_S3_TESTS") != "1":
        pytest.skip("set YOUTUBE_RUN_S3_TESTS=1 for real Garage upload verification")
    api = upload_api
    real = get_settings()
    for field in (
        "s3_endpoint_url",
        "s3_bucket",
        "s3_region",
        "s3_access_key_file",
        "s3_secret_key_file",
        "s3_access_key_id",
        "s3_secret_access_key",
    ):
        setattr(api.settings, field, getattr(real, field))
    api.settings.upload_max_bytes = 32 * 1024 * 1024
    video_id = await draft(api)
    chunk = b"streaming-original" * 4096
    digest = hashlib.sha256()
    total = 0

    async def chunks() -> AsyncIterator[bytes]:
        nonlocal total
        for _ in range(384):
            digest.update(chunk)
            total += len(chunk)
            yield chunk

    original_put = OriginalStorage.put
    read_sizes = []

    def bounded_put(storage, key, spool):
        file = spool.file

        class BoundedReader:
            def read(self, size=-1):
                assert 0 <= size <= 8 * 1024 * 1024
                read_sizes.append(size)
                return file.read(size)

            def __getattr__(self, name):
                return getattr(file, name)

        spool.file = BoundedReader()
        try:
            original_put(storage, key, spool)
        finally:
            spool.file = file

    with patch.object(OriginalStorage, "put", bounded_put):
        response = await api.client.post(
            f"/api/v1/videos/{video_id}/upload", headers=headers(api), content=chunks()
        )
    assert response.status_code == 202, response.text
    assert total > 3 * 8 * 1024 * 1024
    assert len(read_sizes) >= 4
    async with api.sessions() as db:
        asset = await db.scalar(select(VideoAsset).where(VideoAsset.video_id == video_id))
        key = asset.object_key
        assert asset.size_bytes == total
        assert asset.sha256 == digest.hexdigest()
    access, secret = real.load_s3_credentials()

    def verify_storage():
        client = boto3.client(
            "s3",
            endpoint_url=str(real.s3_endpoint_url),
            region_name=real.s3_region,
            aws_access_key_id=access,
            aws_secret_access_key=secret,
        )
        anonymous = boto3.client(
            "s3", endpoint_url=str(real.s3_endpoint_url), config=Config(signature_version=UNSIGNED)
        )
        try:
            obj = client.get_object(Bucket=real.s3_bucket, Key=key)
            received = hashlib.sha256()
            try:
                for part in obj["Body"].iter_chunks(65536):
                    received.update(part)
            finally:
                obj["Body"].close()
            assert received.hexdigest() == digest.hexdigest()
            assert obj["ContentType"] == "application/octet-stream"
            with pytest.raises(ClientError) as denied:
                anonymous.get_object(Bucket=real.s3_bucket, Key=key)
            assert denied.value.response["ResponseMetadata"]["HTTPStatusCode"] in (403, 404)
        finally:
            client.delete_object(Bucket=real.s3_bucket, Key=key)
            client.close()
            anonymous.close()

    await asyncio.to_thread(verify_storage)


async def test_upload_page_and_static_script(upload_api: ApiContext) -> None:
    response = await upload_api.client.get("/upload")
    assert response.status_code == 200
    assert '<progress id="progress"' in response.text
    assert response.headers["cache-control"] == "private, no-store"
    script = await upload_api.client.get("/static/upload.js")
    assert script.status_code == 200
