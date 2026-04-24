"""Shared embedded MCP (Model Context Protocol) server for A2A sidecars.

Transport: streamable-http. Callers POST a JSON-RPC 2.0 request and get a
single JSON-RPC response. Surface:

  initialize → protocolVersion, serverInfo, capabilities.tools
  tools/list → [a2a_call_peer]
  tools/call → name=a2a_call_peer → forward_to_peer_fn(...)

The dispatcher is dependency-injected: callers pass ``lookup_peer_fn``,
``forward_to_peer_fn``, and (optionally) an outbound rate limiter. Both
the Hermes and OpenClaw sidecars share this implementation.

Previously one 451-line module; split per review-2 §10 into
``envelope`` / ``upstream`` / ``tool_desc`` / ``dispatcher`` so callers
that only need a subset (e.g. ``_common.inbound_limits`` needing only
``ERR_PARSE`` + ``json_rpc_error``) don't pull the dispatcher into their
import graph. All public names remain importable from ``_common.mcp``
exactly as before — external call sites and tests are unchanged.
"""

from _common.mcp.dispatcher import handle_mcp_request
from _common.mcp.envelope import (
    ERR_A2A_UPSTREAM,
    ERR_INTERNAL,
    ERR_INVALID_PARAMS,
    ERR_INVALID_REQUEST,
    ERR_METHOD_NOT_FOUND,
    ERR_PARSE,
    json_rpc_error,
    json_rpc_result,
)
from _common.mcp.tool_desc import (
    BASE_DESCRIPTION,
    MAX_PEERS_IN_DESCRIPTION,
    MAX_SKILLS_IN_PEER_LINE,
    MCP_PROTOCOL_VERSION,
    TOOL_NAME,
    format_peer_line,
    format_peer_summary,
    is_tool_desc_static,
    tool_desc_include_role,
    tool_descriptor,
)
from _common.mcp.upstream import UpstreamError, write_upstream_error_response

__all__ = [
    "MCP_PROTOCOL_VERSION",
    "TOOL_NAME",
    "MAX_PEERS_IN_DESCRIPTION",
    "MAX_SKILLS_IN_PEER_LINE",
    "BASE_DESCRIPTION",
    "ERR_PARSE",
    "ERR_INVALID_REQUEST",
    "ERR_METHOD_NOT_FOUND",
    "ERR_INVALID_PARAMS",
    "ERR_INTERNAL",
    "ERR_A2A_UPSTREAM",
    "UpstreamError",
    "write_upstream_error_response",
    "format_peer_line",
    "format_peer_summary",
    "is_tool_desc_static",
    "tool_descriptor",
    "tool_desc_include_role",
    "json_rpc_result",
    "json_rpc_error",
    "handle_mcp_request",
]
