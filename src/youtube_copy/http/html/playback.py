"""Minimal HTML5 player using the same viewing policy as media and metadata."""

import uuid

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select

from youtube_copy.http.api.playback import NO_STORE, Database, Viewer
from youtube_copy.http.html.uploads import templates
from youtube_copy.infrastructure.database.models import Channel, Video
from youtube_copy.videos.playback import view_video

router = APIRouter()


@router.get("/watch/{video_id}", response_class=HTMLResponse, include_in_schema=False)
async def watch(video_id: uuid.UUID, request: Request, db: Database, auth: Viewer) -> HTMLResponse:
    video = await view_video(db, video_id, auth)
    channel = await db.scalar(
        select(Channel).join(Video, Video.channel_id == Channel.id).where(Video.id == video_id)
    )
    return templates.TemplateResponse(
        request=request,
        name="watch.html",
        context={"video": video, "channel": channel, "auth": auth},
        headers=NO_STORE,
    )
