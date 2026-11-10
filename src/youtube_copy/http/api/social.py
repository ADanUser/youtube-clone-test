"""CSRF-protected social and playlist endpoints."""

import uuid

from fastapi import APIRouter, Response

from youtube_copy.http.api.playback import Database, Owner
from youtube_copy.social.schemas import CommentCreate, PlaylistCreate, PlaylistReorder
from youtube_copy.social.service import (
    add_playlist_video,
    create_comment,
    create_playlist,
    delete_comment,
    remove_playlist_video,
    reorder_playlist,
    set_like,
    set_subscription,
)

router = APIRouter(tags=["social"])
NO_STORE = {"Cache-Control": "private, no-store"}


@router.put("/videos/{video_id}/like", status_code=204)
async def like(video_id: uuid.UUID, db: Database, auth: Owner) -> Response:
    await set_like(db, video_id, auth, enabled=True)
    return Response(status_code=204, headers=NO_STORE)


@router.delete("/videos/{video_id}/like", status_code=204)
async def unlike(video_id: uuid.UUID, db: Database, auth: Owner) -> Response:
    await set_like(db, video_id, auth, enabled=False)
    return Response(status_code=204, headers=NO_STORE)


@router.post("/videos/{video_id}/comments", status_code=201)
async def comment(
    video_id: uuid.UUID, command: CommentCreate, db: Database, auth: Owner
) -> dict[str, str]:
    created = await create_comment(db, video_id, auth, command)
    return {"id": str(created.id), "body": created.body}


@router.delete("/comments/{comment_id}", status_code=204)
async def remove_comment(comment_id: uuid.UUID, db: Database, auth: Owner) -> Response:
    await delete_comment(db, comment_id, auth)
    return Response(status_code=204, headers=NO_STORE)


@router.put("/channels/{channel_id}/subscription", status_code=204)
async def subscribe(channel_id: uuid.UUID, db: Database, auth: Owner) -> Response:
    await set_subscription(db, channel_id, auth, enabled=True)
    return Response(status_code=204, headers=NO_STORE)


@router.delete("/channels/{channel_id}/subscription", status_code=204)
async def unsubscribe(channel_id: uuid.UUID, db: Database, auth: Owner) -> Response:
    await set_subscription(db, channel_id, auth, enabled=False)
    return Response(status_code=204, headers=NO_STORE)


@router.post("/playlists", status_code=201)
async def new_playlist(command: PlaylistCreate, db: Database, auth: Owner) -> dict[str, str]:
    playlist = await create_playlist(db, auth, command)
    return {"id": str(playlist.id), "title": playlist.title}


@router.put("/playlists/{playlist_id}/videos/{video_id}", status_code=204)
async def add_video(
    playlist_id: uuid.UUID, video_id: uuid.UUID, db: Database, auth: Owner
) -> Response:
    await add_playlist_video(db, playlist_id, video_id, auth)
    return Response(status_code=204, headers=NO_STORE)


@router.delete("/playlists/{playlist_id}/videos/{video_id}", status_code=204)
async def remove_video(
    playlist_id: uuid.UUID, video_id: uuid.UUID, db: Database, auth: Owner
) -> Response:
    await remove_playlist_video(db, playlist_id, video_id, auth)
    return Response(status_code=204, headers=NO_STORE)


@router.put("/playlists/{playlist_id}/order", status_code=204)
async def order(
    playlist_id: uuid.UUID, command: PlaylistReorder, db: Database, auth: Owner
) -> Response:
    await reorder_playlist(db, playlist_id, command.video_ids, auth)
    return Response(status_code=204, headers=NO_STORE)
