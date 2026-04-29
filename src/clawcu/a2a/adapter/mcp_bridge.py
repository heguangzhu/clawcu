"""Minimal MCP bridge exposing ``a2a_call_peer`` from the A2A adapter."""

from __future__ import annotations

import os
import urllib.parse
from typing import Any

import httpx
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

TOOL_NAME = "a2a_call_peer"
MCP_PROTOCOL_VERSION = "2024-11-05"


def _rpc_result(rpc_id: Any, result: Any) -> JSONResponse:
    return JSONResponse({"jsonrpc": "2.0", "id": rpc_id, "result": result})


def _rpc_error(rpc_id: Any, code: int, message: str, data: Any = None) -> JSONResponse:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return JSONResponse({"jsonrpc": "2.0", "id": rpc_id, "error": error})


def _registry_url(arguments: dict[str, Any]) -> str:
    raw = arguments.get("registry_url") or os.environ.get("A2A_REGISTRY_URL")
    if isinstance(raw, str) and raw.strip():
        return raw.rstrip("/")
    return "http://host.docker.internal:9100"


def _registry_headers() -> dict[str, str]:
    token = os.environ.get("A2A_REGISTRY_TOKEN", "").strip()
    return {"Authorization": f"Bearer {token}"} if token else {}


def _extract_reply(result: dict[str, Any]) -> str:
    artifacts = result.get("artifacts")
    if isinstance(artifacts, list):
        for artifact in artifacts:
            parts = artifact.get("parts") if isinstance(artifact, dict) else None
            if isinstance(parts, list):
                for part in parts:
                    if isinstance(part, dict) and isinstance(part.get("text"), str):
                        return part["text"]
    status = result.get("status")
    if isinstance(status, dict):
        message = status.get("message")
        parts = message.get("parts") if isinstance(message, dict) else None
        if isinstance(parts, list):
            for part in parts:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    return part["text"]
    return ""


async def _call_peer(arguments: dict[str, Any]) -> dict[str, Any]:
    target = arguments.get("to")
    message = arguments.get("message")
    if not isinstance(target, str) or not target.strip():
        raise ValueError("argument 'to' is required")
    if not isinstance(message, str) or not message:
        raise ValueError("argument 'message' is required")

    timeout = float(arguments.get("timeout_seconds") or os.environ.get("A2A_SEND_TIMEOUT", "300"))
    registry_url = _registry_url(arguments)
    sender = os.environ.get("A2A_AGENT_NAME", "agent")
    async with httpx.AsyncClient(timeout=timeout) as client:
        quoted_target = urllib.parse.quote(target.strip(), safe="")
        card_resp = await client.get(
            f"{registry_url}/agents/{quoted_target}",
            headers=_registry_headers(),
        )
        card_resp.raise_for_status()
        card = card_resp.json()
        endpoint = card.get("endpoint")
        if not isinstance(endpoint, str) or not endpoint:
            interfaces = card.get("supported_interfaces") or []
            if interfaces and isinstance(interfaces[0], dict):
                endpoint = interfaces[0].get("url")
        if not isinstance(endpoint, str) or not endpoint:
            raise ValueError(f"registry card for {target!r} has no endpoint")

        rpc_body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "message/send",
            "params": {
                "message": {
                    "role": "user",
                    "parts": [{"type": "text", "text": message}],
                },
            },
        }
        send_resp = await client.post(endpoint, json=rpc_body)
        send_resp.raise_for_status()
        payload = send_resp.json()
        if isinstance(payload.get("error"), dict):
            raise RuntimeError(payload["error"].get("message") or "peer returned JSON-RPC error")
        result = payload.get("result") if isinstance(payload.get("result"), dict) else payload
        if not isinstance(result, dict):
            raise RuntimeError("peer returned malformed JSON-RPC result")

    reply = _extract_reply(result)
    return {
        "from": target,
        "to": target,
        "caller": sender,
        "reply": reply,
        "task": result,
    }


def _tool_descriptor() -> dict[str, Any]:
    return {
        "name": TOOL_NAME,
        "description": "Call another local A2A agent and return its reply.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "to": {"type": "string", "description": "Target agent name in the A2A registry."},
                "message": {"type": "string", "description": "Message to send to the target agent."},
                "registry_url": {"type": "string", "description": "Optional registry URL override."},
                "timeout_seconds": {"type": "number", "description": "Optional request timeout."},
            },
            "required": ["to", "message"],
            "additionalProperties": False,
        },
    }


async def handle_mcp(request: Request) -> Response:
    try:
        payload = await request.json()
    except Exception:
        return _rpc_error(None, -32700, "Parse error")
    if not isinstance(payload, dict):
        return _rpc_error(None, -32600, "Invalid Request")

    rpc_id = payload.get("id")
    method = payload.get("method")
    if method == "notifications/initialized":
        return Response(status_code=202)
    if method == "initialize":
        return _rpc_result(
            rpc_id,
            {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "clawcu-a2a-adapter", "version": "0.1.0"},
            },
        )
    if method == "ping":
        return _rpc_result(rpc_id, {})
    if method == "tools/list":
        return _rpc_result(rpc_id, {"tools": [_tool_descriptor()]})
    if method == "tools/call":
        params = payload.get("params") or {}
        if not isinstance(params, dict) or params.get("name") != TOOL_NAME:
            return _rpc_error(rpc_id, -32602, f"unknown tool: {params.get('name') if isinstance(params, dict) else None}")
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            return _rpc_error(rpc_id, -32602, "tool arguments must be an object")
        try:
            structured = await _call_peer(arguments)
        except ValueError as exc:
            return _rpc_error(rpc_id, -32602, str(exc))
        except httpx.HTTPStatusError as exc:
            return _rpc_error(
                rpc_id,
                -32000,
                f"HTTP error from A2A peer or registry: {exc.response.status_code}",
            )
        except Exception as exc:
            return _rpc_error(rpc_id, -32000, str(exc))
        return _rpc_result(
            rpc_id,
            {
                "content": [{"type": "text", "text": structured["reply"]}],
                "isError": False,
                "structuredContent": structured,
            },
        )
    return _rpc_error(rpc_id, -32601, f"Method not found: {method}")
