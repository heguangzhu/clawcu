from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Callable

from clawcu.models import InstanceRecord
from clawcu.subprocess_utils import CommandError, run_command


class DockerManager:
    INTERNAL_GATEWAY_PORT = 18789

    def __init__(self, runner: Callable = run_command):
        self.runner = runner

    def image_exists(self, image_tag: str) -> bool:
        try:
            self.runner(["docker", "image", "inspect", image_tag])
            return True
        except CommandError:
            return False

    def pull_image(self, image_tag: str) -> None:
        self.runner(["docker", "pull", image_tag], stream_output=True)

    def tag_image(self, source_image: str, target_image: str) -> None:
        self.runner(["docker", "tag", source_image, target_image], capture_output=False)

    def build_image(self, source_dir: Path, image_tag: str, *, preferred_variant: str = "slim") -> None:
        dockerfile = source_dir / "Dockerfile"
        dockerfile_text = dockerfile.read_text(encoding="utf-8") if dockerfile.exists() else ""
        command = ["docker", "build", "-t", image_tag]

        # Newer OpenClaw Dockerfiles select the smaller runtime via build args,
        # while older variants may expose a dedicated `slim` stage.
        if preferred_variant and "ARG OPENCLAW_VARIANT" in dockerfile_text and "base-slim" in dockerfile_text:
            command.extend(["--build-arg", f"OPENCLAW_VARIANT={preferred_variant}"])
        elif preferred_variant == "slim" and (
            " AS slim" in dockerfile_text or " as slim" in dockerfile_text
        ):
            command.extend(["--target", "slim"])

        command.append(".")
        self.runner(command, cwd=source_dir, capture_output=False)

    def inspect_container(self, container_name: str) -> dict | None:
        try:
            result = self.runner(
                ["docker", "inspect", container_name, "--format", "{{json .}}"]
            )
        except CommandError:
            return None
        stdout = getattr(result, "stdout", "").strip()
        return json.loads(stdout) if stdout else None

    def container_status(self, container_name: str) -> str:
        inspection = self.inspect_container(container_name)
        if not inspection:
            return "missing"
        state = inspection.get("State", {})
        status = state.get("Status", "unknown")
        health = state.get("Health", {})
        health_status = health.get("Status")
        if status == "running" and health_status in {"starting", "unhealthy"}:
            return health_status
        return status

    def run_container(self, record: InstanceRecord, *, env_file: Path | None = None) -> None:
        command = [
            "docker",
            "run",
            "-d",
            "--name",
            record.container_name,
            "--restart",
            "unless-stopped",
            "--cpus",
            record.cpu,
            "--memory",
            record.memory,
            "-p",
            f"{record.port}:{self.INTERNAL_GATEWAY_PORT}",
            "-v",
            f"{record.datadir}:/home/node/.openclaw",
            "-e",
            "HOST=0.0.0.0",
            "--label",
            "com.clawcu.managed=true",
            "--label",
            f"com.clawcu.service={record.service}",
            "--label",
            f"com.clawcu.instance={record.name}",
        ]
        if env_file is not None:
            command.extend(["--env-file", str(env_file)])
        command.append(record.image_tag)
        self.runner(command)

    def exec_in_container(
        self, container_name: str, command: list[str], **kwargs
    ) -> object:
        return self.runner(
            ["docker", "exec", container_name] + command, **kwargs
        )

    def exec_in_container_interactive(self, container_name: str, command: list[str]) -> object:
        docker_command = ["docker", "exec"]
        if sys.stdin.isatty():
            docker_command.append("-i")
        if sys.stdout.isatty() and sys.stderr.isatty():
            docker_command.append("-t")
        return self.runner(
            docker_command + [container_name] + command,
            capture_output=False,
        )

    def start_container(self, container_name: str) -> None:
        self.runner(["docker", "start", container_name])

    def stop_container(self, container_name: str) -> None:
        self.runner(["docker", "stop", container_name])

    def restart_container(self, container_name: str) -> None:
        self.runner(["docker", "restart", container_name])

    def remove_container(self, container_name: str, *, missing_ok: bool = False) -> None:
        try:
            self.runner(["docker", "rm", "-f", container_name])
        except CommandError:
            if not missing_ok:
                raise

    def stream_logs(self, container_name: str, *, follow: bool = False) -> None:
        command = ["docker", "logs"]
        if follow:
            command.append("-f")
        command.append(container_name)
        self.runner(command, capture_output=False)
