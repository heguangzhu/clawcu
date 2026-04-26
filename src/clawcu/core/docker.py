from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Callable

from clawcu.core.models import ContainerRunSpec, InstanceRecord
from clawcu.core.subprocess_utils import CommandError, run_command


def resolve_a2a_bind_interface() -> str:
    """Return the interface prefix docker should bind A2A sidecar ports on.

    Review-12 P1-D1: A2A sidecar ports are reachable from any host network
    interface because ``docker run -p host:internal`` binds to 0.0.0.0 by
    default. Any machine on the LAN can POST ``/a2a/send`` /
    ``/a2a/outbound`` / ``/mcp`` with no auth, drain the LLM quota, or
    inject adversarial messages into a native agent's session.

    Restricting the sidecar to 127.0.0.1 on the host preserves:

    * the CLI path (``clawcu a2a send`` hits 127.0.0.1 after the iter-11
      P1-C1 localize step);
    * container→container on Darwin, because Docker Desktop's userland
      proxy forwards ``host.docker.internal:<host_port>`` into the host's
      loopback (empirically confirmed under 27.x).

    On Linux, ``host.docker.internal`` is added as an extra_hosts pointer
    at the docker bridge gateway IP (typically 172.17.0.1). Binding only
    to 127.0.0.1 on the host would break container→container because the
    bridge-gateway traffic never reaches a loopback-only port. We
    therefore fall back to 0.0.0.0 on Linux by default and let the
    operator opt in to stricter binding via the env var once they've
    configured their firewall or network policy.

    The env var ``CLAWCU_A2A_BIND_INTERFACE`` lets advanced users override
    both defaults (e.g. bind to a specific LAN IP, or accept the
    defaults).
    """
    override = os.environ.get("CLAWCU_A2A_BIND_INTERFACE")
    if isinstance(override, str) and override.strip():
        return override.strip()
    if sys.platform == "darwin":
        return "127.0.0.1"
    return ""


class DockerManager:
    INSPECT_TIMEOUT_SECONDS = 5
    PULL_TIMEOUT_SECONDS = 1800
    RUN_TIMEOUT_SECONDS = 1800
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

    def list_local_images(self, image_repo: str) -> list[str]:
        """Return the locally available tags for ``image_repo``.

        Uses ``docker image ls <repo> --format {{.Tag}}``. Returns an
        empty list on any failure (no such repo, docker daemon
        unreachable, etc.) — this is a best-effort UX helper, not a
        correctness primitive, so callers should not treat emptiness as
        authoritative "repo has no tags".
        """
        try:
            result = self.runner(
                ["docker", "image", "ls", image_repo, "--format", "{{.Tag}}"],
                timeout_seconds=self.INSPECT_TIMEOUT_SECONDS,
            )
        except Exception:
            return []
        stdout = getattr(result, "stdout", "") or ""
        tags = [line.strip() for line in stdout.splitlines() if line.strip()]
        # Docker prints "<none>" for dangling/untagged images; filter.
        tags = [tag for tag in tags if tag != "<none>"]
        # Stable, deduplicated order.
        return sorted(set(tags))

    def pull_image(self, image_tag: str) -> None:
        self.runner(
            ["docker", "pull", image_tag],
            stream_output=True,
            timeout_seconds=self.PULL_TIMEOUT_SECONDS,
        )

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
        build_args: dict[str, str] | None = None,
    ) -> None:
        command = ["docker", "build"]
        if dockerfile:
            command.extend(["-f", str(dockerfile)])
        if build_contexts:
            for name, path in sorted(build_contexts.items()):
                command.extend(["--build-context", f"{name}={path}"])
        if build_args:
            for name, value in sorted(build_args.items()):
                command.extend(["--build-arg", f"{name}={value}"])
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
            "missing",
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
        for source_path, target_path in spec.additional_mounts:
            command.extend(["-v", f"{source_path}:{target_path}"])
        # Review-12 P1-D1: additional_ports today are all A2A sidecar ports
        # (both adapters only add entries here for the sidecar). Bind them to
        # a single interface so LAN hosts can't reach /a2a/send,
        # /a2a/outbound, or /mcp without a docker connection. See
        # ``resolve_a2a_bind_interface`` for the platform defaults.
        a2a_iface = resolve_a2a_bind_interface()
        for host_port, internal_port in spec.additional_ports:
            publish = (
                f"{a2a_iface}:{host_port}:{internal_port}"
                if a2a_iface
                else f"{host_port}:{internal_port}"
            )
            command.extend(["-p", publish])
        for host_name, host_ip in spec.extra_hosts:
            # `host-gateway` is a docker-engine magic value (Linux parity
            # with Docker Desktop's auto-resolved host.docker.internal).
            command.extend(["--add-host", f"{host_name}:{host_ip}"])
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
        workdir: str | None = None,
        user: str | None = None,
    ) -> object:
        """Run an interactive `docker exec` in the container.

        ``env`` is a key→value dict; each entry becomes ``-e KEY=VAL``.
        ``workdir`` and ``user`` map to ``docker exec --workdir`` and
        ``--user`` respectively — they are passed through verbatim so
        callers can use any value Docker accepts (e.g. ``1000:1000``).
        """
        docker_command = ["docker", "exec"]
        if sys.stdin.isatty():
            docker_command.append("-i")
        if sys.stdout.isatty() and sys.stderr.isatty():
            docker_command.append("-t")
        if workdir:
            docker_command.extend(["--workdir", workdir])
        if user:
            docker_command.extend(["--user", user])
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

    def stop_container(self, container_name: str, *, timeout: int | None = None) -> None:
        """Stop a container.

        ``timeout`` is the grace period in seconds passed to ``docker stop
        --time``. When ``None``, uses the short default (5s) tuned for
        quick managed-instance cycling. Longer values give a running
        OpenClaw/Hermes task time to finish its in-flight work before
        SIGKILL fires.
        """
        grace_seconds = 5 if timeout is None else max(0, int(timeout))
        # The outer process timeout must cover the grace window plus
        # docker's own overhead, otherwise a well-behaved --time 60 gets
        # killed externally at our STOP_TIMEOUT_SECONDS budget.
        process_timeout = max(self.STOP_TIMEOUT_SECONDS, grace_seconds + 10)
        try:
            self.runner(
                ["docker", "stop", "--time", str(grace_seconds), container_name],
                timeout_seconds=process_timeout,
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
        except CommandError as exc:
            details = f"{exc.stderr}\n{exc.stdout}".lower()
            if not missing_ok or "no such container" not in details:
                raise

    def stream_logs(
        self,
        container_name: str,
        *,
        follow: bool = False,
        tail: int | None = None,
        since: str | None = None,
    ) -> None:
        command = ["docker", "logs"]
        if follow:
            command.append("-f")
        if tail is not None and tail > 0:
            command.extend(["--tail", str(tail)])
        if since:
            command.extend(["--since", since])
        command.append(container_name)
        self.runner(
            command,
            capture_output=False,
            timeout_seconds=None if follow else self.LOGS_TIMEOUT_SECONDS,
        )
