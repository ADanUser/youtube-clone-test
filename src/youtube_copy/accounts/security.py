"""Password, token, normalization, and CSRF primitives."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import re
import secrets
import time

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from argon2.low_level import Type

_IDENTIFIER_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9._-]{1,62}[a-z0-9])?$")
_PASSWORD_HASHER = PasswordHasher(
    time_cost=3,
    memory_cost=65_536,
    parallelism=4,
    hash_len=32,
    salt_len=16,
    type=Type.ID,
)
_DUMMY_PASSWORD_HASH = (
    "$argon2id$v=19$m=65536,t=3,p=4$natpXwL2bu3WxeOrLecb2Q$"
    "bfwgoeJlVuB32j024peQ8ewbPbXdPiSqbXcqEq+HDFk"
)


def normalize_username(value: str) -> str:
    """Normalize and validate a username stored as a stable login identifier."""
    normalized = value.strip().casefold()
    if not _IDENTIFIER_PATTERN.fullmatch(normalized):
        raise ValueError(
            "username must use 3-64 lowercase letters, digits, dots, underscores, or hyphens"
        )
    return normalized


def normalize_handle(value: str) -> str:
    """Apply the same safe alphabet to public channel handles."""
    return normalize_username(value)


def hash_token(token: str) -> str:
    """Store only a deterministic cryptographic digest of opaque tokens."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_opaque_token() -> str:
    return secrets.token_urlsafe(32)


async def hash_password(password: str) -> str:
    """Run memory-hard Argon2id outside the event loop."""
    return await asyncio.to_thread(_PASSWORD_HASHER.hash, password)


async def verify_password(password: str, password_hash: str | None) -> bool:
    """Verify real and missing accounts with comparable Argon2id work."""
    candidate_hash = password_hash or _DUMMY_PASSWORD_HASH

    def verify() -> bool:
        try:
            return _PASSWORD_HASHER.verify(candidate_hash, password)
        except (InvalidHashError, VerifyMismatchError):
            return False

    valid = await asyncio.to_thread(verify)
    return valid and password_hash is not None


def issue_preauth_csrf(secret: str, issued_at: int | None = None) -> str:
    """Create a signed, time-bounded token for login and registration."""
    timestamp = int(time.time()) if issued_at is None else issued_at
    nonce = new_opaque_token()
    payload = f"{timestamp}.{nonce}"
    signature = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{signature}"


def verify_preauth_csrf(token: str, secret: str, ttl_seconds: int, now: int | None = None) -> bool:
    """Validate signature and age without exposing why a token failed."""
    try:
        timestamp_text, nonce, supplied_signature = token.split(".", 2)
        timestamp = int(timestamp_text)
    except (TypeError, ValueError):
        return False
    current_time = int(time.time()) if now is None else now
    if not nonce or timestamp > current_time + 30 or current_time - timestamp > ttl_seconds:
        return False
    payload = f"{timestamp}.{nonce}"
    expected = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(supplied_signature, expected)
