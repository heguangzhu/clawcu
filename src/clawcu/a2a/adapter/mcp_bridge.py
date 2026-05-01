"""Minimal MCP bridge exposing A2A registry and peer-call tools."""

from __future__ import annotations

import os
import asyncio
import time
import urllib.parse
from typing import Any

import httpx
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from .tasks import config_from_env

CALL_TOOL_NAME = "a2a_call_peer"
CALL_ASYNC_TOOL_NAME = "a2a_call_peer_async"
GET_TASK_TOOL_NAME = "a2a_get_task"
WAIT_TASK_TOOL_NAME = "a2a_wait_task"
CANCEL_TASK_TOOL_NAME = "a2a_cancel_task"
LIST_TOOL_NAME = "a2a_list_peers"
MCP_PROTOCOL_VERSION = "2024-11-05"
DEFAULT_SEND_TIMEOUT_SECONDS = 86400.0
DEFAULT_WAIT_TIMEOUT_SECONDS = 15.0
DEFAULT_WAIT_POLL_INTERVAL_SECONDS = 2.0
TOOL_PEER_CACHE_TTL_SECONDS = 30.0
TOOL_PEER_FETCH_TIMEOUT_SECONDS = 2.0
TASK_TERMINAL_STATES = frozenset({"completed", "failed", "canceled"})
_TOOL_PEER_CACHE: dict[str, Any] = {"expires_at": 0.0, "summary": ""}


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


def _wait_timeout(arguments: dict[str, Any]) -> float:
    raw_default = os.environ.get("A2A_TASK_WAIT_TIMEOUT", str(DEFAULT_WAIT_TIMEOUT_SECONDS))
    try:
        default = float(raw_default)
    except (TypeError, ValueError):
        default = DEFAULT_WAIT_TIMEOUT_SECONDS
    cap = max(1.0, default)
    if "timeout_seconds" not in arguments:
        return cap
    try:
        requested = float(arguments["timeout_seconds"])
    except (TypeError, ValueError):
        return cap
    return min(cap, max(1.0, requested))


def _wait_poll_interval(arguments: dict[str, Any]) -> float:
    raw = arguments.get("poll_interval_seconds", DEFAULT_WAIT_POLL_INTERVAL_SECONDS)
    try:
        interval = float(raw)
    except (TypeError, ValueError):
        interval = DEFAULT_WAIT_POLL_INTERVAL_SECONDS
    return min(30.0, max(0.5, interval))


def _extract_reply(result: dict[str, Any]) -> str:
    artifacts = result.get("artifacts")
    if isinstance(artifacts, list):
        for artifact in artifacts:
            parts = artifact.get("parts") if isinstance(artifact, dict) else None
            if isinstance(parts, list):
                for part in parts:
                    if isinstance(part, dict) and isinstance(part.get("text"), str):
                        return part["text"]
    reply = _extract_message_reply(result.get("message"))
    if reply:
        return reply
    status = result.get("status")
    if isinstance(status, dict):
        reply = _extract_message_reply(status.get("message"))
        if reply:
            return reply
    return ""


def _extract_message_reply(message: Any) -> str:
    parts = message.get("parts") if isinstance(message, dict) else None
    if isinstance(parts, list):
        for part in parts:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                return part["text"]
    return ""


def _task_state(task: dict[str, Any]) -> str:
    status = task.get("status")
    state = status.get("state") if isinstance(status, dict) else None
    return state if isinstance(state, str) else ""


def _task_structured(
    target: str,
    task_id: str,
    task: dict[str, Any],
    *,
    timed_out: bool = False,
    timeout_seconds: float | None = None,
) -> dict[str, Any]:
    structured: dict[str, Any] = {
        "from": target,
        "task_id": task_id,
        "reply": _extract_reply(task),
        "task": task,
    }
    if timed_out:
        structured["timed_out"] = True
        if timeout_seconds is not None:
            structured["timeout_seconds"] = timeout_seconds
    return structured


def _task_tool_text(structured: dict[str, Any]) -> str:
    reply = structured.get("reply")
    if isinstance(reply, str) and reply:
        return reply
    task_id = str(structured.get("task_id") or "")
    target = str(structured.get("from") or "")
    task = structured.get("task")
    state = _task_state(task) if isinstance(task, dict) else ""
    text = f"Task {task_id}"
    if target:
        text += f" from {target}"
    if state:
        text += f" is {state}"
    if structured.get("timed_out"):
        timeout = structured.get("timeout_seconds")
        if isinstance(timeout, (int, float)):
            text += f" (wait timed out after {timeout:g}s)"
        else:
            text += " (wait timed out)"
        text += (
            ". Tell the user the task is still running"
            " and call a2a_wait_task again with the same to and task_id."
        )
    return text


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
    target_name = target.strip()
    quoted_target = urllib.parse.quote(target_name, safe="")
    try:
        card_resp = await client.get(
            f"{registry_url}/agents/{quoted_target}",
            headers=_registry_headers(),
        )
        card_resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code != 404:
            raise
        list_resp = await client.get(f"{registry_url}/agents", headers=_registry_headers())
        list_resp.raise_for_status()
        payload = list_resp.json()
        if not isinstance(payload, list):
            raise RuntimeError("registry returned malformed agent list") from exc
        wanted = target_name.casefold()
        for item in payload:
            if not isinstance(item, dict):
                continue
            name = item.get("name")
            if isinstance(name, str) and name.casefold() == wanted:
                return item
        raise
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


def _tool_peer_summary(peers: list[dict[str, Any]]) -> str:
    own_name = os.environ.get("A2A_AGENT_NAME", "").strip()
    visible = [peer for peer in peers if peer.get("name") != own_name]
    if not visible:
        return ""
    parts: list[str] = []
    for peer in visible[:16]:
        details: list[str] = []
        role = peer.get("role")
        if isinstance(role, str) and role:
            details.append(role)
        skills = peer.get("skills")
        if isinstance(skills, list) and skills:
            details.append("skills: " + ", ".join(str(skill) for skill in skills[:6]))
        suffix = f" ({'; '.join(details)})" if details else ""
        parts.append(f"{peer['name']}{suffix}")
    if len(visible) > 16:
        parts.append(f"...and {len(visible) - 16} more")
    return "Available peers: " + "; ".join(parts)


async def _peer_summary_for_descriptions() -> str:
    now = time.monotonic()
    cached = _TOOL_PEER_CACHE.get("summary")
    if now < float(_TOOL_PEER_CACHE.get("expires_at") or 0.0):
        return cached if isinstance(cached, str) else ""
    try:
        registry_url = _registry_url({})
        async with httpx.AsyncClient(timeout=TOOL_PEER_FETCH_TIMEOUT_SECONDS) as client:
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
        summary = _tool_peer_summary(peers)
    except Exception:
        summary = ""
    _TOOL_PEER_CACHE["summary"] = summary
    _TOOL_PEER_CACHE["expires_at"] = now + TOOL_PEER_CACHE_TTL_SECONDS
    return summary


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


async def _fetch_task(
    client: httpx.AsyncClient,
    registry_url: str,
    target: str,
    task_id: str,
    *,
    cancel: bool = False,
) -> dict[str, Any]:
    card = await _get_peer_card(client, registry_url, target)
    endpoint = _jsonrpc_endpoint(card)
    if not endpoint:
        raise ValueError(f"registry card for {target!r} has no endpoint")
    url = _task_endpoint(endpoint, task_id, cancel=cancel)
    if cancel:
        task_resp = await client.post(url, json={})
    else:
        task_resp = await client.get(url)
    task_resp.raise_for_status()
    task = task_resp.json()
    if not isinstance(task, dict):
        raise RuntimeError("peer returned malformed task")
    return task


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
        task = await _fetch_task(client, registry_url, target, task_id.strip())
    return _task_structured(target, task_id.strip(), task)


async def _wait_task(arguments: dict[str, Any]) -> dict[str, Any]:
    target = arguments.get("to")
    task_id = arguments.get("task_id")
    if not isinstance(target, str) or not target.strip():
        raise ValueError("argument 'to' is required")
    if not isinstance(task_id, str) or not task_id.strip():
        raise ValueError("argument 'task_id' is required")

    target = target.strip()
    task_id = task_id.strip()
    timeout_seconds = _wait_timeout(arguments)
    poll_interval = _wait_poll_interval(arguments)
    deadline = time.monotonic() + timeout_seconds
    registry_url = _registry_url(arguments)
    task: dict[str, Any] | None = None

    async with httpx.AsyncClient(timeout=_send_timeout(arguments)) as client:
        card = await _get_peer_card(client, registry_url, target)
        endpoint = _jsonrpc_endpoint(card)
        if not endpoint:
            raise ValueError(f"registry card for {target!r} has no endpoint")
        task_url = _task_endpoint(endpoint, task_id)
        while True:
            task_resp = await client.get(task_url)
            task_resp.raise_for_status()
            task = task_resp.json()
            if not isinstance(task, dict):
                raise RuntimeError("peer returned malformed task")
            if _task_state(task) in TASK_TERMINAL_STATES:
                return _task_structured(target, task_id, task)
            if time.monotonic() >= deadline:
                return _task_structured(
                    target,
                    task_id,
                    task,
                    timed_out=True,
                    timeout_seconds=timeout_seconds,
                )
            await asyncio.sleep(min(poll_interval, max(0.0, deadline - time.monotonic())))


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
        task = await _fetch_task(client, registry_url, target, task_id.strip(), cancel=True)
    return _task_structured(target, task_id.strip(), task)


def _description_with_peers(description: str, peer_summary: str) -> str:
    return f"{description} {peer_summary}" if peer_summary else description


def _tool_descriptor(peer_summary: str = "") -> dict[str, Any]:
    return {
        "name": CALL_TOOL_NAME,
        "description": _description_with_peers(
            "Synchronously call another local A2A agent and return its reply. Use only for quick requests expected to finish within 30 seconds. Do not use for market data, research, web access, code execution, or other nontrivial work because the MCP client may time out before the peer replies. For those requests, use a2a_call_peer_async, then a2a_wait_task. If the target name is unknown, call a2a_list_peers first.",
            peer_summary,
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "to": {"type": "string", "description": "Target agent name in the A2A registry. Matching is case-insensitive; use the peer names shown in the tool description when available."},
                "message": {"type": "string", "description": "Message to send to the target agent."},
                "registry_url": {"type": "string", "description": "Optional registry URL override."},
                "timeout_seconds": {"type": "number", "description": "Optional request timeout; values below the adapter's 24h floor are ignored."},
            },
            "required": ["to", "message"],
            "additionalProperties": False,
        },
    }


def _async_tool_descriptor(peer_summary: str = "") -> dict[str, Any]:
    return {
        "name": CALL_ASYNC_TOOL_NAME,
        "description": _description_with_peers(
            "Preferred tool for market data, research, web access, code execution, or any peer request that may take more than 30 seconds. Starts a long-running call to another local A2A agent and returns task metadata immediately. After this returns a task_id, call a2a_wait_task to wait for the final reply, or a2a_get_task to poll current status.",
            peer_summary,
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "to": {"type": "string", "description": "Target agent name in the A2A registry. Matching is case-insensitive; use the peer names shown in the tool description when available."},
                "message": {"type": "string", "description": "Message to send to the target agent."},
                "registry_url": {"type": "string", "description": "Optional registry URL override."},
                "timeout_seconds": {"type": "number", "description": "Optional request timeout; values below the adapter's 24h floor are ignored."},
            },
            "required": ["to", "message"],
            "additionalProperties": False,
        },
    }


def _wait_tool_descriptor(peer_summary: str = "") -> dict[str, Any]:
    return {
        "name": WAIT_TASK_TOOL_NAME,
        "description": _description_with_peers(
            "Wait for an asynchronous A2A peer task to reach completed, failed, or canceled. Defaults to 15 seconds to keep the user informed. When the task is completed and has a reply, this tool returns the reply directly. If it times out with working status, first tell the user the peer is still working on it, then call this tool again with the same to and task_id.",
            peer_summary,
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "to": {"type": "string", "description": "Target agent name in the A2A registry. Matching is case-insensitive; use the peer names shown in the tool description when available."},
                "task_id": {"type": "string", "description": "Peer task id returned by a2a_call_peer_async."},
                "registry_url": {"type": "string", "description": "Optional registry URL override."},
                "timeout_seconds": {"type": "number", "description": "Maximum time to wait for completion. Defaults to 15 seconds."},
                "poll_interval_seconds": {"type": "number", "description": "How often to poll task status. Defaults to 2 seconds."},
            },
            "required": ["to", "task_id"],
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
                "to": {"type": "string", "description": "Target agent name in the A2A registry. Matching is case-insensitive; use the peer names shown in the tool description when available."},
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


def _tool_descriptors(peer_summary: str = "") -> list[dict[str, Any]]:
    tools = [_tool_descriptor(peer_summary)]
    if config_from_env().enabled:
        tools.extend(
            [
                _async_tool_descriptor(peer_summary),
                _wait_tool_descriptor(peer_summary),
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
        peer_summary = await _peer_summary_for_descriptions()
        return _rpc_result(
            rpc_id,
            {"tools": _tool_descriptors(peer_summary)},
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
            WAIT_TASK_TOOL_NAME,
            CANCEL_TASK_TOOL_NAME,
            LIST_TOOL_NAME,
        }:
            return _rpc_error(rpc_id, -32602, f"unknown tool: {params.get('name') if isinstance(params, dict) else None}")
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            return _rpc_error(rpc_id, -32602, "tool arguments must be an object")
        if tool_name in {CALL_ASYNC_TOOL_NAME, GET_TASK_TOOL_NAME, WAIT_TASK_TOOL_NAME, CANCEL_TASK_TOOL_NAME} and not config_from_env().enabled:
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
            text = (
                f"Submitted task {structured['task_id']}. "
                f"Call a2a_wait_task with to={structured['from']} and task_id={structured['task_id']} to wait for the final reply. If it reports the task is still working, call a2a_wait_task again with the same task_id."
                if structured["task_id"]
                else "Submitted asynchronous A2A task. Call a2a_get_task or a2a_wait_task with the returned task id to read the result."
            )
            return _rpc_result(
                rpc_id,
                {
                    "content": [{"type": "text", "text": text}],
                    "isError": False,
                    "structuredContent": structured,
                },
            )
        if tool_name in {GET_TASK_TOOL_NAME, WAIT_TASK_TOOL_NAME, CANCEL_TASK_TOOL_NAME}:
            try:
                if tool_name == GET_TASK_TOOL_NAME:
                    structured = await _get_task(arguments)
                elif tool_name == WAIT_TASK_TOOL_NAME:
                    structured = await _wait_task(arguments)
                else:
                    structured = await _cancel_task(arguments)
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
            text = _task_tool_text(structured)
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
