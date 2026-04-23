"""Hermes sidecar inbound-request reject-early guards.

Four knobs keep the sidecar from turning adversarial requests into OOMs
or into 5-minute-timeout-pinned worker threads:

1. ``_max_body_bytes()`` — cap on ``rfile.read(length)`` allocations.
2. ``_parse_content_length()`` — rejects negative / non-numeric /
   over-the-cap ``Content-Length`` headers before any read happens.
3. ``_hop_budget()`` — A2A ``X-A2A-Hop`` limit that refuses loop traffic
   with a 508 before any gateway work.
4. ``read_inbound_json_body()`` — composes (1)+(2) with JSON + dict
   validation and writes a uniform ``{error, request_id}`` 400/413
   response on any failure. ``/a2a/send`` and ``/a2a/outbound`` share
   it; ``/mcp`` uses its own JSON-RPC-envelope error path.

Lifted out of ``server.py`` so the handler code reads as business logic
instead of a 1,000-line wall that mixes reject-early guards with
request dispatch. ``server.py`` re-imports the names so tests reading
``mod._parse_content_length`` / ``mod._BadContentLength`` keep working.
"""

from __future__ import annotations

import json
import os
from typing import Any

from _common.http_response import write_json_response


# Hop budget — see a2a-design-1.md §Loop protection. X-A2A-Hop increments
# on every mesh hop; an inbound /a2a/send that sees hop>=budget is refused
# with 508 before any gateway work happens.
def _hop_budget() -> int:
    try:
        v = int(os.environ.get("A2A_HOP_BUDGET") or "8")
    except ValueError:
        return 8
    return v if v >= 1 else 8


# Review-14 P1-F1: cap inbound POST body size. Without this, an attacker
# declares Content-Length: 10GB and self.rfile.read(length) allocates that
# much memory on the sidecar process (OOM). OpenClaw's sidecar already
# applies a 64KB cap in readJsonBody; mirror that here so both runtimes
# behave the same under adversarial load. Tunable via A2A_MAX_BODY_BYTES
# for operators who need to route larger payloads (e.g. embedded images).
DEFAULT_MAX_BODY_BYTES = 64 * 1024


def _max_body_bytes() -> int:
    raw = os.environ.get("A2A_MAX_BODY_BYTES")
    if raw is None or str(raw).strip() == "":
        return DEFAULT_MAX_BODY_BYTES
    try:
        v = int(raw)
    except ValueError:
        return DEFAULT_MAX_BODY_BYTES
    return v if v > 0 else DEFAULT_MAX_BODY_BYTES


class _BadContentLength(Exception):
    """Raised when the client sends a Content-Length we refuse to honor."""


def _parse_content_length(headers: Any, *, cap: int) -> int:
    """Parse the request's Content-Length header, rejecting hostile values.

    Review-15 P1-G1: a raw ``int(self.headers.get('Content-Length') or 0)``
    accepts ``-1`` (causing ``rfile.read(-1)`` to block indefinitely waiting
    for EOF — a DoS on the ThreadingHTTPServer worker thread) and raises
    ``ValueError`` on non-numeric values (dropping the connection without
    a proper 400 response). This helper returns a non-negative bounded
    length or raises ``_BadContentLength`` for the handler to convert to
    an HTTP 400 / 413.
    """
    raw = headers.get("Content-Length") if hasattr(headers, "get") else None
    if raw is None:
        return 0
    stripped = str(raw).strip()
    if stripped == "":
        return 0
    try:
        length = int(stripped)
    except ValueError as exc:
        raise _BadContentLength(f"invalid Content-Length: {stripped!r}") from exc
    if length < 0:
        raise _BadContentLength(f"negative Content-Length: {length}")
    if length > cap:
        raise _BadContentLength(f"request body exceeds {cap} bytes")
    return length


def read_inbound_json_body(
    handler: Any,
    *,
    cap: int,
    request_id: str,
    rid_headers: dict[str, str],
) -> dict[str, Any] | None:
    """Parse an inbound POST body into a JSON dict.

    Unified prelude for ``/a2a/send`` and ``/a2a/outbound``: enforces the
    body-size cap, reads ``Content-Length`` bytes, parses UTF-8 JSON, and
    rejects non-dict payloads. On any failure it writes a uniform
    ``{error, request_id}`` response (400 for bad shape / JSON, 413 for
    oversize) with the caller's ``X-A2A-Request-Id`` header echoed back,
    and returns ``None`` — so the caller's next line is just ``return``.
    Returns the parsed dict on success.

    Not used by ``/mcp``: that endpoint needs JSON-RPC envelopes
    (``{jsonrpc, id, error: {code, message}}``) rather than flat
    ``{error}`` bodies, so it keeps its own parse path.
    """
    try:
        length = _parse_content_length(handler.headers, cap=cap)
    except _BadContentLength as exc:
        status = 413 if "exceeds" in str(exc) else 400
        write_json_response(
            handler,
            status,
            {"error": str(exc), "request_id": request_id},
            extra_headers=rid_headers,
        )
        return None
    raw = handler.rfile.read(length) if length else b""
    try:
        payload = json.loads(raw.decode("utf-8"))
    except Exception as exc:  # noqa: BLE001 — surface any parse error
        write_json_response(
            handler,
            400,
            {"error": f"bad json: {exc}", "request_id": request_id},
            extra_headers=rid_headers,
        )
        return None
    if not isinstance(payload, dict):
        write_json_response(
            handler,
            400,
            {"error": "body must be a JSON object", "request_id": request_id},
            extra_headers=rid_headers,
        )
        return None
    return payload


class _BadPayload(Exception):
    """Raised when a parsed-body field has the wrong shape.

    Distinct from ``_BadContentLength`` (which fires before the body is
    read): this one fires on a validated dict whose field-level types
    don't match the protocol. Callers translate the message into a
    400 ``{error, request_id}`` response.
    """


def parse_optional_non_empty_string(
    payload: dict[str, Any], field_name: str
) -> str | None:
    """Pull an optional protocol-level string field out of the parsed body.

    Review-14 P1-C and its /a2a/outbound mirror: fields like ``thread_id``
    are OPTIONAL (absent → ``None`` → downstream treats the turn as
    stateless). Present-but-wrong-type is a client bug worth surfacing
    as a 400 rather than silently dropping, so the peer learns their
    payload shape is broken. Consolidates the 13-line parse block that
    was duplicated byte-for-byte across ``/a2a/send`` and
    ``/a2a/outbound``.
    """
    raw = payload.get(field_name, None)
    if raw is None:
        return None
    if isinstance(raw, str) and raw:
        return raw
    raise _BadPayload(
        f"'{field_name}' must be a non-empty string when provided"
    )
