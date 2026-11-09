"""Unit checks for authentication security primitives."""

from __future__ import annotations

from youtube_copy.accounts.rate_limit import SlidingWindowRateLimiter
from youtube_copy.accounts.security import (
    hash_password,
    issue_preauth_csrf,
    verify_password,
    verify_preauth_csrf,
)


async def test_passwords_use_argon2id_and_reject_a_mismatch() -> None:
    password_hash = await hash_password("a-long-test-password")

    assert password_hash.startswith("$argon2id$")
    assert await verify_password("a-long-test-password", password_hash)
    assert not await verify_password("wrong-password", password_hash)
    assert not await verify_password("wrong-password", None)


def test_preauth_csrf_signature_and_expiry() -> None:
    secret = "test-secret-that-is-at-least-thirty-two-characters"
    token = issue_preauth_csrf(secret, issued_at=1_000)

    assert verify_preauth_csrf(token, secret, ttl_seconds=300, now=1_300)
    assert not verify_preauth_csrf(token, secret, ttl_seconds=300, now=1_301)
    assert not verify_preauth_csrf(token, f"{secret}-wrong", ttl_seconds=300, now=1_100)


async def test_rate_limiter_returns_retry_delay() -> None:
    limiter = SlidingWindowRateLimiter()

    assert (await limiter.check("client", limit=2, window_seconds=60)).allowed
    assert (await limiter.check("client", limit=2, window_seconds=60)).allowed
    rejected = await limiter.check("client", limit=2, window_seconds=60)

    assert not rejected.allowed
    assert 1 <= rejected.retry_after_seconds <= 60
