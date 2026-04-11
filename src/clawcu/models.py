from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


HistoryEntry = dict[str, Any]


@dataclass
class InstanceSpec:
    service: str
    name: str
    version: str
    datadir: str
    port: int
    cpu: str
    memory: str


@dataclass
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
        return cls(**data)
