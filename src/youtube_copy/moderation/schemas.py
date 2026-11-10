"""Strict moderation command and response contracts."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from youtube_copy.accounts.schemas import StrictInputModel


class ReportCreate(StrictInputModel):
    reason: str = Field(min_length=1, max_length=2000)

    @field_validator("reason")
    @classmethod
    def strip_reason(cls, value: str) -> str:
        if not (stripped := value.strip()):
            raise ValueError("reason must not be blank")
        return stripped


class ModerationCommand(ReportCreate):
    action: Literal["hide", "restore"]
    report_id: uuid.UUID | None = None


class ReportDecision(ReportCreate):
    status: Literal["rejected"]


class ReportResponse(BaseModel):
    id: uuid.UUID
    video_id: uuid.UUID
    reporter_id: uuid.UUID
    reporter_username: str
    reason: str
    status: Literal["open", "resolved", "rejected"]
    created_at: datetime
    video_visibility: Literal["public", "private"]
    moderation_state: Literal["visible", "hidden"]
    can_view: bool
    video_title: str | None = None


class ModerationActionResponse(BaseModel):
    id: uuid.UUID
    video_id: uuid.UUID
    report_id: uuid.UUID | None
    action: Literal["hide", "restore"]
    reason: str
    created_at: datetime
