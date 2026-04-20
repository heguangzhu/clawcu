from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from clawcu.a2a.card import AgentCard

DEFAULT_TIMEOUT = 5.0
DEFAULT_SEND_TIMEOUT = 60.0


class A2AClientError(RuntimeError):
    pass


def _http_json(
    url: str,
    *,
    method: str = "GET",
    body: Any = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> tuple[int, Any]:
    data: bytes | None = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
            status = response.status
    except urllib.error.HTTPError as exc:
        raw = exc.read() or b""
        status = exc.code
    except urllib.error.URLError as exc:
        raise A2AClientError(f"request failed: {url}: {exc.reason}") from exc
    if not raw:
        return status, None
    try:
        return status, json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise A2AClientError(f"invalid JSON from {url}: {exc}") from exc


def lookup_agent(registry_url: str, name: str, *, timeout: float = DEFAULT_TIMEOUT) -> AgentCard:
    base = registry_url.rstrip("/")
    url = f"{base}/agents/{urllib.parse.quote(name, safe='')}"
    status, payload = _http_json(url, timeout=timeout)
    if status == 404:
        raise A2AClientError(f"agent '{name}' not found in registry {registry_url}")
    if status >= 400 or not isinstance(payload, dict):
        raise A2AClientError(f"registry lookup failed ({status}): {payload!r}")
    return AgentCard.from_dict(payload)


def list_agents(registry_url: str, *, timeout: float = DEFAULT_TIMEOUT) -> list[AgentCard]:
    base = registry_url.rstrip("/")
    url = f"{base}/agents"
    status, payload = _http_json(url, timeout=timeout)
    if status >= 400 or not isinstance(payload, list):
        raise A2AClientError(f"registry list failed ({status}): {payload!r}")
    return [AgentCard.from_dict(item) for item in payload]


def post_message(
    endpoint: str,
    *,
    sender: str,
    target: str,
    message: str,
    timeout: float = DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    body = {"from": sender, "to": target, "message": message}
    status, payload = _http_json(endpoint, method="POST", body=body, timeout=timeout)
    if status >= 400 or not isinstance(payload, dict):
        raise A2AClientError(f"send failed ({status}): {payload!r}")
    return payload


def send_via_registry(
    *,
    registry_url: str,
    sender: str,
    target: str,
    message: str,
    lookup_timeout: float = DEFAULT_TIMEOUT,
    send_timeout: float = DEFAULT_SEND_TIMEOUT,
) -> dict[str, Any]:
    card = lookup_agent(registry_url, target, timeout=lookup_timeout)
    return post_message(
        card.endpoint,
        sender=sender,
        target=target,
        message=message,
        timeout=send_timeout,
    )
