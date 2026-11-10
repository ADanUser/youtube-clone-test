"""User-facing screens, discovery pagination, escaping, and owner edits."""

import uuid

import pytest
from sqlalchemy import select

from test_accounts_api import ApiContext, csrf_headers, register
from test_accounts_api import api_context as api_context
from youtube_copy.accounts.dependencies import CSRF_COOKIE_NAME
from youtube_copy.infrastructure.database.models import Channel, Video


@pytest.fixture
async def screen_case(api_context: ApiContext):
    api = api_context
    name = "screens-" + uuid.uuid4().hex[:10]
    account = await register(
        api.client, name, name + "@example.com", "correct horse battery staple"
    )
    user_id = uuid.UUID(account.json()["id"])
    async with api.sessions() as db:
        channel = await db.scalar(select(Channel).where(Channel.user_id == user_id))
        for number in range(13):
            db.add(
                Video(
                    channel_id=channel.id,
                    title=f"Visible title {number} <script>",
                    description="Description",
                    visibility="public",
                    processing_state="ready",
                    duration_ms=65_000,
                )
            )
        db.add(
            Video(
                channel_id=channel.id,
                title="Private title",
                visibility="private",
                processing_state="ready",
            )
        )
        await db.commit()
    return api, name


async def test_discovery_channel_search_and_pagination(screen_case):
    api, handle = screen_case
    api.client.cookies.clear()
    home = await api.client.get("/")
    assert home.status_code == 200
    assert home.text.count('class="group min-w-0"') == 12
    assert "Visible title" in home.text and "Private title" not in home.text
    assert "Visible title 0 <script>" not in home.text
    assert "Visible title 0 &lt;script&gt;" in home.text
    second = await api.client.get("/?page=2")
    assert second.text.count('class="group min-w-0"') == 1
    search = await api.client.get("/search", params={"q": "title 1"})
    assert search.status_code == 200 and "Результаты по запросу" in search.text
    assert "Visible title 1" in search.text
    channel = await api.client.get(f"/channel/{handle}")
    assert channel.status_code == 200 and f"@{handle}" in channel.text
    missing = await api.client.get("/channel/not-a-channel")
    assert missing.status_code == 404 and "Страница не найдена" in missing.text


async def test_auth_upload_studio_and_owner_edit(screen_case):
    api, handle = screen_case
    assert (await api.client.get("/login")).status_code == 303
    upload = await api.client.get("/upload")
    assert upload.status_code == 200 and "Загрузка видео" in upload.text
    studio = await api.client.get("/studio")
    assert studio.status_code == 200 and "Контент канала" in studio.text
    async with api.sessions() as db:
        channel = await db.scalar(select(Channel).where(Channel.handle == handle))
        video = await db.scalar(
            select(Video).where(Video.title == "Private title", Video.channel_id == channel.id)
        )
        video_id = video.id
    changed = await api.client.patch(
        f"/api/v1/videos/{video_id}",
        headers=csrf_headers(api.client.cookies[CSRF_COOKIE_NAME]),
        json={"title": "Updated title", "visibility": "public"},
    )
    assert changed.status_code == 204
    async with api.sessions() as db:
        video = await db.get(Video, video_id)
        assert video.title == "Updated title" and video.visibility == "public"
    api.client.cookies.clear()
    assert (await api.client.get("/login")).status_code == 200
    assert (await api.client.get("/register")).status_code == 200
    assert (await api.client.get("/studio")).status_code == 401
