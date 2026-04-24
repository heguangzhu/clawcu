"""OpenClaw sidecar outbound-peering helpers.

Named ``outbound`` rather than ``peering`` because hermes/sidecar/peering.py
already owns that module name on ``sys.path`` — both sidecars bootstrap
by prepending their own directory, so shared module names would collide
in tests that import both in the same process.

All upstream calls that talk to the **A2A mesh** (the registry and peer
sidecars) live here:

* :func:`fetch_peer_list` — GET ``/agents`` on the registry, returns a
  filtered list of peer cards or ``None`` on any failure. Best-effort so
  the refresh loop never throws into the cache.
* :func:`create_peer_cache` — millisecond-unit wrapper around
  :func:`_common.peer_cache.create_peer_cache` (which runs in seconds).
* :func:`lookup_peer` — GET ``/agents/{name}`` on the registry. Raises
  :class:`UpstreamError` with proper HTTP status on missing peer /
  non-200 / non-json / card-missing-endpoint.
* :func:`forward_to_peer` — POST the A2A body to a peer's ``/a2a/send``
  endpoint. Translates transport failures into 504, hop-limit rejection
  into 508, rate-limit into 429, and other non-2xx into 502 via
  :class:`UpstreamError`.
* :func:`read_allow_client_registry_url` — env-driven policy switch that
  controls whether inbound /a2a/send may override the registry URL per
  request. Off by default.

Kept out of ``server.py`` so the file's remaining content can focus on
inbound-handler glue. ``server.py`` re-exports the names so tests that
use ``sidecar.lookup_peer`` / ``sidecar.forward_to_peer`` / etc. keep
working unchanged.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any, Callable, Dict, Optional
from urllib.parse import quote as _quote

from _common.mcp import UpstreamError
from _common.peer_cache import (
    DEFAULT_REGISTRY_URL,
    create_peer_cache as _shared_peer_cache,
    default_registry_url,
    read_allow_client_registry_url,
)
from _common.protocol import REQUEST_ID_HEADER

from http_client import http_request_raw, parse_http_url, post_json


def fetch_peer_list(registry_url: str, timeout_ms: int) -> Optional[list]:
    parsed = parse_http_url(registry_url)
    base = parsed["pathname"].rstrip("/")
    path = f"{base}/agents"
    try:
        resp = http_request_raw(
            method="GET",
            host=parsed["host"],
            port=parsed["port"],
            path=path,
            headers={"accept": "application/json", "user-agent": "a2a-bridge-sidecar/0.3"},
            timeout_ms=timeout_ms,
            scheme=parsed["scheme"],
        )
    except Exception:
        return None
    if resp["status"] < 200 or resp["status"] >= 300:
        return None
    try:
        parsed_body = json.loads(resp["body"])
    except Exception:
        return None
    if not isinstance(parsed_body, list):
        return None
    return [p for p in parsed_body if isinstance(p, dict) and isinstance(p.get("name"), str)]


def _default_now_ms() -> int:
    return int(time.time() * 1000)


def create_peer_cache(
    registry_url: str,
    timeout_ms: int,
    fresh_ms: int = 30_000,
    stale_ms: int = 300_000,
    now_fn: Callable[[], int] = _default_now_ms,
    fetch_fn: Callable[..., Optional[list]] = fetch_peer_list,
):
    """TTL cache wrapping :func:`fetch_peer_list`. See
    :func:`_common.peer_cache.create_peer_cache` for the algorithm. This
    wrapper keeps OpenClaw's millisecond-unit external surface; internally
    everything runs in seconds against the shared implementation."""

    def _do_fetch():
        # Some callers pass a kwargs-style stub, the real fetch_peer_list
        # accepts both — try kwargs first so we don't break existing fakes.
        try:
            return fetch_fn(registry_url=registry_url, timeout_ms=timeout_ms)
        except TypeError:
            return fetch_fn(registry_url, timeout_ms)

    return _shared_peer_cache(
        _do_fetch,
        fresh_s=fresh_ms / 1000.0,
        stale_s=stale_ms / 1000.0,
        now_fn=lambda: now_fn() / 1000.0,
    )


def lookup_peer(registry_url: str, peer_name: str, timeout_ms: int) -> Dict[str, Any]:
    parsed = parse_http_url(registry_url)
    base = parsed["pathname"].rstrip("/")
    path = f"{base}/agents/{_quote(peer_name, safe='')}"
    resp = http_request_raw(
        method="GET",
        host=parsed["host"],
        port=parsed["port"],
        path=path,
        headers={"accept": "application/json", "user-agent": "a2a-bridge-sidecar/0.3"},
        timeout_ms=timeout_ms,
        scheme=parsed["scheme"],
    )
    status = resp["status"]
    body = resp["body"]
    if status == 404:
        raise UpstreamError(f"peer '{peer_name}' not found in registry", http_status=404)
    if status < 200 or status >= 300:
        raise UpstreamError(f"registry lookup {status}: {body[:200]}", http_status=503)
    try:
        card = json.loads(body)
    except Exception as exc:
        raise UpstreamError(f"registry returned non-json: {exc}", http_status=503)
    if not isinstance(card, dict) or not isinstance(card.get("endpoint"), str) or not card.get("endpoint"):
        raise UpstreamError(f"registry card for '{peer_name}' missing endpoint", http_status=503)
    return card


def forward_to_peer(
    endpoint: str,
    self_name: str,
    peer_name: str,
    message: str,
    thread_id: Optional[str],
    hop: int,
    timeout_ms: int,
    request_id: Optional[str] = None,
) -> Dict[str, Any]:
    parsed = parse_http_url(endpoint)
    body_obj: Dict[str, Any] = {"from": self_name, "to": peer_name, "message": message}
    if thread_id:
        body_obj["thread_id"] = thread_id
    headers = {"x-a2a-hop": str(hop)}
    if request_id:
        headers[REQUEST_ID_HEADER] = request_id
    try:
        resp = post_json(
            host=parsed["host"],
            port=parsed["port"],
            path=parsed["pathname"] + parsed["search"],
            headers=headers,
            body_obj=body_obj,
            timeout_ms=timeout_ms,
            scheme=parsed["scheme"],
        )
    except Exception as exc:
        raise UpstreamError(f"peer unreachable or timed out: {exc}", http_status=504)
    status = resp["status"]
    body = resp["body"]
    if 200 <= status < 300:
        try:
            return json.loads(body)
        except Exception as exc:
            raise UpstreamError(f"peer returned non-json: {exc}", http_status=502)
    if status == 508:
        raise UpstreamError(f"peer rejected hop limit: {body[:200]}", http_status=508)
    if status == 429:
        raise UpstreamError(f"peer rate-limited: {body[:200]}", http_status=429)
    raise UpstreamError(f"peer HTTP {status}: {body[:200]}", http_status=502, peer_status=status)


# ``DEFAULT_REGISTRY_URL`` / ``default_registry_url`` /
# ``read_allow_client_registry_url`` now live in ``_common.peer_cache`` so
# both sidecars share one fallback + one SSRF opt-in parser. Re-imported at
# the top of this module so the public surface stays unchanged for call
# sites that reach ``outbound.default_registry_url`` /
# ``outbound.read_allow_client_registry_url``.
