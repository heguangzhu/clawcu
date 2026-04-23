"""Shared HTTP response helpers for the sidecars.

Both sidecars reply to every route with JSON — agent card, error
responses, /a2a/send reply, /mcp dispatch, peer-list summary. They
previously each had a near-identical ``_write_json`` / ``_json_response``
helper; this module is the single source of truth.
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler
from typing import Any, Mapping, Optional


def write_json_response(
    handler: BaseHTTPRequestHandler,
    status: int,
    body: Any,
    extra_headers: Optional[Mapping[str, str]] = None,
) -> None:
    """Serialize ``body`` as JSON and send a complete HTTP response.

    Uses ``ensure_ascii=False`` so non-ASCII error messages (Chinese
    persona strings, emoji) stay human-readable on the wire instead of
    being escaped to ``\\uXXXX``. Header names use canonical casing
    (``Content-Type`` / ``Content-Length``) — HTTP/1.1 is case-insensitive
    so this is cosmetic, but consistent with stdlib convention.
    """
    payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(payload)))
    if extra_headers:
        for name, value in extra_headers.items():
            handler.send_header(name, value)
    handler.end_headers()
    handler.wfile.write(payload)


__all__ = ["write_json_response"]
