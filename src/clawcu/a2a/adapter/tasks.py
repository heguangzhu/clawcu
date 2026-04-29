"""Redis-backed A2A task facade for async adapter calls."""

from __future__ import annotations

import json
import os
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit, unquote

STATE_SUBMITTED = "submitted"
STATE_WORKING = "working"
STATE_COMPLETED = "completed"
STATE_FAILED = "failed"
STATE_CANCELED = "canceled"

TERMINAL_STATES = frozenset({STATE_COMPLETED, STATE_FAILED, STATE_CANCELED})
VALID_STATES = frozenset(
    {STATE_SUBMITTED, STATE_WORKING, STATE_COMPLETED, STATE_FAILED, STATE_CANCELED}
)
VALID_TRANSITIONS = {
    STATE_SUBMITTED: frozenset({STATE_WORKING, STATE_FAILED, STATE_CANCELED}),
    STATE_WORKING: frozenset({STATE_COMPLETED, STATE_FAILED, STATE_CANCELED}),
}

DEFAULT_REDIS_URL = "redis://host.docker.internal:6379/0"
DEFAULT_ASYNC_ENABLED = True
DEFAULT_RETAIN_S = 86400
DEFAULT_EVENTS_IDLE_TIMEOUT_S = 60
DEFAULT_PROGRESS_INTERVAL_S = 3
DEFAULT_QUEUE_PREFIX = "clawcu:a2a"

_SAFE_QUEUE_PART = re.compile(r"[^A-Za-z0-9_.-]+")


class TaskError(Exception):
    """Raised for invalid task mutations or storage failures."""

    def __init__(self, message: str, *, http_status: int = 400) -> None:
        super().__init__(message)
        self.http_status = http_status


@dataclass(frozen=True)
class RedisDsn:
    """Parsed Redis connection settings used by arq and task storage."""

    url: str
    host: str
    port: int
    database: int
    password: str | None = None
    ssl: bool = False


@dataclass(frozen=True)
class AsyncTaskConfig:
    """Environment-derived async task settings."""

    enabled: bool = DEFAULT_ASYNC_ENABLED
    redis_url: str = DEFAULT_REDIS_URL
    queue_name: str = ""
    retain_s: int = DEFAULT_RETAIN_S
    deadline_s: int = DEFAULT_RETAIN_S
    workers: int = 4
    default_mode: str = "sync"
    progress_interval_s: int = DEFAULT_PROGRESS_INTERVAL_S
    events_idle_timeout_s: int = DEFAULT_EVENTS_IDLE_TIMEOUT_S


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_bool(value: str | None, *, default: bool = False) -> bool:
    """Parse a loose env-style boolean."""

    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def parse_positive_int(value: str | None, *, default: int) -> int:
    """Parse a positive integer env value, falling back on invalid input."""

    if value is None or value == "":
        return default
    try:
        parsed = int(value)
    except ValueError:
        return default
    return parsed if parsed > 0 else default


def parse_default_mode(value: str | None) -> str:
    mode = (value or "").strip().lower()
    return mode if mode in {"sync", "async"} else "sync"


def parse_redis_url(url: str | None = None) -> RedisDsn:
    """Parse a redis:// or rediss:// URL into RedisDsn."""

    raw = (url or DEFAULT_REDIS_URL).strip() or DEFAULT_REDIS_URL
    parsed = urlsplit(raw)
    if parsed.scheme not in {"redis", "rediss"}:
        raise ValueError("A2A_REDIS_URL must use redis:// or rediss://")
    if not parsed.hostname:
        raise ValueError("A2A_REDIS_URL must include a host")
    database = 0
    path = parsed.path.strip("/")
    if path:
        try:
            database = int(path.split("/", 1)[0])
        except ValueError as exc:
            raise ValueError("A2A_REDIS_URL database must be an integer") from exc
        if database < 0:
            raise ValueError("A2A_REDIS_URL database must be non-negative")
    return RedisDsn(
        url=raw,
        host=parsed.hostname,
        port=parsed.port or 6379,
        database=database,
        password=unquote(parsed.password) if parsed.password else None,
        ssl=parsed.scheme == "rediss",
    )


def redis_settings_from_url(url: str | None = None) -> Any:
    """Build arq RedisSettings lazily so importing this module does not require arq."""

    from arq.connections import RedisSettings

    dsn = parse_redis_url(url)
    return RedisSettings(
        host=dsn.host,
        port=dsn.port,
        database=dsn.database,
        password=dsn.password,
        ssl=dsn.ssl,
    )


def queue_name_for(instance_name: str, *, prefix: str = DEFAULT_QUEUE_PREFIX) -> str:
    """Return the per-instance arq queue name."""

    cleaned = _SAFE_QUEUE_PART.sub("-", instance_name.strip()).strip("-")
    return f"{prefix}:{cleaned or 'agent'}"


def config_from_env(env: dict[str, str] | None = None) -> AsyncTaskConfig:
    """Read async task settings from environment-like mapping."""

    source = env if env is not None else os.environ
    instance = source.get("A2A_AGENT_NAME") or source.get("A2A_SELF_NAME") or "agent"
    queue_name = source.get("A2A_QUEUE_NAME") or source.get("A2A_ARQ_QUEUE_NAME")
    return AsyncTaskConfig(
        enabled=parse_bool(source.get("A2A_ASYNC_ENABLED"), default=DEFAULT_ASYNC_ENABLED),
        redis_url=(source.get("A2A_REDIS_URL") or DEFAULT_REDIS_URL).strip()
        or DEFAULT_REDIS_URL,
        queue_name=(queue_name or queue_name_for(instance)).strip(),
        retain_s=parse_positive_int(
            source.get("A2A_TASK_RETAIN_S"), default=DEFAULT_RETAIN_S
        ),
        deadline_s=parse_positive_int(
            source.get("A2A_TASK_DEADLINE_S"), default=DEFAULT_RETAIN_S
        ),
        workers=parse_positive_int(source.get("A2A_TASK_WORKERS"), default=4),
        default_mode=parse_default_mode(source.get("A2A_DEFAULT_MODE")),
        progress_interval_s=parse_positive_int(
            source.get("A2A_TASK_PROGRESS_INTERVAL_S"),
            default=DEFAULT_PROGRESS_INTERVAL_S,
        ),
        events_idle_timeout_s=parse_positive_int(
            source.get("A2A_TASK_EVENTS_IDLE_TIMEOUT_S"),
            default=DEFAULT_EVENTS_IDLE_TIMEOUT_S,
        ),
    )


def mint_task_id() -> str:
    return f"task_{uuid.uuid4().hex}"


def task_key(task_id: str) -> str:
    return f"a2a:task:{task_id}"


def task_events_key(task_id: str) -> str:
    return f"a2a:task:{task_id}:events"


def instance_index_key(instance: str) -> str:
    return f"a2a:task-index:{instance}"


def a2a_state_from_arq_status(status: Any, *, snapshot_state: str | None = None) -> str | None:
    """Map arq JobStatus-like values into the public A2A state vocabulary.

    arq status values are enums at runtime, but tests and fallback paths may
    pass strings. Prefer an already-terminal snapshot because arq's "complete"
    only tells us the job ended, not whether the A2A result was success,
    failure, or cancellation.
    """

    if snapshot_state in TERMINAL_STATES:
        return snapshot_state
    value = getattr(status, "value", status)
    name = getattr(status, "name", "")
    text = str(value or name).lower()
    if "not_found" in text or text == "none":
        return None
    if "queued" in text or "deferred" in text:
        return STATE_SUBMITTED
    if "progress" in text or "running" in text or "in_progress" in text:
        return STATE_WORKING
    if "complete" in text:
        return snapshot_state if snapshot_state in VALID_STATES else STATE_COMPLETED
    return snapshot_state if snapshot_state in VALID_STATES else None


class TaskStore:
    """Small A2A-owned facade over Redis snapshots and stream events."""

    def __init__(self, redis: Any, *, retain_s: int = DEFAULT_RETAIN_S) -> None:
        self.redis = redis
        self.retain_s = max(1, int(retain_s or DEFAULT_RETAIN_S))

    async def create(
        self,
        *,
        instance: str,
        peer: str,
        message: str,
        task_id: str | None = None,
        thread_id: str | None = None,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        tid = task_id or mint_task_id()
        now = _utc_now()
        snapshot: dict[str, Any] = {
            "task_id": tid,
            "instance": instance,
            "peer": peer,
            "state": STATE_SUBMITTED,
            "created_at": now,
            "updated_at": now,
            "thread_id": thread_id,
            "request_id": request_id,
            "input": {"message": message},
            "result": None,
            "error": None,
            "last_progress_at": None,
            "last_progress_message": None,
        }
        await self._write_snapshot(snapshot)
        await self._append_event(
            tid, {"event": STATE_SUBMITTED, "state": STATE_SUBMITTED, "ts": now}
        )
        await self._maybe_sadd(instance_index_key(instance), tid)
        return snapshot

    async def get(self, task_id: str) -> dict[str, Any] | None:
        raw = await self.redis.get(task_key(task_id))
        if raw is None:
            return None
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            return None
        return payload if isinstance(payload, dict) else None

    async def transition(
        self,
        task_id: str,
        to_state: str,
        *,
        result: Any = None,
        error: Any = None,
        message: str | None = None,
    ) -> dict[str, Any]:
        if to_state not in VALID_STATES:
            raise TaskError(f"unknown target state: {to_state}")
        snapshot = await self.get(task_id)
        if snapshot is None:
            raise TaskError("task not found", http_status=404)
        current = snapshot.get("state")
        if current in TERMINAL_STATES:
            if current == to_state:
                return snapshot
            raise TaskError(
                f"task already terminal ({current}); cannot transition to {to_state}",
                http_status=409,
            )
        allowed = VALID_TRANSITIONS.get(str(current), frozenset())
        if to_state != current and to_state not in allowed:
            raise TaskError(f"illegal transition {current} -> {to_state}", http_status=409)
        now = _utc_now()
        snapshot["state"] = to_state
        snapshot["updated_at"] = now
        if result is not None:
            snapshot["result"] = result
        if error is not None:
            snapshot["error"] = error
        await self._write_snapshot(snapshot)
        event: dict[str, Any] = {"event": to_state, "state": to_state, "ts": now}
        if message:
            event["message"] = message
        if result is not None:
            event["result"] = result
        if error is not None:
            event["error"] = error
        await self._append_event(task_id, event)
        return snapshot

    async def progress(self, task_id: str, message: str | None = None) -> dict[str, Any] | None:
        snapshot = await self.get(task_id)
        if snapshot is None or snapshot.get("state") in TERMINAL_STATES:
            return snapshot
        now = _utc_now()
        note = str(message)[:200] if message else None
        snapshot["updated_at"] = now
        snapshot["last_progress_at"] = now
        if note:
            snapshot["last_progress_message"] = note
        await self._write_snapshot(snapshot)
        event: dict[str, Any] = {"event": "progress", "ts": now}
        if note:
            event["message"] = note
        await self._append_event(task_id, event)
        return snapshot

    async def request_cancel(self, task_id: str) -> dict[str, Any]:
        return await self.transition(
            task_id,
            STATE_CANCELED,
            error={"message": "canceled by client", "http_status": 499},
        )

    async def read_events(self, task_id: str, *, after_id: str = "0-0") -> list[dict[str, Any]]:
        min_id = f"({after_id}" if after_id and after_id != "0-0" else "-"
        rows = await self.redis.xrange(task_events_key(task_id), min=min_id)
        events: list[dict[str, Any]] = []
        for event_id, fields in rows or []:
            decoded_id = event_id.decode("utf-8") if isinstance(event_id, bytes) else str(event_id)
            data = _field_get(fields, "data")
            if isinstance(data, bytes):
                data = data.decode("utf-8")
            try:
                event = json.loads(data)
            except (TypeError, ValueError):
                continue
            if isinstance(event, dict):
                event["_id"] = decoded_id
                events.append(event)
        return events

    async def _write_snapshot(self, snapshot: dict[str, Any]) -> None:
        payload = json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"))
        await self.redis.set(task_key(snapshot["task_id"]), payload, ex=self.retain_s)

    async def _append_event(self, task_id: str, event: dict[str, Any]) -> None:
        await self.redis.xadd(
            task_events_key(task_id),
            {"data": json.dumps(event, ensure_ascii=False, separators=(",", ":"))},
        )
        await self.redis.expire(task_events_key(task_id), self.retain_s)

    async def _maybe_sadd(self, key: str, value: str) -> None:
        sadd = getattr(self.redis, "sadd", None)
        if callable(sadd):
            await sadd(key, value)
            await self.redis.expire(key, self.retain_s)


def _field_get(fields: Any, key: str) -> Any:
    if not isinstance(fields, dict):
        return None
    return fields.get(key) if key in fields else fields.get(key.encode("utf-8"))


__all__ = [
    "AsyncTaskConfig",
    "a2a_state_from_arq_status",
    "DEFAULT_ASYNC_ENABLED",
    "DEFAULT_EVENTS_IDLE_TIMEOUT_S",
    "DEFAULT_PROGRESS_INTERVAL_S",
    "DEFAULT_QUEUE_PREFIX",
    "DEFAULT_REDIS_URL",
    "RedisDsn",
    "STATE_CANCELED",
    "STATE_COMPLETED",
    "STATE_FAILED",
    "STATE_SUBMITTED",
    "STATE_WORKING",
    "TERMINAL_STATES",
    "TaskError",
    "TaskStore",
    "config_from_env",
    "instance_index_key",
    "mint_task_id",
    "parse_bool",
    "parse_default_mode",
    "parse_positive_int",
    "parse_redis_url",
    "queue_name_for",
    "redis_settings_from_url",
    "task_events_key",
    "task_key",
]
