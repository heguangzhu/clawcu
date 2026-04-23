"""Embedded MCP (Model Context Protocol) server (Python port of mcp.js).

Transport: streamable-http. Callers POST a JSON-RPC 2.0 request to /mcp and
get a single JSON-RPC response back. Surface:
  initialize → protocolVersion, serverInfo, capabilities.tools
  tools/list → [a2a_call_peer]
  tools/call → name=a2a_call_peer → calls deps["forwardToPeer"]
"""
from __future__ import annotations

from typing import Any, Dict, Optional

MCP_PROTOCOL_VERSION = "2024-11-05"
TOOL_NAME = "a2a_call_peer"
MAX_PEERS_IN_DESCRIPTION = 16
MAX_SKILLS_IN_PEER_LINE = 3

BASE_DESCRIPTION = (
    "Call another agent in the A2A federation and return its reply. "
    "Use when the current task needs data or work owned by a different "
    "agent (e.g., an analyst for market data, a writer for prose)."
)

ERR_PARSE = -32700
ERR_INVALID_REQUEST = -32600
ERR_METHOD_NOT_FOUND = -32601
ERR_INVALID_PARAMS = -32602
ERR_INTERNAL = -32603
ERR_A2A_UPSTREAM = -32001


def format_peer_line(peer: dict, include_role: bool = False) -> str:
    skills = peer.get("skills")
    if not isinstance(skills, list):
        skills = []
    head = ", ".join(skills[:MAX_SKILLS_IN_PEER_LINE])
    tail = ", ..." if len(skills) > MAX_SKILLS_IN_PEER_LINE else ""
    role_val = peer.get("role") if include_role else None
    role = f" [{role_val}]" if isinstance(role_val, str) and role_val else ""
    name = peer.get("name", "")
    if not head:
        return f"  - {name}{role}"
    return f"  - {name}{role} ({head}{tail})"


def format_peer_summary(peers, self_name: Optional[str], include_role: bool = False) -> str:
    if not isinstance(peers, list) or not peers:
        return ""
    others = [p for p in peers if isinstance(p, dict) and p.get("name") and p.get("name") != self_name]
    if not others:
        return ""
    shown = [format_peer_line(p, include_role=include_role) for p in others[:MAX_PEERS_IN_DESCRIPTION]]
    hidden = len(others) - len(shown)
    tail = [f"  ...and {hidden} more"] if hidden > 0 else []
    return "\n".join([""] + ["Available peers:"] + shown + tail)


def tool_descriptor(peers=None, self_name: Optional[str] = None, include_role: bool = False) -> dict:
    description = BASE_DESCRIPTION
    summary = format_peer_summary(peers, self_name, include_role=include_role)
    if summary:
        description += summary
        description += "\n\nThe `to` field must match one of the peers above (case-sensitive)."
    else:
        description += " The target agent name must be registered in the A2A registry."
    return {
        "name": TOOL_NAME,
        "description": description,
        "inputSchema": {
            "type": "object",
            "properties": {
                "to": {
                    "type": "string",
                    "description": "Peer agent name as registered in the A2A registry.",
                },
                "message": {
                    "type": "string",
                    "description": "The question or task for the peer agent.",
                },
                "thread_id": {
                    "type": "string",
                    "description": "Optional. Reuse a prior conversation thread with the peer.",
                },
            },
            "required": ["to", "message"],
        },
    }


def json_rpc_result(rpc_id, result) -> dict:
    return {"jsonrpc": "2.0", "id": rpc_id, "result": result}


def json_rpc_error(rpc_id, code: int, message: str, data=None) -> dict:
    err: Dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": rpc_id, "error": err}


class UpstreamError(Exception):
    """Raised by forward_to_peer / lookup_peer for correlated MCP errors."""

    def __init__(self, message: str, http_status: Optional[int] = None, peer_status: Optional[int] = None):
        super().__init__(message)
        self.http_status = http_status
        self.peer_status = peer_status


def handle_mcp_request(body, deps: dict) -> dict:
    """Dispatches a JSON-RPC request. `deps` is a dict with:
      plugin_version, self_name, include_role, registry_url, timeout_ms,
      request_id, outbound_limiter, outbound_limit_key (callable),
      list_peers (callable -> list or None),
      lookup_peer (callable -> {"endpoint": ...}),
      forward_to_peer (callable -> {"reply": ..., "from": ..., "thread_id": ...})
    """
    if (
        not isinstance(body, dict)
        or body.get("jsonrpc") != "2.0"
        or not isinstance(body.get("method"), str)
    ):
        rpc_id = body.get("id") if isinstance(body, dict) else None
        return json_rpc_error(rpc_id, ERR_INVALID_REQUEST, "expected JSON-RPC 2.0 request")
    rpc_id = body.get("id")
    method = body["method"]
    params = body.get("params") or {}
    try:
        if method == "initialize":
            return json_rpc_result(
                rpc_id,
                {
                    "protocolVersion": MCP_PROTOCOL_VERSION,
                    "serverInfo": {
                        "name": "clawcu-a2a",
                        "version": deps.get("plugin_version") or "unknown",
                    },
                    "capabilities": {"tools": {}},
                },
            )
        if method == "tools/list":
            peers = None
            list_peers = deps.get("list_peers")
            if callable(list_peers):
                try:
                    peers = list_peers()
                except Exception:
                    peers = None
            return json_rpc_result(
                rpc_id,
                {
                    "tools": [
                        tool_descriptor(
                            peers=peers,
                            self_name=deps.get("self_name"),
                            include_role=bool(deps.get("include_role")),
                        )
                    ]
                },
            )
        if method == "tools/call":
            return _handle_tools_call(rpc_id, params, deps)
        if method in ("notifications/initialized", "ping"):
            return json_rpc_result(rpc_id, {})
        return json_rpc_error(rpc_id, ERR_METHOD_NOT_FOUND, f"unknown method: {method}")
    except Exception as exc:
        return json_rpc_error(rpc_id, ERR_INTERNAL, str(exc))


def _handle_tools_call(rpc_id, params, deps) -> dict:
    request_id = deps.get("request_id") or None

    def with_rid(extra: Optional[dict] = None) -> dict:
        base = dict(extra or {})
        base["requestId"] = request_id
        return base

    if not isinstance(params, dict) or params.get("name") != TOOL_NAME:
        return json_rpc_error(
            rpc_id,
            ERR_METHOD_NOT_FOUND,
            f"unknown tool: {params.get('name') if isinstance(params, dict) else None}",
            with_rid(),
        )
    args = params.get("arguments") or {}
    if not isinstance(args.get("to"), str) or not args.get("to"):
        return json_rpc_error(rpc_id, ERR_INVALID_PARAMS, "missing 'to' (string)", with_rid())
    if not isinstance(args.get("message"), str) or not args.get("message"):
        return json_rpc_error(rpc_id, ERR_INVALID_PARAMS, "missing 'message' (string)", with_rid())
    thread_id = args.get("thread_id") if isinstance(args.get("thread_id"), str) and args.get("thread_id") else None

    limiter = deps.get("outbound_limiter")
    limit_key_fn = deps.get("outbound_limit_key")
    if limiter is not None and callable(limit_key_fn):
        key = limit_key_fn(thread_id=thread_id, self_name=deps.get("self_name"))
        decision = limiter.check(key)
        if not decision.allowed:
            return json_rpc_error(
                rpc_id,
                ERR_A2A_UPSTREAM,
                f"self-origin rate limit exceeded ({decision.limit}/min)",
                with_rid({"httpStatus": 429, "retryAfterMs": decision.retry_after_ms}),
            )

    lookup_peer = deps.get("lookup_peer")
    forward_to_peer = deps.get("forward_to_peer")
    if not callable(lookup_peer) or not callable(forward_to_peer):
        return json_rpc_error(rpc_id, ERR_INTERNAL, "peer dispatch not configured", with_rid())

    try:
        card = lookup_peer(
            registry_url=deps.get("registry_url"),
            peer_name=args["to"],
            timeout_ms=deps.get("timeout_ms"),
        )
    except UpstreamError as exc:
        return json_rpc_error(
            rpc_id,
            ERR_A2A_UPSTREAM,
            f"registry lookup failed: {exc}",
            with_rid({"httpStatus": exc.http_status or 503}),
        )
    except Exception as exc:
        return json_rpc_error(
            rpc_id,
            ERR_A2A_UPSTREAM,
            f"registry lookup failed: {exc}",
            with_rid({"httpStatus": 503}),
        )

    try:
        peer_resp = forward_to_peer(
            endpoint=card["endpoint"],
            self_name=deps.get("self_name"),
            peer_name=args["to"],
            message=args["message"],
            thread_id=thread_id,
            hop=1,
            timeout_ms=deps.get("timeout_ms"),
            request_id=request_id,
        )
    except UpstreamError as exc:
        return json_rpc_error(
            rpc_id,
            ERR_A2A_UPSTREAM,
            f"peer call failed: {exc}",
            with_rid({"httpStatus": exc.http_status or 502, "peerStatus": exc.peer_status}),
        )
    except Exception as exc:
        return json_rpc_error(
            rpc_id,
            ERR_A2A_UPSTREAM,
            f"peer call failed: {exc}",
            with_rid({"httpStatus": 502}),
        )

    reply = peer_resp.get("reply") if isinstance(peer_resp.get("reply"), str) else ""
    from_val = peer_resp.get("from") or deps.get("self_name")
    resp_thread = peer_resp.get("thread_id") if isinstance(peer_resp.get("thread_id"), str) else thread_id
    return json_rpc_result(
        rpc_id,
        {
            "content": [{"type": "text", "text": reply}],
            "isError": False,
            "structuredContent": {
                "from": from_val,
                "to": args["to"],
                "reply": reply,
                "thread_id": resp_thread,
                "request_id": request_id,
            },
        },
    )
