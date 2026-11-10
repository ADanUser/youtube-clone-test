"""Account, authentication, and administrative application services."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from youtube_copy.accounts.schemas import AdminUserUpdateRequest, RegisterRequest
from youtube_copy.accounts.security import (
    hash_password,
    hash_token,
    new_opaque_token,
    verify_password,
)
from youtube_copy.infrastructure.database.models import Channel, Session, User


class AccountConflictError(Exception):
    """A normalized email, username, or handle already exists."""


class InvalidCredentialsError(Exception):
    """Credentials are invalid or the account cannot sign in."""


class AccountNotFoundError(Exception):
    """The requested account does not exist."""


class LastAdministratorError(Exception):
    """An operation would remove the last active administrator."""


@dataclass(frozen=True)
class IssuedSession:
    token: str
    csrf_token: str
    expires_at: datetime


@dataclass(frozen=True)
class AuthContext:
    session: Session
    user: User
    channel: Channel


def _new_session(user_id: uuid.UUID, ttl_seconds: int) -> tuple[Session, IssuedSession]:
    token = new_opaque_token()
    csrf_token = new_opaque_token()
    expires_at = datetime.now(UTC) + timedelta(seconds=ttl_seconds)
    session = Session(
        id=uuid.uuid4(),
        user_id=user_id,
        token_hash=hash_token(token),
        csrf_token_hash=hash_token(csrf_token),
        expires_at=expires_at,
    )
    return session, IssuedSession(token, csrf_token, expires_at)


async def register_account(
    db: AsyncSession, command: RegisterRequest, session_ttl_seconds: int
) -> tuple[AuthContext, IssuedSession]:
    """Atomically create a user, its one channel, and an authenticated session."""
    password_hash = await hash_password(command.password)
    user_id = uuid.uuid4()
    channel_id = uuid.uuid4()
    user = User(
        id=user_id,
        email_normalized=str(command.email).casefold(),
        username_normalized=command.username,
        password_hash=password_hash,
        role="user",
        is_active=True,
    )
    channel = Channel(
        id=channel_id,
        user_id=user_id,
        handle=command.username,
        title=command.channel_title or command.username,
        description="",
    )
    session, issued = _new_session(user_id, session_ttl_seconds)
    try:
        async with db.begin():
            db.add(user)
            await db.flush()
            db.add_all((channel, session))
        await db.refresh(user)
        await db.refresh(channel)
    except IntegrityError as error:
        await db.rollback()
        raise AccountConflictError from error
    return AuthContext(session, user, channel), issued


async def login_account(
    db: AsyncSession,
    login: str,
    password: str,
    session_ttl_seconds: int,
    previous_token: str | None,
) -> tuple[AuthContext, IssuedSession]:
    """Verify credentials and rotate any browser session supplied by the caller."""
    result = await db.execute(
        select(User, Channel)
        .join(Channel, Channel.user_id == User.id)
        .where(or_(User.email_normalized == login, User.username_normalized == login))
    )
    row = result.one_or_none()
    user = row[0] if row else None
    valid_password = await verify_password(password, user.password_hash if user else None)
    if user is None or not valid_password or not user.is_active:
        raise InvalidCredentialsError

    channel = row[1]
    session, issued = _new_session(user.id, session_ttl_seconds)
    if previous_token:
        await db.execute(delete(Session).where(Session.token_hash == hash_token(previous_token)))
    db.add(session)
    await db.commit()
    return AuthContext(session, user, channel), issued


async def authenticate_session(db: AsyncSession, raw_token: str | None) -> AuthContext | None:
    """Resolve the session and re-check expiry and account state on every request."""
    if not raw_token:
        return None
    result = await db.execute(
        select(Session, User, Channel)
        .join(User, User.id == Session.user_id)
        .join(Channel, Channel.user_id == User.id)
        .where(Session.token_hash == hash_token(raw_token))
    )
    row = result.one_or_none()
    if row is None:
        return None
    session, user, channel = row
    if session.expires_at <= datetime.now(UTC) or not user.is_active:
        await db.delete(session)
        await db.commit()
        return None
    return AuthContext(session, user, channel)


async def logout_session(db: AsyncSession, session_id: uuid.UUID) -> None:
    await db.execute(delete(Session).where(Session.id == session_id))
    await db.commit()


async def update_account_as_admin(
    db: AsyncSession, user_id: uuid.UUID, command: AdminUserUpdateRequest
) -> User:
    """Update role/state while serializing protection of the last active admin."""
    active_admin_ids = list(
        (
            await db.scalars(
                select(User.id)
                .where(User.role == "admin", User.is_active.is_(True))
                .order_by(User.id)
                .with_for_update()
            )
        ).all()
    )
    user = await db.scalar(select(User).where(User.id == user_id).with_for_update())
    if user is None:
        raise AccountNotFoundError

    removes_active_admin = user.id in active_admin_ids and (
        (command.role is not None and command.role != "admin") or command.is_active is False
    )
    if removes_active_admin and len(active_admin_ids) == 1:
        raise LastAdministratorError

    if command.role is not None:
        user.role = command.role
    if command.is_active is not None:
        user.is_active = command.is_active
    if not user.is_active:
        await db.execute(delete(Session).where(Session.user_id == user.id))
    await db.commit()
    await db.refresh(user)
    return user


async def list_accounts_as_admin(db: AsyncSession) -> list[User]:
    return list((await db.scalars(select(User).order_by(User.created_at, User.id))).all())
