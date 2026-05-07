"""Tests for clawcu.a2a.adapter.worker."""

from __future__ import annotations

import asyncio

import pytest


class FakeRedis:
    def __init__(self):
        self.values = {}
        self.expiries = {}
        self.streams = {}
        self.sets = {}
        self.closed = False
        self._stream_id = 0

    async def set(self, key, value, ex=None):
        self.values[key] = value
        if ex is not None:
            self.expiries[key] = ex

    async def get(self, key):
        return self.values.get(key)

    async def xadd(self, key, fields):
        self._stream_id += 1
        event_id = f"{self._stream_id}-0"
        self.streams.setdefault(key, []).append((event_id, dict(fields)))
        return event_id

    async def xrange(self, key, min="-"):  # noqa: A002
        return self.streams.get(key, [])

    async def expire(self, key, ttl):
        self.expiries[key] = ttl

    async def sadd(self, key, value):
        self.sets.setdefault(key, set()).add(value)

    async def close(self):
        self.closed = True


def _clear_worker_env(monkeypatch):
    for key in (
        "A2A_AGENT_NAME",
        "A2A_SELF_NAME",
        "A2A_QUEUE_NAME",
        "A2A_ARQ_QUEUE_NAME",
        "A2A_REDIS_URL",
        "A2A_TASK_RETAIN_S",
        "A2A_TASK_DEADLINE_S",
        "A2A_TASK_WORKERS",
    ):
        monkeypatch.delenv(key, raising=False)


def test_queue_name_uses_canonical_env(monkeypatch):
    from clawcu.a2a.adapter import worker

    _clear_worker_env(monkeypatch)
    monkeypatch.setenv("A2A_AGENT_NAME", "writer")
    monkeypatch.setenv("A2A_QUEUE_NAME", "clawcu:a2a:custom")

    assert worker.queue_name() == "clawcu:a2a:custom"
    assert worker.queue_name("analyst") == "clawcu:a2a:analyst"


def test_queue_name_tolerates_arq_alias(monkeypatch):
    from clawcu.a2a.adapter import worker

    _clear_worker_env(monkeypatch)
    monkeypatch.setenv("A2A_AGENT_NAME", "writer")
    monkeypatch.setenv("A2A_ARQ_QUEUE_NAME", "clawcu:a2a:legacy")

    assert worker.config().queue_name == "clawcu:a2a:legacy"
    assert worker.queue_name() == "clawcu:a2a:legacy"


def test_config_uses_task_facade_env_parser(monkeypatch):
    from clawcu.a2a.adapter import worker

    _clear_worker_env(monkeypatch)
    monkeypatch.setenv("A2A_AGENT_NAME", "writer")
    monkeypatch.setenv("A2A_REDIS_URL", "redis://redis.internal:6379/3")
    monkeypatch.setenv("A2A_TASK_RETAIN_S", "23")
    monkeypatch.setenv("A2A_TASK_DEADLINE_S", "17")
    monkeypatch.setenv("A2A_TASK_WORKERS", "2")

    cfg = worker.config()

    assert cfg.redis_url == "redis://redis.internal:6379/3"
    assert cfg.queue_name == "clawcu:a2a:writer"
    assert cfg.retain_s == 23
    assert cfg.deadline_s == 17
    assert cfg.workers == 2


def test_build_worker_settings_uses_task_facade_redis_settings(monkeypatch):
    from clawcu.a2a.adapter import worker

    _clear_worker_env(monkeypatch)
    sentinel = object()
    monkeypatch.setenv("A2A_AGENT_NAME", "writer")
    monkeypatch.setenv("A2A_REDIS_URL", "redis://redis.internal:6379/3")
    monkeypatch.setenv("A2A_TASK_RETAIN_S", "23")
    monkeypatch.setenv("A2A_TASK_DEADLINE_S", "17")
    monkeypatch.setenv("A2A_TASK_WORKERS", "2")
    monkeypatch.setattr(
        worker.tasks,
        "redis_settings_from_url",
        lambda url: sentinel if url == "redis://redis.internal:6379/3" else None,
    )

    settings = worker.build_worker_settings()

    assert settings.functions == [worker.run_gateway_turn]
    assert settings.redis_settings is sentinel
    assert settings.queue_name == "clawcu:a2a:writer"
    assert settings.max_jobs == 2
    assert settings.job_timeout == 17
    assert settings.keep_result == 23
    assert settings.allow_abort_jobs is True
    assert settings.max_tries == 1
    assert settings.retry_jobs is False


def test_run_gateway_turn_completes_with_ctx_redis(monkeypatch):
    from clawcu.a2a.adapter import tasks, worker

    redis = FakeRedis()
    store = tasks.TaskStore(redis)
    asyncio.run(
        store.create(
            instance="analyst",
            peer="writer",
            message="hello",
            task_id="task_1",
        )
    )

    async def call_gateway(text, auth_token):
        assert text == "hello"
        assert auth_token == "token"
        return "world"

    monkeypatch.setattr(worker, "_call_gateway", call_gateway)

    result = asyncio.run(
        worker.run_gateway_turn(
            {"redis": redis, "gateway_auth_token": "token"},
            {"task_id": "task_1", "input": {"message": "hello"}},
        )
    )

    assert result["state"] == "completed"
    assert result["result"]["message"]["parts"] == [{"type": "text", "text": "world"}]
    events = asyncio.run(store.read_events("task_1"))
    assert [event["event"] for event in events] == [
        "submitted",
        "working",
        "progress",
        "completed",
    ]


def test_run_gateway_turn_preserves_configured_retention(monkeypatch):
    from clawcu.a2a.adapter import tasks, worker

    redis = FakeRedis()
    store = tasks.TaskStore(redis, retain_s=23)
    asyncio.run(
        store.create(
            instance="analyst",
            peer="writer",
            message="hello",
            task_id="task_1",
        )
    )

    async def call_gateway(_text, _auth_token):
        return "world"

    monkeypatch.setenv("A2A_TASK_RETAIN_S", "23")
    monkeypatch.setattr(worker, "_call_gateway", call_gateway)

    asyncio.run(
        worker.run_gateway_turn({"redis": redis}, {"task_id": "task_1", "message": "hello"})
    )

    assert redis.expiries[tasks.task_key("task_1")] == 23


def test_run_gateway_turn_creates_pool_when_ctx_has_no_redis(monkeypatch):
    from clawcu.a2a.adapter import tasks, worker

    redis = FakeRedis()
    asyncio.run(
        tasks.TaskStore(redis).create(
            instance="analyst",
            peer="writer",
            message="hello",
            task_id="task_1",
        )
    )

    async def create_pool():
        return redis

    async def call_gateway(_text, _auth_token):
        return "world"

    monkeypatch.setattr(worker, "_create_redis_pool", create_pool)
    monkeypatch.setattr(worker, "_call_gateway", call_gateway)

    result = asyncio.run(worker.run_gateway_turn({}, {"task_id": "task_1", "message": "hello"}))

    assert result["state"] == "completed"
    assert redis.closed is True


def test_run_gateway_turn_fails_on_empty_reply(monkeypatch):
    from clawcu.a2a.adapter import tasks, worker

    redis = FakeRedis()
    asyncio.run(
        tasks.TaskStore(redis).create(
            instance="analyst",
            peer="writer",
            message="hello",
            task_id="task_1",
        )
    )

    async def call_gateway(_text, _auth_token):
        return ""

    monkeypatch.setattr(worker, "_call_gateway", call_gateway)

    result = asyncio.run(
        worker.run_gateway_turn({"redis": redis}, {"task_id": "task_1", "message": "hello"})
    )

    assert result["state"] == "failed"
    assert result["error"] == "Empty gateway reply"


def test_run_gateway_turn_skips_terminal_snapshot(monkeypatch):
    from clawcu.a2a.adapter import tasks, worker

    redis = FakeRedis()
    store = tasks.TaskStore(redis)
    asyncio.run(
        store.create(
            instance="analyst",
            peer="writer",
            message="hello",
            task_id="task_1",
        )
    )
    asyncio.run(store.request_cancel("task_1"))

    async def call_gateway(_text, _auth_token):
        raise AssertionError("gateway should not be called for terminal tasks")

    monkeypatch.setattr(worker, "_call_gateway", call_gateway)

    result = asyncio.run(
        worker.run_gateway_turn({"redis": redis}, {"task_id": "task_1", "message": "hello"})
    )

    assert result["state"] == "canceled"


def test_run_gateway_turn_marks_canceled_and_reraises(monkeypatch):
    from clawcu.a2a.adapter import tasks, worker

    redis = FakeRedis()
    store = tasks.TaskStore(redis)
    asyncio.run(
        store.create(
            instance="analyst",
            peer="writer",
            message="hello",
            task_id="task_1",
        )
    )

    async def call_gateway(_text, _auth_token):
        raise asyncio.CancelledError

    monkeypatch.setattr(worker, "_call_gateway", call_gateway)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            worker.run_gateway_turn(
                {"redis": redis},
                {"task_id": "task_1", "message": "hello"},
            )
        )

    snapshot = asyncio.run(store.get("task_1"))
    assert snapshot["state"] == "canceled"
    assert snapshot["error"] == "Worker job canceled"
