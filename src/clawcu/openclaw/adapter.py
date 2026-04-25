from __future__ import annotations

import copy
import json
import re
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from clawcu import __version__ as clawcu_version
from clawcu.a2a.sidecar_plugin import resolve_advertise_host
from clawcu.core.adapters import ServiceAdapter
from clawcu.core.models import AccessInfo, ContainerRunSpec, InstanceRecord, InstanceSpec
from clawcu.core.validation import (
    build_instance_record,
    normalize_version,
    resolve_datadir,
    updated_record,
    utc_now_iso,
    validate_cpu,
    validate_memory,
    validate_name,
    validate_port,
)
from clawcu.openclaw.manager import OpenClawManager


class OpenClawAdapter(ServiceAdapter):
    service_name = "openclaw"
    display_name = "OpenClaw"
    default_port = 18799
    internal_port = 18789

    # Review-1 §3: A2A protocol defaults. Gateway occupies display_port,
    # sidecar binds display_port + 1 — so federation probes try the base
    # first (in case someone co-locates a custom plugin on the gateway
    # port) then the sidecar slot.
    a2a_skills = ("chat", "tools")
    a2a_role = "OpenClaw local assistant"
    a2a_plugin_port_offsets = (0, 1)

    def __init__(self, manager: OpenClawManager):
        self.manager = manager

    def prepare_artifact(self, version: str) -> str:
        return self.manager.ensure_image(version)

    def default_datadir(self, service, name: str) -> str:
        return str((service.store.paths.home / name).resolve())

    def default_auth_mode(self) -> str:
        return "token"

    def build_spec(self, service, *, name: str, version: str, datadir: str | None, port: int | None, cpu: str, memory: str) -> InstanceSpec:
        validated_name = validate_name(name)
        resolved_datadir = resolve_datadir(datadir) if datadir else self.default_datadir(service, validated_name)
        resolved_port = validate_port(port) if port is not None else service._next_available_port(self.default_port)
        return InstanceSpec(
            service=self.service_name,
            name=validated_name,
            version=normalize_version(version),
            datadir=resolved_datadir,
            port=resolved_port,
            cpu=validate_cpu(cpu),
            memory=validate_memory(memory),
            auth_mode="token",
        )

    def env_path(self, service, record: InstanceRecord | str) -> Path:
        name = record if isinstance(record, str) else record.name
        return service.store.instance_env_path(name)

    # When an instance is baked with the A2A plugin, the sidecar binds
    # this internal port; the host publishes ``record.port + 1`` so the
    # federation probe in clawcu.a2a.card finds the card at the expected
    # neighbor port (``plugin_port_candidates`` = (0, 1)).
    a2a_internal_port = 18790

    def run_spec(self, service, record: InstanceRecord) -> ContainerRunSpec:
        env_path = self.env_path(service, record)
        env_values = service._load_env_file(env_path)
        additional_ports: list[tuple[int, int]] = []
        extra_hosts: list[tuple[str, str]] = []
        extra_env: dict[str, str] = {}
        # The baked image's entrypoint-a2a.sh supervises BOTH the stock
        # gateway and the sidecar; we don't override CMD here because the
        # image's CMD already launches the gateway with the same flags we
        # pass in the non-a2a case.
        command: list[str] | None = [
            "node",
            "openclaw.mjs",
            "gateway",
            "--allow-unconfigured",
            "--bind",
            "lan",
            "--port",
            str(self.internal_port),
        ]
        if record.a2a_enabled:
            a2a_host_port = record.port + 1
            additional_ports.append((a2a_host_port, self.a2a_internal_port))
            # Review-1 P0-B: Linux hosts don't resolve host.docker.internal
            # without an explicit --add-host. Docker Desktop resolves it
            # automatically, so this is a no-op on macOS/Windows. Required
            # for /a2a/outbound to reach the host-side registry.
            extra_hosts.append(("host.docker.internal", "host-gateway"))
            extra_env.update(
                {
                    "A2A_SIDECAR_NAME": record.name,
                    "A2A_SIDECAR_PORT": str(self.a2a_internal_port),
                    # Review-9 P1-A3: peers in other containers can't reach
                    # 127.0.0.1 — the baked default used to break cross-
                    # container A2A calls on Docker Desktop. Resolver returns
                    # host.docker.internal on Darwin/Windows and honors the
                    # per-record override when set via --a2a-advertise-host.
                    "A2A_SIDECAR_ADVERTISE_HOST": resolve_advertise_host(record),
                    "A2A_SIDECAR_ADVERTISE_PORT": str(a2a_host_port),
                    # Sidecar forwards /a2a/send to the gateway's own
                    # OpenAI-compat endpoint so the agent's native
                    # persona + skills drive the reply. This env tells
                    # the sidecar which port the gateway is listening
                    # on inside the container.
                    "A2A_GATEWAY_PORT": str(self.internal_port),
                    # OpenClaw gateway exposes readiness at /healthz. The
                    # sidecar is now gateway-agnostic (review-7 P2-E) —
                    # each adapter declares its own readiness path so the
                    # sidecar doesn't need to know the service layout.
                    "A2A_GATEWAY_READY_PATH": "/healthz",
                    # Review-10 P2-C: tee sidecar logs into the datadir
                    # mount so they survive `clawcu recreate`. Host sees
                    # them at <record.datadir>/logs/a2a-sidecar.log.
                    "A2A_SIDECAR_LOG_DIR": "/home/node/.openclaw/logs",
                    # Review-13 P1-C: per-peer / per-thread conversation
                    # history. When the peer sends thread_id, the sidecar
                    # reads/appends <dir>/<peer>/<thread_id>.jsonl so the
                    # agent sees continuous context. Lives under the
                    # datadir mount so threads survive `clawcu recreate`.
                    "A2A_THREAD_DIR": "/home/node/.openclaw/threads",
                }
            )
            # Review-1 P0-B: auto-inject a registry URL default so
            # /a2a/outbound works out of the box on Linux (host.docker.internal
            # only resolves thanks to the extra_hosts we added above). User
            # overrides in the env file win — we only set it when absent.
            if not env_values.get("A2A_REGISTRY_URL"):
                extra_env["A2A_REGISTRY_URL"] = "http://host.docker.internal:9100"
            # a2a-design-4.md §P0-A: auto-wiring bootstrap. The sidecar reads
            # these on start and merges `mcp.servers.a2a` into the OpenClaw
            # config file so the LLM sees `a2a_call_peer` as an MCP tool
            # without any IDENTITY.md edit. User env-file entries win.
            if not env_values.get("A2A_ENABLED"):
                extra_env["A2A_ENABLED"] = "true"
            if not env_values.get("A2A_SERVICE_MCP_CONFIG_PATH"):
                extra_env["A2A_SERVICE_MCP_CONFIG_PATH"] = "/home/node/.openclaw/openclaw.json"
            if not env_values.get("A2A_SERVICE_MCP_CONFIG_FORMAT"):
                extra_env["A2A_SERVICE_MCP_CONFIG_FORMAT"] = "json"
        return ContainerRunSpec(
            internal_port=self.internal_port,
            mount_target="/home/node/.openclaw",
            env_file=str(env_path) if env_path.exists() else None,
            extra_env=extra_env,
            command=command,
            additional_ports=additional_ports,
            extra_hosts=extra_hosts,
        )

    def configure_before_run(self, service, record: InstanceRecord) -> None:
        config_path = Path(record.datadir) / "openclaw.json"
        workspace_dir = Path(record.datadir) / "workspace"
        workspace_dir.mkdir(parents=True, exist_ok=True)
        (workspace_dir / "memory").mkdir(parents=True, exist_ok=True)
        workspace_state_path = workspace_dir / ".openclaw" / "workspace-state.json"
        workspace_state_path.parent.mkdir(parents=True, exist_ok=True)
        if not workspace_state_path.exists():
            workspace_state_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "onboardingCompletedAt": utc_now_iso(),
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
        try:
            config: dict = {}
            if config_path.exists():
                raw_config = config_path.read_text(encoding="utf-8").strip()
                if raw_config:
                    config = json.loads(raw_config)
                else:
                    service.reporter(
                        f"Gateway config at {config_path} was empty. Rebuilding a minimal config."
                    )
            gw = config.setdefault("gateway", {})
            gw["mode"] = "local"
            gw["bind"] = "lan"
            gw["port"] = self.internal_port
            control_ui = gw.setdefault("controlUi", {})
            control_ui["allowedOrigins"] = ["*"]
            control_ui["dangerouslyAllowHostHeaderOriginFallback"] = True
            auth = gw.setdefault("auth", {})
            auth["mode"] = record.auth_mode
            if record.auth_mode == "token" and not auth.get("token"):
                auth["token"] = secrets.token_hex(24)
            config.setdefault("agents", {}).setdefault("defaults", {})["workspace"] = (
                "/home/node/.openclaw/workspace"
            )
            # A2A sidecar forwards /a2a/send through the gateway's native
            # OpenAI-compat endpoint so it hits the real agent runtime (full
            # persona + skills + tool loop) instead of bypassing straight to
            # the LLM. That endpoint is gated by config and off by default.
            if getattr(record, "a2a_enabled", False):
                http_cfg = gw.setdefault("http", {})
                endpoints = http_cfg.setdefault("endpoints", {})
                chat_completions = endpoints.setdefault("chatCompletions", {})
                chat_completions["enabled"] = True
            config_path.write_text(
                json.dumps(config, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            service._make_runtime_tree_writable(Path(record.datadir))
            service.reporter(
                f"Gateway configured: mode=local, bind=lan, port={self.internal_port}, auth.mode={record.auth_mode}, controlUi.allowedOrigins=[*]."
            )
        except Exception as exc:
            service.reporter(f"Could not auto-configure gateway: {exc}")

    def wait_for_readiness(self, service, record: InstanceRecord) -> InstanceRecord:
        pending_statuses = {"starting", "created"}
        if record.status == "running":
            return record
        service.reporter(
            f"Waiting for OpenClaw to become ready on port {record.port}. "
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
            healthcheck = getattr(service, "_host_healthcheck_ready", None)
            ready = healthcheck(current) if callable(healthcheck) else self._host_healthcheck_ready(current)
            if ready:
                ready = updated_record(current, status="running", last_error=None)
                service.store.save_record(ready)
                service.reporter(
                    f"OpenClaw health endpoint is responding on http://127.0.0.1:{ready.port}/healthz. Marking the instance as ready."
                )
                return ready
            if current.status == "running":
                return current
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
                    f"OpenClaw is still {current.status} on port {current.port} after {elapsed}s. Continuing to wait for readiness."
                )
                last_reported_status = current.status
                last_reported_bucket = progress_bucket

            time.sleep(service.STARTUP_POLL_INTERVAL_SECONDS)

    def _host_healthcheck_ready(self, record: InstanceRecord) -> bool:
        url = f"http://127.0.0.1:{record.port}/healthz"
        request = urllib.request.Request(url, headers={"User-Agent": f"clawcu/{clawcu_version}"})
        try:
            with urllib.request.urlopen(request, timeout=2) as response:
                return 200 <= response.status < 400
        except (urllib.error.URLError, TimeoutError, ValueError, OSError):
            return False

    def access_info(self, service, record: InstanceRecord) -> AccessInfo:
        base_url = f"http://127.0.0.1:{record.port}/"
        token = self._gateway_token(record)
        if token:
            base_url = f"{base_url}#token={urllib.parse.quote(token, safe='')}"
        return AccessInfo(
            base_url=base_url,
            readiness_label="healthz",
            auth_hint="dashboard token" if token else "native OpenClaw auth",
            token=token,
        )

    def lifecycle_summary(self, service, action: str, record: InstanceRecord) -> str:
        verb = {
            "created": "created",
            "retried": "retried",
            "recreated": "recreated",
            "upgraded": "upgraded",
            "rolled_back": "rolled back",
        }.get(action, action)
        if record.status == "running":
            return f"Instance '{record.name}' {verb}. OpenClaw {record.version} is ready on port {record.port}."
        if record.status == "starting":
            return (
                f"Instance '{record.name}' {verb}, but OpenClaw is still starting on port {record.port}. "
                "Wait for the health check to turn healthy before visiting the UI."
            )
        if record.status == "unhealthy":
            return (
                f"Instance '{record.name}' {verb}, but the container is unhealthy on port {record.port}. "
                "Check 'clawcu inspect' or 'clawcu logs' before trying the UI."
            )
        return f"Instance '{record.name}' {verb} with status '{record.status}' on port {record.port}."

    def configure_instance(self, service, name: str, extra_args: list[str] | None = None) -> None:
        record = service._persist_live_status(service.store.load_record(name))
        command = ["node", "openclaw.mjs", "configure", *(extra_args or [])]
        env_values = self.exec_env(service, record)
        service.store.append_log(
            f"configure instance name={record.name} args={' '.join(extra_args or [])}".strip()
        )
        service.docker.exec_in_container_interactive(record.container_name, command, env=env_values)

    def exec_env(self, service, record: InstanceRecord) -> dict[str, str]:
        return service._load_env_file(self.env_path(service, record))

    def container_env_matches(self, service, record: InstanceRecord, inspection: dict | None) -> bool:
        desired_env = self.exec_env(service, record)
        if not desired_env:
            return True
        config = inspection.get("Config", {}) if isinstance(inspection, dict) else {}
        existing_env = config.get("Env")
        if not isinstance(existing_env, list):
            return True
        existing_keys = {
            entry.split("=", 1)[0]
            for entry in existing_env
            if isinstance(entry, str) and "=" in entry
        }
        return all(key in existing_keys for key in desired_env)

    def tui_instance(self, service, name: str, *, agent: str = "main") -> None:
        agent_name = (agent or "main").strip() or "main"
        record = service._persist_live_status(service.store.load_record(name))
        pending_request_id = self._latest_pending_request_id(record)
        if pending_request_id:
            self.approve_pairing(service, name, request_id=pending_request_id)
        else:
            service.reporter(
                f"No pending pairing request was found for instance '{record.name}'. Launching TUI directly."
            )
        command = ["openclaw", "tui"]
        if agent_name != "main":
            command.extend(["--agent", agent_name])
        service.exec_instance(name, command)

    def token(self, service, name: str) -> str:
        record = service._persist_live_status(service.store.load_record(name))
        token = self._gateway_token(record)
        if token:
            return token
        raise ValueError(f"Instance '{record.name}' does not have a dashboard token configured.")

    def list_pending_pairings(self, service, name: str) -> list[dict[str, object]]:
        record = service._persist_live_status(service.store.load_record(name))
        pending_path = Path(record.datadir) / "devices" / "pending.json"
        if not pending_path.exists():
            return []
        try:
            raw = pending_path.read_text(encoding="utf-8").strip()
            if not raw:
                return []
            pending = json.loads(raw)
        except Exception:
            return []
        if not isinstance(pending, dict):
            return []
        entries: list[dict[str, object]] = []
        for value in pending.values():
            if isinstance(value, dict) and value.get("requestId"):
                entries.append(dict(value))
        # Newest first so interactive selection shows fresh requests.
        entries.sort(key=lambda item: item.get("ts", 0), reverse=True)
        return entries

    def approve_pairing(self, service, name: str, request_id: str | None = None) -> str:
        record = service._persist_live_status(service.store.load_record(name))
        selected_request_id = request_id or self._latest_pending_request_id(record)
        if not selected_request_id:
            raise ValueError(
                f"Instance '{record.name}' has no pending pairing requests."
            )
        env_values = self.exec_env(service, record)
        service.reporter(
            f"Approving pairing request {selected_request_id} for instance '{record.name}'."
        )
        service.docker.exec_in_container(
            record.container_name,
            ["node", "openclaw.mjs", "devices", "approve", selected_request_id],
            env=env_values,
        )
        service.store.append_log(
            f"approve pairing instance={record.name} request_id={selected_request_id}"
        )
        return selected_request_id

    def _gateway_token(self, record: InstanceRecord) -> str | None:
        config_path = Path(record.datadir) / "openclaw.json"
        if not config_path.exists():
            return None
        try:
            raw_config = config_path.read_text(encoding="utf-8").strip()
            if not raw_config:
                return None
            config = json.loads(raw_config)
        except Exception:
            return None
        token = config.get("gateway", {}).get("auth", {}).get("token")
        if isinstance(token, str) and token.strip():
            return token.strip()
        return None

    def _latest_pending_request_id(self, record: InstanceRecord) -> str | None:
        pending_path = Path(record.datadir) / "devices" / "pending.json"
        if not pending_path.exists():
            return None
        try:
            raw_pending = pending_path.read_text(encoding="utf-8").strip()
            if not raw_pending:
                return None
            pending = json.loads(raw_pending)
        except Exception:
            return None
        if not isinstance(pending, dict) or not pending:
            return None
        latest = max(
            pending.values(),
            key=lambda item: item.get("ts", 0) if isinstance(item, dict) else 0,
        )
        if not isinstance(latest, dict):
            return None
        request_id = latest.get("requestId")
        if isinstance(request_id, str) and request_id.strip():
            return request_id.strip()
        return None

    def instance_provider_summary(self, service, record: InstanceRecord) -> dict[str, str]:
        config_path = Path(record.datadir) / "openclaw.json"
        config = service._load_json_file(config_path)
        summary = service._config_provider_summary(config)
        if summary["providers"] != "-" or summary["models"] != "-":
            return summary

        provider_names: list[str] = []
        model_names: list[str] = []
        for agent_name in service._managed_agent_names(Path(record.datadir)):
            agent_summary = service._agent_runtime_provider_summary(Path(record.datadir), agent_name)
            provider_names.extend(service._split_summary_values(agent_summary["providers"]))
            model_names.extend(service._split_summary_values(agent_summary["models"]))
        return service._summary_from_lists(provider_names, model_names)

    def instance_agent_summaries(self, service, record: InstanceRecord) -> list[dict[str, str]]:
        config_path = Path(record.datadir) / "openclaw.json"
        config = service._load_json_file(config_path)
        root_provider_summary = service._config_provider_summary(config)
        configured = service._configured_agent_models(config)
        if configured:
            summaries: list[dict[str, str]] = []
            for agent_summary in configured:
                runtime_summary = service._agent_runtime_provider_summary(Path(record.datadir), agent_summary["agent"])
                effective_summary = runtime_summary
                if runtime_summary["providers"] == "-" and runtime_summary["models"] == "-":
                    effective_summary = root_provider_summary
                summaries.append({**effective_summary, **agent_summary})
            return summaries

        agent_names = service._managed_agent_names(Path(record.datadir))
        if not agent_names:
            return []

        default_primary, default_fallbacks = service._configured_default_agent_model(config)
        return [
            {
                "agent": agent_name,
                **service._agent_runtime_provider_summary(Path(record.datadir), agent_name),
                "primary": default_primary,
                "fallbacks": default_fallbacks,
            }
            for agent_name in agent_names
        ]

    def local_instance_summaries(self, service) -> list[dict]:
        config_path = service._local_openclaw_home() / "openclaw.json"
        if not config_path.exists():
            return []
        config = service._load_json_file(config_path)
        provider_summary = service._config_provider_summary(config)
        version = service._config_version(config)
        return [
            {
                "source": "local",
                "name": "local-openclaw",
                "home": str(service._local_openclaw_home()),
                "version": version,
                "port": self.internal_port,
                "status": "local",
                "providers": provider_summary["providers"],
                "models": provider_summary["models"],
                "service": self.service_name,
            }
        ]

    def removed_instance_summary(self, service, root: Path) -> dict | None:
        config_path = root / "openclaw.json"
        if not config_path.exists():
            return None
        config = service._load_json_file(config_path)
        metadata = service._load_instance_metadata(root)
        provider_summary = service._config_provider_summary(config)
        version = str(metadata.get("version") or service._config_version(config) or "-")
        port_value = service._coerce_metadata_port(metadata.get("port"))
        return {
            "source": "removed",
            "name": root.name,
            "home": str(root),
            "version": version,
            "port": port_value if port_value is not None else "-",
            "status": "removed",
            "access_url": "-",
            "providers": provider_summary["providers"],
            "models": provider_summary["models"],
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
        config_path = root / "openclaw.json"
        if not config_path.exists():
            return None
        metadata = service._load_instance_metadata(root)
        resolved_version = str(
            version
            or metadata.get("version")
            or service._config_version(service._load_json_file(config_path))
            or ""
        ).strip()
        if not resolved_version or resolved_version == "-":
            raise ValueError(
                f"Removed OpenClaw instance '{root.name}' does not record a recoverable version. "
                f"Re-run `clawcu recreate {root.name} --version <version>` to restore it."
            )
        resolved_port = service._coerce_metadata_port(metadata.get("port")) or service._next_available_port(
            self.default_port
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
            dashboard_port=None,
            image_tag_override=(
                str(metadata.get("image_tag") or "").strip()
                if version is None and str(metadata.get("image_tag") or "").strip()
                else None
            ),
        )

    def local_agent_summaries(self, service) -> list[dict]:
        config_path = service._local_openclaw_home() / "openclaw.json"
        if not config_path.exists():
            return []
        config = service._load_json_file(config_path)
        provider_summary = service._config_provider_summary(config)
        version = service._config_version(config)
        summaries: list[dict] = []
        for agent_summary in service._configured_agent_models(config):
            summaries.append(
                {
                    "source": "local",
                    "instance": "local-openclaw",
                    "home": str(service._local_openclaw_home()),
                    "service": self.service_name,
                    "version": version,
                    "port": self.internal_port,
                    "status": "local",
                    "providers": provider_summary["providers"],
                    "models": provider_summary["models"],
                    **agent_summary,
                }
            )
        return summaries

    def scan_model_config_bundles(self, service, root: Path, env_values: dict[str, str] | None = None) -> list[dict[str, object]]:
        root = root.expanduser().resolve()
        env_values = env_values or {}
        bundles: list[dict[str, object]] = []
        root_config_path = root / "openclaw.json"
        root_config = service._load_json_file(root_config_path)
        root_models = root_config.get("models", {})
        root_providers = root_models.get("providers", {}) if isinstance(root_models, dict) else {}
        root_auth = root_config.get("auth", {})
        root_auth_profiles = root_auth.get("profiles", {}) if isinstance(root_auth, dict) else {}
        if not isinstance(root_providers, dict) or not root_providers:
            return []

        for provider_name, provider_payload in root_providers.items():
            if not isinstance(provider_name, str) or not isinstance(provider_payload, dict):
                continue
            resolved_provider_payload = service._resolve_env_placeholders(provider_payload, env_values)
            if not isinstance(resolved_provider_payload, dict):
                continue
            resolved_root_auth_payload = service._resolve_env_placeholders(
                {"profiles": copy.deepcopy(root_auth_profiles)}
                if isinstance(root_auth_profiles, dict)
                else {},
                env_values,
            )
            bundles.append(
                {
                    "service": self.service_name,
                    "name": provider_name,
                    "metadata": {
                        "service": self.service_name,
                        "kind": "openclaw-provider",
                        "provider": provider_name,
                        "api_style": service._infer_api_style(resolved_provider_payload),
                        "endpoint": resolved_provider_payload.get("baseUrl"),
                    },
                    "auth_profiles": service._build_auth_bundle_for_provider(
                        resolved_root_auth_payload if isinstance(resolved_root_auth_payload, dict) else {},
                        provider_name,
                        resolved_provider_payload,
                    ),
                    "models": {
                        "providers": {
                            provider_name: copy.deepcopy(resolved_provider_payload),
                        }
                    },
                }
            )
        return bundles

    def apply_provider(self, service, bundle: dict[str, object], instance: str, *, agent: str = "main", primary: str | None = None, fallbacks: list[str] | None = None, persist: bool = False) -> dict[str, str]:
        record = service.store.load_record(instance)
        if record.service != self.service_name:
            raise ValueError(f"Provider bundle '{bundle.get('name')}' cannot be applied to {record.service} instance '{record.name}'.")
        agent_name = agent.strip() or "main"
        env_key = service._store_provider_api_key_in_instance_env(record.name, str(bundle["name"]), bundle) if persist else None
        runtime_dir = Path(record.datadir) / "agents" / agent_name / "agent"
        runtime_dir.mkdir(parents=True, exist_ok=True)
        auth_path = runtime_dir / "auth-profiles.json"
        models_path = runtime_dir / "models.json"
        merged_auth = service._merge_auth_payloads(service._load_json_file(auth_path), dict(bundle.get("auth_profiles", {})))
        merged_models = service._merge_models_payloads(service._load_json_file(models_path), dict(bundle.get("models", {})))
        config_path = Path(record.datadir) / "openclaw.json"
        config = service._load_json_file(config_path)
        config = service._upsert_root_provider_models_config(config, dict(bundle.get("models", {})), env_key=env_key)
        config = service._upsert_agent_model_config(config, agent_name=agent_name, primary=primary, fallbacks=fallbacks)
        config_path.write_text(
            json.dumps(config, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        auth_path.write_text(
            json.dumps(merged_auth, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        models_path.write_text(
            json.dumps(merged_models, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        service.store.append_log(
            "provider apply "
            f"provider={bundle['name']} instance={record.name} agent={agent_name} "
            f"primary={primary or '-'} fallbacks={','.join(fallbacks or []) or '-'} "
            f"runtime_dir={runtime_dir}"
        )
        return {
            "provider": str(bundle["name"]),
            "service": self.service_name,
            "instance": record.name,
            "agent": agent_name,
            "runtime_dir": str(runtime_dir),
            "env_key": env_key or "-",
            "persist": "yes" if persist else "no",
            "primary": primary or "-",
            "fallbacks": ", ".join(fallbacks) if fallbacks else "-",
        }

    def bundle_to_canonical(self, service, bundle):
        from clawcu.core.provider_models import (
            CanonicalModel,
            CanonicalProvider,
            MissingCredentialError,
        )

        models_payload = bundle.get("models", {}) or {}
        if not isinstance(models_payload, dict):
            models_payload = {}
        provider_name, payload = service._single_provider_entry(models_payload)

        # api_key: prefer providers.<name>.apiKey, fall back to auth_profiles.
        api_key: str | None = None
        raw_payload_key = payload.get("apiKey") if isinstance(payload, dict) else None
        if isinstance(raw_payload_key, str) and raw_payload_key.strip() and not raw_payload_key.startswith("$"):
            api_key = raw_payload_key.strip()
        if not api_key:
            auth_payload = bundle.get("auth_profiles", {}) or {}
            profiles = auth_payload.get("profiles", {}) if isinstance(auth_payload, dict) else {}
            if isinstance(profiles, dict):
                for profile in profiles.values():
                    if not isinstance(profile, dict):
                        continue
                    for key_name in ("key", "apiKey"):
                        candidate = profile.get(key_name)
                        if isinstance(candidate, str) and candidate.strip():
                            api_key = candidate.strip()
                            break
                    if api_key:
                        break

        if not api_key:
            raise MissingCredentialError(
                f"OpenClaw bundle for provider {provider_name!r} has no usable api_key."
            )

        api_style = (payload.get("api") if isinstance(payload, dict) else None) or "openai"
        base_url = (payload.get("baseUrl") if isinstance(payload, dict) else None) or None
        headers = payload.get("headers") if isinstance(payload, dict) else None
        if headers is not None and not isinstance(headers, dict):
            headers = None

        models_list = payload.get("models", []) if isinstance(payload, dict) else []
        canonical_models: list[CanonicalModel] = []
        for m in models_list if isinstance(models_list, list) else []:
            if not isinstance(m, dict) or not m.get("id"):
                continue
            canonical_models.append(CanonicalModel(
                id=str(m["id"]),
                name=str(m.get("name")) if m.get("name") is not None else None,
                context_window=int(m["contextWindow"]) if isinstance(m.get("contextWindow"), int) else None,
                max_tokens=int(m["maxTokens"]) if isinstance(m.get("maxTokens"), int) else None,
                inputs=tuple(str(x) for x in m["input"]) if isinstance(m.get("input"), list) else (),
                reasoning=bool(m["reasoning"]) if isinstance(m.get("reasoning"), bool) else None,
                cost=dict(m["cost"]) if isinstance(m.get("cost"), dict) else None,
            ))

        default_model_id = canonical_models[0].id if canonical_models else None

        extras: dict = {}
        auth_payload = bundle.get("auth_profiles", {})
        last_good = auth_payload.get("lastGood") if isinstance(auth_payload, dict) else None
        if isinstance(last_good, dict):
            extras["openclaw_lastGood"] = dict(last_good)

        return CanonicalProvider(
            name=provider_name,
            api_style=str(api_style),
            base_url=base_url,
            auth_type="api_key",
            api_key=api_key,
            api_key_env_var=None,
            models=tuple(canonical_models),
            default_model_id=default_model_id,
            headers=dict(headers) if headers else None,
            extras=extras,
        )

    def write_canonical(self, service, canonical, record, *, agent="main", persist=False, dry_run=False):
        raise NotImplementedError("OpenClawAdapter.write_canonical (Task 7)")

    def provider_models(self, service, bundle: dict[str, object]) -> list[str]:
        return service._bundle_model_ids(dict(bundle.get("models", {})))
