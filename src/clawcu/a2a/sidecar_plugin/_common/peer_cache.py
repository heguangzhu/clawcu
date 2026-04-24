"""Shared TTL cache for the A2A registry peer list.

Both sidecars cache ``GET /agents`` so the MCP ``tools/list`` response can
interpolate an up-to-date peer summary without hitting the registry on
every call (a2a-design-5.md §P1-H). This module owns the TTL + stale-OK
+ inflight-dedup logic; the HTTP fetch is injected, so each sidecar keeps
its own ``fetch_peer_list`` (they use different stdlib HTTP helpers).

Contract:

  cache = create_peer_cache(
      fetch_fn=lambda: do_get_agents(url, timeout),
      fresh_s=30.0,
      stale_s=300.0,
  )
  peers = cache.get()   # list[dict] | None

  Within ``fresh_s`` of the last successful fetch: served from the cache.
  Past ``fresh_s`` but within ``stale_s`` on fetch *failure*: served stale.
  Past ``stale_s``: ``None``.

  Concurrent ``get()`` calls funnel through a single in-flight fetch;
  followers block on an Event and receive the leader's result (a2a-design-
  5.md §P1-H concurrent-fetch dedupe).
"""

from __future__ import annotations

import os
import threading
import time as _time
from typing import Any, Callable, List, Mapping, Optional


# Shared default for the A2A registry URL. Both sidecars publish via the
# ``host.docker.internal:9100`` adapter by convention; the constant lives
# here next to :func:`default_registry_url` so the two sidecars can't drift
# on the fallback.
DEFAULT_REGISTRY_URL = "http://host.docker.internal:9100"


def default_registry_url(env: Optional[Mapping[str, str]] = None) -> str:
    """Resolve the A2A registry URL used when no caller-supplied override
    is in scope.

    Reads ``A2A_REGISTRY_URL`` from ``env`` (or ``os.environ``) and falls
    back to :data:`DEFAULT_REGISTRY_URL` when the value is missing, not a
    string, empty, or whitespace-only. Accepting either ``env=`` (openclaw
    call style) or no args (hermes call style) keeps one helper in charge
    of the fallback shape.
    """
    source = env if env is not None else os.environ
    raw = source.get("A2A_REGISTRY_URL")
    if not isinstance(raw, str):
        return DEFAULT_REGISTRY_URL
    stripped = raw.strip()
    return stripped or DEFAULT_REGISTRY_URL


class PeerCache:
    """Return type for :func:`create_peer_cache`. Only ``get()`` is public."""

    __slots__ = ("_get",)

    def __init__(self, get_fn: Callable[[], Optional[List[dict]]]) -> None:
        self._get = get_fn

    def get(self) -> Optional[List[dict]]:
        return self._get()


def create_peer_cache(
    fetch_fn: Callable[[], Optional[List[dict]]],
    *,
    fresh_s: float = 30.0,
    stale_s: float = 300.0,
    now_fn: Optional[Callable[[], float]] = None,
) -> PeerCache:
    """Build a PeerCache around a zero-arg ``fetch_fn``.

    The fetcher is always called with no arguments — callers close over
    their own ``registry_url`` + timeout. A fetch that returns ``None``
    (or raises) is treated as a transient failure: the previous value is
    served if still inside the stale window, otherwise ``None``.
    """
    _now = now_fn if now_fn is not None else _time.monotonic

    state: dict[str, Any] = {"cached": None, "fetched_at": 0.0, "inflight": None}
    lock = threading.Lock()

    def get() -> Optional[List[dict]]:
        now = _now()
        with lock:
            if state["cached"] is not None and now - state["fetched_at"] < fresh_s:
                return state["cached"]
            inflight = state["inflight"]
            is_leader = inflight is None
            if is_leader:
                inflight = {"done": threading.Event(), "result": None}
                state["inflight"] = inflight

        if not is_leader:
            inflight["done"].wait()
            return inflight["result"]

        try:
            got = fetch_fn()
        except Exception:  # noqa: BLE001
            got = None

        with lock:
            if got is not None:
                state["cached"] = got
                state["fetched_at"] = _now()
            elif state["cached"] is not None and _now() - state["fetched_at"] < stale_s:
                # Serve stale — leave cached/fetched_at untouched.
                pass
            else:
                state["cached"] = None
            inflight["result"] = state["cached"]
            state["inflight"] = None
            inflight["done"].set()
        return inflight["result"]

    return PeerCache(get)


__all__ = [
    "DEFAULT_REGISTRY_URL",
    "PeerCache",
    "create_peer_cache",
    "default_registry_url",
]
