"""Back-compat shim. The real implementation now lives in
:mod:`_common.inbound_limits` so the OpenClaw sidecar can share the
same reject-early surface. Kept for any caller that still reaches for
``hermes/sidecar/inbound_limits.py`` by name.
"""

from _common.inbound_limits import (  # noqa: F401
    DEFAULT_MAX_BODY_BYTES,
    _BadContentLength,
    _BadPayload,
    _hop_budget,
    _max_body_bytes,
    _parse_content_length,
    parse_optional_non_empty_string,
    read_inbound_json_body,
    read_inbound_mcp_body,
    require_non_empty_string,
)
