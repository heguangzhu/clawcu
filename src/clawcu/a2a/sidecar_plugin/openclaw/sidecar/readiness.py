"""Gateway readiness primitives (Python port of readiness.js).

Caches a successful /healthz probe for READY_TTL_MS (5 minutes). On
suspected gateway failure, callers should `invalidate_gateway_ready()` so
the next request re-probes instead of waiting out the TTL.

The TTL cache itself (positive-observation expiry + invalidate) lives in
``_common.readiness.ReadinessCache`` and is shared with the hermes
sidecar. This module owns the openclaw-specific wait-loop, the
``/healthz`` probe, and the ``looks_like_gateway_down`` error classifier
— all of which differ from hermes and stay local.
"""
from __future__ import annotations

import http.client
import re
import time
from typing import Callable, Optional

from _common.readiness import ReadinessCache

READY_TTL_MS = 5 * 60 * 1000

GATEWAY_DOWN_PATTERNS = [
    re.compile(r"ECONNREFUSED|ECONNRESET|ETIMEDOUT|socket hang up", re.IGNORECASE),
    re.compile(r"gateway /v1/chat/completions 5\d\d"),
    re.compile(r"gateway returned non-json"),
]


def looks_like_gateway_down(err_or_message) -> bool:
    if isinstance(err_or_message, str):
        msg = err_or_message
    elif err_or_message is None:
        msg = ""
    else:
        msg = getattr(err_or_message, "message", None) or str(err_or_message) or ""
    for pat in GATEWAY_DOWN_PATTERNS:
        if pat.search(msg):
            return True
    return False


def _default_now_ms() -> int:
    return int(time.time() * 1000)


def probe_gateway_ready(
    host: str,
    port: int,
    path: str = "/healthz",
    timeout_ms: int = 2000,
    http_connection_cls=http.client.HTTPConnection,
) -> bool:
    """Single GET against the gateway. 2xx/3xx → ready. Anything else → not."""
    conn = None
    try:
        conn = http_connection_cls(host=host, port=port, timeout=timeout_ms / 1000.0)
        conn.request("GET", path)
        resp = conn.getresponse()
        try:
            resp.read()
        except Exception:
            pass
        status = resp.status or 0
        return 200 <= status < 400
    except Exception:
        return False
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


class Readiness:
    def __init__(
        self,
        ttl_ms: int = READY_TTL_MS,
        now_fn: Callable[[], int] = _default_now_ms,
        sleep_fn: Optional[Callable[[int], None]] = None,
    ) -> None:
        self.ttl_ms = ttl_ms
        self.now_fn = now_fn
        self.sleep_fn = sleep_fn if sleep_fn is not None else (lambda ms: time.sleep(ms / 1000.0))
        self._cache = ReadinessCache()

    def wait_for_gateway_ready(
        self,
        host: str,
        port: int,
        deadline_ms: int,
        path: str = "/healthz",
        probe_timeout_ms: int = 2000,
        poll_interval_ms: int = 500,
        probe: Callable[..., bool] = probe_gateway_ready,
    ) -> bool:
        now = self.now_fn()
        if self._cache.is_fresh(now):
            return True
        end = now + deadline_ms
        while self.now_fn() < end:
            ok = probe(host=host, port=port, path=path, timeout_ms=probe_timeout_ms)
            if ok:
                self._cache.mark_ready(self.now_fn(), self.ttl_ms)
                return True
            self.sleep_fn(poll_interval_ms)
        return False

    def invalidate_gateway_ready(self) -> None:
        self._cache.invalidate()

    def _ready_until_value(self) -> float:
        return self._cache.expires_at


def create_readiness(
    ttl_ms: int = READY_TTL_MS,
    now_fn: Callable[[], int] = _default_now_ms,
    sleep_fn: Optional[Callable[[int], None]] = None,
) -> Readiness:
    return Readiness(ttl_ms=ttl_ms, now_fn=now_fn, sleep_fn=sleep_fn)


# Module-level singleton, matching server.js module behavior.
_default = create_readiness()


def wait_for_gateway_ready(**kwargs) -> bool:
    return _default.wait_for_gateway_ready(**kwargs)


def invalidate_gateway_ready() -> None:
    _default.invalidate_gateway_ready()
