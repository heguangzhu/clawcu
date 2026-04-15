from __future__ import annotations

import re
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from clawcu.core.models import InstanceRecord, InstanceSpec, ProviderRecord

NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$")
MEMORY_PATTERN = re.compile(r"^\d+(\.\d+)?([bkmgBKMG])?$")
API_STYLE_PATTERN = {"openai", "anthropic"}
DOCKER_TAG_SAFE = re.compile(r"[^A-Za-z0-9_.-]+")


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def normalize_version(version: str) -> str:
    cleaned = version.strip()
    if cleaned.startswith("v"):
        cleaned = cleaned[1:]
    if not cleaned:
        raise ValueError("Version cannot be empty.")
    return cleaned


def normalize_ref(ref: str) -> str:
    cleaned = ref.strip()
    if not cleaned:
        raise ValueError("Version cannot be empty.")
    return cleaned


def normalize_service_version(service: str, version: str) -> str:
    if service == "openclaw":
        return normalize_version(version)
    return normalize_ref(version)


def upstream_ref_for_version(version: str) -> str:
    return f"v{normalize_version(version)}"


def upstream_ref_for_service(service: str, version: str) -> str:
    if service == "openclaw":
        return upstream_ref_for_version(version)
    return normalize_ref(version)


def _docker_tag_component(value: str) -> str:
    safe = DOCKER_TAG_SAFE.sub("-", value.strip())
    safe = safe.strip("-.")
    return safe or "latest"


def image_tag_for_version(version: str) -> str:
    return f"clawcu/openclaw:{normalize_version(version)}"


def image_tag_for_service(service: str, version: str) -> str:
    normalized = normalize_service_version(service, version)
    return f"clawcu/{service}:{_docker_tag_component(normalized)}"


def container_name_for_instance(name: str) -> str:
    return f"clawcu-openclaw-{name}"


def container_name_for_service(service: str, name: str) -> str:
    return f"clawcu-{service}-{name}"


def validate_name(name: str) -> str:
    if not NAME_PATTERN.match(name):
        raise ValueError(
            "Instance name must start with an alphanumeric character and use only letters, numbers, dot, dash, or underscore."
        )
    return name


def validate_provider_name(name: str) -> str:
    return validate_name(name)


def validate_api_style(api_style: str) -> str:
    cleaned = api_style.strip().lower()
    if cleaned not in API_STYLE_PATTERN:
        raise ValueError("API style must be either 'openai' or 'anthropic'.")
    return cleaned


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


def validate_api_key(api_key: str) -> str:
    cleaned = api_key.strip()
    if not cleaned:
        raise ValueError("API key cannot be empty.")
    return cleaned


def normalize_endpoint(endpoint: str | None) -> str | None:
    if endpoint is None:
        return None
    cleaned = endpoint.strip()
    return cleaned or None


def parse_models_csv(models: str) -> list[str]:
    parsed: list[str] = []
    seen: set[str] = set()
    for item in models.split(","):
        cleaned = item.strip()
        if not cleaned or cleaned in seen:
            continue
        parsed.append(cleaned)
        seen.add(cleaned)
    if not parsed:
        raise ValueError("Please provide at least one model name.")
    return parsed


def build_instance_record(spec: InstanceSpec, *, status: str, history: list[dict] | None = None) -> InstanceRecord:
    normalized_version = normalize_service_version(spec.service, spec.version)
    timestamp = utc_now_iso()
    return InstanceRecord(
        service=spec.service,
        name=spec.name,
        version=normalized_version,
        upstream_ref=upstream_ref_for_service(spec.service, normalized_version),
        image_tag=image_tag_for_service(spec.service, normalized_version),
        container_name=container_name_for_service(spec.service, spec.name),
        datadir=resolve_datadir(spec.datadir),
        port=spec.port,
        cpu=spec.cpu,
        memory=spec.memory,
        auth_mode=spec.auth_mode,
        status=status,
        created_at=timestamp,
        updated_at=timestamp,
        last_error=None,
        history=history or [],
    )


def build_provider_record(
    *,
    name: str,
    api_style: str,
    api_key: str,
    endpoint: str | None,
    models: list[str],
) -> ProviderRecord:
    timestamp = utc_now_iso()
    return ProviderRecord(
        name=validate_provider_name(name),
        api_style=validate_api_style(api_style),
        api_key=validate_api_key(api_key),
        endpoint=normalize_endpoint(endpoint),
        models=models,
        created_at=timestamp,
        updated_at=timestamp,
    )


def updated_provider_record(record: ProviderRecord, **changes: object) -> ProviderRecord:
    refreshed = replace(record, **changes)
    refreshed.updated_at = utc_now_iso()
    return refreshed


def updated_record(record: InstanceRecord, **changes: object) -> InstanceRecord:
    refreshed = replace(record, **changes)
    refreshed.updated_at = utc_now_iso()
    return refreshed
