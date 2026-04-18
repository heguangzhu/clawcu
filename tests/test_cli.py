from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from rich.console import Console
from typer.testing import CliRunner

from clawcu.cli import _actionable_hint_for, _display_version, app
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


def test_actionable_hint_matches_canonical_service_errors() -> None:
    assert _actionable_hint_for("Instance 'foo' was not found.") == (
        "Run `clawcu list` to see managed instances."
    )
    assert _actionable_hint_for("Provider 'bar' was not found.") == (
        "Run `clawcu provider list` to see collected providers."
    )
    assert _actionable_hint_for(
        "Provider bundle 'openclaw:baz' was not found."
    ) == (
        "Run `clawcu provider list` to see collected providers, "
        "or `clawcu provider collect` to import new ones."
    )
    assert _actionable_hint_for(
        "Instance 'writer' has no rollback snapshot for version 2026.4.1."
    ) == "Run `clawcu rollback <name> --list` to see available rollback targets."


def test_actionable_hint_ignores_unrelated_docker_stderr() -> None:
    # Regression guard for product_review-2.md P0#2: substring matching
    # on "instance"/"image"/"does not exist" fired on arbitrary Docker
    # errors (e.g. "pull access denied") and produced misleading hints.
    docker_pull_failure = (
        "Failed to create instance 'scratch-clone': Unable to find image "
        "'clawcu/openclaw:2026.4.15' locally\n"
        "docker: Error response from daemon: pull access denied for "
        "clawcu/openclaw, repository does not exist or may require "
        "'docker login'"
    )
    assert _actionable_hint_for(docker_pull_failure) is None


def test_start_command_sets_progress_reporter(monkeypatch) -> None:
    from clawcu import cli

    service = FakeService()
    monkeypatch.setattr(cli, "get_service", lambda: service)
    monkeypatch.setattr(cli, "console", _make_wide_console())

    result = runner.invoke(app, ["start", "writer"])

    assert result.exit_code == 0
    assert service.reporter is cli._print_progress
    assert ("start_instance", (), {"name": "writer"}) in service.calls


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

    def remove_provider(self, name: str, *, force: bool = False) -> list[dict[str, str]]:
        self._record("remove_provider", name=name, force=force)
        return []

    def find_instances_using_provider(self, name: str) -> list[dict[str, str]]:
        self._record("find_instances_using_provider", name=name)
        return list(getattr(self, "provider_usage", {}).get(name, []))

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

    def stop_instance(self, name: str, *, timeout: int | None = None) -> InstanceRecord:
        self._record("stop_instance", name=name, timeout=timeout)
        return self._instance(name=name)

    def restart_instance(
        self,
        name: str,
        *,
        recreate_if_config_changed: bool = True,
    ) -> InstanceRecord:
        self._record(
            "restart_instance",
            name=name,
            recreate_if_config_changed=recreate_if_config_changed,
        )
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

    def recreate_instance(
        self,
        name: str,
        *,
        fresh: bool = False,
        timeout: int | None = None,
    ) -> InstanceRecord:
        self._record("recreate_instance", name=name, fresh=fresh, timeout=timeout)
        if self.reporter:
            self.reporter("Recreating instance")
        record = self._instance(name=name)
        return record

    def upgrade_plan(self, name: str, *, version: str) -> dict:
        self._record("upgrade_plan", name=name, version=version)
        return {
            "instance": name,
            "service": "openclaw",
            "current_version": "2026.4.1",
            "target_version": version,
            "datadir": f"/tmp/{name}",
            "env_path": f"/tmp/{name}.env",
            "env_exists": True,
            "env_keys": ["OPENAI_API_KEY", "OPENAI_BASE_URL"],
            "env_carryover": "preserved",
            "projected_image": f"ghcr.io/openclaw/openclaw:{version}",
            "snapshot_root": f"/tmp/snapshots/{name}",
            "snapshot_label": f"upgrade-to-{version}",
        }

    def list_upgradable_versions(
        self, name: str, *, include_remote: bool = True
    ) -> dict:
        self._record(
            "list_upgradable_versions", name=name, include_remote=include_remote
        )
        return {
            "instance": name,
            "service": "openclaw",
            "image_repo": "ghcr.io/openclaw/openclaw",
            "current_version": "2026.4.1",
            "history": ["2026.3.20", "2026.4.1"],
            "local_images": ["2026.4.1", "2026.4.2"],
            "remote_versions": ["2026.4.1", "2026.4.2", "2026.4.3"]
            if include_remote
            else None,
            "remote_error": None,
            "remote_registry": "ghcr.io" if include_remote else None,
            "remote_requested": include_remote,
        }

    def upgrade_instance(self, name: str, *, version: str) -> InstanceRecord:
        self._record("upgrade_instance", name=name, version=version)
        if self.reporter:
            self.reporter("Step 1/4: Preparing an upgrade plan")
            self.reporter("Step 4/4: Recreating the container")
        return self._instance(name=name, version=version)

    def rollback_plan(self, name: str, *, to_version: str | None = None) -> dict:
        self._record("rollback_plan", name=name, to_version=to_version)
        target = to_version or "2026.4.0"
        return {
            "instance": name,
            "service": "openclaw",
            "current_version": "2026.4.1",
            "target_version": target,
            "datadir": f"/tmp/{name}",
            "env_path": f"/tmp/{name}.env",
            "env_exists": True,
            "restore_snapshot": f"/tmp/snapshots/{name}/20260101-upgrade-to-2026.4.1",
            "restore_snapshot_exists": True,
            "selected_action": "upgrade",
            "selected_timestamp": "2026-01-01T00:00:00Z",
            "projected_image": f"ghcr.io/openclaw/openclaw:{target}",
            "snapshot_root": f"/tmp/snapshots/{name}",
            "snapshot_label": "rollback-from-2026.4.1",
        }

    def list_rollback_targets(self, name: str) -> dict:
        self._record("list_rollback_targets", name=name)
        return {
            "instance": name,
            "service": "openclaw",
            "current_version": "2026.4.1",
            "targets": [
                {
                    "index": 0,
                    "action": "upgrade",
                    "timestamp": "2026-01-01T00:00:00Z",
                    "from_version": "2026.3.20",
                    "to_version": "2026.4.0",
                    "snapshot_dir": f"/tmp/snapshots/{name}/20260101-upgrade-to-2026.4.0",
                    "snapshot_exists": True,
                    "restores_to": "2026.3.20",
                },
                {
                    "index": 1,
                    "action": "upgrade",
                    "timestamp": "2026-02-01T00:00:00Z",
                    "from_version": "2026.4.0",
                    "to_version": "2026.4.1",
                    "snapshot_dir": f"/tmp/snapshots/{name}/20260201-upgrade-to-2026.4.1",
                    "snapshot_exists": True,
                    "restores_to": "2026.4.0",
                },
            ],
        }

    def rollback_instance(
        self, name: str, *, to_version: str | None = None
    ) -> InstanceRecord:
        self._record("rollback_instance", name=name, to_version=to_version)
        if self.reporter:
            self.reporter("Step 1/4: Preparing to roll back")
            self.reporter("Step 4/4: Starting OpenClaw")
        return self._instance(name=name, version=to_version or "2026.4.0")

    def clone_instance(
        self,
        source_name: str,
        *,
        name: str,
        datadir: str | None = None,
        port: int | None = None,
        version: str | None = None,
        include_secrets: bool = True,
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
            version=version,
            include_secrets=include_secrets,
        )
        record = self._instance(name=name)
        if datadir is not None:
            record.datadir = datadir
        if port is not None:
            record.port = port
        if version is not None:
            record.version = version
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
    assert "Roll an instance back to an earlier snapshot." in result.stdout
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
    # Rollback help now documents the --to / --list surface.
    assert "--to" in rollback_result.stdout
    assert "--list" in rollback_result.stdout


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


def test_provider_models_help_is_a_leaf_command() -> None:
    """v0.2: the trailing `list` level was removed — `provider models`
    is now a direct leaf command that takes a provider name."""
    result = runner.invoke(app, ["provider", "models", "--help"])

    assert result.exit_code == 0
    assert "List the models stored in a collected provider." in result.stdout
    assert "NAME" in result.stdout


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

    assert create_result.exit_code == 0
    assert pull_result.exit_code == 0
    assert provider_result.exit_code == 0
    assert "create [OPTIONS] SERVICE" in create_result.stdout
    assert "pull [OPTIONS] SERVICE" in pull_result.stdout
    assert "provider [OPTIONS] COMMAND [ARGS]..." in provider_result.stdout
    assert "Missing command" not in create_result.stdout
    assert "Missing command" not in pull_result.stdout

    # `provider models` is now a leaf command that takes a provider name
    # directly (v0.2 dropped the trailing `list` level). Missing the name
    # surfaces the standard Typer "Missing argument 'NAME'" error.
    provider_models_result = runner.invoke(app, ["provider", "models"])
    assert provider_models_result.exit_code == 2
    assert "Missing argument" in provider_models_result.output


def test_missing_required_arguments_exit_with_posix_error() -> None:
    """Commands with required positional args / options error cleanly.

    Post v0.2, ClawCU uses Typer's native required-option enforcement
    instead of the old "show help, exit 0 on no args" behavior. That
    makes `--help` output distinguish required from optional via the
    `*` gutter marker, at the cost of switching missing-arg behavior
    from "print help" to "print error + exit 2" — standard POSIX CLI.
    """
    commands_missing_name = [
        "inspect",
        "token",
        "provider show",
        "provider remove",
        "provider models",
        "start",
        "stop",
        "restart",
        "recreate",
        "upgrade",
        "rollback",
        "logs",
        "remove",
        "approve",
        "tui",
        "getenv",
        "setenv",
        "unsetenv",
    ]
    for command in commands_missing_name:
        result = runner.invoke(app, command.split())
        assert result.exit_code == 2, (
            f"{command!r} should exit 2 on missing required arg, got "
            f"{result.exit_code}: {result.stdout}"
        )
        # Typer's standard "Missing argument 'NAME'" error. Ensures the
        # user sees WHICH arg is missing, not a wall of help text.
        assert "Missing argument" in result.output or "Missing option" in result.output, (
            f"{command!r} should surface a 'Missing' error: {result.output}"
        )

    # provider apply has two positional args; both should be surfaced.
    result = runner.invoke(app, ["provider", "apply"])
    assert result.exit_code == 2

    # provider collect has three mutually-exclusive scope flags; a clean
    # error is preferable to an exit-2 help dump.
    result = runner.invoke(app, ["provider", "collect"])
    assert result.exit_code == 1
    assert "--all" in result.output and "--instance" in result.output

    # clone has --name as a required OPTION; missing it should also
    # surface via Typer's standard missing-option error.
    result = runner.invoke(app, ["clone", "writer"])
    assert result.exit_code == 2
    assert "Missing option" in result.output


def test_required_options_render_in_help_with_asterisk() -> None:
    """--help for a command with a required option shows the `*` marker.

    This is the user-facing outcome of switching from manual
    `_show_help_and_exit` checks to Typer `required=True` — you can
    now tell at a glance which options are mandatory.
    """
    result = runner.invoke(app, ["create", "openclaw", "--help"])
    assert result.exit_code == 0
    # Required options carry a `*` in the left gutter AND a [required]
    # suffix in the Rich panel. We assert on the stable "required"
    # marker rather than the cosmetic asterisk.
    assert "required" in result.output.lower()


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
            "overwrite": False,
        },
    )


def test_token_command_prints_instance_token(monkeypatch) -> None:
    service = FakeService()
    monkeypatch.setattr("clawcu.cli.get_service", lambda: service)

    result = runner.invoke(app, ["token", "writer"])

    assert result.exit_code == 0
    # Default output now labels both pieces.
    assert "Token:" in result.stdout
    assert "token-writer" in result.stdout
    assert "URL:" in result.stdout
    assert "#token=token-writer" in result.stdout
    assert service.calls[0] == ("token", (), {"name": "writer"})


def test_token_command_token_only_omits_url_and_label(monkeypatch) -> None:
    service = FakeService()
    monkeypatch.setattr("clawcu.cli.get_service", lambda: service)

    result = runner.invoke(app, ["token", "writer", "--token-only"])

    assert result.exit_code == 0
    assert result.stdout.strip() == "token-writer"


def test_token_command_url_only_prints_access_url(monkeypatch) -> None:
    service = FakeService()
    monkeypatch.setattr("clawcu.cli.get_service", lambda: service)

    result = runner.invoke(app, ["token", "writer", "--url-only"])

    assert result.exit_code == 0
    assert result.stdout.strip() == "http://127.0.0.1:3000/#token=token-writer"


def test_token_command_rejects_url_and_token_only_together(monkeypatch) -> None:
    service = FakeService()
    monkeypatch.setattr("clawcu.cli.get_service", lambda: service)

    result = runner.invoke(app, ["token", "writer", "--url-only", "--token-only"])

    assert result.exit_code == 1
    assert "mutually exclusive" in result.stdout


def test_token_command_copy_flag_invokes_clipboard(monkeypatch) -> None:
    service = FakeService()
    monkeypatch.setattr("clawcu.cli.get_service", lambda: service)
    calls: list[tuple[str, str]] = []

    def fake_copy(value: str) -> tuple[bool, str]:
        calls.append(("copy", value))
        return True, "pbcopy"

    monkeypatch.setattr("clawcu.cli._copy_to_clipboard", fake_copy)

    result = runner.invoke(app, ["token", "writer", "--copy"])

    assert result.exit_code == 0
    assert calls == [("copy", "token-writer")]
    assert "Copied token to clipboard (pbcopy)" in result.stdout


def test_token_command_copy_flag_warns_when_backend_missing(monkeypatch) -> None:
    service = FakeService()
    monkeypatch.setattr("clawcu.cli.get_service", lambda: service)
    monkeypatch.setattr(
        "clawcu.cli._copy_to_clipboard",
        lambda value: (False, "no clipboard backend found"),
    )

    result = runner.invoke(app, ["token", "writer", "--copy"])

    assert result.exit_code == 0
    assert "Could not copy to clipboard" in result.stdout


def test_token_command_hints_hermes_native_auth(monkeypatch) -> None:
    service = FakeService()

    def not_supported(name: str) -> str:
        raise ValueError(
            "`clawcu token` is not supported for Hermes instances."
        )

    service.token = not_supported  # type: ignore[assignment]
    monkeypatch.setattr("clawcu.cli.get_service", lambda: service)

    result = runner.invoke(app, ["token", "javis"])

    assert result.exit_code == 1
    assert "not supported" in result.stdout
    assert "Hermes uses native auth" in result.stdout
    assert "clawcu config javis" in result.stdout


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
    assert service.calls[1] == ("recreate_instance", (), {"name": "writer", "fresh": False, "timeout": None})


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
    assert service.calls[1] == ("recreate_instance", (), {"name": "writer", "fresh": False, "timeout": None})


def test_setenv_command_from_file_loads_assignments(monkeypatch, tmp_path) -> None:
    service = FakeService()
    monkeypatch.setattr("clawcu.cli.get_service", lambda: service)

    env_file = tmp_path / "bundle.env"
    env_file.write_text(
        "\n".join(
            [
                "# a comment",
                "",
                "OPENAI_API_KEY=sk-file",
                "OPENAI_BASE_URL=https://file.example.com/v1",
                "   NOT_A_PAIR_LINE   ",  # no '=' -> ignored
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    result = runner.invoke(app, ["setenv", "writer", "--from-file", str(env_file)])

    assert result.exit_code == 0, result.stdout
    assert "/tmp/writer.env" in result.stdout
    assert service.calls[0] == (
        "set_instance_env",
        (),
        {
            "name": "writer",
            "assignments": [
                "OPENAI_API_KEY=sk-file",
                "OPENAI_BASE_URL=https://file.example.com/v1",
            ],
        },
    )


def test_setenv_command_from_file_and_inline_are_exclusive(monkeypatch, tmp_path) -> None:
    service = FakeService()
    monkeypatch.setattr("clawcu.cli.get_service", lambda: service)

    env_file = tmp_path / "bundle.env"
    env_file.write_text("KEY=val\n", encoding="utf-8")

    result = runner.invoke(
        app,
        ["setenv", "writer", "FOO=bar", "--from-file", str(env_file)],
    )

    assert result.exit_code != 0
    assert "not both" in result.stdout.lower() or "not both" in (result.stderr or "").lower()
    # service was never asked to write
    assert not any(call[0] == "set_instance_env" for call in service.calls)


def test_setenv_command_from_file_missing_file_errors(monkeypatch, tmp_path) -> None:
    service = FakeService()
    monkeypatch.setattr("clawcu.cli.get_service", lambda: service)

    missing = tmp_path / "nope.env"
    result = runner.invoke(app, ["setenv", "writer", "--from-file", str(missing)])

    assert result.exit_code != 0
    assert "not found" in result.stdout.lower() or "not found" in (result.stderr or "").lower()
    assert not any(call[0] == "set_instance_env" for call in service.calls)


def test_setenv_dry_run_shows_diff_without_writing(monkeypatch) -> None:
    service = FakeService()
    monkeypatch.setattr("clawcu.cli.get_service", lambda: service)

    # FakeService.get_instance_env returns:
    #   OPENAI_API_KEY=sk-test, OPENAI_BASE_URL=https://api.example.com/v1
    result = runner.invoke(
        app,
        [
            "setenv",
            "writer",
            "OPENAI_API_KEY=sk-new",  # updated (sensitive; masked)
            "NEW_FLAG=1",  # added (not sensitive)
            "--dry-run",
        ],
    )

    assert result.exit_code == 0, result.stdout
    assert "Dry run" in result.stdout
    # Added key visible in plain text (not sensitive)
    assert "+ NEW_FLAG=1" in result.stdout
    # Updated sensitive key uses masking, not the raw token
    assert "~ OPENAI_API_KEY" in result.stdout
    assert "sk-new" not in result.stdout
    assert "sk-test" not in result.stdout
    # No write / no recreate was attempted
    assert not any(call[0] == "set_instance_env" for call in service.calls)
    assert not any(call[0] == "recreate_instance" for call in service.calls)
    # But we did need to read current state
    assert any(call[0] == "get_instance_env" for call in service.calls)


def test_setenv_dry_run_reveal_shows_raw_values(monkeypatch) -> None:
    service = FakeService()
    monkeypatch.setattr("clawcu.cli.get_service", lambda: service)

    result = runner.invoke(
        app,
        ["setenv", "writer", "OPENAI_API_KEY=sk-new", "--dry-run", "--reveal"],
    )

    assert result.exit_code == 0, result.stdout
    assert "sk-new" in result.stdout
    assert not any(call[0] == "set_instance_env" for call in service.calls)


def test_setenv_dry_run_and_apply_are_mutually_exclusive(monkeypatch) -> None:
    service = FakeService()
    monkeypatch.setattr("clawcu.cli.get_service", lambda: service)

    result = runner.invoke(
        app,
        ["setenv", "writer", "FOO=bar", "--dry-run", "--apply"],
    )

    assert result.exit_code != 0
    assert "mutually exclusive" in result.stdout.lower() or "mutually exclusive" in (result.stderr or "").lower()
    assert not any(call[0] == "set_instance_env" for call in service.calls)


def test_unsetenv_dry_run_previews_removals(monkeypatch) -> None:
    service = FakeService()
    monkeypatch.setattr("clawcu.cli.get_service", lambda: service)

    result = runner.invoke(
        app,
        [
            "unsetenv",
            "writer",
            "OPENAI_BASE_URL",  # present
            "MISSING_KEY",  # not present -> no-op
            "--dry-run",
        ],
    )

    assert result.exit_code == 0, result.stdout
    assert "Dry run" in result.stdout
    # Removal of the present key is shown (OPENAI_BASE_URL is not sensitive)
    assert "- OPENAI_BASE_URL=https://api.example.com/v1" in result.stdout
    # Missing key is listed under no-op footer
    assert "MISSING_KEY" in result.stdout
    # No write / no recreate
    assert not any(call[0] == "unset_instance_env" for call in service.calls)
    assert not any(call[0] == "recreate_instance" for call in service.calls)


def test_unsetenv_dry_run_and_apply_are_mutually_exclusive(monkeypatch) -> None:
    service = FakeService()
    monkeypatch.setattr("clawcu.cli.get_service", lambda: service)

    result = runner.invoke(
        app,
        ["unsetenv", "writer", "OPENAI_API_KEY", "--dry-run", "--apply"],
    )

    assert result.exit_code != 0
    assert "mutually exclusive" in result.stdout.lower() or "mutually exclusive" in (result.stderr or "").lower()
    assert not any(call[0] == "unset_instance_env" for call in service.calls)


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
    remove_calls = [c for c in service.calls if c[0] == "remove_provider"]
    assert remove_calls == [("remove_provider", (), {"name": "openai-main", "force": False})]


def test_provider_remove_requires_confirmation_in_non_interactive(monkeypatch) -> None:
    service = FakeService()
    monkeypatch.setattr("clawcu.cli.get_service", lambda: service)

    result = runner.invoke(app, ["provider", "remove", "openai-main"])

    assert result.exit_code == 1
    assert "--yes" in result.stdout
    # Service call MUST NOT happen without confirmation
    assert all(call[0] != "remove_provider" for call in service.calls)


def test_provider_remove_warns_when_instances_reference_provider(monkeypatch) -> None:
    service = FakeService()
    service.provider_usage = {
        "openai-main": [{"instance": "writer", "agent": "main", "service": "openclaw"}]
    }
    monkeypatch.setattr("clawcu.cli.get_service", lambda: service)

    result = runner.invoke(app, ["provider", "remove", "openai-main", "--yes"])

    assert result.exit_code != 0
    assert "writer/main" in result.stdout
    assert "--force" in result.stdout
    assert all(call[0] != "remove_provider" for call in service.calls)


def test_provider_remove_force_deletes_even_when_in_use(monkeypatch) -> None:
    service = FakeService()
    service.provider_usage = {
        "openai-main": [{"instance": "writer", "agent": "main", "service": "openclaw"}]
    }
    monkeypatch.setattr("clawcu.cli.get_service", lambda: service)

    result = runner.invoke(app, ["provider", "remove", "openai-main", "--force", "--yes"])

    assert result.exit_code == 0
    remove_calls = [c for c in service.calls if c[0] == "remove_provider"]
    assert remove_calls == [("remove_provider", (), {"name": "openai-main", "force": True})]


def test_provider_models_list_command_forwards_arguments(monkeypatch) -> None:
    """v0.2: `clawcu provider models <name>` replaces the older
    `clawcu provider models list <name>` form — the trailing `list` level
    was dropped per design review.
    """
    service = FakeService()
    monkeypatch.setattr("clawcu.cli.get_service", lambda: service)

    list_result = runner.invoke(app, ["provider", "models", "openai-main"])

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


def test_recreate_command_forwards_timeout_and_skips_retry(monkeypatch) -> None:
    service = FakeService()
    monkeypatch.setattr("clawcu.cli.get_service", lambda: service)

    result = runner.invoke(app, ["recreate", "writer", "--timeout", "30"])

    assert result.exit_code == 0
    method_names = [call[0] for call in service.calls]
    assert "retry_instance" not in method_names
    recreate_call = next(call for call in service.calls if call[0] == "recreate_instance")
    assert recreate_call[2] == {"name": "writer", "fresh": False, "timeout": 30}


def test_recreate_command_fresh_requires_confirm_by_default(monkeypatch) -> None:
    service = FakeService()
    monkeypatch.setattr("clawcu.cli.get_service", lambda: service)

    result = runner.invoke(app, ["recreate", "writer", "--fresh"])

    assert result.exit_code != 0
    assert not any(call[0] == "recreate_instance" for call in service.calls)


def test_recreate_command_fresh_with_yes_wipes_datadir(monkeypatch) -> None:
    service = FakeService()
    monkeypatch.setattr("clawcu.cli.get_service", lambda: service)

    result = runner.invoke(app, ["recreate", "writer", "--fresh", "--yes"])

    assert result.exit_code == 0
    recreate_call = next(call for call in service.calls if call[0] == "recreate_instance")
    assert recreate_call[2] == {"name": "writer", "fresh": True, "timeout": None}


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


def test_create_command_applies_provider_after_create(monkeypatch) -> None:
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
            "--apply-provider",
            "openai-main",
            "--apply-agent",
            "reviewer",
            "--apply-persist",
        ],
    )

    assert result.exit_code == 0
    apply_calls = [c for c in service.calls if c[0] == "apply_provider"]
    assert len(apply_calls) == 1
    assert apply_calls[0][2]["provider"] == "openai-main"
    assert apply_calls[0][2]["instance"] == "writer"
    assert apply_calls[0][2]["agent"] == "reviewer"
    assert apply_calls[0][2]["persist"] is True
    assert "Applied provider:" in result.stdout


def test_create_command_surfaces_apply_provider_failure_without_aborting(monkeypatch) -> None:
    class FlakyService(FakeService):
        def apply_provider(self, provider, instance, agent="main", *, persist=False, **kwargs):  # type: ignore[override]
            self._record("apply_provider", provider=provider, instance=instance, agent=agent, persist=persist)
            raise RuntimeError("collected bundle is invalid")

    service = FlakyService()
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
            "--apply-provider",
            "openai-main",
        ],
    )

    assert result.exit_code == 0
    assert "Created instance:" in result.stdout
    assert "--apply-provider failed" in result.stdout
    assert "clawcu provider apply openai-main writer" in result.stdout


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


def test_inspect_command_renders_human_view_by_default(monkeypatch) -> None:
    service = FakeService()
    monkeypatch.setattr("clawcu.cli.get_service", lambda: service)
    monkeypatch.setattr("clawcu.cli.console", _make_wide_console())

    result = runner.invoke(app, ["inspect", "writer"])

    assert result.exit_code == 0
    # Compact human view instead of raw JSON dump.
    assert "Instance: writer" in result.stdout
    assert "Service" in result.stdout
    assert "openclaw" in result.stdout
    assert "Snapshots" in result.stdout
    assert "latest_upgrade_snapshot" in result.stdout
    # Should NOT dump the full JSON by default.
    assert '"container"' not in result.stdout
    assert service.calls[-1] == ("inspect_instance", (), {"name": "writer"})


def test_inspect_command_json_mode_preserves_raw_payload(monkeypatch) -> None:
    service = FakeService()
    monkeypatch.setattr("clawcu.cli.get_service", lambda: service)

    result = runner.invoke(app, ["--json", "inspect", "writer"])

    assert result.exit_code == 0
    assert '"Name": "writer"' in result.stdout
    assert '"snapshots"' in result.stdout
    assert '"latest_upgrade_snapshot"' in result.stdout


def test_inspect_command_json_flag_accepted_after_subcommand(monkeypatch) -> None:
    """`--json` should work before OR after the subcommand for UX parity."""
    service = FakeService()
    monkeypatch.setattr("clawcu.cli.get_service", lambda: service)

    result = runner.invoke(app, ["inspect", "writer", "--json"])

    assert result.exit_code == 0
    assert '"Name": "writer"' in result.stdout
    assert '"snapshots"' in result.stdout


def test_list_command_json_flag_accepted_after_subcommand(monkeypatch) -> None:
    service = FakeService()
    monkeypatch.setattr("clawcu.cli.get_service", lambda: service)

    result = runner.invoke(app, ["list", "--json"])

    assert result.exit_code == 0
    # JSON payload starts with `[`
    assert result.stdout.strip().startswith("[")


def test_token_command_json_flag_accepted_after_subcommand(monkeypatch) -> None:
    service = FakeService()
    monkeypatch.setattr("clawcu.cli.get_service", lambda: service)

    result = runner.invoke(app, ["token", "writer", "--json"])

    assert result.exit_code == 0
    assert '"token"' in result.stdout
    assert "token-writer" in result.stdout


def test_getenv_command_json_flag_accepted_after_subcommand(monkeypatch) -> None:
    service = FakeService()
    monkeypatch.setattr("clawcu.cli.get_service", lambda: service)

    result = runner.invoke(app, ["getenv", "writer", "--json"])

    assert result.exit_code == 0
    assert '"instance"' in result.stdout
    assert '"values"' in result.stdout


def test_provider_list_json_flag_accepted_after_subcommand(monkeypatch) -> None:
    service = FakeService()
    monkeypatch.setattr("clawcu.cli.get_service", lambda: service)

    result = runner.invoke(app, ["provider", "list", "--json"])

    assert result.exit_code == 0
    assert result.stdout.strip().startswith("[")


def test_inspect_command_folds_history_by_default(monkeypatch) -> None:
    service = FakeService()

    original_inspect = service.inspect_instance

    def with_history(name: str) -> dict:
        payload = original_inspect(name)
        payload["instance"]["history"] = [
            {"action": "create", "timestamp": "2026-04-13T09:00:00+00:00"},
            {"action": "upgrade", "timestamp": "2026-04-14T00:00:00+00:00", "to_version": "2026.4.5"},
        ]
        return payload

    service.inspect_instance = with_history  # type: ignore[assignment]
    monkeypatch.setattr("clawcu.cli.get_service", lambda: service)
    monkeypatch.setattr("clawcu.cli.console", _make_wide_console())

    result = runner.invoke(app, ["inspect", "writer"])

    assert result.exit_code == 0
    assert "History" in result.stdout
    assert "2 event(s)" in result.stdout
    # Folded: the first (creation) event's timestamp is not shown; only the
    # latest is referenced in the summary line.
    assert "2026-04-13T09:00:00+00:00" not in result.stdout
    assert "pass --show-history to expand" in result.stdout


def test_inspect_command_show_history_expands_events(monkeypatch) -> None:
    service = FakeService()

    original_inspect = service.inspect_instance

    def with_history(name: str) -> dict:
        payload = original_inspect(name)
        payload["instance"]["history"] = [
            {"action": "create", "timestamp": "2026-04-13T09:00:00+00:00"},
            {"action": "upgrade", "timestamp": "2026-04-14T00:00:00+00:00", "to_version": "2026.4.5"},
        ]
        return payload

    service.inspect_instance = with_history  # type: ignore[assignment]
    monkeypatch.setattr("clawcu.cli.get_service", lambda: service)
    monkeypatch.setattr("clawcu.cli.console", _make_wide_console())

    result = runner.invoke(app, ["inspect", "writer", "--show-history"])

    assert result.exit_code == 0
    assert "2026-04-13T09:00:00+00:00" in result.stdout
    assert "2026-04-14T00:00:00+00:00" in result.stdout
    assert "to_version=2026.4.5" in result.stdout


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
        (["start", "writer"], "start_instance", {"name": "writer"}),
        (["stop", "writer"], "stop_instance", {"name": "writer", "timeout": None}),
        (
            ["restart", "writer"],
            "restart_instance",
            # Default ON: restart now auto-promotes to recreate when
            # env drift is detected, mirroring start_instance's
            # pre-existing behavior.
            {"name": "writer", "recreate_if_config_changed": True},
        ),
        (
            ["rollback", "writer", "--yes"],
            "rollback_instance",
            {"name": "writer", "to_version": None},
        ),
    ]

    for argv, expected_call, expected_kwargs in cases:
        service.calls.clear()
        result = runner.invoke(app, argv)
        assert result.exit_code == 0
        assert (expected_call, (), expected_kwargs) in service.calls
        if expected_call in {"start_instance", "restart_instance", "rollback_instance"}:
            assert service.calls[-1] == ("dashboard_url", (), {"name": "writer"})


def test_stop_command_passes_time_to_service(monkeypatch) -> None:
    service = FakeService()
    monkeypatch.setattr("clawcu.cli.get_service", lambda: service)

    result = runner.invoke(app, ["stop", "writer", "--time", "60"])

    assert result.exit_code == 0, result.stdout
    assert "Stopped instance:" in result.stdout
    assert "grace 60s" in result.stdout
    assert service.calls[0] == ("stop_instance", (), {"name": "writer", "timeout": 60})


def test_stop_command_short_flag_t_is_accepted(monkeypatch) -> None:
    service = FakeService()
    monkeypatch.setattr("clawcu.cli.get_service", lambda: service)

    result = runner.invoke(app, ["stop", "writer", "-t", "15"])

    assert result.exit_code == 0, result.stdout
    assert service.calls[0] == ("stop_instance", (), {"name": "writer", "timeout": 15})


def test_stop_command_rejects_negative_time(monkeypatch) -> None:
    service = FakeService()
    monkeypatch.setattr("clawcu.cli.get_service", lambda: service)

    result = runner.invoke(app, ["stop", "writer", "--time", "-1"])

    assert result.exit_code != 0
    assert not any(call[0] == "stop_instance" for call in service.calls)


def test_restart_command_default_passes_recreate_if_config_changed_true(monkeypatch) -> None:
    """`clawcu restart <name>` should default to drift-detecting recreate."""
    service = FakeService()
    monkeypatch.setattr("clawcu.cli.get_service", lambda: service)

    result = runner.invoke(app, ["restart", "writer"])

    assert result.exit_code == 0, result.stdout
    assert service.calls[0] == (
        "restart_instance",
        (),
        {"name": "writer", "recreate_if_config_changed": True},
    )


def test_restart_command_explicit_on_flag_matches_default(monkeypatch) -> None:
    """Explicit --recreate-if-config-changed behaves same as default."""
    service = FakeService()
    monkeypatch.setattr("clawcu.cli.get_service", lambda: service)

    result = runner.invoke(
        app, ["restart", "writer", "--recreate-if-config-changed"]
    )

    assert result.exit_code == 0, result.stdout
    assert service.calls[0] == (
        "restart_instance",
        (),
        {"name": "writer", "recreate_if_config_changed": True},
    )


def test_restart_command_no_flag_forces_plain_docker_restart(monkeypatch) -> None:
    """`--no-recreate-if-config-changed` is the opt-out escape hatch."""
    service = FakeService()
    monkeypatch.setattr("clawcu.cli.get_service", lambda: service)

    result = runner.invoke(
        app, ["restart", "writer", "--no-recreate-if-config-changed"]
    )

    assert result.exit_code == 0, result.stdout
    assert service.calls[0] == (
        "restart_instance",
        (),
        {"name": "writer", "recreate_if_config_changed": False},
    )


def test_upgrade_command_accepts_version_option(monkeypatch) -> None:
    service = FakeService()
    monkeypatch.setattr("clawcu.cli.get_service", lambda: service)

    result = runner.invoke(
        app, ["upgrade", "writer", "--version", "2026.4.2", "--yes"]
    )

    assert result.exit_code == 0, result.stdout
    # Plan was rendered before execution (new safety preview).
    assert "2026.4.1" in result.stdout and "2026.4.2" in result.stdout
    assert "Step 1/4: Preparing an upgrade plan" in result.stdout
    assert "Step 4/4: Recreating the container" in result.stdout
    # First call is the plan lookup, last two are upgrade + dashboard.
    call_names = [call[0] for call in service.calls]
    assert "upgrade_plan" in call_names
    assert service.calls[-2] == (
        "upgrade_instance",
        (),
        {"name": "writer", "version": "2026.4.2"},
    )
    assert service.calls[-1] == ("dashboard_url", (), {"name": "writer"})


def test_upgrade_command_list_versions_prints_history_and_local(monkeypatch) -> None:
    service = FakeService()
    monkeypatch.setattr("clawcu.cli.get_service", lambda: service)

    result = runner.invoke(app, ["upgrade", "writer", "--list-versions"])

    assert result.exit_code == 0, result.stdout
    # Image repo shown
    assert "ghcr.io/openclaw/openclaw" in result.stdout
    # Current version marker
    assert "2026.4.1" in result.stdout
    # History + local images both rendered
    assert "2026.3.20" in result.stdout
    assert "2026.4.2" in result.stdout
    # No actual upgrade was attempted
    call_names = [call[0] for call in service.calls]
    assert "list_upgradable_versions" in call_names
    assert "upgrade_instance" not in call_names
    assert "upgrade_plan" not in call_names


def test_upgrade_command_list_versions_json(monkeypatch) -> None:
    service = FakeService()
    monkeypatch.setattr("clawcu.cli.get_service", lambda: service)

    result = runner.invoke(
        app, ["upgrade", "writer", "--list-versions", "--json"]
    )

    assert result.exit_code == 0, result.stdout
    assert '"image_repo"' in result.stdout
    assert '"local_images"' in result.stdout


def test_upgrade_list_versions_queries_remote_by_default(monkeypatch) -> None:
    service = FakeService()
    monkeypatch.setattr("clawcu.cli.get_service", lambda: service)

    result = runner.invoke(app, ["upgrade", "writer", "--list-versions"])

    assert result.exit_code == 0, result.stdout
    # Remote should have been requested by default.
    assert (
        "list_upgradable_versions",
        (),
        {"name": "writer", "include_remote": True},
    ) in service.calls
    # Remote tag from the FakeService payload should render.
    assert "2026.4.3" in result.stdout
    # Remote section header present (with "Remote" label).
    assert "Remote" in result.stdout


def test_upgrade_list_versions_no_remote_suppresses_query(monkeypatch) -> None:
    service = FakeService()
    monkeypatch.setattr("clawcu.cli.get_service", lambda: service)

    result = runner.invoke(
        app, ["upgrade", "writer", "--list-versions", "--no-remote"]
    )

    assert result.exit_code == 0, result.stdout
    # include_remote=False was threaded through the service call.
    assert (
        "list_upgradable_versions",
        (),
        {"name": "writer", "include_remote": False},
    ) in service.calls
    assert "skipped (--no-remote)" in result.stdout


def test_upgrade_list_versions_renders_remote_failure_warning(monkeypatch) -> None:
    service = FakeService()

    # Override the fake to simulate a failed remote fetch.
    def _failing(name, *, include_remote=True):
        service._record(
            "list_upgradable_versions", name=name, include_remote=include_remote
        )
        return {
            "instance": name,
            "service": "openclaw",
            "image_repo": "ghcr.io/openclaw/openclaw",
            "current_version": "2026.4.1",
            "history": ["2026.4.1"],
            "local_images": ["2026.4.1"],
            "remote_versions": None,
            "remote_error": "network error: timeout",
            "remote_registry": "ghcr.io",
            "remote_requested": True,
        }

    service.list_upgradable_versions = _failing  # type: ignore[method-assign]
    monkeypatch.setattr("clawcu.cli.get_service", lambda: service)

    result = runner.invoke(app, ["upgrade", "writer", "--list-versions"])

    assert result.exit_code == 0, result.stdout
    # The warning line includes the reason so users know why remote is
    # missing; no crash on the failure.
    assert "fetch failed" in result.stdout
    assert "timeout" in result.stdout


def _make_truncating_service(remote_tags: list[str]) -> FakeService:
    """Build a FakeService whose list_upgradable_versions returns ``remote_tags``.

    Used to exercise the CLI truncation branch around 10+ remote versions.
    """
    service = FakeService()

    def _with_many(name, *, include_remote=True):
        service._record(
            "list_upgradable_versions", name=name, include_remote=include_remote
        )
        return {
            "instance": name,
            "service": "openclaw",
            "image_repo": "ghcr.io/openclaw/openclaw",
            "current_version": "2026.4.1",
            "history": ["2026.4.1"],
            "local_images": ["2026.4.1"],
            "remote_versions": list(remote_tags) if include_remote else None,
            "remote_error": None,
            "remote_registry": "ghcr.io" if include_remote else None,
            "remote_requested": include_remote,
        }

    service.list_upgradable_versions = _with_many  # type: ignore[method-assign]
    return service


def test_upgrade_list_versions_truncates_remote_to_last_ten(monkeypatch) -> None:
    # 15 tags in ascending order — default render should only show the
    # 10 most recent (tail) and mention the hidden ones. Two distinct
    # year buckets keep "hidden" tags from being substring-matched
    # inside "shown" tags (e.g. 2020.1.1 is not a prefix of 2026.5.10).
    hidden_tags = [f"2020.1.{i}" for i in range(1, 6)]
    shown_tags = [f"2026.5.{i}" for i in range(1, 11)]
    remote_tags = hidden_tags + shown_tags
    service = _make_truncating_service(remote_tags)
    monkeypatch.setattr("clawcu.cli.get_service", lambda: service)

    result = runner.invoke(app, ["upgrade", "writer", "--list-versions"])

    assert result.exit_code == 0, result.stdout
    # Tail (most recent 10) is shown...
    for recent in shown_tags:
        assert recent in result.stdout
    # ...and the earliest hidden tags are NOT shown.
    for hidden in hidden_tags:
        assert hidden not in result.stdout
    # Truncation summary communicates the hidden count + how to see all.
    assert "showing 10 of 15" in result.stdout
    assert "--all-versions" in result.stdout


def test_upgrade_list_versions_all_versions_shows_full_remote(monkeypatch) -> None:
    remote_tags = [f"2026.5.{i}" for i in range(1, 16)]
    service = _make_truncating_service(remote_tags)
    monkeypatch.setattr("clawcu.cli.get_service", lambda: service)

    result = runner.invoke(
        app, ["upgrade", "writer", "--list-versions", "--all-versions"]
    )

    assert result.exit_code == 0, result.stdout
    # Every tag is rendered — no truncation.
    for tag in remote_tags:
        assert tag in result.stdout
    # Header shows the full count without the "showing N of M" hint.
    assert "15 release tags" in result.stdout
    assert "showing 10 of" not in result.stdout


def test_upgrade_list_versions_does_not_truncate_below_threshold(monkeypatch) -> None:
    # At exactly 10 remote tags, truncation must NOT kick in — the list
    # is already at the preview limit.
    remote_tags = [f"2026.5.{i}" for i in range(1, 11)]
    service = _make_truncating_service(remote_tags)
    monkeypatch.setattr("clawcu.cli.get_service", lambda: service)

    result = runner.invoke(app, ["upgrade", "writer", "--list-versions"])

    assert result.exit_code == 0, result.stdout
    assert "10 release tags" in result.stdout
    assert "showing" not in result.stdout
    assert "--all-versions to see" not in result.stdout


def test_upgrade_list_versions_json_payload_is_not_truncated(monkeypatch) -> None:
    # JSON consumers must always get the full list; truncation is
    # presentational only.
    remote_tags = [f"2026.5.{i}" for i in range(1, 16)]
    service = _make_truncating_service(remote_tags)
    monkeypatch.setattr("clawcu.cli.get_service", lambda: service)

    result = runner.invoke(
        app, ["upgrade", "writer", "--list-versions", "--json"]
    )

    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["remote_versions"] == remote_tags


def test_upgrade_command_dry_run_shows_plan_without_executing(monkeypatch) -> None:
    service = FakeService()
    monkeypatch.setattr("clawcu.cli.get_service", lambda: service)

    result = runner.invoke(
        app,
        ["upgrade", "writer", "--version", "2026.4.2", "--dry-run"],
    )

    assert result.exit_code == 0, result.stdout
    assert "Dry run" in result.stdout
    # Plan fields rendered
    assert "2026.4.1" in result.stdout and "2026.4.2" in result.stdout
    assert "preserved" in result.stdout.lower() or "key(s)" in result.stdout
    assert "upgrade-to-2026.4.2" in result.stdout
    # No upgrade call went through
    call_names = [call[0] for call in service.calls]
    assert "upgrade_plan" in call_names
    assert "upgrade_instance" not in call_names


def test_upgrade_command_dry_run_json_emits_plan(monkeypatch) -> None:
    service = FakeService()
    monkeypatch.setattr("clawcu.cli.get_service", lambda: service)

    result = runner.invoke(
        app,
        ["upgrade", "writer", "--version", "2026.4.2", "--dry-run", "--json"],
    )

    assert result.exit_code == 0, result.stdout
    assert '"current_version"' in result.stdout
    assert '"target_version"' in result.stdout
    assert '"snapshot_label"' in result.stdout


def test_upgrade_command_without_yes_refuses_in_non_interactive(monkeypatch) -> None:
    """In CliRunner (no TTY) and no --yes, the confirm prompt must refuse."""
    service = FakeService()
    monkeypatch.setattr("clawcu.cli.get_service", lambda: service)

    result = runner.invoke(app, ["upgrade", "writer", "--version", "2026.4.2"])

    assert result.exit_code != 0
    # Plan gets rendered first (that's fine), but the upgrade must NOT run.
    call_names = [call[0] for call in service.calls]
    assert "upgrade_instance" not in call_names


def test_rollback_command_prints_progress(monkeypatch) -> None:
    service = FakeService()
    monkeypatch.setattr("clawcu.cli.get_service", lambda: service)

    result = runner.invoke(app, ["rollback", "writer", "--yes"])

    assert result.exit_code == 0
    assert "Step 1/4: Preparing to roll back" in result.stdout
    assert "Step 4/4: Starting OpenClaw" in result.stdout
    # Rollback now always renders a plan (via rollback_plan) before executing.
    call_names = [call[0] for call in service.calls]
    assert call_names.index("rollback_plan") < call_names.index("rollback_instance")
    assert (
        "rollback_instance",
        (),
        {"name": "writer", "to_version": None},
    ) in service.calls
    assert service.calls[-1] == ("dashboard_url", (), {"name": "writer"})


def test_rollback_command_requires_confirmation_in_non_interactive(monkeypatch) -> None:
    service = FakeService()
    monkeypatch.setattr("clawcu.cli.get_service", lambda: service)

    result = runner.invoke(app, ["rollback", "writer"])

    assert result.exit_code == 1
    assert "--yes" in result.stdout
    assert all(call[0] != "rollback_instance" for call in service.calls)


def test_rollback_command_accepts_to_version_option(monkeypatch) -> None:
    service = FakeService()
    monkeypatch.setattr("clawcu.cli.get_service", lambda: service)

    result = runner.invoke(
        app,
        ["rollback", "writer", "--to", "2026.3.20", "--yes"],
    )

    assert result.exit_code == 0, result.stdout
    # Plan is previewed with the requested target version first.
    assert (
        "rollback_plan",
        (),
        {"name": "writer", "to_version": "2026.3.20"},
    ) in service.calls
    # Then the instance rollback is invoked with the same target.
    assert (
        "rollback_instance",
        (),
        {"name": "writer", "to_version": "2026.3.20"},
    ) in service.calls


def test_rollback_command_list_renders_targets(monkeypatch) -> None:
    service = FakeService()
    monkeypatch.setattr("clawcu.cli.get_service", lambda: service)

    result = runner.invoke(app, ["rollback", "writer", "--list"])

    assert result.exit_code == 0, result.stdout
    # --list is pure-read; it must not execute a rollback.
    assert ("list_rollback_targets", (), {"name": "writer"}) in service.calls
    call_names = [call[0] for call in service.calls]
    assert "rollback_instance" not in call_names
    assert "2026.3.20" in result.stdout
    assert "2026.4.0" in result.stdout


def test_rollback_command_list_json_emits_payload(monkeypatch) -> None:
    service = FakeService()
    monkeypatch.setattr("clawcu.cli.get_service", lambda: service)

    result = runner.invoke(app, ["rollback", "writer", "--list", "--json"])

    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["instance"] == "writer"
    assert len(payload["targets"]) == 2
    assert payload["targets"][0]["restores_to"] == "2026.3.20"


def test_rollback_command_dry_run_skips_execution(monkeypatch) -> None:
    service = FakeService()
    monkeypatch.setattr("clawcu.cli.get_service", lambda: service)

    result = runner.invoke(
        app,
        ["rollback", "writer", "--to", "2026.4.0", "--dry-run"],
    )

    assert result.exit_code == 0, result.stdout
    assert "Dry run" in result.stdout
    # rollback_plan must be called; rollback_instance must NOT run.
    call_names = [call[0] for call in service.calls]
    assert "rollback_plan" in call_names
    assert "rollback_instance" not in call_names


def test_rollback_command_dry_run_json_emits_plan(monkeypatch) -> None:
    service = FakeService()
    monkeypatch.setattr("clawcu.cli.get_service", lambda: service)

    result = runner.invoke(
        app,
        ["rollback", "writer", "--to", "2026.4.0", "--dry-run", "--json"],
    )

    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["current_version"] == "2026.4.1"
    assert payload["target_version"] == "2026.4.0"
    assert "restore_snapshot" in payload


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
            "version": None,
            "include_secrets": True,
        },
    )
    assert service.calls[-1] == ("dashboard_url", (), {"name": "writer-exp"})


def test_clone_command_accepts_version_override(monkeypatch) -> None:
    # --version switches the clone to a different service version
    # without touching the source. Useful for "clone then upgrade".
    service = FakeService()
    monkeypatch.setattr("clawcu.cli.get_service", lambda: service)

    result = runner.invoke(
        app,
        [
            "clone",
            "writer",
            "--name",
            "writer-exp",
            "--version",
            "2026.4.2",
        ],
    )

    assert result.exit_code == 0, result.stdout
    # Service call carries the requested version, default include_secrets stays True.
    clone_call = next(c for c in service.calls if c[0] == "clone_instance")
    assert clone_call[2]["version"] == "2026.4.2"
    assert clone_call[2]["include_secrets"] is True
    # Success line mentions the version switch so the user sees it.
    assert "2026.4.2" in result.stdout
    assert "switched from source" in result.stdout


def test_clone_command_exclude_secrets_forwards_flag(monkeypatch) -> None:
    # --exclude-secrets flips include_secrets off AND surfaces a
    # prominent warning so the user knows they need to re-seed env.
    service = FakeService()
    monkeypatch.setattr("clawcu.cli.get_service", lambda: service)

    result = runner.invoke(
        app,
        [
            "clone",
            "writer",
            "--name",
            "writer-exp",
            "--exclude-secrets",
        ],
    )

    assert result.exit_code == 0, result.stdout
    clone_call = next(c for c in service.calls if c[0] == "clone_instance")
    assert clone_call[2]["include_secrets"] is False
    # Post-clone line tells the user env wasn't copied + how to re-seed.
    assert "NOT copied" in result.stdout
    assert "setenv" in result.stdout


def test_clone_command_include_secrets_is_default(monkeypatch) -> None:
    # When neither flag is passed, include_secrets defaults to True and
    # the CLI stays quiet about env (no warning line).
    service = FakeService()
    monkeypatch.setattr("clawcu.cli.get_service", lambda: service)

    result = runner.invoke(
        app,
        ["clone", "writer", "--name", "writer-exp"],
    )

    assert result.exit_code == 0, result.stdout
    clone_call = next(c for c in service.calls if c[0] == "clone_instance")
    assert clone_call[2]["include_secrets"] is True
    assert "NOT copied" not in result.stdout


def test_clone_command_help_documents_env_semantics(monkeypatch) -> None:
    # Design-review requirement: clone help must explicitly state
    # whether the env file is copied. The description mentions env
    # copying plus the --exclude-secrets escape hatch.
    result = runner.invoke(app, ["clone", "--help"])

    assert result.exit_code == 0
    assert "env file" in result.stdout
    assert "--exclude-secrets" in result.stdout


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
