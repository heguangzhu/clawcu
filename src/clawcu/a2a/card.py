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


def card_from_record(record: Any, *, host: str = "127.0.0.1") -> AgentCard:
    name = getattr(record, "name", None)
    service = getattr(record, "service", "") or ""
    if not isinstance(name, str) or not name:
        raise ValueError("record.name is required to build an AgentCard")
    return AgentCard(
        name=name,
        role=role_for_service(service),
        skills=skills_for_service(service),
        endpoint=bridge_endpoint_for(record, host=host),
    )
