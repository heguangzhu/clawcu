"""Minimal shared utilities for the A2A control plane (stdlib-only)."""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler
from typing import Any, Mapping


# -- Capped response reader --------------------------------------------------

class ResponseTooLarge(Exception):
    """Raised when a response body exceeds the configured byte cap."""


def read_capped_bytes(response, cap: int = 4 * 1024 * 1024) -> bytes:
    """Read up to ``cap`` bytes from ``response``; raise if exceeded."""
    chunks: list[bytes] = []
    total = 0
    while True:
        to_read = min(65536, max(0, cap + 1 - total))
        if to_read == 0:
            raise ResponseTooLarge(f"response exceeds {cap} bytes")
        chunk = response.read(to_read)
        if not chunk:
            break
        total += len(chunk)
        if total > cap:
            raise ResponseTooLarge(f"response exceeds {cap} bytes")
        chunks.append(chunk)
    return b"".join(chunks)


# -- JSON response writer (http.server) --------------------------------------

def write_json_response(
    handler: BaseHTTPRequestHandler,
    status: int,
    body: Any,
    extra_headers: Mapping[str, str] | None = None,
) -> None:
    """Serialize ``body`` as JSON and send a complete HTTP response."""
    payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(payload)))
    if extra_headers:
        for name, value in extra_headers.items():
            handler.send_header(name, value)
    handler.end_headers()
    handler.wfile.write(payload)


# -- Advertise host resolution -----------------------------------------------

def resolve_advertise_host(record) -> str:
    """Return the hostname a peer uses to reach this agent.

    Falls back to auto-detection: Docker Desktop → ``host.docker.internal``,
    plain Linux → ``127.0.0.1``.
    """
    if record.a2a_advertise_host:
        return record.a2a_advertise_host
    import platform
    if platform.system() in ("Darwin", "Windows"):
        return "host.docker.internal"
    return "127.0.0.1"
