"""JSON-RPC 2.0 dispatcher for the embedded MCP surface.

Extracted from the monolithic ``_common.mcp`` (review-2 §10). This is
the business-logic core of the MCP implementation: ``initialize``,
``tools/list``, ``tools/call``. Isolated from the envelope/upstream/
tool-desc modules so those smaller pieces can be imported alone (see
``_common.inbound_limits`` — it only needs ``ERR_PARSE`` +
``json_rpc_error``, not the dispatcher).

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

from _common.mcp.envelope import (
    ERR_A2A_UPSTREAM,
    ERR_INTERNAL,
    ERR_INVALID_PARAMS,
    ERR_INVALID_REQUEST,
    ERR_METHOD_NOT_FOUND,
    json_rpc_error,
    json_rpc_result,
)
from _common.mcp.tool_desc import (
    MCP_PROTOCOL_VERSION,
    TOOL_NAME,
    tool_descriptor,
)
from _common.mcp.upstream import UpstreamError


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


__all__ = ["handle_mcp_request"]
