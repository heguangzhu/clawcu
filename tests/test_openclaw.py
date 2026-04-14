from __future__ import annotations

from pathlib import Path

from clawcu.docker import DockerManager
from clawcu.openclaw import OpenClawManager
from clawcu.subprocess_utils import CommandError
from clawcu.models import InstanceRecord


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


def test_build_image_prefers_variant_arg_for_current_openclaw_layout(tmp_path) -> None:
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text(
        "\n".join(
            [
                "ARG OPENCLAW_VARIANT=default",
                "FROM base-slim",
                "FROM base-${OPENCLAW_VARIANT}",
            ]
        ),
        encoding="utf-8",
    )
    runner = RecordingRunner()
    manager = DockerManager(runner=runner)

    manager.build_image(tmp_path, "clawcu/openclaw:2026.4.1")

    command, cwd, _ = runner.calls[0]
    assert "--build-arg" in command
    assert "OPENCLAW_VARIANT=slim" in command
    assert "--target" not in command
    assert cwd == tmp_path


def test_build_image_falls_back_to_target_when_legacy_stage_exists(tmp_path) -> None:
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text("FROM build AS slim\n", encoding="utf-8")
    runner = RecordingRunner()
    manager = DockerManager(runner=runner)

    manager.build_image(tmp_path, "clawcu/openclaw:legacy")

    command, _, _ = runner.calls[0]
    assert "--target" in command
    assert "slim" in command


def test_pull_image_streams_progress_output() -> None:
    runner = RecordingRunner()
    manager = DockerManager(runner=runner)

    manager.pull_image("ghcr.io/openclaw/openclaw:2026.4.10")

    command, _, options = runner.calls[0]
    assert command == ["docker", "pull", "ghcr.io/openclaw/openclaw:2026.4.10"]
    assert options["stream_output"] is True


class FakeStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    def source_dir(self, service: str, version: str) -> Path:
        path = self.root / service / version
        path.parent.mkdir(parents=True, exist_ok=True)
        return path


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

    def build_image(self, source_dir: Path, image_tag: str, *, preferred_variant: str = "slim") -> None:
        self.calls.append(("build_image", (str(source_dir), image_tag, preferred_variant)))


def test_ensure_image_prefers_official_registry_pull(tmp_path) -> None:
    docker = FakeDocker()
    manager = OpenClawManager(FakeStore(tmp_path), docker)

    image_tag = manager.ensure_image("2026.4.1")

    assert image_tag == "clawcu/openclaw:2026.4.1"
    assert docker.calls == [
        ("pull_image", ("ghcr.io/openclaw/openclaw:2026.4.1",)),
        ("tag_image", ("ghcr.io/openclaw/openclaw:2026.4.1", "clawcu/openclaw:2026.4.1")),
    ]


def test_ensure_image_falls_back_to_local_build_when_official_pull_fails(tmp_path) -> None:
    docker = FakeDocker()
    docker.pull_error = CommandError(["docker", "pull", "ghcr.io/openclaw/openclaw:2026.4.1"], 1, "", "nope")
    manager = OpenClawManager(FakeStore(tmp_path), docker)

    source_dir = tmp_path / "openclaw" / "2026.4.1"
    source_dir.mkdir(parents=True)
    (source_dir / ".git").mkdir()
    (source_dir / "Dockerfile").write_text("FROM build AS slim\n", encoding="utf-8")

    image_tag = manager.ensure_image("2026.4.1")

    assert image_tag == "clawcu/openclaw:2026.4.1"
    assert docker.calls[0] == ("pull_image", ("ghcr.io/openclaw/openclaw:2026.4.1",))
    assert docker.calls[1][0] == "build_image"


def test_ensure_image_reports_missing_version_across_registry_and_git(tmp_path) -> None:
    docker = FakeDocker()
    docker.pull_error = CommandError(
        ["docker", "pull", "ghcr.io/openclaw/openclaw:2026.7.1"],
        1,
        "",
        'failed to resolve reference "ghcr.io/openclaw/openclaw:2026.7.1": not found',
    )
    messages: list[str] = []

    def failing_runner(command: list[str], *, cwd=None, capture_output=True, check=True, stream_output=False):
        raise CommandError(command, 128, "", "fatal: Remote branch v2026.7.1 not found in upstream origin")

    manager = OpenClawManager(
        FakeStore(tmp_path),
        docker,
        runner=failing_runner,
        reporter=messages.append,
    )

    try:
        manager.ensure_image("2026.7.1")
        assert False, "ensure_image should have failed"
    except RuntimeError as exc:
        assert (
            str(exc)
            == "OpenClaw version 2026.7.1 was not found in the official image registry or upstream git tag v2026.7.1."
        )

    assert (
        "Step 2/5: Official image for OpenClaw 2026.7.1 was not found. Trying the upstream git tag v2026.7.1 instead."
        in messages
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

    manager.run_container(record)

    command, _, _ = runner.calls[0]
    assert f"18809:{DockerManager.INTERNAL_GATEWAY_PORT}" in command
    assert "HOST=0.0.0.0" in command
    assert "PORT=3000" not in command


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


def test_exec_in_container_interactive_uses_tty_flags_with_terminal(monkeypatch) -> None:
    runner = RecordingRunner()
    manager = DockerManager(runner=runner)
    monkeypatch.setattr("clawcu.docker.sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("clawcu.docker.sys.stdout.isatty", lambda: True)
    monkeypatch.setattr("clawcu.docker.sys.stderr.isatty", lambda: True)

    manager.exec_in_container_interactive("clawcu-openclaw-writer", ["pwd"])

    command, _, _ = runner.calls[0]
    assert command == ["docker", "exec", "-i", "-t", "clawcu-openclaw-writer", "pwd"]
