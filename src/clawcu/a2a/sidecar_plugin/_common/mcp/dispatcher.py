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

from datetime import datetime, timezone
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
    ASYNC_TOOL_NAMES,
    MCP_PROTOCOL_VERSION,
    TOOL_NAME,
    TOOL_NAME_ASYNC,
    TOOL_NAME_CANCEL_TASK,
    TOOL_NAME_GET_TASK,
    async_call_tool_descriptor,
    cancel_task_tool_descriptor,
    get_task_tool_descriptor,
    tool_descriptor,
)
from _common.mcp.upstream import UpstreamError


def _default_outbound_key(**kwargs) -> str:
    # Lazy import so importing _common.mcp doesn't force _common.outbound_limit.
    from _common.outbound_limit import key_for
    return key_for(**kwargs)


def _parse_iso(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _format_age(iso_str: Any, *, now: Optional[datetime] = None) -> str:
    """Render an age relative to ``now`` as ``Xs`` / ``XmYs`` / ``ХhYm``.

    Returns ``""`` when the input doesn't parse — the caller drops the
    field rather than emitting ``"???"``.
    """
    parsed = _parse_iso(iso_str)
    if parsed is None:
        return ""
    if now is None:
        now = datetime.now(timezone.utc)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    delta = (now - parsed).total_seconds()
    if delta < 0:
        delta = 0.0
    seconds = int(delta)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m{seconds % 60:02d}s"
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    return f"{hours}h{minutes:02d}m"


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
    # Async tool plumbing (Phase 1). When ``async_enabled`` is True, the
    # three task-model tools appear in tools/list and tools/call routes
    # them via ``get_task_fn`` / ``cancel_task_fn`` (and
    # ``forward_to_peer_fn`` called with ``mode="async"`` for dispatch).
    async_enabled: bool = False,
    get_task_fn: Optional[Callable] = None,
    cancel_task_fn: Optional[Callable] = None,
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
            tools: list = [
                tool_descriptor(
                    peers=peers,
                    self_name=self_name,
                    include_role=include_role,
                )
            ]
            if async_enabled:
                tools.append(async_call_tool_descriptor())
                tools.append(get_task_tool_descriptor())
                tools.append(cancel_task_tool_descriptor())
            return json_rpc_result(rpc_id, {"tools": tools})
        if method == "tools/call":
            tool_name = params.get("name") if isinstance(params, dict) else None
            if async_enabled and tool_name in ASYNC_TOOL_NAMES:
                return _handle_async_tools_call(
                    rpc_id,
                    params,
                    tool_name=tool_name,
                    self_name=self_name,
                    registry_url=registry_url,
                    timeout=timeout,
                    request_id=request_id,
                    lookup_peer_fn=lookup_peer_fn,
                    forward_to_peer_fn=forward_to_peer_fn,
                    get_task_fn=get_task_fn,
                    cancel_task_fn=cancel_task_fn,
                    outbound_limiter=outbound_limiter,
                    outbound_limit_key_fn=outbound_limit_key_fn,
                )
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
        # Pin mode="sync" so the peer always returns the inline reply text,
        # regardless of its A2A_DEFAULT_MODE. The MCP tool name itself is the
        # contract — leaving mode unset would let the peer's wire-level
        # default flip this tool into async (202 + task_id, no reply).
        peer_resp = forward_to_peer_fn(
            card["endpoint"],
            self_name,
            args["to"],
            args["message"],
            thread_id,
            1,
            timeout,
            request_id,
            "sync",
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


def _handle_async_tools_call(
    rpc_id: Any,
    params: Any,
    *,
    tool_name: str,
    self_name: str,
    registry_url: str,
    timeout: Any,
    request_id: Optional[str],
    lookup_peer_fn: Callable,
    forward_to_peer_fn: Callable,
    get_task_fn: Optional[Callable],
    cancel_task_fn: Optional[Callable],
    outbound_limiter: Any = None,
    outbound_limit_key_fn: Optional[Callable] = None,
) -> Dict[str, Any]:
    def _with_rid(extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        merged: Dict[str, Any] = dict(extra or {})
        merged["requestId"] = request_id
        return merged

    args = params.get("arguments") if isinstance(params, dict) else None
    if not isinstance(args, dict):
        return json_rpc_error(
            rpc_id, ERR_INVALID_PARAMS, "missing 'arguments' object", _with_rid()
        )

    # ---- a2a_call_peer_async ---------------------------------------------
    if tool_name == TOOL_NAME_ASYNC:
        if not isinstance(args.get("to"), str) or not args["to"]:
            return json_rpc_error(
                rpc_id, ERR_INVALID_PARAMS, "missing 'to' (string)", _with_rid()
            )
        if not isinstance(args.get("message"), str) or not args["message"]:
            return json_rpc_error(
                rpc_id, ERR_INVALID_PARAMS, "missing 'message' (string)", _with_rid()
            )
        raw_thread = args.get("thread_id")
        thread_id = raw_thread if isinstance(raw_thread, str) and raw_thread else None

        if outbound_limiter is not None:
            key_fn = outbound_limit_key_fn if callable(outbound_limit_key_fn) else _default_outbound_key
            key = key_fn(thread_id=thread_id, self_name=self_name)
            decision = outbound_limiter.check(key)
            if not decision.allowed:
                return json_rpc_error(
                    rpc_id,
                    ERR_A2A_UPSTREAM,
                    f"self-origin rate limit exceeded ({decision.limit}/min)",
                    _with_rid({"httpStatus": 429, "retryAfterMs": decision.retry_after_ms}),
                )

        try:
            card = lookup_peer_fn(registry_url, args["to"], timeout)
        except UpstreamError as exc:
            return json_rpc_error(
                rpc_id, ERR_A2A_UPSTREAM, f"registry lookup failed: {exc}",
                _with_rid({"httpStatus": exc.http_status or 503}),
            )
        except Exception as exc:  # noqa: BLE001
            return json_rpc_error(
                rpc_id, ERR_A2A_UPSTREAM, f"registry lookup failed: {exc}",
                _with_rid({"httpStatus": 503}),
            )

        # Call peer's /a2a/send with mode=async. forward_to_peer_fn takes
        # positional args to match the existing documented signature; the
        # trailing ``mode="async"`` kwarg is accepted by the openclaw
        # implementation (outbound.forward_to_peer).
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
                "async",
            )
        except UpstreamError as exc:
            return json_rpc_error(
                rpc_id, ERR_A2A_UPSTREAM, f"peer call failed: {exc}",
                _with_rid({"httpStatus": exc.http_status or 502, "peerStatus": exc.peer_status}),
            )
        except Exception as exc:  # noqa: BLE001
            return json_rpc_error(
                rpc_id, ERR_A2A_UPSTREAM, f"peer call failed: {exc}",
                _with_rid({"httpStatus": 502}),
            )

        task_id_v = peer_resp.get("task_id") if isinstance(peer_resp, dict) else None
        state_v = peer_resp.get("state") if isinstance(peer_resp, dict) else None
        if not isinstance(task_id_v, str):
            return json_rpc_error(
                rpc_id, ERR_A2A_UPSTREAM,
                "peer did not return a task_id (async mode may not be enabled)",
                _with_rid({"httpStatus": 502}),
            )
        summary = (
            f"Task {task_id_v} dispatched to {args['to']} (state={state_v}). "
            f"Poll with a2a_get_task(peer=\"{args['to']}\", task_id=\"{task_id_v}\")."
        )
        return json_rpc_result(
            rpc_id,
            {
                "content": [{"type": "text", "text": summary}],
                "isError": False,
                "structuredContent": {
                    "peer": args["to"],
                    "task_id": task_id_v,
                    "state": state_v,
                    "thread_id": thread_id,
                    "request_id": request_id,
                },
            },
        )

    # ---- a2a_get_task / a2a_cancel_task ----------------------------------
    if tool_name in (TOOL_NAME_GET_TASK, TOOL_NAME_CANCEL_TASK):
        if not isinstance(args.get("peer"), str) or not args["peer"]:
            return json_rpc_error(
                rpc_id, ERR_INVALID_PARAMS, "missing 'peer' (string)", _with_rid()
            )
        if not isinstance(args.get("task_id"), str) or not args["task_id"]:
            return json_rpc_error(
                rpc_id, ERR_INVALID_PARAMS, "missing 'task_id' (string)", _with_rid()
            )
        try:
            card = lookup_peer_fn(registry_url, args["peer"], timeout)
        except UpstreamError as exc:
            return json_rpc_error(
                rpc_id, ERR_A2A_UPSTREAM, f"registry lookup failed: {exc}",
                _with_rid({"httpStatus": exc.http_status or 503}),
            )
        except Exception as exc:  # noqa: BLE001
            return json_rpc_error(
                rpc_id, ERR_A2A_UPSTREAM, f"registry lookup failed: {exc}",
                _with_rid({"httpStatus": 503}),
            )

        fn = get_task_fn if tool_name == TOOL_NAME_GET_TASK else cancel_task_fn
        if not callable(fn):
            return json_rpc_error(
                rpc_id, ERR_METHOD_NOT_FOUND, f"tool unavailable: {tool_name}", _with_rid()
            )
        try:
            snapshot = fn(card["endpoint"], args["task_id"], timeout, request_id)
        except UpstreamError as exc:
            return json_rpc_error(
                rpc_id, ERR_A2A_UPSTREAM, f"peer task call failed: {exc}",
                _with_rid({"httpStatus": exc.http_status or 502, "peerStatus": exc.peer_status}),
            )
        except Exception as exc:  # noqa: BLE001
            return json_rpc_error(
                rpc_id, ERR_A2A_UPSTREAM, f"peer task call failed: {exc}",
                _with_rid({"httpStatus": 502}),
            )
        state_v = snapshot.get("state") if isinstance(snapshot, dict) else None
        # Surface the terminal payload in the text channel. structuredContent
        # carries the full snapshot, but most LLM clients only consume
        # ``content`` — leaving the reply/error there would let a poll see
        # ``state=completed`` and still have no idea what the answer was.
        text_v = f"task {args['task_id']} state={state_v}"
        if tool_name == TOOL_NAME_GET_TASK and isinstance(snapshot, dict):
            if state_v == "completed":
                result_v = snapshot.get("result")
                reply_v = result_v.get("reply") if isinstance(result_v, dict) else None
                if isinstance(reply_v, str) and reply_v:
                    text_v = f"task {args['task_id']} state=completed\n\n{reply_v}"
            elif state_v == "failed":
                error_v = snapshot.get("error")
                err_msg = error_v.get("message") if isinstance(error_v, dict) else None
                if isinstance(err_msg, str) and err_msg:
                    text_v = f"task {args['task_id']} state=failed\nerror: {err_msg}"
            elif state_v in ("submitted", "working"):
                # Working/submitted tasks have no result yet, but the LLM
                # (and via it, the user) needs to know the worker is
                # alive. Surface elapsed time, last activity age, and the
                # most recent breadcrumb the peer emitted.
                now = datetime.now(timezone.utc)
                elapsed = _format_age(snapshot.get("created_at"), now=now)
                last_activity = _format_age(
                    snapshot.get("last_progress_at") or snapshot.get("updated_at"),
                    now=now,
                )
                note = snapshot.get("last_progress_message")
                parts = [f"task {args['task_id']} state={state_v}"]
                if elapsed:
                    parts.append(f"elapsed {elapsed}")
                if last_activity:
                    parts.append(f"last activity {last_activity} ago")
                head = ", ".join(parts)
                if isinstance(note, str) and note:
                    text_v = f'{head}\nnote: "{note}"'
                else:
                    text_v = head
        return json_rpc_result(
            rpc_id,
            {
                "content": [{"type": "text", "text": text_v}],
                "isError": False,
                "structuredContent": snapshot,
            },
        )

    return json_rpc_error(
        rpc_id, ERR_METHOD_NOT_FOUND, f"unknown async tool: {tool_name}", _with_rid()
    )


__all__ = ["handle_mcp_request"]
