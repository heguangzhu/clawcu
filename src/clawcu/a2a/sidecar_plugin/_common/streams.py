"""Capped streaming reader for urllib / http.client responses.

Both sidecars must refuse to buffer arbitrarily-large responses: a
compromised peer or registry (or a mis-aligned gateway bug) would
otherwise OOM the sidecar by claiming a huge ``Content-Length``.
Previously duplicated in ``openclaw/sidecar/server.py`` (returning
``str``) and ``hermes/sidecar/server.py`` (returning ``bytes``); the
implementations had diverged on both signature and on whether they
probed with ``read(cap + 1)`` in one shot or chunked.

This module is the single source of truth. ``read_capped_bytes`` is the
primitive; ``read_capped_text`` is the thin UTF-8 wrapper. We read in
64 KiB chunks so a response claiming a huge ``Content-Length`` aborts
before allocating anywhere near that many bytes.
"""

from __future__ import annotations

DEFAULT_RESPONSE_CAP_BYTES = 4 * 1024 * 1024  # 4 MiB — outbound/peer default


class ResponseTooLarge(Exception):
    """Raised when a response body exceeds the configured byte cap."""


def read_capped_bytes(response, cap: int = DEFAULT_RESPONSE_CAP_BYTES) -> bytes:
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


def read_capped_text(
    response,
    cap: int = DEFAULT_RESPONSE_CAP_BYTES,
    *,
    encoding: str = "utf-8",
    errors: str = "replace",
) -> str:
    """Read up to ``cap`` bytes and decode as text.

    ``errors='replace'`` ensures a malformed-UTF-8 attack surfaces as
    a ``�`` in the error message instead of crashing the handler with
    ``UnicodeDecodeError`` — the cap check is the real safety net.
    """
    return read_capped_bytes(response, cap).decode(encoding, errors=errors)
