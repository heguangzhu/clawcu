"""Shared inbound-payload field validators for the sidecars.

Once the body has been parsed into a dict (by hermes's
:func:`_common.inbound_limits.read_inbound_json_body` or openclaw's
``read_json_body``), the handlers still need field-level shape checks —
for example, ``thread_id`` is OPTIONAL but must be a non-empty string
*when provided*. That pattern showed up byte-for-byte in four places:
hermes /a2a/send, hermes /a2a/outbound, openclaw /a2a/send, and openclaw
/a2a/outbound. This module owns the single source of truth.

:class:`BadPayload` is distinct from the pre-read ``_BadContentLength``:
this one fires on an already-validated dict whose field-level type is
wrong. Callers translate the message into a uniform
``{error, request_id}`` 400 response via
:func:`write_bad_payload_response`.
"""

from __future__ import annotations

from typing import Any, Mapping

from _common.http_response import write_json_response


class BadPayload(Exception):
    """Raised when a parsed-body field has the wrong shape."""


def parse_optional_non_empty_string(
    payload: dict[str, Any], field_name: str
) -> str | None:
    """Pull an optional protocol-level string field out of the parsed body.

    Absent (key missing OR explicit ``None``) → ``None`` → downstream
    treats the turn as stateless. Present-but-wrong-type or empty string
    is a client bug worth surfacing as a 400 rather than silently
    dropping, so the peer learns their payload shape is broken.
    """
    raw = payload.get(field_name, None)
    if raw is None:
        return None
    if isinstance(raw, str) and raw:
        return raw
    raise BadPayload(
        f"'{field_name}' must be a non-empty string when provided"
    )


def require_non_empty_string(payload: dict[str, Any], field_name: str) -> str:
    """Pull a required protocol-level string field out of the parsed body.

    Sibling of :func:`parse_optional_non_empty_string` for fields like
    ``/a2a/send`` ``message`` or ``/a2a/outbound`` ``to`` where absence
    itself is a 400. Missing (including ``None``), wrong-type, and empty
    string all raise :class:`BadPayload`; the caller translates that to
    a single ``{error, request_id}`` response shape so four handler
    sites don't each maintain their own parallel inline check.
    """
    raw = payload.get(field_name, None)
    if isinstance(raw, str) and raw:
        return raw
    raise BadPayload(f"'{field_name}' must be a non-empty string")


def write_bad_payload_response(
    handler: Any,
    exc: BadPayload,
    *,
    request_id: str,
    rid_headers: Mapping[str, str],
) -> None:
    """Write the uniform 400 envelope for a :class:`BadPayload`.

    Both sidecars batch their required+optional field checks under one
    ``try`` so the rejection path is a single call. The envelope
    (``{error, request_id}`` + echoed ``X-A2A-Request-Id``) is identical
    across runtimes, so it lives next to the validators that raise it
    rather than being copy-pasted into each ``server.py``.
    """
    write_json_response(
        handler,
        400,
        {"error": str(exc), "request_id": request_id},
        extra_headers=rid_headers,
    )
