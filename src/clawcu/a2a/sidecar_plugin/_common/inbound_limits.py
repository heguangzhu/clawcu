"""Shared inbound-request reject-early guards for both sidecars.

Four knobs keep the sidecar from turning adversarial requests into OOMs
or into 5-minute-timeout-pinned worker threads:

1. ``_max_body_bytes()`` — cap on ``rfile.read(length)`` allocations.
2. ``_parse_content_length()`` — rejects negative / non-numeric /
   over-the-cap ``Content-Length`` headers before any read happens.
3. ``_hop_budget()`` — A2A ``X-A2A-Hop`` limit that refuses loop traffic
   with a 508 before any gateway work.
4. ``read_inbound_json_body()`` — composes (1)+(2) with JSON + dict
   validation and writes a uniform ``{error, request_id}`` 400/413
   response on any failure. ``/a2a/send`` and ``/a2a/outbound`` share it.
5. ``read_inbound_mcp_body()`` — same body-cap + content-length discipline
   as (4) but writes the JSON-RPC 2.0 envelope ``/mcp`` requires, and
   allows ``None`` / non-dict payloads (``handle_mcp_request`` does its
   own schema check).

Previously lived under ``hermes/sidecar/inbound_limits.py``; moved into
``_common`` so OpenClaw can share the same reject-early surface instead
of carrying its own Content-Length-less variant. Sidecar ``server.py``
modules re-import the names so tests reading ``mod._parse_content_length``
/ ``mod._BadContentLength`` keep working.
"""

from __future__ import annotations

import json
import os
from typing import Any

from _common.http_response import write_json_response
from _common.mcp import ERR_PARSE as _MCP_ERR_PARSE, json_rpc_error
from _common.payload import (  # noqa: F401
    BadPayload as _BadPayload,
    parse_optional_non_empty_string,
    require_non_empty_string,
)
from _common.protocol import (
    REQUEST_ID_HEADER,
    hop_budget_from_env,
    read_or_mint_request_id,
)


# Hop budget — see a2a-design-1.md §Loop protection. X-A2A-Hop increments
# on every mesh hop; an inbound /a2a/send that sees hop>=budget is refused
# with 508 before any gateway work happens. Thin wrapper so existing test
# stubs that reach ``mod._hop_budget`` keep working; the implementation
# lives in ``_common.protocol`` so both sidecars read it the same way.
def _hop_budget() -> int:
    return hop_budget_from_env()


# Review-14 P1-F1: cap inbound POST body size. Without this, an attacker
# declares Content-Length: 10GB and self.rfile.read(length) allocates that
# much memory on the sidecar process (OOM). Tunable via A2A_MAX_BODY_BYTES
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


def read_inbound_mcp_body(
    handler: Any,
    *,
    cap: int,
    err_parse_code: int,
    rid_headers: dict[str, str],
) -> tuple[bool, Any]:
    """Parse an inbound POST body destined for ``/mcp``.

    Counterpart to :func:`read_inbound_json_body` for the JSON-RPC 2.0
    endpoint. Shares the body-size cap + ``Content-Length`` discipline,
    but differs in two ways:

    * Error responses use the JSON-RPC envelope
      ``{"jsonrpc":"2.0","id":null,"error":{"code":<parse>,"message":...}}``
      with the caller-supplied ``err_parse_code`` (``MCP_ERR_PARSE``).
    * Empty bodies yield ``payload=None`` and non-dict payloads are
      passed through — ``handle_mcp_request`` does its own schema check.

    Returns ``(True, payload)`` on success (``payload`` may be any
    JSON-decoded value, including ``None``). Returns ``(False, None)``
    when an error response has already been written; the caller's next
    line is just ``return``.
    """
    try:
        length = _parse_content_length(handler.headers, cap=cap)
    except _BadContentLength as exc:
        status = 413 if "exceeds" in str(exc) else 400
        write_json_response(
            handler,
            status,
            json_rpc_error(None, err_parse_code, str(exc)),
            extra_headers=rid_headers,
        )
        return False, None
    raw = handler.rfile.read(length) if length else b""
    try:
        payload = json.loads(raw.decode("utf-8")) if raw else None
    except Exception as exc:  # noqa: BLE001 — surface any parse error
        write_json_response(
            handler,
            400,
            json_rpc_error(None, err_parse_code, f"bad json: {exc}"),
            extra_headers=rid_headers,
        )
        return False, None
    return True, payload


def mcp_prelude(
    handler: Any, *, cap: int
) -> tuple[str, dict[str, str], Any, bool]:
    """Shared ``/mcp`` entry-point prelude: mint the request ID, build the
    rid-headers dict, and read the JSON-RPC body with the MCP parse-error
    code baked in.

    Returns ``(request_id, rid_headers, payload, ok)``. When ``ok`` is
    ``False`` the parse-error envelope has already been written (via
    :func:`read_inbound_mcp_body`, which emits the JSON-RPC 2.0 shape
    ``/mcp`` requires) and the caller's next line is just ``return``.

    The pairing — request-id mint, rid-headers dict, and the
    ``err_parse_code=ERR_PARSE`` call into ``read_inbound_mcp_body`` — is
    identical in both sidecar flavors. Centralising it keeps the
    request-ID echo and the JSON-RPC parse envelope from drifting if
    either sidecar grows a new MCP entry point.
    """
    request_id = read_or_mint_request_id(handler.headers)
    rid_headers = {REQUEST_ID_HEADER: request_id}
    ok, payload = read_inbound_mcp_body(
        handler,
        cap=cap,
        err_parse_code=_MCP_ERR_PARSE,
        rid_headers=rid_headers,
    )
    return request_id, rid_headers, payload, ok


# parse_optional_non_empty_string + the BadPayload exception live in
# _common/payload.py (re-imported above under the legacy _BadPayload name
# so tests that reach mod._BadPayload keep working). Two sidecars and four
# handlers all do the same thread_id-style optional-string check, so the
# single source of truth lives in _common.
