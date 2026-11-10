"""Allowlisted social and playlist commands."""

import uuid
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class StrictCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CommentCreate(StrictCommand):
    body: str = Field(min_length=1, max_length=5000)

    @field_validator("body")
    @classmethod
    def nonblank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("comment must not be blank")
        return value


class PlaylistCreate(StrictCommand):
    title: str = Field(min_length=1, max_length=200)
    visibility: Literal["public", "private"] = "private"

    @field_validator("title")
    @classmethod
    def nonblank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("title must not be blank")
        return value


class PlaylistReorder(StrictCommand):
    video_ids: list[uuid.UUID] = Field(max_length=500)
