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
from typing import Any, Mapping, Optional

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


__all__ = [
    "REQUEST_ID_HEADER",
    "HOP_HEADER",
    "DEFAULT_HOP_BUDGET",
    "hop_budget_from_env",
    "looks_like_request_id",
    "read_or_mint_request_id",
    "read_hop_header",
]
