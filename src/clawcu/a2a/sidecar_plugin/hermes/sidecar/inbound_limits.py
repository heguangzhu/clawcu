"""Hermes sidecar inbound-request reject-early guards.

Three knobs keep the sidecar from turning adversarial requests into OOMs
or into 5-minute-timeout-pinned worker threads:

1. ``_max_body_bytes()`` — cap on ``rfile.read(length)`` allocations.
2. ``_parse_content_length()`` — rejects negative / non-numeric /
   over-the-cap ``Content-Length`` headers before any read happens.
3. ``_hop_budget()`` — A2A ``X-A2A-Hop`` limit that refuses loop traffic
   with a 508 before any gateway work.

Lifted out of ``server.py`` so the handler code reads as business logic
instead of a 1,000-line wall that mixes reject-early guards with
request dispatch. ``server.py`` re-imports the names so tests reading
``mod._parse_content_length`` / ``mod._BadContentLength`` keep working.
"""

from __future__ import annotations

import os
from typing import Any


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
