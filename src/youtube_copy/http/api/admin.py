"""Administrator-only account state and role endpoints."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from youtube_copy.accounts.dependencies import require_admin
from youtube_copy.accounts.schemas import AccountResponse, AdminUserUpdateRequest
from youtube_copy.accounts.service import (
    AccountNotFoundError,
    AuthContext,
    LastAdministratorError,
    update_account_as_admin,
)
from youtube_copy.http.api.responses import account_response
from youtube_copy.infrastructure.database.session import get_db_session

router = APIRouter(prefix="/admin/users", tags=["administration"])


@router.patch("/{user_id}", response_model=AccountResponse)
async def update_user(
    user_id: uuid.UUID,
    command: AdminUserUpdateRequest,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    _admin: Annotated[AuthContext, Depends(require_admin)],
) -> AccountResponse:
    try:
        user = await update_account_as_admin(db, user_id, command)
    except AccountNotFoundError as error:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "account not found") from error
    except LastAdministratorError as error:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "cannot remove the last active administrator"
        ) from error
    return account_response(user)
