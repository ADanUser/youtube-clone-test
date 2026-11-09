"""Channel profile API contracts."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from youtube_copy.accounts.security import normalize_handle


class ChannelUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    handle: str | None = Field(default=None, min_length=3, max_length=64)
    title: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=5000)

    @model_validator(mode="after")
    def require_change(self) -> ChannelUpdateRequest:
        if not self.model_fields_set:
            raise ValueError("at least one channel field must be supplied")
        if any(getattr(self, field) is None for field in self.model_fields_set):
            raise ValueError("channel fields must not be null")
        return self

    @field_validator("handle")
    @classmethod
    def validate_handle(cls, value: str | None) -> str | None:
        return normalize_handle(value) if value is not None else None

    @field_validator("title")
    @classmethod
    def validate_title(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise ValueError("title must not be blank")
        return stripped


class ChannelResponse(BaseModel):
    id: uuid.UUID
    handle: str
    title: str
    description: str
    created_at: datetime
    updated_at: datetime
