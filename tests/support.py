from __future__ import annotations

import json
from pathlib import Path

from clawcu.models import ContainerRunSpec, InstanceRecord
from clawcu.paths import get_paths
from clawcu.service import ClawCUService
from clawcu.storage import StateStore


class FakeDockerManager:
    def __init__(self) -> None:
        self.commands: list[tuple[str, str]] = []
        self.exec_commands: list[tuple[str, list[str], dict]] = []
        self.interactive_exec_commands: list[tuple[str, list[str], dict]] = []
        self.status_map: dict[str, str] = {}
        self.health_map: dict[str, str] = {}
        self.status_sequences: dict[str, list[str]] = {}
        self.startup_sequences: dict[str, list[str]] = {}
        self.run_env_files: list[str | None] = []
        self.fail_next_run = False
        self.fail_next_start = False
        self.run_errors: list[Exception] = []

    def image_exists(self, image_tag: str) -> bool:
        return True

    def run_container(self, record: InstanceRecord, spec: ContainerRunSpec) -> None:
        self.commands.append(("run", record.container_name))
        self.run_env_files.append(spec.env_file)
        if self.run_errors:
            raise self.run_errors.pop(0)
        if self.fail_next_run:
            self.fail_next_run = False
            raise RuntimeError("boom")
        self.status_map[record.container_name] = "running"
        if record.container_name in self.startup_sequences:
            self.status_sequences[record.container_name] = list(
                self.startup_sequences[record.container_name]
            )

    def container_status(self, container_name: str) -> str:
        sequence = self.status_sequences.get(container_name)
        if sequence:
            status = sequence.pop(0)
            if not sequence:
                self.status_sequences.pop(container_name, None)
            if status in {"starting", "unhealthy"}:
                self.status_map[container_name] = "running"
                self.health_map[container_name] = status
            else:
                self.status_map[container_name] = status
                if status == "running":
                    self.health_map.pop(container_name, None)
            return status
        status = self.status_map.get(container_name, "missing")
        if status == "running":
            health = self.health_map.get(container_name)
            if health in {"starting", "unhealthy"}:
                return health
        return status

    def inspect_container(self, container_name: str) -> dict | None:
        status = self.status_map.get(container_name)
        if not status:
            return None
        state: dict[str, object] = {"Status": status}
        if status == "running" and container_name in self.health_map:
            state["Health"] = {"Status": self.health_map[container_name]}
        return {"Name": container_name, "State": state}

    def start_container(self, container_name: str) -> None:
        self.commands.append(("start", container_name))
        if self.fail_next_start:
            self.fail_next_start = False
            raise RuntimeError("port is already allocated")
        self.status_map[container_name] = "running"

    def stop_container(self, container_name: str) -> None:
        self.commands.append(("stop", container_name))
        self.status_map[container_name] = "exited"

    def restart_container(self, container_name: str) -> None:
        self.commands.append(("restart", container_name))
        self.status_map[container_name] = "running"

    def remove_container(self, container_name: str, *, missing_ok: bool = False) -> None:
        self.commands.append(("rm", container_name))
        self.status_map.pop(container_name, None)

    def exec_in_container(self, container_name: str, command: list[str], **kwargs) -> object:
        self.commands.append(("exec", container_name))
        self.exec_commands.append((container_name, command, kwargs))
        return type("Completed", (), {"stdout": "", "stderr": "", "returncode": 0})()

    def exec_in_container_interactive(self, container_name: str, command: list[str], **kwargs) -> object:
        self.commands.append(("exec-interactive", container_name))
        self.interactive_exec_commands.append((container_name, command, kwargs))
        return type("Completed", (), {"stdout": "", "stderr": "", "returncode": 0})()

    def stream_logs(self, container_name: str, *, follow: bool = False) -> None:
        self.commands.append(("logs", container_name))


class FakeOpenClawManager:
    def __init__(self) -> None:
        self.versions: list[str] = []
        self.image_repo = "ghcr.io/openclaw/openclaw"

    def build_image(self, version: str) -> str:
        self.versions.append(version)
        return f"clawcu/openclaw:{version}"

    def ensure_image(self, version: str) -> str:
        self.versions.append(version)
        return f"clawcu/openclaw:{version}"


class FakeHermesManager:
    def __init__(self) -> None:
        self.versions: list[str] = []
        self.source_repo = "https://github.com/NousResearch/hermes-agent.git"

    def ensure_image(self, version: str) -> str:
        self.versions.append(version)
        return f"clawcu/hermes:{version}"


def make_service(
    temp_clawcu_home,
) -> tuple[ClawCUService, FakeDockerManager, FakeOpenClawManager, StateStore]:
    store = StateStore(get_paths())
    docker = FakeDockerManager()
    openclaw = FakeOpenClawManager()
    hermes = FakeHermesManager()
    service = ClawCUService(store=store, docker=docker, openclaw=openclaw, hermes=hermes)
    service._local_openclaw_home = lambda: temp_clawcu_home / ".missing-openclaw"  # type: ignore[method-assign]
    service._local_hermes_home = lambda: temp_clawcu_home / ".missing-hermes"  # type: ignore[method-assign]
    return service, docker, openclaw, store


def write_provider_source(
    root: Path,
    *,
    agent_name: str = "main",
    provider_name: str = "minimax",
    profile_name: str = "minimax:cn",
    api_key: str = "sk-test",
    api: str = "anthropic-messages",
    endpoint: str = "https://api.minimaxi.com/anthropic",
    models: list[dict] | None = None,
) -> None:
    runtime_dir = root / "agents" / agent_name / "agent"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    models_payload = {
        "providers": {
            provider_name: {
                "baseUrl": endpoint,
                "api": api,
                "authHeader": api.startswith("anthropic"),
                "models": models
                or [
                    {
                        "id": "MiniMax-M2.7",
                        "name": "MiniMax M2.7",
                    }
                ],
                "apiKey": api_key,
            }
        }
    }
    auth_payload = {
        "version": 1,
        "profiles": {
            profile_name: {
                "type": "api_key",
                "provider": provider_name,
                "key": api_key,
            }
        },
        "lastGood": {
            provider_name: profile_name,
        },
        "usageStats": {
            profile_name: {
                "errorCount": 0,
                "lastUsed": 1775986644716,
            }
        },
    }
    (runtime_dir / "models.json").write_text(
        json.dumps(models_payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (runtime_dir / "auth-profiles.json").write_text(
        json.dumps(auth_payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def write_root_provider_source(
    root: Path,
    *,
    provider_name: str = "minimax",
    api_key: str = "sk-root",
    api: str = "anthropic-messages",
    endpoint: str = "https://api.minimaxi.com/anthropic",
    models: list[dict] | None = None,
    profile_name: str | None = None,
) -> None:
    root.mkdir(parents=True, exist_ok=True)
    profiles: dict[str, dict] = {}
    if profile_name is not None:
        profiles[profile_name] = {
            "provider": provider_name,
            "mode": "api_key",
        }
    payload = {
        "models": {
            "providers": {
                provider_name: {
                    "baseUrl": endpoint,
                    "api": api,
                    "authHeader": api.startswith("anthropic"),
                    "models": models
                    or [
                        {
                            "id": "MiniMax-M2.7",
                            "name": "MiniMax M2.7",
                        }
                    ],
                    "apiKey": api_key,
                }
            }
        },
        "auth": {
            "profiles": profiles,
        },
    }
    (root / "openclaw.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
