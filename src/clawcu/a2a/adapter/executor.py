"""GatewayExecutor — bridges A2A tasks to the service's chat/completions API."""

from __future__ import annotations

import json
import logging
import os
from typing import Any

import httpx

try:
    from a2a.helpers.proto_helpers import new_text_part
    from a2a.server.agent_execution import AgentExecutor, RequestContext
    from a2a.server.events import EventQueue
    from a2a.server.tasks import TaskUpdater
except ModuleNotFoundError as exc:
    if exc.name != "a2a" and not (exc.name or "").startswith("a2a."):
        raise
    new_text_part = None
    AgentExecutor = object
    RequestContext = Any
    EventQueue = Any
    TaskUpdater = None

log = logging.getLogger("clawcu-a2a-adapter")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_GATEWAY_URL = os.environ.get("A2A_GATEWAY_URL", "http://127.0.0.1:18789")
_GATEWAY_AUTH_TOKEN = os.environ.get("A2A_GATEWAY_AUTH_TOKEN", "")
_GATEWAY_TIMEOUT = float(os.environ.get("A2A_GATEWAY_TIMEOUT", "86400"))
_GATEWAY_READY_PATH = os.environ.get("A2A_GATEWAY_READY_PATH", "/healthz")


async def _call_gateway(text: str, auth_token: str) -> str:
    """POST to the service gateway's ``/v1/chat/completions`` and return the reply."""
    headers: dict[str, str] = {"content-type": "application/json"}
    if auth_token:
        headers["authorization"] = f"Bearer {auth_token}"

    body = {
        "messages": [{"role": "user", "content": text}],
        "stream": False,
    }

    async with httpx.AsyncClient(timeout=_GATEWAY_TIMEOUT) as client:
        resp = await client.post(
            f"{_GATEWAY_URL}/v1/chat/completions",
            headers=headers,
            json=body,
        )
        resp.raise_for_status()
        data = resp.json()

    choices = data.get("choices") or []
    if choices:
        return choices[0].get("message", {}).get("content", "")
    return ""


async def _check_gateway_ready() -> bool:
    """Probe the gateway's readiness endpoint."""
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(f"{_GATEWAY_URL}{_GATEWAY_READY_PATH}")
            return resp.status_code == 200
    except Exception:
        return False


# ---------------------------------------------------------------------------
# GatewayExecutor
# ---------------------------------------------------------------------------

class GatewayExecutor(AgentExecutor):
    """AgentExecutor that forwards A2A messages to the co-located service gateway."""

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        if TaskUpdater is None or new_text_part is None:
            raise RuntimeError(
                "GatewayExecutor requires the optional a2a-sdk dependency. "
                "Install it with `pip install clawcu[a2a]`."
            )
        updater = TaskUpdater(event_queue, context.task_id, context.context_id)
        user_text = context.get_user_input()

        if not user_text:
            await updater.failed(message="Empty message")
            return

        if not await _check_gateway_ready():
            await updater.failed(message="Gateway not ready")
            return

        try:
            reply = await _call_gateway(user_text, _GATEWAY_AUTH_TOKEN)
            if not reply:
                await updater.failed(message="Empty gateway reply")
                return

            msg = updater.new_agent_message([new_text_part(reply)])
            await event_queue.enqueue_event(msg)
        except Exception as exc:
            log.exception("gateway call failed")
            await updater.failed(message=f"Gateway error: {exc}")

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        if TaskUpdater is None:
            raise RuntimeError(
                "GatewayExecutor requires the optional a2a-sdk dependency. "
                "Install it with `pip install clawcu[a2a]`."
            )
        updater = TaskUpdater(event_queue, context.task_id, context.context_id)
        await updater.cancel()
