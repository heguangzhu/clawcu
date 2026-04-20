from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any

DEFAULT_REGISTRY_PORT = 9100
DEFAULT_BRIDGE_PORT = 19100

_SERVICE_SKILLS: dict[str, list[str]] = {
    "openclaw": ["chat", "tools"],
    "hermes": ["chat", "analysis"],
}

_SERVICE_ROLES: dict[str, str] = {
    "openclaw": "OpenClaw local assistant",
    "hermes": "Hermes local analyst",
}

# Fallback for when the adapter pipeline cannot be reached — design-2 D5
# anchors these as the canonical plugin-exposure ports. Kept in sync with
# the adapter defaults manually; prefer display_port_for_record when a
# ClawCUService is in hand.
_SERVICE_DEFAULT_DISPLAY_PORT: dict[str, int] = {
    "openclaw": 18819,
    "hermes": 9129,
}

# OpenClaw's container occupies display_port with its gateway UI, so the
# plugin sidecar binds display_port + 1 (see proto/openclaw-plugin/INSTALL.md).
# Hermes binds display_port directly. Order matters: probed in sequence,
# first hit wins, so the "true plugin" path precedes the sidecar fallback.
_SERVICE_PLUGIN_PORT_OFFSETS: dict[str, tuple[int, ...]] = {
    "openclaw": (0, 1),
    "hermes": (0,),
}


@dataclass(frozen=True)
class AgentCard:
    name: str
    role: str
    skills: list[str] = field(default_factory=list)
    endpoint: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self, *, indent: int | None = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, sort_keys=True)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AgentCard":
        missing = {"name", "role", "skills", "endpoint"} - data.keys()
        if missing:
            raise ValueError(f"AgentCard missing fields: {sorted(missing)}")
        skills = list(data["skills"])
        if not all(isinstance(s, str) and s for s in skills):
            raise ValueError("AgentCard.skills must be non-empty strings")
        name = data["name"]
        role = data["role"]
        endpoint = data["endpoint"]
        if not (isinstance(name, str) and name):
            raise ValueError("AgentCard.name must be a non-empty string")
        if not (isinstance(role, str) and role):
            raise ValueError("AgentCard.role must be a non-empty string")
        if not (isinstance(endpoint, str) and endpoint):
            raise ValueError("AgentCard.endpoint must be a non-empty string")
        return cls(name=name, role=role, skills=skills, endpoint=endpoint)

    @classmethod
    def from_json(cls, payload: str) -> "AgentCard":
        return cls.from_dict(json.loads(payload))


def skills_for_service(service: str) -> list[str]:
    return list(_SERVICE_SKILLS.get(service, ["chat"]))


def role_for_service(service: str) -> str:
    return _SERVICE_ROLES.get(service, f"{service} local agent")


def bridge_port_for(record: Any) -> int:
    port = getattr(record, "port", None)
    if isinstance(port, int) and port > 0:
        return port + 1000
    return DEFAULT_BRIDGE_PORT


def bridge_endpoint_for(record: Any, *, host: str = "127.0.0.1") -> str:
    return f"http://{host}:{bridge_port_for(record)}/a2a/send"


def display_port_for_record(record: Any, *, service: Any = None) -> int:
    """Resolve the port a plugin would expose on.

    Prefers ``service.adapter_for_record(record).display_port(service, record)``
    when a ClawCUService is in hand; falls back to the service-type default
    map and finally to ``record.port``. The fallback keeps unit tests that
    pass lightweight fakes working without instantiating the full service.
    """
    if service is not None:
        try:
            adapter = service.adapter_for_record(record)
            return int(adapter.display_port(service, record))
        except Exception:  # noqa: BLE001 — best-effort, fall back
            pass
    service_name = getattr(record, "service", "") or ""
    default = _SERVICE_DEFAULT_DISPLAY_PORT.get(service_name)
    if default is not None:
        return default
    port = getattr(record, "port", None)
    if isinstance(port, int) and port > 0:
        return port
    return DEFAULT_BRIDGE_PORT


def plugin_port_candidates(record: Any, *, service: Any = None) -> list[int]:
    """Ports to probe when discovering a plugin's self-reported AgentCard.

    Callers should try these in order and take the first live card. OpenClaw
    returns ``[display_port, display_port + 1]`` because the container itself
    occupies display_port with its gateway UI and the Node sidecar binds the
    neighbor port; Hermes returns just ``[display_port]``. Duplicates are
    removed while preserving order so an adapter-reported port that already
    matches the sidecar slot doesn't get probed twice.
    """
    base = display_port_for_record(record, service=service)
    service_name = getattr(record, "service", "") or ""
    offsets = _SERVICE_PLUGIN_PORT_OFFSETS.get(service_name, (0,))
    ordered: list[int] = []
    for offset in offsets:
        port = base + offset
        if port not in ordered:
            ordered.append(port)
    return ordered


def plugin_endpoint_for(
    record: Any,
    *,
    service: Any = None,
    host: str = "127.0.0.1",
) -> str:
    return f"http://{host}:{display_port_for_record(record, service=service)}/a2a/send"


def card_from_record(
    record: Any,
    *,
    service: Any = None,
    host: str = "127.0.0.1",
) -> AgentCard:
    name = getattr(record, "name", None)
    svc = getattr(record, "service", "") or ""
    if not isinstance(name, str) or not name:
        raise ValueError("record.name is required to build an AgentCard")
    return AgentCard(
        name=name,
        role=role_for_service(svc),
        skills=skills_for_service(svc),
        endpoint=plugin_endpoint_for(record, service=service, host=host),
    )
