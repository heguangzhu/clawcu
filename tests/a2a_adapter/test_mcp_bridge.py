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
