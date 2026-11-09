"""Explicit account API input and output models."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator, model_validator

from youtube_copy.accounts.security import normalize_username


class StrictInputModel(BaseModel):
    """Reject fields that are not part of the public command contract."""

    model_config = ConfigDict(extra="forbid")


class RegisterRequest(StrictInputModel):
    email: EmailStr = Field(max_length=254)
    username: str = Field(min_length=3, max_length=64)
    password: str = Field(min_length=12, max_length=128)
    channel_title: str | None = Field(default=None, min_length=1, max_length=120)

    @field_validator("username")
    @classmethod
    def validate_username(cls, value: str) -> str:
        return normalize_username(value)

    @field_validator("password")
    @classmethod
    def validate_password_bytes(cls, value: str) -> str:
        if len(value.encode("utf-8")) > 128:
            raise ValueError("password must not exceed 128 UTF-8 bytes")
        return value

    @field_validator("channel_title")
    @classmethod
    def strip_channel_title(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise ValueError("channel title must not be blank")
        return stripped


class LoginRequest(StrictInputModel):
    login: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=128)

    @field_validator("login")
    @classmethod
    def normalize_login(cls, value: str) -> str:
        return value.strip().casefold()

    @field_validator("password")
    @classmethod
    def validate_password_bytes(cls, value: str) -> str:
        if len(value.encode("utf-8")) > 128:
            raise ValueError("password must not exceed 128 UTF-8 bytes")
        return value


class AdminUserUpdateRequest(StrictInputModel):
    role: Literal["user", "moderator", "admin"] | None = None
    is_active: bool | None = None

    @model_validator(mode="after")
    def require_change(self) -> AdminUserUpdateRequest:
        if self.role is None and self.is_active is None:
            raise ValueError("at least one account field must be supplied")
        return self


class AccountResponse(BaseModel):
    id: uuid.UUID
    email: str
    username: str
    role: Literal["user", "moderator", "admin"]
    is_active: bool
    created_at: datetime


class AuthenticatedAccountResponse(AccountResponse):
    channel_id: uuid.UUID
    channel_handle: str


class CsrfTokenResponse(BaseModel):
    csrf_token: str


class MessageResponse(BaseModel):
    message: str
