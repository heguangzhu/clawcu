"""Tests for the adapter JSON-RPC bridge."""

import asyncio
import json


class _FakeRequest:
    def __init__(self, payload):
        self._payload = payload

    async def json(self):
        return self._payload


def test_handle_jsonrpc_message_send_calls_gateway(monkeypatch):
    from clawcu.a2a.adapter import server

    async def ready():
        return True

    async def call_gateway(text, auth_token):
        assert text == "ping"
        return "pong"

    monkeypatch.setattr(server, "_check_gateway_ready", ready)
    monkeypatch.setattr(server, "_call_gateway", call_gateway)

    response = asyncio.run(
        server.handle_jsonrpc(
            _FakeRequest(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "message/send",
                    "params": {
                        "message": {
                            "role": "user",
                            "parts": [{"type": "text", "text": "ping"}],
                        }
                    },
                }
            )
        )
    )
    payload = json.loads(response.body)

    assert payload["result"]["artifacts"] == [
        {"parts": [{"type": "text", "text": "pong"}]}
    ]


def test_handle_jsonrpc_send_message_accepts_v1_shape(monkeypatch):
    from clawcu.a2a.adapter import server

    async def ready():
        return True

    async def call_gateway(text, auth_token):
        assert text == "ping"
        return "pong"

    monkeypatch.setattr(server, "_check_gateway_ready", ready)
    monkeypatch.setattr(server, "_call_gateway", call_gateway)

    response = asyncio.run(
        server.handle_jsonrpc(
            _FakeRequest(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "SendMessage",
                    "params": {
                        "message": {
                            "role": "ROLE_USER",
                            "parts": [{"text": "ping"}],
                        }
                    },
                }
            )
        )
    )
    payload = json.loads(response.body)

    assert payload["id"] == 2
    assert payload["result"]["message"]["parts"] == [{"type": "text", "text": "pong"}]
