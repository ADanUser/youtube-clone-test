"""Reports, audit, account controls, and moderator privacy boundaries."""

import uuid

import pytest
from sqlalchemy import func, select, update

from test_accounts_api import ApiContext, csrf_headers, register
from test_accounts_api import api_context as api_context
from youtube_copy.accounts.dependencies import CSRF_COOKIE_NAME
from youtube_copy.infrastructure.database.models import (
    Channel,
    ModerationAction,
    Report,
    User,
    Video,
)

pytestmark = pytest.mark.postgres


async def _identity(api: ApiContext, label: str) -> tuple[uuid.UUID, dict[str, str]]:
    name = f"moderation-{label}-" + uuid.uuid4().hex[:8]
    response = await register(
        api.client, name, name + "@example.com", "correct horse battery staple"
    )
    result = (uuid.UUID(response.json()["id"]), dict(api.client.cookies))
    api.client.cookies.clear()
    return result


async def test_report_moderation_audit_and_private_video_boundary(api_context: ApiContext) -> None:
    api = api_context
    owner_id, owner_cookies = await _identity(api, "owner")
    moderator_id, moderator_cookies = await _identity(api, "moderator")
    _ordinary_id, ordinary_cookies = await _identity(api, "ordinary")
    async with api.sessions() as db:
        await db.execute(update(User).where(User.id == moderator_id).values(role="moderator"))
        owner_channel = await db.scalar(select(Channel).where(Channel.user_id == owner_id))
        private_video = Video(
            channel_id=owner_channel.id,
            title="Never disclose this private title",
            description="Never disclose this private description",
            visibility="private",
            processing_state="ready",
        )
        db.add(private_video)
        await db.commit()
        video_id = private_video.id

    api.client.cookies.update(owner_cookies)
    report = await api.client.post(
        f"/api/v1/videos/{video_id}/reports",
        headers=csrf_headers(owner_cookies[CSRF_COOKIE_NAME]),
        json={"reason": "Policy violation"},
    )
    assert report.status_code == 201
    report_id = report.json()["id"]
    duplicate = await api.client.post(
        f"/api/v1/videos/{video_id}/reports",
        headers=csrf_headers(owner_cookies[CSRF_COOKIE_NAME]),
        json={"reason": "Duplicate"},
    )
    assert duplicate.status_code == 409

    api.client.cookies.clear()
    api.client.cookies.update(ordinary_cookies)
    assert (await api.client.get("/moderation")).status_code == 403
    assert (await api.client.get("/api/v1/moderation/reports")).status_code == 403
    forbidden_report = await api.client.post(
        f"/api/v1/videos/{video_id}/reports",
        headers=csrf_headers(ordinary_cookies[CSRF_COOKIE_NAME]),
        json={"reason": "I cannot see it"},
    )
    assert forbidden_report.status_code == 404

    api.client.cookies.clear()
    api.client.cookies.update(moderator_cookies)
    queue = await api.client.get("/api/v1/moderation/reports")
    assert queue.status_code == 200
    item = next(value for value in queue.json() if value["id"] == report_id)
    assert item["video_visibility"] == "private"
    assert item["can_view"] is False
    assert item["video_title"] is None
    panel = await api.client.get("/moderation")
    assert panel.status_code == 200
    assert "Never disclose this private title" not in panel.text
    assert "Never disclose this private description" not in panel.text

    # A role is never an audience grant: all ordinary viewing routes retain the shared 404 policy.
    for path in (
        f"/watch/{video_id}",
        f"/api/v1/videos/{video_id}",
        f"/api/v1/videos/{video_id}/thumbnail",
        f"/api/v1/videos/{video_id}/stream",
    ):
        assert (await api.client.get(path)).status_code == 404

    hidden = await api.client.post(
        f"/api/v1/moderation/videos/{video_id}",
        headers=csrf_headers(moderator_cookies[CSRF_COOKIE_NAME]),
        json={"action": "hide", "reason": "Confirmed", "report_id": report_id},
    )
    assert hidden.status_code == 200
    async with api.sessions() as db:
        video = await db.get(Video, video_id)
        stored_report = await db.get(Report, uuid.UUID(report_id))
        actions = await db.scalar(
            select(func.count())
            .select_from(ModerationAction)
            .where(
                ModerationAction.video_id == video_id,
                ModerationAction.actor_id == moderator_id,
                ModerationAction.reason == "Confirmed",
            )
        )
        assert video.moderation_state == "hidden"
        assert stored_report.status == "resolved" and stored_report.resolved_by == moderator_id
        assert actions == 1

    restored = await api.client.post(
        f"/api/v1/moderation/videos/{video_id}",
        headers=csrf_headers(moderator_cookies[CSRF_COOKIE_NAME]),
        json={"action": "restore", "reason": "Appeal accepted"},
    )
    assert restored.status_code == 200
    async with api.sessions() as db:
        assert (await db.get(Video, video_id)).moderation_state == "visible"
        assert (
            await db.scalar(
                select(func.count())
                .select_from(ModerationAction)
                .where(ModerationAction.video_id == video_id)
            )
            == 2
        )


async def test_moderator_cannot_manage_accounts(api_context: ApiContext) -> None:
    api = api_context
    moderator_id, cookies = await _identity(api, "account-moderator")
    target_id, _ = await _identity(api, "account-target")
    async with api.sessions() as db:
        await db.execute(update(User).where(User.id == moderator_id).values(role="moderator"))
        await db.commit()
    api.client.cookies.update(cookies)
    assert (await api.client.get("/api/v1/admin/users")).status_code == 403
    changed = await api.client.patch(
        f"/api/v1/admin/users/{target_id}",
        headers=csrf_headers(cookies[CSRF_COOKIE_NAME]),
        json={"is_active": False},
    )
    assert changed.status_code == 403
