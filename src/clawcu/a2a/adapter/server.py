"""Starlette app and uvicorn entrypoint for the ClawCU A2A adapter."""

from __future__ import annotations

import logging
import os
import time
from typing import Any

from a2a.server.routes import create_agent_card_routes
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from .card import build_agent_card
from .executor import _GATEWAY_AUTH_TOKEN, _call_gateway, _check_gateway_ready
from .mcp_bridge import handle_mcp
from .tasks import (
    STATE_CANCELED,
    TaskError,
    TaskStore,
    config_from_env,
    mint_task_id,
    redis_settings_from_url,
)

log = logging.getLogger("clawcu-a2a-adapter")


def _rpc_error(rpc_id: Any, code: int, message: str) -> JSONResponse:
    return JSONResponse(
        {"jsonrpc": "2.0", "id": rpc_id, "error": {"code": code, "message": message}}
    )


def _extract_message_text(params: Any) -> str:
    if not isinstance(params, dict):
        return ""
    message = params.get("message")
    if not isinstance(message, dict):
        return ""
    parts = message.get("parts")
    if not isinstance(parts, list):
        return ""
    text_parts: list[str] = []
    for part in parts:
        if not isinstance(part, dict):
            continue
        text = part.get("text")
        if isinstance(text, str) and text:
            text_parts.append(text)
    return "\n".join(text_parts).strip()


def _is_blocking(params: Any) -> bool:
    """Return whether message/send should block for the reply."""

    if isinstance(params, dict):
        configuration = params.get("configuration")
        if isinstance(configuration, dict) and isinstance(configuration.get("blocking"), bool):
            return bool(configuration["blocking"])
        metadata = params.get("metadata")
        if isinstance(metadata, dict):
            mode = str(metadata.get("mode") or "").strip().lower()
            if mode in {"sync", "async"}:
                return mode == "sync"
    return config_from_env().default_mode != "async"


def _agent_name() -> str:
    return os.environ.get("A2A_AGENT_NAME") or os.environ.get("A2A_SELF_NAME") or "agent"


async def _redis_pool():
    from arq import create_pool

    cfg = config_from_env()
    return await create_pool(redis_settings_from_url(cfg.redis_url))


async def _task_store() -> TaskStore:
    cfg = config_from_env()
    redis = await _redis_pool()
    return TaskStore(redis, retain_s=cfg.retain_s)


async def _enqueue_async_task(text: str, rpc_id: Any, params: Any) -> dict[str, Any]:
    cfg = config_from_env()
    if not cfg.enabled:
        raise TaskError("async A2A is disabled; set A2A_ASYNC_ENABLED=true", http_status=503)

    redis = await _redis_pool()
    store = TaskStore(redis, retain_s=cfg.retain_s)
    task_id = mint_task_id()
    request_id = str(rpc_id) if rpc_id is not None else None
    thread_id = None
    if isinstance(params, dict):
        metadata = params.get("metadata")
        if isinstance(metadata, dict) and isinstance(metadata.get("thread_id"), str):
            thread_id = metadata["thread_id"]
    snapshot = await store.create(
        instance=_agent_name(),
        peer="jsonrpc",
        message=text,
        task_id=task_id,
        thread_id=thread_id,
        request_id=request_id,
    )
    job = await redis.enqueue_job(
        "run_gateway_turn",
        {"task_id": task_id, "message": text},
        _job_id=task_id,
        _queue_name=cfg.queue_name,
    )
    if job is None:
        raise TaskError("task already queued", http_status=409)
    return snapshot


def _task_result(snapshot: dict[str, Any]) -> dict[str, Any]:
    task_id = snapshot["task_id"]
    state = snapshot.get("state") or "submitted"
    result: dict[str, Any] = {
        "id": task_id,
        "status": {"state": state},
        "metadata": {
            "task_id": task_id,
            "request_id": snapshot.get("request_id"),
        },
    }
    payload_result = snapshot.get("result")
    if isinstance(payload_result, dict):
        artifacts = payload_result.get("artifacts")
        message = payload_result.get("message")
        if isinstance(artifacts, list):
            result["artifacts"] = artifacts
        if isinstance(message, dict):
            result["message"] = message
    reply = payload_result.get("reply") if isinstance(payload_result, dict) else None
    if isinstance(reply, str) and reply and "artifacts" not in result:
        part = {"type": "text", "text": reply}
        result["artifacts"] = [{"parts": [part]}]
        result["message"] = {"role": "agent", "parts": [part]}
    return result


async def handle_jsonrpc(request: Request) -> JSONResponse:
    try:
        payload = await request.json()
    except Exception:
        return _rpc_error(None, -32700, "Parse error")

    if not isinstance(payload, dict):
        return _rpc_error(None, -32600, "Invalid request")

    rpc_id = payload.get("id")
    method = payload.get("method")
    if method not in {"message/send", "SendMessage"}:
        return _rpc_error(rpc_id, -32601, "Method not found")

    text = _extract_message_text(payload.get("params"))
    if not text:
        return _rpc_error(rpc_id, -32602, "Empty message")

    if not _is_blocking(payload.get("params")):
        try:
            snapshot = await _enqueue_async_task(text, rpc_id, payload.get("params"))
        except TaskError as exc:
            return _rpc_error(rpc_id, -32000, str(exc))
        except Exception as exc:
            log.exception("async task enqueue failed")
            return _rpc_error(rpc_id, -32000, f"Async task error: {exc}")
        return JSONResponse(
            {"jsonrpc": "2.0", "id": rpc_id, "result": _task_result(snapshot)}
        )

    if not await _check_gateway_ready():
        return _rpc_error(rpc_id, -32000, "Gateway not ready")

    try:
        reply = await _call_gateway(text, _GATEWAY_AUTH_TOKEN)
    except Exception as exc:
        log.exception("gateway call failed")
        return _rpc_error(rpc_id, -32000, f"Gateway error: {exc}")

    result = {
        "id": f"task-{rpc_id}",
        "status": {"state": "completed"},
        "artifacts": [{"parts": [{"type": "text", "text": reply}]}],
        "message": {"role": "agent", "parts": [{"type": "text", "text": reply}]},
    }
    return JSONResponse({"jsonrpc": "2.0", "id": rpc_id, "result": result})


async def handle_task_get(request: Request) -> JSONResponse:
    task_id = request.path_params["task_id"]
    try:
        snapshot = await (await _task_store()).get(task_id)
    except Exception as exc:
        log.exception("task get failed")
        return JSONResponse({"error": f"task storage error: {exc}"}, status_code=500)
    if snapshot is None:
        return JSONResponse({"error": f"task not found: {task_id}"}, status_code=404)
    return JSONResponse(_task_result(snapshot))


async def handle_task_cancel(request: Request) -> JSONResponse:
    task_id = request.path_params["task_id"]
    try:
        store = await _task_store()
        snapshot = await store.request_cancel(task_id)
        try:
            redis = store.redis
            from arq.jobs import Job

            await Job(task_id, redis=redis, _queue_name=config_from_env().queue_name).abort()
        except Exception:
            log.debug("arq abort failed for %s", task_id, exc_info=True)
    except TaskError as exc:
        return JSONResponse({"error": str(exc)}, status_code=exc.http_status)
    except Exception as exc:
        log.exception("task cancel failed")
        return JSONResponse({"error": f"task storage error: {exc}"}, status_code=500)
    return JSONResponse(_task_result(snapshot))


async def handle_task_events(request: Request):
    task_id = request.path_params["task_id"]
    try:
        from sse_starlette.sse import EventSourceResponse
    except Exception:
        return JSONResponse({"error": "SSE support is unavailable"}, status_code=503)

    store = await _task_store()
    try:
        initial = await store.get(task_id)
    except Exception as exc:
        log.exception("task events lookup failed")
        return JSONResponse({"error": f"task storage error: {exc}"}, status_code=500)
    if initial is None:
        return JSONResponse({"error": f"task not found: {task_id}"}, status_code=404)

    async def _events():
        import asyncio

        cfg = config_from_env()
        last_id = request.headers.get("last-event-id") or "0-0"
        started_at = time.monotonic()
        next_heartbeat_at = started_at + cfg.progress_interval_s
        while True:
            disconnected = getattr(request, "is_disconnected", None)
            if callable(disconnected) and await disconnected():
                break

            emitted = False
            for event in await store.read_events(task_id, after_id=last_id):
                event_id = event.pop("_id", None)
                if event_id:
                    last_id = event_id
                emitted = True
                yield {"event": event.get("event", "status"), "id": event_id, "data": event}

            snapshot = await store.get(task_id)
            if snapshot and snapshot.get("state") in {STATE_CANCELED, "completed", "failed"}:
                yield {"event": "end", "data": {}}
                break

            now = time.monotonic()
            if now - started_at >= cfg.events_idle_timeout_s:
                yield {"event": "end", "data": {"reason": "idle_timeout"}}
                break
            if not emitted and now >= next_heartbeat_at:
                yield {"event": "heartbeat", "data": {}}
                next_heartbeat_at = now + cfg.progress_interval_s
            await asyncio.sleep(min(1.0, float(cfg.progress_interval_s)))

    return EventSourceResponse(_events())


def create_app() -> Starlette:
    """Build the Starlette application with JSON-RPC and agent-card routes."""
    agent_card = build_agent_card()

    routes = [
        *create_agent_card_routes(agent_card),
        Route("/", handle_jsonrpc, methods=["POST"]),
        Route("/mcp", handle_mcp, methods=["POST"]),
        Route("/tasks/{task_id}", handle_task_get, methods=["GET"]),
        Route("/tasks/{task_id}/cancel", handle_task_cancel, methods=["POST"]),
        Route("/tasks/{task_id}/events", handle_task_events, methods=["GET"]),
    ]

    return Starlette(routes=routes)


def main() -> None:
    """Run the adapter via uvicorn (entrypoint for ``python -m clawcu.a2a.adapter.server``)."""
    import os

    import uvicorn

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    host = os.environ.get("A2A_ADAPTER_HOST", "0.0.0.0")
    port = int(os.environ.get("A2A_ADAPTER_PORT", "18790"))
    log.info("starting clawcu-a2a-adapter on %s:%s", host, port)
    uvicorn.run(create_app(), host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
