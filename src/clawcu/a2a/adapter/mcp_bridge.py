"""Minimal MCP bridge exposing A2A registry and peer-call tools."""

from __future__ import annotations

import os
import urllib.parse
from typing import Any

import httpx
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from .tasks import config_from_env

CALL_TOOL_NAME = "a2a_call_peer"
CALL_ASYNC_TOOL_NAME = "a2a_call_peer_async"
GET_TASK_TOOL_NAME = "a2a_get_task"
CANCEL_TASK_TOOL_NAME = "a2a_cancel_task"
LIST_TOOL_NAME = "a2a_list_peers"
MCP_PROTOCOL_VERSION = "2024-11-05"
DEFAULT_SEND_TIMEOUT_SECONDS = 86400.0


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


def _send_timeout(arguments: dict[str, Any]) -> float:
    raw_floor = os.environ.get("A2A_SEND_TIMEOUT", str(DEFAULT_SEND_TIMEOUT_SECONDS))
    try:
        timeout = max(DEFAULT_SEND_TIMEOUT_SECONDS, float(raw_floor))
    except (TypeError, ValueError):
        timeout = DEFAULT_SEND_TIMEOUT_SECONDS

    if "timeout_seconds" not in arguments:
        return timeout
    try:
        requested = float(arguments["timeout_seconds"])
    except (TypeError, ValueError):
        return timeout
    return max(timeout, requested)


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


def _interface_endpoint(card: dict[str, Any]) -> str:
    endpoint = card.get("endpoint")
    if isinstance(endpoint, str):
        return endpoint
    interfaces = card.get("supported_interfaces") or card.get("supportedInterfaces") or []
    if isinstance(interfaces, list):
        for interface in interfaces:
            if not isinstance(interface, dict):
                continue
            url = interface.get("url")
            if isinstance(url, str) and url:
                return url
    return ""


def _netloc_with_port(parsed: urllib.parse.SplitResult, port: int) -> str:
    hostname = parsed.hostname or ""
    if ":" in hostname and not hostname.startswith("["):
        hostname = f"[{hostname}]"
    if parsed.username:
        auth = urllib.parse.quote(parsed.username, safe="")
        if parsed.password:
            auth += ":" + urllib.parse.quote(parsed.password, safe="")
        hostname = f"{auth}@{hostname}"
    return f"{hostname}:{port}"


def _jsonrpc_endpoint(card: dict[str, Any]) -> str:
    endpoint = _interface_endpoint(card)
    if not endpoint:
        return ""
    try:
        parsed = urllib.parse.urlsplit(endpoint)
    except ValueError:
        return endpoint
    path = parsed.path.rstrip("/")
    if path != "/a2a/send":
        return endpoint

    # Compatibility with older ClawCU registries that published the
    # pre-0.4 sidecar endpoint. In the companion-adapter layout, OpenClaw's
    # A2A adapter is exposed on the neighboring host port and speaks
    # JSON-RPC at "/".
    role = str(card.get("role") or card.get("description") or "").lower()
    port = parsed.port
    if "openclaw" in role and port is not None:
        return parsed._replace(
            netloc=_netloc_with_port(parsed, port + 1),
            path="",
            query="",
            fragment="",
        ).geturl()
    return parsed._replace(path="", query="", fragment="").geturl()


def _task_endpoint(jsonrpc_endpoint: str, task_id: str, *, cancel: bool = False) -> str:
    parsed = urllib.parse.urlsplit(jsonrpc_endpoint)
    task_path = f"/tasks/{urllib.parse.quote(task_id, safe='')}"
    if cancel:
        task_path += "/cancel"
    return parsed._replace(path=task_path, query="", fragment="").geturl()


def _skill_names(raw_skills: Any) -> list[str]:
    skills: list[str] = []
    if not isinstance(raw_skills, list):
        return skills
    for item in raw_skills:
        if isinstance(item, str) and item:
            skills.append(item)
            continue
        if not isinstance(item, dict):
            continue
        name = item.get("name") or item.get("id")
        if isinstance(name, str) and name:
            skills.append(name)
        tags = item.get("tags")
        if isinstance(tags, list):
            skills.extend(tag for tag in tags if isinstance(tag, str) and tag)
    return sorted(set(skills))


def _normalize_peer(card: dict[str, Any]) -> dict[str, Any] | None:
    name = card.get("name")
    if not isinstance(name, str) or not name:
        return None
    role = card.get("role") or card.get("description") or ""
    protocol = card.get("protocol")
    if not isinstance(protocol, list):
        protocol = []
    return {
        "name": name,
        "role": role if isinstance(role, str) else "",
        "skills": _skill_names(card.get("skills")),
        "endpoint": _interface_endpoint(card),
        "protocol": [item for item in protocol if isinstance(item, str)],
    }


async def _list_peers(arguments: dict[str, Any]) -> dict[str, Any]:
    timeout = _send_timeout(arguments)
    registry_url = _registry_url(arguments)
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.get(f"{registry_url}/agents", headers=_registry_headers())
        resp.raise_for_status()
        payload = resp.json()
    if not isinstance(payload, list):
        raise RuntimeError("registry returned malformed agent list")
    peers = [
        peer
        for item in payload
        if isinstance(item, dict)
        for peer in [_normalize_peer(item)]
        if peer is not None
    ]
    return {"registry_url": registry_url, "peers": peers}


async def _get_peer_card(
    client: httpx.AsyncClient,
    registry_url: str,
    target: str,
) -> dict[str, Any]:
    quoted_target = urllib.parse.quote(target.strip(), safe="")
    card_resp = await client.get(
        f"{registry_url}/agents/{quoted_target}",
        headers=_registry_headers(),
    )
    card_resp.raise_for_status()
    card = card_resp.json()
    if not isinstance(card, dict):
        raise RuntimeError(f"registry card for {target!r} is malformed")
    return card


def _peers_text(peers: list[dict[str, Any]]) -> str:
    if not peers:
        return "No A2A peers are registered."
    lines = ["Registered A2A peers:"]
    for peer in peers:
        detail_parts: list[str] = []
        if peer.get("role"):
            detail_parts.append(str(peer["role"]))
        skills = peer.get("skills")
        if isinstance(skills, list) and skills:
            detail_parts.append("skills: " + ", ".join(str(skill) for skill in skills))
        suffix = f" ({'; '.join(detail_parts)})" if detail_parts else ""
        lines.append(f"- {peer['name']}{suffix}")
    return "\n".join(lines)


async def _call_peer(arguments: dict[str, Any]) -> dict[str, Any]:
    target = arguments.get("to")
    message = arguments.get("message")
    if not isinstance(target, str) or not target.strip():
        raise ValueError("argument 'to' is required")
    if not isinstance(message, str) or not message:
        raise ValueError("argument 'message' is required")

    timeout = _send_timeout(arguments)
    registry_url = _registry_url(arguments)
    sender = os.environ.get("A2A_AGENT_NAME", "agent")
    async with httpx.AsyncClient(timeout=timeout) as client:
        card = await _get_peer_card(client, registry_url, target)
        endpoint = _jsonrpc_endpoint(card)
        if not endpoint:
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
                "configuration": {"blocking": True},
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


async def _call_peer_async(arguments: dict[str, Any]) -> dict[str, Any]:
    target = arguments.get("to")
    message = arguments.get("message")
    if not isinstance(target, str) or not target.strip():
        raise ValueError("argument 'to' is required")
    if not isinstance(message, str) or not message:
        raise ValueError("argument 'message' is required")

    timeout = _send_timeout(arguments)
    registry_url = _registry_url(arguments)
    sender = os.environ.get("A2A_AGENT_NAME", "agent")
    async with httpx.AsyncClient(timeout=timeout) as client:
        card = await _get_peer_card(client, registry_url, target)
        endpoint = _jsonrpc_endpoint(card)
        if not endpoint:
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
                "configuration": {"blocking": False},
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

    task_id = result.get("id") or result.get("taskId")
    return {
        "from": target,
        "to": target,
        "caller": sender,
        "task_id": task_id if isinstance(task_id, str) else "",
        "task": result,
    }


async def _get_task(arguments: dict[str, Any]) -> dict[str, Any]:
    target = arguments.get("to")
    task_id = arguments.get("task_id")
    if not isinstance(target, str) or not target.strip():
        raise ValueError("argument 'to' is required")
    if not isinstance(task_id, str) or not task_id.strip():
        raise ValueError("argument 'task_id' is required")

    timeout = _send_timeout(arguments)
    registry_url = _registry_url(arguments)
    async with httpx.AsyncClient(timeout=timeout) as client:
        card = await _get_peer_card(client, registry_url, target)
        endpoint = _jsonrpc_endpoint(card)
        if not endpoint:
            raise ValueError(f"registry card for {target!r} has no endpoint")
        task_resp = await client.get(_task_endpoint(endpoint, task_id.strip()))
        task_resp.raise_for_status()
        task = task_resp.json()
        if not isinstance(task, dict):
            raise RuntimeError("peer returned malformed task")
    return {"from": target, "task_id": task_id.strip(), "task": task}


async def _cancel_task(arguments: dict[str, Any]) -> dict[str, Any]:
    target = arguments.get("to")
    task_id = arguments.get("task_id")
    if not isinstance(target, str) or not target.strip():
        raise ValueError("argument 'to' is required")
    if not isinstance(task_id, str) or not task_id.strip():
        raise ValueError("argument 'task_id' is required")

    timeout = _send_timeout(arguments)
    registry_url = _registry_url(arguments)
    async with httpx.AsyncClient(timeout=timeout) as client:
        card = await _get_peer_card(client, registry_url, target)
        endpoint = _jsonrpc_endpoint(card)
        if not endpoint:
            raise ValueError(f"registry card for {target!r} has no endpoint")
        task_resp = await client.post(_task_endpoint(endpoint, task_id.strip(), cancel=True), json={})
        task_resp.raise_for_status()
        task = task_resp.json()
        if not isinstance(task, dict):
            raise RuntimeError("peer returned malformed task")
    return {"from": target, "task_id": task_id.strip(), "task": task}


def _tool_descriptor() -> dict[str, Any]:
    return {
        "name": CALL_TOOL_NAME,
        "description": "Call another local A2A agent and return its reply. If the target name is unknown, call a2a_list_peers first.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "to": {"type": "string", "description": "Target agent name in the A2A registry."},
                "message": {"type": "string", "description": "Message to send to the target agent."},
                "registry_url": {"type": "string", "description": "Optional registry URL override."},
                "timeout_seconds": {"type": "number", "description": "Optional request timeout; values below the adapter's 24h floor are ignored."},
            },
            "required": ["to", "message"],
            "additionalProperties": False,
        },
    }


def _async_tool_descriptor() -> dict[str, Any]:
    return {
        "name": CALL_ASYNC_TOOL_NAME,
        "description": "Start an asynchronous call to another local A2A agent and return the task metadata.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "to": {"type": "string", "description": "Target agent name in the A2A registry."},
                "message": {"type": "string", "description": "Message to send to the target agent."},
                "registry_url": {"type": "string", "description": "Optional registry URL override."},
                "timeout_seconds": {"type": "number", "description": "Optional request timeout; values below the adapter's 24h floor are ignored."},
            },
            "required": ["to", "message"],
            "additionalProperties": False,
        },
    }


def _task_tool_descriptor(name: str, description: str) -> dict[str, Any]:
    return {
        "name": name,
        "description": description,
        "inputSchema": {
            "type": "object",
            "properties": {
                "to": {"type": "string", "description": "Target agent name in the A2A registry."},
                "task_id": {"type": "string", "description": "Peer task id."},
                "registry_url": {"type": "string", "description": "Optional registry URL override."},
                "timeout_seconds": {"type": "number", "description": "Optional request timeout; values below the adapter's 24h floor are ignored."},
            },
            "required": ["to", "task_id"],
            "additionalProperties": False,
        },
    }


def _list_tool_descriptor() -> dict[str, Any]:
    return {
        "name": LIST_TOOL_NAME,
        "description": "List A2A agents registered in the local A2A registry.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "registry_url": {"type": "string", "description": "Optional registry URL override."},
                "timeout_seconds": {"type": "number", "description": "Optional request timeout; values below the adapter's 24h floor are ignored."},
            },
            "additionalProperties": False,
        },
    }


def _tool_descriptors() -> list[dict[str, Any]]:
    tools = [_tool_descriptor()]
    if config_from_env().enabled:
        tools.extend(
            [
                _async_tool_descriptor(),
                _task_tool_descriptor(GET_TASK_TOOL_NAME, "Fetch an asynchronous A2A peer task by id."),
                _task_tool_descriptor(CANCEL_TASK_TOOL_NAME, "Cancel an asynchronous A2A peer task by id."),
            ]
        )
    tools.append(_list_tool_descriptor())
    return tools


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
        return _rpc_result(
            rpc_id,
            {"tools": _tool_descriptors()},
        )
    if method == "tools/call":
        params = payload.get("params") or {}
        if not isinstance(params, dict):
            return _rpc_error(rpc_id, -32602, "tool params must be an object")
        tool_name = params.get("name")
        if tool_name not in {
            CALL_TOOL_NAME,
            CALL_ASYNC_TOOL_NAME,
            GET_TASK_TOOL_NAME,
            CANCEL_TASK_TOOL_NAME,
            LIST_TOOL_NAME,
        }:
            return _rpc_error(rpc_id, -32602, f"unknown tool: {params.get('name') if isinstance(params, dict) else None}")
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            return _rpc_error(rpc_id, -32602, "tool arguments must be an object")
        if tool_name in {CALL_ASYNC_TOOL_NAME, GET_TASK_TOOL_NAME, CANCEL_TASK_TOOL_NAME} and not config_from_env().enabled:
            return _rpc_error(
                rpc_id,
                -32000,
                "async A2A tools are disabled; unset A2A_ASYNC_ENABLED or set it to true",
            )
        if tool_name == LIST_TOOL_NAME:
            try:
                structured = await _list_peers(arguments)
            except httpx.HTTPStatusError as exc:
                return _rpc_error(
                    rpc_id,
                    -32000,
                    f"HTTP error from A2A registry: {exc.response.status_code}",
                )
            except Exception as exc:
                return _rpc_error(rpc_id, -32000, str(exc) or exc.__class__.__name__)
            return _rpc_result(
                rpc_id,
                {
                    "content": [{"type": "text", "text": _peers_text(structured["peers"])}],
                    "isError": False,
                    "structuredContent": structured,
                },
            )
        if tool_name == CALL_ASYNC_TOOL_NAME:
            try:
                structured = await _call_peer_async(arguments)
            except ValueError as exc:
                return _rpc_error(rpc_id, -32602, str(exc))
            except httpx.HTTPStatusError as exc:
                return _rpc_error(
                    rpc_id,
                    -32000,
                    f"HTTP error from A2A peer or registry: {exc.response.status_code}",
                )
            except Exception as exc:
                return _rpc_error(rpc_id, -32000, str(exc) or exc.__class__.__name__)
            text = f"Submitted task {structured['task_id']}" if structured["task_id"] else "Submitted asynchronous A2A task"
            return _rpc_result(
                rpc_id,
                {
                    "content": [{"type": "text", "text": text}],
                    "isError": False,
                    "structuredContent": structured,
                },
            )
        if tool_name in {GET_TASK_TOOL_NAME, CANCEL_TASK_TOOL_NAME}:
            try:
                structured = await (_get_task(arguments) if tool_name == GET_TASK_TOOL_NAME else _cancel_task(arguments))
            except ValueError as exc:
                return _rpc_error(rpc_id, -32602, str(exc))
            except httpx.HTTPStatusError as exc:
                return _rpc_error(
                    rpc_id,
                    -32000,
                    f"HTTP error from A2A peer or registry: {exc.response.status_code}",
                )
            except Exception as exc:
                return _rpc_error(rpc_id, -32000, str(exc) or exc.__class__.__name__)
            status = structured["task"].get("status")
            text = f"Task {structured['task_id']}"
            if isinstance(status, dict) and isinstance(status.get("state"), str):
                text += f" is {status['state']}"
            return _rpc_result(
                rpc_id,
                {
                    "content": [{"type": "text", "text": text}],
                    "isError": False,
                    "structuredContent": structured,
                },
            )
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
            return _rpc_error(rpc_id, -32000, str(exc) or exc.__class__.__name__)
        return _rpc_result(
            rpc_id,
            {
                "content": [{"type": "text", "text": structured["reply"]}],
                "isError": False,
                "structuredContent": structured,
            },
        )
    return _rpc_error(rpc_id, -32601, f"Method not found: {method}")
