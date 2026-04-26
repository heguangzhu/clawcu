"""JSON-RPC 2.0 envelope helpers + MCP error codes.

Extracted from the monolithic ``_common.mcp`` module (review-2 §10) so
callers that only need ``json_rpc_error`` / ``ERR_PARSE`` (e.g.
``_common.inbound_limits``) don't pull the dispatcher or the upstream
error machinery into their import graph.

The split is internal: all names remain re-exported from
``_common.mcp``, so external imports (sidecar servers, tests) are
unchanged.
"""

from __future__ import annotations

from typing import Any, Dict

# JSON-RPC 2.0 + MCP error codes.
ERR_PARSE = -32700
ERR_INVALID_REQUEST = -32600
ERR_METHOD_NOT_FOUND = -32601
ERR_INVALID_PARAMS = -32602
ERR_INTERNAL = -32603
ERR_A2A_UPSTREAM = -32001


def json_rpc_result(rpc_id: Any, result: Any) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": rpc_id, "result": result}


def json_rpc_error(
    rpc_id: Any, code: int, message: str, data: Any = None
) -> Dict[str, Any]:
    err: Dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": rpc_id, "error": err}


__all__ = [
    "ERR_PARSE",
    "ERR_INVALID_REQUEST",
    "ERR_METHOD_NOT_FOUND",
    "ERR_INVALID_PARAMS",
    "ERR_INTERNAL",
    "ERR_A2A_UPSTREAM",
    "json_rpc_result",
    "json_rpc_error",
]
