"""Hermes sidecar outbound peer helpers.

Extracted from ``server.py`` so handlers that talk to the A2A registry or
forward to a peer can depend on a narrow surface (``lookup_peer``,
``fetch_peer_list``, ``forward_to_peer``) instead of the 1400-line sidecar
module. Also owns the URL allow-list (``_validate_outbound_url``) and the
no-redirect ``_OPENER`` used for every outbound call.

Why a module and not just functions on ``server``
-------------------------------------------------
``server.py`` re-imports these names so its call sites and the existing
test suite (which reaches them via ``mod.lookup_peer`` / ``mod.OutboundError``)
keep working without touching hundreds of test lines. Symbol identity is
preserved: ``server._OPENER is peering._OPENER`` and
``server.OutboundError is peering.OutboundError``.
"""

from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from typing import Any
from urllib.error import HTTPError, URLError

from _common.mcp import UpstreamError
from _common.peer_cache import (
    BadOutboundUrl as _BadOutboundUrl,
    DEFAULT_REGISTRY_URL,
    create_peer_cache as _shared_peer_cache,
    default_registry_url as _default_registry_url,
    parse_peer_list_response,
    validate_outbound_url as _validate_outbound_url,
)
from _common.protocol import REQUEST_ID_HEADER
from _common import streams as _streams


# ``DEFAULT_REGISTRY_URL`` / ``_default_registry_url`` are re-imported from
# ``_common.peer_cache`` at module top so tests/call sites that reach
# ``peering.DEFAULT_REGISTRY_URL`` / ``peering._default_registry_url`` keep
# working after the consolidation.

# Per-call outbound cap (4 MiB) — registry and peer responses. The local-gateway
# cap (64 MiB) is not used here: that one belongs to server.py because it
# guards calls to the *co-resident* Hermes gateway, not outbound peer traffic.
A2A_MAX_RESPONSE_BYTES = 4 * 1024 * 1024

_ResponseTooLarge = _streams.ResponseTooLarge
_read_capped = _streams.read_capped_bytes


class OutboundError(UpstreamError):
    """Hermes-flavoured ``UpstreamError`` with legacy ``(http_status, message)``
    positional constructor. Call sites raise ``OutboundError(status, msg)``
    everywhere; keeping the positional signature avoids a big churn patch
    while still letting ``_common.mcp.handle_mcp_request`` catch via
    ``except UpstreamError``."""

    def __init__(self, http_status: int, message: str, peer_status: int | None = None) -> None:
        super().__init__(message, http_status=http_status, peer_status=peer_status)


# ``_BadOutboundUrl`` / ``_validate_outbound_url`` are re-imported from
# ``_common.peer_cache`` (where they live under their public names) so
# both sidecars enforce the same http/https allow-list. Kept under the
# leading-underscore alias so tests that reach ``peering._BadOutboundUrl``
# / ``peering._validate_outbound_url`` continue to work.
from _common.peer_cache import _OUTBOUND_URL_ALLOWED_SCHEMES  # noqa: F401,E402


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Return None from redirect_request so urllib surfaces 3xx as an
    HTTPError instead of following.

    Review-20 P1-L1: CPython's default ``HTTPRedirectHandler`` admits
    redirects into ``{"http", "https", "ftp", ""}``. ``_validate_outbound_url``
    only gates the URL passed into ``urlopen``, so a peer returning
    ``302 Location: ftp://attacker/`` would bypass the scheme allow-list
    by redirecting into ftp:// from inside urlopen. Short-circuiting the
    redirect chain here keeps peer traffic pinned to the allow-listed
    URL. The 3xx response surfaces via the existing ``except HTTPError``
    arms in lookup_peer / fetch_peer_list / forward_to_peer, producing
    ``peer HTTP 302: …`` style errors.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        return None


_OPENER = urllib.request.build_opener(_NoRedirectHandler)


def lookup_peer(
    registry_url: str, peer_name: str, timeout: float
) -> dict[str, Any]:
    try:
        _validate_outbound_url(registry_url)
    except _BadOutboundUrl as e:
        raise OutboundError(400, f"invalid registry url: {e}") from e
    base = registry_url.rstrip("/")
    url = f"{base}/agents/{urllib.parse.quote(peer_name, safe='')}"
    req = urllib.request.Request(url, method="GET", headers={"Accept": "application/json"})
    try:
        with _OPENER.open(req, timeout=timeout) as resp:
            raw = _read_capped(resp).decode("utf-8")
            status = resp.status
    except _ResponseTooLarge as e:
        raise OutboundError(503, f"registry response too large: {e}") from e
    except HTTPError as e:
        try:
            body_raw = _read_capped(e) if hasattr(e, "read") else b""
        except _ResponseTooLarge:
            body_raw = b""
        body = body_raw.decode("utf-8", errors="replace")
        if e.code == 404:
            raise OutboundError(404, f"peer '{peer_name}' not found in registry") from e
        raise OutboundError(503, f"registry lookup {e.code}: {body[:200]}") from e
    except URLError as e:
        raise OutboundError(503, f"registry unreachable: {e.reason}") from e
    except (OSError, TimeoutError) as e:
        raise OutboundError(503, f"registry request failed: {e}") from e
    if status != 200:
        raise OutboundError(503, f"registry lookup {status}: {raw[:200]}")
    try:
        card = json.loads(raw)
    except Exception as e:
        raise OutboundError(503, f"registry returned non-json: {e}") from e
    if not isinstance(card, dict) or not isinstance(card.get("endpoint"), str) or not card["endpoint"]:
        raise OutboundError(503, f"registry card for '{peer_name}' missing endpoint")
    return card


def fetch_peer_list(registry_url: str, timeout: float) -> list[dict[str, Any]] | None:
    """Fetch the full peer list from the registry's `GET /agents` endpoint.

    Returns a filtered list of peer dicts on 2xx + JSON array response, or
    None on any failure (404, 5xx, network, non-JSON, non-array). Callers
    fall back to a static tool description; tools/list must never fail
    because of a registry hiccup (a2a-design-5.md §P1-H).
    """
    base = registry_url.rstrip("/")
    url = f"{base}/agents"
    req = urllib.request.Request(url, method="GET", headers={"Accept": "application/json"})
    try:
        with _OPENER.open(req, timeout=timeout) as resp:
            raw = _read_capped(resp).decode("utf-8")
            status = resp.status
    except (HTTPError, URLError, OSError, TimeoutError, _ResponseTooLarge):
        return None
    if status < 200 or status >= 300:
        return None
    return parse_peer_list_response(raw)


def create_peer_cache(
    registry_url: str,
    timeout: float = 5.0,
    fresh_s: float = 30.0,
    stale_s: float = 300.0,
    now_fn: Any = None,
    fetch_fn: Any = None,
) -> Any:
    """TTL cache around ``fetch_peer_list``. See a2a-design-5.md §P1-H for
    the cache strategy (30s fresh, 5min stale-OK on failure, null fallback).
    Thin wrapper that binds ``registry_url`` + ``timeout`` into the
    zero-arg fetcher expected by :func:`_common.peer_cache.create_peer_cache`.
    """
    fetcher = fetch_fn if fetch_fn is not None else fetch_peer_list
    return _shared_peer_cache(
        lambda: fetcher(registry_url, timeout),
        fresh_s=fresh_s,
        stale_s=stale_s,
        now_fn=now_fn,
    )


def forward_to_peer(
    endpoint: str,
    self_name: str,
    peer_name: str,
    message: str,
    thread_id: str | None,
    hop: int,
    timeout: float,
    request_id: str | None = None,
    mode: str | None = None,
) -> dict[str, Any]:
    try:
        _validate_outbound_url(endpoint)
    except _BadOutboundUrl as e:
        raise OutboundError(502, f"peer card endpoint rejected: {e}") from e
    body: dict[str, Any] = {"from": self_name, "to": peer_name, "message": message}
    if thread_id:
        body["thread_id"] = thread_id
    if mode:
        body["mode"] = mode
    data = json.dumps(body).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "X-A2A-Hop": str(hop),
        "User-Agent": "a2a-bridge-sidecar/0.3",
    }
    if request_id:
        headers[REQUEST_ID_HEADER] = request_id
    req = urllib.request.Request(endpoint, data=data, method="POST", headers=headers)
    try:
        with _OPENER.open(req, timeout=timeout) as resp:
            raw = _read_capped(resp).decode("utf-8")
            status = resp.status
    except _ResponseTooLarge as e:
        raise OutboundError(502, f"peer response too large: {e}") from e
    except HTTPError as e:
        try:
            body_bytes = _read_capped(e) if hasattr(e, "read") else b""
        except _ResponseTooLarge:
            body_bytes = b""
        body_raw = body_bytes.decode("utf-8", errors="replace")
        if e.code == 508:
            raise OutboundError(508, f"peer rejected hop limit: {body_raw[:200]}", e.code) from e
        if e.code == 429:
            raise OutboundError(429, f"peer rate-limited: {body_raw[:200]}", e.code) from e
        raise OutboundError(502, f"peer HTTP {e.code}: {body_raw[:200]}", e.code) from e
    except URLError as e:
        raise OutboundError(504, f"peer unreachable or timed out: {e.reason}") from e
    except (OSError, TimeoutError) as e:
        raise OutboundError(504, f"peer request failed: {e}") from e
    if status < 200 or status >= 300:
        raise OutboundError(502, f"peer HTTP {status}: {raw[:200]}", status)
    try:
        return json.loads(raw)
    except Exception as e:
        raise OutboundError(502, f"peer returned non-json: {e}") from e


def _task_endpoint(endpoint: str, task_id: str, suffix: str = "") -> str:
    """Derive the peer's task endpoint from its ``/a2a/send`` advertised URL.

    Registry cards advertise the send endpoint (e.g. ``…/a2a/send``); task
    endpoints (``/a2a/tasks/<id>`` + ``/cancel``) share the same mount point
    one level up. Mirror of openclaw's ``_task_base_path``, specialized to
    hermes' full-URL urllib-based transport.
    """
    parsed = urllib.parse.urlsplit(endpoint)
    path = parsed.path
    if path.endswith("/a2a/send"):
        base = path[: -len("/send")]
    elif path.endswith("/send"):
        base = path[: -len("/send")]
    else:
        base = path.rstrip("/")
    new_path = f"{base}/tasks/{urllib.parse.quote(task_id, safe='')}"
    if suffix:
        new_path = new_path + suffix
    return urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, new_path, "", "")
    )


def get_task_from_peer(
    endpoint: str,
    task_id: str,
    timeout: float,
    request_id: str | None = None,
) -> dict[str, Any]:
    try:
        _validate_outbound_url(endpoint)
    except _BadOutboundUrl as e:
        raise OutboundError(502, f"peer endpoint rejected: {e}") from e
    url = _task_endpoint(endpoint, task_id)
    headers = {"Accept": "application/json", "User-Agent": "a2a-bridge-sidecar/0.3"}
    if request_id:
        headers[REQUEST_ID_HEADER] = request_id
    req = urllib.request.Request(url, method="GET", headers=headers)
    try:
        with _OPENER.open(req, timeout=timeout) as resp:
            raw = _read_capped(resp).decode("utf-8")
            status = resp.status
    except _ResponseTooLarge as e:
        raise OutboundError(502, f"peer response too large: {e}") from e
    except HTTPError as e:
        try:
            body_bytes = _read_capped(e) if hasattr(e, "read") else b""
        except _ResponseTooLarge:
            body_bytes = b""
        body_raw = body_bytes.decode("utf-8", errors="replace")
        if e.code == 404:
            raise OutboundError(404, f"task not found on peer: {task_id}", e.code) from e
        raise OutboundError(502, f"peer HTTP {e.code}: {body_raw[:200]}", e.code) from e
    except URLError as e:
        raise OutboundError(504, f"peer unreachable or timed out: {e.reason}") from e
    except (OSError, TimeoutError) as e:
        raise OutboundError(504, f"peer request failed: {e}") from e
    if status < 200 or status >= 300:
        raise OutboundError(502, f"peer HTTP {status}: {raw[:200]}", status)
    try:
        return json.loads(raw)
    except Exception as e:
        raise OutboundError(502, f"peer returned non-json: {e}") from e


def cancel_task_on_peer(
    endpoint: str,
    task_id: str,
    timeout: float,
    request_id: str | None = None,
) -> dict[str, Any]:
    try:
        _validate_outbound_url(endpoint)
    except _BadOutboundUrl as e:
        raise OutboundError(502, f"peer endpoint rejected: {e}") from e
    url = _task_endpoint(endpoint, task_id, suffix="/cancel")
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "a2a-bridge-sidecar/0.3",
    }
    if request_id:
        headers[REQUEST_ID_HEADER] = request_id
    req = urllib.request.Request(url, data=b"{}", method="POST", headers=headers)
    try:
        with _OPENER.open(req, timeout=timeout) as resp:
            raw = _read_capped(resp).decode("utf-8")
            status = resp.status
    except _ResponseTooLarge as e:
        raise OutboundError(502, f"peer response too large: {e}") from e
    except HTTPError as e:
        try:
            body_bytes = _read_capped(e) if hasattr(e, "read") else b""
        except _ResponseTooLarge:
            body_bytes = b""
        body_raw = body_bytes.decode("utf-8", errors="replace")
        if e.code == 404:
            raise OutboundError(404, f"task not found on peer: {task_id}", e.code) from e
        raise OutboundError(502, f"peer HTTP {e.code}: {body_raw[:200]}", e.code) from e
    except URLError as e:
        raise OutboundError(504, f"peer unreachable or timed out: {e.reason}") from e
    except (OSError, TimeoutError) as e:
        raise OutboundError(504, f"peer request failed: {e}") from e
    if status < 200 or status >= 300:
        raise OutboundError(502, f"peer HTTP {status}: {raw[:200]}", status)
    try:
        return json.loads(raw)
    except Exception as e:
        raise OutboundError(502, f"peer returned non-json: {e}") from e
