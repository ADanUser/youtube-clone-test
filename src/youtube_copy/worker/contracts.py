"""Immutable data crossing the database, storage and media processing boundaries."""

import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class TranscodePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    upload_id: uuid.UUID


class DeletePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    keys: list[str] = Field(max_length=100)


@dataclass(frozen=True)
class Lease:
    id: uuid.UUID
    token: uuid.UUID
    video_id: uuid.UUID | None
    kind: str
    payload: dict[str, Any]


@dataclass(frozen=True)
class Source:
    video_id: uuid.UUID
    upload_id: uuid.UUID
    object_key: str
    size_bytes: int
    sha256: str


@dataclass(frozen=True)
class MediaInfo:
    duration_ms: int
    width: int
    height: int


@dataclass(frozen=True)
class Artifact:
    kind: Literal["mp4", "thumbnail"]
    object_key: str
    size_bytes: int
    sha256: str
    mime_type: str


@dataclass(frozen=True)
class LocalMedia:
    info: MediaInfo
    mp4: Path
    thumbnail: Path


class ProcessingError(Exception):
    def __init__(self, code: str, *, retryable: bool = False) -> None:
        self.code = code
        self.retryable = retryable
        super().__init__(code)


class LeaseLostError(Exception):
    """Stop processing without altering the state owned by a newer worker."""
