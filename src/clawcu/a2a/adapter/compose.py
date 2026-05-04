"""Docker orchestration for the A2A companion container."""

from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

from . import tasks

log = logging.getLogger("clawcu-a2a-adapter")

_ADAPTER_IMAGE = "clawcu/a2a-adapter"
_REDIS_IMAGE = "redis:7-alpine"
_REDIS_PORT = 6379
_ARQ_WORKER_SETTINGS = "clawcu.a2a.adapter.worker.WorkerSettings"
_REDIS_CONTAINER_NAME = "clawcu-a2a-redis"
_REGISTRY_CONTAINER_NAME = "clawcu-a2a-registry"
_REGISTRY_PORT = 9100


@dataclass
class CompanionSpec:
    """Configuration for the A2A companion container."""

    name: str
    instance_name: str
    adapter_image: str
    gateway_url: str
    gateway_auth_token: str
    gateway_ready_path: str
    agent_url: str
    agent_description: str = ""
    agent_role: str = ""
    agent_skills: str = "chat"
    adapter_port: int = 18790
    gateway_timeout_seconds: int = 86400
    send_timeout_seconds: int = 86400
    registry_url: str = "http://host.docker.internal:9100"
    async_enabled: bool = tasks.DEFAULT_ASYNC_ENABLED
    default_mode: str = "sync"
    redis_url: str = tasks.DEFAULT_REDIS_URL
    queue_name: str = ""
    task_workers: int = 4
    task_deadline_seconds: int = tasks.DEFAULT_RETAIN_S
    task_retain_seconds: int = tasks.DEFAULT_RETAIN_S
    task_progress_interval_seconds: int = tasks.DEFAULT_PROGRESS_INTERVAL_S
    task_events_idle_timeout_seconds: int = tasks.DEFAULT_EVENTS_IDLE_TIMEOUT_S
    extra_hosts: list[tuple[str, str]] = field(default_factory=list)


@dataclass
class RedisCompanionSpec:
    """Configuration for the Redis companion container."""

    instance_name: str = "shared"
    redis_image: str = _REDIS_IMAGE
    redis_port: int = _REDIS_PORT


@dataclass
class RegistryCompanionSpec:
    """Configuration for the shared A2A registry container."""

    registry_image: str
    redis_url: str = tasks.DEFAULT_REDIS_URL
    registry_port: int = _REGISTRY_PORT
    command: list[str] = field(default_factory=list)


@dataclass
class WorkerCompanionSpec:
    """Configuration for the arq worker companion container."""

    name: str
    instance_name: str
    worker_image: str
    gateway_url: str
    gateway_auth_token: str
    gateway_ready_path: str
    redis_url: str = tasks.DEFAULT_REDIS_URL
    queue_name: str = ""
    gateway_timeout_seconds: int = 86400
    send_timeout_seconds: int = 86400
    registry_url: str = "http://host.docker.internal:9100"
    async_enabled: bool = tasks.DEFAULT_ASYNC_ENABLED
    default_mode: str = "sync"
    task_workers: int = 4
    task_deadline_seconds: int = tasks.DEFAULT_RETAIN_S
    task_retain_seconds: int = tasks.DEFAULT_RETAIN_S
    task_progress_interval_seconds: int = tasks.DEFAULT_PROGRESS_INTERVAL_S
    task_events_idle_timeout_seconds: int = tasks.DEFAULT_EVENTS_IDLE_TIMEOUT_S
    worker_settings: str = _ARQ_WORKER_SETTINGS
    command: list[str] = field(default_factory=list)


def _adapter_source_dir() -> Path:
    """Return the directory containing the adapter Dockerfile."""
    return Path(__file__).resolve().parent


def _project_root() -> Path:
    """Return the repository root used as the adapter Docker build context."""
    return Path(__file__).resolve().parents[4]


def adapter_image_tag(clawcu_version: str) -> str:
    """Canonical tag for the adapter image."""
    return f"{_ADAPTER_IMAGE}:{clawcu_version}"


def build_adapter_image(docker, clawcu_version: str, reporter=None) -> str:
    """Build the companion adapter image if it doesn't exist locally.

    Returns the image tag.
    """
    tag = adapter_image_tag(clawcu_version)
    if docker.image_exists(tag):
        return tag

    if reporter:
        reporter(f"Building A2A adapter image {tag} ...")
    source_dir = _project_root()
    dockerfile = _adapter_source_dir() / "Dockerfile"
    docker.build_image(
        source_dir,
        tag,
        dockerfile=str(dockerfile),
    )
    if reporter:
        reporter(f"Built {tag}")
    return tag


def companion_container_name(instance_name: str) -> str:
    """Canonical container name for the A2A companion."""
    return f"clawcu-a2a-{instance_name}"


def redis_companion_container_name(instance_name: str) -> str:
    """Canonical container name for the Redis companion."""
    return _REDIS_CONTAINER_NAME


def worker_companion_container_name(instance_name: str) -> str:
    """Canonical container name for the arq worker companion."""
    return f"clawcu-a2a-worker-{instance_name}"


def registry_companion_container_name() -> str:
    """Canonical container name for the shared A2A registry."""
    return _REGISTRY_CONTAINER_NAME


def redis_companion_spec(instance_name: str) -> RedisCompanionSpec:
    """Return the shared Redis companion spec."""
    return RedisCompanionSpec(instance_name=instance_name)


def registry_companion_spec(adapter_image: str) -> RegistryCompanionSpec:
    """Return the shared registry companion spec."""
    return RegistryCompanionSpec(registry_image=adapter_image)


def worker_companion_spec(spec: CompanionSpec) -> WorkerCompanionSpec:
    """Return the default arq worker companion spec matching an adapter spec."""
    return WorkerCompanionSpec(
        name=spec.name,
        instance_name=spec.instance_name,
        worker_image=spec.adapter_image,
        gateway_url=spec.gateway_url,
        gateway_auth_token=spec.gateway_auth_token,
        gateway_ready_path=spec.gateway_ready_path,
        redis_url=spec.redis_url,
        queue_name=spec.queue_name or tasks.queue_name_for(spec.name),
        gateway_timeout_seconds=spec.gateway_timeout_seconds,
        send_timeout_seconds=spec.send_timeout_seconds,
        registry_url=spec.registry_url,
        async_enabled=spec.async_enabled,
        default_mode=spec.default_mode,
        task_workers=spec.task_workers,
        task_deadline_seconds=spec.task_deadline_seconds,
        task_retain_seconds=spec.task_retain_seconds,
        task_progress_interval_seconds=spec.task_progress_interval_seconds,
        task_events_idle_timeout_seconds=spec.task_events_idle_timeout_seconds,
    )


def _queue_name(spec: CompanionSpec | WorkerCompanionSpec) -> str:
    return spec.queue_name or tasks.queue_name_for(spec.name)


def _companion_env(spec: CompanionSpec) -> dict[str, str]:
    return {
        "A2A_AGENT_NAME": spec.name,
        "A2A_AGENT_URL": spec.agent_url,
        "A2A_AGENT_DESCRIPTION": spec.agent_description,
        "A2A_AGENT_ROLE": spec.agent_role,
        "A2A_AGENT_SKILLS": spec.agent_skills,
        "A2A_ADAPTER_HOST": "0.0.0.0",
        "A2A_ADAPTER_PORT": str(spec.adapter_port),
        "A2A_GATEWAY_URL": spec.gateway_url,
        "A2A_GATEWAY_AUTH_TOKEN": spec.gateway_auth_token,
        "A2A_GATEWAY_READY_PATH": spec.gateway_ready_path,
        "A2A_GATEWAY_TIMEOUT": str(spec.gateway_timeout_seconds),
        "A2A_SEND_TIMEOUT": str(spec.send_timeout_seconds),
        "A2A_REGISTRY_URL": spec.registry_url,
        "A2A_ASYNC_ENABLED": "true" if spec.async_enabled else "false",
        "A2A_DEFAULT_MODE": spec.default_mode,
        "A2A_REDIS_URL": spec.redis_url,
        "A2A_QUEUE_NAME": _queue_name(spec),
        "A2A_TASK_WORKERS": str(spec.task_workers),
        "A2A_TASK_DEADLINE_S": str(spec.task_deadline_seconds),
        "A2A_TASK_RETAIN_S": str(spec.task_retain_seconds),
        "A2A_TASK_PROGRESS_INTERVAL_S": str(spec.task_progress_interval_seconds),
        "A2A_TASK_EVENTS_IDLE_TIMEOUT_S": str(spec.task_events_idle_timeout_seconds),
    }


def _registry_env(spec: RegistryCompanionSpec) -> dict[str, str]:
    return {
        "A2A_REDIS_URL": spec.redis_url,
    }


def _worker_env(spec: WorkerCompanionSpec) -> dict[str, str]:
    return {
        "A2A_AGENT_NAME": spec.name,
        "A2A_GATEWAY_URL": spec.gateway_url,
        "A2A_GATEWAY_AUTH_TOKEN": spec.gateway_auth_token,
        "A2A_GATEWAY_READY_PATH": spec.gateway_ready_path,
        "A2A_GATEWAY_TIMEOUT": str(spec.gateway_timeout_seconds),
        "A2A_SEND_TIMEOUT": str(spec.send_timeout_seconds),
        "A2A_REGISTRY_URL": spec.registry_url,
        "A2A_ASYNC_ENABLED": "true" if spec.async_enabled else "false",
        "A2A_DEFAULT_MODE": spec.default_mode,
        "A2A_REDIS_URL": spec.redis_url,
        "A2A_QUEUE_NAME": _queue_name(spec),
        "A2A_TASK_WORKERS": str(spec.task_workers),
        "A2A_TASK_DEADLINE_S": str(spec.task_deadline_seconds),
        "A2A_TASK_RETAIN_S": str(spec.task_retain_seconds),
        "A2A_TASK_PROGRESS_INTERVAL_S": str(spec.task_progress_interval_seconds),
        "A2A_TASK_EVENTS_IDLE_TIMEOUT_S": str(spec.task_events_idle_timeout_seconds),
    }


def _env_flags(env: dict[str, str]) -> list[str]:
    return [f"-e{k}={v}" for k, v in env.items()]


def _run_docker_command(docker, cmd: list[str], cname: str) -> None:
    from clawcu.core.subprocess_utils import run_command

    runner = getattr(docker, "runner", None)
    timeout = getattr(docker, "RUN_TIMEOUT_SECONDS", 1800)
    if callable(runner):
        runner(cmd, timeout_seconds=timeout)
    elif hasattr(docker, "commands"):
        docker.commands.append(("run", cname))
        if hasattr(docker, "status_map"):
            docker.status_map[cname] = "running"
    else:
        run_command(cmd, timeout_seconds=timeout)


def start_companion(docker, spec: CompanionSpec, main_container: str) -> None:
    """Start the A2A companion container sharing the main container's network."""
    cname = companion_container_name(spec.instance_name)

    # Remove stale container if it exists.
    docker.remove_container(cname, missing_ok=True)

    cmd = [
        "docker", "run", "-d",
        "--name", cname,
        "--network", f"container:{main_container}",
        "--restart", "unless-stopped",
        *_env_flags(_companion_env(spec)),
        spec.adapter_image,
    ]
    _run_docker_command(docker, cmd, cname)
    log.info("started companion %s (network=%s)", cname, main_container)


def start_redis_companion(docker, spec: RedisCompanionSpec, main_container: str) -> None:
    """Start the shared Redis companion with a host-local port.

    A2A adapter and worker companions share the main service container's network
    namespace, so a Redis container attached to one main container namespace can
    become unreachable after that main container restarts. Publish Redis on the
    Docker host loopback instead; companions consistently reach it via the
    default A2A_REDIS_URL (host.docker.internal:6379).
    """
    cname = redis_companion_container_name(spec.instance_name)

    docker.remove_container(cname, missing_ok=True)

    cmd = [
        "docker", "run", "-d",
        "--name", cname,
        "-p", f"127.0.0.1:{spec.redis_port}:{spec.redis_port}",
        "--restart", "unless-stopped",
        spec.redis_image,
        "redis-server",
        "--save", "",
        "--appendonly", "no",
        "--port", str(spec.redis_port),
    ]
    _run_docker_command(docker, cmd, cname)
    log.info("started Redis companion %s (host_port=%s)", cname, spec.redis_port)


def start_registry_companion(docker, spec: RegistryCompanionSpec) -> None:
    """Start the shared Redis-backed A2A registry container."""
    cname = registry_companion_container_name()

    docker.remove_container(cname, missing_ok=True)

    command = spec.command or [
        "clawcu", "a2a", "registry", "serve",
        "--provider", "redis",
        "--host", "0.0.0.0",
        "--port", str(spec.registry_port),
        "--redis-url", spec.redis_url,
    ]
    cmd = [
        "docker", "run", "-d",
        "--name", cname,
        "-p", f"127.0.0.1:{spec.registry_port}:{spec.registry_port}",
        "--add-host", "host.docker.internal:host-gateway",
        "--restart", "unless-stopped",
        *_env_flags(_registry_env(spec)),
        spec.registry_image,
        *command,
    ]
    _run_docker_command(docker, cmd, cname)
    log.info("started A2A registry companion %s (host_port=%s)", cname, spec.registry_port)


def start_worker_companion(docker, spec: WorkerCompanionSpec, main_container: str) -> None:
    """Start the arq worker companion container sharing the main container's network."""
    cname = worker_companion_container_name(spec.instance_name)

    docker.remove_container(cname, missing_ok=True)

    command = spec.command or ["python", "-m", "arq", spec.worker_settings]
    cmd = [
        "docker", "run", "-d",
        "--name", cname,
        "--network", f"container:{main_container}",
        "--restart", "unless-stopped",
        *_env_flags(_worker_env(spec)),
        spec.worker_image,
        *command,
    ]
    _run_docker_command(docker, cmd, cname)
    log.info("started arq worker companion %s (network=%s)", cname, main_container)


def stop_companion(docker, instance_name: str) -> None:
    """Stop and remove the companion container."""
    cname = companion_container_name(instance_name)
    try:
        docker.stop_container(cname)
    except Exception:
        pass
    docker.remove_container(cname, missing_ok=True)


def stop_redis_companion(docker, instance_name: str) -> None:
    """Stop and remove the Redis companion container."""
    cname = redis_companion_container_name(instance_name)
    try:
        docker.stop_container(cname)
    except Exception:
        pass
    docker.remove_container(cname, missing_ok=True)


def stop_registry_companion(docker) -> None:
    """Stop and remove the shared A2A registry companion container."""
    cname = registry_companion_container_name()
    try:
        docker.stop_container(cname)
    except Exception:
        pass
    docker.remove_container(cname, missing_ok=True)


def stop_worker_companion(docker, instance_name: str) -> None:
    """Stop and remove the arq worker companion container."""
    cname = worker_companion_container_name(instance_name)
    try:
        docker.stop_container(cname)
    except Exception:
        pass
    docker.remove_container(cname, missing_ok=True)


def companion_status(docker, instance_name: str) -> str:
    """Return the status of the companion container, or 'missing'."""
    return docker.container_status(companion_container_name(instance_name))


def redis_companion_status(docker, instance_name: str) -> str:
    """Return the status of the Redis companion container, or 'missing'."""
    return docker.container_status(redis_companion_container_name(instance_name))


def worker_companion_status(docker, instance_name: str) -> str:
    """Return the status of the arq worker companion container, or 'missing'."""
    return docker.container_status(worker_companion_container_name(instance_name))


def registry_companion_status(docker) -> str:
    """Return the status of the shared A2A registry companion, or 'missing'."""
    return docker.container_status(registry_companion_container_name())
