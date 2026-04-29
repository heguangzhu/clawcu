"""Tests for the adapter JSON-RPC bridge."""

import asyncio
import json


class _FakeRequest:
    def __init__(self, payload):
        self._payload = payload

    async def json(self):
        return self._payload


class _FakeTaskRequest:
    def __init__(self, task_id):
        self.path_params = {"task_id": task_id}
        self.headers = {}


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


def test_handle_jsonrpc_message_send_can_enqueue_async(monkeypatch):
    from clawcu.a2a.adapter import server

    async def enqueue(text, rpc_id, params):
        assert text == "ping"
        assert rpc_id == 3
        assert params["configuration"] == {"blocking": False}
        return {
            "task_id": "task_123",
            "state": "submitted",
            "request_id": "3",
            "result": None,
        }

    monkeypatch.setattr(server, "_enqueue_async_task", enqueue)

    response = asyncio.run(
        server.handle_jsonrpc(
            _FakeRequest(
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "message/send",
                    "params": {
                        "message": {
                            "role": "user",
                            "parts": [{"type": "text", "text": "ping"}],
                        },
                        "configuration": {"blocking": False},
                    },
                }
            )
        )
    )
    payload = json.loads(response.body)

    assert payload["id"] == 3
    assert payload["result"]["id"] == "task_123"
    assert payload["result"]["status"] == {"state": "submitted"}
    assert payload["result"]["metadata"]["task_id"] == "task_123"


def test_handle_jsonrpc_async_disabled_returns_error(monkeypatch):
    from clawcu.a2a.adapter import server

    monkeypatch.setenv("A2A_ASYNC_ENABLED", "false")

    response = asyncio.run(
        server.handle_jsonrpc(
            _FakeRequest(
                {
                    "jsonrpc": "2.0",
                    "id": 4,
                    "method": "message/send",
                    "params": {
                        "message": {
                            "role": "user",
                            "parts": [{"type": "text", "text": "ping"}],
                        },
                        "configuration": {"blocking": False},
                    },
                }
            )
        )
    )
    payload = json.loads(response.body)

    assert payload["id"] == 4
    assert payload["error"]["code"] == -32000
    assert "set it to true" in payload["error"]["message"]


def test_task_get_returns_task_result(monkeypatch):
    from clawcu.a2a.adapter import server

    class Store:
        async def get(self, task_id):
            assert task_id == "task_123"
            return {
                "task_id": "task_123",
                "state": "completed",
                "request_id": "rid",
                "result": {"reply": "done"},
            }

    async def task_store():
        return Store()

    monkeypatch.setattr(server, "_task_store", task_store)

    response = asyncio.run(server.handle_task_get(_FakeTaskRequest("task_123")))
    payload = json.loads(response.body)

    assert payload["id"] == "task_123"
    assert payload["status"] == {"state": "completed"}
    assert payload["artifacts"] == [{"parts": [{"type": "text", "text": "done"}]}]


def test_task_get_returns_worker_a2a_result_shape(monkeypatch):
    from clawcu.a2a.adapter import server

    class Store:
        async def get(self, task_id):
            assert task_id == "task_123"
            return {
                "task_id": "task_123",
                "state": "completed",
                "request_id": "rid",
                "result": {
                    "reply": "done",
                    "artifacts": [
                        {"parts": [{"type": "text", "text": "done"}]},
                    ],
                    "message": {
                        "role": "agent",
                        "parts": [{"type": "text", "text": "done"}],
                    },
                },
            }

    async def task_store():
        return Store()

    monkeypatch.setattr(server, "_task_store", task_store)

    response = asyncio.run(server.handle_task_get(_FakeTaskRequest("task_123")))
    payload = json.loads(response.body)

    assert payload["artifacts"] == [{"parts": [{"type": "text", "text": "done"}]}]
    assert payload["message"] == {
        "role": "agent",
        "parts": [{"type": "text", "text": "done"}],
    }


def test_task_cancel_marks_task_canceled(monkeypatch):
    from clawcu.a2a.adapter import server

    class Store:
        redis = object()

        async def request_cancel(self, task_id):
            assert task_id == "task_123"
            return {
                "task_id": "task_123",
                "state": "canceled",
                "request_id": "rid",
                "result": None,
            }

    async def task_store():
        return Store()

    monkeypatch.setattr(server, "_task_store", task_store)
    monkeypatch.setattr(server.log, "debug", lambda *args, **kwargs: None)

    response = asyncio.run(server.handle_task_cancel(_FakeTaskRequest("task_123")))
    payload = json.loads(response.body)

    assert payload["id"] == "task_123"
    assert payload["status"] == {"state": "canceled"}


def test_task_events_replays_events_and_closes_on_terminal(monkeypatch):
    from clawcu.a2a.adapter import server

    class Store:
        async def get(self, task_id):
            assert task_id == "task_123"
            return {"task_id": "task_123", "state": "completed", "result": None}

        async def read_events(self, task_id, *, after_id="0-0"):
            assert task_id == "task_123"
            assert after_id == "0-0"
            return [
                {"_id": "1-0", "event": "submitted", "state": "submitted"},
                {"_id": "2-0", "event": "completed", "state": "completed"},
            ]

    async def task_store():
        return Store()

    monkeypatch.setattr(server, "_task_store", task_store)

    response = asyncio.run(server.handle_task_events(_FakeTaskRequest("task_123")))

    async def collect():
        events = []
        async for event in response.body_iterator:
            events.append(event)
        return events

    events = asyncio.run(collect())

    assert [event["event"] for event in events] == ["submitted", "completed", "end"]


def test_task_events_emits_heartbeat_for_nonterminal_task(monkeypatch):
    from clawcu.a2a.adapter import server

    class Store:
        async def get(self, task_id):
            assert task_id == "task_123"
            return {"task_id": "task_123", "state": "working", "result": None}

        async def read_events(self, task_id, *, after_id="0-0"):
            assert task_id == "task_123"
            return []

    async def task_store():
        return Store()

    monkeypatch.setenv("A2A_TASK_PROGRESS_INTERVAL_S", "1")
    monkeypatch.setenv("A2A_TASK_EVENTS_IDLE_TIMEOUT_S", "3")
    monkeypatch.setattr(server, "_task_store", task_store)

    response = asyncio.run(server.handle_task_events(_FakeTaskRequest("task_123")))

    async def first_event():
        return await asyncio.wait_for(anext(response.body_iterator), timeout=2.0)

    event = asyncio.run(first_event())

    assert event["event"] == "heartbeat"
