"""arq worker entrypoint for async A2A gateway turns."""

from __future__ import annotations

import asyncio
import importlib
import os
from collections.abc import Mapping
from typing import Any

from . import tasks

DEFAULT_JOB_TIMEOUT_S = 86400


def _task_env() -> dict[str, str]:
    env = dict(os.environ)
    if "A2A_QUEUE_NAME" not in env and "A2A_ARQ_QUEUE_NAME" in env:
        env["A2A_QUEUE_NAME"] = env["A2A_ARQ_QUEUE_NAME"]
    return env


def config() -> tasks.AsyncTaskConfig:
    """Return worker config using the task facade's environment parser."""
    return tasks.config_from_env(_task_env())


def queue_name(instance: str | None = None) -> str:
    """Return the canonical arq queue name."""
    cfg = config()
    if instance is None:
        return cfg.queue_name
    return tasks.queue_name_for(instance)


def _payload_task_id(payload: Mapping[str, Any]) -> str:
    value = payload.get("task_id") or payload.get("id")
    if value:
        return str(value)
    raise ValueError("worker payload missing task_id")


def _payload_text(payload: Mapping[str, Any]) -> str:
    value = payload.get("text") or payload.get("message")
    if isinstance(value, str):
        return value.strip()

    input_payload = payload.get("input")
    if isinstance(input_payload, Mapping):
        value = input_payload.get("message") or input_payload.get("text")
        if isinstance(value, str):
            return value.strip()

    return ""


async def _call_gateway(text: str, auth_token: str) -> str:
    gateway_executor = importlib.import_module("clawcu.a2a.adapter.executor")
    return await gateway_executor._call_gateway(text, auth_token)


def _gateway_auth_token() -> str:
    try:
        gateway_executor = importlib.import_module("clawcu.a2a.adapter.executor")
    except ModuleNotFoundError as exc:
        if exc.name and exc.name.startswith("a2a"):
            return os.environ.get("A2A_GATEWAY_AUTH_TOKEN", "")
        raise
    return str(getattr(gateway_executor, "_GATEWAY_AUTH_TOKEN", ""))


def _gateway_result(reply: str) -> dict[str, Any]:
    part = {"type": "text", "text": reply}
    return {
        "reply": reply,
        "artifacts": [{"parts": [part]}],
        "message": {"role": "agent", "parts": [part]},
    }


async def _create_redis_pool() -> Any:
    try:
        from arq.connections import create_pool
    except ModuleNotFoundError as exc:
        raise RuntimeError("arq is required to run the A2A async worker") from exc
    return await create_pool(tasks.redis_settings_from_url(config().redis_url))


async def _close_redis_pool(redis: Any) -> None:
    aclose = getattr(redis, "aclose", None)
    if aclose is not None:
        result = aclose()
        if hasattr(result, "__await__"):
            await result
        return
    close = getattr(redis, "close", None)
    if close is None:
        return
    result = close()
    if hasattr(result, "__await__"):
        await result
    wait_closed = getattr(redis, "wait_closed", None)
    if wait_closed is not None:
        result = wait_closed()
        if hasattr(result, "__await__"):
            await result


async def _redis_from_ctx(ctx: Mapping[str, Any]) -> tuple[Any, bool]:
    redis = ctx.get("redis") or ctx.get("redis_pool")
    if redis is not None:
        return redis, False
    return await _create_redis_pool(), True


async def run_gateway_turn(ctx: dict[str, Any], payload: dict[str, Any]) -> Any:
    """Run one async A2A task by forwarding the prompt to the local gateway."""
    if not isinstance(payload, Mapping):
        raise TypeError("worker payload must be a mapping")

    redis, owns_redis = await _redis_from_ctx(ctx)
    store = tasks.TaskStore(redis, retain_s=config().retain_s)
    task_id = _payload_task_id(payload)
    text = _payload_text(payload)
    auth_token = str(
        payload.get("auth_token")
        or ctx.get("gateway_auth_token", "")
        or _gateway_auth_token()
    )

    try:
        snapshot = await store.get(task_id)
        if snapshot is not None and snapshot.get("state") in tasks.TERMINAL_STATES:
            return snapshot

        await store.transition(task_id, tasks.STATE_WORKING)

        if not text:
            await store.transition(task_id, tasks.STATE_FAILED, error="Empty message")
            return await store.get(task_id)

        try:
            await store.progress(task_id, "calling gateway")
            reply = await _call_gateway(text, auth_token)
        except asyncio.CancelledError:
            await store.transition(
                task_id,
                tasks.STATE_CANCELED,
                error="Worker job canceled",
            )
            raise
        except Exception as exc:
            await store.transition(
                task_id,
                tasks.STATE_FAILED,
                error=f"Gateway error: {exc}",
            )
            return await store.get(task_id)

        if not reply:
            await store.transition(
                task_id,
                tasks.STATE_FAILED,
                error="Empty gateway reply",
            )
            return await store.get(task_id)

        snapshot = await store.get(task_id)
        if snapshot is not None and snapshot.get("state") in tasks.TERMINAL_STATES:
            return snapshot

        await store.transition(
            task_id,
            tasks.STATE_COMPLETED,
            result=_gateway_result(reply),
        )
        return await store.get(task_id)
    finally:
        if owns_redis:
            await _close_redis_pool(redis)


def build_worker_settings() -> type:
    """Create an arq WorkerSettings class from current task facade config."""
    cfg = config()

    class WorkerSettings:
        functions = [run_gateway_turn]
        redis_settings = tasks.redis_settings_from_url(cfg.redis_url)
        queue_name = cfg.queue_name
        max_jobs = cfg.workers
        job_timeout = cfg.deadline_s or DEFAULT_JOB_TIMEOUT_S
        keep_result = cfg.retain_s
        allow_abort_jobs = True
        max_tries = 1
        retry_jobs = False

    return WorkerSettings


try:
    WorkerSettings = build_worker_settings()
except ModuleNotFoundError:
    _cfg = config()

    class WorkerSettings:  # type: ignore[no-redef]
        """Importable settings shape for environments without arq installed."""

        functions = [run_gateway_turn]
        redis_settings = None
        queue_name = _cfg.queue_name
        max_jobs = _cfg.workers
        job_timeout = _cfg.deadline_s or DEFAULT_JOB_TIMEOUT_S
        keep_result = _cfg.retain_s
        allow_abort_jobs = True
        max_tries = 1
        retry_jobs = False


def main() -> None:
    """Run the arq worker."""
    try:
        from arq.cli import cli
    except ModuleNotFoundError as exc:
        raise RuntimeError("arq is required to run the A2A async worker") from exc

    cli(["clawcu.a2a.adapter.worker.WorkerSettings"])


if __name__ == "__main__":
    main()
