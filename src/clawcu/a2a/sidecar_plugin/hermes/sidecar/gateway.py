"""Hermes-gateway surface: readiness probe, ready-cache, and chat call.

Both the inbound readiness check (``wait_for_gateway_ready``) and the
outbound chat completion call (``call_hermes``) target the *co-resident*
Hermes gateway — a trusted peer sitting behind 127.0.0.1 inside the same
container. Collecting them here lets ``server.py`` depend on one narrow
surface instead of the 1400-line sidecar module, and keeps the two places
that care about the gateway host/port + response caps from drifting.

Why two caps
------------
Outbound-peer responses are capped at 4 MiB (``peering.A2A_MAX_RESPONSE_BYTES``)
because a compromised or malicious peer is the primary OOM threat. The
co-resident Hermes gateway is trusted, but a buggy streaming response can
still OOM the sidecar — ``A2A_LOCAL_UPSTREAM_CAP`` (64 MiB) is the safety
net there. Different trust boundaries, different numbers.

Test-monkey-patch surface
-------------------------
``server.py`` re-imports the public names so that call sites and tests
that do ``server.call_hermes = stub`` / ``server.wait_for_gateway_ready
= stub`` keep working. The ready-cache object is shared by identity, not
copied, so ``server._gateway_ready_cache`` and
``gateway._gateway_ready_cache`` refer to the same ``ReadinessCache``.
"""

from __future__ import annotations

import json
import urllib.request
from typing import TYPE_CHECKING
from urllib.error import HTTPError, URLError

from _common.readiness import ReadinessCache
from _common import streams as _streams

if TYPE_CHECKING:
    from config import Config


# Cache a recent "ready" observation so we don't probe on every /a2a/send.
# 5-minute TTL matches the openclaw sidecar.
_GATEWAY_READY_TTL_S = 5 * 60
_gateway_ready_cache = ReadinessCache()


# Local-upstream cap for call_hermes (Review-22 P2-N1). See module docstring
# for the rationale; kept at module scope so tests and diagnostics can read
# the value without instantiating anything.
A2A_LOCAL_UPSTREAM_CAP = 64 * 1024 * 1024


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


def call_hermes(
    cfg: "Config",
    message: str,
    peer_from: str,
    history: list[dict[str, str]] | None = None,
) -> str:
    """POST to Hermes' OpenAI-compat /v1/chat/completions, return assistant text."""

    messages: list[dict[str, str]] = []
    if cfg.system_prompt:
        messages.append({"role": "system", "content": cfg.system_prompt})
    if history:
        messages.extend(history)
    # Tag the incoming message with its A2A origin so the LLM has context.
    prefix = f"[from agent '{peer_from}'] " if peer_from else ""
    messages.append({"role": "user", "content": prefix + message})

    body = json.dumps(
        {
            "model": cfg.model,
            "messages": messages,
            "stream": False,
        }
    ).encode("utf-8")

    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    if cfg.api_key:
        headers["Authorization"] = f"Bearer {cfg.api_key}"

    req = urllib.request.Request(
        cfg.chat_url(), data=body, method="POST", headers=headers
    )
    with urllib.request.urlopen(req, timeout=cfg.timeout) as resp:
        raw = _streams.read_capped_bytes(resp, cap=A2A_LOCAL_UPSTREAM_CAP).decode("utf-8")
    payload = json.loads(raw)

    try:
        return payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError(
            f"unexpected chat response shape: {payload!r}"
        ) from exc
