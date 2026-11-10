"""Authorized media responses; no object URLs escape the server."""

import uuid
from collections.abc import AsyncIterator, Callable
from typing import Annotated

import anyio
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.types import Receive, Scope, Send

from youtube_copy.accounts.dependencies import get_optional_auth, require_authenticated_csrf
from youtube_copy.accounts.service import AuthContext
from youtube_copy.infrastructure.database.session import get_db_session
from youtube_copy.infrastructure.media import MediaStorage
from youtube_copy.videos.playback import change_grant, view_asset, view_video
from youtube_copy.videos.ranges import UnsatisfiableRangeError, byte_range
from youtube_copy.videos.schemas import VideoStatus
from youtube_copy.videos.service import blocking

router = APIRouter(prefix="/videos", tags=["playback"])
Database = Annotated[AsyncSession, Depends(get_db_session)]
Viewer = Annotated[AuthContext | None, Depends(get_optional_auth)]
Owner = Annotated[AuthContext, Depends(require_authenticated_csrf)]
NO_STORE = {"Cache-Control": "private, no-store"}


async def io[T](call: Callable[..., T], *args: object) -> T:
    # Wait for blocking I/O before cleanup, including cancellation during S3 open/read.
    with anyio.CancelScope(shield=True):
        return await blocking(call, *args)


class MediaResponse(StreamingResponse):
    def __init__(
        self, storage: MediaStorage, length: int, status: int, headers: dict[str, str]
    ) -> None:
        self.storage = storage
        super().__init__(self.chunks(length), status_code=status, headers=headers)

    async def chunks(self, remaining: int) -> AsyncIterator[bytes]:
        while remaining:
            chunk = await io(self.storage.read, min(256 * 1024, remaining))
            if not chunk:
                raise OSError("media stream interrupted")
            remaining -= len(chunk)
            yield chunk

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            # Covers disconnect/send failure before the iterator even starts.
            await io(self.storage.close)


async def media(
    video_id: uuid.UUID, request: Request, db: AsyncSession, auth: AuthContext | None, kind: str
) -> Response:
    key, size = await view_asset(db, video_id, kind, auth)
    storage = None
    transferred = False
    try:
        storage = MediaStorage(request.app.state.settings)
        head = await io(storage.head, key)
        if head["ContentLength"] != size:
            raise OSError("media size mismatch")
        etag = head["ETag"]
        headers = {
            **NO_STORE,
            "Content-Type": "video/mp4" if kind == "mp4" else "image/jpeg",
            "Accept-Ranges": "bytes",
            "Content-Length": str(size),
            "ETag": etag,
            "X-Content-Type-Options": "nosniff",
        }
        if request.method == "HEAD":
            return Response(headers=headers)
        selected = None
        # We advertise only a strong ETag, not a strong Last-Modified date validator.
        if_range = request.headers.get("if-range")
        if if_range is None or (if_range == etag and not etag.startswith("W/")):
            try:
                selected = byte_range(",".join(request.headers.getlist("range")) or None, size)
            except UnsatisfiableRangeError:
                return Response(
                    status_code=416,
                    headers={
                        **NO_STORE,
                        "Content-Range": f"bytes */{size}",
                        "Accept-Ranges": "bytes",
                    },
                )
        length = size if selected is None else selected[1] - selected[0] + 1
        result = await io(storage.open, key, etag, selected)
        if result["ContentLength"] != length or result["ETag"] != etag:
            raise OSError("media representation changed")
        if selected:
            content_range = f"bytes {selected[0]}-{selected[1]}/{size}"
            if result.get("ContentRange") != content_range:
                raise OSError("media range mismatch")
            headers["Content-Range"] = content_range
        headers["Content-Length"] = str(length)
        response = MediaResponse(storage, length, 206 if selected else 200, headers)
        transferred = True
        return response
    except Exception as error:
        raise HTTPException(503, "media_unavailable", headers=NO_STORE) from error
    finally:
        if storage is not None and not transferred:
            await io(storage.close)


MEDIA_RESPONSES = {
    200: {"description": "Full representation (HEAD has no body)"},
    206: {"description": "Single byte range; Content-Range and Content-Length"},
    404: {"description": "Video unavailable or access denied"},
    416: {"description": "Unsatisfiable range; Content-Range: bytes */size"},
    503: {"description": "Media storage unavailable"},
}


@router.get("/{video_id}/stream", responses=MEDIA_RESPONSES, response_class=Response)
@router.head("/{video_id}/stream", responses=MEDIA_RESPONSES, response_class=Response)
async def stream(video_id: uuid.UUID, request: Request, db: Database, auth: Viewer) -> Response:
    return await media(video_id, request, db, auth, "mp4")


@router.get("/{video_id}/thumbnail", responses=MEDIA_RESPONSES, response_class=Response)
@router.head("/{video_id}/thumbnail", responses=MEDIA_RESPONSES, response_class=Response)
async def thumbnail(video_id: uuid.UUID, request: Request, db: Database, auth: Viewer) -> Response:
    return await media(video_id, request, db, auth, "thumbnail")


@router.get("/{video_id}", response_model=VideoStatus)
async def metadata(
    video_id: uuid.UUID, response: Response, db: Database, auth: Viewer
) -> VideoStatus:
    response.headers.update(NO_STORE)
    return await view_video(db, video_id, auth)


@router.put("/{video_id}/grants/{user_id}", status_code=204)
async def grant(video_id: uuid.UUID, user_id: uuid.UUID, db: Database, auth: Owner) -> Response:
    await change_grant(db, video_id, user_id, auth, grant=True)
    return Response(status_code=204, headers=NO_STORE)


@router.delete("/{video_id}/grants/{user_id}", status_code=204)
async def revoke(video_id: uuid.UUID, user_id: uuid.UUID, db: Database, auth: Owner) -> Response:
    await change_grant(db, video_id, user_id, auth, grant=False)
    return Response(status_code=204, headers=NO_STORE)
