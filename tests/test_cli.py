from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from rich.console import Console
from typer.testing import CliRunner

from clawcu.cli import _display_version, app
from clawcu.models import InstanceRecord

runner = CliRunner()


def _make_wide_console() -> Console:
    """Build a Rich console that always renders at a wide terminal width.

    CliRunner runs without a real TTY; Rich's auto-detected width defaults to
    a narrow value that truncates column headers. Tests that assert on full
    headers monkeypatch ``clawcu.cli.console`` with this instead.
    """
    return Console(width=200, force_terminal=False, no_color=True)


def test_display_version_prefers_release_date_format() -> None:
    assert _display_version("2026.4.12") == "2026.4.12"
    assert _display_version("v2026.4.8") == "2026.4.8"
    assert _display_version("v0.9.0 (2026.4.16)") == "2026.4.16"
    assert _display_version("main") == "main"


class FakeService:
    def __init__(self) -> None:
        self.pulled_versions: list[str] = []
        self.calls: list[tuple[str, tuple, dict]] = []
        self.reporter = None
        self.store = SimpleNamespace(paths=SimpleNamespace(home=Path("/tmp/clawcu-test-home")))
        self.instance_statuses: dict[str, str] = {}

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
            auth_mode="token",
            status="running",
            created_at="2026-04-11T00:00:00+00:00",
            updated_at="2026-04-11T00:00:00+00:00",
            history=[],
        )

    def _provider_summary(self, name: str = "openai-main", api_style: str = "openai") -> dict:
        return {
            "service": "openclaw",
            "name": name,
            "provider": "openai",
            "api_style": api_style,
            "api_key": "sk-test-1234567890",
            "endpoint": "https://api.example.com/v1",
            "models": ["gpt-5", "gpt-4.1"],
        }

    def _provider_payload(self, name: str = "openai-main") -> dict:
        return {
            "service": "openclaw",
            "name": name,
            "metadata": {
                "service": "openclaw",
                "provider": "openai",
                "api_style": "openai",
                "endpoint": "https://api.example.com/v1",
            },
            "auth_profiles": {
                "profiles": {
                    "openai:default": {
                        "type": "api_key",
                        "provider": "openai",
                        "key": "sk-test",
                    }
                }
            },
            "models": {
                "providers": {
                    "openai": {
                        "api": "openai-responses",
                        "baseUrl": "https://api.example.com/v1",
                        "models": [
                            {"id": "gpt-5", "name": "GPT-5"},
                            {"id": "gpt-4.1", "name": "GPT-4.1"},
                        ],
                    }
                }
            },
        }

    def pull_openclaw(self, version: str) -> str:
        self._record("pull_openclaw", version=version)
        self.pulled_versions.append(version)
        if self.reporter:
            self.reporter("Starting OpenClaw image preparation")
        return f"clawcu/openclaw:{version}"

    def pull_hermes(self, version: str) -> str:
        self._record("pull_hermes", version=version)
        self.pulled_versions.append(version)
        if self.reporter:
            self.reporter("Starting Hermes image preparation")
        return f"clawcu/hermes-agent:{version}"

    def create_openclaw(self, **kwargs) -> InstanceRecord:
        self._record("create_openclaw", **kwargs)
        if self.reporter:
            self.reporter("Step 1/5: Validating options")
            self.reporter("Step 5/5: Starting the Docker container")
        return self._instance(name=kwargs["name"], version=kwargs["version"])

    def create_hermes(self, **kwargs) -> InstanceRecord:
        self._record("create_hermes", **kwargs)
        if self.reporter:
            self.reporter("Step 1/5: Validating options")
            self.reporter("Step 5/5: Starting the Docker container")
        record = self._instance(name=kwargs["name"], version=kwargs["version"])
        record.service = "hermes"
        record.container_name = f"clawcu-hermes-{kwargs['name']}"
        record.image_tag = f"clawcu/hermes-agent:{kwargs['version']}"
        record.auth_mode = "native"
        record.port = kwargs.get("port") or 8652
        return record

    def check_setup(self) -> list[dict[str, str | bool]]:
        self._record("check_setup")
        return [
            {
                "name": "docker_cli",
                "status": "ok",
                "ok": True,
                "summary": "Docker CLI is installed at /usr/local/bin/docker.",
                "hint": "",
            },
            {
                "name": "docker_daemon",
                "status": "ok",
                "ok": True,
                "summary": "Docker daemon is running (server 29.3.1).",
                "hint": "",
            },
            {
                "name": "clawcu_home",
                "status": "ok",
                "ok": True,
                "summary": "ClawCU home directory is ready at /tmp/clawcu-test-home.",
                "hint": "",
            },
            {
                "name": "clawcu_runtime_dirs",
                "status": "ok",
                "ok": True,
                "summary": "ClawCU runtime directories are ready: /tmp/clawcu-test-home/instances, /tmp/clawcu-test-home/sources, /tmp/clawcu-test-home/logs, /tmp/clawcu-test-home/snapshots.",
                "hint": "",
            },
            {
                "name": "openclaw_image_repo",
                "status": "ok",
                "ok": True,
                "summary": "OpenClaw image repo is configured as ghcr.io/openclaw/openclaw.",
                "hint": "",
            },
            {
                "name": "hermes_image_repo",
                "status": "ok",
                "ok": True,
                "summary": "Hermes image repo is configured as clawcu/hermes-agent.",
                "hint": "",
            },
        ]

    def get_openclaw_image_repo(self) -> str:
        self._record("get_openclaw_image_repo")
        return "ghcr.io/openclaw/openclaw"

    def set_openclaw_image_repo(self, image_repo: str) -> str:
        self._record("set_openclaw_image_repo", image_repo=image_repo)
        return image_repo

    def get_hermes_image_repo(self) -> str:
        self._record("get_hermes_image_repo")
        return "clawcu/hermes-agent"

    def set_hermes_image_repo(self, image_repo: str) -> str:
        self._record("set_hermes_image_repo", image_repo=image_repo)
        return image_repo

    def suggest_openclaw_image_repo(self) -> str:
        self._record("suggest_openclaw_image_repo")
        return "ghcr.io/openclaw/openclaw"

    def get_clawcu_home(self) -> str:
        self._record("get_clawcu_home")
        return "/tmp/clawcu-test-home"

    def set_clawcu_home(self, home: str) -> str:
        self._record("set_clawcu_home", home=home)
        return home

    def collect_providers(self, **kwargs) -> dict[str, list[str]]:
        self._record("collect_providers", **kwargs)
        return {
            "saved": ["openai-main (instance:writer)"],
            "merged": ["openai-main (instance:writer)"],
            "skipped": ["openai-main-2 (instance:writer)"],
            "scanned": ["/tmp/writer"],
        }

    def list_providers(self) -> list[dict]:
        self._record("list_providers")
        return [self._provider_summary()]

    def show_provider(self, name: str) -> dict:
        self._record("show_provider", name=name)
        return self._provider_payload(name=name)

    def apply_provider(
        self,
        provider: str,
        instance: str,
        agent: str = "main",
        *,
        primary: str | None = None,
        fallbacks: list[str] | None = None,
        persist: bool = False,
    ) -> dict:
        self._record(
            "apply_provider",
            provider=provider,
            instance=instance,
            agent=agent,
            persist=persist,
            primary=primary,
            fallbacks=fallbacks,
        )
        return {
            "provider": provider,
            "instance": instance,
            "agent": agent,
            "runtime_dir": f"/tmp/{instance}/agents/{agent}/agent",
            "env_key": "CLAWCU_PROVIDER_OPENAI_API_KEY" if persist else "-",
            "persist": "yes" if persist else "no",
            "primary": primary or "-",
            "fallbacks": ", ".join(fallbacks) if fallbacks else "-",
        }

    def remove_provider(self, name: str) -> None:
        self._record("remove_provider", name=name)

    def list_provider_models(self, name: str) -> list[str]:
        self._record("list_provider_models", name=name)
        return ["gpt-5", "gpt-4.1"]

    def list_instances(self, *, running_only: bool = False) -> list[InstanceRecord]:
        self._record("list_instances", running_only=running_only)
        return [self._instance()]

    def list_instance_summaries(self, *, running_only: bool = False) -> list[dict]:
        self._record("list_instance_summaries", running_only=running_only)
        payload = self._instance().to_dict()
        payload.update(
            {
                "source": "managed",
                "home": "/Users/test/.clawcu/writer",
                "providers": "openai, anthropic",
                "models": "openai/gpt-5, anthropic/claude-sonnet-4.5",
                "snapshot": "upgrade 2026.4.1 -> 2026.4.2",
            }
        )
        return [payload]

    def list_agent_summaries(self, *, running_only: bool = False) -> list[dict]:
        self._record("list_agent_summaries", running_only=running_only)
        return [
            {
                "source": "managed",
                "instance": "writer",
                "home": "/Users/test/.clawcu/writer",
                "agent": "main",
                "service": "openclaw",
                "version": "2026.4.1",
                "port": 3000,
                "status": "running",
                "primary": "openai/gpt-5",
                "fallbacks": "anthropic/claude-sonnet-4.5",
                "providers": "openai, anthropic",
                "models": "openai/gpt-5, anthropic/claude-sonnet-4.5",
            },
            {
                "source": "managed",
                "instance": "writer",
                "home": "/Users/test/.clawcu/writer",
                "agent": "chat",
                "service": "openclaw",
                "version": "2026.4.1",
                "port": 3000,
                "status": "running",
                "primary": "anthropic/claude-sonnet-4.5",
                "fallbacks": "-",
                "providers": "openai, anthropic",
                "models": "openai/gpt-5, anthropic/claude-sonnet-4.5",
            },
        ]

    def list_local_instance_summaries(self) -> list[dict]:
        self._record("list_local_instance_summaries")
        return [
            {
                "source": "local",
                "name": "local",
                "home": "/Users/test/.openclaw",
                "version": "2026.4.1",
                "port": 18789,
                "status": "local",
                "providers": "openai, anthropic",
                "models": "openai/gpt-5, anthropic/claude-sonnet-4.5",
            }
        ]

    def list_local_agent_summaries(self) -> list[dict]:
        self._record("list_local_agent_summaries")
        return [
            {
                "source": "local",
                "instance": "local",
                "home": "/Users/test/.openclaw",
                "agent": "main",
                "service": "openclaw",
                "version": "2026.4.1",
                "port": 18789,
                "status": "local",
                "primary": "openai/gpt-5",
                "fallbacks": "anthropic/claude-sonnet-4.5",
                "providers": "openai, anthropic",
                "models": "openai/gpt-5, anthropic/claude-sonnet-4.5",
            }
        ]

    def dashboard_url(self, name: str) -> str:
        self._record("dashboard_url", name=name)
        return f"http://127.0.0.1:3000/#token=token-{name}"

    def inspect_instance(self, name: str) -> dict:
        self._record("inspect_instance", name=name)
        return {
            "instance": self._instance(name=name).to_dict(),
            "snapshots": {
                "latest_upgrade_snapshot": f"/tmp/{name}-upgrade-snapshot",
                "latest_rollback_snapshot": f"/tmp/{name}-rollback-snapshot",
                "latest_restored_snapshot": f"/tmp/{name}-upgrade-snapshot",
            },
            "container": {"Name": name},
        }

    def token(self, name: str) -> str:
        self._record("token", name=name)
        return f"token-{name}"

    def set_instance_env(self, name: str, assignments: list[str]) -> dict:
        self._record("set_instance_env", name=name, assignments=assignments)
        return {
            "instance": name,
            "path": f"/tmp/{name}.env",
            "updated_keys": [item.split("=", 1)[0] for item in assignments],
            "status": "running",
        }

    def get_instance_env(self, name: str) -> dict:
        self._record("get_instance_env", name=name)
        return {
            "instance": name,
            "path": f"/tmp/{name}.env",
            "values": {
                "OPENAI_API_KEY": "sk-test",
                "OPENAI_BASE_URL": "https://api.example.com/v1",
            },
            "status": "running",
        }

    def unset_instance_env(self, name: str, keys: list[str]) -> dict:
        self._record("unset_instance_env", name=name, keys=keys)
        return {
            "instance": name,
            "path": f"/tmp/{name}.env",
            "removed_keys": [key for key in keys if key == "OPENAI_API_KEY"],
            "status": "running",
        }

    def approve_pairing(self, name: str, request_id: str | None = None) -> str:
        self._record("approve_pairing", name=name, request_id=request_id)
        if self.reporter:
            self.reporter("Approving pairing request")
        return request_id or "latest-request"

    def configure_instance(self, name: str, extra_args: list[str] | None = None) -> None:
        self._record("configure_instance", name=name, extra_args=extra_args or [])

    def exec_instance(self, name: str, command: list[str]) -> None:
        self._record("exec_instance", name=name, command=command)

    def tui_instance(self, name: str, agent: str = "main") -> None:
        self._record("tui_instance", name=name, agent=agent)

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
        status = self.instance_statuses.get(name, "running")
        if status != "create_failed":
            raise ValueError(
                f"Instance '{name}' is in status '{status}'. Only create_failed instances can be retried."
            )
        if self.reporter:
            self.reporter("Step 1/4: Loading the failed instance record")
            self.reporter("Step 4/4: Recreating the Docker container")
        record = self._instance(name=name)
        record.status = "running"
        return record

    def recreate_instance(self, name: str) -> InstanceRecord:
        self._record("recreate_instance", name=name)
        if self.reporter:
            self.reporter("Recreating instance")
        record = self._instance(name=name)
        return record

    def upgrade_instance(self, name: str, *, version: str) -> InstanceRecord:
        self._record("upgrade_instance", name=name, version=version)
        if self.reporter:
            self.reporter("Step 1/4: Preparing an upgrade plan")
            self.reporter("Step 4/4: Recreating the container")
        return self._instance(name=name, version=version)

    def rollback_instance(self, name: str) -> InstanceRecord:
        self._record("rollback_instance", name=name)
        if self.reporter:
            self.reporter("Step 1/4: Preparing to roll back")
            self.reporter("Step 4/4: Starting OpenClaw")
        return self._instance(name=name, version="2026.4.0")

    def clone_instance(
        self,
        source_name: str,
        *,
        name: str,
        datadir: str | None = None,
        port: int | None = None,
    ) -> InstanceRecord:
        if self.reporter:
            self.reporter("Step 1/5: Validating the source instance")
            self.reporter("Step 5/5: Starting the cloned Docker container")
        self._record(
            "clone_instance",
            source_name=source_name,
            name=name,
            datadir=datadir,
            port=port,
        )
        record = self._instance(name=name)
        if datadir is not None:
            record.datadir = datadir
        if port is not None:
            record.port = port
        return record

    def stream_logs(
        self,
        name: str,
        *,
        follow: bool = False,
        tail: int | None = None,
        since: str | None = None,
    ) -> None:
        self._record("stream_logs", name=name, follow=follow, tail=tail, since=since)

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


def test_pull_hermes_command(monkeypatch) -> None:
    service = FakeService()
    monkeypatch.setattr("clawcu.cli.get_service", lambda: service)

    result = runner.invoke(app, ["pull", "hermes", "--version", "v0.9.0"])

    assert result.exit_code == 0
    assert "Built image" in result.stdout
    assert "Starting Hermes image preparation" in result.stdout
    assert service.calls[0] == ("pull_hermes", (), {"version": "v0.9.0"})


def test_root_version_flag_prints_version() -> None:
    result = runner.invoke(app, ["--version"])

    assert result.exit_code == 0
    from clawcu import __version__

    assert f"clawcu {__version__}" in result.stdout


def test_create_help_uses_service_language_and_lists_supported_services() -> None:
    result = runner.invoke(app, ["create", "--help"])

    assert result.exit_code == 0
    assert "Usage: " in result.stdout
    assert "create [OPTIONS] SERVICE" in result.stdout
    assert "openclaw" in result.stdout
    assert "hermes" in result.stdout


def test_pull_help_uses_service_language_and_lists_supported_services() -> None:
    result = runner.invoke(app, ["pull", "--help"])

    assert result.exit_code == 0
    assert "pull [OPTIONS] SERVICE" in result.stdout
    assert "openclaw" in result.stdout
    assert "hermes" in result.stdout


def test_root_help_lists_descriptions_for_top_level_commands() -> None:
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "--install-completion" not in result.stdout
    assert "--show-completion" not in result.stdout
    assert "list" in result.stdout
    assert "List managed instances." in result.stdout
    assert "inspect" in result.stdout
    assert "Show detailed state for a managed instance." in result.stdout
    assert "token" in result.stdout
    assert "Print the dashboard token for a managed instance." in result.stdout
    assert "setup" in result.stdout
    assert "Check local prerequisites and configure the default ClawCU home" in result.stdout
    assert "and service image repos." in result.stdout
    assert "provider" in result.stdout
    assert "Collect and reuse model configuration assets" in result.stdout
    assert "approve" in result.stdout
    assert "Approve a pending browser pairing request for an instance." in result.stdout
    assert "config" in result.stdout
    assert "Run the native configuration flow inside a managed instance." in result.stdout
    assert "exec" in result.stdout
    assert "Run an arbitrary command inside a managed instance container." in result.stdout
    assert "start" in result.stdout
    assert "Start a stopped managed instance." in result.stdout
    assert "stop" in result.stdout
    assert "Stop a running managed instance." in result.stdout
    assert "restart" in result.stdout
    assert "Restart a managed instance." in result.stdout
    assert "recreate" in result.stdout
    assert "Recreate an existing instance." in result.stdout
    assert "Auto-retries instances in" in result.stdout
    assert "upgrade" in result.stdout
    assert "Upgrade an instance to a newer service version" in result.stdout
    assert "rollback" in result.stdout
    assert "data-directory and env snapshot" in result.stdout
    assert "clone" in result.stdout
    assert "Clone an existing instance into a separate experiment instance." in result.stdout
    assert "logs" in result.stdout
    assert "Stream or print Docker logs for a managed instance." in result.stdout
    assert "remove" in result.stdout
    assert "Remove an instance and optionally delete its data directory." in result.stdout


def test_create_help_no_longer_exposes_auth_option() -> None:
    result = runner.invoke(app, ["create", "openclaw", "--help"])

    assert result.exit_code == 0
    assert "--auth" not in result.stdout


def test_upgrade_and_rollback_help_mentions_env_snapshots() -> None:
    upgrade_result = runner.invoke(app, ["upgrade", "--help"])
    rollback_result = runner.invoke(app, ["rollback", "--help"])

    assert upgrade_result.exit_code == 0
    assert "data directory and env file" in upgrade_result.stdout
    assert rollback_result.exit_code == 0
    assert "data-directory and env snapshot" in rollback_result.stdout


def test_provider_help_lists_subcommands() -> None:
    result = runner.invoke(app, ["provider", "--help"])

    assert result.exit_code == 0
    assert "Collect and reuse model configuration assets from managed instances" in result.stdout
    assert "local" in result.stdout
    assert "homes." in result.stdout
    assert "collect" in result.stdout
    assert "Collect model configuration assets from managed instances or local" in result.stdout
    assert "agent homes." in result.stdout
    assert "list" in result.stdout
    assert "List all collected provider assets." in result.stdout
    assert "show" in result.stdout
    assert "Show the collected auth-profiles.json and models.json" in result.stdout
    assert "apply" in result.stdout
    assert "Apply a collected provider to a managed instance agent." in result.stdout
    assert "remove" in result.stdout
    assert "Remove a collected provider directory." in result.stdout
    assert "models" in result.stdout


def test_provider_models_help_lists_subcommands() -> None:
    result = runner.invoke(app, ["provider", "models", "--help"])

    assert result.exit_code == 0
    assert "Inspect the models stored in a collected provider." in result.stdout
    assert "list" in result.stdout
    assert "List the models stored in a collected provider." in result.stdout


def test_setup_command_reports_success(monkeypatch) -> None:
    service = FakeService()
    monkeypatch.setattr("clawcu.cli.get_service", lambda: service)
    monkeypatch.setattr("clawcu.cli._is_interactive_stdin", lambda: False)
    monkeypatch.setattr(
        "clawcu.cli._completion_check",
        lambda _service: {
            "name": "shell_completion",
            "status": "warn",
            "summary": "Shell completion script is ready for zsh at /tmp/clawcu-test-home/completions/_clawcu, but your shell profile does not appear to load it yet.",
            "hint": "Add this to ~/.zshrc: fpath=(/tmp/clawcu-test-home/completions $fpath) && autoload -Uz compinit && compinit",
        },
    )

    result = runner.invoke(app, ["setup"])

    assert result.exit_code == 0
    assert "Checking local prerequisites for ClawCU..." in result.stdout
    assert "Docker CLI is installed" in result.stdout
    assert "Docker daemon is running" in result.stdout
    assert "ClawCU home directory is ready" in result.stdout
    assert "ClawCU runtime directories are ready" in result.stdout
    assert "OpenClaw image repo is configured as ghcr.io/openclaw/openclaw." in result.stdout
    assert "Hermes image repo is configured as clawcu/hermes-agent." in result.stdout
    assert "Shell completion script is ready for zsh" not in result.stdout
    assert "ClawCU setup check passed." in result.stdout
    assert service.calls[0] == ("check_setup", (), {})


def test_setup_command_returns_nonzero_when_a_check_fails(monkeypatch) -> None:
    service = FakeService()

    def failing_setup() -> list[dict[str, str | bool]]:
        return [
            {
                "name": "docker_cli",
                "ok": False,
                "summary": "Docker CLI is not installed.",
                "hint": "Install Docker Desktop.",
            }
        ]

    service.check_setup = failing_setup
    monkeypatch.setattr("clawcu.cli.get_service", lambda: service)
    monkeypatch.setattr("clawcu.cli._is_interactive_stdin", lambda: False)
    monkeypatch.setattr(
        "clawcu.cli._completion_check",
        lambda _service: {
            "name": "shell_completion",
            "status": "warn",
            "summary": "Shell completion script is ready.",
            "hint": "",
        },
    )

    result = runner.invoke(app, ["setup"])

    assert result.exit_code == 1
    assert "Docker CLI is not installed." in result.stdout
    assert "Hint: Install Docker Desktop." in result.stdout


def test_setup_command_prompts_for_openclaw_image_repo_in_interactive_shell(monkeypatch) -> None:
    service = FakeService()
    monkeypatch.setattr("clawcu.cli.get_service", lambda: service)
    monkeypatch.setattr("clawcu.cli._is_interactive_stdin", lambda: True)
    answers = iter(
        [
            "/tmp/custom-clawcu-home",
            "registry.example.com/openclaw/openclaw",
            "registry.example.com/hermes-agent",
        ]
    )
    monkeypatch.setattr(
        "clawcu.cli.typer.prompt",
        lambda *_args, **_kwargs: next(answers),
    )
    monkeypatch.setattr(
        "clawcu.cli._completion_check",
        lambda _service: {
            "name": "shell_completion",
            "status": "ok",
            "summary": "Shell completion script is ready.",
            "hint": "",
        },
    )

    result = runner.invoke(app, ["setup"])

    assert result.exit_code == 0
    assert "ClawCU home" in result.stdout
    assert "OpenClaw image repo" in result.stdout
    assert "Hermes image repo" in result.stdout
    assert "Saved ClawCU home: /tmp/custom-clawcu-home" in result.stdout
    assert "Saved OpenClaw image repo: registry.example.com/openclaw/openclaw" in result.stdout
    assert "Saved Hermes image repo: registry.example.com/hermes-agent" in result.stdout
    assert ("get_clawcu_home", (), {}) in service.calls
    assert ("suggest_openclaw_image_repo", (), {}) in service.calls
    assert ("get_hermes_image_repo", (), {}) in service.calls
    assert (
        "set_clawcu_home",
        (),
        {"home": "/tmp/custom-clawcu-home"},
    ) in service.calls
    assert (
        "set_hermes_image_repo",
        (),
        {"image_repo": "registry.example.com/hermes-agent"},
    ) in service.calls


def test_setup_command_uses_existing_hermes_repo_as_interactive_default(monkeypatch) -> None:
    service = FakeService()
    monkeypatch.setattr("clawcu.cli.get_service", lambda: service)
    monkeypatch.setattr("clawcu.cli._is_interactive_stdin", lambda: True)
    prompts: list[tuple[tuple, dict]] = []
    answers = iter(
        [
            "/tmp/custom-clawcu-home",
            "ghcr.nju.edu.cn/openclaw/openclaw",
            "clawcu/hermes-agent",
        ]
    )

    def fake_prompt(*args, **kwargs):
        prompts.append((args, kwargs))
        return next(answers)

    monkeypatch.setattr("clawcu.cli.typer.prompt", fake_prompt)
    monkeypatch.setattr(service, "suggest_openclaw_image_repo", lambda: "ghcr.nju.edu.cn/openclaw/openclaw")

    result = runner.invoke(app, ["setup"])

    assert result.exit_code == 0
    assert prompts[1][0][0] == "OpenClaw image repo"
    assert prompts[1][1]["default"] == "ghcr.nju.edu.cn/openclaw/openclaw"
    assert prompts[2][0][0] == "Hermes image repo"
    assert prompts[2][1]["default"] == "clawcu/hermes-agent"
    assert (
        "set_openclaw_image_repo",
        (),
        {"image_repo": "ghcr.nju.edu.cn/openclaw/openclaw"},
    ) in service.calls


def test_setup_command_shows_completion_only_when_requested(monkeypatch) -> None:
    service = FakeService()
    monkeypatch.setattr("clawcu.cli.get_service", lambda: service)
    monkeypatch.setattr("clawcu.cli._is_interactive_stdin", lambda: False)
    monkeypatch.setattr(
        "clawcu.cli._completion_check",
        lambda _service: {
            "name": "shell_completion",
            "status": "warn",
            "summary": "Shell completion script is ready for zsh at /tmp/clawcu-test-home/completions/_clawcu, but your shell profile does not appear to load it yet.",
            "hint": "Add this to ~/.zshrc: fpath=(/tmp/clawcu-test-home/completions $fpath) && autoload -Uz compinit && compinit",
        },
    )

    result = runner.invoke(app, ["setup", "--completion"])

    assert result.exit_code == 0
    assert "Shell completion script is ready for zsh" in result.stdout
    assert "Hint: Add this to ~/.zshrc" in result.stdout


def test_setup_command_noninteractive_shows_config_hint(monkeypatch) -> None:
    service = FakeService()
    monkeypatch.setattr("clawcu.cli.get_service", lambda: service)
    monkeypatch.setattr("clawcu.cli._is_interactive_stdin", lambda: False)

    result = runner.invoke(app, ["setup"])

    assert result.exit_code == 0
    assert "Non-interactive shell detected." in result.stdout
    assert "--hermes-image-repo" in result.stdout


def test_setup_command_noninteractive_applies_explicit_options(monkeypatch) -> None:
    service = FakeService()
    monkeypatch.setattr("clawcu.cli.get_service", lambda: service)
    monkeypatch.setattr("clawcu.cli._is_interactive_stdin", lambda: False)

    result = runner.invoke(
        app,
        [
            "setup",
            "--clawcu-home",
            "/tmp/noninteractive-home",
            "--openclaw-image-repo",
            "registry.example.com/openclaw/openclaw",
            "--hermes-image-repo",
            "registry.example.com/hermes-agent",
        ],
    )

    assert result.exit_code == 0
    assert "Saved ClawCU home: /tmp/noninteractive-home" in result.stdout
    assert "Saved OpenClaw image repo: registry.example.com/openclaw/openclaw" in result.stdout
    assert "Saved Hermes image repo: registry.example.com/hermes-agent" in result.stdout
    assert (
        "set_clawcu_home",
        (),
        {"home": "/tmp/noninteractive-home"},
    ) in service.calls
    assert (
        "set_openclaw_image_repo",
        (),
        {"image_repo": "registry.example.com/openclaw/openclaw"},
    ) in service.calls
    assert (
        "set_hermes_image_repo",
        (),
        {"image_repo": "registry.example.com/hermes-agent"},
    ) in service.calls


def test_recreate_help_no_longer_exposes_auth_option() -> None:
    result = runner.invoke(app, ["recreate", "--help"])

    assert result.exit_code == 0
    assert "--auth" not in result.stdout


def test_config_help_explains_passthrough_usage() -> None:
    result = runner.invoke(app, ["config", "--help"])

    assert result.exit_code == 0
    assert "service-native setup or configuration flow" in result.stdout
    assert "clawcu config <instance>" in result.stdout
    assert "clawcu config <instance> -- --help" in result.stdout


def test_exec_help_explains_passthrough_usage() -> None:
    result = runner.invoke(app, ["exec", "--help"])

    assert result.exit_code == 0
    assert "provided command inside the managed instance container" in result.stdout
    assert "clawcu exec <instance> openclaw config" in result.stdout
    assert "clawcu exec <instance> pwd" in result.stdout
    assert "clawcu exec <instance> ls" in result.stdout


def test_empty_service_groups_show_help_instead_of_error() -> None:
    create_result = runner.invoke(app, ["create"])
    pull_result = runner.invoke(app, ["pull"])
    provider_result = runner.invoke(app, ["provider"])
    provider_models_result = runner.invoke(app, ["provider", "models"])

    assert create_result.exit_code == 0
    assert pull_result.exit_code == 0
    assert provider_result.exit_code == 0
    assert provider_models_result.exit_code == 0
    assert "create [OPTIONS] SERVICE" in create_result.stdout
    assert "pull [OPTIONS] SERVICE" in pull_result.stdout
    assert "provider [OPTIONS] COMMAND [ARGS]..." in provider_result.stdout
    assert "provider models [OPTIONS] COMMAND [ARGS]..." in provider_models_result.stdout
    assert "Missing command" not in create_result.stdout
    assert "Missing command" not in pull_result.stdout


def test_empty_argument_commands_show_help_instead_of_error() -> None:
    commands = [
        ("inspect", "inspect [OPTIONS] [NAME]"),
        ("token", "token [OPTIONS] [NAME]"),
        ("provider collect", "provider collect [OPTIONS]"),
        ("provider show", "provider show [OPTIONS] [NAME]"),
        ("provider apply", "provider apply [OPTIONS] [PROVIDER] [INSTANCE]"),
        ("provider remove", "provider remove [OPTIONS] [NAME]"),
        ("provider models list", "provider models list [OPTIONS] [NAME]"),
        ("exec", "exec [OPTIONS] [NAME] COMMAND [ARGS]..."),
        ("start", "start [OPTIONS] [NAME]"),
        ("stop", "stop [OPTIONS] [NAME]"),
        ("restart", "restart [OPTIONS] [NAME]"),
        ("recreate", "recreate [OPTIONS] [NAME]"),
        ("upgrade", "upgrade [OPTIONS] [NAME]"),
        ("rollback", "rollback [OPTIONS] [NAME]"),
        ("clone", "clone [OPTIONS] [SOURCE_NAME]"),
        ("logs", "logs [OPTIONS] [NAME]"),
        ("remove", "remove [OPTIONS] [NAME]"),
    ]

    for command, usage in commands:
        result = runner.invoke(app, command.split())
        assert result.exit_code == 0
        assert usage in result.stdout
        assert "Missing argument" not in result.stdout


def test_list_command_defaults_to_managed_source(monkeypatch) -> None:
    service = FakeService()
    monkeypatch.setattr("clawcu.cli.get_service", lambda: service)

    result = runner.invoke(app, ["list"])

    assert result.exit_code == 0
    assert "ClawCU Instances" in result.stdout
    # Default no longer mixes local pseudo-instances into the table.
    assert service.calls == [
        ("list_instance_summaries", (), {"running_only": False}),
    ]


def test_list_command_source_all_includes_local(monkeypatch) -> None:
    service = FakeService()
    monkeypatch.setattr("clawcu.cli.get_service", lambda: service)

    result = runner.invoke(app, ["list", "--source", "all"])

    assert result.exit_code == 0
    assert service.calls == [
        ("list_local_instance_summaries", (), {}),
        ("list_instance_summaries", (), {"running_only": False}),
    ]


def test_list_command_service_filter_drops_other_services(monkeypatch) -> None:
    service = FakeService()
    monkeypatch.setattr("clawcu.cli.get_service", lambda: service)

    # Fake service returns openclaw rows; hermes filter yields nothing.
    result = runner.invoke(app, ["list", "--service", "hermes"])

    assert result.exit_code == 0
    assert "No instances found." in result.stdout


def test_list_command_status_filter_drops_other_statuses(monkeypatch) -> None:
    service = FakeService()
    monkeypatch.setattr("clawcu.cli.get_service", lambda: service)

    result = runner.invoke(app, ["list", "--status", "stopped"])

    assert result.exit_code == 0
    assert "No instances found." in result.stdout


def test_list_command_rejects_unknown_source(monkeypatch) -> None:
    service = FakeService()
    monkeypatch.setattr("clawcu.cli.get_service", lambda: service)

    result = runner.invoke(app, ["list", "--source", "bogus"])

    assert result.exit_code == 1
    assert "Unknown --source 'bogus'" in result.stdout


def test_provider_collect_command_accepts_source_selection(monkeypatch) -> None:
    service = FakeService()
    monkeypatch.setattr("clawcu.cli.get_service", lambda: service)

    result = runner.invoke(
        app,
        [
            "provider",
            "collect",
            "--instance",
            "writer",
        ],
    )

    assert result.exit_code == 0
    assert "Collected provider:" in result.stdout
    assert "Merged duplicate:" in result.stdout
    assert "Skipped duplicate:" in result.stdout
    assert "Collect summary: scanned 1 source(s), collected 1, merged 1, skipped 1." in result.stdout
    assert service.calls[0] == (
        "collect_providers",
        (),
        {
            "all_instances": False,
            "instance": "writer",
            "path": None,
        },
    )


def test_token_command_prints_instance_token(monkeypatch) -> None:
    service = FakeService()
    monkeypatch.setattr("clawcu.cli.get_service", lambda: service)

    result = runner.invoke(app, ["token", "writer"])

    assert result.exit_code == 0
    assert "token-writer" in result.stdout
    assert service.calls[0] == ("token", (), {"name": "writer"})


def test_provider_apply_command_supports_persist(monkeypatch) -> None:
    service = FakeService()
    monkeypatch.setattr("clawcu.cli.get_service", lambda: service)

    result = runner.invoke(
        app,
        [
            "provider",
            "apply",
            "openai",
            "writer",
            "--agent",
            "chat",
            "--persist",
            "--primary",
            "openai/gpt-5",
        ],
    )

    assert result.exit_code == 0
    assert "Applied provider:" in result.stdout
    assert "Persistence:" in result.stdout
    assert service.calls[0] == (
        "apply_provider",
        (),
        {
            "provider": "openai",
            "instance": "writer",
            "agent": "chat",
            "persist": True,
            "primary": "openai/gpt-5",
            "fallbacks": None,
        },
    )


def test_setenv_command_updates_instance_env(monkeypatch) -> None:
    service = FakeService()
    monkeypatch.setattr("clawcu.cli.get_service", lambda: service)

    result = runner.invoke(
        app,
        ["setenv", "writer", "OPENAI_API_KEY=sk-test", "OPENAI_BASE_URL=https://api.example.com/v1"],
    )

    assert result.exit_code == 0
    assert "/tmp/writer.env" in result.stdout
    assert "OPENAI_API_KEY" in result.stdout
    assert "Changes will apply the next time the container is recreated." in result.stdout
    assert "recreate writer" in result.stdout
    assert service.calls[0] == (
        "set_instance_env",
        (),
        {
            "name": "writer",
            "assignments": [
                "OPENAI_API_KEY=sk-test",
                "OPENAI_BASE_URL=https://api.example.com/v1",
            ],
        },
    )


def test_setenv_command_can_recreate_instance_immediately(monkeypatch) -> None:
    service = FakeService()
    monkeypatch.setattr("clawcu.cli.get_service", lambda: service)

    result = runner.invoke(
        app,
        ["setenv", "writer", "OPENAI_API_KEY=sk-test", "--apply"],
    )

    assert result.exit_code == 0
    assert "/tmp/writer.env" in result.stdout
    assert "Recreated instance:" in result.stdout
    assert "Open URL:" in result.stdout
    assert service.calls[0] == (
        "set_instance_env",
        (),
        {
            "name": "writer",
            "assignments": ["OPENAI_API_KEY=sk-test"],
        },
    )
    assert service.calls[1] == ("recreate_instance", (), {"name": "writer"})


def test_getenv_command_lists_instance_environment(monkeypatch) -> None:
    service = FakeService()
    monkeypatch.setattr("clawcu.cli.get_service", lambda: service)

    result = runner.invoke(app, ["getenv", "writer"])

    assert result.exit_code == 0
    # Sensitive keys must be masked by default
    assert "OPENAI_API_KEY=sk-test\n" not in result.stdout
    assert "OPENAI_API_KEY=" in result.stdout
    # Non-sensitive values stay readable
    assert "OPENAI_BASE_URL=https://api.example.com/v1" in result.stdout
    assert "--reveal" in result.stdout
    assert service.calls[0] == ("get_instance_env", (), {"name": "writer"})


def test_getenv_command_reveal_shows_raw_values(monkeypatch) -> None:
    service = FakeService()
    monkeypatch.setattr("clawcu.cli.get_service", lambda: service)

    result = runner.invoke(app, ["getenv", "writer", "--reveal"])

    assert result.exit_code == 0
    assert "OPENAI_API_KEY=sk-test" in result.stdout
    assert "OPENAI_BASE_URL=https://api.example.com/v1" in result.stdout


def test_unsetenv_command_removes_instance_environment(monkeypatch) -> None:
    service = FakeService()
    monkeypatch.setattr("clawcu.cli.get_service", lambda: service)

    result = runner.invoke(app, ["unsetenv", "writer", "OPENAI_API_KEY", "MISSING_KEY"])

    assert result.exit_code == 0
    assert "/tmp/writer.env" in result.stdout
    assert "removed: OPENAI_API_KEY" in result.stdout
    assert service.calls[0] == (
        "unset_instance_env",
        (),
        {"name": "writer", "keys": ["OPENAI_API_KEY", "MISSING_KEY"]},
    )


def test_unsetenv_command_can_recreate_instance_immediately(monkeypatch) -> None:
    service = FakeService()
    monkeypatch.setattr("clawcu.cli.get_service", lambda: service)

    result = runner.invoke(app, ["unsetenv", "writer", "OPENAI_API_KEY", "--apply"])

    assert result.exit_code == 0
    assert "Recreated instance:" in result.stdout
    assert "Open URL:" in result.stdout
    assert service.calls[0] == (
        "unset_instance_env",
        (),
        {"name": "writer", "keys": ["OPENAI_API_KEY"]},
    )
    assert service.calls[1] == ("recreate_instance", (), {"name": "writer"})


def test_provider_list_command_shows_providers(monkeypatch) -> None:
    service = FakeService()
    monkeypatch.setattr("clawcu.cli.get_service", lambda: service)

    result = runner.invoke(app, ["provider", "list"])

    assert result.exit_code == 0
    assert "openai" in result.stdout
    assert "API_KEY" in result.stdout
    # Narrow default: key column shows status only, never any key bytes
    assert "set" in result.stdout
    assert "sk-tes" not in result.stdout
    assert "sk-test-1234567890" not in result.stdout
    assert service.calls[0] == ("list_providers", (), {})


def test_provider_list_wide_masks_key_by_default(monkeypatch) -> None:
    service = FakeService()
    monkeypatch.setattr("clawcu.cli.get_service", lambda: service)
    # Force Rich to render at a width wide enough to keep column headers
    monkeypatch.setattr("clawcu.cli.console", _make_wide_console())

    result = runner.invoke(app, ["provider", "list", "--wide"])

    assert result.exit_code == 0
    # --wide adds PROVIDER and ENDPOINT columns, API key still masked
    assert "ENDPOINT" in result.stdout
    assert "sk-tes" in result.stdout  # masked form starts with first 6 chars
    assert "sk-test-1234567890" not in result.stdout  # full key not shown


def test_provider_list_reveal_shows_full_key(monkeypatch) -> None:
    service = FakeService()
    monkeypatch.setattr("clawcu.cli.get_service", lambda: service)

    result = runner.invoke(app, ["provider", "list", "--wide", "--reveal"])

    assert result.exit_code == 0
    assert "sk-test-1234567890" in result.stdout


def test_provider_show_command_returns_json(monkeypatch) -> None:
    service = FakeService()
    monkeypatch.setattr("clawcu.cli.get_service", lambda: service)

    result = runner.invoke(app, ["provider", "show", "openai-main"])

    assert result.exit_code == 0
    assert '"auth_profiles"' in result.stdout
    assert '"models"' in result.stdout
    assert "sk-test" not in result.stdout
    assert "*******" in result.stdout
    assert service.calls[0] == ("show_provider", (), {"name": "openai-main"})


def test_provider_apply_command_defaults_agent_to_main(monkeypatch) -> None:
    service = FakeService()
    monkeypatch.setattr("clawcu.cli.get_service", lambda: service)

    result = runner.invoke(app, ["provider", "apply", "openai-main", "writer"])

    assert result.exit_code == 0
    assert "Applied provider:" in result.stdout
    assert "openai-main -> writer/main" in result.stdout
    assert service.calls[0] == (
        "apply_provider",
        (),
        {
            "provider": "openai-main",
            "instance": "writer",
            "agent": "main",
            "persist": False,
            "primary": None,
            "fallbacks": None,
        },
    )


def test_provider_apply_command_accepts_explicit_agent(monkeypatch) -> None:
    service = FakeService()
    monkeypatch.setattr("clawcu.cli.get_service", lambda: service)

    result = runner.invoke(app, ["provider", "apply", "openai-main", "writer", "--agent", "chat"])

    assert result.exit_code == 0
    assert "openai-main -> writer/chat" in result.stdout
    assert service.calls[0] == (
        "apply_provider",
        (),
        {
            "provider": "openai-main",
            "instance": "writer",
            "agent": "chat",
            "persist": False,
            "primary": None,
            "fallbacks": None,
        },
    )


def test_provider_apply_command_accepts_primary_and_fallbacks(monkeypatch) -> None:
    service = FakeService()
    monkeypatch.setattr("clawcu.cli.get_service", lambda: service)

    result = runner.invoke(
        app,
        [
            "provider",
            "apply",
            "openai-main",
            "writer",
            "--agent",
            "chat",
            "--primary",
            "openai/gpt-5",
            "--fallbacks",
            "anthropic/claude-sonnet-4.5,openai/gpt-4.1",
        ],
    )

    assert result.exit_code == 0
    assert "Agent models:" in result.stdout
    assert "primary=openai/gpt-5" in result.stdout
    assert "fallbacks=anthropic/claude-sonnet-4.5" in result.stdout
    assert "openai/gpt-4.1" in result.stdout
    assert service.calls[0] == (
        "apply_provider",
        (),
        {
            "provider": "openai-main",
            "instance": "writer",
            "agent": "chat",
            "persist": False,
            "primary": "openai/gpt-5",
            "fallbacks": ["anthropic/claude-sonnet-4.5", "openai/gpt-4.1"],
        },
    )


def test_provider_remove_command_forwards_name(monkeypatch) -> None:
    service = FakeService()
    monkeypatch.setattr("clawcu.cli.get_service", lambda: service)

    result = runner.invoke(app, ["provider", "remove", "openai-main", "--yes"])

    assert result.exit_code == 0
    assert "Removed provider:" in result.stdout
    assert service.calls[0] == ("remove_provider", (), {"name": "openai-main"})


def test_provider_remove_requires_confirmation_in_non_interactive(monkeypatch) -> None:
    service = FakeService()
    monkeypatch.setattr("clawcu.cli.get_service", lambda: service)

    result = runner.invoke(app, ["provider", "remove", "openai-main"])

    assert result.exit_code == 1
    assert "--yes" in result.stdout
    # Service call MUST NOT happen without confirmation
    assert all(call[0] != "remove_provider" for call in service.calls)


def test_provider_models_list_command_forwards_arguments(monkeypatch) -> None:
    service = FakeService()
    monkeypatch.setattr("clawcu.cli.get_service", lambda: service)

    list_result = runner.invoke(app, ["provider", "models", "list", "openai-main"])

    assert list_result.exit_code == 0
    assert "gpt-5" in list_result.stdout
    assert service.calls[0] == ("list_provider_models", (), {"name": "openai-main"})


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
    assert "(status: running)" in result.stdout
    assert "Open URL:" in result.stdout
    assert "http://127.0.0.1:3000/#token=token-writer" in result.stdout
    assert service.calls[0] == (
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
    assert service.calls[-1] == (
        "dashboard_url",
        (),
        {"name": "writer"},
    )


def test_create_hermes_command_uses_defaults(monkeypatch) -> None:
    service = FakeService()
    monkeypatch.setattr("clawcu.cli.get_service", lambda: service)

    result = runner.invoke(
        app,
        [
            "create",
            "hermes",
            "--name",
            "scribe",
            "--version",
            "v0.9.0",
        ],
    )

    assert result.exit_code == 0
    assert "Created instance: scribe (v0.9.0)" in result.stdout
    assert "Open URL:" in result.stdout
    assert service.calls[0] == (
        "create_hermes",
        (),
        {
            "name": "scribe",
            "version": "v0.9.0",
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


def test_retry_command_is_removed(monkeypatch) -> None:
    service = FakeService()
    monkeypatch.setattr("clawcu.cli.get_service", lambda: service)

    result = runner.invoke(app, ["retry", "writer"])

    # The `retry` subcommand has been removed in favor of `recreate`.
    assert result.exit_code != 0


def test_recreate_command_recreates_instance(monkeypatch) -> None:
    service = FakeService()
    monkeypatch.setattr("clawcu.cli.get_service", lambda: service)

    result = runner.invoke(app, ["recreate", "writer"])

    assert result.exit_code == 0
    assert "Recreated instance:" in result.stdout
    assert "(status: running)" in result.stdout
    assert "Open URL:" in result.stdout
    assert service.calls[-1] == ("dashboard_url", (), {"name": "writer"})


def test_recreate_command_auto_retries_create_failed_instance(monkeypatch) -> None:
    service = FakeService()
    service.instance_statuses["writer"] = "create_failed"
    monkeypatch.setattr("clawcu.cli.get_service", lambda: service)

    result = runner.invoke(app, ["recreate", "writer"])

    assert result.exit_code == 0
    assert "Retried instance:" in result.stdout
    # retry_instance is called first; recreate_instance should NOT be called
    method_names = [call[0] for call in service.calls]
    assert "retry_instance" in method_names
    assert "recreate_instance" not in method_names


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
    assert service.calls[0][2]["cpu"] == "2"
    assert service.calls[0][2]["memory"] == "4g"


def test_list_running_option_is_forwarded(monkeypatch) -> None:
    service = FakeService()
    monkeypatch.setattr("clawcu.cli.get_service", lambda: service)

    result = runner.invoke(app, ["list", "--managed", "--running"])

    assert result.exit_code == 0
    assert "ClawCU Instances" in result.stdout
    assert service.calls[-1] == ("list_instance_summaries", (), {"running_only": True})


def test_list_agents_option_shows_agent_rows(monkeypatch) -> None:
    service = FakeService()
    monkeypatch.setattr("clawcu.cli.get_service", lambda: service)

    # Default source is now "managed" so only managed agent summaries are fetched.
    result = runner.invoke(app, ["list", "--agents"])

    assert result.exit_code == 0
    assert "ClawCU Agents" in result.stdout
    assert "main" in result.stdout
    assert service.calls == [
        ("list_agent_summaries", (), {"running_only": False}),
    ]


def test_list_agents_source_all_includes_local_agents(monkeypatch) -> None:
    service = FakeService()
    monkeypatch.setattr("clawcu.cli.get_service", lambda: service)

    result = runner.invoke(app, ["list", "--agents", "--source", "all"])

    assert result.exit_code == 0
    assert service.calls == [
        ("list_local_agent_summaries", (), {}),
        ("list_agent_summaries", (), {"running_only": False}),
    ]


def test_list_agents_managed_option_shows_managed_agent_rows(monkeypatch) -> None:
    service = FakeService()
    monkeypatch.setattr("clawcu.cli.get_service", lambda: service)

    result = runner.invoke(app, ["list", "--managed", "--agents"])

    assert result.exit_code == 0
    assert "ClawCU Agents" in result.stdout
    assert "chat" in result.stdout
    assert service.calls[-1] == ("list_agent_summaries", (), {"running_only": False})


def test_list_local_option_filters_to_local(monkeypatch) -> None:
    service = FakeService()
    monkeypatch.setattr("clawcu.cli.get_service", lambda: service)

    result = runner.invoke(app, ["list", "--local"])

    assert result.exit_code == 0
    assert service.calls == [("list_local_instance_summaries", (), {})]


def test_inspect_command_accepts_instance_name(monkeypatch) -> None:
    service = FakeService()
    monkeypatch.setattr("clawcu.cli.get_service", lambda: service)

    result = runner.invoke(app, ["inspect", "writer"])

    assert result.exit_code == 0
    assert '"Name": "writer"' in result.stdout
    assert '"snapshots"' in result.stdout
    assert '"latest_upgrade_snapshot"' in result.stdout
    assert service.calls[-1] == ("inspect_instance", (), {"name": "writer"})


def test_approve_command_uses_latest_request_by_default(monkeypatch) -> None:
    service = FakeService()
    monkeypatch.setattr("clawcu.cli.get_service", lambda: service)

    result = runner.invoke(app, ["approve", "writer"])

    assert result.exit_code == 0
    assert "Approving pairing request" in result.stdout
    assert "Approved pairing:" in result.stdout
    assert "Open URL:" in result.stdout
    assert service.calls[0] == ("approve_pairing", (), {"name": "writer", "request_id": None})
    assert service.calls[-1] == ("dashboard_url", (), {"name": "writer"})


def test_approve_command_accepts_explicit_request_id(monkeypatch) -> None:
    service = FakeService()
    monkeypatch.setattr("clawcu.cli.get_service", lambda: service)

    result = runner.invoke(app, ["approve", "writer", "req-123"])

    assert result.exit_code == 0
    assert service.calls[0] == ("approve_pairing", (), {"name": "writer", "request_id": "req-123"})


def test_config_command_passes_through_extra_args(monkeypatch) -> None:
    service = FakeService()
    monkeypatch.setattr("clawcu.cli.get_service", lambda: service)

    result = runner.invoke(app, ["config", "writer", "--section", "models"])

    assert result.exit_code == 0
    assert service.calls[0] == (
        "configure_instance",
        (),
        {"name": "writer", "extra_args": ["--section", "models"]},
    )


def test_exec_command_passes_through_container_command(monkeypatch) -> None:
    service = FakeService()
    monkeypatch.setattr("clawcu.cli.get_service", lambda: service)

    result = runner.invoke(app, ["exec", "writer", "openclaw", "config"])

    assert result.exit_code == 0
    assert service.calls[0] == (
        "exec_instance",
        (),
        {"name": "writer", "command": ["openclaw", "config"]},
    )


def test_tui_command_uses_main_agent_by_default(monkeypatch) -> None:
    service = FakeService()
    monkeypatch.setattr("clawcu.cli.get_service", lambda: service)

    result = runner.invoke(app, ["tui", "writer"])

    assert result.exit_code == 0
    assert service.calls[0] == (
        "tui_instance",
        (),
        {"name": "writer", "agent": "main"},
    )


def test_tui_command_accepts_explicit_agent(monkeypatch) -> None:
    service = FakeService()
    monkeypatch.setattr("clawcu.cli.get_service", lambda: service)

    result = runner.invoke(app, ["tui", "writer", "--agent", "chat"])

    assert result.exit_code == 0
    assert service.calls[0] == (
        "tui_instance",
        (),
        {"name": "writer", "agent": "chat"},
    )


def test_lifecycle_commands_accept_instance_name(monkeypatch) -> None:
    service = FakeService()
    monkeypatch.setattr("clawcu.cli.get_service", lambda: service)

    cases = [
        (["start", "writer"], "start_instance"),
        (["stop", "writer"], "stop_instance"),
        (["restart", "writer"], "restart_instance"),
        (["rollback", "writer", "--yes"], "rollback_instance"),
    ]

    for argv, expected_call in cases:
        service.calls.clear()
        result = runner.invoke(app, argv)
        assert result.exit_code == 0
        assert service.calls[0] == (expected_call, (), {"name": "writer"})
        if expected_call in {"start_instance", "restart_instance", "rollback_instance"}:
            assert service.calls[-1] == ("dashboard_url", (), {"name": "writer"})


def test_upgrade_command_accepts_version_option(monkeypatch) -> None:
    service = FakeService()
    monkeypatch.setattr("clawcu.cli.get_service", lambda: service)

    result = runner.invoke(app, ["upgrade", "writer", "--version", "2026.4.2"])

    assert result.exit_code == 0
    assert "Step 1/4: Preparing an upgrade plan" in result.stdout
    assert "Step 4/4: Recreating the container" in result.stdout
    assert service.calls[-2] == (
        "upgrade_instance",
        (),
        {"name": "writer", "version": "2026.4.2"},
    )
    assert service.calls[-1] == ("dashboard_url", (), {"name": "writer"})


def test_rollback_command_prints_progress(monkeypatch) -> None:
    service = FakeService()
    monkeypatch.setattr("clawcu.cli.get_service", lambda: service)

    result = runner.invoke(app, ["rollback", "writer", "--yes"])

    assert result.exit_code == 0
    assert "Step 1/4: Preparing to roll back" in result.stdout
    assert "Step 4/4: Starting OpenClaw" in result.stdout
    assert service.calls[-2] == ("rollback_instance", (), {"name": "writer"})
    assert service.calls[-1] == ("dashboard_url", (), {"name": "writer"})


def test_rollback_command_requires_confirmation_in_non_interactive(monkeypatch) -> None:
    service = FakeService()
    monkeypatch.setattr("clawcu.cli.get_service", lambda: service)

    result = runner.invoke(app, ["rollback", "writer"])

    assert result.exit_code == 1
    assert "--yes" in result.stdout
    assert all(call[0] != "rollback_instance" for call in service.calls)


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
    assert "Step 1/5: Validating the source instance" in result.stdout
    assert "Step 5/5: Starting the cloned Docker container" in result.stdout
    assert service.calls[-2] == (
        "clone_instance",
        (),
        {
            "source_name": "writer",
            "name": "writer-exp",
            "datadir": "/tmp/writer-exp",
            "port": 3001,
        },
    )
    assert service.calls[-1] == ("dashboard_url", (), {"name": "writer-exp"})


def test_logs_follow_option_is_forwarded(monkeypatch) -> None:
    service = FakeService()
    monkeypatch.setattr("clawcu.cli.get_service", lambda: service)

    result = runner.invoke(app, ["logs", "writer", "--follow"])

    assert result.exit_code == 0
    assert service.calls[-1] == (
        "stream_logs",
        (),
        {"name": "writer", "follow": True, "tail": 200, "since": None},
    )


def test_logs_tail_zero_disables_tail_limit(monkeypatch) -> None:
    service = FakeService()
    monkeypatch.setattr("clawcu.cli.get_service", lambda: service)

    result = runner.invoke(app, ["logs", "writer", "--tail", "0"])

    assert result.exit_code == 0
    assert service.calls[-1] == (
        "stream_logs",
        (),
        {"name": "writer", "follow": False, "tail": None, "since": None},
    )


def test_logs_since_option_is_forwarded(monkeypatch) -> None:
    service = FakeService()
    monkeypatch.setattr("clawcu.cli.get_service", lambda: service)

    result = runner.invoke(app, ["logs", "writer", "--tail", "50", "--since", "10m"])

    assert result.exit_code == 0
    assert service.calls[-1] == (
        "stream_logs",
        (),
        {"name": "writer", "follow": False, "tail": 50, "since": "10m"},
    )


def test_remove_delete_data_and_keep_data_flags(monkeypatch) -> None:
    service = FakeService()
    monkeypatch.setattr("clawcu.cli.get_service", lambda: service)

    delete_result = runner.invoke(app, ["remove", "writer", "--delete-data", "--yes"])
    keep_result = runner.invoke(app, ["remove", "writer", "--keep-data", "--yes"])

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


def test_remove_requires_confirmation_in_non_interactive(monkeypatch) -> None:
    service = FakeService()
    monkeypatch.setattr("clawcu.cli.get_service", lambda: service)

    result = runner.invoke(app, ["remove", "writer", "--delete-data"])

    assert result.exit_code == 1
    assert "--yes" in result.stdout
    assert all(call[0] != "remove_instance" for call in service.calls)
