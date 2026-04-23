"""Per-peer sliding-window rate limiter (Python port of ratelimit.js).

Tracks inbound /a2a/send calls per peer name. `perMinute=0` disables it. The
`max_peers` cap with stalest-first eviction keeps memory bounded against
peers that rotate their identity.
"""
from __future__ import annotations

import math
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable, Deque, Dict


def _default_now_ms() -> int:
    return int(time.time() * 1000)


@dataclass
class RateDecision:
    ok: bool
    remaining: float  # math.inf when per_minute <= 0 and ok
    reset_ms: int


class RateLimiter:
    def __init__(
        self,
        per_minute: int = 30,
        window_ms: int = 60 * 1000,
        now_fn: Callable[[], int] = _default_now_ms,
        max_peers: int = 1024,
    ) -> None:
        self.per_minute = per_minute
        self.window_ms = window_ms
        self.now_fn = now_fn
        self.max_peers = max_peers
        # Insertion order preserved by dict; inner deque for O(1) popleft.
        self._hits: Dict[str, Deque[int]] = {}

    def allow(self, peer: str) -> RateDecision:
        if self.per_minute <= 0:
            return RateDecision(ok=True, remaining=math.inf, reset_ms=0)
        now = self.now_fn()
        window_start = now - self.window_ms
        timestamps = self._hits.get(peer)
        if timestamps is None:
            if len(self._hits) >= self.max_peers:
                stalest_key = None
                stalest_ts = math.inf
                for k, ts in self._hits.items():
                    last = ts[-1] if ts else 0
                    if last < stalest_ts:
                        stalest_ts = last
                        stalest_key = k
                if stalest_key is not None:
                    self._hits.pop(stalest_key, None)
            timestamps = deque()
            self._hits[peer] = timestamps
        while timestamps and timestamps[0] < window_start:
            timestamps.popleft()
        if len(timestamps) >= self.per_minute:
            oldest = timestamps[0]
            reset_ms = max(0, oldest + self.window_ms - now)
            return RateDecision(ok=False, remaining=0, reset_ms=reset_ms)
        timestamps.append(now)
        return RateDecision(ok=True, remaining=self.per_minute - len(timestamps), reset_ms=0)

    def _peers(self) -> Dict[str, Deque[int]]:
        # Shallow copy for tests/inspection, mirroring the Node helper.
        return {k: deque(v) for k, v in self._hits.items()}


def create_rate_limiter(
    per_minute: int = 30,
    window_ms: int = 60 * 1000,
    now_fn: Callable[[], int] = _default_now_ms,
    max_peers: int = 1024,
) -> RateLimiter:
    return RateLimiter(per_minute=per_minute, window_ms=window_ms, now_fn=now_fn, max_peers=max_peers)
