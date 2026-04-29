"""Tests for the adapter MCP bridge."""

import asyncio

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


def test_call_peer_uses_registry_and_jsonrpc(monkeypatch):
    from clawcu.a2a.adapter import mcp_bridge

    calls = []

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, url, **kwargs):
            calls.append(("GET", url, None))
            return _FakeResponse({"endpoint": "http://peer.local"})

        async def post(self, url, json):
            calls.append(("POST", url, json))
            return _FakeResponse(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "result": {
                        "id": "task-1",
                        "artifacts": [{"parts": [{"type": "text", "text": "pong"}]}],
                    },
                }
            )

    monkeypatch.setattr(mcp_bridge.httpx, "AsyncClient", FakeClient)
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
