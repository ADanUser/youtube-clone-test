"""Concurrent PostgreSQL mutations and cross-surface privacy/CSRF/IDOR."""

import asyncio
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select, update

from test_accounts_api import ApiContext
from test_accounts_api import api_context as api_context
from test_social import auth_headers
from test_social import social_case as social_case
from youtube_copy.infrastructure.database.models import (
    Comment,
    Like,
    PlaylistItem,
    Subscription,
    User,
    Video,
)

pytestmark = pytest.mark.postgres
type Identity = tuple[uuid.UUID, dict[str, str], str]
type SocialCase = tuple[ApiContext, Identity, Identity, uuid.UUID, uuid.UUID, list[uuid.UUID]]


async def test_concurrent_likes_subscriptions_and_playlist_edits(social_case: SocialCase) -> None:
    api, _, viewer, channel_id, _, videos = social_case
    api.client.cookies.update(viewer[1])
    headers = auth_headers(viewer[1])
    for url, model, condition in [
        (f"/api/v1/videos/{videos[0]}/like", Like, Like.video_id == videos[0]),
        (
            f"/api/v1/channels/{channel_id}/subscription",
            Subscription,
            Subscription.channel_id == channel_id,
        ),
    ]:
        replies = await asyncio.gather(*(api.client.put(url, headers=headers) for _ in range(8)))
        assert [r.status_code for r in replies] == [204] * 8
        async with api.sessions() as db:
            assert await db.scalar(select(func.count()).select_from(model).where(condition)) == 1
        replies = await asyncio.gather(*(api.client.delete(url, headers=headers) for _ in range(8)))
        assert [r.status_code for r in replies] == [204] * 8
        async with api.sessions() as db:
            assert await db.scalar(select(func.count()).select_from(model).where(condition)) == 0
    created = await api.client.post(
        "/api/v1/playlists", headers=headers, json={"title": "Concurrent list"}
    )
    playlist_id = created.json()["id"]
    prefix = f"/api/v1/playlists/{playlist_id}"
    replies = await asyncio.gather(
        *(api.client.put(f"{prefix}/videos/{v}", headers=headers) for v in videos[:2])
    )
    assert [r.status_code for r in replies] == [204, 204]
    for order in ([str(v) for v in videos[:2]], [str(v) for v in reversed(videos[:2])]):
        replies = await asyncio.gather(
            *(
                api.client.put(f"{prefix}/order", headers=headers, json={"video_ids": order})
                for _ in range(4)
            )
        )
        assert [r.status_code for r in replies] == [204] * 4
    replies = await asyncio.gather(
        *(api.client.delete(f"{prefix}/videos/{v}", headers=headers) for v in videos[:2])
    )
    assert [r.status_code for r in replies] == [204, 204]
    async with api.sessions() as db:
        assert (
            await db.scalar(
                select(func.count())
                .select_from(PlaylistItem)
                .where(PlaylistItem.playlist_id == uuid.UUID(playlist_id))
            )
            == 0
        )


async def test_social_mutations_require_csrf_and_playlist_ownership(
    social_case: SocialCase,
) -> None:
    api, owner, viewer, channel_id, _, videos = social_case
    api.client.cookies.update(owner[1])
    headers = auth_headers(owner[1])
    playlist = await api.client.post(
        "/api/v1/playlists", headers=headers, json={"title": "Secret list"}
    )
    playlist_id = playlist.json()["id"]
    prefix = f"/api/v1/playlists/{playlist_id}"
    comment = await api.client.post(
        f"/api/v1/videos/{videos[0]}/comments", headers=headers, json={"body": "Keep me"}
    )
    comment_id = comment.json()["id"]
    operations = [
        ("PUT", f"/api/v1/videos/{videos[0]}/like", None),
        ("DELETE", f"/api/v1/videos/{videos[0]}/like", None),
        ("POST", f"/api/v1/videos/{videos[0]}/comments", {"body": "No CSRF"}),
        ("DELETE", f"/api/v1/comments/{comment_id}", None),
        ("PUT", f"/api/v1/channels/{channel_id}/subscription", None),
        ("DELETE", f"/api/v1/channels/{channel_id}/subscription", None),
        ("POST", "/api/v1/playlists", {"title": "No CSRF"}),
        ("PUT", f"{prefix}/videos/{videos[0]}", None),
        ("DELETE", f"{prefix}/videos/{videos[0]}", None),
        ("PUT", f"{prefix}/order", {"video_ids": []}),
        ("POST", f"/api/v1/videos/{videos[0]}/reports", {"reason": "No CSRF"}),
    ]
    for bad_headers in (
        {},
        {**headers, "Origin": "https://attacker.test"},
        {**headers, "X-CSRF-Token": "invalid"},
    ):
        for method, url, body in operations:
            response = await api.client.request(method, url, headers=bad_headers, json=body)
            assert response.status_code == 403, (method, url, response.text)
    api.client.cookies.clear()
    api.client.cookies.update(viewer[1])
    assert (await api.client.get(f"/playlist/{playlist_id}")).status_code == 404
    for method, url, body in operations[7:10]:
        response = await api.client.request(method, url, headers=auth_headers(viewer[1]), json=body)
        assert response.status_code == 404
    async with api.sessions() as db:
        assert (await db.get(Comment, uuid.UUID(comment_id))).deleted_at is None
        assert (
            await db.scalar(
                select(func.count())
                .select_from(PlaylistItem)
                .where(PlaylistItem.playlist_id == uuid.UUID(playlist_id))
            )
            == 0
        )


@pytest.mark.parametrize(
    "state", ["private", "hidden", "pending", "processing", "failed", "deleted", "blocked"]
)
async def test_discovery_and_social_state_matrix(social_case: SocialCase, state: str) -> None:
    api, owner, viewer, _, _, videos = social_case
    video_id = videos[0]
    title = "matrix-" + uuid.uuid4().hex
    api.client.cookies.update(owner[1])
    playlist = await api.client.post(
        "/api/v1/playlists",
        headers=auth_headers(owner[1]),
        json={"title": "Matrix list", "visibility": "public"},
    )
    playlist_id = playlist.json()["id"]
    assert (
        await api.client.put(
            f"/api/v1/playlists/{playlist_id}/videos/{video_id}", headers=auth_headers(owner[1])
        )
    ).status_code == 204
    values = {"title": title}
    if state == "private":
        values["visibility"] = "private"
    elif state == "hidden":
        values["moderation_state"] = "hidden"
    elif state == "deleted":
        values["deleted_at"] = datetime.now(UTC)
    elif state != "blocked":
        values["processing_state"] = state
    async with api.sessions.begin() as db:
        await db.execute(update(Video).where(Video.id == video_id).values(**values))
        if state == "blocked":
            await db.execute(update(User).where(User.id == owner[0]).values(is_active=False))
    for role in (None, "user", "moderator", "admin"):
        api.client.cookies.clear()
        if role:
            async with api.sessions.begin() as db:
                await db.execute(update(User).where(User.id == viewer[0]).values(role=role))
            api.client.cookies.update(viewer[1])
        for path in (f"/search?q={title}", f"/channel/{owner[2]}", f"/playlist/{playlist_id}"):
            response = await api.client.get(path)
            assert f"/watch/{video_id}" not in response.text
        assert (await api.client.get(f"/watch/{video_id}")).status_code == 404
        if role:
            for method, suffix, body in (
                ("PUT", "like", None),
                ("POST", "comments", {"body": "Forbidden"}),
                ("POST", "reports", {"reason": "Forbidden"}),
            ):
                response = await api.client.request(
                    method,
                    f"/api/v1/videos/{video_id}/{suffix}",
                    headers=auth_headers(viewer[1]),
                    json=body,
                )
                assert response.status_code == 404


async def test_revoked_grant_removes_comments_discovery_and_playlist_items(
    social_case: SocialCase,
) -> None:
    api, owner, viewer, _, _, videos = social_case
    private_id = videos[2]
    api.client.cookies.update(owner[1])
    owner_headers = auth_headers(owner[1])
    grant = f"/api/v1/videos/{private_id}/grants/{viewer[0]}"
    assert (await api.client.put(grant, headers=owner_headers)).status_code == 204
    playlist = await api.client.post(
        "/api/v1/playlists",
        headers=owner_headers,
        json={"title": "Shared list", "visibility": "public"},
    )
    playlist_id = playlist.json()["id"]
    assert (
        await api.client.put(
            f"/api/v1/playlists/{playlist_id}/videos/{private_id}", headers=owner_headers
        )
    ).status_code == 204
    api.client.cookies.clear()
    api.client.cookies.update(viewer[1])
    viewer_headers = auth_headers(viewer[1])
    comment = await api.client.post(
        f"/api/v1/videos/{private_id}/comments",
        headers=viewer_headers,
        json={"body": "Private comment sentinel"},
    )
    assert comment.status_code == 201
    paths = ("/search?q=Owner+secret", f"/channel/{owner[2]}", f"/playlist/{playlist_id}")
    # Discovery remains public-only even for a grant recipient.
    for path in paths[:2]:
        assert f"/watch/{private_id}" not in (await api.client.get(path)).text
    assert f"/watch/{private_id}" in (await api.client.get(paths[2])).text
    own_list = await api.client.post(
        "/api/v1/playlists", headers=viewer_headers, json={"title": "Viewer list"}
    )
    assert (
        await api.client.put(
            f"/api/v1/playlists/{own_list.json()['id']}/videos/{private_id}", headers=viewer_headers
        )
    ).status_code == 403
    api.client.cookies.clear()
    api.client.cookies.update(owner[1])
    assert (await api.client.delete(grant, headers=owner_headers)).status_code == 204
    api.client.cookies.clear()
    api.client.cookies.update(viewer[1])
    for path in paths:
        response = await api.client.get(path)
        assert f"/watch/{private_id}" not in response.text
        assert "Private comment sentinel" not in response.text
    assert (await api.client.get(f"/watch/{private_id}")).status_code == 404
    assert (
        await api.client.post(
            f"/api/v1/videos/{private_id}/comments",
            headers=viewer_headers,
            json={"body": "Revoked"},
        )
    ).status_code == 404


async def test_invalid_playlist_orders_leave_membership_unchanged(social_case: SocialCase) -> None:
    api, _, viewer, _, _, videos = social_case
    api.client.cookies.update(viewer[1])
    headers = auth_headers(viewer[1])
    created = await api.client.post(
        "/api/v1/playlists", headers=headers, json={"title": "Stable order"}
    )
    playlist_id = created.json()["id"]
    prefix = f"/api/v1/playlists/{playlist_id}"
    original = [str(v) for v in videos[:2]]
    for video_id in original:
        assert (
            await api.client.put(f"{prefix}/videos/{video_id}", headers=headers)
        ).status_code == 204
    for order in ([], original[:1], [original[0], original[0]], [original[0], str(uuid.uuid4())]):
        response = await api.client.put(
            f"{prefix}/order", headers=headers, json={"video_ids": order}
        )
        assert response.status_code == 422
        async with api.sessions() as db:
            actual = await db.scalars(
                select(PlaylistItem.video_id)
                .where(PlaylistItem.playlist_id == uuid.UUID(playlist_id))
                .order_by(PlaylistItem.position)
            )
            assert [str(v) for v in actual] == original
