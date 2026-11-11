"""Social uniqueness, ownership, playlist ordering, and privacy boundaries."""

import uuid

import pytest
from sqlalchemy import func, select

from test_accounts_api import ApiContext, csrf_headers, register
from test_accounts_api import api_context as api_context
from youtube_copy.accounts.dependencies import CSRF_COOKIE_NAME
from youtube_copy.infrastructure.database.models import (
    Channel,
    Comment,
    Like,
    PlaylistItem,
    Subscription,
    Video,
)


def auth_headers(cookies: dict[str, str]) -> dict[str, str]:
    return csrf_headers(cookies[CSRF_COOKIE_NAME])


@pytest.fixture
async def social_case(api_context: ApiContext):
    api = api_context
    identities = []
    for label in ("owner", "viewer"):
        name = f"social-{label}-" + uuid.uuid4().hex[:8]
        response = await register(
            api.client, name, name + "@example.com", "correct horse battery staple"
        )
        identities.append((uuid.UUID(response.json()["id"]), dict(api.client.cookies), name))
        api.client.cookies.clear()
    owner, viewer = identities
    async with api.sessions() as db:
        owner_channel = await db.scalar(select(Channel).where(Channel.user_id == owner[0]))
        viewer_channel = await db.scalar(select(Channel).where(Channel.user_id == viewer[0]))
        videos = [
            Video(
                channel_id=owner_channel.id,
                title="Public one",
                visibility="public",
                processing_state="ready",
            ),
            Video(
                channel_id=owner_channel.id,
                title="Public two",
                visibility="public",
                processing_state="ready",
            ),
            Video(
                channel_id=owner_channel.id,
                title="Owner secret",
                visibility="private",
                processing_state="ready",
            ),
        ]
        db.add_all(videos)
        await db.commit()
        video_ids = [video.id for video in videos]
    return api, owner, viewer, owner_channel.id, viewer_channel.id, video_ids


async def test_likes_comments_and_subscriptions_are_unique_and_authorized(social_case):
    api, owner, viewer, owner_channel_id, viewer_channel_id, videos = social_case
    public_id, _, private_id = videos
    api.client.cookies.update(viewer[1])
    headers = auth_headers(viewer[1])
    for _ in range(2):
        assert (
            await api.client.put(f"/api/v1/videos/{public_id}/like", headers=headers)
        ).status_code == 204
    async with api.sessions() as db:
        assert (
            await db.scalar(
                select(func.count()).select_from(Like).where(Like.video_id == public_id)
            )
            == 1
        )
    assert (
        await api.client.put(f"/api/v1/videos/{private_id}/like", headers=headers)
    ).status_code == 404
    api.client.cookies.clear()
    assert (await api.client.put(f"/api/v1/videos/{public_id}/like")).status_code == 401
    assert (await api.client.put(f"/api/v1/videos/{private_id}/like")).status_code == 401
    api.client.cookies.update(viewer[1])
    comment = await api.client.post(
        f"/api/v1/videos/{public_id}/comments",
        headers=headers,
        json={"body": "<script>alert(1)</script>"},
    )
    assert comment.status_code == 201
    page = await api.client.get(f"/watch/{public_id}")
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page.text
    comment_id = comment.json()["id"]
    api.client.cookies.clear()
    api.client.cookies.update(owner[1])
    assert (
        await api.client.delete(f"/api/v1/comments/{comment_id}", headers=auth_headers(owner[1]))
    ).status_code == 404
    api.client.cookies.clear()
    api.client.cookies.update(viewer[1])
    for _ in range(2):
        assert (
            await api.client.put(
                f"/api/v1/channels/{owner_channel_id}/subscription", headers=headers
            )
        ).status_code == 204
    assert (
        await api.client.put(f"/api/v1/channels/{viewer_channel_id}/subscription", headers=headers)
    ).status_code == 409
    async with api.sessions() as db:
        assert (
            await db.scalar(
                select(func.count())
                .select_from(Subscription)
                .where(Subscription.channel_id == owner_channel_id)
            )
            == 1
        )
        assert (
            await db.scalar(
                select(func.count()).select_from(Comment).where(Comment.video_id == public_id)
            )
            == 1
        )


async def test_playlists_filter_private_videos_and_preserve_order(social_case):
    api, owner, viewer, _, _, videos = social_case
    first_id, second_id, private_id = videos
    api.client.cookies.update(viewer[1])
    viewer_headers = auth_headers(viewer[1])
    created = await api.client.post(
        "/api/v1/playlists",
        headers=viewer_headers,
        json={"title": "Viewer list", "visibility": "public"},
    )
    playlist_id = created.json()["id"]
    for video_id in (first_id, second_id):
        for _ in range(2):
            assert (
                await api.client.put(
                    f"/api/v1/playlists/{playlist_id}/videos/{video_id}",
                    headers=viewer_headers,
                )
            ).status_code == 204
    forbidden = await api.client.put(
        f"/api/v1/playlists/{playlist_id}/videos/{private_id}", headers=viewer_headers
    )
    assert forbidden.status_code == 404
    order = await api.client.put(
        f"/api/v1/playlists/{playlist_id}/order",
        headers=viewer_headers,
        json={"video_ids": [str(second_id), str(first_id)]},
    )
    assert order.status_code == 204
    async with api.sessions() as db:
        rows = list(
            await db.scalars(
                select(PlaylistItem.video_id)
                .where(PlaylistItem.playlist_id == uuid.UUID(playlist_id))
                .order_by(PlaylistItem.position)
            )
        )
        assert rows == [second_id, first_id]
    api.client.cookies.clear()
    public_page = await api.client.get(f"/playlist/{playlist_id}")
    assert public_page.status_code == 200
    assert public_page.text.index("Public two") < public_page.text.index("Public one")
    assert "Owner secret" not in public_page.text
    api.client.cookies.update(owner[1])
    owner_headers = auth_headers(owner[1])
    owner_list = await api.client.post(
        "/api/v1/playlists",
        headers=owner_headers,
        json={"title": "Owner list", "visibility": "public"},
    )
    owner_playlist_id = owner_list.json()["id"]
    assert (
        await api.client.put(
            f"/api/v1/playlists/{owner_playlist_id}/videos/{private_id}",
            headers=owner_headers,
        )
    ).status_code == 204
    api.client.cookies.clear()
    filtered = await api.client.get(f"/playlist/{owner_playlist_id}")
    assert filtered.status_code == 200 and "Owner secret" not in filtered.text
