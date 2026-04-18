from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


HistoryEntry = dict[str, Any]


@dataclass(kw_only=True)
class InstanceSpec:
    service: str
    name: str
    version: str
    datadir: str
    port: int
    cpu: str
    memory: str
    auth_mode: str
    dashboard_port: int | None = None
    image_tag_override: str | None = None


@dataclass(kw_only=True)
class InstanceRecord(InstanceSpec):
    upstream_ref: str
    image_tag: str
    container_name: str
    status: str
    created_at: str
    updated_at: str
    last_error: str | None = None
    history: list[HistoryEntry] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "InstanceRecord":
        payload = dict(data)
        payload.setdefault("auth_mode", "token")
        payload.setdefault("dashboard_port", None)
        return cls(**payload)


@dataclass
class ProviderRecord:
    name: str
    api_style: str
    api_key: str
    endpoint: str | None
    models: list[str]
    created_at: str
    updated_at: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ProviderRecord":
        payload = dict(data)
        payload.setdefault("endpoint", None)
        payload.setdefault("models", [])
        return cls(**payload)


@dataclass(frozen=True)
class AccessInfo:
    base_url: str | None
    readiness_label: str
    auth_hint: str | None = None
    token: str | None = None


@dataclass(frozen=True)
class ContainerRunSpec:
    internal_port: int
    mount_target: str
    env_file: str | None = None
    extra_env: dict[str, str] = field(default_factory=dict)
    command: list[str] | None = None
    additional_ports: list[tuple[int, int]] = field(default_factory=list)
    additional_mounts: list[tuple[str, str]] = field(default_factory=list)
