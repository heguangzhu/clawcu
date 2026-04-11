from __future__ import annotations

import re
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from clawcu.models import InstanceRecord, InstanceSpec

NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$")
MEMORY_PATTERN = re.compile(r"^\d+(\.\d+)?([bkmgBKMG])?$")


def utc_now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def normalize_version(version: str) -> str:
    cleaned = version.strip()
    if cleaned.startswith("v"):
        cleaned = cleaned[1:]
    if not cleaned:
        raise ValueError("Version cannot be empty.")
    return cleaned


def upstream_ref_for_version(version: str) -> str:
    return f"v{normalize_version(version)}"


def image_tag_for_version(version: str) -> str:
    return f"clawcu/openclaw:{normalize_version(version)}"


def container_name_for_instance(name: str) -> str:
    return f"clawcu-openclaw-{name}"


def validate_name(name: str) -> str:
    if not NAME_PATTERN.match(name):
        raise ValueError(
            "Instance name must start with an alphanumeric character and use only letters, numbers, dot, dash, or underscore."
        )
    return name


def validate_port(port: int) -> int:
    if port < 1 or port > 65535:
        raise ValueError("Port must be between 1 and 65535.")
    return port


def validate_cpu(cpu: str) -> str:
    try:
        value = float(cpu)
    except ValueError as exc:
        raise ValueError("CPU must be a positive number.") from exc
    if value <= 0:
        raise ValueError("CPU must be greater than 0.")
    return str(cpu)


def validate_memory(memory: str) -> str:
    if not MEMORY_PATTERN.match(memory):
        raise ValueError("Memory must look like 512m, 2g, or 1.")
    return memory.lower()


def resolve_datadir(datadir: str) -> str:
    return str(Path(datadir).expanduser().resolve())


def build_instance_record(spec: InstanceSpec, *, status: str, history: list[dict] | None = None) -> InstanceRecord:
    normalized_version = normalize_version(spec.version)
    timestamp = utc_now_iso()
    return InstanceRecord(
        service=spec.service,
        name=spec.name,
        version=normalized_version,
        upstream_ref=upstream_ref_for_version(normalized_version),
        image_tag=image_tag_for_version(normalized_version),
        container_name=container_name_for_instance(spec.name),
        datadir=resolve_datadir(spec.datadir),
        port=spec.port,
        cpu=spec.cpu,
        memory=spec.memory,
        status=status,
        created_at=timestamp,
        updated_at=timestamp,
        last_error=None,
        history=history or [],
    )


def updated_record(record: InstanceRecord, **changes: object) -> InstanceRecord:
    refreshed = replace(record, **changes)
    refreshed.updated_at = utc_now_iso()
    return refreshed
