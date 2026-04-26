"""Upstream error class + HTTP envelope writer shared by both sidecars.

Extracted from the monolithic ``_common.mcp`` module (review-2 §10).
Hermes subclasses ``UpstreamError`` as ``OutboundError`` with a legacy
positional ``(http_status, message)`` signature; the dispatcher catches
``UpstreamError`` polymorphically in ``tools/call``.
"""

from __future__ import annotations

from typing import Any, Dict, Mapping, Optional

from _common.http_response import write_json_response


class UpstreamError(Exception):
    """Raised by ``lookup_peer_fn`` / ``forward_to_peer_fn`` for MCP errors.

    Carries an HTTP-shaped status so the dispatcher can surface the
    correct ``httpStatus`` / ``peerStatus`` in ``error.data``. Hermes
    subclasses this as ``OutboundError`` with a legacy positional
    ``(http_status, message)`` signature for its existing call sites.
    """

    def __init__(
        self,
        message: str,
        http_status: Optional[int] = None,
        peer_status: Optional[int] = None,
    ) -> None:
        super().__init__(message)
        self.http_status = http_status
        self.peer_status = peer_status


def write_upstream_error_response(
    handler: Any,
    exc: UpstreamError,
    *,
    request_id: str,
    rid_headers: Mapping[str, str],
    default_status: int = 502,
) -> None:
    """Write the uniform ``{error, request_id[, peer_status]}`` envelope
    both sidecars emit when lookup_peer / forward_to_peer raises.

    ``default_status`` is the status used when ``exc.http_status`` is
    missing/falsy — 503 for lookup-phase failures (registry unreachable),
    502 for forward-phase failures (peer unreachable). ``peer_status``
    is added to the body only when the exception carries one so the
    envelope stays flat for simple upstream failures.
    """
    body: Dict[str, Any] = {"error": str(exc), "request_id": request_id}
    if exc.peer_status is not None:
        body["peer_status"] = exc.peer_status
    write_json_response(
        handler,
        exc.http_status or default_status,
        body,
        extra_headers=rid_headers,
    )


__all__ = ["UpstreamError", "write_upstream_error_response"]
