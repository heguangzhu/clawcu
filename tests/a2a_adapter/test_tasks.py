"""Tests for the Redis-backed A2A task facade."""

import asyncio
import json

import pytest


class _FakeRedis:
    def __init__(self):
        self.values = {}
        self.expiries = {}
        self.streams = {}
        self.sets = {}
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
        rows = self.streams.get(key, [])
        if isinstance(min, str) and min.startswith("("):
            after = min[1:]
            return [(eid, fields) for eid, fields in rows if _id_gt(eid, after)]
        return rows

    async def expire(self, key, ttl):
        self.expiries[key] = ttl

    async def sadd(self, key, value):
        self.sets.setdefault(key, set()).add(value)


def _id_gt(left, right):
    left_a, left_b = [int(part) for part in left.split("-", 1)]
    right_a, right_b = [int(part) for part in right.split("-", 1)]
    return (left_a, left_b) > (right_a, right_b)


def test_config_from_env_defaults_to_sync_and_async_enabled():
    from clawcu.a2a.adapter.tasks import DEFAULT_REDIS_URL, config_from_env

    cfg = config_from_env({})

    assert cfg.enabled is True
    assert cfg.redis_url == DEFAULT_REDIS_URL
    assert cfg.default_mode == "sync"
    assert cfg.queue_name == "clawcu:a2a:agent"


def test_config_from_env_reads_async_settings():
    from clawcu.a2a.adapter.tasks import config_from_env

    cfg = config_from_env(
        {
            "A2A_ASYNC_ENABLED": "true",
            "A2A_REDIS_URL": "redis://redis.internal:6380/2",
            "A2A_AGENT_NAME": "writer",
            "A2A_TASK_RETAIN_S": "60",
            "A2A_TASK_DEADLINE_S": "120",
            "A2A_TASK_WORKERS": "8",
            "A2A_DEFAULT_MODE": "async",
            "A2A_TASK_PROGRESS_INTERVAL_S": "2",
            "A2A_TASK_EVENTS_IDLE_TIMEOUT_S": "9",
        }
    )

    assert cfg.enabled is True
    assert cfg.redis_url == "redis://redis.internal:6380/2"
    assert cfg.queue_name == "clawcu:a2a:writer"
    assert cfg.retain_s == 60
    assert cfg.deadline_s == 120
    assert cfg.workers == 8
    assert cfg.default_mode == "async"
    assert cfg.progress_interval_s == 2
    assert cfg.events_idle_timeout_s == 9


def test_config_from_env_can_disable_async():
    from clawcu.a2a.adapter.tasks import config_from_env

    cfg = config_from_env({"A2A_ASYNC_ENABLED": "false"})

    assert cfg.enabled is False
    assert cfg.default_mode == "sync"


def test_config_from_env_accepts_legacy_arq_queue_name_alias():
    from clawcu.a2a.adapter.tasks import config_from_env

    cfg = config_from_env(
        {
            "A2A_AGENT_NAME": "writer",
            "A2A_ARQ_QUEUE_NAME": "clawcu:a2a:legacy",
        }
    )

    assert cfg.queue_name == "clawcu:a2a:legacy"


@pytest.mark.parametrize(
    ("url", "host", "port", "database", "ssl"),
    [
        ("redis://localhost", "localhost", 6379, 0, False),
        ("redis://:secret@redis.local:6380/4", "redis.local", 6380, 4, False),
        ("rediss://redis.local/1", "redis.local", 6379, 1, True),
    ],
)
def test_parse_redis_url(url, host, port, database, ssl):
    from clawcu.a2a.adapter.tasks import parse_redis_url

    parsed = parse_redis_url(url)

    assert parsed.host == host
    assert parsed.port == port
    assert parsed.database == database
    assert parsed.ssl is ssl


def test_parse_redis_url_rejects_bad_scheme():
    from clawcu.a2a.adapter.tasks import parse_redis_url

    with pytest.raises(ValueError, match="redis:// or rediss://"):
        parse_redis_url("http://localhost:6379")


def test_queue_name_sanitizes_instance_name():
    from clawcu.a2a.adapter.tasks import queue_name_for

    assert queue_name_for("Writer 01!") == "clawcu:a2a:Writer-01"
    assert queue_name_for("  ") == "clawcu:a2a:agent"


def test_mint_task_id_shape():
    from clawcu.a2a.adapter.tasks import mint_task_id

    task_id = mint_task_id()

    assert task_id.startswith("task_")
    assert len(task_id) == len("task_") + 32


def test_task_store_create_transition_progress_and_events():
    from clawcu.a2a.adapter.tasks import (
        STATE_COMPLETED,
        STATE_WORKING,
        TaskStore,
        instance_index_key,
        task_events_key,
        task_key,
    )

    redis = _FakeRedis()
    store = TaskStore(redis, retain_s=30)

    snapshot = asyncio.run(
        store.create(
            instance="analyst",
            peer="writer",
            message="summarize",
            task_id="task_abc",
            thread_id="thread-1",
            request_id="rid-1",
        )
    )
    assert snapshot["state"] == "submitted"
    assert task_key("task_abc") in redis.values
    assert redis.expiries[task_key("task_abc")] == 30
    assert "task_abc" in redis.sets[instance_index_key("analyst")]

    working = asyncio.run(store.transition("task_abc", STATE_WORKING))
    assert working["state"] == STATE_WORKING

    progressed = asyncio.run(store.progress("task_abc", "calling gateway"))
    assert progressed["last_progress_message"] == "calling gateway"

    completed = asyncio.run(
        store.transition(
            "task_abc",
            STATE_COMPLETED,
            result={"reply": "done"},
            message="complete",
        )
    )
    assert completed["state"] == STATE_COMPLETED
    assert completed["result"] == {"reply": "done"}

    raw = redis.values[task_key("task_abc")]
    assert json.loads(raw)["state"] == STATE_COMPLETED

    events = asyncio.run(store.read_events("task_abc"))
    assert [event["event"] for event in events] == [
        "submitted",
        "working",
        "progress",
        "completed",
    ]
    assert redis.expiries[task_events_key("task_abc")] == 30


def test_task_store_rejects_illegal_transition():
    from clawcu.a2a.adapter.tasks import STATE_COMPLETED, TaskError, TaskStore

    store = TaskStore(_FakeRedis())
    asyncio.run(
        store.create(instance="analyst", peer="writer", message="hi", task_id="task_bad")
    )

    with pytest.raises(TaskError, match="illegal transition"):
        asyncio.run(store.transition("task_bad", STATE_COMPLETED))


def test_task_store_cancel_is_terminal():
    from clawcu.a2a.adapter.tasks import STATE_COMPLETED, TaskError, TaskStore

    store = TaskStore(_FakeRedis())
    asyncio.run(
        store.create(instance="analyst", peer="writer", message="hi", task_id="task_cancel")
    )
    canceled = asyncio.run(store.request_cancel("task_cancel"))
    assert canceled["state"] == "canceled"

    with pytest.raises(TaskError, match="already terminal"):
        asyncio.run(store.transition("task_cancel", STATE_COMPLETED, result={"reply": "late"}))


@pytest.mark.parametrize(
    ("status", "state"),
    [
        ("queued", "submitted"),
        ("deferred", "submitted"),
        ("in_progress", "working"),
        ("running", "working"),
        ("complete", "completed"),
        ("not_found", None),
    ],
)
def test_a2a_state_from_arq_status(status, state):
    from clawcu.a2a.adapter.tasks import a2a_state_from_arq_status

    assert a2a_state_from_arq_status(status) == state


def test_a2a_state_from_arq_status_prefers_terminal_snapshot():
    from clawcu.a2a.adapter.tasks import a2a_state_from_arq_status

    assert (
        a2a_state_from_arq_status("complete", snapshot_state="failed")
        == "failed"
    )
