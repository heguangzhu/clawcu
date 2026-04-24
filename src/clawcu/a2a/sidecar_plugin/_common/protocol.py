"""Shared A2A wire-protocol header helpers.

The sidecars surface a few small header concerns identically:

- ``X-A2A-Request-Id``: end-to-end correlation ID. Accepted from the
  caller if it survives a conservative allow-list (uuid4, ulid, short
  opaque tag; no control chars; ≤128 bytes) — otherwise minted.
- ``X-A2A-Hop``: hop counter for loop protection. Parsed defensively so
  a malformed or negative header decays to ``0`` rather than raising.

Both helpers accept either a plain ``dict`` (tests/fakes) or a
``http.server.BaseHTTPRequestHandler``-style header container (real
HTTP traffic). For ``dict``, lookup is case-insensitive so callers can
pass either ``"X-A2A-Request-Id"`` or ``"x-a2a-request-id"`` keys — matching
the HTTP/1.1 case-insensitive contract that stdlib ``email.Message``
already honours.
"""

from __future__ import annotations

import os
import uuid
from typing import Any, Callable, Dict, Mapping, Optional

from _common.http_response import write_json_response

REQUEST_ID_HEADER = "X-A2A-Request-Id"
HOP_HEADER = "X-A2A-Hop"

DEFAULT_HOP_BUDGET = 8

_REQUEST_ID_MAX_LEN = 128


def hop_budget_from_env(env: Optional[Mapping[str, str]] = None) -> int:
    """Resolve ``A2A_HOP_BUDGET`` with default ``8``, rejecting bad values.

    Both sidecars read the same env var with the same semantics — an
    absent/empty value, a non-integer, or a non-positive value all fall
    back to the default. Kept here next to :func:`read_hop_header` so
    the hop-counter read and the hop-budget read share one home.
    """
    source = env if env is not None else os.environ
    raw = source.get("A2A_HOP_BUDGET")
    if raw is None or str(raw).strip() == "":
        return DEFAULT_HOP_BUDGET
    try:
        v = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_HOP_BUDGET
    return v if v > 0 else DEFAULT_HOP_BUDGET


def _header_get(headers: Any, name: str) -> Optional[str]:
    """Case-insensitive header lookup for dict or HTTPMessage."""
    if headers is None:
        return None
    if not hasattr(headers, "get"):
        return None
    direct = headers.get(name)
    if direct is not None:
        return direct
    if isinstance(headers, dict):
        lower = name.lower()
        for key, value in headers.items():
            if isinstance(key, str) and key.lower() == lower:
                return value
    return None


def looks_like_request_id(value: Any) -> bool:
    """Return ``True`` for values safe to use as a log-greppable request ID.

    Accepts uuid4, uuid7, ulid, short opaque tags. Rejects control chars,
    whitespace (incl. tab/CR/LF), empty strings, and anything longer than
    ``_REQUEST_ID_MAX_LEN`` bytes.
    """
    if not isinstance(value, str):
        return False
    if not value or len(value) > _REQUEST_ID_MAX_LEN:
        return False
    for ch in value:
        code = ord(ch)
        if code < 0x20:
            return False
        if code in (0x20, 0x09, 0x0A, 0x0D):
            return False
    return True


def read_or_mint_request_id(headers: Any) -> str:
    """Return the caller-supplied request ID if valid, else a fresh uuid4."""
    raw = _header_get(headers, REQUEST_ID_HEADER)
    if isinstance(raw, str):
        candidate = raw.strip()
        if looks_like_request_id(candidate):
            return candidate
    return uuid.uuid4().hex


def read_hop_header(headers: Any) -> int:
    """Return the inbound X-A2A-Hop value, defensively clamped to ``>= 0``.

    Malformed, non-numeric, NaN, or negative values decay to ``0``. An
    absent header also returns ``0``.
    """
    raw = _header_get(headers, HOP_HEADER)
    if raw is None:
        return 0
    try:
        n = float(str(raw).strip())
    except (TypeError, ValueError):
        return 0
    if n != n or n < 0:  # NaN guard, then negative guard
        return 0
    return int(n)


HopRefusedLogger = Callable[[str, str, int, int], None]


def hop_prelude(
    handler: Any,
    *,
    route: str,
    budget: Optional[int] = None,
    on_refused: Optional[HopRefusedLogger] = None,
) -> tuple[int, str, dict[str, str], bool]:
    """Shared ``/a2a/send`` + ``/a2a/outbound`` prelude: mint the request
    ID, read the hop counter, and refuse with 508 when the hop budget is
    spent.

    Returns ``(incoming_hop, request_id, rid_headers, refused)``. When
    ``refused`` is ``True`` the 508 envelope has already been written
    (with the caller's ``X-A2A-Request-Id`` echoed back) and the caller's
    next line is just ``return``. The incoming hop count is returned in
    both paths because ``/a2a/outbound`` forwards ``hop+1`` on the
    outgoing POST and both handlers log it.

    ``budget`` defaults to :func:`hop_budget_from_env`, so tests that
    monkey-patch ``A2A_HOP_BUDGET`` are honored per-call without the
    caller having to re-resolve it. ``on_refused`` lets the two sidecars
    keep their distinct log formats (``[sidecar:<name>] …`` vs stdlib
    ``%-format``) without the prelude having to know about either.
    """
    budget_v = hop_budget_from_env() if budget is None else budget
    incoming_hop = read_hop_header(handler.headers)
    request_id = read_or_mint_request_id(handler.headers)
    rid_headers = {REQUEST_ID_HEADER: request_id}
    if incoming_hop >= budget_v:
        if on_refused is not None:
            on_refused(route, request_id, incoming_hop, budget_v)
        write_json_response(
            handler,
            508,
            {
                "error": (
                    f"hop budget exceeded (hop={incoming_hop}, budget={budget_v})"
                ),
                "request_id": request_id,
            },
            extra_headers=rid_headers,
        )
        return incoming_hop, request_id, rid_headers, True
    return incoming_hop, request_id, rid_headers, False


def write_error_envelope(
    handler: Any,
    status: int,
    error: str,
    *,
    request_id: str,
    rid_headers: Mapping[str, str],
    **extra: Any,
) -> None:
    """Write the uniform ``{"error": msg, "request_id": rid, ...}`` envelope.

    Every sidecar error surface — 400 shape, 413 body-cap, 503 gateway
    not ready, 502/504 upstream, 500 internal — emits this shape with the
    caller's ``X-A2A-Request-Id`` echoed in both the header and the body.
    One helper keeps the pairing in lockstep so no error path drifts on
    "did I remember the request_id?".

    Extra body fields (``detail``, ``peer_status``, ``retry_after_ms``,
    etc.) merge in as kwargs — this is how the gateway HTTPError path
    attaches a truncated upstream body without inflating the signature.
    """
    body: Dict[str, Any] = {"error": error, "request_id": request_id}
    body.update(extra)
    write_json_response(handler, status, body, extra_headers=rid_headers)


def write_outbound_reply_response(
    handler: Any,
    *,
    self_name: str,
    to: str,
    peer_resp: Any,
    fallback_thread_id: Optional[str],
    request_id: str,
    rid_headers: dict[str, str],
) -> None:
    """Write the ``/a2a/outbound`` success envelope.

    Both sidecars return the same 5-field shape after a successful peer
    forward — ``{from, to, reply, thread_id, request_id}`` — with the
    reply coerced to ``""`` if the peer's payload is malformed and the
    thread ID falling back to the caller-supplied one if the peer
    didn't echo a string. Centralising the defensive ``isinstance``
    checks keeps the two handlers from drifting on what counts as a
    "well-formed peer reply".
    """
    is_dict = isinstance(peer_resp, dict)
    reply = peer_resp.get("reply") if is_dict else None
    resp_thread = peer_resp.get("thread_id") if is_dict else None
    write_json_response(
        handler,
        200,
        {
            "from": self_name,
            "to": to,
            "reply": reply if isinstance(reply, str) else "",
            "thread_id": resp_thread if isinstance(resp_thread, str) else fallback_thread_id,
            "request_id": request_id,
        },
        extra_headers=rid_headers,
    )


__all__ = [
    "REQUEST_ID_HEADER",
    "HOP_HEADER",
    "DEFAULT_HOP_BUDGET",
    "hop_budget_from_env",
    "hop_prelude",
    "looks_like_request_id",
    "read_or_mint_request_id",
    "read_hop_header",
    "write_error_envelope",
    "write_outbound_reply_response",
]
