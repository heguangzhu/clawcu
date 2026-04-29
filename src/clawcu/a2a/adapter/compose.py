"""Docker orchestration for the A2A companion container."""

from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("clawcu-a2a-adapter")

_ADAPTER_IMAGE = "clawcu/a2a-adapter"


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


def _adapter_source_dir() -> Path:
    """Return the directory containing the adapter Dockerfile."""
    return Path(__file__).resolve().parent


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
    source_dir = _adapter_source_dir()
    docker.build_image(
        source_dir,
        tag,
        dockerfile=str(source_dir / "Dockerfile"),
    )
    if reporter:
        reporter(f"Built {tag}")
    return tag


def companion_container_name(instance_name: str) -> str:
    """Canonical container name for the A2A companion."""
    return f"clawcu-a2a-{instance_name}"


def start_companion(docker, spec: CompanionSpec, main_container: str) -> None:
    """Start the A2A companion container sharing the main container's network."""
    cname = companion_container_name(spec.instance_name)

    # Remove stale container if it exists.
    docker.remove_container(cname, missing_ok=True)

    env = {
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
    }

    env_flags = [f"-e{k}={v}" for k, v in env.items()]

    cmd = [
        "docker", "run", "-d",
        "--name", cname,
        "--network", f"container:{main_container}",
        "--restart", "unless-stopped",
        *env_flags,
        spec.adapter_image,
    ]

    from clawcu.core.subprocess_utils import run_command
    run_command(cmd, timeout=docker.RUN_TIMEOUT_SECONDS)
    log.info("started companion %s (network=%s)", cname, main_container)


def stop_companion(docker, instance_name: str) -> None:
    """Stop and remove the companion container."""
    cname = companion_container_name(instance_name)
    try:
        docker.stop_container(cname)
    except Exception:
        pass
    docker.remove_container(cname, missing_ok=True)


def companion_status(docker, instance_name: str) -> str:
    """Return the status of the companion container, or 'missing'."""
    return docker.container_status(companion_container_name(instance_name))
