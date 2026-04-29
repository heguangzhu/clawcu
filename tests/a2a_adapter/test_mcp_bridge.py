"""Tests for the adapter MCP bridge."""

import asyncio
import json

import pytest

pytest.importorskip("httpx", reason="httpx not installed")


class _FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            import httpx

            raise httpx.HTTPStatusError("boom", request=None, response=self)


class _FakeRequest:
    def __init__(self, payload):
        self._payload = payload

    async def json(self):
        return self._payload


class _FakeClient:
    def __init__(self, calls, *args, **kwargs):
        self.calls = calls

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def get(self, url, **kwargs):
        self.calls.append(("GET", url, None))
        if url.endswith("/agents"):
            return _FakeResponse(
                [
                    {
                        "name": "writer",
                        "role": "Writer agent",
                        "skills": ["chat", "drafting"],
                        "endpoint": "http://writer.local",
                        "protocol": ["a2a/v0.1"],
                    },
                    {
                        "name": "analyst",
                        "role": "Analyst agent",
                        "skills": [{"name": "chat", "tags": ["analysis"]}],
                        "supported_interfaces": [
                            {"url": "http://peer.local", "protocol_version": "0.1"}
                        ],
                    },
                ]
            )
        if "/tasks/" in url:
            return _FakeResponse(
                {"id": url.rsplit("/", 1)[-1], "status": {"state": "working"}}
            )
        return _FakeResponse(
            {
                "name": "analyst",
                "description": "Analyst agent",
                "supported_interfaces": [
                    {"url": "http://peer.local", "protocol_version": "0.1"}
                ],
                "skills": [{"name": "chat", "tags": ["analysis"]}],
            }
        )

    async def post(self, url, json):
        self.calls.append(("POST", url, json))
        if url.endswith("/cancel"):
            return _FakeResponse(
                {
                    "id": url.rstrip("/").rsplit("/", 2)[-2],
                    "status": {"state": "canceled"},
                }
            )
        return _FakeResponse(
            {
                "jsonrpc": "2.0",
                "id": json.get("id"),
                "result": {
                    "id": "task-1",
                    "artifacts": [{"parts": [{"type": "text", "text": "pong"}]}],
                },
            }
        )


def test_call_peer_uses_registry_and_jsonrpc(monkeypatch):
    from clawcu.a2a.adapter import mcp_bridge

    calls = []

    monkeypatch.setattr(
        mcp_bridge.httpx,
        "AsyncClient",
        lambda *a, **kw: _FakeClient(calls, *a, **kw),
    )
    monkeypatch.setenv("A2A_AGENT_NAME", "writer")

    result = asyncio.run(
        mcp_bridge._call_peer(
            {"to": "analyst", "message": "ping", "registry_url": "http://registry"}
        )
    )

    assert result["reply"] == "pong"
    assert result["caller"] == "writer"
    assert calls[0] == ("GET", "http://registry/agents/analyst", None)
    assert calls[1][0:2] == ("POST", "http://peer.local")
    assert calls[1][2]["method"] == "message/send"
    assert calls[1][2]["params"]["configuration"] == {"blocking": True}


def test_call_peer_async_requests_nonblocking_jsonrpc(monkeypatch):
    from clawcu.a2a.adapter import mcp_bridge

    calls = []

    monkeypatch.setattr(
        mcp_bridge.httpx,
        "AsyncClient",
        lambda *a, **kw: _FakeClient(calls, *a, **kw),
    )
    monkeypatch.setenv("A2A_AGENT_NAME", "writer")

    result = asyncio.run(
        mcp_bridge._call_peer_async(
            {"to": "analyst", "message": "ping", "registry_url": "http://registry"}
        )
    )

    assert result["caller"] == "writer"
    assert result["task_id"] == "task-1"
    assert calls[0] == ("GET", "http://registry/agents/analyst", None)
    assert calls[1][0:2] == ("POST", "http://peer.local")
    assert calls[1][2]["method"] == "message/send"
    assert calls[1][2]["params"]["configuration"] == {"blocking": False}


def test_peer_task_endpoints_derive_from_jsonrpc_endpoint(monkeypatch):
    from clawcu.a2a.adapter import mcp_bridge

    calls = []

    monkeypatch.setattr(
        mcp_bridge.httpx,
        "AsyncClient",
        lambda *a, **kw: _FakeClient(calls, *a, **kw),
    )

    get_result = asyncio.run(
        mcp_bridge._get_task(
            {"to": "analyst", "task_id": "task 1", "registry_url": "http://registry"}
        )
    )
    cancel_result = asyncio.run(
        mcp_bridge._cancel_task(
            {"to": "analyst", "task_id": "task 1", "registry_url": "http://registry"}
        )
    )

    assert get_result["task"]["status"]["state"] == "working"
    assert cancel_result["task"]["status"]["state"] == "canceled"
    assert calls[1] == ("GET", "http://peer.local/tasks/task%201", None)
    assert calls[3] == ("POST", "http://peer.local/tasks/task%201/cancel", {})


def test_list_peers_uses_registry(monkeypatch):
    from clawcu.a2a.adapter import mcp_bridge

    calls = []
    monkeypatch.setattr(
        mcp_bridge.httpx,
        "AsyncClient",
        lambda *a, **kw: _FakeClient(calls, *a, **kw),
    )

    result = asyncio.run(mcp_bridge._list_peers({"registry_url": "http://registry"}))

    assert result["registry_url"] == "http://registry"
    assert result["peers"][0]["name"] == "writer"
    assert result["peers"][1]["name"] == "analyst"
    assert result["peers"][1]["endpoint"] == "http://peer.local"
    assert result["peers"][1]["skills"] == ["analysis", "chat"]
    assert calls == [("GET", "http://registry/agents", None)]


def test_jsonrpc_endpoint_rewrites_legacy_openclaw_send_endpoint():
    from clawcu.a2a.adapter import mcp_bridge

    endpoint = mcp_bridge._jsonrpc_endpoint(
        {
            "name": "analyst",
            "role": "OpenClaw local assistant",
            "endpoint": "http://host.docker.internal:19629/a2a/send",
        }
    )

    assert endpoint == "http://host.docker.internal:19630"


def test_jsonrpc_endpoint_removes_legacy_non_openclaw_send_path():
    from clawcu.a2a.adapter import mcp_bridge

    endpoint = mcp_bridge._jsonrpc_endpoint(
        {
            "name": "javis",
            "role": "Hermes local analyst",
            "endpoint": "http://host.docker.internal:9129/a2a/send",
        }
    )

    assert endpoint == "http://host.docker.internal:9129"


def test_mcp_tools_list_exposes_async_tools_by_default(monkeypatch):
    from clawcu.a2a.adapter import mcp_bridge

    monkeypatch.delenv("A2A_ASYNC_ENABLED", raising=False)
    request = _FakeRequest(
        {"jsonrpc": "2.0", "id": 6, "method": "tools/list", "params": {}}
    )

    response = asyncio.run(mcp_bridge.handle_mcp(request))
    payload = json.loads(response.body)
    tool_names = [tool["name"] for tool in payload["result"]["tools"]]

    assert tool_names == [
        "a2a_call_peer",
        "a2a_call_peer_async",
        "a2a_get_task",
        "a2a_cancel_task",
        "a2a_list_peers",
    ]


def test_mcp_tools_list_hides_async_tools_when_disabled(monkeypatch):
    from clawcu.a2a.adapter import mcp_bridge

    monkeypatch.setenv("A2A_ASYNC_ENABLED", "false")
    request = _FakeRequest(
        {"jsonrpc": "2.0", "id": 6, "method": "tools/list", "params": {}}
    )

    response = asyncio.run(mcp_bridge.handle_mcp(request))
    payload = json.loads(response.body)
    tool_names = [tool["name"] for tool in payload["result"]["tools"]]

    assert tool_names == [
        "a2a_call_peer",
        "a2a_list_peers",
    ]


def test_mcp_tools_list_exposes_async_tools_when_enabled(monkeypatch):
    from clawcu.a2a.adapter import mcp_bridge

    monkeypatch.setenv("A2A_ASYNC_ENABLED", "true")
    request = _FakeRequest(
        {"jsonrpc": "2.0", "id": 6, "method": "tools/list", "params": {}}
    )

    response = asyncio.run(mcp_bridge.handle_mcp(request))
    payload = json.loads(response.body)
    tool_names = [tool["name"] for tool in payload["result"]["tools"]]

    assert tool_names == [
        "a2a_call_peer",
        "a2a_call_peer_async",
        "a2a_get_task",
        "a2a_cancel_task",
        "a2a_list_peers",
    ]


def test_send_timeout_clamps_low_tool_argument_to_24h(monkeypatch):
    from clawcu.a2a.adapter import mcp_bridge

    monkeypatch.setenv("A2A_SEND_TIMEOUT", "86400")

    assert mcp_bridge._send_timeout({"timeout_seconds": 90}) == 86400


def test_mcp_tools_call_routes_agent_to_peer(monkeypatch):
    from clawcu.a2a.adapter import mcp_bridge

    calls = []
    monkeypatch.setattr(
        mcp_bridge.httpx,
        "AsyncClient",
        lambda *a, **kw: _FakeClient(calls, *a, **kw),
    )
    monkeypatch.setenv("A2A_AGENT_NAME", "writer")
    request = _FakeRequest(
        {
            "jsonrpc": "2.0",
            "id": 7,
            "method": "tools/call",
            "params": {
                "name": "a2a_call_peer",
                "arguments": {
                    "to": "analyst",
                    "message": "ping",
                    "registry_url": "http://registry",
                },
            },
        }
    )

    response = asyncio.run(mcp_bridge.handle_mcp(request))
    payload = json.loads(response.body)

    assert payload["id"] == 7
    assert payload["result"]["content"] == [{"type": "text", "text": "pong"}]
    structured = payload["result"]["structuredContent"]
    assert structured["caller"] == "writer"
    assert structured["from"] == "analyst"
    assert calls[0] == ("GET", "http://registry/agents/analyst", None)
    assert calls[1][0:2] == ("POST", "http://peer.local")
    assert calls[1][2]["params"]["configuration"] == {"blocking": True}


def test_mcp_tools_call_routes_async_peer_call(monkeypatch):
    from clawcu.a2a.adapter import mcp_bridge

    calls = []
    monkeypatch.setattr(
        mcp_bridge.httpx,
        "AsyncClient",
        lambda *a, **kw: _FakeClient(calls, *a, **kw),
    )
    monkeypatch.setenv("A2A_ASYNC_ENABLED", "true")
    request = _FakeRequest(
        {
            "jsonrpc": "2.0",
            "id": 9,
            "method": "tools/call",
            "params": {
                "name": "a2a_call_peer_async",
                "arguments": {
                    "to": "analyst",
                    "message": "ping",
                    "registry_url": "http://registry",
                },
            },
        }
    )

    response = asyncio.run(mcp_bridge.handle_mcp(request))
    payload = json.loads(response.body)

    assert payload["id"] == 9
    assert payload["result"]["content"] == [
        {"type": "text", "text": "Submitted task task-1"}
    ]
    assert payload["result"]["structuredContent"]["task_id"] == "task-1"
    assert calls[1][2]["params"]["configuration"] == {"blocking": False}


def test_mcp_tools_call_routes_task_tools(monkeypatch):
    from clawcu.a2a.adapter import mcp_bridge

    calls = []
    monkeypatch.setattr(
        mcp_bridge.httpx,
        "AsyncClient",
        lambda *a, **kw: _FakeClient(calls, *a, **kw),
    )
    monkeypatch.setenv("A2A_ASYNC_ENABLED", "true")
    get_request = _FakeRequest(
        {
            "jsonrpc": "2.0",
            "id": 10,
            "method": "tools/call",
            "params": {
                "name": "a2a_get_task",
                "arguments": {
                    "to": "analyst",
                    "task_id": "task-1",
                    "registry_url": "http://registry",
                },
            },
        }
    )
    cancel_request = _FakeRequest(
        {
            "jsonrpc": "2.0",
            "id": 11,
            "method": "tools/call",
            "params": {
                "name": "a2a_cancel_task",
                "arguments": {
                    "to": "analyst",
                    "task_id": "task-1",
                    "registry_url": "http://registry",
                },
            },
        }
    )

    get_response = asyncio.run(mcp_bridge.handle_mcp(get_request))
    cancel_response = asyncio.run(mcp_bridge.handle_mcp(cancel_request))
    get_payload = json.loads(get_response.body)
    cancel_payload = json.loads(cancel_response.body)

    assert get_payload["result"]["content"] == [
        {"type": "text", "text": "Task task-1 is working"}
    ]
    assert cancel_payload["result"]["content"] == [
        {"type": "text", "text": "Task task-1 is canceled"}
    ]
    assert calls[1] == ("GET", "http://peer.local/tasks/task-1", None)
    assert calls[3] == ("POST", "http://peer.local/tasks/task-1/cancel", {})


def test_mcp_tools_call_lists_peers(monkeypatch):
    from clawcu.a2a.adapter import mcp_bridge

    calls = []
    monkeypatch.setattr(
        mcp_bridge.httpx,
        "AsyncClient",
        lambda *a, **kw: _FakeClient(calls, *a, **kw),
    )
    request = _FakeRequest(
        {
            "jsonrpc": "2.0",
            "id": 8,
            "method": "tools/call",
            "params": {
                "name": "a2a_list_peers",
                "arguments": {"registry_url": "http://registry"},
            },
        }
    )

    response = asyncio.run(mcp_bridge.handle_mcp(request))
    payload = json.loads(response.body)

    assert payload["id"] == 8
    assert "writer" in payload["result"]["content"][0]["text"]
    assert "analyst" in payload["result"]["content"][0]["text"]
    assert payload["result"]["structuredContent"]["peers"][1]["name"] == "analyst"
    assert calls == [("GET", "http://registry/agents", None)]


def test_mcp_tools_call_uses_exception_type_for_empty_error(monkeypatch):
    from clawcu.a2a.adapter import mcp_bridge

    async def fail(_arguments):
        raise TimeoutError()

    monkeypatch.setattr(mcp_bridge, "_call_peer", fail)
    request = _FakeRequest(
        {
            "jsonrpc": "2.0",
            "id": 8,
            "method": "tools/call",
            "params": {
                "name": "a2a_call_peer",
                "arguments": {"to": "analyst", "message": "ping"},
            },
        }
    )

    response = asyncio.run(mcp_bridge.handle_mcp(request))
    payload = json.loads(response.body)

    assert payload["id"] == 8
    assert payload["error"]["message"] == "TimeoutError"
