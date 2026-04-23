"""Self-origin outbound rate limiter (Python port of outbound_limit.js).

Shared limiter for /a2a/outbound and MCP tools/call handlers. The key is
either `thread:<threadId>` or `self:<selfName>` (defaulting to "anon").
Default 60 calls / 60s, configurable via A2A_OUTBOUND_RATE_LIMIT.
"""
from __future__ import annotations

import os
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable, Deque, Dict, Mapping, Optional

DEFAULT_RPM = 60
WINDOW_MS = 60_000
DEFAULT_SWEEP_INTERVAL_MS = 300_000


def _default_now_ms() -> int:
    return int(time.time() * 1000)


def _coerce_int_env(raw, fallback: int, allow_zero: bool = False, clamp_nonneg: bool = False) -> int:
    if raw is None:
        return fallback
    s = str(raw).strip()
    if s == "":
        return fallback
    try:
        n = float(s)
    except (TypeError, ValueError):
        return fallback
    if n != int(n):
        return fallback
    n_int = int(n)
    if clamp_nonneg:
        return max(0, n_int)
    if n_int <= 0 and not allow_zero:
        return fallback
    return n_int


def read_rpm(env: Optional[Mapping[str, str]] = None) -> int:
    e = env if env is not None else os.environ
    return _coerce_int_env(e.get("A2A_OUTBOUND_RATE_LIMIT"), DEFAULT_RPM)


def read_sweep_interval_ms(env: Optional[Mapping[str, str]] = None) -> int:
    e = env if env is not None else os.environ
    return _coerce_int_env(
        e.get("A2A_OUTBOUND_SWEEP_INTERVAL_MS"),
        DEFAULT_SWEEP_INTERVAL_MS,
        clamp_nonneg=True,
    )


def key_for(thread_id: Optional[str] = None, self_name: Optional[str] = None) -> str:
    if isinstance(thread_id, str) and thread_id:
        return f"thread:{thread_id}"
    return f"self:{self_name or 'anon'}"


@dataclass
class OutboundDecision:
    allowed: bool
    limit: int
    count: int = 0
    retry_after_ms: int = 0


class OutboundLimiter:
    def __init__(
        self,
        rpm: Optional[int] = None,
        now_fn: Callable[[], int] = _default_now_ms,
    ) -> None:
        self.limit = rpm if (isinstance(rpm, int) and rpm > 0) else DEFAULT_RPM
        self.now_fn = now_fn
        self._hits: Dict[str, Deque[int]] = {}

    def check(self, key: str) -> OutboundDecision:
        now = self.now_fn()
        cutoff = now - WINDOW_MS
        arr = self._hits.get(key)
        if arr is None:
            arr = deque()
            self._hits[key] = arr
        while arr and arr[0] <= cutoff:
            arr.popleft()
        if len(arr) >= self.limit:
            retry_after_ms = max(0, arr[0] + WINDOW_MS - now)
            return OutboundDecision(allowed=False, limit=self.limit, retry_after_ms=retry_after_ms)
        arr.append(now)
        return OutboundDecision(allowed=True, limit=self.limit, count=len(arr))

    def sweep(self) -> None:
        now = self.now_fn()
        cutoff = now - WINDOW_MS
        empty_keys = []
        for k, arr in self._hits.items():
            while arr and arr[0] <= cutoff:
                arr.popleft()
            if not arr:
                empty_keys.append(k)
        for k in empty_keys:
            self._hits.pop(k, None)

    def size(self) -> int:
        return len(self._hits)

    def reset(self) -> None:
        self._hits.clear()


def create_outbound_limiter(
    rpm: Optional[int] = None,
    now_fn: Callable[[], int] = _default_now_ms,
) -> OutboundLimiter:
    return OutboundLimiter(rpm=rpm, now_fn=now_fn)


class SweepTimer:
    """Best-effort periodic sweep. `.unref()`-style: daemon thread, so it
    doesn't block interpreter shutdown.
    """

    def __init__(
        self,
        limiter: OutboundLimiter,
        interval_ms: int,
        logger=None,
    ) -> None:
        self.limiter = limiter
        self.interval_s = interval_ms / 1000.0
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._logger = logger

    def start(self) -> None:
        if self._thread is not None:
            return
        t = threading.Thread(target=self._run, name="a2a-outbound-sweep", daemon=True)
        self._thread = t
        t.start()

    def stop(self) -> None:
        self._stop.set()
        t = self._thread
        self._thread = None
        if t is not None:
            t.join(timeout=1.0)

    def _run(self) -> None:
        while not self._stop.wait(self.interval_s):
            try:
                self.limiter.sweep()
            except Exception as err:  # pragma: no cover - defensive
                if self._logger is not None:
                    try:
                        self._logger.warn(f"[sidecar] outbound-sweep failed: {err}")
                    except Exception:
                        pass


def create_sweep_timer(
    limiter: OutboundLimiter,
    interval_ms: int,
    logger=None,
) -> Optional[SweepTimer]:
    if limiter is None or not hasattr(limiter, "sweep"):
        return None
    if not isinstance(interval_ms, int) or interval_ms <= 0:
        return None
    timer = SweepTimer(limiter=limiter, interval_ms=interval_ms, logger=logger)
    timer.start()
    return timer
