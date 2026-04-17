from __future__ import annotations

from pathlib import Path

from clawcu.core.models import ContainerRunSpec
from clawcu.docker import DockerManager
from clawcu.models import InstanceRecord
from clawcu.openclaw import OpenClawManager
from clawcu.subprocess_utils import CommandError


class RecordingRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], Path | None, dict]] = []

    def __call__(self, command: list[str], *, cwd=None, capture_output=True, check=True, stream_output=False):
        self.calls.append(
            (
                command,
                cwd,
                {
                    "capture_output": capture_output,
                    "check": check,
                    "stream_output": stream_output,
                },
            )
        )
        return type("Completed", (), {"stdout": "", "stderr": "", "returncode": 0})()


def test_pull_image_streams_progress_output() -> None:
    runner = RecordingRunner()
    manager = DockerManager(runner=runner)

    manager.pull_image("ghcr.io/openclaw/openclaw:2026.4.10")

    command, _, options = runner.calls[0]
    assert command == ["docker", "pull", "ghcr.io/openclaw/openclaw:2026.4.10"]
    assert options["stream_output"] is True


class FakeDocker:
    def __init__(self) -> None:
        self.existing_images: set[str] = set()
        self.calls: list[tuple[str, tuple[str, ...]]] = []
        self.pull_error: CommandError | None = None

    def image_exists(self, image_tag: str) -> bool:
        return image_tag in self.existing_images

    def pull_image(self, image_tag: str) -> None:
        self.calls.append(("pull_image", (image_tag,)))
        if self.pull_error:
            raise self.pull_error

    def tag_image(self, source_image: str, target_image: str) -> None:
        self.calls.append(("tag_image", (source_image, target_image)))

def test_ensure_image_prefers_official_registry_pull(tmp_path) -> None:
    docker = FakeDocker()
    manager = OpenClawManager(object(), docker)

    image_tag = manager.ensure_image("2026.4.1")

    assert image_tag == "clawcu/openclaw:2026.4.1"
    assert docker.calls == [
        ("pull_image", ("ghcr.io/openclaw/openclaw:2026.4.1",)),
        ("tag_image", ("ghcr.io/openclaw/openclaw:2026.4.1", "clawcu/openclaw:2026.4.1")),
    ]


def test_ensure_image_reports_missing_version_in_official_registry(tmp_path) -> None:
    docker = FakeDocker()
    docker.pull_error = CommandError(
        ["docker", "pull", "ghcr.io/openclaw/openclaw:2026.4.1"],
        1,
        "",
        'failed to resolve reference "ghcr.io/openclaw/openclaw:2026.4.1": not found',
    )
    messages: list[str] = []
    manager = OpenClawManager(
        object(),
        docker,
        reporter=messages.append,
    )

    try:
        manager.ensure_image("2026.4.1")
        assert False, "ensure_image should have failed"
    except RuntimeError as exc:
        assert (
            str(exc)
            == "OpenClaw version 2026.4.1 was not found in the official image registry ghcr.io/openclaw/openclaw."
        )

    assert messages == [
        "Step 2/5: Pulling official image ghcr.io/openclaw/openclaw:2026.4.1. This usually takes 10-60 seconds depending on your network."
    ]


def test_ensure_image_reports_pull_failure_without_build_fallback(tmp_path) -> None:
    docker = FakeDocker()
    docker.pull_error = CommandError(["docker", "pull", "ghcr.io/openclaw/openclaw:2026.4.1"], 1, "", "network nope")
    manager = OpenClawManager(object(), docker)

    try:
        manager.ensure_image("2026.4.1")
        assert False, "ensure_image should have failed"
    except RuntimeError as exc:
        assert (
            str(exc)
            == "Failed to prepare OpenClaw 2026.4.1 from the official image registry ghcr.io/openclaw/openclaw: Command failed (1): docker pull ghcr.io/openclaw/openclaw:2026.4.1\nnetwork nope"
        )


def test_run_container_binds_host_port_to_internal_gateway_port() -> None:
    runner = RecordingRunner()
    manager = DockerManager(runner=runner)
    record = InstanceRecord(
        service="openclaw",
        name="writer",
        version="2026.4.1",
        upstream_ref="v2026.4.1",
        image_tag="clawcu/openclaw:2026.4.1",
        container_name="clawcu-openclaw-writer",
        datadir="/tmp/writer",
        port=18809,
        cpu="1",
        memory="2g",
        auth_mode="token",
        status="creating",
        created_at="2026-04-11T00:00:00+00:00",
        updated_at="2026-04-11T00:00:00+00:00",
        history=[],
    )

    manager.run_container(
        record,
        ContainerRunSpec(
            internal_port=18789,
            mount_target="/home/node/.openclaw",
            extra_env={"HOST": "0.0.0.0"},
        ),
    )

    command, _, _ = runner.calls[0]
    assert "18809:18789" in command
    assert "HOST=0.0.0.0" in command
    assert "PORT=3000" not in command


def test_run_container_supports_additional_port_bindings() -> None:
    runner = RecordingRunner()
    manager = DockerManager(runner=runner)
    record = InstanceRecord(
        service="hermes",
        name="javis",
        version="v2026.4.13",
        upstream_ref="v2026.4.13",
        image_tag="clawcu/hermes-agent:v2026.4.13",
        container_name="clawcu-hermes-javis",
        datadir="/tmp/javis",
        port=8652,
        dashboard_port=9129,
        cpu="1",
        memory="2g",
        auth_mode="native",
        status="creating",
        created_at="2026-04-11T00:00:00+00:00",
        updated_at="2026-04-11T00:00:00+00:00",
        history=[],
    )

    manager.run_container(
        record,
        ContainerRunSpec(
            internal_port=8642,
            mount_target="/opt/data",
            additional_ports=[(9129, 9119)],
        ),
    )

    command, _, _ = runner.calls[0]
    assert "8652:8642" in command
    assert "9129:9119" in command


def test_run_container_appends_explicit_container_command() -> None:
    runner = RecordingRunner()
    manager = DockerManager(runner=runner)
    record = InstanceRecord(
        service="openclaw",
        name="writer",
        version="2026.4.1",
        upstream_ref="v2026.4.1",
        image_tag="clawcu/openclaw:2026.4.1",
        container_name="clawcu-openclaw-writer",
        datadir="/tmp/writer",
        port=18809,
        cpu="1",
        memory="2g",
        auth_mode="token",
        status="creating",
        created_at="2026-04-11T00:00:00+00:00",
        updated_at="2026-04-11T00:00:00+00:00",
        history=[],
    )

    manager.run_container(
        record,
        ContainerRunSpec(
            internal_port=18789,
            mount_target="/home/node/.openclaw",
            command=[
                "node",
                "openclaw.mjs",
                "gateway",
                "--allow-unconfigured",
                "--bind",
                "lan",
                "--port",
                "18789",
            ],
        ),
    )

    command, _, _ = runner.calls[0]
    assert command[-8:] == [
        "node",
        "openclaw.mjs",
        "gateway",
        "--allow-unconfigured",
        "--bind",
        "lan",
        "--port",
        "18789",
    ]


def test_stop_and_restart_container_use_short_timeout() -> None:
    runner = RecordingRunner()
    manager = DockerManager(runner=runner)

    manager.stop_container("clawcu-openclaw-writer")
    manager.restart_container("clawcu-openclaw-writer")

    stop_command, _, _ = runner.calls[0]
    restart_command, _, _ = runner.calls[1]
    assert stop_command == ["docker", "stop", "--time", "5", "clawcu-openclaw-writer"]
    assert restart_command == ["docker", "restart", "--time", "5", "clawcu-openclaw-writer"]


def test_stop_container_ignores_missing_container() -> None:
    def runner(command: list[str], **_kwargs):
        raise CommandError(command, 1, "", "Error response from daemon: No such container: clawcu-openclaw-writer")

    manager = DockerManager(runner=runner)

    manager.stop_container("clawcu-openclaw-writer")


def test_exec_in_container_interactive_omits_tty_flags_without_terminal(monkeypatch) -> None:
    runner = RecordingRunner()
    manager = DockerManager(runner=runner)
    monkeypatch.setattr("clawcu.docker.sys.stdin.isatty", lambda: False)
    monkeypatch.setattr("clawcu.docker.sys.stdout.isatty", lambda: False)
    monkeypatch.setattr("clawcu.docker.sys.stderr.isatty", lambda: False)

    manager.exec_in_container_interactive("clawcu-openclaw-writer", ["pwd"])

    command, _, options = runner.calls[0]
    assert command == ["docker", "exec", "clawcu-openclaw-writer", "pwd"]
    assert options["capture_output"] is False


def test_exec_in_container_interactive_passes_env_values(monkeypatch) -> None:
    runner = RecordingRunner()
    manager = DockerManager(runner=runner)
    monkeypatch.setattr("clawcu.docker.sys.stdin.isatty", lambda: False)
    monkeypatch.setattr("clawcu.docker.sys.stdout.isatty", lambda: False)
    monkeypatch.setattr("clawcu.docker.sys.stderr.isatty", lambda: False)

    manager.exec_in_container_interactive(
        "clawcu-openclaw-writer",
        ["node", "openclaw.mjs", "tui"],
        env={"CLAWCU_PROVIDER_KIMI_CODING_API_KEY": "sk-kimi"},
    )

    command, _, _ = runner.calls[0]
    assert command == [
        "docker",
        "exec",
        "-e",
        "CLAWCU_PROVIDER_KIMI_CODING_API_KEY=sk-kimi",
        "clawcu-openclaw-writer",
        "node",
        "openclaw.mjs",
        "tui",
    ]


def test_exec_in_container_interactive_uses_tty_flags_with_terminal(monkeypatch) -> None:
    runner = RecordingRunner()
    manager = DockerManager(runner=runner)
    monkeypatch.setattr("clawcu.docker.sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("clawcu.docker.sys.stdout.isatty", lambda: True)
    monkeypatch.setattr("clawcu.docker.sys.stderr.isatty", lambda: True)

    manager.exec_in_container_interactive("clawcu-openclaw-writer", ["pwd"])

    command, _, _ = runner.calls[0]
    assert command == ["docker", "exec", "-i", "-t", "clawcu-openclaw-writer", "pwd"]
