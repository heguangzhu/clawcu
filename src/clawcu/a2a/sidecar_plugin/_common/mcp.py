"""Shared embedded MCP (Model Context Protocol) server for A2A sidecars.

Transport: streamable-http. Callers POST a JSON-RPC 2.0 request and get a
single JSON-RPC response. Surface:

  initialize → protocolVersion, serverInfo, capabilities.tools
  tools/list → [a2a_call_peer]
  tools/call → name=a2a_call_peer → forward_to_peer_fn(...)

The dispatcher is dependency-injected: callers pass ``lookup_peer_fn``,
``forward_to_peer_fn``, and (optionally) an outbound rate limiter. Both
the Hermes and OpenClaw sidecars share this implementation; previously
each owned a near-identical copy.

Callback call convention is positional so any real-world ``lookup_peer``
/ ``forward_to_peer`` that keeps the documented parameter order works
unchanged regardless of whether its local parameter is named
``timeout`` or ``timeout_ms``:

  lookup_peer_fn(registry_url, peer_name, timeout)
  forward_to_peer_fn(endpoint, self_name, peer_name, message,
                    thread_id, hop, timeout, request_id)
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Optional

MCP_PROTOCOL_VERSION = "2024-11-05"
TOOL_NAME = "a2a_call_peer"
MAX_PEERS_IN_DESCRIPTION = 16
MAX_SKILLS_IN_PEER_LINE = 3

# JSON-RPC 2.0 + MCP error codes.
ERR_PARSE = -32700
ERR_INVALID_REQUEST = -32600
ERR_METHOD_NOT_FOUND = -32601
ERR_INVALID_PARAMS = -32602
ERR_INTERNAL = -32603
ERR_A2A_UPSTREAM = -32001

BASE_DESCRIPTION = (
    "Call another agent in the A2A federation and return its reply. "
    "Use when the current task needs data or work owned by a different "
    "agent (e.g., an analyst for market data, a writer for prose)."
)


class UpstreamError(Exception):
    """Raised by ``lookup_peer_fn`` / ``forward_to_peer_fn`` for MCP errors.

    Carries an HTTP-shaped status so the dispatcher can surface the
    correct ``httpStatus`` / ``peerStatus`` in ``error.data``. Hermes
    subclasses this as ``OutboundError`` with a legacy positional
    ``(http_status, message)`` signature for its existing call sites.
    """

    def __init__(
        self,
        message: str,
        http_status: Optional[int] = None,
        peer_status: Optional[int] = None,
    ) -> None:
        super().__init__(message)
        self.http_status = http_status
        self.peer_status = peer_status


def format_peer_line(peer: Dict[str, Any], *, include_role: bool = False) -> str:
    skills_raw = peer.get("skills")
    skills = skills_raw if isinstance(skills_raw, list) else []
    head = ", ".join(str(s) for s in skills[:MAX_SKILLS_IN_PEER_LINE])
    tail = ", ..." if len(skills) > MAX_SKILLS_IN_PEER_LINE else ""
    role_raw = peer.get("role") if include_role else None
    role = f" [{role_raw}]" if isinstance(role_raw, str) and role_raw else ""
    name = peer.get("name", "")
    if not head:
        return f"  - {name}{role}"
    return f"  - {name}{role} ({head}{tail})"


def format_peer_summary(
    peers: Any, self_name: Optional[str], *, include_role: bool = False
) -> str:
    """Multi-line summary injected into the tool description.

    Excludes self and caps at ``MAX_PEERS_IN_DESCRIPTION``. Returns ``""``
    when the list is empty or only contains self — callers then fall
    back to the static description.
    """
    if not isinstance(peers, list) or not peers:
        return ""
    others = [
        p for p in peers
        if isinstance(p, dict)
        and isinstance(p.get("name"), str)
        and p.get("name") != self_name
    ]
    if not others:
        return ""
    shown = others[:MAX_PEERS_IN_DESCRIPTION]
    hidden = len(others) - len(shown)
    lines = ["", "Available peers:"] + [
        format_peer_line(p, include_role=include_role) for p in shown
    ]
    if hidden > 0:
        lines.append(f"  ...and {hidden} more")
    return "\n".join(lines)


def tool_descriptor(
    peers: Any = None,
    self_name: Optional[str] = None,
    *,
    include_role: bool = False,
) -> Dict[str, Any]:
    description = BASE_DESCRIPTION
    summary = format_peer_summary(peers, self_name, include_role=include_role)
    if summary:
        description += summary
        description += (
            "\n\nThe `to` field must match one of the peers above "
            "(case-sensitive)."
        )
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


def json_rpc_result(rpc_id: Any, result: Any) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": rpc_id, "result": result}


def json_rpc_error(
    rpc_id: Any, code: int, message: str, data: Any = None
) -> Dict[str, Any]:
    err: Dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": rpc_id, "error": err}


def _default_outbound_key(**kwargs) -> str:
    # Lazy import so importing _common.mcp doesn't force _common.outbound_limit.
    from _common.outbound_limit import key_for
    return key_for(**kwargs)


def handle_mcp_request(
    body: Any,
    *,
    self_name: str,
    registry_url: str,
    timeout: Any,
    request_id: Optional[str],
    plugin_version: str,
    lookup_peer_fn: Callable,
    forward_to_peer_fn: Callable,
    outbound_limiter: Any = None,
    outbound_limit_key_fn: Optional[Callable] = None,
    list_peers_fn: Optional[Callable] = None,
    include_role: bool = False,
) -> Dict[str, Any]:
    """Dispatch one JSON-RPC 2.0 request against the embedded MCP surface."""
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
                        "version": plugin_version,
                    },
                    "capabilities": {"tools": {}},
                },
            )
        if method == "tools/list":
            peers: Any = None
            if callable(list_peers_fn):
                try:
                    peers = list_peers_fn()
                except Exception:  # noqa: BLE001
                    # tools/list must never fail because of a registry
                    # hiccup — the LLM must still see the tool. Fallback
                    # is the static description (a2a-design-5.md §P1-H).
                    peers = None
            return json_rpc_result(
                rpc_id,
                {
                    "tools": [
                        tool_descriptor(
                            peers=peers,
                            self_name=self_name,
                            include_role=include_role,
                        )
                    ]
                },
            )
        if method == "tools/call":
            return _handle_tools_call(
                rpc_id,
                params,
                self_name=self_name,
                registry_url=registry_url,
                timeout=timeout,
                request_id=request_id,
                lookup_peer_fn=lookup_peer_fn,
                forward_to_peer_fn=forward_to_peer_fn,
                outbound_limiter=outbound_limiter,
                outbound_limit_key_fn=outbound_limit_key_fn,
            )
        if method in ("notifications/initialized", "ping"):
            return json_rpc_result(rpc_id, {})
        return json_rpc_error(rpc_id, ERR_METHOD_NOT_FOUND, f"unknown method: {method}")
    except Exception as exc:  # noqa: BLE001
        return json_rpc_error(rpc_id, ERR_INTERNAL, str(exc))


def _handle_tools_call(
    rpc_id: Any,
    params: Any,
    *,
    self_name: str,
    registry_url: str,
    timeout: Any,
    request_id: Optional[str],
    lookup_peer_fn: Callable,
    forward_to_peer_fn: Callable,
    outbound_limiter: Any = None,
    outbound_limit_key_fn: Optional[Callable] = None,
) -> Dict[str, Any]:
    # P2-K: every error response in this handler carries requestId in
    # data so a JSON-RPC-only client can correlate to X-A2A-Request-Id.
    def _with_rid(extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        merged: Dict[str, Any] = dict(extra or {})
        merged["requestId"] = request_id
        return merged

    if not isinstance(params, dict) or params.get("name") != TOOL_NAME:
        return json_rpc_error(
            rpc_id,
            ERR_METHOD_NOT_FOUND,
            f"unknown tool: {params.get('name') if isinstance(params, dict) else None}",
            _with_rid(),
        )
    args = params.get("arguments") or {}
    if not isinstance(args.get("to"), str) or not args["to"]:
        return json_rpc_error(rpc_id, ERR_INVALID_PARAMS, "missing 'to' (string)", _with_rid())
    if not isinstance(args.get("message"), str) or not args["message"]:
        return json_rpc_error(rpc_id, ERR_INVALID_PARAMS, "missing 'message' (string)", _with_rid())
    raw_thread = args.get("thread_id")
    thread_id = raw_thread if isinstance(raw_thread, str) and raw_thread else None

    # Self-origin rate limit (a2a-design-4.md §P1-B). Shared bucket with
    # /a2a/outbound so an LLM firing 200 a2a_call_peer calls in one turn
    # can't nuke the provider quota.
    if outbound_limiter is not None:
        key_fn = outbound_limit_key_fn if callable(outbound_limit_key_fn) else _default_outbound_key
        key = key_fn(thread_id=thread_id, self_name=self_name)
        decision = outbound_limiter.check(key)
        if not decision.allowed:
            return json_rpc_error(
                rpc_id,
                ERR_A2A_UPSTREAM,
                f"self-origin rate limit exceeded ({decision.limit}/min)",
                _with_rid(
                    {
                        "httpStatus": 429,
                        "retryAfterMs": decision.retry_after_ms,
                    }
                ),
            )

    try:
        card = lookup_peer_fn(registry_url, args["to"], timeout)
    except UpstreamError as exc:
        return json_rpc_error(
            rpc_id,
            ERR_A2A_UPSTREAM,
            f"registry lookup failed: {exc}",
            _with_rid({"httpStatus": exc.http_status or 503}),
        )
    except Exception as exc:  # noqa: BLE001
        return json_rpc_error(
            rpc_id,
            ERR_A2A_UPSTREAM,
            f"registry lookup failed: {exc}",
            _with_rid({"httpStatus": 503}),
        )

    try:
        peer_resp = forward_to_peer_fn(
            card["endpoint"],
            self_name,
            args["to"],
            args["message"],
            thread_id,
            1,
            timeout,
            request_id,
        )
    except UpstreamError as exc:
        return json_rpc_error(
            rpc_id,
            ERR_A2A_UPSTREAM,
            f"peer call failed: {exc}",
            _with_rid(
                {
                    "httpStatus": exc.http_status or 502,
                    "peerStatus": exc.peer_status,
                }
            ),
        )
    except Exception as exc:  # noqa: BLE001
        return json_rpc_error(
            rpc_id,
            ERR_A2A_UPSTREAM,
            f"peer call failed: {exc}",
            _with_rid({"httpStatus": 502}),
        )

    reply = peer_resp.get("reply") if isinstance(peer_resp.get("reply"), str) else ""
    from_val = peer_resp.get("from") or self_name
    returned_thread = peer_resp.get("thread_id")
    resp_thread = returned_thread if isinstance(returned_thread, str) else thread_id
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
    "format_peer_line",
    "format_peer_summary",
    "tool_descriptor",
    "json_rpc_result",
    "json_rpc_error",
    "handle_mcp_request",
]
