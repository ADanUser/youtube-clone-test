"""Report submission and role-protected moderation endpoints."""

import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from youtube_copy.accounts.dependencies import (
    require_authenticated_csrf,
    require_moderator,
    require_moderator_csrf,
)
from youtube_copy.accounts.service import AuthContext
from youtube_copy.infrastructure.database.session import get_db_session
from youtube_copy.moderation.schemas import (
    ModerationActionResponse,
    ModerationCommand,
    ReportCreate,
    ReportDecision,
    ReportResponse,
)
from youtube_copy.moderation.service import (
    create_report,
    decide_report,
    list_reports,
    moderate_video,
)

router = APIRouter(tags=["moderation"])
Database = Annotated[AsyncSession, Depends(get_db_session)]


@router.post("/videos/{video_id}/reports", status_code=201)
async def report_video(
    video_id: uuid.UUID,
    command: ReportCreate,
    db: Database,
    auth: Annotated[AuthContext, Depends(require_authenticated_csrf)],
) -> dict[str, str]:
    report = await create_report(db, video_id, auth, command)
    return {"id": str(report.id), "status": report.status}


@router.get("/moderation/reports", response_model=list[ReportResponse])
async def reports(
    db: Database,
    auth: Annotated[AuthContext, Depends(require_moderator)],
    report_status: Literal["open", "resolved", "rejected"] = Query("open", alias="status"),
) -> list[ReportResponse]:
    return [
        ReportResponse(
            id=item.report.id,
            video_id=item.report.video_id,
            reporter_id=item.report.reporter_id,
            reporter_username=item.reporter_username,
            reason=item.report.reason,
            status=item.report.status,
            created_at=item.report.created_at,
            video_visibility=item.video_visibility,
            moderation_state=item.moderation_state,
            can_view=item.can_view,
            video_title=item.video_title,
        )
        for item in await list_reports(db, auth, status=report_status)
    ]


@router.post("/moderation/videos/{video_id}", response_model=ModerationActionResponse)
async def change_moderation(
    video_id: uuid.UUID,
    command: ModerationCommand,
    db: Database,
    auth: Annotated[AuthContext, Depends(require_moderator_csrf)],
) -> ModerationActionResponse:
    return ModerationActionResponse.model_validate(
        await moderate_video(db, video_id, auth, command), from_attributes=True
    )


@router.post("/moderation/reports/{report_id}/decision")
async def report_decision(
    report_id: uuid.UUID,
    command: ReportDecision,
    db: Database,
    auth: Annotated[AuthContext, Depends(require_moderator_csrf)],
) -> dict[str, str]:
    report = await decide_report(db, report_id, auth, command)
    return {"id": str(report.id), "status": report.status}
