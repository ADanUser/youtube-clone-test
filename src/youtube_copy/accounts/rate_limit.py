"""Bounded in-process sliding-window limits for authentication endpoints."""

from __future__ import annotations

import asyncio
import math
import time
from collections import OrderedDict, deque
from dataclasses import dataclass


@dataclass(frozen=True)
class RateLimitDecision:
    allowed: bool
    retry_after_seconds: int = 0


class SlidingWindowRateLimiter:
    """Rate limit a single web process while bounding attacker-controlled keys."""

    def __init__(self, max_keys: int = 10_000) -> None:
        self._events: OrderedDict[str, deque[float]] = OrderedDict()
        self._lock = asyncio.Lock()
        self._max_keys = max_keys

    async def check(self, key: str, limit: int, window_seconds: int) -> RateLimitDecision:
        now = time.monotonic()
        cutoff = now - window_seconds
        async with self._lock:
            events = self._events.setdefault(key, deque())
            while events and events[0] <= cutoff:
                events.popleft()
            self._events.move_to_end(key)
            if len(events) >= limit:
                retry_after = max(1, math.ceil(events[0] + window_seconds - now))
                return RateLimitDecision(False, retry_after)
            events.append(now)
            while len(self._events) > self._max_keys:
                self._events.popitem(last=False)
        return RateLimitDecision(True)
