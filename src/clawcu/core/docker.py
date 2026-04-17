from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Callable

from clawcu.core.models import ContainerRunSpec, InstanceRecord
from clawcu.core.subprocess_utils import CommandError, run_command


class DockerManager:
    INSPECT_TIMEOUT_SECONDS = 5
    RUN_TIMEOUT_SECONDS = 20
    START_TIMEOUT_SECONDS = 20
    STOP_TIMEOUT_SECONDS = 15
    RESTART_TIMEOUT_SECONDS = 20
    EXEC_TIMEOUT_SECONDS = 30
    LOGS_TIMEOUT_SECONDS = 10

    def __init__(self, runner: Callable = run_command):
        self.runner = runner

    def image_exists(self, image_tag: str) -> bool:
        try:
            self.runner(
                ["docker", "image", "inspect", image_tag],
                timeout_seconds=self.INSPECT_TIMEOUT_SECONDS,
            )
            return True
        except Exception:
            return False

    def pull_image(self, image_tag: str) -> None:
        self.runner(["docker", "pull", image_tag], stream_output=True)

    def tag_image(self, source_image: str, target_image: str) -> None:
        self.runner(["docker", "tag", source_image, target_image], capture_output=False)

    def build_image(
        self,
        source_dir: Path,
        image_tag: str,
        *,
        preferred_variant: str | None = None,
        dockerfile: str | Path | None = None,
        build_contexts: dict[str, str | Path] | None = None,
    ) -> None:
        command = ["docker", "build"]
        if dockerfile:
            command.extend(["-f", str(dockerfile)])
        if build_contexts:
            for name, path in sorted(build_contexts.items()):
                command.extend(["--build-context", f"{name}={path}"])
        command.extend(["-t", image_tag, "."])
        self.runner(command, cwd=source_dir, capture_output=False)

    def inspect_container(self, container_name: str) -> dict | None:
        try:
            result = self.runner(
                ["docker", "inspect", container_name, "--format", "{{json .}}"],
                timeout_seconds=self.INSPECT_TIMEOUT_SECONDS,
            )
        except Exception:
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

    def run_container(self, record: InstanceRecord, spec: ContainerRunSpec) -> None:
        command = [
            "docker",
            "run",
            "-d",
            "--pull",
            "never",
            "--name",
            record.container_name,
            "--restart",
            "unless-stopped",
            "--cpus",
            record.cpu,
            "--memory",
            record.memory,
            "-p",
            f"{record.port}:{spec.internal_port}",
            "-v",
            f"{record.datadir}:{spec.mount_target}",
            "--label",
            "com.clawcu.managed=true",
            "--label",
            f"com.clawcu.service={record.service}",
            "--label",
            f"com.clawcu.instance={record.name}",
        ]
        for host_port, internal_port in spec.additional_ports:
            command.extend(["-p", f"{host_port}:{internal_port}"])
        if spec.env_file:
            command.extend(["--env-file", spec.env_file])
        for key, value in sorted(spec.extra_env.items()):
            command.extend(["-e", f"{key}={value}"])
        command.append(record.image_tag)
        if spec.command:
            command.extend(spec.command)
        self.runner(command, timeout_seconds=self.RUN_TIMEOUT_SECONDS)

    def exec_in_container(
        self,
        container_name: str,
        command: list[str],
        *,
        env: dict[str, str] | None = None,
        **kwargs,
    ) -> object:
        docker_command = ["docker", "exec"]
        if env:
            for key, value in sorted(env.items()):
                docker_command.extend(["-e", f"{key}={value}"])
        kwargs.setdefault("timeout_seconds", self.EXEC_TIMEOUT_SECONDS)
        return self.runner(docker_command + [container_name] + command, **kwargs)

    def exec_in_container_interactive(
        self,
        container_name: str,
        command: list[str],
        *,
        env: dict[str, str] | None = None,
    ) -> object:
        docker_command = ["docker", "exec"]
        if sys.stdin.isatty():
            docker_command.append("-i")
        if sys.stdout.isatty() and sys.stderr.isatty():
            docker_command.append("-t")
        if env:
            for key, value in sorted(env.items()):
                docker_command.extend(["-e", f"{key}={value}"])
        return self.runner(
            docker_command + [container_name] + command,
            capture_output=False,
        )

    def start_container(self, container_name: str) -> None:
        self.runner(
            ["docker", "start", container_name],
            timeout_seconds=self.START_TIMEOUT_SECONDS,
        )

    def stop_container(self, container_name: str) -> None:
        try:
            self.runner(
                ["docker", "stop", "--time", "5", container_name],
                timeout_seconds=self.STOP_TIMEOUT_SECONDS,
            )
        except CommandError as exc:
            details = f"{exc.stderr}\n{exc.stdout}".lower()
            if "no such container" not in details:
                raise

    def restart_container(self, container_name: str) -> None:
        self.runner(
            ["docker", "restart", "--time", "5", container_name],
            timeout_seconds=self.RESTART_TIMEOUT_SECONDS,
        )

    def remove_container(self, container_name: str, *, missing_ok: bool = False) -> None:
        try:
            self.runner(
                ["docker", "rm", "-f", container_name],
                timeout_seconds=self.STOP_TIMEOUT_SECONDS,
            )
        except Exception:
            if not missing_ok:
                raise

    def stream_logs(self, container_name: str, *, follow: bool = False) -> None:
        command = ["docker", "logs"]
        if follow:
            command.append("-f")
        command.append(container_name)
        self.runner(
            command,
            capture_output=False,
            timeout_seconds=None if follow else self.LOGS_TIMEOUT_SECONDS,
        )
