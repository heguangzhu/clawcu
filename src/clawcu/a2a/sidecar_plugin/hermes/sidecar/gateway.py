"""Hermes-gateway readiness probe + ready-cache.

Extracted from ``server.py`` so ``/a2a/send`` handlers and tests can depend
on a narrow surface (``wait_for_gateway_ready`` / ``invalidate_gateway_ready_cache``)
instead of the whole 1400-line sidecar module.

The cache is a module-global ``ReadinessCache`` shared by ``wait_for_gateway_ready``
and ``invalidate_gateway_ready_cache``; tests manipulate it directly via
``gateway._gateway_ready_cache`` and — because ``server.py`` re-imports the
same name — equivalently via ``server._gateway_ready_cache`` (identity, not
copy). Rebinding the *name* on ``server`` (e.g. ``server.wait_for_gateway_ready
= stub``) still works because ``server.py`` call sites resolve the symbol via
``server``'s globals, not ``gateway``'s.
"""

from __future__ import annotations

import urllib.request
from typing import TYPE_CHECKING
from urllib.error import HTTPError, URLError

from _common.readiness import ReadinessCache

if TYPE_CHECKING:
    from server import Config


# Cache a recent "ready" observation so we don't probe on every /a2a/send.
# 5-minute TTL matches the openclaw sidecar.
_GATEWAY_READY_TTL_S = 5 * 60
_gateway_ready_cache = ReadinessCache()


def _probe_gateway_ready(cfg: "Config") -> bool:
    req = urllib.request.Request(cfg.health_url(), method="GET")
    try:
        with urllib.request.urlopen(req, timeout=cfg.ready_probe_timeout) as resp:
            return 200 <= resp.status < 400
    except (HTTPError, URLError, OSError, TimeoutError):
        return False


def wait_for_gateway_ready(cfg: "Config", now_fn=None, sleep_fn=None) -> bool:
    """Block until Hermes /health responds 2xx or the deadline elapses.

    Returns True if the gateway became ready, False on timeout. Called lazily
    from /a2a/send so an early-arriving peer request doesn't 502 just because
    the supervisor hasn't finished bringing Hermes up yet.
    """
    import time as _time

    now = now_fn or _time.time
    sleep = sleep_fn or _time.sleep
    if _gateway_ready_cache.is_fresh(now()):
        return True
    deadline = now() + cfg.ready_deadline
    while now() < deadline:
        if _probe_gateway_ready(cfg):
            _gateway_ready_cache.mark_ready(now(), ttl=_GATEWAY_READY_TTL_S)
            return True
        sleep(cfg.ready_poll_interval)
    return False


def invalidate_gateway_ready_cache() -> None:
    """Drop the "gateway is ready" cache so the next ``/a2a/send`` re-probes.

    Called after upstream signals that suggest the gateway may have died
    mid-flight (unreachable socket, 5xx): the 5-minute TTL otherwise lets the
    sidecar keep pushing into a dead gateway without re-probing, turning one
    gateway flake into a 5-minute outage.
    """
    _gateway_ready_cache.invalidate()
