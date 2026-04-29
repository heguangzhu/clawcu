"""Starlette app and uvicorn entrypoint for the ClawCU A2A adapter."""

from __future__ import annotations

import logging
from typing import Any

from a2a.server.routes import create_agent_card_routes
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from .card import build_agent_card
from .executor import _GATEWAY_AUTH_TOKEN, _call_gateway, _check_gateway_ready
from .mcp_bridge import handle_mcp

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


def create_app() -> Starlette:
    """Build the Starlette application with JSON-RPC and agent-card routes."""
    agent_card = build_agent_card()

    routes = [
        *create_agent_card_routes(agent_card),
        Route("/", handle_jsonrpc, methods=["POST"]),
        Route("/mcp", handle_mcp, methods=["POST"]),
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
