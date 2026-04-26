from __future__ import annotations

import os
import re
import secrets
import time
import urllib.error
import urllib.request
from pathlib import Path

import yaml

from clawcu import __version__ as clawcu_version
from clawcu.a2a.sidecar_plugin import resolve_advertise_host
from clawcu.core.adapters import ServiceAdapter
from clawcu.core.models import AccessInfo, ContainerRunSpec, InstanceRecord, InstanceSpec
from clawcu.core.validation import (
    build_instance_record,
    normalize_service_version,
    resolve_datadir,
    updated_record,
    utc_now_iso,
    validate_cpu,
    validate_memory,
    validate_name,
    validate_port,
)
from clawcu.hermes.manager import HermesManager

HERMES_MODEL_ENV_ALLOWLIST = {
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "OPENROUTER_API_KEY",
    "ANTHROPIC_API_KEY",
    "AI_GATEWAY_API_KEY",
    "GLM_API_KEY",
    "GLM_BASE_URL",
    "KIMI_API_KEY",
    "KIMI_BASE_URL",
    "MINIMAX_API_KEY",
    "MINIMAX_BASE_URL",
    "MINIMAX_CN_API_KEY",
    "MINIMAX_CN_BASE_URL",
    "DASHSCOPE_API_KEY",
    "DASHSCOPE_BASE_URL",
    "HF_TOKEN",
    "HF_BASE_URL",
    "DEEPSEEK_API_KEY",
    "KILOCODE_API_KEY",
    "OPENCODE_ZEN_API_KEY",
    "OPENCODE_GO_API_KEY",
    "COPILOT_GITHUB_TOKEN",
    "GH_TOKEN",
}

HERMES_EXECUTABLE = "hermes"
HERMES_EXEC_PATH = f"/opt/hermes/.venv/bin:/opt/hermes:{os.environ.get('PATH', '/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin')}"

# Persona contract: Hermes reads $HERMES_HOME/SOUL.md on every turn (see
# hermes-agent agent/prompt_builder.py::load_soul_md). Our run_spec sets
# HERMES_HOME=/opt/data and mounts record.datadir onto /opt/data, so a file
# at <datadir>/SOUL.md lands exactly where load_soul_md expects it. This is
# the Hermes equivalent of OpenClaw's <datadir>/workspace/IDENTITY.md path.
HERMES_SOUL_FILENAME = "SOUL.md"
_HERMES_SOUL_PLACEHOLDER = """\
# Hermes Agent Persona

Hermes reads this file verbatim on every turn and prepends it to the agent's
system prompt (see `agent/prompt_builder.py::load_soul_md` in hermes-agent).
Clawcu mounts it to `$HERMES_HOME/SOUL.md` inside the container.

Edit freely — Hermes reloads on the next turn, no restart needed. Delete the
file to fall back to Hermes' built-in `DEFAULT_AGENT_IDENTITY`.

---

## Minimal example / 最小示例

Replace everything below with your own persona. Keep it short — this text is
layered on *top* of Hermes' built-in system prompt, so you don't need to
re-spell the assistant's base rules; focus on identity and voice.

> You are "Scribe", a concise research assistant.
>
> - Keep replies under five sentences unless the user asks for more.
> - When asked for code, always wrap it in fenced blocks.
> - If you're uncertain, say so in one sentence before answering.

## 最小示例 (中文)

把下面内容换成你自己的 persona 即可. 文字会被叠在 Hermes 自带 system
prompt *之上*, 不需要重写助手的基础规则 — 只写身份和语气.

> 你是 "抄写员", 一个简洁的研究助手.
>
> - 除非用户要求详细回答, 回复控制在五句话以内.
> - 给代码时始终用 fenced code block 包裹.
> - 如果不确定, 先用一句话说明, 再回答问题.

## A2A signature tip

If you rely on A2A federation and want to verify the persona layer is
active end-to-end, add a short mandatory signature line (e.g. `— Scribe`)
and check inbound replies contain it.
"""


class HermesAdapter(ServiceAdapter):
    service_name = "hermes"
    display_name = "Hermes"
    default_port = 8652
    internal_port = 8642
    dashboard_default_port = 9129
    dashboard_internal_port = 9119

    # Review-1 §3: A2A protocol defaults. Hermes sidecar binds
    # display_port directly — only one offset to probe.
    a2a_skills = ("chat", "analysis")
    a2a_role = "Hermes local analyst"
    a2a_plugin_port_offsets = (0,)

    def __init__(self, manager: HermesManager):
        self.manager = manager

    def prepare_artifact(self, version: str) -> str:
        return self.manager.ensure_image(version)

    def default_datadir(self, service, name: str) -> str:
        return str((service.store.paths.home / name).resolve())

    def default_auth_mode(self) -> str:
        return "native"

    def build_spec(self, service, *, name: str, version: str, datadir: str | None, port: int | None, cpu: str, memory: str) -> InstanceSpec:
        validated_name = validate_name(name)
        resolved_datadir = resolve_datadir(datadir) if datadir else self.default_datadir(service, validated_name)
        resolved_port = validate_port(port) if port is not None else service._next_available_port(self.default_port)
        resolved_dashboard_port = service._next_available_port(self.dashboard_default_port)
        if resolved_dashboard_port == resolved_port:
            resolved_dashboard_port = service._next_available_port(
                resolved_dashboard_port + service.PORT_SEARCH_STEP
            )
        return InstanceSpec(
            service=self.service_name,
            name=validated_name,
            version=normalize_service_version(self.service_name, version),
            datadir=resolved_datadir,
            port=resolved_port,
            cpu=validate_cpu(cpu),
            memory=validate_memory(memory),
            auth_mode="native",
            dashboard_port=resolved_dashboard_port,
        )

    def env_path(self, service, record: InstanceRecord | str) -> Path:
        if isinstance(record, str):
            datadir = self.default_datadir(service, record)
        else:
            datadir = record.datadir
        return Path(datadir) / ".env"

    def profile_home_path(self, service, record: InstanceRecord | str) -> Path:
        if isinstance(record, str):
            datadir = self.default_datadir(service, record)
        else:
            datadir = record.datadir
        return Path(datadir) / ".hermes"

    def run_spec(self, service, record: InstanceRecord) -> ContainerRunSpec:
        env_path = self.env_path(service, record)
        env_values = service._load_env_file(env_path)
        profile_home = self.profile_home_path(service, record)
        extra_env: dict[str, str] = {
            "HERMES_HOME": "/opt/data",
            "API_SERVER_ENABLED": "true",
            "API_SERVER_HOST": "0.0.0.0",
            "API_SERVER_KEY": env_values["API_SERVER_KEY"],
        }
        # When the a2a plugin is baked in, the sidecar binds the dashboard
        # internal port (9119) — which Hermes itself does not use. So the
        # existing dashboard_port → 9119 mapping already exposes the A2A
        # sidecar to the host; we just need to tell the sidecar what name
        # and advertised endpoint to report.
        extra_hosts: list[tuple[str, str]] = []
        if record.a2a_enabled:
            advertised_port = record.dashboard_port or self.dashboard_default_port
            # Review-1 P0-B: Linux parity. Docker Desktop auto-resolves
            # host.docker.internal; Linux doesn't unless we ask. Always
            # safe to pass — a no-op when the DNS name already resolves.
            extra_hosts.append(("host.docker.internal", "host-gateway"))
            extra_env.update(
                {
                    "A2A_SELF_NAME": record.name,
                    "A2A_BIND_PORT": str(self.dashboard_internal_port),
                    # Review-9 P1-A3: see openclaw adapter. Resolver picks
                    # host.docker.internal on Darwin/Windows so cross-
                    # container peer calls land on the right host.
                    "A2A_ADVERTISE_HOST": resolve_advertise_host(record),
                    "A2A_ADVERTISE_PORT": str(advertised_port),
                    # Hermes' API server exposes readiness at /health. The
                    # sidecar is gateway-agnostic (review-7 P2-E): the
                    # adapter declares the path so the same sidecar code
                    # can wrap gateways that use /healthz instead.
                    "A2A_GATEWAY_READY_PATH": "/health",
                    # Review-10 P2-C: tee sidecar logs into the datadir
                    # mount so they survive `clawcu recreate`. Host sees
                    # them at <record.datadir>/logs/a2a-sidecar.log.
                    "A2A_SIDECAR_LOG_DIR": "/opt/data/logs",
                    # Review-14 P1-C: per-peer / per-thread conversation
                    # history. Mirror of openclaw iter 13: when the peer
                    # sends thread_id, the sidecar reads/appends
                    # <dir>/<peer>/<thread_id>.jsonl so the agent sees
                    # continuous context. Lives under the /opt/data mount
                    # so threads survive `clawcu recreate`.
                    "A2A_THREAD_DIR": "/opt/data/threads",
                }
            )
            # Review-1 P0-B: auto-inject registry URL default; user env
            # overrides (parity with OpenClaw adapter).
            if not env_values.get("A2A_REGISTRY_URL"):
                extra_env["A2A_REGISTRY_URL"] = "http://host.docker.internal:9100"
            # a2a-design-4.md §P0-A: auto-wiring bootstrap. The sidecar
            # merges `mcp.servers.a2a` into the Hermes config.yaml so the
            # LLM sees `a2a_call_peer`. User env-file entries win.
            if not env_values.get("A2A_ENABLED"):
                extra_env["A2A_ENABLED"] = "true"
            if not env_values.get("A2A_SERVICE_MCP_CONFIG_PATH"):
                extra_env["A2A_SERVICE_MCP_CONFIG_PATH"] = "/opt/data/config.yaml"
            if not env_values.get("A2A_SERVICE_MCP_CONFIG_FORMAT"):
                extra_env["A2A_SERVICE_MCP_CONFIG_FORMAT"] = "yaml"
        return ContainerRunSpec(
            internal_port=self.internal_port,
            mount_target="/opt/data",
            env_file=str(env_path) if env_path.exists() else None,
            extra_env=extra_env,
            # The Hermes Docker image already uses an entrypoint that executes
            # `hermes "$@"`, so we only pass the subcommand here.
            command=["gateway", "run"],
            additional_ports=[
                (record.dashboard_port, self.dashboard_internal_port)
            ]
            if record.dashboard_port is not None
            else [],
            additional_mounts=[(str(profile_home), "/root/.hermes")],
            extra_hosts=extra_hosts,
        )

    def configure_before_run(self, service, record: InstanceRecord) -> None:
        datadir = Path(record.datadir)
        datadir.mkdir(parents=True, exist_ok=True)
        profile_home = self.profile_home_path(service, record)
        profile_home.mkdir(parents=True, exist_ok=True)
        config_path = datadir / "config.yaml"
        if not config_path.exists():
            payload = {
                "model": {
                    "provider": "openrouter",
                    "default": "anthropic/claude-sonnet-4.6",
                },
                "terminal": {
                    "backend": "local",
                },
            }
            config_path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
        env_path = self.env_path(service, record)
        env_path.parent.mkdir(parents=True, exist_ok=True)
        env_values = service._load_env_file(env_path)
        if not env_values.get("API_SERVER_KEY"):
            env_values["API_SERVER_KEY"] = secrets.token_hex(32)
        env_path.write_text(service._dump_env_file(env_values), encoding="utf-8")
        # For a2a-enabled instances scaffold a SOUL.md placeholder on first
        # configure so the A2A peer gets an identifiable persona from turn 1.
        # We never overwrite an existing SOUL.md — that's the user's content.
        if getattr(record, "a2a_enabled", False):
            soul_path = datadir / HERMES_SOUL_FILENAME
            if not soul_path.exists():
                soul_path.write_text(_HERMES_SOUL_PLACEHOLDER, encoding="utf-8")
                service.reporter(
                    f"Wrote Hermes persona placeholder to {soul_path}. "
                    "Edit this file to customize the agent's identity; Hermes reloads it on each turn."
                )
        service._make_runtime_tree_writable(datadir)

    def wait_for_readiness(self, service, record: InstanceRecord) -> InstanceRecord:
        pending_statuses = {"starting", "created"}
        if record.status == "running" and self._dashboard_ready(record):
            return record
        service.reporter(
            f"Waiting for Hermes to become ready on port {record.port}. "
            f"You can watch Docker state with 'docker ps --filter name={record.container_name}' "
            f"or inspect details with 'clawcu inspect {record.name}' or 'clawcu logs {record.name}'."
        )
        start_time = time.monotonic()
        startup_timeout = max(float(getattr(service, "STARTUP_TIMEOUT_SECONDS", 120.0)), 0.0)
        last_reported_status: str | None = None
        last_reported_bucket = -1
        current = record
        while True:
            current = service._persist_live_status(current)
            if self._dashboard_ready(current):
                ready = updated_record(current, status="running", last_error=None)
                service.store.save_record(ready)
                service.reporter(
                    f"Hermes API server is responding on http://127.0.0.1:{ready.port}/health. Marking the instance as ready."
                )
                return ready
            if current.status not in pending_statuses and current.status != "running":
                message = (
                    f"Instance '{current.name}' did not become ready. Current status is '{current.status}'. "
                    f"Use 'clawcu inspect {current.name}' or 'clawcu logs {current.name}' for details."
                )
                service.store.save_record(updated_record(current, last_error=message))
                raise RuntimeError(message)
            elapsed = int(time.monotonic() - start_time)
            if startup_timeout and (time.monotonic() - start_time) >= startup_timeout:
                message = (
                    f"Instance '{current.name}' did not become ready within {int(startup_timeout)}s. "
                    f"Current status is '{current.status}'. "
                    f"Use 'clawcu inspect {current.name}' or 'clawcu logs {current.name}' for details."
                )
                service.store.save_record(updated_record(current, last_error=message))
                raise RuntimeError(message)
            progress_bucket = (
                int(elapsed // service.STARTUP_PROGRESS_INTERVAL_SECONDS)
                if service.STARTUP_PROGRESS_INTERVAL_SECONDS > 0
                else elapsed
            )
            if current.status != last_reported_status or progress_bucket != last_reported_bucket:
                service.reporter(
                    f"Hermes is still {current.status} on port {current.port} after {elapsed}s. Continuing to wait for readiness."
                )
                last_reported_status = current.status
                last_reported_bucket = progress_bucket
            time.sleep(service.STARTUP_POLL_INTERVAL_SECONDS)

    def _dashboard_ready(self, record: InstanceRecord) -> bool:
        urls = [
            f"http://127.0.0.1:{record.port}/",
            f"http://127.0.0.1:{record.port}/health",
            f"http://127.0.0.1:{record.port}/healthz",
            f"http://127.0.0.1:{record.port}/v1/models",
        ]
        for url in urls:
            request = urllib.request.Request(url, headers={"User-Agent": f"clawcu/{clawcu_version}"})
            try:
                with urllib.request.urlopen(request, timeout=2) as response:
                    if 200 <= response.status < 500:
                        return True
            except (urllib.error.URLError, TimeoutError, ValueError, OSError):
                continue
        return False

    def _dashboard_base_url(self, record: InstanceRecord) -> str:
        port = record.dashboard_port or self.dashboard_internal_port
        return f"http://127.0.0.1:{port}/"

    def access_info(self, service, record: InstanceRecord) -> AccessInfo:
        return AccessInfo(
            base_url=self._dashboard_base_url(record),
            readiness_label="dashboard",
            auth_hint="Hermes dashboard (use `clawcu tui <instance>` for chat, API server stays on /health)",
        )

    def display_port(self, service, record: InstanceRecord) -> int:
        return record.dashboard_port or record.port

    def lifecycle_summary(self, service, action: str, record: InstanceRecord) -> str:
        verb = {
            "created": "created",
            "retried": "retried",
            "recreated": "recreated",
            "upgraded": "upgraded",
            "rolled_back": "rolled back",
        }.get(action, action)
        if record.status == "running":
            return (
                f"Instance '{record.name}' {verb}. Hermes {record.version} is ready. "
                f"Dashboard: {self._dashboard_base_url(record)} (API: http://127.0.0.1:{record.port}/health)."
            )
        return f"Instance '{record.name}' {verb} with status '{record.status}' on port {record.port}."

    def configure_instance(self, service, name: str, extra_args: list[str] | None = None) -> None:
        record = service._persist_live_status(service.store.load_record(name))
        command = self.normalize_exec_command(service, record, ["hermes", "setup", *(extra_args or [])])
        env_values = self.exec_env(service, record)
        service.store.append_log(
            f"configure instance name={record.name} args={' '.join(extra_args or [])}".strip()
        )
        service.docker.exec_in_container_interactive(record.container_name, command, env=env_values)

    def exec_env(self, service, record: InstanceRecord) -> dict[str, str]:
        env_values = service._load_env_file(self.env_path(service, record))
        existing_path = env_values.get("PATH", "")
        env_values["PATH"] = f"{HERMES_EXEC_PATH}:{existing_path}" if existing_path else HERMES_EXEC_PATH
        return env_values

    def normalize_exec_command(self, service, record: InstanceRecord, command: list[str]) -> list[str]:
        if command and command[0] == "hermes":
            return [HERMES_EXECUTABLE, *command[1:]]
        return command

    def tui_instance(self, service, name: str, *, agent: str = "main") -> None:
        record = service._persist_live_status(service.store.load_record(name))
        env_values = self.exec_env(service, record)
        service.store.append_log(f"tui instance name={record.name} agent={agent}")
        command = self.normalize_exec_command(service, record, ["hermes", "chat"])
        service.docker.exec_in_container_interactive(record.container_name, command, env=env_values)

    def instance_provider_summary(self, service, record: InstanceRecord) -> dict[str, str]:
        config = self._load_config(record)
        provider = str(config.get("model", {}).get("provider") or "-")
        default_model = str(config.get("model", {}).get("default") or config.get("model", {}).get("model") or "-")
        fallbacks: list[str] = []
        fallback_model = config.get("fallback_model", {})
        if isinstance(fallback_model, dict):
            provider_name = fallback_model.get("provider")
            model_name = fallback_model.get("model")
            if isinstance(provider_name, str) and isinstance(model_name, str) and provider_name.strip() and model_name.strip():
                fallbacks.append(f"{provider_name.strip()}/{model_name.strip()}")
        models = []
        if default_model != "-":
            models.append(default_model)
        models.extend(fallbacks)
        return {
            "providers": provider,
            "models": ", ".join(dict.fromkeys(model for model in models if model and model != "-")) or "-",
        }

    def instance_agent_summaries(self, service, record: InstanceRecord) -> list[dict[str, str]]:
        config = self._load_config(record)
        primary = str(config.get("model", {}).get("default") or config.get("model", {}).get("model") or "-")
        fallbacks = "-"
        fallback_model = config.get("fallback_model", {})
        if isinstance(fallback_model, dict):
            provider_name = fallback_model.get("provider")
            model_name = fallback_model.get("model")
            if isinstance(provider_name, str) and isinstance(model_name, str) and provider_name.strip() and model_name.strip():
                fallbacks = f"{provider_name.strip()}/{model_name.strip()}"
        summary = self.instance_provider_summary(service, record)
        return [
            {
                "agent": "main",
                "primary": primary,
                "fallbacks": fallbacks,
                **summary,
            }
        ]

    def local_instance_summaries(self, service) -> list[dict]:
        root = service._local_hermes_home()
        if not (root / "config.yaml").exists():
            return []
        version = self._local_version(service)
        record = build_instance_record(
            InstanceSpec(
                service=self.service_name,
                name="local-hermes",
                version=version,
                datadir=str(root),
                port=self.default_port,
                cpu="1",
                memory="1g",
                auth_mode="native",
            ),
            status="local",
            history=[],
        )
        summary = self.instance_provider_summary(service, record)
        return [
            {
                "source": "local",
                "name": "local-hermes",
                "home": str(root),
                "version": version,
                "port": self.dashboard_internal_port,
                "status": "local",
                "access_url": f"http://127.0.0.1:{self.dashboard_internal_port}/",
                "providers": summary["providers"],
                "models": summary["models"],
                "service": self.service_name,
            }
        ]

    def removed_instance_summary(self, service, root: Path) -> dict | None:
        config_path = root / "config.yaml"
        if not config_path.exists():
            return None
        metadata = service._load_instance_metadata(root)
        record = build_instance_record(
            InstanceSpec(
                service=self.service_name,
                name=root.name,
                version=str(metadata.get("version") or "-"),
                datadir=str(root),
                port=self.default_port,
                cpu="1",
                memory="1g",
                auth_mode="native",
            ),
            status="removed",
            history=[],
        )
        summary = self.instance_provider_summary(service, record)
        port_value = service._coerce_metadata_port(metadata.get("port"))
        return {
            "source": "removed",
            "name": root.name,
            "home": str(root),
            "version": record.version,
            "port": port_value if port_value is not None else "-",
            "status": "removed",
            "access_url": "-",
            "providers": summary["providers"],
            "models": summary["models"],
            "service": self.service_name,
            "snapshot": "-",
        }

    def removed_instance_spec(
        self,
        service,
        root: Path,
        *,
        version: str | None = None,
    ) -> InstanceSpec | None:
        config_path = root / "config.yaml"
        if not config_path.exists():
            return None
        metadata = service._load_instance_metadata(root)
        resolved_version = str(version or metadata.get("version") or "").strip()
        if not resolved_version:
            raise ValueError(
                f"Removed Hermes instance '{root.name}' does not record its version. "
                f"Re-run `clawcu recreate {root.name} --version <version>` to restore it."
            )
        resolved_port = service._coerce_metadata_port(metadata.get("port")) or service._next_available_port(
            self.default_port
        )
        resolved_dashboard_port = service._coerce_metadata_port(
            metadata.get("dashboard_port")
        ) or service._next_available_port(self.dashboard_default_port)
        if resolved_dashboard_port == resolved_port:
            resolved_dashboard_port = service._next_available_port(
                resolved_dashboard_port + service.PORT_SEARCH_STEP
            )
        return InstanceSpec(
            service=self.service_name,
            name=root.name,
            version=resolved_version,
            datadir=str(root),
            port=resolved_port,
            cpu=str(metadata.get("cpu") or "1"),
            memory=str(metadata.get("memory") or "2g"),
            auth_mode=str(metadata.get("auth_mode") or self.default_auth_mode()),
            dashboard_port=resolved_dashboard_port,
            image_tag_override=(
                str(metadata.get("image_tag") or "").strip()
                if version is None and str(metadata.get("image_tag") or "").strip()
                else None
            ),
        )

    def local_agent_summaries(self, service) -> list[dict]:
        root = service._local_hermes_home()
        if not (root / "config.yaml").exists():
            return []
        version = self._local_version(service)
        record = build_instance_record(
            InstanceSpec(
                service=self.service_name,
                name="local-hermes",
                version=version,
                datadir=str(root),
                port=self.internal_port,
                cpu="1",
                memory="1g",
                auth_mode="native",
            ),
            status="local",
            history=[],
        )
        summaries = self.instance_agent_summaries(service, record)
        return [
            {
                "source": "local",
                "instance": "local-hermes",
                "home": str(root),
                "service": self.service_name,
                "version": version,
                "port": self.internal_port,
                "status": "local",
                **summary,
            }
            for summary in summaries
        ]

    def scan_model_config_bundles(self, service, root: Path, env_values: dict[str, str] | None = None) -> list[dict[str, object]]:
        root = root.expanduser().resolve()
        config_path = root / "config.yaml"
        if not config_path.exists():
            raise FileNotFoundError(f"Hermes home '{root}' does not contain config.yaml.")
        config_text = config_path.read_text(encoding="utf-8")
        config = yaml.safe_load(config_text) or {}
        if not isinstance(config, dict):
            raise ValueError(f"Expected a YAML mapping in '{config_path}'.")
        model_cfg = config.get("model", {})
        if not isinstance(model_cfg, dict):
            raise FileNotFoundError(f"Hermes home '{root}' does not contain model configuration.")
        provider = str(model_cfg.get("provider") or "custom").strip() or "custom"
        env_values = env_values or service._load_env_file(root / ".env")
        relevant_env = {key: value for key, value in env_values.items() if key in HERMES_MODEL_ENV_ALLOWLIST}
        relevant_config = {}
        for key in ("model", "fallback_model", "smart_model_routing", "custom_providers"):
            if key in config:
                relevant_config[key] = config[key]
        bundle: dict[str, object] = {
            "service": self.service_name,
            "name": provider,
            "metadata": {
                "service": self.service_name,
                "kind": "hermes-model-config",
                "provider": provider,
                "api_style": "openai",
                "endpoint": env_values.get("OPENAI_BASE_URL")
                or env_values.get(f"{provider.upper().replace('-', '_')}_BASE_URL"),
            },
            "config_yaml": yaml.safe_dump(relevant_config, sort_keys=False),
            "env": service._dump_env_file(relevant_env),
        }
        # Hermes Codex-style providers require an `auth.json` alongside the
        # config/env (OAuth tokens refreshed by `hermes auth`). Carry it in the
        # bundle so `provider apply` can reproduce the credentials on a fresh
        # instance instead of forcing a re-auth.
        auth_json_path = root / "auth.json"
        if auth_json_path.exists():
            bundle["auth_json"] = auth_json_path.read_text(encoding="utf-8")
        return [bundle]

    def apply_provider(self, service, bundle: dict[str, object], instance: str, *, agent: str = "main", primary: str | None = None, fallbacks: list[str] | None = None, persist: bool = False) -> dict[str, str]:
        record = service.store.load_record(instance)
        if record.service != self.service_name:
            raise ValueError(f"Provider bundle '{bundle.get('name')}' cannot be applied to {record.service} instance '{record.name}'.")
        config_path = Path(record.datadir) / "config.yaml"
        target_config = self._load_config(record)
        incoming_config = yaml.safe_load(str(bundle.get("config_yaml") or "")) or {}
        if isinstance(incoming_config, dict):
            for key in ("model", "fallback_model", "smart_model_routing", "custom_providers"):
                if key in incoming_config:
                    target_config[key] = incoming_config[key]
        model_cfg = target_config.setdefault("model", {})
        if not isinstance(model_cfg, dict):
            model_cfg = {}
            target_config["model"] = model_cfg
        if primary:
            if "/" in primary:
                provider_name, model_name = primary.split("/", 1)
                model_cfg["provider"] = provider_name
                model_cfg["default"] = model_name
            else:
                model_cfg["default"] = primary
        if fallbacks:
            first = fallbacks[0]
            if "/" in first:
                provider_name, model_name = first.split("/", 1)
                target_config["fallback_model"] = {"provider": provider_name, "model": model_name}
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(yaml.safe_dump(target_config, sort_keys=False), encoding="utf-8")

        env_path = self.env_path(service, record)
        target_env = service._load_env_file(env_path)
        incoming_env = service._load_env_text(str(bundle.get("env") or ""))
        target_env.update(incoming_env)
        env_path.write_text(service._dump_env_file(target_env), encoding="utf-8")

        # Restore Codex auth.json when present in the bundle — without this the
        # target instance's hermes gateway would 500 with "No Codex credentials
        # stored" until the operator runs `hermes auth` inside it.
        incoming_auth_json = bundle.get("auth_json")
        if isinstance(incoming_auth_json, str) and incoming_auth_json:
            auth_json_path = Path(record.datadir) / "auth.json"
            auth_json_path.parent.mkdir(parents=True, exist_ok=True)
            auth_json_path.write_text(incoming_auth_json, encoding="utf-8")
        service.store.append_log(
            "provider apply "
            f"provider={bundle['name']} instance={record.name} agent=main "
            f"primary={primary or '-'} fallbacks={','.join(fallbacks or []) or '-'} "
            f"config_path={config_path}"
        )
        return {
            "provider": str(bundle["name"]),
            "service": self.service_name,
            "instance": record.name,
            "agent": "main",
            "config_path": str(config_path),
            "env_path": str(env_path),
            "persist": "yes" if persist else "yes",
            "primary": primary or "-",
            "fallbacks": ", ".join(fallbacks) if fallbacks else "-",
        }

    def provider_models(self, service, bundle: dict[str, object]) -> list[str]:
        config = yaml.safe_load(str(bundle.get("config_yaml") or "")) or {}
        models: list[str] = []
        if isinstance(config, dict):
            model_cfg = config.get("model", {})
            if isinstance(model_cfg, dict):
                provider = model_cfg.get("provider")
                default_model = model_cfg.get("default") or model_cfg.get("model")
                if isinstance(provider, str) and isinstance(default_model, str):
                    models.append(f"{provider}/{default_model}")
            fallback_cfg = config.get("fallback_model", {})
            if isinstance(fallback_cfg, dict):
                provider = fallback_cfg.get("provider")
                model_name = fallback_cfg.get("model")
                if isinstance(provider, str) and isinstance(model_name, str):
                    models.append(f"{provider}/{model_name}")
        return models

    def _load_config(self, record: InstanceRecord) -> dict:
        config_path = Path(record.datadir) / "config.yaml"
        if not config_path.exists():
            return {}
        data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        return data if isinstance(data, dict) else {}

    def _local_version(self, service) -> str:
        try:
            result = service.runner(["hermes", "version"])
        except Exception:
            return "-"
        stdout = str(getattr(result, "stdout", "") or "").strip()
        if not stdout:
            return "-"
        first_line = stdout.splitlines()[0].strip()
        match = re.search(r"Hermes Agent\s+(.+)$", first_line)
        if match:
            return match.group(1).strip()
        return first_line or "-"
