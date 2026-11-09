"""Owner-only draft and streaming upload endpoints."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import ClientDisconnect

from youtube_copy.accounts.dependencies import get_current_auth, require_authenticated_csrf
from youtube_copy.accounts.service import AuthContext
from youtube_copy.infrastructure.database.session import get_db_session
from youtube_copy.videos.errors import UploadError
from youtube_copy.videos.schemas import VideoCreate, VideoStatus
from youtube_copy.videos.service import create_video, get_upload_status, receive_original

router = APIRouter(prefix="/videos", tags=["uploads"])
Database = Annotated[AsyncSession, Depends(get_db_session)]
Authenticated = Annotated[AuthContext, Depends(require_authenticated_csrf)]


async def limit_uploads(request: Request, auth: AuthContext, action: str) -> None:
    settings = request.app.state.settings
    decision = await request.app.state.upload_rate_limiter.check(
        f"{action}:{auth.user.id}",
        settings.upload_rate_limit_attempts,
        settings.upload_rate_limit_window_seconds,
    )
    if not decision.allowed:
        raise HTTPException(
            429, "upload_rate_limit", headers={"Retry-After": str(decision.retry_after_seconds)}
        )


@router.post("", response_model=VideoStatus, status_code=201)
async def draft_video(
    command: VideoCreate, request: Request, response: Response, db: Database, auth: Authenticated
) -> VideoStatus:
    response.headers["Cache-Control"] = "private, no-store"
    await limit_uploads(request, auth, "draft")
    return await create_video(db, auth, command)


@router.get("/{video_id}/status", response_model=VideoStatus)
async def upload_status(
    video_id: uuid.UUID,
    response: Response,
    db: Database,
    auth: Annotated[AuthContext, Depends(get_current_auth)],
) -> VideoStatus:
    response.headers["Cache-Control"] = "private, no-store"
    return await get_upload_status(db, video_id, auth.channel.id)


@router.post(
    "/{video_id}/upload",
    response_model=VideoStatus,
    status_code=202,
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "application/octet-stream": {"schema": {"type": "string", "format": "binary"}}
            },
        }
    },
)
async def upload_video(
    video_id: uuid.UUID, request: Request, response: Response, db: Database, auth: Authenticated
) -> VideoStatus:
    response.headers["Cache-Control"] = "private, no-store"
    await limit_uploads(request, auth, "upload")
    # Raw bytes avoid multipart parsers spooling files before authentication/limits.
    if request.headers.get("content-type", "").lower() != "application/octet-stream":
        raise UploadError(415, "use_application_octet_stream")
    if request.headers.get("content-encoding", "identity").lower() != "identity":
        raise UploadError(415, "encoded_upload_unsupported")
    length_header = request.headers.get("content-length")
    if length_header is not None and (
        not length_header.isascii() or not length_header.isdigit() or len(length_header) > 20
    ):
        raise UploadError(400, "invalid_content_length")
    try:
        return await receive_original(
            db,
            auth,
            video_id,
            request.stream(),
            request.app.state.settings,
            request.app.state.upload_slots,
            request.app.state.original_storage,
            int(length_header) if length_header is not None else None,
        )
    except ClientDisconnect as error:
        raise UploadError(400, "upload_interrupted") from error
