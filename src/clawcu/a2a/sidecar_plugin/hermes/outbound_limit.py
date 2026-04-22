"""Self-origin outbound rate limit shared by /a2a/outbound and /mcp tool-call
handlers (a2a-design-4.md §P1-B). Hop budget caps depth (A→B→A→B runaway);
this caps breadth (one LLM turn fires 200 parallel a2a_call_peer calls and
nukes the provider quota).

Key: thread_id when present, else the caller's own registered name
(``self:<name>``). Limit: N calls / rolling 60s / key. Default 60/min,
tunable via ``A2A_OUTBOUND_RATE_LIMIT`` env var.

Mirror of ``sidecar_plugin/openclaw/sidecar/outbound_limit.js`` so both
runtimes behave identically under load.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections import deque
from typing import Callable, Mapping

_log = logging.getLogger("clawcu.a2a.outbound_limit")

DEFAULT_RPM = 60
WINDOW_MS = 60_000


def read_rpm(env: Mapping[str, str] | None = None) -> int:
    src = env if env is not None else os.environ
    raw = src.get("A2A_OUTBOUND_RATE_LIMIT")
    if raw is None or str(raw).strip() == "":
        return DEFAULT_RPM
    try:
        n = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_RPM
    if n != n or n <= 0 or int(n) != n:  # NaN / non-positive / non-integer
        return DEFAULT_RPM
    return int(n)


def key_for(*, thread_id: str | None = None, self_name: str | None = None) -> str:
    if isinstance(thread_id, str) and thread_id:
        return f"thread:{thread_id}"
    return f"self:{self_name or 'anon'}"


class OutboundLimiter:
    """Sliding-window counter. Thread-safe: the sidecar uses
    ThreadingHTTPServer, so /a2a/outbound and /mcp can land on different
    threads at the same instant."""

    def __init__(
        self,
        rpm: int | None = None,
        now_fn: Callable[[], float] = lambda: time.monotonic() * 1000.0,
    ) -> None:
        self.limit = rpm if isinstance(rpm, int) and rpm > 0 else DEFAULT_RPM
        self._now = now_fn
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def check(self, key: str) -> dict:
        now = self._now()
        cutoff = now - WINDOW_MS
        with self._lock:
            arr = self._hits.get(key)
            if arr is None:
                arr = deque()
                self._hits[key] = arr
            while arr and arr[0] <= cutoff:
                arr.popleft()
            if len(arr) >= self.limit:
                retry_after_ms = arr[0] + WINDOW_MS - now
                return {
                    "allowed": False,
                    "retry_after_ms": max(0.0, retry_after_ms),
                    "limit": self.limit,
                }
            arr.append(now)
            return {"allowed": True, "count": len(arr), "limit": self.limit}

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()

    def sweep(self) -> None:
        """P1-J (a2a-design-5.md): drop buckets whose deques have emptied
        past the window. Opportunistic cleanup — safe to call at any time;
        cheap when there are few keys."""
        now = self._now()
        cutoff = now - WINDOW_MS
        with self._lock:
            for key in list(self._hits.keys()):
                arr = self._hits[key]
                while arr and arr[0] <= cutoff:
                    arr.popleft()
                if not arr:
                    del self._hits[key]

    def size(self) -> int:
        with self._lock:
            return len(self._hits)


def create_outbound_limiter(
    rpm: int | None = None,
    now_fn: Callable[[], float] = lambda: time.monotonic() * 1000.0,
) -> OutboundLimiter:
    return OutboundLimiter(rpm=rpm, now_fn=now_fn)


DEFAULT_SWEEP_INTERVAL_MS = 300_000


def read_sweep_interval_ms(env: Mapping[str, str] | None = None) -> int:
    """P2-L (a2a-design-6.md): read A2A_OUTBOUND_SWEEP_INTERVAL_MS.
    Positive integer → custom cadence. 0 / negative / invalid → disabled."""
    src = env if env is not None else os.environ
    raw = src.get("A2A_OUTBOUND_SWEEP_INTERVAL_MS")
    if raw is None or str(raw).strip() == "":
        return DEFAULT_SWEEP_INTERVAL_MS
    try:
        n = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_SWEEP_INTERVAL_MS
    if n != n or int(n) != n:
        return DEFAULT_SWEEP_INTERVAL_MS
    return max(0, int(n))


def create_sweep_thread(
    limiter: OutboundLimiter,
    interval_ms: int,
    *,
    stop_event: threading.Event | None = None,
) -> threading.Thread | None:
    """Spawn a daemon thread that calls ``limiter.sweep()`` every
    ``interval_ms`` ms. Returns None when interval_ms <= 0 so the
    caller can cleanly disable cleanup via env. The stop_event lets
    tests (and graceful shutdown) wake the thread early."""
    if interval_ms <= 0:
        return None
    event = stop_event if stop_event is not None else threading.Event()
    interval_s = interval_ms / 1000.0

    def _loop() -> None:
        while not event.wait(interval_s):
            try:
                limiter.sweep()
            except Exception as exc:
                # a2a-design-7.md §P2-N: sweep is opportunistic cleanup,
                # never load-bearing — still swallow, but leave a breadcrumb.
                _log.warning("outbound-sweep failed: %s", exc)

    t = threading.Thread(target=_loop, name="a2a-outbound-sweep", daemon=True)
    t.start()
    return t
