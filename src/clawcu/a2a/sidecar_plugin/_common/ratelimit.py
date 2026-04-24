"""Per-peer sliding-window rate limiter (inbound /a2a/send).

Tracks inbound calls per peer name. ``per_minute=0`` disables it. The
``max_peers`` cap with stalest-first eviction keeps memory bounded against
peers that rotate their identity.

Thread-safe: both runtimes use ``ThreadingHTTPServer``, so concurrent
/a2a/send calls from the same peer can land on different threads at the
same instant.
"""
from __future__ import annotations

import math
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable, Deque, Dict, Mapping

from _common.http_response import write_json_response


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
        self._hits: Dict[str, Deque[int]] = {}
        self._lock = threading.Lock()

    def allow(self, peer: str) -> RateDecision:
        if self.per_minute <= 0:
            return RateDecision(ok=True, remaining=math.inf, reset_ms=0)
        now = self.now_fn()
        window_start = now - self.window_ms
        with self._lock:
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
            return RateDecision(
                ok=True, remaining=self.per_minute - len(timestamps), reset_ms=0
            )

    def _peers(self) -> Dict[str, Deque[int]]:
        with self._lock:
            return {k: deque(v) for k, v in self._hits.items()}


def create_rate_limiter(
    per_minute: int = 30,
    window_ms: int = 60 * 1000,
    now_fn: Callable[[], int] = _default_now_ms,
    max_peers: int = 1024,
) -> RateLimiter:
    return RateLimiter(
        per_minute=per_minute,
        window_ms=window_ms,
        now_fn=now_fn,
        max_peers=max_peers,
    )


def write_peer_rate_limit_response(
    handler: Any,
    decision: RateDecision,
    *,
    peer: str,
    request_id: str,
    rid_headers: Mapping[str, str],
) -> None:
    """Write the uniform peer-origin 429 envelope both sidecars emit when
    :meth:`RateLimiter.allow` rejects.

    ``Retry-After`` is the ceiling of ``reset_ms / 1000``, clamped to a
    minimum of 1 — ceiling (from hermes) matches HTTP semantics so a
    client retrying at the advertised time isn't rejected again for
    sub-second jitter, and the ``max(1, ...)`` floor (from openclaw)
    guarantees ``Retry-After: 0`` never appears when ``reset_ms`` is
    legitimately tiny.
    """
    retry_after_s = max(1, (decision.reset_ms + 999) // 1000)
    write_json_response(
        handler,
        429,
        {
            "error": f"rate limit exceeded for peer '{peer}'",
            "resetMs": decision.reset_ms,
            "request_id": request_id,
        },
        extra_headers={**rid_headers, "Retry-After": str(retry_after_s)},
    )
