from __future__ import annotations

from pathlib import Path

import pytest

from clawcu.core.models import ContainerRunSpec
from clawcu.docker import DockerManager
from clawcu.models import InstanceRecord
from clawcu.openclaw import OpenClawManager
from clawcu.subprocess_utils import CommandError


class RecordingRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], Path | None, dict]] = []

    def __call__(
        self,
        command: list[str],
        *,
        cwd=None,
        capture_output=True,
        check=True,
        stream_output=False,
        timeout_seconds=None,
    ):
        self.calls.append(
            (
                command,
                cwd,
                {
                    "capture_output": capture_output,
                    "check": check,
                    "stream_output": stream_output,
                    "timeout_seconds": timeout_seconds,
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
    assert options["timeout_seconds"] == DockerManager.PULL_TIMEOUT_SECONDS


def test_stream_logs_includes_tail_and_since_when_provided() -> None:
    runner = RecordingRunner()
    manager = DockerManager(runner=runner)

    manager.stream_logs(
        "clawcu-openclaw-writer",
        follow=False,
        tail=200,
        since="10m",
    )

    command, _, _ = runner.calls[0]
    assert command == [
        "docker",
        "logs",
        "--tail",
        "200",
        "--since",
        "10m",
        "clawcu-openclaw-writer",
    ]


def test_stream_logs_omits_tail_when_tail_is_none() -> None:
    runner = RecordingRunner()
    manager = DockerManager(runner=runner)

    manager.stream_logs("clawcu-openclaw-writer", follow=True, tail=None, since=None)

    command, _, _ = runner.calls[0]
    assert command == ["docker", "logs", "-f", "clawcu-openclaw-writer"]


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

    assert image_tag == "ghcr.io/openclaw/openclaw:2026.4.1"
    assert docker.calls == []


def test_pull_official_image_streams_registry_pull(tmp_path) -> None:
    docker = FakeDocker()
    messages: list[str] = []
    manager = OpenClawManager(object(), docker, reporter=messages.append)

    image_tag = manager.pull_official_image("2026.4.1")

    assert image_tag == "ghcr.io/openclaw/openclaw:2026.4.1"
    assert docker.calls == [("pull_image", ("ghcr.io/openclaw/openclaw:2026.4.1",))]
    assert messages == [
        "Step 2/5: Pulling official image ghcr.io/openclaw/openclaw:2026.4.1. This usually takes 10-60 seconds depending on your network."
    ]


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
        ),
    )

    command, _, _ = runner.calls[0]
    assert command[:5] == ["docker", "run", "-d", "--pull", "missing"]
    assert "18809:18789" in command
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

    command, _, options = runner.calls[0]
    assert "8652:8642" in command
    assert "9129:9119" in command
    assert options["timeout_seconds"] == DockerManager.RUN_TIMEOUT_SECONDS


def test_run_container_supports_additional_mount_bindings() -> None:
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
            additional_mounts=[("/tmp/javis/.hermes", "/root/.hermes")],
        ),
    )

    command, _, options = runner.calls[0]
    assert "/tmp/javis:/opt/data" in command
    assert "/tmp/javis/.hermes:/root/.hermes" in command
    assert options["timeout_seconds"] == DockerManager.RUN_TIMEOUT_SECONDS


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

    command, _, options = runner.calls[0]
    assert command[:5] == ["docker", "run", "-d", "--pull", "missing"]
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
    assert options["timeout_seconds"] == DockerManager.RUN_TIMEOUT_SECONDS


def test_stop_and_restart_container_use_short_timeout() -> None:
    runner = RecordingRunner()
    manager = DockerManager(runner=runner)

    manager.stop_container("clawcu-openclaw-writer")
    manager.restart_container("clawcu-openclaw-writer")

    stop_command, _, stop_options = runner.calls[0]
    restart_command, _, restart_options = runner.calls[1]
    assert stop_command == ["docker", "stop", "--time", "5", "clawcu-openclaw-writer"]
    assert restart_command == ["docker", "restart", "--time", "5", "clawcu-openclaw-writer"]
    assert stop_options["timeout_seconds"] == DockerManager.STOP_TIMEOUT_SECONDS
    assert restart_options["timeout_seconds"] == DockerManager.RESTART_TIMEOUT_SECONDS


def test_list_local_images_returns_sorted_tags_for_repo() -> None:
    captured: list[list[str]] = []

    def runner(command: list[str], **_kwargs):
        captured.append(list(command))
        return type(
            "Completed",
            (),
            {"stdout": "2026.4.2\n2026.4.1\n<none>\n2026.4.2\n", "stderr": "", "returncode": 0},
        )()

    manager = DockerManager(runner=runner)

    tags = manager.list_local_images("ghcr.io/openclaw/openclaw")

    assert captured[0] == [
        "docker",
        "image",
        "ls",
        "ghcr.io/openclaw/openclaw",
        "--format",
        "{{.Tag}}",
    ]
    # Sorted, deduplicated, <none> filtered out.
    assert tags == ["2026.4.1", "2026.4.2"]


def test_list_local_images_swallows_runner_errors() -> None:
    def runner(command: list[str], **_kwargs):
        raise CommandError(command, 1, "", "docker daemon is not running")

    manager = DockerManager(runner=runner)

    assert manager.list_local_images("ghcr.io/openclaw/openclaw") == []


def test_stop_container_honors_custom_timeout() -> None:
    runner = RecordingRunner()
    manager = DockerManager(runner=runner)

    manager.stop_container("clawcu-openclaw-writer", timeout=60)

    command, _, options = runner.calls[0]
    assert command == ["docker", "stop", "--time", "60", "clawcu-openclaw-writer"]
    # The outer process budget must cover the grace window + overhead,
    # not just the short STOP_TIMEOUT_SECONDS default.
    assert options["timeout_seconds"] >= 60 + 10


def test_stop_container_timeout_zero_is_allowed() -> None:
    runner = RecordingRunner()
    manager = DockerManager(runner=runner)

    manager.stop_container("clawcu-openclaw-writer", timeout=0)

    command, _, _ = runner.calls[0]
    assert command == ["docker", "stop", "--time", "0", "clawcu-openclaw-writer"]


def test_stop_container_ignores_missing_container() -> None:
    def runner(command: list[str], **_kwargs):
        raise CommandError(command, 1, "", "Error response from daemon: No such container: clawcu-openclaw-writer")

    manager = DockerManager(runner=runner)

    manager.stop_container("clawcu-openclaw-writer")


def test_remove_container_ignores_missing_container_when_allowed() -> None:
    def runner(command: list[str], **_kwargs):
        raise CommandError(command, 1, "", "Error response from daemon: No such container: clawcu-openclaw-writer")

    manager = DockerManager(runner=runner)

    manager.remove_container("clawcu-openclaw-writer", missing_ok=True)


def test_remove_container_raises_non_missing_errors_even_when_missing_ok() -> None:
    def runner(command: list[str], **_kwargs):
        raise CommandError(command, 124, "", "Timed out after 15 seconds")

    manager = DockerManager(runner=runner)

    with pytest.raises(CommandError, match="Timed out"):
        manager.remove_container("clawcu-openclaw-writer", missing_ok=True)


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
