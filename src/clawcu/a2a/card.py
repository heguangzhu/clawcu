from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any

DEFAULT_REGISTRY_PORT = 9100
DEFAULT_BRIDGE_PORT = 19100

# Review-1 §3: these per-service tables are now a fallback used only when
# the caller cannot provide a ``ClawCUService`` (lightweight unit tests
# that fake records without a full service instance). The authoritative
# source of these values is each ``ServiceAdapter`` subclass — see
# ``a2a_skills`` / ``a2a_role`` / ``a2a_plugin_port_offsets``. Adding a
# third service should not require editing this module.
_SERVICE_SKILLS: dict[str, list[str]] = {
    "openclaw": ["chat", "tools"],
    "hermes": ["chat", "analysis"],
}

_SERVICE_ROLES: dict[str, str] = {
    "openclaw": "OpenClaw local assistant",
    "hermes": "Hermes local analyst",
}

_SERVICE_DEFAULT_DISPLAY_PORT: dict[str, int] = {
    "openclaw": 18819,
    "hermes": 9129,
}

_SERVICE_PLUGIN_PORT_OFFSETS: dict[str, tuple[int, ...]] = {
    "openclaw": (0, 1),
    "hermes": (0,),
}


def _adapter_for(service: Any, service_name: str) -> Any | None:
    """Return the adapter for ``service_name`` via a ClawCUService handle,
    or ``None`` if unavailable. Best-effort — falls back silently so unit
    tests that pass minimal stubs still work."""
    if service is None or not service_name:
        return None
    for attr in ("adapter_for_service", "adapter_for_record"):
        method = getattr(service, attr, None)
        if not callable(method):
            continue
        try:
            if attr == "adapter_for_service":
                return method(service_name)
        except Exception:  # noqa: BLE001 — best-effort
            return None
    return None


@dataclass(frozen=True)
class AgentCard:
    name: str
    role: str
    skills: list[str] = field(default_factory=list)
    endpoint: str = ""
    # Review-1 §15: optional protocol-version advertisement, e.g.
    # ``["a2a/v0.1"]``. ``None`` means "unspecified" (pre-§15 card); peers
    # that care may treat it as implicit v0.1. Omitted from ``to_dict``
    # when unset so the wire format is byte-identical to the pre-§15
    # schema for cards that haven't opted in.
    protocol: list[str] | None = None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        if data.get("protocol") is None:
            data.pop("protocol", None)
        return data

    def to_json(self, *, indent: int | None = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, sort_keys=True)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AgentCard":
        # Review-1 §2: forward-compatibility — accept unknown fields and
        # quietly drop them. A newer peer that adds ``pub_key`` or
        # ``capabilities`` must not make an older client raise ValueError.
        # Strict schema enforcement is a non-goal for a forward-compat
        # protocol; callers that want stricter validation layer it above.
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
        protocol = data.get("protocol")
        if protocol is not None and not (
            isinstance(protocol, list)
            and all(isinstance(p, str) and p for p in protocol)
        ):
            raise ValueError("AgentCard.protocol must be a list of non-empty strings")
        return cls(
            name=name,
            role=role,
            skills=skills,
            endpoint=endpoint,
            protocol=protocol,
        )

    @classmethod
    def from_json(cls, payload: str) -> "AgentCard":
        return cls.from_dict(json.loads(payload))


def skills_for_service(service_name: str, *, service: Any = None) -> list[str]:
    """Return the A2A ``skills`` list advertised for this service type.

    Prefers ``adapter.a2a_skills`` when a ``ClawCUService`` handle is
    provided; falls back to the module-level table otherwise.
    """
    adapter = _adapter_for(service, service_name)
    if adapter is not None:
        skills = getattr(adapter, "a2a_skills", None)
        if skills:
            return list(skills)
    return list(_SERVICE_SKILLS.get(service_name, ["chat"]))


def role_for_service(service_name: str, *, service: Any = None) -> str:
    """Return the A2A ``role`` string for this service type."""
    adapter = _adapter_for(service, service_name)
    if adapter is not None:
        role = getattr(adapter, "a2a_role", None)
        if role:
            return role
    return _SERVICE_ROLES.get(service_name, f"{service_name} local agent")


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
    adapter = _adapter_for(service, service_name)
    offsets: tuple[int, ...]
    if adapter is not None and getattr(adapter, "a2a_plugin_port_offsets", None):
        offsets = tuple(adapter.a2a_plugin_port_offsets)
    else:
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
        role=role_for_service(svc, service=service),
        skills=skills_for_service(svc, service=service),
        endpoint=plugin_endpoint_for(record, service=service, host=host),
    )
