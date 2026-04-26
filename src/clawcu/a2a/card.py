from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any

DEFAULT_REGISTRY_PORT = 9100
DEFAULT_BRIDGE_PORT = 19100

# Review-1 §3: card.py is the protocol layer — it must NOT hold
# service-specific knowledge. The A2A defaults (``a2a_skills`` /
# ``a2a_role`` / ``a2a_plugin_port_offsets`` / ``display_port``) live on
# the ``ServiceAdapter`` subclass for each service. Callers in the
# control plane (registry.py, cli.py, detect.py) always hand in a
# ``ClawCUService``, so the adapter path covers every real code path.
# The fallbacks below are generic defaults used only when a caller
# (a unit-test fake record) cannot produce an adapter — they do not
# branch on service name, so adding a third service never requires
# editing this module.
_FALLBACK_SKILLS: tuple[str, ...] = ("chat",)


def _adapter_for(service: Any, service_name: str) -> Any | None:
    """Return the adapter for ``service_name`` via a ClawCUService handle,
    or ``None`` if unavailable. Best-effort — falls back silently so unit
    tests that pass minimal service stubs still work."""
    if service is None or not service_name:
        return None
    lookup = getattr(service, "adapter_for_service", None)
    if not callable(lookup):
        return None
    try:
        return lookup(service_name)
    except (ValueError, KeyError, AttributeError, TypeError):
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
    provided; falls back to a single generic ``["chat"]`` otherwise.
    No service-name branching — a new service advertises its skills by
    setting ``a2a_skills`` on its ``ServiceAdapter`` subclass.
    """
    adapter = _adapter_for(service, service_name)
    if adapter is not None:
        skills = getattr(adapter, "a2a_skills", None)
        if skills:
            return list(skills)
    return list(_FALLBACK_SKILLS)


def role_for_service(service_name: str, *, service: Any = None) -> str:
    """Return the A2A ``role`` string for this service type.

    Prefers ``adapter.a2a_role`` when a ``ClawCUService`` handle is
    provided; otherwise templates ``"{service_name} local agent"``.
    """
    adapter = _adapter_for(service, service_name)
    if adapter is not None:
        role = getattr(adapter, "a2a_role", None)
        if role:
            return role
    return f"{service_name} local agent" if service_name else "local agent"


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
    when a ClawCUService is in hand; otherwise falls back to ``record.port``
    (the port the instance itself listens on) and finally
    ``DEFAULT_BRIDGE_PORT``. No service-name branching — each adapter owns
    its port layout.
    """
    if service is not None:
        try:
            adapter = service.adapter_for_record(record)
            return int(adapter.display_port(service, record))
        except (ValueError, KeyError, AttributeError, TypeError):
            # Best-effort: fall through to the naive defaults. Narrower
            # than bare ``Exception`` so real bugs (ImportError, etc.)
            # still surface.
            pass
    port = getattr(record, "port", None)
    if isinstance(port, int) and port > 0:
        return port
    return DEFAULT_BRIDGE_PORT


def plugin_port_candidates(record: Any, *, service: Any = None) -> list[int]:
    """Ports to probe when discovering a plugin's self-reported AgentCard.

    Callers should try these in order and take the first live card. The
    adapter's ``a2a_plugin_port_offsets`` drives the probe order: OpenClaw
    uses ``(0, 1)`` because its gateway holds display_port and the Node
    sidecar binds the neighbor slot; Hermes uses ``(0,)``. Without an
    adapter we probe only the base port. Duplicates are removed while
    preserving order so an adapter-reported port that already matches the
    sidecar slot doesn't get probed twice.
    """
    base = display_port_for_record(record, service=service)
    service_name = getattr(record, "service", "") or ""
    adapter = _adapter_for(service, service_name)
    offsets: tuple[int, ...] = (0,)
    if adapter is not None:
        adapter_offsets = getattr(adapter, "a2a_plugin_port_offsets", None)
        if adapter_offsets:
            offsets = tuple(adapter_offsets)
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
