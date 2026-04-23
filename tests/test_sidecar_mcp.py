"""Tests for the shared MCP JSON-RPC dispatcher in ``_common/mcp.py``.

Originally a port of the Node-side OpenClaw-specific ``sidecar_mcp.test.js``;
now exercises the unified dispatcher shared with Hermes. Uses the kwargs
signature directly so both runtimes are covered by the same contract.
"""
from __future__ import annotations

import os
import sys

import pytest

_SIDECAR_PLUGIN_ROOT = os.path.abspath(
    os.path.join(
        os.path.dirname(__file__),
        "..",
        "src",
        "clawcu",
        "a2a",
        "sidecar_plugin",
    )
)
if _SIDECAR_PLUGIN_ROOT not in sys.path:
    sys.path.insert(0, _SIDECAR_PLUGIN_ROOT)

from _common.mcp import (  # noqa: E402
    ERR_A2A_UPSTREAM,
    ERR_INVALID_PARAMS,
    ERR_INVALID_REQUEST,
    ERR_METHOD_NOT_FOUND,
    MCP_PROTOCOL_VERSION,
    UpstreamError,
    handle_mcp_request,
)


def base_kwargs(**overrides):
    """Default kwargs for ``handle_mcp_request``; positional-style stubs."""

    def default_lookup(_reg, _name, _t):
        return {"name": "analyst", "endpoint": "http://127.0.0.1:9129/a2a/send"}

    def default_forward(_ep, _self, _peer, _msg, _thread, _hop, _t, _rid):
        return {"from": "analyst", "reply": "42", "thread_id": None}

    kwargs = {
        "self_name": "writer",
        "registry_url": "http://127.0.0.1:9100",
        "timeout": 2000,
        "request_id": "req-1",
        "plugin_version": "0.3.3.testsha",
        "lookup_peer_fn": default_lookup,
        "forward_to_peer_fn": default_forward,
    }
    kwargs.update(overrides)
    return kwargs


# -- initialize --------------------------------------------------------------


def test_initialize_returns_protocol_version_and_server_info():
    res = handle_mcp_request(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize"}, **base_kwargs()
    )
    assert res["jsonrpc"] == "2.0"
    assert res["id"] == 1
    assert res["result"]["protocolVersion"] == MCP_PROTOCOL_VERSION
    assert res["result"]["serverInfo"]["name"] == "clawcu-a2a"
    assert res["result"]["serverInfo"]["version"] == "0.3.3.testsha"
    assert res["result"]["capabilities"]["tools"] == {}


# -- tools/list --------------------------------------------------------------


def test_tools_list_exposes_a2a_call_peer_only():
    res = handle_mcp_request(
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, **base_kwargs()
    )
    tools = res["result"]["tools"]
    assert len(tools) == 1
    tool = tools[0]
    assert tool["name"] == "a2a_call_peer"
    assert tool["inputSchema"]["type"] == "object"
    assert tool["inputSchema"]["required"] == ["to", "message"]
    assert "thread_id" in tool["inputSchema"]["properties"]


# -- tools/call happy path ---------------------------------------------------


def test_tools_call_forwards_and_returns_text_content():
    seen = {"lookup": None, "forward": None}

    def lookup(registry_url, peer_name, timeout):
        seen["lookup"] = {
            "registry_url": registry_url,
            "peer_name": peer_name,
            "timeout": timeout,
        }
        return {"name": "analyst", "endpoint": "http://127.0.0.1:9129/a2a/send"}

    def forward(endpoint, self_name, peer_name, message, thread_id, hop, timeout, request_id):
        seen["forward"] = {
            "endpoint": endpoint,
            "self_name": self_name,
            "peer_name": peer_name,
            "message": message,
            "thread_id": thread_id,
            "hop": hop,
            "timeout": timeout,
            "request_id": request_id,
        }
        return {"from": "analyst", "reply": "Q1 was up 18%", "thread_id": "t-1"}

    res = handle_mcp_request(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "a2a_call_peer",
                "arguments": {
                    "to": "analyst",
                    "message": "Q1 revenue?",
                    "thread_id": "t-1",
                },
            },
        },
        **base_kwargs(lookup_peer_fn=lookup, forward_to_peer_fn=forward),
    )
    assert "error" not in res
    assert res["result"]["isError"] is False
    assert res["result"]["content"] == [{"type": "text", "text": "Q1 was up 18%"}]
    sc = res["result"]["structuredContent"]
    assert sc["to"] == "analyst"
    assert sc["reply"] == "Q1 was up 18%"
    assert sc["thread_id"] == "t-1"
    assert sc["request_id"] == "req-1"
    assert seen["lookup"]["peer_name"] == "analyst"
    assert seen["lookup"]["registry_url"] == "http://127.0.0.1:9100"
    assert seen["forward"]["endpoint"] == "http://127.0.0.1:9129/a2a/send"
    assert seen["forward"]["thread_id"] == "t-1"
    assert seen["forward"]["hop"] == 1
    assert seen["forward"]["request_id"] == "req-1"


def test_tools_call_without_thread_id_passes_none():
    seen_thread = {"v": "uninitialized"}

    def forward(_ep, _self, _peer, _msg, thread_id, _hop, _t, _rid):
        seen_thread["v"] = thread_id
        return {"from": "analyst", "reply": "ok", "thread_id": None}

    res = handle_mcp_request(
        {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {"name": "a2a_call_peer", "arguments": {"to": "p", "message": "m"}},
        },
        **base_kwargs(forward_to_peer_fn=forward),
    )
    assert seen_thread["v"] is None
    assert res["result"]["structuredContent"]["thread_id"] is None


# -- tools/call error paths --------------------------------------------------


def test_tools_call_missing_to_invalid_params():
    res = handle_mcp_request(
        {
            "jsonrpc": "2.0",
            "id": 5,
            "method": "tools/call",
            "params": {"name": "a2a_call_peer", "arguments": {"message": "hi"}},
        },
        **base_kwargs(),
    )
    assert res["error"]["code"] == ERR_INVALID_PARAMS
    assert "missing 'to'" in res["error"]["message"]


def test_tools_call_missing_message_invalid_params():
    res = handle_mcp_request(
        {
            "jsonrpc": "2.0",
            "id": 6,
            "method": "tools/call",
            "params": {"name": "a2a_call_peer", "arguments": {"to": "analyst"}},
        },
        **base_kwargs(),
    )
    assert res["error"]["code"] == ERR_INVALID_PARAMS


def test_tools_call_unknown_tool_name_method_not_found():
    res = handle_mcp_request(
        {
            "jsonrpc": "2.0",
            "id": 7,
            "method": "tools/call",
            "params": {"name": "nonsense", "arguments": {}},
        },
        **base_kwargs(),
    )
    assert res["error"]["code"] == ERR_METHOD_NOT_FOUND


def test_tools_call_registry_lookup_failure_with_http_status_404():
    def lookup(_reg, _name, _t):
        raise UpstreamError("peer 'analyst' not found in registry", http_status=404)

    res = handle_mcp_request(
        {
            "jsonrpc": "2.0",
            "id": 8,
            "method": "tools/call",
            "params": {
                "name": "a2a_call_peer",
                "arguments": {"to": "analyst", "message": "hi"},
            },
        },
        **base_kwargs(lookup_peer_fn=lookup),
    )
    assert res["error"]["code"] == ERR_A2A_UPSTREAM
    assert res["error"]["data"]["httpStatus"] == 404
    assert "registry lookup failed" in res["error"]["message"]


def test_tools_call_peer_forward_failure_surfaces_http_status_and_peer_status():
    def forward(*_args, **_kwargs):
        raise UpstreamError("peer HTTP 500", http_status=502, peer_status=500)

    res = handle_mcp_request(
        {
            "jsonrpc": "2.0",
            "id": 9,
            "method": "tools/call",
            "params": {
                "name": "a2a_call_peer",
                "arguments": {"to": "analyst", "message": "hi"},
            },
        },
        **base_kwargs(forward_to_peer_fn=forward),
    )
    assert res["error"]["code"] == ERR_A2A_UPSTREAM
    assert res["error"]["data"]["httpStatus"] == 502
    assert res["error"]["data"]["peerStatus"] == 500


# -- top-level dispatch ------------------------------------------------------


def test_unknown_method_is_method_not_found():
    res = handle_mcp_request(
        {"jsonrpc": "2.0", "id": 10, "method": "resources/list"}, **base_kwargs()
    )
    assert res["error"]["code"] == ERR_METHOD_NOT_FOUND


def test_non_jsonrpc_is_invalid_request():
    res = handle_mcp_request({"method": "initialize"}, **base_kwargs())
    assert res["error"]["code"] == ERR_INVALID_REQUEST


def test_ping_is_acknowledged_as_empty_result():
    res = handle_mcp_request(
        {"jsonrpc": "2.0", "id": 11, "method": "ping"}, **base_kwargs()
    )
    assert "error" not in res
    assert res["result"] == {}


def test_notifications_initialized_acknowledged():
    res = handle_mcp_request(
        {"jsonrpc": "2.0", "id": None, "method": "notifications/initialized"},
        **base_kwargs(),
    )
    assert "error" not in res


def test_null_id_preserved_in_error_response():
    res = handle_mcp_request({"jsonrpc": "1.0"}, **base_kwargs())
    assert res["id"] is None
    assert res["error"]["code"] == ERR_INVALID_REQUEST


# -- self-origin outbound rate limit ----------------------------------------


def test_tools_call_rate_limits_after_rpm_returns_429_and_retry_after_ms():
    from _common.outbound_limit import create_outbound_limiter, key_for

    limiter = create_outbound_limiter(rpm=2)

    def call_once():
        return handle_mcp_request(
            {
                "jsonrpc": "2.0",
                "id": 99,
                "method": "tools/call",
                "params": {
                    "name": "a2a_call_peer",
                    "arguments": {
                        "to": "analyst",
                        "message": "hi",
                        "thread_id": "t-lim",
                    },
                },
            },
            **base_kwargs(outbound_limiter=limiter, outbound_limit_key_fn=key_for),
        )

    r1 = call_once()
    r2 = call_once()
    r3 = call_once()
    assert "result" in r1
    assert "result" in r2
    assert "error" in r3
    assert r3["error"]["code"] == ERR_A2A_UPSTREAM
    assert r3["error"]["data"]["httpStatus"] == 429
    assert r3["error"]["data"]["retryAfterMs"] > 0


def test_tools_call_without_limiter_kwargs_is_permissive():
    res = handle_mcp_request(
        {
            "jsonrpc": "2.0",
            "id": 100,
            "method": "tools/call",
            "params": {
                "name": "a2a_call_peer",
                "arguments": {"to": "analyst", "message": "hi"},
            },
        },
        **base_kwargs(),
    )
    assert "result" in res


# -- templated tool description ---------------------------------------------


def test_tools_list_without_list_peers_keeps_static_description():
    res = handle_mcp_request(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, **base_kwargs()
    )
    desc = res["result"]["tools"][0]["description"]
    assert "Available peers" not in desc
    assert "registered in the A2A registry" in desc


def test_tools_list_with_list_peers_injects_summary_and_excludes_self():
    peers = [
        {"name": "writer", "role": "author", "skills": ["prose"]},
        {"name": "analyst", "role": "analyst", "skills": ["market data", "charts"]},
        {"name": "editor", "role": "editor", "skills": ["copyedit"]},
    ]
    res = handle_mcp_request(
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        **base_kwargs(list_peers_fn=lambda: peers),
    )
    desc = res["result"]["tools"][0]["description"]
    assert "Available peers:" in desc
    assert "- analyst (market data, charts)" in desc
    assert "- editor (copyedit)" in desc
    assert "- writer" not in desc


def test_tools_list_truncates_long_peer_list():
    peers = [{"name": f"peer-{i}", "skills": [f"s{i}"]} for i in range(20)]
    res = handle_mcp_request(
        {"jsonrpc": "2.0", "id": 3, "method": "tools/list"},
        **base_kwargs(list_peers_fn=lambda: peers),
    )
    desc = res["result"]["tools"][0]["description"]
    assert "- peer-0 " in desc
    assert "- peer-15 " in desc
    assert "...and 4 more" in desc
    assert "- peer-16 " not in desc


def test_tools_list_survives_list_peers_throwing():
    def boom():
        raise RuntimeError("registry unreachable")

    res = handle_mcp_request(
        {"jsonrpc": "2.0", "id": 4, "method": "tools/list"},
        **base_kwargs(list_peers_fn=boom),
    )
    assert "result" in res
    assert "Available peers" not in res["result"]["tools"][0]["description"]


def test_tools_list_skills_over_3_are_elided():
    peers = [{"name": "polymath", "skills": ["a", "b", "c", "d", "e"]}]
    res = handle_mcp_request(
        {"jsonrpc": "2.0", "id": 5, "method": "tools/list"},
        **base_kwargs(list_peers_fn=lambda: peers),
    )
    desc = res["result"]["tools"][0]["description"]
    assert "- polymath (a, b, c, ...)" in desc


# -- optional role in peer summary ------------------------------------------


def test_tools_list_omits_role_by_default():
    peers = [{"name": "analyst", "role": "senior market analyst", "skills": ["market data"]}]
    res = handle_mcp_request(
        {"jsonrpc": "2.0", "id": 20, "method": "tools/list"},
        **base_kwargs(list_peers_fn=lambda: peers),
    )
    desc = res["result"]["tools"][0]["description"]
    assert "- analyst (market data)" in desc
    assert "[senior market analyst]" not in desc


def test_tools_list_renders_role_when_include_role_true():
    peers = [{"name": "analyst", "role": "senior market analyst", "skills": ["market data"]}]
    res = handle_mcp_request(
        {"jsonrpc": "2.0", "id": 21, "method": "tools/list"},
        **base_kwargs(list_peers_fn=lambda: peers, include_role=True),
    )
    desc = res["result"]["tools"][0]["description"]
    assert "- analyst [senior market analyst] (market data)" in desc


def test_tools_list_include_role_empty_role_drops_brackets():
    peers = [{"name": "analyst", "role": "", "skills": ["market data"]}]
    res = handle_mcp_request(
        {"jsonrpc": "2.0", "id": 22, "method": "tools/list"},
        **base_kwargs(list_peers_fn=lambda: peers, include_role=True),
    )
    desc = res["result"]["tools"][0]["description"]
    assert "- analyst (market data)" in desc
    assert "[]" not in desc


def test_tools_list_with_only_self_renders_static():
    res = handle_mcp_request(
        {"jsonrpc": "2.0", "id": 6, "method": "tools/list"},
        **base_kwargs(list_peers_fn=lambda: [{"name": "writer", "skills": []}]),
    )
    assert "Available peers" not in res["result"]["tools"][0]["description"]


# -- P2-K: request_id on MCP error data -------------------------------------


def test_tool_call_errors_carry_request_id_in_data_registry_fail():
    def lookup(_reg, _name, _t):
        raise UpstreamError("peer 'ghost' not found", http_status=404)

    res = handle_mcp_request(
        {
            "jsonrpc": "2.0",
            "id": 7,
            "method": "tools/call",
            "params": {
                "name": "a2a_call_peer",
                "arguments": {"to": "ghost", "message": "hi"},
            },
        },
        **base_kwargs(request_id="rid-7", lookup_peer_fn=lookup),
    )
    assert res["error"]["code"] == ERR_A2A_UPSTREAM
    assert res["error"]["data"]["httpStatus"] == 404
    assert res["error"]["data"]["requestId"] == "rid-7"


def test_invalid_params_errors_also_carry_request_id():
    res = handle_mcp_request(
        {
            "jsonrpc": "2.0",
            "id": 8,
            "method": "tools/call",
            "params": {"name": "a2a_call_peer", "arguments": {"message": "hi"}},
        },
        **base_kwargs(request_id="rid-8"),
    )
    assert res["error"]["code"] == ERR_INVALID_PARAMS
    assert res["error"]["data"]["requestId"] == "rid-8"
