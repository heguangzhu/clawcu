from __future__ import annotations

from typer.testing import CliRunner

from clawcu.cli import app
from clawcu.models import InstanceRecord

runner = CliRunner()


class FakeService:
    def __init__(self) -> None:
        self.pulled_versions: list[str] = []
        self.calls: list[tuple[str, tuple, dict]] = []
        self.reporter = None

    def _record(self, method: str, *args, **kwargs) -> None:
        self.calls.append((method, args, kwargs))

    def set_reporter(self, reporter) -> None:
        self.reporter = reporter

    def _instance(self, name: str = "writer", version: str = "2026.4.1") -> InstanceRecord:
        return InstanceRecord(
            service="openclaw",
            name=name,
            version=version,
            upstream_ref=f"v{version}",
            image_tag=f"clawcu/openclaw:{version}",
            container_name=f"clawcu-openclaw-{name}",
            datadir=f"/tmp/{name}",
            port=3000 if name == "writer" else 3001,
            cpu="1",
            memory="2g",
            status="running",
            created_at="2026-04-11T00:00:00+00:00",
            updated_at="2026-04-11T00:00:00+00:00",
            history=[],
        )

    def pull_openclaw(self, version: str) -> str:
        self._record("pull_openclaw", version=version)
        self.pulled_versions.append(version)
        if self.reporter:
            self.reporter("Starting OpenClaw image preparation")
        return f"clawcu/openclaw:{version}"

    def create_openclaw(self, **kwargs) -> InstanceRecord:
        self._record("create_openclaw", **kwargs)
        if self.reporter:
            self.reporter("Step 1/5: Validating options")
            self.reporter("Step 5/5: Starting the Docker container")
        return self._instance(name=kwargs["name"], version=kwargs["version"])

    def list_instances(self, *, running_only: bool = False) -> list[InstanceRecord]:
        self._record("list_instances", running_only=running_only)
        return [self._instance()]

    def inspect_instance(self, name: str) -> dict:
        self._record("inspect_instance", name=name)
        return {"instance": self._instance(name=name).to_dict(), "container": {"Name": name}}

    def start_instance(self, name: str) -> InstanceRecord:
        self._record("start_instance", name=name)
        return self._instance(name=name)

    def stop_instance(self, name: str) -> InstanceRecord:
        self._record("stop_instance", name=name)
        return self._instance(name=name)

    def restart_instance(self, name: str) -> InstanceRecord:
        self._record("restart_instance", name=name)
        return self._instance(name=name)

    def retry_instance(self, name: str) -> InstanceRecord:
        self._record("retry_instance", name=name)
        if self.reporter:
            self.reporter("Step 1/4: Loading the failed instance record")
            self.reporter("Step 4/4: Recreating the Docker container")
        record = self._instance(name=name)
        record.status = "running"
        return record

    def upgrade_instance(self, name: str, *, version: str) -> InstanceRecord:
        self._record("upgrade_instance", name=name, version=version)
        return self._instance(name=name, version=version)

    def rollback_instance(self, name: str) -> InstanceRecord:
        self._record("rollback_instance", name=name)
        return self._instance(name=name, version="2026.4.0")

    def clone_instance(self, source_name: str, *, name: str, datadir: str, port: int) -> InstanceRecord:
        self._record(
            "clone_instance",
            source_name=source_name,
            name=name,
            datadir=datadir,
            port=port,
        )
        record = self._instance(name=name)
        record.datadir = datadir
        record.port = port
        return record

    def stream_logs(self, name: str, *, follow: bool = False) -> None:
        self._record("stream_logs", name=name, follow=follow)

    def remove_instance(self, name: str, *, delete_data: bool = False) -> None:
        self._record("remove_instance", name=name, delete_data=delete_data)


def test_pull_openclaw_command(monkeypatch) -> None:
    service = FakeService()
    monkeypatch.setattr("clawcu.cli.get_service", lambda: service)

    result = runner.invoke(app, ["pull", "openclaw", "--version", "2026.4.1"])

    assert result.exit_code == 0
    assert "Built image" in result.stdout
    assert "Starting OpenClaw image preparation" in result.stdout
    assert service.pulled_versions == ["2026.4.1"]


def test_root_version_flag_prints_version() -> None:
    result = runner.invoke(app, ["--version"])

    assert result.exit_code == 0
    assert "clawcu 0.0.1" in result.stdout


def test_create_help_uses_service_language_and_lists_supported_services() -> None:
    result = runner.invoke(app, ["create", "--help"])

    assert result.exit_code == 0
    assert "Usage: " in result.stdout
    assert "create [OPTIONS] SERVICE" in result.stdout
    assert "openclaw" in result.stdout


def test_pull_help_uses_service_language_and_lists_supported_services() -> None:
    result = runner.invoke(app, ["pull", "--help"])

    assert result.exit_code == 0
    assert "pull [OPTIONS] SERVICE" in result.stdout
    assert "openclaw" in result.stdout


def test_empty_service_groups_show_help_instead_of_error() -> None:
    create_result = runner.invoke(app, ["create"])
    pull_result = runner.invoke(app, ["pull"])

    assert create_result.exit_code == 0
    assert pull_result.exit_code == 0
    assert "create [OPTIONS] SERVICE" in create_result.stdout
    assert "pull [OPTIONS] SERVICE" in pull_result.stdout
    assert "Missing command" not in create_result.stdout
    assert "Missing command" not in pull_result.stdout


def test_empty_argument_commands_show_help_instead_of_error() -> None:
    commands = [
        ("inspect", "inspect [OPTIONS] [NAME]"),
        ("start", "start [OPTIONS] [NAME]"),
        ("stop", "stop [OPTIONS] [NAME]"),
        ("restart", "restart [OPTIONS] [NAME]"),
        ("retry", "retry [OPTIONS] [NAME]"),
        ("upgrade", "upgrade [OPTIONS] [NAME]"),
        ("rollback", "rollback [OPTIONS] [NAME]"),
        ("clone", "clone [OPTIONS] [SOURCE_NAME]"),
        ("logs", "logs [OPTIONS] [NAME]"),
        ("remove", "remove [OPTIONS] [NAME]"),
    ]

    for command, usage in commands:
        result = runner.invoke(app, [command])
        assert result.exit_code == 0
        assert usage in result.stdout
        assert "Missing argument" not in result.stdout


def test_list_command_shows_instances(monkeypatch) -> None:
    service = FakeService()
    monkeypatch.setattr("clawcu.cli.get_service", lambda: service)

    result = runner.invoke(app, ["list"])

    assert result.exit_code == 0
    assert "writer" in result.stdout
    assert "2026.4.1" in result.stdout


def test_create_command_uses_defaults(monkeypatch) -> None:
    service = FakeService()
    monkeypatch.setattr("clawcu.cli.get_service", lambda: service)

    result = runner.invoke(
        app,
        [
            "create",
            "openclaw",
            "--name",
            "writer",
            "--version",
            "2026.4.1",
        ],
    )

    assert result.exit_code == 0
    assert "Step 1/5: Validating options" in result.stdout
    assert "Step 5/5: Starting the Docker container" in result.stdout
    assert service.calls[-1] == (
        "create_openclaw",
        (),
        {
            "name": "writer",
            "version": "2026.4.1",
            "datadir": None,
            "port": None,
            "cpu": "1",
            "memory": "2g",
        },
    )


def test_create_command_surfaces_duplicate_name_error(monkeypatch) -> None:
    service = FakeService()

    def fail_create(**kwargs):
        raise ValueError("Instance 'writer' already exists.")

    service.create_openclaw = fail_create
    monkeypatch.setattr("clawcu.cli.get_service", lambda: service)

    result = runner.invoke(
        app,
        [
            "create",
            "openclaw",
            "--name",
            "writer",
            "--version",
            "2026.4.1",
        ],
    )

    assert result.exit_code == 1
    assert "Instance 'writer' already exists." in result.stdout


def test_retry_command_retries_failed_instance(monkeypatch) -> None:
    service = FakeService()
    monkeypatch.setattr("clawcu.cli.get_service", lambda: service)

    result = runner.invoke(app, ["retry", "writer"])

    assert result.exit_code == 0
    assert "Step 1/4: Loading the failed instance record" in result.stdout
    assert "Retried instance:" in result.stdout
    assert service.calls[-1] == ("retry_instance", (), {"name": "writer"})


def test_create_command_accepts_explicit_resource_options(monkeypatch) -> None:
    service = FakeService()
    monkeypatch.setattr("clawcu.cli.get_service", lambda: service)

    result = runner.invoke(
        app,
        [
            "create",
            "openclaw",
            "--name",
            "writer",
            "--version",
            "2026.4.1",
            "--datadir",
            "/tmp/writer",
            "--port",
            "3000",
            "--cpu",
            "2",
            "--memory",
            "4g",
        ],
    )

    assert result.exit_code == 0
    assert service.calls[-1][2]["cpu"] == "2"
    assert service.calls[-1][2]["memory"] == "4g"


def test_list_running_option_is_forwarded(monkeypatch) -> None:
    service = FakeService()
    monkeypatch.setattr("clawcu.cli.get_service", lambda: service)

    result = runner.invoke(app, ["list", "--running"])

    assert result.exit_code == 0
    assert service.calls[-1] == ("list_instances", (), {"running_only": True})


def test_inspect_command_accepts_instance_name(monkeypatch) -> None:
    service = FakeService()
    monkeypatch.setattr("clawcu.cli.get_service", lambda: service)

    result = runner.invoke(app, ["inspect", "writer"])

    assert result.exit_code == 0
    assert '"Name": "writer"' in result.stdout
    assert service.calls[-1] == ("inspect_instance", (), {"name": "writer"})


def test_lifecycle_commands_accept_instance_name(monkeypatch) -> None:
    service = FakeService()
    monkeypatch.setattr("clawcu.cli.get_service", lambda: service)

    cases = [
        (["start", "writer"], "start_instance"),
        (["stop", "writer"], "stop_instance"),
        (["restart", "writer"], "restart_instance"),
        (["rollback", "writer"], "rollback_instance"),
    ]

    for argv, expected_call in cases:
        result = runner.invoke(app, argv)
        assert result.exit_code == 0
        assert service.calls[-1] == (expected_call, (), {"name": "writer"})


def test_upgrade_command_accepts_version_option(monkeypatch) -> None:
    service = FakeService()
    monkeypatch.setattr("clawcu.cli.get_service", lambda: service)

    result = runner.invoke(app, ["upgrade", "writer", "--version", "2026.4.2"])

    assert result.exit_code == 0
    assert service.calls[-1] == (
        "upgrade_instance",
        (),
        {"name": "writer", "version": "2026.4.2"},
    )


def test_clone_command_accepts_required_options(monkeypatch) -> None:
    service = FakeService()
    monkeypatch.setattr("clawcu.cli.get_service", lambda: service)

    result = runner.invoke(
        app,
        [
            "clone",
            "writer",
            "--name",
            "writer-exp",
            "--datadir",
            "/tmp/writer-exp",
            "--port",
            "3001",
        ],
    )

    assert result.exit_code == 0
    assert service.calls[-1] == (
        "clone_instance",
        (),
        {
            "source_name": "writer",
            "name": "writer-exp",
            "datadir": "/tmp/writer-exp",
            "port": 3001,
        },
    )


def test_logs_follow_option_is_forwarded(monkeypatch) -> None:
    service = FakeService()
    monkeypatch.setattr("clawcu.cli.get_service", lambda: service)

    result = runner.invoke(app, ["logs", "writer", "--follow"])

    assert result.exit_code == 0
    assert service.calls[-1] == (
        "stream_logs",
        (),
        {"name": "writer", "follow": True},
    )


def test_remove_delete_data_and_keep_data_flags(monkeypatch) -> None:
    service = FakeService()
    monkeypatch.setattr("clawcu.cli.get_service", lambda: service)

    delete_result = runner.invoke(app, ["remove", "writer", "--delete-data"])
    keep_result = runner.invoke(app, ["remove", "writer", "--keep-data"])

    assert delete_result.exit_code == 0
    assert keep_result.exit_code == 0
    assert service.calls[-2] == (
        "remove_instance",
        (),
        {"name": "writer", "delete_data": True},
    )
    assert service.calls[-1] == (
        "remove_instance",
        (),
        {"name": "writer", "delete_data": False},
    )
