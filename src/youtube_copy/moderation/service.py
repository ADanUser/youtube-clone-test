"""Moderation workflows with privacy-preserving report metadata and append-only audit."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from youtube_copy.accounts.service import AuthContext
from youtube_copy.infrastructure.database.models import (
    ModerationAction,
    Report,
    User,
    Video,
)
from youtube_copy.moderation.schemas import ModerationCommand, ReportCreate, ReportDecision
from youtube_copy.videos.errors import UploadError
from youtube_copy.videos.playback import can_view_video


@dataclass(frozen=True)
class ReportView:
    report: Report
    reporter_username: str
    video_visibility: str
    moderation_state: str
    can_view: bool
    video_title: str | None


async def create_report(
    db: AsyncSession, video_id: uuid.UUID, auth: AuthContext, command: ReportCreate
) -> Report:
    if not await db.scalar(
        select(Video.id).where(Video.id == video_id, can_view_video(auth.user.id))
    ):
        raise UploadError(404, "video_not_found")
    report = Report(video_id=video_id, reporter_id=auth.user.id, reason=command.reason)
    db.add(report)
    try:
        await db.commit()
    except IntegrityError as error:
        await db.rollback()
        raise UploadError(409, "open_report_exists") from error
    await db.refresh(report)
    return report


async def list_reports(
    db: AsyncSession, auth: AuthContext, *, status: str = "open"
) -> list[ReportView]:
    viewable = can_view_video(auth.user.id)
    rows = (
        await db.execute(
            select(
                Report,
                User.username_normalized,
                Video.visibility,
                Video.moderation_state,
                viewable.label("can_view"),
                Video.title,
            )
            .join(User, User.id == Report.reporter_id)
            .join(Video, Video.id == Report.video_id)
            .where(Report.status == status)
            .order_by(Report.created_at, Report.id)
        )
    ).all()
    return [
        ReportView(report, username, visibility, state, allowed, title if allowed else None)
        for report, username, visibility, state, allowed, title in rows
    ]


async def moderate_video(
    db: AsyncSession, video_id: uuid.UUID, auth: AuthContext, command: ModerationCommand
) -> ModerationAction:
    video = await db.scalar(select(Video).where(Video.id == video_id).with_for_update())
    if video is None or video.deleted_at is not None:
        raise UploadError(404, "video_not_found")
    report = None
    if command.report_id is not None:
        report = await db.scalar(
            select(Report)
            .where(Report.id == command.report_id, Report.video_id == video_id)
            .with_for_update()
        )
        if report is None or report.status != "open":
            raise UploadError(409, "report_not_open")
    video.moderation_state = "hidden" if command.action == "hide" else "visible"
    action = ModerationAction(
        video_id=video.id,
        actor_id=auth.user.id,
        report_id=report.id if report else None,
        action=command.action,
        reason=command.reason,
    )
    db.add(action)
    if report:
        report.status = "resolved"
        report.resolved_by = auth.user.id
        report.resolved_at = datetime.now(UTC)
    await db.commit()
    await db.refresh(action)
    return action


async def decide_report(
    db: AsyncSession, report_id: uuid.UUID, auth: AuthContext, command: ReportDecision
) -> Report:
    report = await db.scalar(select(Report).where(Report.id == report_id).with_for_update())
    if report is None:
        raise UploadError(404, "report_not_found")
    if report.status != "open":
        raise UploadError(409, "report_not_open")
    report.status = command.status
    report.resolved_by = auth.user.id
    report.resolved_at = datetime.now(UTC)
    await db.commit()
    await db.refresh(report)
    return report
